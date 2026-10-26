"""assert_no_compile_errors tool. Gate, NOT runtime evidence."""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "assert_no_compile_errors"


async def assert_no_compile_errors() -> dict[str, Any]:
    resp = await bridge_call("GetCompileStatus", {})
    return {
        "has_errors": resp.get("hasErrors", False),
        "errors": resp.get("errors", []),
        "is_runtime_evidence": False,    # HARDCODED — spec §3.2 point 4
    }


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool() -> dict[str, Any]:
        return await assert_no_compile_errors()
