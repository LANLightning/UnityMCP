"""query_scene tool. Read-only. No token."""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "query_scene"


async def query_scene(scene: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"scene": scene} if scene else {}
    scene_resp = await bridge_call("GetSceneState", args)
    ps_resp = await bridge_call("GetPlayState", {})
    tail_resp = await bridge_call("TailEditorLog", {"maxLines": 1})

    lines = tail_resp.get("lines", [])
    return {
        "rootObjects": scene_resp.get("rootObjects", []),
        "play_state": ps_resp["state"],
        "line_number": lines[-1]["lineNumber"] if lines else 0,
    }


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(scene: str | None = None) -> dict[str, Any]:
        return await query_scene(scene)
