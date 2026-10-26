"""Administrative failure contracts: identity guards, CAS, and structured errors."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from unity_mcp_py.bridge import cli
from unity_mcp_py.bridge.control import BridgeBindingStore, EndpointSnapshot
from unity_mcp_py.tools import bridge_admin


@pytest.fixture(autouse=True)
def isolated_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITY_MCP_AUTH_TOKEN", "test-only-token-" * 4)
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(tmp_path / "bindings.json"))


@pytest.mark.parametrize("kwargs,code", [
    ({"action": "pair"}, "EXPECTED_ID_REQUIRED"),
    ({"action": "clear", "side": "binding"}, "CLIENT_ID_REQUIRED"),
    ({"action": "clear", "side": "accepted", "port": None}, "PORT_REQUIRED"),
    ({"action": "clear", "side": "accepted"}, "EXPECTED_ID_REQUIRED"),
])
def test_admin_requires_explicit_destructive_identity(kwargs, code):
    result = cli.run_admin_action(**kwargs)
    assert result["errorCode"] == code
    assert result["exitCode"] == 2
    assert result["success"] is False


@pytest.mark.parametrize("kwargs,message", [
    ({"action": "status", "force": "yes"}, "force must be a boolean"),
    ({"action": "unsupported"}, "unsupported bridge admin action"),
    ({"action": "clear", "side": "all"}, "clear requires"),
])
def test_programmatic_api_rejects_invalid_arguments(kwargs, message):
    with pytest.raises(ValueError, match=message):
        cli.run_admin_action(**kwargs)


def test_cli_value_error_is_json_not_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_dispatch", Mock(side_effect=ValueError("invalid endpoint")))
    assert cli.main(["owners", "--port", "17873"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["errorCode"] == "INVALID_ARGUMENT"
    assert result["error"] == "invalid endpoint"


def test_owners_reports_snapshot_without_control_connection(monkeypatch):
    monkeypatch.setattr(cli, "inspect_endpoint", lambda host, port: EndpointSnapshot(host, port, listener_pid=123))
    client = Mock(side_effect=AssertionError("owners must not acquire a control connection"))
    monkeypatch.setattr(cli, "BridgeControlClient", client)
    result = cli.run_admin_action("owners")
    assert result["socket"]["listener_pid"] == 123
    assert result["exitCode"] == 0
    client.assert_not_called()


def test_diagnose_merges_wedge_evidence_on_unhealthy_control(monkeypatch):
    monkeypatch.setattr(cli, "inspect_endpoint", lambda host, port: EndpointSnapshot(host, port, listener_pid=123))
    control = Mock()
    control.status.return_value = {"success": False, "responsive": False}
    monkeypatch.setattr(cli, "BridgeControlClient", Mock(return_value=control))
    monkeypatch.setattr(cli, "read_wedge_evidence", lambda port, pid: {"wedgeReason": "stalled"})
    result = cli.run_admin_action("diagnose")
    assert result["exitCode"] == 0
    assert result["diagnosis"]["reason"] == "unity_main_thread_wedged"
    assert result["diagnosis"]["evidence"]["control"]["wedgeActive"] is True


def test_clear_absent_binding_is_idempotent():
    result = cli.run_admin_action("clear", side="binding", client_id="absent")
    assert result["success"] is True
    assert result["changed"] is False


def test_clear_binding_refuses_changed_bridge_and_preserves_record():
    store = BridgeBindingStore()
    original = store.pair("agent", "127.0.0.1", 17873, "bridge-a")
    result = cli.run_admin_action("clear", side="binding", client_id="agent", bridge_id="bridge-b")
    assert result["errorCode"] == "STALE_TARGET"
    assert store.get("agent") == original


def test_clear_binding_revision_race_preserves_newer_binding(monkeypatch):
    store = BridgeBindingStore()
    store.pair("agent", "127.0.0.1", 17873, "bridge-a")
    real_clear = store.clear_if_current

    def raced_clear(client_id, revision):
        store.pair(client_id, "127.0.0.1", 17874, "bridge-b")
        return real_clear(client_id, revision)

    monkeypatch.setattr(store, "clear_if_current", raced_clear)
    monkeypatch.setattr(cli, "_store", lambda: store)
    result = cli.run_admin_action("clear", side="binding", client_id="agent")
    assert result["errorCode"] == "STALE_TARGET"
    assert store.get("agent").expected_bridge_id == "bridge-b"


def test_clear_binding_with_current_revision_succeeds():
    store = BridgeBindingStore()
    store.pair("agent", "127.0.0.1", 17873, "bridge-a")
    result = cli.run_admin_action("clear", side="binding", client_id="agent", bridge_id="bridge-a")
    assert result["changed"] is True
    assert result["exitCode"] == 0
    assert store.get("agent") is None


@pytest.mark.parametrize("status,error_code", [
    ({"success": False, "errorCode": "UNREACHABLE"}, "UNREACHABLE"),
    ({"success": True, "bridgeId": "other", "clientId": "agent"}, "STALE_TARGET"),
    ({"success": True, "bridgeId": "bridge-a", "clientId": "other"}, "STALE_TARGET"),
])
def test_clear_accepted_never_disconnects_unverified_owner(monkeypatch, status, error_code):
    control = Mock()
    control.status.return_value = status
    monkeypatch.setattr(cli, "BridgeControlClient", Mock(return_value=control))
    result = cli.run_admin_action("clear", side="accepted", client_id="agent", bridge_id="bridge-a")
    assert result["errorCode"] == error_code
    assert result["exitCode"] != 0
    control.disconnect_receiver.assert_not_called()


def test_clear_accepted_propagates_all_generation_guards(monkeypatch):
    control = Mock()
    control.status.return_value = {
        "success": True, "bridgeId": "bridge-a", "clientId": "agent",
        "bridgeInstanceId": "instance-a", "connectionGeneration": 7,
        "clientInstanceId": "client-instance-a",
    }
    control.disconnect_receiver.return_value = {"success": True}
    monkeypatch.setattr(cli, "BridgeControlClient", Mock(return_value=control))
    result = cli.run_admin_action("clear", side="accepted", client_id="agent", bridge_id="bridge-a")
    assert result["exitCode"] == 0
    control.disconnect_receiver.assert_called_once_with(
        expected_bridge_id="bridge-a", expected_bridge_instance_id="instance-a",
        expected_generation=7, expected_client_id="agent",
        expected_client_instance_id="client-instance-a",
    )


@pytest.mark.parametrize("success", [True, False])
def test_listener_clear_preserves_failure_exit_code(monkeypatch, success):
    control = Mock()
    control.clear_listener.return_value = {"success": success}
    monkeypatch.setattr(cli, "BridgeControlClient", Mock(return_value=control))
    result = cli.run_admin_action("clear", side="listener", bridge_id="bridge-a")
    assert result["exitCode"] == (0 if success else 1)
    control.clear_listener.assert_called_once_with("bridge-a")


@pytest.mark.parametrize("repaired", [True, False])
def test_repair_forwards_target_identity_and_result(monkeypatch, repaired):
    repair = Mock(return_value={"repaired": repaired})
    monkeypatch.setattr(cli, "repair_endpoint", repair)
    result = cli.run_admin_action("repair", client_id="agent", bridge_id="bridge-a", force=True)
    assert result["exitCode"] == (0 if repaired else 1)
    repair.assert_called_once_with("127.0.0.1", 17873, client_id="agent", expected_bridge_id="bridge-a", force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"client_id": "agent", "bridge_id": "bridge-a", "host": "::1"}])
async def test_live_pair_rejects_missing_identity_or_unsupported_host(monkeypatch, kwargs):
    bind = AsyncMock()
    monkeypatch.setattr(bridge_admin, "_do_bind_bridge", bind)
    result = await bridge_admin.run_bridge_admin(None, action="pair", **kwargs)
    assert result["errorCode"] == "INVALID_ARGUMENT"
    assert result["exitCode"] == 2
    bind.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code,exit_code", [
    (ValueError("bad argument"), "INVALID_ARGUMENT", 2),
    (OSError("transport unavailable"), "ADMIN_OPERATION_FAILED", 1),
    (RuntimeError("operation failed"), "ADMIN_OPERATION_FAILED", 1),
])
async def test_mcp_admin_returns_structured_failures(monkeypatch, error, code, exit_code):
    monkeypatch.setattr(bridge_admin, "run_admin_action", Mock(side_effect=error))
    result = await bridge_admin.run_bridge_admin(None, action="status")
    assert result == {"success": False, "errorCode": code, "error": str(error), "exitCode": exit_code}


@pytest.mark.asyncio
async def test_registered_admin_wrapper_forwards_parameters(monkeypatch):
    registered = {}

    def tool(*, name):
        def decorator(fn):
            registered[name] = fn
            return fn
        return decorator

    run = AsyncMock(return_value={"success": True, "exitCode": 0})
    monkeypatch.setattr(bridge_admin, "run_bridge_admin", run)
    bridge_admin.register(SimpleNamespace(tool=tool))
    ctx = object()
    result = await registered["bridge_admin"](ctx, "status", port=17874, client_id="agent")
    assert result["success"] is True
    run.assert_awaited_once_with(ctx, action="status", port=17874, host="127.0.0.1", client_id="agent", bridge_id=None, side=None, force=False)
