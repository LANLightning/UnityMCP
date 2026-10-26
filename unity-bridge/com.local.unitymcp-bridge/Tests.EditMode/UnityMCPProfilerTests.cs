// UnityMCPProfilerTests.cs — Tests.EditMode/
// Pure tests for the optional profiler command logic (Execute*-style entry points).
// Gated by UNITY_TEST_FRAMEWORK_AVAILABLE (Unity ignores; standalone csproj compiles).

#if UNITY_TEST_FRAMEWORK_AVAILABLE

using System.Collections.Generic;
using NUnit.Framework;
using UnityMCP.Bridge.Protocol;

namespace UnityMCP.Bridge.Tests
{
    [TestFixture]
    public class UnityMCPProfilerTests
    {
        [Test]
        public void AggregateMetric_KnownVector_ComputesStats()
        {
            // 10,20,30,40,50 → min10 max50 avg30 median30 p95~50
            var samples = new List<double> { 10, 20, 30, 40, 50 };
            ProfilerMetricAgg agg = UnityMCPCommands.AggregateMetric(samples);

            Assert.AreEqual(10, agg.Min, 1e-6);
            Assert.AreEqual(50, agg.Max, 1e-6);
            Assert.AreEqual(30, agg.Avg, 1e-6);
            Assert.AreEqual(30, agg.Median, 1e-6);
            Assert.AreEqual(5, agg.Samples);
            Assert.GreaterOrEqual(agg.P95, 40);  // p95 of 5 points lands at/near the top
            Assert.LessOrEqual(agg.P95, 50);
        }

        [Test]
        public void AggregateMetric_EmptyVector_ReturnsZeros()
        {
            ProfilerMetricAgg agg = UnityMCPCommands.AggregateMetric(new List<double>());
            Assert.AreEqual(0, agg.Samples);
            Assert.AreEqual(0, agg.Avg, 1e-6);
        }

        [Test]
        public void AggregateMetric_SingleValue_AllEqualThatValue()
        {
            ProfilerMetricAgg agg = UnityMCPCommands.AggregateMetric(new List<double> { 16.6 });
            Assert.AreEqual(16.6, agg.Min, 1e-6);
            Assert.AreEqual(16.6, agg.Max, 1e-6);
            Assert.AreEqual(16.6, agg.Median, 1e-6);
            Assert.AreEqual(16.6, agg.P95, 1e-6);
            Assert.AreEqual(1, agg.Samples);
        }

        [Test]
        public void ScenarioState_ApplyThenRestore_RoundTrips()
        {
            // Simulated Editor knobs.
            int quality = 5;
            int targetFps = -1;
            int gvWidth = 1280, gvHeight = 720;

            var knobs = new UnityMCPCommands.ScenarioKnobs
            {
                GetQuality = () => quality,
                SetQuality = v => quality = v,
                GetTargetFps = () => targetFps,
                SetTargetFps = v => targetFps = v,
                GetGameView = () => (gvWidth, gvHeight),
                SetGameView = (w, h) => { gvWidth = w; gvHeight = h; },
            };

            // Snapshot current, apply a low-end scenario.
            UnityMCPCommands.ScenarioSnapshot snap = UnityMCPCommands.CaptureScenarioSnapshot(knobs);
            UnityMCPCommands.ApplyScenario(knobs, quality: 1, targetFps: 30, width: 1920, height: 1080);
            Assert.AreEqual(1, quality);
            Assert.AreEqual(30, targetFps);
            Assert.AreEqual(1920, gvWidth);

            // Restore must put everything back to the captured values.
            UnityMCPCommands.RestoreScenarioSnapshot(knobs, snap);
            Assert.AreEqual(5, quality);
            Assert.AreEqual(-1, targetFps);
            Assert.AreEqual(1280, gvWidth);
            Assert.AreEqual(720, gvHeight);
        }

        [Test]
        public void ApplyScenario_NullKnobs_NoThrow()
        {
            // Defensive: applying with no overrides leaves state untouched.
            int quality = 3;
            var knobs = new UnityMCPCommands.ScenarioKnobs
            {
                GetQuality = () => quality, SetQuality = v => quality = v,
                GetTargetFps = () => -1, SetTargetFps = _ => { },
                GetGameView = () => (800, 600), SetGameView = (_, __) => { },
            };
            UnityMCPCommands.ApplyScenario(knobs, quality: null, targetFps: null, width: null, height: null);
            Assert.AreEqual(3, quality);  // unchanged
        }

