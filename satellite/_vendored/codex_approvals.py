"""codex_approvals.py — bridge Codex app-server approval ``ServerRequest``s onto
the platform permission decision, and build the JSON-RPC response.

Stdlib-only + config-free so it vendors verbatim to the satellite via
``scripts/sync-satellite-code.sh`` (one shared bridge for the local layer and
the remote satellite — edit here, run the sync script, bump
``SHARED_CODEX_APPROVALS_HASH`` in ``satellite/config.py``).

Codex's native tools are gated at the **sandbox boundary**, not per-tool: with
``approvalPolicy != never`` the daemon fires an approval ``ServerRequest`` only
when a command/edit would **escape** the sandbox (write outside the writable
root, network, escalation). We translate each escape to a ``(tool_name,
tool_input)`` and run it through the **one** decision authority
(``decide_tool_permission``) so the escape obeys the session's mode +
SecurityContext, exactly like the CLI hook.

**Configured MCP tool calls are gated HERE too** — NOT by any transport /
interceptor gate. Codex emits a ``mcpServer/elicitation/request`` (handled by
``_handle_mcp_elicitation``) for every ``[mcp_servers.*]`` tool call under
``approvalsReviewer:"user"``, regardless of transport — stdio, HTTP, and Docker
MCPs all gate the same way through the same decision authority (0.4: verified
transport-agnostic; no reverse-proxy / ``/mcp-gate`` route exists).

Verified live vs codex 0.120.0 (``item/commandExecution/requestApproval`` carries
``command``+``cwd`` inline; legacy ``execCommandApproval``/``applyPatchApproval``
are self-contained; ``item/fileChange/requestApproval`` is lean → ``itemId``
only). Decision enums: legacy ``ReviewDecision`` (approved/denied → both
*continue* the turn on deny); v2 ``CommandExecutionApprovalDecision``
(accept / decline / cancel) + ``FileChangeApprovalDecision`` (accept/decline).
**Deny semantics (verified live vs 0.120.0):** ``decline`` = reject THIS
command but let the turn CONTINUE (model sees the rejection and responds, like
the Claude CLI); ``cancel`` = ABORT the whole turn (no rejection, ends
silently). The daemon's ``availableDecisions`` advertises only accept-variants +
``cancel`` — so a per-command Deny must send ``decline`` regardless, never the
advertised ``cancel``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Awaitable, Callable


def self_hash() -> str:
    """SHA256 of this module's source — used by the satellite drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""

# Approval ServerRequest methods, grouped by how we translate them.
_EXEC_APPROVALS = frozenset({
    "execCommandApproval",                      # legacy, self-contained
    "item/commandExecution/requestApproval",    # v2 (carries command+cwd inline)
})
_PATCH_APPROVALS = frozenset({
    "applyPatchApproval",                       # legacy, self-contained (fileChanges)
    "item/fileChange/requestApproval",          # v2 (lean — itemId only)
})
_ESCALATION = "item/permissions/requestApproval"
APPROVAL_METHODS = _EXEC_APPROVALS | _PATCH_APPROVALS | {_ESCALATION}

# A decision call: (tool_name, tool_input) -> {"decision": "allow"|"deny", ...}.
DecideFn = Callable[[str, dict], Awaitable[dict]]

# A question call: (questions) -> the answers MAP {<question_id>: {"answers": [str, ...]}}.
# Surfaces the codex ``request_user_input`` questions to the dashboard, blocks for
# the human answer, and returns it keyed by the VERBATIM question id. ``None`` (task
# runs / not wired) ⇒ the bridge declines with empty answers.
AskFn = Callable[[list], Awaitable[dict]]


