// UnityMCPServer.cs
// [InitializeOnLoad] TCP bridge server for the UnityMCP Editor package.
//
// Lifecycle:
//   - Static constructor runs at every domain reload (Editor startup, script compile, etc.).
//   - Opens a TcpListener on IPAddress.Loopback (never Any) at the configured port.
//   - Polls for new connections and incoming data on EditorApplication.update (main thread).
//   - One client at a time; a second connection attempt receives INVALID_STATE.
//   - Newline-terminated requests are queued and processed within a small per-frame budget.
//   - Tools > UnityMCP > Restart Bridge triggers a manual restart.
//
// Threading model:
//   Unity Editor is single-threaded. The listener socket stays in BLOCKING mode and we
//   use TcpListener.Pending() as a non-blocking gate before AcceptTcpClient() — this is
//   the correct non-busy pattern. Forcing Server.Blocking=false would make AcceptTcpClient
//   throw "operation not allowed on a non-blocking Socket" every frame.

using System;
using System.Collections.Generic;
using Stopwatch = System.Diagnostics.Stopwatch;
using System.Net;
using System.Net.Sockets;
using System.Text;
using UnityEditor;
using UnityEngine;
using UnityMCP.Bridge.Protocol;

namespace UnityMCP.Bridge
{
    [InitializeOnLoad]
    public static class UnityMCPServer
    {
        // -------------------------------------------------------------------------
        // Configuration
        // -------------------------------------------------------------------------

        private const string LogPrefix = "[UnityMCP] ";
        private const int ReadBufferSize = 65536;
        private const int MaxRequestsPerEditorUpdate = 4;
        private const int MaxRequestProcessingMillisPerUpdate = 4;
        private const int MaxPendingConnections = 8;
        private const int MaxCandidateBytes = 65536;
        private const double CandidateTimeoutSeconds = 2.0;
        private static readonly Encoding StrictUtf8 = new UTF8Encoding(false, true);

        // -------------------------------------------------------------------------
        // State
        // -------------------------------------------------------------------------

        private static TcpListener s_listener;
        private static TcpClient s_client;
        private static NetworkStream s_stream;

        private sealed class CandidateConnection
        {
            public TcpClient Client;
            public NetworkStream Stream;
            public readonly StringBuilder Buffer = new StringBuilder(1024);
            public readonly Decoder Decoder = StrictUtf8.GetDecoder();
            public int ReceivedBytes;
            public string AuthNonce;
            public double AcceptedAt;
        }

        private static readonly List<CandidateConnection> s_candidates =
            new List<CandidateConnection>();

        // Serializes writes to s_stream. Normally only the main thread writes (one
        // response per request, serially), so this is uncontended. The wedge
        // watchdog runs on a BACKGROUND thread and may call TrySendWedgeAlert while
        // the main thread is frozen inside a handler — the lock guarantees neither
        // can interleave bytes into a half-written frame. During a real wedge the
        // main thread is stuck in (e.g.) DoString, not mid-write, so the lock is
        // free for the watchdog to take.
        private static readonly object s_writeLock = new object();

        /// <summary>Ring-buffer of accumulated bytes waiting to be split into lines.</summary>
        private static readonly StringBuilder s_lineBuffer = new StringBuilder(4096);
        private static readonly Queue<string> s_pendingLines = new Queue<string>();

        private static bool s_started;
        private static int s_preferredPort;
        private static int s_port;
        private static bool s_allowPortFallback;
        private static int s_fallbackPortCount;
        private static int s_connectionGeneration;
        private static string s_clientId;
        private static string s_clientInstanceId;
        private static int? s_clientProcessId;
        private static bool s_restartRequested;
        private static readonly ListenerRecoveryState s_listenerRecovery =
            new ListenerRecoveryState();
        private static string s_bridgeId;
        private static string s_bridgeInstanceId;
        private static string s_projectPath;
        private static string s_authToken;

        internal static string BridgeId
        {
            get { return s_bridgeId; }
        }

        internal static int ListenerPort => s_port;
        internal static int ConnectionGeneration => s_connectionGeneration;
        internal static string ConnectedClientId => s_clientId;
        internal static string ConnectedClientInstanceId => s_clientInstanceId;
        internal static string BridgeInstanceId => s_bridgeInstanceId;
        internal static string ProjectPath => s_projectPath;

        // -------------------------------------------------------------------------
        // Boot
        // -------------------------------------------------------------------------

