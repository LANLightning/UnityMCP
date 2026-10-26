"""exit_playmode tool. Write op. Wraps bridge ExitPlaymode."""
from __future__ import annotations

from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "exit_playmode"


async def exit_playmode() -> dict[str, Any]:
    """Exit Unity PlayMode and issue a write-op token for observation."""
    return await write_op(TOOL_NAME, {}, bridge_method="ExitPlaymode")


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool() -> dict[str, Any]:
        return await exit_playmode()
