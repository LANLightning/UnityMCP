"""Soft-grounding wiring: capture_screenshot / get_console_logs / observe_evidence
reset the lua runway after a real bridge round-trip (spec 2026-06-09 §6.3)."""
import pytest

from unity_mcp_py.state import LUA_BUDGET
from unity_mcp_py.tools.capture_screenshot import capture_screenshot
from unity_mcp_py.tools.get_console_logs import get_console_logs
from unity_mcp_py.tools.observe_evidence import observe_evidence


@pytest.fixture(autouse=True)
async def _reset_lua_budget():
    await LUA_BUDGET.reset()
    yield
    await LUA_BUDGET.reset()


async def _fill_runway(n: int = 2):
    for _ in range(n):
        await LUA_BUDGET.charge_run()


@pytest.mark.asyncio
async def test_get_console_logs_soft_grounds(unity_bridge_session, mock_bridge_server):
    await _fill_runway()
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with(
        {"lines": [{"lineNumber": 5, "logType": "Log", "message": "hi", "stackTrace": ""}]}
    )
    result = await get_console_logs()
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0
    assert snap["soft_used"] == 1
    assert result["lua_runway"]["grounded"] is True


@pytest.mark.asyncio
async def test_observe_evidence_soft_grounds(unity_bridge_session, mock_bridge_server):
    await _fill_runway()
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": []})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    result = await observe_evidence()
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0
    assert snap["soft_used"] == 1
    assert result["lua_runway"]["grounded"] is True


@pytest.mark.asyncio
async def test_capture_screenshot_soft_grounds_on_real_frame(
    unity_bridge_session, mock_bridge_server
):
    await _fill_runway()
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": False, "sessionId": "s1"}
    )
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": "aGVsbG8=", "sizeBytes": 5}
    )
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 9}]})
    result = await capture_screenshot(mode="metadata")
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 0
    assert snap["soft_used"] == 1
    assert result["lua_runway"]["grounded"] is True


@pytest.mark.asyncio
async def test_capture_screenshot_no_ground_without_frame(
    unity_bridge_session, mock_bridge_server
):
    """A null-texture capture (no frame) does NOT ground."""
    from unity_mcp_py.bridge import bridge_client
    await _fill_runway()
    bridge = mock_bridge_server
    bridge.expect_request("GetVerificationSessionState").respond_with(
        {"armed": True, "gateScreenshots": False, "sessionId": "s1"}
    )
    bridge.expect_request("CaptureScreenshot").respond_error(
        "INTERNAL", "texture is null/invalid"
    )
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 9}]})
    result = await capture_screenshot(mode="metadata")
    assert result["screenshot_available"] is False
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 2   # unchanged: no real frame -> no ground
    assert result["lua_runway"]["grounded"] is False


@pytest.mark.asyncio
async def test_soft_limit_reached_reports_not_grounded(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    """After SOFT_LIMIT soft grounds, a further soft ground does not clear."""
    monkeypatch.setattr(LUA_BUDGET, "soft_limit", 1)
    await LUA_BUDGET.ground(hard=False)   # soft_used -> 1 == limit
    await _fill_runway(1)                 # since_ground -> 1
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with(
        {"lines": [{"lineNumber": 5, "logType": "Log", "message": "hi", "stackTrace": ""}]}
    )
    result = await get_console_logs()
    assert result["lua_runway"]["grounded"] is False
    snap = await LUA_BUDGET.snapshot()
    assert snap["since_ground"] == 1      # not cleared
