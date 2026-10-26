"""Tests for lua_eval tool — recoverable grounding runway (spec 2026-06-09)."""
import pytest

from unity_mcp_py.state import (
    GUARD,
    LUA_BUDGET,
    LuaGroundingRequiredError,
    LuaRunawayError,
)
from unity_mcp_py.tools.lua_eval import InvalidLuaScriptError, lua_eval


@pytest.fixture(autouse=True)
async def _reset_lua_budget():
    """Reset the process-level runway before/after each test (isolation)."""
    await LUA_BUDGET.reset()
    yield
    await LUA_BUDGET.reset()


@pytest.mark.asyncio
async def test_lua_eval_success_issues_token_and_runway(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """lua_eval returns bridge result + write_op_token + lua_runway block."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 310}]})
    bridge.expect_request("LuaEval").respond_with({
        "ok": True, "returnCount": 1, "returnValues": ["True"], "raw": "True",
    })

    result = await lua_eval("return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE(66)", goal="verify panel 66 visible")

    assert result["ok"] is True
    assert "write_op_token" in result
    assert result["requires_observation"] is True
    # New: runway state surfaced so the agent can watch it fill.
    assert result["lua_runway"]["since_ground"] == 1
    assert result["lua_runway"]["threshold"] == LUA_BUDGET.soft_threshold
    assert result["lua_runway"]["grounded"] is False

    # Token is actually registered in the guard (not just present in the response).
    pending = await GUARD.list_pending()
    assert any(p.token == result["write_op_token"] for p in pending)

    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_lua_eval_passes_chunk_name(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """chunk_name is forwarded to the bridge args when provided."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request(
        "LuaEval",
        args_predicate=lambda a: a.get("chunkName") == "verify_open"
        and a.get("script") == "return 1",
    ).respond_with({"ok": True, "returnCount": 0, "returnValues": []})

    result = await lua_eval("return 1", goal="forward chunk name", chunk_name="verify_open")
    assert "write_op_token" in result
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_lua_eval_no_token_on_bridge_error(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """A bridge-level error (e.g. EditMode lua_vm_not_running) issues no token."""
    from unity_mcp_py.bridge import bridge_client
    BridgeError = bridge_client.BridgeError

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 5}]})
    bridge.expect_request("LuaEval").respond_error(
        "INVALID_STATE", "lua_vm_not_running: PROJECT_FACADE.PROJECT_LUA_MANAGER is null"
    )

    with pytest.raises(BridgeError, match="lua_vm_not_running"):
        await lua_eval("PROJECT_RUNTIME.global.ui:open(66)", goal="open panel 66 in editmode")

    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_eval" for p in pending)


@pytest.mark.parametrize("bad_script", ["", "   ", "\n\t  \n"])
@pytest.mark.asyncio
async def test_lua_eval_rejects_empty_script(bad_script: str) -> None:
    """Empty/whitespace script rejected before any bridge call; no token."""
    with pytest.raises(InvalidLuaScriptError):
        await lua_eval(bad_script, goal="should never reach bridge")
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_eval" for p in pending)


@pytest.mark.asyncio
async def test_lua_eval_rejects_oversized_script() -> None:
    """A megabyte script is rejected before any bridge traffic."""
    with pytest.raises(InvalidLuaScriptError, match="exceeds"):
        await lua_eval("x" * 20000, goal="oversized")


@pytest.mark.parametrize("bad_goal", ["", "   ", "\n\t  \n"])
async def test_lua_eval_rejects_empty_goal_before_bridge_and_charge(bad_goal: str) -> None:
    """Empty/whitespace goal rejected before bridge AND before charging the runway."""
    with pytest.raises(InvalidLuaScriptError, match="goal"):
        await lua_eval("return 1", goal=bad_goal)
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_eval" for p in pending)
    # Validation precedes charge -> runway untouched.
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0
    assert snap["total"] == 0


async def test_lua_eval_rejects_oversized_goal() -> None:
    """An oversized goal is rejected before any bridge traffic and charges nothing."""
    with pytest.raises(InvalidLuaScriptError, match="goal exceeds"):
        await lua_eval("return 1", goal="x" * 5000)
    snap = await LUA_BUDGET.snapshot()
    assert snap["total"] == 0


