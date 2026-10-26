"""Escalation gate: count consecutive same-goal verify_assertions failures.

When the agent drives the game with lua_eval command-line operations (e.g.
PROJECT_RUNTIME.global.ui:open(uid)) that do NOT flow through the real data path, a panel can
come up as a hollow shell whose verification diverges from real user interaction.
verify_assertions failing repeatedly under the SAME goal is the objective signal
that the lua path won't get there — the agent should escalate_via_click (restore
the lua op, re-enter via a real click).

Process-local, no persistence across restarts (matches LuaRunwayBudget /
WriteOpGuard). Pure in-memory (asyncio.Lock only) — never touches the bridge or
disk, so it adds no latency to the verify path.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ._env_utils import _read_positive_int_env

_DEFAULT_THRESHOLD = 2
_ENV_THRESHOLD = "UNITY_MCP_ESCALATE_THRESHOLD"


@dataclass(frozen=True)
class EscalationOutcome:
    """Immutable result of a record_verify() call, surfaced by verify_assertions."""

    should_escalate: bool
    goal: str | None
    fail_count: int


def escalation_response(outcome: EscalationOutcome) -> dict:
    """Build the standard ``escalation`` verdict block from an outcome.

    Only call this when ``outcome.should_escalate`` is True.
    """
    return {
        "should_escalate": True,
        "goal": outcome.goal,
        "fail_count": outcome.fail_count,
        "advice": (
            f"lua 路径已连续 {outcome.fail_count} 次验证失败，疑似命令行操作未携带"
            "真实数据。请用 escalate_via_click：传入 undo_lua 还原之前的 lua 操作"
            "（如 PROJECT_RUNTIME.global.ui:close(uid)），再以 simulate_click 定位参数走真实入口"
            "重新进入，让后续截图/断言面对真实环境。"
        ),
    }


class EscalationGate:
    """Process-local consecutive same-goal verify-failure counter. See module docstring."""

    def __init__(self, threshold: int | None = None) -> None:
        self.threshold = (
            threshold if threshold is not None
            else _read_positive_int_env(_ENV_THRESHOLD, _DEFAULT_THRESHOLD)
        )
        self._goal: str | None = None
        self._fail_count = 0
        self._lock = asyncio.Lock()

    async def record_verify(self, goal: str | None, passed: bool) -> EscalationOutcome:
        """Record one verify verdict and decide whether to escalate.

        Precedence:
          1. goal is None  -> no-op (back-compat); counters untouched.
          2. passed is True -> reset to (goal, 0).
          3. passed is False:
               goal != current -> rebase to (goal, 1)   [this failure is the 1st]
               else             -> fail_count += 1
             should_escalate = fail_count >= threshold.
        """
        async with self._lock:
            if goal is None:
                return EscalationOutcome(False, self._goal, self._fail_count)
            if passed:
                self._goal = goal
                self._fail_count = 0
                return EscalationOutcome(False, goal, 0)
            if goal != self._goal:
                self._goal = goal
                self._fail_count = 1
            else:
                self._fail_count += 1
            return EscalationOutcome(
                self._fail_count >= self.threshold, goal, self._fail_count
            )

    async def clear(self, goal: str) -> bool:
        """Reset the counter for ``goal`` (escalate_via_click success).

        Returns True if the counter was actually cleared, False if it was a
        no-op. No-op when the gate's current goal is not ``goal`` (single-slot
        design: if the active goal switched since escalation was signalled — e.g.
        an interleaved verify_assertions for a different goal rebased the gate —
        the old goal's counter no longer exists, so there is nothing to clear and
        the new goal's counter is left untouched). Callers should surface this
        boolean rather than assuming the clear succeeded.
        """
        async with self._lock:
            if self._goal == goal:
                self._fail_count = 0
                return True
            return False

    async def reset(self) -> None:
        """Zero all state (test isolation)."""
        async with self._lock:
            self._goal = None
            self._fail_count = 0

    async def snapshot(self) -> dict[str, str | int | None]:
        """Introspection view (tests)."""
        async with self._lock:
            return {
                "goal": self._goal,
                "fail_count": self._fail_count,
                "threshold": self.threshold,
            }


# Module-level singleton (process-wide)
ESCALATION_GATE = EscalationGate()
