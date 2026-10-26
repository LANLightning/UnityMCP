"""escalate_via_click tool. Write op. Command-line fallback to real input.

When lua_eval command-line operations (e.g. PROJECT_RUNTIME.global.ui:open(uid)) don't carry
real data — surfaced by repeated verify_assertions failures under the same goal
(verdict.escalation.should_escalate) — the agent should NOT keep forcing the lua
path. This tool atomically:

  1. Restores the lua op by running the caller-supplied ``undo_lua`` (the inverse
     operation, e.g. PROJECT_RUNTIME.global.ui:close(uid)).
  2. Re-enters through REAL simulated input by clicking the true entry-point
     button (delegates to simulate_click).
  3. On a landed click, clears the escalation gate for ``goal`` and soft-grounds
     the lua runway (entering the real environment is a reality-touching act).

If the undo fails, the click is NOT attempted (never stack a click on an
un-restored dirty state) and the gate is left intact. If the click misses, the
gate is left intact so the agent can retry with a different locator / mode.

PlayMode-only (lua VM + simulate both require Play Mode).
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call
from ..state import LUA_BUDGET, ESCALATION_GATE
from .simulate_click import simulate_click
from .verify_assertions import verify_assertions

TOOL_NAME = "escalate_via_click"

# Reuse lua_eval's script cap and verify/lua goal cap for consistency.
_MAX_UNDO_LUA_LENGTH = 16384
_MAX_GOAL_LENGTH = 200
_VALID_MODES = frozenset({"raycast", "force"})


class InvalidEscalateArg(ValueError):
    """Raised when args fail validation before any bridge traffic."""


async def escalate_via_click(
    undo_lua: str,
    panel_uid: int,
    goal: str,
    ref_key: str | None = None,
    path: str | None = None,
    mode: str = "raycast",
    assertions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Restore a lua op, re-enter via a real click, then re-verify. See module docstring.

    Args:
        undo_lua: Inverse lua to restore the prior command-line op (e.g.
            ``PROJECT_RUNTIME.global.ui:close(123)``). Required, non-empty, <=16384 chars.
        panel_uid: Numeric panel uid the entry-point node lives in.
        goal: The same goal key used in verify_assertions, so a landed click can
            clear that goal's escalation counter. Required, non-empty, <=200.
        ref_key: PROJECT_NODE_REFERENCE key of the real entry button. XOR path.
        path: Transform path of the real entry button. XOR ref_key.
        mode: "raycast" (default, self-validating) or "force".
        assertions: Optional verify_assertions list. When provided AND the real
            click lands, the tool re-verifies the real environment itself and
            decides ``needs_user`` — so the agent does NOT insert a user query
            before the click. Reverify PASS -> needs_user False (agent proceeds
            silently). Reverify FAIL -> needs_user True (the ONLY point the agent
            should ask the user). Omitted -> legacy behavior, no reverify.

    Returns:
        The simulate_click result enriched with:
          - ``restored: True``
          - ``escalation_cleared: bool`` — True iff the click landed and, when
            assertions were given, the reverify PASSED (so the gate was cleared).
          - ``reverify_passed: bool | None`` — None if no assertions were given or
            the click missed (reverify is skipped); else the reverify verdict.
          - ``needs_user: bool`` — True ONLY when the click landed, assertions were
            given, and the reverify still FAILED. This is the agent's single signal
            to ask the user; in every other case the agent proceeds without asking.
          - ``reverify: dict`` — the full verify_assertions verdict (present only
            when a reverify ran).
        Carries simulate_click's ``write_op_token``.

    Raises:
        InvalidEscalateArg: bad undo_lua/goal/mode/locator (raised before any
            bridge traffic — the undo never fires for an invalid call).
        bridge_client.BridgeError: the undo LuaEval failed (re-raised; no click).
        InvalidSimulateClickArg: the locator passed escalate's XOR check but
            failed simulate_click's charset validation. Raised AFTER the undo —
            so the lua state is restored, no click happened, and the gate is left
            intact (retry with a corrected locator).
    """
    if not isinstance(undo_lua, str) or len(undo_lua.strip()) == 0:
        raise InvalidEscalateArg("undo_lua must be a non-empty string")
    if len(undo_lua) > _MAX_UNDO_LUA_LENGTH:
        raise InvalidEscalateArg(
            f"undo_lua exceeds {_MAX_UNDO_LUA_LENGTH} chars ({len(undo_lua)})"
        )
    if not isinstance(goal, str) or len(goal.strip()) == 0:
        raise InvalidEscalateArg("goal must be a non-empty string")
    if len(goal) > _MAX_GOAL_LENGTH:
        raise InvalidEscalateArg(f"goal exceeds {_MAX_GOAL_LENGTH} chars ({len(goal)})")
    if mode not in _VALID_MODES:
        raise InvalidEscalateArg(f"mode must be one of {sorted(_VALID_MODES)}")
    if assertions is not None and not isinstance(assertions, list):
        raise InvalidEscalateArg("assertions must be a list when provided")
    # panel_uid and the locator (ref_key XOR path) are re-validated by the delegated
    # simulate_click, but check the static cases here too so we never run the undo
    # for a call that can't possibly click (a wasted, side-effecting round-trip).
    if not isinstance(panel_uid, int) or isinstance(panel_uid, bool) or panel_uid == 0:
        raise InvalidEscalateArg(f"panel_uid must be a non-zero int, got {panel_uid!r}")
    if (ref_key is None) == (path is None):
        raise InvalidEscalateArg("provide exactly one of ref_key | path")

    # 1. Restore. If this raises BridgeError, propagate WITHOUT clicking or clearing.
    await bridge_call("LuaEval", {"script": undo_lua})

    # 2. Real click (issues its own write_op_token; validates the locator charset).
    click = await simulate_click(panel_uid, ref_key=ref_key, path=path, mode=mode)

    landed = bool(click.get("clicked"))
    reverify_passed: bool | None = None
    reverify_verdict: dict[str, Any] | None = None
    gate_cleared = False

    # A click that MISSED is a mechanical locator problem: the agent retries with a
    # different locator/mode. Do not reverify, do not clear, do not flag the user.
    if not landed:
        return {
            **click,
            "restored": True,
            "escalation_cleared": False,
            "reverify_passed": None,
            "needs_user": False,
        }

    # 3. The click LANDED — we are now facing the real environment.
    if assertions is None:
        # Legacy path: no reverify requested. Clear the gate + soft-ground exactly
        # as before. clear() returns whether it actually reset this goal's counter
        # (no-op if an interleaved verify rebased the gate to another goal), so
        # surface the REAL result rather than assuming the click cleared it.
        gate_cleared = await ESCALATION_GATE.clear(goal)
        await LUA_BUDGET.ground(hard=False)
        return {
            **click,
            "restored": True,
            "escalation_cleared": gate_cleared,
            "reverify_passed": None,
            "needs_user": False,
        }

    # 4. Reverify the real environment ourselves. verify_assertions records its own
    # verdict against the SAME goal in the escalation gate: a PASS resets the
    # counter to 0 (== clearing the gate), a FAIL re-records a same-goal failure.
    # So the gate ends up consistent with reality without a separate manual clear.
    reverify_verdict = await verify_assertions(assertions, goal=goal)
    reverify_passed = bool(reverify_verdict.get("passed"))
    # verify_assertions hard-grounds the runway already; reflect the gate state.
    snap = await ESCALATION_GATE.snapshot()
    gate_cleared = reverify_passed and snap.get("fail_count") == 0
    # needs_user is the agent's SOLE cue to ask the user: only when the real click
    # landed yet the real environment still does not match the goal.
    needs_user = not reverify_passed

    return {
        **click,
        "restored": True,
        "escalation_cleared": gate_cleared,
        "reverify_passed": reverify_passed,
        "needs_user": needs_user,
        "reverify": reverify_verdict,
    }


