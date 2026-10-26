// UnityMCPUIInspector.cs
// Reflection-based read-only inspection of the live PROJECT_UI/PROJECT UI tree, for the
// ListUINodes and ResolveLocalization bridge commands (Phase 2).
//
// The bridge Editor assembly does NOT reference the game assembly (PROJECT_NAMESPACE.Scripts)
// or the precompiled PROJECT_UI.Runtime.dll, so everything game-side is reached by reflection —
// the same pattern UnityMCPCommands.DefaultLuaDoString already uses to reach
// PROJECT_NAMESPACE.PROJECT_FACADE.PROJECT_LUA_MANAGER.PROJECT_LUA_VM. UnityEngine types (Transform, RectTransform,
// Camera, RectTransformUtility, GameObject) ARE referenced and used directly.
//
// All game state only exists in Play Mode; every entry point throws
// UIInspectorNotReadyException (mapped to INVALID_STATE) if the game isn't running.

using System;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;

namespace UnityMCP.Bridge
{
    internal sealed class UIInspectorNotReadyException : Exception
    {
        public UIInspectorNotReadyException(string message) : base(message) { }
    }

    /// <summary>One enumerated UI node, serialized into ListUINodesResult.</summary>
    internal sealed class UINodeInfo
    {
        public int Index;
        public int InstanceId;
        public int PanelUid;
        public string Layer;
        public string Name;
        public string Path;          // hierarchy path relative to the layer root
        public string Type;          // PROJECT_UI widget type (PROJECT_WIDGET.GetTypeName) or component type
        public string Text;          // resolved displayed string (PROJECT_TEXT/PROJECT_RICH_TEXT .text)
        public string TextId;        // localization key (PROJECT_BASE_TEXT .textId)
        public bool Visible;
        public bool Clickable;
        public float ScreenX;        // PNG-space rect (top-left origin), in screen pixels
        public float ScreenY;
        public float ScreenW;
        public float ScreenH;
    }

    internal static class UnityMCPUIInspector
    {
        // ---- cached reflection handles (resolved lazily, reset if assembly changes) ----
        private static Type s_projectFacadeType;
        private static Type s_PROJECT_UI_MANAGERType;
        private static Type s_uwidgetType;
        private static Type s_objectRefType;
        private static Type s_localizationTextType;
        private static Type s_languageTypeEnum;
        private static Type s_uiUtilsType;
        private static Type s_panelConfigType;

        private static readonly string[] s_layerNames =
            { "PROJECT_PANEL_LAYER", "PROJECT_POPUP_LAYER", "PROJECT_INFO_LAYER", "PROJECT_SCENE_LAYER" };

        private const int MaxNodesDefault = 500;

        // -------------------------------------------------------------------------
        // Public entry points
        // -------------------------------------------------------------------------

        /// <summary>
        /// Enumerate UI nodes under the open panel layers. When <paramref name="interactableOnly"/>
        /// is true (default), only nodes that are clickable or carry text are returned.
        /// </summary>
        internal static List<UINodeInfo> ListNodes(
            int? panelUidFilter, string[] layerFilter, bool interactableOnly, int maxNodes,
            string pathPrefix,
            out int gameViewWidth, out int gameViewHeight)
        {
            gameViewWidth = Screen.width;
            gameViewHeight = Screen.height;

            object PROJECT_UI_MANAGER = ResolveUiManager();   // throws if not ready
            Camera cam = GetOrthographicCamera(PROJECT_UI_MANAGER);
            int screenH = gameViewHeight;

            var nodes = new List<UINodeInfo>();
            int cap = maxNodes > 0 ? maxNodes : MaxNodesDefault;
            bool hasPrefix = !string.IsNullOrEmpty(pathPrefix);

            foreach (string layer in s_layerNames)
            {
                if (layerFilter != null && layerFilter.Length > 0 && Array.IndexOf(layerFilter, layer) < 0)
                    continue;

                var layerRt = GetLayerRoot(PROJECT_UI_MANAGER, layer) as Transform;
                if (layerRt == null)
                    continue;

                for (int i = 0; i < layerRt.childCount && nodes.Count < cap; i++)
                {
                    Transform panel = layerRt.GetChild(i);
                    int panelUid = ReadPanelUid(panel);
                    if (panelUidFilter.HasValue && panelUid != panelUidFilter.Value)
                        continue;

                    // pathPrefix is matched against the path RELATIVE to the layer root
                    // (so it includes the panel root name, e.g. "PROJECT_PANEL/Window/...").
                    WalkNode(panel, layer, panelUid, layerRt, nodes, cam, screenH,
                        interactableOnly, cap, hasPrefix ? pathPrefix : null);
                }
            }

            return nodes;
        }

