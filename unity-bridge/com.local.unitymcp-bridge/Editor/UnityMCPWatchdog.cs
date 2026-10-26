// UnityMCPWatchdog.cs
// Background-thread watchdog that detects a WEDGED Editor main thread.
//
// Why this exists:
//   Every bridge command runs synchronously on Unity's single Editor main thread
//   (UnityMCPCommands.Dispatch). A handler that runs unbounded code — above all
//   LuaEval with a no-exit Lua loop (`while true do end`) — freezes the main
//   thread FOREVER. Once wedged, EditorApplication.update stops firing, so nothing
//   on the main thread (the TCP drain, the coroutine pump, any update tick) can
//   notice or recover. The project's xLua exposes no lua_sethook, so the running
//   chunk cannot be preempted either.
//
// What this does (and does NOT do):
//   - A dedicated BACKGROUND thread (the only code path that keeps running while
//     the main thread is frozen) polls UnityMCPCommands.InFlight. If the same
//     synchronous request has been running longer than the stall threshold, it
//     unblocks the waiting client EARLY via UnityMCPServer.TrySendWedgeAlert (a
//     precise EDITOR_BUSY error instead of the client's generic 30s timeout) and
//     logs the wedge.
//   - It does NOT unfreeze the main thread (impossible without preemption) and it
//     does NOT kill the Editor. Recovery from a true wedge is still a manual
//     restart; the watchdog only makes the freeze visible and diagnosable.
//
// Thread-safety: the watchdog thread touches only thread-safe surfaces —
// UnityMCPCommands.InFlight (lock-guarded snapshot), a process Stopwatch clock,
// and UnityMCPServer.TrySendWedgeAlert (s_writeLock-guarded). It never calls a
// main-thread-only Unity API (EditorApplication.*, Debug.Log from the thread is
// avoided — we route the alert log through the main-thread-safe LogError inside
// TrySendWedgeAlert's caller path is not used; see SafeLog below).

using System;
using System.Threading;
using UnityEditor;
using UnityEngine;

namespace UnityMCP.Bridge
{
    [InitializeOnLoad]
    internal static class UnityMCPWatchdog
    {
        // Default stall threshold. A healthy synchronous command returns in
        // single-digit milliseconds; even a heavy OpenScene is well under a second.
        // 5s is comfortably past any legitimate synchronous handler while still
        // beating the Python client's 30s socket timeout, so the client gets a
        // precise wedge error instead of a vague timeout.
        private const int DefaultStallMs = 5000;
        private const int MinStallMs = 1000;     // floor — avoid alerting on normal work
        private const int PollIntervalMs = 250;  // background poll cadence

        private static readonly object s_lifecycleLock = new object();
        private static Thread s_thread;
        private static volatile bool s_stop;
        private static int s_stallMs = DefaultStallMs;

        // The request id we have already alerted on, so we fire EXACTLY ONCE per
        // wedge. Written only by the watchdog thread; read only by the watchdog
        // thread. Cleared when the in-flight request changes or goes idle.
        private static string s_alertedRequestId;

        static UnityMCPWatchdog()
        {
            // The static ctor runs on every domain reload. We do NOT auto-start here;
            // UnityMCPServer.Start() (which already owns the IsBridgeDisabled gate)
            // calls Start() so the watchdog and listener share one enable decision.
        }

        /// <summary>
        /// Start the background watchdog thread. Idempotent. No-op in batch mode
        /// (no interactive editor loop to protect) or when explicitly disabled via
        /// UNITY_MCP_WATCHDOG_DISABLED.
        /// </summary>
        internal static void Start()
        {
            if (Application.isBatchMode)
                return;
            if (IsDisabledByEnv())
            {
                Debug.Log("[UnityMCP] Watchdog disabled via UNITY_MCP_WATCHDOG_DISABLED.");
                return;
            }

            lock (s_lifecycleLock)
            {
                if (s_thread != null && s_thread.IsAlive)
                    return;

                s_stallMs = ReadStallMsFromEnv();
                s_stop = false;
                s_alertedRequestId = null;
                s_thread = new Thread(WatchLoop)
                {
                    Name = "UnityMCP-Watchdog",
                    IsBackground = true,
                };
                s_thread.Start();
                Debug.Log($"[UnityMCP] Watchdog started (stall threshold {s_stallMs}ms).");
            }
        }

        /// <summary>
        /// Signal the watchdog thread to stop and join briefly. Background threads
        /// survive a domain reload, so this MUST be called from
        /// UnityMCPServer.OnBeforeAssemblyReload or the thread leaks and keeps a
        /// reference to soon-to-be-stale statics. Idempotent.
        /// </summary>
        internal static void Stop()
        {
            Thread t;
            lock (s_lifecycleLock)
            {
                s_stop = true;
                t = s_thread;
                s_thread = null;
            }
            if (t != null && t.IsAlive)
            {
                try { t.Join(PollIntervalMs * 4); }
                catch { /* best-effort; it's a background thread either way */ }
            }
        }

