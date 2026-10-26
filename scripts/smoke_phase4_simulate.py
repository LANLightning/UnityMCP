"""Live smoke for Phase 7 simulate suite (click / key / set-text).

Run ONLY when no other MCP client holds the bridge. PlayMode-only.
Demonstrates the close-the-loop: open the target panel → click its close button by refKey.

Usage:
    python scripts/smoke_phase4_simulate.py --uid <PROJECT_PANEL_ID> --ref <PROJECT_NODE_REF>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unity_mcp_py.bridge.session import BridgeSession  # noqa: E402


async def run(uid: int, ref: str, action: str | None) -> int:
    s = BridgeSession(host="127.0.0.1", port=17873)
    print("handshake:", await s.connect())

    # 1) ensure the panel is open
    r = await s.send("OpenPanel", {"uid": uid, "timeoutMs": 5000, "verify": True})
    print(f"OpenPanel({uid}): visible={r.get('visible')} elapsedMs={r.get('elapsedMs')}")

    # 2) click a node by refKey (raycast — self-validating)
    if ref:
        c = await s.send("SimulateClick", {"panelUid": uid, "refKey": ref, "mode": "raycast"})
        print(f"SimulateClick(refKey={ref}): clicked={c.get('clicked')} "
              f"nodeFound={c.get('nodeFound')} note={c.get('note')}")
        await asyncio.sleep(0.5)
        # did it close? (close button → panel no longer visible)
        v = await s.send("LuaEval", {"script": f"return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE({uid}) == true"})
        print(f"  panel still visible after click: {v.get('raw')}")

    # 3) optional: trigger an action key
    if action:
        k = await s.send("SimulateKey", {"actionPath": action})
        print(f"SimulateKey({action}): triggered={k.get('triggered')}")

    await s.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", type=int, required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--action", default=None)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.uid, args.ref, args.action)))


if __name__ == "__main__":
    main()
