"""Shared helper: snapshot line_number → bridge call → issue token.

See spec §6 write-op pairing.
"""
from __future__ import annotations

import json
from typing import Any

from ..bridge.session import bridge_call
from ..state import GUARD


async def write_op(
    tool_name: str,
    args: dict[str, Any],
    bridge_method: str,
    response_timeout: float = 30.0,
) -> dict[str, Any]:
    """Standard write flow:
    1. Snapshot line_number_before via TailEditorLog.
    2. Send bridge_method with args.
    3. On success, issue token.
    4. Return bridge response enriched with token fields.

    If bridge_call raises, no token is issued (failure observability preserved).

    ``response_timeout`` overrides the default for write ops the bridge holds open
    for a while (e.g. OpenPanel polls until the panel is visible up to timeoutMs).
    """
    tail_resp = await bridge_call("TailEditorLog", {"maxLines": 1})
    lines = tail_resp.get("lines", [])
    line_number_before: int = lines[-1]["lineNumber"] if lines else 0

    bridge_resp = await bridge_call(bridge_method, args, response_timeout=response_timeout)

    args_summary = json.dumps(args, ensure_ascii=False)[:200]
    ctx = await GUARD.issue(tool_name, args_summary, line_number_before)

    return {
        **bridge_resp,
        "write_op_token": ctx.token,
        "requires_observation": True,
        "line_number_before": line_number_before,
    }
