"""Tests for BridgeSession host validation (MEDIUM security fix 2026-05-13)."""
from __future__ import annotations

import pytest

from unity_mcp_py.bridge.session import BridgeSession, NonLoopbackBridgeHostError


# ---------------------------------------------------------------------------
# Loopback allow / non-loopback reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loopback_host",
    ["127.0.0.1", "127.0.0.2", "::1"],
)
def test_bridge_session_accepts_loopback(loopback_host: str) -> None:
    """All addresses in 127.0.0.0/8 and ::1 are allowed without bypass env."""
    session = BridgeSession(host=loopback_host, port=17873)
    assert session.host == loopback_host


@pytest.mark.parametrize(
    "nonloopback_host",
    [
        "10.0.0.1",
        "192.168.1.5",
        "8.8.8.8",
        "0.0.0.0",
        "169.254.1.1",   # link-local IPv4
        "localhost",     # names are rejected to avoid DNS rebinding
    ],
)
def test_bridge_session_rejects_nonloopback(
    nonloopback_host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any non-loopback address must raise NonLoopbackBridgeHostError."""
    monkeypatch.delenv("UNITY_MCP_ALLOW_NONLOOPBACK", raising=False)
    with pytest.raises(NonLoopbackBridgeHostError, match="loopback"):
        BridgeSession(host=nonloopback_host, port=17873)


def test_bridge_session_bypass_env_does_not_allow_nonloopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote plaintext transport stays disabled even if a legacy env var exists."""
    monkeypatch.setenv("UNITY_MCP_ALLOW_NONLOOPBACK", "1")
    with pytest.raises(NonLoopbackBridgeHostError):
        BridgeSession(host="10.20.30.40", port=17873)


def test_bridge_session_rejects_unresolvable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host names are rejected; only literal loopback addresses are supported."""
    monkeypatch.delenv("UNITY_MCP_ALLOW_NONLOOPBACK", raising=False)
    with pytest.raises(NonLoopbackBridgeHostError):
        # An RFC-2606 reserved name that never resolves.
        BridgeSession(host="invalid.example", port=17873)