_DESCRIPTION = (__doc__ or "") + """
USE WHEN: verify_assertions returned escalation.should_escalate=true — your lua
command-line operations are not reaching the real environment. Pass undo_lua to
restore your prior lua op and the real entry button's panel_uid + ref_key/path;
this tool restores then clicks for real, so the next screenshot/assertion faces
the true environment.

DO NOT ask the user before the real click. The fallback (undo -> real click ->
reverify) is deterministic and runs entirely inside this tool. Pass ``assertions``
(same shape as verify_assertions) describing the real-environment success state;
after a landed click the tool re-verifies and returns ``needs_user``:
  - needs_user=false  -> the real path worked (or click missed -> retry locator);
                         proceed WITHOUT asking the user.
  - needs_user=true   -> the click landed but the real environment STILL doesn't
                         match the goal — THIS is the only time you ask the user.
Omitting ``assertions`` keeps the legacy behavior (no reverify; you decide).
"""


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME, description=_DESCRIPTION)
    async def _tool(
        undo_lua: str,
        panel_uid: int,
        goal: str,
        ref_key: str | None = None,
        path: str | None = None,
        mode: str = "raycast",
        assertions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await escalate_via_click(
            undo_lua,
            panel_uid,
            goal,
            ref_key=ref_key,
            path=path,
            mode=mode,
            assertions=assertions,
        )
