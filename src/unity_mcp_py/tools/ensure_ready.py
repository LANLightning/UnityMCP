"""ensure_ready tool. Setup. No token.

Verifies bridge connectivity. Does NOT launch Unity in MVP — assumes
PROJECT_EDITOR Unity Editor is already running with bridge listener on 127.0.0.1:17873.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..bridge.session import bridge_call, get_session_async

logger = logging.getLogger(__name__)

TOOL_NAME = "ensure_ready"


async def ensure_ready(
    project_path: str | None = None,
    engine_binary_path: str | None = None,    # accepted but unused in MVP
    auto_enter_playmode: bool = False,
) -> dict[str, Any]:
    project_path = project_path or os.environ.get("UNITY_MCP_PROJECT_PATH")
    if not project_path:
        return {
            "ready": False,
            "error": "project_path missing and UNITY_MCP_PROJECT_PATH not set",
        }

    session = await get_session_async()
    try:
        handshake = await session.connect()
    except Exception as exc:
        logger.warning("Bridge connect failed: %s", exc)
        return {
            "ready": False,
            "error": f"bridge_unreachable: {exc}",
            "remedy": (
                "Launch PROJECT_EDITOR Unity Editor; verify ProjectSettings/UnityMCPBridge.disabled "
                "marker absent; verify port 17873 listening."
            ),
        }

    play_state = await bridge_call("GetPlayState", {})
    state_str = play_state.get("state", "Unknown")

    if auto_enter_playmode and state_str != "PlayMode":
        await bridge_call("EnterPlaymode", {})
        play_state = await bridge_call("GetPlayState", {})
        state_str = play_state.get("state", "Unknown")

    return {
        "ready": True,
        "alreadyRunning": handshake.get("alreadyConnected", False),
        "editorVersion": handshake.get("editorVersion", ""),
        "projectPath": handshake.get("projectPath", project_path),
        "playmodeState": state_str,
    }


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        project_path: str | None = None,
        engine_binary_path: str | None = None,
        auto_enter_playmode: bool = False,
    ) -> dict[str, Any]:
        return await ensure_ready(project_path, engine_binary_path, auto_enter_playmode)
