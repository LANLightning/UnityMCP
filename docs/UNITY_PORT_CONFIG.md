# Unity Bridge 端口配置指南

当你需要同时运行多个 Unity 编辑器实例时，每个实例需要配置不同的 bridge 端口。

## 方法 1：通过环境变量配置（推荐）

在启动 Unity 编辑器前设置环境变量：

### Windows (PowerShell)

```powershell
# 启动实例 1 - 使用默认端口 17873
Start-Process "<UNITY_EDITOR_PATH>" -ArgumentList "-projectPath", "<PROJECT_ROOT>"

# 启动实例 2 - 使用端口 17874
$env:UNITY_MCP_BRIDGE_PORT = "17874"
Start-Process "<UNITY_EDITOR_PATH>" -ArgumentList "-projectPath", "<PROJECT_ROOT>"

# 启动实例 3 - 使用端口 17875
$env:UNITY_MCP_BRIDGE_PORT = "17875"
Start-Process "<UNITY_EDITOR_PATH>" -ArgumentList "-projectPath", "<PROJECT_ROOT>"
```

### Windows (Command Prompt)

```cmd
REM 启动实例 1
start "" "<UNITY_EDITOR_PATH>" -projectPath "<PROJECT_ROOT>"

REM 启动实例 2
set UNITY_MCP_BRIDGE_PORT=17874
start "" "<UNITY_EDITOR_PATH>" -projectPath "<PROJECT_ROOT>"

REM 启动实例 3
set UNITY_MCP_BRIDGE_PORT=17875
start "" "<UNITY_EDITOR_PATH>" -projectPath "<PROJECT_ROOT>"
```

### macOS / Linux (Bash)

```bash
#!/bin/bash

# 启动实例 1 - 使用默认端口 17873
<UNITY_EDITOR_PATH> -projectPath <PROJECT_A_ROOT> &

# 启动实例 2 - 使用端口 17874
UNITY_MCP_BRIDGE_PORT=17874 <UNITY_EDITOR_PATH> -projectPath <PROJECT_B_ROOT> &

# 启动实例 3 - 使用端口 17875
UNITY_MCP_BRIDGE_PORT=17875 <UNITY_EDITOR_PATH> -projectPath <PROJECT_C_ROOT> &
```

## 方法 2：通过 Unity Editor 菜单配置

1. 打开 Unity 项目
2. 菜单栏选择 `Window > UnityMCP > Bridge Settings`
3. 在设置窗口中找到 `Port` 字段
4. 修改为不同的端口号（例如 17874、17875 等）
5. 点击 `Apply` 或 `Save`
6. 重启 Unity 编辑器使配置生效

## 方法 3：通过配置文件

在项目目录中找到或创建配置文件：

```
ProjectRoot/
└── Packages/
    └── com.local.unitymcp-bridge/
        └── Runtime/
            └── BridgeSettings.json
```

编辑 `BridgeSettings.json`：

```json
{
  "port": 17874,
  "host": "127.0.0.1",
  "autoStart": true
}
```

## 方法 4：通过命令行参数

启动 Unity 时传递命令行参数：

```bash
Unity.exe -projectPath "<PROJECT_ROOT>" -unityMcpPort 17874
```

注意：需要 bridge 包支持此命令行参数。

## 推荐的端口分配方案

为避免冲突，建议使用以下端口分配：

| 实例编号 | 端口  | 用途示例           |
|---------|------|-------------------|
| 1       | 17873 | 主开发项目         |
| 2       | 17874 | 测试项目           |
| 3       | 17875 | 演示/Demo 项目     |
| 4       | 17876 | 实验性功能测试     |
| 5       | 17877 | 旧版本对比         |
| ...     | ...  | ...               |

## 验证配置

启动 Unity 编辑器后，在 Console 窗口中应该看到类似的日志：

```
[UnityMCP] Bridge server started on 127.0.0.1:17874
[UnityMCP] Protocol version: 1.9.0
[UnityMCP] Waiting for client connection...
```

确认日志中的端口号与你配置的一致。

## 使用 select_bridge 连接

配置完成后，使用 MCP 工具连接：

```python
# 1. 扫描所有实例
select_bridge(scan=True)

# 2. 连接到指定端口
select_bridge(port=17874)

# 3. 验证连接
select_bridge()  # 查看当前状态
```

## 常见问题

### Q: 修改端口后 Unity 启动失败

**A**: 检查该端口是否被其他程序占用：

```powershell
# Windows
netstat -ano | findstr :17874

# macOS/Linux
lsof -i :17874
```

### Q: 多个实例使用了相同端口

**A**: 后启动的实例会失败并在 Console 显示错误：

```
[UnityMCP] Error: Address already in use (port 17873)
[UnityMCP] Bridge server failed to start
```

解决：为每个实例分配不同端口。

### Q: 环境变量设置不生效

**A**: 确保：
1. 在启动 Unity **之前**设置环境变量
2. 使用正确的变量名：`UNITY_MCP_BRIDGE_PORT`
3. 从设置了环境变量的终端/进程中启动 Unity

### Q: 如何在 Unity Hub 中配置不同端口

**A**: Unity Hub 目前不直接支持此功能。推荐方案：
1. 创建启动脚本（见上面的 PowerShell/Bash 示例）
2. 或者在每个项目的 `BridgeSettings.json` 中硬编码不同端口

## 自动化脚本示例

创建一个启动脚本来管理多个实例：

### start_multi_unity.ps1 (Windows)

```powershell
# Unity 编辑器路径
$unityPath = "<UNITY_EDITOR_PATH>"

# 项目配置
$projects = @(
    @{ Path = "<PROJECT_ROOT>"; Port = 17873 },
    @{ Path = "<PROJECT_ROOT>"; Port = 17874 },
    @{ Path = "<PROJECT_ROOT>"; Port = 17875 }
)

# 启动每个实例
foreach ($project in $projects) {
    Write-Host "启动项目: $($project.Path) (端口: $($project.Port))"
    
    $env:UNITY_MCP_BRIDGE_PORT = $project.Port
    Start-Process $unityPath -ArgumentList "-projectPath", $project.Path
    
    # 等待 2 秒再启动下一个，避免启动冲突
    Start-Sleep -Seconds 2
}

Write-Host "所有实例已启动"
```

### start_multi_unity.sh (macOS/Linux)

```bash
#!/bin/bash

# Unity 编辑器路径
UNITY_PATH="<UNITY_EDITOR_PATH>"

# 项目配置
declare -a projects=(
    "<PROJECT_A_ROOT>:17873"
    "<PROJECT_B_ROOT>:17874"
    "<PROJECT_C_ROOT>:17875"
)

# 启动每个实例
for project_config in "${projects[@]}"; do
    IFS=':' read -r path port <<< "$project_config"
    echo "启动项目: $path (端口: $port)"
    
    UNITY_MCP_BRIDGE_PORT=$port "$UNITY_PATH" -projectPath "$path" &
    
    # 等待 2 秒再启动下一个
    sleep 2
done

echo "所有实例已启动"
```

使用方法：

```bash
# Windows
.\start_multi_unity.ps1

# macOS/Linux
chmod +x start_multi_unity.sh
./start_multi_unity.sh
```

## 端口范围规划

建议的端口范围：

- **17873-17879**: 开发实例（7个端口）
- **17880-17882**: 测试实例（3个端口）
- **17883-17889**: 预留

这样可以支持最多 10 个同时运行的 Unity 实例，足够大部分使用场景。
