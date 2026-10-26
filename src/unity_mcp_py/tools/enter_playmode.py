"""enter_playmode tool. Write op. Wraps bridge EnterPlaymode."""
from __future__ import annotations

from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "enter_playmode"


async def enter_playmode() -> dict[str, Any]:
    """Enter Unity PlayMode and issue a write-op token for observation."""
    return await write_op(TOOL_NAME, {}, bridge_method="EnterPlaymode")


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool() -> dict[str, Any]:
        return await enter_playmode()
