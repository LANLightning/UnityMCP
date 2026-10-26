"""Unity MCP Python Server entrypoint."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import bridge  # noqa: F401  (eager-imports vendored bridge_client/human_collab so any wiring error is loud at startup)
from .recorder import RECORDER, REGISTRY
from .state import GUARD
from .telemetry import TELEMETRY

logger = logging.getLogger("unity_mcp_py")

# Server-level guidance surfaced to the AI at session start (MCP "instructions").
# Encodes the verification workflow the operator requires — most importantly that
# the FINAL verification of any change is evidence-based (a screenshot of the real
# running game), not just read-state assertions. Keep this concise and actionable;
# it is injected into every session's context.
SERVER_INSTRUCTIONS = """\
# unity-mcp-py — verifying changes in the live Unity Editor

These tools drive a running Unity Editor. Use them to *observe reality*, not to
guess. Read-state probes (`lua_eval`, `list_ui_nodes`, `get_console_logs`) are for
diagnosis; they do NOT by themselves prove a user-facing change works.

## Final-verification rules (MANDATORY)

When you are about to claim a change is done/fixed/working, run a FINAL
verification and follow these rules:

1. **Always capture a screenshot.** The final verification MUST include at least
   one `capture_screenshot` of the real running game showing the expected result.
   A change is not "verified" on the strength of `lua_eval`/log/assertion output
   alone — those can pass while the on-screen behavior is wrong or while the
   feature path never actually ran. Look at the image and confirm it matches the
   intent before reporting success.

2. **Reproduce the user's steps with simulated human operations.** If the user's
   request described how to reproduce the issue (a sequence of clicks / key or
   gamepad presses / inputs / which buttons to push), drive that same sequence in
   the final verification using `simulate_click`, `simulate_key`, and
   `set_input_text` (open panels with `open_panel`; find nodes with
   `list_ui_nodes`). Verify the outcome of the actual interaction — do not
   shortcut it by calling internal functions that bypass the input path.

3. **Confirm the starting screen before you shoot.** Before taking a verification
   screenshot it is OK — and encouraged when unsure — to ASK the user whether the
   game is currently on the relevant screen/state (e.g. "你现在是否在 X 界面？"),
   so the screenshot captures the right thing instead of an unrelated view.

4. **Multi-screen flows → multiple screenshots.** If verifying the change spans
   more than one screen/state (e.g. open panel → navigate → trigger → result),
   capture a screenshot at each meaningful step so the whole flow is evidenced,
   not just the final frame.

## Gotchas this guidance exists to prevent

- Gamepad navigation (`PROJECT_NAVIGATION_SERVICE`) is only active in real gamepad/console mode.
  Simulated/`Navigate`-style calls silently no-op when nav is inactive — so a
  "nothing happened" result from a read probe in mouse mode proves nothing. Verify
  gamepad behavior by observing the real running game (screenshot), and when in
  doubt ask the user to drive their physical controller.
- `simulate_key` only fires REAL InputActionAsset actions; virtual PROJECT_HOTKEY_SYSTEM
  hotkeys are not reachable that way — use `simulate_click` on the bound button.
- 命令行操作可能不携带真实数据 → 升级到模拟点击兜底。用 `lua_eval`（如
  `PROJECT_RUNTIME.global.ui:open(uid)`）打开界面等操作可能不走真实数据流，导致界面空壳、与
  真实校验有差距。当你用 `verify_assertions`（带 `goal`）校验同一目标连续失败、
  verdict 返回 `escalation.should_escalate=true` 时，不要继续用 lua 硬撑——改用
  `escalate_via_click`：传 `undo_lua`（还原之前的 lua 操作，如
  `PROJECT_RUNTIME.global.ui:close(uid)`）+ 真实入口按钮的 `panel_uid`+`ref_key`/`path`，工具
  会先还原再以真实点击进入，让后续截图/断言面对真实环境。
