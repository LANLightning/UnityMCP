"""record_stop tool. Closes the active recording session."""
from __future__ import annotations

from typing import Any

from ..recorder import RECORDER

TOOL_NAME = "record_stop"


async def record_stop() -> dict[str, Any]:
    return await RECORDER.stop()


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool() -> dict[str, Any]:
        return await record_stop()
