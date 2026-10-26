// UnityMCPEventBridge.cs
// Subscribes to Unity Editor events and forwards them to the connected MCP client
// as NotificationEnvelope frames via UnityMCPServer.SendNotification.
//
// Phase 1 (events): playmode.changed, log.line, scene.opened, selection.changed,
//                   compile.done.
// Phase 2 fixes:
//   MED-1 — compile timing survives domain reload. OnBeforeAssemblyReloadInternal
//           persists ticks to SessionState ("UnityMCPEventBridge.CompileStartTicks")
//           AND starts an in-process Stopwatch (s_compileStopwatch). OnAfter reads
//           whichever is available, then erases/nulls it.
//   MED-2 — log filter: Error/Assert/Exception always pass; UNITY_MCP_LOG_MIN_LEVEL
//           configurable for the rest. Bounded queue with overflow counter; when
//           the queue is full, the oldest entry is dropped and the overflow
//           counter is incremented. Overflow is reported as a rate-limited
//           log.overflow notification.

using System;
using System.Collections.Concurrent;
using System.Threading;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityMCP.Bridge.Protocol;
using Stopwatch = System.Diagnostics.Stopwatch;

namespace UnityMCP.Bridge
{
    public static class UnityMCPEventBridge
    {
        // -------------------------------------------------------------------------
        // Event names (must match protocol.schema.json events)
        // -------------------------------------------------------------------------

        public static class EventNames
        {
            public const string PlaymodeChanged   = "playmode.changed";
            public const string LogLine           = "log.line";
            public const string SceneOpened       = "scene.opened";
            public const string SelectionChanged  = "selection.changed";
            public const string CompileDone       = "compile.done";
            public const string LogOverflow       = "log.overflow";    // Phase 2 MED-2
        }

        // -------------------------------------------------------------------------
        // Persistent keys (SessionState — survives domain reload, cleared on Editor exit)
        // -------------------------------------------------------------------------

        internal const string CompileStartTicksKey = "UnityMCPEventBridge.CompileStartTicks";

        // -------------------------------------------------------------------------
        // State
        // -------------------------------------------------------------------------

        private static bool s_initialized;

        /// <summary>
        /// Thread-safe queue of log lines captured off the main thread.
        /// Drained on EditorApplication.update.
        /// </summary>
        private static readonly ConcurrentQueue<LogLine> s_logQueue = new ConcurrentQueue<LogLine>();

        private static long s_overflowCount;
        private static DateTime s_lastOverflowNotifyAt = DateTime.MinValue;
        private static readonly TimeSpan OverflowNotifyInterval = TimeSpan.FromSeconds(1);
        private const int MaxLogLinesPerEditorUpdate = 128;
        private const int MaxLogNotificationMillisPerUpdate = 4;

        // Settable from tests via __MinLevelOrdinalForTest / __QueueCap. Defaults are
        // applied in Initialize() based on env vars (UNITY_MCP_LOG_MIN_LEVEL,
        // UNITY_MCP_LOG_QUEUE_CAP) and otherwise fall back to 0 / 5000.
        private static int s_minLevelOrdinal;
        private static int s_queueCap = 5000;

        /// <summary>
        /// Phase 2 MED-1: in-process compile timer. Set by OnBeforeAssemblyReloadInternal,
        /// consumed and nulled by OnAfterAssemblyReload. Survives within-domain calls;
        /// SessionState handles across-domain.
        /// </summary>
        private static Stopwatch s_compileStopwatch;

        // -------------------------------------------------------------------------
        // Initialize / Teardown
        // -------------------------------------------------------------------------

        public static void Initialize()
        {
            // Re-read env-driven defaults on each Initialize call so tests can flip
            // the env var between runs.
            s_minLevelOrdinal = ParseMinLevelOrdinal(
                Environment.GetEnvironmentVariable("UNITY_MCP_LOG_MIN_LEVEL"));
            string capEnv = Environment.GetEnvironmentVariable("UNITY_MCP_LOG_QUEUE_CAP");
            if (int.TryParse(capEnv, out int parsedCap) && parsedCap > 0)
                s_queueCap = parsedCap;
            else
                s_queueCap = 5000;

            if (s_initialized) return;

            EditorApplication.playModeStateChanged   += OnPlaymodeStateChanged;
            Application.logMessageReceivedThreaded   += OnLogMessageReceived;
            EditorSceneManager.sceneOpened           += OnSceneOpened;
            Selection.selectionChanged               += OnSelectionChanged;

            AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReloadInternal;
            AssemblyReloadEvents.afterAssemblyReload  += OnAfterAssemblyReload;

            EditorApplication.update += OnEditorUpdate;

            s_initialized = true;
            Debug.Log("[UnityMCP] Event bridge initialized.");
        }

