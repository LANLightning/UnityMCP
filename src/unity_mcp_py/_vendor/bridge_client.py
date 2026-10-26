"""bridge_client.py — TCP client for the UnityMCP bridge (protocol 1.10.0)."""
from __future__ import annotations

import json
import os
import select
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# Default cap on the in-memory notification backlog. Without a cap the bridge
# can SendNotification(log.line) on every Unity Debug.Log; an idle subscriber-
# less client will grow unbounded over a long PlayMode session. 2000 leaves
# generous headroom for legitimate burst subscribers (collect_notifications)
# while bounding memory.
_DEFAULT_NOTIFICATIONS_MAX = 2000

# Bound the first session round-trip independently of the general 30s timeout.
HANDSHAKE_RESPONSE_TIMEOUT_SECONDS = 5.0


def _configure_keepalive(sock: socket.socket) -> None:
    """Enable bounded TCP liveness detection without assuming one OS API."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option_name, value in (
        ("TCP_KEEPIDLE", 10),
        ("TCP_KEEPINTVL", 3),
        ("TCP_KEEPCNT", 3),
    ):
        option = getattr(socket, option_name, None)
        if option is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass
    # Windows exposes SIO_KEEPALIVE_VALS through ioctl: enabled, idle ms, interval ms.
    ioctl_code = getattr(socket, "SIO_KEEPALIVE_VALS", None)
    if ioctl_code is not None:
        try:
            sock.ioctl(ioctl_code, (1, 10_000, 3_000))
        except OSError:
            pass


class BridgeError(Exception):
    """ok:false response from bridge. Carries .code and .message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class BridgeProtocolError(Exception):
    """Malformed frame, missing required fields, JSON parse error."""


class BridgeConnectionError(Exception):
    """Socket couldn't connect, refused, or closed mid-call."""


