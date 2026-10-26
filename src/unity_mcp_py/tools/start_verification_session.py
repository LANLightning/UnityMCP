"""start_verification_session tool. Read-op (control).

Begins a no-human UI verification run on the bridge:
  - shows the Editor-side hint banner "当前正在操作Unity验证，请勿人为操作"
    (an EditorWindow — NOT captured by ScreenCapture, does not intercept clicks),
  - arms human-takeover detection (real mouse/keyboard/touch/gamepad activity is
    classified: mouse-move-only pauses the run; a real click/key/scroll aborts it).

Returns a sessionId. Pair with end_verification_session to tear it down.
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "start_verification_session"

_MIN_PAUSE_SILENCE_MS = 200
_MAX_PAUSE_SILENCE_MS = 60000


class InvalidVerificationSessionArg(ValueError):
    """Raised when a session arg is out of range before any bridge traffic."""


async def start_verification_session(
    note: str | None = None,
    pause_silence_ms: int | None = None,
    abort_on_high: bool = True,
    gate_screenshots: bool = True,
) -> dict[str, Any]:
    """Start a verification session (show hint banner + arm takeover detection).

    Args:
        note: Optional sub-caption shown under the banner.
        pause_silence_ms: After a low-impact (mouse-move) takeover the run pauses
            until the real device is silent for this long, then resumes. Default
            2000ms on the bridge. Bounded [200, 60000].
        abort_on_high: If True (default) a high-impact takeover (real click/key/
            scroll/touch/gamepad) aborts the in-flight verification run.
        gate_screenshots: If True (default) every capture_screenshot during this
            session pauses for the user to click 继续 in the hint window before the
            image is delivered to the AI — blocking all downstream AI tool calls
            until the user confirms. The user can click 继续且不再询问 to switch to
            auto-approve, or 重新截图 to refresh the frame.

    Returns:
        {"sessionId": str, "started": bool}.
    """
    args: dict[str, Any] = {
        "abortOnHigh": bool(abort_on_high),
        "gateScreenshots": bool(gate_screenshots),
    }
    if note is not None:
        args["note"] = str(note)
    if pause_silence_ms is not None:
        pause = int(pause_silence_ms)
        if pause < _MIN_PAUSE_SILENCE_MS or pause > _MAX_PAUSE_SILENCE_MS:
            raise InvalidVerificationSessionArg(
                f"pause_silence_ms must be in [{_MIN_PAUSE_SILENCE_MS}, "
                f"{_MAX_PAUSE_SILENCE_MS}], got {pause}"
            )
        args["pauseSilenceMs"] = pause

    return await bridge_call("StartVerificationSession", args)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        note: str | None = None,
        pause_silence_ms: int | None = None,
        abort_on_high: bool = True,
        gate_screenshots: bool = True,
    ) -> dict[str, Any]:
        return await start_verification_session(
            note, pause_silence_ms, abort_on_high, gate_screenshots
        )