        static UnityMCPServer()
        {
            // Disable gate: env var UNITY_MCP_BRIDGE_DISABLED=1 OR a marker file
            // ProjectSettings/UnityMCPBridge.disabled at the project root makes the
            // bridge skip StartListener + UnityMCPEventBridge.Initialize. Used when
            // multiple Unity instances reference the same UPM package and only one
            // should actually own the TCP port (e.g. PROJECT_EDITOR working + sandbox verify).
            if (IsBridgeDisabled())
            {
                Debug.Log($"{LogPrefix}Bridge disabled (UNITY_MCP_BRIDGE_DISABLED env var or ProjectSettings/UnityMCPBridge.disabled marker); skipping listener + event bridge for this Editor instance.");
                return;
            }

            ListenerPortOptions portOptions;
            try
            {
                portOptions = UnityMCPListenerPortPolicy.Resolve(
                    Environment.GetEnvironmentVariable("UNITY_MCP_BRIDGE_PORT"),
                    Environment.GetEnvironmentVariable("UNITY_MCP_BRIDGE_ALLOW_PORT_FALLBACK"),
                    Environment.GetEnvironmentVariable("UNITY_MCP_BRIDGE_FALLBACK_PORT_COUNT"));
            }
            catch (ArgumentException ex)
            {
                // Fail closed without poisoning the type initializer. A corrected
                // process environment can recover on the next Editor launch.
                Debug.LogError($"{LogPrefix}Invalid listener configuration; " +
                    $"listener was not started: {ex.Message}");
                return;
            }
            s_preferredPort = portOptions.PreferredPort;
            s_port = s_preferredPort;
            s_allowPortFallback = portOptions.AllowFallback;
            s_fallbackPortCount = portOptions.FallbackPortCount;

            s_projectPath = System.IO.Path.GetFullPath(
                System.IO.Path.Combine(Application.dataPath, ".."));
            string configuredBridgeId = Environment.GetEnvironmentVariable("UNITY_MCP_BRIDGE_ID");
            s_bridgeId = string.IsNullOrWhiteSpace(configuredBridgeId)
                ? s_projectPath
                : configuredBridgeId.Trim();
            s_bridgeInstanceId = Guid.NewGuid().ToString("N");
            try
            {
                s_authToken = LoadOrCreateAuthToken();
            }
            catch (Exception ex)
            {
                // Fail closed without poisoning the type initializer. A later domain
                // reload can recover after the token file/configuration is repaired.
                Debug.LogError($"{LogPrefix}Bridge authentication initialization failed; " +
                    $"listener was not started: {ex.Message}");
                return;
            }

            EditorApplication.update += OnEditorUpdate;
            AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReload;

            if (!StartListener())
                ScheduleListenerRetry();
            UnityMCPEventBridge.Initialize();

            // Start the wedge watchdog only when the bridge actually owns the port
            // (IsBridgeDisabled already returned false above). The watchdog runs a
            // background thread that detects a frozen main thread and unblocks the
            // waiting client early; it is meaningless without a live listener.
            UnityMCPWatchdog.Start();
        }

        /// <summary>
        /// Returns true if the bridge should NOT start its listener in this Editor
        /// instance. Two gates, either disables: env var UNITY_MCP_BRIDGE_DISABLED set
        /// to "1" or "true", OR a marker file `ProjectSettings/UnityMCPBridge.disabled`
        /// present at the project root.
        /// </summary>
        private static bool IsBridgeDisabled()
        {
            // AssetImportWorkers and any other batchMode Unity process must never start
            // the TCP listener — they have no editor context and would steal the port from
            // the real editor instance (all share SO_REUSEADDR on the same loopback port).
            if (Application.isBatchMode)
                return true;

            string envDisabled = Environment.GetEnvironmentVariable("UNITY_MCP_BRIDGE_DISABLED");
            if (!string.IsNullOrEmpty(envDisabled) &&
                (envDisabled == "1" || string.Equals(envDisabled, "true", StringComparison.OrdinalIgnoreCase)))
            {
                return true;
            }

            try
            {
                string projectRoot = System.IO.Path.GetFullPath(System.IO.Path.Combine(Application.dataPath, ".."));
                string marker = System.IO.Path.Combine(projectRoot, "ProjectSettings", "UnityMCPBridge.disabled");
                return System.IO.File.Exists(marker);
            }
            catch
            {
                return false;
            }
        }

        // -------------------------------------------------------------------------
        // Listener management
        // -------------------------------------------------------------------------

        private static bool StartListener()
        {
            StopListener();

            ListenerPortOptions options = new ListenerPortOptions(
                s_preferredPort, s_allowPortFallback, s_fallbackPortCount);
            if (UnityMCPListenerFactory.TryStart(
                options, out TcpListener listener, out int selectedPort,
                out List<int> attemptedPorts, out Exception lastError))
            {
                // Listener socket MUST be blocking. Pending() + AcceptTcpClient() at
                // OnEditorUpdate-tick rate is the correct non-busy pattern. Forcing
                // Blocking=false here makes AcceptTcpClient throw "operation not allowed
                // on a non-blocking Socket" every frame.
                s_listener = listener;
                s_port = selectedPort;
                s_started = true;
                ClearWedgeEvidence();
                if (selectedPort == s_preferredPort)
                {
                    Debug.Log($"{LogPrefix}Bridge listening on 127.0.0.1:{s_port}");
                }
                else
                {
                    Debug.LogWarning(
                        $"{LogPrefix}Preferred port {s_preferredPort} is unavailable; " +
                        $"bridge recovered on fallback 127.0.0.1:{s_port}. " +
                        "Use select_bridge/bind_bridge with the reported port.");
                }
                return true;
            }

            s_listener = null;
            s_port = s_preferredPort;
            s_started = false;
            string attempted = attemptedPorts.Count == 0
                ? s_preferredPort.ToString()
                : string.Join(", ", attemptedPorts);
            Debug.LogError(
                $"{LogPrefix}Failed to start listener. Tried ports [{attempted}]: " +
                (lastError?.Message ?? "no candidate ports were available"));
            return false;
        }

        private static void ScheduleListenerRetry()
        {
            s_restartRequested = true;
            double delay = s_listenerRecovery.Schedule(EditorApplication.timeSinceStartup);
            if (s_listenerRecovery.Attempts == 1 || s_listenerRecovery.Attempts % 10 == 0)
            {
                Debug.LogWarning(
                    $"{LogPrefix}Listener recovery attempt {s_listenerRecovery.Attempts} scheduled " +
                    $"in {delay:0.##} seconds.");
            }
        }

        private static void StopListener()
        {
            DisconnectClient();
            CloseCandidateConnections();

            if (s_listener != null)
            {
                try { s_listener.Stop(); } catch { /* ignore */ }
                s_listener = null;
            }

            s_started = false;
        }

        // -------------------------------------------------------------------------
        // Client management
        // -------------------------------------------------------------------------

