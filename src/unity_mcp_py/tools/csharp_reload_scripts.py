"""csharp_reload_scripts tool. Write op. Wraps bridge CsharpReloadScripts.

Triggers ``UnityEditor.Compilation.CompilationPipeline.RequestScriptCompilation``.
In Play Mode the bridge refuses unless ``force=true``, in which case it
performs exit Playmode → compile → re-enter Playmode.
"""
from __future__ import annotations

from typing import Any

from ._write_op_helper import write_op
from ..bridge.session import get_session_async

TOOL_NAME = "csharp_reload_scripts"


async def csharp_reload_scripts(force: bool = False) -> dict[str, Any]:
    """Request a C# script recompile.

    Args:
        force: If True and the Editor is in Play Mode, exit → compile → re-enter.
            Default False fails loudly from Play Mode with ``INVALID_STATE``.

    Returns:
        Bridge result enriched with ``write_op_token`` / ``line_number_before``.
        Fields: ``reload_id`` (UUID) and ``force_was_used``.
    """
    result = await write_op(
        TOOL_NAME,
        {"force": bool(force)},
        bridge_method="CsharpReloadScripts",
    )
    # Reload succeeded → code was (re)applied → gate the next screenshot until a
    # human confirms the visual result. write_op raises on bridge failure, so we
    # only reach here on success.
    (await get_session_async()).code_dirty = True
    return result


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(force: bool = False) -> dict[str, Any]:
        return await csharp_reload_scripts(force)
