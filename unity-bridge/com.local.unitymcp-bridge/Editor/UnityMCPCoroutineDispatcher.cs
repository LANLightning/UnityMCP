// UnityMCPCoroutineDispatcher.cs
// The SINGLE async pump for verification-class bridge commands.
//
// Requirement (user): the verification flow must NOT block or occupy the Unity
// Editor main thread, and may use AT MOST ONE coroutine to service tool calls.
//
// Design:
//   - Verification commands (OpenPanel, SimulateClick, ...) return null from their
//     Handle* and Enqueue() a Func<IEnumerator> here. They are run SERIALLY (one at
//     a time — matching the bridge's single-client serial contract) by ONE
//     EditorCoroutine started lazily and torn down when the queue drains. There is
//     never a second coroutine.
//   - Between every step (MoveNext) of the running tool, the pump polls
//     VerificationSession.PollTakeoverTick() so a human grabbing the mouse/keyboard
//     is noticed mid-flight. LOW (mouse-move only) → pause until device silence;
//     HIGH (real click/key/scroll/touch/gamepad) → abort the whole queue.
//   - Cooperative: every step hands the frame back (yield), so the TCP drain and
//     short synchronous commands (Handshake, GetPlayState, TailEditorLog) keep
//     flowing. No Thread.Sleep, no busy-wait — ever.
//
// The existing HandleCaptureScreenshot (delayCall) and HandleWaitForIdle
// (update += tick) keep their own ad-hoc async; they are short and self-terminating
// and are NOT coroutines, so they don't violate the "at most one coroutine" rule.
// Only verification commands funnel through this dispatcher.

using System;
using System.Collections;
using System.Collections.Generic;
using Unity.EditorCoroutines.Editor;
using UnityEditor;
using UnityEngine;
using UnityMCP.Bridge.Protocol;

namespace UnityMCP.Bridge
{
    [InitializeOnLoad]
    internal static class UnityMCPCoroutineDispatcher
    {
        private struct Job
        {
            public string RequestId;
            public Func<IEnumerator> Work;
        }

        private static readonly Queue<Job> s_queue = new Queue<Job>();
        private static EditorCoroutine s_pump;            // the ONE coroutine; null when idle
        private static string s_activeRequestId;          // request currently being serviced

        static UnityMCPCoroutineDispatcher()
        {
            // Teardown on domain reload / playmode exit so an in-flight verification
            // never leaves the client waiting forever or the hint banner orphaned.
            AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReload;
            EditorApplication.playModeStateChanged += OnPlayModeStateChanged;
        }

        internal static bool IsBusy => s_pump != null;
        internal static int QueueDepth => s_queue.Count;

        /// <summary>
        /// Queue a verification tool's coroutine. The tool's Handle* must return null;
        /// the coroutine is responsible for eventually calling UnityMCPServer.SendResponse
        /// for <paramref name="requestId"/>. Starts the single pump if idle.
        /// </summary>
        internal static void Enqueue(string requestId, Func<IEnumerator> work)
        {
            if (string.IsNullOrEmpty(requestId) || work == null)
                return;

            s_queue.Enqueue(new Job { RequestId = requestId, Work = work });
            if (s_pump == null)
                s_pump = EditorCoroutineUtility.StartCoroutineOwnerless(PumpLoop());
        }

        /// <summary>
        /// Abort every queued and in-flight verification request with an error response,
        /// stop the pump, and clear the queue. Used on human-takeover (HIGH), client
        /// disconnect, playmode exit, and domain reload.
        /// </summary>
        internal static void AbortAll(string code, string message)
        {
            // Respond to the in-flight request (if any) first so the client is unblocked.
            if (!string.IsNullOrEmpty(s_activeRequestId))
            {
                TrySendError(s_activeRequestId, code, message);
                s_activeRequestId = null;
            }

            while (s_queue.Count > 0)
            {
                Job job = s_queue.Dequeue();
                TrySendError(job.RequestId, code, message);
            }

            if (s_pump != null)
            {
                try { EditorCoroutineUtility.StopCoroutine(s_pump); }
                catch { /* coroutine may already be dead (domain reload) */ }
                s_pump = null;
            }
        }

        private static IEnumerator PumpLoop()
        {
            while (s_queue.Count > 0)
            {
                Job job = s_queue.Dequeue();
                s_activeRequestId = job.RequestId;

                IEnumerator inner;
                try
                {
                    inner = job.Work();
                }
                catch (Exception ex)
                {
                    TrySendError(job.RequestId, ErrorCodes.Internal,
                        $"verification job factory threw: {ex.Message}");
                    s_activeRequestId = null;
                    continue;
                }

                bool aborted = false;
                while (true)
                {
                    // Poll human takeover BEFORE advancing the tool another step.
                    VerificationSession.Takeover verdict = VerificationSession.PollTakeoverTick();
                    if (verdict == VerificationSession.Takeover.High)
                    {
                        // HIGH: abort the whole queue (this request + everything waiting).
                        AbortAll(ErrorCodes.EditorBusy, "human_takeover_detected");
                        aborted = true;
                        break;
                    }
                    if (verdict == VerificationSession.Takeover.Low)
                    {
                        // LOW: pause — keep the banner up, hand frames back until the
                        // real device goes silent (or escalates to HIGH).
                        while (true)
                        {
                            yield return null;
                            VerificationSession.Takeover v = VerificationSession.PollTakeoverTick();
                            if (v == VerificationSession.Takeover.High)
                            {
                                AbortAll(ErrorCodes.EditorBusy, "human_takeover_detected");
                                aborted = true;
                                break;
                            }
                            if (VerificationSession.IsDeviceSilentEnough())
                                break; // resume the tool
                        }
                        if (aborted)
                            break;
                    }

                    bool moved;
                    try
                    {
                        moved = inner.MoveNext();
                    }
                    catch (Exception ex)
                    {
                        // A profiler sample that throws mid-flight must still restore the
                        // Editor knobs it altered (symmetry with the reload/playmode-exit
                        // hooks); the coroutine's own finally only disposes recorders.
                        UnityMCPCommands.RunPendingRestoreIfAny();
                        TrySendError(job.RequestId, ErrorCodes.Internal,
                            $"verification step threw: {ex.Message}");
                        break;
                    }

                    if (!moved)
                        break; // tool finished (it already sent its own response)

                    yield return inner.Current; // cooperative: hand the frame back
                }

                s_activeRequestId = null;
                if (aborted)
                    yield break; // AbortAll already stopped us; queue is flushed
            }

            s_pump = null; // drained → no lingering coroutine
        }

        private static void TrySendError(string requestId, string code, string message)
        {
            try
            {
                UnityMCPServer.SendResponse(
                    ProtocolSerializer.ErrorResponse(requestId, code, message));
            }
            catch { /* stream may be gone; best-effort */ }
        }

        private static void OnBeforeAssemblyReload()
        {
            // EditorCoroutines do not survive a domain reload; flush so no client hangs.
            AbortAll(ErrorCodes.InvalidState, "domain_reload_aborted_verification");
            UnityMCPCommands.RunPendingRestoreIfAny();
            VerificationSession.ForceEnd();
        }

        private static void OnPlayModeStateChanged(PlayModeStateChange change)
        {
            if (change == PlayModeStateChange.ExitingPlayMode)
            {
                // Every Lua seam needs PlayMode; abort anything in flight.
                AbortAll(ErrorCodes.InvalidState, "play_mode_exited");
                UnityMCPCommands.RunPendingRestoreIfAny();
                VerificationSession.ForceEnd();
            }
        }
    }
}