async def test_lua_eval_over_runway_raises_without_bridge_or_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the runway is exhausted, lua_eval raises BEFORE any bridge call.

    Only ONE TailEditorLog+LuaEval pair is scripted — consumed by the first call.
    The second call is over-runway. PROOF it never reached the bridge: if charge_run
    were (wrongly) called AFTER the bridge, the second call would hit an EMPTY mock
    queue and surface a BridgeError — not the LuaGroundingRequiredError asserted here.
    A different exception type would fail pytest.raises below.
    """
    monkeypatch.setattr(LUA_BUDGET, "soft_threshold", 1)

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("LuaEval").respond_with(
        {"ok": True, "returnCount": 0, "returnValues": []}
    )

    first = await lua_eval("return 1", goal="g")
    await GUARD.consume([first["write_op_token"]])

    # Second call is over-runway: must raise the recoverable error, NOT a BridgeError.
    with pytest.raises(LuaGroundingRequiredError):
        await lua_eval("return 2", goal="g")

    # No second token issued (the blocked call had zero side effects).
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_eval" for p in pending)


async def test_lua_eval_recovers_after_grounding(
    unity_bridge_session: object,
    mock_bridge_server: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE headline behavior: a soft ground re-opens the runway after a block."""
    monkeypatch.setattr(LUA_BUDGET, "soft_threshold", 1)

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("LuaEval").respond_with(
        {"ok": True, "returnCount": 0, "returnValues": []}
    )
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 8}]})
    bridge.expect_request("LuaEval").respond_with(
        {"ok": True, "returnCount": 0, "returnValues": []}
    )

    first = await lua_eval("return 1", goal="g")
    await GUARD.consume([first["write_op_token"]])

    with pytest.raises(LuaGroundingRequiredError):
        await lua_eval("return 2", goal="g")

    # Ground (as a real observe tool would) and continue — no session lockout.
    await LUA_BUDGET.ground(hard=False)
    second = await lua_eval("return 3", goal="g")
    assert "write_op_token" in second
    # Recovery semantics: ground() reset since_ground to 0, this call charged it to 1.
    assert second["lua_runway"]["since_ground"] == 1
    await GUARD.consume([second["write_op_token"]])


async def test_lua_eval_charges_on_bridge_error(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Charge-on-attempt preserved: a bridge error still advances the runway."""
    from unity_mcp_py.bridge import bridge_client
    BridgeError = bridge_client.BridgeError

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 9}]})
    bridge.expect_request("LuaEval").respond_error(
        "INVALID_STATE", "lua_vm_not_running: PROJECT_FACADE.PROJECT_LUA_MANAGER is null"
    )

    with pytest.raises(BridgeError, match="lua_vm_not_running"):
        await lua_eval("PROJECT_RUNTIME.global.ui:open(66)", goal="attempt counts")

    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 1
    assert snap["total"] == 1


async def test_lua_eval_runaway_backstop_is_terminal(
    unity_bridge_session: object,
    mock_bridge_server: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifetime backstop raises before the bridge and issues no token."""
    monkeypatch.setattr(LUA_BUDGET, "soft_threshold", 100)
    monkeypatch.setattr(LUA_BUDGET, "runaway_max", 1)

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request("LuaEval").respond_with(
        {"ok": True, "returnCount": 0, "returnValues": []}
    )

    first = await lua_eval("return 1", goal="g")
    await GUARD.consume([first["write_op_token"]])

    with pytest.raises(LuaRunawayError):
        await lua_eval("return 2", goal="g")
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_eval" for p in pending)


# --- Degenerate-infinite-loop tripwire (Mechanism A) -------------------------


@pytest.mark.parametrize(
    "bad_script",
    [
        "while true do end",
        "while  true  do  end",          # whitespace-tolerant
        "while\ttrue\tdo\tend",          # tabs
        "while 1 do end",
        "repeat until false",
        "repeat until nil",
        "print(1) while true do end",    # embedded after a legit statement
        "while true do end -- hang",     # trailing comment doesn't save it
    ],
)
@pytest.mark.asyncio
async def test_lua_eval_rejects_degenerate_infinite_loop(bad_script: str) -> None:
    """No-exit loops are rejected before any bridge call and issue no token.

    Fixtures (unity_bridge_session, mock_bridge_server) are deliberately NOT
    listed: the tripwire must fire before any bridge connection is attempted,
    so the test passes without a mock bridge (mirrors lua_reload_module's
    injection-rejection test)."""
    with pytest.raises(InvalidLuaScriptError, match="degenerate infinite loop"):
        await lua_eval(bad_script, goal="should never reach bridge")
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_eval" for p in pending)
    # Validation precedes charge -> runway untouched.
    snap = await LUA_BUDGET.snapshot()
    assert snap["total"] == 0


@pytest.mark.parametrize(
    "ok_script",
    [
        "while true do break end",                 # has an exit
        "while true do if x then break end end",   # conditional exit
        "for i = 1, 10 do end",                     # bounded numeric for
        "while running do step() end",             # non-literal condition
        "while True do end",                        # `True` is nil in Lua -> 0 iters
        "-- while true do end\nreturn 1",          # pattern only in a comment
        "--[[ while true do end ]] return 2",      # pattern only in block comment
    ],
)
@pytest.mark.asyncio
async def test_lua_eval_allows_loops_with_an_exit(
    ok_script: str,
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Legitimate loops (and comment-only matches) are NOT false-positived."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request("LuaEval").respond_with(
        {"ok": True, "returnCount": 0, "returnValues": []}
    )

    result = await lua_eval(ok_script, goal="legit loop must pass the tripwire")
    assert "write_op_token" in result
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_lua_eval_allow_unbounded_bypasses_tripwire(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """allow_unbounded=True lets a no-exit loop through to the bridge."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request(
        "LuaEval", args_predicate=lambda a: a.get("script") == "while true do end"
    ).respond_with({"ok": True, "returnCount": 0, "returnValues": []})

    result = await lua_eval(
        "while true do end", goal="intentional unbounded chunk", allow_unbounded=True
    )
    assert "write_op_token" in result
    await GUARD.consume([result["write_op_token"]])
