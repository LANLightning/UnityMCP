"""operate_camera tool. Write op. Operates the camera AND auto-captures evidence.

Performs a whitelisted camera operation (zoom / setTimeScale / setWorldCameraEnable /
enablePhoto / setTargetPlayer / focus) by calling the game's own PROJECT_RUNTIME.game.camera
method, and returns a before/after camera snapshot. ``applied`` is True ONLY when the
op ran without a Lua error AND produced an observable before≠after state delta — a
no-op or errored call returns applied=False with a ``note`` (no hallucinated success).

Scene/mode sensitivity (verified on real machine): some actions only take effect in
the right camera mode/scene. ``zoom`` requires the player camera to be active (it
routes through PROJECT_CAMERA_STACK:HandleCommand(Zoom)); in non-player scenes (e.g. Homeland)
it errors/no-ops and you'll get applied=False + a note. ``setTimeScale`` changes
PROJECT_CAMERA_SERVICE.timeScale and is observable in ANY scene — it's the most reliable action
to confirm the operate+evidence loop works. Read the ``note`` when applied=False.

Only safe, well-understood operations are exposed (NOT arbitrary methods). For
anything else, use lua_eval. PlayMode-only.
"""
from __future__ import annotations

from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "operate_camera"

# action -> which extra arg it needs (for early validation + good error messages).
_ACTIONS_VALUE = frozenset({"zoom", "setTimeScale"})
_ACTIONS_FLAG = frozenset({"setWorldCameraEnable", "enablePhoto"})
_ACTIONS_ACTOR = frozenset({"setTargetPlayer", "focus"})
_ALL_ACTIONS = _ACTIONS_VALUE | _ACTIONS_FLAG | _ACTIONS_ACTOR


class InvalidOperateCameraArg(ValueError):
    """Raised when action/args are invalid before any bridge call."""


async def operate_camera(
    action: str,
    value: float | None = None,
    actor_id: int | None = None,
    flag: bool | None = None,
    settle_ms: int | None = None,
) -> dict[str, Any]:
    """Operate the camera and return before/after evidence.

    Args:
        action: One of zoom | setTimeScale | setWorldCameraEnable | enablePhoto |
            setTargetPlayer | focus.
        value: Numeric arg for zoom / setTimeScale.
        actor_id: Entity actorId for setTargetPlayer / focus.
        flag: Boolean for setWorldCameraEnable / enablePhoto.
        settle_ms: Reserved; bridge currently snapshots immediately after.

    Returns:
        Bridge result + write-op token: {action, applied, before:{...}, after:{...}}.

    Raises:
        InvalidOperateCameraArg: unknown action / missing required arg.
    """
    if action not in _ALL_ACTIONS:
        raise InvalidOperateCameraArg(
            f"action must be one of {sorted(_ALL_ACTIONS)}, got {action!r}"
        )
    if action in _ACTIONS_VALUE and value is None:
        raise InvalidOperateCameraArg(f"action {action!r} requires value")
    if action in _ACTIONS_ACTOR and actor_id is None:
        raise InvalidOperateCameraArg(f"action {action!r} requires actor_id")

    args: dict[str, Any] = {"action": action}
    if value is not None:
        args["value"] = float(value)
    if actor_id is not None:
        args["actorId"] = int(actor_id)
    if flag is not None:
        args["flag"] = bool(flag)
    if settle_ms is not None:
        args["settleMs"] = max(0, int(settle_ms))

    return await write_op(TOOL_NAME, args, bridge_method="OperateCamera")


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        action: str,
        value: float | None = None,
        actor_id: int | None = None,
        flag: bool | None = None,
        settle_ms: int | None = None,
    ) -> dict[str, Any]:
        return await operate_camera(action, value, actor_id, flag, settle_ms)
