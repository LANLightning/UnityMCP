"""MCP tool-call recorder and replay engine.

Records every tool invocation (except blacklisted ones) to a JSONL session file
when recording is active. The wrap() helper is installed via a monkey-patch on
mcp.tool in server.register_tools(); see server.py for the wiring.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

SERVER_VERSION = "0.1.0"

BLACKLIST: frozenset[str] = frozenset({
    "record_start",
    "record_stop",
    "list_sessions",
    "replay",
    "observe_evidence",
    # Administrative mutations are intentionally never replayable. A recorded
    # clear/repair could otherwise target a different future connection after
    # the command refreshes its CAS evidence.
    "bridge_admin",
})

_DEFAULT_ELISION_BYTES = 65536
_HEADER_TYPE = "header"


def _elision_threshold() -> int:
    raw = os.environ.get("UNITY_MCP_RECORD_ELISION_BYTES")
    if not raw:
        return _DEFAULT_ELISION_BYTES
    try:
        return max(1024, int(raw))
    except ValueError:
        return _DEFAULT_ELISION_BYTES


def sessions_dir() -> Path:
    """Resolve the session storage directory.

    Honors UNITY_MCP_SESSIONS_DIR; otherwise %LOCALAPPDATA% on Windows,
    ~/.unity-mcp-py/sessions elsewhere.
    """
    override = os.environ.get("UNITY_MCP_SESSIONS_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "unity-mcp-py" / "sessions"
    return Path.home() / ".unity-mcp-py" / "sessions"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _elide_large_strings(obj: Any, threshold: int) -> Any:
    """Recursively replace large str values with elision markers.

    Only acts on dict values (keyed strings); list strings are left intact for
    cheap call sites. Recording results are always dicts in this codebase.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and len(v) >= threshold:
                digest = hashlib.sha256(v.encode("utf-8", "replace")).hexdigest()[:16]
                out[k] = {"_elided": k, "len": len(v), "sha256": digest}
            elif isinstance(v, (dict, list)):
                out[k] = _elide_large_strings(v, threshold)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_elide_large_strings(x, threshold) for x in obj]
    return obj


def _json_safe(obj: Any) -> Any:
    """Coerce non-JSON-serializable shapes to JSON-safe ones.

    PERF 2026-05-25: previous implementation did ``try: json.dumps(obj)`` to
    *probe* serializability, which serialized the entire payload once before
    the real ``json.dumps`` call ever ran. For a 1.2 MB screenshot payload
    that doubled the CPU cost of every recorded entry. We now walk the tree
    once, only stringifying dict keys (which json.dumps refuses outright);
    unknown scalar types fall through to ``default=str`` at the dump site.
    """
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    return obj


