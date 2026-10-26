"""Tests for select_bridge tool."""
from __future__ import annotations

import json
import hashlib
import hmac
import socket
import threading

import pytest

from unity_mcp_py.bridge.control import EndpointSnapshot
from unity_mcp_py.bridge.session import BridgeSession, set_session_for_test
from unity_mcp_py.tools import select_bridge
from unity_mcp_py.tools.select_bridge import (
    _do_select_bridge,
    _scan_bridge_instances,
    _scan_bridge_instances_with_retry,
)


class MockBridgeServer:
    """模拟 Unity bridge 服务器用于测试。"""

    def __init__(self, port: int, project_path: str, scene_name: str) -> None:
        self.port = port
        self.project_path = project_path
        self.scene_name = scene_name
        self.server_socket: socket.socket | None = None
        self.running = False
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """启动模拟服务器。"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", self.port))
        self.server_socket.listen(1)
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """停止模拟服务器。"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        if self.thread:
            self.thread.join(timeout=2)

    def _serve(self) -> None:
        """处理客户端连接。"""
        while self.running:
            try:
                self.server_socket.settimeout(0.5)
                try:
                    client_socket, _ = self.server_socket.accept()
                except socket.timeout:
                    continue

                stream = client_socket.makefile("rwb")
                challenge = json.loads(stream.readline().decode())
                nonce = "n" * 64
                instance_id = f"mock-{self.port}"
                payload = "\n".join(
                    [
                        "unity-mcp-server-v1",
                        challenge["id"],
                        nonce,
                        self.project_path,
                        instance_id,
                    ]
                )
                server_proof = hmac.new(
                    b"t" * 32, payload.encode(), hashlib.sha256
                ).hexdigest()
                challenge_response = {
                    "id": challenge["id"],
                    "ok": True,
                    "result": {
                        "nonce": nonce,
                        "challengeRequestId": challenge["id"],
                        "bridgeId": self.project_path,
                        "bridgeInstanceId": instance_id,
                        "serverProof": server_proof,
                    },
                }
                stream.write((json.dumps(challenge_response) + "\n").encode())
                stream.flush()
                request = json.loads(stream.readline().decode())

                if request.get("command") == "Handshake":
                    response = {
                        "id": request["id"],
                        "ok": True,
                        "result": {
                            "protocolVersion": "1.10.0",
                            "projectPath": self.project_path,
                            "sceneName": self.scene_name,
                            "isPlaying": False,
                            "bridgeId": self.project_path,
                        }
                    }
                elif request.get("command") == "BridgeControl" and request.get("args", {}).get("action") == "status":
                    response = {
                        "id": request["id"],
                        "ok": True,
                        "result": {
                            "bridgeId": self.project_path,
                            "projectPath": self.project_path,
                            "listenerPid": 1234,
                            "clientConnected": False,
                            "connectionGeneration": 0,
                        },
                    }
                else:
                    response = {
                        "id": request["id"],
                        "ok": False,
                        "error": {
                            "code": "UNKNOWN_COMMAND",
                            "message": f"Unknown command: {request.get('command')}"
                        }
                    }

                # 发送响应
                response_line = json.dumps(response) + "\n"
                stream.write(response_line.encode("utf-8"))
                stream.flush()
                stream.close()
                client_socket.close()
            except Exception:
                pass


@pytest.fixture
def mock_bridges():
    """创建两个模拟 bridge 服务器。"""
    base_port = _find_consecutive_ports()
    bridge1 = MockBridgeServer(base_port, "<PROJECT_A_ROOT>", "SceneA")
    bridge2 = MockBridgeServer(base_port + 1, "<PROJECT_B_ROOT>", "SceneB")

    bridge1.start()
    bridge2.start()

    # 等待服务器启动
    import time
    time.sleep(0.2)

    yield bridge1, bridge2

    bridge1.stop()
    bridge2.stop()


def _find_consecutive_ports() -> int:
    for base_port in range(24000, 50000, 2):
        sockets: list[socket.socket] = []
        try:
            for port in (base_port, base_port + 1):
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                candidate.bind(("127.0.0.1", port))
                sockets.append(candidate)
            return base_port
        except OSError:
            continue
        finally:
            for candidate in sockets:
                candidate.close()
    raise RuntimeError("could not find two consecutive test ports")


@pytest.fixture(autouse=True)
def reset_session(monkeypatch, tmp_path):
    """每个测试后重置全局 session。"""
    monkeypatch.setenv("UNITY_MCP_AUTH_TOKEN", "t" * 32)
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(tmp_path / "bindings.json"))
    yield
    set_session_for_test(None)


def test_scan_bridge_instances(mock_bridges):
    """测试扫描 bridge 实例。"""
    bridge1, bridge2 = mock_bridges

    instances = _scan_bridge_instances(base_port=bridge1.port, max_ports=2)

    # 应该找到两个实例
    assert len(instances) == 2

    # 检查第一个实例
    instance1 = next((i for i in instances if i["port"] == bridge1.port), None)
    assert instance1 is not None
    assert instance1["project_path"] == "<PROJECT_A_ROOT>"
    assert instance1["bridge_id"] == "<PROJECT_A_ROOT>"
    assert instance1["client_connected"] is False

    # 检查第二个实例
    instance2 = next((i for i in instances if i["port"] == bridge2.port), None)
    assert instance2 is not None
    assert instance2["project_path"] == "<PROJECT_B_ROOT>"
    assert instance2["bridge_id"] == "<PROJECT_B_ROOT>"


