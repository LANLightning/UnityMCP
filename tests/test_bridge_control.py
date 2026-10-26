"""Out-of-band bridge pairing, diagnosis, and recovery tests."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import anyio
import pytest

from unity_mcp_py.bridge import bridge_client
from unity_mcp_py.bridge import control as bridge_control
from unity_mcp_py.bridge import session as bridge_session
from unity_mcp_py.bridge.control import (
    BridgeBinding,
    BridgeBindingStore,
    BridgeControlClient,
    EndpointDiagnosis,
    EndpointSnapshot,
    diagnose_endpoint,
    parse_windows_tcp_rows,
    repair_endpoint,
)
from unity_mcp_py.bridge.session import (
    BridgeSession,
    close_current_session,
    get_session,
    set_session_for_test,
    switch_session,
)
from unity_mcp_py.tools import bind_bridge
from unity_mcp_py.tools import bridge_admin


class _RecordingClient:
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.result = result or {}
        self.error = error
        self.closed = False
        self.connected = False

    def connect(self, timeout_seconds: float) -> None:
        self.connected = True

    def handshake(self) -> dict:
        if self.error:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed = True


def test_binding_store_pairs_two_named_endpoints_and_clears_one(tmp_path: Path) -> None:
    store = BridgeBindingStore(tmp_path / "bindings.json")

    record = store.pair(
        client_id="codex-project_fixture",
        host="127.0.0.1",
        port=17874,
        expected_bridge_id="game-b",
    )

    assert record.client_id == "codex-project_fixture"
    assert record.port == 17874
    assert record.expected_bridge_id == "game-b"
    assert store.get("codex-project_fixture") == record
    assert store.clear("codex-project_fixture") is True
    assert store.get("codex-project_fixture") is None


def test_binding_store_rejects_invalid_or_non_loopback_endpoint(tmp_path: Path) -> None:
    store = BridgeBindingStore(tmp_path / "bindings.json")

    with pytest.raises(ValueError, match="client_id"):
        store.pair("", "127.0.0.1", 17873, None)
    with pytest.raises(ValueError, match="loopback"):
        store.pair("codex-project_fixture", "10.0.0.8", 17873, None)
    with pytest.raises(ValueError, match="port"):
        store.pair("codex-project_fixture", "127.0.0.1", 70000, None)
    with pytest.raises(ValueError, match="client_id"):
        store.pair("x" * 257, "127.0.0.1", 17873, "game-a")
    with pytest.raises(ValueError, match="expected_bridge_id"):
        store.pair("codex-project_fixture", "127.0.0.1", 17873, "x" * 257)


def test_binding_store_clear_if_current_rejects_a_replaced_binding(tmp_path: Path) -> None:
    store = BridgeBindingStore(tmp_path / "bindings.json")
    old = store.pair("codex-project_fixture", "127.0.0.1", 17873, "game-a")
    replacement = store.pair("codex-project_fixture", "127.0.0.1", 17874, "game-b")

    assert store.clear_if_current("codex-project_fixture", old.revision) is False
    assert store.get("codex-project_fixture") == replacement


@pytest.mark.asyncio
async def test_switch_session_is_atomic_when_new_handshake_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    old = BridgeSession(port=17873)
    old_client = _RecordingClient({"bridgeId": "old"})
    old._client = old_client  # type: ignore[assignment]
    old._handshake_done = True
    set_session_for_test(old)

    failing = BridgeSession(port=17874)
    failing_client = _RecordingClient(error=bridge_client.BridgeConnectionError("busy"))
    monkeypatch.setattr(failing, "_make_client", lambda: failing_client)

    with pytest.raises(bridge_client.BridgeConnectionError, match="busy"):
        await switch_session(failing)

    assert get_session() is old
    assert old_client.closed is False
    assert failing_client.closed is True


@pytest.mark.asyncio
async def test_live_pair_commits_session_binding_and_closes_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindings_path = tmp_path / "bindings.json"
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(bindings_path))
    old = BridgeSession(port=17873, client_id="codex-project_fixture")
    old_client = _RecordingClient({"bridgeId": "game-a"})
    old._client = old_client  # type: ignore[assignment]
    old._handshake_done = True
    old.bridge_id = "game-a"
    set_session_for_test(old)
    candidate_client = _RecordingClient({"bridgeId": "game-b"})
    monkeypatch.setattr(BridgeSession, "_make_client", lambda unused: candidate_client)

    try:
        result = await bridge_admin.run_bridge_admin(
            object(),
            action="pair",
            port=17874,
            client_id="codex-project_fixture",
            bridge_id="game-b",
        )

        current = get_session()
        binding = BridgeBindingStore(bindings_path).get("codex-project_fixture")
        assert result["success"] is True
        assert current is not old
        assert current.port == 17874
        assert current._client is candidate_client
        assert binding is not None
        assert (binding.port, binding.expected_bridge_id) == (17874, "game-b")
        assert old_client.closed is True
    finally:
        current = get_session()
        await current.close()
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_live_pair_handshake_failure_preserves_session_and_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindings_path = tmp_path / "bindings.json"
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(bindings_path))
    store = BridgeBindingStore(bindings_path)
    old_binding = store.pair("codex-project_fixture", "127.0.0.1", 17873, "game-a")
    old = BridgeSession(port=17873, client_id="codex-project_fixture")
    old_client = _RecordingClient({"bridgeId": "game-a"})
    old._client = old_client  # type: ignore[assignment]
    old._handshake_done = True
    old.bridge_id = "game-a"
    set_session_for_test(old)
    candidate_client = _RecordingClient(
        error=bridge_client.BridgeConnectionError("handshake rejected")
    )
    monkeypatch.setattr(BridgeSession, "_make_client", lambda unused: candidate_client)

    try:
        result = await bridge_admin.run_bridge_admin(
            object(),
            action="pair",
            port=17874,
            client_id="codex-project_fixture",
            bridge_id="game-b",
        )

        assert result["success"] is False
        assert get_session() is old
        assert store.get("codex-project_fixture") == old_binding
        assert old_client.closed is False
        assert candidate_client.closed is True
    finally:
        await old.close()
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_live_pair_commit_conflict_closes_candidate_and_rolls_back_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindings_path = tmp_path / "bindings.json"
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(bindings_path))
    store = BridgeBindingStore(bindings_path)
    old_binding = store.pair("codex-project_fixture", "127.0.0.1", 17873, "game-a")
    old = BridgeSession(port=17873, client_id="codex-project_fixture")
    old_client = _RecordingClient({"bridgeId": "game-a"})
    old._client = old_client  # type: ignore[assignment]
    old._handshake_done = True
    old.bridge_id = "game-a"
    intruder = BridgeSession(port=17875, client_id="codex-other")
    intruder_client = _RecordingClient({"bridgeId": "game-c"})
    intruder._client = intruder_client  # type: ignore[assignment]
    intruder._handshake_done = True
    intruder.bridge_id = "game-c"
    set_session_for_test(old)

    class ConflictingClient(_RecordingClient):
        def handshake(self) -> dict:
            set_session_for_test(intruder)
            return super().handshake()

    candidate_client = ConflictingClient({"bridgeId": "game-b"})
    monkeypatch.setattr(BridgeSession, "_make_client", lambda unused: candidate_client)

    try:
        result = await bridge_admin.run_bridge_admin(
            object(),
            action="pair",
            port=17874,
            client_id="codex-project_fixture",
            bridge_id="game-b",
        )

        assert result["success"] is False
        assert get_session() is intruder
        assert store.get("codex-project_fixture") == old_binding
        assert candidate_client.closed is True
        assert old_client.closed is False
    finally:
        await old.close()
        await intruder.close()
        set_session_for_test(None)
@pytest.mark.asyncio
async def test_switch_session_closes_old_only_after_new_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    old = BridgeSession(port=17873)
    old_client = _RecordingClient({"bridgeId": "old"})
    old._client = old_client  # type: ignore[assignment]
    old._handshake_done = True
    set_session_for_test(old)

    new = BridgeSession(port=17874)
    new_client = _RecordingClient({"bridgeId": "game-b"})
    monkeypatch.setattr(new, "_make_client", lambda: new_client)

    result = await switch_session(new)

    assert result["bridgeId"] == "game-b"
    assert get_session() is new
    assert old_client.closed is True


@pytest.mark.asyncio
async def test_clear_current_receiver_closes_socket_and_resets_singleton() -> None:
    session = BridgeSession(port=17873)
    client = _RecordingClient({"bridgeId": "game-a"})
    session._client = client  # type: ignore[assignment]
    session._handshake_done = True
    set_session_for_test(session)

    closed = await close_current_session(expected_port=17873)

    assert closed is True
    assert client.closed is True
    assert get_session() is not session


def test_parse_windows_tcp_rows_reports_listener_and_both_owners() -> None:
    rows = [
        {"LocalAddress": "127.0.0.1", "LocalPort": 17873, "RemoteAddress": "0.0.0.0", "RemotePort": 0, "State": "Listen", "OwningProcess": 4100, "ProcessExists": True},
        {"LocalAddress": "127.0.0.1", "LocalPort": 17873, "RemoteAddress": "127.0.0.1", "RemotePort": 52001, "State": "Established", "OwningProcess": 4100},
        {"LocalAddress": "127.0.0.1", "LocalPort": 52001, "RemoteAddress": "127.0.0.1", "RemotePort": 17873, "State": "Established", "OwningProcess": 9200},
    ]

    snapshot = parse_windows_tcp_rows(rows, host="127.0.0.1", port=17873)

    assert snapshot.listener_pid == 4100
    assert snapshot.listener_process_exists is True
    assert snapshot.listener_connections[0].owner_pid == 4100
    assert snapshot.client_connections[0].owner_pid == 9200


@pytest.mark.parametrize(
    ("snapshot", "control", "reason"),
    [
        (EndpointSnapshot("127.0.0.1", 17873), None, "listener_missing"),
        (EndpointSnapshot("127.0.0.1", 17873, listener_pid=10), {"responsive": True, "success": True, "clientConnected": False}, "healthy_idle"),
        (EndpointSnapshot("127.0.0.1", 17873, listener_pid=10), {"responsive": True, "success": True, "clientConnected": True, "clientId": "codex-project_fixture"}, "occupied"),
        (EndpointSnapshot("127.0.0.1", 17873, listener_pid=10, listener_process_exists=True, listener_process_name="Unity"), {"responsive": False, "error": "timeout"}, "bridge_main_thread_unresponsive"),
        (EndpointSnapshot("127.0.0.1", 17873, listener_pid=10, listener_process_exists=False), {"responsive": False, "error": "timeout", "wedgeActive": True}, "orphan_listener"),
        (EndpointSnapshot("127.0.0.1", 17873, listener_pid=10, listener_process_exists=None), {"responsive": False, "error": "timeout"}, "listener_owner_unknown"),
        (EndpointSnapshot("127.0.0.1", 17873, listener_pid=10, listener_process_exists=False), {"responsive": True, "success": True, "clientConnected": False, "listenerPid": 11}, "healthy_idle"),
    ],
)
def test_diagnose_endpoint_classifies_stuck_reasons(
    snapshot: EndpointSnapshot,
    control: dict | None,
    reason: str,
) -> None:
    diagnosis = diagnose_endpoint(snapshot, control)
    assert diagnosis.reason == reason


def test_repair_orphan_listener_recommends_fallback_without_restarting_unity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnosis = EndpointDiagnosis(
        "orphan_listener",
        "Windows still reports a listener for an exited process.",
        False,
        {"socket": {"listener_pid": 36412}, "control": {}},
    )
    monkeypatch.setattr(bridge_control, "diagnose_live_endpoint", lambda *unused: diagnosis)

    result = repair_endpoint("127.0.0.1", 17873)

    assert result["repaired"] is False
    assert result["manualRestartRequired"] is False
    assert result["fallbackScanRecommended"] is True
    assert result["recommendedPorts"][0] == 17874


def test_repair_unknown_listener_owner_takes_no_destructive_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnosis = EndpointDiagnosis(
        "listener_owner_unknown",
        "The owner could not be verified.",
        False,
        {"socket": {"listener_pid": 10}, "control": {}},
    )
    monkeypatch.setattr(bridge_control, "diagnose_live_endpoint", lambda *unused: diagnosis)

    result = repair_endpoint("127.0.0.1", 17873)

    assert result["repaired"] is False
    assert result["changed"] is False
    assert result["manualRestartRequired"] is False
    assert result["refused"] == "listener_owner_unknown"
    assert result["actions"] == []




@pytest.mark.asyncio
async def test_unbind_uses_revision_cas_and_does_not_close_replaced_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Binding:
        port = 17874
        revision = "old-revision"

    class FakeStore:
        def clear_if_current(self, client_id: str, revision: str) -> bool:
            assert client_id == "codex-project_fixture"
            assert revision == "old-revision"
            return False

    closed = False

    async def fake_close(**unused: object) -> bool:
        nonlocal closed
        closed = True
        return True

    monkeypatch.setattr(bind_bridge, "_get_session_id", lambda unused: "codex-project_fixture")
    monkeypatch.setattr(bind_bridge, "BridgeBindingStore", FakeStore)
    monkeypatch.setattr(bind_bridge, "_load_or_migrate_binding", lambda *unused: Binding())
    monkeypatch.setattr(bind_bridge, "close_current_session", fake_close)

    result = await bind_bridge._do_unbind_bridge(object())

    assert result["success"] is False
    assert result["errorCode"] == "BINDING_CHANGED"
    assert closed is False
def test_control_client_status_and_disconnect_use_out_of_band_control(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []
    client = BridgeControlClient("127.0.0.1", 17873, auth_token="x" * 32)

    def fake_request(action: str, args: dict | None = None) -> dict:
        calls.append((action, args or {}))
        return {
            "responsive": True,
            "success": True,
            "action": action,
            "bridgeId": "game-a",
            "bridgeInstanceId": "bridge-process-1",
        }

    monkeypatch.setattr(client, "request", fake_request)

    assert client.status()["action"] == "status"
    assert client.clear_listener(expected_bridge_id="game-a")["action"] == "restart_listener"
    assert client.disconnect_receiver(
        expected_bridge_id="game-a",
        expected_bridge_instance_id="bridge-process-1",
        expected_generation=7,
        expected_client_id="codex-project_fixture",
        expected_client_instance_id="instance-1",
    )["action"] == "disconnect_client"
    assert calls == [
        ("status", {}),
        ("status", {}),
        (
            "restart_listener",
            {
                "expectedBridgeId": "game-a",
                "expectedBridgeInstanceId": "bridge-process-1",
                "expectedConnectionGeneration": None,
            },
        ),
        (
            "disconnect_client",
            {
                "expectedBridgeId": "game-a",
                "expectedBridgeInstanceId": "bridge-process-1",
                "expectedConnectionGeneration": 7,
                "expectedClientId": "codex-project_fixture",
                "expectedClientInstanceId": "instance-1",
            },
        ),
    ]


def test_handshake_sends_pair_identity_and_rejects_wrong_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    client = bridge_client.BridgeClient(
        client_id="codex-project_fixture",
        client_instance_id="process-generation-2",
        expected_bridge_id="game-a",
        force_takeover=True,
        auth_token="x" * 32,
    )
    sent: dict[str, object] = {}

    def fake_send(command: str, args: dict, response_timeout: float = 30.0) -> dict:
        sent.update({"command": command, "args": args})
        return {"bridgeId": "game-a"}

    monkeypatch.setattr(client, "send", fake_send)
    assert client.handshake()["bridgeId"] == "game-a"
    assert sent["command"] == "Handshake"
    args = sent["args"]
    assert isinstance(args, dict)
    assert args["clientId"] == "codex-project_fixture"
    assert args["clientInstanceId"] == "process-generation-2"
    assert args["expectedBridgeId"] == "game-a"
    assert args["forceTakeover"] is True
    assert "authToken" not in args

    monkeypatch.setattr(client, "send", lambda *unused_args, **unused_kwargs: {"bridgeId": "game-b"})
    with pytest.raises(bridge_client.BridgeProtocolError, match="identity mismatch"):
        client.handshake()


@pytest.mark.asyncio
async def test_bind_force_takeover_uses_status_generation_cas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, object] = {}

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)

        async def connect(self) -> dict[str, object]:
            return {"bridgeId": "game-a"}

        async def close(self) -> None:
            return None

    async def fake_commit(candidate: object, *, expected_previous: object) -> None:
        created["committed"] = candidate

    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(tmp_path / "bindings.json"))
    monkeypatch.setattr(bind_bridge, "_get_session_id", lambda unused: "codex-project_fixture")
    monkeypatch.setattr(bind_bridge, "_load_or_migrate_binding", lambda *unused: None)
    monkeypatch.setattr(bind_bridge, "BridgeSession", FakeSession)
    monkeypatch.setattr(bind_bridge, "commit_connected_session", fake_commit)
    monkeypatch.setattr(
        bind_bridge.BridgeControlClient,
        "status",
        lambda self: {
            "success": True,
            "bridgeId": "game-a",
            "bridgeInstanceId": "bridge-process-3",
            "clientId": "codex-project_fixture",
            "connectionGeneration": 9,
        },
    )

    result = await bind_bridge._do_bind_bridge(
        object(),
        port=17873,
        expected_bridge_id="game-a",
        force_takeover=True,
    )

    assert result["success"] is True
    assert created["expected_bridge_instance_id"] == "bridge-process-3"
    assert created["expected_connection_generation"] == 9


def test_unity_server_accepts_control_connections_while_primary_is_connected() -> None:
    source = Path(
        "unity-bridge/com.local.unitymcp-bridge/Editor/UnityMCPServer.cs"
    ).read_text(encoding="utf-8")

    assert "AcceptPendingConnections();" in source
    assert "ProcessControlClients();" in source
    assert 'case "status"' in source
    assert 'case "disconnect_client"' in source
    assert 'case "restart_listener"' in source


@pytest.mark.asyncio
async def test_bind_reuses_connected_session_when_transport_client_id_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = r"<PROJECT_ROOT>"
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(tmp_path / "bindings.json"))
    monkeypatch.setattr(bind_bridge, "_get_session_id", lambda unused: "temp-38236")

    active = BridgeSession(port=17874, client_id="mcp-38236")
    active._client = object()  # type: ignore[assignment]
    active._handshake_done = True
    active.bridge_id = project
    set_session_for_test(active)

    class UnexpectedSession:
        def __init__(self, **unused: object) -> None:
            raise AssertionError("bind attempted a second transport handshake")

    monkeypatch.setattr(bind_bridge, "BridgeSession", UnexpectedSession)
    try:
        result = await bind_bridge._do_bind_bridge(
            object(), port=17874, expected_bridge_id=project
        )

        assert result["success"] is True
        assert result["already_connected"] is True
        assert result["session_id"] == "temp-38236"
        stored = BridgeBindingStore(tmp_path / "bindings.json").get("temp-38236")
        assert stored is not None
        assert stored.port == 17874
        assert stored.expected_bridge_id == project
    finally:
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_bind_rolls_back_if_active_session_changes_while_pairing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = r"<PROJECT_ROOT>"
    bindings_path = tmp_path / "bindings.json"
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(bindings_path))
    monkeypatch.setattr(bind_bridge, "_get_session_id", lambda unused: "temp-38236")

    active = BridgeSession(port=17874, client_id="mcp-38236")
    active._client = object()  # type: ignore[assignment]
    active._handshake_done = True
    active.bridge_id = project
    set_session_for_test(active)

    async def changed(unused: object) -> bool:
        return False

    monkeypatch.setattr(bind_bridge, "is_current_session", changed)
    try:
        result = await bind_bridge._do_bind_bridge(
            object(), port=17874, expected_bridge_id=project
        )

        assert result["success"] is False
        assert result["errorCode"] == "SESSION_CHANGED"
        assert BridgeBindingStore(bindings_path).get("temp-38236") is None
    finally:
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_close_current_session_requires_exact_session_generation() -> None:
    old = BridgeSession(port=17874, client_id="codex-project_fixture")
    replacement = BridgeSession(port=17874, client_id="codex-project_fixture")
    replacement._client = object()  # type: ignore[assignment]
    replacement._handshake_done = True
    set_session_for_test(replacement)

    try:
        closed = await close_current_session(
            expected_session=old,
            expected_port=17874,
            expected_client_id="codex-project_fixture",
        )

        assert closed is False
        assert replacement._client is not None
        assert replacement._handshake_done is True
    finally:
        # Avoid invoking BridgeClient.close on the simple object test double.
        replacement._client = None
        replacement._handshake_done = False
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_async_initialization_is_closed_before_unbind_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    candidate = BridgeSession(port=17874, client_id="codex-project_fixture")

    def delayed_initialization() -> BridgeSession:
        started.set()
        assert release.wait(2)
        return candidate

    monkeypatch.setattr(
        bridge_session, "_session_from_binding_or_environment", delayed_initialization
    )
    set_session_for_test(None)
    initialize_task = asyncio.create_task(bridge_session.get_session_async())
    assert await asyncio.to_thread(started.wait, 1)
    close_task = asyncio.create_task(close_current_session())
    await asyncio.sleep(0)
    assert close_task.done() is False

    release.set()
    try:
        assert await initialize_task is candidate
        assert await close_task is True
        assert await bridge_session.is_current_session(candidate) is False
    finally:
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_cancelled_binding_mutation_finishes_and_rolls_back() -> None:
    started = threading.Event()
    release = threading.Event()
    rolled_back: list[str] = []

    class Written:
        revision = "new-revision"

    def mutate() -> Written:
        started.set()
        assert release.wait(2)
        return Written()

    def rollback(written: Written) -> None:
        rolled_back.append(written.revision)

    task = asyncio.create_task(
        bind_bridge._run_binding_mutation(
            mutate, rollback_on_cancel=rollback
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert rolled_back == ["new-revision"]


@pytest.mark.asyncio
async def test_restore_rejects_binding_revision_changed_during_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = BridgeBinding(
        client_id="codex-project_fixture",
        host="127.0.0.1",
        port=17874,
        expected_bridge_id="game-a",
        revision="old-revision",
    )
    aborted = False

    class FakeStore:
        def get(self, client_id: str) -> BridgeBinding | None:
            assert client_id == "codex-project_fixture"
            return binding

        def matches_revision(self, client_id: str, revision: str) -> bool:
            assert client_id == "codex-project_fixture"
            assert revision == "old-revision"
            return False

    class FakeSession:
        def __init__(self, **unused: object) -> None:
            pass

        async def connect(self) -> dict[str, object]:
            return {"bridgeId": "game-a"}

        async def abort(self) -> None:
            nonlocal aborted
            aborted = True

    previous = BridgeSession(port=17873, client_id="codex-project_fixture")
    set_session_for_test(previous)
    monkeypatch.setattr(bind_bridge, "_get_session_id", lambda unused: "codex-project_fixture")
    monkeypatch.setattr(bind_bridge, "BridgeBindingStore", FakeStore)
    monkeypatch.setattr(bind_bridge, "BridgeSession", FakeSession)
    try:
        result = await bind_bridge._do_restore_binding(object())
        assert result["success"] is False
        assert result["errorCode"] == "BINDING_CHANGED"
        assert aborted is True
    finally:
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_session_switch_aborts_old_socket_without_waiting_for_send_lock() -> None:
    closed = threading.Event()

    class FakeClient:
        def close(self) -> None:
            closed.set()

    old = BridgeSession(port=17873, client_id="codex-project_fixture")
    old._client = FakeClient()  # type: ignore[assignment]
    old._handshake_done = True
    candidate = BridgeSession(port=17874, client_id="codex-project_fixture")
    candidate._client = object()  # type: ignore[assignment]
    candidate._handshake_done = True
    set_session_for_test(old)
    await old._send_lock.acquire()
    try:
        await asyncio.wait_for(
            bridge_session.commit_connected_session(
                candidate, expected_previous=old
            ),
            timeout=1,
        )
        assert closed.is_set()
    finally:
        old._send_lock.release()
        candidate._client = None
        candidate._handshake_done = False
        set_session_for_test(None)


def test_binding_store_revision_and_restore_if_absent_use_real_store(
    tmp_path: Path,
) -> None:
    store = BridgeBindingStore(tmp_path / "bindings.json")
    original = store.pair("codex-project_fixture", "127.0.0.1", 17874, "game-a")

    assert store.matches_revision("codex-project_fixture", original.revision) is True
    assert store.clear_if_current("codex-project_fixture", original.revision) is True
    assert store.restore_if_absent("codex-project_fixture", original) is True
    assert store.matches_revision("codex-project_fixture", original.revision) is True

    newer = store.pair("codex-project_fixture", "127.0.0.1", 17875, "game-b")
    assert store.restore_if_absent("codex-project_fixture", original) is False
    assert store.matches_revision("codex-project_fixture", newer.revision) is True


@pytest.mark.asyncio
async def test_unbind_cancelled_after_clear_restores_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = BridgeBindingStore(tmp_path / "bindings.json")
    binding = store.pair("codex-project_fixture", "127.0.0.1", 17874, "game-a")
    active = BridgeSession(port=17874, client_id="codex-project_fixture")

    async def cancelled_close(**unused: object) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(bind_bridge, "close_current_session", cancelled_close)
    set_session_for_test(active)
    try:
        with pytest.raises(asyncio.CancelledError):
            await bind_bridge._clear_binding_and_disconnect(
                store, "codex-project_fixture", binding, active
            )

        restored = store.get("codex-project_fixture")
        assert restored is not None
        assert restored.revision == binding.revision
    finally:
        set_session_for_test(None)


@pytest.mark.asyncio
async def test_send_cancellation_uses_stable_client_after_concurrent_abort() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def __init__(self) -> None:
            self.close_count = 0

        def send(
            self,
            command: str,
            args: dict[str, object],
            response_timeout: float,
        ) -> dict[str, object]:
            started.set()
            assert release.wait(2)
            raise RuntimeError("socket closed")

        def close(self) -> None:
            self.close_count += 1
            # abort closes once; the send cancellation path closes the same local
            # reference a second time and thereby releases the worker.
            if self.close_count >= 2:
                release.set()

    client = BlockingClient()
    session = BridgeSession(port=17874, client_id="codex-project_fixture")
    session._client = client  # type: ignore[assignment]
    session._handshake_done = True
    send_task = asyncio.create_task(session.send("Ping", {}))
    assert await asyncio.to_thread(started.wait, 1)

    await session.abort()
    assert session._client is None
    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task
    assert client.close_count == 2


@pytest.mark.asyncio
async def test_anyio_cancel_scope_waits_for_binding_rollback() -> None:
    started = threading.Event()
    release = threading.Event()
    rolled_back: list[str] = []

    class Written:
        revision = "anyio-revision"

    def mutate() -> Written:
        started.set()
        assert release.wait(2)
        return Written()

    def rollback(written: Written) -> None:
        rolled_back.append(written.revision)

    async def operation() -> None:
        try:
            await bind_bridge._run_binding_mutation(
                mutate, rollback_on_cancel=rollback
            )
        except asyncio.CancelledError:
            pass

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(operation)
        assert await asyncio.to_thread(started.wait, 1)
        task_group.cancel_scope.cancel()
        release.set()

    assert rolled_back == ["anyio-revision"]
