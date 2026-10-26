# bind_bridge 快速参考

## 核心概念

`bind_bridge` 是一个**会话级绑定**工具，它会记住当前 MCP 会话与 Unity bridge 的绑定关系。

### vs `select_bridge`

| 特性 | `bind_bridge` | `select_bridge` |
|------|--------------|----------------|
| 绑定持久化 | ✅ 保存到磁盘 | ❌ 仅当前连接 |
| 会话隔离 | ✅ 每个会话独立 | ❌ 全局单例 |
| 自动恢复 | ✅ 重启后恢复 | ❌ 需要重新连接 |
| 使用场景 | 长期固定绑定 | 临时切换测试 |

---

## 基本用法

### 🚀 自动绑定（最快）
```python
bind_bridge(auto=True)
```
- **只有 1 个实例**：自动绑定
- **多个实例**：返回列表让你选择

### 📌 绑定到指定端口
```python
bind_bridge(port=17874)
```
保存绑定关系，下次自动连接

### 📊 查看绑定状态
```python
bind_bridge()
```
显示当前会话的绑定端口和连接状态

### 🔄 恢复绑定
```python
bind_bridge(restore=True)
```
从持久化文件恢复之前的绑定

### ❌ 解除绑定
```python
bind_bridge(unbind=True)
```
删除当前会话的绑定关系

---

## 典型工作流

### 工作流 1: 初次设置（推荐）
```python
# 步骤 1: 自动检测并绑定
result = bind_bridge(auto=True)

# 如果只有一个实例，自动绑定成功
if result['success']:
    print(f"✓ 已绑定到: {result['project_path']}")
else:
    # 多个实例时，查看列表
    for inst in result['instances']:
        print(f"端口 {inst['port']}: {inst['project_path']}")
    
    # 选择一个绑定
    bind_bridge(port=17874)
```

### 工作流 2: 日常使用
```python
# 启动 MCP 后，绑定会自动恢复
# 如果没有自动恢复，手动恢复：
bind_bridge(restore=True)

# 现在可以直接使用其他工具，无需每次指定端口
lua_eval(script="return PROJECT_RUNTIME.me.name", goal="获取玩家名")
capture_screenshot()
```

### 工作流 3: 多项目切换
```python
# 项目 A 工作
bind_bridge(port=17873)
# ... 执行操作 ...

# 切换到项目 B
bind_bridge(port=17874)
# ... 执行操作 ...

# 下次启动时，会话会记住最后的绑定（17874）
```

---

## 返回值格式

### 绑定成功
```json
{
  "action": "bind",
  "success": true,
  "session_id": "claude-code-abc123",
  "port": 17874,
  "message": "会话 claude-code-abc123 已绑定到端口 17874",
  "project_path": "<PROJECT_ROOT>",
  "scene_name": "TestScene"
}
```

### 状态查询
```json
{
  "action": "status",
  "session_id": "claude-code-abc123",
  "bound_port": 17874,
  "current_port": 17874,
  "is_bound": true,
  "is_connected": true,
  "message": "会话 claude-code-abc123 已绑定到端口 17874"
}
```

### 自动绑定（多实例）
```json
{
  "action": "auto",
  "success": false,
  "found_instances": 2,
  "instances": [
    {"port": 17873, "project_path": "<PROJECT_ROOT>", ...},
    {"port": 17874, "project_path": "<PROJECT_ROOT>", ...}
  ],
  "message": "找到 2 个 Unity 实例，请使用 bind_bridge(port=...) 明确指定"
}
```

---

## 高级用法

### 会话隔离
不同的 Claude Code 会话可以绑定到不同的 Unity 实例：

```python
# 会话 A (项目 GameA 的窗口)
bind_bridge(port=17873)

# 会话 B (项目 GameB 的窗口)
bind_bridge(port=17874)

# 两个会话互不干扰，各自保持自己的绑定
```

### 持久化位置
绑定关系保存在：
```
~/.claude/mcp-servers/unity-mcp/session_bindings.json
```

