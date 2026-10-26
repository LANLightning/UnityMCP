"""Smoke verification for the 6 new tools against a live Unity bridge.

Run from repo root:
    python scripts/verify_new_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="unity-mcp-verify-")
    os.environ["UNITY_MCP_SESSIONS_DIR"] = tmpdir
    print(f"[setup] UNITY_MCP_SESSIONS_DIR={tmpdir}\n")

    from unity_mcp_py.recorder import RECORDER, REGISTRY
    from unity_mcp_py.tools.get_console_logs import get_console_logs
    from unity_mcp_py.tools.clear_console import clear_console
    from unity_mcp_py.tools.record_start import record_start
    from unity_mcp_py.tools.record_stop import record_stop
    from unity_mcp_py.tools.list_sessions import list_sessions
    from unity_mcp_py.tools.replay import replay
    from unity_mcp_py.tools.query_scene import query_scene
    from unity_mcp_py.tools.ensure_ready import ensure_ready

    # Make replay-able tools discoverable in REGISTRY.
    REGISTRY.register("query_scene", query_scene)
    REGISTRY.register("get_console_logs", get_console_logs)

    # 1. get_console_logs unfiltered
    print("[1] get_console_logs() — first 5 lines, unfiltered")
    res = await get_console_logs(limit=5)
    print(f"    next_seq={res['next_seq']} cursor={res['cursor']} truncated={res['truncated']}")
    for ln in res["lines"][-3:]:
        print(f"    seq={ln['seq']} level={ln['level']} msg={ln['message'][:80]!r}")
    seq_after_pull = res["next_seq"]
    print()

    # 2. filter by level
    print("[2] get_console_logs(level=['Error','Warning','Exception','Assert'])")
    res2 = await get_console_logs(level=["Error", "Warning", "Exception", "Assert"], limit=5)
    print(f"    returned {len(res2['lines'])} non-info lines")
    print()

    # 3. clear_console
    print("[3] clear_console()")
    cc = await clear_console()
    print(f"    previous={cc['previous_cursor']} new={cc['new_cursor']}")
    assert cc["new_cursor"] >= cc["previous_cursor"]
    print()

    # 4. get_console_logs after clear → should be near-empty
    print("[4] get_console_logs() after clear — expect ~0 new lines")
    res4 = await get_console_logs(limit=5)
    print(f"    lines_returned={len(res4['lines'])} cursor={res4['cursor']}")
    print()

    # 5. record_start → call ensure_ready + query_scene → record_stop
    print("[5] record_start('verify-001')")
    info = await record_start(session_id="verify-001", note="smoke")
    print(f"    path={info['path']}")
    assert RECORDER.active

    # Manually mimic middleware-wrapped calls (script bypasses server.py wiring).
    wrapped_ready = RECORDER.wrap("ensure_ready", ensure_ready)
    wrapped_scene = RECORDER.wrap("query_scene", query_scene)
    await wrapped_ready()
    await wrapped_scene()
    stop_info = await record_stop()
    print(f"    stop line_count={stop_info['line_count']}")
    assert stop_info["line_count"] == 2
    print()

    # 6. list_sessions
    print("[6] list_sessions()")
    ls = await list_sessions(limit=10)
    print(f"    {len(ls['sessions'])} session(s) on disk")
    for s in ls["sessions"]:
        print(f"    - {s['session_id']} lines={s['line_count']} note={s['note']!r}")
    print()

    # Inspect the JSONL file directly
    p = Path(info["path"])
    print(f"[7] file dump: {p}")
    for i, raw in enumerate(p.read_text("utf-8").splitlines()):
        obj = json.loads(raw)
        if obj.get("type") == "header":
            print(f"    HEADER: started_at={obj['started_at']} note={obj['note']!r}")
        else:
            print(f"    [{i}] tool={obj['tool']} ok={obj['ok']} dur_ms={obj['dur_ms']} "
                  f"args={obj['args']}")
    print()

    # 8. replay
    print("[8] replay('verify-001')")
    REGISTRY.register("ensure_ready", ensure_ready)
    rep = await replay("verify-001")
    print(f"    total={rep['total']} executed={rep['executed']} "
          f"skipped={rep['skipped']} diffs={len(rep['diffs'])} errors={len(rep['errors'])}")
    if rep["diffs"]:
        for d in rep["diffs"]:
            print(f"    DIFF idx={d['idx']} tool={d['tool']} "
                  f"recorded={d['recorded_keys']} actual={d['actual_keys']}")
    if rep["errors"]:
        for e in rep["errors"]:
            print(f"    ERROR {e}")
    print()

    print("OK — all 6 new tools exercised against live bridge.")


if __name__ == "__main__":
    asyncio.run(main())
