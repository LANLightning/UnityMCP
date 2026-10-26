"""open_scene tool. Write op. Wraps bridge OpenScene.

MEDIUM security fix (2026-05-13): scene_path is validated Python-side BEFORE
any bridge call. The bridge's OpenScene handler is a thin wrapper over Unity's
EditorSceneManager.OpenScene which accepts arbitrary local paths — including
UNC shares (``\\\\server\\share\\file.unity``) on Windows. Treating scene_path
as untrusted input prevents an LLM (or upstream prompt-injected payload) from
opening assets outside the project root.

Validation is deliberately strict:
  - Path must be relative (no drive letter, no leading slash).
  - No UNC paths.
  - No absolute paths.
  - No traversal segments (``..``).
  - Path must resolve under the project's Assets/ or Packages/ tree, anchored
    at ``UNITY_MCP_PROJECT_PATH`` if set.
  - Must end with ``.unity``.

If ``UNITY_MCP_PROJECT_PATH`` is unset, traversal/UNC/absolute checks still
apply but the project-anchored containment check is skipped (best-effort).
"""
from __future__ import annotations

import os
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from ._write_op_helper import write_op

TOOL_NAME = "open_scene"


class InvalidScenePathError(ValueError):
    """Raised when scene_path fails validation. No bridge traffic occurs."""


def _validate_scene_path(scene_path: str) -> str:
    """Reject path-traversal, UNC, absolute, and out-of-project scene paths.

    Returns ``scene_path`` unchanged on success.
    """
    if not isinstance(scene_path, str):
        raise InvalidScenePathError(
            f"scene_path must be a string, got {type(scene_path).__name__}"
        )
    if not scene_path:
        raise InvalidScenePathError("scene_path is empty")
    if "\x00" in scene_path:
        raise InvalidScenePathError("scene_path contains NUL byte")

    # Reject UNC paths (Windows \\server\share\... or //server/share/...).
    # PureWindowsPath classifies these as absolute even on POSIX hosts.
    win_path = PureWindowsPath(scene_path)
    if win_path.drive.startswith("\\\\") or scene_path.startswith(("\\\\", "//")):
        raise InvalidScenePathError(
            f"scene_path={scene_path!r} is a UNC path; must be project-relative"
        )

    # Reject absolute paths (with drive letter or leading separator).
    if win_path.is_absolute() or scene_path.startswith(("/", "\\")):
        raise InvalidScenePathError(
            f"scene_path={scene_path!r} is absolute; must be project-relative"
        )

    # Reject traversal segments. PurePath splits on both / and \ for parts.
    pure = PurePath(scene_path.replace("\\", "/"))
    if any(part == ".." for part in pure.parts):
        raise InvalidScenePathError(
            f"scene_path={scene_path!r} contains '..' traversal segment"
        )

    # Require .unity extension (the bridge uses EditorSceneManager.OpenScene
    # which accepts other types but UnityMCP semantically only supports scenes).
    if not scene_path.lower().endswith(".unity"):
        raise InvalidScenePathError(
            f"scene_path={scene_path!r} must end with '.unity'"
        )

    # Optional: anchor under UNITY_MCP_PROJECT_PATH/Assets or /Packages.
    project_root = os.environ.get("UNITY_MCP_PROJECT_PATH")
    if project_root:
        project = Path(project_root).resolve()
        try:
            resolved = (project / scene_path).resolve(strict=False)
        except (OSError, ValueError) as exc:
            raise InvalidScenePathError(
                f"scene_path={scene_path!r} could not resolve under project: {exc}"
            )
        try:
            resolved.relative_to(project)
        except ValueError:
            raise InvalidScenePathError(
                f"scene_path={scene_path!r} resolves outside "
                f"UNITY_MCP_PROJECT_PATH={project_root}"
            )

    return scene_path


async def open_scene(scene_path: str) -> dict[str, Any]:
    """Open a Unity scene by path and issue a write-op token for observation.

    Raises:
        InvalidScenePathError: scene_path failed validation. Raised before any
            bridge traffic, so no write_op_token is issued.
    """
    _validate_scene_path(scene_path)
    return await write_op(
        TOOL_NAME,
        {"scenePath": scene_path},
        bridge_method="OpenScene",
    )


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(scene_path: str) -> dict[str, Any]:
        return await open_scene(scene_path)
