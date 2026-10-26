"""simulate_key tool. Write op. Triggers an input ACTION by its action path.

Calls PROJECT_RUNTIME.global.PROJECT_INPUT_SERVICE:PROJECT_TRIGGER_ACTION(actionPath, 'Performed') then
'Canceled' — this routes through the game's InnerHandleInputInfo funnel exactly
like real input, but bypasses the focus / inputEnabled gate, so it works even when
the Editor Game view isn't focused. Use for "press the key bound to feature X"
(e.g. "Hud/OpenMap", "Player/Jump").

IMPORTANT — only REAL InputActionAsset actions can be triggered this way. Virtual
PROJECT_HOTKEY_SYSTEM bindings (isVirtual=true, driven by PROJECT_HOTKEY_CALLBACK — most panel hotkeys
like "Common/ClosePanelCommon") are NOT in playerInput.actions, so this returns
triggered=false with a note. For those, use simulate_click on the bound button, or
lua_eval the panel method.

Device-level raw keystrokes (InputSystem.QueueStateEvent on Keyboard) are deferred
to a later phase. PlayMode-only.
"""
from __future__ import annotations

import re
from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "simulate_key"

# Action paths look like "Map/Action" or "Common/ClosePanelCommon" / "Raw/KeyBoardEsc".
# Interpolated into a DoString script → charset-allowlist (letters/digits/_/-/slash).
_ACTION_PATH_RE = re.compile(r"^[A-Za-z0-9_/\-]+$")


class InvalidSimulateKeyArg(ValueError):
    """Raised when actionPath is missing/illegal before any bridge traffic."""


async def simulate_key(action_path: str) -> dict[str, Any]:
    """Trigger an input action by its action path.

    Args:
        action_path: The bound action path, e.g. "Hud/OpenMap", "Player/Jump",
            "Common/ClosePanelCommon".

    Returns:
        Bridge result + write-op token: {triggered, actionPath, ...}. ``triggered``
        is whether the action existed and ran.

    Raises:
        InvalidSimulateKeyArg: empty/illegal action_path (before any bridge call).
    """
    if not isinstance(action_path, str) or len(action_path.strip()) == 0:
        raise InvalidSimulateKeyArg("action_path must be a non-empty string")
    if not _ACTION_PATH_RE.match(action_path):
        raise InvalidSimulateKeyArg(
            f"action_path {action_path!r} has illegal chars (allow [A-Za-z0-9_/-])"
        )
    return await write_op(TOOL_NAME, {"actionPath": action_path}, bridge_method="SimulateKey")


_DESCRIPTION = (__doc__ or "") + """
FINAL-VERIFICATION ROLE: when the user gave repro steps involving key/gamepad
presses, replay them here during final verification, then capture_screenshot to
evidence the outcome. (Reaches only real InputActionAsset actions — for virtual
PROJECT_HOTKEY_SYSTEM hotkeys use simulate_click on the bound button.)
"""


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME, description=_DESCRIPTION)
    async def _tool(action_path: str) -> dict[str, Any]:
        return await simulate_key(action_path)