        [Test]
        public void ExecuteProfilerCapabilities_NotPlayMode_NotAnalyzable()
        {
            ProfilerCapabilitiesResult r = UnityMCPCommands.ExecuteProfilerCapabilities(
                isPlayMode: false,
                recordersAvailable: () => true,
                memoryProfilerInstalled: () => false);

            Assert.IsFalse(r.Analyzable);
            Assert.AreEqual("not in PlayMode", r.Reason);
            StringAssert.Contains("enter_playmode", r.Remedy);
            Assert.IsFalse(r.PlayMode);
        }

        [Test]
        public void ExecuteProfilerCapabilities_PlayModeWithRecorders_Analyzable()
        {
            ProfilerCapabilitiesResult r = UnityMCPCommands.ExecuteProfilerCapabilities(
                isPlayMode: true,
                recordersAvailable: () => true,
                memoryProfilerInstalled: () => true);

            Assert.IsTrue(r.Analyzable);
            Assert.IsTrue(r.PlayMode);
            Assert.IsTrue(r.MemoryProfilerInstalled);
        }

        [Test]
        public void ExecuteProfilerCapabilities_PlayModeNoRecorders_NotAnalyzable()
        {
            ProfilerCapabilitiesResult r = UnityMCPCommands.ExecuteProfilerCapabilities(
                isPlayMode: true,
                recordersAvailable: () => false,
                memoryProfilerInstalled: () => false);

            Assert.IsFalse(r.Analyzable);
            StringAssert.Contains("recorder", r.Reason.ToLowerInvariant());
        }

        [Test]
        public void ExecuteProfilerCapabilities_Analyzable_PopulatesAvailableMetrics()
        {
            // §6.3: the analyzable path must report which counters are available.
            var probed = new Newtonsoft.Json.Linq.JObject
            {
                ["cpu"] = new Newtonsoft.Json.Linq.JArray("Main Thread", "Draw Calls Count"),
                ["memory"] = new Newtonsoft.Json.Linq.JArray("Total Used Memory"),
            };
            ProfilerCapabilitiesResult r = UnityMCPCommands.ExecuteProfilerCapabilities(
                isPlayMode: true,
                recordersAvailable: () => true,
                memoryProfilerInstalled: () => false,
                availableMetricsProbe: () => probed);

            Assert.IsTrue(r.Analyzable);
            Assert.IsNotNull(r.AvailableMetrics);
            Assert.AreEqual(2, ((Newtonsoft.Json.Linq.JArray)r.AvailableMetrics["cpu"]).Count);
        }

        [Test]
        public void ExecuteMemorySnapshot_PackageAbsent_ReturnsUnavailable()
        {
            MemorySnapshotResult r = UnityMCPCommands.ExecuteMemorySnapshot(
                packageInstalled: false,
                captureFn: () => ("ignored", 0L));

            Assert.IsFalse(r.Available);
            StringAssert.Contains("memoryprofiler", r.Reason.ToLowerInvariant());
            Assert.IsNotNull(r.Remedy);
        }

        [Test]
        public void ExecuteMemorySnapshot_PackagePresent_ReturnsPathAndSize()
        {
            MemorySnapshotResult r = UnityMCPCommands.ExecuteMemorySnapshot(
                packageInstalled: true,
                captureFn: () => ("/tmp/x.snap", 4096L));

            Assert.IsTrue(r.Available);
            Assert.AreEqual("/tmp/x.snap", r.Path);
            Assert.AreEqual(4096L, r.SizeBytes);
        }

        [Test]
        public void PendingRestore_AfterApply_IsArmed_AndClearsOnRestore()
        {
            // The static pending-restore guard must be ARMED between apply and
            // restore so a domain-reload/playmode-exit hook can fire it (C4).
            int quality = 7;
            var knobs = new UnityMCPCommands.ScenarioKnobs
            {
                GetQuality = () => quality, SetQuality = v => quality = v,
                GetTargetFps = () => -1, SetTargetFps = _ => { },
                GetGameView = () => (1280, 720), SetGameView = (_, __) => { },
            };
            UnityMCPCommands.ResetPendingRestoreForTest();
            Assert.IsFalse(UnityMCPCommands.HasPendingRestore);

            UnityMCPCommands.ArmPendingRestore(knobs, UnityMCPCommands.CaptureScenarioSnapshot(knobs));
            UnityMCPCommands.ApplyScenario(knobs, quality: 1, targetFps: null, width: null, height: null);
            Assert.IsTrue(UnityMCPCommands.HasPendingRestore);
            Assert.AreEqual(1, quality);

            // Defensive restore (what the reload/exit hook calls) puts it back AND disarms.
            UnityMCPCommands.RunPendingRestoreIfAny();
            Assert.AreEqual(7, quality);
            Assert.IsFalse(UnityMCPCommands.HasPendingRestore);
        }
    }
}

#endif
