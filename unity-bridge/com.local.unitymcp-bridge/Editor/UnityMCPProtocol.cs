// UnityMCPProtocol.cs
// Wire-protocol POCOs + ProtocolSerializer + ProtocolConstants for the
// UnityMCP Editor bridge. Single source of truth for the wire format is
// protocol.schema.json — ProtocolVersion is loaded from there at static init.
//
// ProtocolVersion is NOT hand-written here. It is loaded from static init from
// the schema file. If the schema cannot be located the version falls back to
// "unknown" and a warning is logged.

using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace UnityMCP.Bridge.Protocol
{
    // ---------------------------------------------------------------------------
    // Protocol constants
    // ---------------------------------------------------------------------------

    /// <summary>
    /// Wire-protocol constants. ProtocolVersion is derived from protocol.schema.json at
    /// static init time. If the schema file cannot be located, ProtocolVersion falls back
    /// to "unknown" and a warning is logged — the server will still start but a
    /// PROTOCOL_VERSION_MISMATCH error may be returned on Handshake.
    /// </summary>
    public static class ProtocolConstants
    {
        /// <summary>
        /// Protocol version derived from protocol.schema.json at module load time.
        /// Must match the "protocolVersion" field in that file.
        /// </summary>
        public static string ProtocolVersion { get; private set; }

        static ProtocolConstants()
        {
            ProtocolVersion = LoadProtocolVersionFromSchema();
        }

        private static string LoadProtocolVersionFromSchema()
        {
            try
            {
                string schemaPath = ResolveSchemaPath();
                if (schemaPath == null)
                {
                    Debug.LogWarning("[UnityMCP] Could not locate protocol.schema.json. " +
                                     "ProtocolVersion will be reported as 'unknown'. " +
                                     "Ensure the package is installed as a UPM package.");
                    return "unknown";
                }

                string json = File.ReadAllText(schemaPath);
                var doc = JObject.Parse(json);
                var version = doc["protocolVersion"]?.ToObject<string>();

                if (string.IsNullOrEmpty(version))
                {
                    Debug.LogWarning("[UnityMCP] protocol.schema.json found but 'protocolVersion' " +
                                     "field is missing or empty. ProtocolVersion will be 'unknown'.");
                    return "unknown";
                }

                return version;
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[UnityMCP] Exception while loading protocol.schema.json: {ex.Message}. " +
                                 "ProtocolVersion will be 'unknown'.");
                return "unknown";
            }
        }

        private static string ResolveSchemaPath()
        {
            // Strategy 1: AssetDatabase lookup (works in Editor context).
            try
            {
#if UNITY_EDITOR
                string[] guids = UnityEditor.AssetDatabase.FindAssets(
                    "protocol.schema t:TextAsset",
                    new[] { "Packages/com.local.unitymcp-bridge" });

                foreach (string guid in guids)
                {
                    string assetPath = UnityEditor.AssetDatabase.GUIDToAssetPath(guid);
                    if (assetPath.EndsWith("protocol.schema.json", StringComparison.OrdinalIgnoreCase))
                    {
                        string fullPath = Path.GetFullPath(
                            Path.Combine(Application.dataPath, "..", assetPath));
                        if (File.Exists(fullPath))
                            return fullPath;
                    }
                }
#endif
            }
            catch
            {
                // AssetDatabase not available yet (e.g., during domain reload boot)
            }

            // Strategy 2: Walk up from assembly location to find Packages/<our-package>/protocol.schema.json.
            try
            {
                string assemblyLocation = Assembly.GetExecutingAssembly().Location;
                DirectoryInfo dir = new DirectoryInfo(Path.GetDirectoryName(assemblyLocation) ?? ".");
                for (int i = 0; i < 6 && dir != null; i++, dir = dir.Parent)
                {
                    string candidate = Path.Combine(dir.FullName, "Packages",
                        "com.local.unitymcp-bridge", "protocol.schema.json");
                    if (File.Exists(candidate))
                        return candidate;
                }
            }
            catch
            {
                // Ignore filesystem errors
            }

            return null;
        }
    }

    // ---------------------------------------------------------------------------
    // Error codes
    // ---------------------------------------------------------------------------

    public static class ErrorCodes
    {
        public const string InvalidRequest          = "INVALID_REQUEST";
        public const string InvalidState            = "INVALID_STATE";
        public const string EditorBusy              = "EDITOR_BUSY";
        public const string Timeout                 = "TIMEOUT";
        public const string NotFound                = "NOT_FOUND";
        public const string Internal                = "INTERNAL";
        public const string ProtocolVersionMismatch = "PROTOCOL_VERSION_MISMATCH";
    }

    // ---------------------------------------------------------------------------
    // Envelopes
    // ---------------------------------------------------------------------------

    [Serializable]
    public class RequestEnvelope
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("command")] public string Command { get; set; }
        [JsonProperty("args")] public JObject Args { get; set; }
    }

    [Serializable]
    public class ResponseEnvelope
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("ok")] public bool Ok { get; set; }
        [JsonProperty("result", NullValueHandling = NullValueHandling.Ignore)]
        public object Result { get; set; }
        [JsonProperty("error", NullValueHandling = NullValueHandling.Ignore)]
        public ErrorShape Error { get; set; }
    }

    [Serializable]
    public class ErrorShape
    {
        [JsonProperty("code")] public string Code { get; set; }
        [JsonProperty("message")] public string Message { get; set; }
    }

    [Serializable]
    public class NotificationEnvelope
    {
        [JsonProperty("event")] public string Event { get; set; }
        [JsonProperty("payload")] public object Payload { get; set; }
        [JsonProperty("timestamp")] public string Timestamp { get; set; }
    }

    // ---------------------------------------------------------------------------
    // Command args POCOs
    // ---------------------------------------------------------------------------

    [Serializable]
    public class HandshakeArgs
    {
        [JsonProperty("clientProtocolVersion")] public string ClientProtocolVersion { get; set; }
        [JsonProperty("clientId")] public string ClientId { get; set; }
        [JsonProperty("clientInstanceId")] public string ClientInstanceId { get; set; }
        [JsonProperty("clientProcessId")] public int? ClientProcessId { get; set; }
        [JsonProperty("expectedBridgeId")] public string ExpectedBridgeId { get; set; }
        [JsonProperty("forceTakeover")] public bool? ForceTakeover { get; set; }
        [JsonProperty("authProof")] public string AuthProof { get; set; }
        [JsonProperty("expectedBridgeInstanceId")] public string ExpectedBridgeInstanceId { get; set; }
        [JsonProperty("expectedConnectionGeneration")] public int? ExpectedConnectionGeneration { get; set; }
    }

    [Serializable]
    public class RunEditMethodArgs
    {
        [JsonProperty("typeName")] public string TypeName { get; set; }
        [JsonProperty("methodName")] public string MethodName { get; set; }
        [JsonProperty("args")] public object[] Args { get; set; }
    }

    [Serializable]
    public class GetSceneStateArgs
    {
        [JsonProperty("scene")] public string Scene { get; set; }
    }

    [Serializable]
    public class CaptureScreenshotArgs
    {
        [JsonProperty("width")] public int? Width { get; set; }
        [JsonProperty("height")] public int? Height { get; set; }
        [JsonProperty("supersize")] public int? Supersize { get; set; }
    }

    [Serializable]
    public class TailEditorLogArgs
    {
        [JsonProperty("sinceLineNumber")] public int? SinceLineNumber { get; set; }
        [JsonProperty("maxLines")] public int? MaxLines { get; set; }
    }

    [Serializable]
    public class QueryGameObjectArgs
    {
        [JsonProperty("instanceId")] public int? InstanceId { get; set; }
        [JsonProperty("path")] public string Path { get; set; }
    }

    // ----- Phase 2 command args -----

    [Serializable]
    public class OpenSceneArgs
    {
        [JsonProperty("scenePath")] public string ScenePath { get; set; }
        [JsonProperty("mode")] public string Mode { get; set; }
    }

    [Serializable]
    public class RefreshAssetsArgs
    {
        [JsonProperty("waitForCompile")] public bool? WaitForCompile { get; set; }
        [JsonProperty("timeoutMs")] public int? TimeoutMs { get; set; }
    }

    [Serializable]
    public class PingObjectArgs
    {
        [JsonProperty("instanceId")] public int? InstanceId { get; set; }
        [JsonProperty("path")] public string Path { get; set; }
    }

    [Serializable]
    public class SetGameViewSizeArgs
    {
        [JsonProperty("width")] public int Width { get; set; }
        [JsonProperty("height")] public int Height { get; set; }
    }

    [Serializable]
    public class WaitForIdleArgs
    {
        [JsonProperty("seconds")] public double Seconds { get; set; }
        [JsonProperty("source")] public string Source { get; set; }
    }

    // ----- Phase 3 (protocol 1.3.0) command args -----

    [Serializable]
    public class LuaReloadModuleArgs
    {
        [JsonProperty("modules")] public string[] Modules { get; set; }
        [JsonProperty("imp_files")] public string[] ImpFiles { get; set; }
        [JsonProperty("del_key_models")] public string[] DelKeyModels { get; set; }
    }

    [Serializable]
    public class CsharpReloadScriptsArgs
    {
        [JsonProperty("force")] public bool Force { get; set; }
    }

    // ----- Phase 4 (protocol 1.4.0) command args -----

    [Serializable]
    public class LuaEvalArgs
    {
        [JsonProperty("script")] public string Script { get; set; }
        [JsonProperty("chunkName")] public string ChunkName { get; set; }
    }

    [Serializable]
    public class StartVerificationSessionArgs
    {
        [JsonProperty("note")] public string Note { get; set; }
        [JsonProperty("pauseSilenceMs")] public int? PauseSilenceMs { get; set; }
        [JsonProperty("abortOnHigh")] public bool? AbortOnHigh { get; set; }
        [JsonProperty("gateScreenshots")] public bool? GateScreenshots { get; set; }
    }

    [Serializable]
    public class EndVerificationSessionArgs
    {
        [JsonProperty("sessionId")] public string SessionId { get; set; }
    }

    [Serializable]
    public class ListUINodesArgs
    {
        [JsonProperty("panelUid")] public int? PanelUid { get; set; }
        [JsonProperty("layers")] public string[] Layers { get; set; }
        [JsonProperty("includeAll")] public bool? IncludeAll { get; set; }
        [JsonProperty("maxNodes")] public int? MaxNodes { get; set; }
        [JsonProperty("pathPrefix")] public string PathPrefix { get; set; }
    }

    [Serializable]
    public class ResolveLocalizationArgs
    {
        [JsonProperty("ids")] public int[] Ids { get; set; }
        [JsonProperty("language")] public string Language { get; set; }
    }

    [Serializable]
    public class OpenPanelArgs
    {
        [JsonProperty("uid")] public int Uid { get; set; }
        [JsonProperty("info")] public JObject Info { get; set; }
        [JsonProperty("timeoutMs")] public int? TimeoutMs { get; set; }
        [JsonProperty("verify")] public bool? Verify { get; set; }
    }

    // ----- Phase 6 (protocol 1.7.0) simulate suite -----
    // Node locator shared by SimulateClick / SetInputText: a panel uid plus EITHER a
    // refKey (panel PROJECT_NODE_REFERENCE key) OR a transform path relative to the panel root.

    [Serializable]
    public class SimulateClickArgs
    {
        [JsonProperty("panelUid")] public int PanelUid { get; set; }
        [JsonProperty("refKey")] public string RefKey { get; set; }
        [JsonProperty("path")] public string Path { get; set; }
        [JsonProperty("mode")] public string Mode { get; set; }   // "raycast" (default) | "force"
    }

    [Serializable]
    public class SimulateKeyArgs
    {
        [JsonProperty("actionPath")] public string ActionPath { get; set; }
    }

    [Serializable]
    public class SetInputTextArgs
    {
        [JsonProperty("panelUid")] public int PanelUid { get; set; }
        [JsonProperty("refKey")] public string RefKey { get; set; }
        [JsonProperty("path")] public string Path { get; set; }
        [JsonProperty("text")] public string Text { get; set; }
    }

    // ---------------------------------------------------------------------------
    // Command result POCOs
    // ---------------------------------------------------------------------------

    [Serializable]
    public class HandshakeResult
    {
        [JsonProperty("serverProtocolVersion")] public string ServerProtocolVersion { get; set; }
        [JsonProperty("editorVersion")] public string EditorVersion { get; set; }
        [JsonProperty("projectPath")] public string ProjectPath { get; set; }
        [JsonProperty("bridgeId")] public string BridgeId { get; set; }
        [JsonProperty("bridgeInstanceId")] public string BridgeInstanceId { get; set; }
        [JsonProperty("listenerProcessId")] public int ListenerProcessId { get; set; }
        [JsonProperty("listenerPort")] public int ListenerPort { get; set; }
        [JsonProperty("connectionGeneration")] public int ConnectionGeneration { get; set; }
        [JsonProperty("clientId")] public string ClientId { get; set; }
        [JsonProperty("clientInstanceId")] public string ClientInstanceId { get; set; }
    }

    [Serializable]
    public class PlaymodeResult
    {
        [JsonProperty("state")] public string State { get; set; }
    }

    [Serializable]
    public class RunEditMethodResult
    {
        [JsonProperty("returnValue", NullValueHandling = NullValueHandling.Ignore)]
        public object ReturnValue { get; set; }
        [JsonProperty("capturedLogs")] public List<string> CapturedLogs { get; set; }
    }

    [Serializable]
    public class GetSceneStateResult
    {
        [JsonProperty("rootObjects")] public List<GameObjectSummary> RootObjects { get; set; }
    }

    [Serializable]
    public class GameObjectSummary
    {
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("instanceId")] public int InstanceId { get; set; }
        [JsonProperty("active")] public bool Active { get; set; }
        [JsonProperty("components")] public List<string> Components { get; set; }
        [JsonProperty("childCount")] public int ChildCount { get; set; }
    }

    [Serializable]
    public class CaptureScreenshotResult
    {
        [JsonProperty("pngBase64")] public string PngBase64 { get; set; }
    }

    [Serializable]
    public class TailEditorLogResult
    {
        [JsonProperty("lines")] public List<LogLine> Lines { get; set; }
    }

    [Serializable]
    public class LogLine
    {
        [JsonProperty("logType")] public string LogType { get; set; }
        [JsonProperty("message")] public string Message { get; set; }
        [JsonProperty("stackTrace", NullValueHandling = NullValueHandling.Ignore)]
        public string StackTrace { get; set; }
        [JsonProperty("lineNumber")] public int LineNumber { get; set; }
    }

    [Serializable]
    public class QueryGameObjectResult
    {
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("instanceId")] public int InstanceId { get; set; }
        [JsonProperty("active")] public bool Active { get; set; }
        [JsonProperty("components")] public List<ComponentDetails> Components { get; set; }
    }

    [Serializable]
    public class ComponentDetails
    {
        [JsonProperty("type")] public string Type { get; set; }
        [JsonProperty("fields")] public Dictionary<string, object> Fields { get; set; }
    }

    // ----- Phase 2 command results -----

    [Serializable]
    public class OpenSceneResult
    {
        [JsonProperty("sceneName")] public string SceneName { get; set; }
        [JsonProperty("scenePath")] public string ScenePath { get; set; }
        [JsonProperty("mode")] public string Mode { get; set; }
        [JsonProperty("isLoaded")] public bool IsLoaded { get; set; }
    }

    [Serializable]
    public class RefreshAssetsResult
    {
        [JsonProperty("refreshed")] public bool Refreshed { get; set; }
        [JsonProperty("compileSucceeded")] public bool CompileSucceeded { get; set; }
        [JsonProperty("durationMs")] public double DurationMs { get; set; }
    }

    [Serializable]
    public class PingObjectResult
    {
        [JsonProperty("instanceId", NullValueHandling = NullValueHandling.Ignore)]
        public int? InstanceId { get; set; }
        [JsonProperty("path", NullValueHandling = NullValueHandling.Ignore)]
        public string Path { get; set; }
        [JsonProperty("found")] public bool Found { get; set; }
    }

    [Serializable]
    public class SetGameViewSizeResult
    {
        [JsonProperty("width")] public int Width { get; set; }
        [JsonProperty("height")] public int Height { get; set; }
        [JsonProperty("applied")] public bool Applied { get; set; }
    }

    [Serializable]
    public class WaitForIdleResult
    {
        [JsonProperty("idleAchieved")] public bool IdleAchieved { get; set; }
        [JsonProperty("elapsedSeconds")] public double ElapsedSeconds { get; set; }
        [JsonProperty("lastEventAt", NullValueHandling = NullValueHandling.Ignore)]
        public string LastEventAt { get; set; }
    }

    // ----- Phase 3 (protocol 1.3.0) command results -----

    [Serializable]
    public class LuaReloadModuleResult
    {
        [JsonProperty("ok")] public bool Ok { get; set; }
        [JsonProperty("reloaded")] public List<string> Reloaded { get; set; }
        [JsonProperty("error", NullValueHandling = NullValueHandling.Ignore)]
        public string Error { get; set; }
    }

    [Serializable]
    public class CsharpReloadScriptsResult
    {
        [JsonProperty("reload_id")] public string ReloadId { get; set; }
        [JsonProperty("force_was_used")] public bool ForceWasUsed { get; set; }
    }

    [Serializable]
    public class GetPlayStateResult
    {
        [JsonProperty("state")] public string State { get; set; }
    }

    [Serializable]
    public class GetCompileStatusResult
    {
        [JsonProperty("hasErrors")] public bool HasErrors { get; set; }
        [JsonProperty("errors")] public List<string> Errors { get; set; }
    }

    // ----- Phase 4 (protocol 1.4.0) command results -----

    [Serializable]
    public class LuaEvalResult
    {
        [JsonProperty("ok")] public bool Ok { get; set; }
        [JsonProperty("returnCount")] public int ReturnCount { get; set; }
        [JsonProperty("returnValues")] public List<string> ReturnValues { get; set; }
        [JsonProperty("raw", NullValueHandling = NullValueHandling.Ignore)]
        public string Raw { get; set; }
    }

    [Serializable]
    public class StartVerificationSessionResult
    {
        [JsonProperty("sessionId")] public string SessionId { get; set; }
        [JsonProperty("started")] public bool Started { get; set; }
    }

    [Serializable]
    public class EndVerificationSessionResult
    {
        [JsonProperty("ended")] public bool Ended { get; set; }
        [JsonProperty("lowEvents")] public int LowEvents { get; set; }
        [JsonProperty("highEvents")] public int HighEvents { get; set; }
    }

    [Serializable]
    public class GetVerificationSessionStateResult
    {
        [JsonProperty("armed")] public bool Armed { get; set; }
        [JsonProperty("sessionId", NullValueHandling = NullValueHandling.Ignore)]
        public string SessionId { get; set; }
        [JsonProperty("autoStarted")] public bool AutoStarted { get; set; }
        [JsonProperty("gateScreenshots")] public bool GateScreenshots { get; set; }
    }

    [Serializable]
    public class UINodeDto
    {
        [JsonProperty("index")] public int Index { get; set; }
        [JsonProperty("instanceId")] public int InstanceId { get; set; }
        [JsonProperty("panelUid")] public int PanelUid { get; set; }
        [JsonProperty("layer")] public string Layer { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("path")] public string Path { get; set; }
        [JsonProperty("type")] public string Type { get; set; }
        [JsonProperty("text", NullValueHandling = NullValueHandling.Ignore)]
        public string Text { get; set; }
        [JsonProperty("textId", NullValueHandling = NullValueHandling.Ignore)]
        public string TextId { get; set; }
        [JsonProperty("visible")] public bool Visible { get; set; }
        [JsonProperty("clickable")] public bool Clickable { get; set; }
        [JsonProperty("screenX")] public float ScreenX { get; set; }
        [JsonProperty("screenY")] public float ScreenY { get; set; }
        [JsonProperty("screenW")] public float ScreenW { get; set; }
        [JsonProperty("screenH")] public float ScreenH { get; set; }
    }

    [Serializable]
    public class ListUINodesResult
    {
        [JsonProperty("gameViewWidth")] public int GameViewWidth { get; set; }
        [JsonProperty("gameViewHeight")] public int GameViewHeight { get; set; }
        [JsonProperty("nodeCount")] public int NodeCount { get; set; }
        [JsonProperty("nodes")] public List<UINodeDto> Nodes { get; set; }
    }

    [Serializable]
    public class LocalizationEntryDto
    {
        [JsonProperty("id")] public int Id { get; set; }
        [JsonProperty("text", NullValueHandling = NullValueHandling.Ignore)]
        public string Text { get; set; }
    }

    [Serializable]
    public class ResolveLocalizationResult
    {
        [JsonProperty("language")] public string Language { get; set; }
        [JsonProperty("entries")] public List<LocalizationEntryDto> Entries { get; set; }
    }

    [Serializable]
    public class OpenPanelResult
    {
        [JsonProperty("uid")] public int Uid { get; set; }
        [JsonProperty("opened")] public bool Opened { get; set; }     // open() call was issued
        [JsonProperty("visible")] public bool Visible { get; set; }   // PROJECT_PANEL_VISIBLE(uid) == true
        [JsonProperty("isOpen")] public bool IsOpen { get; set; }     // PROJECT_PANEL_OPEN(uid) (_isOpen)
        [JsonProperty("topPanelUid")] public int TopPanelUid { get; set; }
        [JsonProperty("timedOut")] public bool TimedOut { get; set; }
        [JsonProperty("refused")] public bool Refused { get; set; }   // PROJECT_CAN_OPEN silent no-op inferred
        [JsonProperty("elapsedMs")] public double ElapsedMs { get; set; }
    }

    [Serializable]
    public class SimulateClickResult
    {
        [JsonProperty("clicked")] public bool Clicked { get; set; }
        [JsonProperty("mode")] public string Mode { get; set; }
        [JsonProperty("nodeFound")] public bool NodeFound { get; set; }
        [JsonProperty("note", NullValueHandling = NullValueHandling.Ignore)]
        public string Note { get; set; }
    }

    [Serializable]
    public class SimulateKeyResult
    {
        [JsonProperty("triggered")] public bool Triggered { get; set; }
        [JsonProperty("actionPath")] public string ActionPath { get; set; }
        [JsonProperty("note", NullValueHandling = NullValueHandling.Ignore)]
        public string Note { get; set; }
    }

    [Serializable]
    public class SetInputTextResult
    {
        [JsonProperty("applied")] public bool Applied { get; set; }
        [JsonProperty("nodeFound")] public bool NodeFound { get; set; }
        [JsonProperty("note", NullValueHandling = NullValueHandling.Ignore)]
        public string Note { get; set; }
    }

    // ----- Phase 8 (protocol 1.8.0): non-UI observe/operate -----
    // observe/list commands return a Lua-built JSON string verbatim (the snippet is
    // fixed/audited, so the AI reads named fields without authoring Lua). The bridge
    // parses it back to a JToken so the result is structured JSON on the wire, not a
    // string-wrapped blob.

    [Serializable]
    public class ObserveCameraArgs
    {
        [JsonProperty("targetPos")] public double[] TargetPos { get; set; } // optional [x,y,z]
    }

    [Serializable]
    public class ListEntitiesArgs
    {
        [JsonProperty("actorType")] public string ActorType { get; set; }
        [JsonProperty("className")] public string ClassName { get; set; }
        [JsonProperty("templateId")] public int? TemplateId { get; set; }
        [JsonProperty("summary")] public bool? Summary { get; set; }
        [JsonProperty("maxEntities")] public int? MaxEntities { get; set; }
    }

    [Serializable]
    public class ObserveEntityArgs
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("actorId")] public int? ActorId { get; set; }
        [JsonProperty("templateId")] public int? TemplateId { get; set; }
    }

    [Serializable]
    public class ObserveEffectArgs
    {
        [JsonProperty("generatorId")] public int GeneratorId { get; set; }
        [JsonProperty("effectId")] public int EffectId { get; set; }
    }

    [Serializable]
    public class ObserveWindowArgs
    {
        [JsonProperty("luaExpr")] public string LuaExpr { get; set; }
        [JsonProperty("intervalMs")] public int? IntervalMs { get; set; }
        [JsonProperty("durationMs")] public int? DurationMs { get; set; }
    }

    [Serializable]
    public class OperateCameraArgs
    {
        [JsonProperty("action")] public string Action { get; set; }
        [JsonProperty("value")] public double? Value { get; set; }
        [JsonProperty("actorId")] public int? ActorId { get; set; }
        [JsonProperty("flag")] public bool? Flag { get; set; }
        [JsonProperty("settleMs")] public int? SettleMs { get; set; }
    }

    // Result envelope shared by ObserveCamera/ListEntities/ObserveEntity/ListEffects/
    // ObserveEffect — carries the Lua-built JSON as a parsed token.
    [Serializable]
    public class ObserveResult
    {
        [JsonProperty("ok")] public bool Ok { get; set; }
        [JsonProperty("data", NullValueHandling = NullValueHandling.Ignore)]
        public JToken Data { get; set; }
        [JsonProperty("note", NullValueHandling = NullValueHandling.Ignore)]
        public string Note { get; set; }
    }

    [Serializable]
    public class ObserveWindowResult
    {
        [JsonProperty("everTrue")] public bool EverTrue { get; set; }
        [JsonProperty("firstTrueMs")] public double FirstTrueMs { get; set; }
        [JsonProperty("lastTrueMs")] public double LastTrueMs { get; set; }
        [JsonProperty("peak")] public double Peak { get; set; }
        [JsonProperty("sampleCount")] public int SampleCount { get; set; }
        [JsonProperty("validSampleCount")] public int ValidSampleCount { get; set; }
        [JsonProperty("elapsedMs")] public double ElapsedMs { get; set; }
        [JsonProperty("samples")] public List<ObserveWindowSample> Samples { get; set; }
        // Set when EVERY sample failed to evaluate (expr invalid / errored / always
        // nil): the window did NOT silently pass — the expression itself is the
        // problem. Distinguishes "expr broken" from "expr valid but value was 0".
        [JsonProperty("exprError", NullValueHandling = NullValueHandling.Ignore)]
        public string ExprError { get; set; }
    }

    [Serializable]
    public class ObserveWindowSample
    {
        [JsonProperty("t")] public double T { get; set; }
        [JsonProperty("value")] public double Value { get; set; }
    }

    [Serializable]
    public class OperateCameraResult
    {
        [JsonProperty("action")] public string Action { get; set; }
        [JsonProperty("applied")] public bool Applied { get; set; }
        [JsonProperty("before", NullValueHandling = NullValueHandling.Ignore)]
        public JToken Before { get; set; }
        [JsonProperty("after", NullValueHandling = NullValueHandling.Ignore)]
        public JToken After { get; set; }
        [JsonProperty("note", NullValueHandling = NullValueHandling.Ignore)]
        public string Note { get; set; }
    }

    // ----- Phase 9 (protocol 1.9.0): optional performance profiler -----

    [Serializable]
    public class ProfilerCapabilitiesResult
    {
        [JsonProperty("analyzable")] public bool Analyzable { get; set; }
        [JsonProperty("reason", NullValueHandling = NullValueHandling.Ignore)]
        public string Reason { get; set; }
        [JsonProperty("remedy", NullValueHandling = NullValueHandling.Ignore)]
        public string Remedy { get; set; }
        [JsonProperty("playMode")] public bool PlayMode { get; set; }
        [JsonProperty("recordersAvailable")] public bool RecordersAvailable { get; set; }
        [JsonProperty("memoryProfilerInstalled")] public bool MemoryProfilerInstalled { get; set; }
        [JsonProperty("availableMetrics")] public JObject AvailableMetrics { get; set; }
    }

    [Serializable]
    public class ProfilerSampleArgs
    {
        // The scenario object is free-form JSON (validated Python-side); the bridge
        // reads known keys. Keeping it as JObject avoids a brittle nested POCO.
        [JsonProperty("scenario")] public JObject Scenario { get; set; }
        [JsonProperty("mode")] public string Mode { get; set; }
        [JsonProperty("sampleFrames")] public int? SampleFrames { get; set; }
    }

    [Serializable]
    public class ProfilerMetricAgg
    {
        [JsonProperty("min")] public double Min { get; set; }
        [JsonProperty("max")] public double Max { get; set; }
        [JsonProperty("avg")] public double Avg { get; set; }
        [JsonProperty("median")] public double Median { get; set; }
        [JsonProperty("p95")] public double P95 { get; set; }
        [JsonProperty("samples")] public int Samples { get; set; }
    }

    [Serializable]
    public class ProfilerSampleResult
    {
        [JsonProperty("label")] public string Label { get; set; }
        [JsonProperty("mode")] public string Mode { get; set; }
        [JsonProperty("framesSampled")] public int FramesSampled { get; set; }
        [JsonProperty("metrics")] public Dictionary<string, ProfilerMetricAgg> Metrics { get; set; }
        [JsonProperty("warning", NullValueHandling = NullValueHandling.Ignore)]
        public string Warning { get; set; }
    }

    [Serializable]
    public class MemorySnapshotResult
    {
        [JsonProperty("available")] public bool Available { get; set; }
        [JsonProperty("path", NullValueHandling = NullValueHandling.Ignore)]
        public string Path { get; set; }
        [JsonProperty("sizeBytes", NullValueHandling = NullValueHandling.Ignore)]
        public long? SizeBytes { get; set; }
        [JsonProperty("reason", NullValueHandling = NullValueHandling.Ignore)]
        public string Reason { get; set; }
        [JsonProperty("remedy", NullValueHandling = NullValueHandling.Ignore)]
        public string Remedy { get; set; }
    }

    // ---------------------------------------------------------------------------
    // Notification payload POCOs (Phase 1 + Phase 2)
    // ---------------------------------------------------------------------------

    [Serializable]
    public class PlaymodeChangedPayload
    {
        [JsonProperty("state")] public string State { get; set; }
    }

    /// <summary>Alias for LogLine — used as the log.line event payload.</summary>
    [Serializable]
    public class LogLinePayload : LogLine { }

    [Serializable]
    public class SceneOpenedPayload
    {
        [JsonProperty("sceneName")] public string SceneName { get; set; }
        [JsonProperty("scenePath")] public string ScenePath { get; set; }
        [JsonProperty("mode")] public string Mode { get; set; }
    }

    [Serializable]
    public class SelectionChangedPayload
    {
        [JsonProperty("instanceIds")] public int[] InstanceIds { get; set; }
        [JsonProperty("objectPath", NullValueHandling = NullValueHandling.Ignore)]
        public string ObjectPath { get; set; }
    }

    [Serializable]
    public class CompileDonePayload
    {
        [JsonProperty("success")] public bool Success { get; set; }
        [JsonProperty("durationMs")] public double DurationMs { get; set; }
    }

    [Serializable]
    public class LogOverflowPayload
    {
        [JsonProperty("dropped")] public int Dropped { get; set; }
        [JsonProperty("since")] public string Since { get; set; }
    }

    // ---------------------------------------------------------------------------
    // ProtocolSerializer — static helpers for parsing/building envelopes
    // ---------------------------------------------------------------------------

    public static class ProtocolSerializer
    {
        private static readonly JsonSerializerSettings s_settings = new JsonSerializerSettings
        {
            NullValueHandling = NullValueHandling.Ignore,
            DateParseHandling = DateParseHandling.None,
        };

        public static RequestEnvelope ParseRequest(string line)
        {
            return JsonConvert.DeserializeObject<RequestEnvelope>(line, s_settings);
        }

        public static string SerializeResponse(ResponseEnvelope envelope)
        {
            return JsonConvert.SerializeObject(envelope, s_settings);
        }

        public static string SerializeNotification(NotificationEnvelope envelope)
        {
            return JsonConvert.SerializeObject(envelope, s_settings);
        }

        public static ResponseEnvelope OkResponse(string id, object result)
        {
            return new ResponseEnvelope { Id = id, Ok = true, Result = result };
        }

        public static ResponseEnvelope ErrorResponse(string id, string code, string message)
        {
            return new ResponseEnvelope
            {
                Id = id,
                Ok = false,
                Error = new ErrorShape { Code = code, Message = message },
            };
        }

        /// <summary>Deserialize a request's args JObject into a typed POCO.</summary>
        public static T DeserializeArgs<T>(RequestEnvelope env) where T : class
        {
            if (env?.Args == null)
                return null;
            return env.Args.ToObject<T>();
        }

        /// <summary>Build a NotificationEnvelope with a current UTC ISO-8601 timestamp.</summary>
        public static NotificationEnvelope BuildNotification(string eventName, object payload)
        {
            return new NotificationEnvelope
            {
                Event = eventName,
                Payload = payload,
                Timestamp = DateTime.UtcNow.ToString("o", System.Globalization.CultureInfo.InvariantCulture),
            };
        }
    }
}
