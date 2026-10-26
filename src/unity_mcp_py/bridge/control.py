"""Out-of-band bridge pairing, endpoint inspection, diagnosis, and recovery."""
from __future__ import annotations

import ipaddress
import hashlib
import hmac
import json
import os
import platform
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator, Mapping


DEFAULT_BINDINGS_FILE = (
    Path.home() / ".unity-mcp" / "bridge_bindings.json"
)
DEFAULT_AUTH_TOKEN_FILE = Path.home() / ".unity-mcp" / "auth-token"
MAX_CONTROL_RESPONSE_BYTES = 1024 * 1024
MAX_BINDINGS_FILE_BYTES = 1024 * 1024
AUTH_PROOF_FIELDS = (
    "action",
    "clientProtocolVersion",
    "clientId",
    "clientInstanceId",
    "clientProcessId",
    "expectedBridgeId",
    "forceTakeover",
    "expectedBridgeInstanceId",
    "expectedConnectionGeneration",
    "expectedClientId",
    "expectedClientInstanceId",
)


def _proof_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def make_client_proof(
    token: str, nonce: str, request_id: str, command: str, args: Mapping[str, Any]
) -> str:
    parts = ["unity-mcp-client-v1", nonce, request_id, command]
    parts.extend(f"{key}={_proof_value(args.get(key))}" for key in AUTH_PROOF_FIELDS)
    return hmac.new(token.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def verify_server_proof(
    token: str, result: Mapping[str, Any], expected_challenge_id: str
) -> bool:
    nonce = str(result.get("nonce") or "")
    challenge_id = str(result.get("challengeRequestId") or "")
    payload = "\n".join(
        [
            "unity-mcp-server-v1",
            challenge_id,
            nonce,
            str(result.get("bridgeId") or ""),
            str(result.get("bridgeInstanceId") or ""),
        ]
    )
    expected = hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return (
        bool(nonce)
        and challenge_id == expected_challenge_id
        and hmac.compare_digest(expected, str(result.get("serverProof") or ""))
    )


def resolve_bindings_file() -> Path:
    configured = os.environ.get("UNITY_MCP_BINDINGS_FILE")
    return Path(configured) if configured else DEFAULT_BINDINGS_FILE


def load_or_create_auth_token() -> str:
    configured = os.environ.get("UNITY_MCP_AUTH_TOKEN")
    if configured:
        if len(configured) < 32:
            raise ValueError("UNITY_MCP_AUTH_TOKEN must contain at least 32 characters")
        return configured

    path = DEFAULT_AUTH_TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return token
    last_error: Exception | None = None
    for _ in range(20):
        try:
            token = path.read_text(encoding="ascii").strip()
            if len(token) >= 32:
                return token
            last_error = ValueError(f"invalid UnityMCP auth token file: {path}")
        except OSError as exc:
            last_error = exc
        time.sleep(0.025)
    raise ValueError(f"unable to read a complete UnityMCP auth token file: {path}") from last_error


@dataclass(frozen=True)
class BridgeBinding:
    client_id: str
    host: str
    port: int
    expected_bridge_id: str | None = None
    updated_at: str = ""
    revision: str = ""


class BridgeBindingStore:
    """Persistent client-id to bridge-endpoint bindings with atomic updates."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or resolve_bindings_file()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def list(self) -> list[BridgeBinding]:
        with _file_lock(self.lock_path):
            records = self._load()
        return [records[key] for key in sorted(records)]

    def get(self, client_id: str) -> BridgeBinding | None:
        with _file_lock(self.lock_path):
            return self._load().get(client_id)

    def pair(
        self,
        client_id: str,
        host: str,
        port: int,
        expected_bridge_id: str | None,
    ) -> BridgeBinding:
        client_id = _validate_identifier(client_id, "client_id")
        _validate_loopback(host)
        _validate_port(port)
        if expected_bridge_id is not None:
            expected_bridge_id = _validate_identifier(
                expected_bridge_id, "expected_bridge_id"
            )

        record = BridgeBinding(
            client_id=client_id,
            host=host,
            port=port,
            expected_bridge_id=expected_bridge_id,
            updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            revision=uuid.uuid4().hex,
        )
        with _file_lock(self.lock_path):
            records = self._load()
            self._save({**records, client_id: record})
        return record

    def clear(self, client_id: str) -> bool:
        with _file_lock(self.lock_path):
            records = self._load()
            if client_id not in records:
                return False
            self._save({key: value for key, value in records.items() if key != client_id})
            return True

    def clear_if_current(self, client_id: str, expected_revision: str) -> bool:
        """Clear only the binding revision observed by the caller."""
        with _file_lock(self.lock_path):
            records = self._load()
            current = records.get(client_id)
            if current is None or current.revision != expected_revision:
                return False
            self._save({key: value for key, value in records.items() if key != client_id})
            return True

    def restore_if_current(
        self,
        client_id: str,
        expected_revision: str,
        previous: BridgeBinding | None,
    ) -> bool:
        """Rollback only the exact record written by this transaction."""
        with _file_lock(self.lock_path):
            records = self._load()
            current = records.get(client_id)
            if current is None or current.revision != expected_revision:
                return False
            replacement = {key: value for key, value in records.items() if key != client_id}
            if previous is not None:
                replacement[client_id] = previous
            self._save(replacement)
            return True

    def matches_revision(self, client_id: str, expected_revision: str) -> bool:
        """Return whether the exact binding revision is still current."""
        with _file_lock(self.lock_path):
            current = self._load().get(client_id)
            return current is not None and current.revision == expected_revision

    def restore_if_absent(
        self, client_id: str, previous: BridgeBinding
    ) -> bool:
        """Restore a cleared record only when no newer binding exists."""
        with _file_lock(self.lock_path):
            records = self._load()
            if client_id in records:
                return False
            if previous.client_id != client_id:
                raise ValueError("previous binding belongs to a different client")
            self._save({**records, client_id: previous})
            return True

    def _load(self) -> dict[str, BridgeBinding]:
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > MAX_BINDINGS_FILE_BYTES:
                raise ValueError("binding file exceeds size limit")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("binding file root must be an object")
            raw_records = payload.get("bindings", payload)
            if not isinstance(raw_records, dict):
                raise ValueError("bindings must be an object")
            records: dict[str, BridgeBinding] = {}
            for key, value in raw_records.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise ValueError("binding entries must map string IDs to objects")
                record = BridgeBinding(
                    client_id=str(value.get("client_id", key)),
                    host=str(value["host"]),
                    port=int(value["port"]),
                    expected_bridge_id=value.get("expected_bridge_id"),
                    updated_at=str(value.get("updated_at", "")),
                    revision=str(value.get("revision", "")),
                )
                if not record.client_id.strip() or len(record.client_id) > 256:
                    raise ValueError("invalid client_id")
                _validate_loopback(record.host)
                _validate_port(record.port)
                records[key] = record
            return records
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f"invalid bridge binding file {self.path}: {exc}") from exc

    def _save(self, records: Mapping[str, BridgeBinding]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "bindings": {key: asdict(value) for key, value in records.items()},
        }
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class EndpointConnection:
    local_address: str
    local_port: int
    remote_address: str
    remote_port: int
    state: str
    owner_pid: int | None
    process_name: str | None = None
    process_exists: bool | None = None


@dataclass(frozen=True)
class EndpointSnapshot:
    host: str
    port: int
    listener_pid: int | None = None
    listener_process_name: str | None = None
    listener_process_exists: bool | None = None
    listener_connections: tuple[EndpointConnection, ...] = field(default_factory=tuple)
    client_connections: tuple[EndpointConnection, ...] = field(default_factory=tuple)
    inspection_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_windows_tcp_rows(
    rows: Iterable[Mapping[str, Any]], host: str, port: int
) -> EndpointSnapshot:
    target_address = ipaddress.ip_address(host)
    listener_pid: int | None = None
    listener_name: str | None = None
    listener_exists: bool | None = None
    listener_connections: list[EndpointConnection] = []
    client_connections: list[EndpointConnection] = []

    for row in rows:
        local_port = _safe_int(row.get("LocalPort"))
        remote_port = _safe_int(row.get("RemotePort"))
        state = str(row.get("State") or "Unknown")
        owner_pid = _safe_int(row.get("OwningProcess"), allow_zero=False)
        connection = EndpointConnection(
            local_address=str(row.get("LocalAddress") or ""),
            local_port=local_port or 0,
            remote_address=str(row.get("RemoteAddress") or ""),
            remote_port=remote_port or 0,
            state=state,
            owner_pid=owner_pid,
            process_name=_clean_optional(row.get("ProcessName")),
            process_exists=_safe_bool(row.get("ProcessExists")),
        )
        local_matches = _address_matches(row.get("LocalAddress"), target_address)
        remote_matches = _address_matches(row.get("RemoteAddress"), target_address)
        if local_port == port and local_matches and state.lower() in {"listen", "listening"}:
            listener_pid = owner_pid
            listener_name = connection.process_name
            listener_exists = connection.process_exists
        elif local_port == port and local_matches and _is_loopback_literal(row.get("RemoteAddress")):
            listener_connections.append(connection)
        elif remote_port == port and remote_matches and _is_loopback_literal(row.get("LocalAddress")):
            client_connections.append(connection)

    return EndpointSnapshot(
        host=host,
        port=port,
        listener_pid=listener_pid,
        listener_process_name=listener_name,
        listener_process_exists=listener_exists,
        listener_connections=tuple(listener_connections),
        client_connections=tuple(client_connections),
    )


def inspect_endpoint(
    host: str, port: int, *, timeout_seconds: float = 5.0
) -> EndpointSnapshot:
    _validate_loopback(host)
    _validate_port(port)
    if not 0 < timeout_seconds <= 5:
        raise ValueError("timeout_seconds must be in (0, 5]")
    if platform.system() != "Windows":
        return _inspect_with_psutil(host, port)

    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        "$rows = Get-NetTCPConnection -ErrorAction Stop | "
        "Where-Object { $_.LocalPort -eq " + str(port) +
        " -or $_.RemotePort -eq " + str(port) + " }; "
        "$rows | ForEach-Object { $name = $null; $exists = $null; "
        "try { $process = Get-Process -Id $_.OwningProcess -ErrorAction Stop; "
        "$exists = $true; try { $name = $process.ProcessName } catch {} } "
        "catch [Microsoft.PowerShell.Commands.ProcessCommandException] { $exists = $false } "
        "catch { $exists = $null }; "
        "[pscustomobject]@{ LocalAddress=$_.LocalAddress; LocalPort=$_.LocalPort; "
        "RemoteAddress=$_.RemoteAddress; RemotePort=$_.RemotePort; State=[string]$_.State; "
        "OwningProcess=$_.OwningProcess; ProcessName=$name; ProcessExists=$exists } } | ConvertTo-Json -Compress"
    )
    try:
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
        raw = completed.stdout.strip()
        if not raw:
            rows: list[Mapping[str, Any]] = []
        else:
            decoded = json.loads(raw)
            rows = decoded if isinstance(decoded, list) else [decoded]
        return parse_windows_tcp_rows(rows, host, port)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return EndpointSnapshot(host, port, inspection_error=str(exc))


class BridgeControlClient:
    """Short-lived management connection that never becomes the business client."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout_seconds: float = 2.0,
        *,
        auth_token: str | None = None,
    ) -> None:
        _validate_loopback(host)
        _validate_port(port)
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.auth_token = auth_token or load_or_create_auth_token()

    def request(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout_seconds
            ) as connection:
                connection.settimeout(self.timeout_seconds)
                challenge_id = str(uuid.uuid4())
                challenge = {
                    "id": challenge_id,
                    "command": "BridgeChallenge",
                    "args": {},
                }
                connection.sendall((json.dumps(challenge) + "\n").encode("utf-8"))
                challenge_response = _decode_control_response(
                    _receive_line(connection), challenge_id
                )
                challenge_result = challenge_response.get("result") or {}
                if not challenge_response.get("ok") or not verify_server_proof(
                    self.auth_token, challenge_result, challenge_id
                ):
                    raise ValueError("bridge failed authenticated server challenge")

                request_id = str(uuid.uuid4())
                request_args = {"action": action, **(args or {})}
                request_args["authProof"] = make_client_proof(
                    self.auth_token,
                    str(challenge_result["nonce"]),
                    request_id,
                    "BridgeControl",
                    request_args,
                )
                envelope = {
                    "id": request_id,
                    "command": "BridgeControl",
                    "args": request_args,
                }
                connection.sendall((json.dumps(envelope) + "\n").encode("utf-8"))
                response = _receive_line(connection)
        except (OSError, TimeoutError, ValueError) as exc:
            return {
                "responsive": False,
                "action": action,
                "error": str(exc),
                "errorType": type(exc).__name__,
            }

        try:
            decoded = _decode_control_response(response, request_id)
        except (UnicodeDecodeError, ValueError) as exc:
            return {
                "responsive": False,
                "action": action,
                "error": f"invalid control response: {exc}",
                "errorType": "protocol_error",
            }
        if not decoded.get("ok"):
            error = decoded.get("error") or {}
            return {
                "responsive": True,
                "success": False,
                "action": action,
                "error": error.get("message", "control request failed"),
                "errorCode": error.get("code", "UNKNOWN"),
            }
        return {
            "responsive": True,
            "success": True,
            "action": action,
            **(decoded.get("result") or {}),
        }

    def status(self) -> dict[str, Any]:
        return self.request("status")

    def disconnect_receiver(
        self,
        *,
        expected_bridge_id: str,
        expected_bridge_instance_id: str,
        expected_generation: int,
        expected_client_id: str | None = None,
        expected_client_instance_id: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "expectedBridgeId": expected_bridge_id,
            "expectedBridgeInstanceId": expected_bridge_instance_id,
            "expectedConnectionGeneration": expected_generation,
            **({"expectedClientId": expected_client_id} if expected_client_id else {}),
            **(
                {"expectedClientInstanceId": expected_client_instance_id}
                if expected_client_instance_id
                else {}
            ),
        }
        return self.request("disconnect_client", args)

    def clear_listener(self, expected_bridge_id: str | None = None) -> dict[str, Any]:
        if not expected_bridge_id:
            return {
                "responsive": True,
                "success": False,
                "action": "restart_listener",
                "errorCode": "EXPECTED_ID_REQUIRED",
                "error": "expected bridge identity is required",
            }
        status = self.status()
        if not status.get("success"):
            return status
        if status.get("bridgeId") != expected_bridge_id:
            return {
                "responsive": True,
                "success": False,
                "action": "restart_listener",
                "errorCode": "STALE_TARGET",
                "error": "bridge identity changed before listener restart",
            }
        return self.request(
            "restart_listener",
            {
                "expectedBridgeId": expected_bridge_id,
                "expectedBridgeInstanceId": status.get("bridgeInstanceId"),
                "expectedConnectionGeneration": status.get("connectionGeneration"),
                **(
                    {
                        "expectedClientId": status.get("clientId"),
                        "expectedClientInstanceId": status.get("clientInstanceId"),
                    }
                    if status.get("clientConnected")
                    else {}
                ),
            },
        )


