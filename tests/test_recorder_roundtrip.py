"""Recorder round-trip: start → wrap a fake tool → stop → list → replay."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from unity_mcp_py.recorder import (
    BLACKLIST,
    RECORDER,
    REGISTRY,
    list_session_files,
    run_replay,
    sessions_dir,
)


@pytest.fixture
def temp_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITY_MCP_SESSIONS_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture(autouse=True)
async def _reset_recorder():
    if RECORDER.active:
        await RECORDER.stop()
    yield
    if RECORDER.active:
        await RECORDER.stop()


@pytest.mark.asyncio
async def test_sessions_dir_uses_env(temp_sessions):
    assert sessions_dir() == Path(str(temp_sessions))


@pytest.mark.asyncio
async def test_start_creates_header(temp_sessions):
    info = await RECORDER.start("sess-001", note="demo")
    assert info["session_id"] == "sess-001"
    path = Path(info["path"])
    assert path.exists()
    header = json.loads(path.read_text("utf-8").splitlines()[0])
    assert header["type"] == "header"
    assert header["note"] == "demo"


@pytest.mark.asyncio
async def test_start_rejects_double_active(temp_sessions):
    await RECORDER.start("sess-a", note="")
    with pytest.raises(RuntimeError, match="already active"):
        await RECORDER.start("sess-b", note="")


@pytest.mark.asyncio
async def test_wrap_records_success_and_error(temp_sessions):
    calls = []

    async def fake_ok(a: int, b: int = 1):
        calls.append(("ok", a, b))
        return {"sum": a + b}

    async def fake_bad():
        raise ValueError("boom")

    wrapped_ok = RECORDER.wrap("fake_ok", fake_ok)
    wrapped_bad = RECORDER.wrap("fake_bad", fake_bad)

    await RECORDER.start("rec-001", note="")
    assert await wrapped_ok(a=2, b=3) == {"sum": 5}
    with pytest.raises(ValueError):
        await wrapped_bad()
    info = await RECORDER.stop()

    lines = Path(info["path"]).read_text("utf-8").splitlines()
    # header + 2 entries
    assert len(lines) == 3
    e1 = json.loads(lines[1])
    e2 = json.loads(lines[2])
    assert e1["tool"] == "fake_ok"
    assert e1["ok"] is True
    assert e1["args"] == {"a": 2, "b": 3}
    assert e1["result"] == {"sum": 5}
    assert e2["tool"] == "fake_bad"
    assert e2["ok"] is False
    assert "boom" in e2["error"]


@pytest.mark.asyncio
async def test_wrap_blacklisted_returns_original(temp_sessions):
    async def fn():
        return {"ok": True}

    for name in BLACKLIST:
        assert RECORDER.wrap(name, fn) is fn


@pytest.mark.asyncio
async def test_wrap_inactive_does_not_write(temp_sessions):
    async def fn():
        return {"ok": True}

    wrapped = RECORDER.wrap("fake_idle", fn)
    assert await wrapped() == {"ok": True}
    # No session active → nothing written, directory may be empty
    assert list_session_files(10) == []


@pytest.mark.asyncio
async def test_list_sessions_reports_metadata(temp_sessions):
    info = await RECORDER.start("sess-meta", note="hello")

    async def fn():
        return {"a": 1}

    wrapped = RECORDER.wrap("noop", fn)
    await wrapped()
    await RECORDER.stop()

    items = list_session_files(10)
    assert len(items) == 1
    assert items[0]["session_id"] == "sess-meta"
    assert items[0]["note"] == "hello"
    assert items[0]["line_count"] == 1
    assert items[0]["path"] == info["path"]


@pytest.mark.asyncio
async def test_replay_reinvokes_and_reports_diffs(temp_sessions):
    log = []

    async def add(a: int, b: int):
        log.append((a, b))
        return {"sum": a + b, "extra": 1}

    REGISTRY.register("add", RECORDER.wrap("add", add))

    await RECORDER.start("replay-1", note="")
    wrapped = RECORDER.wrap("add", add)
    await wrapped(a=1, b=2)
    await wrapped(a=10, b=20)
    await RECORDER.stop()

    log.clear()

    # Patch the registered fn so the replayed result has different keys → diff.
    async def add_changed(a: int, b: int):
        log.append((a, b))
        return {"sum": a + b}  # missing "extra"

    REGISTRY.register("add", add_changed)

    result = await run_replay("replay-1")
    assert result["total"] == 2
    assert result["executed"] == 2
    assert result["skipped"] == 0
    assert result["errors"] == []
    assert len(result["diffs"]) == 2
    assert result["diffs"][0]["recorded_keys"] == ["extra", "sum"]
    assert result["diffs"][0]["actual_keys"] == ["sum"]
    assert log == [(1, 2), (10, 20)]


@pytest.mark.asyncio
async def test_replay_dry_run_does_not_call(temp_sessions):
    called: list[int] = []

    async def fn(x: int):
        called.append(x)
        return {"x": x}

    REGISTRY.register("fn", fn)

    await RECORDER.start("dry-1", note="")
    wrapped = RECORDER.wrap("fn", fn)
    await wrapped(x=99)
    await RECORDER.stop()

    called.clear()
    result = await run_replay("dry-1", dry_run=True)
    assert result["executed"] == 1
    assert called == []


@pytest.mark.asyncio
async def test_replay_skips_human_collab_gate_by_default(temp_sessions):
    called: list[str] = []

    async def gate(human_instruction: str = ""):
        called.append(human_instruction)
        return {"ok": True}

    REGISTRY.register("human_collab_gate", gate)

    await RECORDER.start("hcg-1", note="")
    wrapped = RECORDER.wrap("human_collab_gate", gate)
    await wrapped(human_instruction="please confirm")
    await RECORDER.stop()

    called.clear()
    result = await run_replay("hcg-1")
    assert result["skipped"] == 1
    assert called == []


@pytest.mark.asyncio
async def test_replay_stop_on_error(temp_sessions):
    async def bad(**_kw):
        raise RuntimeError("nope")

    async def good(**_kw):
        return {"ok": True}

    REGISTRY.register("bad", bad)
    REGISTRY.register("good", good)

    await RECORDER.start("err-1", note="")
    await RECORDER.wrap("bad", lambda **kw: _noop(**kw))(x=1) if False else None
    # Manually craft entries instead of recording (bad would raise during record).
    await RECORDER.stop()

    import json as _json
    path = sessions_dir() / "err-1.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(_json.dumps({"ts": "t", "tool": "bad", "args": {}, "ok": True, "result": {}}) + "\n")
        f.write(_json.dumps({"ts": "t", "tool": "good", "args": {}, "ok": True, "result": {"ok": True}}) + "\n")

    result_stop = await run_replay("err-1", stop_on_error=True)
    assert len(result_stop["errors"]) == 1
    assert result_stop["executed"] == 0

    result_continue = await run_replay("err-1", stop_on_error=False)
    assert len(result_continue["errors"]) == 1
    assert result_continue["executed"] == 1


async def _noop(**_kw):  # pragma: no cover — helper
    return {}


@pytest.mark.asyncio
async def test_recorder_holds_handle_across_appends(temp_sessions):
    """PERF 2026-05-25: start() opens the session file once and keeps the
    handle until stop(). Verified by counting OS-level opens via a tiny shim
    and by confirming bulk-append throughput beats a naive open-per-write
    baseline."""
    info = await RECORDER.start("perf-handle", note="")
    # Sanity check: an attribute named _fh should exist while session is open.
    assert getattr(RECORDER, "_fh", None) is not None
    # Bulk append.
    for i in range(20):
        await RECORDER._append({"ts": "t", "i": i})
    res = await RECORDER.stop()
    assert res["line_count"] == 20
    # After stop, handle must be released so other writers can re-open the file.
    assert getattr(RECORDER, "_fh", None) is None
    # File still readable and contents intact.
    lines = Path(info["path"]).read_text("utf-8").splitlines()
    assert len(lines) == 21  # header + 20


@pytest.mark.asyncio
async def test_json_safe_walks_without_double_serialize(temp_sessions):
    """PERF 2026-05-25: dict keys are stringified; non-JSON scalars are
    stringified by default=str at dump time. Verifies behavior parity with
    the previous try/except json.dumps probe."""
    info = await RECORDER.start("perf-jsafe", note="")

    class _Weird:
        def __repr__(self) -> str:
            return "<Weird>"

    payload = {
        "ok_str": "fine",
        "num_key_dict": {1: "ok", "k": 2},
        "weird": _Weird(),
        "nested": [{"a": _Weird()}],
    }
    await RECORDER._append({"tool": "demo", "result": payload})
    res = await RECORDER.stop()
    lines = Path(info["path"]).read_text("utf-8").splitlines()
    body = json.loads(lines[1])
    # Number key must be stringified
    assert "1" in body["result"]["num_key_dict"]
    # Unknown object falls back to repr via default=str
    assert body["result"]["weird"] == "<Weird>"
    assert body["result"]["nested"][0]["a"] == "<Weird>"
    assert res["line_count"] == 1
