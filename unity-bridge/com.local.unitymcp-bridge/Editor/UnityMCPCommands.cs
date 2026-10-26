// UnityMCPCommands.cs
// Dispatch table + 13 command handlers for the UnityMCP Editor bridge.
//
// Architecture:
//   - Dispatch(env) is a switch on env.Command -> Handle<Cmd>(env).
//   - Each Handle<Cmd> resolves Unity-runtime delegates and forwards to a matching
//     Execute<Cmd>(...) overload that takes those delegates explicitly. Execute*
//     is the unit-test surface — same code path runs in both Unity and the
//     standalone test process, but tests inject deterministic fakes.
//   - Phase 0: 8 commands.
//   - Phase 1: TailEditorLog soft-deprecated (warn-once + [Obsolete]).
//   - Phase 2: 5 new commands with delegate-injected Execute* signatures that
//     return ResponseEnvelope directly so all validation lives in Execute*.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using Unity.Profiling;
using UnityEngine.SceneManagement;
using UnityMCP.Bridge.Protocol;

namespace UnityMCP.Bridge
{
    public static class UnityMCPCommands
    {
        // -------------------------------------------------------------------------
        // Command name constants
        // -------------------------------------------------------------------------

        public static class CommandNames
        {
            // Phase 0
            public const string Handshake         = "Handshake";
            public const string EnterPlaymode     = "EnterPlaymode";
            public const string ExitPlaymode      = "ExitPlaymode";
            public const string RunEditMethod     = "RunEditMethod";
            public const string GetSceneState     = "GetSceneState";
            public const string CaptureScreenshot = "CaptureScreenshot";
            public const string TailEditorLog     = "TailEditorLog";   // deprecated
            public const string QueryGameObject   = "QueryGameObject";

            // Phase 2
            public const string OpenScene        = "OpenScene";
            public const string RefreshAssets    = "RefreshAssets";
            public const string PingObject       = "PingObject";
            public const string SetGameViewSize  = "SetGameViewSize";
            public const string WaitForIdle      = "WaitForIdle";

            // Phase 3 (protocol 1.3.0): hot-reload + state queries
            public const string LuaReloadModule     = "LuaReloadModule";
            public const string CsharpReloadScripts = "CsharpReloadScripts";
            public const string GetPlayState        = "GetPlayState";
            public const string GetCompileStatus    = "GetCompileStatus";

            // Phase 4 (protocol 1.4.0): no-human UI verification layer
            public const string LuaEval                  = "LuaEval";
            public const string StartVerificationSession = "StartVerificationSession";
            public const string EndVerificationSession   = "EndVerificationSession";

            // Phase 5 (protocol 1.5.0): UI node inventory + localization
            public const string ListUINodes              = "ListUINodes";
            public const string ResolveLocalization      = "ResolveLocalization";

            // Phase 6 (protocol 1.6.0): open panel by uid with async verify
            public const string OpenPanel                = "OpenPanel";

            // Phase 7 (protocol 1.7.0): simulate suite (main path, all via DoString)
            public const string SimulateClick            = "SimulateClick";
            public const string SimulateKey              = "SimulateKey";
            public const string SetInputText             = "SetInputText";

            // Phase 8 (protocol 1.8.0): non-UI observe/operate (camera/entity/effect)
            public const string GetVerificationSessionState = "GetVerificationSessionState";
            public const string ObserveCamera            = "ObserveCamera";
            public const string ListEntities             = "ListEntities";
            public const string ObserveEntity            = "ObserveEntity";
            public const string ListEffects              = "ListEffects";
            public const string ObserveEffect            = "ObserveEffect";
            public const string ObserveWindow            = "ObserveWindow";
            public const string OperateCamera            = "OperateCamera";

            // Phase 9 (protocol 1.9.0): optional performance profiler
            public const string ProfilerCapabilities = "ProfilerCapabilities";
            public const string ProfilerSample       = "ProfilerSample";
            public const string MemorySnapshot       = "MemorySnapshot";
        }

        // Phase 2 MED-1: SessionState key prefix that survives domain reload so the
        // post-reload compile.done event can be correlated back to the request.
        private const string RefreshAssetsMarkerPrefix = "UnityMCPBridge.RefreshAssetsRequest.";

        // WaitForIdle hard upper bound (seconds). Anything above is INVALID_REQUEST.
        private const double WaitForIdleMaxSeconds = 120.0;

        // -------------------------------------------------------------------------
        // Internal state
        // -------------------------------------------------------------------------

        private static bool s_tailDeprecationWarned;

        private static readonly LinkedList<LogLine> s_logRingBuffer = new LinkedList<LogLine>();
        private const int LogRingCap = 5000;
        private const int MaxTailEditorLogLines = 500;
        private static int s_logLineCounter;
        private static readonly object s_logLock = new object();

        internal static void PushLogLine(LogLine line)
        {
            lock (s_logLock)
            {
                s_logLineCounter += 1;
                line.LineNumber = s_logLineCounter;
                s_logRingBuffer.AddLast(line);
                while (s_logRingBuffer.Count > LogRingCap)
                    s_logRingBuffer.RemoveFirst();
            }
        }

        internal static int CurrentLogLineCounter()
        {
            lock (s_logLock) { return s_logLineCounter; }
        }

        // -------------------------------------------------------------------------
        // In-flight synchronous dispatch tracking (for the wedge watchdog)
        //
        // Every command runs synchronously on the Editor main thread inside
        // Dispatch's switch. If a handler runs unbounded code (e.g. LuaEval with a
        // no-exit loop), the main thread wedges and EditorApplication.update stops
        // firing — so only a BACKGROUND thread can notice. UnityMCPWatchdog polls
        // InFlight from that thread; we expose a thread-safe snapshot here.
        //
        // The clock is a process Stopwatch (monotonic, thread-safe to Read) rather
        // than EditorApplication.timeSinceStartup, which is main-thread-only and
        // would itself be frozen during a wedge.
        // -------------------------------------------------------------------------

        internal struct InFlightSnapshot
        {
            public bool Active;
            public string RequestId;
            public string Command;
            public double StartMs;
            public int ConnectionGeneration;
        }

        internal static class InFlight
        {
            private static readonly object s_lock = new object();
            private static readonly System.Diagnostics.Stopwatch s_clock =
                System.Diagnostics.Stopwatch.StartNew();
            private static bool s_active;
            private static string s_requestId;
            private static string s_command;
            private static double s_startMs;
            private static int s_connectionGeneration;

            /// <summary>Monotonic milliseconds since process start. Thread-safe.</summary>
            internal static double NowMs => s_clock.Elapsed.TotalMilliseconds;

            internal static void Begin(string requestId, string command)
            {
                lock (s_lock)
                {
                    s_active = true;
                    s_requestId = requestId;
                    s_command = command;
                    s_startMs = s_clock.Elapsed.TotalMilliseconds;
                    s_connectionGeneration = UnityMCPServer.ConnectionGeneration;
                }
            }

            internal static void End()
            {
                lock (s_lock)
                {
                    s_active = false;
                    s_requestId = null;
                    s_command = null;
                }
            }

            internal static InFlightSnapshot Snapshot()
            {
                lock (s_lock)
                {
                    return new InFlightSnapshot
                    {
                        Active = s_active,
                        RequestId = s_requestId,
                        Command = s_command,
                        StartMs = s_startMs,
                        ConnectionGeneration = s_connectionGeneration,
                    };
                }
            }
        }

        // -------------------------------------------------------------------------
        // Dispatch
        // -------------------------------------------------------------------------

        public static ResponseEnvelope Dispatch(RequestEnvelope env)
        {
            // Mark this command as the in-flight synchronous dispatch so the
            // background watchdog can detect a wedged main thread. Async-pump tools
            // (OpenPanel/ObserveWindow/WaitForIdle) return null from their handler
            // almost immediately, so their sync window is ~0 and End() clears them
            // before their cooperative wait begins — no false wedge alert.
            InFlight.Begin(env?.Id, env?.Command);
            try
            {
                switch (env.Command)
                {
                    case CommandNames.Handshake:         return HandleHandshake(env);
                    case CommandNames.EnterPlaymode:     return HandleEnterPlaymode(env);
                    case CommandNames.ExitPlaymode:      return HandleExitPlaymode(env);
                    case CommandNames.RunEditMethod:     return HandleRunEditMethod(env);
                    case CommandNames.GetSceneState:     return HandleGetSceneState(env);
                    case CommandNames.CaptureScreenshot: return HandleCaptureScreenshot(env);
                    case CommandNames.TailEditorLog:
#pragma warning disable CS0618 // intentional internal call into deprecated handler
                        return HandleTailEditorLog(env);
#pragma warning restore CS0618
                    case CommandNames.QueryGameObject:   return HandleQueryGameObject(env);

                    case CommandNames.OpenScene:         return HandleOpenScene(env);
                    case CommandNames.RefreshAssets:     return HandleRefreshAssets(env);
                    case CommandNames.PingObject:        return HandlePingObject(env);
                    case CommandNames.SetGameViewSize:   return HandleSetGameViewSize(env);
                    case CommandNames.WaitForIdle:       return HandleWaitForIdle(env);

                    case CommandNames.LuaReloadModule:     return HandleLuaReloadModule(env);
                    case CommandNames.CsharpReloadScripts: return HandleCsharpReloadScripts(env);
                    case CommandNames.GetPlayState:        return HandleGetPlayState(env);
                    case CommandNames.GetCompileStatus:    return HandleGetCompileStatus(env);

                    case CommandNames.LuaEval:                  return HandleLuaEval(env);
                    case CommandNames.StartVerificationSession: return HandleStartVerificationSession(env);
                    case CommandNames.EndVerificationSession:   return HandleEndVerificationSession(env);

                    case CommandNames.ListUINodes:              return HandleListUINodes(env);
                    case CommandNames.ResolveLocalization:      return HandleResolveLocalization(env);

                    case CommandNames.OpenPanel:                return HandleOpenPanel(env);

                    case CommandNames.SimulateClick:            return HandleSimulateClick(env);
                    case CommandNames.SimulateKey:              return HandleSimulateKey(env);
                    case CommandNames.SetInputText:             return HandleSetInputText(env);

                    case CommandNames.GetVerificationSessionState: return HandleGetVerificationSessionState(env);
                    case CommandNames.ObserveCamera:            return HandleObserveCamera(env);
                    case CommandNames.ListEntities:             return HandleListEntities(env);
                    case CommandNames.ObserveEntity:            return HandleObserveEntity(env);
                    case CommandNames.ListEffects:              return HandleListEffects(env);
                    case CommandNames.ObserveEffect:            return HandleObserveEffect(env);
                    case CommandNames.ObserveWindow:            return HandleObserveWindow(env);
                    case CommandNames.OperateCamera:            return HandleOperateCamera(env);
                    case CommandNames.ProfilerCapabilities: return HandleProfilerCapabilities(env);
                    case CommandNames.ProfilerSample:       return HandleProfilerSample(env);
                    case CommandNames.MemorySnapshot:       return HandleMemorySnapshot(env);

                    default:
                        return ProtocolSerializer.ErrorResponse(
                            env.Id, ErrorCodes.InvalidRequest,
                            $"Unknown command: '{env.Command}'.");
                }
            }
            catch (Exception ex)
            {
                Debug.LogError($"[UnityMCP] Unhandled exception in {env.Command}: {ex}");
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal, ex.Message);
            }
            finally
            {
                // Clear the in-flight marker whether the handler returned a response
                // (sync tool) or null (async-pump tool). Either way the SYNCHRONOUS
                // portion is done; the watchdog should no longer count this request.
                InFlight.End();
            }
        }