class Recorder:
    """Single-active-session recorder. Process-global by design."""

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._path: Path | None = None
        self._started_at: str | None = None
        self._line_count: int = 0
        self._lock = asyncio.Lock()
        # PERF 2026-05-25: session file handle held across the whole recording
        # session so each _append is a single write() instead of open+write+close.
        self._fh: Any = None
        # virtual console cursor lives here too — single shared mutable state.
        self._virtual_cursor: int = 0

    # ----- console cursor -----

    def get_cursor(self) -> int:
        return self._virtual_cursor

    def set_cursor(self, value: int) -> None:
        self._virtual_cursor = max(0, int(value))

    # ----- session lifecycle -----

    @property
    def active(self) -> bool:
        return self._session_id is not None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def line_count(self) -> int:
        return self._line_count

    @property
    def started_at(self) -> str | None:
        return self._started_at

    async def start(self, session_id: str | None, note: str) -> dict[str, Any]:
        async with self._lock:
            if self._session_id is not None:
                raise RuntimeError(
                    f"recording already active: session_id={self._session_id}"
                )
            sid = session_id or self._gen_id()
            directory = sessions_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{sid}.jsonl"
            if path.exists():
                raise RuntimeError(f"session file already exists: {path}")
            started_at = _now_iso()
            header = {
                "type": _HEADER_TYPE,
                "session_id": sid,
                "started_at": started_at,
                "note": note,
                "server_version": SERVER_VERSION,
            }
            # Write header then keep the handle open for body appends.
            fh = path.open("w", encoding="utf-8")
            fh.write(json.dumps(header) + "\n")
            fh.flush()
            self._session_id = sid
            self._path = path
            self._started_at = started_at
            self._line_count = 0
            self._fh = fh
            return {
                "session_id": sid,
                "path": str(path),
                "started_at": started_at,
            }

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            if self._session_id is None:
                raise RuntimeError("no active recording session")
            result = {
                "session_id": self._session_id,
                "path": str(self._path),
                "line_count": self._line_count,
            }
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
            self._fh = None
            self._session_id = None
            self._path = None
            self._started_at = None
            self._line_count = 0
            return result

    async def _append(self, entry: dict[str, Any]) -> None:
        # Hold the lock for file I/O so concurrent tool calls serialize cleanly.
        async with self._lock:
            if self._fh is None:
                return
            # default=str catches any non-JSON scalar that slipped past
            # _json_safe (e.g. custom objects in args/result).
            line = json.dumps(
                _json_safe(entry), ensure_ascii=False, default=str
            )
            self._fh.write(line + "\n")
            self._fh.flush()
            self._line_count += 1

    # ----- tool wrapper -----

    def wrap(self, tool_name: str, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if tool_name in BLACKLIST:
            return fn

        threshold = _elision_threshold()

        @functools.wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            if not self.active:
                return await fn(*args, **kwargs)
            t0 = time.monotonic()
            entry: dict[str, Any] = {
                "ts": _now_iso(),
                "tool": tool_name,
                "args": _json_safe(kwargs if kwargs else (list(args) if args else {})),
            }
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = repr(exc)
                entry["dur_ms"] = int((time.monotonic() - t0) * 1000)
                try:
                    await self._append(entry)
                except Exception:  # noqa: BLE001
                    logger.exception("recorder: failed to append error entry")
                raise
            entry["ok"] = True
            entry["result"] = _elide_large_strings(_json_safe(result), threshold)
            entry["dur_ms"] = int((time.monotonic() - t0) * 1000)
            try:
                await self._append(entry)
            except Exception:  # noqa: BLE001
                logger.exception("recorder: failed to append entry")
            return result

        return _wrapped

    # ----- helpers -----

    @staticmethod
    def _gen_id() -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{ts}-{secrets.token_hex(3)}"


# Process-wide singleton.
RECORDER = Recorder()


# Replay engine -------------------------------------------------------------

class _Registry:
    """Lookup of registered tool functions for replay."""

    def __init__(self) -> None:
        self._funcs: dict[str, Callable[..., Awaitable[Any]]] = {}

    def register(self, name: str, fn: Callable[..., Awaitable[Any]]) -> None:
        self._funcs[name] = fn

    def get(self, name: str) -> Callable[..., Awaitable[Any]] | None:
        return self._funcs.get(name)

    def clear(self) -> None:
        """Drop all registered tool fns (test isolation between register_tools() runs)."""
        self._funcs.clear()


REGISTRY = _Registry()


async def run_replay(
    session_id: str,
    dry_run: bool = False,
    skip_tools: list[str] | None = None,
    stop_on_error: bool = True,
) -> dict[str, Any]:
    """Read a recorded JSONL file and re-invoke each tool sequentially."""
    if skip_tools is None:
        skip_tools = ["human_collab_gate"]
    skip_set = set(skip_tools) | BLACKLIST

    path = sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"session not found: {path}")

    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == _HEADER_TYPE:
                continue
            entries.append(obj)

    total = len(entries)
    executed = 0
    skipped = 0
    diffs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for idx, entry in enumerate(entries):
        tool = entry.get("tool", "")
        args = entry.get("args") or {}
        if tool in skip_set:
            skipped += 1
            continue
        fn = REGISTRY.get(tool)
        if fn is None:
            errors.append({"idx": idx, "tool": tool, "error": "not_registered"})
            if stop_on_error:
                break
            continue
        if dry_run:
            executed += 1
            continue
        call_kwargs = args if isinstance(args, dict) else {}
        try:
            actual = await fn(**call_kwargs)
        except Exception as exc:
            errors.append({"idx": idx, "tool": tool, "error": repr(exc)})
            if stop_on_error:
                break
            continue
        executed += 1
        recorded_result = entry.get("result")
        if isinstance(recorded_result, dict) and isinstance(actual, dict):
            rk = sorted(recorded_result.keys())
            ak = sorted(actual.keys())
            if rk != ak:
                diffs.append({
                    "idx": idx,
                    "tool": tool,
                    "recorded_keys": rk,
                    "actual_keys": ak,
                })

    return {
        "total": total,
        "executed": executed,
        "skipped": skipped,
        "diffs": diffs,
        "errors": errors,
    }


def list_session_files(limit: int) -> list[dict[str, Any]]:
    """Enumerate session files newest-first with cheap header parse."""
    directory = sessions_dir()
    if not directory.exists():
        return []
    files = sorted(
        directory.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(0, limit)]
    out: list[dict[str, Any]] = []
    for p in files:
        try:
            with p.open("r", encoding="utf-8") as f:
                first = f.readline()
                body_count = sum(1 for _ in f)
        except OSError:
            continue
        header: dict[str, Any] = {}
        try:
            header = json.loads(first) if first else {}
        except json.JSONDecodeError:
            header = {}
        out.append({
            "session_id": header.get("session_id", p.stem),
            "started_at": header.get("started_at", ""),
            "note": header.get("note", ""),
            "line_count": body_count,
            "path": str(p),
            "size_bytes": p.stat().st_size,
        })
    return out
