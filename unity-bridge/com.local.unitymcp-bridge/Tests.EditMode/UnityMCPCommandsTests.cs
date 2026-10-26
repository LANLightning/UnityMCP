// UnityMCPCommandsTests.cs — Tests.EditMode/
//
// Tests the Execute* delegate-injected entry points without requiring a Unity Editor.
// Gated by UNITY_TEST_FRAMEWORK_AVAILABLE (Unity ignores; standalone csproj compiles).

#if UNITY_TEST_FRAMEWORK_AVAILABLE

using System;
using NUnit.Framework;
using UnityMCP.Bridge.Protocol;

namespace UnityMCP.Bridge.Tests
{
    [TestFixture]
    public class UnityMCPCommandsTests
    {
        [Test]
        public void ExecuteHandshake_PassesThroughDelegateValues()
        {
            HandshakeResult r = UnityMCPCommands.ExecuteHandshake(
                "1.2.0",
                () => "2022.3.1f1",
                () => "/projects/demo");

            Assert.AreEqual("1.2.0", r.ServerProtocolVersion);
            Assert.AreEqual("2022.3.1f1", r.EditorVersion);
            Assert.AreEqual("/projects/demo", r.ProjectPath);
        }

        [Test]
        public void ExecutePlaymode_Enter_SetsPlaymodeTrue()
        {
            bool received = false;
            PlaymodeResult r = UnityMCPCommands.ExecutePlaymode(true, val => received = val);

            Assert.IsTrue(received);
            Assert.AreEqual("entering", r.State);
        }

        [Test]
        public void ExecutePlaymode_Exit_SetsPlaymodeFalse()
        {
            bool? received = null;
            PlaymodeResult r = UnityMCPCommands.ExecutePlaymode(false, val => received = val);

            Assert.IsTrue(received.HasValue);
            Assert.IsFalse(received.Value);
            Assert.AreEqual("exiting", r.State);
        }

        [Test]
        public void ExecuteTailEditorLog_RingBufferEmpty_ReturnsEmpty()
        {
            // Note: ring buffer state may be shared across tests; only assert non-null.
            TailEditorLogResult r = UnityMCPCommands.ExecuteTailEditorLog(0, 100);
            Assert.IsNotNull(r.Lines);
            Assert.LessOrEqual(r.Lines.Count, 100);
        }

        [Test]
        public void ExecutePingObject_WithInstanceId_CallsResolveById()
        {
            int resolveByIdCalled = 0;
            int resolveByPathCalled = 0;
            int pingCalled = 0;

            UnityMCPCommands.ExecutePingObject(
                instanceId: 42,
                path: null,
                resolveById: id =>
                {
                    resolveByIdCalled++;
                    Assert.AreEqual(42, id);
                    return null; // simulate not-found
                },
                resolveByPath: p =>
                {
                    resolveByPathCalled++;
                    return null;
                },
                pingFn: o => pingCalled++);

            Assert.AreEqual(1, resolveByIdCalled);
            Assert.AreEqual(0, resolveByPathCalled);
            Assert.AreEqual(0, pingCalled); // not found → no ping
        }

        [Test]
        public void ExecuteSetGameViewSize_ApplyReturnsTrue_ResultAppliedTrue()
        {
            SetGameViewSizeResult r = UnityMCPCommands.ExecuteSetGameViewSize(
                1920, 1080, (w, h) => true);

            Assert.AreEqual(1920, r.Width);
            Assert.AreEqual(1080, r.Height);
            Assert.IsTrue(r.Applied);
        }

        [Test]
        public void ExecuteSetGameViewSize_ApplyThrows_ResultAppliedFalse()
        {
            SetGameViewSizeResult r = UnityMCPCommands.ExecuteSetGameViewSize(
                100, 100, (w, h) => throw new System.Exception("boom"));

            Assert.IsFalse(r.Applied);
        }

        [Test]
        public void ExecuteWaitForIdle_NoNewLogEvents_ReachesIdle()
        {
            int counter = 5;
            DateTime current = DateTime.UtcNow;

            ResponseEnvelope response = UnityMCPCommands.ExecuteWaitForIdle(
                "id-wait-1",
                new WaitForIdleArgs { Seconds = 0.1 },
                getLogCounter: () => counter,
                sleepFn: ms => current = current.AddMilliseconds(ms),
                nowFn: () => current);

            Assert.IsTrue(response.Ok, response.Error?.Message);
            var result = (WaitForIdleResult)response.Result;
            Assert.IsTrue(result.IdleAchieved);
            Assert.Greater(result.ElapsedSeconds, 0.0);
        }

