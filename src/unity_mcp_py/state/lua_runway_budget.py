"""Recoverable grounding runway for lua_eval. See spec 2026-06-09.

Replaces the terminal two-gate goal budget. The runway measures how long the
agent has gone WITHOUT comparing against reality:

  - since_ground: lua executions since the last reality-check (any grounding).
  - soft_used:    soft glances (screenshot/logs/observe) since the last HARD
                  verify. Capped at SOFT_LIMIT so the agent can't dodge real
                  verification by screenshot-spamming.
  - total:        lifetime lua executions; the only terminal backstop.

charge_run() is called on every lua_eval ATTEMPT (before the bridge). When the
runway is exhausted it raises a RECOVERABLE LuaGroundingRequiredError — the agent
grounds itself (a real bridge round-trip in capture_screenshot / get_console_logs
/ observe_evidence / verify_assertions calls ground()), which resets the runway,
and continues. Only the lifetime backstop (LuaRunawayError) is terminal.

Process-local, no persistence across restarts (matches WriteOpGuard).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from ._env_utils import _read_positive_int_env

logger = logging.getLogger("unity_mcp_py")

_DEFAULT_SOFT_THRESHOLD = 5     # un-grounded lua runs before grounding required
_DEFAULT_SOFT_LIMIT = 8         # total soft glances that clear before a hard verify is required
_DEFAULT_RUNAWAY_MAX = 500      # lifetime terminal backstop

_ENV_SOFT_THRESHOLD = "UNITY_MCP_LUA_RUNWAY"
_ENV_SOFT_LIMIT = "UNITY_MCP_LUA_SOFT_LIMIT"
_ENV_RUNAWAY_MAX = "UNITY_MCP_LUA_RUNAWAY_MAX"


class LuaBudgetError(RuntimeError):
    """Base for both runway circuit-breaker errors."""


class LuaGroundingRequiredError(LuaBudgetError):
    """RECOVERABLE: too many lua runs since the last reality-check.

    The agent must ground itself (capture_screenshot / get_console_logs /
    observe_evidence, or verify_assertions for a full re-arm) and then continue.
    """

    def __init__(
        self, since_ground: int, threshold: int, soft_used: int, soft_limit: int
    ) -> None:
        self.since_ground = since_ground
        self.threshold = threshold
        self.soft_used = soft_used
        self.soft_limit = soft_limit
        if soft_used >= soft_limit:
            msg = (
                f"你已连续软观察 {soft_used} 次仍未做硬验证。"
                "请用 **verify_assertions** 把『当前中间态应当满足什么』"
                "写成可裁决的断言并通过，再继续；纯截图/读 log 已不再解锁。"
            )
        else:
            msg = (
                f"你已连续跑了 {since_ground} 次 lua_eval 未与现实比对。"
                "请先用 capture_screenshot / get_console_logs / observe_evidence "
                "看一眼真实状态（或用 verify_assertions 断言中间态符合目标），"
                "确认当前进展与目标一致后再继续。"
            )
        super().__init__(msg)


class LuaRunawayError(LuaBudgetError):
    """TERMINAL: lifetime lua execution backstop reached — almost certainly a loop."""

    def __init__(self, total: int, limit: int) -> None:
        self.total = total
        self.limit = limit
        super().__init__(
            f"本进程已执行 {total} 次 lua_eval，触及失控兜底上限。"
            "这几乎一定是探查陷入死循环——请停止并向用户汇报，必要时重启会话。"
        )


@dataclass(frozen=True)
class GroundOutcome:
    """Immutable result of a ground() call, surfaced by the calling tool.

    ``since_ground`` and ``soft_used`` reflect the POST-ground state (e.g. after a
    successful soft ground, ``since_ground`` is 0 and ``soft_used`` is the new
    incremented value).
    """

    cleared: bool
    reason: Literal["soft", "hard", "soft_limit_reached", "not_grounded"]
    since_ground: int
    soft_used: int


def runway_response(outcome: GroundOutcome, budget: "LuaRunwayBudget") -> dict:
    """Build the standard ``lua_runway`` response block from a ground outcome.

    Shared by every grounding tool so the block shape is identical across the
    server (capture_screenshot / get_console_logs / observe_evidence / verify).

    Reads budget.soft_limit / soft_threshold, which are init-time constants (never
    mutated after construction except by tests), so no lock is needed here.
    """
    return {
        "since_ground": outcome.since_ground,
        "soft_used": outcome.soft_used,
        "soft_limit": budget.soft_limit,
        "threshold": budget.soft_threshold,
        "grounded": outcome.cleared,
    }


class LuaRunwayBudget:
    """Process-local grounding runway. See module docstring."""

    def __init__(
        self,
        soft_threshold: int | None = None,
        soft_limit: int | None = None,
        runaway_max: int | None = None,
    ) -> None:
        self.soft_threshold = (
            soft_threshold if soft_threshold is not None
            else _read_positive_int_env(_ENV_SOFT_THRESHOLD, _DEFAULT_SOFT_THRESHOLD)
        )
        self.soft_limit = (
            soft_limit if soft_limit is not None
            else _read_positive_int_env(_ENV_SOFT_LIMIT, _DEFAULT_SOFT_LIMIT)
        )
        self.runaway_max = (
            runaway_max if runaway_max is not None
            else _read_positive_int_env(_ENV_RUNAWAY_MAX, _DEFAULT_RUNAWAY_MAX)
        )
        self._since_ground = 0
        self._soft_used = 0
        self._total = 0
        self._lock = asyncio.Lock()

    async def charge_run(self) -> tuple[int, int]:
        """Charge one lua execution attempt. Returns (since_ground, total).

        Raises LuaRunawayError (terminal) if the lifetime backstop is reached —
        checked FIRST so a runaway can't be masked. Raises LuaGroundingRequiredError
        (recoverable) if the runway is exhausted. Either raise leaves all counters
        unchanged (the over-runway attempt is refused with zero effect).
        """
        async with self._lock:
            if self._total >= self.runaway_max:
                raise LuaRunawayError(self._total, self.runaway_max)
            if self._since_ground >= self.soft_threshold:
                raise LuaGroundingRequiredError(
                    self._since_ground, self.soft_threshold,
                    self._soft_used, self.soft_limit,
                )
            self._since_ground += 1
            self._total += 1
            return (self._since_ground, self._total)

    async def ground(self, hard: bool) -> GroundOutcome:
        """Reset the runway after a real reality-check.

        hard=True (verify_assertions): always clears since_ground AND soft_used.
        hard=False (screenshot/logs/observe): clears since_ground and bumps
        soft_used, but ONLY while soft_used < soft_limit. Once the soft budget is
        spent, a soft ground does not clear (cleared=False) — forcing a hard verify.

        The returned GroundOutcome fields reflect post-ground state.
        """
        async with self._lock:
            if hard:
                self._since_ground = 0
                self._soft_used = 0
                return GroundOutcome(True, "hard", 0, 0)
            if self._soft_used >= self.soft_limit:
                return GroundOutcome(
                    False, "soft_limit_reached", self._since_ground, self._soft_used
                )
            # soft_used is bumped regardless of since_ground (even when there is
            # nothing to clear, e.g. a glance before any lua_eval) — it bounds TOTAL
            # soft glances per verify cycle, not just runway-clearing ones.
            self._since_ground = 0
            self._soft_used += 1
            return GroundOutcome(True, "soft", 0, self._soft_used)

    async def reset(self) -> None:
        """Zero all counters (test isolation)."""
        async with self._lock:
            self._since_ground = 0
            self._soft_used = 0
            self._total = 0

    async def snapshot(self) -> dict[str, int]:
        """Return a copy of the three counters (introspection/tests)."""
        async with self._lock:
            return {
                "since_ground": self._since_ground,
                "soft_used": self._soft_used,
                "total": self._total,
            }

    async def snapshot_outcome(self) -> "GroundOutcome":
        """A non-mutating GroundOutcome view (cleared=False) for callers that did
        not actually ground (e.g. a screenshot that returned no frame)."""
        async with self._lock:
            return GroundOutcome(False, "not_grounded", self._since_ground, self._soft_used)


# Module-level singleton (process-wide)
LUA_BUDGET = LuaRunwayBudget()
