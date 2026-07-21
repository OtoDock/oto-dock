"""Codex approval-request bridge (codex_approvals.py).

Pure translation + response-shaping, verified against the live codex 0.120.0
ServerRequest/response shapes:
  * exec approvals (legacy ``execCommandApproval`` Array<string> + v2
    ``item/commandExecution/requestApproval`` string command+cwd) → Bash gate.
  * patch approvals (legacy ``applyPatchApproval`` fileChanges + lean v2
    ``item/fileChange/requestApproval`` itemId-only) → Write gate.
  * escalation → synthetic CodexEscalation gate.
  * decision enums: legacy approved/denied; v2 accept/decline (honoring
    ``availableDecisions``); escalation GrantedPermissionProfile + scope.
  * the handler factory injects the decision authority and answers non-approval
    ServerRequests safely.

No daemon / no DB — pure logic.
"""

import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core.layers.codex.codex_approvals import (  # noqa: E402
    approval_to_tool,
    approval_for_sandbox,
    build_response,
    build_sandbox_policy,
    make_server_request_handler,
    APPROVAL_METHODS,
)


# ─────────────────────────────────────────────────────────────────────────
# approval_to_tool
# ─────────────────────────────────────────────────────────────────────────


def test_exec_v2_carries_command_and_cwd():
    # The live shape (from the V4 probe): command is a string, cwd present.
    tn, ti = approval_to_tool(
        "item/commandExecution/requestApproval",
        {"command": "printf probe > /home/u/x.txt", "cwd": "/work", "itemId": "c1"},
    )
    assert tn == "Bash"
    assert ti == {"command": "printf probe > /home/u/x.txt", "cwd": "/work"}


def test_exec_legacy_joins_array_command():
    tn, ti = approval_to_tool(
        "execCommandApproval",
        {"command": ["/bin/bash", "-lc", "rm -rf /"], "cwd": "/work"},
    )
    assert tn == "Bash"
    assert ti["command"] == "/bin/bash -lc rm -rf /"
    assert ti["cwd"] == "/work"


def test_exec_v2_uses_unwrapped_command_actions():
    """0.120.0 sends `command` as the `bash -lc '<inner>'` wrapper PLUS the
    unwrapped inner in `commandActions[].command` (verified live). The bridge
    must hand decide_tool_permission the INNER command so the bash policy sees
    `curl` (extended tier → prompts) — NOT the outer `bash`, which isn't
    allow-listed and would hard-deny the call WITHOUT ever prompting."""
    tn, ti = approval_to_tool(
        "item/commandExecution/requestApproval",
        {
            "command": "/bin/bash -lc 'curl -sL https://example.com | head -n 1'",
            "cwd": "/tmp",
            "commandActions": [
                {"type": "unknown", "command": "curl -sL https://example.com | head -n 1"}
            ],
        },
    )
    assert tn == "Bash"
    assert ti["command"] == "curl -sL https://example.com | head -n 1"
    assert "bash" not in ti["command"]      # the wrapper is gone
    assert ti["cwd"] == "/tmp"


def test_exec_unwraps_bash_lc_string_fallback():
    """No commandActions → unwrap the `<shell> -…c '<inner>'` wrapper from the
    `command` string so decide still sees the real command."""
    _, ti = approval_to_tool(
        "item/commandExecution/requestApproval",
        {"command": "/bin/bash -lc 'git push origin main'", "cwd": "/work"},
    )
    assert ti["command"] == "git push origin main"


def test_exec_multiple_command_actions_joined():
    """Multiple commandActions are joined with `;` so _check_bash validates each
    segment (a pipeline / compound command)."""
    _, ti = approval_to_tool(
        "item/commandExecution/requestApproval",
        {"command": "/bin/bash -lc '...'",
         "commandActions": [{"command": "curl -sL https://x"},
                            {"command": "rm -rf /etc"}]},
    )
    assert ti["command"] == "curl -sL https://x ; rm -rf /etc"


def test_exec_bare_command_passes_through():
    """A non-wrapper command is left unchanged (no false unwrap)."""
    _, ti = approval_to_tool(
        "item/commandExecution/requestApproval",
        {"command": "curl -sL https://example.com", "cwd": "/work"},
    )
    assert ti["command"] == "curl -sL https://example.com"


