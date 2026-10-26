"""auto_login tool. MCP-driven game login.

Drives the project client from the PlayMode Login screen into an authenticated
session using a configured account. Replicates the Bot PROJECT_LOGIN_TEST path: set
PROJECT_CLIENT_STATE.UserName/ServerId, then PROJECT_SERVICES.PROJECT_LOGIN_SERVICE:PROJECT_TEST_LOGIN().

Returns when the server ACCEPTS the login (PROJECT_LOGIN_SERVICE.token becomes non-nil),
which happens in PROJECT_LOGIN_CALLBACK before role-creation branching. Does NOT enter
PlayMode (caller must already be in PlayMode at the Login screen) and does NOT
use the lua_eval tool (would consume its goal budget) — it calls the LuaEval
bridge command directly: one mutating kick via write_op (issues one token), then
read-only polls that issue no token.

Credentials resolve arg -> env (UNITY_MCP_LOGIN_USERNAME / _SERVER_ID /
_SCENE_ID). The username is charset-validated so it is injection-safe to
interpolate into the Lua string literal.

CAVEAT: calling PROJECT_TEST_LOGIN directly bypasses PROJECT_LOGIN_CONTROLLER:PROJECT_LOGIN_CLICK's
``-<serverId>`` username suffixing and server-list validation. Pass the exact
server-expected username (same contract as the Bot).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from ..bridge.session import bridge_call
from ..bridge import bridge_client
from ._write_op_helper import write_op

TOOL_NAME = "auto_login"

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_MAX_TIMEOUT_S = 60.0
_ENV_USERNAME = "UNITY_MCP_LOGIN_USERNAME"
_ENV_SERVER_ID = "UNITY_MCP_LOGIN_SERVER_ID"
_ENV_SCENE_ID = "UNITY_MCP_LOGIN_SCENE_ID"


class InvalidAutoLoginArg(ValueError):
    """Raised when credentials/args are invalid before any bridge traffic."""


def _coerce_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidAutoLoginArg(f"{field} must be an integer, got {value!r}")


def _resolve_credentials(
    username: str | None,
    server_id: int | None,
    scene_id: int | None,
) -> tuple[str, int, int | None]:
    """Resolve (username, server_id, scene_id) with arg -> env precedence.

    Raises InvalidAutoLoginArg if username/server_id are missing or malformed.
    scene_id is optional (None when neither arg nor env supplies it).
    """
    username = username if username is not None else os.environ.get(_ENV_USERNAME)
    if not isinstance(username, str) or not _USERNAME_RE.match(username):
        raise InvalidAutoLoginArg(
            f"username missing or has illegal chars (allow [A-Za-z0-9_-]): {username!r}"
        )

    if server_id is None:
        env_server = os.environ.get(_ENV_SERVER_ID)
        if env_server is None:
            raise InvalidAutoLoginArg(
                f"server_id missing (pass arg or set {_ENV_SERVER_ID})"
            )
        server_id = _coerce_int(env_server, "server_id")
    else:
        server_id = _coerce_int(server_id, "server_id")

    if scene_id is None:
        env_scene = os.environ.get(_ENV_SCENE_ID)
        scene_id = _coerce_int(env_scene, "scene_id") if env_scene is not None else None
    else:
        scene_id = _coerce_int(scene_id, "scene_id")

    return username, server_id, scene_id


_LUA_REQUIRE = (
    'local PROJECT_CLIENT_STATE = require("PROJECT_ADAPTER.SessionState")\n'
    'local PROJECT_SERVICES = require("PROJECT_ADAPTER.Services")\n'
)

PRECONDITION_SCRIPT = (
    _LUA_REQUIRE
    + 'return (PROJECT_CLIENT_STATE.Player ~= nil) and "player"\n'
    + '    or (PROJECT_SERVICES and PROJECT_SERVICES.PROJECT_LOGIN_SERVICE ~= nil) and "ready"\n'
    + '    or "noagent"'
)

POLL_SCRIPT = (
    _LUA_REQUIRE
    + "local a = PROJECT_SERVICES.PROJECT_LOGIN_SERVICE\n"
    + 'return (a and a.token ~= nil) and "accepted"\n'
    + '    or (PROJECT_CLIENT_STATE.Player ~= nil) and "player" or "pending"'
)


logger = logging.getLogger(__name__)

# Thin wrapper so tests can monkeypatch only this module's clock without
# affecting bridge_client.py's own time.monotonic calls.
_now = time.monotonic


def _build_kick_script(username: str, server_id: int, scene_id: int | None) -> str:
    lines = [
        _LUA_REQUIRE.rstrip("\n"),
        "local a = PROJECT_SERVICES.PROJECT_LOGIN_SERVICE",
        "a.token = nil",
        f'PROJECT_CLIENT_STATE.UserName = "{username}"',
        f"PROJECT_CLIENT_STATE.ServerId = {server_id}",
    ]
    if scene_id is not None:
        lines.append(f"a.PROJECT_DEFAULT_SCENE = {scene_id}")
    lines.append("a:PROJECT_TEST_LOGIN()")
    lines.append("return true")
    return "\n".join(lines)


async def _probe() -> str:
    """Run the precondition probe. Returns 'player' | 'ready' | 'noagent'.

    Raises bridge_client.BridgeError (e.g. INVALID_STATE in EditMode); the caller
    maps that to a not_in_playmode result.
    """
    resp = await bridge_call("LuaEval", {"script": PRECONDITION_SCRIPT})
    return str(resp.get("raw", ""))


async def _poll_state() -> str:
    """One read poll. Returns 'accepted' | 'player' | 'pending'."""
    resp = await bridge_call("LuaEval", {"script": POLL_SCRIPT})
    return str(resp.get("raw", ""))


async def auto_login(
    username: str | None = None,
    server_id: int | None = None,
    scene_id: int | None = None,
    timeout_s: float = 15.0,
    poll_interval_s: float = 0.5,
) -> dict[str, Any]:
    """Drive game login from the PlayMode Login screen with a configured account.

    Args:
        username: Account name. Falls back to UNITY_MCP_LOGIN_USERNAME.
            Charset [A-Za-z0-9_-] (injection-safe Lua interpolation).
        server_id: Cluster id. Falls back to UNITY_MCP_LOGIN_SERVER_ID.
        scene_id: Optional default scene for new characters. Falls back to
            UNITY_MCP_LOGIN_SCENE_ID. None -> not set.
        timeout_s: Max seconds to wait for the server to accept the login.
        poll_interval_s: Seconds between poll reads (kept injectable for tests).

    Returns:
        Precondition short-circuits (no kick): already-logged-in success carries
        write_op_token=None; not_in_playmode / login_agent_unavailable carry no
        token. Once the kick fires, EVERY return (accepted, player, or timeout)
        carries a real write_op_token and requires_observation=True — the caller
        MUST consume it. Success: {accepted, loggedIn, finalState, username,
        serverId, elapsedMs, write_op_token, requires_observation}. Timeout adds
        {error: "login_timeout", remedy}.

    Raises:
        InvalidAutoLoginArg: missing/malformed credentials or out-of-range
            timeout — raised before any bridge traffic, no token issued.
    """
    if not isinstance(timeout_s, (int, float)) or timeout_s <= 0 or timeout_s > _MAX_TIMEOUT_S:
        raise InvalidAutoLoginArg(f"timeout_s must be in (0, {_MAX_TIMEOUT_S}], got {timeout_s!r}")

    username, server_id, scene_id = _resolve_credentials(username, server_id, scene_id)

    try:
        state = await _probe()
    except bridge_client.BridgeError as exc:
        # INVALID_STATE means the Lua VM is unreachable (EditMode / stopped),
        # which is operationally "not in PlayMode". Any OTHER bridge error code
        # (e.g. a defect in PRECONDITION_SCRIPT) is NOT a user-state problem and
        # must surface rather than be mislabeled — re-raise it.
        if exc.code != "INVALID_STATE":
            raise
        logger.info("auto_login precondition probe failed (not in PlayMode): %s", exc)
        return {
            "accepted": False,
            "error": "not_in_playmode",
            "remedy": "Enter PlayMode at the Login screen first (ensure_ready / enter_playmode).",
            "detail": str(exc),
        }

    if state == "player":
        return {
            "accepted": True,
            "loggedIn": True,
            "finalState": "player",
            "note": "already_logged_in",
            "username": username,
            "serverId": server_id,
            "elapsedMs": 0,
            "write_op_token": None,
            "requires_observation": False,
        }
    if state == "noagent":
        return {
            "accepted": False,
            "error": "login_agent_unavailable",
            "remedy": "Open the Login panel in PlayMode so PROJECT_SERVICES.PROJECT_LOGIN_SERVICE exists.",
            "username": username,
            "serverId": server_id,
        }

    # state == "ready": fire the login, then poll until accepted/player/timeout.
    start = _now()
    kick_script = _build_kick_script(username, server_id, scene_id)
    kick = await write_op(TOOL_NAME, {"script": kick_script}, bridge_method="LuaEval")
    token = kick["write_op_token"]

    final_state = "pending"
    deadline = start + timeout_s
    try:
        while True:
            final_state = await _poll_state()
            if final_state in ("accepted", "player"):
                break
            if _now() >= deadline:
                break
            await asyncio.sleep(poll_interval_s)
    except bridge_client.BridgeError as exc:
        # Bridge dropped mid-poll (e.g. Unity left PlayMode after the kick fired).
        # The kick already mutated game state and issued a token, so we MUST return
        # it for observation rather than let the exception leak the token in GUARD.
        logger.info("auto_login poll phase bridge error: %s", exc)
        return {
            "accepted": False,
            "error": "poll_bridge_error",
            "remedy": (
                "Bridge dropped while polling login state. Run get_console_logs; "
                "verify Unity is still in PlayMode and the bridge is connected."
            ),
            "detail": str(exc),
            "finalState": final_state,
            "username": username,
            "serverId": server_id,
            "write_op_token": token,
            "requires_observation": True,
        }

    elapsed_ms = int((_now() - start) * 1000)
    accepted = final_state in ("accepted", "player")
    result: dict[str, Any] = {
        "accepted": accepted,
        "loggedIn": final_state == "player",
        "finalState": final_state,
        "username": username,
        "serverId": server_id,
        "elapsedMs": elapsed_ms,
        "write_op_token": token,
        "requires_observation": True,
    }
    if not accepted:
        result["error"] = "login_timeout"
        result["remedy"] = (
            "Server did not accept the login within timeout. Run get_console_logs "
            "(failure path logs SERVER_LOGIN_FAIL); verify the gate/server is reachable."
        )
    return result


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        username: str | None = None,
        server_id: int | None = None,
        scene_id: int | None = None,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        return await auto_login(username, server_id, scene_id, timeout_s)