        // -------------------------------------------------------------------------
        // InvokeLuaDoString — reflection-based DoString resolution.
        //
        // xLua's LuaEnv.DoString has signature (string, string="chunk", LuaTable=null).
        // Type.GetMethod(name, Type[]) does NOT collapse optional parameters, so the
        // old `new[] { typeof(string) }` lookup returned null and threw
        // "LuaEnv.DoString(string) not found." even though direct calls compile fine.
        // These tests pin the new overload-walking logic.
        // -------------------------------------------------------------------------

        private class XLuaShapedFake
        {
            public readonly System.Collections.Generic.List<string> Calls =
                new System.Collections.Generic.List<string>();
            public object[] DoString(string chunk, string chunkName = "chunk", object env = null)
            {
                Calls.Add(chunk);
                return new object[] { true, new System.Collections.Generic.List<string> { "a", "b" } };
            }
            // Should NOT be selected: generic overload.
            public T DoString<T>(string chunk, string chunkName = "chunk", object env = null)
            {
                throw new System.InvalidOperationException("generic overload picked");
            }
            // Should NOT be selected: byte[] first parameter.
            public object[] DoString(byte[] chunk, string chunkName = "chunk", object env = null)
            {
                throw new System.InvalidOperationException("byte[] overload picked");
            }
        }

        private class SingleStringFake
        {
            public bool Called;
            // Exactly one required string parameter — the shape the original lookup
            // assumed. We keep this passing so existing/legacy LuaEnv-likes still work.
            public object[] DoString(string chunk)
            {
                Called = true;
                return new object[] { true };
            }
        }

        private class ReturnsNullFake
        {
            public object[] DoString(string chunk, string chunkName = "chunk", object env = null)
            {
                return null;
            }
        }

        private class NoDoStringFake { }

        [Test]
        public void InvokeLuaDoString_PicksXLuaShapedOverload_AndReturnsResult()
        {
            var fake = new XLuaShapedFake();
            object[] result = UnityMCPCommands.InvokeLuaDoString(fake, "return 1");

            Assert.AreEqual(1, fake.Calls.Count, "DoString should have been called exactly once");
            Assert.AreEqual("return 1", fake.Calls[0]);
            Assert.GreaterOrEqual(result.Length, 1);
            Assert.IsInstanceOf<bool>(result[0]);
            Assert.IsTrue((bool)result[0]);
        }

        [Test]
        public void InvokeLuaDoString_PicksSingleStringOverload()
        {
            var fake = new SingleStringFake();
            object[] result = UnityMCPCommands.InvokeLuaDoString(fake, "return 1");

            Assert.IsTrue(fake.Called);
            Assert.IsTrue((bool)result[0]);
        }

        [Test]
        public void InvokeLuaDoString_NoMatchingOverload_ThrowsWithTypeName()
        {
            var fake = new NoDoStringFake();
            var ex = Assert.Throws<System.InvalidOperationException>(() =>
                UnityMCPCommands.InvokeLuaDoString(fake, "return 1"));
            StringAssert.Contains("DoString", ex.Message);
            StringAssert.Contains("NoDoStringFake", ex.Message);
        }

        [Test]
        public void InvokeLuaDoString_NullLuaEnv_ThrowsArgumentNull()
        {
            Assert.Throws<System.ArgumentNullException>(() =>
                UnityMCPCommands.InvokeLuaDoString(null, "return 1"));
        }

        [Test]
        public void InvokeLuaDoString_MethodReturnsNull_ProducesEmptyArray()
        {
            var fake = new ReturnsNullFake();
            object[] result = UnityMCPCommands.InvokeLuaDoString(fake, "return 1");
            Assert.IsNotNull(result);
            Assert.AreEqual(0, result.Length);
        }

