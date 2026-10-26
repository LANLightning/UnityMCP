> 当前版本的工具数量、默认参数、适配边界与安全要求以根目录 README.md 为准；本文件保留历史参数示例。

# unity-mcp-py 使用说明

完整中文使用手册。版本：Python **0.10.0** / 桥 **0.8.0** / 协议 **1.9.0**。本文档配套 [README.md](../README.md) 英文项目首页。

---

## 目录

1. [架构与定位](#架构与定位)
2. [前置条件](#前置条件)
3. [安装](#安装)
4. [配置 MCP 客户端](#配置-mcp-客户端)
5. [20 个工具详解](#20-个工具详解)
6. [典型工作流](#典型工作流)
7. [环境变量速查](#环境变量速查)
8. [可选功能 / Optional features](#可选功能--optional-features)
9. [故障排查](#故障排查)
10. [性能与调优](#性能与调优)

---

## 架构与定位

`unity-mcp-py` 是一个 **MCP stdio server**，把 Unity Editor 的能力暴露给 Claude 等大模型 client。它本身**只是中继**：

```
LLM (Claude Code)  ──stdio JSON-RPC──▶  unity-mcp-py  ──TCP 17873──▶  Unity Editor C# bridge
```

- LLM 通过 MCP 协议调用 `mcp__unity-mcp-py__xxx` 工具
- `unity-mcp-py` 把请求翻成 Unity bridge 的 JSON-RPC 命令
- Unity 端的 C# package `com.local.unitymcp-bridge` 在 Editor 主线程执行
- 结果原路返回

核心设计原则 — **runtime-evidence-only**：所有"写操作"（PlayMode 切换、场景打开、Lua/C# 重载等）执行后会返回一个 `write_op_token`，**LLM 必须用该 token 调用 `observe_evidence` 拿到运行时证据后才能宣告"完成"**。这避免了 LLM 仅凭"我改了文件"就声称功能正确。

---

## 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| Python | ≥ 3.11 | `python --version` |
| Unity Editor | 2022.3+（任何版本，bridge 兼容）| 启动后看 Editor 标题栏 |
| Unity bridge package | `com.local.unitymcp-bridge`，listener on `127.0.0.1:17873` | `Test-NetConnection 127.0.0.1 -Port 17873`（Windows）|
| 自包含 | `bridge_client.py` / `human_collab.py` 已 vendored 进 `_vendor/`，无需装 ECC flow | — |

**自包含说明**：早期版本依赖 `~/.claude/flow/lib`，现已把 `bridge_client.py` / `human_collab.py` vendored 进 `src/unity_mcp_py/_vendor/`，wheel 自带、可在无 ECC flow 的机器上安装。维护者从上游同步用 `scripts/sync_from_flow.ps1`（会自动中性化开发机路径）。

---

## 安装

unity-mcp-py 有 **两个必须配套安装** 的产物：

| 产物 | 装到哪 | 文件 |
|---|---|---|
| **Python MCP server** | 你的开发机 | `dist/unity_mcp_py-0.10.0-py3-none-any.whl` |
| **Unity bridge (UPM 包)** | Unity 工程的 `Packages/` 目录 | `dist/com.local.unitymcp-bridge-0.8.0.tgz` |

Python 0.10.0 / 桥 0.8.0 / 协议 1.9.0（版本各自独立）。**只装一边不通**：Python wheel 没有 bridge 没人接，bridge 没有 wheel 没人喂。

打部署包见 README「Deploy to another machine」与 bundle 内 DEPLOY.md。

### Python 侧安装

#### 方式 A：从源码 editable install（开发推荐）

```bash
git clone <repo-url> <MCP_ROOT>
cd <MCP_ROOT>
pip install -e .[dev]
```

`-e` 让你改源码不用重装；`[dev]` 拉测试依赖。

#### 方式 B：wheel 包安装（部署/分发）

```bash
pip install dist/unity_mcp_py-0.10.0-py3-none-any.whl
```

wheel 自带元数据，安装后 `unity-mcp-py` 命令直接可用。

#### 验证 Python 侧

```bash
python -c "import unity_mcp_py; from unity_mcp_py.tools.observe_evidence import observe_evidence; import inspect; print('OK:', inspect.signature(observe_evidence))"
```

命令行支持：

```bash
unity-mcp-py --version     # 打印版本号后退出
unity-mcp-py --help        # 打印用法说明后退出
unity-mcp-py               # 进入 stdio server 模式（给 MCP client spawn 用，不要手敲）
```

如果你在终端误敲 `unity-mcp-py`（无参数），它会进入 JSON-RPC 等待状态，你回车一下会看到 `Invalid JSON: EOF` 错误——那是它把你的换行键当协议消息解析了。**这是正常表现**，不是装错。要确认装好就用 `--version` 或上面的 `inspect.signature`。

### Unity 侧安装（bridge package）

1. 打开你的 Unity 工程
2. **Window → Package Manager**
3. 左上 **+** 按钮 → **Add package from tarball...**
4. 选 `dist/com.local.unitymcp-bridge-0.8.0.tgz`
5. Unity 导入后，bridge 自动在 `127.0.0.1:17873` 监听

#### 验证 Unity 侧

```bash
# 应能连上 17873
Test-NetConnection 127.0.0.1 -Port 17873     # PowerShell
# 或
nc -zv 127.0.0.1 17873                       # bash
```

或者直接跑端到端：

```bash
python scripts/verify_mcp_protocol.py
```

期望看到 `[OK] All invariants hold`。

### Bridge 源码改动后重新打包

如果你改了 `unity-bridge/com.local.unitymcp-bridge/` 下的 C# 源：

```bash
python scripts/pack_unity_bridge.py
# 产出 dist/com.local.unitymcp-bridge-0.8.0.tgz（或 package.json 里的最新版本号）
```

然后在 Unity Package Manager 重新 "Add package from tarball" 选新的 .tgz——Unity 会把旧版本替换掉。

### 两侧版本对齐规则

| 改动 | 怎么对齐 |
|---|---|
| Python 改动（工具行为/参数/MCP 协议层） | bump Python `pyproject.toml` `version` |
| Bridge 改动（C# / wire 协议） | bump bridge `package.json` `version` 和 `protocol.schema.json` `protocolVersion` |
| 任何 wire-protocol 不兼容改动 | 两边都 bump，且都重打包 |

`unity-mcp-py` 启动时握手会检查 protocol 版本，不匹配会报 `PROTOCOL_VERSION_MISMATCH`。

---

## 配置 MCP 客户端

### Claude Code（`~/.claude.json`）

```json
{
  "mcpServers": {
    "unity-mcp-py": {
      "command": "python",
      "args": ["-m", "unity_mcp_py.server"],
      "cwd": "<MCP_ROOT>",
      "env": {
        "PYTHONPATH": "<MCP_ROOT>/src",
        "UNITY_MCP_PROJECT_PATH": "<PROJECT_ROOT>"
      }
    }
  }
}
```

- `cwd` 仅在 editable install 时需要；wheel 安装不需要
- `PYTHONPATH` 同上
- `UNITY_MCP_PROJECT_PATH` 给 `open_scene` 做路径安全校验，强烈建议设

重启 Claude Code 后，session 头部会列出 20 个 `mcp__unity-mcp-py__*` 工具。

### 其它 MCP client

任何遵循 MCP stdio 规范的 client 都可以，命令统一是：

```bash
python -m unity_mcp_py.server
```

输入输出走 stdin/stdout，日志走 stderr。

---

## 20 个工具详解

约定：
- **写操作**会返回 `write_op_token` 字段，必须用它调一次 `observe_evidence(consume_tokens=[token])`
- 所有参数都是关键字参数，类型注解严格

### 1. `ensure_ready`

**用途**：握手 + 检查 Unity 是否在线，返回 editor 版本和项目路径。

**参数**：
- `project_path: str = None` — 可选，默认读 `UNITY_MCP_PROJECT_PATH`
- `engine_binary_path: str = None` — 占位（MVP 不启动 Unity）
- `auto_enter_playmode: bool = False` — 顺便进 PlayMode

**返回**：
```json
{"ready": true, "alreadyRunning": false,
 "editorVersion": "2022.3.62f1",
 "projectPath": "<PROJECT_ROOT>",
 "playmodeState": "PlayMode"}
```

**用法**：每次会话开头先调一次，确认 bridge 通。

---

### 2. `assert_no_compile_errors`

**用途**：拒绝在 C# 编译有错时继续 — 防止 LLM 在错乱状态下做后续判断。

**参数**：无

**返回**：成功返回 `{compiled: true, errors: []}`；失败抛 `BridgeError`。

---

### 3. `query_scene`

**用途**：拉当前活跃场景的根 GameObjects 概要。

**参数**：
- `scene: str = None` — 可选场景名；不传则当前活跃场景

**返回**：
```json
{"sceneName": "Main", "rootObjects": [
  {"name": "Player", "instanceId": 123, "active": true,
   "components": ["Transform","Rigidbody"], "childCount": 2}
]}
```

---

### 4. `get_console_logs`

**用途**：增量拉 Unity Console 日志，可按级别/正则过滤。

**参数**：
- `since_seq: int = None` — 上次的 cursor；None 时用 recorder 内部 cursor
- `level: list[str] = None` — `["Log","Warning","Error","Exception","Assert"]` 子集
- `pattern: str = None` — Python 正则，对 message 字段匹配
- `limit: int = 200` — 最多返回多少条（上限 500）

**返回**：
```json
{"lines": [{"seq":100, "level":"Error", "message":"...", "stack_trace":"..."}],
 "next_seq": 100, "truncated": false, "cursor": 50}
```

**典型用法**：
```python
# 只看错误
get_console_logs(level=["Error","Exception","Assert"], limit=20)
# 增量拉
get_console_logs(since_seq=next_seq_from_last_call)
```

---

### 5. `observe_evidence` ⭐

**用途**：核心证据观察工具。一次拿到 play state + log delta + scene + 可选截图。

**参数**：
- `since_line_number: int = None` — 日志增量起点
- `consume_tokens: list[str] = None` — 消费 write_op_token（必须）
- `include_screenshot: bool = False` ⚠️ — **默认不截图**（payload 缩小 3×）
- `max_log_lines: int = None` — 默认 500；想要小 payload 可以设小

**返回**：
```json
{"play_state": "PlayMode",
 "current_line_number": 1234,
 "log_delta": [{"line_number":1100, "log_type":"Log", "condition":"...", "stack_trace":"..."}],
 "scene_objects": [...],
 "screenshot_png_base64": "",
 "screenshot_skipped": true,
 "consumed_tokens": ["abc-123"],
 "expired_tokens": [],
 "unknown_tokens": []}
```

**性能**：默认 ~50ms / 600KB；带截图 ~400ms / 2MB。**除非真要看画面，不要传 `include_screenshot=True`**。

---

### 6. `capture_screenshot` ⭐

**用途**：截 Game View 当前画面。

**参数**：
- `width: int = None`、`height: int = None` — 输出分辨率
- `supersize: int = 1` — 倍率
- `mode: str = "full"` ⚠️ — `full | metadata | file_ref`

**mode 详解**：

| mode | png_base64 | file_path | 用途 |
|---|---|---|---|
| `full` | 完整 base64 | `""` | 旧默认；LLM 要看图时用 |
| `metadata` | `""` | `""` | 只要 sha256 + sizeBytes，payload < 300B |
| `file_ref` | `""` | `%LOCALAPPDATA%/unity-mcp-py/screenshots/<sha>.png` | 本地 agent 想读字节但不想吃 LLM token |

**返回**：
```json
{"png_base64":"...", "sizeBytes":1234567, "sha256":"a1b2...",
 "mode":"full", "file_path":"",
 "play_state":"PlayMode", "line_number":1234,
 "screenshot_available":true, "capture_error":null}
```

**EditMode 注意**：Game View 没相机时 C# 会返回 `INTERNAL: Passed in texture is invalid (null)`，本工具自动降级为 `screenshot_available=false`，**不抛异常**。

**审批门（条件阻断，2026-06-08）**：截图**仅在代码 reload 之后**才会阻断等待用户点「继续」——`csharp_reload_scripts` / `lua_reload_module` 成功后标记「代码已改」，下一张截图弹审批门，用户确认即清除该标记；其后的截图不再阻断（可连拍多张看效果），直到下一次 reload。无 reload 时截图直接返回、不阻断，且 `file_ref` 落盘图会在**下次截图时**被自动清理（磁盘最多保留一张）。验证横幅与人为接管检测始终在线，与是否阻断无关。

---

## 最终验证规约（强制）

这套规约通过 FastMCP 的 `instructions`（会话开始即注入 AI 上下文）+ `capture_screenshot` / `simulate_*` 工具描述下发，源码在 `src/unity_mcp_py/server.py` 的 `SERVER_INSTRUCTIONS`，被 `tests/test_verification_guidance.py` 锁定。改动 `server.py`/工具后**需重启 MCP server**（instructions 在进程启动时读取一次）。

当要宣称某改动「完成 / 修好 / 生效」前，最终验证必须遵守：

1. **必须截图**：最终验证至少要有一张 `capture_screenshot`，展示真实运行画面里的预期结果。只靠 `lua_eval` / 读 log / 断言通过**不算验证**——它们可能在画面错误、或功能分支根本没走到的情况下也「通过」。看图确认符合意图再报成功。
2. **复现用户步骤要模拟人操作**：若用户的需求里描述了复现方式（点哪、按什么键 / 手柄键、输入什么、推哪个按钮），最终验证就用 `simulate_click` / `simulate_key` / `set_input_text` 把同一串操作跑一遍（用 `open_panel` 开面板、`list_ui_nodes` 找节点），验证真实交互的结果——不要用绕过输入链路的内部函数抄近路。
3. **截图前先确认所在界面**：截图前可以、且在不确定时鼓励**先问用户**当前是否在相关界面 / 状态（例如「你现在是否在 X 界面？」），让截图截到对的东西。
4. **跨多界面→多张截图**：若验证跨越多个界面 / 状态（开面板 → 导航 → 触发 → 结果），在每个关键步骤各截一张，让整条流程都有图为证，而非只截最后一帧。

**易踩坑**：手柄导航（`PROJECT_NAVIGATION_SERVICE`）只在真实手柄 / 主机模式下激活；鼠标模式下 `Navigate` 类调用会**静默 no-op**——所以鼠标模式下读探针得到的「没反应」什么都证明不了。手柄行为要靠看真实运行画面（截图）验证，必要时请用户亲自推物理手柄。

---

### 7-13, auto_login. 写操作工具

所有写操作返回 `write_op_token` + `requires_observation: true` + `line_number_before`。**必须**接 `observe_evidence(consume_tokens=[token])`。

| 工具 | 参数 | 作用 |
|---|---|---|
| `enter_playmode` | — | EditMode → PlayMode |
| `exit_playmode` | — | PlayMode → EditMode |
| `auto_login` | `username: str = None, server_id: int = None, scene_id: int = None, timeout_s: float = 15.0` | PlayMode 登录界面用配置账号驱动登录；账号 arg→env（`UNITY_MCP_LOGIN_USERNAME`/`_SERVER_ID`/`_SCENE_ID`）；返回 `accepted`（服务器是否受理，token 置位即受理，可能早于完全进世界）；**不**负责进 PlayMode；绕过 `PROJECT_LOGIN_CONTROLLER:PROJECT_LOGIN_CLICK` 选服校验与 `-<serverId>` 后缀，须传服务端精确账号名 |
| `open_scene` | `scene_path: str` | 打开 `.unity` 场景；路径强校验（拒绝 UNC / 绝对路径 / `..` / 越出 `UNITY_MCP_PROJECT_PATH`）|
| `run_method` | `type_name: str, method_name: str, args: list = None` | 调用 C# 静态方法 |
| `lua_eval` | `script: str, goal: str, chunk_name: str = None` | 在活 xLua VM 跑任意 Lua（最强逃生口）。`goal` 必填（声明目的，不再计预算）。受**可恢复的 grounding runway** 约束：连跑若干次未"看现实"会抛**可恢复**错（默认 5 次），用 `capture_screenshot`/`get_console_logs`/`observe_evidence`（软，封顶 8）或 `verify_assertions`（硬，全清）grounding 后即可继续——**不再锁死会话**。返回里带 `lua_runway` 状态块。见下方说明 |
| `lua_reload_module` | `modules: list[str], imp_files: list = None, del_key_models: list = None` | 走 `PROJECT_ADAPTER.Reload.reload`；模块名走严格白名单 `^[A-Za-z0-9_][A-Za-z0-9_.]*$` |
| `csharp_reload_scripts` | `force: bool = False` | 程序集重编 + 重载 |
| `clear_console` | — | 清空 Console（也会重置 cursor）|

---

### lua_eval 的 grounding runway（可恢复，不锁会话）

`lua_eval` 是最强逃生口（活 VM 跑任意 Lua），所以**有界**——但**可恢复**，不再是杀会话的固定上限（旧的"双闸 N=5/目标、M=20 distinct 目标"已废弃，撞上限锁死整会话的行为不复存在）。

预算度量的是**"距上次与现实比对，已连跑了多少次 lua"**（`since_ground`）：

- **软门（默认 5）**：`since_ground` 达阈值时，下一次 `lua_eval` 抛**可恢复**的 `LuaGroundingRequiredError`（调桥前抛，零副作用、不发 token）。去做一次 grounding 即可继续。
- **grounding = 真实桥往返**：`capture_screenshot`（真出一帧）/ `get_console_logs` / `observe_evidence` 是**软** grounding——重置 `since_ground`，但每次记一笔 `soft_used`，**封顶 `soft_limit`（默认 8）**；`verify_assertions` 是**硬** grounding——`since_ground` 与 `soft_used` 全清。软观察用尽后纯截图/读 log 不再解锁，**必须** `verify_assertions` 把"中间态应满足什么"写成可裁决断言通过（这是**防幻视**的关键杠杆）。
- **硬 grounding 永不死锁**：`verify_assertions` 自身只读、**不计** runway，所以永远可调用来恢复（其 `lua_expr` 也不再计预算）。无论断言 PASS/FAIL 都全清——失败也已让 agent 拿到裁决过的现实。
- **唯一终止性上限**：进程级 `RUNAWAY`（默认 500）失控兜底，只有真死循环才触及。
- 每个 runway 感知工具（`lua_eval` 及四个 grounding 工具）返回里都带 `lua_runway` 块 `{since_ground, soft_used, soft_limit, threshold, grounded}`，可据此在撞墙前主动 grounding。
- 调参：`UNITY_MCP_LUA_RUNWAY` / `UNITY_MCP_LUA_SOFT_LIMIT` / `UNITY_MCP_LUA_RUNAWAY_MAX`（见[环境变量速查](#环境变量速查)）。

---

### auto_login（MCP 驱动游戏登录）

**用途**：在 PlayMode 登录界面，用配置账号把客户端从登录页驱动进入已认证会话。复用 Bot `PROJECT_LOGIN_TEST` 的 `PROJECT_LOGIN_SERVICE:PROJECT_TEST_LOGIN()` 路径——纯 Lua 驱动，无需改 C# 桥。

**前置**：已在 PlayMode 且停在登录界面（本工具不负责进 PlayMode，先用 `ensure_ready` / `enter_playmode`）。

**凭据**（arg 覆盖 env）：

| 字段 | arg | env |
|---|---|---|
| 账号 | `username` | `UNITY_MCP_LOGIN_USERNAME` |
| 服务器 id | `server_id` | `UNITY_MCP_LOGIN_SERVER_ID` |
| 场景 id（可选）| `scene_id` | `UNITY_MCP_LOGIN_SCENE_ID` |

账号字符集 `[A-Za-z0-9_-]`（注入安全）。

**返回**：成功 `{accepted, loggedIn, finalState, username, serverId, elapsedMs, write_op_token, requires_observation}`；失败 `{accepted:false, error, remedy}`，`error ∈ {not_in_playmode, login_agent_unavailable, login_timeout}`。已登录则短路 `{accepted:true, loggedIn:true, note:"already_logged_in"}`。`accepted:true` 表示服务器受理登录（`PROJECT_LOGIN_SERVICE.token` 置位），可能早于完全进入世界；全新账号走创角分支，仍报 `accepted:true, loggedIn:false`。

**注意**：一旦发起登录即产生 `write_op_token`（即使超时也要 `observe_evidence` 消费）；登录失败时 `get_console_logs` 会有 `SERVER_LOGIN_FAIL`。

---

### 14. `list_pending_writes`

**用途**：列出所有未消费的 token，附带一个供「验证子代理」用的 prompt 模板。

**典型场景**：主 LLM 发了 5 个写操作，可以拉这个列表 → 起 5 个并行子代理各自消费一个 token 做验证。

---

### 15. `human_collab_gate`

**用途**：Mode B 人工协作门 — LLM 改完代码后，让人工先操作 Unity 走一遍真实流程，再让 LLM 拿差异判断是否成功。

**两次调用协议**：

**第一次**（不带 `user_choice`）：
- 拍 baseline 快照（log 行号、当前 PNG）
- 返回 `verdict: "awaiting_user"` + `_wu_id`（不透露绝对路径）

**第二次**（带 `user_choice="Done|Skipped|Failed"` + `_wu_id`）：
- 拉新 baseline，diff
- 返回 `verdict: "passed|skipped|failed"` + `log_delta` + `error_count`

参数：
- `human_instruction: str` — 给人看的操作说明
- `fail_levels: list[str] = ["Error","Exception","Assert"]` — 算失败的 log 等级

---

### 16-18. Recorder 会话录制 / 回放

| 工具 | 参数 | 作用 |
|---|---|---|
| `record_start` | `session_id: str = None, note: str = ""` | 开录；返回 `session_id` + JSONL 路径 |
| `record_stop` | — | 停录；返回 `line_count` |
| `list_sessions` | `limit: int = 20` | 列最近 N 次会话（newest-first）|
| `replay` | `session_id: str, dry_run: bool = False, skip_tools: list = None, stop_on_error: bool = True` | 把录像里每条工具调用按顺序重发；`skip_tools` 默认包含 `human_collab_gate`（避免再次卡用户）|

**用途**：
- 调试：回放复现 bug
- 回归：录一段 happy path，每次升级跑一遍
- 训练：把会话日志喂给离线 LLM 评估

---

## 典型工作流

### 场景 A：让 Claude 改代码并自测

```
User: 帮我给 HomeStationManage 面板加一个"刷新"按钮，PlayMode 测一下没报错。

Claude:
1. （改 Lua 文件）
2. lua_reload_module(modules=["Guis.Panels.HomeStationManage"])
   → write_op_token: a1
3. observe_evidence(consume_tokens=["a1"], max_log_lines=50)
   → log_delta 有没有 reload 异常？
4. enter_playmode()
   → write_op_token: a2
5. observe_evidence(consume_tokens=["a2"], include_screenshot=True)
   → 看 PlayMode 起来没？画面里有按钮没？
6. （LLM 触发按钮 — 通过 run_method 或 human_collab_gate）
7. observe_evidence(consume_tokens=[...], include_screenshot=True)
   → 行为有没有触发？日志有没有 Error/Exception？
8. exit_playmode() → observe_evidence
9. 报告：刷新按钮已加，PlayMode 5 秒内无 error，截图见 ...
```

### 场景 B：Mode B 人工验证

适合 LLM 做不了"用户操作"的场景（点击复杂 UI / 多步交互）。

```
1. （LLM 改代码）
2. csharp_reload_scripts() → token: b1
3. observe_evidence(consume_tokens=["b1"]) → 编译过没？
4. human_collab_gate(human_instruction="请打开公告面板并切到'活动'页",
                     fail_levels=["Error","Exception"])
   → 返回 verdict: "awaiting_user", _wu_id: "mcp-abcd1234"
5. ──── 这时 LLM 调 AskUserQuestion 弹"完成/跳过/失败"给用户 ────
6. human_collab_gate(user_choice="Done", _wu_id="mcp-abcd1234",
                     human_instruction="...")
   → 返回 verdict: "passed", log_delta: [...无 error]
```

### 场景 C：录制 + 回放调试

```
1. record_start(note="repro bug-12345")
2. （正常工作流，把 bug 复现）
3. record_stop()  → session_id: "20260525T120000-abcd"
4. 改代码
5. replay(session_id="20260525T120000-abcd", stop_on_error=False)
   → 看 diffs / errors：哪一步行为变了？
```

---

## 环境变量速查

| 变量 | 默认 | 作用 |
|---|---|---|
| `UNITY_MCP_BRIDGE_HOST` | `127.0.0.1` | bridge 主机；仅接受字面量环回 IP，不支持非环回或 DNS 主机名 |
| `UNITY_MCP_BRIDGE_PORT` | `17873` | bridge TCP 端口 |
| `UNITY_MCP_PROJECT_PATH` | （unset）| `open_scene` 路径校验锚点；强烈建议设 |
| `UNITY_MCP_SCHEMA_PATH` | （unset）| 协议 schema 文件路径；新设备留空自动降级到内置版本，仅旧开发机指回本地 schema 时填 |
| `UNITY_MCP_SESSIONS_DIR` | `%LOCALAPPDATA%/unity-mcp-py/sessions` | recorder JSONL 落盘目录 |
| `UNITY_MCP_SCREENSHOT_DIR` | `%LOCALAPPDATA%/unity-mcp-py/screenshots` | `mode=file_ref` 截图落盘目录 |
| `UNITY_MCP_RECORD_ELISION_BYTES` | `65536` | recorder 大字符串折叠阈值 |
| `UNITY_MCP_HCG_LOG_DIR` | `${TEMP}/unity-mcp-py-hcg` | human_collab_gate 日志目录 |
| `UNITY_MCP_NOTIFY_MAX` | `2000` | bridge_client notification 队列上限 |
| `UNITY_MCP_LUA_RUNWAY` | `5` | `lua_eval` grounding runway：距上次"看现实"可连跑多少次 lua，超出即可恢复报错要求 grounding |
| `UNITY_MCP_LUA_SOFT_LIMIT` | `8` | 两次硬验证之间软观察（截图/读 log/observe）解锁 runway 的次数上限；用尽后必须 `verify_assertions` |
| `UNITY_MCP_LUA_RUNAWAY_MAX` | `500` | `lua_eval` 进程级失控兜底总次数；唯一的终止性上限，仅死循环会触及 |
| `UNITY_MCP_ENABLE_PROFILER` | （unset）| 设 `1` 开启可选性能分析工具 `profile_performance`；默认关闭、不注册（零足迹）|
| `UNITY_MCP_PROFILER_THRESHOLD_PCT` | `5` | `profile_performance` 回归判定阈值（百分比）：单指标变化超过此值才标 regressed/improved |
| `UNITY_MCP_PROFILER_SNAPSHOT_DIR` | `${TEMP}` | `memory_snapshot=True` 时 `.snap` 堆快照落盘目录（仅装了 `com.unity.memoryprofiler` 时生效）|
| `UNITY_MCP_LOGIN_USERNAME` | （unset）| `auto_login` 默认账号名；arg 可覆盖 |
| `UNITY_MCP_LOGIN_SERVER_ID` | （unset）| `auto_login` 默认服务器/集群 id |
| `UNITY_MCP_LOGIN_SCENE_ID` | （unset）| `auto_login` 新角色默认场景 id（可选）|

---

## 可选功能 / Optional features

部分工具默认**不注册**（dark by default），需用环境变量显式开启，开启后才出现在工具列表。关闭时零导入、零足迹，对既有工具面无任何影响。

### `profile_performance`（性能分析，需 `UNITY_MCP_ENABLE_PROFILER=1`）

在客户端 MCP 配置的 `env` 里加 `"UNITY_MCP_ENABLE_PROFILER": "1"` 即可启用。它采样 Unity 引擎 `ProfilerRecorder` 计数器（CPU/渲染/GC/内存），跨多帧非阻塞采集，支持**场景矩阵**（画质等级 × 目标帧率 × 分辨率 × 画质子参数 × 游戏状态 Lua）一次覆盖多种"模拟机器/设置"。

工具**自管理生命周期**：首次调用记录 baseline（进程级缓存，**断开 bridge 不会清**），之后调用自动 diff 出每场景每指标的前后变化并标注回归；`reset=True` 清空 baseline 重新开始。无法分析时（未进 PlayMode / 无可用计数器）返回 `analyzable:false` + 原因 + 补救建议，而非无意义的 0。

参数：`scenarios`（场景矩阵，≤8；省略则用 baseline 矩阵或当前 Editor 状态）、`mode`（`cpu`/`memory`/`both`）、`sample_frames`（每场景采样帧数，默认 120）、`memory_snapshot`（按需触发完整堆快照，需 `com.unity.memoryprofiler`）、`reset`。

相关环境变量见[环境变量速查](#环境变量速查)：`UNITY_MCP_ENABLE_PROFILER` / `UNITY_MCP_PROFILER_THRESHOLD_PCT` / `UNITY_MCP_PROFILER_SNAPSHOT_DIR`。

---

## 故障排查

### 启动报错 `unity-mcp-py bridge adapter expected ... missing`

`bridge_client.py` / `human_collab.py` 已 vendored 进 `src/unity_mcp_py/_vendor/`，正常 wheel 安装不应出现此错。若出现，多半是包损坏或源码树残缺，重装 wheel 或重新 `git clone` 即可。

### `bridge_unreachable: connect to 127.0.0.1:17873 failed`

1. Unity Editor 没开 → 启动
2. Unity 开了但 bridge 没起 → 检查 `Packages/com.local.unitymcp-bridge` 已安装；可能被 `ProjectSettings/UnityMCPBridge.disabled` 标记位关掉了
3. 端口被占 → `netstat -ano | findstr 17873` 看是不是别的 Unity 进程

### `PROTOCOL_VERSION_MISMATCH`

bridge 端协议是 1.9.0，但 `unity-mcp-py` 跑了旧的 fallback。升到最新版（fallback 已升 1.9.0），或确保能读到 `protocol.schema.json`。

### `observe_evidence` 返回的截图字段总是空

那是当前的默认行为。要截图请显式传 `include_screenshot=True`。这是为了避免每次 observe 都浪费 2MB token。

### `capture_screenshot` 返回 `screenshot_available: false`

Game View 在 EditMode 没相机时会触发。先进 PlayMode 或在 Editor 里手动放置相机。

### `InvalidScenePathError`

`open_scene` 拒绝了你的路径。检查：
- 必须 `.unity` 结尾
- 必须项目相对路径（不能 `<PROJECT_ROOT>` / `\\server\share\...` / 含 `..`）
- 必须落在 `UNITY_MCP_PROJECT_PATH` 之下

---

## 性能与调优

### 实测（PlayMode，live Unity）

| 调用 | wall-clock | payload |
|---|---:|---:|
| `observe_evidence()` 默认 | 52 ms | 600 KB |
| `observe_evidence(max_log_lines=20)` | 46 ms | 23 KB |
| `observe_evidence(include_screenshot=True)` | 378 ms | 1.8 MB |
| `capture_screenshot(mode='metadata')` | 327 ms | **254 B** |
| `capture_screenshot(mode='file_ref')` | 304 ms | 391 B |
| `capture_screenshot(mode='full')` | 410 ms | 1.2 MB |
| `query_scene()` | 46 ms | 1.8 KB |
| `get_console_logs(limit=50)` | 16 ms | 60 KB |

### 调优建议

1. **不要无脑开截图**：default 不开是为了省 token，确实要看画面再开
2. **窄 query 日志**：`get_console_logs(level=["Error"], limit=10)` 比 `observe_evidence()` 拿 500 行更便宜
3. **批量验证用 file_ref**：连续截多张图就用 `mode=file_ref`，本地 agent 用 sha256 索引复用
4. **长 PlayMode 时 notification 队列已 cap 2000**：默认值，UNITY_MCP_NOTIFY_MAX 可调
5. **录制大会话**：超过 64KB 的字符串自动 elide 成 `{_elided, len, sha256}`，磁盘消耗有界

---

## 进一步阅读

- 架构 spec: `docs/specs/2026-05-12-unity-mcp-py-design.md`
- Recorder spec: `docs/specs/2026-05-16-console-and-record-replay-design.md`
- 优化对比: `docs/superpowers/benchmarks/2026-05-25-comparison.md`
- 真实环境验证: `docs/superpowers/benchmarks/2026-05-25-real-environment-verification.md`

---

**问题反馈**：在源码仓 Issues 里开 ticket，复现命令贴上 `python scripts/verify_mcp_protocol.py` 的输出。
