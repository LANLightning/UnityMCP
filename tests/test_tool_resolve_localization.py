"""Tests for resolve_localization tool (Phase 5)."""
import pytest

from unity_mcp_py.tools.resolve_localization import (
    InvalidLocalizationArg,
    resolve_localization,
)


@pytest.mark.asyncio
async def test_resolve_localization_passthrough(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    bridge = mock_bridge_server
    bridge.expect_request("ResolveLocalization").respond_with({
        "language": "en",
        "entries": [{"id": 1001, "text": "Settings"}, {"id": 1002, "text": "Close"}],
    })

    result = await resolve_localization(ids=[1001, 1002], language="en")
    assert result["language"] == "en"
    assert result["entries"][0]["text"] == "Settings"
    assert "write_op_token" not in result


@pytest.mark.asyncio
async def test_resolve_localization_forwards_language(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    bridge = mock_bridge_server
    bridge.expect_request(
        "ResolveLocalization",
        args_predicate=lambda a: a.get("ids") == [42] and a.get("language") == "ja_JP",
    ).respond_with({"language": "ja_JP", "entries": [{"id": 42, "text": "設定"}]})

    result = await resolve_localization(ids=[42], language="ja_JP")
    assert result["entries"][0]["text"] == "設定"


@pytest.mark.asyncio
async def test_resolve_localization_rejects_empty() -> None:
    with pytest.raises(InvalidLocalizationArg, match="non-empty"):
        await resolve_localization(ids=[])


@pytest.mark.asyncio
async def test_resolve_localization_rejects_non_int() -> None:
    with pytest.raises(InvalidLocalizationArg):
        await resolve_localization(ids=["1001"])  # type: ignore[list-item]
