import pytest
from unity_mcp_py.tools.ensure_ready import ensure_ready


@pytest.mark.asyncio
async def test_ensure_ready_returns_ready_when_connected(
    unity_bridge_session, mock_bridge_server, monkeypatch
):
    monkeypatch.setenv("UNITY_MCP_PROJECT_PATH", "/mock/project")
    bridge = mock_bridge_server
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    result = await ensure_ready()

    assert result["ready"] is True
    assert result["playmodeState"] == "EditMode"


@pytest.mark.asyncio
async def test_ensure_ready_missing_project_path_returns_error(monkeypatch):
    monkeypatch.delenv("UNITY_MCP_PROJECT_PATH", raising=False)
    result = await ensure_ready(project_path=None)
    assert result["ready"] is False
    assert "project_path missing" in result["error"]


@pytest.mark.asyncio
async def test_ensure_ready_bridge_unreachable_returns_error(monkeypatch):
    """When the bridge cannot be reached, returns ready=False with remedy."""
    from unity_mcp_py.bridge.session import set_session_for_test, BridgeSession

    class _FailingSession(BridgeSession):
        async def connect(self):
            raise ConnectionRefusedError("Connection refused")

    set_session_for_test(_FailingSession(port=19999))
    try:
        result = await ensure_ready(project_path="/mock/project")
        assert result["ready"] is False
        assert "bridge_unreachable" in result["error"]
        assert "remedy" in result
    finally:
        set_session_for_test(None)
