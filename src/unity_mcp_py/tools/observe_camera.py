"""observe_camera tool. Read-only. No token.

Returns evidence-grade camera runtime state (position/rotation/fov/active mode/
zoom/follow target/shake/timeScale/...) so the AI can verify "the camera did X"
from real queried state — not by reading code or guessing from a screenshot.

PlayMode-only (reads the live Lua/C# camera managers). Optionally pass a world
target_pos [x,y,z] to also get inViewport / inViewportCenter (proof the camera
frames that point).
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "observe_camera"


class InvalidObserveCameraArg(ValueError):
    """Raised when target_pos is malformed before any bridge call."""


async def observe_camera(target_pos: list[float] | None = None) -> dict[str, Any]:
    """Snapshot the camera's runtime state.

    Args:
        target_pos: Optional world point [x, y, z]; when given, the result's data
            also includes inViewport / inViewportCenter.

    Returns:
        {"ok": bool, "data": {position, rotationEuler, fov, farPlane, activeModeName,
        isBlending, PROJECT_CURRENT_ZOOM, zoomTarget, followActorId, activeShakeCount,
        worldCameraEnabled, timeScale, focusGroup, [inViewport, inViewportCenter]}}.
    """
    args: dict[str, Any] = {}
    if target_pos is not None:
        if len(target_pos) != 3 or not all(isinstance(v, (int, float)) for v in target_pos):
            raise InvalidObserveCameraArg("target_pos must be [x, y, z] numbers")
        args["targetPos"] = [float(v) for v in target_pos]
    return await bridge_call("ObserveCamera", args)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(target_pos: list[float] | None = None) -> dict[str, Any]:
        return await observe_camera(target_pos)
