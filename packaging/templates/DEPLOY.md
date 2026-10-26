> Public anonymized adapter template: PROJECT_* names are placeholders. Adapt privately before using project-specific tools.

# unity-mcp 部署说明书

把本目录拷到目标设备，按下面步骤安装并填好本地化设置即可。

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Python | ≥ 3.11 (`python --version` / macOS/Linux 用 `python3 --version`) |
| Unity Editor | ≥ 2022.3 |

## 2. 安装 Python 侧 (MCP server)

- Windows: `pwsh -File install.ps1`（若被执行策略阻止: `Set-ExecutionPolicy -Scope Process Bypass; .\install.ps1`）
- macOS/Linux: `bash install.sh`
- 手动: `pip install unity_mcp_py-*.whl`

验证: `unity-mcp-py --version`

## 3. 安装 Unity 侧 (bridge)

1. 打开目标 Unity 工程
2. Window → Package Manager
3. 左上 **+** → **Add package from tarball...**
4. 选本目录的 `com.local.unitymcp-bridge-*.tgz`
5. 导入后桥自动在 `127.0.0.1:17873` 监听

## 4. 本地化待填清单

> 这些值因设备/账号而异，必须自己填。

| 设置 | 必填? | 填在哪 | 示例 |
|---|---|---|---|
| `UNITY_MCP_PROJECT_PATH` | **必填** | `.env` 或 MCP `env` 块 | `<PROJECT_ROOT>` |
| `UNITY_MCP_LOGIN_USERNAME` / `_SERVER_ID` / `_SCENE_ID` | 可选 (仅 auto_login) | `.env` | 测试账号 |
| `~/.claude.json` 的 `mcpServers` 块 | 必填 (Claude Code) | `~/.claude.json` | 见 `claude.json.snippet` |
| `UNITY_MCP_BRIDGE_HOST` / `_PORT` | 可选 (默认 127.0.0.1:17873) | `.env` | — |
| `UNITY_MCP_SCHEMA_PATH` | 可选/进阶 | `.env` | 见 §7 |

填法：
1. 复制 `.env.example` 为 `.env`，把 `<...>` 占位替换成真实值。
2. 打开 `claude.json.snippet`，把 `<FILL_IN...>` 换成你的工程路径。若 `~/.claude.json` 已有 `mcpServers`，只把里面的 `"unity-mcp-py": {...}` 这一项加进去（不要整块覆盖，否则会删掉你已有的其它 MCP server）；若还没有 `mcpServers`，整段贴进去即可。
3. 重启 Claude Code，会话头部应列出 `mcp__unity-mcp-py__*` 工具。

> 注：`claude.json.snippet` 用 wheel 安装后的 `unity-mcp-py` 命令入口；若你是源码开发，命令改用 `python -m unity_mcp_py.server`。

## 5. 验证

```
unity-mcp-py --version
# 桥连通性
Test-NetConnection 127.0.0.1 -Port 17873   # Windows
nc -zv 127.0.0.1 17873                      # macOS/Linux
```

> 端到端协议自检 `python scripts/verify_mcp_protocol.py` 需在源码仓内运行（部署包不含 scripts/）。

## 6. 故障排查

完整工具说明与排错见源码仓 `docs/USAGE.md`。常见：
- 桥连不上 → Unity 没开 / bridge 包没装 / 端口被占
- `--version` 不认 → wheel 没装进当前 Python 环境

## 7. 进阶：指回本地 schema

新设备读不到协议 schema 文件时，握手会自动降级到内置版本，无需任何配置。
只有旧开发机想用本地 `protocol.schema.json` 校验时，才设：

```
UNITY_MCP_SCHEMA_PATH=<PROJECT_ROOT>/Packages/com.local.unitymcp-bridge/protocol.schema.json
```
