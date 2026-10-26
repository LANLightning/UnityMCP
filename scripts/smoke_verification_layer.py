"""Live smoke test for the Phase 4 no-human UI verification layer.

Run this ONLY when no other MCP client holds the bridge (the bridge is
single-client). It connects directly, then exercises the new 1.4.0 commands:
  - StartVerificationSession  (shows the Editor hint banner; EditMode-safe)
  - LuaEval                   (EditMode → INVALID_STATE lua_vm_not_running;
                               PlayMode → runs real Lua, e.g. open a panel)
  - EndVerificationSession

Usage:
    # Make sure the live MCP server is NOT connected (it holds the 1 client slot).
    python scripts/smoke_verification_layer.py
    # In PlayMode, also try opening a panel:
    python scripts/smoke_verification_layer.py --open-uid <PROJECT_PANEL_ID>

The point of --open-uid is to prove the universal LuaEval seam end to end:
  LuaEval("PROJECT_RUNTIME.global.ui:open(<uid>); return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE(<uid>)")
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unity_mcp_py.bridge.session import BridgeSession  # noqa: E402


async def run(open_uid: int | None) -> int:
    session = BridgeSession(host="127.0.0.1", port=17873)
    hs = await session.connect()
    print("handshake:", hs)

    start = await session.send(
        "StartVerificationSession",
        {"note": "smoke", "pauseSilenceMs": 1500, "abortOnHigh": True},
    )
    print("start:", start)
    session_id = start.get("sessionId")

    # LuaEval: in EditMode this surfaces lua_vm_not_running; in PlayMode it runs.
    try:
        le = await session.send("LuaEval", {"script": "return 1 + 1"})
        print("luaeval(1+1):", le)
    except Exception as exc:  # noqa: BLE001
        print("luaeval error (expected in EditMode):", type(exc).__name__, str(exc)[:160])

    if open_uid is not None:
        script = (
            f"PROJECT_RUNTIME.global.ui:open({open_uid}); "
            f"return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE({open_uid})"
        )
        try:
            opened = await session.send("LuaEval", {"script": script})
            print(f"luaeval(open {open_uid}):", opened)
            # Re-check after a moment (open is async; visibility lands a few frames later).
            await asyncio.sleep(1.0)
            chk = await session.send(
                "LuaEval", {"script": f"return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE({open_uid})"}
            )
            print(f"PROJECT_PANEL_VISIBLE({open_uid}) after settle:", chk)
        except Exception as exc:  # noqa: BLE001
            print("open error:", type(exc).__name__, str(exc)[:160])

    end = await session.send("EndVerificationSession", {"sessionId": session_id})
    print("end:", end)

    await session.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open-uid", type=int, default=None,
                    help="PlayMode only: open this panel uid via LuaEval and verify visibility.")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.open_uid)))


if __name__ == "__main__":
    main()
