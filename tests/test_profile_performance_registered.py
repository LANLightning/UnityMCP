"""profile_performance must register ONLY when UNITY_MCP_ENABLE_PROFILER=1."""
from __future__ import annotations

import importlib

import pytest


def _fresh_register(monkeypatch, flag_value):
    """Re-import server and run register_tools under a controlled env flag.

    register_tools() mirrors tools into the module-global REGISTRY, so we read
    that to assert presence/absence. We reload the server module to reset its
    one-time import state between flag values.
    """
    if flag_value is None:
        monkeypatch.delenv("UNITY_MCP_ENABLE_PROFILER", raising=False)
    else:
        monkeypatch.setenv("UNITY_MCP_ENABLE_PROFILER", flag_value)
    import unity_mcp_py.server as server
    importlib.reload(server)
    from unity_mcp_py.recorder import REGISTRY
    REGISTRY.clear()   # required: singleton accumulates across tests (see Step 0)
    server.register_tools()
    return REGISTRY


def test_profiler_absent_when_flag_off(monkeypatch):
    reg = _fresh_register(monkeypatch, None)
    assert reg.get("profile_performance") is None


def test_profiler_absent_when_flag_zero(monkeypatch):
    reg = _fresh_register(monkeypatch, "0")
    assert reg.get("profile_performance") is None


def test_profiler_present_when_flag_one(monkeypatch):
    reg = _fresh_register(monkeypatch, "1")
    assert reg.get("profile_performance") is not None


def test_feature_enabled_only_exact_one(monkeypatch):
    from unity_mcp_py.server import _feature_enabled
    for val, expected in [("1", True), ("0", False), ("true", False), ("", False)]:
        monkeypatch.setenv("UNITY_MCP_ENABLE_PROFILER", val)
        assert _feature_enabled("UNITY_MCP_ENABLE_PROFILER") is expected
    monkeypatch.delenv("UNITY_MCP_ENABLE_PROFILER", raising=False)
    assert _feature_enabled("UNITY_MCP_ENABLE_PROFILER") is False
