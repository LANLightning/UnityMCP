"""Tests for run_method tool."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.run_method import run_method


@pytest.mark.asyncio
async def test_run_method_issues_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """run_method must return bridge result enriched with write_op_token."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 90}]})
    bridge.expect_request("RunEditMethod").respond_with(
        {"returnValue": 42, "capturedLogs": []}
    )

    result = await run_method("My.Class", "DoThing", [1, 2])

    assert result["returnValue"] == 42
    assert result["capturedLogs"] == []
    assert "write_op_token" in result
    assert result["line_number_before"] == 90
    assert result["requires_observation"] is True

    # Token must be registered in GUARD
    pending = await GUARD.list_pending()
    assert any(p.token == result["write_op_token"] for p in pending)

    # Cleanup
    await GUARD.consume([result["write_op_token"]])
