"""Verification-workflow guidance is surfaced to the AI.

The operator requires that the FINAL verification of any change be evidence-based
(a screenshot of the real running game) and, when the user gave repro steps, driven
by simulated human operations. That guidance lives in two places the AI actually
reads: the server-level MCP `instructions`, and the descriptions of the
verification-critical tools. These tests lock both in so a refactor can't silently
drop them.
"""
from __future__ import annotations

import asyncio

import pytest


def test_server_exposes_instructions() -> None:
    from unity_mcp_py import server

    instr = getattr(server.mcp, "instructions", None)
    assert instr, "server must expose MCP instructions"
    assert instr == server.SERVER_INSTRUCTIONS


@pytest.mark.parametrize(
    "needle",
    [
        "capture_screenshot",          # rule 1: screenshot required
        "simulate_click",              # rule 2: replay repro steps
        "ASK the user",                # rule 3: confirm the screen
        "multiple screenshots",        # rule 4 (heading text)
        "Multi-screen",                # rule 4
        "PROJECT_NAVIGATION_SERVICE",                  # gamepad-inactive gotcha
    ],
)
def test_instructions_cover_each_rule(needle: str) -> None:
    from unity_mcp_py import server

    assert needle in server.SERVER_INSTRUCTIONS, f"instructions missing: {needle!r}"


def _tool_descriptions() -> dict[str, str]:
    from unity_mcp_py import server

    server.register_tools()

    async def _list() -> dict[str, str]:
        tools = await server.mcp.list_tools()
        return {t.name: (t.description or "") for t in tools}

    return asyncio.run(_list())


@pytest.mark.parametrize(
    "tool_name",
    ["capture_screenshot", "simulate_click", "simulate_key", "set_input_text"],
)
def test_verification_tools_have_descriptions(tool_name: str) -> None:
    descriptions = _tool_descriptions()
    desc = descriptions.get(tool_name, "")
    assert desc.strip(), f"{tool_name} must expose a non-empty description"
    assert "FINAL-VERIFICATION ROLE" in desc, (
        f"{tool_name} description must carry the final-verification pointer"
    )


def test_capture_screenshot_description_demands_a_shot() -> None:
    descriptions = _tool_descriptions()
    desc = descriptions["capture_screenshot"]
    assert "MUST" in desc and "screenshot" in desc.lower()
