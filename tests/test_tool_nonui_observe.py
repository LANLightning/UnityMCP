"""Tests for Phase 8 non-UI observe/operate tools."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.observe_camera import InvalidObserveCameraArg, observe_camera
from unity_mcp_py.tools.list_entities import list_entities
from unity_mcp_py.tools.observe_entity import InvalidObserveEntityArg, observe_entity
from unity_mcp_py.tools.list_effects import list_effects
from unity_mcp_py.tools.observe_effect import observe_effect
from unity_mcp_py.tools.observe_window import InvalidObserveWindowArg, observe_window
from unity_mcp_py.tools.operate_camera import InvalidOperateCameraArg, operate_camera


# ----------------------------------------------------------------- observe_camera
@pytest.mark.asyncio
async def test_observe_camera_passthrough(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request("ObserveCamera").respond_with(
        {"ok": True, "data": {"fov": 60.0, "activeModeName": "PlayerCamera"}}
    )
    r = await observe_camera()
    assert r["ok"] is True and r["data"]["activeModeName"] == "PlayerCamera"
    assert "write_op_token" not in r


@pytest.mark.asyncio
async def test_observe_camera_target_pos(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request(
        "ObserveCamera", args_predicate=lambda a: a.get("targetPos") == [1.0, 2.0, 3.0]
    ).respond_with({"ok": True, "data": {"inViewport": True}})
    r = await observe_camera(target_pos=[1, 2, 3])
    assert r["data"]["inViewport"] is True


@pytest.mark.asyncio
async def test_observe_camera_rejects_bad_target() -> None:
    with pytest.raises(InvalidObserveCameraArg):
        await observe_camera(target_pos=[1, 2])


# ----------------------------------------------------------------- list_entities
@pytest.mark.asyncio
async def test_list_entities_summary(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request(
        "ListEntities", args_predicate=lambda a: a.get("summary") is True
    ).respond_with({"ok": True, "data": {"summary": {"Player": 1, "Puppet": 3}}})
    r = await list_entities(summary=True)
    assert r["data"]["summary"]["Puppet"] == 3


@pytest.mark.asyncio
async def test_list_entities_filters_and_cap(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request(
        "ListEntities",
        args_predicate=lambda a: a.get("actorType") == "Puppet" and a.get("maxEntities") == 2000,
    ).respond_with({"ok": True, "data": {"count": 0, "entities": []}})
    await list_entities(actor_type="Puppet", max_entities=99999)


# ----------------------------------------------------------------- observe_entity
@pytest.mark.asyncio
async def test_observe_entity_by_id(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request(
        "ObserveEntity", args_predicate=lambda a: a.get("id") == "ent_42"
    ).respond_with({"ok": True, "data": {"found": True, "hp": 100}})
    r = await observe_entity(entity_id="ent_42")
    assert r["data"]["found"] is True


@pytest.mark.asyncio
async def test_observe_entity_requires_exactly_one() -> None:
    with pytest.raises(InvalidObserveEntityArg):
        await observe_entity()
    with pytest.raises(InvalidObserveEntityArg):
        await observe_entity(entity_id="a", actor_id=1)


# ----------------------------------------------------------------- effects
@pytest.mark.asyncio
async def test_list_effects(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request("ListEffects").respond_with(
        {"ok": True, "data": {"count": 1, "effects": [{"generatorId": 0, "effectId": 5}]}}
    )
    r = await list_effects()
    assert r["data"]["count"] == 1


@pytest.mark.asyncio
async def test_observe_effect(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request(
        "ObserveEffect", args_predicate=lambda a: a.get("generatorId") == 0 and a.get("effectId") == 5
    ).respond_with({"ok": True, "data": {"exists": True, "playing": True, "particleCount": 12}})
    r = await observe_effect(0, 5)
    assert r["data"]["particleCount"] == 12


# ----------------------------------------------------------------- observe_window
@pytest.mark.asyncio
async def test_observe_window_passthrough(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request(
        "ObserveWindow",
        args_predicate=lambda a: a.get("luaExpr") == "#PROJECT_RUNTIME.global.PROJECT_CAMERA_SHAKE_SERVICE.activeShakes"
        and a.get("intervalMs") == 50,
    ).respond_with({"everTrue": True, "firstTrueMs": 120.0, "lastTrueMs": 400.0,
                    "peak": 1.0, "sampleCount": 40, "elapsedMs": 2000.0, "samples": []})
    r = await observe_window("#PROJECT_RUNTIME.global.PROJECT_CAMERA_SHAKE_SERVICE.activeShakes")
    assert r["everTrue"] is True


@pytest.mark.parametrize("bad", [
    "x = 1",                # assignment
    "do return 1 end",      # statement keywords
    "os.execute('x')",      # banned identifier
    "a(); b()",             # semicolon
    "function() end",       # function
    "",                     # empty
])
@pytest.mark.asyncio
async def test_observe_window_rejects_impure(bad: str) -> None:
    with pytest.raises(InvalidObserveWindowArg):
        await observe_window(bad)


@pytest.mark.parametrize("ok_expr", [
    "PROJECT_RUNTIME.global.PROJECT_EFFECT_SERVICE:IsEffectPlaying(0, 5)",
    "a == b",
    "x ~= nil",
    "n >= 3",
    "#PROJECT_RUNTIME.global.PROJECT_CAMERA_SHAKE_SERVICE.activeShakes > 0",
])
@pytest.mark.asyncio
async def test_observe_window_accepts_pure(unity_bridge_session, mock_bridge_server, ok_expr: str) -> None:
    mock_bridge_server.expect_request("ObserveWindow").respond_with(
        {"everTrue": False, "firstTrueMs": -1, "lastTrueMs": -1, "peak": 0,
         "sampleCount": 0, "elapsedMs": 100, "samples": []}
    )
    r = await observe_window(ok_expr, duration_ms=100)
    assert "everTrue" in r


# ----------------------------------------------------------------- operate_camera
@pytest.mark.asyncio
async def test_operate_camera_zoom_issues_token(unity_bridge_session, mock_bridge_server) -> None:
    mock_bridge_server.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 3}]})
    mock_bridge_server.expect_request(
        "OperateCamera", args_predicate=lambda a: a.get("action") == "zoom" and a.get("value") == 0.5
    ).respond_with({"action": "zoom", "applied": True,
                    "before": {"PROJECT_CURRENT_ZOOM": 1.0}, "after": {"PROJECT_CURRENT_ZOOM": 0.5}})
    r = await operate_camera("zoom", value=0.5)
    assert r["after"]["PROJECT_CURRENT_ZOOM"] == 0.5
    assert "write_op_token" in r
    await GUARD.consume([r["write_op_token"]])


@pytest.mark.asyncio
async def test_operate_camera_rejects_unknown_action() -> None:
    with pytest.raises(InvalidOperateCameraArg):
        await operate_camera("explode")


@pytest.mark.asyncio
async def test_operate_camera_zoom_requires_value() -> None:
    with pytest.raises(InvalidOperateCameraArg, match="requires value"):
        await operate_camera("zoom")


@pytest.mark.asyncio
async def test_operate_camera_focus_requires_actor() -> None:
    with pytest.raises(InvalidOperateCameraArg, match="requires actor_id"):
        await operate_camera("focus")
