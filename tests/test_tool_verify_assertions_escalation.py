"""verify_assertions goal + escalation hook.

A FAIL verdict under the same goal, repeated to the threshold, must append an
``escalation`` advice block (verify_assertions NEVER raises for this). goal=None
preserves today's behavior exactly.
"""
from __future__ import annotations

import pytest

from unity_mcp_py.state import LUA_BUDGET
from unity_mcp_py.state.escalation_gate import ESCALATION_GATE
from unity_mcp_py.tools.verify_assertions import verify_assertions


@pytest.fixture(autouse=True)
async def _reset_state():
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()
    yield
    await LUA_BUDGET.reset()
    await ESCALATION_GATE.reset()


def _fail_playstate(bridge):
    """Script one GetPlayState that makes a play_state assertion FAIL."""
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})


@pytest.mark.asyncio
async def test_repeated_failure_under_goal_appends_escalation(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    assertions = [{"type": "play_state", "equals": "PlayMode"}]

    _fail_playstate(bridge)
    r1 = await verify_assertions(assertions, goal="G")
    assert r1["passed"] is False
    assert "escalation" not in r1  # first failure: threshold not yet reached

    _fail_playstate(bridge)
    r2 = await verify_assertions(assertions, goal="G")
    assert r2["passed"] is False
    assert r2["escalation"]["should_escalate"] is True
    assert r2["escalation"]["goal"] == "G"
    assert r2["escalation"]["fail_count"] == 2
    assert "escalate_via_click" in r2["escalation"]["advice"]


@pytest.mark.asyncio
async def test_pass_clears_and_emits_no_escalation(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    assertions = [{"type": "play_state", "equals": "PlayMode"}]

    _fail_playstate(bridge)
    await verify_assertions(assertions, goal="G")
    # now a PASS
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    r = await verify_assertions(assertions, goal="G")
    assert r["passed"] is True
    assert "escalation" not in r


@pytest.mark.asyncio
async def test_no_goal_never_escalates(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    assertions = [{"type": "play_state", "equals": "PlayMode"}]
    for _ in range(3):
        _fail_playstate(bridge)
        r = await verify_assertions(assertions)  # no goal arg
        assert "escalation" not in r


@pytest.mark.asyncio
async def test_existing_verdict_shape_and_hard_ground_preserved(
    unity_bridge_session, mock_bridge_server
):
    """Regression: verdict still has passed/failures/results/lua_runway and
    hard-grounds the lua runway (since_ground reset)."""
    bridge = mock_bridge_server
    # First spend some runway so we can see the hard-ground reset it.
    await LUA_BUDGET.charge_run()
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    r = await verify_assertions([{"type": "play_state", "equals": "PlayMode"}], goal="G")
    assert {"passed", "failures", "results", "lua_runway"}.issubset(r.keys())
    assert r["lua_runway"]["since_ground"] == 0  # hard re-arm


@pytest.mark.asyncio
async def test_goal_switch_does_not_escalate_on_new_goals_first_failure(
    unity_bridge_session, mock_bridge_server
):
    """A failure under goal A, then a failure under goal B, must NOT escalate on
    B's first failure — switching goal rebases the counter to 1."""
    bridge = mock_bridge_server
    assertions = [{"type": "play_state", "equals": "PlayMode"}]

    _fail_playstate(bridge)
    ra = await verify_assertions(assertions, goal="A")
    assert ra["passed"] is False
    assert "escalation" not in ra

    _fail_playstate(bridge)
    rb = await verify_assertions(assertions, goal="B")
    assert rb["passed"] is False
    assert "escalation" not in rb  # B's first failure: rebased to fail_count=1
