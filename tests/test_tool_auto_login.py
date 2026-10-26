"""Tests for the auto_login tool (MCP-driven game login)."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.auto_login import (
    InvalidAutoLoginArg,
    _resolve_credentials,
    _build_kick_script,
    auto_login,
)


def test_resolve_prefers_args_over_env(monkeypatch):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "envuser")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    creds = _resolve_credentials("arguser", 200, None)
    assert creds == ("arguser", 200, None)


def test_resolve_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "envuser")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SCENE_ID", "5")
    creds = _resolve_credentials(None, None, None)
    assert creds == ("envuser", 100, 5)


def test_resolve_missing_username_raises(monkeypatch):
    monkeypatch.delenv("UNITY_MCP_LOGIN_USERNAME", raising=False)
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    with pytest.raises(InvalidAutoLoginArg, match="username"):
        _resolve_credentials(None, 100, None)


def test_resolve_missing_server_id_raises(monkeypatch):
    monkeypatch.delenv("UNITY_MCP_LOGIN_SERVER_ID", raising=False)
    with pytest.raises(InvalidAutoLoginArg, match="server_id"):
        _resolve_credentials("u", None, None)


def test_resolve_non_int_server_id_raises(monkeypatch):
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "notanint")
    with pytest.raises(InvalidAutoLoginArg, match="server_id"):
        _resolve_credentials("u", None, None)


@pytest.mark.parametrize("bad", ["bad user", "drop;table", 'a"b', "back\\slash", ""])
def test_resolve_rejects_illegal_username(bad):
    with pytest.raises(InvalidAutoLoginArg, match="username"):
        _resolve_credentials(bad, 100, None)


def test_build_kick_script_includes_scene_when_present():
    script = _build_kick_script("alice", 100, 7)
    assert 'PROJECT_CLIENT_STATE.UserName = "alice"' in script
    assert "PROJECT_CLIENT_STATE.ServerId = 100" in script
    assert "a.PROJECT_DEFAULT_SCENE = 7" in script
    assert "a:PROJECT_TEST_LOGIN()" in script
    assert "a.token = nil" in script


def test_build_kick_script_omits_scene_when_absent():
    script = _build_kick_script("alice", 100, None)
    assert "PROJECT_DEFAULT_SCENE" not in script
    assert "a:PROJECT_TEST_LOGIN()" in script


@pytest.mark.asyncio
async def test_not_in_playmode_returns_error(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_error(
        "INVALID_STATE", "lua_vm_not_running: PROJECT_FACADE.PROJECT_LUA_MANAGER is null"
    )

    result = await auto_login()

    assert result["accepted"] is False
    assert result["error"] == "not_in_playmode"
    assert "remedy" in result
    # Pure read probe issues no write-op token.
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "auto_login" for p in pending)


@pytest.mark.asyncio
async def test_login_agent_unavailable_returns_error(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "noagent"})

    result = await auto_login()

    assert result["accepted"] is False
    assert result["error"] == "login_agent_unavailable"


@pytest.mark.asyncio
async def test_already_logged_in_short_circuits(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "player"})

    result = await auto_login()

    assert result["accepted"] is True
    assert result["loggedIn"] is True
    assert result["note"] == "already_logged_in"
    assert result["elapsedMs"] == 0
    assert result["write_op_token"] is None
    assert result["requires_observation"] is False
    # No kick happened -> no token.
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "auto_login" for p in pending)


@pytest.mark.asyncio
async def test_validation_error_before_any_bridge_traffic(monkeypatch):
    monkeypatch.delenv("UNITY_MCP_LOGIN_USERNAME", raising=False)
    monkeypatch.delenv("UNITY_MCP_LOGIN_SERVER_ID", raising=False)
    # No bridge session installed: if it reached the bridge it would error
    # differently. Missing creds must raise first.
    with pytest.raises(InvalidAutoLoginArg):
        await auto_login()


@pytest.mark.asyncio
async def test_unexpected_bridge_error_is_not_masked(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    """A non-INVALID_STATE bridge error (e.g. script defect) must surface, not be
    relabeled as not_in_playmode."""
    from unity_mcp_py.bridge import bridge_client

    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_error("SCRIPT_ERROR", "lua syntax error")

    with pytest.raises(bridge_client.BridgeError, match="SCRIPT_ERROR"):
        await auto_login()


@pytest.mark.asyncio
async def test_kick_then_accepted_issues_one_token(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    # Probe -> ready
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "ready"})
    # Kick is a write_op: TailEditorLog then LuaEval
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 42}]})
    bridge.expect_request(
        "LuaEval",
        args_predicate=lambda a: "PROJECT_TEST_LOGIN" in a.get("script", ""),
    ).respond_with({"ok": True, "raw": "True"})
    # First poll -> accepted
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "accepted"})

    result = await auto_login(poll_interval_s=0)

    assert result["accepted"] is True
    assert result["finalState"] == "accepted"
    assert result["username"] == "alice"
    assert result["serverId"] == 100
    assert "elapsedMs" in result
    assert "write_op_token" in result

    # Exactly one pending token from the kick.
    pending = await GUARD.list_pending()
    mine = [p for p in pending if p.tool_name == "auto_login"]
    assert len(mine) == 1
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_poll_reaches_player_state(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "ready"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "True"})
    # pending, then player
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "pending"})
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "player"})

    result = await auto_login(poll_interval_s=0)

    assert result["accepted"] is True
    assert result["loggedIn"] is True
    assert result["finalState"] == "player"
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_timeout_when_poll_stays_pending(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "ready"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "True"})
    # Exactly two pending polls, then the monotonic clock crosses the deadline.
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "pending"})
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "pending"})

    # Deterministic clock via the module-local _now alias (not time.monotonic directly,
    # to avoid also patching bridge_client.py which imports the same time module).
    # Call sites of _now() in auto_login.py (Fix A loop):
    #   #1  start = _now()                          → 0.0  (deadline = 0.0 + 1.0 = 1.0)
    #   #2  if _now() >= deadline  (after poll1)    → 0.0  (< 1.0: keep going, sleep)
    #   #3  if _now() >= deadline  (after poll2)    → 10.0 (>= 1.0: break)
    #   #4  elapsed_ms = int((_now() - start)*1000) → 10.0
    times = iter([0.0, 0.0, 10.0, 10.0])
    monkeypatch.setattr(
        "unity_mcp_py.tools.auto_login._now",
        lambda: next(times),
    )

    result = await auto_login(timeout_s=1.0, poll_interval_s=0)

    assert result["accepted"] is False
    assert result["error"] == "login_timeout"
    assert "remedy" in result
    assert result["finalState"] == "pending"
    assert result["loggedIn"] is False
    # The kick still issued a token (the write happened); consume it.
    await GUARD.consume([result["write_op_token"]])


def test_auto_login_module_has_register():
    """The module exposes register(mcp) so server_tools() can wire it."""
    from unity_mcp_py.tools import auto_login as mod
    assert hasattr(mod, "register")
    assert callable(mod.register)


def test_auto_login_registers_with_a_fake_mcp():
    """register(mcp) calls mcp.tool('auto_login') and registers a callable."""
    from unity_mcp_py.tools import auto_login as mod

    registered = {}

    class _FakeMcp:
        def tool(self, name):
            def deco(fn):
                registered[name] = fn
                return fn
            return deco

    mod.register(_FakeMcp())
    assert "auto_login" in registered
    assert callable(registered["auto_login"])


def test_auto_login_is_registered_in_server():
    """auto_login is wired into register_tools()'s import + module list."""
    import inspect
    from unity_mcp_py import server

    src = inspect.getsource(server.register_tools)
    assert "auto_login" in src


@pytest.mark.asyncio
async def test_poll_phase_bridge_error_returns_token(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    """If the bridge errors DURING polling (after the kick fired), the issued
    write-op token must still be returned so the caller can consume it — not leak."""
    monkeypatch.setenv("UNITY_MCP_LOGIN_USERNAME", "alice")
    monkeypatch.setenv("UNITY_MCP_LOGIN_SERVER_ID", "100")
    bridge = mock_bridge_server
    # Probe -> ready
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "ready"})
    # Kick (write_op): TailEditorLog then LuaEval
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 9}]})
    bridge.expect_request("LuaEval").respond_with({"ok": True, "raw": "True"})
    # First poll -> bridge error (e.g. Unity dropped PlayMode mid-session)
    bridge.expect_request("LuaEval").respond_error("INVALID_STATE", "lua_vm_not_running")

    result = await auto_login(poll_interval_s=0)

    assert result["accepted"] is False
    assert result["error"] == "poll_bridge_error"
    assert "detail" in result
    # The token from the kick MUST be returned (not leaked) and require observation.
    assert result["write_op_token"] is not None
    assert result["requires_observation"] is True
    # Caller can consume it -> no leak.
    await GUARD.consume([result["write_op_token"]])
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "auto_login" for p in pending)
