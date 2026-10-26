"""replay tool. Re-invokes the tool sequence from a recorded session."""
from __future__ import annotations

from typing import Any

from ..recorder import run_replay

TOOL_NAME = "replay"


async def replay(
    session_id: str,
    dry_run: bool = False,
    skip_tools: list[str] | None = None,
    stop_on_error: bool = True,
) -> dict[str, Any]:
    return await run_replay(session_id, dry_run, skip_tools, stop_on_error)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        session_id: str,
        dry_run: bool = False,
        skip_tools: list[str] | None = None,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        return await replay(session_id, dry_run, skip_tools, stop_on_error)
