"""Integration: telemetry is wired into the live bridge + tool path.

Proves two seams:
  1. BridgeSession.send ticks the bridge-RTT contextvar, so a tool that makes N
     bridge calls records bridge_rtts == N.
  2. observe_evidence — which the recorder BLACKLISTS — is still measured by
     telemetry (the whole point of an always-on sink).
"""
from __future__ import annotations

import json

import pytest

from unity_mcp_py.telemetry import Telemetry


def _read_lines(path):
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


@pytest.mark.asyncio
async def test_observe_evidence_records_bridge_rtts(
    tmp_path, unity_bridge_session, mock_bridge_server
):
    """observe_evidence issues 3 bridge calls (TailEditorLog, GetSceneState,
    GetPlayState) with no screenshot; telemetry must count all 3."""
    from unity_mcp_py.tools.observe_evidence import observe_evidence

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": []})
    bridge.expect_request("GetSceneState").respond_with({"rootObjects": []})
    bridge.expect_request("GetPlayState").respond_with({"state": "EditMode"})

    tel = Telemetry(path=tmp_path / "tel.jsonl")
    await tel.wrap("observe_evidence", observe_evidence)()

    entry = _read_lines(tmp_path / "tel.jsonl")[0]
    assert entry["tool"] == "observe_evidence"
    assert entry["bridge_rtts"] == 3
    assert entry["ok"] is True
