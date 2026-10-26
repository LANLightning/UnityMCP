"""Live smoke for Phase 6 OpenPanel (async open + verify).

Run ONLY when no other MCP client holds the bridge (single-client). PlayMode-only.

Usage:
    python scripts/smoke_phase3_openpanel.py --uid <PROJECT_PANEL_ID>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unity_mcp_py.bridge.session import BridgeSession  # noqa: E402


async def run(uid: int, timeout_ms: int) -> int:
    s = BridgeSession(host="127.0.0.1", port=17873)
    print("handshake:", await s.connect())

    # Close it first (best-effort) so we observe a real open transition.
    try:
        await s.send("LuaEval", {"script": f"PROJECT_RUNTIME.global.ui:PROJECT_CLOSE_IMMEDIATE({uid}, true)"})
        await asyncio.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass

    try:
        r = await s.send("OpenPanel", {"uid": uid, "timeoutMs": timeout_ms, "verify": True})
        print("OpenPanel result:")
        for k in ("uid", "opened", "visible", "isOpen", "topPanelUid",
                  "timedOut", "refused", "elapsedMs"):
            print(f"  {k} = {r.get(k)}")
        if r.get("visible"):
            print("  => SUCCESS: panel opened and became visible")
        elif r.get("refused"):
            print("  => REFUSED: PROJECT_CAN_OPEN silently blocked it (locked/switch off/platform)")
        elif r.get("timedOut"):
            print("  => TIMED OUT: open accepted but view never showed in time")
    except Exception as exc:  # noqa: BLE001
        print("OpenPanel error:", type(exc).__name__, str(exc)[:200])

    await s.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", type=int, required=True)
    ap.add_argument("--timeout-ms", type=int, default=5000)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.uid, args.timeout_ms)))


if __name__ == "__main__":
    main()
