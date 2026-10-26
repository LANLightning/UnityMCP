from pathlib import Path

import pytest
from unity_mcp_py.tools.capture_screenshot import capture_screenshot


@pytest.mark.asyncio
async def test_capture_screenshot_returns_png_and_play_state(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with(
        {"lines": [{"lineNumber": 42}]})

    result = await capture_screenshot()

    assert result["png_base64"] == "ZmFrZQ=="
    assert result["sizeBytes"] == 6
    assert result["play_state"] == "PlayMode"
    assert result["line_number"] == 42


@pytest.mark.asyncio
async def test_capture_screenshot_derives_size_bytes_when_bridge_omits_it(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with({"pngBase64": "ZmFrZQ=="})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    result = await capture_screenshot()

    assert result["png_base64"] == "ZmFrZQ=="
    assert result["sizeBytes"] == 4
    assert result["screenshot_available"] is True
    assert result["capture_error"] is None


@pytest.mark.asyncio
async def test_capture_screenshot_handles_null_texture_error(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_error(
        "INTERNAL", "Passed in texture is invalid (null).")
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    result = await capture_screenshot()

    assert result["png_base64"] == ""
    assert result["sizeBytes"] == 0
    assert result["play_state"] == "EditMode"
    assert result["line_number"] == 0
    assert result["screenshot_available"] is False
    assert "texture is invalid" in result["capture_error"]


@pytest.mark.asyncio
async def test_capture_screenshot_metadata_mode_omits_base64(
    unity_bridge_session, mock_bridge_server
):
    """mode='metadata' returns sha256+sizeBytes; no base64 in payload."""
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "QUJDREVG", "sizeBytes": 4})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    result = await capture_screenshot(mode="metadata")

    assert result["png_base64"] == ""
    assert result["sizeBytes"] == 4
    assert len(result["sha256"]) == 64
    assert result["mode"] == "metadata"
    assert result["file_path"] == ""


@pytest.mark.asyncio
async def test_capture_screenshot_full_mode_keeps_base64(
    unity_bridge_session, mock_bridge_server
):
    """mode='full' (default) preserves legacy behavior."""
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "QUJDREVG", "sizeBytes": 4})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    result = await capture_screenshot(mode="full")

    assert result["png_base64"] == "QUJDREVG"
    assert result["mode"] == "full"
    assert len(result["sha256"]) == 64


@pytest.mark.asyncio
async def test_capture_screenshot_file_ref_mode_writes_disk(
    unity_bridge_session, mock_bridge_server, tmp_path, monkeypatch
):
    """mode='file_ref' persists PNG to UNITY_MCP_SCREENSHOT_DIR and returns the path."""
    monkeypatch.setenv("UNITY_MCP_SCREENSHOT_DIR", str(tmp_path))
    bridge = mock_bridge_server
    # base64 "iVBORw0KGgo=" is a 9-byte PNG header sentinel; round-trips cleanly.
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "iVBORw0KGgo=", "sizeBytes": 8})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    result = await capture_screenshot(mode="file_ref")

    assert result["png_base64"] == ""
    assert result["mode"] == "file_ref"
    assert result["file_path"]
    fp = Path(result["file_path"])
    assert fp.is_file()
    assert fp.parent == tmp_path
    assert fp.suffix == ".png"
    assert fp.read_bytes() == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_capture_screenshot_rejects_invalid_mode(
    unity_bridge_session, mock_bridge_server
):
    with pytest.raises(ValueError, match="mode="):
        await capture_screenshot(mode="bogus")


@pytest.mark.asyncio
async def test_capture_screenshot_auto_starts_session_when_unarmed(
    unity_bridge_session, mock_bridge_server
):
    """If no verification session is armed, capture_screenshot opens one first."""
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": False})
    bridge.expect_request("StartVerificationSession").respond_with(
        {"sessionId": "auto-123", "started": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 9}]})

    result = await capture_screenshot()

    assert result["png_base64"] == "ZmFrZQ=="
    assert result["auto_started_session"] == "auto-123"


@pytest.mark.asyncio
async def test_capture_screenshot_reuses_armed_session(
    unity_bridge_session, mock_bridge_server
):
    """If a session is already armed, no StartVerificationSession is issued."""
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "sessionId": "existing"})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})

    result = await capture_screenshot()
    assert result["auto_started_session"] is None
