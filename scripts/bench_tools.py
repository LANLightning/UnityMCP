"""Reproducible micro-benchmark against live Unity bridge.

Usage:
    python scripts/bench_tools.py

Requires:
    - Unity Editor running with bridge on 127.0.0.1:17873
    - UnityMCP installed in editable mode (pip install -e .)

Output:
    Human-readable table to stdout + machine-readable JSON to stderr.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from unity_mcp_py.bridge.session import bridge_call, get_session  # noqa: E402
from unity_mcp_py.tools.capture_screenshot import capture_screenshot  # noqa: E402
from unity_mcp_py.tools.observe_evidence import observe_evidence  # noqa: E402
from unity_mcp_py.tools.get_console_logs import get_console_logs  # noqa: E402
from unity_mcp_py.tools.query_scene import query_scene  # noqa: E402


async def time_call(label: str, coro_factory, samples: int = 5) -> dict:
    times: list[float] = []
    payload_bytes = 0
    last = None
    for _ in range(samples):
        t0 = time.perf_counter()
        last = await coro_factory()
        dt = (time.perf_counter() - t0) * 1000
        times.append(dt)
    if isinstance(last, dict):
        payload_bytes = len(json.dumps(last, ensure_ascii=False, default=str).encode("utf-8"))
    return {
        "label": label,
        "n": samples,
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "payload_bytes": payload_bytes,
    }


async def main() -> None:
    await get_session().connect()
    results: list[dict] = []

    # Raw bridge probes (high sample count for stable median).
    results.append(await time_call(
        "bridge.GetPlayState", lambda: bridge_call("GetPlayState", {}), samples=10))
    results.append(await time_call(
        "bridge.TailEditorLog(maxLines=1)",
        lambda: bridge_call("TailEditorLog", {"maxLines": 1}), samples=10))
    results.append(await time_call(
        "bridge.TailEditorLog(maxLines=200)",
        lambda: bridge_call("TailEditorLog", {"maxLines": 200})))
    results.append(await time_call(
        "bridge.GetSceneState", lambda: bridge_call("GetSceneState", {})))
    results.append(await time_call(
        "bridge.CaptureScreenshot", lambda: bridge_call("CaptureScreenshot", {}), samples=3))

    # Tool-level (the surface LLM sees).
    results.append(await time_call(
        "tool.get_console_logs(limit=50)", lambda: get_console_logs(limit=50)))
    results.append(await time_call(
        "tool.query_scene", lambda: query_scene()))
    results.append(await time_call(
        "tool.capture_screenshot", lambda: capture_screenshot(), samples=3))
    results.append(await time_call(
        "tool.observe_evidence (default)", lambda: observe_evidence(), samples=3))

    # New-API probes; skipped if signatures lack these params (baseline run).
    try:
        results.append(await time_call(
            "tool.observe_evidence(include_screenshot=False)",
            lambda: observe_evidence(include_screenshot=False), samples=5))
    except TypeError:
        pass
    try:
        results.append(await time_call(
            "tool.capture_screenshot(mode='metadata')",
            lambda: capture_screenshot(mode="metadata"), samples=5))
    except TypeError:
        pass

    width = 48
    print(f"{'Label':<{width}}  {'n':>3}  {'med ms':>8}  {'min ms':>8}  {'max ms':>8}  {'payload B':>10}")
    print("-" * (width + 50))
    for r in results:
        print(f"{r['label']:<{width}}  {r['n']:>3}  {r['median_ms']:>8.2f}  "
              f"{r['min_ms']:>8.2f}  {r['max_ms']:>8.2f}  {r['payload_bytes']:>10}")
    sys.stderr.write(json.dumps(results, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
