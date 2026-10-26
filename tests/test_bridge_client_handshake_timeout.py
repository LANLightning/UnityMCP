"""Handshake must fail fast, not inherit the 30s catch-all response timeout.

Telemetry (575 real calls) showed ~330s of aggregate wall-time was a handful of
`send('Handshake') timed out after 30.0s` tails: a dead/stalled Editor made the
first call of a session block the full 30s before erroring. The connect() socket
timeout is short (5s) but the *handshake response* used send()'s 30s default.

This suite pins the contract: handshake() must pass an explicit short
response_timeout so a non-responsive bridge fails in seconds, not 30s.

SYNC GUARD: src/unity_mcp_py/_vendor/bridge_client.py is a copy synced (-Force)
from ~/.claude/flow/lib/bridge_client.py via scripts/sync_from_flow.ps1. The flow
source still ships the 30s-default handshake. If someone re-runs that sync WITHOUT
first upstreaming this fix, these tests go RED — that red is the intended signal
to re-apply the short-timeout change to the vendored copy (and ideally upstream).
"""
from __future__ import annotations

from unity_mcp_py.bridge import bridge_client


class _SpySend:
    """Records the response_timeout passed to each send() call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def __call__(
        self, command: str, args: dict | None = None, response_timeout: float = 30.0
    ) -> dict:
        self.calls.append((command, response_timeout))
        return {"editorVersion": "test", "projectPath": "/x"}


def test_handshake_uses_short_response_timeout(monkeypatch) -> None:
    """handshake() must NOT inherit send()'s 30s default.

    A handshake against a stalled Editor should fail in a few seconds. We assert
    the response_timeout it passes is the short handshake constant, well under the
    30s catch-all.
    """
    bc = bridge_client.BridgeClient()
    spy = _SpySend()
    monkeypatch.setattr(bc, "send", spy)

    bc.handshake()

    assert len(spy.calls) == 1, "handshake should issue exactly one send"
    command, timeout = spy.calls[0]
    assert command == "Handshake"
    assert timeout <= 5.0, (
        f"handshake response_timeout={timeout}s — must be the short handshake "
        f"timeout (<=5s), not the 30s catch-all default"
    )
    assert timeout > 0, "handshake timeout must be positive"


def test_handshake_timeout_constant_is_exported() -> None:
    """The handshake response timeout is a named constant (tunable, documented),
    not a magic literal buried in the call."""
    val = getattr(bridge_client, "HANDSHAKE_RESPONSE_TIMEOUT_SECONDS", None)
    assert val is not None, (
        "bridge_client must export HANDSHAKE_RESPONSE_TIMEOUT_SECONDS"
    )
    # 5s: a busy Editor servicing EditorApplication.update at a few Hz mid-compile
    # can legitimately need >3s to accept+service the handshake; 5s is the safe
    # floor (matches the existing connect-phase _HANDSHAKE_TIMEOUT_SECONDS).
    assert 3.0 <= val <= 10.0, f"handshake timeout {val}s outside sane [3,10] range"
