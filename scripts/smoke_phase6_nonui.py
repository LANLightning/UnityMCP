"""Live smoke for Phase 8 non-UI observe/operate (camera/entity/effect).

Run ONLY when no other MCP client holds the bridge. PlayMode-only.

Usage:
    python scripts/smoke_phase6_nonui.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unity_mcp_py.bridge.session import BridgeSession  # noqa: E402


def _show(label, r):
    print(f"--- {label} ---")
    print(json.dumps(r, ensure_ascii=False, indent=2)[:1200])


async def run() -> int:
    s = BridgeSession(host="127.0.0.1", port=17873)
    print("handshake:", await s.connect())

    _show("ObserveCamera", await s.send("ObserveCamera", {}))
    _show("ListEntities(summary)", await s.send("ListEntities", {"summary": True}))
    _show("ListEntities(first rows)", await s.send("ListEntities", {"maxEntities": 5}))

    # If there's a player entity, observe it.
    ents = await s.send("ListEntities", {"actorType": "Player", "maxEntities": 1})
    rows = (ents.get("data") or {}).get("entities") or []
    if rows:
        eid = rows[0]["id"]
        _show(f"ObserveEntity({eid})", await s.send("ObserveEntity", {"id": eid}))

    _show("ListEffects", await s.send("ListEffects", {}))

    # operate_camera zoom with before/after evidence.
    _show("OperateCamera(zoom 0.5)", await s.send("OperateCamera", {"action": "zoom", "value": 0.5}))

    # observe_window: watch camera shake count for 2s.
    _show("ObserveWindow(shakes)", await s.send(
        "ObserveWindow",
        {"luaExpr": "#PROJECT_RUNTIME.global.PROJECT_CAMERA_SHAKE_SERVICE.activeShakes", "intervalMs": 50, "durationMs": 1500},
    ))

    await s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
