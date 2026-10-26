# UnityMCP：让 AI 通过 MCP 控制 Unity Editor

UnityMCP 由 **Python MCP 服务**和 **Unity Editor C# Bridge 包**组成。AI 根据工具定义选择操作，Python 校验参数并发出桥接命令，Unity 在编辑器主线程执行，再把状态、日志、截图或断言结果交还给 AI。

> **公开脱敏适配模板：**文档、示例和源码中的 `PROJECT_*` / `<PROJECT_*>` 均为通用代称，不对应任何真实项目。高级功能的项目接口必须由使用者在自己的私有环境中接入；公开版不包含真实绑定关系。本地实际运行版本与公开模板分离。

> **兼容性先读：**编辑器连接、场景、播放模式、日志、截图等能力使用 Unity API；UI、Lua、登录、实体、相机和特效等高级能力包含 **项目 UI / Lua 运行时适配接口**，不是任意 Unity 项目安装后都能使用。反射消除了编译时依赖，但没有消除运行时依赖。

## 当前源码状态

| 项目 | 当前值 / 说明 |
|---|---|
| Python 包 | `unity-mcp-py`，元数据版本 `0.10.0` |
| Unity UPM 包 | `com.local.unitymcp-bridge`，元数据版本 `0.8.0` |
| 桥接协议 | `1.10.0`，以桥包内 `protocol.schema.json` 为准 |
| 默认工具数 | **41**，由实际 FastMCP 注册列表核实 |
| 可选工具 | `profile_performance`，开启后共 **42** 个 |
| Python | 3.11+；此次测试环境为 Windows / Python 3.14.3 |
| MCP Python SDK | `mcp>=1.0.0,<2`；使用 v1 FastMCP API，不能直接升级到 v2 |
| Unity | 包声明最低 2022.3；具体项目仍需 EditMode 与实机验证 |
| 版本性质 | 公开脱敏模板；项目绑定需自行适配，不能直接覆盖私有运行版本 |

此次整理保留当前多实例绑定、认证、恢复功能，并整合此前性能分支的两个改动：握手响应超时设为 5 秒；重复截图默认省略相同帧的图像数据。没有把旧分支整个覆盖到新工作区。

**不要直接使用旧 `dist/` 中同名压缩包。**修改 Python 或 C# 后必须重新构建两端；协议相同也不意味着旧包已包含最新实现。具体测试与发布记录见 `PUBLICATION_REPORT.md`。

## 目录导航

