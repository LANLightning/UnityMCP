"""Guards H1: the vendored bridge client must NOT carry a hardcoded dev-machine
schema path, and must honor UNITY_MCP_SCHEMA_PATH. See
docs/specs/2026-06-09-deployment-bundle-design.md §3 H1.
"""
from __future__ import annotations

from pathlib import Path

from unity_mcp_py.bridge import bridge_client

_VENDOR_SRC = Path(bridge_client.__file__).read_text(encoding="utf-8")


def test_no_dev_fingerprint_in_source():
    """No developer-machine path may ship in the vendored client."""
    for needle in ("EXAMPLE_DEVELOPER_IDENTIFIER", "EXAMPLE_MACHINE_PREFIX", "EXAMPLE_MACHINE_IDENTIFIER"):
        assert needle not in _VENDOR_SRC, f"dev fingerprint {needle!r} still in bridge_client.py"


def test_env_schema_path_is_honored(tmp_path, monkeypatch):
    """When UNITY_MCP_SCHEMA_PATH points at a file and no shared file exists,
    the client resolves _schema_path to it."""
    schema = tmp_path / "protocol.schema.json"
    schema.write_text('{"protocolVersion": "1.8.0"}', encoding="utf-8")
    monkeypatch.setenv("UNITY_MCP_SCHEMA_PATH", str(schema))
    # Force the user-home shared path to miss by pointing HOME at an empty dir.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no_home"))
    bc = bridge_client.BridgeClient()
    assert bc._schema_path == schema


def test_no_env_and_no_shared_resolves_none(tmp_path, monkeypatch):
    """No env var + no user-home shared file => _schema_path is None
    (handshake then uses the pinned fallback version)."""
    monkeypatch.delenv("UNITY_MCP_SCHEMA_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no_home"))
    bc = bridge_client.BridgeClient()
    assert bc._schema_path is None


def test_explicit_schema_path_arg_wins(tmp_path, monkeypatch):
    """An explicit schema_path arg overrides env var and shared path."""
    explicit = tmp_path / "explicit.json"
    explicit.write_text('{"protocolVersion": "1.8.0"}', encoding="utf-8")
    other = tmp_path / "env.json"
    other.write_text('{"protocolVersion": "1.8.0"}', encoding="utf-8")
    monkeypatch.setenv("UNITY_MCP_SCHEMA_PATH", str(other))
    bc = bridge_client.BridgeClient(schema_path=explicit)
    assert bc._schema_path == explicit
