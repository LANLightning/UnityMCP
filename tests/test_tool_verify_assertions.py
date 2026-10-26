"""Feature 1 — verify_assertions: server-side pass/fail verdict, no model in loop.

Evaluates declarative checks entirely in Python against bridge reads and returns
a compact {passed, failures, results} verdict (~tens of tokens) so the recurring
"is it still correct?" check never round-trips through the model.

Assertion types:
  - expect_no_errors:  no Error/Exception/Assert in the log tail
  - log_matches:       a log line matches (or, with absent=True, must NOT match)
  - play_state:        GetPlayState equals an expected value
  - node_exists:       ListUINodes has a node by name/path
  - node_visible:      ListUINodes has a node by name/path that is visible
  - lua_expr:          LuaEval of an expression equals an expected stringified value
"""
from __future__ import annotations

import pytest

from unity_mcp_py.state import LUA_BUDGET
from unity_mcp_py.tools.verify_assertions import (
    verify_assertions,
    UnknownAssertionError,
)


@pytest.fixture(autouse=True)
async def _reset_lua_budget():
    await LUA_BUDGET.reset()
    yield
    await LUA_BUDGET.reset()


# ---- expect_no_errors -------------------------------------------------------

@pytest.mark.asyncio
async def test_expect_no_errors_passes_on_clean_log(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 1, "logType": "Log", "message": "fine", "stackTrace": ""},
        {"lineNumber": 2, "logType": "Warning", "message": "meh", "stackTrace": ""},
    ]})

    result = await verify_assertions([{"type": "expect_no_errors"}])

    assert result["passed"] is True
    assert result["failures"] == []


@pytest.mark.asyncio
async def test_expect_no_errors_fails_on_error_line(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 5, "logType": "Error", "message": "NullRef", "stackTrace": "at X"},
    ]})

    result = await verify_assertions([{"type": "expect_no_errors"}])

    assert result["passed"] is False
    assert len(result["failures"]) == 1
    assert "NullRef" in result["failures"][0]["detail"]


# ---- log_matches ------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_matches_passes_when_pattern_present(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 1, "logType": "Log", "message": "quest 42 completed", "stackTrace": ""},
    ]})

    result = await verify_assertions([
        {"type": "log_matches", "pattern": r"quest \d+ completed"}
    ])

    assert result["passed"] is True


@pytest.mark.asyncio
async def test_log_matches_absent_fails_when_pattern_present(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 1, "logType": "Error", "message": "FORBIDDEN", "stackTrace": ""},
    ]})

    result = await verify_assertions([
        {"type": "log_matches", "pattern": "FORBIDDEN", "absent": True}
    ])

    assert result["passed"] is False


# ---- play_state -------------------------------------------------------------

@pytest.mark.asyncio
async def test_play_state_equals_passes(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})

    result = await verify_assertions([{"type": "play_state", "equals": "PlayMode"}])

    assert result["passed"] is True


