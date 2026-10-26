"""Tests verifying the project scaffolding and basic imports."""
import importlib


def test_package_imports() -> None:
    mod = importlib.import_module("unity_mcp_py")
    assert hasattr(mod, "__version__")


def test_server_module_imports_without_starting() -> None:
    mod = importlib.import_module("unity_mcp_py.server")
    assert hasattr(mod, "mcp")
    assert hasattr(mod, "register_tools")
    # register_tools must be idempotent and not require bridge yet
    mod.register_tools()


def test_bridge_adapter_reexports_flow_libs() -> None:
    from unity_mcp_py import bridge
    assert hasattr(bridge, "bridge_client")
    assert hasattr(bridge, "human_collab")
