"""lua_eval tool. Write op. Runs an arbitrary Lua chunk in the live xLua VM.

This is the universal key for the no-human UI verification layer: open panels
(`PROJECT_RUNTIME.global.ui:open(uid)`), verify (`return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE(uid)`),
simulate clicks (`node:PROJECT_CLICK_WITH_HIT_TEST(false)`), trigger input actions
(`PROJECT_RUNTIME.global.PROJECT_INPUT_SERVICE:PROJECT_TRIGGER_ACTION(path, "Performed")`), set input text.

It wraps the bridge LuaEval command, which reuses the same
PROJECT_FACADE.PROJECT_LUA_MANAGER.PROJECT_LUA_VM DoString seam LuaReloadModule already proves. The Lua
VM only exists in Play Mode; the bridge returns INVALID_STATE 'lua_vm_not_running'
in EditMode.

GROUNDING RUNWAY (spec 2026-06-09): every call must declare a `goal` (its concrete
objective). The runway counts lua executions since the agent last checked reality;
when it fills, the call raises a RECOVERABLE LuaGroundingRequiredError — ground via
capture_screenshot / get_console_logs / observe_evidence (soft, capped) or
verify_assertions (hard, full re-arm), then continue. Only a high lifetime backstop
(LuaRunawayError) is terminal. Charged on ATTEMPT (before the bridge), so a refused
call has zero side effects and a bridge error still advances the runway.

SECURITY: this is arbitrary code execution, correctly gated as a WRITE op (the
token must be consumed by observe_evidence). There is no point allowlisting the
script body (any Lua is allowed by design), so we only cap its size and require
non-empty. Callers that interpolate untrusted strings into the script are
responsible for escaping — prefer the typed simulate_* / open_panel tools which
charset-validate their interpolated fragments.
"""
from __future__ import annotations

import re
from typing import Any

from ..state import LUA_BUDGET, runway_response
from ._write_op_helper import write_op

TOOL_NAME = "lua_eval"

# Upper bound on the script chunk size. Generous (covers multi-statement verify
# scripts) but bounds accidental megabyte payloads across the TCP bridge.
_MAX_SCRIPT_LENGTH = 16384

# Upper bound on the goal label. A goal is a short declared objective, not a
# payload; we cap it to nudge concise objectives and bound log/echo size.
_MAX_GOAL_LENGTH = 200

# --- Degenerate-infinite-loop tripwire (NOT a security boundary) -------------
# lua_eval runs arbitrary Lua synchronously on Unity's single Editor main thread.
# A no-exit loop (`while true do end`) freezes the Editor FOREVER — and because
# the thread is wedged INSIDE the call, nothing on the C# side can release it
# (the project's xLua exposes no lua_sethook to preempt a running chunk). The
# Python client's 30s timeout unblocks THIS call, but Unity stays frozen.
#
# So we reject the canonical no-escape forms here, before any bridge traffic.
# This is a best-effort TRIPWIRE for the fat-fingered hang, not exhaustive: the
# legitimate idiom `while true do ... break end` is deliberately NOT matched
# (it has an exit), and anything cleverer slips through to the editor watchdog
# (Mechanism B), which detects the wedge and surfaces it instead of a silent
# 30s stall. Callers who really mean an unbounded chunk pass allow_unbounded=True.
#
# Keywords are matched CASE-SENSITIVELY: Lua's `true`/`false`/`nil` are lowercase
# keywords; `while True do end` loops zero times (undefined `True` is nil/falsy),
# so flagging it would be a false positive.
_DEGENERATE_LOOP_PATTERNS = [
    (re.compile(r"\bwhile\s+true\s+do\s+end\b"), "while true do end"),
    (re.compile(r"\bwhile\s+1\s+do\s+end\b"), "while 1 do end"),
    (re.compile(r"\brepeat\s+until\s+false\b"), "repeat until false"),
    (re.compile(r"\brepeat\s+until\s+nil\b"), "repeat until nil"),
]

# Lua block comment --[[ ... ]] (incl. leveled --[==[ ... ]==]) then line comment.
_LUA_BLOCK_COMMENT_RE = re.compile(r"--\[(=*)\[.*?\]\1\]", re.DOTALL)
_LUA_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _strip_lua_comments(script: str) -> str:
    """Best-effort strip of Lua comments so the tripwire neither (a) fires on a
    pattern that only appears inside a comment nor (b) is fooled by `end` hidden
    in a comment. Block comments first (they may span the line-comment marker),
    then line comments."""
    without_block = _LUA_BLOCK_COMMENT_RE.sub(" ", script)
    return _LUA_LINE_COMMENT_RE.sub(" ", without_block)


class InvalidLuaScriptError(ValueError):
    """Raised when the script/goal fails basic validation before any bridge traffic."""


