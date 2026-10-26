"""observe_evidence tool. See spec §5.2.

Returns ONLY raw runtime artifacts. No interpretation.
Calls bridge sequentially (BridgeClient is single-connection, serialized).

PERF 2026-05-25: ``include_screenshot`` defaults to False. CaptureScreenshot is
the single largest cost in the default observe path — it triggers a Unity main-
thread GPU readback (~300ms wall-clock) and inflates the response payload by
1-3 MB of base64. Callers that need a frame must opt in.

PERF 2026-06-06 (Feature 3): the default response is now SLIM. The recurring
"is it still fine?" poll dominates verification, and most of its payload was
chatty Info-level logs, repeated lines, and verbose scene-object component
dumps the model never read. Slim mode (default):
  - filters logs to {Warning, Error, Exception, Assert} (override via ``log_levels``)
  - run-length-collapses consecutive identical messages into ``repeat: N``
  - caps each message / stack_trace to ~2KB
  - collapses scene_objects to {name, active, child_count, component_count}
  - adds a one-line ``verdict`` summary ("errors:N warnings:M play:X roots:K")
Pass ``verbose=True`` for the legacy full shape (no severity filter, raw
rootObjects passthrough, untruncated fields).

``max_log_lines`` caps the TailEditorLog request so chatty editor logs don't
balloon the response. Default 500 matches the bridge-side cap.
"""
from __future__ import annotations
import logging
from typing import Any

from ..bridge.session import bridge_call
from ..state import GUARD, LUA_BUDGET, runway_response

logger = logging.getLogger(__name__)

TOOL_NAME = "observe_evidence"

_DEFAULT_MAX_LOG_LINES = 500

# Slim-mode log severity allowlist. Mirrors get_console_logs._VALID_LEVELS minus
# the chatty "Log" level. Info/Debug logs rarely matter for a pass/fail poll.
_DEFAULT_SLIM_LEVELS = frozenset({"Warning", "Error", "Exception", "Assert"})

# Per-field character cap in slim mode. ~2KB keeps a real stack trace readable
# while bounding a runaway megabyte log line.
_MAX_FIELD_CHARS = 2048

_ERROR_LEVELS = frozenset({"Error", "Exception", "Assert"})


def _cap(text: str) -> str:
    """Truncate to at most _MAX_FIELD_CHARS chars TOTAL, marker included.

    Reserves a fixed budget for the "…(+N chars)" marker so the returned string
    never exceeds the cap (the marker's own length must count against it).
    """
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    marker_budget = 24  # comfortably covers "…(+<digits> chars)"
    keep = max(_MAX_FIELD_CHARS - marker_budget, 0)
    return text[:keep] + f"…(+{len(text) - keep} chars)"


def _slim_log_delta(
    lines: list[dict[str, Any]], levels: frozenset[str] | set[str]
) -> list[dict[str, Any]]:
    """Filter by severity, run-length-collapse duplicates, cap field sizes.

    Each output entry carries line_number (of the LAST occurrence in a run),
    log_type, condition, stack_trace, and repeat (run length).
    """
    out: list[dict[str, Any]] = []
    for ln in lines:
        lv = ln.get("logType", "")
        if lv not in levels:
            continue
        msg = _cap(ln.get("message", ""))
        stack = _cap(ln.get("stackTrace", ""))
        if out and out[-1]["condition"] == msg and out[-1]["log_type"] == lv:
            out[-1]["repeat"] += 1
            out[-1]["line_number"] = ln.get("lineNumber", out[-1]["line_number"])
            continue
        out.append({
            "line_number": ln.get("lineNumber", 0),
            "log_type": lv,
            "condition": msg,
            "stack_trace": stack,
            "repeat": 1,
        })
    return out


