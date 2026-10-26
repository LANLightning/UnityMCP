"""MCP-facing bridge administration tool used by session prompts."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from mcp.server.fastmcp import Context

from ..bridge.cli import run_admin_action
from .bind_bridge import _do_bind_bridge

logger = logging.getLogger(__name__)

BridgeAction = Literal["pair", "status", "owners", "diagnose", "clear", "repair"]
ClearSide = Literal["binding", "accepted", "listener"]


async def run_bridge_admin(
    ctx: Context | Any,
    *,
    action: BridgeAction | str,
    port: int = 17873,
    host: str = "127.0.0.1",
    client_id: str | None = None,
    bridge_id: str | None = None,
    side: ClearSide | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Execute an administrative action with a stable structured boundary."""
    try:
        if action == "pair":
            if not client_id or not bridge_id:
                return _invalid("pair requires client_id and bridge_id")
            if host != "127.0.0.1":
                return _invalid("live pair currently requires host=127.0.0.1")
            result = await _do_bind_bridge(
                ctx,
                port=port,
                expected_bridge_id=bridge_id,
                force_takeover=force,
                client_id=client_id,
            )
            return {**result, "exitCode": 0 if result.get("success") else 1}
        result = await asyncio.to_thread(
            run_admin_action,
            action,
            host=host,
            port=port,
            client_id=client_id,
            bridge_id=bridge_id,
            side=side,
            force=force,
        )
        return result
    except ValueError as exc:
        return _invalid(str(exc))
    except (OSError, RuntimeError) as exc:
        logger.exception("Bridge administration failed for action=%s", action)
        return {
            "success": False,
            "errorCode": "ADMIN_OPERATION_FAILED",
            "error": str(exc),
            "exitCode": 1,
        }


def _invalid(message: str) -> dict[str, Any]:
    return {
        "success": False,
        "errorCode": "INVALID_ARGUMENT",
        "error": message,
        "exitCode": 2,
    }


def register(mcp_instance: Any) -> None:
    # FastMCP registers the raw server-side name. MCP clients add the
    # ``mcp__<server>__`` namespace when exposing the tool to a session.
    @mcp_instance.tool(name="bridge_admin")
    async def bridge_admin(
        ctx: Context,
        action: BridgeAction,
        port: int = 17873,
        host: str = "127.0.0.1",
        client_id: str | None = None,
        bridge_id: str | None = None,
        side: ClearSide | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Pair, inspect, diagnose, clear, or safely repair a Unity bridge.

        Actions: pair, status, owners, diagnose, clear, repair. Clear also
        requires side=binding|accepted|listener. Destructive operations retain
        bridge/client/generation identity checks and never kill a process.
        """
        return await run_bridge_admin(
            ctx,
            action=action,
            port=port,
            host=host,
            client_id=client_id,
            bridge_id=bridge_id,
            side=side,
            force=force,
        )