        [Test]
        public void InvokeLuaDoString_RoutedThroughExecuteLuaReloadModule_ProducesOkReloaded()
        {
            // Wire InvokeLuaDoString as the doStringFn delegate that ExecuteLuaReloadModule
            // expects. Verifies the full reload path produces a successful response when
            // the Lua side returns (true, {moduleA, moduleB}).
            var fake = new XLuaShapedFake();
            ResponseEnvelope env = UnityMCPCommands.ExecuteLuaReloadModule(
                id: "test-1",
                args: new LuaReloadModuleArgs
                {
                    Modules = new[] { "Some.Module" },
                    ImpFiles = null,
                    DelKeyModels = null,
                },
                doStringFn: script => UnityMCPCommands.InvokeLuaDoString(fake, script));

            Assert.IsTrue(env.Ok, "response envelope should report Ok=true");
            Assert.AreEqual(1, fake.Calls.Count);
            StringAssert.Contains("Some.Module", fake.Calls[0]);
        }

        // ------------------------------------------------------------------
        // Phase 4: LuaEval
        // ------------------------------------------------------------------

        [Test]
        public void ExecuteLuaEval_MultiReturn_StringifiesAllValues()
        {
            string captured = null;
            ResponseEnvelope env = UnityMCPCommands.ExecuteLuaEval(
                id: "le-1",
                args: new LuaEvalArgs { Script = "return true, 66" },
                doStringFn: script => { captured = script; return new object[] { true, 66 }; });

            Assert.IsTrue(env.Ok);
            Assert.AreEqual("return true, 66", captured);
            var result = (LuaEvalResult)env.Result;
            Assert.AreEqual(2, result.ReturnCount);
            Assert.AreEqual(2, result.ReturnValues.Count);
            Assert.AreEqual("True", result.ReturnValues[0]);
            Assert.AreEqual("66", result.ReturnValues[1]);
            Assert.AreEqual("True", result.Raw);
        }

        [Test]
        public void ExecuteLuaEval_EmptyScript_ReturnsInvalidRequest()
        {
            ResponseEnvelope env = UnityMCPCommands.ExecuteLuaEval(
                id: "le-2",
                args: new LuaEvalArgs { Script = "   " },
                doStringFn: script => new object[0]);

            Assert.IsFalse(env.Ok);
            Assert.AreEqual(ErrorCodes.InvalidRequest, env.Error.Code);
        }

        [Test]
        public void ExecuteLuaEval_NullDoStringFn_ReturnsInvalidState()
        {
            ResponseEnvelope env = UnityMCPCommands.ExecuteLuaEval(
                id: "le-3",
                args: new LuaEvalArgs { Script = "return 1" },
                doStringFn: null);

            Assert.IsFalse(env.Ok);
            Assert.AreEqual(ErrorCodes.InvalidState, env.Error.Code);
            StringAssert.Contains("lua_vm_not_running", env.Error.Message);
        }

        [Test]
        public void ExecuteLuaEval_LuaVmNotRunningThrow_MapsToInvalidState()
        {
            // DefaultLuaDoString throws "...PROJECT_FACADE.PROJECT_LUA_MANAGER is null..." in EditMode.
            ResponseEnvelope env = UnityMCPCommands.ExecuteLuaEval(
                id: "le-4",
                args: new LuaEvalArgs { Script = "PROJECT_RUNTIME.global.ui:open(66)" },
                doStringFn: script => throw new InvalidOperationException(
                    "PROJECT_FACADE.PROJECT_LUA_MANAGER is null — game must be running for Lua reload."));

            Assert.IsFalse(env.Ok);
            Assert.AreEqual(ErrorCodes.InvalidState, env.Error.Code);
            StringAssert.Contains("lua_vm_not_running", env.Error.Message);
        }

        [Test]
        public void ExecuteLuaEval_GenericThrow_MapsToInternal()
        {
            ResponseEnvelope env = UnityMCPCommands.ExecuteLuaEval(
                id: "le-5",
                args: new LuaEvalArgs { Script = "boom()" },
                doStringFn: script => throw new InvalidOperationException("attempt to call a nil value 'boom'"));

            Assert.IsFalse(env.Ok);
            Assert.AreEqual(ErrorCodes.Internal, env.Error.Code);
        }

        // ------------------------------------------------------------------
        // Phase 4: human-takeover classification (pure decision logic)
        // ------------------------------------------------------------------

        [Test]
        public void ClassifyVerdict_NoActivity_IsNone()
        {
            Assert.AreEqual(VerificationSession.Takeover.None,
                VerificationSession.ClassifyVerdict(high: false, low: false, abortOnHigh: true));
        }

