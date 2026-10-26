"""verify_assertions must be registered as a live MCP tool, not just importable."""
from __future__ import annotations


def test_verify_assertions_is_registered():
    from unity_mcp_py.server import register_tools
    from unity_mcp_py.recorder import REGISTRY

    register_tools()
    assert REGISTRY.get("verify_assertions") is not None
