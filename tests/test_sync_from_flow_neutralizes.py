"""Guards that sync_from_flow.ps1 contains the H1 self-heal: after copying the
upstream bridge_client.py it rewrites the PROJECT_EDITOR path to UNITY_MCP_SCHEMA_PATH and
throws if any dev fingerprint survives. See spec §3 H1.
"""
from __future__ import annotations

from pathlib import Path

_SCRIPT = (Path(__file__).resolve().parent.parent / "scripts" / "sync_from_flow.ps1").read_text(
    encoding="utf-8"
)


def test_script_rewrites_to_env_var():
    assert "UNITY_MCP_SCHEMA_PATH" in _SCRIPT, "sync script must inject the env-var read"


def test_script_has_fingerprint_guard():
    # The guard must throw on a surviving fingerprint.
    assert "EXAMPLE_DEVELOPER_IDENTIFIER" in _SCRIPT and "throw" in _SCRIPT, "sync script must fail loud on fingerprint"