        /// <summary>
        /// Resolve localization ids → strings for a given language (any language, in the
        /// Editor). Uses the per-id PROJECT_LOCALIZATION_LOOKUP (NOT the batch variant, which
        /// ignores its language arg — confirmed bug at Localization.cs:53+).
        /// </summary>
        internal static Dictionary<int, string> ResolveLocalization(IEnumerable<int> ids, string language)
        {
            Type locType = ResolvePROJECT_LOCALIZATION_SERVICEType();
            Type langEnum = ResolvePROJECT_LANGUAGEEnum();

            object langValue = ParseLanguage(langEnum, language);
            MethodInfo mi = locType.GetMethod("PROJECT_LOCALIZATION_LOOKUP",
                BindingFlags.Public | BindingFlags.Static, null,
                new[] { typeof(int), langEnum }, null);
            if (mi == null)
                throw new UIInspectorNotReadyException(
                    "PROJECT_LOCALIZATION_SERVICE.PROJECT_LOCALIZATION_LOOKUP(int, PROJECT_LANGUAGE) not found " +
                    "(editor-only API; is this a non-editor build?).");

            var result = new Dictionary<int, string>();
            foreach (int id in ids)
            {
                try
                {
                    object s = mi.Invoke(null, new object[] { id, langValue });
                    result[id] = s?.ToString();
                }
                catch
                {
                    result[id] = null;
                }
            }
            return result;
        }

        // -------------------------------------------------------------------------
        // Node walk
        // -------------------------------------------------------------------------

        private static void WalkNode(
            Transform node, string layer, int panelUid, Transform pathRoot,
            List<UINodeInfo> outList, Camera cam, int screenH,
            bool interactableOnly, int cap, string pathPrefix)
        {
            if (outList.Count >= cap)
                return;

            object uwidget = GetComponentReflected(node, PROJECT_WIDGETType());
            if (uwidget != null)
            {
                string path = RelativePath(pathRoot, node);
                bool pathOk = pathPrefix == null ||
                    path.StartsWith(pathPrefix, StringComparison.OrdinalIgnoreCase);

                if (pathOk)
                {
                    string text = ReadNodeText(node);
                    string textId = ReadNodeTextId(uwidget, node);
                    bool clickable = IsClickable(node, uwidget);
                    bool include = !interactableOnly || clickable || !string.IsNullOrEmpty(text);

                    if (include)
                    {
                        var info = new UINodeInfo
                        {
                            Index = outList.Count,
                            InstanceId = node.gameObject.GetInstanceID(),
                            PanelUid = panelUid,
                            Layer = layer,
                            Name = node.name,
                            Path = path,
                            Type = ReadTypeName(uwidget) ?? "PROJECT_WIDGET",
                            Text = text,
                            TextId = textId,
                            Visible = ReadVisible(uwidget, node),
                            Clickable = clickable,
                        };
                        FillScreenRect(node, cam, screenH, info);
                        outList.Add(info);
                    }
                }
            }

            for (int i = 0; i < node.childCount && outList.Count < cap; i++)
                WalkNode(node.GetChild(i), layer, panelUid, pathRoot, outList, cam, screenH,
                    interactableOnly, cap, pathPrefix);
        }

        // -------------------------------------------------------------------------
        // Reflection helpers — resolution
        // -------------------------------------------------------------------------

        private static Type FindType(string fullName)
        {
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type t = asm.GetType(fullName, throwOnError: false);
                if (t != null) return t;
            }
            return null;
        }

        private static object ResolveUiManager()
        {
            s_projectFacadeType ??= FindType("PROJECT_NAMESPACE.PROJECT_FACADE");
            if (s_projectFacadeType == null)
                throw new UIInspectorNotReadyException(
                    "PROJECT_NAMESPACE.PROJECT_FACADE not found — the game assembly is not loaded (enter Play Mode).");

