"""Live smoke for Phase 2 (ListUINodes + ResolveLocalization).

Run ONLY when no other MCP client holds the bridge (single-client). In EditMode
both commands return INVALID_STATE (game not running) — that itself is a correct
result. In PlayMode, open a panel first (e.g. via LuaEval) then list its nodes.

Usage:
    python scripts/smoke_phase2_listui.py                 # EditMode: expect INVALID_STATE
    python scripts/smoke_phase2_listui.py --open-uid <PROJECT_PANEL_ID>   # PlayMode: open the target panel then list
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unity_mcp_py.bridge.session import BridgeSession  # noqa: E402


async def run(open_uid: int | None) -> int:
    s = BridgeSession(host="127.0.0.1", port=17873)
    print("handshake:", await s.connect())

    if open_uid is not None:
        try:
            r = await s.send("LuaEval", {"script": f"PROJECT_RUNTIME.global.ui:open({open_uid})"})
            print(f"open({open_uid}):", r)
            await asyncio.sleep(1.0)  # async open lands a few frames later
        except Exception as exc:  # noqa: BLE001
            print("open error:", type(exc).__name__, str(exc)[:160])

    try:
        args = {"panelUid": open_uid} if open_uid is not None else {}
        nodes = await s.send("ListUINodes", args)
        print(f"ListUINodes: gameView={nodes.get('gameViewWidth')}x{nodes.get('gameViewHeight')} "
              f"nodeCount={nodes.get('nodeCount')}")
        for n in (nodes.get("nodes") or [])[:15]:
            print(f"  [{n['index']}] {n['type']:<10} '{n.get('text','') or ''}' "
                  f"click={n['clickable']} vis={n['visible']} "
                  f"rect=({n['screenX']:.0f},{n['screenY']:.0f},{n['screenW']:.0f}x{n['screenH']:.0f}) "
                  f"path={n['path']}")
    except Exception as exc:  # noqa: BLE001
        print("ListUINodes error:", type(exc).__name__, str(exc)[:160])

    # If any node had a textId, resolve a couple of them across languages.
    try:
        loc = await s.send("ResolveLocalization", {"ids": [1, 2, 3], "language": "zh_CN"})
        print("ResolveLocalization sample:", loc)
    except Exception as exc:  # noqa: BLE001
        print("ResolveLocalization error:", type(exc).__name__, str(exc)[:160])

    await s.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open-uid", type=int, default=None)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.open_uid)))


if __name__ == "__main__":
    main()
