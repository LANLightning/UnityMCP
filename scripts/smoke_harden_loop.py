"""End-to-end smoke: LLM-style correction loop with injection + hardening checks.

Demonstrates the runtime-evidence-only loop that MEDIUM-tier hardening enables:

  Phase 1 — input sanitation (NO bridge traffic, NO tokens):
    - lua_reload_module rejects a Lua-injection payload
    - open_scene rejects a UNC path
    - Both raise BEFORE any bridge call; GUARD stays empty.

  Phase 2 — two-call human_collab_gate flow with opaque _wu_id:
    - First call returns _wu_id (NOT _baseline_context).
    - Second call with _user_choice + _wu_id resolves verdict=skipped.

  Phase 3 — live Mode A loop with real bridge, batch write + parallel verify:
    - enter_playmode → Lua reload of a known module → scene snapshot
    - 3 tokens issued, consumed exactly once each via observe_evidence

Prereq:
  - PROJECT_EDITOR Unity Editor running with bridge on 127.0.0.1:17873
  - PROJECT_ADAPTER.Reload is bootstrap-initialized (T2 verified during WU-4)

Run:
  $env:UNITY_MCP_PROJECT_PATH = "<PROJECT_ROOT>"
  python scripts/smoke_harden_loop.py

Exit code 0 on SMOKE PASS; non-zero and a clear assertion on any failure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, "src")

from unity_mcp_py.bridge.session import bridge_call
from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.ensure_ready import ensure_ready
from unity_mcp_py.tools.enter_playmode import enter_playmode
from unity_mcp_py.tools.exit_playmode import exit_playmode
from unity_mcp_py.tools.human_collab_gate import human_collab_gate
from unity_mcp_py.tools.lua_reload_module import (
    InvalidLuaModuleNameError,
    lua_reload_module,
)
from unity_mcp_py.tools.observe_evidence import observe_evidence
from unity_mcp_py.tools.open_scene import InvalidScenePathError, open_scene


async def phase1_input_sanitation() -> None:
    """HIGH + MEDIUM guards trigger BEFORE any bridge traffic."""
    print("Phase 1: input sanitation")

    starting_tokens = {c.token for c in await GUARD.list_pending()}

    # Lua injection — HIGH fix: Python allowlist regex blocks it.
    try:
        await lua_reload_module(modules=["Foo'); os.execute('whoami'); --"])
    except InvalidLuaModuleNameError as exc:
        print(f"  [OK] lua_reload_module rejected injection: {exc}")
    else:
        raise AssertionError("lua_reload_module did NOT reject injection payload")

    # UNC path — MEDIUM fix: open_scene rejects before bridge.
    try:
        await open_scene(r"\\attacker\share\evil.unity")
    except InvalidScenePathError as exc:
        print(f"  [OK] open_scene rejected UNC: {exc}")
    else:
        raise AssertionError("open_scene did NOT reject UNC path")

    # Traversal — same guard.
    try:
        await open_scene("Assets/../../outside.unity")
    except InvalidScenePathError as exc:
        print(f"  [OK] open_scene rejected traversal: {exc}")
    else:
        raise AssertionError("open_scene did NOT reject traversal")

    # GUARD must still hold exactly the set of tokens it had on entry.
    after_tokens = {c.token for c in await GUARD.list_pending()}
    leaked = after_tokens - starting_tokens
    assert not leaked, (
        f"TOKEN LEAK: rejected inputs issued {len(leaked)} tokens: {leaked}"
    )
    print(f"  [OK] GUARD clean: no tokens issued for rejected inputs")


async def phase2_human_collab_two_call(tmp_dir: str) -> None:
    """Two-call flow with opaque _wu_id (MEDIUM fix: no path leakage)."""
    print("Phase 2: human_collab_gate two-call flow")
    os.environ["UNITY_MCP_HCG_LOG_DIR"] = tmp_dir

    # First call: NO _user_choice → awaiting_user + _wu_id.
    r1 = await human_collab_gate(
        human_instruction="Press Play and click the red button",
        fail_levels=["Error", "Exception"],
    )
    assert r1["verdict"] == "awaiting_user", r1
    assert "_wu_id" in r1 and r1["_wu_id"].startswith("mcp-"), r1
    assert "_baseline_context" not in r1, (
        "MEDIUM regression: _baseline_context leaked into LLM context"
    )
    wu_id = r1["_wu_id"]
    print(f"  [OK] first call -> verdict=awaiting_user, _wu_id={wu_id}")

    # Second call: simulate user "Skipped" → resolves without further bridge calls.
    r2 = await human_collab_gate(
        human_instruction="Press Play and click the red button",
        fail_levels=["Error", "Exception"],
        _user_choice="Skipped",
        _wu_id=wu_id,
    )
    assert r2["verdict"] == "skipped", r2
    assert r2["baseline_line_number"] == r1["baseline_line_number"]
    print(f"  [OK] second call -> verdict=skipped, baseline_line={r2['baseline_line_number']}")

    # Third call with the SAME _wu_id must fail — baseline was consumed.
    try:
        await human_collab_gate(
            human_instruction="x",
            _user_choice="Done",
            _wu_id=wu_id,
        )
    except ValueError as exc:
        assert "not found" in str(exc), exc
        print(f"  [OK] replay of consumed _wu_id correctly rejected")
    else:
        raise AssertionError("baseline should have been single-use")


async def phase3_batch_write_parallel_verify(project_path: str) -> None:
    """Real bridge: 3 writes → list_pending → 3 parallel observes consume exactly once each."""
    print("Phase 3: batch write + parallel verify (live bridge)")

    r = await ensure_ready(project_path=project_path)
    assert r["ready"], f"bridge not ready: {r.get('error')}"

    # Normalize: if already in PlayMode, exit first (via direct bridge call to
    # avoid issuing a token for the pre-condition reset).
    if r.get("playmodeState") == "PlayMode":
        await bridge_call("ExitPlaymode", {})
        stale = [c.token for c in await GUARD.list_pending()]
        if stale:
            await GUARD.consume(stale)

    # Batch 3 writes that exercise different tools:
    # enter_playmode → exit_playmode → enter_playmode.
    # Each issues exactly one token; all three coexist in GUARD.
    w1 = await enter_playmode()
    w2 = await exit_playmode()
    w3 = await enter_playmode()
    tokens = [w1["write_op_token"], w2["write_op_token"], w3["write_op_token"]]
    assert len(set(tokens)) == 3, f"tokens should be unique: {tokens}"
    print(f"  [OK] 3 writes issued {len(tokens)} unique tokens: {tokens}")

    # list_pending_writes shows all three.
    pending = await GUARD.list_pending()
    pending_ids = {c.token for c in pending}
    assert set(tokens) <= pending_ids, (
        f"GUARD missing tokens: expected {tokens}, have {pending_ids}"
    )
    print(f"  [OK] GUARD holds all {len(tokens)} tokens pre-verify")

    # Parallel verify: N=3 observe_evidence calls, each consuming exactly 1 token.
    # Retry-wrap each in case CaptureScreenshot isn't frame-ready.
    async def _consume_one(token: str, idx: int) -> dict:
        from lib.bridge_client import BridgeError as _BE
        for attempt in range(1, 6):
            try:
                return await observe_evidence(consume_tokens=[token])
            except _BE as exc:
                if "invalid" in str(exc).lower() or "null" in str(exc).lower():
                    await asyncio.sleep(0.8)
                    continue
                raise
        raise RuntimeError(f"verifier {idx} exhausted retries")

    verdicts = await asyncio.gather(
        _consume_one(tokens[0], 0),
        _consume_one(tokens[1], 1),
        _consume_one(tokens[2], 2),
    )

    total_consumed = sum(len(v["consumed_tokens"]) for v in verdicts)
    total_unknown = sum(len(v["unknown_tokens"]) for v in verdicts)
    assert total_consumed == 3, (
        f"expected 3 total consumes, got {total_consumed}; verdicts={verdicts}"
    )
    assert total_unknown == 0, f"no verifier should see unknown tokens: {verdicts}"
    print(f"  [OK] 3 parallel observes consumed 3 tokens exactly once each")

    # Each verdict must cite play_state as a concrete string (runtime-evidence rule).
    for i, v in enumerate(verdicts):
        assert v["play_state"] in {"EditMode", "PlayMode", "Paused"}, (
            f"verdict {i} play_state invalid: {v['play_state']!r}"
        )
    print(f"  [OK] all verdicts include concrete play_state (runtime-evidence check)")

    # Cleanup: leave Unity in EditMode.
    if verdicts[2]["play_state"] == "PlayMode":
        final = await exit_playmode()
        await observe_evidence(consume_tokens=[final["write_op_token"]])


async def main() -> None:
    project_path = os.environ.get("UNITY_MCP_PROJECT_PATH")
    if not project_path:
        raise SystemExit("Set UNITY_MCP_PROJECT_PATH to your Unity project root before running.")
    tmp_dir = os.path.join(
        os.environ.get("TEMP", "/tmp"),
        f"unity-mcp-harden-smoke-{int(time.time())}",
    )
    os.makedirs(tmp_dir, exist_ok=True)

    started_at = time.time()

    await phase1_input_sanitation()
    print()
    await phase2_human_collab_two_call(tmp_dir)
    print()
    await phase3_batch_write_parallel_verify(project_path)

    elapsed = time.time() - started_at
    print(f"\nSMOKE PASS: harden-loop end-to-end green in {elapsed:.1f}s.")


if __name__ == "__main__":
    asyncio.run(main())
