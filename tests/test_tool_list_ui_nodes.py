"""Tests for list_ui_nodes tool (Phase 5 — UI node inventory)."""
import pytest

from unity_mcp_py.tools.list_ui_nodes import InvalidLayerError, list_ui_nodes


@pytest.mark.asyncio
async def test_list_ui_nodes_passthrough(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Returns the bridge node inventory; read-only (no write_op_token)."""
    bridge = mock_bridge_server
    bridge.expect_request("ListUINodes").respond_with({
        "gameViewWidth": 1366,
        "gameViewHeight": 768,
        "nodeCount": 1,
        "nodes": [{
            "index": 0, "instanceId": 123, "panelUid": 66, "layer": "PROJECT_PANEL_LAYER",
            "name": "btnClose", "path": "Window/btnClose", "type": "PROJECT_BUTTON",
            "text": "关闭", "visible": True, "clickable": True,
            "screenX": 10.0, "screenY": 20.0, "screenW": 80.0, "screenH": 30.0,
        }],
    })

    result = await list_ui_nodes(panel_uid=66)
    assert result["nodeCount"] == 1
    assert result["nodes"][0]["name"] == "btnClose"
    assert result["nodes"][0]["clickable"] is True
    assert "write_op_token" not in result


@pytest.mark.asyncio
async def test_list_ui_nodes_forwards_args(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """panel_uid / layers / include_all / max_nodes reach the bridge."""
    bridge = mock_bridge_server
    bridge.expect_request(
        "ListUINodes",
        args_predicate=lambda a: (
            a.get("panelUid") == 69
            and a.get("layers") == ["PROJECT_POPUP_LAYER"]
            and a.get("includeAll") is True
            and a.get("maxNodes") == 50
        ),
    ).respond_with({"gameViewWidth": 0, "gameViewHeight": 0, "nodeCount": 0, "nodes": []})

    result = await list_ui_nodes(
        panel_uid=69, layers=["PROJECT_POPUP_LAYER"], include_all=True, max_nodes=50
    )
    assert result["nodeCount"] == 0


@pytest.mark.parametrize("bad_layers", [["frobnicate"], ["PROJECT_PANEL_LAYER", "nope"], ["Overlay"]])
@pytest.mark.asyncio
async def test_list_ui_nodes_rejects_bad_layer(bad_layers: list) -> None:
    """Unknown layer names are rejected before any bridge call."""
    with pytest.raises(InvalidLayerError):
        await list_ui_nodes(layers=bad_layers)


@pytest.mark.asyncio
async def test_list_ui_nodes_caps_max_nodes(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """max_nodes is clamped to the 2000 ceiling before being sent."""
    bridge = mock_bridge_server
    bridge.expect_request(
        "ListUINodes",
        args_predicate=lambda a: a.get("maxNodes") == 2000,
    ).respond_with({"gameViewWidth": 0, "gameViewHeight": 0, "nodeCount": 0, "nodes": []})

    await list_ui_nodes(max_nodes=999999)