        private static void WatchLoop()
        {
            while (!s_stop)
            {
                try
                {
                    Tick();
                }
                catch
                {
                    // Never let an exception kill the watchdog thread silently; just
                    // continue to the next poll. (Can't safely Debug.Log off-thread
                    // on every spin — TrySendWedgeAlert below handles the one alert.)
                }
                // Short sleeps so Stop() responds promptly.
                Thread.Sleep(PollIntervalMs);
            }
        }

        private static void Tick()
        {
            UnityMCPCommands.InFlightSnapshot snap = UnityMCPCommands.InFlight.Snapshot();
            double nowMs = UnityMCPCommands.InFlight.NowMs;

            WatchdogVerdict verdict = Evaluate(snap, nowMs, s_stallMs, s_alertedRequestId);
            if (verdict.ShouldAlert)
            {
                s_alertedRequestId = snap.RequestId;
                UnityMCPServer.TrySendWedgeAlert(
                    snap.RequestId, snap.Command, verdict.StalledMs,
                    snap.ConnectionGeneration);
                UnityMCPServer.WriteWedgeEvidence(
                    snap.RequestId, snap.Command, verdict.StalledMs,
                    snap.ConnectionGeneration);
            }
            else if (verdict.ClearAlert)
            {
                s_alertedRequestId = null;
                UnityMCPServer.ClearWedgeEvidence();
            }
        }

        // ------------------------------------------------------------------------
        // Pure decision function — unit-tested without a thread or Unity (the test
        // assembly sees internals via InternalsVisibleTo). Given the current
        // in-flight snapshot, the clock, the threshold, and what we last alerted on,
        // decide whether to fire a (single) wedge alert or clear the alerted state.
        // ------------------------------------------------------------------------

        internal struct WatchdogVerdict
        {
            public bool ShouldAlert;   // fire TrySendWedgeAlert this tick
            public bool ClearAlert;    // reset s_alertedRequestId (request changed/idle)
            public double StalledMs;   // how long the wedged request has run
        }

        internal static WatchdogVerdict Evaluate(
            UnityMCPCommands.InFlightSnapshot snap,
            double nowMs,
            double thresholdMs,
            string alreadyAlertedRequestId)
        {
            // Idle main thread: nothing in flight. Clear any stale alerted id.
            if (!snap.Active || string.IsNullOrEmpty(snap.RequestId))
            {
                return new WatchdogVerdict
                {
                    ShouldAlert = false,
                    ClearAlert = alreadyAlertedRequestId != null,
                    StalledMs = 0,
                };
            }

            // A different request is now in flight than the one we alerted on — the
            // previous wedge resolved (or a new command started). Reset so the new
            // one can alert on its own merits, but do not alert this same tick.
            if (alreadyAlertedRequestId != null &&
                !string.Equals(alreadyAlertedRequestId, snap.RequestId, StringComparison.Ordinal))
            {
                return new WatchdogVerdict { ShouldAlert = false, ClearAlert = true, StalledMs = 0 };
            }

            // Already alerted on THIS request — fire exactly once, stay silent after.
            if (string.Equals(alreadyAlertedRequestId, snap.RequestId, StringComparison.Ordinal))
            {
                return new WatchdogVerdict { ShouldAlert = false, ClearAlert = false, StalledMs = 0 };
            }

            double elapsed = nowMs - snap.StartMs;
            if (elapsed > thresholdMs)
            {
                return new WatchdogVerdict { ShouldAlert = true, ClearAlert = false, StalledMs = elapsed };
            }

            // In flight but still within budget — keep watching.
            return new WatchdogVerdict { ShouldAlert = false, ClearAlert = false, StalledMs = elapsed };
        }

        // ------------------------------------------------------------------------
        // Env config — same idiom as UNITY_MCP_BRIDGE_PORT / UNITY_MCP_BRIDGE_DISABLED.
        // ------------------------------------------------------------------------

        private static bool IsDisabledByEnv()
        {
            string v = Environment.GetEnvironmentVariable("UNITY_MCP_WATCHDOG_DISABLED");
            return !string.IsNullOrEmpty(v) &&
                   (v == "1" || string.Equals(v, "true", StringComparison.OrdinalIgnoreCase));
        }

        private static int ReadStallMsFromEnv()
        {
            string v = Environment.GetEnvironmentVariable("UNITY_MCP_WATCHDOG_STALL_MS");
            if (int.TryParse(v, out int parsed) && parsed >= MinStallMs)
                return parsed;
            return DefaultStallMs;
        }
    }
}
