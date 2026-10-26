"""observe_effect tool. Read-only. No token.

Per-effect runtime state by generatorId + effectId: {exists, key, status, playing,
playedTime, active, particleCount}. particleCount is summed over the effect's child
ParticleSystems (ground truth that VFX actually emitted).

PlayMode-only. Effects are transient — a single observe after the window proves
nothing; use observe_window to sample during playback.
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "observe_effect"


async def observe_effect(generator_id: int, effect_id: int) -> dict[str, Any]:
    """Observe one effect instance.

    Args:
        generator_id: Owner generator id (0 = global generator for most skill/world FX).
        effect_id: The effectId returned by the game's PlayEffect.

    Returns:
        {"ok": bool, "data": {exists, key, status, playing, playedTime, active,
        particleCount}}.
    """
    return await bridge_call(
        "ObserveEffect", {"generatorId": int(generator_id), "effectId": int(effect_id)}
    )


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(generator_id: int, effect_id: int) -> dict[str, Any]:
        return await observe_effect(generator_id, effect_id)
