"""set_input_text tool. Write op. Sets an PROJECT_UI input field's text and commits it.

Resolves the input node (panel uid + refKey OR path), sets its settable `.text`,
calls ActivateInputField(), then fires the field's PROJECT_COMMIT_TEXT callback (if any) so
the panel reacts as if the user typed and committed. Does NOT synthesize per-key
device events (unreliable for TMP/UGUI input fields under the new Input System).

PlayMode-only.
"""
from __future__ import annotations

import re
from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "set_input_text"

_REFKEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_/().\- ]+$")
_MAX_TEXT_LEN = 4096


class InvalidSetInputTextArg(ValueError):
    """Raised when locator/text are invalid before any bridge traffic."""


async def set_input_text(
    panel_uid: int,
    text: str,
    ref_key: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Set an input field's text in an open panel.

    Args:
        panel_uid: Numeric panel uid the input field lives in.
        text: The text to set (committed via the field's end-edit callback).
        ref_key: PROJECT_NODE_REFERENCE key of the input field. XOR path.
        path: Transform path under the panel root. XOR ref_key.

    Returns:
        Bridge result + write-op token: {applied, nodeFound, note?, ...}.

    Raises:
        InvalidSetInputTextArg: bad uid / not exactly one locator / bad chars / oversized text.
    """
    if not isinstance(panel_uid, int) or isinstance(panel_uid, bool) or panel_uid == 0:
        raise InvalidSetInputTextArg(f"panel_uid must be a non-zero int, got {panel_uid!r}")
    if (ref_key is None) == (path is None):
        raise InvalidSetInputTextArg("provide exactly one of ref_key | path")
    if not isinstance(text, str):
        raise InvalidSetInputTextArg("text must be a string")
    if len(text) > _MAX_TEXT_LEN:
        raise InvalidSetInputTextArg(f"text exceeds {_MAX_TEXT_LEN} chars")

    args: dict[str, Any] = {"panelUid": panel_uid, "text": text}
    if ref_key is not None:
        if not _REFKEY_RE.match(ref_key):
            raise InvalidSetInputTextArg(f"ref_key {ref_key!r} has illegal chars (allow [A-Za-z0-9_])")
        args["refKey"] = ref_key
    else:
        if not _PATH_RE.match(path or ""):
            raise InvalidSetInputTextArg(f"path {path!r} has illegal chars")
        args["path"] = path

    return await write_op(TOOL_NAME, args, bridge_method="SetInputText")


_DESCRIPTION = (__doc__ or "") + """
FINAL-VERIFICATION ROLE: when the user's repro steps involve typing into a field,
set it here during final verification, then capture_screenshot to evidence the
result.
"""


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME, description=_DESCRIPTION)
    async def _tool(
        panel_uid: int,
        text: str,
        ref_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        return await set_input_text(panel_uid, text, ref_key, path)
