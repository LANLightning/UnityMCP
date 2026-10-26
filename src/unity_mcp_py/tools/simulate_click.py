"""simulate_click tool. Write op. Clicks a UI node via PROJECT_UI's own simulate API.

Main path (no real input device, no screen coordinate): resolves the node by
panel uid + refKey (panel PROJECT_NODE_REFERENCE key) OR path (transform path under the
panel root), then calls:
  - mode='raycast' (default): PROJECT_CLICKABLE:PROJECT_CLICK_WITH_HIT_TEST(false) — raycasts at
    the node's screen point and only fires if it's the topmost/visible hit; returns
    whether it actually clicked (self-validating occlusion check).
  - mode='force': PROJECT_CLICKABLE:PROJECT_CLICK_DIRECT(false) — fires the handler with no
    hit-test (use when the node is known-good but raycast fails, e.g. off-screen).

PlayMode-only. Device-pixel clicking (screenXY via InputSystem.QueueStateEvent) is
deferred to a later phase.
"""
from __future__ import annotations

import re
from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "simulate_click"

# refKey is an PROJECT_NODE_REFERENCE key (Lua identifier-ish). path is a transform path
# ("A/B/C"). Both are interpolated into a DoString script → charset-allowlist as
# defense in depth (the C# side also EscapeSingleQuoted's them).
_REFKEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_/().\- ]+$")
_VALID_MODES = frozenset({"raycast", "force"})


class InvalidSimulateClickArg(ValueError):
    """Raised when locator/mode are invalid before any bridge traffic."""


async def simulate_click(
    panel_uid: int,
    ref_key: str | None = None,
    path: str | None = None,
    mode: str = "raycast",
) -> dict[str, Any]:
    """Click a node in an open panel.

    Args:
        panel_uid: Numeric panel uid the node lives in (UIConst.UI_ID_*).
        ref_key: PROJECT_NODE_REFERENCE key for the node (e.g. "btnClosePROJECT_BUTTON"). XOR path.
        path: Transform path under the panel root (e.g. "Window/TopBar/BtnBack"). XOR ref_key.
        mode: "raycast" (default, self-validating) or "force" (no hit-test).

    Returns:
        Bridge result + write-op token: {clicked, mode, nodeFound, note?, ...}.

    Raises:
        InvalidSimulateClickArg: bad uid / not exactly one locator / bad chars / bad mode.
    """
    if not isinstance(panel_uid, int) or isinstance(panel_uid, bool) or panel_uid == 0:
        raise InvalidSimulateClickArg(f"panel_uid must be a non-zero int, got {panel_uid!r}")
    if (ref_key is None) == (path is None):
        raise InvalidSimulateClickArg("provide exactly one of ref_key | path")
    if mode not in _VALID_MODES:
        raise InvalidSimulateClickArg(f"mode must be one of {sorted(_VALID_MODES)}")

    args: dict[str, Any] = {"panelUid": panel_uid, "mode": mode}
    if ref_key is not None:
        if not _REFKEY_RE.match(ref_key):
            raise InvalidSimulateClickArg(f"ref_key {ref_key!r} has illegal chars (allow [A-Za-z0-9_])")
        args["refKey"] = ref_key
    else:
        if not _PATH_RE.match(path or ""):
            raise InvalidSimulateClickArg(f"path {path!r} has illegal chars")
        args["path"] = path

    return await write_op(TOOL_NAME, args, bridge_method="SimulateClick")


_DESCRIPTION = (__doc__ or "") + """
FINAL-VERIFICATION ROLE: when the user described repro steps, drive the same
clicks here during final verification instead of calling internal handlers that
bypass the input path; then capture_screenshot to evidence the result.
"""


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME, description=_DESCRIPTION)
    async def _tool(
        panel_uid: int,
        ref_key: str | None = None,
        path: str | None = None,
        mode: str = "raycast",
    ) -> dict[str, Any]:
        return await simulate_click(panel_uid, ref_key, path, mode)