async def lua_eval(
    script: str,
    goal: str,
    chunk_name: str | None = None,
    allow_unbounded: bool = False,
) -> dict[str, Any]:
    """Run an arbitrary Lua chunk in the running game's xLua VM.

    Args:
        script: The Lua chunk to execute. To read a value back, ``return`` a
            primitive (bool/number/string) — Lua tables/userdata do NOT marshal
            across the bridge and surface as their ``tostring``.
        goal: The concrete objective this execution serves (e.g.
            ``'打开背包面板并确认可见'``). Required (declared purpose); it no longer
            keys a budget. The runway bounds how long you may run lua WITHOUT
            grounding in real evidence — when it fills you must ground (screenshot /
            logs / observe_evidence, or verify_assertions) and continue.
        chunk_name: Optional chunk name for Lua error tracebacks.
        allow_unbounded: Escape hatch for the degenerate-infinite-loop tripwire.
            Default False rejects no-exit loops (``while true do end``) before they
            reach Unity, because such a chunk freezes the Editor main thread forever
            (it runs synchronously and cannot be preempted). Pass True only if you
            genuinely intend an unbounded chunk and accept that it will wedge the
            Editor (the watchdog will surface it; recovery is a manual restart).

    Returns:
        Bridge result enriched with ``write_op_token`` / ``line_number_before`` and a
        ``lua_runway`` block (``since_ground`` / ``soft_used`` / ``soft_limit`` /
        ``threshold`` / ``grounded``) so you can watch the runway fill call-by-call.

    Raises:
        InvalidLuaScriptError: empty/oversized script, empty/oversized goal, or a
            degenerate no-exit loop (unless allow_unbounded=True). Raised before any
            bridge traffic and before any runway charge.
        LuaGroundingRequiredError: RECOVERABLE — too many lua runs since the last
            reality-check. Ground yourself, then retry. Raised before any bridge
            traffic; no token issued.
        LuaRunawayError: TERMINAL backstop — lifetime lua execution ceiling reached
            (almost certainly a loop). Stop and report to the user.
    """
    if not isinstance(script, str) or len(script.strip()) == 0:
        raise InvalidLuaScriptError("script must be a non-empty string")
    if len(script) > _MAX_SCRIPT_LENGTH:
        raise InvalidLuaScriptError(
            f"script exceeds {_MAX_SCRIPT_LENGTH} chars ({len(script)})"
        )
    if not isinstance(goal, str) or len(goal.strip()) == 0:
        raise InvalidLuaScriptError("goal must be a non-empty string")
    if len(goal) > _MAX_GOAL_LENGTH:
        raise InvalidLuaScriptError(
            f"goal exceeds {_MAX_GOAL_LENGTH} chars ({len(goal)})"
        )

    # Degenerate-infinite-loop tripwire. A no-exit loop wedges Unity's main thread
    # forever and cannot be released once started, so reject it BEFORE any bridge
    # traffic. Best-effort only (see _DEGENERATE_LOOP_PATTERNS); the editor
    # watchdog is the real net. Bypass with allow_unbounded=True.
    if not allow_unbounded:
        stripped = _strip_lua_comments(script)
        for pattern, label in _DEGENERATE_LOOP_PATTERNS:
            if pattern.search(stripped):
                raise InvalidLuaScriptError(
                    f"script contains a degenerate infinite loop ({label!r}) that "
                    "would freeze the Unity Editor main thread permanently. If this "
                    "is intentional, pass allow_unbounded=True."
                )

    # Charge the grounding runway BEFORE any bridge traffic. May raise a
    # LuaBudgetError (LuaGroundingRequiredError — recoverable; or LuaRunawayError —
    # terminal backstop), in which case no game state is touched and no write-op
    # token is issued. Charged on attempt (not success): the count stands even if
    # the bridge then errors. `goal` is still required (declared purpose) but no
    # longer keys a counter.
    await LUA_BUDGET.charge_run()

    args: dict[str, Any] = {"script": script}
    if chunk_name is not None:
        args["chunkName"] = chunk_name
    result = await write_op(TOOL_NAME, args, bridge_method="LuaEval")
    # Surface the runway state so the agent can watch it fill call-by-call. lua_eval
    # CONSUMES runway and never grounds, so `grounded` is always False here — we use
    # snapshot_outcome() (a non-mutating view) through the shared runway_response
    # helper so the block shape is identical across every runway-aware tool.
    outcome = await LUA_BUDGET.snapshot_outcome()
    result["lua_runway"] = runway_response(outcome, LUA_BUDGET)
    return result


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        script: str,
        goal: str,
        chunk_name: str | None = None,
        allow_unbounded: bool = False,
    ) -> dict[str, Any]:
        return await lua_eval(script, goal, chunk_name, allow_unbounded)
