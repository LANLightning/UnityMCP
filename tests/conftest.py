"""Pytest fixtures for unity-mcp-py.

Reuses flow's mock_bridge_server via pytest_plugins; adds unity_bridge_session
that wires a BridgeSession to the mock's ephemeral port and installs it as the
module singleton for the duration of one test.
"""
from __future__ import annotations

import sys
import hashlib
import hmac
import json
from pathlib import Path

import pytest

_FLOW_TESTS = Path.home() / ".claude" / "flow" / "tests"
if str(_FLOW_TESTS) not in sys.path:
    sys.path.insert(0, str(_FLOW_TESTS))

pytest_plugins = ["conftest"]   # pulls mock_bridge_server fixture


@pytest.fixture(autouse=True)
def unity_mcp_test_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNITY_MCP_AUTH_TOKEN", "t" * 32)


@pytest.fixture
async def unity_bridge_session(mock_bridge_server: object) -> object:
    """Yields a BridgeSession wired to the mock server's port.

    Installs the session as module singleton; resets on teardown.
    Handshake is pre-scripted so tools auto-connect cleanly.
    """
    from unity_mcp_py.bridge.session import (
        BridgeSession,
        set_session_for_test,
    )
    original_process = mock_bridge_server._process_request  # type: ignore[attr-defined]

    def process_with_challenge(connection: object, request: dict) -> None:
        if request.get("command") != "BridgeChallenge":
            original_process(connection, request)
            return
        nonce = "n" * 64
        bridge_id = "/mock/project"
        bridge_instance_id = "mock-bridge-instance"
        payload = "\n".join(
            ["unity-mcp-server-v1", request["id"], nonce, bridge_id, bridge_instance_id]
        )
        proof = hmac.new(b"t" * 32, payload.encode(), hashlib.sha256).hexdigest()
        response = {
            "id": request["id"],
            "ok": True,
            "result": {
                "nonce": nonce,
                "challengeRequestId": request["id"],
                "bridgeId": bridge_id,
                "bridgeInstanceId": bridge_instance_id,
                "serverProof": proof,
            },
        }
        connection.sendall((json.dumps(response) + "\n").encode())

    mock_bridge_server._process_request = process_with_challenge  # type: ignore[attr-defined]
    mock_bridge_server.expect_handshake_then_respond(version="1.10.0")  # type: ignore[attr-defined]
    session = BridgeSession(host="127.0.0.1", port=mock_bridge_server.port)  # type: ignore[attr-defined]
    set_session_for_test(session)
    try:
        yield session
    finally:
        await session.close()
        set_session_for_test(None)
