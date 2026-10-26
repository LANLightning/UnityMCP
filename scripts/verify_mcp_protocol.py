"""Real-environment verification: spawn unity-mcp-py as a fresh MCP subprocess
and exercise the optimized tools the way Claude actually invokes them.

This goes one level higher than scripts/bench_tools.py: instead of importing
the tool functions directly, we go through the entire MCP JSON-RPC stack
(stdio framing, initialize handshake, tools/list, tools/call). If any of the
new parameters or response fields broke the FastMCP registration, this script
will surface it.

Prereqs:
    - Unity Editor running on 127.0.0.1:17873 (PlayMode preferred)
    - `pip install -e .` already done
    - UNITY_MCP_PROJECT_PATH should be set (we forward whatever's in the env)
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402


def _payload_bytes(obj) -> int:
    return len(json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))


async def _time_call(session: ClientSession, tool: str, args: dict, n: int = 3) -> dict:
    times: list[float] = []
    last_result = None
    for _ in range(n):
        t0 = time.perf_counter()
        result = await session.call_tool(tool, args)
        times.append((time.perf_counter() - t0) * 1000)
        last_result = result
    # MCP CallToolResult is { content: [...], isError, structuredContent? }
    # Tool's actual dict return is in structuredContent.
    payload = last_result.structuredContent if last_result else None
    return {
        "tool": tool,
        "args": args,
        "n": n,
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "payload_bytes": _payload_bytes(payload) if payload is not None else 0,
        "result_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
        "is_error": bool(last_result.isError) if last_result else False,
    }


async def main() -> int:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        # Forwarded to the subprocess; "" is safe here — this script exercises
        # protocol/schema, not scene ops, so open_scene is never called.
        "UNITY_MCP_PROJECT_PATH": os.environ.get("UNITY_MCP_PROJECT_PATH", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
    }

    params = StdioServerParameters(
        command="python",
        args=["-m", "unity_mcp_py.server"],
        env=env,
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. List tools — confirms the registered set + parameter schemas.
            tools = await session.list_tools()
            tool_index = {t.name: t for t in tools.tools}
            print(f"# Registered tools ({len(tool_index)}):")
            for name in sorted(tool_index):
                schema = tool_index[name].inputSchema or {}
                params_list = sorted((schema.get("properties") or {}).keys())
                print(f"  - {name}({', '.join(params_list)})")
            print()

            # Sanity: confirm new params are exposed at the MCP layer.
            obs_params = sorted(
                (tool_index["observe_evidence"].inputSchema.get("properties") or {}).keys()
            )
            shot_params = sorted(
                (tool_index["capture_screenshot"].inputSchema.get("properties") or {}).keys()
            )
            assert "include_screenshot" in obs_params, \
                f"observe_evidence missing include_screenshot at MCP layer: {obs_params}"
            assert "max_log_lines" in obs_params, \
                f"observe_evidence missing max_log_lines: {obs_params}"
            assert "mode" in shot_params, \
                f"capture_screenshot missing mode: {shot_params}"
            print(f"[ok] observe_evidence params: {obs_params}")
            print(f"[ok] capture_screenshot params: {shot_params}")
            print()

            # 2. Warm bridge — ensure_ready first (registers connection).
            ready = await session.call_tool("ensure_ready", {})
            print("# ensure_ready:")
            print(json.dumps(ready.structuredContent, ensure_ascii=False)[:200])
            print()

            # 3. Probe each scenario.
            print("# Wall-clock + payload via real MCP stdio:")
            results: list[dict] = []

            results.append(await _time_call(
                session, "observe_evidence", {}, n=5))
            results.append(await _time_call(
                session, "observe_evidence",
                {"max_log_lines": 20}, n=5))
            results.append(await _time_call(
                session, "observe_evidence",
                {"include_screenshot": True}, n=3))

            results.append(await _time_call(
                session, "capture_screenshot",
                {"mode": "metadata"}, n=3))
            results.append(await _time_call(
                session, "capture_screenshot",
                {"mode": "file_ref"}, n=3))
            results.append(await _time_call(
                session, "capture_screenshot",
                {"mode": "full"}, n=3))

            # Legacy probes (no new params) — proves backward compatibility.
            results.append(await _time_call(
                session, "query_scene", {}))
            results.append(await _time_call(
                session, "get_console_logs", {"limit": 50}))

            width = 60
            print(f"{'tool(args)':<{width}}  {'med ms':>8}  {'payload B':>10}  err  keys")
            print("-" * (width + 50))
            for r in results:
                tag = f"{r['tool']}({json.dumps(r['args'])})"
                err = "Y" if r["is_error"] else " "
                print(
                    f"{tag:<{width}}  {r['median_ms']:>8.2f}  {r['payload_bytes']:>10}"
                    f"  {err}   {len(r['result_keys'])} keys"
                )

            # Save machine-readable record.
            out = ROOT / "docs" / "superpowers" / "benchmarks" / "2026-05-25-mcp-protocol-verify.json"
            out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\nSaved → {out}")

            # Final invariants.
            obs_default = next(r for r in results if r["tool"] == "observe_evidence" and not r["args"])
            obs_full = next(r for r in results if r["tool"] == "observe_evidence" and r["args"].get("include_screenshot"))
            shot_meta = next(r for r in results if r["tool"] == "capture_screenshot" and r["args"].get("mode") == "metadata")
            shot_full = next(r for r in results if r["tool"] == "capture_screenshot" and r["args"].get("mode") == "full")

            print("\n# Invariants:")
            print(f"  observe_evidence default payload  = {obs_default['payload_bytes']:>10} B (target: < 1.2 MB)")
            print(f"  observe_evidence include_shot     = {obs_full['payload_bytes']:>10} B (target: > 1 MB)")
            print(f"  capture_screenshot mode=metadata  = {shot_meta['payload_bytes']:>10} B (target: < 1 KB)")
            print(f"  capture_screenshot mode=full      = {shot_full['payload_bytes']:>10} B (target: > 1 MB)")

            ok = (
                obs_default["payload_bytes"] < 1_200_000
                and obs_full["payload_bytes"] > 1_000_000
                and shot_meta["payload_bytes"] < 1024
                and shot_full["payload_bytes"] > 1_000_000
            )
            if not ok:
                print("\n[FAIL] FAIL: an invariant did not hold.")
                return 1
            print("\n[ok] All invariants hold. Real MCP-protocol verification passed.")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
