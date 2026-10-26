import pytest
from unity_mcp_py.tools.observe_evidence import observe_evidence
from unity_mcp_py.state import GUARD


@pytest.mark.asyncio
async def test_observe_evidence_returns_runtime_pack(
    unity_bridge_session, mock_bridge_server
):
    """observe_evidence(include_screenshot=True, verbose=True) returns the full
    legacy runtime pack: play_state, log_delta (all levels), scene_objects, shot.

    PERF 2026-06-06: the DEFAULT response is now slim (Warning+ logs only), so a
    Log-level line + raw scene_objects require verbose=True. The default-slim
    behavior is covered in test_observe_evidence_slim.py.
    """
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 100, "logType": "Log",
         "message": "PlayMode entered", "stackTrace": ""}
    ]})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": [
        {"name": "Cube", "instanceId": 1, "active": True,
         "components": ["Transform"], "childCount": 0}
    ]})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZS1wbmc=", "sizeBytes": 8})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})

    result = await observe_evidence(
        since_line_number=None, consume_tokens=[], include_screenshot=True,
        verbose=True,
    )

    assert result["play_state"] == "PlayMode"
    assert result["current_line_number"] == 100
    assert len(result["log_delta"]) == 1
    assert result["log_delta"][0]["condition"] == "PlayMode entered"
    assert len(result["scene_objects"]) == 1
    assert result["screenshot_png_base64"] == "ZmFrZS1wbmc="
    assert result["screenshot_skipped"] is False
    assert result["consumed_tokens"] == []


@pytest.mark.asyncio
async def test_observe_evidence_consumes_token(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": []})
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    ctx = await GUARD.issue("test_tool", "{}", 50)
    result = await observe_evidence(consume_tokens=[ctx.token])

    assert result["consumed_tokens"] == [ctx.token]
    assert result["expired_tokens"] == []
    assert result["unknown_tokens"] == []


@pytest.mark.asyncio
async def test_observe_evidence_no_screenshot_by_default(
    unity_bridge_session, mock_bridge_server
):
    """PERF 2026-05-25: default include_screenshot=False — no CaptureScreenshot
    request is dispatched; screenshot_png_base64 is empty; screenshot_skipped=True."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": []})
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    result = await observe_evidence()

    assert result["screenshot_png_base64"] == ""
    assert result["screenshot_skipped"] is True
    assert result["play_state"] == "EditMode"


@pytest.mark.asyncio
async def test_observe_evidence_include_screenshot_true_returns_image(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": []})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "QUJD", "sizeBytes": 2})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})

    result = await observe_evidence(include_screenshot=True)

    assert result["screenshot_png_base64"] == "QUJD"
    assert result["screenshot_skipped"] is False


@pytest.mark.asyncio
async def test_observe_evidence_max_log_lines_passed_through(
    unity_bridge_session, mock_bridge_server
):
    """max_log_lines must reach the bridge as TailEditorLog.maxLines."""
    bridge = mock_bridge_server
    captured: dict = {}

    def _capture_args(args: dict) -> bool:
        captured.update(args)
        return True

    bridge.expect_request("TailEditorLog", args_predicate=_capture_args).respond_with(
        {"lines": []})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": []})
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    await observe_evidence(max_log_lines=20)

    assert captured.get("maxLines") == 20
