"""bind_bridge tool — 将 Unity bridge 绑定到当前 MCP 会话。

这是一个会话级的绑定命令，当有多个 Unity 编辑器实例运行时使用。
与 select_bridge 不同，这个工具会记住当前会话的绑定关系。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import Context

from ..bridge.control import BridgeBindingStore, BridgeControlClient
from ..bridge.session import (
    BridgeSession,
    close_current_session,
    commit_connected_session,
    get_session_async,
    is_current_session,
)

logger = logging.getLogger(__name__)

# 会话绑定持久化文件
_BINDING_OPERATION_LOCK = asyncio.Lock()

async def _run_binding_mutation(
    function: Any,
    *args: Any,
    rollback_on_cancel: Any | None = None,
) -> Any:
    """Finish file mutations before releasing the operation lock on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        with anyio.CancelScope(shield=True):
            try:
                result = await task
            except Exception:
                logger.exception("Binding mutation failed while its caller was cancelled")
            else:
                if rollback_on_cancel is not None:
                    rollback_task = asyncio.create_task(
                        asyncio.to_thread(rollback_on_cancel, result)
                    )
                    try:
                        await rollback_task
                    except Exception:
                        logger.exception("Binding cancellation rollback failed")
        raise cancelled


async def _clear_binding_and_disconnect(
    store: BridgeBindingStore,
    session_id: str,
    binding: Any,
    active: BridgeSession,
) -> tuple[bool, bool]:
    """Atomically finish or compensate unbind across cancellation."""
    cleared = await _run_binding_mutation(
        store.clear_if_current,
        session_id,
        binding.revision,
        rollback_on_cancel=lambda did_clear: (
            not did_clear or store.restore_if_absent(session_id, binding)
        ),
    )
    if not cleared:
        return False, False

    try:
        disconnected = False
        if active.client_id == session_id and active.port == binding.port:
            disconnected = await close_current_session(
                expected_session=active,
                expected_port=binding.port,
                expected_client_id=session_id,
            )
        return True, disconnected
    except asyncio.CancelledError:
        # close_current_session detaches before aborting. If the exact session is
        # still current, cancellation happened in the clear->close window and the
        # durable delete must be compensated. A newer binding is never overwritten.
        with anyio.CancelScope(shield=True):
            if await is_current_session(active):
                await _run_binding_mutation(
                    store.restore_if_absent, session_id, binding
                )
        raise


_BINDINGS_FILE = Path.home() / ".claude" / "mcp-servers" / "unity-mcp" / "session_bindings.json"


def _load_bindings() -> dict[str, int]:
    """加载会话绑定关系，并清理失效的临时会话。

    Returns:
        Dict mapping session_id -> port
    """
    if not _BINDINGS_FILE.exists():
        return {}
    try:
        bindings = json.loads(_BINDINGS_FILE.read_text(encoding="utf-8"))
        # BUGFIX (2026-08-07): 清理失效的临时会话（temp-<pid> 格式）
        # 临时会话只在进程生命周期内有效，重启后这些绑定已失效
        current_pid = os.getpid()
        cleaned_bindings = {}
        for session_id, port in bindings.items():
            # 保留非临时会话和当前进程的临时会话
            if not session_id.startswith("temp-"):
                cleaned_bindings[session_id] = port
            elif session_id == f"temp-{current_pid}":
                cleaned_bindings[session_id] = port
            else:
                logger.debug(f"Cleaned stale temp session binding: {session_id} -> {port}")

        # 如果清理后有变化，立即保存
        if len(cleaned_bindings) != len(bindings):
            _save_bindings(cleaned_bindings)
            logger.info(f"Cleaned {len(bindings) - len(cleaned_bindings)} stale session bindings")

        return cleaned_bindings
    except Exception as e:
        logger.warning(f"Failed to load session bindings: {e}")
        return {}