- [快速安装](#快速安装)
- [ai-控制的完整链路](#ai-控制的完整链路)
- [重要工具如何实现](#重要工具如何实现)
- [验证闭环与调用示例](#验证闭环与调用示例)
- [项目适配边界](#项目适配边界)
- [安全与数据边界](#安全与数据边界)
- [开发测试和打包](#开发测试和打包)

## 快速安装

### 1. 安装 Python 服务

下载仓库源码后进入根目录，创建专用虚拟环境，不建议把开发依赖装入全局 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

macOS/Linux 使用 `.venv/bin/python`。**Unix 用户必须先完成下文的认证目录权限配置，再启动 Bridge。**

### 2. 安装 Unity Bridge

在 Unity Package Manager 中选择 **Add package from disk...**，打开本仓库的：

```text
unity-bridge/com.local.unitymcp-bridge/package.json
```

或先执行 `python scripts/pack_unity_bridge.py`，再用 **Add package from tarball...** 导入新生成的 tgz。桥包依赖 Newtonsoft JSON、Editor Coroutines、Input System；版本以 `package.json` 为准。

需要打开真实 Unity 项目并等待脚本编译完成。`ensure_ready` **不会替你启动 Unity**。

### 3. 配置 MCP 客户端

`<MCP_ROOT>` 表示本工具源码根目录，`<PROJECT_ROOT>` 表示使用者自己的 Unity 工程，`<UNITY_EDITOR_PATH>` 表示编辑器可执行文件。占位符必须替换后才能执行。

以下是通用 MCP stdio JSON 配置示意，路径替换为本机实际路径。不同客户端的配置文件位置不同。

```json
{
  "mcpServers": {
    "unity-mcp-py": {
      "command": "<MCP_ROOT>/.venv/Scripts/python.exe",
      "args": ["-m", "unity_mcp_py.server"],
      "env": {
        "UNITY_MCP_PROJECT_PATH": "<PROJECT_ROOT>",
        "UNITY_MCP_BRIDGE_HOST": "127.0.0.1",
        "UNITY_MCP_BRIDGE_PORT": "17873",
        "UNITY_MCP_CLIENT_ID": "mygame-editor-agent"
      }
    }
  }
}
```

Codex 的等价项目配置采用 TOML：

```toml
[mcp_servers.unity-mcp-py]
command = '<MCP_ROOT>/.venv/Scripts/python.exe'
args = ['-m', 'unity_mcp_py.server']

[mcp_servers.unity-mcp-py.env]
UNITY_MCP_PROJECT_PATH = '<PROJECT_ROOT>'
UNITY_MCP_BRIDGE_HOST = '127.0.0.1'
UNITY_MCP_BRIDGE_PORT = '17873'
UNITY_MCP_CLIENT_ID = 'mygame-editor-agent'
```

一个稳定且独立的 `UNITY_MCP_CLIENT_ID` 对应一个逻辑客户端；多个客户端不要复用相同身份。未配置时会回退到会话变量或 PID，PID 改变后不能保证自动恢复原绑定。启动后先让 AI 调 `select_bridge(scan=true)`、核对项目，再绑定目标，而不是盲目接管第一个端口。

### 4. 多 Unity 实例与端口

- Bridge 优先监听本机 `127.0.0.1:17873`。
- 未显式指定端口且端口占用时，Unity listener 可回退至 `17874..17883`。
- Python 默认扫描覆盖 `17873..17882`；**17883 需要明确指定**，不要把扫描范围与回退范围混为一谈。
- 优先使用 `bind_bridge` 持久绑定；只想切换当前连接时使用 `select_bridge`。
- 使用 `expected_bridge_id` 核对身份，不仅核对端口；不应自动设置 `force_takeover=true` 抢占其他工作会话。
- Python 当前只接受字面量 loopback IP，不接受 DNS 主机名或公网地址；旧文档中的远程开关不适用。

## AI 控制的完整链路

```text
用户自然语言
  ↓
外部模型 + MCP 客户端（选择工具、生成结构化参数）
  ↓ MCP / stdio
Python FastMCP：参数 schema → telemetry → 可选 JSONL recorder
  ↓ tools/*.py → bridge_call / BridgeSession
线程转发 + 异步锁串行化 + 身份握手 / HMAC
  ↓ TCP，换行分隔的自定义 JSON request/response
UnityMCPServer：接收请求 → 主线程队列
  ↓ EditorApplication.update
UnityMCPCommands.Dispatch：命令处理器 / 跨帧协程
  ↓
Unity Editor API、反射调用、项目 Lua VM
  ↓
结构化结果 / 日志 / PNG / 断言 → 模型决定下一步
```

**Bridge 中没有大模型，也不会自己理解自然语言。**控制来自模型选择的工具调用，执行来自可审查的 Python/C# 实现。MCP 协议与 Unity TCP 桥接协议是两层不同协议；不要把后者称为完整 MCP JSON-RPC 服务。

实现入口：

- `src/unity_mcp_py/server.py`：工具注册、服务端指导语、记录/遥测中间件。
- `src/unity_mcp_py/tools/`：每个工具的参数处理及业务编排。
- `src/unity_mcp_py/bridge/session.py`：连接、串行调用、切换及取消清理。
- `src/unity_mcp_py/_vendor/bridge_client.py`：连接认证、握手、请求 ID、响应匹配、通知缓冲及超时。
- `unity-bridge/com.local.unitymcp-bridge/Editor/UnityMCPServer.cs`：本机 TCP listener、认证和主线程调度。
- 同目录 `UnityMCPCommands.cs`：命令分派及编辑器/Lua 操作。
- 同目录 `UnityMCPUIInspector.cs`、`UnityMCPWorldObserver.cs`：项目 UI 和世界状态适配。

Unity 主线程每帧使用有限请求数量/时间预算；长时间等待通过协程跨帧完成。Python 异步并不代表 Unity 可以在任意后台线程安全执行 API。

## 重要工具如何实现

以下 Python 入口均为 `src/unity_mcp_py/tools/<工具名>.py`。表中的命令由 `UnityMCPCommands.cs` 分派，除非标注为 Python 本地操作。参数完整 schema 以客户端 `tools/list` 为准；客户端显示的 `mcp__...__` 前缀不是服务器原始工具名。

### A. 连接、身份与恢复（4 个）

| 工具 | AI 如何控制 / 实现 | 使用边界 |
|---|---|---|
| `ensure_ready` | `BridgeSession.connect()` 完成认证和 Handshake，调用 `GetPlayState`；可选进入 PlayMode | `engine_binary_path` 当前不启动程序；项目路径参数不等于严格的目标身份验证。重复连接返回的握手信息可能是缓存状态。 |
| `select_bridge` | 管理通道查询各端口状态，不占用业务槽；准备候选连接，握手成功后切换活动 session | 指定端口、预期 bridge ID；失败不能误把另一项目当成目标。 |
| `bind_bridge` | 在选择逻辑之外持久化 client→bridge 绑定；文件锁、revision/CAS、失败补偿控制并发 | `auto` 遇多实例返回候选，需选择；`restore` 依赖稳定客户端身份；`unbind` 解除当前绑定。 |
| `bridge_admin` | `run_bridge_admin` 分派 `pair/status/owners/diagnose/clear/repair`；使用带认证的控制协议 | `clear` 区分 binding/accepted/listener；破坏性动作核对实例与连接代次，不杀进程。不是公网远程管理工具。 |

### B. 编辑器、场景与编译（8 个）

| 工具 | AI 如何控制 / 实现 | 使用边界 |
|---|---|---|
| `enter_playmode` | 写操作调用 `EnterPlaymode`，设置编辑器播放状态 | 不证明游戏初始化、资源加载或登录完成，需继续观察。 |
| `exit_playmode` | 调用 `ExitPlaymode` 返回编辑模式 | 会停止运行态，应先保存需要的证据。 |
| `open_scene` | Python 校验场景路径，发送 `OpenScene`，C# 使用 `EditorSceneManager` | MCP 参数是 `scene_path`；不能假设桥接协议额外模式均被 MCP 暴露。操作会改变当前场景。 |
| `query_scene` | `GetSceneState` 枚举场景根对象/组件概要，并附带播放状态、日志位置 | 不是完整深度对象图，也不是游戏业务实体列表。 |
| `run_method` | `RunEditMethod` 查找已加载程序集的类型，通过反射调用 public static 方法，收集返回值和日志 | 不是 C# 编译器；有危险类型/命名限制，可用 Unity 进程环境变量 `UNITY_MCP_RUN_METHOD_ALLOW` 收窄类型前缀，但不是沙箱。 |
| `assert_no_compile_errors` | 查询编译状态，发现编译错误时阻止当前检查通过 | 编译通过不等于运行逻辑正确。 |
| `get_console_logs` | `TailEditorLog` 读取内存日志窗口，Python 按游标、级别、正则和数量过滤 | 日志缓冲有上限；不能证明未保留的历史没有错误。 |
| `clear_console` | 查询最新日志位置并推进 Python recorder 的虚拟日志游标，返回 previous_cursor/new_cursor | 不删除 Unity Console 日志，不产生写操作 token；它是逻辑游标重置，不是编辑器清屏。 |

### C. 截图、证据和断言（5 个）

#### `capture_screenshot`

1. 查询验证会话；根据 `session.code_dirty` 决定是否需要人工批准截图。
2. 调用 Unity `CaptureScreenshot`，等待帧处理后取得 PNG。
3. Python 输出 `full`（Base64 图像）、`metadata`（摘要/大小）、`file_ref`（本地 PNG 路径）。
4. 默认 `skip_if_unchanged=true`：仅当 full 模式与该 Python 进程上次实际交付的 full 帧相同时才省略图片，返回 `unchanged`。metadata/file_ref 不更新该交付记录；file_ref 每次返回有效文件路径。已交付并不保证客户端真的展示或模型已经查看；最终验收应设 `skip_if_unchanged=false` 并实际查看图片。
5. `file_ref` 需要客户端另行读取本地图片；后续截图会清理之前跟踪的缓存文件，需留档时自行复制。

代码重载会置 dirty 标记，下一次截图可等待用户点击“继续”。这不是始终无人干预的截图服务。遇到 `screenshot_available=false` 或空图片不能当作已完成视觉验证。图片摘要当前按 Base64 文本计算，不能直接当作 PNG 文件原始字节哈希。

#### `observe_evidence`

顺序取得日志增量、场景根对象、播放状态和可选截图，再消费指定 `write_op_token`。默认不截图，默认精简日志和场景字段；`verbose=true` 可保留更完整信息。该工具提供证据，不替模型理解画面。可选截图失败可能表现为缺少图像，因此必须检查实际输出。

#### `verify_assertions`

Python 服务端执行显式断言并返回 `passed/failures/results`，避免模型逐条猜测：

- `expect_no_errors`：保留的日志窗口中无错误。
- `log_matches`：匹配或排除某正则。
- `play_state`：播放状态符合预期。
- `node_exists/node_visible`：匹配指定 UI 节点。
- `lua_expr`：执行表达式并比较字符串化结果，必须注明 goal。

只证明所写断言，不证明画面正确。`lua_expr` 走真实 Lua 执行，并非强制只读沙箱。有效完成的断言检查会重新补充 Lua runway；断言失败时应看原因，而不是自动宣称验证成功。

#### `list_pending_writes`

读取 Python `WriteOpGuard` 中尚未消费的操作标记，帮助 AI 找出缺失的观察步骤。标记是进程内状态，不随重启永久保存。

#### `escalate_via_click`

同一目标验证反复失败时，执行调用方提供的 `undo_lua`，再用指定 UI 入口 `simulate_click`，可选重新执行断言。用于避免“内部函数把面板打开了，但绕过业务数据初始化”的假成功。undo 不是自动生成的事务回滚；点击也仍然是项目模拟接口。

### D. Lua、热重载与开发登录（4 个）

| 工具 | 实现路径 | 前提和风险 |
|---|---|---|
| `lua_eval` | `LuaEval` → `DefaultLuaDoString()` → 反射获得 `<PROJECT_LUA_VM>` → xLua `DoString()` | 需要项目 VM 与 PlayMode；script 长度受限、goal 必填。runway 要求定期观察现实，但不是权限隔离；客户端超时不能保证终止 Unity 主线程死循环。 |
| `lua_reload_module` | 校验模块列表后调用 `<PROJECT_RELOAD_MODULE>` | 不是所有 Lua 框架通用重载；检查 payload 中的 `ok` 与 traceback。传输成功不代表脚本成功；成功路径标记代码变更。 |
| `csharp_reload_scripts` | `CsharpReloadScripts` → `CompilationPipeline.RequestScriptCompilation`；force 路径处理播放模式切换 | 域重载可能影响连接和运行状态，不承诺任意状态无损热替换。之后重新检查编译并观察。 |
| `auto_login` | 向 `<PROJECT_LOGIN_SERVICE>` 提交测试登录参数，轮询登录 token 是否存在 | 项目特定开发登录入口，不是通用账号密码表单。绕过 SDK/UI 的部分逻辑；被服务器接受不代表角色创建或进场完成。仅对授权测试环境使用。 |

`auto_login` 的默认参数来自 `UNITY_MCP_LOGIN_USERNAME`、`UNITY_MCP_LOGIN_SERVER_ID`、`UNITY_MCP_LOGIN_SCENE_ID`。不要把真实账号写进仓库。Lua runway 的配置为 `UNITY_MCP_LUA_RUNWAY`、`UNITY_MCP_LUA_SOFT_LIMIT`、`UNITY_MCP_LUA_RUNAWAY_MAX`；这是工作流约束，不是恶意脚本隔离。

### E. UI、本地化与输入（6 个）

| 工具 | AI 调用后真实执行 | 不代表什么 |
|---|---|---|
| `list_ui_nodes` | `UnityMCPUIInspector.ListNodes()` 遍历项目 UI layer 下的 PROJECT_UI 节点，提取 instanceId、panelUid、name/path、文字、textId、可见性、交互属性和屏幕矩形 | 不是通用 UGUI/UI Toolkit 检查器；节点数量上限会截断结果。 |
| `resolve_localization` | 根据 ID 和语言反射调用项目 `PROJECT_LOCALIZATION_LOOKUP(int, PROJECT_LANGUAGE)` | 不是自动翻译，也不会替你生成缺失的词条。 |
| `open_panel` | `PROJECT_RUNTIME.global.ui:open(uid, info)` 后用跨帧轮询检查是否打开/可见 | 内部开面板不一定走真实用户入口，不能证明业务数据链正确；info 仅支持平面基本类型。 |
| `simulate_click` | 用 panel UID + refKey/path 找 PROJECT_UI 节点；raycast 模式调 `PROJECT_CLICK_WITH_HIT_TEST(false)`，force 模式调 `PROJECT_CLICK_DIRECT(false)` | 不是操作系统鼠标注入；force 绕过命中检查。必须检查 `clicked/nodeFound`，不是看见 token 就算点中。 |
| `simulate_key` | `PROJECT_RUNTIME.global.PROJECT_INPUT_SERVICE:PROJECT_TRIGGER_ACTION` 依次触发 Performed 和 Canceled | 不是物理键盘/手柄输入；可能绕过焦点等输入 gate，仅覆盖真实 InputActionAsset，虚拟热键可能不触发。 |
| `set_input_text` | 找到输入节点，设置 text、激活输入框，再调用 PROJECT_COMMIT_TEXT | 不是逐键输入，不验证 IME、每键事件或硬件输入路径。 |

**路径基准不同：**`list_ui_nodes.path` 相对于 UI layer root，包含 panel root 名称；`simulate_click` / `set_input_text.path` 相对于指定 panel root。调用前需转换路径，不能原样照抄。`ref_key` 来自项目 PROJECT_NODE_REFERENCE 配置，不能用节点 name 或 instanceId 代替。

这些工具适合让 AI 复现**应用层交互路径**。涉及硬件手柄、输入法、焦点或系统级鼠标行为时，需要用户或真正设备级 E2E 测试补充。

### F. 世界状态、相机与特效（7 个）

| 工具 | 实现 | AI 应如何解读 |
|---|---|---|
| `observe_camera` | `UnityMCPWorldObserver` 查询项目 camera manager，合并反射字段，可计算目标投影 | 观察项目相机状态，不是任意 Camera 的全量管理工具。 |
| `list_entities` | 构造 Lua 查询 `PROJECT_RUNTIME.global.PROJECT_ENTITY_SERVICE`，过滤 actor/class/template 并裁剪数量 | 返回逻辑实体目录，不等于 Hierarchy GameObject 列表。 |
| `observe_entity` | 按 entity/actor/template 定位，采集位置、HP、生命/战斗/动画等状态 | 读取已有实体，不负责生成或修改它。 |
| `list_effects` | 反射项目 effect manager 列出特效生成器/实例 | 是当前时刻快照，瞬态特效可能已结束。 |
| `observe_effect` | Lua 查询实例状态/时长，结合子 ParticleSystem 粒子计数 | 未看到不等于从未播放，应必要时改用时间窗口。 |
| `observe_window` | 跨帧重复计算 Lua 表达式，返回采样值、everTrue、首次/末次命中等 | 可捕捉短暂状态，但采样有间隔；语法限制不是完全纯函数或安全证明。 |
| `operate_camera` | 白名单 action 映射 `PROJECT_RUNTIME.game.camera` 方法，比较 before/after | `applied` 依赖无错误且可观测变化；`settle_ms` 当前保留但未实现延迟等待，不承诺动画已稳定。 |

### G. 人机协作（3 个）

- `start_verification_session`：在 Unity 显示验证提示，监听人类接管输入，配置暂停/中止和截图 gate。提示窗不进入 Game View 截图。显式启动默认 gate 与截图工具按 dirty 自动调整是两条不同路径。
- `end_verification_session`：结束指定 session，清理验证状态。应传对应 session ID，避免误结束后来者。
- `human_collab_gate`：第一次保存截图/日志基线，返回待用户操作状态与 opaque ID；第二次接收真实用户的 Done/Skipped/Failed，再比较证据。基线约一小时过期。AI 不能自行伪造用户已完成的结论。

### H. 录制和回放（4 个）

`record_start` 开启工具调用 JSONL 记录；`record_stop` 停止并返回摘要；`list_sessions` 列出已有记录；`replay` 从注册表查找同名工具重新调用，并比较结果。

**录制不是视频。回放不是只读浏览。**`replay` 默认 `dry_run=false`，可能再次进入播放、点击、改场景或执行代码；先设 `dry_run=true` 检查计划，再选择 `skip_tools` 和 `stop_on_error`。录制参数/结果没有完整敏感字段脱敏，不能直接公开。

### I. 可选性能工具（1 个，默认关闭）

在 Python MCP 进程启动前设置 `UNITY_MCP_ENABLE_PROFILER=1`，重新启动后注册 `profile_performance`：

1. `ProfilerCapabilities` 确认可用性；不可分析时返回原因，不编造指标。
2. 按 scenarios、cpu/memory/both、sample_frames 构造矩阵。
3. Unity 用 `ProfilerRecorder` / 相关采样命令跨帧收集指标，可临时改变质量、目标帧率和分辨率，结束时尝试恢复。
4. 第一次成功采样建立 Python 内存 baseline，后续按场景 label 比较并报告回归。
5. baseline 可跨 Bridge 断线保留，但**不跨 Python 进程重启**；`reset=true` 清空。

它不是自动性能修复器，也不自动证明根因；强退等情况不保证配置恢复。不要拿不同项目或不同工作负载的数据作无条件比较。

## 验证闭环与调用示例

### 写操作标记的真实含义

`_write_op_helper.write_op()` 先取日志游标，执行命令，成功返回后登记 `write_op_token`、`requires_observation` 和 `line_number_before`。AI 再把 token 传给 `observe_evidence` 关联后续观察。

**这不是强制安全事务。**未消费 token 不会统一拦截所有后续操作，payload 里的业务结果也可能失败。服务端 `SERVER_INSTRUCTIONS` 的最终截图规则是模型行为指导，不是不可绕过的“完成证明”。

### 示例流程（工具调用示意，不是 Python 导入调用）

```text
select_bridge(scan=true)
bind_bridge(port=17873, expected_bridge_id="从扫描结果取得的真实 ID")
ensure_ready(project_path="<PROJECT_ROOT>")
assert_no_compile_errors()
enter_playmode()
observe_evidence(consume_tokens=["上一调用返回的 token"])

# 仅在对应项目 UI 适配存在时：
list_ui_nodes(panel_uid=实际面板编号)
simulate_click(panel_uid=实际面板编号, ref_key="真实节点引用", mode="raycast")
observe_evidence(consume_tokens=["点击返回的 token"])
verify_assertions(assertions=[{"type":"expect_no_errors"}])
capture_screenshot(mode="full", skip_if_unchanged=false)
```

工具接受 JSON 类型参数，示例中的中文占位值需换成运行时查到的数据。多屏流程逐步取证；最后实际查看图像。普通没有高级适配的 Unity 项目可先验证编辑器工具，不应直接复制项目 Lua 示例。

## 项目适配边界

公开文档只使用下列接口角色代称，不列出真实项目代码：

- Lua VM：`<PROJECT_LUA_VM>`，表示项目的 Lua 执行入口。
- UI：`<PROJECT_UI_SYSTEM>`，表示节点枚举、定位、点击和输入适配接口。
- 本地化：`<PROJECT_LOCALIZATION_SERVICE>`，表示按词条 ID 和语言查询文本的接口。
- 运行时观察：`<PROJECT_CAMERA_SERVICE>`、`<PROJECT_ENTITY_SERVICE>`、`<PROJECT_EFFECT_SERVICE>`。
- 重载：`<PROJECT_RELOAD_MODULE>`，表示项目自己实现的模块重载能力。
- 登录：`<PROJECT_LOGIN_SERVICE>`，表示授权测试环境的登录适配接口。

源码里使用合法示例标识符（不带尖括号）；它们仍然是占位契约，不是任何真实项目的类名或调用链。通用 Unity/MCP API、协议字段和 MCP 工具名保持原名。
迁移时应修改 `UnityMCPCommands` 中 Lua 获取/调用入口、`UnityMCPUIInspector` 和 `UnityMCPWorldObserver` 的适配，再为目标项目添加 EditMode/PlayMode 测试。MCP 工具的 schema、错误语义和协议字段应保持同步。未适配时如实返回不可用，而不是返回空结果伪装成功。

## 安全与数据边界

- 仅供可信本机开发环境。只监听 loopback 不意味着同一 OS 用户下的恶意进程不可访问。
- 当前认证采用 nonce-bound HMAC，核对 bridge/client/instance/generation；不是 TLS，也不是远程托管服务设计。
- `lua_eval`、`run_method`、业务重放拥有实际执行/修改能力。白名单、runway、token 不能替代 OS 隔离与备份。
- 账号、认证文件、绑定文件、真实环境变量、截图、JSONL、Console 日志都可能含私人数据；不要提交到版本库或直接发给无关模型服务。
- 变更工具可能触发游戏后端请求。使用专门测试账号和环境，避免生产交易、付费资源或不可逆业务操作。
- 请求超时不等于 Unity 中的操作已经撤销；先查状态再决定是否重试。

### macOS/Linux 必须完成的权限缓解

当前 C# Bridge 自动创建认证文件时**尚未自动收紧 Unix 权限**。在默认 umask/可遍历 home 下存在其他本机用户读取 token 的条件性风险。以下是部署要求，不是声称代码已经修复。

关闭 Unity/MCP，确认目录/文件属于当前用户、无额外 ACL 授权且不是共享/同步目录，然后执行：

```sh
umask 077
test ! -L "$HOME/.unity-mcp" || exit 1
mkdir -p "$HOME/.unity-mcp"
chmod 700 "$HOME/.unity-mcp"
if test -e "$HOME/.unity-mcp/auth-token"; then
  test ! -L "$HOME/.unity-mcp/auth-token" || exit 1
  chmod 600 "$HOME/.unity-mcp/auth-token"
fi
```

保持目录 0700，并在首次创建后确认 token 为 0600。仅改 umask 不能修复旧文件，也不能保证 Unity Hub 启动的进程继承设置。已泄露的 token 需由用户另行安排轮换；此操作不会打印或覆盖现有 token。Windows 应确保用户配置目录 ACL 私有。

## 开发测试和打包

```powershell
python -m pip install -e ".[dev]"
python -m pip install pytest-cov pip-audit
python -m pytest -q --cov=unity_mcp_py --cov-report=term-missing
python -m pip_audit
python scripts/build_release.py
```

构建脚本默认先运行测试，再构建 Python wheel、Unity tgz 与部署 bundle。输出写入 `dist/`，不提交旧二进制。不要在测试失败时用 `--skip-tests` 对外声称已验证。

测试分层：Python 单元/MockBridge 集成测试；桥包 `Tests.EditMode`；真实 Unity 项目的手工/自动 PlayMode 验证。Python 测试通过不等于 C# 已在所有 Unity/OS 组合编译，更不等于项目 UI/登录流程已实测。本次没有执行真实游戏交互。

## 常见故障

| 现象 | 检查方法 |
|---|---|
| 找不到 bridge | Unity 是否打开、包是否导入成功、编译是否干净、实际监听端口、禁用标记 |
| 端口忙 / 多实例 | `select_bridge(scan=true)` 和 `bridge_admin(action="status", port=...)`，核对 ID 后再绑定 |
| 握手超时 | 新实现握手响应 5 秒超时；Unity 主线程编译/阻塞时稍后重试，不直接抢占或杀进程 |
| 协议不一致 | 同步更新 Python 与 Unity 包；检查 schema 版本，不只看 wheel/tgz 文件名 |
| Lua/UI 类型找不到 | 项目尚未接入 UI / Lua 占位契约，不能靠换端口解决 |
| 点击无效 | 检查 nodeFound/blocked/ clicked 等实际字段、路径和遮挡；force 与真实用户操作并不等价 |
| 截图等待 | 代码 dirty 后可能需要用户点击继续；不要擅自代替人工确认 |
| 截图没有像素 | 检查 mode、unchanged、screenshot_available；最终视觉验证强制 full 且不跳过 |
| 绑定重启丢失 | 设置稳定独立 client ID，核对绑定存储和预期 bridge ID |

## 许可证

本仓库附 MIT `LICENSE`。该许可证不自动授予第三方游戏项目、素材、SDK 或内部业务接口的使用权；迁移适配前应确认相关授权。
