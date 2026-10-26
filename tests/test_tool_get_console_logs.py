import pytest

from unity_mcp_py.recorder import RECORDER
from unity_mcp_py.tools.get_console_logs import get_console_logs


@pytest.fixture(autouse=True)
def _reset_cursor():
    RECORDER.set_cursor(0)
    yield
    RECORDER.set_cursor(0)


@pytest.mark.asyncio
async def test_returns_lines_with_basic_fields(
    unity_bridge_session, mock_bridge_server
):
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 10, "logType": "Log", "message": "hello", "stackTrace": ""},
        {"lineNumber": 11, "logType": "Warning", "message": "warn", "stackTrace": "s"},
    ]})

    res = await get_console_logs()
    assert res["next_seq"] == 11
    assert res["truncated"] is False
    assert res["cursor"] == 0
    assert [ln["seq"] for ln in res["lines"]] == [10, 11]
    assert res["lines"][1]["level"] == "Warning"
    assert res["lines"][1]["stack_trace"] == "s"


@pytest.mark.asyncio
async def test_filters_by_level(unity_bridge_session, mock_bridge_server):
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 1, "logType": "Log", "message": "a", "stackTrace": ""},
        {"lineNumber": 2, "logType": "Error", "message": "b", "stackTrace": ""},
        {"lineNumber": 3, "logType": "Warning", "message": "c", "stackTrace": ""},
    ]})
    res = await get_console_logs(level=["Error", "Warning"])
    levels = [ln["level"] for ln in res["lines"]]
    assert levels == ["Error", "Warning"]


@pytest.mark.asyncio
async def test_filters_by_pattern(unity_bridge_session, mock_bridge_server):
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 1, "logType": "Log", "message": "spawned cube", "stackTrace": ""},
        {"lineNumber": 2, "logType": "Log", "message": "ignored", "stackTrace": ""},
        {"lineNumber": 3, "logType": "Log", "message": "spawn sphere", "stackTrace": ""},
    ]})
    res = await get_console_logs(pattern=r"^spawn ")
    msgs = [ln["message"] for ln in res["lines"]]
    assert msgs == ["spawn sphere"]


@pytest.mark.asyncio
async def test_truncates_to_limit(unity_bridge_session, mock_bridge_server):
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": i, "logType": "Log", "message": str(i), "stackTrace": ""}
        for i in range(1, 11)
    ]})
    res = await get_console_logs(limit=3)
    assert res["truncated"] is True
    assert [ln["seq"] for ln in res["lines"]] == [8, 9, 10]


@pytest.mark.asyncio
async def test_uses_virtual_cursor_when_since_none(
    unity_bridge_session, mock_bridge_server
):
    RECORDER.set_cursor(42)
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": []})
    res = await get_console_logs()
    assert res["next_seq"] == 42
    assert res["cursor"] == 42


@pytest.mark.asyncio
async def test_passes_limit_as_max_lines_to_bridge(
    unity_bridge_session, mock_bridge_server
):
    mock_bridge_server.expect_request(
        "TailEditorLog",
        args_predicate=lambda args: args.get("maxLines") == 3,
    ).respond_with({"lines": [
        {"lineNumber": 1, "logType": "Log", "message": "a", "stackTrace": ""},
    ]})

    res = await get_console_logs(limit=3)

    assert res["next_seq"] == 1


@pytest.mark.asyncio
async def test_caps_bridge_fetch_limit_to_500(
    unity_bridge_session, mock_bridge_server
):
    mock_bridge_server.expect_request(
        "TailEditorLog",
        args_predicate=lambda args: args.get("maxLines") == 500,
    ).respond_with({"lines": []})

    res = await get_console_logs(limit=5000)

    assert res["lines"] == []