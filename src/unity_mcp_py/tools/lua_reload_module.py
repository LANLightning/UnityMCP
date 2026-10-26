"""lua_reload_module tool. Write op. Wraps bridge LuaReloadModule.

Hot-reloads one or more Lua modules via the project's
PROJECT_ADAPTER.Reload.reload(list, impFiles, delKeyModels) entry point.
"""
from __future__ import annotations

import re
from typing import Any

from ._write_op_helper import write_op
from ..bridge.session import get_session_async

TOOL_NAME = "lua_reload_module"

# Defense-in-depth against Lua-injection via attacker-controlled module names
# routed through MCP tool args. The C# bridge already escapes single quotes
# and backslashes when building the DoString script, but layered escaping is
# fragile (NUL bytes, edge cases in xLua's handling of `]]` long-string
# closers). We restrict module-name strings here to a strict allowlist that
# matches Lua's `require()` legal identifier syntax: ASCII letters, digits,
# underscore, and the dot separator. No spaces, no quotes, no operators.
_MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.]*$")
_MAX_NAME_LENGTH = 256


class InvalidLuaModuleNameError(ValueError):
    """Raised when a module/imp/del name fails the allowlist check."""


def _validate_module_names(field: str, names: list[str]) -> list[str]:
    """Reject any name that doesn't match the Lua-identifier allowlist.

    Returns the input unchanged on success; raises ``InvalidLuaModuleNameError``
    with the offending name + index on the first violation. Empty inputs are
    allowed for ``imp_files`` / ``del_key_models`` but caught upstream as ``None``.
    """
    for i, name in enumerate(names):
        if not isinstance(name, str):
            raise InvalidLuaModuleNameError(
                f"{field}[{i}] must be a string, got {type(name).__name__}"
            )
        if len(name) == 0:
            raise InvalidLuaModuleNameError(f"{field}[{i}] is empty")
        if len(name) > _MAX_NAME_LENGTH:
            raise InvalidLuaModuleNameError(
                f"{field}[{i}] exceeds {_MAX_NAME_LENGTH} chars"
            )
        if not _MODULE_NAME_PATTERN.match(name):
            raise InvalidLuaModuleNameError(
                f"{field}[{i}]={name!r} contains characters outside [A-Za-z0-9_.]"
            )
    return names


async def lua_reload_module(
    modules: list[str],
    imp_files: list[str] | None = None,
    del_key_models: list[str] | None = None,
) -> dict[str, Any]:
    """Trigger an in-process Lua module reload.

    Args:
        modules: Module names to reload (required, non-empty).
        imp_files: Files that newly 'import' something (optional).
        del_key_models: Keys to remove from KeyModels before reload (optional).

    Returns:
        Bridge result enriched with ``write_op_token`` / ``line_number_before``.
        ``result.ok`` reports Lua-side success; the envelope is still Ok=true
        for a Lua-side failure so the caller can read the traceback.

    Raises:
        InvalidLuaModuleNameError: any name fails the allowlist
            ``^[A-Za-z0-9_][A-Za-z0-9_.]*$`` check. Raised before any bridge
            traffic, so no token is issued.
    """
    if not modules:
        raise InvalidLuaModuleNameError("modules must be a non-empty list")
    _validate_module_names("modules", list(modules))
    if imp_files is not None:
        _validate_module_names("imp_files", list(imp_files))
    if del_key_models is not None:
        _validate_module_names("del_key_models", list(del_key_models))

    args: dict[str, Any] = {"modules": list(modules)}
    if imp_files is not None:
        args["imp_files"] = list(imp_files)
    if del_key_models is not None:
        args["del_key_models"] = list(del_key_models)
    result = await write_op(
        TOOL_NAME,
        args,
        bridge_method="LuaReloadModule",
    )
    # Reload succeeded → code was (re)applied → gate the next screenshot. Name
    # validation above already raised for bad input before any bridge traffic;
    # write_op raises on bridge failure. So we only reach here on success.
    (await get_session_async()).code_dirty = True
    return result


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        modules: list[str],
        imp_files: list[str] | None = None,
        del_key_models: list[str] | None = None,
    ) -> dict[str, Any]:
        return await lua_reload_module(modules, imp_files, del_key_models)
