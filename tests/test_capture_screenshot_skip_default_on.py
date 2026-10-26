"""capture_screenshot must suppress a byte-identical frame BY DEFAULT.

Token-cost optimization (telemetry: screenshots are ~96% of all token spend; a
single full-res frame can be ~2MB base64 / hundreds of K tokens). When an agent
takes several verification screenshots of the SAME on-screen state in a row, the
2nd..Nth identical frames cost full tokens for zero new information.

Making skip_if_unchanged default to True suppresses ONLY a frame whose sha256 is
identical to the previous one this process returned. The moment the screen
changes, the new frame is returned in full — so the "final verification must SHOW
a screenshot" rule is preserved: any frame the model hasn't seen yet always comes
back with its base64 intact.

This is strictly safer than changing the default `mode`: mode=file_ref would
hide EVERY frame's bytes behind a path (the model sees nothing in MCP clients
that can't read local files); skip_if_unchanged only hides a REPEAT of a frame
the model already saw.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from unity_mcp_py.tools.capture_screenshot import capture_screenshot
from unity_mcp_py.bridge.session import get_session


@pytest.fixture(autouse=True)
def _reset_last_sha(unity_bridge_session):
    sess = get_session()
    if hasattr(sess, "last_screenshot_sha"):
        sess.last_screenshot_sha = None
    yield


def _script_capture(bridge, png_base64: str, size: int = 4) -> None:
    """Script one full capture round-trip sequence returning the given bytes."""
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": png_base64, "sizeBytes": size})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})


@pytest.mark.asyncio
async def test_identical_frame_suppressed_by_default(
    unity_bridge_session, mock_bridge_server
):
    """With NO skip_if_unchanged arg, a 2nd identical frame is suppressed."""
    bridge = mock_bridge_server
    _script_capture(bridge, "U0FNRQ==")
    _script_capture(bridge, "U0FNRQ==")  # same bytes

    first = await capture_screenshot()           # default args only
    second = await capture_screenshot()          # default args only

    # First frame is always shown in full (model hasn't seen it yet).
    assert first["png_base64"] == "U0FNRQ=="
    assert first["unchanged"] is False
    # Second identical frame is suppressed by the new default.
    assert second["unchanged"] is True
    assert second["png_base64"] == ""
    # sha + metadata still reported so the caller knows which frame matched.
    assert second["sha256"] == first["sha256"]
    assert second["play_state"] == "PlayMode"


@pytest.mark.asyncio
async def test_changed_frame_still_shown_by_default(
    unity_bridge_session, mock_bridge_server
):
    """A CHANGED frame must always come back in full — the see-the-screenshot
    rule is never weakened by the default-on skip."""
    bridge = mock_bridge_server
    _script_capture(bridge, "Rklstg==")
    _script_capture(bridge, "T1RIRVI=")  # different bytes

    first = await capture_screenshot()
    second = await capture_screenshot()

    assert first["png_base64"] == "Rklstg=="
    assert second["unchanged"] is False
    assert second["png_base64"] == "T1RIRVI="
    assert second["sha256"] != first["sha256"]


@pytest.mark.asyncio
async def test_explicit_skip_false_overrides_default(
    unity_bridge_session, mock_bridge_server
):
    """A caller that explicitly wants every frame can pass skip_if_unchanged=False
    and get full bytes even on an identical frame (opt-out escape hatch)."""
    bridge = mock_bridge_server
    _script_capture(bridge, "U0FNRQ==")
    _script_capture(bridge, "U0FNRQ==")

    first = await capture_screenshot(skip_if_unchanged=False)
    second = await capture_screenshot(skip_if_unchanged=False)

    assert first["png_base64"] == "U0FNRQ=="
    assert second["png_base64"] == "U0FNRQ=="
    assert second["unchanged"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_mode", ["metadata", "file_ref"])
async def test_non_full_capture_does_not_suppress_first_full(
    unity_bridge_session, mock_bridge_server, monkeypatch, tmp_path, initial_mode
):
    monkeypatch.setenv("UNITY_MCP_SCREENSHOT_DIR", str(tmp_path))
    _script_capture(mock_bridge_server, "U0FNRQ==")
    _script_capture(mock_bridge_server, "U0FNRQ==")

    initial = await capture_screenshot(mode=initial_mode)
    assert initial["png_base64"] == ""
    assert unity_bridge_session.last_screenshot_sha is None
    full = await capture_screenshot(mode="full")

    assert full["png_base64"] == "U0FNRQ=="
    assert full["unchanged"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_mode", ["full", "file_ref"])
async def test_file_ref_always_returns_existing_image(
    unity_bridge_session, mock_bridge_server, monkeypatch, tmp_path, initial_mode
):
    monkeypatch.setenv("UNITY_MCP_SCREENSHOT_DIR", str(tmp_path))
    _script_capture(mock_bridge_server, "U0FNRQ==")
    _script_capture(mock_bridge_server, "U0FNRQ==")

    initial = await capture_screenshot(mode=initial_mode)
    result = await capture_screenshot(mode="file_ref")

    assert result["file_path"]
    assert Path(result["file_path"]).read_bytes() == b"SAME"
    assert result["unchanged"] is False
    if initial_mode == "file_ref":
        assert result["file_path"] == initial["file_path"]
        assert Path(initial["file_path"]).is_file()
