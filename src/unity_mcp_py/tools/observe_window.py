"""observe_window tool. Read-only. No token (it only samples, never mutates).

Polls a pure boolean/number Lua expression each frame over a time window and reports
whether/when it was ever true and the sampled series. This is how you prove a
TRANSIENT thing happened (an effect played, a camera blend completed, an entity
reached a state) — a single observe would miss the sub-second window.

The bridge holds the response for the whole window, so a long response_timeout is
used. luaExpr must be a PURE expression (no assignment, no statements) — that
restriction is enforced both here and on the bridge. For arbitrary code use lua_eval.

PlayMode-only.
"""
from __future__ import annotations

import re
from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "observe_window"

_MIN_INTERVAL_MS = 10
_MAX_INTERVAL_MS = 5000
_MIN_DURATION_MS = 50
_MAX_DURATION_MS = 120000
_MAX_EXPR_LEN = 400

# Statements / dangerous identifiers that must never appear in a pure expression.
_BANNED = re.compile(
    r"\b(function|do|end|while|for|repeat|until|goto|os|io|require|load|loadstring|dofile|DoString)\b"
)


class InvalidObserveWindowArg(ValueError):
    """Raised when the expression/window is invalid before any bridge call."""


def _validate_expr(expr: str) -> None:
    if not isinstance(expr, str) or len(expr.strip()) == 0:
        raise InvalidObserveWindowArg("lua_expr must be a non-empty string")
    if len(expr) > _MAX_EXPR_LEN:
        raise InvalidObserveWindowArg(f"lua_expr exceeds {_MAX_EXPR_LEN} chars")
    if ";" in expr:
        raise InvalidObserveWindowArg("lua_expr must be a single expression (no ';')")
    # Reject a bare '=' assignment (but allow ==, ~=, <=, >=).
    for i, ch in enumerate(expr):
        if ch != "=":
            continue
        prev = expr[i - 1] if i > 0 else ""
        nxt = expr[i + 1] if i + 1 < len(expr) else ""
        if prev in "=~<>" or nxt == "=":
            continue
        raise InvalidObserveWindowArg("lua_expr must not contain assignment '='")
    if _BANNED.search(expr):
        raise InvalidObserveWindowArg(
            "lua_expr must be a pure expression (no function/do/end/loops/os/io/require/load)"
        )


async def observe_window(
    lua_expr: str,
    interval_ms: int = 50,
    duration_ms: int = 2000,
) -> dict[str, Any]:
    """Sample a pure Lua expression over a time window.

    Args:
        lua_expr: A pure boolean/number expression, e.g.
            "PROJECT_RUNTIME.global.PROJECT_EFFECT_SERVICE:IsEffectPlaying(0, 123)" or
            "#PROJECT_RUNTIME.global.PROJECT_CAMERA_SHAKE_SERVICE.activeShakes". Booleans are coerced to 1/0.
        interval_ms: Sample period (bounded [10, 5000], default 50).
        duration_ms: Total window (bounded [50, 120000], default 2000).

    Returns:
        {everTrue, firstTrueMs, lastTrueMs, peak, sampleCount, elapsedMs, samples:[{t,value}]}.

    Raises:
        InvalidObserveWindowArg: expression not pure / window out of range.
    """
    _validate_expr(lua_expr)
    iv = max(_MIN_INTERVAL_MS, min(int(interval_ms), _MAX_INTERVAL_MS))
    dur = max(_MIN_DURATION_MS, min(int(duration_ms), _MAX_DURATION_MS))

    # The bridge holds the response for the whole window; give the socket headroom.
    response_timeout = max(30.0, dur / 1000.0 + 15.0)
    return await bridge_call(
        "ObserveWindow",
        {"luaExpr": lua_expr, "intervalMs": iv, "durationMs": dur},
        response_timeout=response_timeout,
    )


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        lua_expr: str,
        interval_ms: int = 50,
        duration_ms: int = 2000,
    ) -> dict[str, Any]:
        return await observe_window(lua_expr, interval_ms, duration_ms)