示例内容：
```json
{
  "claude-code-session-1": 17873,
  "claude-code-session-2": 17874,
  "temp-12345": 17875
}
```

### 会话 ID 来源
1. **环境变量** `CLAUDE_SESSION_ID`（Claude Code 自动设置）
2. **MCP Context** 中的会话信息
3. **临时 ID** `temp-{进程ID}`（重启后丢失）

---

## 常见场景

### ✅ 场景 1: 每天固定用一个项目
```python
# 第一次
bind_bridge(auto=True)

# 以后每次启动 Claude Code
bind_bridge(restore=True)  # 自动恢复到昨天的项目
```

### ✅ 场景 2: 在两个项目间频繁切换
```python
# 不要用 bind_bridge，用 select_bridge
select_bridge(port=17873)  # 临时切换到 A
select_bridge(port=17874)  # 临时切换到 B
```

### ✅ 场景 3: 同时开发多个项目（多窗口）
```python
# 窗口 1 (GameA)
bind_bridge(port=17873)

# 窗口 2 (GameB) - 新的 Claude Code 窗口
bind_bridge(port=17874)

# 每个窗口记住自己的绑定
```

---

## 故障排查

### ❌ 绑定后连接失败
```python
# 检查 Unity 实例是否还在运行
bind_bridge()  # 查看状态

# 重新扫描
bind_bridge(auto=True)
```

### ❌ 恢复绑定失败
```python
# 端口可能已被占用或 Unity 关闭了
# 重新绑定
bind_bridge(auto=True)
```

### ❌ 会话 ID 每次都变
如果每次都是 `temp-xxx`，说明：
- 不是从 Claude Code 启动
- 或者环境变量未设置

**解决**: 手动设置环境变量
```bash
export CLAUDE_SESSION_ID="my-stable-session"
```

---

## 最佳实践

### ✅ DO

- **固定项目用 bind_bridge**
  ```python
  bind_bridge(auto=True)  # 首次
  bind_bridge(restore=True)  # 日常
  ```

- **临时测试用 select_bridge**
  ```python
  select_bridge(scan=True)
  select_bridge(port=17875)
  ```

- **多窗口各自绑定**
  - 窗口 1 → 端口 17873
  - 窗口 2 → 端口 17874

### ❌ DON'T

- ❌ 不要频繁切换后又用 bind_bridge
  ```python
  # 错误：频繁 bind 会不断修改持久化文件
  bind_bridge(port=17873)
  bind_bridge(port=17874)
  bind_bridge(port=17873)
  ```

- ❌ 不要混用 bind 和 select
  ```python
  # 可能造成混乱
  bind_bridge(port=17873)
  select_bridge(port=17874)  # 这会覆盖绑定吗？
  ```

---

## 命令对比

| 命令 | 用途 | 持久化 | 速度 |
|-----|------|--------|------|
| `bind_bridge(auto=True)` | 快速绑定当前会话 | ✅ | ⚡ 最快 |
| `bind_bridge(port=X)` | 绑定到指定端口 | ✅ | ⚡ 快 |
| `select_bridge(scan=True)` | 扫描所有实例 | ❌ | 🐌 慢（1-2秒） |
| `select_bridge(port=X)` | 临时切换 | ❌ | ⚡ 快 |

---

## 快速决策树

```
需要多实例管理？
├─ 是 → 长期固定一个项目？
│   ├─ 是 → 用 bind_bridge(auto=True)
│   └─ 否 → 需要频繁切换？
│       ├─ 是 → 用 select_bridge(port=...)
│       └─ 否 → 多窗口各自项目？
│           └─ 是 → 每个窗口用 bind_bridge(port=...)
└─ 否 → 使用默认端口 17873
```

---

## 总结

**一句话**: `bind_bridge(auto=True)` 让你的会话记住 Unity 实例，无需每次手动连接。

**最常用命令**:
```python
bind_bridge(auto=True)      # 首次绑定
bind_bridge()               # 查看状态
bind_bridge(restore=True)   # 恢复绑定
```
