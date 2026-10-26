"""resolve_localization tool. Read-only. No token.

Resolve localization ids (textId) → display strings for any language, in the
Editor. Lets the AI match a screenshot's literal string to a node's textId across
locales, or read what a node's textId actually displays.

Uses the bridge's per-id PROJECT_LOCALIZATION_LOOKUP (NOT the batch variant, which
ignores its language arg). PlayMode-only (reflects into the running game).
"""
from __future__ import annotations

from typing import Any

from ..bridge.session import bridge_call

TOOL_NAME = "resolve_localization"

_MAX_IDS = 500


class InvalidLocalizationArg(ValueError):
    """Raised when ids are missing/invalid before any bridge call."""


async def resolve_localization(
    ids: list[int],
    language: str | None = None,
) -> dict[str, Any]:
    """Resolve localization ids to strings.

    Args:
        ids: textId integers to resolve (1..500).
        language: Target language code (e.g. "zh_CN", "en", "ja_JP"). Default
            zh_CN on the bridge.

    Returns:
        {"language": str, "entries": [{"id": int, "text": str}, ...]}.

    Raises:
        InvalidLocalizationArg: empty/oversized/invalid ids (before any bridge call).
    """
    if not ids:
        raise InvalidLocalizationArg("ids must be a non-empty list")
    if len(ids) > _MAX_IDS:
        raise InvalidLocalizationArg(f"ids exceeds {_MAX_IDS} entries ({len(ids)})")
    int_ids: list[int] = []
    for i, v in enumerate(ids):
        if not isinstance(v, int) or isinstance(v, bool):
            raise InvalidLocalizationArg(f"ids[{i}]={v!r} must be an int")
        int_ids.append(v)

    args: dict[str, Any] = {"ids": int_ids}
    if language is not None:
        args["language"] = str(language)

    return await bridge_call("ResolveLocalization", args)


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        ids: list[int],
        language: str | None = None,
    ) -> dict[str, Any]:
        return await resolve_localization(ids, language)
