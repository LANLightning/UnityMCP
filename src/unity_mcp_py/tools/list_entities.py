"""list_entities tool. Read-only. No token.

Enumerates live logical game entities (NPC/monster/player/pet/...) from the Lua
EntityManager registry, with optional filters. Each row carries id/className/
actorType/templateId/actorId/camp/life/position/yaw/playerDistance/active/visible.
With summary=True, returns per-type counts (cheap spawn/despawn proof).

PlayMode-only. These are Lua objects, invisible to GameObject scene walks.
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "list_entities"
_MAX_ENTITIES_CAP = 2000


async def list_entities(
    actor_type: str | None = None,
    class_name: str | None = None,
    template_id: int | None = None,
    summary: bool = False,
    max_entities: int | None = None,
) -> dict[str, Any]:
    """List live entities.

    Args:
        actor_type: Filter by actorType string (e.g. "Player", "Puppet", "Pet").
        class_name: Filter by Lua className (e.g. "ClientPuppet").
        template_id: Filter by config/prototype templateId.
        summary: If True, return per-type counts instead of the full list.
        max_entities: Cap on rows (default 500, ceiling 2000).

    Returns:
        {"ok": bool, "data": {count, entities:[...]}} or, with summary,
        {"ok": bool, "data": {summary: {Player: n, Puppet: n, ...}}}.
    """
    args: dict[str, Any] = {}
    if actor_type is not None:
        args["actorType"] = str(actor_type)
    if class_name is not None:
        args["className"] = str(class_name)
    if template_id is not None:
        args["templateId"] = int(template_id)
    if summary:
        args["summary"] = True
    if max_entities is not None:
        args["maxEntities"] = max(1, min(int(max_entities), _MAX_ENTITIES_CAP))
    return await bridge_call("ListEntities", args)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        actor_type: str | None = None,
        class_name: str | None = None,
        template_id: int | None = None,
        summary: bool = False,
        max_entities: int | None = None,
    ) -> dict[str, Any]:
        return await list_entities(actor_type, class_name, template_id, summary, max_entities)
