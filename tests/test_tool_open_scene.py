"""Tests for open_scene tool."""
import pytest

from unity_mcp_py.state import GUARD
from unity_mcp_py.tools.open_scene import InvalidScenePathError, open_scene


@pytest.mark.asyncio
async def test_open_scene_issues_token(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """open_scene must return bridge result with write_op_token and correct line_number_before."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 80}]})
    bridge.expect_request("OpenScene").respond_with({"loadedScene": "Assets/Test.unity"})

    result = await open_scene("Assets/Test.unity")

    assert result["loadedScene"] == "Assets/Test.unity"
    assert "write_op_token" in result
    assert result["line_number_before"] == 80
    assert result["requires_observation"] is True

    # Token must be registered in GUARD
    pending = await GUARD.list_pending()
    assert any(p.token == result["write_op_token"] for p in pending)

    # Cleanup
    await GUARD.consume([result["write_op_token"]])


# ---------------------------------------------------------------------------
# MEDIUM fix (2026-05-13): scene_path validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        r"\\attacker\share\evil.unity",        # UNC (backslash)
        r"//attacker/share/evil.unity",        # UNC (forward slash)
        r"C:\Windows\System32\evil.unity",     # absolute with drive
        r"/etc/passwd.unity",                  # absolute POSIX
        r"Assets/../../outside.unity",         # traversal
        r"Assets/../.." + "\\etc\\evil.unity", # mixed traversal
        r"Assets/Test.txt",                    # wrong extension
        r"",                                   # empty
        "Assets/Test\x00.unity",               # NUL byte
    ],
)
@pytest.mark.asyncio
async def test_open_scene_rejects_bad_paths(bad_path: str) -> None:
    """Non-relative, UNC, traversal, and wrong-extension paths are rejected.

    Fixtures are deliberately NOT listed: the validator must fire before any
    bridge connection is attempted.
    """
    with pytest.raises(InvalidScenePathError):
        await open_scene(bad_path)

    pending = await GUARD.list_pending()
    assert not any(p.tool_name == "open_scene" for p in pending)


@pytest.mark.asyncio
async def test_open_scene_allows_packages_relative_path(
    unity_bridge_session: object,
    mock_bridge_server: object,
) -> None:
    """Packages/ subtree is valid like Assets/ — anchored relative paths allowed."""
    bridge = mock_bridge_server
    bridge.expect_request("TailEditorLog").respond_with({"lines": [{"lineNumber": 90}]})
    bridge.expect_request("OpenScene").respond_with(
        {"loadedScene": "Packages/com.example/Scenes/Demo.unity"}
    )

    result = await open_scene("Packages/com.example/Scenes/Demo.unity")

    assert result["loadedScene"] == "Packages/com.example/Scenes/Demo.unity"
    assert "write_op_token" in result

    await GUARD.consume([result["write_op_token"]])
