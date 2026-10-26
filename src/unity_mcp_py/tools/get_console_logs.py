"""get_console_logs tool. Incremental log pull with level/pattern/limit filters.

Read-only. Reuses bridge TailEditorLog. No write-op token.
"""
from __future__ import annotations

import re
from typing import Any

from ..bridge.session import bridge_call
from ..recorder import RECORDER
from ..state import LUA_BUDGET, runway_response

TOOL_NAME = "get_console_logs"
_MAX_BRIDGE_LOG_LINES = 500

_VALID_LEVELS = frozenset({"Log", "Warning", "Error", "Exception", "Assert"})


async def get_console_logs(
    since_seq: int | None = None,
    level: list[str] | None = None,
    pattern: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    effective_since = since_seq if since_seq is not None else RECORDER.get_cursor()
    effective_limit = max(1, int(limit))
    bridge_limit = min(effective_limit, _MAX_BRIDGE_LOG_LINES)

    tail_args: dict[str, Any] = {"maxLines": bridge_limit}
    if effective_since > 0:
        tail_args["sinceLineNumber"] = effective_since
    resp = await bridge_call("TailEditorLog", tail_args)

    raw_lines = resp.get("lines", []) or []

    level_set: set[str] | None = None
    if level:
        level_set = {lv for lv in level if lv in _VALID_LEVELS}

    regex: re.Pattern[str] | None = None
    if pattern:
        regex = re.compile(pattern)

    filtered: list[dict[str, Any]] = []
    for ln in raw_lines:
        lv = ln.get("logType", "")
        msg = ln.get("message", "")
        if level_set is not None and lv not in level_set:
            continue
        if regex is not None and not regex.search(msg):
            continue
        filtered.append({
            "seq": ln.get("lineNumber", 0),
            "level": lv,
            "message": msg,
            "stack_trace": ln.get("stackTrace", ""),
        })

    truncated = len(filtered) > effective_limit
    if truncated:
        filtered = filtered[-effective_limit:]

    next_seq = raw_lines[-1].get("lineNumber", 0) if raw_lines else effective_since

    # Soft-ground the lua runway: this read completed a real bridge round-trip, so
    # it counts as the agent looking at reality and resets since_ground (capped by
    # soft_limit). An empty log response still counts — it is a real round-trip.
    outcome = await LUA_BUDGET.ground(hard=False)

    return {
        "lines": filtered,
        "next_seq": next_seq,
        "truncated": truncated,
        "cursor": RECORDER.get_cursor(),
        "lua_runway": runway_response(outcome, LUA_BUDGET),
    }


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        since_seq: int | None = None,
        level: list[str] | None = None,
        pattern: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return await get_console_logs(since_seq, level, pattern, limit)