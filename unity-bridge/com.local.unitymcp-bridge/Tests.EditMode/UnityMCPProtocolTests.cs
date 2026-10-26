// UnityMCPProtocolTests.cs — Tests.EditMode/
//
// Wire-protocol round-trip tests. Gated by UNITY_TEST_FRAMEWORK_AVAILABLE so Unity
// ignores them; the standalone .NET csproj at
// ~/.claude/mcp-servers/unity-mcp/tests/csharp/UnityMCPBridge.Tests/ defines that
// symbol and compiles this file with mock Unity types.

#if UNITY_TEST_FRAMEWORK_AVAILABLE

using NUnit.Framework;
using Newtonsoft.Json.Linq;
using UnityMCP.Bridge.Protocol;

namespace UnityMCP.Bridge.Tests
{
    [TestFixture]
    public class UnityMCPProtocolTests
    {
        [Test]
        public void ParseRequest_ValidEnvelope_ExtractsFields()
        {
            string line = "{\"id\":\"abc-123\",\"command\":\"Handshake\",\"args\":{\"clientProtocolVersion\":\"1.2.0\"}}";
            RequestEnvelope env = ProtocolSerializer.ParseRequest(line);

            Assert.AreEqual("abc-123", env.Id);
            Assert.AreEqual("Handshake", env.Command);
            Assert.IsNotNull(env.Args);
            Assert.AreEqual("1.2.0", env.Args["clientProtocolVersion"]?.ToObject<string>());
        }

        [Test]
        public void DeserializeArgs_TypedPocoRoundTrip_PreservesFields()
        {
            var env = new RequestEnvelope
            {
                Id = "x",
                Command = "Handshake",
                Args = JObject.FromObject(new HandshakeArgs { ClientProtocolVersion = "1.2.0" }),
            };
            HandshakeArgs args = ProtocolSerializer.DeserializeArgs<HandshakeArgs>(env);
            Assert.AreEqual("1.2.0", args.ClientProtocolVersion);
        }

        [Test]
        public void SerializeResponse_OkEnvelope_OmitsErrorField()
        {
            var env = ProtocolSerializer.OkResponse("id1", new HandshakeResult
            {
                ServerProtocolVersion = "1.2.0",
                EditorVersion = "2022.3.1f1",
                ProjectPath = "/tmp",
            });
            string json = ProtocolSerializer.SerializeResponse(env);

            Assert.IsTrue(json.Contains("\"ok\":true"));
            Assert.IsTrue(json.Contains("\"serverProtocolVersion\":\"1.2.0\""));
            Assert.IsFalse(json.Contains("\"error\""));
        }

        [Test]
        public void SerializeResponse_ErrorEnvelope_OmitsResultField()
        {
            var env = ProtocolSerializer.ErrorResponse("id2", ErrorCodes.InvalidRequest, "bad input");
            string json = ProtocolSerializer.SerializeResponse(env);

            Assert.IsTrue(json.Contains("\"ok\":false"));
            Assert.IsTrue(json.Contains("\"code\":\"INVALID_REQUEST\""));
            Assert.IsTrue(json.Contains("\"message\":\"bad input\""));
            Assert.IsFalse(json.Contains("\"result\""));
        }

        [Test]
        public void BuildNotification_AddsIsoTimestamp()
        {
            var env = ProtocolSerializer.BuildNotification("playmode.changed",
                new PlaymodeChangedPayload { State = "play" });

            Assert.AreEqual("playmode.changed", env.Event);
            Assert.IsNotNull(env.Timestamp);
            // ISO-8601 round-trip "o" format always contains 'T' and ends with 'Z' for UtcNow.
            Assert.IsTrue(env.Timestamp.Contains("T"));
            Assert.IsTrue(env.Timestamp.EndsWith("Z"));
        }

        [Test]
        public void SerializeNotification_PreservesPayloadFields()
        {
            var env = ProtocolSerializer.BuildNotification("log.overflow",
                new LogOverflowPayload { Dropped = 42, Since = "2026-05-12T00:00:00.000Z" });

            string json = ProtocolSerializer.SerializeNotification(env);
            Assert.IsTrue(json.Contains("\"event\":\"log.overflow\""));
            Assert.IsTrue(json.Contains("\"dropped\":42"));
        }

        [Test]
        public void ErrorCodes_AllConstantsDefined()
        {
            Assert.AreEqual("INVALID_REQUEST", ErrorCodes.InvalidRequest);
            Assert.AreEqual("INVALID_STATE", ErrorCodes.InvalidState);
            Assert.AreEqual("EDITOR_BUSY", ErrorCodes.EditorBusy);
            Assert.AreEqual("TIMEOUT", ErrorCodes.Timeout);
            Assert.AreEqual("NOT_FOUND", ErrorCodes.NotFound);
            Assert.AreEqual("INTERNAL", ErrorCodes.Internal);
            Assert.AreEqual("PROTOCOL_VERSION_MISMATCH", ErrorCodes.ProtocolVersionMismatch);
        }
    }
}

#endif