def test_patch_legacy_extracts_paths():
    tn, ti = approval_to_tool(
        "applyPatchApproval",
        {"fileChanges": {"/etc/passwd": {}, "/work/ok.txt": {}}},
    )
    assert tn == "Write"
    assert ti["file_path"] == "/etc/passwd"  # first path is the representative
    assert set(ti["_codex_paths"]) == {"/etc/passwd", "/work/ok.txt"}


def test_patch_v2_lean_uses_item_paths():
    tn, ti = approval_to_tool(
        "item/fileChange/requestApproval",
        {"itemId": "fc1"},
        item_paths={"fc1": ["/home/u/outside.txt"]},
    )
    assert tn == "Write"
    assert ti["file_path"] == "/home/u/outside.txt"


def test_patch_v2_lean_without_correlation_is_pathless():
    # No item_paths → empty file_path; decide_tool_permission then mode-gates
    # (an escape write still prompts in default mode — the correct floor).
    tn, ti = approval_to_tool("item/fileChange/requestApproval", {"itemId": "fc1"})
    assert tn == "Write"
    assert ti["file_path"] == ""
    assert ti["_codex_paths"] == []


def test_escalation_maps_to_synthetic_tool():
    tn, ti = approval_to_tool(_esc := "item/permissions/requestApproval",
                              {"reason": "wants network"})
    assert tn == "CodexEscalation"
    assert ti["reason"] == "wants network"


def test_non_approval_returns_none():
    assert approval_to_tool("item/tool/call", {}) is None
    assert approval_to_tool("account/chatgptAuthTokens/refresh", {}) is None
    assert approval_to_tool("turn/completed", {}) is None


# ─────────────────────────────────────────────────────────────────────────
# build_response
# ─────────────────────────────────────────────────────────────────────────


def test_legacy_decision_enum():
    assert build_response("execCommandApproval", {}, True) == {"decision": "approved"}
    assert build_response("applyPatchApproval", {}, False) == {"decision": "denied"}


def test_v2_command_decision_enum():
    assert build_response("item/commandExecution/requestApproval", {}, True) == {"decision": "accept"}
    assert build_response("item/commandExecution/requestApproval", {}, False) == {"decision": "decline"}


def test_v2_command_deny_always_declines_never_cancels():
    # The real daemon advertises only accept-variants + "cancel" (verified live:
    # ['accept', {acceptWithExecpolicyAmendment: …}, 'cancel']) — it does NOT list
    # a continue-style deny. A per-command Deny MUST send "decline" (reject this
    # command, CONTINUE the turn), never the advertised "cancel" (which aborts the
    # whole turn — the model gets no rejection and stops). Verified live: decline →
    # model talks back; cancel → 0 further events. So the decision must NOT depend
    # on availableDecisions for the deny path.
    real = {"availableDecisions": ["accept",
                                    {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["curl"]}},
                                    "cancel"]}
    assert build_response("item/commandExecution/requestApproval", real, False) == {"decision": "decline"}
    # Even when "cancel" is the only deny-ish value offered, never send it.
    assert build_response("item/commandExecution/requestApproval", {"availableDecisions": ["accept", "cancel"]}, False) == {"decision": "decline"}
    # Allow is always plain "accept" — never acceptWithExecpolicyAmendment (a
    # one-shot Allow must not persist an allow-rule for the command pattern).
    assert build_response("item/commandExecution/requestApproval", real, True) == {"decision": "accept"}


def test_v2_filechange_decision_enum():
    assert build_response("item/fileChange/requestApproval", {}, True) == {"decision": "accept"}
    assert build_response("item/fileChange/requestApproval", {}, False) == {"decision": "decline"}


def test_escalation_response_grants_on_allow_only():
    req = {"permissions": {"network": None, "fileSystem": None}}
    allow = build_response("item/permissions/requestApproval", req, True)
    assert allow == {"permissions": {"network": None, "fileSystem": None}, "scope": "turn"}
    deny = build_response("item/permissions/requestApproval", req, False)
    assert deny == {"permissions": {}, "scope": "turn"}


# ─────────────────────────────────────────────────────────────────────────
# make_server_request_handler
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_allows_via_decide():
    calls = []

    async def decide(tool_name, tool_input):
        calls.append((tool_name, tool_input))
        return {"decision": "allow"}

    handler = make_server_request_handler(decide)
    resp = await handler(
        "item/commandExecution/requestApproval",
        {"command": "ls", "cwd": "/work"},
    )
    assert resp == {"decision": "accept"}
    assert calls == [("Bash", {"command": "ls", "cwd": "/work"})]


