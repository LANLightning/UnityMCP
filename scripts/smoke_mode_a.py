"""Smoke test: full Mode A loop against PROJECT_EDITOR Unity.

Prereq: PROJECT_EDITOR Unity Editor running, bridge listening on 127.0.0.1:17873.
Run: python scripts/smoke_mode_a.py
"""
import asyncio
import os
import sys

sys.path.insert(0, "src")

from unity_mcp_py.tools.ensure_ready import ensure_ready
from unity_mcp_py.tools.assert_no_compile_errors import assert_no_compile_errors
from unity_mcp_py.tools.enter_playmode import enter_playmode
from unity_mcp_py.tools.exit_playmode import exit_playmode
from unity_mcp_py.tools.observe_evidence import observe_evidence
from unity_mcp_py.bridge.session import bridge_call
from unity_mcp_py.state import GUARD


async def main() -> None:
    project_path = os.environ.get("UNITY_MCP_PROJECT_PATH")
    if not project_path:
        raise SystemExit("Set UNITY_MCP_PROJECT_PATH to your Unity project root before running.")

    print("Step 1: ensure_ready")
    r1 = await ensure_ready(project_path=project_path)
    print(f"  -> {r1}")
    assert r1["ready"], f"Bridge not ready: {r1.get('error')}"

    print("Step 2: assert_no_compile_errors")
    r2 = await assert_no_compile_errors()
    print(f"  -> {r2}")
    assert not r2["has_errors"], f"Compile errors: {r2['errors']}"
    assert r2["is_runtime_evidence"] is False, "is_runtime_evidence must be False (hardcoded gate)"

    # Pre-condition: ensure we start in EditMode so enter_playmode is valid.
    # Use bridge_call directly (bypass write_op_helper) to avoid screenshot failures
    # during playmode transition, then drain any residual tokens from GUARD.
    if r1.get("playmodeState") == "PlayMode":
        print("Step 2b: pre-condition — Unity is in PlayMode; exiting first (direct bridge call)")
        await bridge_call("ExitPlaymode", {})
        # Drain any stale pending tokens before the test proper
        stale_pending = await GUARD.list_pending()
        if stale_pending:
            stale_tokens = [c.token for c in stale_pending]
            await GUARD.consume(stale_tokens)
            print(f"  -> Drained {len(stale_tokens)} stale token(s): {stale_tokens}")
        print(f"  -> Exited playmode via direct bridge call")

    print("Step 3: enter_playmode")
    r3 = await enter_playmode()
    t1 = r3["write_op_token"]
    print(f"  -> token={t1}, line_before={r3['line_number_before']}")
    assert "write_op_token" in r3
    assert r3.get("requires_observation") is True

    print("Step 4: observe_evidence (consume t1) — with CaptureScreenshot retry")
    # Unity may need a moment to render a Game View frame after entering PlayMode.
    # Retry up to 5 times with 1s delay on CaptureScreenshot INTERNAL errors.
    # Note: consume_tokens is passed on every attempt. If CaptureScreenshot fails,
    # GUARD.consume is never reached (it runs after screenshot), so the token
    # remains pending and can be retried correctly.
    r4 = None
    from lib.bridge_client import BridgeError as _BridgeError
    for attempt in range(1, 6):
        try:
            r4 = await observe_evidence(consume_tokens=[t1])
            break
        except _BridgeError as exc:
            if "invalid" in str(exc).lower() or "null" in str(exc).lower():
                print(f"  [attempt {attempt}] CaptureScreenshot not ready yet: {exc}; retrying in 1s...")
                await asyncio.sleep(1.0)
            else:
                raise
    if r4 is None:
        raise RuntimeError("observe_evidence failed after 5 attempts (CaptureScreenshot consistently null)")
    print(
        f"  -> play_state={r4['play_state']}, "
        f"log_lines={len(r4['log_delta'])}, "
        f"scene_objects={len(r4['scene_objects'])}, "
        f"consumed={r4['consumed_tokens']}"
    )
    assert r4["play_state"] == "PlayMode", (
        f"Expected PlayMode, got {r4['play_state']!r}"
    )
    assert r4["consumed_tokens"] == [t1], (
        f"Expected consumed=[{t1}], got {r4['consumed_tokens']}"
    )

    print("Step 5: exit_playmode")
    r5 = await exit_playmode()
    t2 = r5["write_op_token"]
    print(f"  -> token={t2}, line_before={r5['line_number_before']}")

    print("Step 6: observe_evidence (consume t2) — with CaptureScreenshot retry")
    r6 = None
    for attempt in range(1, 6):
        try:
            r6 = await observe_evidence(consume_tokens=[t2])
            break
        except _BridgeError as exc:
            if "invalid" in str(exc).lower() or "null" in str(exc).lower():
                print(f"  [attempt {attempt}] CaptureScreenshot not ready: {exc}; retrying in 1s...")
                await asyncio.sleep(1.0)
            else:
                raise
    if r6 is None:
        raise RuntimeError("observe_evidence (step 6) failed after 5 attempts")
    print(
        f"  -> play_state={r6['play_state']}, "
        f"consumed={r6['consumed_tokens']}"
    )
    assert r6["play_state"] == "EditMode", (
        f"Expected EditMode, got {r6['play_state']!r}"
    )
    assert r6["consumed_tokens"] == [t2], (
        f"Expected consumed=[{t2}], got {r6['consumed_tokens']}"
    )

    print("\nSMOKE PASS: Mode A loop end-to-end green.")


if __name__ == "__main__":
    asyncio.run(main())
