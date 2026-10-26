"""Async wrapper + singleton over the synchronous BridgeClient."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import uuid
from typing import Any

import anyio
from . import bridge_client
from ..telemetry import bridge_rtt_tick

logger = logging.getLogger(__name__)

_DEFAULT_PORT = int(os.environ.get("UNITY_MCP_BRIDGE_PORT", "17873"))
_DEFAULT_HOST = os.environ.get("UNITY_MCP_BRIDGE_HOST", "127.0.0.1")
_HANDSHAKE_TIMEOUT_SECONDS = 5.0

class NonLoopbackBridgeHostError(RuntimeError):
    """Raised when the configured bridge host resolves to a non-loopback address."""


def _is_loopback_host(host: str) -> bool:
    """Return True only for a literal loopback address (no DNS rebinding)."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_host(host: str) -> str:
    """Enforce a literal loopback endpoint; remote transport is unsupported."""
    if not _is_loopback_host(host):
        raise NonLoopbackBridgeHostError(
            f"UNITY_MCP_BRIDGE_HOST={host!r} must be a literal loopback address; "
            "remote and DNS-named bridge transport is not supported."
        )
    return host


class BridgeSession:
    """Lazy-connected async wrapper around a single BridgeClient.

    Thread-safety: BridgeClient is single-threaded; serialize calls via lock.
    """

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        *,
        client_id: str | None = None,
        expected_bridge_id: str | None = None,
        force_takeover: bool = False,
        expected_bridge_instance_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        self.host = _validate_host(host)
        self.port = port
        self.client_id = client_id or default_client_id()
        self.client_instance_id = str(uuid.uuid4())
        self.expected_bridge_id = expected_bridge_id
        self.force_takeover = force_takeover
        self.expected_bridge_instance_id = expected_bridge_instance_id
        self.expected_connection_generation = expected_connection_generation
        self.bridge_id: str | None = None
        self._client: bridge_client.BridgeClient | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._handshake_done = False
        # Feature 4: sha256 of the last screenshot delivered with full image data.
        # capture_screenshot(skip_if_unchanged=True) compares against this only in
        # full mode. Metadata and file references do not establish pixel delivery.
        # Process-lifetime state; survives across tool calls in one session.
        self.last_screenshot_sha: str | None = None
        # Conditional screenshot gate (spec 2026-06-08): set True by the reload
        # tools (csharp_reload_scripts / lua_reload_module) to mark "code was
        # modified". capture_screenshot gates on 继续 iff this is True, and clears
        # it after a successful gated capture (the 继续 click released it).
        self.code_dirty: bool = False
        # Path of the last screenshot we wrote on the CLEAN (ungated) path. The
        # next clean capture unlinks it before tracking its own — "下次调用时清理
        # 上一张", so disk holds at most one clean shot.
        self.last_clean_screenshot_path: str | None = None

    async def connect(self) -> dict[str, Any]:
        """Idempotent connect + handshake. Returns handshake result dict."""
        async with self._connect_lock:
            if self._client is not None and self._handshake_done:
                return {"alreadyConnected": True}
            candidate = self._make_client()
            try:
                await _run_blocking(candidate.connect, _HANDSHAKE_TIMEOUT_SECONDS)
                result = await _run_blocking(candidate.handshake)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await asyncio.to_thread(candidate.close)
                raise
            except Exception:
                await _run_blocking(candidate.close)
                raise
            self._client = candidate
            self._handshake_done = True
            self.bridge_id = result.get("bridgeId")
            return result

    def _make_client(self) -> bridge_client.BridgeClient:
        from .control import load_or_create_auth_token

        return bridge_client.BridgeClient(
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            client_instance_id=self.client_instance_id,
            expected_bridge_id=self.expected_bridge_id,
            force_takeover=self.force_takeover,
            auth_token=load_or_create_auth_token(),
            expected_bridge_instance_id=self.expected_bridge_instance_id,
            expected_connection_generation=self.expected_connection_generation,
        )

    async def send(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        response_timeout: float = 30.0,
    ) -> dict[str, Any]:  # noqa: D401
        """Send a request and return its result dict. Auto-connects if needed.

        The connect-check lives inside ``_send_lock`` to close a TOCTOU race
        where two concurrent callers could each observe ``_handshake_done==False``,
        both call ``connect()`` (idempotent — safe), but one could then proceed
        to ``asyncio.to_thread(self._client.send, ...)`` before the other
        connect() coroutine had actually flipped ``_handshake_done=True``.
        Serializing the whole "ensure-connected + send" sequence under
        ``_send_lock`` guarantees that by the time we reach ``to_thread``,
        ``_client`` is non-None and the handshake has completed. ``connect()``
        holds its own ``_connect_lock`` and never calls ``send()`` recursively,
        so nested-lock acquisition here is deadlock-free.
        """
        async with self._send_lock:
            if self._client is None or not self._handshake_done:
                await self.connect()
            assert self._client is not None
            client = self._client
            # Phase 0 telemetry: count this round-trip against the current tool
            # call's per-call counter (reset by Telemetry.wrap). Ticking here in
            # the async context — not inside to_thread — keeps the contextvar
            # mutation on the task that Telemetry.wrap reads back.
            bridge_rtt_tick()
            worker = asyncio.create_task(
                asyncio.to_thread(
                    client.send, command, args or {}, response_timeout
                )
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Closing the socket interrupts select/recv in the worker. Wait for
                # it to exit before releasing _send_lock so BridgeClient is never
                # used concurrently by a subsequent command.
                with anyio.CancelScope(shield=True):
                    await asyncio.to_thread(client.close)
                    try:
                        await worker
                    except Exception:
                        pass
                    if self._client is client:
                        self._client = None
                        self._handshake_done = False
                        self.bridge_id = None
                raise
            except bridge_client.BridgeConnectionError:
                await self._close_without_send_lock()
                raise

    async def close(self) -> None:
        """Close the underlying TCP connection."""
        async with self._send_lock:
            await self._close_without_send_lock()

    async def abort(self) -> None:
        """Close the socket without waiting for a stuck in-flight send."""
        await self._close_without_send_lock()


    async def _close_without_send_lock(self) -> None:
        async with self._connect_lock:
            client = self._client
            self._client = None
            self._handshake_done = False
            self.bridge_id = None
            if client is not None:
                with anyio.CancelScope(shield=True):
                    await asyncio.to_thread(client.close)


# Module-level singleton (process-wide). Tests replace via fixture.
_SESSION: BridgeSession | None = None
_SESSION_SWITCH_LOCK = asyncio.Lock()


def get_session() -> BridgeSession:
    """Return the module-level singleton BridgeSession, creating it if needed."""
    global _SESSION
    if _SESSION is None:
        _SESSION = _session_from_binding_or_environment()
    return _SESSION


async def get_session_async() -> BridgeSession:
    """Initialize the singleton off the event loop and publish it once."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    async with _SESSION_SWITCH_LOCK:
        if _SESSION is not None:
            return _SESSION
        candidate = await _run_blocking(_session_from_binding_or_environment)
        if _SESSION is None:
            _SESSION = candidate
        return _SESSION


async def is_current_session(expected: BridgeSession) -> bool:
    """Return whether *expected* is still the published process session."""
    async with _SESSION_SWITCH_LOCK:
        return _SESSION is expected


def set_session_for_test(session: BridgeSession | None) -> None:
    """Test fixture injects a session pointing at MockBridgeServer's port."""
    global _SESSION
    _SESSION = session


async def switch_session(candidate: BridgeSession) -> dict[str, Any]:
    """Connect and verify a candidate before atomically replacing the live session."""
    previous = await get_session_async()
    try:
        result = await candidate.connect()
        await commit_connected_session(candidate, expected_previous=previous)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if not await is_current_session(candidate):
                await candidate.abort()
        raise
    except Exception:
        await candidate.close()
        raise
    return result


async def commit_connected_session(
    candidate: BridgeSession, *, expected_previous: BridgeSession | None
) -> None:
    """Publish a prepared session only if the active generation did not change."""
    global _SESSION
    if candidate._client is None or not candidate._handshake_done:
        raise RuntimeError("candidate session must complete its handshake before commit")
    async with _SESSION_SWITCH_LOCK:
        if _SESSION is not expected_previous:
            raise RuntimeError("active bridge session changed while candidate was connecting")
        previous = _SESSION
        _SESSION = candidate
    if previous is not None and previous is not candidate:
        try:
            await _run_async_to_completion(previous.abort())
        except Exception:
            # Publication already succeeded. A best-effort old-session cleanup
            # failure must not roll the singleton back to a stale connection.
            logger.exception("Failed to close previous bridge session after commit")


async def close_current_session(
    *,
    expected_session: BridgeSession | None = None,
    expected_port: int | None = None,
    expected_client_id: str | None = None,
) -> bool:
    """Close only the requested active receiver and reset the process singleton."""
    global _SESSION
    async with _SESSION_SWITCH_LOCK:
        current = _SESSION
        if current is None:
            return False
        if expected_session is not None and current is not expected_session:
            return False
        if expected_port is not None and current.port != expected_port:
            return False
        if expected_client_id is not None and current.client_id != expected_client_id:
            return False
        _SESSION = None
    await _run_async_to_completion(current.abort())
    return True


def default_client_id() -> str:
    return (
        os.environ.get("UNITY_MCP_CLIENT_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or f"mcp-{os.getpid()}"
    )


def _session_from_binding_or_environment() -> BridgeSession:
    client_id = default_client_id()
    try:
        from .control import BridgeBindingStore

        binding = BridgeBindingStore().get(client_id)
    except (OSError, ValueError):
        logger.exception("Failed to load bridge binding for client %s", client_id)
        binding = None
    if binding is not None:
        return BridgeSession(
            host=binding.host,
            port=binding.port,
            client_id=client_id,
            expected_bridge_id=binding.expected_bridge_id,
        )
    return BridgeSession(client_id=client_id)


async def _run_blocking(function: Any, *args: Any) -> Any:
    """Let a worker finish on cancellation so its socket remains reachable for cleanup."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        with anyio.CancelScope(shield=True):
            try:
                await task
            except Exception:
                logger.exception("Worker failed while the caller was being cancelled")
        raise cancelled


async def _run_async_to_completion(awaitable: Any) -> Any:
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        with anyio.CancelScope(shield=True):
            try:
                await task
            except Exception:
                logger.exception("Async cleanup failed while the caller was being cancelled")
        raise cancelled


async def bridge_call(
    command: str,
    args: dict[str, Any] | None = None,
    response_timeout: float = 30.0,
) -> dict[str, Any]:
    """Module-level convenience: dispatch via the singleton.

    ``response_timeout`` defaults to 30s but can be raised for commands that may
    legitimately block for a long time (e.g. CaptureScreenshot under the human
    approval gate, which waits for the user to click 继续).
    """
    session = await get_session_async()
    return await session.send(command, args, response_timeout)
