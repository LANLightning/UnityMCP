"""list_effects tool. Read-only. No token.

Enumerates all currently-live VFX/effect instances across every effect generator,
returning {generatorId, effectId, key, status, playedTime, particleCount} per
instance. This is the only way to discover effectIds the AI did not itself trigger
(the game exposes no Lua enumerator, so the bridge reflects EffectManager's private
generators dictionary).

PlayMode-only. Effects are transient — pair with observe_window for "did it play".
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "list_effects"


async def list_effects() -> dict[str, Any]:
    """List active effect instances.

    Returns:
        {"ok": bool, "data": {count, effects:[{generatorId, effectId, key, status,
        playedTime, particleCount}, ...]}}.
    """
    return await bridge_call("ListEffects", {})


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool() -> dict[str, Any]:
        return await list_effects()