# Codex runs EVERY native command as ``<shell> -…c '<inner>'`` (e.g.
# ``/bin/bash -lc 'curl … | head'``). If we passed that wrapper to the platform
# bash policy it would see the outer ``bash`` (not allow-listed) and HARD-DENY
# the call WITHOUT prompting — so a sandbox-escaping command (network, write
# outside the workspace) could never be approved, only silently blocked. We
# unwrap to the real inner command so ``decide_tool_permission`` evaluates IT
# through the full tier + path pipeline (curl → extended → prompts in default),
# exactly like the Claude CLI's Bash tool. The inner command still goes through
# every dangerous-pattern / bypass / allow-list / path check.
_BASH_WRAP_RE = re.compile(
    r"""^\s*(?:/\S+/)?(?:ba|da|z)?sh\s+-[A-Za-z]*c\s+(['"])(.*)\1\s*$""",
    re.DOTALL,
)


def _unwrap_bash_lc(command: str) -> str:
    """``<shell> -…c '<inner>'`` → ``<inner>``; unchanged if not a shell wrapper."""
    m = _BASH_WRAP_RE.match(command or "")
    return m.group(2) if m else (command or "")


# Codex on Windows runs native commands through the Windows shell —
# ``powershell.exe -Command '<inner>'`` (also ``pwsh -c`` / ``cmd /c``). The
# POSIX _BASH_WRAP_RE won't unwrap those, so without this they'd reach the
# platform gate as an opaque ``powershell.exe …`` *Bash* command (unknown → it
# would prompt, but the INNER PowerShell never gets the PS dangerous-deny /
# cross-user path / credential checks). We ROUTE these to the platform's
# PowerShell checker (``_check_powershell``) instead, which unwraps + analyzes the
# inner and applies the PS dangerous patterns. (``cmd /c`` is grouped with
# PowerShell — the checker dangerous-scans + ASK-nets its inner too.)
_WINDOWS_SHELL_WRAP_RE = re.compile(
    r"""^\s*(?:[A-Za-z]:)?[\\/]?(?:[^\s\\/]+[\\/])*"""          # optional drive + path prefix
    r"""(?:(?:powershell|pwsh)(?:\.exe)?\b|cmd(?:\.exe)?\s+/c\b)""",
    re.IGNORECASE | re.VERBOSE,
)


def _is_windows_shell_wrapper(command: str) -> bool:
    """True if ``command`` invokes the Windows shell (powershell/pwsh/cmd /c) →
    route to the PowerShell checker rather than the Bash checker."""
    return bool(_WINDOWS_SHELL_WRAP_RE.match(command or ""))


def _exec_command(params: dict) -> str:
    """The real command for the bash-policy check.

    Prefer the daemon's already-unwrapped ``commandActions[*].command`` (present
    on ``item/commandExecution/requestApproval`` in 0.120.0 — verified live);
    else unwrap the ``bash -lc`` wrapper from ``command`` (legacy
    ``execCommandApproval`` sends ``command: string[]``). Multiple actions are
    joined with ``;`` so ``_check_bash`` validates every segment.
    """
    actions = params.get("commandActions")
    if isinstance(actions, list):
        parts = [a["command"] for a in actions
                 if isinstance(a, dict) and isinstance(a.get("command"), str)
                 and a["command"].strip()]
        if parts:
            return " ; ".join(parts)
    cmd = params.get("command")
    if isinstance(cmd, list):  # legacy execCommandApproval = Array<string>
        cmd = " ".join(str(c) for c in cmd)
    return _unwrap_bash_lc(cmd or "")


