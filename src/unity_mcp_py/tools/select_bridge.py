"""select_bridge tool — 选择和绑定到特定的 Unity bridge 实例。

当有多个 Unity 编辑器实例运行时，使用此工具选择要连接的 bridge。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from mcp.server.fastmcp import Context

from ..bridge.control import BridgeControlClient, inspect_endpoint
from ..bridge.session import (
    BridgeSession,
    get_session_async,
    switch_session,
)

logger = logging.getLogger(__name__)


def _scan_bridge_instances(
    base_port: int = 17873, max_ports: int = 10,
    excluded_ports: set[int] | None = None,
    *,
    deadline: float | None = None,
    monotonic: Any = time.monotonic,
) -> list[dict[str, Any]]:
    """扫描本地端口范围，不占用 bridge 的业务连接槽。

    Returns:
        List of dicts with keys: port, project_path, scene_name, is_playing
    """
    instances: list[dict[str, Any]] = []
    host = "127.0.0.1"
    for port in range(base_port, base_port + max_ports):
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            break
        if excluded_ports is not None and port in excluded_ports:
            continue
        probe_timeout = 0.5 if remaining is None else min(0.5, remaining)
        result = BridgeControlClient(
            host, port, timeout_seconds=probe_timeout
        ).status()
        if not result.get("responsive") or not result.get("success"):
            if result.get("errorType") == "TimeoutError":
                inspect_remaining = (
                    None if deadline is None else deadline - monotonic()
                )
                if inspect_remaining is not None and inspect_remaining <= 0:
                    break
                inspect_timeout = (
                    5.0 if inspect_remaining is None else min(5.0, inspect_remaining)
                )
                snapshot = inspect_endpoint(
                    host, port, timeout_seconds=inspect_timeout
                )
                if (
                    excluded_ports is not None
                    and snapshot.listener_process_exists is False
                ):
                    excluded_ports.add(port)
            continue
        instances.append({
            "port": port,
            "bridge_id": result.get("bridgeId"),
            "project_path": result.get("projectPath", "unknown"),
            "listener_pid": result.get("listenerPid"),
            "client_connected": result.get("clientConnected", False),
            "client_id": result.get("clientId"),
            "connection_generation": result.get("connectionGeneration", 0),
        })

    return instances


def _scan_bridge_instances_with_retry(
    base_port: int = 17873,
    max_ports: int = 10,
    wait_seconds: float = 15.0,
    poll_interval: float = 0.5,
    *,
    monotonic: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> list[dict[str, Any]]:
    """Scan until an instance appears or a bounded wait expires.

    Confirmed orphan listeners are excluded after the first probe so retries do
    not accumulate dead TCP connections while Unity compiles or reloads domains.
    """
    if isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 120:
        raise ValueError("wait_seconds must be between 0 and 120")
    if isinstance(poll_interval, bool) or not 0 <= poll_interval <= 5:
        raise ValueError("poll_interval must be between 0 and 5")

    excluded_ports: set[int] = set()
    deadline = monotonic() + wait_seconds
    while True:
        instances = _scan_bridge_instances(
            base_port,
            max_ports,
            excluded_ports,
            deadline=deadline,
            monotonic=monotonic,
        )
        now = monotonic()
        if instances or now >= deadline:
            return instances
        sleep(min(poll_interval, deadline - now))


async def _do_select_bridge(
    port: int | None = None,
    scan: bool = False,
    expected_bridge_id: str | None = None,
    force_takeover: bool = False,
    client_id: str | None = None,
    scan_base_port: int = 17873,
    scan_max_ports: int = 10,
    wait_seconds: float = 15.0,
) -> dict[str, Any]:
    """实现 select_bridge 工具的核心逻辑。

    Args:
        port: 要连接的端口号。如果为 None，则显示当前连接状态。
        scan: 是否扫描所有可用的 bridge 实例。

    Returns:
        包含操作结果的字典。
    """
    # 如果请求扫描
    if scan:
        instances = await asyncio.to_thread(
            _scan_bridge_instances_with_retry,
            scan_base_port,
            scan_max_ports,
            wait_seconds,
        )
        return {
            "action": "scan",
            "found_instances": len(instances),
            "instances": instances,
            "message": f"找到 {len(instances)} 个 Unity bridge 实例"
        }

    # 如果没有指定端口，显示当前状态
    if port is None:
        current_session = await get_session_async()
        is_connected = (
            current_session._client is not None
            and current_session._handshake_done
        )
        return {
            "action": "status",
            "connected": is_connected,
            "host": current_session.host,
            "port": current_session.port,
            "message": (
                f"当前{'已连接' if is_connected else '未连接'}到 "
                f"{current_session.host}:{current_session.port}"
            )
        }

    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        return {
            "action": "select",
            "success": False,
            "errorCode": "INVALID_ARGUMENT",
            "error": "port must be an integer in 1..65535",
        }

    current_session = await get_session_async()
    snapshot = await asyncio.to_thread(inspect_endpoint, "127.0.0.1", port)
    if snapshot.listener_process_exists is False:
        control = await asyncio.to_thread(
            lambda: BridgeControlClient(
                "127.0.0.1", port, timeout_seconds=0.5
            ).status()
        )
        if not control.get("success"):
            return {
                "action": "select",
                "success": False,
                "port": port,
                "errorCode": "ORPHAN_LISTENER",
                "error": "the listener owner process has exited",
                "listenerPid": snapshot.listener_pid,
                "fallbackScanRecommended": True,
                "recommendedPorts": list(
                    range(port + 1, min(port + 11, 65536))
                ),
                "message": "该端口是 Windows 孤儿 listener；请扫描并连接 Unity 自动回退的新端口。",
            }

    if (
        current_session.port == port
        and current_session._client is not None
        and current_session._handshake_done
        and (expected_bridge_id is None or current_session.bridge_id == expected_bridge_id)
        and (client_id is None or current_session.client_id == client_id)
    ):
        return {
            "action": "select",
            "success": True,
            "already_connected": True,
            "port": port,
            "bridge_id": current_session.bridge_id,
            "message": f"已连接到端口 {port} 的 Unity bridge",
        }


    takeover_status: dict[str, Any] | None = None
    if force_takeover:
        if not expected_bridge_id or not client_id:
            return {
                "action": "select",
                "success": False,
                "port": port,
                "error": "force_takeover requires expected_bridge_id and client_id",
            }
        takeover_status = await asyncio.to_thread(
            BridgeControlClient("127.0.0.1", port).status
        )
        if (
            not takeover_status.get("success")
            or takeover_status.get("bridgeId") != expected_bridge_id
            or takeover_status.get("clientId") != client_id
        ):
            return {
                "action": "select",
                "success": False,
                "port": port,
                "error": "bridge/client identity changed; refusing force takeover",
                "status": takeover_status,
            }

    new_session = BridgeSession(
        host="127.0.0.1",
        port=port,
        client_id=client_id,
        expected_bridge_id=expected_bridge_id,
        force_takeover=force_takeover,
        expected_bridge_instance_id=(
            takeover_status.get("bridgeInstanceId") if takeover_status else None
        ),
        expected_connection_generation=(
            takeover_status.get("connectionGeneration") if takeover_status else None
        ),
    )
    try:
        handshake_result = await switch_session(new_session)
        return {
            "action": "select",
            "success": True,
            "port": port,
            "handshake_result": handshake_result,
            "message": f"成功连接到端口 {port} 的 Unity bridge"
        }
    except Exception as e:
        return {
            "action": "select",
            "success": False,
            "port": port,
            "error": str(e),
            "message": f"连接到端口 {port} 失败: {e}"
        }


def register(mcp_instance: Any) -> None:
    """注册 select_bridge 工具到 FastMCP 实例。"""

    @mcp_instance.tool(name="select_bridge")
    async def select_bridge(
        ctx: Context,
        port: int | None = None,
        scan: bool = False,
        expected_bridge_id: str | None = None,
        force_takeover: bool = False,
        client_id: str | None = None,
        wait_seconds: float = 15.0,
    ) -> dict[str, Any]:
        """选择和绑定到特定的 Unity bridge 实例。

        当有多个 Unity 编辑器实例运行时使用此工具。每个实例应该配置不同的端口。

        Args:
            port: 要连接的 Unity bridge 端口号。如果不指定，显示当前连接状态。
            scan: 设为 True 时扫描并列出所有可用的 bridge 实例（端口 17873-17882）。

        Returns:
            操作结果，包含连接状态或扫描到的实例列表。

        Examples:
            # 扫描所有可用的 bridge 实例
            select_bridge(scan=True)

            # 查看当前连接状态
            select_bridge()

            # 连接到端口 17874 的 Unity 实例
            select_bridge(port=17874)
        """
        return await _do_select_bridge(
            port=port,
            scan=scan,
            expected_bridge_id=expected_bridge_id,
            force_takeover=force_takeover,
            client_id=client_id,
            wait_seconds=wait_seconds,
        )
