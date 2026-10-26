"""profile_performance — OPTIONAL tool (dark by default). See spec 2026-06-12.

Registers only when UNITY_MCP_ENABLE_PROFILER=1 (see server._OPTIONAL_FEATURES).
Samples Unity engine ProfilerRecorder counters across a scenario matrix via the
C# bridge, self-records a disconnect-surviving baseline (state/profiler_cache.py),
and returns a pure before/after regression diff. Risk attribution is the agent's
job (spec D6).

It mutates live Editor state (quality level / target fps / game-view size) during
a run and ALWAYS restores it; it issues no write_op_token (its evidence is the
returned report). It soft-grounds the lua runway like get_console_logs.
"""
from __future__ import annotations

import os
from typing import Any

from ..bridge.session import bridge_call
from ..state import LUA_BUDGET, PROFILER_CACHE, runway_response
from ..state.profiler_cache import Baseline

TOOL_NAME = "profile_performance"

# Bounds (spec §5.4).
_MAX_SCENARIOS = 8
_MIN_SAMPLE_FRAMES = 10
_MAX_SAMPLE_FRAMES = 1200
_MIN_WARMUP_FRAMES = 0
_MAX_WARMUP_FRAMES = 300
_VALID_MODES = frozenset({"cpu", "memory", "both"})

# Whitelisted quality_overrides keys (spec §5.4). Unknown key → INVALID_REQUEST
# (never silently ignored — avoids "I set a knob that did nothing").
_ALLOWED_QUALITY_KEYS = frozenset({
    "shadowDistance",
    "lodBias",
    "masterTextureLimit",
    "pixelLightCount",
    "antiAliasing",
    "shadowResolution",
    "shadowCascades",
    "vSyncCount",
})

_DEFAULT_THRESHOLD_PCT = 5.0
_ENV_THRESHOLD = "UNITY_MCP_PROFILER_THRESHOLD_PCT"


class InvalidProfileArg(ValueError):
    """Raised when scenarios/mode/frames are invalid, before any bridge call."""


def _validate_scenarios(
    scenarios: list[dict] | None,
    baseline: Baseline | None,
) -> list[dict]:
    """Resolve + validate the scenario matrix.

    None + no baseline  → [{"label": "current"}] (Editor as-is).
    None + baseline      → reuse the baseline's matrix (so compare matches by
                           label instead of going all-unmatched — spec §5.2/§5.5).
                           NOTE: this returns baseline.scenarios BY REFERENCE; callers
                           must not mutate the returned list in that case.
    Otherwise validate the supplied list.
    """
    if scenarios is None:
        if baseline is not None:
            return baseline.scenarios
        return [{"label": "current"}]

    if not isinstance(scenarios, list) or len(scenarios) == 0:
        raise InvalidProfileArg("scenarios must be a non-empty list or omitted")
    if len(scenarios) > _MAX_SCENARIOS:
        raise InvalidProfileArg(
            f"scenarios must contain at most {_MAX_SCENARIOS} entries "
            f"(got {len(scenarios)}); split a larger matrix across calls"
        )

    seen: set[str] = set()
    for sc in scenarios:
        if not isinstance(sc, dict):
            raise InvalidProfileArg("each scenario must be an object")
        label = sc.get("label")
        if not isinstance(label, str) or not label.strip():
            raise InvalidProfileArg("each scenario needs a non-empty 'label'")
        if label in seen:
            raise InvalidProfileArg(f"scenario label must be unique within the call: {label!r}")
        seen.add(label)
        overrides = sc.get("quality_overrides")
        if overrides is not None:
            if not isinstance(overrides, dict):
                raise InvalidProfileArg("quality_overrides must be an object")
            unknown = set(overrides) - _ALLOWED_QUALITY_KEYS
            if unknown:
                raise InvalidProfileArg(
                    f"unknown quality_overrides key(s): {sorted(unknown)}; "
                    f"allowed: {sorted(_ALLOWED_QUALITY_KEYS)}"
                )
        # Clamp warmup_frames to [0, 300] and write it back so both _timeout_for
        # (Python) and the C# coroutine see the bounded value (spec §5.4). A
        # non-int is rejected fail-fast rather than silently coerced.
        if "warmup_frames" in sc:
            wf = sc["warmup_frames"]
            if not isinstance(wf, int) or isinstance(wf, bool):
                raise InvalidProfileArg("warmup_frames must be an integer")
            sc["warmup_frames"] = max(_MIN_WARMUP_FRAMES, min(wf, _MAX_WARMUP_FRAMES))
        # Type-check the remaining knobs at the boundary so a bad value fails fast as
        # INVALID_REQUEST here rather than throwing deep in the C# coroutine (which the
        # agent would see as an opaque INTERNAL error). The bridge still resolves the
        # semantics (quality level name lookup, etc.); we only guard the shapes.
        if "target_fps" in sc:
            tf = sc["target_fps"]
            if not isinstance(tf, int) or isinstance(tf, bool):
                raise InvalidProfileArg("target_fps must be an integer")
        if "resolution" in sc:
            res = sc["resolution"]
            if (not isinstance(res, (list, tuple)) or len(res) != 2
                    or not all(isinstance(d, int) and not isinstance(d, bool) for d in res)):
                raise InvalidProfileArg("resolution must be a [width, height] pair of integers")
    return scenarios


