#if UNITY_TEST_FRAMEWORK_AVAILABLE

using System.Linq;
using NUnit.Framework;

namespace UnityMCP.Bridge.Tests
{
    [TestFixture]
    public sealed class UnityMCPListenerPortPolicyTests
    {
        [Test]
        public void DefaultPort_AllowsBoundedFallback()
        {
            ListenerPortOptions options = UnityMCPListenerPortPolicy.Resolve(null, null, null);

            Assert.AreEqual(17873, options.PreferredPort);
            Assert.IsTrue(options.AllowFallback);
            CollectionAssert.AreEqual(
                Enumerable.Range(17873, 11),
                UnityMCPListenerPortPolicy.EnumerateCandidates(options));
        }

        [Test]
        public void ExplicitPort_IsStrictUnlessFallbackIsOptedIn()
        {
            ListenerPortOptions strict = UnityMCPListenerPortPolicy.Resolve("19001", null, null);
            ListenerPortOptions optedIn = UnityMCPListenerPortPolicy.Resolve("19001", "true", "2");

            CollectionAssert.AreEqual(
                new[] { 19001 },
                UnityMCPListenerPortPolicy.EnumerateCandidates(strict));
            CollectionAssert.AreEqual(
                new[] { 19001, 19002, 19003 },
                UnityMCPListenerPortPolicy.EnumerateCandidates(optedIn));
        }

        [Test]
        public void CandidatePorts_NeverExceedTcpPortLimit()
        {
            ListenerPortOptions options = UnityMCPListenerPortPolicy.Resolve("65534", "1", "10");

            CollectionAssert.AreEqual(
                new[] { 65534, 65535 },
                UnityMCPListenerPortPolicy.EnumerateCandidates(options));
        }

        [TestCase("not-a-port")]
        [TestCase("0")]
        [TestCase("65536")]
        public void InvalidExplicitPort_FailsClosed(string configuredPort)
        {
            Assert.Throws<System.ArgumentException>(() =>
                UnityMCPListenerPortPolicy.Resolve(configuredPort, null, null));
        }

        [Test]
        public void RetryDelay_UsesExponentialBackoffWithCap()
        {
            Assert.AreEqual(0.25, UnityMCPListenerPortPolicy.ComputeRetryDelaySeconds(1));
            Assert.AreEqual(0.5, UnityMCPListenerPortPolicy.ComputeRetryDelaySeconds(2));
            Assert.AreEqual(30.0, UnityMCPListenerPortPolicy.ComputeRetryDelaySeconds(20));
        }

        [Test]
        public void RecoveryState_SchedulesAndResetsDeterministically()
        {
            var recovery = new ListenerRecoveryState();

            Assert.AreEqual(0.25, recovery.Schedule(100.0));
            Assert.AreEqual(1, recovery.Attempts);
            Assert.AreEqual(100.25, recovery.NextRetryAt);

            Assert.AreEqual(0.5, recovery.Schedule(200.0));
            Assert.AreEqual(2, recovery.Attempts);
            Assert.AreEqual(200.5, recovery.NextRetryAt);

            recovery.Reset();
            Assert.AreEqual(0, recovery.Attempts);
            Assert.AreEqual(0.0, recovery.NextRetryAt);
        }
    }
}

#endif
