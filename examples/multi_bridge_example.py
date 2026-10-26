#!/usr/bin/env python
"""示例：在多个 Unity 实例之间切换并执行操作。

使用场景：
- 同时调试两个项目
- 对比不同版本的行为
- 批量操作多个 Unity 实例
"""
import asyncio
import sys

from unity_mcp_py.bridge.session import get_session, set_session_for_test, BridgeSession
from unity_mcp_py.tools.select_bridge import _do_select_bridge


async def example_scan_and_list():
    """示例 1：扫描并列出所有可用的 Unity 实例。"""
    print("=== 示例 1：扫描所有 Unity 实例 ===\n")

    result = await _do_select_bridge(scan=True)

    print(f"找到 {result['found_instances']} 个 Unity 实例:\n")
    for i, instance in enumerate(result['instances'], 1):
        print(f"实例 {i}:")
        print(f"  端口: {instance['port']}")
        print(f"  项目路径: {instance['project_path']}")
        print(f"  场景: {instance['scene_name']}")
        print(f"  是否运行: {'是' if instance['is_playing'] else '否'}")
        print(f"  协议版本: {instance['protocol_version']}")
        print()


async def example_check_status():
    """示例 2：查看当前连接状态。"""
    print("=== 示例 2：查看连接状态 ===\n")

    result = await _do_select_bridge()

    if result['connected']:
        print(f"✓ 已连接到 {result['host']}:{result['port']}")
    else:
        print(f"✗ 未连接（默认端口: {result['port']}）")
    print()


async def example_switch_instances():
    """示例 3：在多个实例之间切换。"""
    print("=== 示例 3：切换 Unity 实例 ===\n")

    # 首先扫描
    scan_result = await _do_select_bridge(scan=True)
    if scan_result['found_instances'] < 2:
        print("需要至少 2 个 Unity 实例才能演示切换功能")
        return

    instances = scan_result['instances']
    port1 = instances[0]['port']
    port2 = instances[1]['port']

    # 连接到第一个实例
    print(f"连接到实例 1 (端口 {port1})...")
    result1 = await _do_select_bridge(port=port1)
    if result1['success']:
        print(f"✓ 成功连接")
        print(f"  项目: {result1['handshake_result']['projectPath']}")
    else:
        print(f"✗ 连接失败: {result1['error']}")
        return

    print()

    # 切换到第二个实例
    print(f"切换到实例 2 (端口 {port2})...")
    result2 = await _do_select_bridge(port=port2)
    if result2['success']:
        print(f"✓ 成功切换")
        print(f"  项目: {result2['handshake_result']['projectPath']}")
    else:
        print(f"✗ 切换失败: {result2['error']}")

    print()


async def example_batch_operations():
    """示例 4：对多个实例执行批量操作。"""
    print("=== 示例 4：批量操作多个实例 ===\n")

    # 扫描所有实例
    scan_result = await _do_select_bridge(scan=True)
    instances = scan_result['instances']

    if not instances:
        print("未找到任何 Unity 实例")
        return

    print(f"将对 {len(instances)} 个实例执行操作:\n")

    # 对每个实例执行操作
    for i, instance in enumerate(instances, 1):
        port = instance['port']
        print(f"[{i}/{len(instances)}] 处理端口 {port}...")

        # 连接到实例
        connect_result = await _do_select_bridge(port=port)
        if not connect_result['success']:
            print(f"  ✗ 连接失败: {connect_result['error']}")
            continue

        print(f"  ✓ 已连接")
        print(f"  项目: {instance['project_path']}")
        print(f"  场景: {instance['scene_name']}")

        # 这里可以执行实际的操作，例如：
        # - await bridge_call("GetConsoleLog", {})
        # - await bridge_call("CaptureScreenshot", {})
        # - await bridge_call("LuaEval", {"script": "..."})

        print()


async def example_error_handling():
    """示例 5：处理连接错误。"""
    print("=== 示例 5：错误处理 ===\n")

    # 尝试连接到不存在的端口
    invalid_port = 19999
    print(f"尝试连接到不存在的端口 {invalid_port}...")

    result = await _do_select_bridge(port=invalid_port)

    if not result['success']:
        print(f"✓ 正确捕获错误:")
        print(f"  {result['message']}")
    else:
        print(f"✗ 意外成功（不应该发生）")

    print()


async def main():
    """运行所有示例。"""
    print("=" * 60)
    print("UnityMCP 多实例管理示例")
    print("=" * 60)
    print()

    examples = [
        ("扫描并列出", example_scan_and_list),
        ("查看状态", example_check_status),
        ("切换实例", example_switch_instances),
        ("批量操作", example_batch_operations),
        ("错误处理", example_error_handling),
    ]

    for i, (name, func) in enumerate(examples, 1):
        try:
            await func()
        except Exception as e:
            print(f"示例 {i} 执行出错: {e}\n")

        if i < len(examples):
            print("-" * 60)
            print()

    print("=" * 60)
    print("所有示例执行完成")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n已取消")
        sys.exit(0)