        private static void DisconnectClient()
        {
            // Abort any in-flight verification work and tear down the hint banner so a
            // dropped client never leaves an orphaned coroutine pump or a stuck banner.
            // Safe to call repeatedly (idempotent) and before the stream is nulled —
            // AbortAll's SendResponse early-returns once s_stream is gone below.
            try { UnityMCPCoroutineDispatcher.AbortAll(ErrorCodes.InvalidState, "client_disconnected"); }
            catch { /* best-effort */ }
            try { VerificationSession.ForceEnd(); }
            catch { /* best-effort */ }

            lock (s_writeLock)
            {
                if (s_stream != null)
                {
                    try { s_stream.Close(); } catch { /* ignore */ }
                    s_stream = null;
                }

                if (s_client != null)
                {
                    try { s_client.Close(); } catch { /* ignore */ }
                    s_client = null;
                }

                s_clientId = null;
                s_clientInstanceId = null;
                s_clientProcessId = null;
                s_streamDecoder.Reset();
            }

            s_lineBuffer.Clear();
            s_pendingLines.Clear();
        }

        // -------------------------------------------------------------------------
        // EditorApplication.update polling
        // -------------------------------------------------------------------------

        private static void OnEditorUpdate()
        {
            if (s_restartRequested &&
                EditorApplication.timeSinceStartup >= s_listenerRecovery.NextRetryAt)
            {
                if (StartListener())
                {
                    s_restartRequested = false;
                    s_listenerRecovery.Reset();
                }
                else
                {
                    ScheduleListenerRetry();
                }
                return;
            }

            if (!s_started || s_listener == null)
                return;

            // Always accept candidates, even while a business client is active. Their
            // first frame decides whether they are out-of-band BridgeControl requests
            // or a Handshake asking to become the business connection.
            AcceptPendingConnections();
            ProcessControlClients();

            if (s_restartRequested)
            {
                s_listenerRecovery.Reset();
                return;
            }

            if (s_client == null)
                return;

            // Verify the current client is still connected.
            if (!IsClientConnected())
            {
                Debug.Log($"{LogPrefix}Client disconnected.");
                DisconnectClient();
                return;
            }

            // Drain available bytes from the stream into our line buffer.
            DrainStream();
        }