@pytest.mark.asyncio
async def test_select_bridge_scan(mock_bridges):
    """测试通过 select_bridge 扫描。"""
    bridge1, _ = mock_bridges
    result = await _do_select_bridge(
        scan=True, scan_base_port=bridge1.port, scan_max_ports=2
    )

    assert result["action"] == "scan"
    assert result["found_instances"] == 2
    assert len(result["instances"]) == 2


@pytest.mark.asyncio
async def test_select_bridge_status():
    """测试查看连接状态。"""
    # 默认状态应该未连接
    result = await _do_select_bridge()

    assert result["action"] == "status"
    assert result["connected"] is False
    assert result["host"] == "127.0.0.1"
    assert result["port"] == 17873


@pytest.mark.asyncio
async def test_select_bridge_connect(mock_bridges):
    """测试连接到指定端口。"""
    # 连接到端口 17874
    _, bridge2 = mock_bridges
    result = await _do_select_bridge(port=bridge2.port)

    assert result["action"] == "select"
    assert result["success"] is True
    assert result["port"] == bridge2.port
    assert "handshake_result" in result

    # 验证连接后的状态
    status = await _do_select_bridge()
    assert status["connected"] is True
    assert status["port"] == bridge2.port


@pytest.mark.asyncio
async def test_select_bridge_switch(mock_bridges):
    """测试切换连接。"""
    # 连接到第一个实例
    bridge1, bridge2 = mock_bridges
    result1 = await _do_select_bridge(port=bridge1.port)
    assert result1["success"] is True
    assert result1["port"] == bridge1.port

    # 切换到第二个实例
    result2 = await _do_select_bridge(port=bridge2.port)
    assert result2["success"] is True
    assert result2["port"] == bridge2.port

    # 验证当前连接
    status = await _do_select_bridge()
    assert status["port"] == bridge2.port


@pytest.mark.asyncio
async def test_select_bridge_invalid_port():
    """测试连接到无效端口。"""
    result = await _do_select_bridge(port=19999)

    assert result["action"] == "select"
    assert result["success"] is False
    assert "error" in result
    assert "connect to 127.0.0.1:19999 failed" in result["error"]


def test_scan_empty_range():
    """测试扫描没有 bridge 的端口范围。"""
    instances = _scan_bridge_instances(base_port=20000, max_ports=3)
    assert len(instances) == 0


@pytest.mark.asyncio
async def test_multiple_scans(mock_bridges):
    """测试多次扫描应该返回一致结果。"""
    bridge1, _ = mock_bridges
    result1 = await _do_select_bridge(
        scan=True, scan_base_port=bridge1.port, scan_max_ports=2
    )
    result2 = await _do_select_bridge(
        scan=True, scan_base_port=bridge1.port, scan_max_ports=2
    )

    assert result1["found_instances"] == result2["found_instances"]
    assert len(result1["instances"]) == len(result2["instances"])


def test_scan_retry_excludes_confirmed_orphan_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    clock = iter((0.0, 0.0, 0.1, 0.2))

    class FakeControlClient:
        def __init__(self, host: str, port: int, timeout_seconds: float) -> None:
            calls.append(port)

        def status(self) -> dict:
            return {"responsive": False, "errorType": "TimeoutError", "error": "timed out"}

    monkeypatch.setattr(select_bridge, "BridgeControlClient", FakeControlClient)
    monkeypatch.setattr(
        select_bridge,
        "inspect_endpoint",
        lambda host, port, **unused: EndpointSnapshot(
            host, port, listener_pid=36412, listener_process_exists=False
        ),
    )

    instances = _scan_bridge_instances_with_retry(
        base_port=17873,
        max_ports=1,
        wait_seconds=0.15,
        poll_interval=0,
        monotonic=lambda: next(clock),
        sleep=lambda unused: None,
    )

    assert instances == []
    assert calls == [17873]


@pytest.mark.asyncio
async def test_select_bridge_refuses_direct_connection_to_orphan_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        select_bridge,
        "inspect_endpoint",
        lambda host, port: EndpointSnapshot(
            host, port, listener_pid=36412, listener_process_exists=False
        ),
    )

    result = await _do_select_bridge(port=17873)

    assert result["success"] is False
    assert result["errorCode"] == "ORPHAN_LISTENER"
    assert result["recommendedPorts"][0] == 17874


def test_scan_retry_stops_between_ports_when_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    now = [0.0]

    class SlowControlClient:
        def __init__(self, host: str, port: int, timeout_seconds: float) -> None:
            calls.append(port)

        def status(self) -> dict:
            now[0] += 0.2
            return {"responsive": False, "errorType": "ConnectionRefusedError"}

    monkeypatch.setattr(select_bridge, "BridgeControlClient", SlowControlClient)

    result = _scan_bridge_instances_with_retry(
        base_port=17873,
        max_ports=3,
        wait_seconds=0.1,
        monotonic=lambda: now[0],
        sleep=lambda unused: None,
    )

    assert result == []
    assert calls == [17873]


@pytest.mark.asyncio
async def test_select_bridge_rechecks_orphan_before_already_connected_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = BridgeSession(port=17873)
    session._client = object()  # type: ignore[assignment]
    session._handshake_done = True
    set_session_for_test(session)
    monkeypatch.setattr(
        select_bridge,
        "inspect_endpoint",
        lambda host, port, **unused: EndpointSnapshot(
            host, port, listener_pid=36412, listener_process_exists=False
        ),
    )
    monkeypatch.setattr(
        select_bridge.BridgeControlClient,
        "status",
        lambda self: {"responsive": False, "errorType": "TimeoutError"},
    )

    result = await _do_select_bridge(port=17873)

    assert result["success"] is False
    assert result["errorCode"] == "ORPHAN_LISTENER"
