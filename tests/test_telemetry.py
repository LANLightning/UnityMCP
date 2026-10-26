"""Tests for the always-on per-tool telemetry sink (Phase 0).

Telemetry is INDEPENDENT of the recorder: it fires on every tool call
regardless of whether a record_start session is active, and it covers tools
the recorder blacklists (e.g. observe_evidence). Each line captures wall-clock,
response payload bytes, a token estimate, and the number of bridge round-trips
the tool issued.
"""
from __future__ import annotations

import json

import pytest

from unity_mcp_py.telemetry import Telemetry, bridge_rtt_tick


def _read_lines(path):
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


@pytest.mark.asyncio
async def test_records_one_line_per_successful_call(tmp_path):
    tel = Telemetry(path=tmp_path / "tel.jsonl")

    async def fn():
        return {"hello": "world"}

    wrapped = tel.wrap("demo_tool", fn)
    result = await wrapped()

    assert result == {"hello": "world"}
    lines = _read_lines(tmp_path / "tel.jsonl")
    assert len(lines) == 1
    entry = lines[0]
    assert entry["tool"] == "demo_tool"
    assert entry["ok"] is True
    assert entry["dur_ms"] >= 0
    assert "ts" in entry


@pytest.mark.asyncio
async def test_token_estimate_tracks_response_bytes(tmp_path):
    tel = Telemetry(path=tmp_path / "tel.jsonl")
    payload = {"blob": "x" * 4000}

    async def fn():
        return payload

    await tel.wrap("big_tool", fn)()

    entry = _read_lines(tmp_path / "tel.jsonl")[0]
    # resp_bytes is the serialized size; token_est is bytes // 4.
    assert entry["resp_bytes"] >= 4000
    assert entry["token_est"] == entry["resp_bytes"] // 4


@pytest.mark.asyncio
async def test_counts_bridge_round_trips(tmp_path):
    tel = Telemetry(path=tmp_path / "tel.jsonl")

    async def fn():
        # Simulate three bridge_call round-trips inside the tool.
        bridge_rtt_tick()
        bridge_rtt_tick()
        bridge_rtt_tick()
        return {}

    await tel.wrap("multi_rtt", fn)()

    entry = _read_lines(tmp_path / "tel.jsonl")[0]
    assert entry["bridge_rtts"] == 3


@pytest.mark.asyncio
async def test_rtt_counter_resets_between_calls(tmp_path):
    tel = Telemetry(path=tmp_path / "tel.jsonl")

    async def one_tick():
        bridge_rtt_tick()
        return {}

    wrapped = tel.wrap("ticker", one_tick)
    await wrapped()
    await wrapped()

    lines = _read_lines(tmp_path / "tel.jsonl")
    assert [ln["bridge_rtts"] for ln in lines] == [1, 1]


@pytest.mark.asyncio
async def test_records_failure_and_reraises(tmp_path):
    tel = Telemetry(path=tmp_path / "tel.jsonl")

    async def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        await tel.wrap("explody", boom)()

    entry = _read_lines(tmp_path / "tel.jsonl")[0]
    assert entry["tool"] == "explody"
    assert entry["ok"] is False
    assert "kaboom" in entry["error"]


@pytest.mark.asyncio
async def test_disabled_telemetry_writes_nothing(tmp_path):
    path = tmp_path / "tel.jsonl"
    tel = Telemetry(path=path, enabled=False)

    async def fn():
        return {"ok": 1}

    result = await tel.wrap("demo", fn)()

    assert result == {"ok": 1}
    assert not path.exists()
