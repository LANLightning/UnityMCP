"""clear_console tool. Advances the virtual cursor; does not touch Unity console."""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call
from ..recorder import RECORDER

TOOL_NAME = "clear_console"


async def clear_console() -> dict[str, Any]:
    previous = RECORDER.get_cursor()
    resp = await bridge_call("TailEditorLog", {"maxLines": 1})
    lines = resp.get("lines", []) or []
    latest = lines[-1].get("lineNumber", previous) if lines else previous
    RECORDER.set_cursor(latest)
    return {"previous_cursor": previous, "new_cursor": RECORDER.get_cursor()}


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool() -> dict[str, Any]:
        return await clear_console()
