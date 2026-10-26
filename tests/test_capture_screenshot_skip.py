"""Feature 4 — capture_screenshot skip_if_unchanged + downscale width.

skip_if_unchanged compares the captured frame's sha256 against the last sha
returned by THIS process (stored on the BridgeSession singleton). On a
byte-identical frame it returns {unchanged: True, png_base64: ""}, killing the
1-3MB base64 payload. A changed frame returns normally and updates the stored
sha. width is passed through to the bridge as a downscale hint.
"""
from __future__ import annotations

import pytest

from unity_mcp_py.tools.capture_screenshot import capture_screenshot
from unity_mcp_py.bridge.session import get_session


@pytest.fixture(autouse=True)
def _reset_last_sha(unity_bridge_session):
    """Each test starts with a clean last-screenshot-sha on the singleton."""
    sess = get_session()
    if hasattr(sess, "last_screenshot_sha"):
        sess.last_screenshot_sha = None
    yield


@pytest.mark.asyncio
async def test_skip_if_unchanged_returns_unchanged_on_identical_frame(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    # Two captures of the SAME bytes. First returns the frame, second is skipped.
    for _ in range(2):
        bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
        bridge.expect_request("CaptureScreenshot").respond_with(
            {"pngBase64": "U0FNRQ==", "sizeBytes": 4})
        bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
        bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})

    first = await capture_screenshot(skip_if_unchanged=True)
    assert first["png_base64"] == "U0FNRQ=="
    assert first["unchanged"] is False

    second = await capture_screenshot(skip_if_unchanged=True)
    assert second["unchanged"] is True
    assert second["png_base64"] == ""
    # sha + metadata still reported so the caller knows which frame it matched.
    assert second["sha256"] == first["sha256"]
    assert second["play_state"] == "PlayMode"


@pytest.mark.asyncio
async def test_skip_if_unchanged_returns_frame_when_changed(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "Rklstg==", "sizeBytes": 4})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "T1RIRVI=", "sizeBytes": 5})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    first = await capture_screenshot(skip_if_unchanged=True)
    second = await capture_screenshot(skip_if_unchanged=True)

    assert first["png_base64"] == "Rklstg=="
    assert second["unchanged"] is False
    assert second["png_base64"] == "T1RIRVI="
    assert second["sha256"] != first["sha256"]


@pytest.mark.asyncio
async def test_explicit_skip_false_keeps_full_frame(
    unity_bridge_session, mock_bridge_server
):
    """With skip_if_unchanged=False (the explicit opt-out), two identical frames
    both return full base64.

    NOTE: as of 2026-06-12 skip_if_unchanged defaults to True, so callers that
    want every byte-identical frame in full must now opt out explicitly. The
    default-on behavior is covered by test_capture_screenshot_skip_default_on.py.
    """
    bridge = mock_bridge_server
    for _ in range(2):
        bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
        bridge.expect_request("CaptureScreenshot").respond_with(
            {"pngBase64": "U0FNRQ==", "sizeBytes": 4})
        bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
        bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    first = await capture_screenshot(skip_if_unchanged=False)
    second = await capture_screenshot(skip_if_unchanged=False)

    assert first["png_base64"] == "U0FNRQ=="
    assert second["png_base64"] == "U0FNRQ=="
    assert second["unchanged"] is False


@pytest.mark.asyncio
async def test_width_passed_through_to_bridge(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    captured: dict = {}

    def _capture_args(args: dict) -> bool:
        captured.update(args)
        return True

    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request(
        "CaptureScreenshot", args_predicate=_capture_args
    ).respond_with({"pngBase64": "QQ==", "sizeBytes": 1})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})

    await capture_screenshot(width=960)

    assert captured.get("width") == 960