class BridgeClient:
    """TCP client for the C# UnityMCP bridge listener. Single-threaded, select-based."""

    _SHORT_POLL_TIMEOUT = 0.05  # seconds; tuning between CPU spin and responsiveness

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 17873,
        schema_path: Path | None = None,
        *,
        client_id: str | None = None,
        client_instance_id: str | None = None,
        expected_bridge_id: str | None = None,
        force_takeover: bool = False,
        auth_token: str | None = None,
        expected_bridge_instance_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id or os.environ.get("UNITY_MCP_CLIENT_ID") or f"mcp-{os.getpid()}"
        self.client_instance_id = client_instance_id or str(uuid.uuid4())
        self.expected_bridge_id = expected_bridge_id
        self.force_takeover = force_takeover
        self.auth_token = auth_token or os.environ.get("UNITY_MCP_AUTH_TOKEN")
        if not self.auth_token:
            from ..bridge.control import load_or_create_auth_token

            self.auth_token = load_or_create_auth_token()
        self.expected_bridge_instance_id = expected_bridge_instance_id
        self.expected_connection_generation = expected_connection_generation
        # Resolve schema_path: explicit arg > user-home shared >
        # UNITY_MCP_SCHEMA_PATH env > None (handshake then uses the pinned
        # fallback). Note: the shared file wins over the env var; delete the
        # shared file to force the env-var path.
        if schema_path:
            self._schema_path = Path(schema_path)
        else:
            shared = (
                Path.home()
                / ".claude"
                / "mcp-servers"
                / "unity-mcp"
                / "shared"
                / "protocol.schema.json"
            )
            # Empty string would make Path("").exists() true (cwd); guard it.
            env_value = os.environ.get("UNITY_MCP_SCHEMA_PATH", "")
            env_schema = Path(env_value) if env_value else None
            if env_schema is not None and env_schema.exists():
                self._schema_path = env_schema
            elif shared.exists():
                self._schema_path = shared
            else:
                self._schema_path = None  # None -> handshake uses pinned fallback (1.8.0)
        self._sock: socket.socket | None = None
        self._read_buf: bytes = b""
        self._responses: dict[str, dict] = {}   # id -> response envelope
        self._notifications: list[dict] = []    # FIFO of notification envelopes
        self._auth_nonce: str | None = None
        self._last_request_id: str | None = None
        # Per-instance cap; honors UNITY_MCP_NOTIFY_MAX env override.
        try:
            self._notifications_max: int = max(
                1, int(os.environ.get("UNITY_MCP_NOTIFY_MAX", _DEFAULT_NOTIFICATIONS_MAX))
            )
        except (TypeError, ValueError):
            self._notifications_max = _DEFAULT_NOTIFICATIONS_MAX

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout_seconds: float = 5.0) -> None:
        """Open TCP socket to the bridge. Idempotent — no-op if already connected."""
        if self._sock is not None:
            return
        s: socket.socket | None = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout_seconds)
            _configure_keepalive(s)
            s.connect((self.host, self.port))
            s.setblocking(False)  # we use select for non-blocking reads
            self._sock = s
            self._authenticate_server(timeout_seconds)
        except Exception as exc:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            if isinstance(exc, (BridgeProtocolError, BridgeError)):
                raise
            raise BridgeConnectionError(
                f"connect to {self.host}:{self.port} failed: {exc}"
            ) from exc

    def close(self) -> None:
        """Close socket idempotently; safe to call even if connect() was never called."""
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._read_buf = b""
        self._responses.clear()
        self._notifications.clear()
        self._auth_nonce = None

    def _authenticate_server(self, timeout_seconds: float) -> None:
        from ..bridge.control import verify_server_proof

        result = self.send("BridgeChallenge", {}, timeout_seconds)
        if not self.auth_token or not self._last_request_id or not verify_server_proof(
            self.auth_token, result, self._last_request_id
        ):
            raise BridgeProtocolError("bridge failed authenticated server challenge")
        self._auth_nonce = str(result["nonce"])

    def __enter__(self) -> "BridgeClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def handshake(self) -> dict:
        """Read protocolVersion from schema, send Handshake, raise on mismatch.

        Returns the result dict from the bridge on success.
        Raises BridgeError('PROTOCOL_VERSION_MISMATCH', ...) on version mismatch.
        The handshake args key is 'clientProtocolVersion' per protocol.schema.json.
        """
        client_version = self._read_protocol_version()
        # Per schema HandshakeArgs: required key is 'clientProtocolVersion'
        result = self.send(
            "Handshake",
            {
                "clientProtocolVersion": client_version,
                "clientId": self.client_id,
                "clientInstanceId": self.client_instance_id,
                "clientProcessId": os.getpid(),
                "expectedBridgeId": self.expected_bridge_id,
                "forceTakeover": self.force_takeover,
                "expectedBridgeInstanceId": self.expected_bridge_instance_id,
                "expectedConnectionGeneration": self.expected_connection_generation,
            },
            response_timeout=HANDSHAKE_RESPONSE_TIMEOUT_SECONDS,
        )
        actual_bridge_id = result.get("bridgeId")
        if self.expected_bridge_id and actual_bridge_id != self.expected_bridge_id:
            raise BridgeProtocolError(
                "connected bridge identity mismatch: "
                f"expected {self.expected_bridge_id!r}, got {actual_bridge_id!r}"
            )
        return result

    def _read_protocol_version(self) -> str:
        """Return protocolVersion from schema file, or '1.10.0' as hardcoded fallback.

        Fallback bumped to 1.8.0 on 2026-06-05: the live bridge ships 1.8.0
        (added non-UI observe/operate: ObserveCamera/ListEntities/ObserveEntity/ListEffects/ObserveEffect/ObserveWindow/OperateCamera + GetVerificationSessionState). Prior 1.7.0 added simulate suite. Prior 1.5.0 added ListUINodes/ResolveLocalization. Cold-start
        machines without a schema file on disk would otherwise handshake with a stale
        version string and trip PROTOCOL_VERSION_MISMATCH. (Prior: 1.3.0→1.4.0 added
        LuaEval/Start+EndVerificationSession; 1.2.0→1.3.0 added GetPlayState/
        GetCompileStatus/LuaReloadModule/CsharpReloadScripts.)
        Fallback bumped to 1.9.0 on 2026-06-12: added optional profiler commands (ProfilerCapabilities/ProfilerSample/MemorySnapshot).
        """
        if self._schema_path is None:
            return "1.10.0"
        try:
            doc = json.loads(self._schema_path.read_text(encoding="utf-8"))
            v = doc.get("protocolVersion") or "1.10.0"
            return str(v)
        except Exception:
            return "1.10.0"

    # ------------------------------------------------------------------
    # Notification backlog trim
    # ------------------------------------------------------------------

    def _trim_notifications(self) -> None:
        """Drop oldest entries when the queue exceeds the per-instance cap.

        Called after every notification append in _drain_once. The cap defaults
        to 2000 but can be overridden via UNITY_MCP_NOTIFY_MAX or by directly
        setting ``_notifications_max`` on the instance (used in tests).
        """
        overflow = len(self._notifications) - self._notifications_max
        if overflow > 0:
            del self._notifications[:overflow]

    # ------------------------------------------------------------------
    # Request/response
    # ------------------------------------------------------------------

    def send(
        self,
        command: str,
        args: dict | None = None,
        response_timeout: float = 30.0,
    ) -> dict:
        """Send a request and block until the matching response arrives.

        Returns the `result` dict on ok=true.
        Raises BridgeError(code, message) on ok=false.
        Raises BridgeProtocolError on malformed JSON or missing required fields.
        Raises BridgeConnectionError on socket EOF mid-call or send failure.
        """
        if self._sock is None:
            raise BridgeConnectionError("send() called before connect()")
        req_id = str(uuid.uuid4())
        self._last_request_id = req_id
        request_args = dict(args or {})
        if command != "BridgeChallenge":
            if not self.auth_token or not self._auth_nonce:
                raise BridgeProtocolError("authenticated bridge challenge was not completed")
            from ..bridge.control import make_client_proof

            request_args["authProof"] = make_client_proof(
                self.auth_token, self._auth_nonce, req_id, command, request_args
            )
        envelope: dict = {"id": req_id, "command": command, "args": request_args}
        line = json.dumps(envelope) + "\n"
        try:
            self._sock.sendall(line.encode("utf-8"))
        except OSError as exc:
            raise BridgeConnectionError(f"send failed: {exc}") from exc

        # Block until the matching response arrives, queueing any notifications.
        deadline = time.monotonic() + response_timeout
        while True:
            if req_id in self._responses:
                resp = self._responses.pop(req_id)
                if resp.get("ok"):
                    return resp.get("result") or {}
                err = resp.get("error") or {}
                raise BridgeError(
                    err.get("code", "UNKNOWN"), err.get("message", "")
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeConnectionError(
                    f"send('{command}') timed out after {response_timeout}s"
                )
            self._drain_once(min(remaining, self._SHORT_POLL_TIMEOUT))

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    def wait_for_notification(
        self,
        event: str,
        timeout_seconds: float,
        predicate: Callable[[dict], bool] | None = None,
    ) -> dict | None:
        """Wait until a matching notification arrives or timeout elapses.

        Drains the internal queue first, then reads from the socket.
        Returns the full notification envelope dict or None on timeout.
        NEVER raises on timeout or socket close.
        """
        # Check pre-existing queue first
        for i, n in enumerate(self._notifications):
            if n.get("event") == event and (predicate is None or predicate(n)):
                self._notifications.pop(i)
                return n

        # Read socket until match or timeout
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            before_len = len(self._notifications)
            try:
                self._drain_once(min(remaining, self._SHORT_POLL_TIMEOUT))
            except BridgeConnectionError:
                return None  # socket closed; honor never-raise contract
            # Check newly arrived notifications
            for i in range(before_len, len(self._notifications)):
                n = self._notifications[i]
                if n.get("event") == event and (predicate is None or predicate(n)):
                    self._notifications.pop(i)
                    return n

    def collect_notifications(
        self, events: list[str], duration_seconds: float
    ) -> list[dict]:
        """Collect all notifications matching any event in `events` for `duration_seconds`.

        Non-matching events remain in the internal queue for later use.
        Never raises on timeout or socket close — returns whatever was collected.
        """
        wanted = set(events)
        collected: list[dict] = []

        # Drain pre-existing queue
        kept: list[dict] = []
        for n in self._notifications:
            if n.get("event") in wanted:
                collected.append(n)
            else:
                kept.append(n)
        self._notifications = kept

        # Read socket for duration
        deadline = time.monotonic() + duration_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return collected
            before_len = len(self._notifications)
            try:
                self._drain_once(min(remaining, self._SHORT_POLL_TIMEOUT))
            except BridgeConnectionError:
                return collected  # socket closed; return partial results
            # Move newly arrived wanted events to collected
            new_kept: list[dict] = []
            for i, n in enumerate(self._notifications):
                if i < before_len:
                    new_kept.append(n)
                    continue
                if n.get("event") in wanted:
                    collected.append(n)
                else:
                    new_kept.append(n)
            self._notifications = new_kept

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _drain_once(self, max_wait: float) -> None:
        """Single select+recv+parse pass.

        Returns when one read pass completes or max_wait elapses.
        Raises BridgeConnectionError if socket closes (EOF).
        Raises BridgeProtocolError if a frame fails JSON parse or is malformed.
        """
        if self._sock is None:
            raise BridgeConnectionError("_drain_once: no socket")
        ready, _, _ = select.select([self._sock], [], [], max_wait)
        if not ready:
            return
        try:
            chunk = self._sock.recv(65536)
        except (OSError, BlockingIOError) as exc:
            raise BridgeConnectionError(f"recv failed: {exc}") from exc
        if not chunk:
            raise BridgeConnectionError("socket closed by peer (EOF)")
        self._read_buf += chunk
        # Split and parse complete newline-terminated frames
        while b"\n" in self._read_buf:
            line, self._read_buf = self._read_buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise BridgeProtocolError(f"malformed JSON frame: {exc}") from exc
            if "id" in frame:
                # Response envelope — correlate by id
                self._responses[frame["id"]] = frame
            elif "event" in frame:
                # Notification envelope — queue for subscribers (capped FIFO)
                self._notifications.append(frame)
                self._trim_notifications()
            else:
                raise BridgeProtocolError(
                    f"frame has neither 'id' nor 'event': {frame!r}"
                )
