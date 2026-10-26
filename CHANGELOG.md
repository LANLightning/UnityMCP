# Changelog

All notable changes to unity-mcp-py will be documented in this file.

The format is based on [Keep a Changelog](http<PROJECT_ROOT>
and this project adheres to [Semantic Versioning](http<PROJECT_ROOT>

## [Unreleased]

### Publication integration — 2026-10-26
- Constrained the MCP SDK to v1 (mcp<2) after a clean-install v2 import failure; validated with 1.30.0.
- Current source has 41 default MCP tools, plus the opt-in profiler (42 total).
- Current bridge wire protocol is 1.10.0; package metadata remains Python 0.10.0 / bridge 0.8.0.
- Integrated 5-second Handshake response timeout without dropping authentication/identity fields.
- Enabled duplicate full-frame screenshot suppression by default; cross-mode delivery regression tests accompany the integration.
- Replaced the root README with source-backed tool implementation and compatibility documentation.
- Excluded internal planning documents from the public snapshot; retained source, tests and public usage docs.
- Documented required Unix authentication-directory permission mitigation; automatic C# permission hardening remains outstanding.

> The entries below are historical notes; some earlier tool counts and binding descriptions have been superseded by the current README.


### Added
- **Multi-instance bridge management** (2 new tools) - 2026-08-07
  - `select_bridge` tool: Scan and switch between Unity bridge instances
    - Scan ports 17873-17882 for available instances
    - Switch connection between multiple Unity Editor instances
    - Query current connection status
  - `bind_bridge` tool: Session-level persistent binding ⭐ **Recommended**
    - One-command binding: `bind_bridge(auto=True)`
    - Persistent across MCP server restarts
    - Session isolation (multiple Claude Code windows → different Unity instances)
    - Auto-restore previous binding
  - Support for managing up to 10 concurrent Unity instances
  - Comprehensive documentation in `docs/MULTI_BRIDGE.md`, `docs/UNITY_PORT_CONFIG.md`, `docs/BIND_BRIDGE_QUICK_REF.md`
  - Example scripts demonstrating multi-instance workflows
  - Full test coverage with MockBridgeServer

### Changed
- Updated tool count from 36 to 38 tools (37 + bind_bridge)
- Enhanced `BridgeSession` to support dynamic port switching via `set_session_for_test()`

### Fixed
- Resolved issue where multiple Unity Editor instances on the same port caused Handshake timeouts

## [0.10.0] - 2026-06-12

### Added
- Optional profiler tools (gated by `UNITY_MCP_ENABLE_PROFILER=1`)
- Protocol version 1.9.0 support
- Memory snapshot capabilities

### Changed
- Updated bridge protocol to 1.9.0
- Performance optimizations for screenshot capture (up to 4744× smaller payload)

### Fixed
- Screenshot metadata mode efficiency improvements

## [0.9.0] - 2026-06-08

### Added
- Verification session management
- Assertion verification with escalation support
- Conditional screenshot gating

### Changed
- Enhanced verification workflow with `verify_assertions` tool
- Improved `escalate_via_click` with automatic assertion re-verification

## [0.8.0] - 2026-06-05

### Added
- Non-UI observation tools: `observe_camera`, `list_entities`, `observe_entity`
- Effect observation: `list_effects`, `observe_effect`
- Window observation: `observe_window`
- Camera operation: `operate_camera`
- Protocol version 1.8.0 support

### Changed
- Updated bridge protocol to 1.8.0
- Expanded tool surface to 36 tools

## [0.7.0] - 2026-05-28

### Added
- Simulate suite: `simulate_click`, `simulate_key`, `set_input_text`
- `open_panel` tool for UI navigation
- Protocol version 1.7.0 support

### Changed
- Enhanced UI interaction capabilities
- Updated bridge protocol to 1.7.0

## [0.6.0] - 2026-05-20

### Added
- `list_ui_nodes` tool for UI hierarchy inspection
- `resolve_localization` tool for localized string resolution
- Protocol version 1.5.0 support

### Changed
- Updated bridge protocol to 1.5.0
- Improved UI debugging capabilities

## [0.5.0] - 2026-05-15

### Added
- Record and replay functionality
- Session management: `record_start`, `record_stop`, `list_sessions`, `replay`
- Telemetry layer for performance monitoring

### Changed
- Tool registration now includes recorder middleware
- All tools wrapped with telemetry tracking

## [0.4.0] - 2026-05-10

### Added
- `lua_eval` tool with runway budget management
- `start_verification_session` and `end_verification_session`
- Lua module hot-reload: `lua_reload_module`
- C# script reload: `csharp_reload_scripts`
- Protocol version 1.4.0 support

### Changed
- Updated bridge protocol to 1.4.0
- Enhanced hot-reload capabilities

## [0.3.0] - 2026-05-05

### Added
- `get_console_logs` with filtering support
- `clear_console` tool
- Play state query: `GetPlayState`
- Compile status check: `GetCompileStatus`
- Protocol version 1.3.0 support

### Changed
- Updated bridge protocol to 1.3.0
- Improved console log handling

## [0.2.0] - 2026-05-01

### Added
- `observe_evidence` bundled snapshot tool
- `capture_screenshot` with multiple modes (full/metadata/file_ref)
- Screenshot deduplication via SHA-256 hashing
- Protocol version 1.2.0 support

### Changed
- Updated bridge protocol to 1.2.0
- Performance optimizations for screenshot handling

## [0.1.0] - 2026-04-25

### Added
- Initial release
- Core bridge connectivity: `ensure_ready`
- Basic tools: `assert_no_compile_errors`, `query_scene`
- PlayMode control: `enter_playmode`, `exit_playmode`
- Scene management: `open_scene`
- Method invocation: `run_method`
- Auto-login support: `auto_login`
- Human collaboration gate: `human_collab_gate`
- Protocol version 1.0.0 support
- FastMCP-based architecture
- Vendored bridge_client and human_collab modules
- Comprehensive test suite (101 tests)
- Documentation and usage examples

### Security
- Loopback-only connection enforcement
- Non-loopback bypass requires explicit `UNITY_MCP_ALLOW_NONLOOPBACK=1`

[Unreleased]: http<PROJECT_ROOT>
[0.10.0]: http<PROJECT_ROOT>
[0.9.0]: http<PROJECT_ROOT>
[0.8.0]: http<PROJECT_ROOT>
[0.7.0]: http<PROJECT_ROOT>
[0.6.0]: http<PROJECT_ROOT>
[0.5.0]: http<PROJECT_ROOT>
[0.4.0]: http<PROJECT_ROOT>
[0.3.0]: http<PROJECT_ROOT>
[0.2.0]: http<PROJECT_ROOT>
[0.1.0]: http<PROJECT_ROOT>