            PropertyInfo uiMgrProp = s_projectFacadeType.GetProperty("PROJECT_UI_MANAGER",
                BindingFlags.Public | BindingFlags.Static);
            object PROJECT_UI_MANAGER = uiMgrProp?.GetValue(null);
            if (PROJECT_UI_MANAGER == null)
                throw new UIInspectorNotReadyException(
                    "PROJECT_FACADE.PROJECT_UI_MANAGER is null — the game is not running (enter Play Mode).");

            s_PROJECT_UI_MANAGERType ??= PROJECT_UI_MANAGER.GetType();
            return PROJECT_UI_MANAGER;
        }

        private static object GetLayerRoot(object PROJECT_UI_MANAGER, string layerName)
        {
            PropertyInfo p = s_PROJECT_UI_MANAGERType.GetProperty(layerName,
                BindingFlags.Public | BindingFlags.Instance);
            return p?.GetValue(PROJECT_UI_MANAGER);
        }

        private static Camera GetOrthographicCamera(object PROJECT_UI_MANAGER)
        {
            try
            {
                PropertyInfo p = s_PROJECT_UI_MANAGERType.GetProperty("orthographicCamera",
                    BindingFlags.Public | BindingFlags.Instance);
                return p?.GetValue(PROJECT_UI_MANAGER) as Camera;
            }
            catch
            {
                return null; // lazy getter may throw before uiRootTran init — fall back below
            }
        }

        private static Type PROJECT_WIDGETType()
        {
            return s_uwidgetType ??= FindType("PROJECT_UI.PROJECT_WIDGET");
        }

        private static Type ResolvePROJECT_LOCALIZATION_SERVICEType()
        {
            s_localizationTextType ??= FindType("PROJECT_NAMESPACE.I18N.PROJECT_LOCALIZATION_SERVICE");
            if (s_localizationTextType == null)
                throw new UIInspectorNotReadyException(
                    "PROJECT_NAMESPACE.I18N.PROJECT_LOCALIZATION_SERVICE not found (enter Play Mode).");
            return s_localizationTextType;
        }

        private static Type ResolvePROJECT_LANGUAGEEnum()
        {
            s_languageTypeEnum ??= FindType("PROJECT_UI.PROJECT_LANGUAGE");
            if (s_languageTypeEnum == null)
                throw new UIInspectorNotReadyException("PROJECT_UI.PROJECT_LANGUAGE enum not found.");
            return s_languageTypeEnum;
        }

        private static object ParseLanguage(Type langEnum, string language)
        {
            if (!string.IsNullOrEmpty(language))
            {
                try { return Enum.Parse(langEnum, language, ignoreCase: true); }
                catch { /* fall through to default */ }
            }
            // Default zh_CN (the editor API's own default).
            try { return Enum.Parse(langEnum, "zh_CN"); }
            catch { return Activator.CreateInstance(langEnum); }
        }

        // -------------------------------------------------------------------------
        // Reflection helpers — per-node reads
        // -------------------------------------------------------------------------

        // transform.GetComponent(Type) — UnityEngine API, but the component TYPE is a
        // reflected PROJECT_UI type, so pass it through the Type overload.
        private static object GetComponentReflected(Transform t, Type componentType)
        {
            if (componentType == null) return null;
            try { return t.GetComponent(componentType); }
            catch { return null; }
        }

        // Read a public instance member by name, trying property THEN field (precompiled
        // PROJECT_UI exposes some members as fields, some as properties — the xLua wrap can't
        // tell us which). Case-insensitive. Returns null if neither exists or throws.
        private static object ReadMemberValue(object obj, string memberName)
        {
            if (obj == null) return null;
            Type t = obj.GetType();
            const BindingFlags flags =
                BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase;
            try
            {
                PropertyInfo p = t.GetProperty(memberName, flags);
                if (p != null && p.CanRead && p.GetIndexParameters().Length == 0)
                    return p.GetValue(obj);
            }
            catch { /* fall through to field */ }
            try
            {
                FieldInfo f = t.GetField(memberName, flags);
                if (f != null)
                    return f.GetValue(obj);
            }
            catch { /* return null */ }
            return null;
        }

