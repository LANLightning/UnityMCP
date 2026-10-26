"""run_method tool. Write op. Wraps bridge RunEditMethod."""
from __future__ import annotations

from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "run_method"


async def run_method(
    type_name: str,
    method_name: str,
    args: list[Any] | None = None,
) -> dict[str, Any]:
    """Invoke a static C# method in the Unity Editor and issue a write-op token."""
    method_args = args or []
    return await write_op(
        TOOL_NAME,
        {"typeName": type_name, "methodName": method_name, "args": method_args},
        bridge_method="RunEditMethod",
    )


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        type_name: str,
        method_name: str,
        args: list[Any] | None = None,
    ) -> dict[str, Any]:
        return await run_method(type_name, method_name, args)
