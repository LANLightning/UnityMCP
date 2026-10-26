"""Tests for open_panel tool (Phase 6 — async open + verify)."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.open_panel import InvalidOpenPanelArg, open_panel


@pytest.mark.asyncio
async def test_open_panel_success_issues_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 200}]})
    bridge.expect_request("OpenPanel").respond_with({
        "uid": 66, "opened": True, "visible": True, "isOpen": True,
        "topPanelUid": 66, "timedOut": False, "refused": False, "elapsedMs": 320.0,
    })

    result = await open_panel(uid=66)
    assert result["visible"] is True
    assert result["uid"] == 66
    assert "write_op_token" in result
    assert result["line_number_before"] == 200
    pending = await GUARD.list_pending()
    assert any(p.token == result["write_op_token"] for p in pending)
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_open_panel_refused_still_issues_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """A PROJECT_CAN_OPEN silent refusal is a normal (non-visible) write result."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 5}]})
    bridge.expect_request("OpenPanel").respond_with({
        "uid": 999, "opened": True, "visible": False, "isOpen": False,
        "topPanelUid": 0, "timedOut": False, "refused": True, "elapsedMs": 5000.0,
    })

    result = await open_panel(uid=999, timeout_ms=5000)
    assert result["visible"] is False
    assert result["refused"] is True
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_open_panel_forwards_info_and_timeout(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request(
        "OpenPanel",
        args_predicate=lambda a: (
            a.get("uid") == 66
            and a.get("timeoutMs") == 8000
            and a.get("info") == {"curPetId": 123}
            and a.get("verify") is True
        ),
    ).respond_with({
        "uid": 66, "opened": True, "visible": True, "isOpen": True,
        "topPanelUid": 66, "timedOut": False, "refused": False, "elapsedMs": 100.0,
    })

    result = await open_panel(uid=66, info={"curPetId": 123}, timeout_ms=8000)
    assert result["visible"] is True
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.parametrize("bad_uid", [0, True, "66"])
@pytest.mark.asyncio
async def test_open_panel_rejects_bad_uid(bad_uid) -> None:
    with pytest.raises(InvalidOpenPanelArg):
        await open_panel(uid=bad_uid)  # type: ignore[arg-type]
    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "open_panel" for p in pending)


@pytest.mark.parametrize("bad_tmo", [50, 200000])
@pytest.mark.asyncio
async def test_open_panel_rejects_bad_timeout(bad_tmo: int) -> None:
    with pytest.raises(InvalidOpenPanelArg, match="timeout_ms"):
        await open_panel(uid=66, timeout_ms=bad_tmo)