def approval_to_tool(
    method: str, params: dict, *, item_paths: dict | None = None,
) -> tuple[str, dict] | None:
    """Translate an approval ``ServerRequest`` to ``(tool_name, tool_input)``.

    Returns ``None`` if ``method`` is not an approval. ``item_paths`` optionally
    maps ``itemId -> [paths]`` (populated from preceding ``item/started``
    notifications) to enrich a lean v2 ``item/fileChange/requestApproval`` — when
    absent the file gate falls back to mode-only (an escape write still prompts
    in default mode, which is the correct floor).
    """
    if method in _EXEC_APPROVALS:
        cmd = _exec_command(params)
        cwd = params.get("cwd") or ""
        # Windows-shell invocations (powershell/pwsh/cmd /c) route to the platform
        # PowerShell checker; everything else is a POSIX/bash command.
        if _is_windows_shell_wrapper(cmd):
            return "PowerShell", {"command": cmd, "cwd": cwd}
        return "Bash", {"command": cmd, "cwd": cwd}

    if method in _PATCH_APPROVALS:
        paths: list[str] = []
        changes = params.get("fileChanges")
        if isinstance(changes, dict):                 # legacy applyPatchApproval
            paths = [str(p) for p in changes.keys()]
        elif item_paths:                               # lean v2 → correlated paths
            paths = list(item_paths.get(params.get("itemId"), []) or [])
        return "Write", {"file_path": paths[0] if paths else "", "_codex_paths": paths}

    if method == _ESCALATION:
        # Permission escalation (expand sandbox for the turn/session). Route it
        # through the same gate as a synthetic tool → denied in plan, prompted in
        # default, allowed in dontAsk (where the sandbox is already full-access so
        # it never actually fires).
        return "CodexEscalation", {"reason": params.get("reason") or ""}

    return None


def build_response(method: str, params: dict, allow: bool) -> dict:
    """Build the JSON-RPC ``result`` answering an approval ``ServerRequest``."""
    if method in ("execCommandApproval", "applyPatchApproval"):
        # Legacy ReviewDecision.
        return {"decision": "approved" if allow else "denied"}

    if method == "item/commandExecution/requestApproval":
        # v2 CommandExecutionApprovalDecision. The daemon's ``availableDecisions``
        # advertises only the ACCEPT variants + ``cancel`` (verified live vs
        # 0.120.0: ``['accept', {acceptWithExecpolicyAmendment: …}, 'cancel']``);
        # it does NOT list a continue-style deny. The correct answer for a
        # per-command **Deny** is ``decline`` — "reject THIS command but let the
        # turn CONTINUE" so the model sees the rejection and responds, exactly
        # like the Claude CLI. ``cancel`` is turn-ABORT (the model gets no
        # rejection and the turn ends silently — verified live: ``decline`` →
        # model talks back; ``cancel`` → 0 further events), so we NEVER send it
        # for a deny even though it's the only deny-ish value advertised. On allow
        # we send plain ``accept`` (not ``acceptWithExecpolicyAmendment``, which
        # would persist an allow-rule for the command pattern — a one-shot Allow
        # must not).
        return {"decision": "accept" if allow else "decline"}

    if method == "item/fileChange/requestApproval":
        # v2 FileChangeApprovalDecision.
        return {"decision": "accept" if allow else "decline"}

    if method == _ESCALATION:
        # PermissionsRequestApprovalResponse = {permissions: GrantedPermissionProfile, scope}.
        # Grant exactly what was requested (turn-scoped) on allow; nothing on deny.
        return {
            "permissions": (params.get("permissions") or {}) if allow else {},
            "scope": "turn",
        }

    return {}


# ─────────────────────────────────────────────────────────────────────────
# Sandbox / approval policy — shared by the local layer and the satellite
# (both must build the structured per-turn sandboxPolicy + derive the approval
# policy from the resolved SandboxMode).
# ─────────────────────────────────────────────────────────────────────────


# The approval policy that makes Codex ask for EVERY shell command its own
# trusted list does not cover and for every patch (``AskForApproval::
# UnlessTrusted``). Accepted on the app-server's ``thread/start``,
# ``thread/resume`` and ``turn/start`` (its integration tests send it at
# 0.153.4 and 0.154.0) but retired from config files and the CLI flag in
# 0.149 — so a session that sends it must be ready for a rejection and fall
# back (``UNTRUSTED_FALLBACK``); the bump checklist probes it.
UNTRUSTED_POLICY = "untrusted"
UNTRUSTED_FALLBACK = "on-request"


