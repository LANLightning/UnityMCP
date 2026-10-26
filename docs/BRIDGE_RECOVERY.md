# Bridge connection pairing, diagnosis, and recovery

The bridge now separates its **business connection** from short-lived
**management connections**. A status scan no longer consumes Unity's one business
connection slot, and a responsive Editor can clear an accepted client without
restarting either process.

## Use session slash commands

After restarting or reconnecting the `unity-mcp-py` MCP server, Claude Code
discovers these MCP prompts as session commands. They behave like `/model`: type
the command in the conversation instead of opening a shell.

```text
/mcp__unity-mcp-py__unity_pair codex-game-a 17873 game-a
/mcp__unity-mcp-py__unity_status 17873 codex-game-a
/mcp__unity-mcp-py__unity_owners 17873
/mcp__unity-mcp-py__unity_diagnose 17873
/mcp__unity-mcp-py__unity_clear accepted 17873 codex-game-a game-a
/mcp__unity-mcp-py__unity_repair 17873 codex-game-a game-a false
```

The generated prompt calls the structured `bridge_admin` MCP tool; it does not
ask the model to synthesize a PowerShell command. Completion/refusal status,
endpoint owners, diagnosis evidence, and repair outcome therefore remain machine
readable. `unity_clear` accepts `binding`, `accepted`, or `listener` as its first
argument. Set the final `unity_repair` argument to `true` only for an intentional
forced takeover; bridge identity checks still apply.

The longer `unity-mcp-py bridge ...` commands below remain available for scripts
and recovery when the MCP session itself cannot start.

## Give both ends stable names

Start each Unity Editor with its own port and bridge ID:

```powershell
$env:UNITY_MCP_BRIDGE_PORT = "17873"
$env:UNITY_MCP_BRIDGE_ID = "game-a"
```

Start the corresponding MCP process with a client ID:

```powershell
$env:UNITY_MCP_CLIENT_ID = "codex-game-a"
```

Both processes also need the same management secret. When Unity and the MCP
server run under the same OS account, they automatically create/read
`~/.unity-mcp/auth-token`. For separate accounts, set the same value (at least 32
characters) before starting each process:

```powershell
$env:UNITY_MCP_AUTH_TOKEN = "replace-with-one-long-random-secret"
```

The secret itself is never sent over the socket. Each connection starts with a
server challenge and then uses nonce-bound HMAC proofs. Bridge transport is
restricted to literal loopback addresses (`127.0.0.0/8` or `::1`); remote and
DNS-named endpoints are intentionally unsupported because business frames are
not encrypted.

Each concurrently running Unity Editor still needs a distinct port. The bridge ID
is the identity guard that prevents a reused port from silently connecting to the
wrong project.

Persist the explicit pair:

```powershell
unity-mcp-py bridge pair `
  --client-id codex-game-a `
  --port 17873 `
  --bridge-id game-a
```

The pair is loaded when that MCP process creates its bridge session. Existing
`UNITY_MCP_BRIDGE_HOST` and `UNITY_MCP_BRIDGE_PORT` settings remain the fallback
when no named pair exists.

## Query both endpoint owners

```powershell
unity-mcp-py bridge owners --port 17873
unity-mcp-py bridge status --port 17873 --client-id codex-game-a
```

`owners` returns the operating-system view of the listener and both sides of every
matching TCP connection, including PID, process name, addresses, ports, and TCP
state. `status` adds Unity's logical view: `bridgeId`, accepted `clientId`, client
instance ID, connection generation, and current watchdog evidence.

## Diagnose a stuck connection

```powershell
unity-mcp-py bridge diagnose --port 17873
```

The stable `reason` values are:

- `listener_missing`: nothing owns the requested listener.
- `healthy_idle`: Unity responds and no business client is accepted.
- `occupied`: Unity responds and reports the accepted client identity.
- `bridge_main_thread_unresponsive`: TCP exists but the Editor did not process a
  management request before the timeout.
- `unity_main_thread_wedged`: the watchdog has evidence of a long-running command.

Every diagnosis includes the raw socket and bridge evidence; it does not infer a
deadlock solely from a TCP timeout.

## Clear one endpoint

Disconnect only the named accepted client while leaving the listener running:

```powershell
unity-mcp-py bridge clear `
  --side accepted `
  --port 17873 `
  --client-id codex-game-a `
  --bridge-id game-a
```

Restart only the named Unity listener (the response is acknowledged before the
listener restarts):

```powershell
unity-mcp-py bridge clear `
  --side listener `
  --port 17873 `
  --bridge-id game-a
```

Remove only a persisted pair:

```powershell
unity-mcp-py bridge clear `
  --side binding `
  --client-id codex-game-a
```

## Attempt a safe repair

```powershell
unity-mcp-py bridge repair `
  --port 17873 `
  --client-id codex-game-a `
  --bridge-id game-a
```

Safe repair gathers a before snapshot, verifies the bridge identity, asks Unity to
drop the matching accepted client, then re-diagnoses the endpoint. It never kills
Unity or an MCP process and never replays an interrupted business command. Use
`--force` only when you intentionally want to disconnect a different logical MCP
client; bridge identity validation still applies.

A true Unity main-thread deadlock cannot be safely unwound from managed bridge
code. In that case repair returns `manualRestartRequired: true` and preserves the
watchdog command/request evidence so the correct Unity process can be restarted
manually.

## Compatibility

- `unity-mcp-py` with no arguments still starts the MCP stdio server.
- Existing `select_bridge` and `bind_bridge` tools remain available.
- Their session switch is now atomic: a failed candidate connection is closed and
  the previous healthy session remains active.
- `select_bridge(scan=True)` uses the management channel and no longer handshakes
  as a temporary business client.
- TCP keepalive is enabled to reap peers that were terminated without a clean
  close.
