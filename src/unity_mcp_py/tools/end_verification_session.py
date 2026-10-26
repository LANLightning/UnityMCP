"""end_verification_session tool. Read-op (control).

Ends a verification run: hides the hint banner, disarms takeover detection, and
aborts any in-flight verification work tied to the session. Returns the low/high
takeover event counts observed during the run.
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "end_verification_session"


class InvalidVerificationSessionArg(ValueError):
    """Raised when the session id is missing before any bridge traffic."""


async def end_verification_session(session_id: str) -> dict[str, Any]:
    """End a verification session by id.

    Args:
        session_id: The id returned by start_verification_session.

    Returns:
        {"ended": bool, "lowEvents": int, "highEvents": int}. ``ended`` is False
        if the id did not match the active session (stale/double end).
    """
    if not isinstance(session_id, str) or len(session_id.strip()) == 0:
        raise InvalidVerificationSessionArg("session_id is required")
    return await bridge_call("EndVerificationSession", {"sessionId": session_id})


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(session_id: str) -> dict[str, Any]:
        return await end_verification_session(session_id)
