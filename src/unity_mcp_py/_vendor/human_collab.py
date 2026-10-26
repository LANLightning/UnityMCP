"""human_collab.py — Mode B human collab gate (Phase 4).

PHASE 4 NOTE: this module intentionally uses the deprecated TailEditorLog command
for ring-buffer baseline+delta queries. Replace with a non-deprecated
GetLogLinesSince(sinceLineNumber) command in a future protocol bump (Phase 5+).
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge_client import (
    BridgeClient, BridgeError, BridgeConnectionError, BridgeProtocolError,
)


DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "instruction_field": "human_instruction",
    "max_logs_returned": 200,
    "auto_pass_on_no_errors": True,
    "fail_levels": ["Error", "Exception", "Assert"],
    "capture_screenshot": True,
    # Phase 5: resource-compare advisor (disabled by default for back-compat)
    "resource_compare": {"enabled": False},
}


def validate_screenshot_path(path: str | Path | None) -> dict:
    """Validate a user-supplied screenshot path. NEVER raises.

    Used by the flow-unity human verify loop when the user pastes a screenshot
    path in response to "请提供能体现问题的截图路径". Returns a small dict the
    orchestrator can stash under `evidence.user_screenshots[]` so audit can
    later distinguish "user gave a path that exists" from "user gave a path
    that vanished" from "user skipped".

    Returns:
        {
          "path": <stringified path or "">,
          "exists": bool,           # False when path is None/empty OR file missing
          "size_bytes": int | None, # None when not exists
          "missing": bool,          # True when path was given but file not found
        }
    """
    if path is None or str(path).strip() == "":
        return {"path": "", "exists": False, "size_bytes": None, "missing": False}
    p_str = str(path).strip()
    try:
        p = Path(p_str)
        if p.exists() and p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            return {"path": p_str, "exists": True, "size_bytes": size, "missing": False}
        return {"path": p_str, "exists": False, "size_bytes": None, "missing": True}
    except Exception:
        # Path() can raise on Windows for some weird strings; treat as missing.
        return {"path": p_str, "exists": False, "size_bytes": None, "missing": True}


def _summarize_errors_for_fix(error_lines: list[dict]) -> str:
    """Produce a short factual summary of failing log lines for orchestrator use.

    The orchestrator (Claude) reads this string and turns it into a real fix plan;
    here we only collate the raw signals. Returns "" when no errors.
    """
    if not error_lines:
        return ""
    counts: dict[str, int] = {}
    for line in error_lines:
        lt = str(line.get("logType", "Unknown"))
        counts[lt] = counts.get(lt, 0) + 1
    first = error_lines[0]
    first_msg = str(first.get("message", "")).strip().splitlines()[0:1]
    first_msg_str = first_msg[0] if first_msg else ""
    if len(first_msg_str) > 240:
        first_msg_str = first_msg_str[:240] + "..."
    counts_str = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    return f"counts=[{counts_str}]; first={first.get('logType','')}: {first_msg_str}"


class HumanCollabGate:
    """Mode B human collab verifier — baseline+delta around an AskUserQuestion block.

    Usage:
        gate = HumanCollabGate(config=flavor.verifiers.unity_mcp.human_collab)
        baseline = gate.prepare(bridge_port=17873, log_dir=run/06b/, wu_id="wu-001")
        # ... orchestrator emits AskUserQuestion with WU's human_instruction ...
        verdict = gate.analyze(bridge_port=17873, baseline_context=baseline,
                              user_response="Done", log_dir=run/06b/, wu_id="wu-001")
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config: dict[str, Any] = {**DEFAULTS, **(config or {})}

    # ----- prepare -----

    def redo(self, bridge_port: int, log_dir: Path, wu_id: str,
             with_screenshot: bool | None = None) -> dict:
        """Drop any previous baseline and capture a fresh one.

        Used by flow-unity's human verify loop when the user chooses "redo": the
        prior baseline + collected screenshots must be discarded so the next
        2-minute window sees only logs from the new user attempt. Implementation-
        wise this is just `prepare()` again — baseline+delta is naturally
        stateless across calls — but exposing `redo()` makes intent explicit at
        the orchestrator and gives a single seam for any future cleanup
        (e.g., deleting prior screenshot files on disk).
        """
        prior_before = log_dir / f"{wu_id}-screenshot-before.png"
        prior_after = log_dir / f"{wu_id}-screenshot-after.png"
        for p in (prior_before, prior_after):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass  # best-effort cleanup; never raise
        return self.prepare(
            bridge_port=bridge_port,
            log_dir=log_dir,
            wu_id=wu_id,
            with_screenshot=with_screenshot,
        )

    def prepare(self, bridge_port: int, log_dir: Path, wu_id: str,
                with_screenshot: bool | None = None) -> dict:
        """Connect to bridge, capture baseline snapshot. NEVER raises.

        Returns baseline_context dict:
        {
          baseline_line_number: int,    # current TailEditorLog max lineNumber
          screenshot_before: str | None, # path to PNG (or None if disabled/failed)
          baseline_at: str,              # ISO-8601 timestamp
          bridge_handshake: dict | None, # for diagnostics
          error: str | None,             # if any step failed; never raises
        }

        CaptureScreenshot is best-effort: if the Unity editor is not in playmode,
        INVALID_STATE is returned and screenshot_before is set to None gracefully.
        """
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, Any] = {
            "baseline_line_number": 0,
            "screenshot_before": None,
            "baseline_at": datetime.now(timezone.utc).isoformat(),
            "bridge_handshake": None,
            "error": None,
        }
        try:
            with BridgeClient(port=bridge_port) as bridge:
                try:
                    bridge.connect()
                    result["bridge_handshake"] = bridge.handshake()
                except (BridgeConnectionError, BridgeError) as exc:
                    result["error"] = f"bridge connect/handshake failed: {exc}"
                    return result

                # TailEditorLog: get current lineNumber (use maxLines=1 to minimize traffic;
                # we only need the latest lineNumber).
                # NOTE: TailEditorLog is deprecated (Phase 1 wu-005 added [Obsolete] + warn-once).
                # We intentionally use it here because the server-side ring buffer is the only way
                # to get baseline+delta without C# changes. Phase 5+: replace with GetLogLinesSince.
                try:
                    tail_res = bridge.send("TailEditorLog", {"sinceLineNumber": 0, "maxLines": 1})
                    lines = tail_res.get("lines") or []
                    # The server returns the most-recent N lines; we want the highest
                    # lineNumber present. If the ring buffer is empty, treat as 0.
                    if lines:
                        result["baseline_line_number"] = max(
                            int(line.get("lineNumber", 0)) for line in lines
                        )
                    else:
                        result["baseline_line_number"] = 0
                except (BridgeError, BridgeConnectionError, BridgeProtocolError) as exc:
                    # TailEditorLog failure — record but continue (screenshot may still work).
                    result["error"] = f"TailEditorLog failed: {exc}"

                # CaptureScreenshot: best-effort; only works in playmode.
                # INVALID_STATE is expected when not in playmode — degrade gracefully to None.
                # `with_screenshot` arg (when explicitly False) suppresses capture;
                # used by flow-unity human verify where users supply screenshots
                # only on observed failure.
                ss_enabled = (
                    self.config.get("capture_screenshot", True)
                    if with_screenshot is None
                    else bool(with_screenshot)
                )
                if ss_enabled:
                    try:
                        cap_res = bridge.send("CaptureScreenshot", {})
                        b64 = cap_res.get("pngBase64", "")
                        if b64:
                            ss_path = log_dir / f"{wu_id}-screenshot-before.png"
                            try:
                                ss_path.write_bytes(base64.b64decode(b64))
                                result["screenshot_before"] = str(ss_path)
                            except Exception as exc:
                                # Screenshot save failed — note but don't fail prepare.
                                if result["error"] is None:
                                    result["error"] = f"screenshot save failed: {exc}"
                    except BridgeError as exc:
                        # INVALID_STATE is expected when not in playmode — that's OK.
                        if exc.code != "INVALID_STATE":
                            # Other errors: record but continue.
                            if result["error"] is None:
                                result["error"] = (
                                    f"screenshot capture failed: {exc.code} {exc.message}"
                                )
                    except (BridgeConnectionError, BridgeProtocolError) as exc:
                        if result["error"] is None:
                            result["error"] = f"screenshot bridge failure: {exc}"
        except Exception as exc:
            # Catch-all: HumanCollabGate.prepare must never raise.
            result["error"] = f"unexpected error in prepare: {exc}"
        return result

    # ----- analyze -----

    def analyze(
        self,
        bridge_port: int,
        baseline_context: dict,
        user_response: str,
        log_dir: Path,
        wu_id: str,
        screen_monitor: Any = None,
        with_screenshot: bool | None = None,
    ) -> dict:
        """Branch on user_response, fetch new logs since baseline, compute verdict.

        Returns VerdictDict:
        {
          passed: bool,
          reason: str,
          new_log_count: int,            # total new lines added since baseline
          error_count: int,              # lines at fail_levels
          errors: list[dict],            # first max_logs_returned matching
          screenshot_before_path: str | None,
          screenshot_after_path: str | None,
          user_response: str,            # echoed for audit
        }

        Decision logic:
          - 'Failed'  -> passed=False, reason='user reported failure'
          - 'Skipped' -> passed=True, reason='user skipped' (no bridge call)
          - 'Done'    -> fetch delta; passed based on error_count + auto_pass_on_no_errors
          - other     -> passed=False, reason='unknown user_response ...'

        NEVER raises.
        """
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        verdict: dict[str, Any] = {
            "passed": False,
            "reason": "",
            "new_log_count": 0,
            "error_count": 0,
            "errors": [],
            "screenshot_before_path": baseline_context.get("screenshot_before"),
            "screenshot_after_path": None,
            "user_response": user_response,
            "suggested_fix": "",
        }

        # Branch on user_response BEFORE touching the bridge.
        if user_response == "Failed":
            verdict["passed"] = False
            verdict["reason"] = "user reported failure"
            return verdict
        if user_response == "Skipped":
            verdict["passed"] = True
            verdict["reason"] = "user skipped — no analysis performed"
            return verdict
        if user_response != "Done":
            verdict["passed"] = False
            verdict["reason"] = (
                f"unknown user_response {user_response!r}; expected Done|Skipped|Failed"
            )
            return verdict

        # user_response == "Done": fetch delta from bridge.
        baseline_line_number = int(baseline_context.get("baseline_line_number", 0))
        max_logs = int(self.config.get("max_logs_returned", 200))
        fail_levels = set(self.config.get("fail_levels", ["Error", "Exception", "Assert"]))

        try:
            with BridgeClient(port=bridge_port) as bridge:
                try:
                    bridge.connect()
                    bridge.handshake()
                except (BridgeConnectionError, BridgeError) as exc:
                    verdict["passed"] = False
                    verdict["reason"] = f"bridge unreachable: {exc}"
                    return verdict

                # Fetch new logs since baseline.
                # NOTE: TailEditorLog is deprecated — intentional, see module docstring.
                try:
                    tail_res = bridge.send(
                        "TailEditorLog",
                        {"sinceLineNumber": baseline_line_number, "maxLines": max_logs},
                    )
                    new_lines = tail_res.get("lines") or []
                except (BridgeError, BridgeConnectionError, BridgeProtocolError) as exc:
                    verdict["passed"] = False
                    verdict["reason"] = f"TailEditorLog failed: {exc}"
                    return verdict

                # Phase 5: screen_monitor timeline (optional)
                if screen_monitor is not None:
                    try:
                        timeline = screen_monitor.timeline()
                        verdict["screenshot_timeline"] = timeline
                        verdict["screenshot_timeline_frame_count"] = len(timeline)
                    except Exception as exc:
                        # never raises — log via the existing pattern (no logger in this module; use a string note)
                        if verdict.get("reason"):
                            pass
                        verdict["screenshot_timeline_error"] = str(exc)

                verdict["new_log_count"] = len(new_lines)
                error_lines = [
                    line for line in new_lines
                    if line.get("logType") in fail_levels
                ]
                verdict["error_count"] = len(error_lines)
                verdict["errors"] = error_lines[:max_logs]

                # Best-effort post-screenshot.
                ss_enabled = (
                    self.config.get("capture_screenshot", True)
                    if with_screenshot is None
                    else bool(with_screenshot)
                )
                if ss_enabled:
                    try:
                        cap_res = bridge.send("CaptureScreenshot", {})
                        b64 = cap_res.get("pngBase64", "")
                        if b64:
                            ss_path = log_dir / f"{wu_id}-screenshot-after.png"
                            try:
                                ss_path.write_bytes(base64.b64decode(b64))
                                verdict["screenshot_after_path"] = str(ss_path)
                            except Exception:
                                pass  # save failure is best-effort
                    except (BridgeError, BridgeConnectionError, BridgeProtocolError):
                        pass  # screenshot is best-effort

                # Decision logic.
                auto_pass = bool(self.config.get("auto_pass_on_no_errors", True))
                if verdict["error_count"] > 0:
                    verdict["passed"] = False
                    verdict["reason"] = (
                        f"{verdict['error_count']} error-level log line(s) during human collab"
                    )
                    verdict["suggested_fix"] = _summarize_errors_for_fix(error_lines)
                    # resource-compare advisor (optional, disabled by default)
                    try:
                        if self.config.get("resource_compare", {}).get("enabled"):
                            from lib import resource_compare  # lazy import — avoids PyYAML cost when disabled
                            rc_cfg = self.config["resource_compare"]
                            if resource_compare.is_resource_keyword_hit(
                                error_lines, rc_cfg.get("keywords", [])
                            ):
                                failing_path = resource_compare.extract_resource_path_from_logs(new_lines)
                                if failing_path:
                                    ref_pool = rc_cfg.get("reference_pool_dir")
                                    if ref_pool:
                                        ref_dir = Path(ref_pool)
                                    else:
                                        ref_dir = Path(failing_path).parent
                                    rc_result = resource_compare.analyze(
                                        failed_resource_path=Path(failing_path),
                                        reference_pool_dir=ref_dir,
                                        asset_types=rc_cfg.get("asset_types", []),
                                        unchanged_days=int(rc_cfg.get("unchanged_days_threshold", 30)),
                                        max_refs=int(rc_cfg.get("max_reference_count", 5)),
                                    )
                                    # Persist as a Markdown report
                                    report_path = log_dir / f"{wu_id}-resource-compare.md"
                                    import json as _json
                                    refs_n = len(rc_result.get("references_used", []))
                                    report_md = (
                                        f"# Resource compare report — {wu_id}\n\n"
                                        f"Failed resource: {failing_path}\n\n"
                                        f"References examined: {refs_n}\n\n"
                                        "## Deltas\n\n"
                                        "```json\n"
                                        f"{_json.dumps(rc_result, indent=2)}\n"
                                        "```\n"
                                    )
                                    report_path.write_text(report_md, encoding="utf-8")
                                    verdict["resource_compare_report_path"] = str(report_path)
                    except Exception as exc:
                        verdict["resource_compare_error"] = str(exc)
                elif verdict["new_log_count"] == 0 and auto_pass:
                    verdict["passed"] = True
                    verdict["reason"] = "no new log events; auto-pass"
                else:
                    verdict["passed"] = True
                    verdict["reason"] = (
                        f"{verdict['new_log_count']} benign new log lines"
                    )
        except Exception as exc:
            verdict["passed"] = False
            verdict["reason"] = f"unexpected error in analyze: {exc}"
        return verdict