def _save_bindings(bindings: dict[str, int]) -> None:
    """保存会话绑定关系。"""
    try:
        _BINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BINDINGS_FILE.write_text(json.dumps(bindings, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save session bindings: {e}")


def _get_session_id(ctx: Context) -> str:
    """获取当前 MCP 会话的唯一标识。

    优先级：
    1. 环境变量 CLAUDE_SESSION_ID（Claude Code 会设置）
    2. Context 中的会话信息
    3. 生成临时 ID
    """
    # 方法 1: 环境变量
    session_id = os.environ.get("UNITY_MCP_CLIENT_ID")
    if session_id:
        return session_id
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    if session_id:
        return session_id

    # 方法 2: Context 中的信息
    # MCP Context 可能包含客户端信息
    if hasattr(ctx, "session_id"):
        return str(ctx.session_id)

    # 方法 3: 使用进程 ID 作为临时标识
    # 注意：这意味着重启 MCP server 会丢失绑定
    return f"temp-{os.getpid()}"


async def _do_bind_bridge(
    ctx: Context,
    port: int | None = None,
    auto: bool = False,
    expected_bridge_id: str | None = None,
    force_takeover: bool = False,
    client_id: str | None = None,
    wait_seconds: float = 30.0,
) -> dict[str, Any]:
    """Serialize binding mutations so bind and unbind cannot cross in-process."""
    async with _BINDING_OPERATION_LOCK:
        return await _do_bind_bridge_locked(
            ctx,
            port,
            auto,
            expected_bridge_id,
            force_takeover,
            client_id,
            wait_seconds,
        )


async def _do_bind_bridge_locked(
    ctx: Context,
    port: int | None = None,
    auto: bool = False,
    expected_bridge_id: str | None = None,
    force_takeover: bool = False,
    client_id: str | None = None,
    wait_seconds: float = 30.0,
) -> dict[str, Any]:
    """实现 bind_bridge 工具的核心逻辑。

    Args:
        ctx: MCP Context，用于获取会话信息
        port: 要绑定的端口号。如果为 None 且 auto=False，显示当前绑定。
        auto: 自动检测并绑定唯一可用的 bridge。

    Returns:
        包含操作结果的字典。
    """
    session_id = client_id.strip() if client_id is not None else _get_session_id(ctx)
    if not session_id:
        return {
            "action": "bind",
            "success": False,
            "errorCode": "CLIENT_ID_REQUIRED",
            "error": "client_id must not be blank",
        }
    store = BridgeBindingStore()
    binding = await asyncio.to_thread(
        _load_or_migrate_binding, store, session_id
    )

    # 如果没有指定端口且不是自动模式，显示当前绑定
    if port is None and not auto:
        current_port = binding.port if binding else None
        current_session = await get_session_async()

        return {
            "action": "status",
            "session_id": session_id,
            "bound_port": current_port,
            "current_port": current_session.port,
            "is_bound": current_port is not None,
            "is_connected": (
                current_session._client is not None
                and current_session._handshake_done
            ),
            "message": (
                f"会话 {session_id} "
                + (f"已绑定到端口 {current_port}" if current_port else "未绑定")
            )
        }

    # 自动模式：扫描并绑定唯一可用的实例
    if auto:
        from .select_bridge import _scan_bridge_instances_with_retry
        instances = await asyncio.to_thread(
            _scan_bridge_instances_with_retry, 17873, 10, wait_seconds
        )

        if len(instances) == 0:
            return {
                "action": "auto",
                "success": False,
                "message": "未找到任何 Unity bridge 实例"
            }
        elif len(instances) == 1:
            port = instances[0]["port"]
            expected_bridge_id = expected_bridge_id or instances[0].get("bridge_id")
        else:
            # 多个实例时，返回列表让用户选择
            return {
                "action": "auto",
                "success": False,
                "found_instances": len(instances),
                "instances": instances,
                "message": (
                    f"找到 {len(instances)} 个 Unity 实例，"
                    "请使用 bind_bridge(port=...) 明确指定要绑定的端口"
                )
            }

    # 绑定到指定端口
    assert port is not None

    current_session = await get_session_async()
    if (
        current_session.port == port
        and current_session._client is not None
        and current_session._handshake_done
        and (
            expected_bridge_id is None
            or current_session.bridge_id == expected_bridge_id
        )
    ):
        previous_binding = binding
        stored = await _run_binding_mutation(
            store.pair,
            session_id,
            current_session.host,
            port,
            current_session.bridge_id or expected_bridge_id,
            rollback_on_cancel=lambda written: store.restore_if_current(
                session_id, written.revision, previous_binding
            ),
        )
        if not await is_current_session(current_session):
            await _run_binding_mutation(
                store.restore_if_current,
                session_id,
                stored.revision,
                previous_binding,
            )
            return {
                "action": "bind",
                "success": False,
                "errorCode": "SESSION_CHANGED",
                "session_id": session_id,
                "port": port,
                "message": "active bridge session changed while persisting binding",
            }
        return {
            "action": "bind",
            "success": True,
            "already_connected": True,
            "session_id": session_id,
            "port": port,
            "binding": asdict(stored),
            "message": f"会话 {session_id} 已绑定到端口 {port}",
        }

    takeover_status: dict[str, Any] | None = None
    if force_takeover:
        if not expected_bridge_id:
            return {
                "action": "bind",
                "success": False,
                "session_id": session_id,
                "port": port,
                "error": "force_takeover requires expected_bridge_id",
            }
        takeover_status = await asyncio.to_thread(
            BridgeControlClient("127.0.0.1", port).status
        )
        if (
            not takeover_status.get("success")
            or takeover_status.get("bridgeId") != expected_bridge_id
            or takeover_status.get("clientId") != session_id
        ):
            return {
                "action": "bind",
                "success": False,
                "session_id": session_id,
                "port": port,
                "error": "bridge/client identity changed; refusing force takeover",
                "status": takeover_status,
            }

    new_session = BridgeSession(
        host="127.0.0.1",
        port=port,
        client_id=session_id,
        expected_bridge_id=expected_bridge_id,
        force_takeover=force_takeover,
        expected_bridge_instance_id=(
            takeover_status.get("bridgeInstanceId") if takeover_status else None
        ),
        expected_connection_generation=(
            takeover_status.get("connectionGeneration") if takeover_status else None
        ),
    )
    previous_session = current_session
    try:
        handshake_result = await new_session.connect()
    except Exception as e:
        return {
            "action": "bind",
            "success": False,
            "session_id": session_id,
            "port": port,
            "error": str(e),
            "message": f"绑定到端口 {port} 失败: {e}"
        }

    try:
        written_binding = await _run_binding_mutation(
            store.pair,
            session_id,
            "127.0.0.1",
            port,
            handshake_result.get("bridgeId") or expected_bridge_id,
            rollback_on_cancel=lambda written: store.restore_if_current(
                session_id, written.revision, binding
            ),
        )
        await commit_connected_session(
            new_session, expected_previous=previous_session
        )
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if not await is_current_session(new_session):
                await new_session.abort()
                if "written_binding" in locals():
                    await _run_binding_mutation(
                        store.restore_if_current,
                        session_id,
                        written_binding.revision,
                        binding,
                    )
        raise
    except Exception as exc:
        await new_session.close()
        if "written_binding" in locals():
            await _run_binding_mutation(
                store.restore_if_current, session_id, written_binding.revision, binding
            )
        return {
            "action": "bind",
            "success": False,
            "session_id": session_id,
            "port": port,
            "error": str(exc),
            "message": f"连接已验证但绑定提交失败: {exc}",
        }

    return {
        "action": "bind",
        "success": True,
        "session_id": session_id,
        "port": port,
        "handshake_result": handshake_result,
        "message": f"会话 {session_id} 已绑定到端口 {port}",
        "project_path": handshake_result.get("projectPath", "unknown"),
        "scene_name": handshake_result.get("sceneName", "unknown"),
    }


async def _do_unbind_bridge(ctx: Context) -> dict[str, Any]:
    """Serialize unbind with bind so a freshly rebound session is never closed."""
    async with _BINDING_OPERATION_LOCK:
        return await _do_unbind_bridge_locked(ctx)


async def _do_unbind_bridge_locked(ctx: Context) -> dict[str, Any]:
    """解除当前会话的 bridge 绑定。"""
    session_id = _get_session_id(ctx)
    store = BridgeBindingStore()
    binding = await asyncio.to_thread(
        _load_or_migrate_binding, store, session_id
    )

    if binding is not None:
        port = binding.port
        active = await get_session_async()
        cleared, disconnected = await _clear_binding_and_disconnect(
            store, session_id, binding, active
        )
        if not cleared:
            return {
                "action": "unbind",
                "success": False,
                "errorCode": "BINDING_CHANGED",
                "session_id": session_id,
                "message": "绑定在解除期间已被其他会话替换；未删除新绑定。",
            }
        return {
            "action": "unbind",
            "success": True,
            "session_id": session_id,
            "port": port,
            "disconnected": disconnected,
            "message": f"会话 {session_id} 已解除与端口 {port} 的绑定"
        }
    else:
        return {
            "action": "unbind",
            "success": False,
            "session_id": session_id,
            "message": f"会话 {session_id} 没有绑定任何端口"
        }


async def _do_restore_binding(ctx: Context) -> dict[str, Any]:
    """Serialize restore with bind/unbind and reject stale revisions."""
    async with _BINDING_OPERATION_LOCK:
        return await _do_restore_binding_locked(ctx)


async def _do_restore_binding_locked(ctx: Context) -> dict[str, Any]:
    """恢复当前会话的 bridge 绑定（从持久化文件）。"""
    session_id = _get_session_id(ctx)
    store = BridgeBindingStore()
    binding = await asyncio.to_thread(
        _load_or_migrate_binding, store, session_id
    )
    if binding is None:
        return {
            "action": "restore",
            "success": False,
            "session_id": session_id,
            "message": f"会话 {session_id} 没有保存的绑定"
        }

    port = binding.port
    previous_session = await get_session_async()
    new_session = BridgeSession(
        host=binding.host,
        port=port,
        client_id=session_id,
        expected_bridge_id=binding.expected_bridge_id,
    )

    try:
        handshake_result = await new_session.connect()
        revision_matches = await asyncio.to_thread(
            store.matches_revision, session_id, binding.revision
        )
        if not revision_matches:
            await new_session.abort()
            return {
                "action": "restore",
                "success": False,
                "errorCode": "BINDING_CHANGED",
                "session_id": session_id,
                "port": port,
                "message": "binding changed while restoring; candidate was closed",
            }
        await commit_connected_session(
            new_session, expected_previous=previous_session
        )
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if not await is_current_session(new_session):
                await new_session.abort()
        raise
    except Exception as e:
        await new_session.abort()
        return {
            "action": "restore",
            "success": False,
            "session_id": session_id,
            "port": port,
            "error": str(e),
            "message": f"恢复连接到端口 {port} 失败: {e}"
        }

    return {
        "action": "restore",
        "success": True,
        "session_id": session_id,
        "port": port,
        "handshake_result": handshake_result,
        "message": f"会话 {session_id} 已恢复到端口 {port} 的连接",
        "project_path": handshake_result.get("projectPath", "unknown"),
    }


def _load_or_migrate_binding(
    store: BridgeBindingStore, session_id: str
):
    # Do not silently re-import the legacy file. It has no tombstone/CAS model,
    # so a successfully cleared binding could otherwise resurrect on the next
    # call if legacy cleanup failed. Users migrate explicitly with `bridge pair`.
    return store.get(session_id)


def _restore_binding(store: BridgeBindingStore, session_id: str, binding: Any) -> None:
    if binding is None:
        store.clear(session_id)
        return
    store.pair(
        binding.client_id,
        binding.host,
        binding.port,
        binding.expected_bridge_id,
    )


def register(mcp_instance: Any) -> None:
    """注册 bind_bridge 工具到 FastMCP 实例。"""

    @mcp_instance.tool(name="bind_bridge")
    async def bind_bridge(
        ctx: Context,
        port: int | None = None,
        auto: bool = False,
        unbind: bool = False,
        restore: bool = False,
        expected_bridge_id: str | None = None,
        force_takeover: bool = False,
        wait_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """将 Unity bridge 绑定到当前 MCP 会话。

        这个工具会记住当前会话的绑定关系，重启 MCP server 后可以自动恢复。
        适合需要长期固定连接到特定 Unity 实例的场景。

        Args:
            port: 要绑定的端口号。如果不指定，显示当前绑定状态。
            auto: 设为 True 时自动检测并绑定。如果只有一个实例则自动绑定，
                  如果有多个实例则返回列表让用户选择。
            unbind: 设为 True 时解除当前会话的绑定。
            restore: 设为 True 时从持久化文件恢复之前的绑定。

        Returns:
            操作结果，包含绑定状态、会话 ID、端口信息等。

        Examples:
            # 自动检测并绑定（如果只有一个实例）
            bind_bridge(auto=True)

            # 绑定到指定端口
            bind_bridge(port=17874)

            # 查看当前绑定状态
            bind_bridge()

            # 解除绑定
            bind_bridge(unbind=True)

            # 恢复之前的绑定
            bind_bridge(restore=True)
        """
        if unbind:
            return await _do_unbind_bridge(ctx)

        if restore:
            return await _do_restore_binding(ctx)

        return await _do_bind_bridge(
            ctx,
            port=port,
            auto=auto,
            expected_bridge_id=expected_bridge_id,
            force_takeover=force_takeover,
            wait_seconds=wait_seconds,
        )
