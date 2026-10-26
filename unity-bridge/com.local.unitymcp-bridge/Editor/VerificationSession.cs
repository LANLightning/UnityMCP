// VerificationSession.cs
// Owns the lifetime of one "no-human UI verification" run:
//   - shows/hides the Editor-side hint banner (VerificationHintWindow),
//   - arms/disarms human-takeover detection,
//   - classifies real-device activity into None / Low / High.
//
// Why device polling is the right discriminator (verified):
//   The automation drives the game via PROJECT_UI handler-invoke (PROJECT_CLICK_DIRECT /
//   PROJECT_CLICK_WITH_HIT_TEST) and InputManager.PROJECT_TRIGGER_ACTION — neither touches
//   Mouse.current / Keyboard.current device state (the game's own HasAnyInput() reads
//   the same device API). So ANY real Mouse/Keyboard/Touch/Gamepad device activity =
//   a human. The ONE exception is the optional Tier-2 device-pixel injection
//   (SimulateClick mode=device / SimulateKey key=...), which DOES QueueStateEvent —
//   those paths must wrap themselves in BeginInjectionSuppression/EndInjectionSuppression
//   so the bridge's own events aren't misread as human takeover.
//
// Decision (user): do NOT block real input (no SetEnableInput(false)) — detect only,
// the human can always take over. Tiered exit: mouse-move-only = Low → pause/resume;
// real click/key/scroll/touch/gamepad = High → abort.

using System;
using UnityEditor;
using UnityEngine;
using UnityEngine.InputSystem;

namespace UnityMCP.Bridge
{
    internal static class VerificationSession
    {
        // Internal surface; the test assembly sees it via InternalsVisibleTo
        // (see Editor/AssemblyInfo.cs). PollTakeoverTick reads Unity devices;
        // ClassifyVerdict (below) is pure and is the standalone-tested unit.
        internal enum Takeover { None, Low, High }

        private static bool s_armed;
        private static string s_sessionId;
        private static double s_pauseSilenceSeconds = 2.0;
        private static bool s_abortOnHigh = true;

        // Counters surfaced in EndVerificationSession.
        private static int s_lowEvents;
        private static int s_highEvents;

        // Last time (editor time) a real device produced activity — drives the
        // "device silent for N seconds" resume condition after a Low pause.
        private static double s_lastDeviceActivityTime;

        // Injection-suppression window: while > Time.frameCount the takeover poll
        // ignores device state (the bridge's own device-mode injection is in flight).
        private static int s_suppressPollUntilFrame;

        // ----- Screenshot approval gate -----
        // When gating is on, each CaptureScreenshot waits (in the single dispatcher
        // coroutine) for the user to click 继续 in the hint window before the base64
        // is delivered to the AI. MCP is serial, so the AI is blocked from issuing
        // any further tool call until this screenshot returns — that IS the "pause
        // the downstream flow until the user confirms" semantics.
        internal enum Approval { Pending, Approved, Rejected }

        private static bool s_gateScreenshots;       // gate enabled for this session
        private static bool s_autoApprove;            // user clicked 继续且不再询问
        private static bool s_approvalPending;        // a screenshot is awaiting approval
        private static Approval s_approvalDecision = Approval.Pending;
        private static bool s_recaptureRequested;     // user clicked 重新截图

        // True when this session was opened automatically by capture_screenshot
        // (the user did not explicitly StartVerificationSession). Diagnostic only —
        // teardown is identical to a normal session.
        private static bool s_autoStarted;

        internal static bool IsArmed => s_armed;
        internal static string SessionId => s_sessionId;
        internal static int LowEvents => s_lowEvents;
        internal static int HighEvents => s_highEvents;
        internal static bool GateScreenshots => s_armed && s_gateScreenshots;
        internal static bool ApprovalPending => s_approvalPending;
        internal static bool AutoApprove => s_autoApprove;
        internal static bool AutoStarted => s_autoStarted;