        private static string ReadTypeName(object uwidget)
        {
            try
            {
                MethodInfo mi = uwidget.GetType().GetMethod("GetTypeName",
                    BindingFlags.Public | BindingFlags.Instance, null, Type.EmptyTypes, null);
                return mi?.Invoke(uwidget, null) as string;
            }
            catch { return null; }
        }

        private static bool ReadVisible(object uwidget, Transform node)
        {
            // Prefer the game's own predicate PROJECT_UI_UTILS.PROJECT_CHECK_VISIBLE(GameObject); fall
            // back to PROJECT_WIDGET.isVisible, then activeSelf.
            try
            {
                s_uiUtilsType ??= FindType("PROJECT_NAMESPACE.Utils.PROJECT_UI_UTILS");
                MethodInfo mi = s_uiUtilsType?.GetMethod("PROJECT_CHECK_VISIBLE",
                    BindingFlags.Public | BindingFlags.Static, null,
                    new[] { typeof(GameObject) }, null);
                if (mi != null)
                {
                    object r = mi.Invoke(null, new object[] { node.gameObject });
                    if (r is bool b) return b;
                }
            }
            catch { /* fall through */ }

            try
            {
                PropertyInfo p = uwidget.GetType().GetProperty("isVisible",
                    BindingFlags.Public | BindingFlags.Instance);
                if (p?.GetValue(uwidget) is bool vb) return vb;
            }
            catch { /* fall through */ }

            return node.gameObject.activeSelf;
        }

        private static bool IsClickable(Transform node, object uwidget)
        {
            // Primary: a PROJECT_BUTTON component is present (PROJECT_BUTTON : PROJECT_CLICKABLE : PROJECT_WIDGET).
            Type buttonType = FindType("PROJECT_UI.PROJECT_BUTTON");
            if (buttonType != null && GetComponentReflected(node, buttonType) != null)
                return true;
            return false;
        }

        private static string ReadNodeText(Transform node)
        {
            // .text getter lives on PROJECT_TEXT / PROJECT_RICH_TEXT (not the PROJECT_BASE_TEXT base).
            foreach (string typeName in new[] { "PROJECT_UI.PROJECT_TEXT", "PROJECT_UI.PROJECT_RICH_TEXT" })
            {
                Type t = FindType(typeName);
                if (t == null) continue;
                object comp = GetComponentReflected(node, t);
                if (comp == null) continue;
                try
                {
                    PropertyInfo p = t.GetProperty("text",
                        BindingFlags.Public | BindingFlags.Instance);
                    object v = p?.GetValue(comp);
                    string s = v as string;
                    if (!string.IsNullOrEmpty(s)) return s;
                }
                catch { /* try next type */ }
            }
            return null;
        }

        private static string ReadNodeTextId(object uwidget, Transform node)
        {
            // .textId getter lives on PROJECT_BASE_TEXT (base of PROJECT_TEXT/PROJECT_RICH_TEXT).
            Type baseTextType = FindType("PROJECT_UI.PROJECT_BASE_TEXT");
            if (baseTextType == null) return null;
            object comp = GetComponentReflected(node, baseTextType);
            if (comp == null) return null;
            try
            {
                PropertyInfo p = baseTextType.GetProperty("textId",
                    BindingFlags.Public | BindingFlags.Instance);
                object v = p?.GetValue(comp);
                string s = v as string;
                return string.IsNullOrEmpty(s) ? null : s;
            }
            catch { return null; }
        }

        private static int ReadPanelUid(Transform panel)
        {
            // The panel root PROJECT_WIDGET.PROJECT_WIDGET_DATA is a PROJECT_PANEL_CONFIG with a public int uid
            // (<PROJECT_ADAPTER_SOURCE>). PROJECT_APPLY_PANEL_CONFIG sets it on mediator.transform's
            // PROJECT_WIDGET (<PROJECT_UI_CONTROLLER_SOURCE>). But the PROJECT_PANEL_LAYER child may be a wrapper
            // above that mediator, so if the direct child has no uid we descend a few
            // levels to find the first PROJECT_WIDGET whose PROJECT_WIDGET_DATA yields a uid.
            int uid = ReadUidFromWidget(panel);
            if (uid != 0)
                return uid;
            return SearchUidInChildren(panel, depth: 3);
        }

