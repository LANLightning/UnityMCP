"""escalate_via_click reverify leg: after a landed real click, the tool itself
re-verifies through the real environment and decides whether the agent must ask
the user — the agent never inserts a user-query before the click.

Contract (only when ``assertions`` is provided):
  - undo -> real click LANDS -> verify_assertions(assertions, goal):
      * reverify PASSES  -> escalation_cleared True, needs_user False, reverify_passed True
      * reverify FAILS   -> needs_user True (the ONLY point the agent should ask),
                            reverify_passed False; gate state follows verify's own
                            re-record (a fresh same-goal fail), NOT a manual clear.
  - click MISSES        -> no reverify (mechanical retry), needs_user False,
                            reverify_passed None.
  - assertions is None  -> legacy behavior: no reverify key churn; reverify_passed
                            None, needs_user False. (Covered by the legacy suite.)
"""
from __future__ import annotations

import pytest

from unity_mcp_py.state import LUA_BUDGET
from unity_mcp_py.state.escalation_gate import ESCALATION_GATE
from unity_mcp_py.tools.escalate_via_click import escalate_via_click


@pytest.fixture(autouse=True)
async def _reset_state():
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()
    yield
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()


@pytest.mark.asyncio
async def test_landed_click_then_reverify_pass_no_user(
    unity_bridge_session, mock_bridge_server
):
    """Click lands AND reverify passes: the real path worked, so the agent must
    NOT ask the user. needs_user False, reverify_passed True, gate cleared."""
    bridge = mock_bridge_server
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})  # undo
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("SimulateClick").respond_with({"clicked": True, "nodeFound": True})
    # reverify leg: one play_state assertion that PASSES
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})

    result = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
        panel_uid=123,
        ref_key="btnOpenReal",
        goal="G",
        assertions=[{"type": "play_state", "equals": "PlayMode"}],
    )

    assert result["clicked"] is True
    assert result["restored"] is True
    assert result["reverify_passed"] is True
    assert result["needs_user"] is False
    assert result["escalation_cleared"] is True
    snap = await ESCALATION_GATE.snapshot()
    assert snap["fail_count"] == 0


@pytest.mark.asyncio
async def test_landed_click_then_reverify_fail_flags_user(
    unity_bridge_session, mock_bridge_server
):
    """Click lands but reverify STILL fails: the real path also did not produce
    the expected state — THIS is the only point the agent should ask the user."""
    bridge = mock_bridge_server
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})  # undo
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("SimulateClick").respond_with({"clicked": True, "nodeFound": True})
    # reverify leg: assertion FAILS (still EditMode)
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    result = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
        panel_uid=123,
        ref_key="btnOpenReal",
        goal="G",
        assertions=[{"type": "play_state", "equals": "PlayMode"}],
    )

    assert result["clicked"] is True
    assert result["reverify_passed"] is False
    assert result["needs_user"] is True
    # reverify ran its own verify_assertions which re-recorded a same-goal fail;
    # the click's manual clear happened first (fail_count 0) then verify re-recorded
    # a fresh fail -> fail_count 1. Either way needs_user is the agent's signal.
    assert "reverify" in result


@pytest.mark.asyncio
async def test_click_miss_skips_reverify_no_user(
    unity_bridge_session, mock_bridge_server
):
    """Click MISSES: mechanical locator problem — retry, do NOT reverify and do
    NOT ask the user. reverify_passed None, needs_user False, gate intact."""
    bridge = mock_bridge_server
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})  # undo
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("SimulateClick").respond_with({"clicked": False, "nodeFound": True})
    # NO GetPlayState scripted — reverify must not run on a miss.

    result = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
        panel_uid=123,
        ref_key="btnOpenReal",
        goal="G",
        assertions=[{"type": "play_state", "equals": "PlayMode"}],
    )

    assert result["clicked"] is False
    assert result["reverify_passed"] is None
    assert result["needs_user"] is False
    assert result["escalation_cleared"] is False
    snap = await ESCALATION_GATE.snapshot()
    assert snap["fail_count"] == 2  # untouched


@pytest.mark.asyncio
async def test_no_assertions_keeps_legacy_shape(
    unity_bridge_session, mock_bridge_server
):
    """assertions omitted: no reverify runs. New keys are present but inert
    (reverify_passed None, needs_user False) so legacy callers are unaffected."""
    bridge = mock_bridge_server
    await ESCALATION_GATE.record_verify("G", passed=False)
    await ESCALATION_GATE.record_verify("G", passed=False)

    bridge.expect_request("LuaEval").respond_with({"raw": "nil"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("SimulateClick").respond_with({"clicked": True, "nodeFound": True})

    result = await escalate_via_click(
        undo_lua="PROJECT_RUNTIME.global.ui:close(123)",
        panel_uid=123,
        ref_key="btnOpenReal",
        goal="G",
    )

    assert result["clicked"] is True
    assert result["escalation_cleared"] is True
    assert result["reverify_passed"] is None
    assert result["needs_user"] is False
