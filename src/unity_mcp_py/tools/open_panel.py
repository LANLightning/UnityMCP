"""open_panel tool. Write op. Opens a UI panel by uid and verifies it became visible.

Solves the async-open timing issue: PROJECT_RUNTIME.global.ui:open(uid) is asynchronous (the
panel's view is created 1+ frames later) and PROJECT_PANEL_VISIBLE(uid) returns nil (not
false) until then. This tool issues the open, then the bridge polls until the panel
is actually visible — or reports a clear terminal state:
  - visible=True                          → success
  - timedOut=True (isOpen=True)           → open accepted but the view never showed
  - refused=True (isOpen=False)           → PROJECT_CAN_OPEN silently refused (function
                                            locked / switch off / platform mismatch)

PlayMode-only (Lua VM). The bridge holds the response until visible or timeout, so a
long response_timeout is used.
"""
from __future__ import annotations

from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "open_panel"

_MIN_TIMEOUT_MS = 100
_MAX_TIMEOUT_MS = 120000


class InvalidOpenPanelArg(ValueError):
    """Raised when uid/timeout are invalid before any bridge traffic."""


async def open_panel(
    uid: int,
    info: dict[str, Any] | None = None,
    timeout_ms: int = 5000,
    verify: bool = True,
) -> dict[str, Any]:
    """Open a panel by numeric uid and (by default) wait until it is visible.

    Args:
        uid: Numeric panel id (UIConst.UI_ID_*; e.g. 66=Setting, 69=FuncMenu).
        info: Optional flat param table passed to open() (string/number/bool values
            only; nested objects/arrays are dropped — open infos are flat here).
        timeout_ms: Max time to poll for visibility (default 5000, bounded
            [100, 120000]).
        verify: If False, issue open() and return immediately without polling.

    Returns:
        Bridge result + write-op token: {uid, opened, visible, isOpen, topPanelUid,
        timedOut, refused, elapsedMs, write_op_token, ...}.

    Raises:
        InvalidOpenPanelArg: uid is 0 / timeout out of range (before any bridge call).
    """
    if not isinstance(uid, int) or isinstance(uid, bool) or uid == 0:
        raise InvalidOpenPanelArg(f"uid must be a non-zero int, got {uid!r}")
    tmo = int(timeout_ms)
    if tmo < _MIN_TIMEOUT_MS or tmo > _MAX_TIMEOUT_MS:
        raise InvalidOpenPanelArg(
            f"timeout_ms must be in [{_MIN_TIMEOUT_MS}, {_MAX_TIMEOUT_MS}], got {tmo}"
        )

    args: dict[str, Any] = {"uid": uid, "timeoutMs": tmo, "verify": bool(verify)}
    if info:
        args["info"] = dict(info)

    # The bridge holds the response until the panel is visible or timeoutMs elapses;
    # give the socket headroom beyond timeoutMs so it never trips the wait.
    response_timeout = max(30.0, tmo / 1000.0 + 15.0)
    return await write_op(
        TOOL_NAME, args, bridge_method="OpenPanel", response_timeout=response_timeout
    )


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        uid: int,
        info: dict[str, Any] | None = None,
        timeout_ms: int = 5000,
        verify: bool = True,
    ) -> dict[str, Any]:
        return await open_panel(uid, info, timeout_ms, verify)
