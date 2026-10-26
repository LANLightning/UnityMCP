"""Tests for WriteOpGuard token store."""
import asyncio
import time

import pytest

from unity_mcp_py.state.write_op_guard import WriteOpGuard, TOKEN_TTL_SECONDS


@pytest.mark.asyncio
async def test_issue_and_consume_roundtrip() -> None:
    g = WriteOpGuard()
    ctx = await g.issue("enter_playmode", "{}", line_number_before=100)
    assert ctx.token
    assert ctx.tool_name == "enter_playmode"
    res = await g.consume([ctx.token])
    assert res.consumed == [ctx.token]
    assert res.expired == []
    assert res.unknown == []


@pytest.mark.asyncio
async def test_consume_twice_marks_second_unknown() -> None:
    g = WriteOpGuard()
    ctx = await g.issue("open_scene", '{"scene_path":"X"}', 200)
    await g.consume([ctx.token])
    res2 = await g.consume([ctx.token])
    assert res2.consumed == []
    assert res2.unknown == [ctx.token]


@pytest.mark.asyncio
async def test_expired_token_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    g = WriteOpGuard()
    ctx = await g.issue("run_method", "{}", 300)
    # Force expiry by rewinding expires_at
    ctx.expires_at = time.time() - 1
    res = await g.consume([ctx.token])
    assert res.expired == [ctx.token]
    assert res.consumed == []


@pytest.mark.asyncio
async def test_list_pending_returns_outstanding() -> None:
    g = WriteOpGuard()
    a = await g.issue("a_tool", "{}", 1)
    b = await g.issue("b_tool", "{}", 2)
    pending = await g.list_pending()
    assert {p.token for p in pending} == {a.token, b.token}


@pytest.mark.asyncio
async def test_gc_once_clears_expired_only() -> None:
    g = WriteOpGuard()
    live = await g.issue("live", "{}", 1)
    stale = await g.issue("stale", "{}", 2)
    stale.expires_at = time.time() - 1
    reaped = await g.gc_once()
    assert reaped == 1
    pending = await g.list_pending()
    assert {p.token for p in pending} == {live.token}


@pytest.mark.asyncio
async def test_concurrent_consume_no_double() -> None:
    g = WriteOpGuard()
    ctx = await g.issue("t", "{}", 0)
    a, b = await asyncio.gather(g.consume([ctx.token]), g.consume([ctx.token]))
    consumed_count = len(a.consumed) + len(b.consumed)
    unknown_count = len(a.unknown) + len(b.unknown)
    assert consumed_count == 1
    assert unknown_count == 1
