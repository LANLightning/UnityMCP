"""Tests for the conditional (dirty-aware) screenshot gate.

Spec: docs/specs/2026-06-08-conditional-screenshot-gate-design.md
"""
from pathlib import Path

import pytest

from unity_mcp_py.bridge.session import BridgeSession


def test_bridge_session_starts_clean_with_no_tracked_file():
    """A fresh session is not dirty and tracks no prior clean screenshot."""
    session = BridgeSession(host="127.0.0.1", port=12345)
    assert session.code_dirty is False
    assert session.last_clean_screenshot_path is None


@pytest.mark.asyncio
async def test_csharp_reload_sets_code_dirty(
    unity_bridge_session, mock_bridge_server
):
    """A successful C# reload marks the session dirty."""
    from unity_mcp_py.state import GUARD
    from unity_mcp_py.tools.csharp_reload_scripts import csharp_reload_scripts

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 5}]})
    bridge.expect_request("CsharpReloadScripts").respond_with(
        {"reload_id": "r1", "force_was_used": False})

    assert unity_bridge_session.code_dirty is False
    result = await csharp_reload_scripts()
    assert unity_bridge_session.code_dirty is True

    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_csharp_reload_failure_leaves_clean(
    unity_bridge_session, mock_bridge_server
):
    """A failed C# reload (bridge error) must NOT mark dirty."""
    from unity_mcp_py.bridge import bridge_client
    from unity_mcp_py.tools.csharp_reload_scripts import csharp_reload_scripts

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 5}]})
    bridge.expect_request("CsharpReloadScripts").respond_error(
        "INVALID_STATE", "playmode_blocks_csharp_reload")

    with pytest.raises(bridge_client.BridgeError):
        await csharp_reload_scripts()
    assert unity_bridge_session.code_dirty is False


@pytest.mark.asyncio
async def test_lua_reload_sets_code_dirty(
    unity_bridge_session, mock_bridge_server
):
    """A successful Lua reload marks the session dirty."""
    from unity_mcp_py.state import GUARD
    from unity_mcp_py.tools.lua_reload_module import lua_reload_module

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request("LuaReloadModule").respond_with({"ok": True})

    assert unity_bridge_session.code_dirty is False
    result = await lua_reload_module(["Foo.Bar"])
    assert unity_bridge_session.code_dirty is True

    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_lua_reload_invalid_name_leaves_clean(
    unity_bridge_session, mock_bridge_server
):
    """A rejected module name raises before any bridge traffic and stays clean."""
    from unity_mcp_py.tools.lua_reload_module import (
        InvalidLuaModuleNameError,
        lua_reload_module,
    )

    with pytest.raises(InvalidLuaModuleNameError):
        await lua_reload_module(["bad name!"])
    assert unity_bridge_session.code_dirty is False


def _capture_tail(bridge):
    """Script the GetPlayState + TailEditorLog that every capture issues last."""
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})


