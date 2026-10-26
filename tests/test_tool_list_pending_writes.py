import pytest
from unity_mcp_py.tools.list_pending_writes import list_pending_writes
from unity_mcp_py.state import GUARD


@pytest.mark.asyncio
async def test_list_includes_agent_prompt_template():
    ctx = await GUARD.issue("open_scene", '{"scene_path":"X"}', 100)
    items = await list_pending_writes()
    found = [i for i in items if i["token"] == ctx.token]
    assert len(found) == 1
    item = found[0]
    assert item["tool_name"] == "open_scene"
    assert item["args_summary"] == '{"scene_path":"X"}'
    assert ctx.token in item["agent_prompt_template"]
    assert "MUST NOT" in item["agent_prompt_template"]
    assert "REQUIREMENT_SLICE" in item["agent_prompt_template"]
    # Cleanup
    await GUARD.consume([ctx.token])
