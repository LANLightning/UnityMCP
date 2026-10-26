"""Always-on per-tool telemetry sink (Phase 0).

Distinct from ``recorder.py``: the recorder only writes while a ``record_start``
session is active and blacklists chatty tools (``observe_evidence``). Telemetry
instead fires on EVERY tool call so we can measure where per-turn cost actually
goes — wall-clock, response payload bytes, a cheap token estimate (bytes // 4),
and the number of bridge round-trips the tool issued.

The bridge round-trip count is collected via a ``contextvar`` ticked once per
``BridgeSession.send`` (see bridge/session.py). Because each tool call runs in
its own async task, the counter is reset per-call by ``Telemetry.wrap`` and read
back when the call returns. This measures SERVER wall-clock + payload, NOT the
client-side model read/think time — pair it with one client-side per-turn
timestamp to fully confirm the model-bound bottleneck.

Enable/disable + path are env-controlled:
  - ``UNITY_MCP_TELEMETRY=0`` disables (default enabled).
  - ``UNITY_MCP_TELEMETRY_PATH`` overrides the JSONL sidecar path.
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Ticked once per bridge round-trip inside the current async task. Reset to 0 by
# Telemetry.wrap at the start of each tool call.
_bridge_rtt: contextvars.ContextVar[int] = contextvars.ContextVar(
    "unity_mcp_bridge_rtt", default=0
)


def bridge_rtt_tick() -> None:
    """Increment the per-call bridge round-trip counter by one.

    Called from BridgeSession.send. Safe to call outside a tool context — the
    contextvar simply carries the count nobody reads.
    """
    _bridge_rtt.set(_bridge_rtt.get() + 1)


def _reset_bridge_rtt() -> None:
    _bridge_rtt.set(0)


def _read_bridge_rtt() -> int:
    return _bridge_rtt.get()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _default_path() -> Path:
    override = os.environ.get("UNITY_MCP_TELEMETRY_PATH")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "unity-mcp-py" / "telemetry.jsonl"
    return Path.home() / ".unity-mcp-py" / "telemetry.jsonl"


def _default_enabled() -> bool:
    return os.environ.get("UNITY_MCP_TELEMETRY", "1") != "0"


def _resp_bytes(result: Any) -> int:
    """Serialized size of a tool result in UTF-8 bytes; 0 if unserializable."""
    try:
        return len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0


class Telemetry:
    """Wraps a tool coroutine to append one JSONL line per invocation."""

    def __init__(self, path: Path | None = None, enabled: bool | None = None) -> None:
        self.path = Path(path) if path is not None else _default_path()
        self.enabled = _default_enabled() if enabled is None else enabled

    def _append(self, entry: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.exception("telemetry: failed to append entry")

    def wrap(
        self, tool_name: str, fn: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        if not self.enabled:
            return fn

        @functools.wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            _reset_bridge_rtt()
            t0 = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                self._append({
                    "ts": _now_iso(),
                    "tool": tool_name,
                    "ok": False,
                    "error": repr(exc),
                    "dur_ms": int((time.monotonic() - t0) * 1000),
                    "bridge_rtts": _read_bridge_rtt(),
                    "resp_bytes": 0,
                    "token_est": 0,
                })
                raise
            rb = _resp_bytes(result)
            self._append({
                "ts": _now_iso(),
                "tool": tool_name,
                "ok": True,
                "dur_ms": int((time.monotonic() - t0) * 1000),
                "bridge_rtts": _read_bridge_rtt(),
                "resp_bytes": rb,
                "token_est": rb // 4,
            })
            return result

        return _wrapped


# Process-wide singleton, mirroring RECORDER.
TELEMETRY = Telemetry()
