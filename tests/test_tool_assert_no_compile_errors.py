import pytest
from unity_mcp_py.tools.assert_no_compile_errors import assert_no_compile_errors


@pytest.mark.asyncio
async def test_assert_no_compile_errors_no_errors(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("GetCompileStatus").respond_with(
        {"hasErrors": False, "errors": []})

    result = await assert_no_compile_errors()

    assert result["has_errors"] is False
    assert result["errors"] == []
    assert result["is_runtime_evidence"] is False   # hardcoded per spec §3.2 point 4


@pytest.mark.asyncio
async def test_assert_no_compile_errors_with_errors(
    unity_bridge_session, mock_bridge_server
):
    bridge = mock_bridge_server
    bridge.expect_request("GetCompileStatus").respond_with({
        "hasErrors": True,
        "errors": [{"file": "Foo.cs", "line": 10, "message": "CS0001 syntax error"}]
    })

    result = await assert_no_compile_errors()

    assert result["has_errors"] is True
    assert len(result["errors"]) == 1
    assert result["is_runtime_evidence"] is False   # always False, even when errors present
