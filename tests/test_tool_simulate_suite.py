"""Tests for the Phase 7 simulate suite: simulate_click / simulate_key / set_input_text."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.simulate_click import InvalidSimulateClickArg, simulate_click
from unity_mcp_py.tools.simulate_key import InvalidSimulateKeyArg, simulate_key
from unity_mcp_py.tools.set_input_text import InvalidSetInputTextArg, set_input_text


# --------------------------------------------------------------------------- click

@pytest.mark.asyncio
async def test_simulate_click_raycast_success(unity_bridge_session, mock_bridge_server) -> None:
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 7}]})
    bridge.expect_request(
        "SimulateClick",
        args_predicate=lambda a: a.get("panelUid") == 66
        and a.get("refKey") == "btnClosePROJECT_BUTTON"
        and a.get("mode") == "raycast",
    ).respond_with({"clicked": True, "mode": "raycast", "nodeFound": True})

    r = await simulate_click(panel_uid=66, ref_key="btnClosePROJECT_BUTTON")
    assert r["clicked"] is True and r["nodeFound"] is True
    assert "write_op_token" in r
    await GUARD.consume([r["write_op_token"]])


@pytest.mark.asyncio
async def test_simulate_click_node_not_found(unity_bridge_session, mock_bridge_server) -> None:
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 1}]})
    bridge.expect_request("SimulateClick").respond_with(
        {"clicked": False, "mode": "raycast", "nodeFound": False, "note": "node not found"}
    )
    r = await simulate_click(panel_uid=66, path="Window/Missing")
    assert r["nodeFound"] is False
    await GUARD.consume([r["write_op_token"]])


@pytest.mark.asyncio
async def test_simulate_click_requires_exactly_one_locator() -> None:
    with pytest.raises(InvalidSimulateClickArg, match="exactly one"):
        await simulate_click(panel_uid=66)  # neither
    with pytest.raises(InvalidSimulateClickArg, match="exactly one"):
        await simulate_click(panel_uid=66, ref_key="a", path="b")  # both


@pytest.mark.parametrize("bad", ["btn'); evil()", "a b", "a/b", "drop;"])
@pytest.mark.asyncio
async def test_simulate_click_rejects_injection_refkey(bad: str) -> None:
    with pytest.raises(InvalidSimulateClickArg):
        await simulate_click(panel_uid=66, ref_key=bad)


@pytest.mark.asyncio
async def test_simulate_click_rejects_bad_mode() -> None:
    with pytest.raises(InvalidSimulateClickArg, match="mode"):
        await simulate_click(panel_uid=66, ref_key="x", mode="device")


# ----------------------------------------------------------------------------- key

@pytest.mark.asyncio
async def test_simulate_key_success(unity_bridge_session, mock_bridge_server) -> None:
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 3}]})
    bridge.expect_request(
        "SimulateKey", args_predicate=lambda a: a.get("actionPath") == "Hud/OpenMap"
    ).respond_with({"triggered": True, "actionPath": "Hud/OpenMap"})

    r = await simulate_key("Hud/OpenMap")
    assert r["triggered"] is True
    await GUARD.consume([r["write_op_token"]])


@pytest.mark.parametrize("bad", ["", "Map Action", "evil'); x(", "a;b"])
@pytest.mark.asyncio
async def test_simulate_key_rejects_bad_action_path(bad: str) -> None:
    with pytest.raises(InvalidSimulateKeyArg):
        await simulate_key(bad)


# ------------------------------------------------------------------------- inputtext

@pytest.mark.asyncio
async def test_set_input_text_success(unity_bridge_session, mock_bridge_server) -> None:
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 9}]})
    bridge.expect_request(
        "SetInputText",
        args_predicate=lambda a: a.get("panelUid") == 66
        and a.get("refKey") == "nameInput"
        and a.get("text") == "Hello 世界",
    ).respond_with({"applied": True, "nodeFound": True})

    r = await set_input_text(panel_uid=66, text="Hello 世界", ref_key="nameInput")
    assert r["applied"] is True
    await GUARD.consume([r["write_op_token"]])


@pytest.mark.asyncio
async def test_set_input_text_requires_one_locator() -> None:
    with pytest.raises(InvalidSetInputTextArg, match="exactly one"):
        await set_input_text(panel_uid=66, text="x")


@pytest.mark.asyncio
async def test_set_input_text_rejects_injection_refkey() -> None:
    with pytest.raises(InvalidSetInputTextArg):
        await set_input_text(panel_uid=66, text="x", ref_key="a'); os.exit(")
