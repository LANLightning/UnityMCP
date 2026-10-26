"""Unit tests for LuaRunwayBudget (recoverable grounding runway for lua_eval).

See spec 2026-06-09-lua-eval-grounding-runway-design.md.
"""
import asyncio

import pytest

from unity_mcp_py.state import (
    LuaRunwayBudget,
    LuaBudgetError,
    LuaGroundingRequiredError,
    LuaRunawayError,
)


def test_error_hierarchy():
    """Both runway errors share the LuaBudgetError base (RuntimeError)."""
    assert issubclass(LuaGroundingRequiredError, LuaBudgetError)
    assert issubclass(LuaRunawayError, LuaBudgetError)
    assert issubclass(LuaBudgetError, RuntimeError)


async def test_charge_run_until_threshold_then_requires_grounding():
    """charge_run succeeds SOFT_THRESHOLD times, then raises (recoverable)."""
    b = LuaRunwayBudget(soft_threshold=3, soft_limit=8, runaway_max=500)
    assert await b.charge_run() == (1, 1)
    assert await b.charge_run() == (2, 2)
    assert await b.charge_run() == (3, 3)
    with pytest.raises(LuaGroundingRequiredError) as exc:
        await b.charge_run()
    assert exc.value.since_ground == 3
    assert exc.value.threshold == 3
    # The raise did NOT increment (counters unchanged by the refusal).
    snap = await b.snapshot()
    assert snap["since_ground"] == 3
    assert snap["total"] == 3


async def test_soft_grounding_resets_since_ground():
    """A soft ground clears since_ground so charge_run proceeds again."""
    b = LuaRunwayBudget(soft_threshold=2, soft_limit=8, runaway_max=500)
    await b.charge_run()
    await b.charge_run()
    with pytest.raises(LuaGroundingRequiredError):
        await b.charge_run()
    outcome = await b.ground(hard=False)
    assert outcome.cleared is True
    assert outcome.reason == "soft"
    assert outcome.since_ground == 0
    # Runway re-opened.
    assert await b.charge_run() == (1, 3)  # total keeps climbing


async def test_soft_limit_caps_soft_glances():
    """After SOFT_LIMIT soft grounds, soft grounding stops clearing."""
    b = LuaRunwayBudget(soft_threshold=5, soft_limit=2, runaway_max=500)
    o1 = await b.ground(hard=False)
    o2 = await b.ground(hard=False)
    assert o1.cleared and o2.cleared
    assert o2.soft_used == 2
    o3 = await b.ground(hard=False)
    assert o3.cleared is False
    assert o3.reason == "soft_limit_reached"
    # since_ground was NOT reset by the refused soft ground.
    await b.charge_run()  # since_ground now 1
    o4 = await b.ground(hard=False)
    assert o4.cleared is False
    snap = await b.snapshot()
    assert snap["since_ground"] == 1  # unchanged by refused soft ground


async def test_grounding_required_message_variant_when_soft_spent():
    """When soft budget is spent and runway is full, the error reports it."""
    b = LuaRunwayBudget(soft_threshold=1, soft_limit=1, runaway_max=500)
    await b.ground(hard=False)          # soft_used -> 1 (== limit)
    await b.charge_run()                # since_ground -> 1 (== threshold)
    with pytest.raises(LuaGroundingRequiredError) as exc:
        await b.charge_run()
    assert exc.value.soft_used >= exc.value.soft_limit


async def test_hard_grounding_resets_both_counters():
    """verify-style hard ground clears since_ground AND soft_used."""
    b = LuaRunwayBudget(soft_threshold=5, soft_limit=2, runaway_max=500)
    await b.ground(hard=False)
    await b.ground(hard=False)          # soft_used == limit
    await b.charge_run()
    outcome = await b.ground(hard=True)
    assert outcome.cleared is True
    assert outcome.reason == "hard"
    snap = await b.snapshot()
    assert snap["since_ground"] == 0
    assert snap["soft_used"] == 0
    # Soft grounding works again after a hard re-arm.
    o = await b.ground(hard=False)
    assert o.cleared is True


async def test_runaway_backstop_is_terminal():
    """Lifetime backstop raises and grounding does not reset total."""
    b = LuaRunwayBudget(soft_threshold=100, soft_limit=100, runaway_max=2)
    await b.charge_run()
    await b.charge_run()                # total == 2 == runaway_max
    with pytest.raises(LuaRunawayError) as exc:
        await b.charge_run()
    assert exc.value.total == 2
    assert exc.value.limit == 2
    # Grounding cannot rescue a runaway.
    await b.ground(hard=True)
    with pytest.raises(LuaRunawayError):
        await b.charge_run()


