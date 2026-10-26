// UnityMCPWatchdogTests.cs — Tests.EditMode/
//
// Tests the pure UnityMCPWatchdog.Evaluate decision function without a thread or
// a live Editor. Gated by UNITY_TEST_FRAMEWORK_AVAILABLE; sees internals via
// InternalsVisibleTo (AssemblyInfo.cs).

#if UNITY_TEST_FRAMEWORK_AVAILABLE

using NUnit.Framework;

namespace UnityMCP.Bridge.Tests
{
    [TestFixture]
    public class UnityMCPWatchdogTests
    {
        private static UnityMCPCommands.InFlightSnapshot Active(
            string reqId, string cmd, double startMs)
        {
            return new UnityMCPCommands.InFlightSnapshot
            {
                Active = true,
                RequestId = reqId,
                Command = cmd,
                StartMs = startMs,
            };
        }

        private static UnityMCPCommands.InFlightSnapshot Idle()
        {
            return new UnityMCPCommands.InFlightSnapshot { Active = false };
        }

        [Test]
        public void Evaluate_BelowThreshold_DoesNotAlert()
        {
            var snap = Active("req-1", "LuaEval", startMs: 1000);
            // 1000 -> 3000 = 2000ms elapsed, threshold 5000 -> no alert yet.
            var v = UnityMCPWatchdog.Evaluate(snap, nowMs: 3000, thresholdMs: 5000, alreadyAlertedRequestId: null);

            Assert.IsFalse(v.ShouldAlert);
            Assert.IsFalse(v.ClearAlert);
            Assert.AreEqual(2000, v.StalledMs, 0.001);
        }

        [Test]
        public void Evaluate_PastThreshold_Alerts()
        {
            var snap = Active("req-1", "LuaEval", startMs: 1000);
            // 1000 -> 7000 = 6000ms elapsed, threshold 5000 -> alert.
            var v = UnityMCPWatchdog.Evaluate(snap, nowMs: 7000, thresholdMs: 5000, alreadyAlertedRequestId: null);

            Assert.IsTrue(v.ShouldAlert);
            Assert.IsFalse(v.ClearAlert);
            Assert.AreEqual(6000, v.StalledMs, 0.001);
        }

        [Test]
        public void Evaluate_ExactlyAtThreshold_DoesNotAlert()
        {
            // Strictly greater-than required; equal is still "within budget".
            var snap = Active("req-1", "LuaEval", startMs: 0);
            var v = UnityMCPWatchdog.Evaluate(snap, nowMs: 5000, thresholdMs: 5000, alreadyAlertedRequestId: null);

            Assert.IsFalse(v.ShouldAlert);
        }

        [Test]
        public void Evaluate_AlreadyAlertedSameRequest_StaysSilent()
        {
            var snap = Active("req-1", "LuaEval", startMs: 1000);
            // Well past threshold, but we already alerted on req-1 -> fire exactly once.
            var v = UnityMCPWatchdog.Evaluate(snap, nowMs: 99999, thresholdMs: 5000, alreadyAlertedRequestId: "req-1");

            Assert.IsFalse(v.ShouldAlert);
            Assert.IsFalse(v.ClearAlert);
        }

        [Test]
        public void Evaluate_DifferentRequestThanAlerted_ClearsWithoutAlerting()
        {
            // A new command is now in flight; the previous wedge resolved. Reset the
            // alerted id this tick (do not alert), so the new request can alert later
            // on its own elapsed time.
            var snap = Active("req-2", "LuaEval", startMs: 8000);
            var v = UnityMCPWatchdog.Evaluate(snap, nowMs: 99999, thresholdMs: 5000, alreadyAlertedRequestId: "req-1");

            Assert.IsFalse(v.ShouldAlert);
            Assert.IsTrue(v.ClearAlert);
        }

        [Test]
        public void Evaluate_NewRequestAlertsOnNextTickAfterClear()
        {
            // Simulate the tick AFTER the clear above: alertedId is now null and the
            // new request has itself exceeded the threshold -> it alerts.
            var snap = Active("req-2", "LuaEval", startMs: 8000);
            var v = UnityMCPWatchdog.Evaluate(snap, nowMs: 99999, thresholdMs: 5000, alreadyAlertedRequestId: null);

            Assert.IsTrue(v.ShouldAlert);
        }

        [Test]
        public void Evaluate_Idle_ClearsStaleAlertedId()
        {
            var v = UnityMCPWatchdog.Evaluate(Idle(), nowMs: 99999, thresholdMs: 5000, alreadyAlertedRequestId: "req-1");

            Assert.IsFalse(v.ShouldAlert);
            Assert.IsTrue(v.ClearAlert);
        }

        [Test]
        public void Evaluate_Idle_NoStaleId_NoOp()
        {
            var v = UnityMCPWatchdog.Evaluate(Idle(), nowMs: 99999, thresholdMs: 5000, alreadyAlertedRequestId: null);

            Assert.IsFalse(v.ShouldAlert);
            Assert.IsFalse(v.ClearAlert);
        }

        [Test]
        public void Evaluate_ActiveButEmptyRequestId_TreatedAsIdle()
        {
            var snap = new UnityMCPCommands.InFlightSnapshot
            {
                Active = true, RequestId = null, Command = null, StartMs = 0,
            };
            var v = UnityMCPWatchdog.Evaluate(snap, nowMs: 99999, thresholdMs: 5000, alreadyAlertedRequestId: "req-1");

            Assert.IsFalse(v.ShouldAlert);
            Assert.IsTrue(v.ClearAlert);
        }
    }
}

#endif