@pytest.mark.asyncio
async def test_handler_denies_via_decide():
    async def decide(tool_name, tool_input):
        return {"decision": "deny", "reason": "outside scope"}

    handler = make_server_request_handler(decide)
    resp = await handler("execCommandApproval", {"command": ["rm", "-rf", "/"]})
    assert resp == {"decision": "denied"}


@pytest.mark.asyncio
async def test_handler_passes_item_paths_for_lean_filechange():
    seen = {}

    async def decide(tool_name, tool_input):
        seen.update(tool_input)
        return {"decision": "deny"}

    handler = make_server_request_handler(
        decide, get_item_paths=lambda: {"fc1": ["/secret"]},
    )
    resp = await handler("item/fileChange/requestApproval", {"itemId": "fc1"})
    assert resp == {"decision": "decline"}
    assert seen["file_path"] == "/secret"


@pytest.mark.asyncio
async def test_handler_non_approval_safe_answers():
    async def decide(tool_name, tool_input):
        raise AssertionError("decide must not be called for non-approvals")

    handler = make_server_request_handler(decide)
    assert await handler("item/tool/requestUserInput", {"questions": []}) == {"answers": {}}
    assert await handler("item/tool/call", {"tool": "x"}) == {"contentItems": [], "success": False}


@pytest.mark.asyncio
async def test_handler_unanswerable_raises_for_error_response():
    async def decide(tool_name, tool_input):
        return {"decision": "allow"}

    handler = make_server_request_handler(decide)
    # Auth-token refresh + elicitation have no safe in-protocol answer → raise,
    # which the AppServerClient turns into a JSON-RPC error (daemon won't hang).
    with pytest.raises(KeyError):
        await handler("account/chatgptAuthTokens/refresh", {})


# ─────────────────────────────────────────────────────────────────────────
# Sandbox / approval policy — shared by local layer + satellite
# ─────────────────────────────────────────────────────────────────────────


def test_approval_for_sandbox():
    assert approval_for_sandbox("danger-full-access") == "never"
    assert approval_for_sandbox("workspace-write") == "on-request"
    assert approval_for_sandbox("read-only") == "on-request"


def test_sandbox_policy_danger_full_access():
    assert build_sandbox_policy("danger-full-access", "/work") == {"type": "dangerFullAccess"}


def test_sandbox_policy_read_only():
    p = build_sandbox_policy("read-only", "/work")
    assert p["type"] == "readOnly"
    assert p["access"] == {"type": "fullAccess"}
    assert p["networkAccess"] is False


def test_sandbox_policy_workspace_write_sets_writable_root():
    p = build_sandbox_policy("workspace-write", "/work/agent/u")
    assert p["type"] == "workspaceWrite"
    assert p["writableRoots"] == ["/work/agent/u"]
    assert p["readOnlyAccess"] == {"type": "fullAccess"}
    assert p["networkAccess"] is False
    assert p["excludeTmpdirEnvVar"] is False
    assert p["excludeSlashTmp"] is False


def test_sandbox_policy_workspace_write_empty_root():
    assert build_sandbox_policy("workspace-write", "")["writableRoots"] == []


# ─────────────────────────────────────────────────────────────────────────
# MCP tool-call gate via mcpServer/elicitation/request (Codex has NO per-MCP-
# tool approval method — this elicitation IS the gate)
# ─────────────────────────────────────────────────────────────────────────


def _elicitation(server="testmcp", tool="ping", kind="mcp_tool_call"):
    meta = {"persist": ["session", "always"], "tool_params": {}}
    if kind is not None:
        meta["codex_approval_kind"] = kind
    return {
        "serverName": server, "mode": "form", "_meta": meta,
        "message": f'Allow the {server} MCP server to run tool "{tool}"?',
        "requestedSchema": {"type": "object", "properties": {}},
    }


@pytest.mark.asyncio
async def test_elicitation_mcp_tool_call_allow_accepts():
    seen = []

    async def decide(tool_name, tool_input):
        seen.append(tool_name)
        return {"decision": "allow"}

    handler = make_server_request_handler(decide)
    resp = await handler("mcpServer/elicitation/request", _elicitation())
    assert resp == {"action": "accept", "content": None, "_meta": None}
    assert seen == ["mcp__testmcp__ping"]   # routed through decide as mcp__server__tool