        // -------------------------------------------------------------------------
        // Phase 0: Handshake
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleHandshake(RequestEnvelope env)
        {
            HandshakeArgs args = ProtocolSerializer.DeserializeArgs<HandshakeArgs>(env);
            if (args == null || string.IsNullOrEmpty(args.ClientProtocolVersion))
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "Handshake requires clientProtocolVersion.");
            }

            string serverVer = ProtocolConstants.ProtocolVersion;
            if (!string.Equals(serverVer, args.ClientProtocolVersion, StringComparison.Ordinal))
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.ProtocolVersionMismatch,
                    $"Server protocol version '{serverVer}' does not match client '{args.ClientProtocolVersion}'.");
            }

            HandshakeResult result = ExecuteHandshake(serverVer,
                () => Application.unityVersion,
                () => System.IO.Path.GetFullPath(System.IO.Path.Combine(Application.dataPath, "..")));
            result.BridgeId = UnityMCPServer.BridgeId;
            result.BridgeInstanceId = UnityMCPServer.BridgeInstanceId;
            result.ListenerProcessId = System.Diagnostics.Process.GetCurrentProcess().Id;
            result.ListenerPort = UnityMCPServer.ListenerPort;
            result.ConnectionGeneration = UnityMCPServer.ConnectionGeneration;
            result.ClientId = UnityMCPServer.ConnectedClientId;
            result.ClientInstanceId = UnityMCPServer.ConnectedClientInstanceId;
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        public static HandshakeResult ExecuteHandshake(
            string serverVersion,
            Func<string> getEditorVersion,
            Func<string> getProjectPath)
        {
            return new HandshakeResult
            {
                ServerProtocolVersion = serverVersion,
                EditorVersion = getEditorVersion(),
                ProjectPath = getProjectPath(),
            };
        }

        // -------------------------------------------------------------------------
        // Phase 0: EnterPlaymode / ExitPlaymode
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleEnterPlaymode(RequestEnvelope env)
        {
            if (EditorApplication.isPlaying)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                    "Editor is already in playmode.");
            }
            PlaymodeResult result = ExecutePlaymode(true, val => EditorApplication.isPlaying = val);
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        private static ResponseEnvelope HandleExitPlaymode(RequestEnvelope env)
        {
            if (!EditorApplication.isPlaying)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                    "Editor is not in playmode.");
            }
            PlaymodeResult result = ExecutePlaymode(false, val => EditorApplication.isPlaying = val);
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        public static PlaymodeResult ExecutePlaymode(bool enter, Action<bool> setPlaymode)
        {
            setPlaymode(enter);
            return new PlaymodeResult { State = enter ? "entering" : "exiting" };
        }

        // -------------------------------------------------------------------------
        // Phase 0: RunEditMethod
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleRunEditMethod(RequestEnvelope env)
        {
            RunEditMethodArgs args = ProtocolSerializer.DeserializeArgs<RunEditMethodArgs>(env);
            if (args == null || string.IsNullOrEmpty(args.TypeName) || string.IsNullOrEmpty(args.MethodName))
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "RunEditMethod requires typeName and methodName.");
            }

            try
            {
                var capturedLogs = new List<string>();
                Application.LogCallback handler = (msg, stack, type) =>
                    capturedLogs.Add($"[{type}] {msg}");

                Application.logMessageReceived += handler;
                try
                {
                    object ret = ExecuteRunEditMethod(args.TypeName, args.MethodName, args.Args ?? Array.Empty<object>());
                    return ProtocolSerializer.OkResponse(env.Id, new RunEditMethodResult
                    {
                        ReturnValue = NormalizeReturnValueForJson(ret),
                        CapturedLogs = capturedLogs,
                    });
                }
                finally
                {
                    Application.logMessageReceived -= handler;
                }
            }
            catch (TargetInvocationException tie)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    tie.InnerException?.Message ?? tie.Message);
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest, ex.Message);
            }
        }

        public static object ExecuteRunEditMethod(string typeName, string methodName, object[] args)
        {
            ValidateRunEditMethodScope(typeName, methodName);

            Type t = null;
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                t = asm.GetType(typeName, throwOnError: false);
                if (t != null) break;
            }
            if (t == null)
                throw new InvalidOperationException($"Type not found: {typeName}");

            // Security HIGH fix: NonPublic removed. Only public static methods may
            // be invoked over the bridge. Private/internal methods must not be
            // callable from an LLM-driven MCP client — they are not part of the
            // published contract and typically assume caller trust.
            MethodInfo mi = t.GetMethod(methodName,
                BindingFlags.Public | BindingFlags.Static | BindingFlags.FlattenHierarchy);
            if (mi == null)
                throw new InvalidOperationException($"Public static method not found: {typeName}.{methodName}");

            return mi.Invoke(null, args);
        }

        // Security HIGH fix (2026-05-13): hard denylist of namespaces + types that
        // grant process-level capability if reflected into. This is NOT the complete
        // attack surface — a determined attacker with arbitrary-method-invocation
        // primitive can still do damage through project code — but it eliminates
        // the obvious trivial wins (spawning processes, file deletion, registry
        // access, arbitrary reflection on MethodInfo.Invoke itself).
        //
        // Allowlist: callers may restrict further via env var UNITY_MCP_RUN_METHOD_ALLOW
        // (comma-separated type-name prefixes). If set, any type whose full name
        // does not start with one of the prefixes is rejected. Default (unset) is
        // permissive modulo the denylist, matching the pre-fix behavior minus the
        // obvious CBRN-equivalent classes.
        private static readonly string[] s_runEditMethodDenyPrefixes = new[]
        {
            "System.Diagnostics.Process",
            "System.IO.File",
            "System.IO.Directory",
            "System.IO.Path",            // path traversal helpers
            "System.Net.",               // socket/webrequest primitives
            "System.Reflection.",        // prevent reflect-to-escape
            "System.Runtime.InteropServices.",  // DllImport, Marshal.Copy, etc.
            "Microsoft.Win32.Registry",
            "System.Environment",        // Environment.Exit, .SetEnvironmentVariable
            "System.AppDomain",
        };

        private static void ValidateRunEditMethodScope(string typeName, string methodName)
        {
            if (string.IsNullOrWhiteSpace(typeName))
                throw new InvalidOperationException("typeName required");
            if (string.IsNullOrWhiteSpace(methodName))
                throw new InvalidOperationException("methodName required");
            if (methodName.StartsWith("_", StringComparison.Ordinal))
                throw new InvalidOperationException(
                    $"Method '{methodName}' starts with underscore; by convention these are internal and cannot be invoked via RunEditMethod.");

            foreach (string denied in s_runEditMethodDenyPrefixes)
            {
                if (typeName.StartsWith(denied, StringComparison.Ordinal))
                {
                    throw new InvalidOperationException(
                        $"Type '{typeName}' is on the RunEditMethod denylist (security hardening; see ValidateRunEditMethodScope).");
                }
            }

            string allowEnv = Environment.GetEnvironmentVariable("UNITY_MCP_RUN_METHOD_ALLOW");
            if (!string.IsNullOrEmpty(allowEnv))
            {
                var allowed = allowEnv.Split(',').Select(p => p.Trim()).Where(p => p.Length > 0).ToArray();
                bool matched = false;
                foreach (var prefix in allowed)
                {
                    if (typeName.StartsWith(prefix, StringComparison.Ordinal))
                    {
                        matched = true;
                        break;
                    }
                }
                if (!matched)
                {
                    throw new InvalidOperationException(
                        $"Type '{typeName}' is not in UNITY_MCP_RUN_METHOD_ALLOW allowlist.");
                }
            }
        }

        // Keep arbitrary RunEditMethod return values inside the wire contract. Unity
        // objects and structs expose recursive properties (for example Vector3.normalized),
        // so returning them raw can make Newtonsoft throw and close the MCP socket.
        private const int MaxJsonSafeDepth = 5;
        private const int MaxJsonSafeItems = 64;

        public static object NormalizeReturnValueForJson(object value)
        {
            return NormalizeJsonSafeValue(value, 0, new HashSet<object>(ReferenceComparer.Instance));
        }

        private static object NormalizeJsonSafeValue(object value, int depth, HashSet<object> seen)
        {
            if (value == null)
                return null;
            if (value is JToken)
                return value;

            Type type = value.GetType();
            if (IsScalarJsonValue(type))
                return value;
            if (type.IsEnum)
                return value.ToString();
            if (depth >= MaxJsonSafeDepth)
                return value.ToString();

            if (value is UnityEngine.Object unityObject)
            {
                return new Dictionary<string, object>
                {
                    ["type"] = type.FullName,
                    ["name"] = unityObject.name,
                    ["instanceId"] = unityObject.GetInstanceID(),
                };
            }

            if (type.Namespace == "UnityEngine" && type.IsValueType)
                return NormalizeUnityValueType(value, depth, seen);

            bool trackReference = !type.IsValueType;
            if (trackReference)
            {
                if (seen.Contains(value))
                    return $"<cycle:{type.FullName}>";
                seen.Add(value);
            }

            try
            {
                if (value is System.Collections.IDictionary dict)
                    return NormalizeDictionary(dict, depth, seen);
                if (value is System.Collections.IEnumerable seq && !(value is string))
                    return NormalizeSequence(seq, depth, seen);
                return NormalizeObjectMembers(value, depth, seen);
            }
            finally
            {
                if (trackReference)
                    seen.Remove(value);
            }
        }

        private static bool IsScalarJsonValue(Type type)
        {
            return type.IsPrimitive
                || type == typeof(string)
                || type == typeof(decimal)
                || type == typeof(DateTime)
                || type == typeof(Guid);
        }

        private static Dictionary<string, object> NormalizeUnityValueType(
            object value,
            int depth,
            HashSet<object> seen)
        {
            var result = new Dictionary<string, object>();
            foreach (FieldInfo field in value.GetType().GetFields(BindingFlags.Public | BindingFlags.Instance))
            {
                try
                {
                    result[field.Name] = NormalizeJsonSafeValue(field.GetValue(value), depth + 1, seen);
                }
                catch
                {
                    result[field.Name] = "<unreadable>";
                }
            }
            return result.Count > 0 ? result : new Dictionary<string, object> { ["value"] = value.ToString() };
        }

        private static Dictionary<string, object> NormalizeDictionary(
            System.Collections.IDictionary dict,
            int depth,
            HashSet<object> seen)
        {
            var result = new Dictionary<string, object>();
            int count = 0;
            foreach (System.Collections.DictionaryEntry entry in dict)
            {
                if (count++ >= MaxJsonSafeItems)
                {
                    result["<truncated>"] = true;
                    break;
                }
                string key = entry.Key?.ToString() ?? "<null>";
                result[key] = NormalizeJsonSafeValue(entry.Value, depth + 1, seen);
            }
            return result;
        }

        private static List<object> NormalizeSequence(
            System.Collections.IEnumerable seq,
            int depth,
            HashSet<object> seen)
        {
            var result = new List<object>();
            int count = 0;
            foreach (object item in seq)
            {
                if (count++ >= MaxJsonSafeItems)
                {
                    result.Add("<truncated>");
                    break;
                }
                result.Add(NormalizeJsonSafeValue(item, depth + 1, seen));
            }
            return result;
        }

        private static Dictionary<string, object> NormalizeObjectMembers(
            object value,
            int depth,
            HashSet<object> seen)
        {
            Type type = value.GetType();
            var result = new Dictionary<string, object> { ["type"] = type.FullName };
            int count = 0;

            foreach (FieldInfo field in type.GetFields(BindingFlags.Public | BindingFlags.Instance))
            {
                if (count++ >= MaxJsonSafeItems)
                {
                    result["<truncated>"] = true;
                    return result;
                }
                try { result[field.Name] = NormalizeJsonSafeValue(field.GetValue(value), depth + 1, seen); }
                catch { result[field.Name] = "<unreadable>"; }
            }

            foreach (PropertyInfo prop in type.GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (count++ >= MaxJsonSafeItems)
                {
                    result["<truncated>"] = true;
                    return result;
                }
                if (!prop.CanRead || prop.GetIndexParameters().Length > 0)
                    continue;
                try { result[prop.Name] = NormalizeJsonSafeValue(prop.GetValue(value), depth + 1, seen); }
                catch { result[prop.Name] = "<unreadable>"; }
            }

            return result;
        }

        private sealed class ReferenceComparer : IEqualityComparer<object>
        {
            public static readonly ReferenceComparer Instance = new ReferenceComparer();
            public new bool Equals(object x, object y) => ReferenceEquals(x, y);
            public int GetHashCode(object obj) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(obj);
        }
        // -------------------------------------------------------------------------
        // Phase 0: GetSceneState
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleGetSceneState(RequestEnvelope env)
        {
            GetSceneStateArgs args = ProtocolSerializer.DeserializeArgs<GetSceneStateArgs>(env);
            Scene scene = ResolveScene(args?.Scene);
            if (!scene.IsValid())
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.NotFound,
                    $"Scene not found: '{args?.Scene}'.");
            }

            GetSceneStateResult result = ExecuteGetSceneState(scene);
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        private static Scene ResolveScene(string nameOrPath)
        {
            if (string.IsNullOrEmpty(nameOrPath))
                return SceneManager.GetActiveScene();

            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                Scene s = SceneManager.GetSceneAt(i);
                if (s.name == nameOrPath || s.path == nameOrPath)
                    return s;
            }
            return default;
        }

        public static GetSceneStateResult ExecuteGetSceneState(Scene scene)
        {
            var summaries = new List<GameObjectSummary>();
            foreach (GameObject root in scene.GetRootGameObjects())
                summaries.Add(SummarizeGameObject(root));
            return new GetSceneStateResult { RootObjects = summaries };
        }

        private static GameObjectSummary SummarizeGameObject(GameObject go)
        {
            var componentTypes = new List<string>();
            // Use the typed GetComponents<Component> entry (the mock provides it).
            foreach (Component c in go.GetComponents<Component>())
                componentTypes.Add(c == null ? "<missing>" : c.GetType().FullName);
            return new GameObjectSummary
            {
                Name = go.name,
                InstanceId = go.GetInstanceID(),
                Active = go.activeSelf,
                Components = componentTypes,
                ChildCount = go.transform != null ? go.transform.childCount : 0,
            };
        }

        // -------------------------------------------------------------------------
        // Phase 0: CaptureScreenshot
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleCaptureScreenshot(RequestEnvelope env)
        {
            CaptureScreenshotArgs args = ProtocolSerializer.DeserializeArgs<CaptureScreenshotArgs>(env);
            int supersize = args?.Supersize ?? 1;
            int? width = args?.Width;
            int? height = args?.Height;
            string requestId = env.Id;

            PrepareGameViewForScreenshot(width, height);

            // ScreenCapture.CaptureScreenshotAsTexture only returns a valid texture when
            // called AFTER the frame has finished rendering (the "end of frame" state).
            // EditorApplication.delayCall fires mid-frame, so it captured nothing and the
            // code fell through to a single-camera fallback that omits the PROJECT_UI UI pass.
            //
            // In PlayMode the player loop is running, so we drive the capture from a real
            // runtime coroutine that yields WaitForEndOfFrame: this grabs the fully
            // composited Game-view back buffer (all cameras + UI). Outside PlayMode there
            // is no player loop / WaitForEndOfFrame, so we keep the old delayCall path
            // (edit-mode captures rarely have runtime UI anyway).
            if (Application.isPlaying)
            {
                EndOfFrameScreenshotCapturer.Capture(supersize, width, height, (base64, error) =>
                {
                    if (error != null || string.IsNullOrEmpty(base64))
                    {
                        UnityMCPServer.SendResponse(ProtocolSerializer.ErrorResponse(
                            requestId, ErrorCodes.Internal,
                            error != null ? error.Message : "end-of-frame capture returned no texture"));
                        return;
                    }
                    DeliverScreenshot(requestId, base64, supersize, width, height);
                });
            }
            else
            {
                EditorApplication.delayCall += () =>
                {
                    PrepareGameViewForScreenshot(width, height);
                    EditorApplication.delayCall += () =>
                    {
                        string base64;
                        try
                        {
                            base64 = ExecuteCaptureScreenshot(supersize, width, height);
                        }
                        catch (Exception ex)
                        {
                            UnityMCPServer.SendResponse(
                                ProtocolSerializer.ErrorResponse(requestId, ErrorCodes.Internal, ex.Message));
                            return;
                        }
                        DeliverScreenshot(requestId, base64, supersize, width, height);
                    };
                };
            }

            return null; // Response will be sent asynchronously.
        }

        // Common tail for both capture paths: apply the approval gate if armed, otherwise
        // persist a human-viewable PNG (only when the hint window is open) and respond.
        private static void DeliverScreenshot(string requestId, string base64,
            int supersize, int? width, int? height)
        {
            // Approval gate: when a verification session is armed AND gating is on, the
            // screenshot is NOT delivered to the AI yet. We mirror it into the hint window
            // (in-memory base64 only — NO disk file while gated, per "门控期间不落 AI 读取文件")
            // and hand off to the single dispatcher coroutine, which waits for the user's
            // 继续 click before responding. MCP is serial, so the AI is blocked until then.
            if (VerificationSession.GateScreenshots)
            {
                VerificationHintWindow.SetPreview(base64, null);
                VerificationSession.BeginApproval();
                UnityMCPCoroutineDispatcher.Enqueue(requestId,
                    () => ScreenshotApprovalCoroutine(requestId, supersize, width, height, base64));
                return; // coroutine sends the response after approval
            }

            string savedPath = VerificationHintWindow.IsShowing
                ? PersistScreenshotPng(base64)
                : null;
            VerificationHintWindow.SetPreview(base64, savedPath);
            UnityMCPServer.SendResponse(ProtocolSerializer.OkResponse(requestId,
                new CaptureScreenshotResult { PngBase64 = base64 }));
        }

        // A throw-away runtime MonoBehaviour that captures the Game view exactly at
        // end-of-frame (so all cameras and the PROJECT_UI UI pass are composited) and hands the
        // encoded PNG back on the main thread. Only used in PlayMode.
        private sealed class EndOfFrameScreenshotCapturer : MonoBehaviour
        {
            private int _supersize;
            private int? _width;
            private int? _height;
            private Action<string, Exception> _done;

            public static void Capture(int supersize, int? width, int? height,
                Action<string, Exception> done)
            {
                var go = new GameObject("UnityMCP_EndOfFrameCapturer")
                {
                    hideFlags = HideFlags.HideAndDontSave
                };
                var c = go.AddComponent<EndOfFrameScreenshotCapturer>();
                c._supersize = supersize;
                c._width = width;
                c._height = height;
                c._done = done;
                c.StartCoroutine(c.Run());
            }

            private IEnumerator Run()
            {
                // Let the requested Game-view size / focus take effect for a frame, then
                // capture at the true end-of-frame state.
                yield return null;
                yield return new WaitForEndOfFrame();

                string base64 = null;
                Exception error = null;
                try
                {
                    base64 = ExecuteCaptureScreenshot(_supersize, _width, _height);
                }
                catch (Exception ex)
                {
                    error = ex;
                }

                Action<string, Exception> cb = _done;
                _done = null;
                UnityEngine.Object.Destroy(gameObject);

                cb?.Invoke(base64, error);
            }
        }

        // The approval-gate coroutine, run in the SINGLE UnityMCPCoroutineDispatcher
        // pump (so "at most one coroutine services tool calls" holds). It yields one
        // frame at a time — never blocking the main thread — until the user clicks
        // 继续 in the hint window, then delivers the base64 to the AI and deletes any
        // on-disk file ("继续后立即删"). Handles 重新截图 by re-capturing in place.
        private static System.Collections.IEnumerator ScreenshotApprovalCoroutine(
            string requestId, int supersize, int? width, int? height, string base64)
        {
            try
            {
                while (true)
                {
                    // The session may end / playmode exit / reload mid-wait; the
                    // dispatcher AbortAll would already have responded for this id and
                    // stopped us, but guard anyway so we never deliver after teardown.
                    if (!VerificationSession.IsArmed)
                        yield break;

                    // User asked for a fresh frame.
                    if (VerificationSession.ConsumeRecaptureRequest())
                    {
                        string recaptured = null;
                        if (Application.isPlaying)
                        {
                            // Mirror the primary path: capture at end-of-frame so the
                            // re-shot frame also includes the UI. Kick off the runtime
                            // coroutine and hand frames back until it reports a result.
                            bool captureDone = false;
                            EndOfFrameScreenshotCapturer.Capture(supersize, width, height,
                                (b64, err) => { recaptured = b64; captureDone = true; });
                            while (!captureDone)
                                yield return null;
                        }
                        else
                        {
                            try { recaptured = ExecuteCaptureScreenshot(supersize, width, height); }
                            catch { /* keep previous frame on failure */ }
                        }
                        if (!string.IsNullOrEmpty(recaptured))
                        {
                            base64 = recaptured;
                            VerificationHintWindow.SetPreview(base64, null);
                        }
                    }

                    VerificationSession.Approval decision = VerificationSession.PollApproval();
                    if (decision == VerificationSession.Approval.Approved)
                    {
                        VerificationSession.EndApproval();
                        UnityMCPServer.SendResponse(ProtocolSerializer.OkResponse(requestId,
                            new CaptureScreenshotResult { PngBase64 = base64 }));
                        // "继续后立即删": the AI now has the base64; remove any on-disk
                        // file the user may have created via 打开目录 so disk stays clean.
                        DeleteCurrentApprovedScreenshotFile(base64);
                        yield break;
                    }
                    if (decision == VerificationSession.Approval.Rejected)
                    {
                        VerificationSession.EndApproval();
                        UnityMCPServer.SendResponse(ProtocolSerializer.ErrorResponse(requestId,
                            ErrorCodes.InvalidState, "screenshot_approval_aborted"));
                        yield break;
                    }

                    yield return null; // cooperative: hand the frame back, keep waiting
                }
            }
            finally
            {
                // Defensive: if the coroutine is stopped (AbortAll) without resolving,
                // make sure the window's approval row is cleared.
                VerificationSession.EndApproval();
            }
        }

        // Delete the on-disk PNG for this base64 if one exists (sha-keyed), used by the
        // approval gate's "继续后立即删". Best-effort; never throws.
        private static void DeleteCurrentApprovedScreenshotFile(string base64)
        {
            try
            {
                if (string.IsNullOrEmpty(base64))
                    return;
                string sha;
                using (var sha256 = System.Security.Cryptography.SHA256.Create())
                {
                    byte[] hash = sha256.ComputeHash(System.Text.Encoding.UTF8.GetBytes(base64));
                    var sb = new System.Text.StringBuilder(hash.Length * 2);
                    foreach (byte b in hash) sb.Append(b.ToString("x2"));
                    sha = sb.ToString();
                }
                string path = System.IO.Path.Combine(ResolveScreenshotDir(), sha + ".png");
                if (System.IO.File.Exists(path))
                    System.IO.File.Delete(path);
            }
            catch { /* best-effort cleanup */ }
        }

        // Persist a base64 PNG to the same on-disk screenshot cache the Python tool
        // uses (UNITY_MCP_SCREENSHOT_DIR, else %LOCALAPPDATA%/unity-mcp-py/screenshots),
        // keyed by sha256 so the C# and Python (mode=file_ref) sides share one file and
        // never duplicate. Returns the full path, or null on any failure (best-effort —
        // a persistence error must never break the screenshot response).
        private static string PersistScreenshotPng(string pngBase64)
        {
            if (string.IsNullOrEmpty(pngBase64))
                return null;
            try
            {
                byte[] data = Convert.FromBase64String(pngBase64);

                // sha256 over the base64 STRING (matches Python's hashlib.sha256(
                // png_base64.encode("utf-8","replace")).hexdigest()).
                string sha;
                using (var sha256 = System.Security.Cryptography.SHA256.Create())
                {
                    byte[] hash = sha256.ComputeHash(System.Text.Encoding.UTF8.GetBytes(pngBase64));
                    var sb = new System.Text.StringBuilder(hash.Length * 2);
                    foreach (byte b in hash) sb.Append(b.ToString("x2"));
                    sha = sb.ToString();
                }

                string dir = ResolveScreenshotDir();
                System.IO.Directory.CreateDirectory(dir);
                string path = System.IO.Path.Combine(dir, sha + ".png");
                if (!System.IO.File.Exists(path))
                    System.IO.File.WriteAllBytes(path, data);
                return path;
            }
            catch
            {
                return null; // best-effort; never break the capture response
            }
        }

        internal static string ResolveScreenshotDir()
        {
            string overrideDir = Environment.GetEnvironmentVariable("UNITY_MCP_SCREENSHOT_DIR");
            if (!string.IsNullOrEmpty(overrideDir))
                return overrideDir;
            string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (!string.IsNullOrEmpty(local))
                return System.IO.Path.Combine(local, "unity-mcp-py", "screenshots");
            string home = Environment.GetEnvironmentVariable("USERPROFILE")
                          ?? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            return System.IO.Path.Combine(home, ".unity-mcp-py", "screenshots");
        }

        public static string ExecuteCaptureScreenshot(int supersize)
        {
            return ExecuteCaptureScreenshot(supersize, null, null);
        }

        public static string ExecuteCaptureScreenshot(int supersize, int? width, int? height)
        {
            PrepareGameViewForScreenshot(width, height);
            return ExecuteCaptureScreenshot(
                supersize,
                width,
                height,
                gameViewCaptureFn: size => ScreenCapture.CaptureScreenshotAsTexture(size),
                fallbackCaptureFn: () => CaptureFromActiveCameraTexture(supersize, width, height));
        }

        public static string ExecuteCaptureScreenshot(
            int supersize,
            int? width,
            int? height,
            Func<int, Texture2D> gameViewCaptureFn,
            Func<Texture2D> fallbackCaptureFn)
        {
            int safeSupersize = Math.Max(1, supersize);
            Exception primaryError = null;

            string primaryBase64 = TryCaptureAndEncode(
                () => gameViewCaptureFn?.Invoke(safeSupersize),
                out primaryError);
            if (!string.IsNullOrEmpty(primaryBase64))
                return primaryBase64;

            Exception fallbackError;
            string fallbackBase64 = TryCaptureAndEncode(fallbackCaptureFn, out fallbackError);
            if (!string.IsNullOrEmpty(fallbackBase64))
                return fallbackBase64;

            if (primaryError != null && fallbackError != null)
            {
                throw new InvalidOperationException(
                    $"Game view screenshot failed: {primaryError.Message}; camera fallback failed: {fallbackError.Message}",
                    fallbackError);
            }
            if (primaryError != null)
            {
                throw new InvalidOperationException(
                    $"Game view screenshot failed: {primaryError.Message}; camera fallback returned no valid texture.",
                    primaryError);
            }
            if (fallbackError != null)
            {
                throw new InvalidOperationException(
                    $"Game view screenshot returned no valid texture; camera fallback failed: {fallbackError.Message}",
                    fallbackError);
            }

            throw new InvalidOperationException(
                "CaptureScreenshot returned no valid texture from Game view or camera fallback.");
        }

        private static void PrepareGameViewForScreenshot(int? width, int? height)
        {
            if (width.HasValue && height.HasValue && width.Value > 0 && height.Value > 0)
                TryApplyGameViewSize(width.Value, height.Value);

            FocusGameViewForScreenshot();
        }

        private static void FocusGameViewForScreenshot()
        {
            try
            {
                Assembly editorAsm = AppDomain.CurrentDomain.GetAssemblies()
                    .FirstOrDefault(a => a.GetName().Name == "UnityEditor"
                                      || a.GetName().Name == "UnityEditor.CoreModule");
                if (editorAsm == null) return;

                Type gameViewType = editorAsm.GetType("UnityEditor.GameView");
                Type editorWindowType = editorAsm.GetType("UnityEditor.EditorWindow");
                if (gameViewType == null || editorWindowType == null) return;

                MethodInfo getWindowMi = editorWindowType.GetMethod("GetWindow", new[] { typeof(Type) });
                object gameView = getWindowMi?.Invoke(null, new object[] { gameViewType });
                if (gameView == null) return;

                editorWindowType.GetMethod("Show", Type.EmptyTypes)?.Invoke(gameView, null);
                editorWindowType.GetMethod("Focus", Type.EmptyTypes)?.Invoke(gameView, null);
                editorWindowType.GetMethod("Repaint", Type.EmptyTypes)?.Invoke(gameView, null);
            }
            catch
            {
                // Best effort only: camera fallback below can still produce a frame.
            }
        }

        private static string TryCaptureAndEncode(Func<Texture2D> captureFn, out Exception error)
        {
            error = null;
            Texture2D tex = null;
            try
            {
                tex = captureFn?.Invoke();
                if (tex == null)
                    return null;

                byte[] png = tex.EncodeToPNG();
                if (png == null || png.Length == 0)
                    return null;

                return Convert.ToBase64String(png);
            }
            catch (Exception ex)
            {
                error = ex;
                return null;
            }
            finally
            {
                if (tex != null)
                    UnityEngine.Object.DestroyImmediate(tex);
            }
        }

        private static Texture2D CaptureFromActiveCameraTexture(int supersize, int? width, int? height)
        {
            Camera camera = Camera.main;
            if (camera == null)
            {
                Camera[] cameras = UnityEngine.Object.FindObjectsOfType<Camera>();
                camera = cameras.FirstOrDefault(c => c != null && c.enabled)
                         ?? cameras.FirstOrDefault(c => c != null);
            }
            if (camera == null)
                return null;

            int safeSupersize = Math.Max(1, supersize);
            int targetWidth = ResolveScreenshotDimension(width, Screen.width, camera.pixelWidth, 1280, safeSupersize);
            int targetHeight = ResolveScreenshotDimension(height, Screen.height, camera.pixelHeight, 720, safeSupersize);

            RenderTexture previousTarget = camera.targetTexture;
            RenderTexture previousActive = RenderTexture.active;
            RenderTexture rt = new RenderTexture(targetWidth, targetHeight, 24);
            Texture2D tex = new Texture2D(targetWidth, targetHeight, TextureFormat.RGB24, false);
            try
            {
                camera.targetTexture = rt;
                RenderTexture.active = rt;
                camera.Render();
                tex.ReadPixels(new Rect(0, 0, targetWidth, targetHeight), 0, 0);
                tex.Apply();
                return tex;
            }
            catch
            {
                UnityEngine.Object.DestroyImmediate(tex);
                throw;
            }
            finally
            {
                camera.targetTexture = previousTarget;
                RenderTexture.active = previousActive;
                rt.Release();
                UnityEngine.Object.DestroyImmediate(rt);
            }
        }

        private static int ResolveScreenshotDimension(
            int? requested,
            int screenValue,
            int cameraValue,
            int fallbackValue,
            int supersize)
        {
            if (requested.HasValue && requested.Value > 0)
                return requested.Value;

            int baseValue = screenValue > 0 ? screenValue : (cameraValue > 0 ? cameraValue : fallbackValue);
            return Math.Max(1, baseValue * Math.Max(1, supersize));
        }

        // -------------------------------------------------------------------------
        // Phase 0 + Phase 1: TailEditorLog (deprecated)
        // -------------------------------------------------------------------------

        [Obsolete("TailEditorLog is deprecated. Subscribe to the 'log.line' notification stream instead.")]
        private static ResponseEnvelope HandleTailEditorLog(RequestEnvelope env)
        {
            if (!s_tailDeprecationWarned)
            {
                s_tailDeprecationWarned = true;
                Debug.LogWarning("[UnityMCP] TailEditorLog is deprecated; subscribe to the 'log.line' " +
                                 "notification stream instead. This warning will not be repeated.");
            }

            TailEditorLogArgs args = ProtocolSerializer.DeserializeArgs<TailEditorLogArgs>(env);
            int since = args?.SinceLineNumber ?? 0;
            int max = args?.MaxLines ?? MaxTailEditorLogLines;
            if (max < 1) max = MaxTailEditorLogLines;
            max = Math.Min(max, MaxTailEditorLogLines);

            TailEditorLogResult result = ExecuteTailEditorLog(since, max);
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        public static TailEditorLogResult ExecuteTailEditorLog(int sinceLineNumber, int maxLines)
        {
            int boundedMaxLines = Math.Max(1, Math.Min(maxLines, MaxTailEditorLogLines));
            var output = new Queue<LogLine>(boundedMaxLines);
            lock (s_logLock)
            {
                foreach (LogLine ll in s_logRingBuffer)
                {
                    if (ll.LineNumber <= sinceLineNumber)
                        continue;

                    output.Enqueue(ll);
                    while (output.Count > boundedMaxLines)
                        output.Dequeue();
                }
            }
            return new TailEditorLogResult { Lines = output.ToList() };
        }

        // -------------------------------------------------------------------------
        // Phase 0: QueryGameObject
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleQueryGameObject(RequestEnvelope env)
        {
            QueryGameObjectArgs args = ProtocolSerializer.DeserializeArgs<QueryGameObjectArgs>(env);
            if (args == null || (args.InstanceId == null && string.IsNullOrEmpty(args.Path)))
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "QueryGameObject requires either instanceId or path.");
            }

            GameObject go = null;
            if (args.InstanceId.HasValue)
                go = EditorUtility.InstanceIDToObject(args.InstanceId.Value) as GameObject;

            if (go == null)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.NotFound,
                    "GameObject not found.");
            }

            QueryGameObjectResult result = ExecuteQueryGameObject(go);
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        public static QueryGameObjectResult ExecuteQueryGameObject(GameObject go)
        {
            var comps = new List<ComponentDetails>();
            foreach (Component c in go.GetComponents<Component>())
            {
                if (c == null)
                {
                    comps.Add(new ComponentDetails { Type = "<missing>", Fields = new Dictionary<string, object>() });
                    continue;
                }
                comps.Add(new ComponentDetails
                {
                    Type = c.GetType().FullName,
                    Fields = ExtractFields(c),
                });
            }

            return new QueryGameObjectResult
            {
                Name = go.name,
                InstanceId = go.GetInstanceID(),
                Active = go.activeSelf,
                Components = comps,
            };
        }

        private static Dictionary<string, object> ExtractFields(Component c)
        {
            var map = new Dictionary<string, object>();
            if (c == null) return map;

            try
            {
                Type t = c.GetType();
                foreach (FieldInfo f in t.GetFields(BindingFlags.Public | BindingFlags.Instance))
                {
                    try { map[f.Name] = StringifyValue(f.GetValue(c)); }
                    catch { map[f.Name] = "<unreadable>"; }
                }
                foreach (PropertyInfo p in t.GetProperties(BindingFlags.Public | BindingFlags.Instance))
                {
                    if (!p.CanRead || p.GetIndexParameters().Length > 0) continue;
                    try { map[p.Name] = StringifyValue(p.GetValue(c)); }
                    catch { /* some Unity properties throw if accessed in the wrong state */ }
                }
            }
            catch
            {
                // best-effort
            }
            return map;
        }

        private static object StringifyValue(object v)
        {
            if (v == null) return null;
            Type t = v.GetType();
            if (t.IsPrimitive || v is string || v is decimal) return v;
            if (v is UnityEngine.Object uo) return uo == null ? null : uo.name;
            return v.ToString();
        }

        // -------------------------------------------------------------------------
        // Phase 2: OpenScene
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleOpenScene(RequestEnvelope env)
        {
            OpenSceneArgs args = ProtocolSerializer.DeserializeArgs<OpenSceneArgs>(env);
            return ExecuteOpenScene(env.Id, args ?? new OpenSceneArgs(),
                getIsPlaying: () => EditorApplication.isPlaying,
                fileExists: p => System.IO.File.Exists(p)
                                || System.IO.File.Exists(System.IO.Path.Combine(
                                       System.IO.Path.GetFullPath(System.IO.Path.Combine(Application.dataPath, "..")), p)),
                openSceneFn: (p, m) => EditorSceneManager.OpenScene(p, m));
        }

        public static ResponseEnvelope ExecuteOpenScene(
            string id,
            OpenSceneArgs args,
            Func<bool> getIsPlaying,
            Func<string, bool> fileExists,
            Func<string, OpenSceneMode, Scene> openSceneFn)
        {
            if (args == null || string.IsNullOrEmpty(args.ScenePath))
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    "OpenScene requires scenePath.");
            }
            if (getIsPlaying != null && getIsPlaying())
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidState,
                    "Cannot open a scene while the Editor is in playmode.");
            }
            if (fileExists != null && !fileExists(args.ScenePath))
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.NotFound,
                    $"Scene file not found: '{args.ScenePath}'.");
            }

            OpenSceneMode mode = ParseSceneMode(args.Mode);
            Scene s;
            try
            {
                s = openSceneFn(args.ScenePath, mode);
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal, ex.Message);
            }

            return ProtocolSerializer.OkResponse(id, new OpenSceneResult
            {
                SceneName = s.name,
                ScenePath = s.path,
                Mode = mode.ToString(),
                IsLoaded = s.isLoaded,
            });
        }

        private static OpenSceneMode ParseSceneMode(string mode)
        {
            if (string.IsNullOrEmpty(mode)) return OpenSceneMode.Single;
            switch (mode.ToLowerInvariant())
            {
                case "single":            return OpenSceneMode.Single;
                case "additive":          return OpenSceneMode.Additive;
                case "additivewithoutloading":
                case "additive_without_loading":
                    return OpenSceneMode.AdditiveWithoutLoading;
                default:                  return OpenSceneMode.Single;
            }
        }

        // -------------------------------------------------------------------------
        // Phase 2: RefreshAssets — non-blocking (durationMs=0)
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleRefreshAssets(RequestEnvelope env)
        {
            RefreshAssetsArgs args = ProtocolSerializer.DeserializeArgs<RefreshAssetsArgs>(env);
            return ExecuteRefreshAssets(env.Id, args ?? new RefreshAssetsArgs(),
                refreshFn: () => AssetDatabase.Refresh(),
                setSessionState: (k, v) => SessionState.SetString(k, v));
        }

        public static ResponseEnvelope ExecuteRefreshAssets(
            string id,
            RefreshAssetsArgs args,
            Action refreshFn,
            Action<string, string> setSessionState)
        {
            try
            {
                refreshFn?.Invoke();
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal, ex.Message);
            }

            // Default WaitForCompile=null treats as true → write a per-request marker
            // so the eventual compile.done event can be correlated to this id.
            bool waitForCompile = args?.WaitForCompile ?? true;
            if (waitForCompile && setSessionState != null)
            {
                string key = RefreshAssetsMarkerPrefix + id;
                setSessionState(key,
                    DateTime.UtcNow.ToString("o", System.Globalization.CultureInfo.InvariantCulture));
            }

            return ProtocolSerializer.OkResponse(id, new RefreshAssetsResult
            {
                Refreshed = true,
                // Non-blocking semantics: durationMs is always 0.0 — the bridge does NOT
                // wait for compilation. Clients should subscribe to compile.done.
                CompileSucceeded = !EditorUtility.scriptCompilationFailed,
                DurationMs = 0.0,
            });
        }

        // -------------------------------------------------------------------------
        // Phase 2: PingObject
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandlePingObject(RequestEnvelope env)
        {
            PingObjectArgs args = ProtocolSerializer.DeserializeArgs<PingObjectArgs>(env);
            return ExecutePingObject(env.Id, args ?? new PingObjectArgs(),
                resolveByInstanceId: id => EditorUtility.InstanceIDToObject(id),
                resolveByPath: p => null, // GameObject.Find is play-mode only — not exposed via PingObject
                pingFn: obj => EditorGUIUtility.PingObject(obj),
                setActiveObject: obj => Selection.activeObject = obj);
        }

        public static ResponseEnvelope ExecutePingObject(
            string id,
            PingObjectArgs args,
            Func<int, UnityEngine.Object> resolveByInstanceId,
            Func<string, GameObject> resolveByPath,
            Action<UnityEngine.Object> pingFn,
            Action<UnityEngine.Object> setActiveObject)
        {
            bool hasId = args != null && args.InstanceId.HasValue;
            bool hasPath = args != null && !string.IsNullOrEmpty(args.Path);
            if (hasId == hasPath)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    "PingObject requires Exactly one of instanceId or path.");
            }

            UnityEngine.Object target = null;
            if (hasId)
                target = resolveByInstanceId?.Invoke(args.InstanceId.Value);
            else
                target = resolveByPath?.Invoke(args.Path);

            bool found = target != null;
            if (found)
            {
                try { pingFn?.Invoke(target); } catch { /* best-effort */ }
                try { setActiveObject?.Invoke(target); } catch { /* best-effort */ }
            }

            return ProtocolSerializer.OkResponse(id, new PingObjectResult
            {
                InstanceId = args.InstanceId,
                Path = args.Path,
                Found = found,
            });
        }

        // -------------------------------------------------------------------------
        // Phase 2: SetGameViewSize
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleSetGameViewSize(RequestEnvelope env)
        {
            SetGameViewSizeArgs args = ProtocolSerializer.DeserializeArgs<SetGameViewSizeArgs>(env);
            return ExecuteSetGameViewSize(env.Id, args ?? new SetGameViewSizeArgs(),
                applyFn: (w, h) => TryApplyGameViewSize(w, h));
        }

        public static ResponseEnvelope ExecuteSetGameViewSize(
            string id,
            SetGameViewSizeArgs args,
            Func<int, int, bool> applyFn)
        {
            if (args == null || args.Width <= 0 || args.Height <= 0)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    "SetGameViewSize requires positive width and height.");
            }

            bool applied;
            try { applied = applyFn != null && applyFn(args.Width, args.Height); }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal, ex.Message);
            }

            return ProtocolSerializer.OkResponse(id, new SetGameViewSizeResult
            {
                Width = args.Width,
                Height = args.Height,
                Applied = applied,
            });
        }

        private static bool TryApplyGameViewSize(int width, int height)
        {
            try
            {
                Assembly editorAsm = AppDomain.CurrentDomain.GetAssemblies()
                    .FirstOrDefault(a => a.GetName().Name == "UnityEditor"
                                      || a.GetName().Name == "UnityEditor.CoreModule");
                if (editorAsm == null) return false;

                Type gvsType = editorAsm.GetType("UnityEditor.GameViewSizes");
                if (gvsType == null) return false;
                object singleton = gvsType.GetProperty("instance",
                    BindingFlags.Public | BindingFlags.Static)?.GetValue(null);
                if (singleton == null) return false;

                MethodInfo currentGroupMi = singleton.GetType().GetMethod("CurrentGroup",
                    BindingFlags.Public | BindingFlags.Instance);
                if (currentGroupMi == null) return false;
                object group = currentGroupMi.Invoke(singleton, null);
                if (group == null) return false;

                Type gvsSizeType     = editorAsm.GetType("UnityEditor.GameViewSize");
                Type gvsSizeTypeEnum = editorAsm.GetType("UnityEditor.GameViewSizeType");
                if (gvsSizeType == null || gvsSizeTypeEnum == null) return false;

                object fixedRes = Enum.Parse(gvsSizeTypeEnum, "FixedResolution");
                object newSize = Activator.CreateInstance(gvsSizeType, fixedRes, width, height,
                    $"MCP {width}x{height}");

                Type groupType = group.GetType();
                groupType.GetMethod("AddCustomSize")?.Invoke(group, new[] { newSize });
                int total = (int)groupType.GetMethod("GetTotalCount").Invoke(group, null);

                Type gameViewType = editorAsm.GetType("UnityEditor.GameView");
                Type ewType       = editorAsm.GetType("UnityEditor.EditorWindow");
                if (gameViewType == null || ewType == null) return false;

                MethodInfo getWindowMi = ewType.GetMethod("GetWindow", new[] { typeof(Type) });
                object gameView = getWindowMi?.Invoke(null, new object[] { gameViewType });
                if (gameView == null) return false;

                PropertyInfo sizeIdxProp = gameViewType.GetProperty("selectedSizeIndex",
                    BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                sizeIdxProp?.SetValue(gameView, total - 1);
                return true;
            }
            catch
            {
                return false;
            }
        }

        // -------------------------------------------------------------------------
        // Phase 2: WaitForIdle
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleWaitForIdle(RequestEnvelope env)
        {
            WaitForIdleArgs args = ProtocolSerializer.DeserializeArgs<WaitForIdleArgs>(env);
            ResponseEnvelope validation = TryCreateWaitForIdleOperation(
                env.Id,
                args ?? new WaitForIdleArgs(),
                getLogCounter: () => UnityMCPEventBridge.GetLogLineCounter(),
                nowFn: () => DateTime.UtcNow,
                out WaitForIdleOperation operation);
            if (validation != null)
                return validation;

            EditorApplication.CallbackFunction tick = null;
            tick = () =>
            {
                ResponseEnvelope response;
                try
                {
                    response = operation.Tick();
                }
                catch (Exception ex)
                {
                    response = ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                        $"WaitForIdle tick failed: {ex.Message}");
                }

                if (response == null)
                    return;

                EditorApplication.update -= tick;
                UnityMCPServer.SendResponse(response);
            };
            EditorApplication.update += tick;
            return null;
        }

        public sealed class WaitForIdleOperation
        {
            private readonly string _id;
            private readonly double _seconds;
            private readonly Func<int> _getLogCounter;
            private readonly Func<DateTime> _nowFn;
            private readonly DateTime _start;
            private readonly DateTime _deadline;
            private int _lastCounter;
            private DateTime _lastEventAt;
            private bool _completed;

            internal WaitForIdleOperation(
                string id,
                double seconds,
                Func<int> getLogCounter,
                Func<DateTime> nowFn)
            {
                _id = id;
                _seconds = seconds;
                _getLogCounter = getLogCounter ?? (() => 0);
                _nowFn = nowFn ?? (() => DateTime.UtcNow);
                _start = _nowFn();
                _deadline = _start.AddSeconds(seconds);
                _lastCounter = _getLogCounter();
                _lastEventAt = _start;
            }

            public ResponseEnvelope Tick()
            {
                if (_completed)
                    return null;

                DateTime now = _nowFn();
                int counter = _getLogCounter();
                if (counter != _lastCounter)
                {
                    _lastCounter = counter;
                    _lastEventAt = now;
                }

                bool idleAchieved = (now - _lastEventAt).TotalSeconds >= _seconds;
                if (!idleAchieved && now < _deadline)
                    return null;

                _completed = true;
                return ProtocolSerializer.OkResponse(_id, new WaitForIdleResult
                {
                    IdleAchieved = idleAchieved,
                    ElapsedSeconds = (now - _start).TotalSeconds,
                    LastEventAt = _lastEventAt.ToString("o", System.Globalization.CultureInfo.InvariantCulture),
                });
            }
        }

        public static ResponseEnvelope TryCreateWaitForIdleOperation(
            string id,
            WaitForIdleArgs args,
            Func<int> getLogCounter,
            Func<DateTime> nowFn,
            out WaitForIdleOperation operation)
        {
            operation = null;
            if (args == null || args.Seconds <= 0)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    "WaitForIdle requires seconds > 0.");
            }
            if (args.Seconds > WaitForIdleMaxSeconds)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    $"WaitForIdle 'seconds' exceeds the hard cap of {WaitForIdleMaxSeconds}.");
            }

            string source = string.IsNullOrEmpty(args.Source) ? "log" : args.Source.Trim().ToLowerInvariant();
            if (source == "mouse")
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    "WaitForIdle source 'mouse' is not supported. Use 'log'.");
            }
            if (source != "log")
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    $"WaitForIdle: unknown source '{args.Source}'.");
            }

            operation = new WaitForIdleOperation(id, args.Seconds, getLogCounter, nowFn);
            return null;
        }

        public static ResponseEnvelope ExecuteWaitForIdle(
            string id,
            WaitForIdleArgs args,
            Func<int> getLogCounter,
            Action<int> sleepFn,
            Func<DateTime> nowFn)
        {
            ResponseEnvelope validation = TryCreateWaitForIdleOperation(
                id, args, getLogCounter, nowFn, out WaitForIdleOperation operation);
            if (validation != null)
                return validation;

            const int pollMs = 50;
            ResponseEnvelope response = null;
            while (response == null)
            {
                sleepFn?.Invoke(pollMs);
                response = operation.Tick();
            }
            return response;
        }

        // -------------------------------------------------------------------------
        // Phase 3 (protocol 1.3.0): LuaReloadModule
        //
        // Wraps the project's PROJECT_ADAPTER.Reload.reload(list, impFiles, delKeyModels)
        // entry point. Uses PROJECT_NAMESPACE.PROJECT_FACADE.PROJECT_LUA_MANAGER.PROJECT_LUA_VM (xLua LuaEnv)
        // to execute the Lua side.
        //
        // The Execute* overload keeps xLua/Unity types out of the test surface by
        // accepting a `doStringFn` delegate that returns an object[] (Lua multi-return
        // translated to CLR).
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleLuaReloadModule(RequestEnvelope env)
        {
            LuaReloadModuleArgs args = ProtocolSerializer.DeserializeArgs<LuaReloadModuleArgs>(env);
            return ExecuteLuaReloadModule(env.Id, args ?? new LuaReloadModuleArgs(),
                doStringFn: DefaultLuaDoString);
        }

        /// <summary>
        /// Execute LuaReloadModule. <paramref name="doStringFn"/> takes a Lua script
        /// string and returns the Lua multi-return translated to a CLR object[].
        /// For the `reload.reload` contract, the first element is the `ok` boolean
        /// and the second is either the list of reloaded modules (on success) or
        /// an error string/traceback (on failure).
        /// </summary>
        public static ResponseEnvelope ExecuteLuaReloadModule(
            string id,
            LuaReloadModuleArgs args,
            Func<string, object[]> doStringFn)
        {
            if (args == null || args.Modules == null || args.Modules.Length == 0)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    "LuaReloadModule requires a non-empty 'modules' array.");
            }
            if (doStringFn == null)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                    "LuaReloadModule: no Lua executor is wired (PROJECT_FACADE.PROJECT_LUA_MANAGER.PROJECT_LUA_VM is null?).");
            }

            string script = BuildLuaReloadScript(args);
            object[] retVals;
            try
            {
                retVals = doStringFn(script) ?? Array.Empty<object>();
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                    $"LuaReloadModule: Lua DoString threw: {ex.Message}");
            }

            bool ok = retVals.Length >= 1 && retVals[0] is bool b && b;
            if (!ok)
            {
                string err = retVals.Length >= 2 && retVals[1] != null
                    ? retVals[1].ToString()
                    : "Lua reload returned false without error payload.";
                return ProtocolSerializer.OkResponse(id, new LuaReloadModuleResult
                {
                    Ok = false,
                    Error = err,
                    Reloaded = new List<string>(),
                });
            }

            var reloaded = new List<string>();
            if (retVals.Length >= 2 && retVals[1] is System.Collections.IEnumerable enumerable
                && !(retVals[1] is string))
            {
                foreach (object item in enumerable)
                {
                    if (item != null) reloaded.Add(item.ToString());
                }
            }
            else
            {
                // Lua might have returned a table-like the bridge can't materialize;
                // fall back to echoing the requested module names.
                reloaded.AddRange(args.Modules);
            }

            return ProtocolSerializer.OkResponse(id, new LuaReloadModuleResult
            {
                Ok = true,
                Reloaded = reloaded,
            });
        }

        private static string BuildLuaReloadScript(LuaReloadModuleArgs args)
        {
            var sb = new System.Text.StringBuilder(256);
            sb.Append("local r = require('PROJECT_ADAPTER.Reload'); return r.reload(");
            AppendStringArrayAsLuaTable(sb, args.Modules);
            sb.Append(", ");
            AppendStringArrayAsLuaTable(sb, args.ImpFiles);
            sb.Append(", ");
            AppendStringArrayAsLuaTable(sb, args.DelKeyModels);
            sb.Append(")");
            return sb.ToString();
        }

        private static void AppendStringArrayAsLuaTable(System.Text.StringBuilder sb, string[] values)
        {
            if (values == null || values.Length == 0)
            {
                sb.Append("{}");
                return;
            }
            sb.Append("{");
            for (int i = 0; i < values.Length; i++)
            {
                if (i > 0) sb.Append(", ");
                sb.Append("'").Append(EscapeSingleQuoted(values[i] ?? string.Empty)).Append("'");
            }
            sb.Append("}");
        }

        private static string EscapeSingleQuoted(string s)
        {
            // Lua '...' strings: escape backslash and single quote.
            return s.Replace("\\", "\\\\").Replace("'", "\\'");
        }

        /// <summary>
        /// Default Lua DoString used at runtime. Resolves the project's xLua
        /// LuaEnv via PROJECT_NAMESPACE.PROJECT_FACADE.PROJECT_LUA_MANAGER.PROJECT_LUA_VM reflectively so
        /// this package does not take a hard reference on Assembly-CSharp.
        /// </summary>
        private static object[] DefaultLuaDoString(string script)
        {
            Type facadeType = null;
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                facadeType = asm.GetType("PROJECT_NAMESPACE.PROJECT_FACADE", throwOnError: false);
                if (facadeType != null) break;
            }
            if (facadeType == null)
                throw new InvalidOperationException(
                    "PROJECT_NAMESPACE.PROJECT_FACADE not found. LuaReloadModule requires the game assembly.");

            PropertyInfo lmProp = facadeType.GetProperty("PROJECT_LUA_MANAGER",
                BindingFlags.Public | BindingFlags.Static);
            if (lmProp == null)
                throw new InvalidOperationException("PROJECT_FACADE.PROJECT_LUA_MANAGER property not found.");
            object lm = lmProp.GetValue(null);
            if (lm == null)
                throw new InvalidOperationException(
                    "PROJECT_FACADE.PROJECT_LUA_MANAGER is null — game must be running for Lua reload.");

            FieldInfo envField = lm.GetType().GetField("PROJECT_LUA_VM",
                BindingFlags.Public | BindingFlags.Instance);
            if (envField == null)
                throw new InvalidOperationException("PROJECT_LUA_MANAGER.PROJECT_LUA_VM field not found.");
            object luaEnv = envField.GetValue(lm);
            if (luaEnv == null)
                throw new InvalidOperationException(
                    "PROJECT_LUA_MANAGER.PROJECT_LUA_VM is null — Lua runtime not initialized.");

            return InvokeLuaDoString(luaEnv, script);
        }

        /// <summary>
        /// Reflectively call <c>DoString</c> on an unknown Lua environment object
        /// without taking a hard reference on xLua. The Lua environment's actual
        /// signature is <c>DoString(string chunk, string chunkName = "chunk", LuaTable env = null)</c>,
        /// so <see cref="Type.GetMethod(string, Type[])"/> with <c>new[] { typeof(string) }</c>
        /// returns null — reflection does not collapse optional parameters. We walk all
        /// <c>DoString</c> overloads and pick the first non-generic one whose first parameter
        /// is <see cref="string"/> and whose remaining parameters are all
        /// <see cref="ParameterInfo.IsOptional"/>, then invoke it with
        /// <see cref="Type.Missing"/> placeholders so the CLR fills in the defaults.
        /// Exposed for unit testing — callers should prefer <see cref="DefaultLuaDoString"/>.
        /// </summary>
        public static object[] InvokeLuaDoString(object luaEnv, string script)
        {
            if (luaEnv == null) throw new ArgumentNullException(nameof(luaEnv));

            MethodInfo doStringMi = luaEnv.GetType()
                .GetMethods(BindingFlags.Public | BindingFlags.Instance)
                .FirstOrDefault(m =>
                {
                    if (m.Name != "DoString") return false;
                    if (m.IsGenericMethodDefinition) return false;
                    ParameterInfo[] ps = m.GetParameters();
                    if (ps.Length < 1) return false;
                    if (ps[0].ParameterType != typeof(string)) return false;
                    for (int i = 1; i < ps.Length; i++)
                        if (!ps[i].IsOptional) return false;
                    return true;
                });
            if (doStringMi == null)
                throw new InvalidOperationException(
                    "LuaEnv.DoString(string, ...) not found on type '" +
                    luaEnv.GetType().FullName +
                    "'. Expected a public instance method whose first parameter is string and remaining parameters are optional.");

            int paramCount = doStringMi.GetParameters().Length;
            object[] callArgs = new object[paramCount];
            callArgs[0] = script;
            for (int i = 1; i < paramCount; i++) callArgs[i] = Type.Missing;

            object result = doStringMi.Invoke(luaEnv, callArgs);
            return result as object[] ?? Array.Empty<object>();
        }

        // -------------------------------------------------------------------------
        // Phase 3 (protocol 1.3.0): CsharpReloadScripts
        //
        // Requests a recompile of the Editor's scripts. In Play Mode this is
        // blocked unless `force=true`, in which case we exit → compile → re-enter.
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleCsharpReloadScripts(RequestEnvelope env)
        {
            CsharpReloadScriptsArgs args = ProtocolSerializer.DeserializeArgs<CsharpReloadScriptsArgs>(env);
            return ExecuteCsharpReloadScripts(env.Id, args ?? new CsharpReloadScriptsArgs(),
                getIsPlaying: () => EditorApplication.isPlaying,
                setPlaymode: val => EditorApplication.isPlaying = val,
                requestCompileFn: () =>
                {
#if UNITY_MCP_BRIDGE_STANDALONE
                    // Standalone tests inject a fake; this branch is never reached.
#else
                    UnityEditor.Compilation.CompilationPipeline.RequestScriptCompilation();
#endif
                },
                newGuidFn: () => Guid.NewGuid().ToString());
        }

        public static ResponseEnvelope ExecuteCsharpReloadScripts(
            string id,
            CsharpReloadScriptsArgs args,
            Func<bool> getIsPlaying,
            Action<bool> setPlaymode,
            Action requestCompileFn,
            Func<string> newGuidFn)
        {
            bool force = args != null && args.Force;
            bool wasPlaying = getIsPlaying != null && getIsPlaying();

            if (wasPlaying && !force)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidState,
                    "playmode_blocks_csharp_reload: call with force=true to exit → compile → re-enter.");
            }

            if (wasPlaying && force && setPlaymode != null)
            {
                try { setPlaymode(false); }
                catch (Exception ex)
                {
                    return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                        $"CsharpReloadScripts: failed to exit playmode: {ex.Message}");
                }
            }

            try
            {
                requestCompileFn?.Invoke();
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                    $"CsharpReloadScripts: RequestScriptCompilation threw: {ex.Message}");
            }

            if (wasPlaying && force && setPlaymode != null)
            {
                try { setPlaymode(true); }
                catch (Exception ex)
                {
                    return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                        $"CsharpReloadScripts: failed to re-enter playmode: {ex.Message}");
                }
            }

            string reloadId = newGuidFn != null ? newGuidFn() : Guid.NewGuid().ToString();
            return ProtocolSerializer.OkResponse(id, new CsharpReloadScriptsResult
            {
                ReloadId = reloadId,
                ForceWasUsed = wasPlaying && force,
            });
        }

        // -------------------------------------------------------------------------
        // Phase 3 (protocol 1.3.0): GetPlayState
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleGetPlayState(RequestEnvelope env)
        {
            return ExecuteGetPlayState(env.Id,
                getIsPlaying: () => EditorApplication.isPlaying,
                getIsPaused: () => EditorApplication.isPaused);
        }

        public static ResponseEnvelope ExecuteGetPlayState(
            string id,
            Func<bool> getIsPlaying,
            Func<bool> getIsPaused)
        {
            bool playing = getIsPlaying != null && getIsPlaying();
            bool paused = getIsPaused != null && getIsPaused();
            string state = !playing ? "EditMode" : (paused ? "Paused" : "PlayMode");
            return ProtocolSerializer.OkResponse(id, new GetPlayStateResult { State = state });
        }

        // -------------------------------------------------------------------------
        // Phase 3 (protocol 1.3.0): GetCompileStatus
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleGetCompileStatus(RequestEnvelope env)
        {
            return ExecuteGetCompileStatus(env.Id,
                getHasErrors: () => EditorUtility.scriptCompilationFailed,
                getErrors: () => new List<string>()); // MVP: ring-buffer scraping left for a later WU
        }

        public static ResponseEnvelope ExecuteGetCompileStatus(
            string id,
            Func<bool> getHasErrors,
            Func<List<string>> getErrors)
        {
            bool hasErrors = getHasErrors != null && getHasErrors();
            List<string> errors = (getErrors != null ? getErrors() : null) ?? new List<string>();
            return ProtocolSerializer.OkResponse(id, new GetCompileStatusResult
            {
                HasErrors = hasErrors,
                Errors = errors,
            });
        }

        // -------------------------------------------------------------------------
        // Phase 4 (protocol 1.4.0): LuaEval
        //
        // Generic "run arbitrary Lua in the live xLua VM" command. Reuses the same
        // DefaultLuaDoString seam LuaReloadModule already proves. This single command
        // is the universal key for the no-human verification layer: open panels
        // (PROJECT_RUNTIME.global.ui:open(uid)), verify (return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE(uid)),
        // simulate clicks (node:PROJECT_CLICK_WITH_HIT_TEST(false)), trigger actions
        // (PROJECT_RUNTIME.global.PROJECT_INPUT_SERVICE:PROJECT_TRIGGER_ACTION(path, "Performed")), set input text.
        //
        // The Lua VM only exists in Play Mode; DefaultLuaDoString throws when
        // PROJECT_FACADE.PROJECT_LUA_MANAGER.PROJECT_LUA_VM is null. We map that to a clean INVALID_STATE
        // 'lua_vm_not_running' rather than a raw INTERNAL stack trace.
        // -------------------------------------------------------------------------

        // Substrings that identify the "game/Lua VM not running" family of throws from
        // DefaultLuaDoString so we can return INVALID_STATE instead of INTERNAL.
        private static readonly string[] s_luaVmNotRunningMarkers = new[]
        {
            "PROJECT_FACADE",                 // "PROJECT_NAMESPACE.PROJECT_FACADE not found"
            "PROJECT_LUA_MANAGER",                // "PROJECT_FACADE.PROJECT_LUA_MANAGER is null"
            "PROJECT_LUA_VM",                    // "PROJECT_LUA_MANAGER.PROJECT_LUA_VM is null"
            "Lua runtime not initialized",
            "game must be running",
        };

        private static bool LooksLikeLuaVmNotRunning(string message)
        {
            if (string.IsNullOrEmpty(message))
                return false;
            foreach (string marker in s_luaVmNotRunningMarkers)
            {
                if (message.IndexOf(marker, StringComparison.OrdinalIgnoreCase) >= 0)
                    return true;
            }
            return false;
        }

        private static ResponseEnvelope HandleLuaEval(RequestEnvelope env)
        {
            LuaEvalArgs args = ProtocolSerializer.DeserializeArgs<LuaEvalArgs>(env);
            return ExecuteLuaEval(env.Id, args ?? new LuaEvalArgs(),
                doStringFn: DefaultLuaDoString);
        }

        /// <summary>
        /// Execute LuaEval. <paramref name="doStringFn"/> runs a Lua chunk and returns
        /// the multi-return as a CLR object[]. Delegate-injected so standalone NUnit
        /// tests can supply a fake (including one that throws to exercise the
        /// lua_vm_not_running mapping). Sync: DoString is a blocking main-thread call
        /// that returns immediately.
        /// </summary>
        public static ResponseEnvelope ExecuteLuaEval(
            string id,
            LuaEvalArgs args,
            Func<string, object[]> doStringFn)
        {
            if (args == null || string.IsNullOrWhiteSpace(args.Script))
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidRequest,
                    "LuaEval requires a non-empty 'script'.");
            }
            if (doStringFn == null)
            {
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidState,
                    "lua_vm_not_running: no Lua executor wired (enter Play Mode first).");
            }

            object[] retVals;
            try
            {
                retVals = doStringFn(args.Script) ?? Array.Empty<object>();
            }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                {
                    return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                }
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                    $"LuaEval DoString threw: {ex.Message}");
            }

            var returnValues = new List<string>(retVals.Length);
            foreach (object v in retVals)
                returnValues.Add(v?.ToString());

            return ProtocolSerializer.OkResponse(id, new LuaEvalResult
            {
                Ok = true,
                ReturnCount = retVals.Length,
                ReturnValues = returnValues,
                Raw = retVals.Length > 0 ? retVals[0]?.ToString() : null,
            });
        }

        // -------------------------------------------------------------------------
        // Phase 4 (protocol 1.4.0): Verification session control
        //
        // StartVerificationSession shows the Editor-side hint banner and arms
        // human-takeover detection; EndVerificationSession hides + disarms. Both are
        // sync and run on the main thread (banner is an EditorWindow).
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleStartVerificationSession(RequestEnvelope env)
        {
            StartVerificationSessionArgs args =
                ProtocolSerializer.DeserializeArgs<StartVerificationSessionArgs>(env)
                ?? new StartVerificationSessionArgs();

            double pauseSilenceSeconds = (args.PauseSilenceMs.HasValue && args.PauseSilenceMs.Value > 0)
                ? args.PauseSilenceMs.Value / 1000.0
                : 2.0;
            bool abortOnHigh = args.AbortOnHigh ?? true;
            bool gateScreenshots = args.GateScreenshots ?? true;

            string sessionId = VerificationSession.Start(
                args.Note, pauseSilenceSeconds, abortOnHigh, gateScreenshots);
            return ProtocolSerializer.OkResponse(env.Id, new StartVerificationSessionResult
            {
                SessionId = sessionId,
                Started = true,
            });
        }

        private static ResponseEnvelope HandleEndVerificationSession(RequestEnvelope env)
        {
            EndVerificationSessionArgs args =
                ProtocolSerializer.DeserializeArgs<EndVerificationSessionArgs>(env)
                ?? new EndVerificationSessionArgs();

            int low = VerificationSession.LowEvents;
            int high = VerificationSession.HighEvents;
            bool ended = VerificationSession.End(args.SessionId);

            return ProtocolSerializer.OkResponse(env.Id, new EndVerificationSessionResult
            {
                Ended = ended,
                LowEvents = low,
                HighEvents = high,
            });
        }

        // -------------------------------------------------------------------------
        // Phase 5 (protocol 1.5.0): ListUINodes
        //
        // Read-only inventory of the live PROJECT_UI/PROJECT UI tree via reflection
        // (UnityMCPUIInspector). Synchronous main-thread call — Dispatch already runs
        // on the Editor main thread, where UGUI/RectTransform access is legal.
        // PlayMode-only: the inspector throws UIInspectorNotReadyException (→ InvalidState)
        // when the game/UI isn't running.
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleListUINodes(RequestEnvelope env)
        {
            ListUINodesArgs args = ProtocolSerializer.DeserializeArgs<ListUINodesArgs>(env)
                ?? new ListUINodesArgs();

            bool interactableOnly = !(args.IncludeAll ?? false);
            int maxNodes = args.MaxNodes ?? 0;

            try
            {
                List<UINodeInfo> nodes = UnityMCPUIInspector.ListNodes(
                    args.PanelUid, args.Layers, interactableOnly, maxNodes, args.PathPrefix,
                    out int gvW, out int gvH);

                var dtos = new List<UINodeDto>(nodes.Count);
                foreach (UINodeInfo n in nodes)
                {
                    dtos.Add(new UINodeDto
                    {
                        Index = n.Index,
                        InstanceId = n.InstanceId,
                        PanelUid = n.PanelUid,
                        Layer = n.Layer,
                        Name = n.Name,
                        Path = n.Path,
                        Type = n.Type,
                        Text = n.Text,
                        TextId = n.TextId,
                        Visible = n.Visible,
                        Clickable = n.Clickable,
                        ScreenX = n.ScreenX,
                        ScreenY = n.ScreenY,
                        ScreenW = n.ScreenW,
                        ScreenH = n.ScreenH,
                    });
                }

                return ProtocolSerializer.OkResponse(env.Id, new ListUINodesResult
                {
                    GameViewWidth = gvW,
                    GameViewHeight = gvH,
                    NodeCount = dtos.Count,
                    Nodes = dtos,
                });
            }
            catch (UIInspectorNotReadyException ex)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState, ex.Message);
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"ListUINodes failed: {ex.Message}");
            }
        }

        // -------------------------------------------------------------------------
        // Phase 5 (protocol 1.5.0): ResolveLocalization
        //
        // Resolve localization ids → strings for any language via the editor-only
        // per-id PROJECT_LOCALIZATION_SERVICE.PROJECT_LOCALIZATION_LOOKUP. (NOT the batch variant,
        // which ignores its language arg — confirmed bug.)
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleResolveLocalization(RequestEnvelope env)
        {
            ResolveLocalizationArgs args = ProtocolSerializer.DeserializeArgs<ResolveLocalizationArgs>(env)
                ?? new ResolveLocalizationArgs();

            if (args.Ids == null || args.Ids.Length == 0)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "ResolveLocalization requires a non-empty 'ids' array.");
            }

            try
            {
                Dictionary<int, string> resolved =
                    UnityMCPUIInspector.ResolveLocalization(args.Ids, args.Language);

                var entries = new List<LocalizationEntryDto>(resolved.Count);
                foreach (int id in args.Ids)
                {
                    resolved.TryGetValue(id, out string text);
                    entries.Add(new LocalizationEntryDto { Id = id, Text = text });
                }

                return ProtocolSerializer.OkResponse(env.Id, new ResolveLocalizationResult
                {
                    Language = string.IsNullOrEmpty(args.Language) ? "zh_CN" : args.Language,
                    Entries = entries,
                });
            }
            catch (UIInspectorNotReadyException ex)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState, ex.Message);
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"ResolveLocalization failed: {ex.Message}");
            }
        }

        // -------------------------------------------------------------------------
        // Phase 6 (protocol 1.6.0): OpenPanel
        //
        // Opens a panel by uid and (by default) polls until it's actually visible,
        // resolving the async-open timing issue: PROJECT_RUNTIME.global.ui:open is async (the view
        // is created 1+ frames later), and PROJECT_PANEL_VISIBLE returns nil (NOT false)
        // before the view exists. We run the open + poll loop in the single dispatcher
        // coroutine (no main-thread block) and report a clear terminal state:
        //   - visible=true                              → success
        //   - timeout & isOpen=false                    → refused (PROJECT_CAN_OPEN silent
        //                                                  no-op: locked func / switch
        //                                                  off / platform mismatch)
        //   - timeout & isOpen=true & visible=false     → timedOut (slow/stuck load)
        // PlayMode-only (Lua VM). Write op (open mutates UI state).
        // -------------------------------------------------------------------------

        private static ResponseEnvelope HandleOpenPanel(RequestEnvelope env)
        {
            OpenPanelArgs args = ProtocolSerializer.DeserializeArgs<OpenPanelArgs>(env)
                ?? new OpenPanelArgs();
            if (args.Uid == 0)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "OpenPanel requires a non-zero 'uid'.");
            }

            int timeoutMs = (args.TimeoutMs.HasValue && args.TimeoutMs.Value > 0)
                ? args.TimeoutMs.Value : 5000;
            bool verify = args.Verify ?? true;
            string requestId = env.Id;
            int uid = args.Uid;
            string infoLua = BuildLuaTableLiteral(args.Info);

            // Issue the open() now (synchronously DoString'd). Guard PlayMode/PROJECT_LUA_VM.
            string openScript = infoLua == null
                ? $"PROJECT_RUNTIME.global.ui:open({uid})"
                : $"PROJECT_RUNTIME.global.ui:open({uid}, {infoLua})";
            try
            {
                DefaultLuaDoString(openScript);
            }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                    return ProtocolSerializer.ErrorResponse(requestId, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                return ProtocolSerializer.ErrorResponse(requestId, ErrorCodes.Internal,
                    $"OpenPanel open() threw: {ex.Message}");
            }

            if (!verify)
            {
                return ProtocolSerializer.OkResponse(requestId, new OpenPanelResult
                {
                    Uid = uid, Opened = true, Visible = false, IsOpen = true,
                    TimedOut = false, Refused = false, ElapsedMs = 0,
                });
            }

            // Poll on the single dispatcher coroutine until visible or timeout.
            var op = new OpenPanelOperation(uid, timeoutMs,
                checkVisibleFn: () => LuaBoolResult($"return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_VISIBLE({uid})"),
                checkOpenFn: () => LuaBoolResult($"return PROJECT_RUNTIME.global.ui:PROJECT_PANEL_OPEN({uid})"),
                topPanelFn: () => LuaIntResult("return PROJECT_RUNTIME.global.ui:PROJECT_TOP_PANEL()"),
                nowMsFn: () => EditorApplication.timeSinceStartup * 1000.0);

            UnityMCPCoroutineDispatcher.Enqueue(requestId, () => OpenPanelCoroutine(requestId, op));
            return null; // coroutine sends the response
        }

        private static System.Collections.IEnumerator OpenPanelCoroutine(string requestId, OpenPanelOperation op)
        {
            while (true)
            {
                OpenPanelResult result;
                try { result = op.Tick(); }
                catch (Exception ex)
                {
                    UnityMCPServer.SendResponse(ProtocolSerializer.ErrorResponse(requestId,
                        ErrorCodes.Internal, $"OpenPanel poll threw: {ex.Message}"));
                    yield break;
                }
                if (result != null)
                {
                    UnityMCPServer.SendResponse(ProtocolSerializer.OkResponse(requestId, result));
                    yield break;
                }
                yield return null; // hand the frame back; open lands 1+ frames later
            }
        }

        // Lua bool with nil→false coercion: PROJECT_PANEL_VISIBLE returns nil (not false) before
        // the view is created, which marshals as a null object[] element.
        private static bool LuaBoolResult(string script)
        {
            object[] r;
            try { r = DefaultLuaDoString(script) ?? Array.Empty<object>(); }
            catch { return false; }
            return r.Length >= 1 && r[0] is bool b && b;
        }

        private static int LuaIntResult(string script)
        {
            object[] r;
            try { r = DefaultLuaDoString(script) ?? Array.Empty<object>(); }
            catch { return 0; }
            if (r.Length < 1 || r[0] == null) return 0;
            if (r[0] is int i) return i;
            if (r[0] is long l) return (int)l;
            if (r[0] is double d) return (int)d;
            if (int.TryParse(r[0].ToString(), out int parsed)) return parsed;
            return 0;
        }

        // Build a Lua table literal from a JSON object for open()'s info arg. Supports
        // primitives (string/number/bool); nested objects/arrays are skipped (open infos
        // in this codebase are flat param tables). Returns null for null/empty info.
        private static string BuildLuaTableLiteral(JObject info)
        {
            if (info == null || !info.HasValues)
                return null;
            var sb = new System.Text.StringBuilder("{");
            bool first = true;
            foreach (var prop in info.Properties())
            {
                JToken v = prop.Value;
                string valLua;
                switch (v.Type)
                {
                    case JTokenType.String:
                        valLua = "'" + EscapeSingleQuoted(v.ToObject<string>() ?? "") + "'"; break;
                    case JTokenType.Integer:
                        valLua = v.ToObject<long>().ToString(System.Globalization.CultureInfo.InvariantCulture); break;
                    case JTokenType.Float:
                        valLua = v.ToObject<double>().ToString(System.Globalization.CultureInfo.InvariantCulture); break;
                    case JTokenType.Boolean:
                        valLua = v.ToObject<bool>() ? "true" : "false"; break;
                    default:
                        continue; // skip nested/unsupported
                }
                // Key: only emit identifier-safe keys (avoids ['weird'] complexity).
                string key = prop.Name;
                if (!System.Text.RegularExpressions.Regex.IsMatch(key, "^[A-Za-z_][A-Za-z0-9_]*$"))
                    continue;
                if (!first) sb.Append(", ");
                sb.Append(key).Append(" = ").Append(valLua);
                first = false;
            }
            sb.Append("}");
            return first ? null : sb.ToString(); // null if nothing emitted
        }

        /// <summary>
        /// Poll state machine for OpenPanel — testable via injected clock + check fns.
        /// Returns null while still waiting, or a terminal OpenPanelResult.
        /// </summary>
        public sealed class OpenPanelOperation
        {
            private readonly int _uid;
            private readonly double _timeoutMs;
            private readonly Func<bool> _checkVisibleFn;
            private readonly Func<bool> _checkOpenFn;
            private readonly Func<int> _topPanelFn;
            private readonly Func<double> _nowMsFn;
            private readonly double _startMs;
            private bool _done;

            public OpenPanelOperation(int uid, double timeoutMs,
                Func<bool> checkVisibleFn, Func<bool> checkOpenFn,
                Func<int> topPanelFn, Func<double> nowMsFn)
            {
                _uid = uid;
                _timeoutMs = timeoutMs;
                _checkVisibleFn = checkVisibleFn;
                _checkOpenFn = checkOpenFn;
                _topPanelFn = topPanelFn;
                _nowMsFn = nowMsFn ?? (() => 0);
                _startMs = _nowMsFn();
            }

            public OpenPanelResult Tick()
            {
                if (_done) return null;
                double elapsed = _nowMsFn() - _startMs;

                bool visible = _checkVisibleFn != null && _checkVisibleFn();
                if (visible)
                {
                    _done = true;
                    return new OpenPanelResult
                    {
                        Uid = _uid, Opened = true, Visible = true,
                        IsOpen = true, TopPanelUid = _topPanelFn != null ? _topPanelFn() : 0,
                        TimedOut = false, Refused = false, ElapsedMs = elapsed,
                    };
                }

                if (elapsed < _timeoutMs)
                    return null; // keep waiting

                // Timed out without becoming visible — classify why.
                _done = true;
                bool isOpen = _checkOpenFn != null && _checkOpenFn();
                return new OpenPanelResult
                {
                    Uid = _uid, Opened = true, Visible = false,
                    IsOpen = isOpen, TopPanelUid = _topPanelFn != null ? _topPanelFn() : 0,
                    TimedOut = isOpen,        // open accepted but view never showed → slow/stuck
                    Refused = !isOpen,        // never even marked open → PROJECT_CAN_OPEN silent refusal
                    ElapsedMs = elapsed,
                };
            }
        }

        // -------------------------------------------------------------------------
        // Phase 7 (protocol 1.7.0): simulate suite — main path, all via DoString.
        //
        // Node locator: a panel uid + EITHER refKey (panel PROJECT_NODE_REFERENCE key) OR a
        // transform path relative to the panel root. The panel root PROJECT_WIDGET is
        // PROJECT_RUNTIME.global.ui:PROJECT_FIND_PANEL(uid).view.widget.
        //   refKey: widget.transform:GetComponent('PROJECT_NODE_REFERENCE'):GetRefValue('<key>')
        //   path:   widget:PROJECT_FIND_CHILD('<path>')
        // String fragments are EscapeSingleQuoted here; the Python tools additionally
        // charset-allowlist refKey/actionPath (defense in depth, like lua_reload_module).
        // PlayMode-only; all WRITE ops.
        // -------------------------------------------------------------------------

        // Returns a Lua expression that evaluates to the located node (or nil), given a
        // panel uid and exactly one of refKey/path. Caller wraps it in a script.
        private static string BuildNodeLocatorLua(int panelUid, string refKey, string path)
        {
            string root = $"(function() local c=PROJECT_RUNTIME.global.ui:PROJECT_FIND_PANEL({panelUid}); " +
                          "if not c or not c.view then return nil end; return c.view.widget end)()";
            if (!string.IsNullOrEmpty(refKey))
            {
                // root.transform:GetComponent('PROJECT_NODE_REFERENCE'):GetRefValue('key')
                return $"(function() local w={root}; if not w then return nil end; " +
                       "local objRef=w.transform:GetComponent('PROJECT_NODE_REFERENCE'); " +
                       "if not objRef then return nil end; " +
                       $"return objRef:GetRefValue('{EscapeSingleQuoted(refKey)}') end)()";
            }
            // path
            return $"(function() local w={root}; if not w then return nil end; " +
                   $"return w:PROJECT_FIND_CHILD('{EscapeSingleQuoted(path)}') end)()";
        }

        private static ResponseEnvelope HandleSimulateClick(RequestEnvelope env)
        {
            SimulateClickArgs args = ProtocolSerializer.DeserializeArgs<SimulateClickArgs>(env)
                ?? new SimulateClickArgs();
            if (args.PanelUid == 0 ||
                (string.IsNullOrEmpty(args.RefKey) == string.IsNullOrEmpty(args.Path)))
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "SimulateClick requires panelUid and exactly one of refKey | path.");
            }

            string mode = string.IsNullOrEmpty(args.Mode) ? "raycast" : args.Mode.Trim().ToLowerInvariant();
            if (mode != "raycast" && mode != "force")
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "SimulateClick mode must be 'raycast' or 'force'.");
            }

            // node:PROJECT_CLICK_WITH_HIT_TEST(false) returns bool (self-validates occlusion);
            // PROJECT_CLICK_DIRECT(false) is the force variant (no hit-test, returns nothing).
            string node = BuildNodeLocatorLua(args.PanelUid, args.RefKey, args.Path);
            string script = mode == "raycast"
                ? $"local n={node}; if not n then return 'NO_NODE' end; return n:PROJECT_CLICK_WITH_HIT_TEST(false)"
                : $"local n={node}; if not n then return 'NO_NODE' end; n:PROJECT_CLICK_DIRECT(false); return true";

            object[] r;
            try { r = DefaultLuaDoString(script) ?? Array.Empty<object>(); }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                    return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"SimulateClick threw: {ex.Message}");
            }

            object first = r.Length >= 1 ? r[0] : null;
            if (first is string s && s == "NO_NODE")
            {
                return ProtocolSerializer.OkResponse(env.Id, new SimulateClickResult
                {
                    Clicked = false, Mode = mode, NodeFound = false,
                    Note = "node not found (panel not open, or wrong refKey/path)",
                });
            }
            bool clicked = first is bool b ? b : (mode == "force");
            return ProtocolSerializer.OkResponse(env.Id, new SimulateClickResult
            {
                Clicked = clicked, Mode = mode, NodeFound = true,
                Note = (mode == "raycast" && !clicked)
                    ? "raycast click returned false: node occluded / not interactable / off-screen"
                    : null,
            });
        }

        private static ResponseEnvelope HandleSimulateKey(RequestEnvelope env)
        {
            SimulateKeyArgs args = ProtocolSerializer.DeserializeArgs<SimulateKeyArgs>(env)
                ?? new SimulateKeyArgs();
            if (string.IsNullOrEmpty(args.ActionPath))
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "SimulateKey requires actionPath.");
            }

            // PROJECT_TRIGGER_ACTION(path,'Performed') then 'Canceled' — routes through
            // InnerHandleInputInfo, bypassing the focus/inputEnabled gate.
            string p = EscapeSingleQuoted(args.ActionPath);
            string script =
                $"local ok = PROJECT_RUNTIME.global.PROJECT_INPUT_SERVICE:PROJECT_TRIGGER_ACTION('{p}', 'Performed'); " +
                $"PROJECT_RUNTIME.global.PROJECT_INPUT_SERVICE:PROJECT_TRIGGER_ACTION('{p}', 'Canceled'); return ok";

            object[] r;
            try { r = DefaultLuaDoString(script) ?? Array.Empty<object>(); }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                    return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"SimulateKey threw: {ex.Message}");
            }

            bool triggered = r.Length >= 1 && r[0] is bool b && b;
            return ProtocolSerializer.OkResponse(env.Id, new SimulateKeyResult
            {
                Triggered = triggered, ActionPath = args.ActionPath,
                // PROJECT_TRIGGER_ACTION only fires REAL InputActionAsset actions
                // (playerInput.actions.FindAction). It returns false for virtual
                // PROJECT_HOTKEY_SYSTEM bindings (isVirtual=true, driven by PROJECT_HOTKEY_CALLBACK — e.g.
                // panel close/hotkeys). For those, click the bound button via
                // SimulateClick, or call the panel method via lua_eval.
                Note = triggered ? null
                    : "action not found as a real InputAction — it may be a virtual " +
                      "KeyBinding (panel hotkey). Use simulate_click on the bound button, " +
                      "or lua_eval the panel method instead.",
            });
        }

        private static ResponseEnvelope HandleSetInputText(RequestEnvelope env)
        {
            SetInputTextArgs args = ProtocolSerializer.DeserializeArgs<SetInputTextArgs>(env)
                ?? new SetInputTextArgs();
            if (args.PanelUid == 0 ||
                (string.IsNullOrEmpty(args.RefKey) == string.IsNullOrEmpty(args.Path)) ||
                args.Text == null)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "SetInputText requires panelUid, text, and exactly one of refKey | path.");
            }

            // UInputField: settable '.text'; then ActivateInputField(); then fire the
            // PROJECT_COMMIT_TEXT callback if present so the panel reacts as if the user committed.
            string node = BuildNodeLocatorLua(args.PanelUid, args.RefKey, args.Path);
            string text = EscapeSingleQuoted(args.Text);
            string script =
                $"local f={node}; if not f then return 'NO_NODE' end; " +
                $"f.text='{text}'; " +
                "if f.ActivateInputField then f:ActivateInputField() end; " +
                "if f.PROJECT_COMMIT_TEXT then f.PROJECT_COMMIT_TEXT(f.text) end; return true";

            object[] r;
            try { r = DefaultLuaDoString(script) ?? Array.Empty<object>(); }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                    return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"SetInputText threw: {ex.Message}");
            }

            object first = r.Length >= 1 ? r[0] : null;
            if (first is string s && s == "NO_NODE")
            {
                return ProtocolSerializer.OkResponse(env.Id, new SetInputTextResult
                {
                    Applied = false, NodeFound = false,
                    Note = "input node not found (panel not open, or wrong refKey/path)",
                });
            }
            return ProtocolSerializer.OkResponse(env.Id, new SetInputTextResult
            {
                Applied = true, NodeFound = true,
            });
        }

        // -------------------------------------------------------------------------
        // Phase 8 (protocol 1.8.0): non-UI observe / operate (camera/entity/effect)
        //
        // observe/list run FIXED, AUDITED Lua snippets (UnityMCPWorldObserver) that
        // build a JSON string in-Lua; we parse it back to a JToken so the AI reads
        // structured named fields — no hand-authored Lua, no source-reading = no
        // hallucination. ListEffects reflects EffectManager (no Lua enumerator).
        // All PlayMode-only; the PROJECT_LUA_VM-null throw maps to InvalidState.
        // -------------------------------------------------------------------------

        // Run a snippet that returns a single JSON string; parse to JToken. Maps
        // lua-vm-not-running to InvalidState. Returns an ObserveResult-bearing envelope.
        private static ResponseEnvelope RunObserveSnippet(string id, string luaScript)
        {
            object[] r;
            try { r = DefaultLuaDoString(luaScript) ?? Array.Empty<object>(); }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                    return ProtocolSerializer.ErrorResponse(id, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                return ProtocolSerializer.ErrorResponse(id, ErrorCodes.Internal,
                    $"observe snippet threw: {ex.Message}");
            }
            string json = r.Length >= 1 ? r[0]?.ToString() : null;
            JToken data = ParseJsonSafe(json);
            return ProtocolSerializer.OkResponse(id, new ObserveResult
            {
                Ok = true,
                Data = data,
                Note = data == null ? $"unparseable snippet return: {Truncate(json, 200)}" : null,
            });
        }

        private static JToken ParseJsonSafe(string json)
        {
            if (string.IsNullOrEmpty(json)) return null;
            try { return JToken.Parse(json); } catch { return null; }
        }

        private static string Truncate(string s, int n)
            => string.IsNullOrEmpty(s) ? s : (s.Length <= n ? s : s.Substring(0, n));

        private static ResponseEnvelope HandleGetVerificationSessionState(RequestEnvelope env)
        {
            return ProtocolSerializer.OkResponse(env.Id, new GetVerificationSessionStateResult
            {
                Armed = VerificationSession.IsArmed,
                SessionId = VerificationSession.SessionId,
                AutoStarted = VerificationSession.AutoStarted,
                GateScreenshots = VerificationSession.GateScreenshots,
            });
        }

        private static ResponseEnvelope HandleObserveCamera(RequestEnvelope env)
        {
            ObserveCameraArgs args = ProtocolSerializer.DeserializeArgs<ObserveCameraArgs>(env)
                ?? new ObserveCameraArgs();
            JToken data;
            try { data = BuildCameraSnapshotToken(args.TargetPos); }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                    return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"observe camera threw: {ex.Message}");
            }
            return ProtocolSerializer.OkResponse(env.Id, new ObserveResult
            {
                Ok = true,
                Data = data,
                Note = data == null ? "camera snapshot unparseable" : null,
            });
        }

        private static ResponseEnvelope HandleListEntities(RequestEnvelope env)
        {
            ListEntitiesArgs args = ProtocolSerializer.DeserializeArgs<ListEntitiesArgs>(env)
                ?? new ListEntitiesArgs();
            // Pre-escape filter literals to single-quoted Lua strings (or null).
            string atLit = string.IsNullOrEmpty(args.ActorType)
                ? null : "'" + EscapeSingleQuoted(args.ActorType) + "'";
            string cnLit = string.IsNullOrEmpty(args.ClassName)
                ? null : "'" + EscapeSingleQuoted(args.ClassName) + "'";
            int maxN = (args.MaxEntities.HasValue && args.MaxEntities.Value > 0)
                ? Math.Min(args.MaxEntities.Value, 2000) : 500;
            string script = UnityMCPWorldObserver.BuildListEntitiesLua(
                atLit, cnLit, args.TemplateId, args.Summary ?? false, maxN);
            return RunObserveSnippet(env.Id, script);
        }

        private static ResponseEnvelope HandleObserveEntity(RequestEnvelope env)
        {
            ObserveEntityArgs args = ProtocolSerializer.DeserializeArgs<ObserveEntityArgs>(env)
                ?? new ObserveEntityArgs();
            string resolver;
            if (!string.IsNullOrEmpty(args.Id))
                resolver = $"PROJECT_RUNTIME.PROJECT_ENTITY_BY_ID('{EscapeSingleQuoted(args.Id)}')";
            else if (args.ActorId.HasValue)
                resolver = $"PROJECT_RUNTIME.PROJECT_ENTITY_BY_ACTOR({args.ActorId.Value})";
            else if (args.TemplateId.HasValue)
                resolver = $"(function() local t=PROJECT_RUNTIME.PROJECT_ENTITIES_BY_TEMPLATE({args.TemplateId.Value}); for _,e in pairs(t or {{}}) do return e end return nil end)()";
            else
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "ObserveEntity requires one of id | actorId | templateId.");
            string script = UnityMCPWorldObserver.BuildObserveEntityLua(resolver);
            return RunObserveSnippet(env.Id, script);
        }

        private static ResponseEnvelope HandleListEffects(RequestEnvelope env)
        {
            // Reflection path (no Lua snippet) — guard PlayMode via the same exception.
            try
            {
                string json = UnityMCPWorldObserver.ListEffectsJson();
                return ProtocolSerializer.OkResponse(env.Id, new ObserveResult
                {
                    Ok = true, Data = ParseJsonSafe(json),
                });
            }
            catch (UIInspectorNotReadyException ex)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                    $"lua_vm_not_running: {ex.Message}");
            }
            catch (Exception ex)
            {
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"ListEffects failed: {ex.Message}");
            }
        }

        private static ResponseEnvelope HandleObserveEffect(RequestEnvelope env)
        {
            ObserveEffectArgs args = ProtocolSerializer.DeserializeArgs<ObserveEffectArgs>(env)
                ?? new ObserveEffectArgs();
            string script = UnityMCPWorldObserver.BuildObserveEffectLua(args.GeneratorId, args.EffectId);
            return RunObserveSnippet(env.Id, script);
        }

        // ObserveWindow: poll a whitelisted boolean/number Lua expr each frame over a
        // window. Async via the single dispatcher coroutine (no main-thread block).
        private static ResponseEnvelope HandleObserveWindow(RequestEnvelope env)
        {
            ObserveWindowArgs args = ProtocolSerializer.DeserializeArgs<ObserveWindowArgs>(env)
                ?? new ObserveWindowArgs();
            if (string.IsNullOrEmpty(args.LuaExpr))
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "ObserveWindow requires luaExpr.");
            if (!IsSafeLuaExpr(args.LuaExpr))
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "ObserveWindow luaExpr must be a pure expression (no '=' assignment, no ';', no 'function'/'do'/'end').");

            int intervalMs = (args.IntervalMs.HasValue && args.IntervalMs.Value > 0) ? args.IntervalMs.Value : 50;
            int durationMs = (args.DurationMs.HasValue && args.DurationMs.Value > 0) ? args.DurationMs.Value : 2000;
            durationMs = Math.Min(durationMs, 120000);

            string requestId = env.Id;
            string evalScript = "return (function() local ok,v = pcall(function() return (" + args.LuaExpr + ") end); " +
                                "if not ok then return nil end; if type(v)=='boolean' then return v and 1 or 0 end; return tonumber(v) end)()";
            var op = new ObserveWindowOperation(intervalMs, durationMs,
                sampleFn: () => LuaNumberOrNull(evalScript),
                nowMsFn: () => EditorApplication.timeSinceStartup * 1000.0);

            UnityMCPCoroutineDispatcher.Enqueue(requestId, () => ObserveWindowCoroutine(requestId, op));
            return null;
        }

        private static System.Collections.IEnumerator ObserveWindowCoroutine(string requestId, ObserveWindowOperation op)
        {
            while (true)
            {
                ObserveWindowResult result;
                try { result = op.Tick(); }
                catch (Exception ex)
                {
                    UnityMCPServer.SendResponse(ProtocolSerializer.ErrorResponse(requestId,
                        ErrorCodes.Internal, $"ObserveWindow tick threw: {ex.Message}"));
                    yield break;
                }
                if (result != null)
                {
                    UnityMCPServer.SendResponse(ProtocolSerializer.OkResponse(requestId, result));
                    yield break;
                }
                yield return null;
            }
        }

        // Returns the number (bool coerced to 1/0) or NaN if nil/error.
        private static double LuaNumberOrNull(string script)
        {
            object[] r;
            try { r = DefaultLuaDoString(script) ?? Array.Empty<object>(); }
            catch { return double.NaN; }
            if (r.Length < 1 || r[0] == null) return double.NaN;
            if (r[0] is double d) return d;
            if (r[0] is float f) return f;
            if (r[0] is long l) return l;
            if (r[0] is int i) return i;
            if (r[0] is bool b) return b ? 1 : 0;
            if (double.TryParse(r[0].ToString(), out double p)) return p;
            return double.NaN;
        }

        // Whitelist: pure expression only. Reject assignment/statements/control words.
        private static bool IsSafeLuaExpr(string expr)
        {
            if (string.IsNullOrEmpty(expr) || expr.Length > 400) return false;
            if (expr.IndexOf(';') >= 0) return false;
            // a single '=' that is not part of ==, ~=, <=, >= is an assignment.
            for (int i = 0; i < expr.Length; i++)
            {
                if (expr[i] != '=') continue;
                char prev = i > 0 ? expr[i - 1] : '\0';
                char next = i + 1 < expr.Length ? expr[i + 1] : '\0';
                bool partOfComparison = prev == '=' || prev == '~' || prev == '<' || prev == '>'
                                        || next == '=';
                if (!partOfComparison) return false;
            }
            foreach (string kw in s_observeWindowBannedWords)
            {
                if (System.Text.RegularExpressions.Regex.IsMatch(expr, "\\b" + kw + "\\b"))
                    return false;
            }
            return true;
        }

        private static readonly string[] s_observeWindowBannedWords =
            { "function", "do", "end", "while", "for", "repeat", "until", "goto",
              "os", "io", "require", "loadstring", "load", "dofile", "DoString" };

        /// <summary>
        /// Pure poll state machine for ObserveWindow — testable with injected clock +
        /// sample fn. Returns null while sampling, or the terminal result after duration.
        /// </summary>
        public sealed class ObserveWindowOperation
        {
            private readonly double _intervalMs;
            private readonly double _durationMs;
            private readonly Func<double> _sampleFn;   // returns value or NaN
            private readonly Func<double> _nowMsFn;
            private readonly double _startMs;
            private double _nextSampleMs;
            private bool _everTrue;
            private double _firstTrueMs = -1;
            private double _lastTrueMs = -1;
            private double _peak = double.NaN;
            private readonly List<ObserveWindowSample> _samples = new List<ObserveWindowSample>();
            private int _attempted;   // total sample attempts (incl. invalid/NaN)
            private bool _done;

            public ObserveWindowOperation(double intervalMs, double durationMs,
                Func<double> sampleFn, Func<double> nowMsFn)
            {
                _intervalMs = intervalMs > 0 ? intervalMs : 50;
                _durationMs = durationMs;
                _sampleFn = sampleFn;
                _nowMsFn = nowMsFn ?? (() => 0);
                _startMs = _nowMsFn();
                _nextSampleMs = _startMs; // sample immediately on first tick
            }

            public ObserveWindowResult Tick()
            {
                if (_done) return null;
                double now = _nowMsFn();
                if (now >= _nextSampleMs)
                {
                    _nextSampleMs = now + _intervalMs;
                    _attempted++;
                    double v = _sampleFn != null ? _sampleFn() : double.NaN;
                    double t = now - _startMs;
                    if (!double.IsNaN(v))
                    {
                        _samples.Add(new ObserveWindowSample { T = t, Value = v });
                        if (double.IsNaN(_peak) || v > _peak) _peak = v;
                        if (v != 0)
                        {
                            _everTrue = true;
                            if (_firstTrueMs < 0) _firstTrueMs = t;
                            _lastTrueMs = t;
                        }
                    }
                }
                if (now - _startMs < _durationMs)
                    return null;

                _done = true;
                // If we sampled but NOTHING evaluated, the expression itself is broken
                // (nil/error every tick) — report it, don't silently return an empty,
                // pass-looking window.
                string exprError = (_attempted > 0 && _samples.Count == 0)
                    ? "luaExpr never evaluated to a number/boolean across all "
                      + _attempted + " samples (it errored or was always nil — check the path)"
                    : null;
                return new ObserveWindowResult
                {
                    EverTrue = _everTrue,
                    FirstTrueMs = _firstTrueMs,
                    LastTrueMs = _lastTrueMs,
                    Peak = double.IsNaN(_peak) ? 0 : _peak,
                    SampleCount = _samples.Count,
                    ValidSampleCount = _samples.Count,
                    ElapsedMs = now - _startMs,
                    Samples = _samples,
                    ExprError = exprError,
                };
            }
        }

        // OperateCamera: map a whitelisted action to a PROJECT_RUNTIME.game.camera method, with a
        // before/after ObserveCamera snapshot bracketing it (write op; auto-evidence).
        private static ResponseEnvelope HandleOperateCamera(RequestEnvelope env)
        {
            OperateCameraArgs args = ProtocolSerializer.DeserializeArgs<OperateCameraArgs>(env)
                ?? new OperateCameraArgs();
            if (string.IsNullOrEmpty(args.Action))
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest,
                    "OperateCamera requires action.");

            string opCall = BuildCameraOperateCall(args, out string err);
            if (opCall == null)
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidRequest, err);

            JToken before = TryBuildCameraSnapshotToken();

            // Run the operation INSIDE a Lua pcall so a Lua-runtime error (e.g. an
            // undeclared global the operation's call chain touches) is captured and
            // reported as opError instead of being silently swallowed — the old code
            // C#-try/catch'd this but xLua surfaces such errors as a returned status,
            // not always a CLR exception, so a failed op looked "applied".
            string guarded = "local ok,e = pcall(function() " + opCall + " end); " +
                             "if ok then return 'OK' else return 'ERR:'..tostring(e) end";
            string opError = null;
            try
            {
                object[] r = DefaultLuaDoString(guarded) ?? Array.Empty<object>();
                string status = r.Length >= 1 ? r[0]?.ToString() : null;
                if (status != null && status.StartsWith("ERR:", StringComparison.Ordinal))
                    opError = status.Substring(4);
            }
            catch (Exception ex)
            {
                if (LooksLikeLuaVmNotRunning(ex.Message))
                    return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.InvalidState,
                        $"lua_vm_not_running: {ex.Message}");
                return ProtocolSerializer.ErrorResponse(env.Id, ErrorCodes.Internal,
                    $"OperateCamera '{args.Action}' threw: {ex.Message}");
            }

            JToken after = TryBuildCameraSnapshotToken();
            // "applied" is proven by EVIDENCE: the Lua call didn't error AND the
            // camera state actually changed (before != after). A no-op that errored
            // or produced no delta is NOT applied — no hallucinated success.
            bool noError = opError == null;
            bool changed = before != null && after != null && !JToken.DeepEquals(before, after);
            string note = opError != null
                ? $"camera op errored: {opError}"
                : (!changed ? "camera op ran without error but produced no observable state change "
                            + "(the action may not apply in the current camera mode/scene)" : null);
            return ProtocolSerializer.OkResponse(env.Id, new OperateCameraResult
            {
                Action = args.Action,
                Applied = noError && changed,
                Before = before, After = after, Note = note,
            });
        }

        // Build the camera snapshot as a JToken, MERGING two sources:
        //   1. the fixed Lua snapshot (everything xLua exports), and
        //   2. C# reflection over the live CameraManager for members xLua does NOT
        //      export (timeScale field, worldCameraEnabled property) — read directly
        //      because the bridge runs as C# inside the Editor.
        // The reflected values OVERRIDE the Lua snapshot's null placeholders, so the
        // before/after evidence delta can actually see e.g. timeScale 1.0 → 0.5.
        // Throws (VM-not-running etc.) propagate to the caller.
        private static JToken BuildCameraSnapshotToken(double[] targetPos)
        {
            string snapshot = UnityMCPWorldObserver.BuildCameraSnapshotLua(targetPos);
            object[] r = DefaultLuaDoString(snapshot) ?? Array.Empty<object>();
            JToken lua = ParseJsonSafe(r.Length >= 1 ? r[0]?.ToString() : null);

            JToken reflected = ParseJsonSafe(UnityMCPWorldObserver.CameraReflectionFieldsJson());
            if (lua is JObject luaObj && reflected is JObject refObj)
            {
                // Overlay reflected members (real values) over the Lua nulls.
                foreach (var prop in refObj.Properties())
                    luaObj[prop.Name] = prop.Value;
            }
            return lua;
        }

        // Same merge, but never throws — used to bracket the operation with before/after
        // evidence where a transient read failure must not abort the write op.
        private static JToken TryBuildCameraSnapshotToken()
        {
            try { return BuildCameraSnapshotToken(null); }
            catch { return null; }
        }

        // Whitelisted camera operations → PROJECT_RUNTIME.game.camera method calls. Only safe,
        // well-understood operations are exposed (NOT arbitrary methods).
        private static string BuildCameraOperateCall(OperateCameraArgs a, out string err)
        {
            err = null;
            double v = a.Value ?? 0;
            string vs = v.ToString(System.Globalization.CultureInfo.InvariantCulture);
            switch (a.Action.Trim().ToLowerInvariant())
            {
                case "zoom":            return $"PROJECT_RUNTIME.game.camera:zoom({vs})";
                case "settimescale":    return $"PROJECT_RUNTIME.game.camera:setTimeScale({vs})";
                case "setworldcameraenable":
                    return $"PROJECT_RUNTIME.game.camera:setWorldCameraEnable({(a.Flag ?? true).ToString().ToLowerInvariant()}, 'mcp')";
                case "enablephoto":
                    return $"PROJECT_RUNTIME.game.camera:enablePhoto({(a.Flag ?? true).ToString().ToLowerInvariant()})";
                case "settargetplayer":
                    if (!a.ActorId.HasValue) { err = "settargetplayer requires actorId"; return null; }
                    return $"(function() local e=PROJECT_RUNTIME.PROJECT_ENTITY_BY_ACTOR({a.ActorId.Value}); if e then PROJECT_RUNTIME.game.camera:setTargetPlayer(e) end end)()";
                case "focus":
                    if (!a.ActorId.HasValue) { err = "focus requires actorId"; return null; }
                    return $"(function() local e=PROJECT_RUNTIME.PROJECT_ENTITY_BY_ACTOR({a.ActorId.Value}); if e then PROJECT_RUNTIME.game.camera:PROJECT_FOCUS_TARGET(e) end end)()";
                default:
                    err = $"unknown camera action '{a.Action}'. Allowed: zoom, setTimeScale, setWorldCameraEnable, enablePhoto, setTargetPlayer, focus.";
                    return null;
            }
        }

        // ===== Phase 9: optional performance profiler =====================

        private static ResponseEnvelope HandleProfilerCapabilities(RequestEnvelope env)
        {
            ProfilerCapabilitiesResult result = ExecuteProfilerCapabilities(
                isPlayMode: EditorApplication.isPlaying,
                recordersAvailable: ProbeRecordersAvailable,
                memoryProfilerInstalled: IsMemoryProfilerInstalled,
                availableMetricsProbe: ProbeAvailableMetrics);
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        // Pure: feasibility decision from injected facts (C1). availableMetricsProbe
        // returns the per-mode list of counters that actually opened valid (§6.3);
        // it is only invoked on the analyzable path.
        public static ProfilerCapabilitiesResult ExecuteProfilerCapabilities(
            bool isPlayMode,
            System.Func<bool> recordersAvailable,
            System.Func<bool> memoryProfilerInstalled,
            System.Func<JObject> availableMetricsProbe = null)
        {
            var r = new ProfilerCapabilitiesResult
            {
                PlayMode = isPlayMode,
                MemoryProfilerInstalled = memoryProfilerInstalled != null && memoryProfilerInstalled(),
            };
            if (!isPlayMode)
            {
                r.Analyzable = false;
                r.Reason = "not in PlayMode";
                r.Remedy = "enter_playmode first (most ProfilerRecorder counters read 0 in EditMode)";
                r.RecordersAvailable = false;
                return r;
            }
            bool recorders = recordersAvailable != null && recordersAvailable();
            r.RecordersAvailable = recorders;
            if (!recorders)
            {
                r.Analyzable = false;
                r.Reason = "ProfilerRecorder counters unavailable";
                r.Remedy = "ensure the Profiler module is active; some counters need a built Player";
                return r;
            }
            r.Analyzable = true;
            r.AvailableMetrics = availableMetricsProbe != null ? availableMetricsProbe() : null;
            return r;
        }

        // Live probe: can we open at least one core recorder?
        private static bool ProbeRecordersAvailable()
        {
            try
            {
                using (var rec = ProfilerRecorder.StartNew(
                    ProfilerCategory.Internal, "Main Thread"))
                {
                    return rec.Valid;
                }
            }
            catch { return false; }
        }

        // Live probe (§6.3): which cpu/memory counters actually open valid, so the
        // tool reports what it CAN measure and silently omits the rest (never fakes).
        private static JObject ProbeAvailableMetrics()
        {
            var result = new JObject();
            foreach (var mode in new[] { "cpu", "memory" })
            {
                var valid = new JArray();
                foreach (string counter in CountersForMode(mode))
                {
                    ProfilerRecorder rec;
                    try { rec = OpenRecorder(counter); } catch { continue; }
                    bool ok = rec.Valid;
                    rec.Dispose();
                    if (ok) valid.Add(counter);
                }
                result[mode] = valid;
            }
            return result;
        }

        // Reflection probe so the bridge compiles without com.unity.memoryprofiler.
        private static bool IsMemoryProfilerInstalled()
        {
            try
            {
                System.Type t = System.Type.GetType(
                    "Unity.MemoryProfiler.MemoryProfiler, Unity.MemoryProfiler");
                return t != null;
            }
            catch { return false; }
        }

        // ----- Restore-on-abort guard (spec §6.2 note) ---------------------------
        // StopCoroutine (AbortAll) does NOT run IEnumerator finally blocks, so a
        // killed ProfilerSample would strand the Editor at a Low/30fps scenario.
        // The active scenario's restore is stashed here and replayed defensively
        // from OnBeforeAssemblyReload / playmode-exit and at the next sample's top.
        private static ScenarioKnobs s_pendingKnobs;
        private static ScenarioSnapshot s_pendingSnapshot;
        private static bool s_hasPendingRestore;

        public static bool HasPendingRestore => s_hasPendingRestore;

        public static void ArmPendingRestore(ScenarioKnobs knobs, ScenarioSnapshot snap)
        {
            s_pendingKnobs = knobs;
            s_pendingSnapshot = snap;
            s_hasPendingRestore = true;
        }

        public static void RunPendingRestoreIfAny()
        {
            if (!s_hasPendingRestore) return;
            try { RestoreScenarioSnapshot(s_pendingKnobs, s_pendingSnapshot); }
            catch { /* best-effort */ }
            finally
            {
                s_hasPendingRestore = false;
                s_pendingKnobs = null;
            }
        }

        public static void ResetPendingRestoreForTest()
        {
            s_hasPendingRestore = false;
            s_pendingKnobs = null;
        }

        // Pure: decide available/unavailable from injected facts (C5).
        public static MemorySnapshotResult ExecuteMemorySnapshot(
            bool packageInstalled, System.Func<(string path, long sizeBytes)> captureFn)
        {
            if (!packageInstalled)
            {
                return new MemorySnapshotResult
                {
                    Available = false,
                    Reason = "com.unity.memoryprofiler not installed",
                    Remedy = "Install via Package Manager to enable heap snapshots",
                };
            }
            var (path, size) = captureFn();
            return new MemorySnapshotResult { Available = true, Path = path, SizeBytes = size };
        }

        private static ResponseEnvelope HandleMemorySnapshot(RequestEnvelope env)
        {
            MemorySnapshotResult result = ExecuteMemorySnapshot(
                IsMemoryProfilerInstalled(),
                captureFn: CaptureMemorySnapshotToTemp);
            return ProtocolSerializer.OkResponse(env.Id, result);
        }

        // Reflection capture so the bridge compiles without the package. Returns a
        // temp .snap path (the multi-MB payload never enters the LLM context).
        private static (string, long) CaptureMemorySnapshotToTemp()
        {
            string dir = System.Environment.GetEnvironmentVariable("UNITY_MCP_PROFILER_SNAPSHOT_DIR");
            if (string.IsNullOrEmpty(dir)) dir = System.IO.Path.GetTempPath();
            string path = System.IO.Path.Combine(dir, "unitymcp_mem_" + System.Guid.NewGuid().ToString("N") + ".snap");
            try
            {
                System.Type t = System.Type.GetType(
                    "Unity.MemoryProfiler.MemoryProfiler, Unity.MemoryProfiler");
                var m = t?.GetMethod("TakeSnapshot", new[] { typeof(string), typeof(System.Action<string, bool>) });
                m?.Invoke(null, new object[] { path, null });
                long size = System.IO.File.Exists(path) ? new System.IO.FileInfo(path).Length : 0L;
                return (path, size);
            }
            catch { return (path, 0L); }
        }

        private static ResponseEnvelope HandleProfilerSample(RequestEnvelope env)
        {
            ProfilerSampleArgs args = ProtocolSerializer.DeserializeArgs<ProfilerSampleArgs>(env)
                ?? new ProfilerSampleArgs();
            string requestId = env.Id;
            string mode = string.IsNullOrEmpty(args.Mode) ? "cpu" : args.Mode;
            int frames = (args.SampleFrames.HasValue && args.SampleFrames.Value > 0)
                ? args.SampleFrames.Value : 120;

            // Defensive: if a prior sample was killed mid-flight, restore first.
            RunPendingRestoreIfAny();

            UnityMCPCoroutineDispatcher.Enqueue(requestId,
                () => ProfilerSampleCoroutine(requestId, args.Scenario, mode, frames));
            return null;
        }

        private static System.Collections.IEnumerator ProfilerSampleCoroutine(
            string requestId, Newtonsoft.Json.Linq.JObject scenario, string mode, int frames)
        {
            string label = scenario?["label"]?.ToString() ?? "current";
            int warmup = scenario?["warmup_frames"]?.ToObject<int>() ?? 30;

            var knobs = LiveScenarioKnobs();
            ScenarioSnapshot snap = CaptureScenarioSnapshot(knobs);
            ArmPendingRestore(knobs, snap);

            // Apply scenario knobs (quality level / fps / resolution).
            int? quality = ResolveQualityLevel(scenario);
            int? targetFps = scenario?["target_fps"]?.ToObject<int?>();
            (int? w, int? h) = ResolveResolution(scenario);
            ApplyScenario(knobs, quality, targetFps, w, h);
            ApplyQualityOverrides(scenario);
            RunGameStateLua(scenario);

            for (int i = 0; i < warmup; i++) yield return null;

            List<string> counters = CountersForMode(mode);
            var recorders = new Dictionary<string, ProfilerRecorder>();
            var series = new Dictionary<string, List<double>>();
            // Track whether each time counter reports nanoseconds, so the FPS
            // derivation converts correctly rather than assuming ns (§6.3).
            var isTimeNs = new Dictionary<string, bool>();
            var metrics = new Dictionary<string, ProfilerMetricAgg>();
            try
            {
                foreach (string c in counters)
                {
                    ProfilerRecorder rec;
                    try { rec = OpenRecorder(c); } catch { continue; }
                    if (rec.Valid)
                    {
                        recorders[c] = rec;
                        series[c] = new List<double>();
                        isTimeNs[c] = rec.UnitType == ProfilerMarkerDataUnit.TimeNanoseconds;
                    }
                    else
                    {
                        rec.Dispose();  // invalid recorder still holds a native handle — release it
                    }
                }

                for (int f = 0; f < frames; f++)
                {
                    foreach (var kv in recorders)
                    {
                        double v = kv.Value.LastValue;
                        series[kv.Key].Add(v);
                    }
                    yield return null;
                }
            }
            finally
            {
                // Runs on normal completion AND on AbortAll's StopCoroutine, so open
                // ProfilerRecorder native handles are never leaked by a killed sample.
                foreach (var kv in recorders) kv.Value.Dispose();
            }

            foreach (var kv in series) metrics[kv.Key] = AggregateMetric(kv.Value);
            AddDerivedMetrics(mode, metrics, isTimeNs);  // fps from frame time, unit-aware

            // Restore BEFORE responding; disarm the pending guard.
            RunPendingRestoreIfAny();

            var result = new ProfilerSampleResult
            {
                Label = label, Mode = mode, FramesSampled = frames, Metrics = metrics,
                Warning = metrics.Count == 0 ? "no counters were available for this mode" : null,
            };
            UnityMCPServer.SendResponse(ProtocolSerializer.OkResponse(requestId, result));
        }

        private static ScenarioKnobs LiveScenarioKnobs() => new ScenarioKnobs
        {
            GetQuality = () => QualitySettings.GetQualityLevel(),
            SetQuality = v => QualitySettings.SetQualityLevel(v, true),
            GetTargetFps = () => Application.targetFrameRate,
            SetTargetFps = v => Application.targetFrameRate = v,
            GetGameView = () => CurrentGameViewSize(),
            SetGameView = (w, h) => TryApplyGameViewSize(w, h),
        };

        private static (int, int) CurrentGameViewSize()
        {
            try { Vector2 r = UnityEditor.Handles.GetMainGameViewSize(); return ((int)r.x, (int)r.y); }
            catch { return (0, 0); }
        }

        private static int? ResolveQualityLevel(Newtonsoft.Json.Linq.JObject sc)
        {
            var tok = sc?["quality_level"];
            if (tok == null) return null;
            if (tok.Type == Newtonsoft.Json.Linq.JTokenType.Integer) return tok.ToObject<int>();
            string name = tok.ToString();
            string[] names = QualitySettings.names;
            for (int i = 0; i < names.Length; i++)
                if (string.Equals(names[i], name, System.StringComparison.OrdinalIgnoreCase)) return i;
            return null;
        }

        private static (int?, int?) ResolveResolution(Newtonsoft.Json.Linq.JObject sc)
        {
            var arr = sc?["resolution"] as Newtonsoft.Json.Linq.JArray;
            if (arr != null && arr.Count == 2) return (arr[0].ToObject<int>(), arr[1].ToObject<int>());
            return (null, null);
        }

        private static void ApplyQualityOverrides(Newtonsoft.Json.Linq.JObject sc)
        {
            var ov = sc?["quality_overrides"] as Newtonsoft.Json.Linq.JObject;
            if (ov == null) return;
            foreach (var p in ov)
            {
                try
                {
                    switch (p.Key)
                    {
                        case "shadowDistance":    QualitySettings.shadowDistance = p.Value.ToObject<float>(); break;
                        case "lodBias":           QualitySettings.lodBias = p.Value.ToObject<float>(); break;
                        // legacy JSON key 'masterTextureLimit' drives the renamed 2022.2+ property
                        case "masterTextureLimit":QualitySettings.globalTextureMipmapLimit = p.Value.ToObject<int>(); break;
                        case "pixelLightCount":   QualitySettings.pixelLightCount = p.Value.ToObject<int>(); break;
                        case "antiAliasing":      QualitySettings.antiAliasing = p.Value.ToObject<int>(); break;
                        case "shadowResolution":  QualitySettings.shadowResolution = (ShadowResolution)p.Value.ToObject<int>(); break;
                        case "shadowCascades":    QualitySettings.shadowCascades = p.Value.ToObject<int>(); break;
                        case "vSyncCount":        QualitySettings.vSyncCount = p.Value.ToObject<int>(); break;
                    }
                }
                catch { /* skip a knob that won't take */ }
            }
        }

        private static void RunGameStateLua(Newtonsoft.Json.Linq.JObject sc)
        {
            string lua = sc?["game_state_lua"]?.ToString();
            if (string.IsNullOrEmpty(lua)) return;
            try { DefaultLuaDoString(lua); } catch { /* agent owns this trust boundary */ }
        }

        private static List<string> CountersForMode(string mode)
        {
            var cpu = new List<string> {
                "Main Thread", "Render Thread", "CPU Total Frame Time", "Draw Calls Count",
                "SetPass Calls Count", "Batches Count", "Triangles Count",
                "Vertices Count", "GC Allocated In Frame",
            };
            var mem = new List<string> {
                "Total Reserved Memory", "Total Used Memory", "GC Reserved Memory",
                "GC Used Memory", "Texture Memory", "Mesh Memory",
                "Material Count", "System Used Memory",
            };
            if (mode == "memory") return mem;
            if (mode == "both") { cpu.AddRange(mem); return cpu; }
            return cpu;
        }

        private static ProfilerRecorder OpenRecorder(string counter)
        {
            // Try the common categories; the first valid one wins.
            foreach (var cat in new[] {
                ProfilerCategory.Internal, ProfilerCategory.Render,
                ProfilerCategory.Memory, ProfilerCategory.Scripts })
            {
                var rec = ProfilerRecorder.StartNew(cat, counter);
                if (rec.Valid) return rec;
                rec.Dispose();
            }
            return ProfilerRecorder.StartNew(ProfilerCategory.Internal, counter);
        }

        private static void AddDerivedMetrics(
            string mode, Dictionary<string, ProfilerMetricAgg> metrics, Dictionary<string, bool> isTimeNs)
        {
            // FPS = 1000 / frame time(ms). Convert the frame-time counter to ms based
            // on its REPORTED unit (ns vs already-ms), not a fixed assumption (§6.3).
            if (mode == "memory") return;
            string key = metrics.ContainsKey("CPU Total Frame Time") ? "CPU Total Frame Time"
                       : (metrics.ContainsKey("Main Thread") ? "Main Thread" : null);
            if (key == null) return;
            double avg = metrics[key].Avg;
            bool ns = isTimeNs != null && isTimeNs.TryGetValue(key, out bool b) && b;
            double avgMs = ns ? avg / 1_000_000.0 : avg;  // ns→ms only when the counter is ns
            if (avgMs <= 0) return;
            double fps = 1000.0 / avgMs;
            metrics["fps"] = new ProfilerMetricAgg { Min = fps, Max = fps, Avg = fps, Median = fps, P95 = fps, Samples = metrics[key].Samples };
            metrics["frameTimeMs"] = new ProfilerMetricAgg { Min = avgMs, Max = avgMs, Avg = avgMs, Median = avgMs, P95 = avgMs, Samples = metrics[key].Samples };
        }

        // ----- Scenario knob plumbing (pure; Editor APIs injected as delegates) -----
        // public so EditMode tests (C2) round-trip apply→restore with simulated knobs.
        public sealed class ScenarioKnobs
        {
            public System.Func<int> GetQuality;
            public System.Action<int> SetQuality;
            public System.Func<int> GetTargetFps;
            public System.Action<int> SetTargetFps;
            public System.Func<(int, int)> GetGameView;
            public System.Action<int, int> SetGameView;
        }

        public struct ScenarioSnapshot
        {
            public int Quality;
            public int TargetFps;
            public int GvWidth;
            public int GvHeight;
        }

        public static ScenarioSnapshot CaptureScenarioSnapshot(ScenarioKnobs k)
        {
            var (w, h) = k.GetGameView();
            return new ScenarioSnapshot
            {
                Quality = k.GetQuality(),
                TargetFps = k.GetTargetFps(),
                GvWidth = w,
                GvHeight = h,
            };
        }

        // null override → leave that knob untouched.
        public static void ApplyScenario(
            ScenarioKnobs k, int? quality, int? targetFps, int? width, int? height)
        {
            if (quality.HasValue) k.SetQuality(quality.Value);
            if (targetFps.HasValue) k.SetTargetFps(targetFps.Value);
            if (width.HasValue && height.HasValue) k.SetGameView(width.Value, height.Value);
        }

        public static void RestoreScenarioSnapshot(ScenarioKnobs k, ScenarioSnapshot s)
        {
            k.SetQuality(s.Quality);
            k.SetTargetFps(s.TargetFps);
            k.SetGameView(s.GvWidth, s.GvHeight);
        }

        // Pure aggregation over a per-frame sample vector. public static so the
        // EditMode test (C3) drives it without a live Editor. p95 uses nearest-rank.
        public static ProfilerMetricAgg AggregateMetric(List<double> samples)
        {
            var agg = new ProfilerMetricAgg();
            if (samples == null || samples.Count == 0)
                return agg;  // all-zero, Samples=0

            var sorted = new List<double>(samples);
            sorted.Sort();
            int n = sorted.Count;

            double sum = 0;
            for (int i = 0; i < n; i++) sum += sorted[i];

            agg.Min = sorted[0];
            agg.Max = sorted[n - 1];
            agg.Avg = sum / n;
            agg.Median = (n % 2 == 1)
                ? sorted[n / 2]
                : (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0;
            // Nearest-rank p95: index = ceil(0.95*n) - 1, clamped.
            int rank = (int)System.Math.Ceiling(0.95 * n) - 1;
            if (rank < 0) rank = 0;
            if (rank >= n) rank = n - 1;
            agg.P95 = sorted[rank];
            agg.Samples = n;
            return agg;
        }
    }
}
