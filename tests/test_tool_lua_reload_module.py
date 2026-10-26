"""Tests for lua_reload_module tool."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.lua_reload_module import (
    InvalidLuaModuleNameError,
    lua_reload_module,
)


@pytest.mark.asyncio
async def test_lua_reload_module_success_issues_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """lua_reload_module returns bridge result with write_op_token on Lua success."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 500}]})
    bridge.expect_request("LuaReloadModule").respond_with({
        "ok": True,
        "reloaded": ["Common.Foo", "Common.Bar"],
    })

    result = await lua_reload_module(
        modules=["Common.Foo", "Common.Bar"],
        imp_files=["Common.Foo"],
    )

    assert result["ok"] is True
    assert result["reloaded"] == ["Common.Foo", "Common.Bar"]
    assert "write_op_token" in result
    assert result["line_number_before"] == 500
    assert result["requires_observation"] is True

    # Token must be registered in GUARD
    pending = await GUARD.list_pending()
    assert any(p.token == result["write_op_token"] for p in pending)

    # Cleanup
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_lua_reload_module_lua_failure_still_issues_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """A Lua-side reload failure (result.ok=false + error) still issues a token.

    The bridge envelope is Ok=true so the caller can inspect the traceback;
    we model that as a normal write-op so the AI sees evidence of the attempt.
    """
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 42}]})
    bridge.expect_request("LuaReloadModule").respond_with({
        "ok": False,
        "reloaded": [],
        "error": "reload traceback: attempt to index nil 'XLuaHelper'",
    })

    result = await lua_reload_module(modules=["Bad.Module"])

    assert result["ok"] is False
    assert "traceback" in result["error"]
    assert "write_op_token" in result
    assert result["line_number_before"] == 42
    assert result["requires_observation"] is True

    # Cleanup
    await GUARD.consume([result["write_op_token"]])


@pytest.mark.asyncio
async def test_lua_reload_module_no_token_on_bridge_error(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Bridge-level error (protocol, connection, etc.) must NOT issue a token.

    Distinguishes a hard bridge-transport failure (network error, protocol
    violation, bridge-internal exception) — which MUST abort the write_op
    contract — from a "Lua-reported failure" (bridge Ok=true but
    ``result.ok=false``), which IS a normal recorded write per the prior test.
    """
    from unity_mcp_py.bridge import bridge_client
    BridgeError = bridge_client.BridgeError

    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 77}]})
    bridge.expect_request("LuaReloadModule").respond_error(
        "INTERNAL", "LuaReloadModule: no Lua executor is wired"
    )

    with pytest.raises(BridgeError, match="no Lua executor"):
        await lua_reload_module(modules=["Common.Foo"])

    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_reload_module" for p in pending)


# ---------------------------------------------------------------------------
# Defense-in-depth: Lua module-name allowlist (security HIGH fix)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_name",
    [
        "Foo'); os.execute('rm -rf /'); --",
        'Foo"); os.execute("whoami")--',
        "Foo\n..\\..\\etc\\passwd",
        "Foo bar",
        "Foo[[injection]]",
        "Foo\x00NullByte",
        "",
    ],
)
@pytest.mark.asyncio
async def test_lua_reload_module_rejects_injection_attempt(bad_name: str) -> None:
    """Attacker-crafted module names must be rejected BEFORE any bridge call.

    Covers the spec §6.2 safety invariant: no token is issued when the tool
    rejects its arguments, and no bridge traffic occurs for malformed input.
    """
    # Fixtures (unity_bridge_session, mock_bridge_server) are deliberately
    # NOT listed: the validator must fire before any bridge connection is
    # attempted, so the test must pass without a mock bridge.
    with pytest.raises(InvalidLuaModuleNameError):
        await lua_reload_module(modules=[bad_name])

    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "lua_reload_module" for p in pending)


@pytest.mark.asyncio
async def test_lua_reload_module_rejects_empty_modules_list() -> None:
    """Zero-length modules list is meaningless and must be rejected."""
    with pytest.raises(InvalidLuaModuleNameError, match="non-empty"):
        await lua_reload_module(modules=[])


@pytest.mark.asyncio
async def test_lua_reload_module_validates_imp_files_and_del_key_models() -> None:
    """The same allowlist applies to imp_files and del_key_models."""
    with pytest.raises(InvalidLuaModuleNameError, match="imp_files"):
        await lua_reload_module(
            modules=["Common.Foo"],
            imp_files=["Bad'); attack();"],
        )
    with pytest.raises(InvalidLuaModuleNameError, match="del_key_models"):
        await lua_reload_module(
            modules=["Common.Foo"],
            del_key_models=["Bad name"],
        )