def _slim_scene_objects(root_objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse each root object to {name, active, child_count, component_count}."""
    out: list[dict[str, Any]] = []
    for obj in root_objects:
        components = obj.get("components") or []
        children = obj.get("children")
        if children is not None:
            child_count = len(children)
        else:
            child_count = int(obj.get("childCount", 0) or 0)
        out.append({
            "name": obj.get("name", ""),
            "active": bool(obj.get("active", False)),
            "child_count": child_count,
            "component_count": len(components),
        })
    return out


def _verdict_line(
    log_delta: list[dict[str, Any]], play_state: str, root_count: int
) -> str:
    """One-line summary: distinct error/warning entries, play state, root count."""
    errors = sum(1 for e in log_delta if e.get("log_type") in _ERROR_LEVELS)
    warnings = sum(1 for e in log_delta if e.get("log_type") == "Warning")
    return f"errors:{errors} warnings:{warnings} play:{play_state} roots:{root_count}"


def _full_log_delta(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy verbose shape: every line, untruncated, no run-length collapse."""
    return [
        {
            "line_number": ln["lineNumber"],
            "log_type": ln["logType"],
            "condition": ln["message"],
            "stack_trace": ln.get("stackTrace", ""),
        }
        for ln in lines
    ]


async def observe_evidence(
    since_line_number: int | None = None,
    consume_tokens: list[str] | None = None,
    include_screenshot: bool = False,
    max_log_lines: int | None = None,
    verbose: bool = False,
    log_levels: list[str] | None = None,
    screenshot_width: int | None = None,
) -> dict[str, Any]:
    consume_tokens = consume_tokens or []

    tail_args: dict[str, Any] = {}
    if since_line_number is not None:
        tail_args["sinceLineNumber"] = since_line_number
    effective_max = max_log_lines if max_log_lines is not None else _DEFAULT_MAX_LOG_LINES
    tail_args["maxLines"] = max(1, int(effective_max))

    log_resp = await bridge_call("TailEditorLog", tail_args)
    scene_resp = await bridge_call("GetSceneState", {})

    screenshot_b64 = ""
    screenshot_skipped = True
    if include_screenshot:
        screenshot_skipped = False
        try:
            shot_args: dict[str, Any] = {}
            if screenshot_width is not None:
                shot_args["width"] = int(screenshot_width)
            screenshot_resp = await bridge_call("CaptureScreenshot", shot_args)
            screenshot_b64 = screenshot_resp.get("pngBase64", "")
        except Exception as exc:
            logger.debug("CaptureScreenshot unavailable: %s", exc)
            screenshot_b64 = ""

    play_state_resp = await bridge_call("GetPlayState", {})
    consume_result = await GUARD.consume(consume_tokens)
    # Soft-ground the lua runway: the three outer bridge reads completed, so this
    # counts as the agent looking at reality (independent of any optional screenshot
    # sub-result). Resets since_ground, capped by soft_limit.
    ground_outcome = await LUA_BUDGET.ground(hard=False)

    lines = log_resp.get("lines", [])
    current_line_number = lines[-1]["lineNumber"] if lines else 0
    root_objects = scene_resp.get("rootObjects", [])

    result: dict[str, Any] = {
        "play_state": play_state_resp["state"],
        "current_line_number": current_line_number,
        "screenshot_png_base64": screenshot_b64,
        "screenshot_skipped": screenshot_skipped,
        "consumed_tokens": consume_result.consumed,
        "expired_tokens": consume_result.expired,
        "unknown_tokens": consume_result.unknown,
        "verbose": verbose,
        "lua_runway": runway_response(ground_outcome, LUA_BUDGET),
    }

    if verbose:
        result["log_delta"] = _full_log_delta(lines)
        result["scene_objects"] = root_objects
        return result

    # Slim (default). Explicit log_levels widen/narrow the severity filter.
    levels: frozenset[str] | set[str]
    if log_levels is not None:
        levels = {lv for lv in log_levels}
    else:
        levels = _DEFAULT_SLIM_LEVELS
    slim_delta = _slim_log_delta(lines, levels)
    result["log_delta"] = slim_delta
    result["scene_objects"] = _slim_scene_objects(root_objects)
    result["verdict"] = _verdict_line(
        slim_delta, play_state_resp["state"], len(root_objects)
    )
    return result


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        since_line_number: int | None = None,
        consume_tokens: list[str] | None = None,
        include_screenshot: bool = False,
        max_log_lines: int | None = None,
        verbose: bool = False,
        log_levels: list[str] | None = None,
        screenshot_width: int | None = None,
    ) -> dict[str, Any]:
        return await observe_evidence(
            since_line_number,
            consume_tokens,
            include_screenshot,
            max_log_lines,
            verbose,
            log_levels,
            screenshot_width,
        )
