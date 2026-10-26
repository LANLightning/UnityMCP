import pytest

from unity_mcp_py.recorder import RECORDER
from unity_mcp_py.tools.clear_console import clear_console


@pytest.fixture(autouse=True)
def _reset_cursor():
    RECORDER.set_cursor(0)
    yield
    RECORDER.set_cursor(0)


@pytest.mark.asyncio
async def test_advances_cursor_to_latest(
    unity_bridge_session, mock_bridge_server
):
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 73, "logType": "Log", "message": "x", "stackTrace": ""}
    ]})

    result = await clear_console()
    assert result["previous_cursor"] == 0
    assert result["new_cursor"] == 73
    assert RECORDER.get_cursor() == 73


@pytest.mark.asyncio
async def test_keeps_cursor_when_no_lines(
    unity_bridge_session, mock_bridge_server
):
    RECORDER.set_cursor(12)
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": []})
    result = await clear_console()
    assert result["previous_cursor"] == 12
    assert result["new_cursor"] == 12
