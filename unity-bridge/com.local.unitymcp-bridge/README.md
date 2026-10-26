# UnityMCP Bridge

Editor-side TCP bridge that exposes Unity automation to MCP clients (Claude Code, /flow verifier, etc.).

## Architecture

- `Editor/UnityMCPServer.cs` — TCP listener on `127.0.0.1:17873`, polled on `EditorApplication.update`. One client at a time.
- `Editor/UnityMCPProtocol.cs` — Wire-protocol POCOs + `ProtocolSerializer` static helpers. `ProtocolConstants.ProtocolVersion` loaded from `protocol.schema.json` at module init.
- `Editor/UnityMCPCommands.cs` — Dispatch table + 13 command handlers (8 original + 5 Phase 2 scene-control commands).
- `Editor/UnityMCPEventBridge.cs` — Subscribes to 5 Unity Editor events and forwards them as `NotificationEnvelope` frames.
- `protocol.schema.json` — **Single source of truth** for wire protocol. Both C# (here) and TypeScript (`~/.claude/mcp-servers/unity-mcp/src/protocol.ts`) derive constants from this file.

## Disable gate

The bridge skips its listener startup when EITHER:
1. Env var `UNITY_MCP_BRIDGE_DISABLED=1` (or `true`)
2. Marker file `ProjectSettings/UnityMCPBridge.disabled` at the project root

Used to safely run multiple Unity instances against the same UPM package without a TCP port-17873 conflict (e.g., a PROJECT_EDITOR working instance + a sandbox verification instance).

## Listener collision recovery

When `UNITY_MCP_BRIDGE_PORT` is unset, the bridge prefers `17873` and automatically
tries the next 10 loopback ports if the preferred port is already owned. The selected
port is reported in the Unity Console and in every bridge status/handshake response.
The bridge continues retrying with capped exponential backoff if the entire candidate
range is unavailable.

An explicit `UNITY_MCP_BRIDGE_PORT` is strict by default so explicit pairing cannot
silently drift to another endpoint. Set `UNITY_MCP_BRIDGE_ALLOW_PORT_FALLBACK=1` for
that process to opt an explicit port into fallback. The number of additional ports is
controlled by `UNITY_MCP_BRIDGE_FALLBACK_PORT_COUNT` (default 10, maximum 100).
An explicit port outside `1..65535` (or a non-numeric value) fails closed and does not
start a listener on a different port.

## Wire protocol

Line-delimited JSON over TCP. See `protocol.schema.json` for full envelope and command definitions. Three frame types:

- **RequestEnvelope**: `{id: UUID, command: string, args?: object}` — client → bridge
- **ResponseEnvelope**: `{id: UUID, ok: bool, result?: object, error?: {code, message}}` — bridge → client, correlated by `id`
- **NotificationEnvelope**: `{event: string, payload: object, timestamp: string}` — bridge → client, unsolicited push (no `id`)

## Testing

Live Unity tests require `UNITY_TEST_FRAMEWORK_AVAILABLE` define (currently never defined — see `Tests.EditMode/com.local.unitymcp-bridge.Tests.Editor.asmdef`). The canonical test pipeline is the standalone .NET project at `~/.claude/mcp-servers/unity-mcp/tests/csharp/UnityMCPBridge.Tests/` — runs via `dotnet test` with mock Unity types and no Editor dependency.