        /// <summary>
        /// Start a verification session: show the hint banner and arm takeover
        /// detection. Returns the new session id. Idempotent-ish: starting while
        /// already armed replaces the config and keeps the banner up.
        /// </summary>
        internal static string Start(string note, double pauseSilenceSeconds, bool abortOnHigh,
            bool gateScreenshots, bool autoStarted = false)
        {
            s_sessionId = Guid.NewGuid().ToString();
            s_pauseSilenceSeconds = pauseSilenceSeconds > 0 ? pauseSilenceSeconds : 2.0;
            s_abortOnHigh = abortOnHigh;
            s_lowEvents = 0;
            s_highEvents = 0;
            s_lastDeviceActivityTime = NowSeconds();
            s_suppressPollUntilFrame = 0;
            s_gateScreenshots = gateScreenshots;
            s_autoApprove = false;
            s_approvalPending = false;
            s_approvalDecision = Approval.Pending;
            s_recaptureRequested = false;
            s_autoStarted = autoStarted;
            s_armed = true;

            VerificationHintWindow.Show(note);
            return s_sessionId;
        }

        // ----- Screenshot approval lifecycle -----

        /// <summary>
        /// Mark a screenshot as awaiting approval and tell the window to show the
        /// 继续 buttons. Returns immediately approved if auto-approve is on. The
        /// dispatcher coroutine then polls PollApproval() each frame.
        /// </summary>
        internal static void BeginApproval()
        {
            s_approvalPending = true;
            s_approvalDecision = s_autoApprove ? Approval.Approved : Approval.Pending;
            VerificationHintWindow.SetAwaitingApproval(true);
        }

        /// <summary>Current approval decision (Pending while waiting for the user).</summary>
        internal static Approval PollApproval()
        {
            return ResolveApproval(s_autoApprove, s_approvalDecision);
        }

        /// <summary>
        /// Pure approval resolution: auto-approve short-circuits to Approved; otherwise
        /// the explicit per-screenshot decision wins. Exposed for standalone NUnit.
        /// </summary>
        internal static Approval ResolveApproval(bool autoApprove, Approval pendingDecision)
        {
            return autoApprove ? Approval.Approved : pendingDecision;
        }

        /// <summary>User clicked 继续工具调用 (this one screenshot).</summary>
        internal static void ApproveCurrent()
        {
            s_approvalDecision = Approval.Approved;
        }

        /// <summary>User clicked 继续且不再询问 (this + all future screenshots).</summary>
        internal static void ApproveAll()
        {
            s_autoApprove = true;
            s_approvalDecision = Approval.Approved;
        }

        /// <summary>User clicked 重新截图 — request the approval coroutine to recapture.</summary>
        internal static void RequestRecapture()
        {
            if (s_approvalPending)
                s_recaptureRequested = true;
        }

        /// <summary>
        /// Consume a pending recapture request (true once per click). The approval
        /// coroutine calls this each tick; on true it re-captures and refreshes the
        /// preview, then keeps waiting for approval.
        /// </summary>
        internal static bool ConsumeRecaptureRequest()
        {
            if (!s_recaptureRequested)
                return false;
            s_recaptureRequested = false;
            return true;
        }

        /// <summary>
        /// Clear the pending state once the dispatcher has delivered/aborted the
        /// screenshot. Called by the approval coroutine after it resolves.
        /// </summary>
        internal static void EndApproval()
        {
            s_approvalPending = false;
            s_approvalDecision = Approval.Pending;
            s_recaptureRequested = false;
            VerificationHintWindow.SetAwaitingApproval(false);
        }

        /// <summary>
        /// End the named session: hide the banner, disarm. No-op if the id doesn't
        /// match the active session (stale end). Returns true if it ended a session.
        /// </summary>
        internal static bool End(string sessionId)
        {
            if (!s_armed)
                return false;
            if (!string.IsNullOrEmpty(sessionId) && sessionId != s_sessionId)
                return false;
            DisarmAndHide();
            return true;
        }

        /// <summary>Unconditional teardown (domain reload / playmode exit / disconnect).</summary>
        internal static void ForceEnd()
        {
            if (!s_armed && VerificationHintWindow.IsShowing == false)
                return;
            DisarmAndHide();
        }

        private static void DisarmAndHide()
        {
            // If a screenshot was mid-approval, mark it Rejected so the dispatcher
            // coroutine resolves (and the AI gets a clean error) instead of hanging.
            if (s_approvalPending)
                s_approvalDecision = Approval.Rejected;
            s_approvalPending = false;
            s_gateScreenshots = false;
            s_autoApprove = false;
            s_autoStarted = false;
            s_armed = false;
            s_sessionId = null;
            VerificationHintWindow.Hide();
        }

