# 多 Unity 实例 Bridge 管理

当你同时打开多个 Unity 编辑器实例时，需要配置不同的端口以避免冲突，并使用 `select_bridge` 工具选择要连接的实例。

## 问题场景

默认情况下，UnityMCP bridge 监听端口 `17873`。如果你打开多个 Unity 编辑器：

- **问题**：所有实例尝试监听同一端口，导致 Handshake 失败或连接到错误的实例
- **症状**：`Handshake` 超时无法返回数据，或连接到了错误的项目

## 解决方案

### 1. 为每个 Unity 实例配置不同的端口

在 Unity 编辑器中，修改 bridge 配置使用不同端口：

**方法 A：通过 Unity Editor 窗口配置**

1. 打开 `Window > UnityMCP > Bridge Settings`
2. 修改 `Port` 字段为不同值：
   - 实例 1: `17873` (默认)
   - 实例 2: `17874`
   - 实例 3: `17875`
   - ...

**方法 B：通过代码配置**

在 Unity 项目中找到 bridge 配置文件，修改端口设置。

### 2. 使用 select_bridge 工具切换连接

#### 查看当前连接状态

```python
# 调用 select_bridge 工具，不传参数
select_bridge()
```

返回示例：
```json
{
  "action": "status",
  "connected": true,
  "host": "127.0.0.1",
  "port": 17873,
  "message": "当前已连接到 127.0.0.1:17873"
}
```

#### 扫描所有可用的 bridge 实例

```python
# 扫描端口范围 17873-17882
select_bridge(scan=True)
```

返回示例：
```json
{
  "action": "scan",
  "found_instances": 2,
  "instances": [
    {
      "port": 17873,
      "project_path": "<PROJECT_ROOT>",
      "scene_name": "MainScene",
      "is_playing": false,
      "protocol_version": "1.9.0"
    },
    {
      "port": 17874,
      "project_path": "<PROJECT_ROOT>",
      "scene_name": "TestScene",
      "is_playing": true,
      "protocol_version": "1.9.0"
    }
  ],
  "message": "找到 2 个 Unity bridge 实例"
}
```

#### 切换到指定端口的实例

```python
# 连接到端口 17874 的 Unity 实例
select_bridge(port=17874)
```

返回示例：
```json
{
  "action": "select",
  "success": true,
  "port": 17874,
  "handshake_result": {
    "protocolVersion": "1.9.0",
    "projectPath": "<PROJECT_ROOT>",
    "sceneName": "TestScene",
    "isPlaying": true
  },
  "message": "成功连接到端口 17874 的 Unity bridge"
}
```

## 工作流示例

### 场景：同时调试两个项目

1. **打开两个 Unity 实例**
   - 实例 A：端口 17873，项目 GameA
   - 实例 B：端口 17874，项目 GameB

2. **扫描可用实例**
   ```python
   select_bridge(scan=True)
   ```

3. **选择要操作的实例**
   ```python
   # 现在要调试 GameB
   select_bridge(port=17874)
   ```

4. **执行操作**
   ```python
   # 现在所有 MCP 工具都会操作 GameB (端口 17874)
   lua_eval(script="return PROJECT_RUNTIME.me.name", goal="获取玩家名称")
   capture_screenshot()
   ```

5. **切换到另一个实例**
   ```python
   # 切换回 GameA
   select_bridge(port=17873)
   
   # 现在操作 GameA
   lua_eval(script="return PROJECT_RUNTIME.me.level", goal="获取玩家等级")
   ```

## 注意事项

1. **端口范围**：扫描默认检查端口 17873-17882（10个端口）
2. **自动重连**：切换端口时会自动关闭旧连接并建立新连接
3. **Handshake 验证**：每次连接都会执行 Handshake 确保协议版本兼容
4. **连接持久性**：连接会保持到下次调用 `select_bridge(port=...)` 或 MCP 服务重启

## 故障排查

### Handshake 超时

**症状**：调用工具时提示 `Handshake timed out after 5s`

**原因**：
- Unity bridge 未启动
- 端口配置错误
- 多个实例监听同一端口

**解决**：
1. 使用 `select_bridge(scan=True)` 查看哪些端口有响应
2. 检查 Unity Console 是否有 bridge 启动日志
3. 确认每个实例使用不同端口

### 连接到错误的实例

**症状**：操作的项目/场景不是预期的

**解决**：
1. 调用 `select_bridge()` 确认当前连接的端口
2. 调用 `select_bridge(scan=True)` 查看所有实例
3. 使用 `select_bridge(port=...)` 切换到正确实例

### 端口冲突

**症状**：Unity Console 显示 `Address already in use`

**解决**：
1. 为每个 Unity 实例分配不同端口
2. 检查是否有其他程序占用该端口
3. 使用 `netstat -ano | findstr :17873` (Windows) 或 `lsof -i :17873` (macOS/Linux) 检查端口占用

## 实现细节

### 扫描机制

`select_bridge(scan=True)` 的工作原理：

1. 遍历端口范围（默认 17873-17882）
2. 对每个端口尝试 TCP 连接（0.5s 超时）
3. 发送 Handshake 命令获取实例信息
4. 返回所有响应的实例列表

### 连接切换

`select_bridge(port=...)` 的工作原理：

1. 关闭当前 BridgeSession 连接
2. 创建新的 BridgeSession 指向目标端口
3. 替换全局单例 `_SESSION`
4. 执行 Handshake 验证连接

### 线程安全

- 使用 `asyncio.Lock` 保护连接状态
- 所有操作序列化执行，避免竞态条件