        public static void Teardown()
        {
            if (!s_initialized) return;

            EditorApplication.playModeStateChanged   -= OnPlaymodeStateChanged;
            Application.logMessageReceivedThreaded   -= OnLogMessageReceived;
            EditorSceneManager.sceneOpened           -= OnSceneOpened;
            Selection.selectionChanged               -= OnSelectionChanged;

            AssemblyReloadEvents.beforeAssemblyReload -= OnBeforeAssemblyReloadInternal;
            AssemblyReloadEvents.afterAssemblyReload  -= OnAfterAssemblyReload;

            EditorApplication.update -= OnEditorUpdate;

            s_initialized = false;
        }

        // -------------------------------------------------------------------------
        // Cross-file helpers
        // -------------------------------------------------------------------------

        public static int GetLogLineCounter() => UnityMCPCommands.CurrentLogLineCounter();

        // -------------------------------------------------------------------------
        // Playmode
        // -------------------------------------------------------------------------

        private static void OnPlaymodeStateChanged(PlayModeStateChange change)
        {
            string state = MapPlayModeState(change);
            UnityMCPServer.SendNotification(EventNames.PlaymodeChanged,
                new PlaymodeChangedPayload { State = state });
        }

        public static string MapPlayModeState(PlayModeStateChange change)
        {
            switch (change)
            {
                case PlayModeStateChange.EnteredEditMode: return "edit";
                case PlayModeStateChange.ExitingEditMode: return "entering";
                case PlayModeStateChange.EnteredPlayMode: return "play";
                case PlayModeStateChange.ExitingPlayMode: return "exiting";
                default: return "unknown";
            }
        }

        // -------------------------------------------------------------------------
        // Logs (Phase 1 base + Phase 2 MED-2 filter / overflow)
        // -------------------------------------------------------------------------

        // Bridge-internal status logs use this prefix. We drop them on the floor so
        // they don't get rebroadcast as log.line notifications and pollute the client
        // stream (or worse, race with a pending response on the same connection).
        private const string BridgeInternalLogPrefix = "[UnityMCP] ";

        private static void OnLogMessageReceived(string condition, string stackTrace, LogType type)
        {
            if (!ShouldPassType(type, s_minLevelOrdinal))
                return;

            if (!string.IsNullOrEmpty(condition) &&
                condition.StartsWith(BridgeInternalLogPrefix, StringComparison.Ordinal))
                return;

            var line = new LogLine
            {
                LogType = type.ToString(),
                Message = condition,
                StackTrace = string.IsNullOrEmpty(stackTrace) ? null : stackTrace,
                LineNumber = 0, // assigned downstream
            };

            // Bounded queue: when at cap, drop oldest then enqueue this one.
            if (s_logQueue.Count >= s_queueCap)
            {
                if (s_logQueue.TryDequeue(out _))
                    Interlocked.Increment(ref s_overflowCount);
            }
            s_logQueue.Enqueue(line);
        }

        private static void OnEditorUpdate()
        {
            int processed = 0;
            long startedTicks = Stopwatch.GetTimestamp();

            while (processed < MaxLogLinesPerEditorUpdate && s_logQueue.TryDequeue(out LogLine ll))
            {
                UnityMCPCommands.PushLogLine(ll);
                UnityMCPServer.SendNotification(EventNames.LogLine,
                    new LogLinePayload
                    {
                        LogType = ll.LogType,
                        Message = ll.Message,
                        StackTrace = ll.StackTrace,
                        LineNumber = ll.LineNumber,
                    });

                processed += 1;
                long elapsedTicks = Stopwatch.GetTimestamp() - startedTicks;
                double elapsedMs = elapsedTicks * 1000.0 / Stopwatch.Frequency;
                if (elapsedMs >= MaxLogNotificationMillisPerUpdate)
                    break;
            }

            MaybeNotifyOverflow();
        }

        private static void MaybeNotifyOverflow()
        {
            long dropped = Interlocked.Read(ref s_overflowCount);
            if (dropped <= 0) return;

            DateTime now = DateTime.UtcNow;
            if (now - s_lastOverflowNotifyAt < OverflowNotifyInterval)
                return;

            // Reset only after we've decided to notify.
            Interlocked.Exchange(ref s_overflowCount, 0);
            s_lastOverflowNotifyAt = now;

            UnityMCPServer.SendNotification(EventNames.LogOverflow, new LogOverflowPayload
            {
                Dropped = (int)Math.Min(dropped, int.MaxValue),
                Since = now.ToString("o", System.Globalization.CultureInfo.InvariantCulture),
            });
        }

        public static bool ShouldPassType(LogType type, int minOrdinal)
        {
            if (type == LogType.Error || type == LogType.Assert || type == LogType.Exception)
                return true;
            return OrdinalForType(type) >= minOrdinal;
        }

