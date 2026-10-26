"""list_sessions tool. Enumerates recorded sessions newest-first."""
from __future__ import annotations

from typing import Any

from ..recorder import list_session_files

TOOL_NAME = "list_sessions"


async def list_sessions(limit: int = 20) -> dict[str, Any]:
    return {"sessions": list_session_files(limit)}


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(limit: int = 20) -> dict[str, Any]:
        return await list_sessions(limit)
