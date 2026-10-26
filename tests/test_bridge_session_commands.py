"""Bridge administration is discoverable as MCP tools and slash-command prompts."""
from __future__ import annotations

import pytest


def test_bridge_admin_tool_and_prompts_are_registered() -> None:
    from unity_mcp_py import server

    server.register_tools()

    tool_names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    prompt_names = {
        prompt.name for prompt in server.mcp._prompt_manager.list_prompts()
    }
    assert {"bridge_admin", "bind_bridge", "select_bridge"} <= tool_names
    assert not any(name.startswith("mcp__") for name in tool_names)
    assert {
        "unity_pair",
        "unity_status",
        "unity_clear",
        "unity_owners",
        "unity_diagnose",
        "unity_repair",
    } <= prompt_names


@pytest.mark.asyncio
async def test_unity_repair_prompt_routes_to_bridge_admin() -> None:
    from unity_mcp_py import server

    server.register_tools()
    messages = await server.mcp._prompt_manager.render_prompt(
        "unity_repair",
        {
            "port": "17873",
            "client_id": "codex-game-a",
            "bridge_id": "game-a",
            "force": "false",
        },
    )

    text = str(messages[0].content)
    assert "mcp__unity-mcp-py__bridge_admin" in text
    assert '"action": "repair"' in text
    assert '"port": "17873"' in text
    assert '"client_id": "codex-game-a"' in text
    assert '"bridge_id": "game-a"' in text


@pytest.mark.asyncio
async def test_bridge_admin_pair_switches_the_live_session(monkeypatch) -> None:
    from unity_mcp_py.tools import bridge_admin

    captured: dict[str, object] = {}

    async def fake_bind(ctx, **kwargs):
        captured["ctx"] = ctx
        captured.update(kwargs)
        return {"success": True, "action": "bind"}

    monkeypatch.setattr(bridge_admin, "_do_bind_bridge", fake_bind)
    context = object()
    result = await bridge_admin.run_bridge_admin(
        context,
        action="pair",
        port=17874,
        client_id="codex-game-b",
        bridge_id="game-b",
    )

    assert result["success"] is True
    assert result["exitCode"] == 0
    assert captured == {
        "ctx": context,
        "port": 17874,
        "expected_bridge_id": "game-b",
        "force_takeover": False,
        "client_id": "codex-game-b",
    }


@pytest.mark.asyncio
async def test_bridge_admin_invalid_action_is_a_structured_refusal() -> None:
    from unity_mcp_py.tools import bridge_admin

    result = await bridge_admin.run_bridge_admin(object(), action="bogus")

    assert result == {
        "success": False,
        "errorCode": "INVALID_ARGUMENT",
        "error": "unsupported bridge admin action: bogus",
        "exitCode": 2,
    }


def test_bridge_admin_cannot_be_recorded_or_replayed() -> None:
    from unity_mcp_py.recorder import BLACKLIST

    assert "bridge_admin" in BLACKLIST
