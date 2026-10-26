"""list_ui_nodes tool. Read-only. No token.

Returns an inventory of the live PROJECT_UI/PROJECT UI nodes so the AI can ground a
screenshot + text description to a concrete node (name / path / type / text /
textId / screen rect / visible / clickable). Backs the no-human verification
loop's "find the node" step.

PlayMode-only: the bridge reflects into the running game; in EditMode it returns
INVALID_STATE. By default only interactable nodes (clickable or carrying text)
are returned; pass include_all=True for every PROJECT_WIDGET.
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "list_ui_nodes"

_VALID_LAYERS = frozenset({"PROJECT_PANEL_LAYER", "PROJECT_POPUP_LAYER", "PROJECT_INFO_LAYER", "PROJECT_SCENE_LAYER"})
_MAX_NODES_CAP = 2000


class InvalidLayerError(ValueError):
    """Raised when a layer name is outside the allowlist before any bridge call."""


async def list_ui_nodes(
    panel_uid: int | None = None,
    layers: list[str] | None = None,
    include_all: bool = False,
    max_nodes: int | None = None,
    path_prefix: str | None = None,
) -> dict[str, Any]:
    """Enumerate UI nodes under the open panel layers.

    Scope note: this covers the PROJECT_UI panel UI tree only (the four PROJECT_UI_MANAGER layer
    roots). World-scene GameObjects, entities, and cameras are NOT UGUI nodes and
    are out of scope for this tool.

    Args:
        panel_uid: Restrict to one open panel by its numeric uid (UIConst.UI_ID_*).
        layers: Subset of {PROJECT_PANEL_LAYER, PROJECT_POPUP_LAYER, PROJECT_INFO_LAYER, PROJECT_SCENE_LAYER}. Default
            = all four.
        include_all: If True, return every PROJECT_WIDGET node (containers + decoration).
            Default False = only clickable or text-carrying nodes.
        max_nodes: Hard cap on returned nodes (bridge default 500, ceiling 2000).
        path_prefix: Keep only nodes whose hierarchy path (relative to the layer
            root, so it includes the panel root name) starts with this prefix, e.g.
            "PROJECT_PANEL/". A robust alternative to panel_uid for isolating one
            panel's subtree.

    Returns:
        {"gameViewWidth", "gameViewHeight", "nodeCount", "nodes": [...]}. Each node
        carries name/path/type/text/textId/visible/clickable and a PNG-space
        (top-left origin) screen rect {screenX, screenY, screenW, screenH}.

    Raises:
        InvalidLayerError: a layer name is not in the allowlist (before any bridge
            traffic).
    """
    args: dict[str, Any] = {"includeAll": bool(include_all)}
    if panel_uid is not None:
        args["panelUid"] = int(panel_uid)
    if layers is not None:
        bad = [lv for lv in layers if lv not in _VALID_LAYERS]
        if bad:
            raise InvalidLayerError(
                f"unknown layer(s) {bad}; must be a subset of {sorted(_VALID_LAYERS)}"
            )
        args["layers"] = list(layers)
    if max_nodes is not None:
        args["maxNodes"] = max(1, min(int(max_nodes), _MAX_NODES_CAP))
    if path_prefix:
        args["pathPrefix"] = str(path_prefix)

    return await bridge_call("ListUINodes", args)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        panel_uid: int | None = None,
        layers: list[str] | None = None,
        include_all: bool = False,
        max_nodes: int | None = None,
        path_prefix: str | None = None,
    ) -> dict[str, Any]:
        return await list_ui_nodes(panel_uid, layers, include_all, max_nodes, path_prefix)