@pytest.mark.asyncio
async def test_dirty_capture_unarmed_starts_gated_session(
    unity_bridge_session, mock_bridge_server
):
    """Dirty + unarmed → StartVerificationSession(gateScreenshots=True)."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    unity_bridge_session.code_dirty = True
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": False})
    gate_seen = {}
    bridge.expect_request(
        "StartVerificationSession",
        args_predicate=lambda a: gate_seen.setdefault("gate", a.get("gateScreenshots")) or True,
    ).respond_with({"sessionId": "g1", "started": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    _capture_tail(bridge)

    result = await capture_screenshot()
    assert result["auto_started_session"] == "g1"
    assert gate_seen["gate"] is True


@pytest.mark.asyncio
async def test_dirty_capture_armed_ungated_refreshes_to_gated(
    unity_bridge_session, mock_bridge_server
):
    """Dirty + armed-but-ungated → refresh with gateScreenshots=True."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    unity_bridge_session.code_dirty = True
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": False})
    gate_seen = {}
    bridge.expect_request(
        "StartVerificationSession",
        args_predicate=lambda a: gate_seen.setdefault("gate", a.get("gateScreenshots")) or True,
    ).respond_with({"sessionId": "g2", "started": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    _capture_tail(bridge)

    result = await capture_screenshot()
    assert gate_seen["gate"] is True
    assert result["auto_started_session"] == "g2"


@pytest.mark.asyncio
async def test_clean_capture_armed_ungated_is_noop(
    unity_bridge_session, mock_bridge_server
):
    """Clean + armed + already-ungated → NO StartVerificationSession (fast path)."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    assert unity_bridge_session.code_dirty is False
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": False})
    # No StartVerificationSession scripted — if one is issued, the mock errors
    # with "expected 'CaptureScreenshot', got 'StartVerificationSession'".
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    _capture_tail(bridge)

    result = await capture_screenshot()
    assert result["auto_started_session"] is None


@pytest.mark.asyncio
async def test_clean_capture_armed_gated_refreshes_to_ungated(
    unity_bridge_session, mock_bridge_server
):
    """Clean + armed + gated → refresh with gateScreenshots=False."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    assert unity_bridge_session.code_dirty is False
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": True})
    gate_seen = {}
    bridge.expect_request(
        "StartVerificationSession",
        args_predicate=lambda a: gate_seen.setdefault("gate", a.get("gateScreenshots")) or True,
    ).respond_with({"sessionId": "u1", "started": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    _capture_tail(bridge)

    await capture_screenshot()
    assert gate_seen["gate"] is False


@pytest.mark.asyncio
async def test_gated_capture_clears_dirty_on_success(
    unity_bridge_session, mock_bridge_server
):
    """A successful gated capture clears code_dirty (the 继续 click released it)."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    unity_bridge_session.code_dirty = True
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": False})
    bridge.expect_request("StartVerificationSession").respond_with(
        {"sessionId": "g", "started": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    _capture_tail(bridge)

    await capture_screenshot()
    assert unity_bridge_session.code_dirty is False


@pytest.mark.asyncio
async def test_gated_capture_keeps_dirty_on_abort(
    unity_bridge_session, mock_bridge_server
):
    """An aborted gated capture (bridge error) leaves code_dirty set."""
    from unity_mcp_py.bridge import bridge_client
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    unity_bridge_session.code_dirty = True
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": False})
    bridge.expect_request("StartVerificationSession").respond_with(
        {"sessionId": "g", "started": True})
    bridge.expect_request("CaptureScreenshot").respond_error(
        "INVALID_STATE", "screenshot_approval_aborted")

    with pytest.raises(bridge_client.BridgeError):
        await capture_screenshot()
    assert unity_bridge_session.code_dirty is True


@pytest.mark.asyncio
async def test_clean_capture_does_not_touch_dirty(
    unity_bridge_session, mock_bridge_server
):
    """A clean capture never sets dirty true."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": False})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "ZmFrZQ==", "sizeBytes": 6})
    _capture_tail(bridge)

    await capture_screenshot()
    assert unity_bridge_session.code_dirty is False


def _script_clean_file_ref_capture(bridge, png_base64):
    """Script a clean (ungated) file_ref capture returning the given bytes."""
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": False})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": png_base64, "sizeBytes": 8})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})