async def test_runaway_checked_before_grounding_required():
    """When both limits are tripped, the terminal backstop wins."""
    b = LuaRunwayBudget(soft_threshold=1, soft_limit=8, runaway_max=1)
    await b.charge_run()                # since_ground==1>=threshold AND total==1>=runaway
    with pytest.raises(LuaRunawayError):
        await b.charge_run()


async def test_env_overrides(monkeypatch):
    """Env vars override the three thresholds; invalid falls back to default."""
    monkeypatch.setenv("UNITY_MCP_LUA_RUNWAY", "2")
    monkeypatch.setenv("UNITY_MCP_LUA_SOFT_LIMIT", "3")
    monkeypatch.setenv("UNITY_MCP_LUA_RUNAWAY_MAX", "9")
    b = LuaRunwayBudget()
    assert b.soft_threshold == 2
    assert b.soft_limit == 3
    assert b.runaway_max == 9

    monkeypatch.setenv("UNITY_MCP_LUA_RUNWAY", "not-an-int")
    monkeypatch.setenv("UNITY_MCP_LUA_SOFT_LIMIT", "-5")
    b2 = LuaRunwayBudget()
    assert b2.soft_threshold == 5   # default
    assert b2.soft_limit == 8       # default (non-positive rejected)


async def test_reset_and_snapshot():
    """reset() zeroes all counters; snapshot() returns the three-key dict."""
    b = LuaRunwayBudget(soft_threshold=5, soft_limit=8, runaway_max=500)
    await b.charge_run()
    await b.ground(hard=False)
    snap = await b.snapshot()
    assert set(snap) == {"since_ground", "soft_used", "total"}
    await b.reset()
    assert await b.snapshot() == {"since_ground": 0, "soft_used": 0, "total": 0}


async def test_concurrent_charge_never_exceeds_threshold():
    """Lock correctness: concurrent charge_run calls cannot overshoot threshold."""
    b = LuaRunwayBudget(soft_threshold=5, soft_limit=8, runaway_max=500)
    results = await asyncio.gather(
        *(b.charge_run() for _ in range(20)),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    raised = [r for r in results if isinstance(r, LuaGroundingRequiredError)]
    # At most threshold succeed before a grounding is forced.
    assert len(ok) == 5
    assert len(raised) == 15
    snap = await b.snapshot()
    assert snap["since_ground"] == 5
    assert snap["total"] == 5


async def test_soft_ground_when_since_ground_zero_burns_a_soft_slot():
    """ground(hard=False) before any charge_run is valid: since_ground stays 0,
    but it still consumes a soft slot (pins the decision)."""
    b = LuaRunwayBudget(soft_threshold=5, soft_limit=8, runaway_max=500)
    outcome = await b.ground(hard=False)
    assert outcome.cleared is True
    assert outcome.since_ground == 0
    snap = await b.snapshot()
    assert snap["since_ground"] == 0
    assert snap["soft_used"] == 1   # a soft slot was burned even with nothing to clear


async def test_concurrent_ground_and_charge_stay_consistent():
    """Lock correctness under mixed concurrent ground/charge: soft_used never
    exceeds soft_limit and counters stay internally consistent."""
    b = LuaRunwayBudget(soft_threshold=100, soft_limit=3, runaway_max=10000)
    # Interleave many soft grounds with charges concurrently.
    tasks = []
    for _ in range(10):
        tasks.append(b.ground(hard=False))
        tasks.append(b.charge_run())
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # No exception from the budget logic under this load (thresholds are high enough).
    assert all(not isinstance(r, Exception) for r in results)
    snap = await b.snapshot()
    # soft_used is capped at soft_limit (further soft grounds were refused).
    assert snap["soft_used"] <= 3


async def test_snapshot_outcome_is_read_only():
    """snapshot_outcome() returns a non-mutating cleared=False view of the runway."""
    b = LuaRunwayBudget(soft_threshold=5, soft_limit=8, runaway_max=500)
    await b.charge_run()
    await b.ground(hard=False)   # soft_used -> 1
    await b.charge_run()         # since_ground -> 1
    before = await b.snapshot()
    outcome = await b.snapshot_outcome()
    after = await b.snapshot()
    assert outcome.cleared is False
    assert outcome.reason == "not_grounded"
    assert outcome.since_ground == before["since_ground"]
    assert outcome.soft_used == before["soft_used"]
    # Counters unchanged by the read-only view.
    assert before == after
