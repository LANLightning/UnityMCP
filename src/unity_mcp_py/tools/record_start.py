"""record_start tool. Begins recording MCP tool calls to a JSONL session."""
from __future__ import annotations

from typing import Any

from ..recorder import RECORDER

TOOL_NAME = "record_start"


async def record_start(
    session_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    return await RECORDER.start(session_id, note)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        session_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return await record_start(session_id, note)