@pytest.mark.asyncio
async def test_clean_file_ref_deletes_previous_on_next_capture(
    unity_bridge_session, mock_bridge_server, tmp_path, monkeypatch
):
    """The next clean file_ref capture unlinks the prior tracked file."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    monkeypatch.setenv("UNITY_MCP_SCREENSHOT_DIR", str(tmp_path))
    bridge = mock_bridge_server

    # First clean capture: writes file A.
    _script_clean_file_ref_capture(bridge, "iVBORw0KGgo=")
    r1 = await capture_screenshot(mode="file_ref")
    first = Path(r1["file_path"])
    assert first.is_file()
    assert unity_bridge_session.last_clean_screenshot_path == str(first)

    # Second clean capture with DIFFERENT bytes: writes file B, deletes A.
    _script_clean_file_ref_capture(bridge, "iVBORw0KGgoAAA==")
    r2 = await capture_screenshot(mode="file_ref")
    second = Path(r2["file_path"])
    assert second.is_file()
    assert second != first
    assert not first.exists()                      # previous shot deleted
    assert unity_bridge_session.last_clean_screenshot_path == str(second)


@pytest.mark.asyncio
async def test_clean_full_mode_still_cleans_prior_tracked_file(
    unity_bridge_session, mock_bridge_server, tmp_path, monkeypatch
):
    """A full-mode clean capture writes no file but still deletes the prior one."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    monkeypatch.setenv("UNITY_MCP_SCREENSHOT_DIR", str(tmp_path))
    bridge = mock_bridge_server

    _script_clean_file_ref_capture(bridge, "iVBORw0KGgo=")
    r1 = await capture_screenshot(mode="file_ref")
    first = Path(r1["file_path"])
    assert first.is_file()

    # full mode writes nothing to disk (file_path == "") but should clean prior.
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": False})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "QUJD", "sizeBytes": 3})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})

    r2 = await capture_screenshot(mode="full")
    assert r2["file_path"] == ""
    assert not first.exists()                      # prior tracked file deleted
    assert unity_bridge_session.last_clean_screenshot_path is None


@pytest.mark.asyncio
async def test_clean_unlink_failure_is_swallowed(
    unity_bridge_session, mock_bridge_server, tmp_path, monkeypatch
):
    """If the previous file is already gone, the capture still returns cleanly."""
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    monkeypatch.setenv("UNITY_MCP_SCREENSHOT_DIR", str(tmp_path))
    # Point the tracker at a path that does not exist; unlink must not raise.
    unity_bridge_session.last_clean_screenshot_path = str(tmp_path / "gone.png")
    bridge = mock_bridge_server
    _script_clean_file_ref_capture(bridge, "iVBORw0KGgo=")

    result = await capture_screenshot(mode="file_ref")
    assert result["file_path"]                     # capture succeeded


@pytest.mark.asyncio
async def test_gated_file_ref_tracks_file_for_reclamation(
    unity_bridge_session, mock_bridge_server, tmp_path, monkeypatch
):
    """A gated file_ref capture persists a PNG that the NEXT capture reclaims.

    Under the gate the bridge holds the frame in memory and deletes its own file
    on 继续 BEFORE Python writes the file_ref PNG, so Python's file must be tracked
    and reclaimed on the next capture (else it leaks).
    """
    from unity_mcp_py.tools.capture_screenshot import capture_screenshot

    monkeypatch.setenv("UNITY_MCP_SCREENSHOT_DIR", str(tmp_path))
    bridge = mock_bridge_server

    # Gated file_ref capture: dirty → gate on → writes file A, tracks it.
    unity_bridge_session.code_dirty = True
    bridge.expect_request("GetVerificationSessionState").respond_with({"armed": False})
    bridge.expect_request("StartVerificationSession").respond_with(
        {"sessionId": "g", "started": True})
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "iVBORw0KGgo=", "sizeBytes": 8})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})

    r1 = await capture_screenshot(mode="file_ref")
    first = Path(r1["file_path"])
    assert first.is_file()
    assert unity_bridge_session.code_dirty is False          # gated success cleared it
    assert unity_bridge_session.last_clean_screenshot_path == str(first)

    # Next capture (now clean) with different bytes reclaims file A.
    _script_clean_file_ref_capture(bridge, "iVBORw0KGgoAAA==")
    r2 = await capture_screenshot(mode="file_ref")
    second = Path(r2["file_path"])
    assert second.is_file()
    assert not first.exists()                                 # gated file reclaimed
    assert unity_bridge_session.last_clean_screenshot_path == str(second)
