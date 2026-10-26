"""verify_assertions tool. Read-only. No write-op token.

Computes a pass/fail verdict for a list of declarative assertions ENTIRELY
server-side, so the recurring "is it still correct?" verification check never
round-trips through the model. The model emits one tool call describing what to
check; the tool returns a compact ``{passed, failures, results, lua_runway}`` verdict (tens
of tokens) instead of raw observation payloads the model would have to read and
interpret.

This is the strongest "remove the model from the loop" lever: a green verdict
means the agent advances without reading any logs, node trees, or screenshots.

Assertion types (each a dict with a ``type`` key):
  - ``expect_no_errors``: fail if any Error/Exception/Assert appears in the log
    tail. Optional ``since_line_number`` scopes to recent lines.
  - ``log_matches``: ``pattern`` (regex). Passes when a log line matches; with
    ``absent: true`` it INVERTS — passes only when NO line matches.
  - ``play_state``: ``equals`` — assert GetPlayState equals the value.
  - ``node_exists`` / ``node_visible``: ``name`` and/or ``path`` — assert a UI
    node is present (and, for node_visible, visible). Optional ``panel_uid`` /
    ``path_prefix`` narrow the ListUINodes query.
  - ``lua_expr``: ``expr`` + ``expect`` + ``goal`` — run a Lua expression in the
    live VM and assert its stringified return equals ``expect``. ``goal`` is
    required (documents intent) but lua_expr does NOT charge the runway and issues
    NO write-op token; verify_assertions hard-grounds (re-arms the runway) on
    completion.

LIMITS: ListUINodes / lua_expr require Play Mode; a bridge INVALID_STATE there
surfaces as a failed assertion with the bridge message, not an exception.
"""
from __future__ import annotations

import re
from typing import Any

from ..bridge import bridge_client
from ..bridge.session import bridge_call
from ..state import ESCALATION_GATE, LUA_BUDGET, escalation_response, runway_response

TOOL_NAME = "verify_assertions"

_ERROR_LEVELS = frozenset({"Error", "Exception", "Assert"})
_TAIL_MAX_LINES = 500


class UnknownAssertionError(ValueError):
    """Raised when an assertion dict has an unrecognized ``type``."""


def _fail(detail: str) -> dict[str, Any]:
    return {"passed": False, "detail": detail}


def _ok() -> dict[str, Any]:
    return {"passed": True, "detail": ""}


async def _eval_expect_no_errors(a: dict[str, Any]) -> dict[str, Any]:
    tail_args: dict[str, Any] = {"maxLines": _TAIL_MAX_LINES}
    since = a.get("since_line_number")
    if since is not None:
        tail_args["sinceLineNumber"] = int(since)
    resp = await bridge_call("TailEditorLog", tail_args)
    bad = [
        ln for ln in resp.get("lines", [])
        if ln.get("logType") in _ERROR_LEVELS
    ]
    if not bad:
        return _ok()
    first = bad[0]
    return _fail(
        f"{len(bad)} error-level log line(s); first: "
        f"[{first.get('logType')}] {first.get('message', '')}"
    )


async def _eval_log_matches(a: dict[str, Any]) -> dict[str, Any]:
    pattern = a.get("pattern")
    if not pattern:
        return _fail("log_matches requires a 'pattern'")
    absent = bool(a.get("absent", False))
    regex = re.compile(pattern)
    tail_args: dict[str, Any] = {"maxLines": _TAIL_MAX_LINES}
    since = a.get("since_line_number")
    if since is not None:
        tail_args["sinceLineNumber"] = int(since)
    resp = await bridge_call("TailEditorLog", tail_args)
    matched = [
        ln for ln in resp.get("lines", [])
        if regex.search(ln.get("message", ""))
    ]
    if absent:
        if matched:
            return _fail(
                f"pattern {pattern!r} matched {len(matched)} line(s) but should be absent; "
                f"first: {matched[0].get('message', '')}"
            )
        return _ok()
    if matched:
        return _ok()
    return _fail(f"pattern {pattern!r} matched no log line")


async def _eval_play_state(a: dict[str, Any]) -> dict[str, Any]:
    expected = a.get("equals")
    if expected is None:
        return _fail("play_state requires 'equals'")
    resp = await bridge_call("GetPlayState", {})
    actual = resp.get("state")
    if actual == expected:
        return _ok()
    return _fail(f"play_state is {actual!r}, expected {expected!r}")


def _node_matches(node: dict[str, Any], name: str | None, path: str | None) -> bool:
    if name is not None and node.get("name") != name:
        return False
    if path is not None and node.get("path") != path:
        return False
    return True


