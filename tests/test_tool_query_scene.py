import pytest
from unity_mcp_py.tools.query_scene import query_scene


@pytest.mark.asyncio
async def test_query_scene_returns_play_state(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": [
        {"name": "X", "instanceId": 1, "active": True,
         "components": [], "childCount": 0}
    ]})
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 5}]})

    res = await query_scene(scene=None)
    assert res["play_state"] == "EditMode"
    assert res["line_number"] == 5
    assert len(res["rootObjects"]) == 1