def approval_for_sandbox(sandbox_mode: str, attended: bool = False) -> str:
    """Derive the Codex ``approvalPolicy`` from the resolved ``SandboxMode``.

    ``danger-full-access`` (dontAsk/auto) → ``"never"``: nothing asks.
    ``read-only`` and ``workspace-write`` → ``"on-request"`` by default, so a
    sandbox escape, an MCP call and Codex's own dangerous-command heuristics
    fire an approval we route through ``decide_tool_permission``.

    ``attended=True`` (a dashboard app-server chat — a person can answer) →
    ``"untrusted"``: every shell command Codex's trusted list does not cover
    and every patch become an approval request, so the bridge decides each
    one exactly as Claude's PreToolUse hook is decided — the read tier runs,
    edits run in acceptEdits and ask in default, destructive commands ask in
    both (HOOKS.md "Attended Codex chats"). Under ``on-request`` Codex ran an
    in-workspace ``rm`` without asking anyone. The interactive TUI keeps
    ``on-request`` (its native prompt is the person's).
    """
    if sandbox_mode == "danger-full-access":
        return "never"
    return UNTRUSTED_POLICY if attended else "on-request"


def is_untrusted_policy_rejection(error: Exception | str) -> bool:
    """Did the app-server refuse a request because of the ``untrusted``
    approval policy? (The fallback trigger — a future Codex may retire the
    wire value as it retired the config key.)"""
    text = str(error).lower()
    return "untrusted" in text or "unlesstrusted" in text or "approvalpolicy" in text


