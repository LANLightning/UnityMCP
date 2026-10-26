"""escalate_via_click: restore the lua op, then re-enter via a real click.

Atomic: undo_lua (LuaEval) -> simulate_click -> clear the escalation gate. A
failed undo must NOT click and must NOT clear; a failed click must NOT clear.
"""
from __future__ import annotations

import pytest

from unity_mcp_py.bridge import bridge_client
from unity_mcp_py.state import LUA_BUDGET
from unity_mcp_py.state.escalation_gate import ESCALATION_GATE
from unity_mcp_py.tools.escalate_via_click import (
    escalate_via_click,
    InvalidEscalateArg,
)


@pytest.fixture(autouse=True)
async def _reset_state():
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()
    yield
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()


@pytest.mark.asyncio
async def test_happy_path_restores_clicks_and_clears(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    # Pre-load the gate to the escalation state for goal "G".
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    # undo leg
    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})
    # simulate_click leg does write_op: TailEditorLog (line snapshot) then SimulateClick
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("SimulateClick").respond_with({"clicked": True, "nodeFound": True})

    result = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
        panel_uid=123,
        ref_key="btnOpenReal",
        goal="G",
    )

    assert result["clicked"] is True
    assert result["restored"] is True
    assert result["escalation_cleared"] is True
    assert "write_op_token" in result
    # gate cleared for G
    snap = await ESCALATION_GATE.snapshot()
    assert snap["fail_count"] == 0


@pytest.mark.asyncio
async def test_undo_failure_does_not_click_or_clear(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    bridge.expect_request("LuaEval").respond_error("INTERNAL", "boom")

    with pytest.raises(bridge_client.BridgeError):
        await escalate_via_click(
            undo_lua="bad()",
            panel_uid=123,
            ref_key="btnOpenReal",
            goal="G",
        )
    # counter intact (no clear), no SimulateClick was scripted/consumed
    snap = await ESCALATION_GATE.snapshot()
    assert snap["fail_count"] == 2


@pytest.mark.asyncio
async def test_click_miss_does_not_clear(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("SimulateClick").respond_with({"clicked": False, "nodeFound": True})

    result = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
        panel_uid=123,
        ref_key="btnOpenReal",
        goal="G",
    )
    assert result["clicked"] is False
    assert result["restored"] is True
    assert result["escalation_cleared"] is False
    snap = await ESCALATION_GATE.snapshot()
    assert snap["fail_count"] == 2  # NOT cleared


@pytest.mark.asyncio
async def test_empty_undo_lua_rejected():
    with pytest.raises(InvalidEscalateArg):
        await escalate_via_click(undo_lua="   ", panel_uid=1, ref_key="x", goal="G")


@pytest.mark.asyncio
async def test_empty_goal_rejected():
    with pytest.raises(InvalidEscalateArg):
        await escalate_via_click(undo_lua="x()", panel_uid=1, ref_key="x", goal=" ")


@pytest.mark.asyncio
async def test_both_locators_rejected():
    with pytest.raises(InvalidEscalateArg):
        await escalate_via_click(
            undo_lua="x()", panel_uid=1, ref_key="x", path="A/B", goal="G"
        )


@pytest.mark.asyncio
async def test_neither_locator_rejected():
    with pytest.raises(InvalidEscalateArg):
        await escalate_via_click(undo_lua="x()", panel_uid=1, goal="G")


@pytest.mark.asyncio
async def test_bad_mode_rejected():
    with pytest.raises(InvalidEscalateArg):
        await escalate_via_click(
            undo_lua="x()", panel_uid=1, ref_key="x", goal="G", mode="teleport"
        )


@pytest.mark.asyncio
async def test_bad_locator_charset_raises_after_undo_without_clearing(
    unity_bridge_session, mock_bridge_server
):
    """A ref_key that passes escalate's XOR check but fails simulate_click's
    charset regex raises InvalidSimulateClickArg AFTER the undo fires: the lua
    state is restored, no click happens, and the gate is NOT cleared."""
    from unity_mcp_py.tools.simulate_click import InvalidSimulateClickArg

    bridge = mock_bridge_server
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    # The undo DOES fire (escalate's own validation passed); simulate_click then
    # rejects the space in the ref_key before any SimulateClick bridge call.
    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})

    with pytest.raises(InvalidSimulateClickArg):
        await escalate_via_click(
            undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
            panel_uid=123,
            ref_key="btn open",  # space -> fails simulate_click's [A-Za-z0-9_] regex
            goal="G",
        )
    snap = await ESCALATION_GATE.snapshot()
    assert snap["fail_count"] == 2  # gate intact — no landed click


@pytest.mark.asyncio
async def test_zero_panel_uid_rejected_before_undo(
    unity_bridge_session, mock_bridge_server
):
    """panel_uid=0 is caught by escalate's own validation BEFORE the undo fires
    (a wasted side-effecting round-trip avoided). No LuaEval is scripted, so a
    consumed request would fail the mock."""
    with pytest.raises(InvalidEscalateArg):
        await escalate_via_click(
            undo_lua="PROJECT_RUNTIME.global.ui:close(123)", panel_uid=0, ref_key="x", goal="G"
        )


@pytest.mark.asyncio
async def test_landed_click_on_rebased_goal_reports_not_cleared(
    unity_bridge_session, mock_bridge_server
):
    """If an interleaved verify rebased the gate to another goal, a landed
    escalate click for the ORIGINAL goal must report escalation_cleared=False
    (clear() is a no-op) rather than lying that it cleared."""
    bridge = mock_bridge_server
    # Gate is escalated for G, then an interleaved verify for H rebases _goal=H.
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("H", passed=False)  # rebases to (H, 1)

    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("SimulateClick").respond_with({"clicked": True, "nodeFound": True})

    result = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
        panel_uid=123,
        ref_key="btnOpenReal",
        goal="G",  # clear(G) is a no-op now that _goal == "H"
    )
    assert result["clicked"] is True
    assert result["escalation_cleared"] is False  # honest: nothing was cleared for G
    # H's counter is untouched by the no-op clear(G)
    snap = await ESCALATION_GATE.snapshot()
    assert snap["goal"] == "H" and snap["fail_count"] == 1
