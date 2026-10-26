// UnityMCPEventBridgeTests.cs — Tests.EditMode/
//
// Tests the pure helpers in UnityMCPEventBridge (filter, path-build, state map).
// Gated by UNITY_TEST_FRAMEWORK_AVAILABLE.

#if UNITY_TEST_FRAMEWORK_AVAILABLE

using NUnit.Framework;
using UnityEditor;
using UnityEngine;

namespace UnityMCP.Bridge.Tests
{
    [TestFixture]
    public class UnityMCPEventBridgeTests
    {
        [Test]
        public void ShouldPassType_ErrorAlwaysPasses_RegardlessOfMin()
        {
            Assert.IsTrue(UnityMCPEventBridge.ShouldPassType(LogType.Error, 4));
            Assert.IsTrue(UnityMCPEventBridge.ShouldPassType(LogType.Assert, 4));
            Assert.IsTrue(UnityMCPEventBridge.ShouldPassType(LogType.Exception, 4));
        }

        [Test]
        public void ShouldPassType_LogBelowMin_Filtered()
        {
            Assert.IsFalse(UnityMCPEventBridge.ShouldPassType(LogType.Log, 1));
            Assert.IsTrue(UnityMCPEventBridge.ShouldPassType(LogType.Warning, 1));
        }

        [Test]
        public void ShouldPassType_LogAtOrAboveMin_Passes()
        {
            Assert.IsTrue(UnityMCPEventBridge.ShouldPassType(LogType.Log, 0));
            Assert.IsTrue(UnityMCPEventBridge.ShouldPassType(LogType.Warning, 0));
        }

        [Test]
        public void MapPlayModeState_AllFourStates()
        {
            Assert.AreEqual("edit",     UnityMCPEventBridge.MapPlayModeState(PlayModeStateChange.EnteredEditMode));
            Assert.AreEqual("entering", UnityMCPEventBridge.MapPlayModeState(PlayModeStateChange.ExitingEditMode));
            Assert.AreEqual("play",     UnityMCPEventBridge.MapPlayModeState(PlayModeStateChange.EnteredPlayMode));
            Assert.AreEqual("exiting",  UnityMCPEventBridge.MapPlayModeState(PlayModeStateChange.ExitingPlayMode));
        }

        [Test]
        public void BuildObjectPath_NullInput_ReturnsNull()
        {
            Assert.IsNull(UnityMCPEventBridge.BuildObjectPath(null));
        }

#if UNITY_INCLUDE_TESTS
        [Test]
        public void MinLevelOrdinalForTest_ParsesAlphaAndNumeric()
        {
            Assert.AreEqual(0, UnityMCPEventBridge.__MinLevelOrdinalForTest(null));
            Assert.AreEqual(0, UnityMCPEventBridge.__MinLevelOrdinalForTest(""));
            Assert.AreEqual(0, UnityMCPEventBridge.__MinLevelOrdinalForTest("log"));
            Assert.AreEqual(1, UnityMCPEventBridge.__MinLevelOrdinalForTest("warning"));
            Assert.AreEqual(1, UnityMCPEventBridge.__MinLevelOrdinalForTest("warn"));
            Assert.AreEqual(2, UnityMCPEventBridge.__MinLevelOrdinalForTest("assert"));
            Assert.AreEqual(3, UnityMCPEventBridge.__MinLevelOrdinalForTest("error"));
            Assert.AreEqual(4, UnityMCPEventBridge.__MinLevelOrdinalForTest("exception"));
            Assert.AreEqual(2, UnityMCPEventBridge.__MinLevelOrdinalForTest("2"));
            // Clamp: out-of-range numeric stays in [0,4]
            Assert.AreEqual(4, UnityMCPEventBridge.__MinLevelOrdinalForTest("9"));
        }
#endif
    }
}

#endif