"""

mcp = FastMCP("unity-mcp-py", instructions=SERVER_INSTRUCTIONS)

# MEDIUM fix (2026-05-13): retain the GC background task reference at module
# scope. asyncio.create_task returns a Task whose only strong reference is the
# event loop's task set, which can be collected while the task is still active
# if the caller discards it. Holding it here keeps the GC loop alive for the
# lifetime of the server process.
_gc_task: asyncio.Task[None] | None = None


def _install_recorder_middleware(mcp_obj: FastMCP) -> "object":
    """Wrap mcp.tool so every registered tool routes through RECORDER.wrap.

    Returns the original decorator so the caller can restore it after all
    register() calls complete. The wrapper also mirrors registered tools into
    REGISTRY so replay can look them up by name.
    """
    original = mcp_obj.tool

    def patched_tool(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        deco = original(name, *args, **kwargs)

        def _outer(fn):  # type: ignore[no-untyped-def]
            # Telemetry (always-on) wraps the raw tool so it fires even when no
            # recording session is active and for recorder-blacklisted tools.
            # RECORDER wraps the telemetry layer so the recorded result is
            # identical to what the tool returned. REGISTRY points at the
            # recorder-wrapped fn so replay reproduces the same outer behavior.
            measured = TELEMETRY.wrap(name, fn)
            wrapped = RECORDER.wrap(name, measured)
            REGISTRY.register(name, wrapped)
            return deco(wrapped)

        return _outer

    mcp_obj.tool = patched_tool  # type: ignore[assignment]
    return original


def _restore_tool(mcp_obj: FastMCP, original: object) -> None:
    mcp_obj.tool = original  # type: ignore[assignment]


# Optional tools register only when their env flag equals exactly "1". Off → the
# module is never imported (zero import cost, zero tool-list footprint). This is
# the reusable gate; future optional tools add one entry here.
_OPTIONAL_FEATURES: dict[str, str] = {
    "profile_performance": "UNITY_MCP_ENABLE_PROFILER",
}


def _feature_enabled(env_var: str) -> bool:
    return os.environ.get(env_var) == "1"


def register_tools() -> None:
    """Register all MCP tools. 13 originals + 6 console/record-replay + 3 verification + 2 UI-inventory + 1 open-panel + 3 simulate + 7 non-UI observe/operate + 1 verify-assertions + 2 bridge-management (select-bridge + bind-bridge). +1 optional (profile_performance, gated by UNITY_MCP_ENABLE_PROFILER)"""
    from .tools import (
        auto_login,
        ensure_ready,
        escalate_via_click,
        assert_no_compile_errors,
        query_scene,
        capture_screenshot,
        observe_evidence,
        list_pending_writes,
        enter_playmode,
        exit_playmode,
        open_scene,
        run_method,
        human_collab_gate,
        lua_reload_module,
        csharp_reload_scripts,
        get_console_logs,
        clear_console,
        record_start,
        record_stop,
        list_sessions,
        replay,
        lua_eval,
        verify_assertions,
        start_verification_session,
        end_verification_session,
        list_ui_nodes,
        resolve_localization,
        open_panel,
        simulate_click,
        simulate_key,
        set_input_text,
        observe_camera,
        list_entities,
        observe_entity,
        list_effects,
        observe_effect,
        observe_window,
        operate_camera,
        select_bridge,
        bind_bridge,
        bridge_admin,
    )
    original = _install_recorder_middleware(mcp)
    try:
        for mod in (
            auto_login,
            ensure_ready,
            escalate_via_click,
            assert_no_compile_errors,
            query_scene,
            capture_screenshot,
            observe_evidence,
            list_pending_writes,
            enter_playmode,
            exit_playmode,
            open_scene,
            run_method,
            human_collab_gate,
            lua_reload_module,
            csharp_reload_scripts,
            get_console_logs,
            clear_console,
            record_start,
            record_stop,
            list_sessions,
            replay,
            lua_eval,
            verify_assertions,
            start_verification_session,
            end_verification_session,
            list_ui_nodes,
            resolve_localization,
            open_panel,
            simulate_click,
            simulate_key,
            set_input_text,
            observe_camera,
            list_entities,
            observe_entity,
            list_effects,
            observe_effect,
            observe_window,
            operate_camera,
            select_bridge,
            bind_bridge,
            bridge_admin,
        ):
            mod.register(mcp)
        from .bridge import prompts as bridge_prompts

        bridge_prompts.register(mcp)
        # Optional tools — registered inside the recorder-middleware window so they
        # get the same telemetry/recorder wrapping as the always-on tools.
        if _feature_enabled(_OPTIONAL_FEATURES["profile_performance"]):
            from .tools import profile_performance as _profile_performance
            _profile_performance.register(mcp)
    finally:
        _restore_tool(mcp, original)


async def _start_gc_then_serve() -> None:
    global _gc_task
    register_tools()
    _gc_task = asyncio.create_task(GUARD.gc_loop(), name="unity-mcp-py.gc_loop")
    await mcp.run_stdio_async()


_VERSION = "0.10.0"

_HELP_TEXT = """\
unity-mcp-py — MCP stdio server for the Unity Editor (v{ver})

This binary speaks MCP JSON-RPC over stdin/stdout. It is not meant to be
invoked directly from a terminal — configure it as an MCP server in your
client (e.g. Claude Code's ~/.claude.json).

Usage:
  unity-mcp-py             # run the stdio server (what MCP clients spawn)
  unity-mcp-py --version   # print version and exit
  unity-mcp-py --help      # this message
  unity-mcp-py bridge --help  # bridge pairing/diagnosis/recovery CLI

Pair with the Unity-side bridge package 'com.local.unitymcp-bridge' (same
version as this server). Bridge protocol: 1.10.0.

Docs:    docs/USAGE.md
Verify:  python scripts/verify_mcp_protocol.py
"""


def main() -> None:
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "bridge":
            from .bridge.cli import main as bridge_cli_main

            raise_code = bridge_cli_main(sys.argv[2:])
            if raise_code:
                sys.exit(raise_code)
            return
        if arg in ("-v", "--version"):
            print(f"unity-mcp-py {_VERSION}")
            return
        if arg in ("-h", "--help"):
            print(_HELP_TEXT.format(ver=_VERSION))
            return
        # Unknown arg: print help to stderr and exit non-zero — better than
        # silently entering stdio mode and printing JSON-RPC errors at the user.
        print(f"unity-mcp-py: unknown argument {arg!r}\n", file=sys.stderr)
        print(_HELP_TEXT.format(ver=_VERSION), file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(_start_gc_then_serve())


if __name__ == "__main__":
    main()
