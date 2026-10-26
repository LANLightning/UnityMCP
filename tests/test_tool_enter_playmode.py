"""Tests for enter_playmode tool."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.enter_playmode import enter_playmode


@pytest.mark.asyncio
async def test_enter_playmode_issues_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """enter_playmode must return bridge result enriched with write_op_token + metadata."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 50}]})
    bridge.expect_request("EnterPlaymode").respond_with({"state": "PlayMode"})

    result = await enter_playmode()

    assert result["state"] == "PlayMode"
    assert result["requires_observation"] is True
    assert "write_op_token" in result
    assert result["line_number_before"] == 50

    # Token must be registered in GUARD
    pending = await GUARD.list_pending()
    assert any(p.token == result["write_op_token"] for p in pending)

    # Cleanup
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_enter_playmode_no_token_on_bridge_error(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Token must NOT be issued when bridge returns an error response."""
    from unity_mcp_py.bridge import bridge_client
    BridgeError = bridge_client.BridgeError

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 50}]})
    bridge.expect_request("EnterPlaymode").respond_error("INTERNAL", "Editor not ready")

    with pytest.raises(BridgeError, match="Editor not ready"):
        await enter_playmode()

    # No token should have been issued for this failed call
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "enter_playmode" for p in pending)