def _validate_mode(mode: str) -> str:
    if mode not in _VALID_MODES:
        raise InvalidProfileArg(f"mode must be one of {sorted(_VALID_MODES)} (got {mode!r})")
    return mode


def _clamp_frames(sample_frames: int) -> int:
    if not isinstance(sample_frames, int) or isinstance(sample_frames, bool):
        raise InvalidProfileArg("sample_frames must be an integer")
    return max(_MIN_SAMPLE_FRAMES, min(sample_frames, _MAX_SAMPLE_FRAMES))


def _timeout_for(sample_frames: int, warmup_frames: int = 30) -> float:
    """Generous socket headroom so a slow low-quality scenario does not trip the
    default 30s bridge timeout, while a true hang stays bounded (spec §6.5).
    Assumes a pessimistic ~10fps floor + 15s slack, hard-capped at 180s.
    """
    total_frames = sample_frames + warmup_frames
    return min(180.0, total_frames / 10.0 + 15.0)


# Metric direction: which way is BAD. lower-is-better metrics regress when they
# rise; higher-is-better (fps) regresses when it falls. Unlisted metrics default
# to lower-is-better (cost-like). Spec §5.5.
_HIGHER_IS_BETTER = frozenset({"fps"})


def _metric_direction(metric: str) -> str:
    return "higher" if metric in _HIGHER_IS_BETTER else "lower"


def _threshold_pct() -> float:
    raw = os.environ.get(_ENV_THRESHOLD)
    if raw is None:
        return _DEFAULT_THRESHOLD_PCT
    try:
        v = float(raw)
        return v if v > 0 else _DEFAULT_THRESHOLD_PCT
    except ValueError:
        return _DEFAULT_THRESHOLD_PCT


def _classify(metric: str, before_agg: dict, after_agg: dict, threshold: float) -> dict:
    """Classify one metric. The verdict is computed on `avg`, but the FULL raw
    aggregates (min/max/avg/median/p95/samples) of BOTH sides are carried into the
    output so the agent can reason beyond the threshold verdict (spec §5.5).

    Precondition: both before_agg and after_agg must contain a numeric 'avg'
    (the _diff caller guards this). The 0.0 fallback below is only a last-resort
    guard against a malformed agg dict; it is not a supported input shape.
    """
    epsilon = 1e-9
    before = float(before_agg.get("avg", 0.0))
    after = float(after_agg.get("avg", 0.0))
    delta_pct = (after - before) / max(abs(before), epsilon) * 100.0
    direction = _metric_direction(metric)
    # Normalize so a positive "bad_pct" always means "got worse".
    bad_pct = delta_pct if direction == "lower" else -delta_pct
    # Boundary: a change of exactly `threshold`% is "unchanged" (strict >, not >=).
    if bad_pct > threshold:
        verdict = "regressed"
    elif bad_pct < -threshold:
        verdict = "improved"
    else:
        verdict = "unchanged"
    return {
        "before": before,
        "after": after,
        "delta_pct": round(delta_pct, 3),
        "direction": direction,
        "verdict": verdict,
        "raw_before": before_agg,   # full {min,max,avg,median,p95,samples}
        "raw_after": after_agg,
    }