        private static void AcceptPendingConnections()
        {
            try
            {
                int accepted = 0;
                while (accepted < MaxRequestsPerEditorUpdate && s_listener.Pending())
                {
                    TcpClient incoming = null;
                    try
                    {
                        incoming = s_listener.AcceptTcpClient();
                        incoming.NoDelay = true;
                        incoming.Client.SetSocketOption(
                            SocketOptionLevel.Socket, SocketOptionName.KeepAlive, true);
                        if (s_candidates.Count >= MaxPendingConnections)
                        {
                            accepted++;
                            continue;
                        }
                        s_candidates.Add(new CandidateConnection
                        {
                            Client = incoming,
                            Stream = incoming.GetStream(),
                            AcceptedAt = EditorApplication.timeSinceStartup,
                        });
                        incoming = null; // candidate list owns it now
                        accepted++;
                    }
                    finally
                    {
                        if (incoming != null)
                        {
                            try { incoming.Close(); } catch { /* best-effort */ }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Debug.LogError($"{LogPrefix}Error accepting client: {ex.Message}");
            }
        }

        private static void ProcessControlClients()
        {
            double now = EditorApplication.timeSinceStartup;
            for (int i = s_candidates.Count - 1; i >= 0; i--)
            {
                CandidateConnection candidate = s_candidates[i];
                if (now - candidate.AcceptedAt > CandidateTimeoutSeconds ||
                    !IsSocketConnected(candidate.Client))
                {
                    CloseCandidateAt(i);
                    continue;
                }
                try
                {
                    if (candidate.Client.Available > 0)
                    {
                        int available = Math.Min(candidate.Client.Available, ReadBufferSize);
                        byte[] bytes = new byte[available];
                        int count = candidate.Stream.Read(bytes, 0, bytes.Length);
                        if (count <= 0)
                        {
                            CloseCandidateAt(i);
                            continue;
                        }
                        candidate.ReceivedBytes += count;
                        if (candidate.ReceivedBytes > MaxCandidateBytes)
                        {
                            SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                                Guid.NewGuid().ToString(), ErrorCodes.InvalidRequest,
                                "First frames exceed the bridge connection limit."));
                            CloseCandidateAt(i);
                            continue;
                        }
                        char[] chars = new char[StrictUtf8.GetMaxCharCount(count)];
                        int charCount = candidate.Decoder.GetChars(
                            bytes, 0, count, chars, 0, false);
                        candidate.Buffer.Append(chars, 0, charCount);
                    }

                    string buffered = candidate.Buffer.ToString();
                    int newline = buffered.IndexOf('\n');
                    if (newline < 0)
                        continue;

                    string firstLine = buffered.Substring(0, newline).TrimEnd('\r');
                    string remainder = buffered.Substring(newline + 1);
                    if (candidate.AuthNonce == null)
                    {
                        if (!HandleCandidateChallenge(candidate, firstLine))
                        {
                            CloseCandidateAt(i);
                            continue;
                        }
                        candidate.Buffer.Clear();
                        candidate.Buffer.Append(remainder);
                        continue;
                    }
                    s_candidates.RemoveAt(i);
                    HandleCandidateFirstFrame(candidate, firstLine, remainder);
                }
                catch (Exception ex)
                {
                    Debug.LogWarning($"{LogPrefix}Candidate connection failed: {ex.Message}");
                    if (i < s_candidates.Count &&
                        object.ReferenceEquals(s_candidates[i], candidate))
                        CloseCandidateAt(i);
                    else
                        CloseCandidate(candidate);
                }
            }
        }

        private static bool HandleCandidateChallenge(
            CandidateConnection candidate, string line)
        {
            try
            {
                RequestEnvelope request = ProtocolSerializer.ParseRequest(line);
                if (string.IsNullOrWhiteSpace(request.Id) ||
                    !Guid.TryParse(request.Id, out _) ||
                    !string.Equals(request.Command, "BridgeChallenge", StringComparison.Ordinal))
                {
                    SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                        string.IsNullOrWhiteSpace(request.Id)
                            ? Guid.NewGuid().ToString()
                            : request.Id,
                        ErrorCodes.InvalidRequest,
                        "The first frame must be BridgeChallenge with a UUID id."));
                    return false;
                }
                string nonce = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
                string proof = ComputeHmacHex(string.Join("\n", new[]
                {
                    "unity-mcp-server-v1", request.Id, nonce, BridgeId, BridgeInstanceId,
                }));
                bool sent = SendCandidateResponse(candidate, ProtocolSerializer.OkResponse(
                    request.Id, new
                    {
                        nonce,
                        challengeRequestId = request.Id,
                        bridgeId = BridgeId,
                        bridgeInstanceId = BridgeInstanceId,
                        serverProof = proof,
                    }));
                if (sent)
                    candidate.AuthNonce = nonce;
                return sent;
            }
            catch (Exception ex)
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    Guid.NewGuid().ToString(), ErrorCodes.InvalidRequest, ex.Message));
                return false;
            }
        }

        private static void HandleCandidateFirstFrame(
            CandidateConnection candidate, string line, string remainder)
        {
            RequestEnvelope request;
            try
            {
                request = ProtocolSerializer.ParseRequest(line);
            }
            catch (Exception ex)
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    Guid.NewGuid().ToString(), ErrorCodes.InvalidRequest, ex.Message));
                CloseCandidate(candidate);
                return;
            }

            if (string.IsNullOrWhiteSpace(request.Id) ||
                !Guid.TryParse(request.Id, out _) ||
                string.IsNullOrWhiteSpace(request.Command))
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    string.IsNullOrWhiteSpace(request.Id)
                        ? Guid.NewGuid().ToString()
                        : request.Id,
                    ErrorCodes.InvalidRequest,
                    "First frame requires a UUID id and non-empty command."));
                CloseCandidate(candidate);
                return;
            }

            if (string.Equals(request.Command, "BridgeControl", StringComparison.Ordinal))
            {
                HandleBridgeControl(candidate, request);
                CloseCandidate(candidate);
                return;
            }

            if (!string.Equals(request.Command, UnityMCPCommands.CommandNames.Handshake,
                StringComparison.Ordinal))
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    request.Id, ErrorCodes.InvalidState,
                    "The first business frame must be Handshake."));
                CloseCandidate(candidate);
                return;
            }

            if (!IsAuthorized(request, candidate.AuthNonce))
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    request.Id, ErrorCodes.InvalidRequest, "Bridge authentication failed."));
                CloseCandidate(candidate);
                return;
            }

            string expectedBridgeId = request.Args?["expectedBridgeId"]?.ToObject<string>();
            string expectedBridgeInstanceId =
                request.Args?["expectedBridgeInstanceId"]?.ToObject<string>();
            int? expectedConnectionGeneration =
                request.Args?["expectedConnectionGeneration"]?.ToObject<int?>();
            bool forceTakeover = request.Args?["forceTakeover"]?.ToObject<bool?>() ?? false;
            if (!string.IsNullOrEmpty(expectedBridgeId) &&
                !string.Equals(expectedBridgeId, BridgeId, StringComparison.Ordinal))
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    request.Id, ErrorCodes.InvalidState,
                    $"Bridge identity mismatch: expected '{expectedBridgeId}', actual '{BridgeId}'."));
                CloseCandidate(candidate);
                return;
            }
            if (forceTakeover &&
                (string.IsNullOrEmpty(expectedBridgeId) ||
                 string.IsNullOrEmpty(expectedBridgeInstanceId) ||
                 !expectedConnectionGeneration.HasValue ||
                 !string.Equals(expectedBridgeInstanceId, BridgeInstanceId,
                     StringComparison.Ordinal) ||
                 expectedConnectionGeneration.Value != s_connectionGeneration))
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    request.Id, ErrorCodes.InvalidState,
                    "Bridge process or connection generation changed; refusing force takeover."));
                CloseCandidate(candidate);
                return;
            }

            // Validate the full protocol handshake before evicting an old client or
            // publishing this candidate into global connection state.
            ResponseEnvelope handshakeResponse = UnityMCPCommands.Dispatch(request);
            if (handshakeResponse == null || !handshakeResponse.Ok)
            {
                SendCandidateResponse(candidate, handshakeResponse ??
                    ProtocolSerializer.ErrorResponse(request.Id, ErrorCodes.Internal,
                        "Handshake returned no response."));
                CloseCandidate(candidate);
                return;
            }

            if (s_client != null && !IsClientConnected())
                DisconnectClient();

            if (s_client != null)
            {
                // Force takeover is deliberately guarded by an explicit bridge identity.
                // An unqualified client can never evict an existing connection.
                string requestedClientId = request.Args?["clientId"]?.ToObject<string>();
                if (!forceTakeover || string.IsNullOrEmpty(expectedBridgeId) ||
                    string.IsNullOrEmpty(expectedBridgeInstanceId) ||
                    !expectedConnectionGeneration.HasValue ||
                    !string.Equals(expectedBridgeInstanceId, BridgeInstanceId,
                        StringComparison.Ordinal) ||
                    expectedConnectionGeneration.Value != s_connectionGeneration ||
                    string.IsNullOrEmpty(requestedClientId) ||
                    !string.Equals(requestedClientId, s_clientId, StringComparison.Ordinal))
                {
                    SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                        request.Id, ErrorCodes.InvalidState,
                        $"Bridge occupied by clientId='{s_clientId ?? "unknown"}', " +
                        $"clientInstanceId='{s_clientInstanceId ?? "unknown"}'."));
                    CloseCandidate(candidate);
                    return;
                }
                DisconnectClient();
            }

            PromoteCandidate(candidate, request, remainder, handshakeResponse);
        }

        private static void PromoteCandidate(
            CandidateConnection candidate,
            RequestEnvelope handshake,
            string remainder,
            ResponseEnvelope handshakeResponse)
        {
            lock (s_writeLock)
            {
                s_client = candidate.Client;
                s_stream = candidate.Stream;
                s_clientId = handshake.Args?["clientId"]?.ToObject<string>();
                s_clientInstanceId = handshake.Args?["clientInstanceId"]?.ToObject<string>();
                s_clientProcessId = handshake.Args?["clientProcessId"]?.ToObject<int?>();
                s_connectionGeneration++;
                s_streamDecoder.Reset();
            }
            ClearWedgeEvidence();
            s_lineBuffer.Clear();
            s_pendingLines.Clear();
            if (!string.IsNullOrEmpty(remainder))
                s_lineBuffer.Append(remainder);

            Debug.Log($"{LogPrefix}Client '{s_clientId ?? "unknown"}' connected from " +
                $"{s_client.Client.RemoteEndPoint} (generation {s_connectionGeneration}).");
            HandshakeResult result = handshakeResponse.Result as HandshakeResult;
            if (result != null)
            {
                result.BridgeId = BridgeId;
                result.BridgeInstanceId = BridgeInstanceId;
                result.ListenerProcessId = System.Diagnostics.Process.GetCurrentProcess().Id;
                result.ListenerPort = ListenerPort;
                result.ConnectionGeneration = ConnectionGeneration;
                result.ClientId = ConnectedClientId;
                result.ClientInstanceId = ConnectedClientInstanceId;
            }
            SendResponse(handshakeResponse);
        }

        private static void HandleBridgeControl(
            CandidateConnection candidate, RequestEnvelope request)
        {
            string action = request.Args?["action"]?.ToObject<string>() ?? "";
            if (!IsAuthorized(request, candidate.AuthNonce))
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    request.Id, ErrorCodes.InvalidRequest, "Bridge authentication failed."));
                return;
            }
            string expectedBridgeId = request.Args?["expectedBridgeId"]?.ToObject<string>();
            string expectedBridgeInstanceId =
                request.Args?["expectedBridgeInstanceId"]?.ToObject<string>();
            string expectedClientId = request.Args?["expectedClientId"]?.ToObject<string>();
            string expectedClientInstanceId =
                request.Args?["expectedClientInstanceId"]?.ToObject<string>();
            int? expectedGeneration =
                request.Args?["expectedConnectionGeneration"]?.ToObject<int?>();
            if (!string.IsNullOrEmpty(expectedBridgeId) &&
                !string.Equals(expectedBridgeId, BridgeId, StringComparison.Ordinal))
            {
                SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                    request.Id, ErrorCodes.InvalidState, "Bridge identity changed; refusing control action."));
                return;
            }

            switch (action)
            {
                case "status":
                    SendCandidateResponse(candidate, ProtocolSerializer.OkResponse(
                        request.Id, BuildControlStatus(action)));
                    return;

                case "disconnect_client":
                    if (string.IsNullOrEmpty(expectedBridgeId) ||
                        string.IsNullOrEmpty(expectedBridgeInstanceId) ||
                        !expectedGeneration.HasValue)
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidRequest,
                            "Bridge instance and connection generation are required."));
                        return;
                    }
                    if (!string.Equals(expectedBridgeInstanceId, BridgeInstanceId,
                            StringComparison.Ordinal) ||
                        expectedGeneration.Value != s_connectionGeneration)
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidState,
                            "Connection generation changed; refusing stale control action."));
                        return;
                    }
                    if (s_client != null &&
                        (string.IsNullOrEmpty(expectedClientId) ||
                         string.IsNullOrEmpty(expectedClientInstanceId)))
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidRequest,
                            "Accepted client ID and instance ID are required."));
                        return;
                    }
                    if (s_client != null &&
                        !string.Equals(expectedClientId, s_clientId, StringComparison.Ordinal))
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidState,
                            "Accepted client identity changed; refusing disconnect."));
                        return;
                    }
                    if (s_client != null &&
                        !string.Equals(expectedClientInstanceId, s_clientInstanceId,
                            StringComparison.Ordinal))
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidState,
                            "Accepted client instance changed; refusing stale control action."));
                        return;
                    }
                    SendCandidateResponse(candidate, ProtocolSerializer.OkResponse(
                        request.Id, new
                        {
                            action = "disconnect_client",
                            disconnectedClientId = s_clientId,
                            previousGeneration = s_connectionGeneration,
                            alreadyClear = s_client == null,
                        }));
                    DisconnectClient();
                    return;

                case "restart_listener":
                    if (string.IsNullOrEmpty(expectedBridgeId) ||
                        string.IsNullOrEmpty(expectedBridgeInstanceId) ||
                        !expectedGeneration.HasValue)
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidRequest,
                            "Bridge instance and connection generation are required to restart the listener."));
                        return;
                    }
                    if (!string.Equals(expectedBridgeInstanceId, BridgeInstanceId,
                            StringComparison.Ordinal) ||
                        expectedGeneration.Value != s_connectionGeneration)
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidState,
                            "Bridge process or connection generation changed; refusing stale listener restart."));
                        return;
                    }
                    if (s_client != null &&
                        (string.IsNullOrEmpty(expectedClientId) ||
                         string.IsNullOrEmpty(expectedClientInstanceId) ||
                         !string.Equals(expectedClientId, s_clientId, StringComparison.Ordinal) ||
                         !string.Equals(expectedClientInstanceId, s_clientInstanceId,
                             StringComparison.Ordinal)))
                    {
                        SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                            request.Id, ErrorCodes.InvalidState,
                            "Accepted client changed; refusing stale listener restart."));
                        return;
                    }
                    if (!SendCandidateResponse(candidate, ProtocolSerializer.OkResponse(
                        request.Id, new
                        {
                            action,
                            bridgeId = BridgeId,
                            bridgeInstanceId = BridgeInstanceId,
                            port = s_port,
                            restartScheduled = true,
                        })))
                        return;
                    // Restart after the ACK has been written and this candidate closes.
                    s_restartRequested = true;
                    return;

                default:
                    SendCandidateResponse(candidate, ProtocolSerializer.ErrorResponse(
                        request.Id, ErrorCodes.InvalidRequest,
                        $"Unknown BridgeControl action '{action}'."));
                    return;
            }
        }

        private static object BuildControlStatus(string action)
        {
            UnityMCPCommands.InFlightSnapshot inFlight = UnityMCPCommands.InFlight.Snapshot();
            double stalledMs = inFlight.Active
                ? Math.Max(0, UnityMCPCommands.InFlight.NowMs - inFlight.StartMs)
                : 0;
            return new
            {
                action,
                bridgeId = BridgeId,
                bridgeInstanceId = BridgeInstanceId,
                listenerPid = System.Diagnostics.Process.GetCurrentProcess().Id,
                port = s_port,
                projectPath = ProjectPath,
                clientConnected = s_client != null && IsClientConnected(),
                clientId = s_clientId,
                clientInstanceId = s_clientInstanceId,
                clientProcessId = s_clientProcessId,
                connectionGeneration = s_connectionGeneration,
                wedgeActive = inFlight.Active && stalledMs > 5000,
                inFlightCommand = inFlight.Command,
                inFlightRequestId = inFlight.RequestId,
                stalledMs,
            };
        }

        private static readonly string[] AuthProofFields =
        {
            "action", "clientProtocolVersion", "clientId", "clientInstanceId",
            "clientProcessId", "expectedBridgeId", "forceTakeover",
            "expectedBridgeInstanceId", "expectedConnectionGeneration",
            "expectedClientId", "expectedClientInstanceId",
        };

        private static bool IsAuthorized(RequestEnvelope request, string nonce)
        {
            string suppliedProof = request.Args?["authProof"]?.ToObject<string>();
            if (string.IsNullOrEmpty(s_authToken) || string.IsNullOrEmpty(nonce) ||
                string.IsNullOrEmpty(suppliedProof))
                return false;
            var parts = new List<string>
            {
                "unity-mcp-client-v1", nonce, request.Id ?? "", request.Command ?? "",
            };
            foreach (string field in AuthProofFields)
                parts.Add(field + "=" + NormalizeProofValue(request.Args?[field]));
            return FixedTimeEquals(ComputeHmacHex(string.Join("\n", parts)), suppliedProof);
        }

        private static string NormalizeProofValue(Newtonsoft.Json.Linq.JToken token)
        {
            if (token == null || token.Type == Newtonsoft.Json.Linq.JTokenType.Null)
                return "";
            if (token.Type == Newtonsoft.Json.Linq.JTokenType.Boolean)
                return token.ToObject<bool>() ? "true" : "false";
            if (token.Type == Newtonsoft.Json.Linq.JTokenType.String)
                return token.ToObject<string>() ?? "";
            return token.ToString(Newtonsoft.Json.Formatting.None);
        }

        private static string ComputeHmacHex(string payload)
        {
            using (var hmac = new System.Security.Cryptography.HMACSHA256(
                Encoding.UTF8.GetBytes(s_authToken)))
            {
                byte[] digest = hmac.ComputeHash(Encoding.UTF8.GetBytes(payload));
                return BitConverter.ToString(digest).Replace("-", "").ToLowerInvariant();
            }
        }

        private static bool FixedTimeEquals(string expected, string supplied)
        {
            if (expected == null || supplied == null || expected.Length != supplied.Length)
                return false;
            int difference = 0;
            for (int i = 0; i < expected.Length; i++)
                difference |= expected[i] ^ supplied[i];
            return difference == 0;
        }

        private static string LoadOrCreateAuthToken()
        {
            string configured = Environment.GetEnvironmentVariable("UNITY_MCP_AUTH_TOKEN");
            if (!string.IsNullOrEmpty(configured))
            {
                if (configured.Length < 32)
                    throw new InvalidOperationException(
                        "UNITY_MCP_AUTH_TOKEN must contain at least 32 characters.");
                return configured;
            }

            string directory = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".unity-mcp");
            string path = System.IO.Path.Combine(directory, "auth-token");
            System.IO.Directory.CreateDirectory(directory);
            if (System.IO.File.Exists(path))
                return ReadAuthTokenWithRetry(path);

            string token = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
            try
            {
                using (var stream = new System.IO.FileStream(
                    path, System.IO.FileMode.CreateNew, System.IO.FileAccess.Write,
                    System.IO.FileShare.None))
                using (var writer = new System.IO.StreamWriter(stream, Encoding.ASCII))
                {
                    writer.WriteLine(token);
                    writer.Flush();
                    stream.Flush(true);
                }
                return token;
            }
            catch (System.IO.IOException)
            {
                // Another Unity/MCP process won the CreateNew race; share its token.
                return ReadAuthTokenWithRetry(path);
            }
        }

        private static string ReadAuthTokenWithRetry(string path)
        {
            Exception lastError = null;
            for (int attempt = 0; attempt < 20; attempt++)
            {
                try
                {
                    return ValidateAuthToken(
                        System.IO.File.ReadAllText(path, Encoding.ASCII).Trim(), path);
                }
                catch (Exception ex) when (
                    ex is System.IO.IOException || ex is InvalidOperationException)
                {
                    lastError = ex;
                    System.Threading.Thread.Sleep(25);
                }
            }
            throw new InvalidOperationException(
                $"Unable to read a complete UnityMCP auth token file: {path}", lastError);
        }

        private static string ValidateAuthToken(string token, string path)
        {
            if (string.IsNullOrEmpty(token) || token.Length < 32)
                throw new InvalidOperationException($"Invalid UnityMCP auth token file: {path}");
            return token;
        }

        private static string WedgeEvidencePath
        {
            get
            {
                return System.IO.Path.Combine(System.IO.Path.GetTempPath(), "unity-mcp",
                    $"bridge-{s_port}-wedge.json");
            }
        }

        internal static void WriteWedgeEvidence(
            string requestId, string command, double stalledMs,
            int expectedGeneration)
        {
            try
            {
                lock (s_writeLock)
                {
                    if (s_connectionGeneration != expectedGeneration)
                        return;
                    string path = WedgeEvidencePath;
                    System.IO.Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path));
                    string timestamp = DateTime.UtcNow.ToString("o");
                    string proof = ComputeHmacHex(string.Join("\n", new[]
                    {
                        "unity-mcp-wedge-v1", BridgeId, BridgeInstanceId,
                        System.Diagnostics.Process.GetCurrentProcess().Id.ToString(),
                        s_port.ToString(), expectedGeneration.ToString(),
                        requestId ?? "", command ?? "", timestamp,
                    }));
                    string json = Newtonsoft.Json.JsonConvert.SerializeObject(new
                    {
                        bridgeId = BridgeId,
                        bridgeInstanceId = BridgeInstanceId,
                        listenerPid = System.Diagnostics.Process.GetCurrentProcess().Id,
                        port = s_port,
                        connectionGeneration = expectedGeneration,
                        requestId,
                        command,
                        stalledMs,
                        timestamp,
                        proof,
                    });
                    System.IO.File.WriteAllText(path, json, Encoding.UTF8);
                }
            }
            catch { /* best-effort background-thread evidence */ }
        }

        internal static void ClearWedgeEvidence()
        {
            try
            {
                string path = WedgeEvidencePath;
                if (System.IO.File.Exists(path))
                    System.IO.File.Delete(path);
            }
            catch { /* best-effort */ }
        }

        private static bool SendCandidateResponse(
            CandidateConnection candidate, ResponseEnvelope response)
        {
            try
            {
                byte[] bytes = Encoding.UTF8.GetBytes(
                    ProtocolSerializer.SerializeResponse(response) + "\n");
                candidate.Stream.Write(bytes, 0, bytes.Length);
                candidate.Stream.Flush();
                return true;
            }
            catch { return false; }
        }

        private static bool IsSocketConnected(TcpClient client)
        {
            if (client == null || !client.Connected)
                return false;
            try
            {
                return !(client.Client.Poll(0, SelectMode.SelectRead) && client.Available == 0);
            }
            catch
            {
                return false;
            }
        }

        private static void CloseCandidateAt(int index)
        {
            CandidateConnection candidate = s_candidates[index];
            s_candidates.RemoveAt(index);
            CloseCandidate(candidate);
        }

        private static void CloseCandidate(CandidateConnection candidate)
        {
            try { candidate.Stream?.Close(); } catch { /* ignore */ }
            try { candidate.Client?.Close(); } catch { /* ignore */ }
        }

        private static void CloseCandidateConnections()
        {
            foreach (CandidateConnection candidate in s_candidates)
                CloseCandidate(candidate);
            s_candidates.Clear();
        }

        private static bool IsClientConnected()
        {
            if (s_client == null || !s_client.Connected)
                return false;

            try
            {
                bool hasData = s_client.Client.Poll(0, SelectMode.SelectRead);
                if (!hasData)
                    return true;

                if (s_client.Available == 0)
                    return false;

                return true;
            }
            catch
            {
                return false;
            }
        }

        private static readonly byte[] s_readBuf = new byte[ReadBufferSize];
        private static readonly Decoder s_streamDecoder = StrictUtf8.GetDecoder();

        private static void DrainStream()
        {
            if (s_stream == null)
                return;

            try
            {
                while (s_client != null && s_client.Available > 0)
                {
                    int count = s_stream.Read(s_readBuf, 0, s_readBuf.Length);
                    if (count == 0)
                        break;

                    char[] chars = new char[StrictUtf8.GetMaxCharCount(count)];
                    int charCount = s_streamDecoder.GetChars(
                        s_readBuf, 0, count, chars, 0, false);
                    s_lineBuffer.Append(chars, 0, charCount);
                }

                QueueBufferedLines();
                ProcessPendingLines();
            }
            catch (Exception ex)
            {
                Debug.LogError($"{LogPrefix}Stream read error: {ex.Message}");
                DisconnectClient();
            }
        }

        private static void QueueBufferedLines()
        {
            string buffered = s_lineBuffer.ToString();
            int newlineIdx;

            while ((newlineIdx = buffered.IndexOf('\n')) >= 0)
            {
                string line = buffered.Substring(0, newlineIdx).TrimEnd('\r');
                buffered = buffered.Substring(newlineIdx + 1);

                if (!string.IsNullOrWhiteSpace(line))
                    s_pendingLines.Enqueue(line);
            }

            s_lineBuffer.Clear();
            s_lineBuffer.Append(buffered);
        }

        private static void ProcessPendingLines()
        {
            Stopwatch stopwatch = Stopwatch.StartNew();
            int processed = 0;
            while (s_pendingLines.Count > 0 && processed < MaxRequestsPerEditorUpdate)
            {
                HandleLine(s_pendingLines.Dequeue());
                processed++;
                if (stopwatch.ElapsedMilliseconds >= MaxRequestProcessingMillisPerUpdate)
                    break;
            }
        }

        private static void HandleLine(string line)
        {
            ResponseEnvelope response;

            try
            {
                RequestEnvelope request = ProtocolSerializer.ParseRequest(line);

                if (string.IsNullOrEmpty(request?.Id) || string.IsNullOrEmpty(request.Command))
                {
                    string correlId = request?.Id ?? Guid.NewGuid().ToString();
                    response = ProtocolSerializer.ErrorResponse(
                        correlId,
                        ErrorCodes.InvalidRequest,
                        "Request must include 'id' (UUID) and 'command'.");
                }
                else
                {
                    response = UnityMCPCommands.Dispatch(request);
                }
            }
            catch (Exception ex)
            {
                response = ProtocolSerializer.ErrorResponse(
                    Guid.NewGuid().ToString(),
                    ErrorCodes.InvalidRequest,
                    $"Failed to parse request: {ex.Message}");
            }

            // null means the handler will send the response asynchronously (e.g. screenshot deferral).
            if (response != null)
                SendResponse(response);
        }

        internal static void SendResponse(ResponseEnvelope response)
        {
            if (s_stream == null)
                return;

            string json;
            try
            {
                json = ProtocolSerializer.SerializeResponse(response) + "\n";
            }
            catch (Exception ex)
            {
                string id = string.IsNullOrEmpty(response?.Id) ? Guid.NewGuid().ToString() : response.Id;
                Debug.LogError($"{LogPrefix}Response serialization failed for {id}: {ex.Message}");
                var fallback = ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                    $"Response serialization failed: {ex.Message}");
                json = ProtocolSerializer.SerializeResponse(fallback) + "\n";
            }

            try
            {
                byte[] bytes = Encoding.UTF8.GetBytes(json);
                lock (s_writeLock)
                {
                    s_stream.Write(bytes, 0, bytes.Length);
                }
            }
            catch (Exception ex)
            {
                Debug.LogError($"{LogPrefix}Failed to send response: {ex.Message}");
                DisconnectClient();
            }
        }

        /// <summary>
        /// Best-effort early-unblock of a client waiting on a request whose handler
        /// has wedged the main thread. Called from the watchdog's BACKGROUND thread,
        /// so it must touch only thread-safe state: it takes s_writeLock (never a
        /// main-thread-only API) and writes one ERROR frame correlated to
        /// <paramref name="requestId"/>. Returns true if a frame was written.
        ///
        /// If the main thread later unwedges and sends its own response for the same
        /// id, the client has already moved on and ignores the duplicate (correlation
        /// is by id). We intentionally do NOT DisconnectClient on failure here — the
        /// connection may still be healthy and the main thread merely busy.
        /// </summary>
        internal static bool TrySendWedgeAlert(
            string requestId, string command, double stalledMs, int expectedGeneration)
        {
            string json;
            try
            {
                var env = ProtocolSerializer.ErrorResponse(
                    string.IsNullOrEmpty(requestId) ? Guid.NewGuid().ToString() : requestId,
                    ErrorCodes.EditorBusy,
                    $"editor_main_thread_wedged: command '{command}' has run >{stalledMs:F0}ms " +
                    "on the Editor main thread and is not responding. The MCP call is being " +
                    "unblocked early; Unity may still be frozen (manual restart may be required).");
                json = ProtocolSerializer.SerializeResponse(env) + "\n";
            }
            catch
            {
                return false; // serialization failure — nothing safe to send
            }

            try
            {
                byte[] bytes = Encoding.UTF8.GetBytes(json);
                lock (s_writeLock)
                {
                    if (s_stream == null || s_connectionGeneration != expectedGeneration)
                        return false;
                    s_stream.Write(bytes, 0, bytes.Length);
                }
                return true;
            }
            catch
            {
                return false; // best-effort; do not disconnect from the watchdog thread
            }
        }

        /// <summary>
        /// Send an unsolicited notification to the connected client.
        /// Fire-and-forget; no correlation id. Early-returns if no client connected.
        /// Called by UnityMCPEventBridge to push Unity Editor events.
        /// </summary>
        public static void SendNotification(string eventName, object payload)
        {
            if (s_stream == null)
                return;

            try
            {
                NotificationEnvelope env = ProtocolSerializer.BuildNotification(eventName, payload);
                string json = ProtocolSerializer.SerializeNotification(env) + "\n";
                byte[] bytes = Encoding.UTF8.GetBytes(json);
                lock (s_writeLock)
                {
                    s_stream.Write(bytes, 0, bytes.Length);
                }
            }
            catch (Exception ex)
            {
                Debug.LogError($"{LogPrefix}Failed to send notification '{eventName}': {ex.Message}");
                DisconnectClient();
            }
        }

        // -------------------------------------------------------------------------
        // Assembly reload cleanup
        // -------------------------------------------------------------------------

        private static void OnBeforeAssemblyReload()
        {
            Debug.Log($"{LogPrefix}Domain reload — stopping bridge.");
            EditorApplication.update -= OnEditorUpdate;
            AssemblyReloadEvents.beforeAssemblyReload -= OnBeforeAssemblyReload;
            // Background threads do NOT die on domain reload — stop the watchdog
            // explicitly or it would leak a thread referencing stale statics.
            UnityMCPWatchdog.Stop();
            UnityMCPEventBridge.Teardown();
            StopListener();
        }

        // -------------------------------------------------------------------------
        // Menu item
        // -------------------------------------------------------------------------

        [MenuItem("Tools/UnityMCP/Restart Bridge")]
        private static void RestartBridgeMenu()
        {
            Debug.Log($"{LogPrefix}Manual restart requested.");
            s_listenerRecovery.Reset();
            if (StartListener())
            {
                s_restartRequested = false;
            }
            else
            {
                ScheduleListenerRetry();
            }
        }
    }
}