        private static int OrdinalForType(LogType t)
        {
            // Verbosity ladder (low → high): Log, Warning, Assert, Error, Exception.
            // Threshold semantics: emit when ordinal >= minOrdinal.
            switch (t)
            {
                case LogType.Log:       return 0;
                case LogType.Warning:   return 1;
                case LogType.Assert:    return 2;
                case LogType.Error:     return 3;
                case LogType.Exception: return 4;
                default:                return 0;
            }
        }

        private static int ParseMinLevelOrdinal(string env)
        {
            if (string.IsNullOrEmpty(env)) return 0;
            switch (env.Trim().ToLowerInvariant())
            {
                case "log":       return 0;
                case "info":      return 0;
                case "warning":   return 1;
                case "warn":      return 1;
                case "assert":    return 2;
                case "error":     return 3;
                case "exception": return 4;
                default:
                    if (int.TryParse(env, out int n)) return Math.Max(0, Math.Min(4, n));
                    return 0;
            }
        }

        // -------------------------------------------------------------------------
        // Scene + Selection
        // -------------------------------------------------------------------------

        private static void OnSceneOpened(Scene scene, OpenSceneMode mode)
        {
            UnityMCPServer.SendNotification(EventNames.SceneOpened, new SceneOpenedPayload
            {
                SceneName = scene.name,
                ScenePath = scene.path,
                Mode = mode.ToString(),
            });
        }

        private static void OnSelectionChanged()
        {
            int[] ids = Selection.instanceIDs ?? Array.Empty<int>();
            string firstPath = null;
            if (ids.Length > 0)
            {
                var go = EditorUtility.InstanceIDToObject(ids[0]) as GameObject;
                if (go != null) firstPath = BuildObjectPath(go);
            }

            UnityMCPServer.SendNotification(EventNames.SelectionChanged,
                new SelectionChangedPayload
                {
                    InstanceIds = ids,
                    ObjectPath = firstPath,
                });
        }

        public static string BuildObjectPath(GameObject go)
        {
            if (go == null) return null;
            string path = go.name;
            Transform t = go.transform != null ? go.transform.parent : null;
            while (t != null)
            {
                path = t.name + "/" + path;
                t = t.parent;
            }
            return "/" + path;
        }

        // -------------------------------------------------------------------------
        // Compile timing (Phase 2 MED-1)
        //
        // On domain reload, the in-process Stopwatch is wiped — but the SessionState
        // string written before the reload survives. We persist BOTH so that:
        //   * fast-path (no reload between Before/After) → use Stopwatch (sub-ms accuracy)
        //   * reload path → fall back to SessionState ticks
        // -------------------------------------------------------------------------

        private static void OnBeforeAssemblyReloadInternal()
        {
            SessionState.SetString(CompileStartTicksKey,
                DateTime.UtcNow.Ticks.ToString(System.Globalization.CultureInfo.InvariantCulture));
            s_compileStopwatch = Stopwatch.StartNew();
        }

        private static void OnAfterAssemblyReload()
        {
            double durationMs;
            bool haveDuration = false;

            string startTicksStr = SessionState.GetString(CompileStartTicksKey, string.Empty);
            if (!string.IsNullOrEmpty(startTicksStr) &&
                long.TryParse(startTicksStr, out long startTicks))
            {
                durationMs = (DateTime.UtcNow.Ticks - startTicks) / (double)TimeSpan.TicksPerMillisecond;
                haveDuration = true;
                SessionState.EraseString(CompileStartTicksKey);
                s_compileStopwatch = null;
            }
            else if (s_compileStopwatch != null)
            {
                durationMs = s_compileStopwatch.Elapsed.TotalMilliseconds;
                haveDuration = true;
                s_compileStopwatch = null;
            }
            else
            {
                durationMs = 0.0;
            }

            if (!haveDuration) return;

            bool success = !EditorUtility.scriptCompilationFailed;
            UnityMCPServer.SendNotification(EventNames.CompileDone, new CompileDonePayload
            {
                Success = success,
                DurationMs = durationMs,
            });
        }

        // -------------------------------------------------------------------------
        // Test accessors — settable so tests can stage state directly
        // -------------------------------------------------------------------------

#if UNITY_INCLUDE_TESTS
        public static ConcurrentQueue<LogLine> __TestQueue => s_logQueue;
        public static bool __TestInitialized => s_initialized;

        public static int __MinLevelOrdinalForTest
        {
            get => s_minLevelOrdinal;
            set => s_minLevelOrdinal = value;
        }

        public static int __QueueCap
        {
            get => s_queueCap;
            set => s_queueCap = value;
        }

        public static long __OverflowCount => Interlocked.Read(ref s_overflowCount);

        public static void __TestReset()
        {
            while (s_logQueue.TryDequeue(out _)) { }
            Interlocked.Exchange(ref s_overflowCount, 0);
            s_lastOverflowNotifyAt = DateTime.MinValue;
            s_compileStopwatch = null;
            // Don't touch s_minLevelOrdinal / s_queueCap — tests set them explicitly.
        }
#endif
    }
}
