"""End-to-end integration: the verify → escalate → verify-pass cycle.

Drives the actual tools (verify_assertions + escalate_via_click) through the real
EscalationGate, mirroring what the agent executes: command-line verify fails
twice under a goal → escalation block appears → escalate_via_click restores +
clicks for real and clears the gate → a subsequent verify passes with no
escalation and the lua runway re-armed.
"""
from __future__ import annotations

import pytest

from unity_mcp_py.state import LUA_BUDGET
from unity_mcp_py.state.escalation_gate import ESCALATION_GATE
from unity_mcp_py.tools.escalate_via_click import escalate_via_click
from unity_mcp_py.tools.verify_assertions import verify_assertions


@pytest.fixture(autouse=True)
async def _reset_state():
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()
    yield
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()


@pytest.mark.asyncio
async def test_full_verify_escalate_verify_cycle(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    goal = "打开背包并确认有真实道具"
    assertions = [{"type": "play_state", "equals": "PlayMode"}]

    # 1. Two command-line verifies FAIL under the same goal -> escalation raised.
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})
    r1 = await verify_assertions(assertions, goal=goal)
    assert r1["passed"] is False
    assert "escalation" not in r1

    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})
    r2 = await verify_assertions(assertions, goal=goal)
    assert r2["passed"] is False
    assert r2["escalation"]["should_escalate"] is True
    assert r2["escalation"]["goal"] == goal

    # 2. Escalate: restore the lua op + real click. Gate clears for the goal.
    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 9}]})
    bridge.expect_request("SimulateClick").respond_with(
        {"clicked": True, "nodeFound": True}
    )
    esc = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(101)",
        panel_uid=101,
        ref_key="btnEnterReal",
        goal=goal,
    )
    assert esc["clicked"] is True
    assert esc["restored"] is True
    assert esc["escalation_cleared"] is True

    # 3. A subsequent verify under the same goal PASSES — no escalation, and the
    #    counter is fully reset (a fresh failure would start at 1, below threshold).
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    r3 = await verify_assertions(assertions, goal=goal)
    assert r3["passed"] is True
    assert "escalation" not in r3

    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})
    r4 = await verify_assertions(assertions, goal=goal)
    assert r4["passed"] is False
    assert "escalation" not in r4  # back to fail_count=1 after the clear+pass