@pytest.mark.asyncio
async def test_elicitation_mcp_tool_call_deny_declines():
    async def decide(tool_name, tool_input):
        return {"decision": "deny"}

    handler = make_server_request_handler(decide)
    resp = await handler("mcpServer/elicitation/request", _elicitation(tool="list_notifications"))
    assert resp == {"action": "decline", "content": None, "_meta": None}


@pytest.mark.asyncio
async def test_elicitation_passes_tool_params_as_input():
    seen = {}

    async def decide(tool_name, tool_input):
        seen["name"] = tool_name
        seen["input"] = tool_input
        return {"decision": "allow"}

    e = _elicitation(server="schedules-mcp", tool="create_task")
    e["_meta"]["tool_params"] = {"title": "x"}
    handler = make_server_request_handler(decide)
    await handler("mcpServer/elicitation/request", e)
    assert seen["name"] == "mcp__schedules-mcp__create_task"
    assert seen["input"] == {"title": "x"}


@pytest.mark.asyncio
async def test_elicitation_non_tool_call_declines_without_deciding():
    async def decide(tool_name, tool_input):
        raise AssertionError("decide must not run for a non-tool-call elicitation")

    # A genuine elicitation (server asking the user for form input) has no
    # mcp_tool_call marker — we decline rather than surface that UI.
    handler = make_server_request_handler(decide)
    resp = await handler("mcpServer/elicitation/request", _elicitation(kind=None))
    assert resp == {"action": "decline", "content": None, "_meta": None}


def test_approval_methods_constant():
    assert "item/commandExecution/requestApproval" in APPROVAL_METHODS
    assert "execCommandApproval" in APPROVAL_METHODS
    assert "item/fileChange/requestApproval" in APPROVAL_METHODS
    assert "item/permissions/requestApproval" in APPROVAL_METHODS
    assert "turn/completed" not in APPROVAL_METHODS


# ─────────────────────────────────────────────────────────────────────────
# request_user_input question bridge (item/tool/requestUserInput)
# ─────────────────────────────────────────────────────────────────────────


_QS = [{"id": "color_theme", "header": "Theme",
        "question": "Which theme?",
        "options": [{"label": "Dark"}, {"label": "Light"}]}]


@pytest.mark.asyncio
async def test_request_user_input_surfaces_and_wraps_answers():
    """With ask_question wired, the question set is surfaced and the returned
    answers MAP (keyed by the verbatim question id) is wrapped in {"answers": …}."""
    seen = {}

    async def ask(questions):
        seen["questions"] = questions
        return {"color_theme": {"answers": ["Dark"]}}

    async def decide(tool_name, tool_input):
        raise AssertionError("decide must not run for a question")

    handler = make_server_request_handler(decide, ask_question=ask)
    resp = await handler("item/tool/requestUserInput", {"questions": _QS})
    assert resp == {"answers": {"color_theme": {"answers": ["Dark"]}}}
    assert seen["questions"] == _QS


@pytest.mark.asyncio
async def test_request_user_input_declines_without_ask():
    """No ask_question wired (autonomous task run / old satellite) → decline
    empty so the held turn unwinds instead of hanging."""
    async def decide(tool_name, tool_input):
        return {"decision": "allow"}

    handler = make_server_request_handler(decide)   # no ask_question
    assert await handler("item/tool/requestUserInput", {"questions": _QS}) == {"answers": {}}


@pytest.mark.asyncio
async def test_request_user_input_empty_questions_declines():
    async def ask(questions):
        raise AssertionError("ask must not run with no questions")

    handler = make_server_request_handler(lambda *a: None, ask_question=ask)
    assert await handler("item/tool/requestUserInput", {"questions": []}) == {"answers": {}}


@pytest.mark.asyncio
async def test_request_user_input_ask_failure_never_hangs():
    """A surface/transport failure declines empty rather than propagating —
    the codex turn must never hang on a question we couldn't answer."""
    async def ask(questions):
        raise RuntimeError("tunnel down")

    handler = make_server_request_handler(lambda *a: None, ask_question=ask)
    assert await handler("item/tool/requestUserInput", {"questions": _QS}) == {"answers": {}}
