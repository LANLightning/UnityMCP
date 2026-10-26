"""capture_screenshot tool. Read-only. No token.

PERF 2026-05-25: ``mode`` parameter controls payload shape.
  - ``full`` (default, unchanged behavior): base64 PNG in response.
  - ``metadata``: sha256 + sizeBytes only — saves the ~2MB base64 from the
    LLM context. Caller can re-request with ``full`` if needed.
  - ``file_ref``: persist PNG to ``UNITY_MCP_SCREENSHOT_DIR`` keyed by sha256
    and return the file path. For local agents that need the bytes without
    paying for them in token context.

Wall-clock cost is dominated by Unity-side CaptureScreenshot (~300ms,
two-tick delay + PNG encode); ``mode`` does not change that. The win is on
the LLM payload side.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from ..bridge import bridge_client
from ..bridge.session import bridge_call, get_session_async
from ..state import LUA_BUDGET, runway_response

TOOL_NAME = "capture_screenshot"

_VALID_MODES = frozenset({"full", "metadata", "file_ref"})

# Generous ceiling so a human approval gate (user clicks 继续) doesn't time out.
# 30 minutes is far longer than any realistic confirm-the-frame wait.
_CAPTURE_TIMEOUT_SECONDS = 1800.0


def _is_null_texture_capture_error(exc: bridge_client.BridgeError) -> bool:
    message = exc.message.lower()
    return (
        exc.code == "INTERNAL"
        and "texture" in message
        and ("invalid" in message or "null" in message)
    )


def _base64_size_bytes(png_base64: str) -> int:
    compact = "".join(png_base64.split())
    if not compact:
        return 0
    padding = len(compact) - len(compact.rstrip("="))
    return max((len(compact) * 3 // 4) - padding, 0)


def _capture_size_bytes(cap: dict[str, Any], png_base64: str) -> int:
    explicit = cap.get("sizeBytes")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    return _base64_size_bytes(png_base64)


def _screenshot_dir() -> Path:
    """Resolve the on-disk cache directory.

    Honors UNITY_MCP_SCREENSHOT_DIR; otherwise %LOCALAPPDATA% on Windows,
    ~/.unity-mcp-py/screenshots elsewhere.
    """
    override = os.environ.get("UNITY_MCP_SCREENSHOT_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "unity-mcp-py" / "screenshots"
    return Path.home() / ".unity-mcp-py" / "screenshots"


def _decode_and_persist(png_base64: str, sha: str) -> str:
    target_dir = _screenshot_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{sha}.png"
    if not target.exists():
        try:
            data = base64.b64decode(png_base64)
        except Exception:
            data = b""
        target.write_bytes(data)
    return str(target)


async def capture_screenshot(
    width: int | None = None,
    height: int | None = None,
    supersize: int = 1,
    mode: str = "full",
    skip_if_unchanged: bool = True,
) -> dict[str, Any]:
    if mode not in _VALID_MODES:
        raise ValueError(
            f"mode={mode!r} must be one of {sorted(_VALID_MODES)}"
        )

    # A verification session stays armed before any screenshot so the AI never
    # captures unguarded: it shows the "正在操作Unity验证" banner and arms
    # human-takeover detection. The APPROVAL GATE (继续) is conditional — it is on
    # iff code was modified since the last confirmation (session.code_dirty, set by
    # the reload tools). We set gateScreenshots to match code_dirty, and only
    # re-issue StartVerificationSession when the current state doesn't already
    # match — so a stream of clean captures against a fresh session adds no extra
    # bridge calls.
    session = await get_session_async()
    want_gate = session.code_dirty
    auto_started_session: dict[str, Any] | None = None
    try:
        state = await bridge_call("GetVerificationSessionState", {})
        armed = bool(state.get("armed", False))
        current_gate = bool(state.get("gateScreenshots", False))
        if (not armed) or (current_gate != want_gate):
            note = "代码已修改，请确认" if want_gate else "auto session for screenshot"
            auto_started_session = await bridge_call(
                "StartVerificationSession",
                {"note": note, "gateScreenshots": want_gate},
            )
    except bridge_client.BridgeError:
        # If the bridge can't report/start session state (e.g. EditMode), fall
        # through and let the capture itself surface the real error.
        pass
    gated_this_call = want_gate

    args: dict[str, Any] = {"supersize": supersize}
    if width is not None:
        args["width"] = width
    if height is not None:
        args["height"] = height
    screenshot_available = True
    capture_error: str | None = None
    try:
        # Long timeout: under the verification approval gate the bridge holds the
        # CaptureScreenshot response until the user clicks 继续, which can take
        # minutes. The default 30s bridge_call timeout would abort that wait.
        cap = await bridge_call(
            "CaptureScreenshot", args, response_timeout=_CAPTURE_TIMEOUT_SECONDS
        )
    except bridge_client.BridgeError as exc:
        if not _is_null_texture_capture_error(exc):
            raise
        screenshot_available = False
        capture_error = str(exc)
        cap = {"pngBase64": "", "sizeBytes": 0}
    ps = await bridge_call("GetPlayState", {})
    tail = await bridge_call("TailEditorLog", {"maxLines": 1})
    lines = tail.get("lines", [])

    png_base64_full = str(cap.get("pngBase64", ""))
    size_bytes = _capture_size_bytes(cap, png_base64_full)
    sha = (
        hashlib.sha256(png_base64_full.encode("utf-8", "replace")).hexdigest()
        if png_base64_full
        else ""
    )

    # Only suppress full payloads already delivered in full. A metadata result or
    # file reference does not establish that the client received the image bytes.
    # file_ref must always return a usable path, including for repeated frames.
    unchanged = bool(
        mode == "full"
        and skip_if_unchanged
        and sha
        and sha == getattr(session, "last_screenshot_sha", None)
    )

    if unchanged:
        out_base64 = ""
        file_path = ""
    elif mode == "full":
        out_base64 = png_base64_full
        file_path = ""
    elif mode == "metadata":
        out_base64 = ""
        file_path = ""
    else:  # file_ref
        out_base64 = ""
        file_path = _decode_and_persist(png_base64_full, sha) if png_base64_full else ""

    # Track only actual full-image delivery, not mere acquisition of a frame.
    if mode == "full" and out_base64 and sha:
        session.last_screenshot_sha = sha

    # If this capture was gated, reaching here means the bridge delivered the
    # frame — i.e. the user clicked 继续 (or auto-approve). The code change has
    # now been visually confirmed, so clear the dirty flag; subsequent captures
    # run clean until the next reload. An abort re-raises above (non-null-texture
    # BridgeError) and never reaches here, so an unconfirmed change stays dirty.
    if gated_this_call and screenshot_available:
        session.code_dirty = False

    # Disk hygiene: whenever a capture persists a PNG to the screenshot dir
    # (mode="file_ref"), the AI reads THIS call's file_path and the file the
    # PREVIOUS capture persisted becomes stale, so unlink it ("下次调用时清理上一张")
    # — disk holds at most one persisted shot. This runs on BOTH the gated and
    # clean paths: under the gate the bridge holds the frame in memory and its
    # on-继续 delete runs BEFORE Python writes the file_ref PNG, so a gated file_ref
    # file would otherwise never be reclaimed. full/metadata modes write nothing
    # (file_path == "") so the tracker resets to None. Best-effort: a missing or
    # locked file must never break the capture.
    prev = session.last_clean_screenshot_path
    if prev and prev != file_path:
        try:
            Path(prev).unlink()
        except OSError:
            pass
    session.last_clean_screenshot_path = file_path or None

    # Soft-ground the lua runway ONLY when a real frame came back (a null-texture /
    # EditMode capture is not grounding). Mirrors screenshot_available.
    if screenshot_available:
        ground_outcome = await LUA_BUDGET.ground(hard=False)
    else:
        ground_outcome = await LUA_BUDGET.snapshot_outcome()

    return {
        "png_base64": out_base64,
        "sizeBytes": size_bytes,
        "sha256": sha,
        "mode": mode,
        "unchanged": unchanged,
        "file_path": file_path,
        "play_state": ps["state"],
        "line_number": lines[-1]["lineNumber"] if lines else 0,
        "screenshot_available": screenshot_available,
        "capture_error": capture_error,
        "auto_started_session": (
            auto_started_session.get("sessionId") if auto_started_session else None
        ),
        "lua_runway": runway_response(ground_outcome, LUA_BUDGET),
    }


_DESCRIPTION = (__doc__ or "") + """
FINAL-VERIFICATION ROLE: the final verification of any user-facing change MUST
include a screenshot from this tool showing the expected result — read-state
probes (lua_eval / logs / assertions) do not by themselves prove the change works.
If the change spans multiple screens, capture one per meaningful step. When unsure
the game is on the relevant screen, ask the user before capturing.
"""


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME, description=_DESCRIPTION)
    async def _tool(
        width: int | None = None,
        height: int | None = None,
        supersize: int = 1,
        mode: str = "full",
        skip_if_unchanged: bool = True,
    ) -> dict[str, Any]:
        return await capture_screenshot(
            width, height, supersize, mode, skip_if_unchanged
        )