@pytest.mark.asyncio
async def test_play_state_equals_fails(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    result = await verify_assertions([{"type": "play_state", "equals": "PlayMode"}])

    assert result["passed"] is False
    assert "EditMode" in result["failures"][0]["detail"]


# ---- node_exists / node_visible --------------------------------------------

@pytest.mark.asyncio
async def test_node_exists_passes(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("ListUINodes").respond_with({"nodes": [
        {"name": "OkButton", "path": "Panel/OkButton", "visible": True, "clickable": True},
    ]})

    result = await verify_assertions([{"type": "node_exists", "name": "OkButton"}])

    assert result["passed"] is True


@pytest.mark.asyncio
async def test_node_visible_fails_when_hidden(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("ListUINodes").respond_with({"nodes": [
        {"name": "OkButton", "path": "Panel/OkButton", "visible": False, "clickable": True},
    ]})

    result = await verify_assertions([{"type": "node_visible", "name": "OkButton"}])

    assert result["passed"] is False


@pytest.mark.asyncio
async def test_node_exists_fails_when_absent(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("ListUINodes").respond_with({"nodes": [
        {"name": "Other", "path": "Panel/Other", "visible": True},
    ]})

    result = await verify_assertions([{"type": "node_exists", "path": "Panel/OkButton"}])

    assert result["passed"] is False


# ---- lua_expr ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_lua_expr_equals_passes(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_with({
        "ok": True, "returnCount": 1, "returnValues": ["1234"], "raw": "1234",
    })

    result = await verify_assertions([
        {"type": "lua_expr", "expr": "return PROJECT_RUNTIME.gold", "expect": "1234",
         "goal": "verify gold"}
    ])

    assert result["passed"] is True


@pytest.mark.asyncio
async def test_lua_expr_is_read_only_no_write_token(unity_bridge_session, mock_bridge_server):
    """verify_assertions is a pure verdict — lua_expr must NOT leave a dangling
    write-op token (unlike lua_eval which is a write op)."""
    from unity_mcp_py.state import GUARD

    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_with({
        "ok": True, "returnCount": 1, "returnValues": ["true"], "raw": "true",
    })

    await verify_assertions([
        {"type": "lua_expr", "expr": "return true", "expect": "true", "goal": "g"}
    ])

    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "verify_assertions" for p in pending)


@pytest.mark.asyncio
async def test_lua_expr_does_not_charge_and_verify_hard_grounds(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    """lua_expr no longer charges the runway; verify_assertions hard-grounds.

    Fail-safe proof of "no charge": with soft_threshold=1, the runway is driven to
    the wall BEFORE the verify. If lua_expr (wrongly) called charge_run() it would
    raise LuaGroundingRequiredError mid-evaluation and this test would error. Since
    verify completes and re-arms (since_ground==0, soft_used==0), lua_expr charged
    nothing.
    """
    monkeypatch.setattr(LUA_BUDGET, "soft_threshold", 1)
    await LUA_BUDGET.charge_run()                 # since_ground == threshold == 1 (at the wall)
    await LUA_BUDGET.ground(hard=False)           # clears since_ground, soft_used -> 1
    await LUA_BUDGET.charge_run()                 # since_ground == 1 again (at the wall)
    pre = await LUA_BUDGET.snapshot()
    assert pre["since_ground"] == 1 and pre["soft_used"] == 1

    bridge = mock_bridge_server
    bridge.expect_request("LuaEval").respond_with(
        {"ok": True, "returnCount": 1, "returnValues": ["x"], "raw": "x"})

    result = await verify_assertions([
        {"type": "lua_expr", "expr": "return 1", "expect": "x", "goal": "budget check"}
    ])
    assert result["passed"] is True
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0
    assert snap["soft_used"] == 0
    assert result["lua_runway"]["grounded"] is True


@pytest.mark.asyncio
async def test_verify_hard_grounds_even_on_failure(
    unity_bridge_session, mock_bridge_server
):
    """A FAILING verify still hard-grounds — the agent now holds ground truth."""
    await LUA_BUDGET.charge_run()
    await LUA_BUDGET.charge_run()

    bridge = mock_bridge_server
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    result = await verify_assertions([
        {"type": "play_state", "equals": "PlayMode"},   # will FAIL
    ])
    assert result["passed"] is False
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0   # grounded despite failure
    assert result["lua_runway"]["grounded"] is True


@pytest.mark.asyncio
async def test_verify_never_deadlocks_recovery(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    """Drive the runway to the wall, then verify_assertions recovers it.

    verify never charges, so it is always callable to re-arm — proving the escape
    hatch can't deadlock.
    """
    monkeypatch.setattr(LUA_BUDGET, "soft_threshold", 1)
    await LUA_BUDGET.charge_run()   # since_ground == threshold; lua_eval would now block

    bridge = mock_bridge_server
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})

    result = await verify_assertions([{"type": "play_state", "equals": "PlayMode"}])
    assert result["passed"] is True
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0   # re-armed; lua_eval can run again


@pytest.mark.asyncio
async def test_verify_empty_assertions_still_hard_grounds(
    unity_bridge_session, mock_bridge_server
):
    """A degenerate empty verify still hard-grounds (harmless; documents the edge)."""
    await LUA_BUDGET.charge_run()
    result = await verify_assertions([])
    assert result["passed"] is True
    assert result["failures"] == []
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0
    assert result["lua_runway"]["grounded"] is True


# ---- multiple assertions + aggregation -------------------------------------

@pytest.mark.asyncio
async def test_multiple_assertions_aggregate_verdict(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 1, "logType": "Error", "message": "boom", "stackTrace": ""},
    ]})

    result = await verify_assertions([
        {"type": "play_state", "equals": "PlayMode"},
        {"type": "expect_no_errors"},
    ])

    # One passed, one failed -> overall fail, exactly one failure recorded.
    assert result["passed"] is False
    assert len(result["results"]) == 2
    assert len(result["failures"]) == 1
    assert result["failures"][0]["index"] == 1


@pytest.mark.asyncio
async def test_unknown_assertion_type_raises(unity_bridge_session, mock_bridge_server):
    # Pre-fill the runway so we can prove the exception path leaves it untouched.
    await LUA_BUDGET.charge_run()
    await LUA_BUDGET.charge_run()
    pre = await LUA_BUDGET.snapshot()

    with pytest.raises(UnknownAssertionError, match="bogus"):
        await verify_assertions([{"type": "bogus"}])

    # UnknownAssertionError reaches no verdict, so it must NOT ground — runway as-is.
    post = await LUA_BUDGET.snapshot()
    assert post == pre


@pytest.mark.asyncio
async def test_empty_assertions_passes_vacuously(unity_bridge_session, mock_bridge_server):
    result = await verify_assertions([])
    assert result["passed"] is True
    assert result["results"] == []
