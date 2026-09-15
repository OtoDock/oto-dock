"""The permission authority: the permission, codex-question, mcp-credentials and
session-files hooks, ``decide_tool_permission`` and ``ask_user_question``.

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from auth.path_policy import (
    EXTERNAL_DENIED_CLI_TOOLS,
    check_tool_access,
    _SHELL_COMMAND_TOOLS,
)
from api.sessions.sessions import verify_session_match
from core.session.session_state import (
    get_session_mode,
    set_session_mode,
    remember_session_tool_allow,
    is_session_tool_allowed,
    get_session_client_type,
    record_hook_activity,
    get_permission_queue,
    wait_for_permission,
    wait_for_question,
    get_session_security,
)
from api.hooks import routing

logger = logging.getLogger("claude-proxy")
router = APIRouter()


# ---------------------------------------------------------------------------
# Permission hook
# ---------------------------------------------------------------------------

class HookPermissionRequest(BaseModel):
    session_id: str
    tool_name: str
    tool_input: dict = {}
    # LIVE permission mode from the Claude PreToolUse hook's stdin — reflects
    # in-TUI Shift+Tab changes the platform can't otherwise see. Empty for
    # callers that don't carry it (Codex bridge, old gate scripts in
    # already-running sessions).
    permission_mode: str = ""


@router.post("/v1/hooks/permission")
async def hook_permission(req: HookPermissionRequest, authorization: str | None = Header(None)):
    """Permission gate for the Claude CLI PreToolUse hook, the satellite stdio
    MCP transport gate, and the satellite Codex approval bridge.

    Thin transport wrapper: it only verifies the session, then delegates to
    ``decide_tool_permission`` — the single decision authority, reused in-process
    by the local Codex app-server approval handler.
    """
    verify_session_match(authorization, req.session_id)
    return await decide_tool_permission(
        req.session_id, req.tool_name, req.tool_input,
        live_permission_mode=req.permission_mode,
    )


class HookCodexQuestionRequest(BaseModel):
    session_id: str
    questions: list = []


@router.post("/v1/hooks/codex-question")
async def hook_codex_question(
    req: HookCodexQuestionRequest, authorization: str | None = Header(None),
):
    """Question bridge for the satellite Codex ``request_user_input`` handler.

    The remote daemon holds the turn open on ``item/tool/requestUserInput``; the
    satellite POSTs the questions here over the loopback tunnel and blocks on the
    proxy's ``ask_user_question`` (surface the dashboard card, wait for the human
    answer). Returns ``{"answers": {<id>: {"answers": [...]}}}`` — the same
    authority the local Codex layer uses in-process.
    """
    verify_session_match(authorization, req.session_id)
    answers = await ask_user_question(req.session_id, req.questions)
    return {"answers": answers}


@router.post("/v1/hooks/mcp-credentials")
async def hook_mcp_credentials(authorization: str | None = Header(None)):
    """Credential broker: return an stdio MCP's secret env at spawn.

    Auth is the per-(session, mcp) CAPABILITY TOKEN ONLY — NOT the session JWT
    and NOT the master key. The agent's bash holds the session JWT + PROXY_URL +
    curl, so accepting it would let the agent harvest every MCP's (and every
    co-resident user's) secrets; the capability token is instead injected only
    into the MCP child's env and stripped by the wrapper before exec. The ``mcp``
    is derived from the token, so a token for one MCP can't fetch another's.
    Reachable directly on the proxy as well as via the satellite tunnel — the
    token binding is the boundary; the tunnel allowlist is defense-in-depth.
    """
    from core.credentials import mcp_broker
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing capability token")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    ident = mcp_broker.verify_token(parts[1])
    if ident is None:
        raise HTTPException(status_code=401, detail="Invalid or expired capability token")
    sid, mcp = ident
    bundle = mcp_broker.get(sid, mcp)
    if bundle is None:
        # Session closed / purged / proxy restarted mid-session → fail fast so
        # the spawn-time wrapper errors cleanly instead of hanging on its timeout.
        raise HTTPException(status_code=404, detail="No credentials for this session")
    return {"env": bundle.env, "http_bearer": bundle.http_bearer}


@router.post("/v1/hooks/session-files")
async def hook_session_files(authorization: str | None = Header(None)):
    """Session-file broker: return a remote session's secret FILES at start.

    The satellite calls this ONCE over the tunnel, before spawning the CLI,
    to materialize per-session secret files (SSH private keys) 0600 under its
    session-secrets dir — wiped at session close. Auth is the session-files
    CAPABILITY TOKEN ONLY (never the session JWT, never the master key); the
    token rides the start payload and never enters the spawned agent env, so
    the agent's bash cannot replay it. Only admin-paired machines ever receive
    a token — the gate is at provisioning time in ``remote_session_start``.
    """
    from core.credentials import mcp_broker
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing capability token")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    sid = mcp_broker.verify_files_token(parts[1])
    if sid is None:
        raise HTTPException(status_code=401, detail="Invalid or expired capability token")
    files = mcp_broker.get_session_files(sid)
    if files is None:
        raise HTTPException(status_code=404, detail="No files for this session")
    return {
        "files": {
            relpath: {"content_b64": f.content_b64, "mode": f.mode}
            for relpath, f in files.items()
        }
    }


def _is_interactive_session(session_id: str) -> bool:
    """True if this is the interactive PTY-backed TUI session. Its residual
    ask-tier prompts go to the CLI's OWN in-terminal permission UI + Shift+Tab
    modes (the real Claude Code UX), NOT a dashboard block-and-wait — returning
    ``"ask"`` lets Claude prompt natively. The hard denies (RBAC / path /
    catastrophe / revoke) above still return ``"deny"`` regardless of surface."""
    if not session_id:
        return False
    from core.session import interactive_session
    return interactive_session.get(session_id) is not None


def _park_interactive_on_dialog(session_id: str, tool_name: str) -> None:
    """The CLI is about to open a dialog that blocks its turn on the human
    (AskUserQuestion cards, the ExitPlanMode approval, Codex's
    request_user_input picker): park the interactive session's turn now —
    the hook call precedes the dialog deterministically, while the transcript
    line the tailer folds on can be journaled late."""
    from core.session import interactive_session
    sess = interactive_session.get(session_id)
    if sess is not None:
        sess.park_on_native_dialog(tool_name)


async def decide_tool_permission(
    session_id: str, tool_name: str, tool_input: dict | None = None,
    live_permission_mode: str = "",
) -> dict:
    """The single permission-decision authority for every execution layer.

    Pass-1 = per-role path policy (``check_tool_access``) + target-revocation +
    OAuth-credential denylist; Pass-2 = mode branching (default / acceptEdits /
    dontAsk / auto / plan) with a dashboard block-and-wait for the prompt cases
    (headless) or an explicit ``"ask"`` for the interactive TUI's native prompt.
    Returns ``{"decision": "allow"|"deny"|"ask", "reason"?: str,
    "updated_input"?: dict}``.

    ``live_permission_mode`` is the CLI-reported mode from the PreToolUse
    hook's stdin (reflects in-TUI Shift+Tab); it overrides the chat's stored
    mode for interactive sessions — see the override in the implementation.

    ``updated_input`` rides along on ALLOW and ASK when Pass-1 rewrote a
    native tool's path arg for a remote satellite (sandbox-virtual / ``~``
    form → the satellite-host path). The Claude PreToolUse hook returns it as
    ``updatedInput`` so the tool executes against the real path — on ASK the
    CLI prompts against the corrected input. Other callers (Codex approval)
    ignore the key.

    Consulted by:
      * the Claude CLI PreToolUse hook (via ``/v1/hooks/permission``),
      * the **local** Codex app-server approval handler (in-process),
      * the **remote** Codex approval bridge
        (via ``/v1/hooks/permission`` over the loopback tunnel).

    NOT consulted by the satellite stdio interceptor — that wrapper does
    path translation only (``/v1/hooks/resolve-tool-arg-paths``); per-tool
    MCP permission gating happens here for every layer.

    No transport auth here — the caller (the endpoint / interceptor) verifies the
    session. ``AskUserQuestion`` is denied after surfacing the question; in plan
    mode non-plan tools are denied (read-only planning).
    """
    _pass1_out: dict = {}
    result = await _decide_tool_permission(
        session_id, tool_name, tool_input, _pass1_out, live_permission_mode,
    )
    if result.get("decision") in ("allow", "ask") and _pass1_out.get("updated_input"):
        return {**result, "updated_input": _pass1_out["updated_input"]}
    return result


# CLI-reported live mode → platform permission mode. "bypassPermissions" is
# the CLI's own no-prompt mode (explicit user opt-in) ≡ dontAsk; the CLI's
# "auto" mode (its classifier decides) maps to acceptEdits — edits run, the
# extended/destructive/MCP tier still asks.
_CLI_LIVE_MODE_MAP = {
    "default": "default",
    "acceptEdits": "acceptEdits",
    "plan": "plan",
    "bypassPermissions": "dontAsk",
    "dontAsk": "dontAsk",
    "auto": "acceptEdits",
}


async def _decide_tool_permission(
    session_id: str, tool_name: str, tool_input: dict | None,
    _pass1_out: dict, live_permission_mode: str = "",
) -> dict:
    """Implementation of :func:`decide_tool_permission`. ``_pass1_out``
    carries Pass-1 side data (currently ``updated_input``) back to the
    wrapper without threading it through every mode-branch return."""
    tool_input = tool_input or {}
    # Codex's hook names MCP tools with a sanitized server key
    # (``mcp__meetings_mcp__direct_to``); everything below keys on the
    # manifest's server name.
    if tool_name.startswith("mcp__"):
        from services.mcp.mcp_permissions import canonical_tool_name
        tool_name = canonical_tool_name(tool_name)
    mode = get_session_mode(session_id)
    client_type = get_session_client_type(session_id)

    # Meeting agents inherit the parent chat's permission mode, and every
    # block-and-wait prompt below posts to route.queue_session_id — the
    # meeting pump's queue for participants, the session's own otherwise.
    route = routing.resolve_hook_route(session_id)
    if route.is_meeting:
        mode = get_session_mode(route.parent_session_id)
        client_type = "dashboard"  # apply dashboard mode logic

    # Interactive TUI: the hook reports the CLI's LIVE permission mode on every
    # call — the mode the human at the terminal actually chose via Shift+Tab —
    # and it overrides the chat's stored mode. Exceptions: "auto" (task
    # sessions, no human at the TUI) and "dontAsk" (an explicit dashboard
    # choice the interactive spawn can't express, so the CLI reports "default"
    # for it) keep the stored mode.
    if (live_permission_mode and mode not in ("auto", "dontAsk")
            and _is_interactive_session(session_id)):
        stored_mode = mode
        mode = _CLI_LIVE_MODE_MAP.get(live_permission_mode, mode)
        if stored_mode == "plan" and mode != "plan":
            # The human left plan mode in the TUI (approved the plan in the
            # native dialog, or Shift+Tab). Persist it: the headless
            # plan_review branch did this bookkeeping on approval, and the
            # interactive path skips that branch now (ExitPlanMode defers).
            # Without it a re-warm from the stored mode re-enters plan mode.
            set_session_mode(session_id, mode)
            try:
                _chat_id = await routing.resolve_hook_chat_id(session_id)
                if _chat_id:
                    from storage import database as task_store
                    await asyncio.to_thread(
                        task_store.update_chat, _chat_id, permission_mode=mode,
                    )
            except Exception:
                logger.debug("Hook permission: plan-mode exit bookkeeping failed",
                             exc_info=True)
            logger.info(f"Hook permission: session={session_id} left plan mode "
                        f"in the TUI → stored mode {mode}")

    # Track hook activity so settle mode knows agents are still working
    record_hook_activity(session_id)
    logger.info(f"Hook permission: session={session_id}, tool={tool_name}, mode={mode}, client={client_type}")

    # Meeting participants: a routed turn is over — deny what follows (and
    # record the routing tool itself). Ahead of every other branch: no mode,
    # tier or allow-memory may lift it.
    if route.is_meeting:
        over = routing._meeting_turn_end_backstop(session_id, route, tool_name, tool_input)
        if over is not None:
            return over

    # EnterPlanMode: always auto-approve (it's just a mode transition)
    if tool_name == "EnterPlanMode":
        return {"decision": "allow"}

    # AskUserQuestion: emit the question event for rendering in the pipe,
    # then DENY the tool so Claude Code doesn't auto-select answers in
    # non-interactive mode (which causes the model to retry 3+ times).
    # The deny reason tells the model the questions were shown to the user.
    if tool_name == "AskUserQuestion":
        # Interactive TUI with a HUMAN present: let the tool RUN — the native
        # terminal renders the question cards and the user answers inline (don't
        # deny + show a dashboard card, which is the headless -p surrogate). An
        # autonomous interactive TASK (client_type "task", no viewer) must NOT —
        # the cards would block on an answer nobody gives. It falls
        # through to the deny-and-inform below, exactly like a headless -p task.
        if _is_interactive_session(session_id) and client_type != "task":
            _park_interactive_on_dialog(session_id, tool_name)
            return {"decision": "allow"}
        queue = get_permission_queue(route.queue_session_id)
        await queue.put({
            "event_type": "question",
            "tool_name": tool_name,
            "tool_input": tool_input,
        })
        return {
            "decision": "deny",
            "reason": (
                "Questions have been displayed to the user in the chat interface. "
                "The user will reply in their next message. "
                "Do NOT re-ask these questions or call AskUserQuestion again this turn."
            ),
        }

    # Codex's question picker (interactive TUI only — the headless app-server
    # bridges request_user_input to a dashboard card without this hook): park
    # the human-driven session's turn the moment the picker opens, like
    # AskUserQuestion above. The rollout tailer persists the card. Codex runs
    # the gate deny-only, so "allow" is the no-op it expects.
    if tool_name == "request_user_input":
        if _is_interactive_session(session_id) and client_type != "task":
            _park_interactive_on_dialog(session_id, tool_name)
        return {"decision": "allow"}

    # Tools auto-approved in all modes (read-only / safe / internal)
    _READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch",
                        "ToolSearch", "Agent", "TaskGet", "TaskList",
                        "TaskOutput", "CronList",
                        "TodoWrite", "TodoRead"}
    # File edit tools auto-approved in acceptEdits
    _FILE_EDIT_TOOLS = {"Write", "Edit", "NotebookEdit"}

    # ExitPlanMode: gate with plan_review in plan mode (dashboard only),
    # auto-approve in all other modes (prevents permission prompt after
    # mode was already changed by implement/cancel actions)
    if tool_name == "ExitPlanMode":
        # Interactive TUI with a HUMAN present: the plan review IS the CLI's
        # native ExitPlanMode dialog — defer, like every other residual ask
        # (the terminal owns approvals). The dashboard plan_review card is
        # the headless -p surrogate: holding the hook on it here blocked the
        # TUI (it renders nothing while a PreToolUse hook is pending), kept
        # the turn open until the card was answered, and journaled no
        # tool_use line meanwhile — the chat looked stuck after a large
        # prompt that entered plan mode on its own (2026-09-10). The mode
        # the user picks in the dialog reaches the platform on the next
        # hook call (live mode). An autonomous interactive TASK is not a
        # dashboard client, so it falls through to the unconditional allow
        # below.
        if _is_interactive_session(session_id) and client_type != "task":
            _park_interactive_on_dialog(session_id, tool_name)
            return {"decision": "defer"}
        if mode == "plan" and client_type == "dashboard":
            # Auto-approve if user already clicked implement (session resumed after death)
            from core.events.stream_pump import _active_pumps
            pump = _active_pumps.get(session_id) if session_id else None
            if not pump:
                for p in _active_pumps.values():
                    if p.session_id == session_id:
                        pump = p
                        break
            if pump and pump.implementing_plan:
                logger.info(f"Hook permission: ExitPlanMode auto-approved (implementing_plan={pump.implementing_plan})")
                current = get_session_mode(session_id)
                if current == "plan":
                    set_session_mode(session_id, "default")
                return {"decision": "allow"}

            plan_content = (tool_input or {}).get("plan", "")
            logger.info(f"Hook permission: ExitPlanMode tool_input keys={list((tool_input or {}).keys())}, has_plan={bool(plan_content)}, plan_len={len(plan_content)}")
            request_id = str(uuid.uuid4())
            queue = get_permission_queue(route.queue_session_id)
            await queue.put({
                "event_type": "plan_review",
                "request_id": request_id,
                "plan": plan_content,
                "tool_input": tool_input or {},
            })
            logger.info(f"Hook permission: dashboard plan_review, request_id={request_id}")
            approved = await wait_for_permission(request_id, session_id, timeout=604800.0)
            logger.info(f"Hook permission: plan_review resolved, approved={approved}")
            if approved:
                current = get_session_mode(session_id)
                if current == "plan":
                    set_session_mode(session_id, "default")
                    queue2 = get_permission_queue(route.queue_session_id)
                    await queue2.put({
                        "event_type": "mode_restored",
                        "mode": "default",
                    })
                    logger.info(f"Hook permission: plan_review fallback -- mode was still plan, restored to default")
                return {"decision": "allow"}
            return {
                "decision": "deny",
                "reason": (
                    "The user wants to modify the plan. Stay in plan mode and wait "
                    "for the user's feedback in their next message. Do NOT call "
                    "ExitPlanMode again until the user approves the revised plan."
                ),
            }
        # Non-plan mode or non-dashboard: always auto-approve
        return {"decision": "allow"}

    # --- Pass 1: Path-based access control ---
    # Check file paths in tool arguments against session's security context.
    # Runs BEFORE mode-based logic. If path check denies, tool is blocked
    # regardless of permission mode (even dontAsk).
    path_decision = None  # Available for Pass 2 Bash tier logic
    security_ctx = get_session_security(session_id)
    if security_ctx is None:
        # Fail CLOSED. The SecurityContext is persisted (set at warmup,
        # reloaded on startup, cleared on close — core/session/session_state.py), so a
        # live session ALWAYS has one here, including one that survived a proxy
        # crash on a satellite. A None therefore means a dead/unknown session —
        # most importantly a CLOSED session whose self-contained 24h JWT was
        # replayed (auth/session_token.py validates only signature+expiry, no
        # liveness check). Deny it rather than fall through to Pass-2 ungated
        # (the old fail-OPEN skip, which let a replayed token bypass path policy).
        logger.warning(
            f"Hook denied (no security context): session={session_id}, tool={tool_name}"
        )
        return {
            "decision": "deny",
            "reason": "Session is no longer active. Send a new message to continue.",
        }
    # External sessions (a phone caller who is not a platform user) never
    # get a shell — the hook floor of that rule (the CLI argv and the
    # settings deny list are the other two layers). Before the path gate and
    # before every mode branch: no role, mode or allow-memory can lift it.
    # The web tools are not floored; WebFetch goes through the SSRF gate in
    # check_tool_access below like every other session.
    if getattr(security_ctx, "principal", None) == "external" and tool_name in EXTERNAL_DENIED_CLI_TOOLS:
        logger.warning(
            f"Hook denied (external session, no shell): session={session_id}, "
            f"tool={tool_name}, agent={security_ctx.agent}"
        )
        return {
            "decision": "deny",
            "reason": f"{tool_name} is not available on external routes.",
        }
    # Per-tool target revocation check. If an admin unpaired
    # the satellite while the session was running, tear down cleanly
    # via a clear tool-error instead of letting the session limp on
    # with a now-invalid cached target.
    from services.path_policy_v2 import check_target_still_valid
    revoked = await asyncio.to_thread(check_target_still_valid, security_ctx)
    if revoked:
        logger.warning(
            f"Hook target revoked: session={session_id}, "
            f"tool={tool_name}, machine_id={security_ctx.target_machine_id}, "
            f"reason={revoked}"
        )
        return {"decision": "deny", "reason": revoked}
    path_decision, _new_plan_file = check_tool_access(
        tool_name, tool_input or {}, security_ctx,
    )
    if not path_decision.allowed:
        logger.warning(
            f"Hook path denied: session={session_id}, tool={tool_name}, "
            f"role={security_ctx.role}, agent={security_ctx.agent}, "
            f"reason={path_decision.reason}"
        )
        return {"decision": "deny", "reason": path_decision.reason}
    # Remote satellites: Pass-1 may have rewritten the path arg (sandbox-
    # virtual / `~` → satellite-host). Stash for the wrapper — it attaches
    # `updated_input` to whatever ALLOW this call ultimately returns, so the
    # rewrite also applies when the allow came from a user prompt approval.
    if path_decision.updated_input is not None:
        _pass1_out["updated_input"] = path_decision.updated_input

    # --- Pass 2: Mode-based logic ---

    # MCP tools carry a manifest-declared permission tier (open / standard /
    # sensitive / critical — services/mcp/mcp_permissions.py). Resolved ONCE
    # here, ahead of every mode short-circuit below, because the tier can both
    # RELAX (open never prompts, even in plan mode) and TIGHTEN (critical
    # prompts even in dontAsk, and is denied in no-human sessions) the
    # mode-only outcome. Non-MCP tools: tier stays None, nothing changes.
    mcp_server = mcp_tool_only = ""
    mcp_tier = None
    if tool_name.startswith("mcp__"):
        from services.mcp import mcp_permissions
        _parts = tool_name.split("__", 2)
        mcp_server = _parts[1] if len(_parts) >= 2 else ""
        mcp_tool_only = _parts[2] if len(_parts) >= 3 else ""
        mcp_tier = mcp_permissions.resolve_tool_tier(mcp_server, mcp_tool_only)

    # Helper: check Bash tier against current mode.
    # Returns {"decision": "allow"} if auto-approved, None if should fall through to prompt.
    def _bash_tier_auto_approve():
        if not path_decision or not path_decision.permission_tier:
            return None  # no tier info -- fall through to prompt
        # Destructive bash (rm / dd / shred / truncate / find -delete / …)
        # prompts EVEN in acceptEdits — "allow edits, but destructive asks".
        # (dontAsk/auto already returned allow at the blanket check above, so
        # this only affects default + acceptEdits.) Tracked separately from the
        # tier so `rm x && curl …` (tier extended) isn't masked. See _check_bash.
        if getattr(path_decision, "destructive", False):
            return None
        tier = path_decision.permission_tier
        # Unknown commands carry tier "ask" → fall through to the prompt (like
        # "extended"): prompt in default/acceptEdits, allowed in dontAsk/auto.
        if tier == "read":
            # Read-tier bash: auto-approve in default, acceptEdits, dontAsk
            return {"decision": "allow"}
        if tier == "edit":
            # "auto" is the task permission mode — treat it like "dontAsk"
            # so a continued (re-warmed) task doesn't prompt on edits.
            if mode in ("acceptEdits", "dontAsk", "auto"):
                return {"decision": "allow"}
            return None  # default mode: prompt
        if tier == "admin":
            if mode in ("dontAsk", "auto"):
                return {"decision": "allow"}
            return None  # default/acceptEdits: prompt
        return None

    # Plan mode: allow read-only tools + plan file writes/edits, deny rest
    # (ExitPlanMode/EnterPlanMode already handled above)
    if mode == "plan":
        if tool_name in _READ_ONLY_TOOLS:
            return {"decision": "allow"}

        # Open-tier MCP tools (pure reads, dashboard display, recoverable
        # memory writes) support planning — the one tier plan mode admits.
        if mcp_tier == "open":
            return {"decision": "allow"}

        # Shell read-tier in plan mode: safe read-only commands (Bash / Monitor /
        # PowerShell — all classified by _check_bash / _check_powershell).
        if tool_name in _SHELL_COMMAND_TOOLS and path_decision and path_decision.permission_tier == "read":
            return {"decision": "allow"}

        # Allow writing/editing plan files in ~/.claude/plans/. Normalize
        # separators: a Windows-satellite session sends the host-absolute
        # path with backslashes (C:\Users\...\.claude\plans\x.md) — without
        # this the plan write is denied as a generic plan-mode write.
        if tool_name in ("Write", "Edit"):
            file_path = ((tool_input or {}).get("file_path", "") or "").replace("\\", "/")
            if "/.claude/plans/" in file_path:
                return {"decision": "allow"}

        return {"decision": "deny"}

    # Dashboard sessions: permission behavior depends on mode
    if client_type == "dashboard":
        # "auto" is the task permission mode (set at task creation). A continued
        # task is a dashboard client, so without this it would fall through to
        # the prompt path even though the UI shows "Don't Ask". Treat auto ≡ dontAsk.
        # Critical-tier MCP tools are the one exception: they prompt in EVERY
        # mode when a human is present (a dashboard client is one), so they
        # fall through to the prompt path below instead of auto-allowing.
        if mode in ("dontAsk", "auto"):
            if mcp_tier != "critical":
                return {"decision": "allow"}

        # Shell tier-based handling (before generic tool checks). Bash / Monitor /
        # PowerShell all carry a tier + destructive flag from the command gate, so
        # the same tier→mode auto-approve applies (read→default, edit→acceptEdits,
        # ask/extended→prompt, destructive→prompt even in acceptEdits).
        if tool_name in _SHELL_COMMAND_TOOLS:
            result = _bash_tier_auto_approve()
            if result:
                return result
            # Fall through to permission prompt

        elif mode == "acceptEdits":
            # Auto-approve read-only + file edit tools
            # Prompt for MCP tools and destructive tools
            if tool_name in _READ_ONLY_TOOLS or tool_name in _FILE_EDIT_TOOLS:
                return {"decision": "allow"}
            # Fall through to prompt

        elif tool_name in _READ_ONLY_TOOLS:
            # "default" mode: only auto-allow read-only tools
            return {"decision": "allow"}

        # Device-local MCP tools (computer / browser / app control): the owner
        # ALREADY consented at the machine-grant level, and a per-click
        # permission prompt would make a mouse/keyboard
        # MCP unusable. Auto-approve a granted device MCP's tools instead of
        # prompting — but NEVER a blanket mcp__* allow: only the specific server
        # whose device_capability is CURRENTLY granted on this session's target.
        # The grant is read live from the SecurityContext (refreshed on revoke),
        # so revoking mid-session makes the next call prompt again. A device MCP
        # can only have loaded (and thus emit a tool call) if it passed the
        # config-build gate, so a matching grant here means it's legitimately in
        # use. (Non-dashboard sessions never reach this branch — they allow at
        # the fallthrough below; only dashboard sessions carry target grants.)
        if tool_name.startswith("mcp__"):  # security_ctx is non-None past the Pass-1 gate
            from services.mcp import mcp_permissions, mcp_registry
            # Critical-tier tools skip EVERY auto-approve below (device grant,
            # session allow-memory, tier table) — they exist to prompt per
            # call, so they drop straight to the prompt path.
            if mcp_tier != "critical":
                cap = mcp_registry.device_capability_for_server(mcp_server)
                granted = getattr(security_ctx, "target_device_grants", None) or set()
                if cap and cap in granted:
                    # High-risk app-connector tools (e.g. execute_blender_code = raw
                    # RCE inside the app, bypassing the bash-tier system) are EXCLUDED
                    # from the blanket device auto-approve: they still prompt even
                    # though the capability is granted.
                    if mcp_registry.is_high_risk_device_tool(mcp_server, mcp_tool_only):
                        logger.info(
                            f"Hook permission: device tool {tool_name} is high-risk "
                            f"(capability '{cap}' granted) — prompting instead of auto-approving"
                        )
                    else:
                        logger.info(
                            f"Hook permission: auto-approving device tool {tool_name} "
                            f"(capability '{cap}' granted on machine "
                            f"{security_ctx.target_machine_id[:8] if security_ctx.target_machine_id else '?'})"
                        )
                        return {"decision": "allow"}

                # Session allow-memory: the user already clicked Allow for this
                # exact tool this session — one Allow covers its later calls
                # instead of raising a fresh card per call. High-risk device
                # tools never enter the set (see the prompt resolution below),
                # so they keep prompting.
                if is_session_tool_allowed(session_id, tool_name):
                    return {"decision": "allow"}

                # Manifest permission tier: open never prompts; standard is
                # silent in acceptEdits. High-risk device tools are exempt
                # from the tier auto-approve — their per-call prompt pinning
                # outranks any manifest declaration (a community manifest
                # must not be able to un-pin them).
                if (
                    mcp_permissions.tier_decision(mcp_tier, mode) == "allow"
                    and not mcp_registry.is_high_risk_device_tool(mcp_server, mcp_tool_only)
                ):
                    return {"decision": "allow"}

        # Interactive TUI with a human at the keyboard: DEFER — the platform
        # has no opinion on the residual ask-tier, so the CLI's own
        # permission engine (the live Shift+Tab mode) governs, exactly like
        # Claude Code outside the platform. Consequences, accepted and
        # documented (PERMISSIONS.md + UPGRADING): trusted-dir Write/Edit
        # run without a prompt in default mode (native CLI behavior,
        # verified 2.1.215/changelog-reviewed to 2.1.243), and the old
        # generic "OtoDock '<mode>' mode" prompt spam is gone. Hard denies
        # (Pass-1) and the carve-outs below are unaffected; stored dontAsk
        # still hard-allowed above (an explicit silence choice the TUI
        # cannot express). Operator decision 2026-07-26.
        if _is_interactive_session(session_id) and client_type != "task":
            # Carve-outs keep the platform "ask" — it feeds the CLI's ONE
            # native prompt (never a second dialog), so this is pure floor:
            # critical-tier MCP tools (contract: prompt in EVERY mode),
            # high-risk device tools (pinned per-call even when the
            # capability is granted), and destructive Bash (must never ride
            # a permissive CLI mode).
            _high_risk = False
            if tool_name.startswith("mcp__"):
                from services.mcp import mcp_registry as _reg
                _high_risk = _reg.is_high_risk_device_tool(mcp_server, mcp_tool_only)
            if (
                mcp_tier == "critical"
                or _high_risk
                or bool(getattr(path_decision, "destructive", False))
            ):
                return {
                    "decision": "ask",
                    "reason": "OtoDock: this action always needs your approval",
                }
            # A satellite path rewrite only rides "allow"/"ask" — never lose
            # it to silence (the tool would run against the sandbox-virtual
            # path and fail). Write/Edit run promptless on allow, which
            # matches their trusted-dir native behavior anyway.
            if _pass1_out.get("updated_input") is not None:
                return {"decision": "allow"}
            return {"decision": "defer"}
        if _is_interactive_session(session_id):
            # An interactive session with NO human (client_type "task")
            # normally never reaches here (tasks run permission_mode "auto",
            # which allowed above) — if one does, ask via the TUI rather
            # than auto-approving every ask-tier tool unattended.
            return {
                "decision": "ask",
                "reason": f"OtoDock '{mode}' mode: this action needs your approval",
            }

        # Block and ask user via dashboard UI
        request_id = str(uuid.uuid4())
        queue = get_permission_queue(route.queue_session_id)
        prompt_data = {
            "event_type": "permission_prompt",
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        if route.meeting_agent:
            prompt_data["meeting_agent"] = route.meeting_agent
        await queue.put(prompt_data)
        logger.info(f"Hook permission: {'meeting ' + route.meeting_agent + ' ' if route.meeting_agent else ''}dashboard blocking for {tool_name}, request_id={request_id}")
        approved = await wait_for_permission(request_id, session_id, timeout=604800.0)
        logger.info(f"Hook permission: dashboard resolved {tool_name}, approved={approved}")
        if approved and tool_name.startswith("mcp__"):
            # Feed the session allow-memory (checked above before prompting).
            # High-risk device tools and critical-tier tools re-prompt per
            # call by design — never remembered. `mcp_registry` is bound by
            # the mcp__ branch above.
            if mcp_tier != "critical" and not mcp_registry.is_high_risk_device_tool(
                mcp_server, mcp_tool_only
            ):
                remember_session_tool_allow(session_id, tool_name)
        return {"decision": "allow" if approved else "deny"}

    # Non-dashboard sessions (tasks, phone): auto-allow everything — EXCEPT
    # critical-tier MCP tools, which require a human answer that these
    # sessions cannot provide. Deny-and-inform instead of hanging.
    if mcp_tier == "critical":
        return {
            "decision": "deny",
            "reason": (
                f"{tool_name} requires interactive user approval and this "
                "session runs unattended. Ask the user to run it from a chat."
            ),
        }
    return {"decision": "allow"}


async def ask_user_question(
    session_id: str, questions: list, timeout: float = 604800.0,
) -> dict:
    """Surface a Codex ``request_user_input`` question set to the dashboard and
    block for the human answer. The single question authority, reused in-process
    by the local Codex layer and over the tunnel by ``/v1/hooks/codex-question``.

    Mirrors the permission block-and-wait: enqueue a ``question_prompt`` on the
    session's permission queue (the pump surfaces the card + the "needs your
    input" ephemeral), then wait for the answer keyed by the VERBATIM question id.
    Returns the answers MAP ``{<id>: {"answers": [...]}}`` (``{}`` on timeout /
    abort, so the held turn unwinds cleanly).
    """
    # Belt-and-braces: only interactive dashboard chats have a human to answer.
    # The config flag already keeps request_user_input off for autonomous runs;
    # decline empty here too so a task/phone/meeting session never hangs a turn.
    from core.execution_layer import UNATTENDED_CLIENT_TYPES
    if get_session_client_type(session_id) in UNATTENDED_CLIENT_TYPES:
        return {}
    route = routing.resolve_hook_route(session_id)
    request_id = str(uuid.uuid4())
    queue = get_permission_queue(route.queue_session_id)
    await queue.put({
        "event_type": "question_prompt",
        "request_id": request_id,
        "tool_name": "request_user_input",
        "tool_input": {"questions": questions},
    })
    logger.info(f"Codex question: dashboard blocking, request_id={request_id}")
    answers = await wait_for_question(request_id, session_id, timeout=timeout)
    logger.info(f"Codex question: resolved request_id={request_id} "
                f"({len(answers)} answered)")
    return answers
