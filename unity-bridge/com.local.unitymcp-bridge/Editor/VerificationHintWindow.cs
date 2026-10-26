// VerificationHintWindow.cs
// The on-screen hint shown during a verification run:
//   "当前正在操作Unity验证，请勿人为操作"
//
// It is an EditorWindow (editor chrome), NOT an in-game UI element. This is
// deliberate and verified: ScreenCapture.CaptureScreenshotAsTexture (the bridge's
// screenshot path) grabs only the composited GAME render, so an editor window is
// NEVER captured into the AI's screenshots, and it cannot intercept the game's
// raycasts/clicks. No game-side code is touched.
//
// It also MIRRORS the latest AI screenshot (the frame the AI is grounding on) into
// the window so a human watching the Editor sees exactly what the AI sees. Because
// the preview lives in this editor window — not the game render — it is likewise
// excluded from the next ScreenCapture, so there is no recursion / pollution.
//
// The mirrored frame is backed by a real on-disk PNG (so a human has a file, not
// just the AI's base64), and the window offers "打开目录" (reveal in explorer) and
// "删除" (delete that PNG) buttons.
//
// Shown by VerificationSession.Start(), hidden by End()/ForceEnd(); the preview is
// pushed by HandleCaptureScreenshot while a session is armed.

using System;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace UnityMCP.Bridge
{
    internal sealed class VerificationHintWindow : EditorWindow
    {
        private const string HintText = "当前正在操作Unity验证，请勿人为操作";
        private const float BannerHeight = 36f;
        private const float HeaderHeight = 124f;  // banner + note + toolbar + captions
        private const float DefaultPreviewHeight = 320f;

        private static VerificationHintWindow s_window;
        private string _note;

        // Latest AI screenshot mirrored into the window. Editor-only texture, owned
        // and destroyed by this window — never added to the game scene.
        private Texture2D _preview;
        private int _previewSeq;

        // On-disk PNG backing the current preview (so a human has a real file).
        private string _previewPath;
        private string _statusMessage;

        // True while a screenshot is awaiting the user's 继续 click (the AI is
        // blocked until then). Drives the approval button row.
        private bool _awaitingApproval;

        internal static bool IsShowing => s_window != null;

        internal static void Show(string note)
        {
            // GetWindow with utility=true makes a floating always-on-top panel that
            // does not dock into the layout and stays above the editor.
            if (s_window == null)
                s_window = GetWindow<VerificationHintWindow>(utility: true, title: "UnityMCP 验证中", focus: false);

            s_window._note = note;
            s_window.minSize = new Vector2(420f, HeaderHeight + DefaultPreviewHeight);
            s_window.ShowUtility();
            s_window.Repaint();
        }

        internal static void Hide()
        {
            if (s_window != null)
            {
                s_window.Close();
                s_window = null;
            }
        }

        /// <summary>
        /// Mirror the latest AI screenshot into the window. <paramref name="filePath"/>
        /// is the on-disk PNG the bridge persisted for this frame (may be null/empty if
        /// persistence failed — the preview still renders from the base64). No-op if the
        /// window isn't open. Safe to call from any main-thread tick.
        /// </summary>
        internal static void SetPreview(string pngBase64, string filePath)
        {
            if (s_window == null || string.IsNullOrEmpty(pngBase64))
                return;
            try
            {
                byte[] png = Convert.FromBase64String(pngBase64);
                s_window.ApplyPreview(png, filePath);
            }
            catch
            {
                // Malformed base64 — leave the previous preview in place.
            }
        }

        /// <summary>
        /// Toggle the "screenshot awaiting your 继续" approval row. Called by
        /// VerificationSession.BeginApproval / EndApproval. No-op if window closed.
        /// </summary>
        internal static void SetAwaitingApproval(bool awaiting)
        {
            if (s_window == null)
                return;
            s_window._awaitingApproval = awaiting;
            s_window.Repaint();
        }

        private void ApplyPreview(byte[] png, string filePath)
        {
            if (_preview == null)
            {
                _preview = new Texture2D(2, 2, TextureFormat.RGBA32, false);
                _preview.hideFlags = HideFlags.HideAndDontSave;
            }
            // ImageConversion.LoadImage resizes the texture to the PNG dimensions.
            if (ImageConversion.LoadImage(_preview, png))
            {
                _previewSeq++;
                _previewPath = filePath;
                _statusMessage = null;
                Repaint();
            }
        }

        private void OnGUI()
        {
            // Red attention banner.
            EditorGUILayout.Space(4f);

            var bannerStyle = new GUIStyle(EditorStyles.boldLabel)
            {
                alignment = TextAnchor.MiddleCenter,
                fontSize = 15,
                normal = { textColor = new Color(0.95f, 0.25f, 0.2f) },
                wordWrap = true,
            };

            var bannerRect = GUILayoutUtility.GetRect(0f, BannerHeight, GUILayout.ExpandWidth(true));
            EditorGUI.DrawRect(bannerRect, new Color(0.20f, 0.04f, 0.04f));
            GUI.Label(bannerRect, "⚠ " + HintText, bannerStyle);

            if (!string.IsNullOrEmpty(_note))
            {
                EditorGUILayout.Space(2f);
                EditorGUILayout.LabelField(_note, EditorStyles.centeredGreyMiniLabel);
            }

            DrawApprovalRow();
            DrawToolbar();
            DrawPreview();
        }

        private void DrawApprovalRow()
        {
            if (!_awaitingApproval)
                return;

            EditorGUILayout.Space(4f);
            var hint = new GUIStyle(EditorStyles.boldLabel)
            {
                alignment = TextAnchor.MiddleCenter,
                normal = { textColor = new Color(1f, 0.78f, 0.2f) },
                wordWrap = true,
            };
            EditorGUILayout.LabelField(
                "⏸ AI 已截图并暂停，等待你确认。截图符合要求请点「继续」。", hint);

            using (new EditorGUILayout.HorizontalScope())
            {
                GUILayout.FlexibleSpace();
                Color prev = GUI.backgroundColor;
                GUI.backgroundColor = new Color(0.30f, 0.65f, 0.30f);
                if (GUILayout.Button("继续工具调用", GUILayout.Height(28f), GUILayout.MinWidth(130f)))
                {
                    VerificationSession.ApproveCurrent();
                    _statusMessage = "已继续。";
                }
                GUI.backgroundColor = new Color(0.30f, 0.5f, 0.65f);
                if (GUILayout.Button("继续且不再询问", GUILayout.Height(28f), GUILayout.MinWidth(130f)))
                {
                    VerificationSession.ApproveAll();
                    _statusMessage = "已继续，本会话后续不再询问。";
                }
                GUI.backgroundColor = new Color(0.55f, 0.45f, 0.25f);
                if (GUILayout.Button("重新截图", GUILayout.Height(28f), GUILayout.MinWidth(90f)))
                {
                    VerificationSession.RequestRecapture();
                    _statusMessage = "正在重新截图…";
                }
                GUI.backgroundColor = prev;
                GUILayout.FlexibleSpace();
            }
            EditorGUILayout.LabelField(
                "（截图不符合要求？点「重新截图」刷新画面，满意后再点「继续」）",
                EditorStyles.centeredGreyMiniLabel);
            EditorGUILayout.Space(2f);
        }

        private void DrawToolbar()
        {
            EditorGUILayout.Space(2f);
            using (new EditorGUILayout.HorizontalScope(EditorStyles.toolbar))
            {
                bool hasFile = !string.IsNullOrEmpty(_previewPath) && File.Exists(_previewPath);
                bool dirExists = Directory.Exists(ScreenshotDir());

                // "打开目录" stays clickable whenever the screenshot directory exists,
                // even after the current file was deleted — it reveals the file if it's
                // still there, otherwise just opens the directory.
                using (new EditorGUI.DisabledScope(!dirExists))
                {
                    if (GUILayout.Button("打开目录", EditorStyles.toolbarButton, GUILayout.Width(80f)))
                        RevealCurrent();
                }
                // "删除" only enabled when there is an actual file to delete.
                using (new EditorGUI.DisabledScope(!hasFile))
                {
                    if (GUILayout.Button("删除", EditorStyles.toolbarButton, GUILayout.Width(60f)))
                        DeleteCurrent();
                }

                GUILayout.FlexibleSpace();

                string label = hasFile
                    ? Path.GetFileName(_previewPath)
                    : (string.IsNullOrEmpty(_previewPath) ? "（无文件）" : "（文件已删除）");
                GUILayout.Label(label, EditorStyles.miniLabel);
            }

            if (!string.IsNullOrEmpty(_statusMessage))
                EditorGUILayout.LabelField(_statusMessage, EditorStyles.centeredGreyMiniLabel);
        }

        private static string ScreenshotDir()
        {
            try { return UnityMCPCommands.ResolveScreenshotDir(); }
            catch { return null; }
        }

        private void RevealCurrent()
        {
            if (!string.IsNullOrEmpty(_previewPath) && File.Exists(_previewPath))
            {
                // RevealInFinder opens the OS file explorer with the file selected.
                EditorUtility.RevealInFinder(_previewPath);
                _statusMessage = null;
                return;
            }
            // File gone (or never persisted) — open the screenshot directory itself.
            string dir = ScreenshotDir();
            if (!string.IsNullOrEmpty(dir) && Directory.Exists(dir))
            {
                EditorUtility.RevealInFinder(dir);
                _statusMessage = null;
            }
            else
            {
                _statusMessage = "截图目录不存在。";
            }
        }

        private void DeleteCurrent()
        {
            if (string.IsNullOrEmpty(_previewPath) || !File.Exists(_previewPath))
            {
                _statusMessage = "文件不存在或已删除。";
                ClearPreview();
                return;
            }
            try
            {
                File.Delete(_previewPath);
                string name = Path.GetFileName(_previewPath);
                // Clear the in-window preview too — the screenshot is gone, so the
                // window should not keep showing it.
                ClearPreview();
                _statusMessage = $"已删除：{name}";
                Repaint();
            }
            catch (Exception ex)
            {
                _statusMessage = $"删除失败：{ex.Message}";
            }
        }

        private void ClearPreview()
        {
            if (_preview != null)
            {
                DestroyImmediate(_preview);
                _preview = null;
            }
            _previewPath = null;
            _previewSeq = 0;
            Repaint();
        }

        private void DrawPreview()
        {
            EditorGUILayout.Space(4f);
            var captionStyle = EditorStyles.centeredGreyMiniLabel;

            if (_preview == null || _preview.width <= 2)
            {
                EditorGUILayout.LabelField("（等待 AI 截图… AI 抓到的画面会显示在这里）", captionStyle);
                return;
            }

            EditorGUILayout.LabelField(
                $"AI 当前看到的画面  ·  {_preview.width}×{_preview.height}  ·  #{_previewSeq}",
                captionStyle);

            // Fit the screenshot into the remaining window area, preserving aspect.
            float availW = position.width - 16f;
            float availH = position.height - HeaderHeight;
            if (availH < 40f) availH = 40f;

            float aspect = (float)_preview.width / Mathf.Max(1, _preview.height);
            float drawW = availW;
            float drawH = drawW / aspect;
            if (drawH > availH)
            {
                drawH = availH;
                drawW = drawH * aspect;
            }

            var area = GUILayoutUtility.GetRect(availW, drawH, GUILayout.ExpandWidth(true));
            var imgRect = new Rect(area.x + (area.width - drawW) * 0.5f, area.y, drawW, drawH);
            EditorGUI.DrawRect(new Rect(imgRect.x - 1f, imgRect.y - 1f, imgRect.width + 2f, imgRect.height + 2f),
                new Color(0f, 0f, 0f, 0.6f));
            GUI.DrawTexture(imgRect, _preview, ScaleMode.ScaleToFit, alphaBlend: false);
        }

        private void OnDestroy()
        {
            if (_preview != null)
            {
                DestroyImmediate(_preview);
                _preview = null;
            }
            if (s_window == this)
                s_window = null;
        }
    }
}
