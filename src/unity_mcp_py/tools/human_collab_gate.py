"""human_collab_gate tool. Mode B wrapper. Self-contained — no token.

Wraps ~/.claude/flow/lib/human_collab.py HumanCollabGate (sync prepare/analyze)
as a single async MCP tool using asyncio.to_thread.

See spec §5.3 for the MCP response shape.

MEDIUM fix (2026-05-13): the previous implementation echoed the full
HumanCollabGate ``baseline`` dict back to the LLM as ``_baseline_context``,
which exposed absolute filesystem paths (screenshot file paths, log dir paths)
into the LLM's context window. We now keep baselines in a server-side dict
keyed by ``_wu_id`` and only return the opaque id. The orchestrator passes
the id back on the second call; we look up the baseline locally.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..bridge import human_collab
from ..bridge.session import get_session_async

logger = logging.getLogger(__name__)

TOOL_NAME = "human_collab_gate"

# Server-side baseline store: {wu_id: (baseline_dict, created_at_unix)}
# Keyed by uuid; entries auto-expire after 1 hour to bound memory.
_BASELINE_TTL_SECONDS = 3600
_baseline_store: dict[str, tuple[dict[str, Any], float]] = {}
_baseline_lock = threading.Lock()


def _store_baseline(wu_id: str, baseline: dict[str, Any]) -> None:
    """Persist a baseline keyed by wu_id; opportunistically GC stale entries."""
    now = time.time()
    with _baseline_lock:
        _baseline_store[wu_id] = (baseline, now)
        stale = [
            k for k, (_, ts) in _baseline_store.items()
            if now - ts > _BASELINE_TTL_SECONDS
        ]
        for k in stale:
            del _baseline_store[k]


def _consume_baseline(wu_id: str) -> dict[str, Any] | None:
    """Pop a baseline by wu_id. Returns None if missing or expired."""
    now = time.time()
    with _baseline_lock:
        entry = _baseline_store.pop(wu_id, None)
    if entry is None:
        return None
    baseline, ts = entry
    if now - ts > _BASELINE_TTL_SECONDS:
        return None
    return baseline


def _get_log_dir_root() -> Path:
    """Returns the log directory root, reading env var at call time (not import time).

    Reading at call time (not module-level) allows monkeypatch.setenv to work in tests.
    """
    env_val = os.environ.get("UNITY_MCP_HCG_LOG_DIR")
    if env_val:
        return Path(env_val)
    return Path(tempfile.gettempdir()) / "unity-mcp-py-hcg"


def _png_path_to_base64(p: str | None) -> str | None:
    """Read a PNG file from disk and return its base64 encoding.

    Returns None if the path is None, empty, or the file cannot be read.
    """
    if not p:
        return None
    try:
        return base64.b64encode(Path(p).read_bytes()).decode("ascii")
    except Exception:
        return None


async def human_collab_gate(
    human_instruction: str,
    fail_levels: list[str] | None = None,
    _user_choice: str | None = None,
    _wu_id: str | None = None,
) -> dict[str, Any]:
    """Mode B human collaboration gate.

    First call (without ``_user_choice``):
        - Runs HumanCollabGate.prepare() to capture baseline snapshot.
        - Stores the baseline server-side keyed by a fresh ``_wu_id``.
        - Returns ``verdict="awaiting_user"`` with the *opaque* ``_wu_id`` and
          baseline summary fields (line number, before screenshot). Internal
          paths are NOT returned.

    Second call (with ``_user_choice`` set to "Done", "Skipped", or "Failed"):
        - Looks up the baseline by ``_wu_id`` (orchestrator must pass it back).
        - Runs HumanCollabGate.analyze() with the user's response.
        - Translates the Phase 4 verdict into the MCP shape (spec §5.3).

    Args:
        human_instruction: Instruction shown to the human user.
        fail_levels: Log levels that count as failures. Defaults to
            ["Error", "Exception", "Assert"].
        _user_choice: Orchestrator-provided user response after AskUserQuestion.
            None on first call; "Done"/"Skipped"/"Failed" on second call.
        _wu_id: Opaque id returned by the first call. Required on the second call.

    Returns:
        MCP-shaped dict per spec §5.3.

    Raises:
        ValueError: second call provided ``_user_choice`` without a matching
            ``_wu_id`` in the baseline store (expired or never issued).
    """
    fail_levels = fail_levels or ["Error", "Exception", "Assert"]
    bridge_port = (await get_session_async()).port

    if _user_choice is None:
        # First call: capture baseline, store server-side.
        wu_id = f"mcp-{uuid.uuid4().hex[:8]}"
        log_dir = _get_log_dir_root() / wu_id
        log_dir.mkdir(parents=True, exist_ok=True)

        config: dict[str, Any] = {"fail_levels": fail_levels}
        gate = human_collab.HumanCollabGate(config=config)
        baseline = await asyncio.to_thread(gate.prepare, bridge_port, log_dir, wu_id)

        # Augment baseline with config + log_dir so the second call can recreate
        # the gate without re-reading env vars.
        baseline["_config"] = config
        baseline["_log_dir"] = str(log_dir)
        _store_baseline(wu_id, baseline)

        return {
            "verdict": "awaiting_user",
            "baseline_line_number": baseline.get("baseline_line_number", 0),
            "before_screenshot_png_base64": _png_path_to_base64(
                baseline.get("screenshot_before")
            ),
            "after_screenshot_png_base64": None,
            "log_delta": [],
            "error_count": 0,
            "_wu_id": wu_id,  # opaque id; no path/internal info
        }

    # Second call: look up baseline by id.
    if not _wu_id:
        raise ValueError(
            "human_collab_gate: _user_choice provided without _wu_id; "
            "the orchestrator must pass back _wu_id from the first call."
        )
    baseline = _consume_baseline(_wu_id)
    if baseline is None:
        raise ValueError(
            f"human_collab_gate: _wu_id={_wu_id!r} not found in baseline store "
            f"(expired after {_BASELINE_TTL_SECONDS}s, or never issued)."
        )

    config = baseline.pop("_config", {"fail_levels": fail_levels})
    log_dir = Path(baseline.pop("_log_dir", str(_get_log_dir_root() / _wu_id)))
    gate = human_collab.HumanCollabGate(config=config)

    analysis = await asyncio.to_thread(
        gate.analyze, bridge_port, baseline, _user_choice, log_dir, _wu_id
    )

    # Translate verdict: Skipped→"skipped", passed→"passed", else→"failed".
    # Check Skipped first because analyze() returns passed=True for Skipped.
    if _user_choice == "Skipped":
        verdict_str = "skipped"
    elif analysis.get("passed"):
        verdict_str = "passed"
    else:
        verdict_str = "failed"

    log_delta = [
        {
            "line_number": e.get("lineNumber", 0),
            "log_type": e.get("logType", ""),
            "condition": e.get("message", ""),
            "stack_trace": e.get("stackTrace", ""),
        }
        for e in analysis.get("errors", [])
    ]

    return {
        "verdict": verdict_str,
        "baseline_line_number": baseline.get("baseline_line_number", 0),
        "before_screenshot_png_base64": _png_path_to_base64(
            analysis.get("screenshot_before_path")
        ),
        "after_screenshot_png_base64": _png_path_to_base64(
            analysis.get("screenshot_after_path")
        ),
        "log_delta": log_delta,
        "error_count": analysis.get("error_count", 0),
    }


def register(mcp: Any) -> None:
    """Register human_collab_gate with the FastMCP instance.

    Note: MCP SDK rejects parameters starting with '_', so the public tool
    parameters are named without the leading underscore. Internally the
    implementation still uses '_user_choice' / '_wu_id' as canonical names.
    """

    @mcp.tool(TOOL_NAME)
    async def _tool(
        human_instruction: str,
        fail_levels: list[str] | None = None,
        user_choice: str | None = None,
        wu_id: str | None = None,
    ) -> dict[str, Any]:
        return await human_collab_gate(
            human_instruction, fail_levels, user_choice, wu_id
        )
