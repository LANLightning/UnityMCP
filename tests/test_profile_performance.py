"""Tests for the optional profile_performance tool + its baseline cache."""
from __future__ import annotations

import pytest

from unity_mcp_py.state.profiler_cache import PROFILER_CACHE, Baseline, ProfilerCache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty global cache (process-lifetime singleton)."""
    PROFILER_CACHE.clear()
    yield
    PROFILER_CACHE.clear()


def test_cache_starts_empty():
    c = ProfilerCache()
    assert c.baseline is None


def test_set_and_read_baseline():
    c = ProfilerCache()
    scenarios = [{"label": "current"}]
    runs = [{"label": "current", "metrics": {"fps": {"avg": 60.0}}}]
    c.set_baseline("cpu", scenarios, runs)
    b = c.baseline
    assert isinstance(b, Baseline)
    assert b.mode == "cpu"
    assert b.scenarios == scenarios
    assert b.runs == runs
    assert b.created_call_seq == 1


def test_clear_purges_baseline():
    c = ProfilerCache()
    c.set_baseline("cpu", [{"label": "x"}], [{"label": "x"}])
    assert c.baseline is not None
    c.clear()
    assert c.baseline is None


def test_seq_increments_per_baseline():
    c = ProfilerCache()
    c.set_baseline("cpu", [{"label": "a"}], [{"label": "a"}])
    first = c.baseline.created_call_seq
    c.set_baseline("cpu", [{"label": "b"}], [{"label": "b"}])
    assert c.baseline.created_call_seq == first + 1


def test_compares_since_baseline_is_an_age():
    # baseline_age_calls must be a TRUE age: 0 at baseline, +1 per compare,
    # reset on a new baseline (not the fixed creation seq).
    c = ProfilerCache()
    c.set_baseline("cpu", [{"label": "s"}], [{"label": "s"}])
    assert c.compares_since_baseline == 0
    assert c.note_compare() == 1
    assert c.note_compare() == 2
    assert c.compares_since_baseline == 2
    c.set_baseline("cpu", [{"label": "s"}], [{"label": "s"}])  # new baseline resets age
    assert c.compares_since_baseline == 0


def test_set_baseline_copies_inputs_so_later_mutation_does_not_corrupt():
    # Task 3's _validate_scenarios mutates scenario dicts in place; the cache must
    # not alias the caller's lists/dicts or the stored baseline would be corrupted.
    c = ProfilerCache()
    scenarios = [{"label": "s", "warmup_frames": 30}]
    runs = [{"label": "s"}]
    c.set_baseline("cpu", scenarios, runs)
    scenarios[0]["warmup_frames"] = 999      # mutate the caller's copy AFTER storing
    scenarios.append({"label": "injected"})
    assert c.baseline.scenarios[0]["warmup_frames"] == 30
    assert len(c.baseline.scenarios) == 1
    # nested dict (quality_overrides) must also be protected (deep copy)
    c2 = ProfilerCache()
    sc = [{"label": "s", "quality_overrides": {"lodBias": 0.5}}]
    c2.set_baseline("cpu", sc, [{"label": "s"}])
    sc[0]["quality_overrides"]["lodBias"] = 999
    assert c2.baseline.scenarios[0]["quality_overrides"]["lodBias"] == 0.5


# ---------------------------------------------------------------- validation (T9)
from unity_mcp_py.tools.profile_performance import (
    InvalidProfileArg,
    _validate_scenarios,
    _MAX_SCENARIOS,
    _MAX_WARMUP_FRAMES,
    _ALLOWED_QUALITY_KEYS,
)


def test_validate_default_scenario_when_none():
    # None with no baseline → one "current" scenario (Editor as-is).
    out = _validate_scenarios(None, baseline=None)
    assert out == [{"label": "current"}]


def test_validate_reuses_baseline_matrix_on_compare():
    from unity_mcp_py.state.profiler_cache import Baseline
    base = Baseline(mode="cpu", scenarios=[{"label": "low"}], runs=[{"label": "low"}], created_call_seq=1)
    out = _validate_scenarios(None, baseline=base)
    assert out == [{"label": "low"}]


def test_validate_rejects_too_many():
    too_many = [{"label": f"s{i}"} for i in range(_MAX_SCENARIOS + 1)]
    with pytest.raises(InvalidProfileArg, match="at most"):
        _validate_scenarios(too_many, baseline=None)


def test_validate_rejects_duplicate_label():
    with pytest.raises(InvalidProfileArg, match="unique"):
        _validate_scenarios([{"label": "x"}, {"label": "x"}], baseline=None)


def test_validate_rejects_empty_label():
    with pytest.raises(InvalidProfileArg, match="label"):
        _validate_scenarios([{"label": ""}], baseline=None)
    with pytest.raises(InvalidProfileArg, match="label"):
        _validate_scenarios([{}], baseline=None)


def test_validate_rejects_unknown_quality_key():
    bad = [{"label": "x", "quality_overrides": {"bogusKnob": 1}}]
    with pytest.raises(InvalidProfileArg, match="quality_overrides"):
        _validate_scenarios(bad, baseline=None)


def test_validate_accepts_known_quality_keys():
    good = [{"label": "x", "quality_overrides": {k: 1 for k in _ALLOWED_QUALITY_KEYS}}]
    out = _validate_scenarios(good, baseline=None)
    assert out[0]["label"] == "x"


def test_validate_clamps_warmup_frames():
    # §5.4: warmup_frames clamped to [0, 300]; the clamped value is written back.
    out = _validate_scenarios([{"label": "x", "warmup_frames": 100000}], baseline=None)
    assert out[0]["warmup_frames"] == _MAX_WARMUP_FRAMES
    out2 = _validate_scenarios([{"label": "y", "warmup_frames": -5}], baseline=None)
    assert out2[0]["warmup_frames"] == 0


def test_validate_rejects_non_int_warmup_frames():
    with pytest.raises(InvalidProfileArg, match="warmup_frames"):
        _validate_scenarios([{"label": "x", "warmup_frames": "lots"}], baseline=None)


def test_validate_rejects_bad_target_fps():
    # Boundary type-check: a non-int target_fps fails fast here, not deep in C#.
    with pytest.raises(InvalidProfileArg, match="target_fps"):
        _validate_scenarios([{"label": "x", "target_fps": "fast"}], baseline=None)
    with pytest.raises(InvalidProfileArg, match="target_fps"):
        _validate_scenarios([{"label": "x", "target_fps": True}], baseline=None)
    # A valid int passes through unchanged.
    out = _validate_scenarios([{"label": "x", "target_fps": 30}], baseline=None)
    assert out[0]["target_fps"] == 30


def test_validate_rejects_bad_resolution():
    for bad in (["1920", "1080"], [1920], [1920, 1080, 1], "1920x1080", [1920, True]):
        with pytest.raises(InvalidProfileArg, match="resolution"):
            _validate_scenarios([{"label": "x", "resolution": bad}], baseline=None)
    # A valid [w, h] int pair passes.
    out = _validate_scenarios([{"label": "x", "resolution": [1920, 1080]}], baseline=None)
    assert out[0]["resolution"] == [1920, 1080]


def test_validate_mode_roundtrip():
    from unity_mcp_py.tools.profile_performance import _validate_mode
    assert _validate_mode("cpu") == "cpu"
    with pytest.raises(InvalidProfileArg, match="one of"):
        _validate_mode("bogus")


def test_validate_rejects_non_list_scenarios():
    with pytest.raises(InvalidProfileArg, match="non-empty list"):
        _validate_scenarios({"label": "x"}, baseline=None)
    with pytest.raises(InvalidProfileArg, match="non-empty list"):
        _validate_scenarios([], baseline=None)


def test_validate_rejects_bool_warmup_frames():
    with pytest.raises(InvalidProfileArg, match="warmup_frames"):
        _validate_scenarios([{"label": "x", "warmup_frames": True}], baseline=None)


def test_clamp_frames_bounds_and_type():
    from unity_mcp_py.tools.profile_performance import _clamp_frames, _MIN_SAMPLE_FRAMES, _MAX_SAMPLE_FRAMES
    assert _clamp_frames(5) == _MIN_SAMPLE_FRAMES
    assert _clamp_frames(99999) == _MAX_SAMPLE_FRAMES
    assert _clamp_frames(120) == 120
    with pytest.raises(InvalidProfileArg, match="sample_frames"):
        _clamp_frames(45.7)


# ---------------------------------------------------------------- lifecycle (T3, T5, T8)
import pytest as _pytest
from unity_mcp_py.tools.profile_performance import profile_performance


@_pytest.mark.asyncio
async def test_reset_clears_and_returns_reset(unity_bridge_session, mock_bridge_server):
    PROFILER_CACHE.set_baseline("cpu", [{"label": "x"}], [{"label": "x"}])
    r = await profile_performance(reset=True)
    assert r["phase"] == "reset" and r["cleared"] is True
    assert PROFILER_CACHE.baseline is None


@_pytest.mark.asyncio
async def test_capabilities_false_short_circuits(unity_bridge_session, mock_bridge_server):
    mock_bridge_server.expect_request("ProfilerCapabilities").respond_with(
        {"analyzable": False, "reason": "not in PlayMode", "remedy": "enter_playmode first"}
    )
    r = await profile_performance(scenarios=[{"label": "current"}])
    assert r["analyzable"] is False
    assert r["reason"] == "not in PlayMode"
    assert r["remedy"] == "enter_playmode first"
    # No baseline recorded when not analyzable.
    assert PROFILER_CACHE.baseline is None


@_pytest.mark.asyncio
async def test_first_call_records_baseline(unity_bridge_session, mock_bridge_server):
    mock_bridge_server.expect_request("ProfilerCapabilities").respond_with(
        {"analyzable": True, "playMode": True, "recordersAvailable": True,
         "memoryProfilerInstalled": False, "availableMetrics": {"cpu": ["fps"], "memory": []}}
    )
    mock_bridge_server.expect_request(
        "ProfilerSample",
        args_predicate=lambda a: a.get("scenario", {}).get("label") == "current",
    ).respond_with(
        {"label": "current", "mode": "cpu", "framesSampled": 120,
         "metrics": {"fps": {"avg": 60.0, "min": 58, "max": 62, "median": 60, "p95": 61},
                     "frameTimeMs": {"avg": 16.6, "min": 16, "max": 17, "median": 16.6, "p95": 17}}}
    )
    r = await profile_performance(scenarios=[{"label": "current"}], mode="cpu")
    assert r["phase"] == "baseline"
    assert r["mode"] == "cpu"
    assert len(r["scenarios"]) == 1 and r["scenarios"][0]["label"] == "current"
    assert "lua_runway" in r
    assert PROFILER_CACHE.baseline is not None
    assert PROFILER_CACHE.baseline.created_call_seq >= 1


# ---------------------------------------------------------------- diff (T4, T6, T11)
from unity_mcp_py.tools.profile_performance import _diff, _metric_direction
from unity_mcp_py.state.profiler_cache import Baseline


def _mk_run(label, **metric_avgs):
    return {"label": label, "metrics": {m: {"avg": v} for m, v in metric_avgs.items()}}


def test_metric_direction():
    assert _metric_direction("fps") == "higher"
    assert _metric_direction("frameTimeMs") == "lower"


def test_diff_flags_regression_lower_is_better():
    # frameTimeMs is lower-is-better; +10% is a regression.
    base = Baseline("cpu", [{"label": "s"}], [_mk_run("s", frameTimeMs=16.0)], 1)
    after = [_mk_run("s", frameTimeMs=18.0)]
    deltas = _diff(base, after)
    d = deltas[0]["metrics"]["frameTimeMs"]
    assert d["verdict"] == "regressed"
    assert d["delta_pct"] > 0


def test_diff_flags_improvement_lower_is_better():
    base = Baseline("cpu", [{"label": "s"}], [_mk_run("s", frameTimeMs=16.0)], 1)
    after = [_mk_run("s", frameTimeMs=14.0)]
    deltas = _diff(base, after)
    assert deltas[0]["metrics"]["frameTimeMs"]["verdict"] == "improved"


def test_diff_fps_higher_is_better():
    # fps is higher-is-better; a drop is a regression.
    base = Baseline("cpu", [{"label": "s"}], [_mk_run("s", fps=60.0)], 1)
    after = [_mk_run("s", fps=50.0)]
    deltas = _diff(base, after)
    assert deltas[0]["metrics"]["fps"]["verdict"] == "regressed"


def test_diff_unchanged_within_threshold():
    base = Baseline("cpu", [{"label": "s"}], [_mk_run("s", frameTimeMs=16.0)], 1)
    after = [_mk_run("s", frameTimeMs=16.3)]  # ~1.9% < 5%
    deltas = _diff(base, after)
    assert deltas[0]["metrics"]["frameTimeMs"]["verdict"] == "unchanged"


def test_diff_threshold_env_override(monkeypatch):
    monkeypatch.setenv("UNITY_MCP_PROFILER_THRESHOLD_PCT", "1")
    base = Baseline("cpu", [{"label": "s"}], [_mk_run("s", frameTimeMs=16.0)], 1)
    after = [_mk_run("s", frameTimeMs=16.3)]  # ~1.9% now exceeds 1%
    deltas = _diff(base, after)
    assert deltas[0]["metrics"]["frameTimeMs"]["verdict"] == "regressed"


def test_diff_unmatched_scenario_reported():
    base = Baseline("cpu", [{"label": "only_in_base"}], [_mk_run("only_in_base", fps=60)], 1)
    after = [_mk_run("only_in_after", fps=60)]
    deltas = _diff(base, after)
    labels = {d["label"]: d for d in deltas}
    assert labels["only_in_base"]["status"] == "unmatched"
    assert labels["only_in_after"]["status"] == "unmatched"


def test_diff_carries_raw_aggregates():
    # §5.5: the compare output must expose the full raw before/after aggregates,
    # not just avg, so the agent can reason beyond the threshold verdict.
    base_run = {"label": "s", "metrics": {"frameTimeMs": {
        "min": 15, "max": 18, "avg": 16.0, "median": 16, "p95": 17.5, "samples": 120}}}
    after_run = {"label": "s", "metrics": {"frameTimeMs": {
        "min": 19, "max": 22, "avg": 20.0, "median": 20, "p95": 21.5, "samples": 120}}}
    base = Baseline("cpu", [{"label": "s"}], [base_run], 1)
    d = _diff(base, [after_run])[0]["metrics"]["frameTimeMs"]
    assert d["raw_before"]["p95"] == 17.5 and d["raw_before"]["min"] == 15
    assert d["raw_after"]["p95"] == 21.5 and d["raw_after"]["max"] == 22
    assert d["raw_before"]["samples"] == 120


def test_diff_none_label_run_is_unmatched_not_silently_matched():
    # A bridge response missing "label" must not match another label-less run.
    base = Baseline("cpu", [{"label": "s"}], [{"metrics": {"fps": {"avg": 60}}}], 1)  # no label
    after = [{"metrics": {"fps": {"avg": 30}}}]  # also no label
    deltas = _diff(base, after)
    assert all(d.get("status") == "unmatched" for d in deltas)
    assert all(d["label"] is None for d in deltas)


@_pytest.mark.asyncio
async def test_second_call_compares(unity_bridge_session, mock_bridge_server):
    caps = {"analyzable": True, "playMode": True, "recordersAvailable": True,
            "memoryProfilerInstalled": False, "availableMetrics": {"cpu": ["fps"], "memory": []}}
    # Baseline call.
    mock_bridge_server.expect_request("ProfilerCapabilities").respond_with(caps)
    mock_bridge_server.expect_request("ProfilerSample").respond_with(
        {"label": "current", "mode": "cpu", "framesSampled": 120,
         "metrics": {"frameTimeMs": {"avg": 16.0}}})
    await profile_performance(scenarios=[{"label": "current"}], mode="cpu")
    # Compare call (scenarios omitted → reuse baseline matrix).
    mock_bridge_server.expect_request("ProfilerCapabilities").respond_with(caps)
    mock_bridge_server.expect_request("ProfilerSample").respond_with(
        {"label": "current", "mode": "cpu", "framesSampled": 120,
         "metrics": {"frameTimeMs": {"avg": 20.0}}})
    r = await profile_performance(mode="cpu")
    assert r["phase"] == "compare"
    assert len(r["regressions"]) == 1


# ---------------------------------------------------------------- invariants (T7, T10, T12)
@_pytest.mark.asyncio
async def test_disconnect_does_not_clear_cache(unity_bridge_session, mock_bridge_server):
    caps = {"analyzable": True, "playMode": True, "recordersAvailable": True,
            "memoryProfilerInstalled": False, "availableMetrics": {"cpu": ["fps"], "memory": []}}
    mock_bridge_server.expect_request("ProfilerCapabilities").respond_with(caps)
    mock_bridge_server.expect_request("ProfilerSample").respond_with(
        {"label": "current", "mode": "cpu", "framesSampled": 120,
         "metrics": {"frameTimeMs": {"avg": 16.0}}})
    await profile_performance(scenarios=[{"label": "current"}], mode="cpu")
    assert PROFILER_CACHE.baseline is not None

    # Simulate the bridge dropping: close the session. The cache MUST survive.
    await unity_bridge_session.close()
    assert PROFILER_CACHE.baseline is not None  # T7: never cleared on transport loss


@_pytest.mark.asyncio
async def test_memory_snapshot_absent_package(unity_bridge_session, mock_bridge_server):
    mock_bridge_server.expect_request("ProfilerCapabilities").respond_with(
        {"analyzable": True, "playMode": True, "recordersAvailable": True,
         "memoryProfilerInstalled": False, "availableMetrics": {"cpu": [], "memory": ["totalUsed"]}})
    mock_bridge_server.expect_request("ProfilerSample").respond_with(
        {"label": "current", "mode": "memory", "framesSampled": 120,
         "metrics": {"totalUsed": {"avg": 1234.0}}})
    mock_bridge_server.expect_request("MemorySnapshot").respond_with(
        {"available": False, "reason": "com.unity.memoryprofiler not installed",
         "remedy": "Install via Package Manager to enable heap snapshots"})
    r = await profile_performance(scenarios=[{"label": "current"}], mode="memory", memory_snapshot=True)
    assert r["memory_snapshot"]["available"] is False
    # counter memory still came through
    assert r["scenarios"][0]["metrics"]["totalUsed"]["avg"] == 1234.0


@_pytest.mark.asyncio
async def test_records_soft_grounding(unity_bridge_session, mock_bridge_server):
    from unity_mcp_py.state import LUA_BUDGET
    await LUA_BUDGET.reset()
    mock_bridge_server.expect_request("ProfilerCapabilities").respond_with(
        {"analyzable": True, "playMode": True, "recordersAvailable": True,
         "memoryProfilerInstalled": False, "availableMetrics": {"cpu": ["fps"], "memory": []}})
    mock_bridge_server.expect_request("ProfilerSample").respond_with(
        {"label": "current", "mode": "cpu", "framesSampled": 120, "metrics": {"fps": {"avg": 60.0}}})
    r = await profile_performance(scenarios=[{"label": "current"}], mode="cpu")
    assert "lua_runway" in r
    assert r["lua_runway"]["grounded"] is True
