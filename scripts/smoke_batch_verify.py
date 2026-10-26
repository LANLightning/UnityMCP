"""Smoke test: batch write + simulated verifier consumption (no real LLM agents).

Validates: 3 sequential writes -> 3 tokens -> list_pending shows all 3 ->
3 parallel observe_evidence each consume exactly 1 token -> no duplicates, no unknowns.

Prereq: PROJECT_EDITOR Unity Editor running, bridge listening on 127.0.0.1:17873.
Run: python scripts/smoke_batch_verify.py
"""
import asyncio
import os
import sys

sys.path.insert(0, "src")

from unity_mcp_py.tools.ensure_ready import ensure_ready
from unity_mcp_py.tools.enter_playmode import enter_playmode
from unity_mcp_py.tools.exit_playmode import exit_playmode
from unity_mcp_py.tools.observe_evidence import observe_evidence
from unity_mcp_py.tools.list_pending_writes import list_pending_writes


async def main() -> None:
    project_path = os.environ.get("UNITY_MCP_PROJECT_PATH")
    if not project_path:
        raise SystemExit("Set UNITY_MCP_PROJECT_PATH to your Unity project root before running.")

    print("Step 1: ensure_ready")
    r0 = await ensure_ready(project_path=project_path)
    print(f"  -> {r0}")
    assert r0["ready"], f"Bridge not ready: {r0.get('error')}"

    # Batch 3 sequential writes (in real workflow these would be subagent-parallel)
    print("Step 2: 3 sequential write ops (enter, exit, enter)")
    r1 = await enter_playmode()
    t1 = r1["write_op_token"]
    print(f"  -> token1={t1}")

    r2 = await exit_playmode()
    t2 = r2["write_op_token"]
    print(f"  -> token2={t2}")

    r3 = await enter_playmode()
    t3 = r3["write_op_token"]
    print(f"  -> token3={t3}")

    tokens = [t1, t2, t3]
    print(f"Issued {len(tokens)} tokens.")

    print("Step 3: list_pending_writes — assert all 3 tokens present")
    listing = await list_pending_writes()
    pending_token_set = {item["token"] for item in listing}
    print(f"  -> list_pending_writes returned {len(listing)} entries; tokens in list: {sorted(pending_token_set)}")
    for t in tokens:
        assert t in pending_token_set, f"Token {t} missing from list_pending_writes"
    print(f"  -> All 3 tokens confirmed pending.")

    print("Step 4: 3 parallel observe_evidence (each consumes 1 token)")
    results = await asyncio.gather(
        observe_evidence(consume_tokens=[t1]),
        observe_evidence(consume_tokens=[t2]),
        observe_evidence(consume_tokens=[t3]),
    )

    total_consumed = sum(len(r["consumed_tokens"]) for r in results)
    total_unknown = sum(len(r["unknown_tokens"]) for r in results)
    total_expired = sum(len(r["expired_tokens"]) for r in results)
    print(
        f"  -> total_consumed={total_consumed}, "
        f"total_unknown={total_unknown}, "
        f"total_expired={total_expired}"
    )
    assert total_consumed == 3, (
        f"Expected 3 consumed, got {total_consumed}. "
        f"unknown={total_unknown}, expired={total_expired}"
    )
    assert total_unknown == 0, (
        f"Expected 0 unknown, got {total_unknown}. "
        "Token plumbing error — possible duplicate consume."
    )
    assert total_expired == 0, f"Expected 0 expired, got {total_expired}."
    print(f"  -> 1:1 mapping verified: each token consumed exactly once.")

    print("Step 5: cleanup — exit playmode + observe")
    r4 = await exit_playmode()
    t4 = r4["write_op_token"]
    r5 = await observe_evidence(consume_tokens=[t4])
    print(f"  -> cleanup token {t4} consumed={r5['consumed_tokens']}, play_state={r5['play_state']}")

    print("\nSMOKE PASS: batch+verify token plumbing green.")


if __name__ == "__main__":
    asyncio.run(main())