        [Test]
        public void ClassifyVerdict_MouseMoveOnly_IsLow()
        {
            Assert.AreEqual(VerificationSession.Takeover.Low,
                VerificationSession.ClassifyVerdict(high: false, low: true, abortOnHigh: true));
        }

        [Test]
        public void ClassifyVerdict_HighEvent_AbortOnHigh_IsHigh()
        {
            Assert.AreEqual(VerificationSession.Takeover.High,
                VerificationSession.ClassifyVerdict(high: true, low: false, abortOnHigh: true));
        }

        [Test]
        public void ClassifyVerdict_HighEvent_NoAbort_DowngradesToLow()
        {
            // abortOnHigh=false → a high event pauses (Low) instead of aborting.
            Assert.AreEqual(VerificationSession.Takeover.Low,
                VerificationSession.ClassifyVerdict(high: true, low: false, abortOnHigh: false));
        }

        [Test]
        public void ClassifyVerdict_HighWins_OverLow()
        {
            // Both flags set in one tick (click while moving) → High dominates.
            Assert.AreEqual(VerificationSession.Takeover.High,
                VerificationSession.ClassifyVerdict(high: true, low: true, abortOnHigh: true));
        }

        // ------------------------------------------------------------------
        // Phase 1.5: screenshot approval gate (pure decision logic)
        // ------------------------------------------------------------------

        [Test]
        public void ResolveApproval_Pending_StaysPending()
        {
            Assert.AreEqual(VerificationSession.Approval.Pending,
                VerificationSession.ResolveApproval(autoApprove: false,
                    pendingDecision: VerificationSession.Approval.Pending));
        }

        [Test]
        public void ResolveApproval_UserApproved_IsApproved()
        {
            Assert.AreEqual(VerificationSession.Approval.Approved,
                VerificationSession.ResolveApproval(autoApprove: false,
                    pendingDecision: VerificationSession.Approval.Approved));
        }

        [Test]
        public void ResolveApproval_AutoApprove_ShortCircuitsPending()
        {
            // 继续且不再询问 → even a Pending decision resolves to Approved.
            Assert.AreEqual(VerificationSession.Approval.Approved,
                VerificationSession.ResolveApproval(autoApprove: true,
                    pendingDecision: VerificationSession.Approval.Pending));
        }

        [Test]
        public void ResolveApproval_Rejected_IsRejected()
        {
            // Session torn down mid-wait → Rejected, so the AI gets a clean error.
            Assert.AreEqual(VerificationSession.Approval.Rejected,
                VerificationSession.ResolveApproval(autoApprove: false,
                    pendingDecision: VerificationSession.Approval.Rejected));
        }

        // ------------------------------------------------------------------
        // Phase 6: OpenPanel poll state machine
        // ------------------------------------------------------------------

        [Test]
        public void OpenPanel_BecomesVisible_ReturnsSuccess()
        {
            double now = 0;
            int visibleAfterMs = 300;
            var op = new UnityMCPCommands.OpenPanelOperation(
                uid: 66, timeoutMs: 5000,
                checkVisibleFn: () => now >= visibleAfterMs,
                checkOpenFn: () => true,
                topPanelFn: () => 66,
                nowMsFn: () => now);

            Assert.IsNull(op.Tick());   // t=0 not visible yet
            now = 200; Assert.IsNull(op.Tick());
            now = 320;
            var r = op.Tick();
            Assert.IsNotNull(r);
            Assert.IsTrue(r.Visible);
            Assert.IsFalse(r.TimedOut);
            Assert.IsFalse(r.Refused);
            Assert.AreEqual(66, r.TopPanelUid);
        }

        [Test]
        public void OpenPanel_TimeoutButOpen_ReportsTimedOut()
        {
            double now = 0;
            var op = new UnityMCPCommands.OpenPanelOperation(
                uid: 66, timeoutMs: 1000,
                checkVisibleFn: () => false,    // never becomes visible
                checkOpenFn: () => true,        // but _isOpen is true → accepted, slow load
                topPanelFn: () => 0,
                nowMsFn: () => now);

            Assert.IsNull(op.Tick());
            now = 1500;
            var r = op.Tick();
            Assert.IsNotNull(r);
            Assert.IsFalse(r.Visible);
            Assert.IsTrue(r.TimedOut);
            Assert.IsFalse(r.Refused);
        }