async def _eval_node(a: dict[str, Any], require_visible: bool) -> dict[str, Any]:
    name = a.get("name")
    path = a.get("path")
    if name is None and path is None:
        return _fail("node assertion requires 'name' or 'path'")
    args: dict[str, Any] = {}
    if a.get("panel_uid") is not None:
        args["panelUid"] = int(a["panel_uid"])
    if a.get("path_prefix"):
        args["pathPrefix"] = str(a["path_prefix"])
    args["includeAll"] = bool(a.get("include_all", True))
    try:
        resp = await bridge_call("ListUINodes", args)
    except bridge_client.BridgeError as exc:
        return _fail(f"ListUINodes failed: {exc}")
    nodes = resp.get("nodes", [])
    matches = [n for n in nodes if _node_matches(n, name, path)]
    target = name if name is not None else path
    if not matches:
        return _fail(f"no UI node matched {target!r}")
    if require_visible and not any(n.get("visible") for n in matches):
        return _fail(f"node {target!r} found but not visible")
    return _ok()


async def _eval_lua_expr(a: dict[str, Any]) -> dict[str, Any]:
    expr = a.get("expr")
    expect = a.get("expect")
    goal = a.get("goal")
    if not expr:
        return _fail("lua_expr requires 'expr'")
    if expect is None:
        return _fail("lua_expr requires 'expect'")
    if not goal or not str(goal).strip():
        return _fail("lua_expr requires a non-empty 'goal'")
    # NOTE: lua_expr does NOT charge the runway. verify_assertions IS the grounding
    # / recovery act (it hard-grounds on completion, see verify_assertions()); if its
    # own expr charged, the call the agent must make to recover could be refused —
    # a deadlock. goal stays required (documents intent) but is not a budget key.
    try:
        resp = await bridge_call("LuaEval", {"script": expr})
    except bridge_client.BridgeError as exc:
        return _fail(f"LuaEval failed: {exc}")
    raw = resp.get("raw")
    if raw is None:
        values = resp.get("returnValues") or []
        raw = values[0] if values else None
    if str(raw) == str(expect):
        return _ok()
    return _fail(f"lua_expr returned {raw!r}, expected {expect!r}")


async def _eval_one(a: dict[str, Any]) -> dict[str, Any]:
    t = a.get("type")
    if t == "expect_no_errors":
        return await _eval_expect_no_errors(a)
    if t == "log_matches":
        return await _eval_log_matches(a)
    if t == "play_state":
        return await _eval_play_state(a)
    if t == "node_exists":
        return await _eval_node(a, require_visible=False)
    if t == "node_visible":
        return await _eval_node(a, require_visible=True)
    if t == "lua_expr":
        return await _eval_lua_expr(a)
    raise UnknownAssertionError(f"unknown assertion type {t!r}")


async def verify_assertions(
    assertions: list[dict[str, Any]], goal: str | None = None
) -> dict[str, Any]:
    """Evaluate ``assertions`` server-side and return a compact verdict.

    Returns ``{passed, failures, results, lua_runway}`` where ``results`` is
    per-assertion ``{index, type, passed, detail}``, ``failures`` is the subset that
    failed, and ``lua_runway`` reflects the hard re-arm this call performed (verify
    is the HARD grounding signal — PASS or FAIL, it fully re-arms the lua runway).
    Assertions are evaluated in order under the bridge's serialized lock.

    Raises:
        UnknownAssertionError: an assertion has an unrecognized ``type`` (raised
            before/at that assertion; assertions are validated as evaluated). This
            path does NOT ground: a malformed assertion list reaches no verdict, so
            no grounding signal is issued and the runway is left as-is.
    """
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for idx, a in enumerate(assertions):
        verdict = await _eval_one(a)
        row = {
            "index": idx,
            "type": a.get("type"),
            "passed": verdict["passed"],
            "detail": verdict["detail"],
        }
        results.append(row)
        if not verdict["passed"]:
            failures.append(row)
    # verify_assertions is the HARD grounding signal: PASS or FAIL, the agent now
    # holds server-adjudicated ground truth, so fully re-arm the lua runway.
    passed = len(failures) == 0
    outcome = await LUA_BUDGET.ground(hard=True)
    verdict: dict[str, Any] = {
        "passed": passed,
        "failures": failures,
        "results": results,
        "lua_runway": runway_response(outcome, LUA_BUDGET),
    }
    # Escalation gate: a FAIL repeated under the same goal means the lua path is
    # not reaching the real environment. Append advice (never raise — this is a
    # read-only adjudication tool). goal=None no-ops, preserving legacy behavior.
    esc = await ESCALATION_GATE.record_verify(goal, passed)
    if esc.should_escalate:
        verdict["escalation"] = escalation_response(esc)
    return verdict


def register(mcp: Any) -> None:
    @mcp.tool(TOOL_NAME)
    async def _tool(
        assertions: list[dict[str, Any]], goal: str | None = None
    ) -> dict[str, Any]:
        return await verify_assertions(assertions, goal)
