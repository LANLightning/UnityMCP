"""Tests for human_collab_gate MCP tool (wu-005 + MEDIUM fix 2026-05-13)."""
from __future__ import annotations

import pytest

from unity_mcp_py.tools.human_collab_gate import human_collab_gate


# ---------------------------------------------------------------------------
# Test 1: awaiting_user path — first call, no _user_choice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_collab_gate_awaiting_user_returns_wu_id_not_context(
    unity_bridge_session, mock_bridge_server, tmp_path, monkeypatch
):
    """First call returns opaque _wu_id; NOT _baseline_context (MEDIUM fix).

    The previous implementation echoed the full baseline dict — including
    absolute screenshot and log_dir paths — into the LLM context. After the
    fix, only the _wu_id is returned; the baseline is held server-side.
    """
    monkeypatch.setenv("UNITY_MCP_HCG_LOG_DIR", str(tmp_path))
    bridge = mock_bridge_server

    bridge.expect_request("TailEditorLog").respond_with(
        {"lines": [{"lineNumber": 200}]}
    )
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    bridge.expect_request("CaptureScreenshot").respond_with(
        {"pngBase64": tiny_png_b64, "sizeBytes": 70}
    )

    result = await human_collab_gate(
        human_instruction="Click red button",
        fail_levels=["Error", "Exception"],
    )

    assert result["verdict"] == "awaiting_user"
    assert result["baseline_line_number"] == 200
    assert result["before_screenshot_png_base64"] is not None
    assert "_wu_id" in result
    assert result["_wu_id"].startswith("mcp-")
    # MEDIUM fix: _baseline_context must NOT leak paths into LLM context.
    assert "_baseline_context" not in result


# ---------------------------------------------------------------------------
# Test 2: skipped path — two-call flow using _wu_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_collab_gate_skipped_path_two_call(
    unity_bridge_session, mock_bridge_server, tmp_path, monkeypatch
):
    """Two-call flow: first call yields _wu_id; second call with Skipped resolves."""
    monkeypatch.setenv("UNITY_MCP_HCG_LOG_DIR", str(tmp_path))
    bridge = mock_bridge_server

    # First-call prepare exchange:
    bridge.expect_request("TailEditorLog").respond_with(
        {"lines": [{"lineNumber": 200}]}
    )
    bridge.expect_request("CaptureScreenshot").respond_error(
        "INVALID_STATE", "not in playmode"
    )

    first = await human_collab_gate(
        human_instruction="Click red button",
        fail_levels=["Error", "Exception"],
    )
    assert first["verdict"] == "awaiting_user"
    wu_id = first["_wu_id"]

    # Second call: Skipped short-circuits BEFORE any additional bridge call.
    second = await human_collab_gate(
        human_instruction="Click red button",
        fail_levels=["Error", "Exception"],
        _user_choice="Skipped",
        _wu_id=wu_id,
    )
    assert second["verdict"] == "skipped"
    assert second["baseline_line_number"] == 200


# ---------------------------------------------------------------------------
# Test 3: second call without _wu_id is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_collab_gate_second_call_requires_wu_id(
    unity_bridge_session, mock_bridge_server
):
    """Providing _user_choice without _wu_id is an orchestrator bug — reject."""
    with pytest.raises(ValueError, match="_wu_id"):
        await human_collab_gate(
            human_instruction="x",
            _user_choice="Done",
            _wu_id=None,
        )


@pytest.mark.asyncio
async def test_human_collab_gate_unknown_wu_id_is_rejected(
    unity_bridge_session, mock_bridge_server
):
    """Stale or forged _wu_id (not in server-side baseline store) must be rejected."""
    with pytest.raises(ValueError, match="not found"):
        await human_collab_gate(
            human_instruction="x",
            _user_choice="Done",
            _wu_id="mcp-deadbeef",
        )