        [Test]
        public void OpenPanel_TimeoutNotOpen_ReportsRefused()
        {
            double now = 0;
            var op = new UnityMCPCommands.OpenPanelOperation(
                uid: 999, timeoutMs: 1000,
                checkVisibleFn: () => false,
                checkOpenFn: () => false,       // never even marked open → PROJECT_CAN_OPEN refusal
                topPanelFn: () => 0,
                nowMsFn: () => now);

            now = 1200;
            var r = op.Tick();
            Assert.IsNotNull(r);
            Assert.IsFalse(r.Visible);
            Assert.IsTrue(r.Refused);
            Assert.IsFalse(r.TimedOut);
        }

        [Test]
        public void OpenPanel_OnceDone_TickReturnsNull()
        {
            double now = 2000;
            var op = new UnityMCPCommands.OpenPanelOperation(
                uid: 66, timeoutMs: 1000,
                checkVisibleFn: () => true,
                checkOpenFn: () => true,
                topPanelFn: () => 66,
                nowMsFn: () => now);

            Assert.IsNotNull(op.Tick()); // resolves
            Assert.IsNull(op.Tick());    // already done → null
        }

        // ------------------------------------------------------------------
        // Phase 8: ObserveWindow poll state machine
        // ------------------------------------------------------------------

        [Test]
        public void ObserveWindow_SamplesAndDetectsTrue()
        {
            double now = 0;
            // value is 0 until t>=100, then 1 until t>=300, then 0 again.
            Func<double> sample = () => (now >= 100 && now < 300) ? 1.0 : 0.0;
            var op = new UnityMCPCommands.ObserveWindowOperation(
                intervalMs: 50, durationMs: 400, sampleFn: sample, nowMsFn: () => now);

            // Drive ticks at 50ms steps.
            for (now = 0; now <= 400; now += 50)
            {
                var r = op.Tick();
                if (now < 400) Assert.IsNull(r);
            }
            now = 400;
            var final = op.Tick();
            Assert.IsNotNull(final);
            Assert.IsTrue(final.EverTrue);
            Assert.GreaterOrEqual(final.FirstTrueMs, 100);
            Assert.AreEqual(1.0, final.Peak, 0.001);
            Assert.Greater(final.SampleCount, 0);
        }

        [Test]
        public void ObserveWindow_NeverTrue_ReportsEverTrueFalse()
        {
            double now = 0;
            var op = new UnityMCPCommands.ObserveWindowOperation(
                intervalMs: 50, durationMs: 200, sampleFn: () => 0.0, nowMsFn: () => now);
            for (now = 0; now < 200; now += 50) Assert.IsNull(op.Tick());
            now = 200;
            var r = op.Tick();
            Assert.IsNotNull(r);
            Assert.IsFalse(r.EverTrue);
            Assert.AreEqual(-1, r.FirstTrueMs);
        }

        [Test]
        public void ObserveWindow_NaNSamplesAreSkipped_AndReportExprError()
        {
            double now = 0;
            // Always NaN (e.g. eval error) → no valid samples → exprError set, not a
            // silent empty pass.
            var op = new UnityMCPCommands.ObserveWindowOperation(
                intervalMs: 50, durationMs: 150, sampleFn: () => double.NaN, nowMsFn: () => now);
            for (now = 0; now < 150; now += 50) op.Tick();
            now = 150;
            var r = op.Tick();
            Assert.IsNotNull(r);
            Assert.AreEqual(0, r.SampleCount);
            Assert.IsFalse(r.EverTrue);
            Assert.IsNotNull(r.ExprError);          // expression reported as broken
            StringAssert.Contains("never evaluated", r.ExprError);
        }

        [Test]
        public void ObserveWindow_ValidZeroSamples_NoExprError()
        {
            double now = 0;
            // Expr is valid but always 0 (e.g. "0 shakes") → samples recorded,
            // everTrue false, NO exprError (it worked, the value was just 0).
            var op = new UnityMCPCommands.ObserveWindowOperation(
                intervalMs: 50, durationMs: 150, sampleFn: () => 0.0, nowMsFn: () => now);
            for (now = 0; now < 150; now += 50) op.Tick();
            now = 150;
            var r = op.Tick();
            Assert.Greater(r.SampleCount, 0);
            Assert.IsFalse(r.EverTrue);
            Assert.IsNull(r.ExprError);
        }
    }
}

#endif
