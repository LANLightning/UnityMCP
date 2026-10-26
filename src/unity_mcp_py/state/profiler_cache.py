"""Process-lifetime baseline cache for the optional profile_performance tool.

Modeled on state/lua_runway_budget.py's module-level singleton. NO disk
persistence: the cache is process-local, so it survives a bridge
disconnect/reconnect (a dropped TCP link never touches the Python heap) but a
server restart starts fresh — correct, since there is no meaningful pre-restart
baseline.

The singleton is NEVER cleared on a BridgeConnectionError / session close(). It
is purged only by an explicit profile_performance(reset=True) call (which routes
to .clear()) or by process exit. This is the spec's "不会由于断开 bridge 删除缓存"
requirement (locked by test T7).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass
class Baseline:
    """One recorded baseline run-set, diffed against on later compare calls."""

    mode: str                       # "cpu" | "memory" | "both"
    scenarios: list[dict[str, Any]]  # scenario DEFINITIONS (reused if compare omits them)
    runs: list[dict[str, Any]]       # raw aggregated per-scenario samples (diff key = run label)
    created_call_seq: int            # monotonic id of the call that recorded this baseline


class ProfilerCache:
    """Process-local baseline holder. See module docstring for lifecycle rules."""

    def __init__(self) -> None:
        self._baseline: Baseline | None = None
        self._seq = 0
        self._compares_since_baseline = 0

    @property
    def baseline(self) -> Baseline | None:
        return self._baseline

    @property
    def compares_since_baseline(self) -> int:
        """How many compare calls have run against the CURRENT baseline (a true
        'age', resets to 0 on set_baseline). This is what the tool returns as
        `baseline_age_calls` — distinct from the baseline's fixed creation seq."""
        return self._compares_since_baseline

    def set_baseline(
        self,
        mode: str,
        scenarios: list[dict[str, Any]],
        runs: list[dict[str, Any]],
    ) -> None:
        self._seq += 1
        self._compares_since_baseline = 0
        self._baseline = Baseline(
            mode=mode,
            scenarios=copy.deepcopy(scenarios),   # deep copy: protect nested quality_overrides too
            runs=copy.deepcopy(runs),
            created_call_seq=self._seq,
        )

    def note_compare(self) -> int:
        """Record one compare against the current baseline; return the new age.

        Caller must ensure a baseline is set first (the tool only calls this on
        the compare branch, i.e. when baseline is not None).
        """
        self._compares_since_baseline += 1
        return self._compares_since_baseline

    def clear(self) -> None:
        """The ONLY purge besides process exit. Routed from reset=True."""
        self._baseline = None
        self._compares_since_baseline = 0
        # _seq is intentionally NOT reset: created_call_seq is a process-lifetime id,
        # not a per-baseline ordinal. User-facing "age" is compares_since_baseline.


# Module-level singleton (process-wide). Survives bridge disconnect by design.
PROFILER_CACHE = ProfilerCache()