        private static int SearchUidInChildren(Transform node, int depth)
        {
            if (depth <= 0)
                return 0;
            for (int i = 0; i < node.childCount; i++)
            {
                Transform child = node.GetChild(i);
                int uid = ReadUidFromWidget(child);
                if (uid != 0)
                    return uid;
                int deeper = SearchUidInChildren(child, depth - 1);
                if (deeper != 0)
                    return deeper;
            }
            return 0;
        }

        private static int ReadUidFromWidget(Transform node)
        {
            object uwidget = GetComponentReflected(node, PROJECT_WIDGETType());
            if (uwidget == null) return 0;
            try
            {
                // ROOT CAUSE of the panelUid-always-0 bug: PROJECT_WIDGET.PROJECT_WIDGET_DATA may be a
                // public FIELD, not a property (PROJECT_UI is precompiled, can't tell from the
                // xLua wrap which only shows `gen.PROJECT_WIDGET_DATA`). The old code used only
                // GetProperty("PROJECT_WIDGET_DATA") → null for a field → uid lost. Read either.
                object PROJECT_WIDGET_DATA = ReadMemberValue(uwidget, "PROJECT_WIDGET_DATA");
                if (PROJECT_WIDGET_DATA == null) return 0;

                Type cdType = PROJECT_WIDGET_DATA.GetType();
                s_panelConfigType ??= cdType;

                // uid may likewise be a public field (PROJECT_PANEL_CONFIG.uid, confirmed) OR a property.
                object uidObj = ReadMemberValue(PROJECT_WIDGET_DATA, "uid");
                if (uidObj is int uid)
                    return uid;
            }
            catch { /* not a PROJECT_PANEL_CONFIG / no uid */ }
            return 0;
        }

        private static string RelativePath(Transform root, Transform node)
        {
            if (node == root) return "";
            var parts = new List<string>();
            Transform cur = node;
            while (cur != null && cur != root)
            {
                parts.Add(cur.name);
                cur = cur.parent;
            }
            parts.Reverse();
            return string.Join("/", parts);
        }

        private static readonly Vector3[] s_corners = new Vector3[4];

        private static void FillScreenRect(Transform node, Camera cam, int screenH, UINodeInfo info)
        {
            var rt = node as RectTransform;
            if (rt == null)
                return;
            try
            {
                // The correct camera for WorldToScreenPoint on a UGUI node is the node's
                // own Canvas.worldCamera (Screen Space - Camera) — or null for Overlay.
                // Fall back to the PROJECT_UI_MANAGER orthographicCamera passed in.
                Camera nodeCam = ResolveNodeCamera(rt) ?? cam;

                rt.GetWorldCorners(s_corners); // 0=BL,1=TL,2=TR,3=BR in world space
                // Project to Unity screen pixels (bottom-left origin).
                Vector2 bl = RectTransformUtility.WorldToScreenPoint(nodeCam, s_corners[0]);
                Vector2 tr = RectTransformUtility.WorldToScreenPoint(nodeCam, s_corners[2]);

                float minX = Mathf.Min(bl.x, tr.x);
                float maxX = Mathf.Max(bl.x, tr.x);
                float minYUnity = Mathf.Min(bl.y, tr.y);
                float maxYUnity = Mathf.Max(bl.y, tr.y);

                info.ScreenX = minX;
                info.ScreenW = maxX - minX;
                // Convert to PNG/top-left origin: pngY = screenH - unityY.
                info.ScreenY = screenH - maxYUnity;
                info.ScreenH = maxYUnity - minYUnity;
            }
            catch { /* leave rect zeroed */ }
        }

        // The camera that renders a node's Canvas. For Screen Space - Camera /
        // World Space this is canvas.worldCamera; for Screen Space - Overlay it is
        // null (and WorldToScreenPoint must receive null). Walks up to the root canvas.
        private static Camera ResolveNodeCamera(Transform node)
        {
            try
            {
                Canvas canvas = node.GetComponentInParent<Canvas>();
                if (canvas == null)
                    return null;
                canvas = canvas.rootCanvas != null ? canvas.rootCanvas : canvas;
                if (canvas.renderMode == RenderMode.ScreenSpaceOverlay)
                    return null;
                return canvas.worldCamera;
            }
            catch
            {
                return null;
            }
        }
    }
}
