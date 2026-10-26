"""observe_entity tool. Read-only. No token.

Full evidence snapshot of one logical entity (resolve by id | actorId | templateId):
position/yaw, hp/maxHp/hpPercent, life (alive/dead/fakeDead), combat (inCombat/lock/
attack target), characterState (+name), animKey, configName, model-loaded, and
type predicates (isPlayer/isPet/isPuppet/isBoss). Proves spawned/moved/damaged/died/
animating from real runtime state.

PlayMode-only.
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "observe_entity"


class InvalidObserveEntityArg(ValueError):
    """Raised when no/invalid handle is given before any bridge call."""


async def observe_entity(
    entity_id: str | None = None,
    actor_id: int | None = None,
    template_id: int | None = None,
) -> dict[str, Any]:
    """Observe one entity. Provide exactly one of entity_id / actor_id / template_id.

    Returns:
        {"ok": bool, "data": {found: bool, ... full evidence ...}}.
    """
    provided = [entity_id is not None, actor_id is not None, template_id is not None]
    if sum(provided) != 1:
        raise InvalidObserveEntityArg(
            "provide exactly one of entity_id | actor_id | template_id"
        )
    args: dict[str, Any] = {}
    if entity_id is not None:
        if not isinstance(entity_id, str) or len(entity_id.strip()) == 0:
            raise InvalidObserveEntityArg("entity_id must be a non-empty string")
        args["id"] = entity_id
    elif actor_id is not None:
        args["actorId"] = int(actor_id)
    else:
        args["templateId"] = int(template_id)  # type: ignore[arg-type]
    return await bridge_call("ObserveEntity", args)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        entity_id: str | None = None,
        actor_id: int | None = None,
        template_id: int | None = None,
    ) -> dict[str, Any]:
        return await observe_entity(entity_id, actor_id, template_id)
