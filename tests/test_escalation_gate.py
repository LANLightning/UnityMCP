"""Unit tests for EscalationGate — the consecutive same-goal verify-failure gate."""
from __future__ import annotations

import pytest

from unity_mcp_py.state.escalation_gate import EscalationGate


@pytest.mark.asyncio
async def test_same_goal_failures_accumulate_to_threshold():
    gate = EscalationGate(threshold=2)
    o1 = await gate.record_verify("G", passed=False)
    assert o1.fail_count == 1 and o1.should_escalate is False
    o2 = await gate.record_verify("G", passed=False)
    assert o2.fail_count == 2 and o2.should_escalate is True
    assert o2.goal == "G"


@pytest.mark.asyncio
async def test_pass_resets_count():
    gate = EscalationGate(threshold=2)
    await gate.record_verify("G", passed=False)
    o = await gate.record_verify("G", passed=True)
    assert o.fail_count == 0 and o.should_escalate is False
    # next failure starts fresh at 1, not 2
    o2 = await gate.record_verify("G", passed=False)
    assert o2.fail_count == 1 and o2.should_escalate is False


@pytest.mark.asyncio
async def test_goal_switch_rebases_count():
    gate = EscalationGate(threshold=2)
    await gate.record_verify("G", passed=False)
    o = await gate.record_verify("H", passed=False)
    assert o.goal == "H" and o.fail_count == 1 and o.should_escalate is False


@pytest.mark.asyncio
async def test_none_goal_never_counts():
    gate = EscalationGate(threshold=2)
    o1 = await gate.record_verify(None, passed=False)
    o2 = await gate.record_verify(None, passed=False)
    assert o1.should_escalate is False and o2.should_escalate is False
    assert o1.fail_count == 0 and o2.fail_count == 0


@pytest.mark.asyncio
async def test_clear_zeroes_goal():
    gate = EscalationGate(threshold=2)
    await gate.record_verify("G", passed=False)
    await gate.record_verify("G", passed=False)
    await gate.clear("G")
    o = await gate.record_verify("G", passed=False)
    assert o.fail_count == 1 and o.should_escalate is False


@pytest.mark.asyncio
async def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("UNITY_MCP_ESCALATE_THRESHOLD", "3")
    gate = EscalationGate()
    assert gate.threshold == 3


@pytest.mark.asyncio
async def test_snapshot_shape():
    gate = EscalationGate(threshold=2)
    await gate.record_verify("G", passed=False)
    snap = await gate.snapshot()
    assert snap == {"goal": "G", "fail_count": 1, "threshold": 2}


def test_exports_from_state_package():
    from unity_mcp_py.state import (
        ESCALATION_GATE,
        EscalationGate,
        EscalationOutcome,
        escalation_response,
    )
    assert ESCALATION_GATE is not None
    assert isinstance(ESCALATION_GATE, EscalationGate)
    assert EscalationOutcome is not None
    assert callable(escalation_response)