def _diff(baseline: Baseline, runs: list[dict]) -> list[dict]:
    """Per-scenario (by label), per-metric direction-aware diff. Unmatched
    scenarios (label in only one side) are reported with status=unmatched, never
    dropped (spec §5.5). Each metric delta carries both sides' full raw aggregates."""
    threshold = _threshold_pct()
    by_label_before = {r.get("label"): r for r in baseline.runs}
    by_label_after = {r.get("label"): r for r in runs}
    all_labels = list(by_label_before.keys()) + [
        l for l in by_label_after if l not in by_label_before
    ]

    out: list[dict] = []
    for label in all_labels:
        if label is None:
            # A run without a "label" is malformed (validation guarantees a label
            # on the request side; this guards against a bad bridge response).
            out.append({"label": None, "status": "unmatched"})
            continue
        b = by_label_before.get(label)
        a = by_label_after.get(label)
        if b is None or a is None:
            out.append({"label": label, "status": "unmatched"})
            continue
        b_metrics = b.get("metrics", {})
        a_metrics = a.get("metrics", {})
        metric_deltas: dict[str, dict] = {}
        for metric in set(b_metrics) & set(a_metrics):
            b_agg = b_metrics[metric]
            a_agg = a_metrics[metric]
            if b_agg.get("avg") is None or a_agg.get("avg") is None:
                continue
            metric_deltas[metric] = _classify(metric, b_agg, a_agg, threshold)
        verdict = "regressed" if any(
            m["verdict"] == "regressed" for m in metric_deltas.values()
        ) else "ok"
        out.append({
            "label": label,
            "status": "matched",
            "verdict": verdict,
            "metrics": metric_deltas,
        })
    return out


async def profile_performance(
    scenarios: list[dict] | None = None,
    mode: str = "cpu",
    sample_frames: int = 120,
    memory_snapshot: bool = False,
    reset: bool = False,
) -> dict[str, Any]:
    # 1. reset is a pure, bridge-free purge.
    if reset:
        PROFILER_CACHE.clear()
        return {"phase": "reset", "cleared": True}

    mode = _validate_mode(mode)
    frames = _clamp_frames(sample_frames)

    # 2. Feasibility probe. analyzable:false is a first-class result, not an error.
    caps = await bridge_call("ProfilerCapabilities")
    if not caps.get("analyzable", False):
        return {
            "analyzable": False,
            "reason": caps.get("reason", "profiling not available"),
            "remedy": caps.get("remedy", ""),
        }

    # 3. Resolve the matrix (reuse baseline's on a compare call with scenarios omitted).
    baseline = PROFILER_CACHE.baseline
    scenario_list = _validate_scenarios(scenarios, baseline)

    # 4. Sample each scenario (one bridge round-trip per scenario).
    runs: list[dict[str, Any]] = []
    for sc in scenario_list:
        r = await bridge_call(
            "ProfilerSample",
            {"scenario": sc, "mode": mode, "sampleFrames": frames},
            response_timeout=_timeout_for(frames, int(sc.get("warmup_frames", 30))),
        )
        runs.append(r)

    snap = None
    if memory_snapshot:
        snap = await bridge_call(
            "MemorySnapshot", {}, response_timeout=_timeout_for(frames)
        )

    # 5. Self-phasing: empty cache → record baseline; baseline present → compare.
    outcome = await LUA_BUDGET.ground(hard=False)
    if baseline is None:
        PROFILER_CACHE.set_baseline(mode, scenario_list, runs)
        resp: dict[str, Any] = {
            "phase": "baseline",
            "mode": mode,
            "scenarios": runs,
            "lua_runway": runway_response(outcome, LUA_BUDGET),
        }
        if snap is not None:
            resp["memory_snapshot"] = snap
        return resp

    # compare branch: diff this run-set against the cached baseline, by scenario label.
    deltas = _diff(baseline, runs)
    age = PROFILER_CACHE.note_compare()   # true age: compares against THIS baseline
    resp = {
        "phase": "compare",
        "mode": mode,
        "deltas": deltas,
        "regressions": [d for d in deltas if d.get("verdict") == "regressed"],
        "baseline_age_calls": age,
        "lua_runway": runway_response(outcome, LUA_BUDGET),
    }
    if snap is not None:
        resp["memory_snapshot"] = snap
    return resp


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        scenarios: list[dict] | None = None,
        mode: str = "cpu",
        sample_frames: int = 120,
        memory_snapshot: bool = False,
        reset: bool = False,
    ) -> dict[str, Any]:
        return await profile_performance(
            scenarios, mode, sample_frames, memory_snapshot, reset
        )