def build_sandbox_policy(sandbox_mode: str, writable_root: str = "") -> dict:
    """Build the structured ``SandboxPolicy`` for a ``turn/start`` override.

    ``thread/start`` takes the simple ``SandboxMode`` enum, but ``turn/start``
    only accepts the structured ``SandboxPolicy`` — so a mid-session mode change
    rides per turn through this. Shapes verified vs codex 0.120.0.
    """
    if sandbox_mode == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if sandbox_mode == "read-only":
        return {"type": "readOnly", "access": {"type": "fullAccess"},
                "networkAccess": False}
    # workspace-write: writable root = the session cwd; full read elsewhere.
    return {
        "type": "workspaceWrite",
        "writableRoots": [writable_root] if writable_root else [],
        "readOnlyAccess": {"type": "fullAccess"},
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


# Codex gates a configured MCP **tool call** via an elicitation whose message is
# ``Allow the <server> MCP server to run tool "<tool>"?`` — the tool name lives
# only in that string (the params carry serverName + _meta, not a toolName field).
_ELICITATION_TOOL_RE = re.compile(r'run tool "([^"]+)"')


def _parse_elicitation_tool(message: str) -> str:
    m = _ELICITATION_TOOL_RE.search(message or "")
    return m.group(1) if m else ""


async def _handle_mcp_elicitation(params: dict, decide: DecideFn, log) -> dict:
    """Answer ``mcpServer/elicitation/request``.

    Codex has **no per-MCP-tool approval method** — instead, under
    ``approvalsReviewer:"user"`` it gates each configured MCP tool call with an
    elicitation tagged ``_meta.codex_approval_kind == "mcp_tool_call"``. THIS is
    the MCP permission gate: route ``mcp__{server}__{tool}`` through the platform
    decision authority and answer ``accept``/``decline``. (In dontAsk the daemon
    runs ``approvalPolicy:"never"`` so this never fires — MCP tools just run.)

    A genuine elicitation (an MCP server asking the user for form/url input) has
    no ``mcp_tool_call`` marker — we don't surface that UI, so we ``decline`` it
    cleanly rather than hang the turn.
    """
    meta = params.get("_meta")
    kind = meta.get("codex_approval_kind") if isinstance(meta, dict) else None
    if kind != "mcp_tool_call":
        return {"action": "decline", "content": None, "_meta": None}
    server = params.get("serverName") or ""
    tool = _parse_elicitation_tool(params.get("message") or "")
    mcp_tool = f"mcp__{server}__{tool}" if tool else f"mcp__{server}__"
    tool_input = meta.get("tool_params") if isinstance(meta.get("tool_params"), dict) else {}
    decision = await decide(mcp_tool, tool_input)
    allow = (decision or {}).get("decision") == "allow"
    if log:
        log(f"codex mcp elicitation {mcp_tool} -> {'accept' if allow else 'decline'}")
    return {"action": "accept" if allow else "decline", "content": None, "_meta": None}


async def _handle_request_user_input(params: dict, ask: "AskFn | None", log) -> dict:
    """Answer ``item/tool/requestUserInput`` (the AskUserQuestion analogue).

    Codex holds the turn OPEN on this server→client request (core waits
    indefinitely — no server timeout) until we answer. With ``ask`` wired we
    surface the questions to the dashboard, block for the human answer, and
    return it keyed by the VERBATIM question id:
    ``{"answers": {<id>: {"answers": [<label|free-text>, ...]}}}`` (multi-value
    + free-text accepted, verified live). Without ``ask`` (autonomous task runs,
    or an old satellite) we decline with empty answers so the turn continues.
    """
    questions = params.get("questions")
    if ask is None or not isinstance(questions, list) or not questions:
        return {"answers": {}}
    try:
        answers = await ask(questions)
    except Exception as e:  # noqa: BLE001 — never hang the turn on a surface failure
        if log:
            log(f"codex request_user_input surface failed ({e}); declining")
        return {"answers": {}}
    return {"answers": answers if isinstance(answers, dict) else {}}


def _non_approval_response(method: str, params: dict) -> dict:
    """Safe answers for non-approval ``ServerRequest``s so the daemon never hangs.

    Raises ``KeyError`` for requests we can't safely answer (auth-token refresh) —
    the client turns that into a JSON-RPC error so the daemon surfaces it cleanly
    instead of stalling. We register no dynamic tools, so those decline.
    (``item/tool/requestUserInput`` is handled by ``_handle_request_user_input``
    in the main switch, not here.)
    """
    if method == "item/tool/call":
        # DynamicToolCall — we register no dynamic tools, so this is never ours.
        return {"contentItems": [], "success": False}
    # account/chatgptAuthTokens/refresh — the daemon manages its own ChatGPT
    # token via auth.json; no safe in-protocol answer.
    raise KeyError(method)


def make_server_request_handler(
    decide: DecideFn,
    *,
    ask_question: "AskFn | None" = None,
    get_item_paths: Callable[[], dict] | None = None,
    log: Callable[[str], None] | None = None,
):
    """Build an ``AppServerClient`` ``ServerRequestHandler``.

    ``decide`` is the injected decision authority — in-process
    ``decide_tool_permission`` on the proxy, or an HTTP POST to
    ``/v1/hooks/permission`` over the loopback tunnel on the satellite. Identical
    translation + response shaping either way; only *who answers* differs.

    ``ask_question`` (optional) is the analogous authority for
    ``request_user_input`` questions — surface + block for the human answer. When
    omitted (autonomous task runs, or an old satellite) questions decline empty.
    """
    async def handler(method: str, params: dict) -> dict:
        # MCP tool calls are gated by an elicitation, not an approval method.
        if method == "mcpServer/elicitation/request":
            return await _handle_mcp_elicitation(params, decide, log)
        # request_user_input (AskUserQuestion analogue) — held open until answered.
        if method == "item/tool/requestUserInput":
            return await _handle_request_user_input(params, ask_question, log)
        if method in APPROVAL_METHODS:
            item_paths = get_item_paths() if get_item_paths else None
            tool_name, tool_input = approval_to_tool(
                method, params, item_paths=item_paths,
            )  # type: ignore[misc]  # not None: method is an approval
            decision = await decide(tool_name, tool_input)
            allow = (decision or {}).get("decision") == "allow"
            if log:
                log(f"codex approval {method} tool={tool_name} -> "
                    f"{'allow' if allow else 'deny'}")
            return build_response(method, params, allow)
        return _non_approval_response(method, params)

    return handler
