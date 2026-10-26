# 多 Unity 实例 Bridge 管理功能总结

## 问题描述

当同时打开多个 Unity 编辑器实例时，所有实例默认监听同一端口（17873），导致：
- Handshake 超时无法返回数据
- 连接到错误的 Unity 实例
- 无法区分要操作哪个项目

## 解决方案

添加了 `select_bridge` MCP 工具，提供以下功能：

### 1. 扫描可用实例
```python
select_bridge(scan=True)
```
自动扫描端口范围 17873-17882，返回所有运行中的 Unity 实例信息（项目路径、场景名称、运行状态等）。

### 2. 查看连接状态
```python
select_bridge()
```
显示当前连接的 Unity 实例端口和连接状态。

### 3. 切换连接
```python
select_bridge(port=17874)
```
关闭当前连接，连接到指定端口的 Unity 实例。

## 实现细节

### 核心文件

1. **工具实现**: `src/unity_mcp_py/tools/select_bridge.py`
   - `_scan_bridge_instances()`: 扫描端口范围查找可用实例
   - `_do_select_bridge()`: 核心逻辑，处理扫描/状态查询/连接切换

2. **会话管理**: `src/unity_mcp_py/bridge/session.py`
   - 使用全局单例 `_SESSION` 管理连接
   - `set_session_for_test()`: 允许替换全局会话实例

3. **服务器注册**: `src/unity_mcp_py/server.py`
   - 在 `register_tools()` 中注册新工具
   - 工具计数更新为 37 个

### 技术特点

- **异步安全**: 使用 `asyncio.Lock` 保护连接状态
- **自动重连**: 切换端口时自动关闭旧连接并建立新连接
- **快速扫描**: 每个端口 0.5s 超时，10 个端口总耗时约 1-2 秒
- **协议验证**: 每次连接都执行 Handshake 验证协议版本兼容性
- **错误处理**: 优雅处理连接失败、端口占用等异常情况

## 使用文档

### 用户文档
- **多实例管理指南**: `docs/MULTI_BRIDGE.md`
  - 问题场景说明
  - 使用方法和示例
  - 工作流演示
  - 故障排查

- **Unity 端口配置**: `docs/UNITY_PORT_CONFIG.md`
  - 4 种配置方法（环境变量、菜单、配置文件、命令行）
  - 推荐端口分配方案
  - 自动化脚本示例
  - 常见问题解答

### 开发文档
- **测试用例**: `tests/test_select_bridge.py`
  - 8 个测试用例，覆盖所有功能
  - 使用 MockBridgeServer 模拟多实例环境
  - 测试扫描、连接、切换、错误处理

- **使用示例**: `examples/multi_bridge_example.py`
  - 5 个实际使用场景演示
  - 批量操作多个实例
  - 错误处理最佳实践

## 使用场景

### 场景 1：同时调试两个项目
```python
# 扫描找到两个项目
select_bridge(scan=True)
# 返回: GameA (17873), GameB (17874)

# 调试 GameA
select_bridge(port=17873)
lua_eval(script="...", goal="...")

# 切换到 GameB
select_bridge(port=17874)
capture_screenshot()
```

### 场景 2：版本对比测试
```python
# 扫描所有版本
result = select_bridge(scan=True)

# 对每个版本执行相同测试
for instance in result['instances']:
    select_bridge(port=instance['port'])
    # 执行测试...
```

### 场景 3：批量操作
```python
# 扫描
instances = select_bridge(scan=True)['instances']

# 批量截图
for inst in instances:
    select_bridge(port=inst['port'])
    capture_screenshot()
```

## Unity 端配置

每个 Unity 实例需要配置不同端口：

### 方法 1：环境变量（推荐）
```powershell
# 实例 1 - 默认端口
Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"

# 实例 2 - 端口 17874
$env:UNITY_MCP_BRIDGE_PORT = "17874"
Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"
```

### 方法 2：配置文件
在 Unity 项目中编辑 `BridgeSettings.json`:
```json
{
  "port": 17874,
  "host": "127.0.0.1",
  "autoStart": true
}
```

## 测试验证

### 运行测试
```bash
cd <MCP_ROOT>
pytest tests/test_select_bridge.py -v
```

### 测试覆盖
- ✅ 扫描多个实例
- ✅ 查看连接状态
- ✅ 连接到指定端口
- ✅ 切换连接
- ✅ 处理无效端口
- ✅ 空端口范围
- ✅ 多次扫描一致性

## 性能特征

- **扫描速度**: 10 个端口 ~1-2 秒
- **连接切换**: ~100-200ms
- **内存占用**: 单个会话 ~1MB
- **并发安全**: 使用锁机制保护共享状态

## 兼容性

- **Python**: 3.11+
- **Unity**: 2022.3+ (需要 bridge 包支持)
- **协议版本**: 1.9.0
- **操作系统**: Windows, macOS, Linux

## 限制和注意事项

1. **端口范围**: 默认扫描 17873-17882（可配置）
2. **同时连接**: 一次只能连接一个实例
3. **网络限制**: 仅支持本地连接（127.0.0.1），拒绝非 loopback 地址
4. **协议要求**: Unity bridge 必须支持 Handshake 命令

## 未来改进

可能的扩展方向：
- [ ] 支持命名连接（别名）
- [ ] 持久化连接历史
- [ ] 自动发现（mDNS/Bonjour）
- [ ] 连接池管理
- [ ] WebSocket 支持
- [ ] 远程连接（认证后）

## 相关问题

### Issue: Handshake 超时
**解决**: 使用 `select_bridge(scan=True)` 查看哪些端口可用

### Issue: 连接到错误实例
**解决**: 使用 `select_bridge()` 确认当前连接，然后用 `select_bridge(port=...)` 切换

### Issue: Unity 启动失败
**解决**: 检查端口冲突，为每个实例分配不同端口

## 贡献者注意事项

修改此功能时需要注意：

1. **线程安全**: 所有会话操作必须在锁保护下进行
2. **错误处理**: 优雅处理所有网络异常
3. **向后兼容**: 不修改现有工具的行为
4. **测试覆盖**: 新功能必须有对应测试用例
5. **文档更新**: 同步更新用户文档和 API 文档

## 版本历史

- **v0.11.0** (2026-08-07): 初始实现
  - 添加 `select_bridge` 工具
  - 支持扫描、状态查询、连接切换
  - 完整文档和测试覆盖

---

## 快速开始

### 1. 配置 Unity 实例
```powershell
# 启动多个实例，每个使用不同端口
$env:UNITY_MCP_BRIDGE_PORT = "17873"; Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"
$env:UNITY_MCP_BRIDGE_PORT = "17874"; Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"
```

### 2. 扫描实例
```python
select_bridge(scan=True)
```

### 3. 选择实例
```python
select_bridge(port=17874)
```

### 4. 开始工作
```python
lua_eval(script="return PROJECT_RUNTIME.me.name", goal="获取玩家名称")
capture_screenshot()
```

---

**完整文档**: 
- [多实例管理指南](MULTI_BRIDGE.md)
- [Unity 端口配置](UNITY_PORT_CONFIG.md)
- [使用示例](../examples/multi_bridge_example.py)
