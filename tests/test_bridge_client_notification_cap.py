"""Tests for the notification-queue overflow cap and protocol fallback.

The unity-mcp-py server imports BridgeClient from ~/.claude/flow/lib via the
bridge adapter; this suite pins the contract from the consumer's side so a
flow-side regression here trips unity-mcp-py CI immediately.
"""
from __future__ import annotations

from pathlib import Path

from unity_mcp_py.bridge import bridge_client


def test_notifications_have_default_cap():
    """BridgeClient initializes with a non-empty default notification cap."""
    bc = bridge_client.BridgeClient()
    cap = getattr(bc, "_notifications_max", None)
    assert cap is not None, "BridgeClient must expose _notifications_max"
    assert cap >= 1000, f"default cap too small: {cap}"


def test_trim_notifications_drops_oldest():
    """When the queue exceeds the cap, the oldest entries are dropped."""
    bc = bridge_client.BridgeClient()
    bc._notifications_max = 5  # type: ignore[attr-defined]
    for i in range(10):
        bc._notifications.append({"event": "log.line", "i": i})
    bc._trim_notifications()  # type: ignore[attr-defined]
    assert len(bc._notifications) == 5
    # Oldest dropped: surviving entries are i=5..9
    indices = [n["i"] for n in bc._notifications]
    assert indices == [5, 6, 7, 8, 9]


def test_handshake_fallback_version_is_1_9_0():
    """When no schema path is reachable, the hardcoded fallback must match the
    bridge's live protocol (1.10.0). A stale fallback would trip
    PROTOCOL_VERSION_MISMATCH on cold-start environments."""
    bc = bridge_client.BridgeClient(
        schema_path=Path("/definitely-nonexistent-path/protocol.schema.json")
    )
    assert bc._read_protocol_version() == "1.10.0"
