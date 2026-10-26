"""Tests for BridgeSession async wrapper and singleton."""
import asyncio

import pytest

from unity_mcp_py.bridge import bridge_client
from unity_mcp_py.bridge.session import BridgeSession, get_session, set_session_for_test


@pytest.mark.asyncio
async def test_session_singleton_default_port() -> None:
    set_session_for_test(None)
    s1 = get_session()
    s2 = get_session()
    assert s1 is s2
    assert s1.port == 17873


@pytest.mark.asyncio
async def test_session_connect_and_send_via_mock(
    unity_bridge_session: BridgeSession,
    mock_bridge_server: object,
) -> None:
    mock_bridge_server.expect_request("Ping").respond_with({"pong": True})  # type: ignore[attr-defined]
    result = await unity_bridge_session.send("Ping")
    assert result == {"pong": True}


@pytest.mark.asyncio
async def test_session_send_serialized(
    unity_bridge_session: BridgeSession,
    mock_bridge_server: object,
) -> None:
    """Concurrent sends should serialize through _send_lock."""
    mock_bridge_server.expect_request("A").respond_with({"x": 1})  # type: ignore[attr-defined]
    mock_bridge_server.expect_request("B").respond_with({"x": 2})  # type: ignore[attr-defined]
    r1, r2 = await asyncio.gather(
        unity_bridge_session.send("A"),
        unity_bridge_session.send("B"),
    )
    assert {r1["x"], r2["x"]} == {1, 2}


class _DisconnectingClient:
    def __init__(self) -> None:
        self.closed = False

    def send(self, command: str, args: dict, response_timeout: float) -> dict:
        raise bridge_client.BridgeConnectionError("socket closed by peer (EOF)")

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_session_resets_stale_client_after_connection_error() -> None:
    session = BridgeSession(host="127.0.0.1", port=17873)
    fake_client = _DisconnectingClient()
    session._client = fake_client  # type: ignore[assignment]
    session._handshake_done = True

    with pytest.raises(bridge_client.BridgeConnectionError):
        await session.send("RunEditMethod", {})

    assert fake_client.closed is True
    assert session._client is None
    assert session._handshake_done is False
