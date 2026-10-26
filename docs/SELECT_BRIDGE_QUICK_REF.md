# select_bridge 快速参考

## 基本用法

### 扫描所有实例
```python
select_bridge(scan=True)
```
**返回**: 找到的实例列表（端口、项目路径、场景名称）

### 查看当前状态
```python
select_bridge()
```
**返回**: 当前连接的端口和状态

### 切换到指定实例
```python
select_bridge(port=17874)
```
**返回**: 连接结果（成功/失败）

---

## 常见场景

### 场景 1: 首次使用
```python
# 1. 扫描找到所有实例
result = select_bridge(scan=True)
print(f"找到 {result['found_instances']} 个实例")

# 2. 选择要连接的实例
for inst in result['instances']:
    print(f"端口 {inst['port']}: {inst['project_path']}")

# 3. 连接
select_bridge(port=17874)
```

### 场景 2: 切换项目
```python
# 从 GameA 切换到 GameB
select_bridge(port=17874)  # GameB

# 执行操作
lua_eval(script="...", goal="...")

# 切换回 GameA
select_bridge(port=17873)  # GameA
```

### 场景 3: 批量操作
```python
# 获取所有实例
instances = select_bridge(scan=True)['instances']

# 对每个实例执行操作
for inst in instances:
    select_bridge(port=inst['port'])
    capture_screenshot()
    print(f"已截图: {inst['project_path']}")
```

---

## 返回值格式

### 扫描 (scan=True)
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
    }
  ],
  "message": "找到 2 个 Unity bridge 实例"
}
```

### 状态查询 (无参数)
```json
{
  "action": "status",
  "connected": true,
  "host": "127.0.0.1",
  "port": 17873,
  "message": "当前已连接到 127.0.0.1:17873"
}
```

### 连接切换 (port=...)
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

---

## Unity 端配置

### 快速配置（PowerShell）
```powershell
# 实例 1 - 默认端口 17873
Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"

# 实例 2 - 端口 17874
$env:UNITY_MCP_BRIDGE_PORT = "17874"
Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"

# 实例 3 - 端口 17875
$env:UNITY_MCP_BRIDGE_PORT = "17875"
Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"
```

### 推荐端口分配
| 实例 | 端口  | 用途     |
|------|------|---------|
| 1    | 17873 | 主项目  |
| 2    | 17874 | 测试    |
| 3    | 17875 | 演示    |
| 4    | 17876 | 实验    |

---

## 故障排查

### ❌ Handshake 超时
```python
# 1. 扫描查看哪些端口可用
select_bridge(scan=True)

# 2. 检查 Unity Console 是否有错误
# 3. 确认端口配置是否正确
```

### ❌ 连接失败
```python
# 检查当前状态
status = select_bridge()
print(f"当前端口: {status['port']}")
print(f"连接状态: {status['connected']}")

# 尝试扫描
scan_result = select_bridge(scan=True)
if scan_result['found_instances'] == 0:
    print("未找到任何 Unity 实例")
else:
    print(f"找到 {scan_result['found_instances']} 个实例")
```

### ❌ 连接到错误实例
```python
# 1. 查看当前连接
current = select_bridge()
print(f"当前连接: {current['port']}")

# 2. 扫描所有实例
all_instances = select_bridge(scan=True)

# 3. 找到目标实例
for inst in all_instances['instances']:
    if 'GameB' in inst['project_path']:
        select_bridge(port=inst['port'])
        break
```

---

## 限制

- **端口范围**: 默认扫描 17873-17882（10 个端口）
- **同时连接**: 一次只能连接一个实例
- **网络**: 仅支持本地连接（127.0.0.1）
- **扫描速度**: ~1-2 秒（取决于端口数量）

---

## 相关文档

- 📖 [完整文档](MULTI_BRIDGE.md) - 详细使用指南
- ⚙️ [Unity 配置](UNITY_PORT_CONFIG.md) - Unity 端口配置方法
- 📝 [使用示例](../examples/multi_bridge_example.py) - 实际代码示例
- 🧪 [测试用例](../tests/test_select_bridge.py) - 单元测试参考

---

## 提示

💡 **首次使用时先扫描**: `select_bridge(scan=True)` 可以快速了解当前有哪些实例

💡 **使用项目路径识别**: 扫描结果包含完整项目路径，便于识别目标实例

💡 **批量操作时保存端口**: 将扫描结果保存到变量，避免重复扫描

💡 **切换前确认当前状态**: 使用 `select_bridge()` 确认当前连接避免混淆
