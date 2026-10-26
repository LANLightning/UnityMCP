"""Tests for csharp_reload_scripts tool."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.csharp_reload_scripts import csharp_reload_scripts


@pytest.mark.asyncio
async def test_csharp_reload_scripts_default_force_false(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Default call (force=false) returns the bridge payload and issues a token."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 120}]})
    bridge.expect_request("CsharpReloadScripts").respond_with({
        "reload_id": "11111111-1111-4111-8111-111111111111",
        "force_was_used": False,
    })

    result = await csharp_reload_scripts()

    assert result["reload_id"] == "11111111-1111-4111-8111-111111111111"
    assert result["force_was_used"] is False
    assert "write_op_token" in result
    assert result["line_number_before"] == 120
    assert result["requires_observation"] is True

    pending = await GUARD.list_pending()
    assert any(p.token == result["write_op_token"] for p in pending)

    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_csharp_reload_scripts_force_true_reports_force_was_used(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """force=True is forwarded and the bridge echoes force_was_used=True."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 999}]})
    bridge.expect_request("CsharpReloadScripts").respond_with({
        "reload_id": "22222222-2222-4222-8222-222222222222",
        "force_was_used": True,
    })

    result = await csharp_reload_scripts(force=True)

    assert result["force_was_used"] is True
    assert result["reload_id"] == "22222222-2222-4222-8222-222222222222"
    assert "write_op_token" in result

    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_csharp_reload_scripts_no_token_on_bridge_error(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Bridge error must NOT issue a write_op_token.

    The no-token-on-error contract from spec §6.2 is critical for the runtime-
    evidence loop: if a compile request failed at the transport level, the AI
    must not be led to believe there is anything to observe.
    """
    from unity_mcp_py.bridge import bridge_client
    BridgeError = bridge_client.BridgeError

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 321}]})
    bridge.expect_request("CsharpReloadScripts").respond_error(
        "INVALID_STATE", "playmode_blocks_csharp_reload"
    )

    with pytest.raises(BridgeError, match="playmode_blocks_csharp_reload"):
        await csharp_reload_scripts(force=False)

    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "csharp_reload_scripts" for p in pending)
