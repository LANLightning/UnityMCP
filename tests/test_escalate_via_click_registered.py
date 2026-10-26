"""escalate_via_click must be registered as a live MCP tool, not just importable.

Mirrors tests/test_verify_assertions_registered.py — registration is verified via
the recorder REGISTRY (the proven pattern in this repo), not an async list_tools.
"""
from __future__ import annotations


def test_escalate_via_click_is_registered():
    from unity_mcp_py.server import register_tools
    from unity_mcp_py.recorder import REGISTRY

    register_tools()
    assert REGISTRY.get("escalate_via_click") is not None
