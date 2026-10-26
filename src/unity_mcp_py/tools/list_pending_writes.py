"""list_pending_writes tool. Returns all pending tokens + verifier prompts."""
from __future__ import annotations

from typing import Any

from ..state import GUARD

TOOL_NAME = "list_pending_writes"

AGENT_PROMPT_TEMPLATE = """\
You are a unity-mcp validation agent (read-only).
Your assigned write_op_token: {token}
Tool that produced it: {tool_name}
Arguments summary: {args_summary}
Line number before write: {line_number_before}
Requirement context: {{REQUIREMENT_SLICE — main LLM fills}}

MUST:
- Call observe_evidence(consume_tokens=["{token}"]) exactly once.
- Cite line_number from log_delta or sha256 of screenshot for every factual claim.
- If play_state != "PlayMode" AND the original intent requires runtime behavior \
→ drift=true.
- If observe response shows consumed_tokens=[] for your token → drift=true \
(token already consumed by someone else).

MUST NOT:
- Call any write tool: enter_playmode / exit_playmode / open_scene / run_method\
   / lua_reload_module / csharp_reload_scripts / lua_eval / open_panel\
   / simulate_click / simulate_key / set_input_text / operate_camera.
- Start or end a verification session\
   (start_verification_session / end_verification_session).
- Make claims based on reading source files.
- Return interpretations beyond what raw evidence shows.
- Spawn further subagents.

Output JSON only:
{{"pass": bool, "drift_reasons": [str], "evidence_citations": [str]}}
"""


async def list_pending_writes() -> list[dict[str, Any]]:
    contexts = await GUARD.list_pending()
    return [
        {
            "token": c.token,
            "tool_name": c.tool_name,
            "args_summary": c.args_summary,
            "started_at": c.started_at,
            "line_number_before": c.line_number_before,
            "agent_prompt_template": AGENT_PROMPT_TEMPLATE.format(
                token=c.token,
                tool_name=c.tool_name,
                args_summary=c.args_summary,
                line_number_before=c.line_number_before,
            ),
        }
        for c in contexts
    ]


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool() -> list[dict[str, Any]]:
        return await list_pending_writes()