        /// <summary>
        /// Begin a window during which the takeover poll ignores device state, because
        /// the bridge itself is about to inject synthetic device events (Tier-2 path).
        /// frames: how many editor frames to suppress (small, e.g. 6).
        /// </summary>
        internal static void BeginInjectionSuppression(int frames)
        {
            int until = Time.frameCount + Mathf.Max(1, frames);
            if (until > s_suppressPollUntilFrame)
                s_suppressPollUntilFrame = until;
        }

        internal static void EndInjectionSuppression()
        {
            // Leave a 1-frame tail so the queued device event (processed next
            // InputSystem.Update) doesn't trip the poll on the very next tick.
            int until = Time.frameCount + 1;
            if (until > s_suppressPollUntilFrame)
                s_suppressPollUntilFrame = until;
        }

        /// <summary>
        /// Classify real-device activity this tick. Returns None when not armed or
        /// inside an injection-suppression window. Updates last-activity time and
        /// the low/high counters.
        /// </summary>
        internal static Takeover PollTakeoverTick()
        {
            if (!s_armed)
                return Takeover.None;
            if (Time.frameCount <= s_suppressPollUntilFrame)
                return Takeover.None;
            // While a screenshot awaits approval, the user is EXPECTED to be moving
            // the mouse and clicking the 继续 button in the hint window. Those are
            // real device events but must NOT be read as a human takeover abort.
            if (s_approvalPending)
                return Takeover.None;

            bool high = false;
            bool low = false;

            try
            {
                Mouse mouse = Mouse.current;
                if (mouse != null)
                {
                    if (mouse.leftButton.wasPressedThisFrame ||
                        mouse.rightButton.wasPressedThisFrame ||
                        mouse.middleButton.wasPressedThisFrame)
                    {
                        high = true;
                    }
                    Vector2 scroll = mouse.scroll.ReadValue();
                    if (Mathf.Abs(scroll.x) > 0.01f || Mathf.Abs(scroll.y) > 0.01f)
                        high = true;

                    Vector2 delta = mouse.delta.ReadValue();
                    if (delta.sqrMagnitude > 0.01f)
                        low = true;
                }

                Keyboard kb = Keyboard.current;
                if (kb != null && kb.anyKey.wasPressedThisFrame)
                    high = true;

                if (Touchscreen.current != null && Touchscreen.current.primaryTouch.press.isPressed)
                    high = true;

                Gamepad gp = Gamepad.current;
                if (gp != null)
                {
                    if (gp.buttonSouth.wasPressedThisFrame || gp.buttonNorth.wasPressedThisFrame ||
                        gp.buttonEast.wasPressedThisFrame || gp.buttonWest.wasPressedThisFrame ||
                        gp.leftStick.ReadValue().sqrMagnitude > 0.2f ||
                        gp.rightStick.ReadValue().sqrMagnitude > 0.2f)
                    {
                        high = true;
                    }
                }
            }
            catch
            {
                // Device layer not ready (e.g. EditMode boot) — treat as no activity.
                return Takeover.None;
            }

            Takeover verdict = ClassifyVerdict(high, low, s_abortOnHigh);
            if (verdict != Takeover.None)
            {
                s_lastDeviceActivityTime = NowSeconds();
                if (high) s_highEvents++;
                else if (low) s_lowEvents++;
            }
            return verdict;
        }

        /// <summary>
        /// Pure classification of a tick's device booleans into a takeover verdict.
        /// Exposed for standalone NUnit tests (no Unity types). A high-impact event
        /// (real click/key/scroll/touch/gamepad) maps to High when abortOnHigh, else
        /// Low (pause instead of abort). A low-impact event (mouse move) maps to Low.
        /// </summary>
        internal static Takeover ClassifyVerdict(bool high, bool low, bool abortOnHigh)
        {
            if (high)
                return abortOnHigh ? Takeover.High : Takeover.Low;
            if (low)
                return Takeover.Low;
            return Takeover.None;
        }

        /// <summary>True once the real device has been quiet for pauseSilenceSeconds.</summary>
        internal static bool IsDeviceSilentEnough()
        {
            return (NowSeconds() - s_lastDeviceActivityTime) >= s_pauseSilenceSeconds;
        }

        private static double NowSeconds()
        {
            // EditorApplication.timeSinceStartup is monotonic on the main thread and
            // available in both EditMode and PlayMode.
            return EditorApplication.timeSinceStartup;
        }
    }
}