def _decode_control_response(response: bytes, request_id: str) -> dict[str, Any]:
    decoded = json.loads(response.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("control response root is not an object")
    if decoded.get("id") != request_id:
        raise ValueError("control response id mismatch")
    return decoded

@dataclass(frozen=True)
class EndpointDiagnosis:
    reason: str
    summary: str
    auto_repairable: bool
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def diagnose_endpoint(
    snapshot: EndpointSnapshot, control: Mapping[str, Any] | None
) -> EndpointDiagnosis:
    evidence = {"socket": snapshot.to_dict(), "control": dict(control or {})}
    control_healthy = bool(
        control and control.get("responsive") and control.get("success")
    )
    if snapshot.listener_process_exists is False and not control_healthy:
        return EndpointDiagnosis(
            "orphan_listener",
            "Windows still reports a listener whose owning process has already exited; restarting the current Unity process will not release it.",
            False,
            evidence,
        )
    if control and control.get("wedgeActive"):
        return EndpointDiagnosis(
            "unity_main_thread_wedged",
            "Unity's watchdog reports a command wedged on the Editor main thread.",
            False,
            evidence,
        )
    if not control or not control.get("responsive"):
        if snapshot.listener_pid is None and not snapshot.inspection_error:
            return EndpointDiagnosis(
                "listener_missing", "No process is listening on the requested endpoint.", False, evidence
            )
        if snapshot.inspection_error:
            return EndpointDiagnosis(
                "inspection_failed",
                "Neither OS endpoint inspection nor the bridge control probe produced reliable state.",
                False,
                evidence,
            )
        if snapshot.listener_process_exists is not True:
            return EndpointDiagnosis(
                "listener_owner_unknown",
                "The listener did not respond and its owning process could not be verified; Unity main-thread failure is not proven.",
                False,
                evidence,
            )
        if snapshot.listener_process_name and "unity" not in snapshot.listener_process_name.lower():
            return EndpointDiagnosis(
                "non_bridge_listener",
                "A non-Unity process owns the requested listener.",
                False,
                evidence,
            )
        return EndpointDiagnosis(
            "bridge_main_thread_unresponsive",
            "The TCP listener exists, but Unity did not process the control request in time.",
            False,
            evidence,
        )
    if not control.get("success"):
        return EndpointDiagnosis(
            "control_protocol_error",
            "The endpoint responded, but did not accept the BridgeControl protocol.",
            False,
            evidence,
        )
    if control.get("clientConnected"):
        return EndpointDiagnosis(
            "occupied",
            "The bridge is responsive and currently has an accepted MCP client.",
            True,
            evidence,
        )
    return EndpointDiagnosis(
        "healthy_idle", "The bridge is responsive and has no accepted MCP client.", True, evidence
    )


def diagnose_live_endpoint(host: str, port: int) -> EndpointDiagnosis:
    snapshot = inspect_endpoint(host, port)
    control = BridgeControlClient(host, port).status()
    if not control.get("success"):
        wedge = read_wedge_evidence(port, snapshot.listener_pid)
        if wedge is not None:
            control = {**control, **wedge, "wedgeActive": True}
    return diagnose_endpoint(snapshot, control)


def read_wedge_evidence(port: int, listener_pid: int | None) -> dict[str, Any] | None:
    path = Path(tempfile.gettempdir()) / "unity-mcp" / f"bridge-{port}-wedge.json"
    try:
        if not path.exists() or path.stat().st_size > 65536:
            return None
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            return None
        evidence_pid = _safe_int(payload.get("listenerPid"), allow_zero=False)
        if listener_pid is None or evidence_pid != listener_pid:
            return None
        timestamp = datetime.fromisoformat(str(payload.get("timestamp", "")).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if abs((datetime.now(timezone.utc) - timestamp).total_seconds()) > 600:
            return None
        proof_payload = "\n".join(
            [
                "unity-mcp-wedge-v1",
                str(payload.get("bridgeId") or ""),
                str(payload.get("bridgeInstanceId") or ""),
                str(evidence_pid),
                str(payload.get("port") or ""),
                str(payload.get("connectionGeneration") or 0),
                str(payload.get("requestId") or ""),
                str(payload.get("command") or ""),
                str(payload.get("timestamp") or ""),
            ]
        )
        expected_proof = hmac.new(
            load_or_create_auth_token().encode(),
            proof_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_proof, str(payload.get("proof") or "")):
            return None
        return payload
    except (OSError, ValueError, TypeError):
        return None


def repair_endpoint(
    host: str,
    port: int,
    *,
    client_id: str | None = None,
    expected_bridge_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    before = diagnose_live_endpoint(host, port)
    actions: list[dict[str, Any]] = []
    if before.reason == "healthy_idle":
        return {"repaired": True, "changed": False, "before": before.to_dict(), "actions": actions}
    if before.reason == "orphan_listener":
        return {
            "repaired": False,
            "changed": False,
            "manualRestartRequired": False,
            "fallbackScanRecommended": True,
            "recommendedPorts": list(range(port + 1, min(port + 11, 65536))),
            "before": before.to_dict(),
            "actions": actions,
        }
    if before.reason == "listener_owner_unknown":
        return {
            "repaired": False,
            "changed": False,
            "manualRestartRequired": False,
            "refused": "listener_owner_unknown",
            "before": before.to_dict(),
            "actions": actions,
        }
    if before.reason in {"listener_missing", "bridge_main_thread_unresponsive", "unity_main_thread_wedged"}:
        return {
            "repaired": False,
            "changed": False,
            "manualRestartRequired": True,
            "before": before.to_dict(),
            "actions": actions,
        }

    control = BridgeControlClient(host, port)
    actual_bridge_id = before.evidence.get("control", {}).get("bridgeId")
    if expected_bridge_id and actual_bridge_id != expected_bridge_id:
        return {
            "repaired": False,
            "changed": False,
            "refused": "bridge_identity_mismatch",
            "expectedBridgeId": expected_bridge_id,
            "actualBridgeId": actual_bridge_id,
            "before": before.to_dict(),
            "actions": actions,
        }
    owner = before.evidence.get("control", {}).get("clientId")
    if owner and not expected_bridge_id:
        return {
            "repaired": False,
            "changed": False,
            "refused": "bridge_identity_required",
            "before": before.to_dict(),
            "actions": actions,
        }
    if owner and not client_id:
        return {
            "repaired": False,
            "changed": False,
            "refused": "client_identity_required",
            "before": before.to_dict(),
            "actions": actions,
        }
    if owner and client_id and owner != client_id and not force:
        return {
            "repaired": False,
            "changed": False,
            "refused": "active_foreign_owner",
            "before": before.to_dict(),
            "actions": actions,
        }
    control_state = before.evidence.get("control", {})
    result = control.disconnect_receiver(
        expected_bridge_id=str(control_state.get("bridgeId")),
        expected_bridge_instance_id=str(control_state.get("bridgeInstanceId")),
        expected_generation=int(control_state.get("connectionGeneration", -1)),
        expected_client_id=owner or client_id,
        expected_client_instance_id=control_state.get("clientInstanceId"),
    )
    actions.append(result)
    if not result.get("success"):
        return {"repaired": False, "changed": False, "before": before.to_dict(), "actions": actions}

    time.sleep(0.1)
    after = diagnose_live_endpoint(host, port)
    return {
        "repaired": after.reason == "healthy_idle",
        "changed": True,
        "before": before.to_dict(),
        "after": after.to_dict(),
        "actions": actions,
    }


def _receive_line(connection: socket.socket) -> bytes:
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            raise ConnectionError("bridge closed the control connection")
        data.extend(chunk)
        if len(data) > MAX_CONTROL_RESPONSE_BYTES:
            raise ValueError("control response exceeds size limit")
    return bytes(data).split(b"\n", 1)[0]


def _inspect_with_psutil(host: str, port: int) -> EndpointSnapshot:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return EndpointSnapshot(host, port, inspection_error="psutil is required off Windows")
    rows: list[dict[str, Any]] = []
    try:
        for conn in psutil.net_connections(kind="tcp"):
            local = conn.laddr or ("", 0)
            remote = conn.raddr or ("", 0)
            if local.port != port and (not remote or remote.port != port):
                continue
            process_exists: bool | None = None
            try:
                if conn.pid:
                    process_name = psutil.Process(conn.pid).name()
                    process_exists = True
                else:
                    process_name = None
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                process_name = None
                process_exists = False
            except (psutil.AccessDenied, OSError):
                process_name = None
            rows.append(
                {
                    "LocalAddress": local.ip,
                    "LocalPort": local.port,
                    "RemoteAddress": remote.ip if remote else "",
                    "RemotePort": remote.port if remote else 0,
                    "State": conn.status,
                    "OwningProcess": conn.pid,
                    "ProcessName": process_name,
                    "ProcessExists": process_exists,
                }
            )
        return parse_windows_tcp_rows(rows, host, port)
    except (psutil.Error, OSError) as exc:
        return EndpointSnapshot(host, port, inspection_error=str(exc))


def _validate_loopback(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"host must be a literal loopback address: {host!r}") from exc
    if not address.is_loopback:
        raise ValueError(f"host must be a literal loopback address: {host!r}")


def _validate_port(port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"port must be an integer in 1..65535, got {port!r}")


def _validate_identifier(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError(f"{field} must contain 1..256 non-whitespace characters")
    return normalized


def _safe_int(value: Any, *, allow_zero: bool = True) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0 and not allow_zero:
        return None
    return parsed


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "false"}:
            return normalized == "true"
    return None


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _address_matches(value: Any, target: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0]) == target
    except ValueError:
        return False


def _is_loopback_literal(value: Any) -> bool:
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        _lock_stream(stream)
        try:
            yield
        finally:
            _unlock_stream(stream)


def _lock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
