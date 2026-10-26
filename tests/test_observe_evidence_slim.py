"""Feature 3 — observe_evidence default-slim reshape.

Default (verbose=False): logs filtered to Warning/Error/Exception/Assert,
repeated lines run-length-collapsed (repeat:N), message/stack capped ~2KB,
scene_objects collapsed to {name,active,child_count,component_count}, plus a
one-line verdict summary. verbose=True returns the legacy full shape.
"""
from __future__ import annotations

import pytest

from unity_mcp_py.tools.observe_evidence import (
    observe_evidence,
    _slim_log_delta,
    _slim_scene_objects,
    _verdict_line,
    _DEFAULT_SLIM_LEVELS,
    _MAX_FIELD_CHARS,
)


# ---- helper-level unit tests ------------------------------------------------

def _ln(n, lvl, msg, stack=""):
    return {"lineNumber": n, "logType": lvl, "message": msg, "stackTrace": stack}


def test_slim_log_delta_drops_info_levels_by_default():
    raw = [_ln(1, "Log", "chatty"), _ln(2, "Warning", "heads up"),
           _ln(3, "Error", "boom")]
    out = _slim_log_delta(raw, levels=_DEFAULT_SLIM_LEVELS)
    conditions = [e["condition"] for e in out]
    assert "chatty" not in conditions
    assert "heads up" in conditions
    assert "boom" in conditions


def test_slim_log_delta_collapses_consecutive_duplicates():
    raw = [_ln(1, "Error", "same"), _ln(2, "Error", "same"),
           _ln(3, "Error", "same"), _ln(4, "Error", "other")]
    out = _slim_log_delta(raw, levels=_DEFAULT_SLIM_LEVELS)
    assert len(out) == 2
    assert out[0]["condition"] == "same"
    assert out[0]["repeat"] == 3
    assert out[1]["condition"] == "other"
    assert out[1]["repeat"] == 1


def test_slim_log_delta_caps_long_fields():
    raw = [_ln(1, "Error", "x" * (_MAX_FIELD_CHARS + 500),
               "s" * (_MAX_FIELD_CHARS + 500))]
    out = _slim_log_delta(raw, levels=_DEFAULT_SLIM_LEVELS)
    assert len(out[0]["condition"]) <= _MAX_FIELD_CHARS
    assert len(out[0]["stack_trace"]) <= _MAX_FIELD_CHARS


def test_slim_scene_objects_collapses_to_counts():
    raw = [{"name": "Cube", "active": True,
            "components": ["Transform", "Renderer"],
            "children": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}]
    out = _slim_scene_objects(raw)
    assert out == [{"name": "Cube", "active": True,
                    "child_count": 3, "component_count": 2}]


def test_verdict_line_summarizes_counts():
    delta = [{"condition": "boom", "log_type": "Error", "repeat": 1},
             {"condition": "warn", "log_type": "Warning", "repeat": 2}]
    line = _verdict_line(delta, play_state="PlayMode", root_count=5)
    assert "errors:1" in line
    assert "warnings:1" in line
    assert "play:PlayMode" in line
    assert "roots:5" in line


# ---- tool-level integration tests -------------------------------------------

@pytest.mark.asyncio
async def test_observe_evidence_default_is_slim(unity_bridge_session, mock_bridge_server):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        _ln(10, "Log", "noise"),
        _ln(11, "Error", "real failure"),
    ]})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": [
        {"name": "Root", "active": True, "components": ["Transform"], "children": []}
    ]})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})

    result = await observe_evidence()

    # Info-level "noise" filtered out by default; Error kept.
    conditions = [e["condition"] for e in result["log_delta"]]
    assert conditions == ["real failure"]
    # scene_objects collapsed.
    assert result["scene_objects"][0] == {
        "name": "Root", "active": True, "child_count": 0, "component_count": 1}
    # verdict present.
    assert "errors:1" in result["verdict"]
    assert result["verbose"] is False


@pytest.mark.asyncio
async def test_observe_evidence_verbose_keeps_full_shape(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 100, "logType": "Log",
         "message": "PlayMode entered", "stackTrace": ""}
    ]})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": [
        {"name": "Cube", "instanceId": 1, "active": True,
         "components": ["Transform"], "childCount": 0}
    ]})
    bridge.expect_request("GetPlayState").respond_with({"state": "PlayMode"})

    result = await observe_evidence(verbose=True)

    # verbose keeps the Log-level line (no severity filter) and the raw scene
    # object (full rootObjects passthrough).
    assert result["log_delta"][0]["condition"] == "PlayMode entered"
    assert result["scene_objects"][0]["name"] == "Cube"
    assert result["scene_objects"][0]["components"] == ["Transform"]
    assert result["verbose"] is True


@pytest.mark.asyncio
async def test_observe_evidence_explicit_levels_override_slim_default(
    unity_bridge_session, mock_bridge_server
):
    """Passing log_levels lets a caller widen the slim filter (e.g. include Log)."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [
        {"lineNumber": 1, "logType": "Log", "message": "wanted", "stackTrace": ""}
    ]})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": []})
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    result = await observe_evidence(log_levels=["Log"])

    assert [e["condition"] for e in result["log_delta"]] == ["wanted"]
