"""The permission authority: the permission, codex-question, mcp-credentials and
session-files hooks, ``decide_tool_permission`` and ``ask_user_question``.

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import contextvars
import dataclasses
import functools
import hmac
import json
import logging
import re as _re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import psycopg
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import config
from api.hooks import routing
from api.sessions.sessions import _session_payload, verify_session_match_async
from auth.path_policy import (
    TIER_ADMIN,
    TIER_EDIT,
    TIER_READ,
    PathDecision,
    SecurityContext,
    check_tool_access,
)
from core.events import tool_roles
from core.execution_layer import (
    ASK_MODE,
    AUTO_REVIEW_MODE,
    DONT_ASK_MODE,
    JUDGE_MODE,
    PERMISSION_MODES,
    PLAN_MODE,
    UNATTENDED_MODE,
    current_mode,
)
from core.session import session_events, session_kind
from core.session.session_state import (
    PROMPT_WAIT_S,
    get_permission_queue,
    get_session_client_type,
    get_session_mode,
    get_session_security,
    is_session_tool_allowed,
    note_live_word,
    record_hook_activity,
    recorded_session_mode,
    remember_session_tool_allow,
    review_paused,
    session_token_refusal,
    set_session_mode,
    took_no_answer,
    wait_for_permission,
    wait_for_question,
    watch_prompt,
)
from ws import wire_events as wire

if TYPE_CHECKING:
    from core.execution_layer import ExecutionLayer

logger = logging.getLogger("otodock")
router = APIRouter()

#: The tool ROLES auto-approved in every mode (read-only / safe / internal):
#: reads, globs, searches, the web tools, tool discovery, a subagent spawn,
#: the task list's reads, the checklist. Everything else asks in Ask; a
#: file edit by path runs in Auto, where a patch, a delete, a skill, a
#: workflow, the task list's edits go to the engine's own reviewer.
_AUTO_APPROVED_ROLES = frozenset({
    tool_roles.READ, tool_roles.GLOB, tool_roles.SEARCH, tool_roles.WEB_FETCH,
    tool_roles.WEB_SEARCH, tool_roles.DISCOVERY, tool_roles.SUBAGENT,
    tool_roles.TASK_READ, tool_roles.CHECKLIST,
})


# ---------------------------------------------------------------------------
# Permission hook
# ---------------------------------------------------------------------------

class HookPermissionRequest(BaseModel):
    session_id: str
    tool_name: str
    tool_input: dict = {}
    # The engine's LIVE mode word from the PreToolUse hook's stdin (Claude
    # Code's own words; Codex's ``default`` / ``bypassPermissions``): the
    # terminal follows a Shift+Tab by it, and it says whether the engine's
    # own reviewer runs (``engine_reviews``). Empty for a call no hook
    # raised (the Codex bridge) and from a gate script older than the field.
    permission_mode: str = ""
    # When the gate sent this call, by the clock of the host the CLI runs on
    # (a satellite's own): a terminal parked on a dialog compares the
    # transcript lines' times against it. None from the Codex bridge.
    hook_sent_at: float | None = None


# The ``hook_sent_at`` of the gate call being decided, for the park below
# (``decide_tool_permission`` runs in the receiver's task).
_hook_sent_at: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "hook_sent_at", default=None,
)


# A decision that waits on a person streams a whitespace keepalive this
# often and the JSON last (JSON readers skip leading whitespace): the stream
# is never idle on its way through a satellite's tunnel, and a caller that
# went away is noticed at the next write, which releases its wait.
_KEEPALIVE_S = 30.0
# A decision ready within this answers as a plain JSON response.
_QUICK_S = 1.0
# A prompt ends this long before its session token expires: an answer after
# that would let the one tool run and refuse every hook that follows.
_TOKEN_MARGIN_S = 60


def _prompt_wait_s(authorization: str | None, session_id: str) -> float:
    """How long a prompt of this caller may wait: ``PROMPT_WAIT_S``, cut to
    the life its session token has left (the master key has no expiry). The
    route verified the token already; this only reads its expiry."""
    try:
        payload = _session_payload(authorization, session_id)
    except HTTPException:
        payload = None
    exp = (payload or {}).get("exp")
    if not isinstance(exp, (int, float)):
        return PROMPT_WAIT_S
    return max(1.0, min(PROMPT_WAIT_S, exp - time.time() - _TOKEN_MARGIN_S))


async def _wait_on_person(queue: asyncio.Queue, request_id: str, wait):
    """Await a prompt's answer (``wait``: its ``wait_for_permission`` or
    ``wait_for_question``). A wait that ends with no answer retires the
    prompt's card: an item on the queue the prompt was raised on, which the
    pump frees its slot for and forwards as ``prompt_retired``. The caller
    gone (the hook's stream dropped, the decision cancelled) is the case a
    retried call raises its own card for; a wait that ran out or that an
    abort or a session close released is the other."""
    watch_prompt(request_id)
    gone = False
    try:
        return await wait
    except asyncio.CancelledError:
        gone = True
        raise
    finally:
        if took_no_answer(request_id) or gone:
            item: dict[str, object] = {"event_type": wire.ITEM_PROMPT_RETIRED, "request_id": request_id}
            if gone:
                item["caller_gone"] = True
            queue.put_nowait(item)


async def _answer_with_keepalive(decision, deny: dict):
    """The decision as plain JSON when it is quick; otherwise a streamed
    answer of whitespace keepalives and the JSON last. The stream always
    ends with one JSON object (``deny`` when the decision raised) and never
    after it; a disconnect cancels the decision, so its wait is released."""
    task = asyncio.ensure_future(decision)
    try:
        return await asyncio.wait_for(asyncio.shield(task), _QUICK_S)
    except asyncio.TimeoutError:
        pass
    except BaseException:
        task.cancel()  # the request went away before the stream began
        raise

    async def body():
        try:
            while True:
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), _KEEPALIVE_S)
                    break
                except asyncio.TimeoutError:
                    yield b" "
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hook prompt: the decision failed while streaming")
            result = deny
        finally:
            if not task.done():
                task.cancel()
        yield json.dumps(result).encode()

    return StreamingResponse(body(), media_type="application/json")


@router.post("/v1/hooks/permission")
async def hook_permission(req: HookPermissionRequest, authorization: str | None = Header(None)):
    """Permission gate for the Claude CLI PreToolUse hook, the satellite stdio
    MCP transport gate, and the satellite Codex approval bridge.

    Thin transport wrapper: it only verifies the session, then delegates to
    ``decide_tool_permission`` — the single decision authority, reused in-process
    by the local Codex app-server approval handler. A decision that waits on a
    person is streamed with keepalives (``_answer_with_keepalive``).
    """
    await verify_session_match_async(authorization, req.session_id)
    _hook_sent_at.set(req.hook_sent_at)
    return await _answer_with_keepalive(
        decide_tool_permission(
            req.session_id, req.tool_name, req.tool_input,
            live_permission_mode=req.permission_mode,
            wait_s=_prompt_wait_s(authorization, req.session_id),
        ),
        {"decision": "deny", "reason": "OtoDock could not decide this tool call"},
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
    await verify_session_match_async(authorization, req.session_id)
    from core.session.session_manager import engine_layer_for_session
    layer = engine_layer_for_session(req.session_id)
    names = layer.capabilities.behaviour.tools.get(tool_roles.QUESTION, ()) if layer else ()

    async def answer() -> dict:
        return {"answers": await ask_user_question(
            req.session_id, req.questions, tool_name=names[0] if names else "",
            timeout=_prompt_wait_s(authorization, req.session_id))}

    return await _answer_with_keepalive(answer(), {"answers": {}})


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
    return {"env": bundle.env}


class HookCliCredentialRequest(BaseModel):
    session_id: str
    host: str
    # What the command names, for a narrowed GitHub reach
    # (``auth/github_reach``): git's repository path, or gh's arguments, the
    # working directory's remote addresses and gh's GH_REPO and GH_HOST. An
    # older helper sends none.
    path: str = Field(default="", max_length=1024)
    command: list[str] = Field(default_factory=list, max_length=256)
    remotes: list[str] = Field(default_factory=list, max_length=32)
    gh_repo: str = Field(default="", max_length=512)
    gh_host: str = Field(default="", max_length=255)


@router.post("/v1/hooks/cli-credential")
async def hook_cli_credential(
    req: HookCliCredentialRequest, authorization: str | None = Header(None),
):
    """The credential a session's command-line tools get for ``host`` right
    now: the git credential helper and the ``gh`` wrapper
    (``proxy/hooks/credential_helper.py``) ask here at each command, so the
    token is in no environment and no file and the current token file is
    read every time. Auth is the session's COMMAND-LINE token
    (``OTO_CLI_TOKEN``, ``session_token.attach_cli_token``): the shell holds
    it, no MCP child does, so an MCP holding the session token cannot read
    the account's token here. The holder is judged as a session token's is,
    its session against the body's. A session token is refused (403) unless
    it was minted before 1.9: a session started before the upgrade has no
    command-line token, and keeps its git and gh until its token expires.
    The master key is refused outright (it names no session). Answers: the
    username git presents, the token and the names the wrapper sets; 404
    when the session holds no credential for the host (not connected, or
    the store emptied by a restart and not re-provisioned); 409 when the
    account needs a reconnect. Reachable directly and over the satellite
    tunnel.
    """
    from core.credentials import mcp_broker, mcp_gateway
    await _authorize_cli_bearer(authorization, req.session_id)
    cred = mcp_broker.cli_credential(req.session_id, req.host)
    if cred is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {req.host.strip().lower()[:253]} account is connected for this session",
        )
    refusal = _reach_refusal(cred, req)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    res = await asyncio.to_thread(mcp_gateway.resolve_cli_credential, cred)
    if isinstance(res, mcp_gateway.Refusal):
        raise HTTPException(status_code=409, detail=res.detail)
    return {"username": res.username, "password": res.secret, "env": list(res.env_names)}


def _reach_refusal(cred, req: HookCliCredentialRequest) -> str:
    """A narrowed GitHub reach judges what the command names: gh by its
    arguments, git by the repository path it asks for."""
    from auth import github_reach
    reach = github_reach.Reach.from_dict(cred.reach)
    if reach is None or not reach.narrows:
        return ""
    if req.command:
        return github_reach.judge_gh(reach, [str(a)[:1024] for a in req.command], list(req.remotes),
                                     gh_repo=req.gh_repo, gh_host=req.gh_host)
    return github_reach.judge_git(reach, req.path)


# The sessions whose pre-1.9 session token was taken on the command-line
# route, logged once each.
_CLI_GRACE_LOGGED: set[str] = set()


async def _authorize_cli_bearer(authorization: str | None, session_id: str) -> None:
    """The command-line route's bearer: the session's command-line token,
    or a session token minted before 1.9 (its session has none). 401 for no
    or an unreadable bearer and for a dead session or holder, 403 for the
    master key, a token of another session, and a session token minted by
    this release."""
    from auth import token_holder
    from auth.external_endpoints import EXTERNAL_BLOCKED_DETAIL
    from auth.session_token import validate_cli_token, validate_session_token
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    if config.is_master_key(token):
        raise HTTPException(status_code=403, detail="A session token is required")
    claims = validate_cli_token(token)
    if claims is None:
        if validate_session_token(token, legacy=False) is not None:
            raise HTTPException(
                status_code=403, detail="This session's command-line token is required")
        if validate_session_token(token) is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        await verify_session_match_async(authorization, session_id)
        if session_id not in _CLI_GRACE_LOGGED:
            _CLI_GRACE_LOGGED.add(session_id)
            logger.info("cli-credential: session %s started before 1.9 presents its "
                        "session token (taken until it expires)", session_id[:8])
        return
    sid = claims.get("sid")
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")
    if not isinstance(sid, str) or not sid or not hmac.compare_digest(
            sid.encode("utf-8", "surrogatepass"), session_id.encode("utf-8", "surrogatepass")):
        raise HTTPException(status_code=403, detail="Session token does not match request session_id")
    if session_token_refusal(claims) or not await token_holder.holder_ok(claims):
        raise HTTPException(status_code=401, detail="Invalid API key")
    # An external caller (a phone caller who is no platform user) reaches
    # only its tools' endpoints, as the middleware rules for its session
    # token; this route is none of them.
    if claims.get("ext") and not claims.get("user_sub"):
        raise HTTPException(status_code=403, detail=EXTERNAL_BLOCKED_DETAIL)


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
        sess.park_on_native_dialog(tool_name, sent_at=_hook_sent_at.get())


async def decide_tool_permission(
    session_id: str, tool_name: str, tool_input: dict | None = None,
    live_permission_mode: str = "", *, wait_s: float = PROMPT_WAIT_S,
) -> dict:
    """The single permission-decision authority for every execution layer.

    Pass-1 = per-role path policy (``check_tool_access``) + target-revocation +
    OAuth-credential denylist; Pass-2 = mode branching (ask / autoReview /
    dontAsk / the unattended ``auto`` / plan / judge) with a dashboard
    block-and-wait for the prompt cases (headless) or an explicit ``"ask"``
    for the interactive TUI's native prompt. Returns ``{"decision":
    "allow"|"deny"|"ask"|"defer", "reason"?: str, "updated_input"?: dict}``;
    ``defer`` is no decision: the engine's own permission engine decides
    (the terminal's residual, and Auto's review set while the engine's
    reviewer runs).

    ``live_permission_mode`` is the engine's mode word from the PreToolUse
    hook's stdin: an interactive terminal's chat follows a Shift+Tab by it
    (``_follow_terminal``), and in Auto it says whether the engine's own
    reviewer judges the call (``ExecutionLayer.engine_reviews``).
    ``wait_s`` bounds a prompt's wait on a person (``PROMPT_WAIT_S``, cut to
    the caller's token life by the route).

    ``updated_input`` rides along on ALLOW, ASK and DEFER when Pass-1
    rewrote a native tool's path arg for a remote satellite (sandbox-virtual
    / ``~`` form → the satellite-host path). The Claude PreToolUse hook
    returns it as ``updatedInput`` so the tool executes against the real
    path — on ASK the CLI prompts against the corrected input, on DEFER (no
    decision) its classifier judges it (probed on 2.1.296). Other callers
    (Codex approval) ignore the key.

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
        wait_s=wait_s,
    )
    if result.get("decision") in ("allow", "ask", "defer") and _pass1_out.get("updated_input"):
        return {**result, "updated_input": _pass1_out["updated_input"]}
    return result


# The stored chat modes a meeting participant honours when its host session
# is gone: the dashboard's modes and ``auto``, which a task, trigger or phone
# chat stores (its unattended meeting keeps running unasked).
_MEETING_STORED_MODES = frozenset({*PERMISSION_MODES, UNATTENDED_MODE})


async def _meeting_mode(route: routing.HookRoute) -> str:
    """A meeting participant's permission mode: its host chat's session's
    recorded mode; when that session is gone, the host chat's stored mode;
    Ask when neither is known. An unknown host never reads as the
    unattended ``auto`` (it did: a participant then wrote without asking)."""
    mode = recorded_session_mode(route.parent_session_id) if route.parent_session_id else ""
    if mode:
        return current_mode(mode)
    stored = ""
    if route.chat_id:
        from storage import database as task_store
        from storage.pg import run_db_fast
        try:
            chat = await run_db_fast(task_store.get_chat, route.chat_id)
        except psycopg.Error:
            logger.warning("Hook permission: the meeting's chat %s could not be read",
                           route.chat_id[:8])
            chat = None
        stored = current_mode(str((chat or {}).get("permission_mode") or ""))
    return stored if stored in _MEETING_STORED_MODES else ASK_MODE


# A terminal's live mode word (Claude Code's own) → the chat's mode it
# moves to on a Shift+Tab. The CLI's auto and acceptEdits are Auto (its
# classifier decides, or its native prompt asks, for what Auto does not
# allow); bypassPermissions is its own no-prompt mode, Don't Ask.
_CLI_LIVE_MODE_MAP = {
    "default": ASK_MODE,
    "acceptEdits": AUTO_REVIEW_MODE,
    "plan": PLAN_MODE,
    "bypassPermissions": DONT_ASK_MODE,
    "dontAsk": DONT_ASK_MODE,
    "auto": AUTO_REVIEW_MODE,
}


# A person's Allow on the hook's card for a shell command or a patch, kept for
# the bridge's request for that same call: Codex asks again when the call
# crosses its sandbox (network, a write outside the workspace), and the person
# already said yes. Session -> {call key: expiry}; one use each.
_ALLOWED_CALLS: dict[str, dict[str, float]] = {}
_ALLOWED_CALL_S = 120.0


def _call_key(role: str, tool_name: str, tool_input: dict) -> str:
    """What makes two requests for one shell command or patch the same: the
    command text (the bridge's unwrapped ``_codex_command``), or the files."""
    if role == tool_roles.SHELL:
        text = tool_input.get("_codex_command") or tool_roles.payload_value(tool_name, tool_input)
        return "cmd:" + str(text).strip() if text else ""
    if role == tool_roles.WRITE:
        files = tool_input.get("_codex_paths")
        if not isinstance(files, list):
            from auth.path_policy import _patch_files
            files = [path for _kind, path, _checks in _patch_files(tool_input)]
        return "files:" + "\n".join(sorted(str(f) for f in files)) if files else ""
    return ""


def _remember_allowed_call(session_id: str, key: str) -> None:
    now = time.monotonic()
    for sid in [sid for sid, calls in _ALLOWED_CALLS.items()
                if sid != session_id and max(calls.values(), default=0.0) <= now]:
        del _ALLOWED_CALLS[sid]
    calls = {k: t for k, t in _ALLOWED_CALLS.get(session_id, {}).items() if t > now}
    calls[key] = now + _ALLOWED_CALL_S
    _ALLOWED_CALLS[session_id] = calls


def _take_allowed_call(session_id: str, key: str) -> bool:
    calls = _ALLOWED_CALLS.get(session_id) or {}
    expiry = calls.pop(key, 0.0)
    if not calls:
        _ALLOWED_CALLS.pop(session_id, None)
    return expiry > time.monotonic()


async def _follow_terminal(session_id: str, mode: str, live: str) -> str:
    """The chat's mode after a terminal's hook call: when the live word
    differs from the last one the terminal reported (the first compared with
    the word the terminal was spawned with), the chat moves to the mode that
    word maps to: the session, the row once, a frame to the chat's viewers.
    A Don't Ask terminal reports ``default`` from its spawn: not a change; a
    pick made on another surface after the spawn is not a change either."""
    new = _CLI_LIVE_MODE_MAP.get(live)
    if not note_live_word(session_id, live) or new is None or new == mode:
        return mode
    set_session_mode(session_id, new)
    try:
        chat_id = await routing.resolve_hook_chat_id(session_id)
        if chat_id:
            from core.events import chat_writer
            from core.session.session_state import broadcast_chat_frame
            from storage import database as task_store
            await chat_writer.submit(chat_id, functools.partial(
                task_store.update_chat, chat_id, permission_mode=new), label="terminal_mode")
            broadcast_chat_frame(chat_id, {"type": wire.MODE_CHANGED, "mode": new, "chat_id": chat_id})
    except (ImportError, psycopg.Error):
        logger.debug("Hook permission: the terminal's mode bookkeeping failed", exc_info=True)
    logger.info("Hook permission: session=%s terminal moved to %s (live %s)", session_id, new, live)
    return new


async def _decide_tool_permission(
    session_id: str, tool_name: str, tool_input: dict | None,
    _pass1_out: dict, live_permission_mode: str = "", *, wait_s: float = PROMPT_WAIT_S,
) -> dict:
    """Implementation of :func:`decide_tool_permission`. ``_pass1_out``
    carries Pass-1 side data (currently ``updated_input``) back to the
    wrapper without threading it through every mode-branch return."""
    call = await _ToolCall.resolve(session_id, tool_name, tool_input, _pass1_out,
                                   live_permission_mode, wait_s=wait_s)
    for stage in _STAGES:
        decision = await stage(call)
        if decision is not None:
            return decision
    return call.unattended()


@dataclass
class _ToolCall:
    """One tool call before the permission authority: what the session and
    the engine say about it, and what the stages resolve on the way (the
    security context, the path gate's decision, an MCP tool's tier)."""

    session_id: str
    tool_name: str
    tool_input: dict
    _pass1_out: dict
    wait_s: float
    layer: "ExecutionLayer | None"
    role: str
    auto_approved: bool
    file_edit: bool
    mode: str
    client_type: str
    route: routing.HookRoute
    # Whether the engine's own reviewer judges this call (Auto's review
    # set): asked of the engine per call, from the live word it reported.
    reviews: bool = False
    # Whether a person drives the session (a meeting participant: its host).
    attended: bool = True
    # The engine's live mode word with the call ("" for a call no hook
    # raised: the Codex bridge's requests).
    live: str = ""
    security_ctx: SecurityContext | None = field(init=False, repr=False)
    path_decision: PathDecision | None = field(init=False, repr=False)
    mcp_server: str = field(init=False, repr=False)
    mcp_tool_only: str = field(init=False, repr=False)
    mcp_tier: str | None = field(init=False, repr=False)

    @classmethod
    async def resolve(cls, session_id: str, tool_name: str, tool_input: dict | None,
                      _pass1_out: dict, live_permission_mode: str, *,
                      wait_s: float) -> "_ToolCall":
        """The call's facts: the platform's name and role for the tool, the
        session's mode and client (a meeting participant's are its chat's,
        an interactive terminal's live mode overrides), the hook activity
        recorded."""
        tool_input = tool_input or {}
        # The engine behind the session translates its native names into the
        # platform's (Codex's hook names MCP tools with a sanitized server key,
        # ``mcp__meetings_mcp__direct_to``; everything below keys on the
        # manifest's server name and on the tool's ROLE — core/events/tool_roles).
        from core.session.session_manager import engine_layer_for_session
        layer = engine_layer_for_session(session_id)
        if layer is not None:
            tool_name = layer.canonical_tool_name(session_id, tool_name)
        role = tool_roles.role_of(tool_name)
        payload = tool_roles.payload_of(tool_name)
        # The roles that run in every mode without a prompt: reads, searches,
        # the web tools, tool discovery, a subagent spawn (its tools are gated
        # on their own), the task list's reads, the checklist.
        auto_approved = role in _AUTO_APPROVED_ROLES
        # A file edit by path (auto-approved in acceptEdits; a patch is not, and
        # a delete never is).
        file_edit = role == tool_roles.WRITE and payload == tool_roles.FILE_PATH
        mode = current_mode(get_session_mode(session_id))
        client_type = get_session_client_type(session_id)
        attended = session_kind.attended(client_type)

        # Meeting agents inherit the parent chat's permission mode, and every
        # block-and-wait prompt below posts to route.queue_session_id — the
        # meeting pump's queue for participants, the session's own otherwise.
        # A participant is attended when its host is (a task's meeting has
        # nobody to answer a card).
        route = routing.resolve_hook_route(session_id)
        if route.is_meeting:
            mode = await _meeting_mode(route)
            attended = session_kind.attended(
                get_session_client_type(route.parent_session_id) if route.parent_session_id else "")
            client_type = session_kind.DASHBOARD.name  # apply dashboard mode logic

        # Interactive terminal driven by a person: its chat follows the mode
        # the person picks in the TUI (Shift+Tab), reported on every call.
        # The person's own terminal only, on an engine whose terminal reports
        # its mode: a task's or a check's stored word never follows it.
        if (live_permission_mode and mode in PERMISSION_MODES
                and client_type == session_kind.DASHBOARD.name and not route.is_meeting
                and _is_interactive_session(session_id)
                and layer is not None and layer.terminal_reports_mode()):
            mode = await _follow_terminal(session_id, mode, live_permission_mode)

        reviews = (mode == AUTO_REVIEW_MODE and layer is not None
                   and layer.engine_reviews(session_id, live_permission_mode)
                   and not review_paused(session_id))

        # Track hook activity so settle mode knows agents are still working
        record_hook_activity(session_id)
        logger.info("Hook permission: session=%s, tool=%s, mode=%s, client=%s", session_id, tool_name, mode, client_type)
        return cls(session_id, tool_name, tool_input, _pass1_out, wait_s, layer, role,
                   auto_approved, file_edit, mode, client_type, route, reviews, attended,
                   live_permission_mode)

    async def no_context(self) -> dict | None:
        """A session with no security context is refused."""
        # Fail CLOSED, before anything is enqueued or allowed. The SecurityContext
        # is persisted (set at warmup, reloaded on startup, cleared on close —
        # core/session/session_state.py), so a live session ALWAYS has one here,
        # including one that survived a proxy crash on a satellite. A None
        # therefore means a dead/unknown session — most importantly a CLOSED
        # session whose self-contained 24h JWT was replayed
        # (auth/session_token.py validates only signature+expiry, no liveness
        # check). Deny it rather than fall through ungated (the old fail-OPEN
        # skip let a replayed token bypass path policy).
        # The steps after this one run only on a context: a None denies here.
        self.security_ctx = get_session_security(self.session_id)
        if self.security_ctx is None:
            logger.warning(
                "Hook denied (no security context): session=%s, tool=%s",
                self.session_id, self.tool_name
            )
            return {
                "decision": "deny",
                "reason": "Session is no longer active. Send a new message to continue.",
            }
        return None

    async def attached_ssh_hosts(self) -> dict | None:
        """A local manager's or editor's shell call carries the agent's SSH
        hosts as the instances name them now (the command gate's opening),
        for this call only: an instance removed mid-session stops admitting.
        Never answers; a failed lookup leaves the opening shut and says so
        (``ssh_hosts=None``)."""
        from core.sandbox import session_config_dir
        from storage.pg import run_db_fast
        ctx = cast("SecurityContext", self.security_ctx)
        if self.role != tool_roles.SHELL or not session_config_dir.wants_attached_ssh_hosts(
                ctx, tool_roles.payload_value(self.tool_name, self.tool_input)):
            return None
        try:
            hosts = await run_db_fast(session_config_dir.attached_ssh_hosts, ctx.agent)
        except Exception:  # noqa: BLE001 — a failed lookup must shut the opening, never fail the hook
            logger.warning("Hook: the SSH hosts of agent %s could not be read; the call "
                           "is refused", ctx.agent, exc_info=True)
            hosts = None
        self.security_ctx = dataclasses.replace(ctx, ssh_hosts=hosts)
        return None

    async def meeting_backstop(self) -> dict | None:
        """A meeting participant's routed turn is over."""
        # Meeting participants: a routed turn is over — deny what follows (and
        # record the routing tool itself). Ahead of every other branch: no mode,
        # tier or allow-memory may lift it.
        if self.route.is_meeting:
            over = routing._meeting_turn_end_backstop(
                self.session_id, self.route, self.tool_name, self.tool_input)
            if over is not None:
                return over
        return None

    async def plan_enter(self) -> dict | None:
        """Entering plan mode."""
        # Entering plan mode: always auto-approve (it's just a mode transition)
        if self.role == tool_roles.PLAN_ENTER:
            return {"decision": "allow"}
        return None

    async def question(self) -> dict | None:
        """The engine's question tool."""
        # The engine's question tool, in this order:
        #  * an interactive TUI with a HUMAN present: let the tool RUN — the
        #    native terminal renders the question cards / picker and the human
        #    answers inline (don't deny + show a dashboard card, which is the
        #    headless surrogate); park the turn the moment the dialog opens,
        #    since the hook call precedes it deterministically;
        #  * an engine whose question tool holds the turn open for the
        #    platform's answers (Codex's request_user_input →
        #    ask_user_question, which declines unattended callers): allow;
        #  * otherwise — Claude headless, and an autonomous interactive TASK on
        #    either engine (no viewer; a picker would block on an answer nobody
        #    gives) — emit the question event for the chat and DENY the tool so
        #    the model doesn't auto-select answers and retry; the reason says
        #    the questions reached the user.
        if self.role == tool_roles.QUESTION:
            interactive = _is_interactive_session(self.session_id)
            if interactive and session_kind.attended(self.client_type):
                _park_interactive_on_dialog(self.session_id, self.tool_name)
                return {"decision": "allow"}
            holds_turn = (self.layer is not None
                          and self.layer.capabilities_for(self.session_id)
                          .behaviour.question_tool_holds_turn)
            if holds_turn and not interactive:
                return {"decision": "allow"}
            queue = get_permission_queue(self.route.queue_session_id)
            await queue.put({
                "event_type": wire.ITEM_QUESTION,
                "tool_name": self.tool_name,
                "tool_input": self.tool_input,
            })
            return {
                "decision": "deny",
                "reason": (
                    "Questions have been displayed to the user in the chat interface. "
                    "The user will reply in their next message. "
                    f"Do NOT re-ask these questions or call {self.tool_name} again this turn."
                ),
            }
        return None

    async def plan_exit(self) -> dict | None:
        """Leaving plan mode."""
        # Leaving plan mode: gate with plan_review in plan mode (dashboard only),
        # auto-approve in all other modes (prevents permission prompt after
        # mode was already changed by implement/cancel actions)
        if self.role == tool_roles.PLAN_EXIT:
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
            if _is_interactive_session(self.session_id) and session_kind.attended(self.client_type):
                _park_interactive_on_dialog(self.session_id, self.tool_name)
                return {"decision": "defer"}
            if self.mode == PLAN_MODE and self.client_type == session_kind.DASHBOARD.name:
                # Auto-approve if user already clicked implement (session resumed after death)
                from core.events.stream_pump import _active_pumps
                pump = _active_pumps.get(self.session_id) if self.session_id else None
                if not pump:
                    for p in _active_pumps.values():
                        if p.session_id == self.session_id:
                            pump = p
                            break
                if pump and pump.implementing_plan:
                    logger.info("Hook permission: ExitPlanMode auto-approved (implementing_plan=%s)", pump.implementing_plan)
                    current = get_session_mode(self.session_id)
                    if current == PLAN_MODE:
                        set_session_mode(self.session_id, ASK_MODE)
                    return {"decision": "allow"}

                plan_content = (self.tool_input or {}).get("plan", "")
                logger.info("Hook permission: ExitPlanMode tool_input keys=%s, has_plan=%s, plan_len=%s", list((self.tool_input or {}).keys()), bool(plan_content), len(plan_content))
                request_id = str(uuid.uuid4())
                queue = get_permission_queue(self.route.queue_session_id)
                await queue.put({
                    "event_type": wire.ITEM_PLAN_REVIEW,
                    "request_id": request_id,
                    "plan": plan_content,
                    "tool_input": self.tool_input or {},
                })
                logger.info("Hook permission: dashboard plan_review, request_id=%s", request_id)
                approved = await _wait_on_person(
                    queue, request_id,
                    wait_for_permission(request_id, self.session_id, timeout=self.wait_s))
                logger.info("Hook permission: plan_review resolved, approved=%s", approved)
                if approved:
                    current = get_session_mode(self.session_id)
                    if current == PLAN_MODE:
                        set_session_mode(self.session_id, ASK_MODE)
                        queue2 = get_permission_queue(self.route.queue_session_id)
                        await queue2.put({
                            "event_type": wire.ITEM_MODE_RESTORED,
                            "mode": ASK_MODE,
                        })
                        logger.info("Hook permission: plan_review fallback -- mode was still plan, restored to Ask")
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
        return None

    async def path_gate(self) -> dict | None:
        """Pass 1: the external callers' shell floor, a revoked target, the
        path policy (its decision kept for Pass 2)."""
        # Check file paths in tool arguments against session's security context.
        # Runs BEFORE mode-based logic. If path check denies, tool is blocked
        # regardless of permission mode (even dontAsk).
        self.path_decision = None  # Available for Pass 2 shell tier logic
        # External sessions (a phone caller who is not a platform user) never
        # get a shell — the hook floor of that rule, by ROLE (the CLI argv and
        # the settings deny list, the other two layers, carry the engine's own
        # shell names). Before the path gate and before every mode branch: no
        # role, mode or allow-memory can lift it. The web tools are not floored;
        # a URL fetch goes through the SSRF gate in check_tool_access below like
        # every other session.
        if (getattr(self.security_ctx, "principal", None) == "external"
                and self.role == tool_roles.SHELL):
            logger.warning(
                "Hook denied (external session, no shell): session=%s, "
                "tool=%s, agent=%s",
                self.session_id, self.tool_name, cast("SecurityContext", self.security_ctx).agent
            )
            return {
                "decision": "deny",
                "reason": f"{self.tool_name} is not available on external routes.",
            }
        # Per-tool target revocation check. If an admin unpaired
        # the satellite while the session was running, tear down cleanly
        # via a clear tool-error instead of letting the session limp on
        # with a now-invalid cached target.
        from services.path_policy_v2 import check_target_still_valid
        revoked = await asyncio.to_thread(check_target_still_valid, self.security_ctx)
        if revoked:
            logger.warning(
                "Hook target revoked: session=%s, "
                "tool=%s, machine_id=%s, "
                "reason=%s", self.session_id, self.tool_name,
                cast("SecurityContext", self.security_ctx).placement.machine_id, revoked
            )
            return {"decision": "deny", "reason": revoked}
        self.path_decision, _new_plan_file = check_tool_access(
            self.tool_name, self.tool_input or {}, cast("SecurityContext", self.security_ctx),
        )
        if not self.path_decision.allowed:
            logger.warning(
                "Hook path denied: session=%s, tool=%s, "
                "role=%s, agent=%s, "
                "reason=%s",
                self.session_id, self.tool_name, cast("SecurityContext", self.security_ctx).role, cast("SecurityContext", self.security_ctx).agent,
                self.path_decision.reason
            )
            return {"decision": "deny", "reason": self.path_decision.reason}
        # Remote satellites: Pass-1 may have rewritten the path arg (sandbox-
        # virtual / `~` → satellite-host). Stash for the wrapper — it attaches
        # `updated_input` to whatever ALLOW this call ultimately returns, so the
        # rewrite also applies when the allow came from a user prompt approval.
        if self.path_decision.updated_input is not None:
            self._pass1_out["updated_input"] = self.path_decision.updated_input
        return None

    async def resolve_mcp_tier(self) -> dict | None:
        """An MCP tool's permission tier (never answers)."""
        # MCP tools carry a manifest-declared permission tier (open / standard /
        # sensitive / critical — services/mcp/mcp_permissions.py). Resolved ONCE
        # here, ahead of every mode short-circuit below, because the tier can both
        # RELAX (open never prompts, even in plan mode) and TIGHTEN (critical
        # prompts even in dontAsk, and is denied in no-human sessions) the
        # mode-only outcome. Non-MCP tools: tier stays None, nothing changes.
        # critical is kept for the platform's own tools that change who sees or
        # does what inside OtoDock: a manifest outside mcps/custom/ loads it as
        # sensitive (mcp_manifest_parse._bounded_tiers).
        self.mcp_server = self.mcp_tool_only = ""
        self.mcp_tier = None
        if self.tool_name.startswith("mcp__"):
            from services.mcp import mcp_permissions
            _parts = self.tool_name.split("__", 2)
            self.mcp_server = _parts[1] if len(_parts) >= 2 else ""
            self.mcp_tool_only = _parts[2] if len(_parts) >= 3 else ""
            self.mcp_tier = mcp_permissions.resolve_tool_tier(self.mcp_server, self.mcp_tool_only)
        return None

    def bash_tier_auto_approve(self) -> dict | None:
        """The shell tier against the current mode: ``{"decision": "allow"}``
        when it is auto-approved, None when it falls through to the prompt."""
        if not self.path_decision or not self.path_decision.permission_tier:
            return None  # no tier info -- fall through to prompt
        # Destructive bash (rm / dd / shred / truncate / find -delete / …)
        # never runs on a tier: it asks in Ask and goes to the engine's
        # reviewer in Auto (Don't Ask and the unattended word allowed it
        # before this). Tracked apart from the tier so `rm x && curl …`
        # (tier extended) isn't masked. See _check_bash.
        if getattr(self.path_decision, "destructive", False):
            return None
        tier = self.path_decision.permission_tier
        # Unknown commands carry tier "ask" → fall through (like "extended"):
        # the card in Ask, the reviewer in Auto, run in Don't Ask.
        if tier == TIER_READ:
            return {"decision": "allow"}
        if tier == TIER_EDIT:
            # The unattended word is a task's: treated as Don't Ask, so a
            # continued (re-warmed) task doesn't prompt on edits.
            if self.mode in (AUTO_REVIEW_MODE, DONT_ASK_MODE, UNATTENDED_MODE):
                return {"decision": "allow"}
            return None  # Ask: the card
        if tier == TIER_ADMIN:
            if self.mode in (DONT_ASK_MODE, UNATTENDED_MODE):
                return {"decision": "allow"}
            return None
        return None

    def auto_allows(self) -> bool:
        """Auto's allow set, exactly what Accept Edits allowed before it: the
        read-only roles, a file edit by path, the shell's read and edit tiers
        (never a destructive command), the open and standard MCP tiers, a
        granted device MCP and a tool the person allowed earlier in the
        session (never a critical one)."""
        if self.auto_approved or self.file_edit:
            return True
        if self.role == tool_roles.SHELL:
            return self.bash_tier_auto_approve() is not None
        if not self.tool_name.startswith("mcp__") or self.mcp_tier == "critical":
            return False
        from services.mcp import mcp_permissions, mcp_registry
        if mcp_permissions.tier_decision(cast("str", self.mcp_tier), AUTO_REVIEW_MODE) == "allow":
            return True
        cap = mcp_registry.device_capability_for_server(self.mcp_server)
        granted = cast("SecurityContext", self.security_ctx).placement.device_grants or set()
        return bool(cap and cap in granted) or is_session_tool_allowed(self.session_id, self.tool_name)

    async def plan_mode(self) -> dict | None:
        """Plan mode: the read-only tools and the plan files."""
        # Plan mode: allow read-only tools + plan file writes/edits, deny rest
        # (ExitPlanMode/EnterPlanMode already handled above)
        if self.mode == PLAN_MODE:
            if self.auto_approved:
                return {"decision": "allow"}

            # Open-tier MCP tools (pure reads, dashboard display, recoverable
            # memory writes) support planning — the one tier plan mode admits.
            if self.mcp_tier == "open":
                return {"decision": "allow"}

            # Shell read-tier in plan mode: safe read-only commands (every shell
            # dialect is classified by _check_bash / _check_powershell).
            if (self.role == tool_roles.SHELL and self.path_decision
                    and self.path_decision.permission_tier == "read"):
                return {"decision": "allow"}

            # Allow writing/editing plan files in ~/.claude/plans/. Normalize
            # separators: a Windows-satellite session sends the host-absolute
            # path with backslashes (C:\Users\...\.claude\plans\x.md) — without
            # this the plan write is denied as a generic plan-mode write.
            if self.file_edit:
                file_path = tool_roles.payload_value(
                    self.tool_name, self.tool_input).replace("\\", "/")
                if "/.claude/plans/" in file_path:
                    return {"decision": "allow"}

            return {"decision": "deny"}
        return None

    async def judge_mode(self) -> dict | None:
        """The judge profile: a judge reads, it does not write."""
        # The judge profile (CHECKS.md): ``git`` is edit tier in the shell
        # classifier as a family, but a judge needs its read subcommands
        # (status, diff, log, …) — one plain command, no pipe or redirect.
        _JUDGE_READ_GIT_RE = _re.compile(
            r"^\s*git\s+(?:(?:-C|--git-dir|--work-tree)\s+\S+\s+|--no-pager\s+)*"
            r"(?:status|diff|log|show|blame|rev-parse|ls-files|ls-tree|branch|remote|describe|"
            r"shortlog|grep|cat-file|stash\s+list|tag\s+-l|tag\s+--list|worktree\s+list)\b"
            r"[^|;&<>`$]*$")
        # The judge profile (CHECKS.md): a check's judge session reads and
        # never writes — the read tools and the read tier of the shell run, the
        # MCP tools the check listed run below the critical tier (the session
        # has no other MCPs), everything that writes is refused with a reason
        # the model can act on. Enforced here for every placement, a machine
        # over the tunnel included; the kernel mounts say the same locally.
        if self.mode == JUDGE_MODE:
            if self.auto_approved:
                return {"decision": "allow"}
            if self.role == tool_roles.SHELL and self.path_decision and (
                    self.path_decision.permission_tier == "read"
                    and not getattr(self.path_decision, "destructive", False)):
                return {"decision": "allow"}
            if (self.role == tool_roles.SHELL and self.path_decision and self.path_decision.allowed
                    and not getattr(self.path_decision, "destructive", False)
                    and _JUDGE_READ_GIT_RE.match(
                        tool_roles.payload_value(self.tool_name, self.tool_input))):
                return {"decision": "allow"}
            if (self.tool_name.startswith("mcp__")
                    and self.mcp_tier in ("open", "standard", "sensitive")):
                return {"decision": "allow"}
            return {"decision": "deny",
                    "reason": "a judge reads, it does not write — report the finding instead"}
        return None

    async def dashboard(self) -> dict | None:
        """A dashboard session: its mode decides, else the person answers a
        prompt. Always answers for a dashboard client."""
        # Dashboard sessions: permission behavior depends on mode
        if self.client_type == session_kind.DASHBOARD.name:
            # The unattended word is a task's (set at task creation). A continued
            # task is a dashboard client, so without this it would fall through to
            # the prompt path even though the UI shows "Don't Ask". Treat it as Don't Ask.
            # Critical-tier MCP tools are the one exception: they prompt in EVERY
            # mode when a human is present (a dashboard client is one), so they
            # fall through to the prompt path below instead of auto-allowing.
            if self.mode in (DONT_ASK_MODE, UNATTENDED_MODE):
                if self.mcp_tier != "critical":
                    return {"decision": "allow"}

            # Auto: its allow set runs; the rest is the engine's own reviewer's
            # (no decision here: the engine judges it), or the person's card
            # when the engine runs no reviewer for this call (an older
            # satellite, a meeting participant, a change not landed yet). A
            # terminal's residual is the CLI's own engine, except a destructive
            # shell call no reviewer judges (a terminal Shift+Tabbed out of its
            # auto mode, or spawned in acceptEdits by an older satellite): it
            # keeps the terminal's carve-out below.
            if self.mode == AUTO_REVIEW_MODE and self.mcp_tier != "critical":
                if self.auto_allows():
                    return {"decision": "allow"}
                if self.reviews:
                    # A satellite path rewrite rides the defer (the CLI
                    # applies it and its classifier judges the corrected call).
                    return {"decision": "defer"}
                if (_is_interactive_session(self.session_id)
                        and session_kind.attended(self.client_type)
                        and not getattr(self.path_decision, "destructive", False)):
                    # As the terminal's residual below: a rewrite rides allow.
                    if self._pass1_out.get("updated_input") is not None:
                        return {"decision": "allow"}
                    return {"decision": "defer"}
                if not self.attended:
                    return self.refused_without_review()

            # Shell tier-based handling (before generic tool checks). Every shell
            # tool carries a tier + destructive flag from the command gate, so
            # the same tier→mode auto-approve applies (read→Ask, edit→Auto,
            # ask/extended→prompt, destructive→prompt in Ask).
            if self.role == tool_roles.SHELL:
                result = self.bash_tier_auto_approve()
                if result:
                    return result
                # Fall through to permission prompt

            elif self.mode == AUTO_REVIEW_MODE:
                # Auto with no reviewer: the read-only roles and a file edit
                # by path ran above; patches, deletes and the rest ask.
                if self.auto_approved or self.file_edit:
                    return {"decision": "allow"}
                # Fall through to prompt

            elif self.auto_approved:
                # Ask: only auto-allow the read-only roles
                return {"decision": "allow"}

            # The bridge's request for a shell command or patch the person just
            # allowed through the hook's card (Codex asks again when the call
            # crosses its sandbox): that Allow answers it, once.
            if not self.live and self.role in (tool_roles.SHELL, tool_roles.WRITE):
                key = _call_key(self.role, self.tool_name, self.tool_input)
                if key and _take_allowed_call(self.session_id, key):
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
            if self.tool_name.startswith("mcp__"):  # security_ctx is non-None past the Pass-1 gate
                from services.mcp import mcp_permissions, mcp_registry
                # Critical-tier tools skip EVERY auto-approve below (device grant,
                # session allow-memory, tier table): they exist to prompt per
                # call, so they drop straight to the prompt path.
                if self.mcp_tier != "critical":
                    cap = mcp_registry.device_capability_for_server(self.mcp_server)
                    granted = cast("SecurityContext", self.security_ctx).placement.device_grants or set()
                    if cap and cap in granted:
                        logger.info(
                            "Hook permission: auto-approving device tool %s "
                            "(capability '%s' granted on machine "
                            "%s)",
                            self.tool_name, cap, cast("SecurityContext", self.security_ctx).placement.machine_id[:8] or '?'
                        )
                        return {"decision": "allow"}

                    # Session allow-memory: the user already clicked Allow for this
                    # exact tool this session — one Allow covers its later calls
                    # instead of raising a fresh card per call. Critical tools
                    # never enter the set (see the prompt resolution below), so
                    # they keep prompting.
                    if is_session_tool_allowed(self.session_id, self.tool_name):
                        return {"decision": "allow"}

                    # Manifest permission tier: open never prompts; standard is
                    # silent in Auto (an mcp__ tool has its tier, above).
                    if mcp_permissions.tier_decision(cast("str", self.mcp_tier), self.mode) == "allow":
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
            if _is_interactive_session(self.session_id) and session_kind.attended(self.client_type):
                # A critical tool in Auto on an engine whose terminal cannot
                # ask from the hook (Codex: the hook only denies, and in Auto
                # its own reviewer would answer the terminal's prompt) never
                # reaches a person: refused, with the way to run it.
                if (self.mcp_tier == "critical" and self.mode == AUTO_REVIEW_MODE
                        and self.layer is not None and not self.layer.hook_can_ask()):
                    return {
                        "decision": "deny",
                        "reason": ("OtoDock: this action needs a person's approval; a Codex "
                                   "terminal in Auto cannot ask for it. Switch the chat to Ask, "
                                   "or run it from the chat."),
                    }
                # Carve-outs keep the platform "ask" — it feeds the CLI's ONE
                # native prompt (never a second dialog), so this is pure floor:
                # critical-tier MCP tools (contract: prompt in EVERY mode) and
                # destructive Bash no reviewer judges (Ask, and Auto when the
                # CLI is not in its auto mode; in its auto mode the CLI's own
                # reviewer judges it: an "ask" would force a prompt there).
                if (self.mcp_tier == "critical"
                        or (not self.reviews
                            and bool(getattr(self.path_decision, "destructive", False)))):
                    return {
                        "decision": "ask",
                        "reason": "OtoDock: this action always needs your approval",
                    }
                # A satellite path rewrite only rides "allow"/"ask" — never lose
                # it to silence (the tool would run against the sandbox-virtual
                # path and fail). Write/Edit run promptless on allow, which
                # matches their trusted-dir native behavior anyway.
                if self._pass1_out.get("updated_input") is not None:
                    return {"decision": "allow"}
                return {"decision": "defer"}
            if _is_interactive_session(self.session_id):
                # An interactive session with NO human (client_type "task")
                # normally never reaches here (tasks run permission_mode "auto",
                # which allowed above) — if one does, ask via the TUI rather
                # than auto-approving every ask-tier tool unattended.
                return {
                    "decision": "ask",
                    "reason": f"OtoDock '{self.mode}' mode: this action needs your approval",
                }

            # Block and ask user via dashboard UI
            request_id = str(uuid.uuid4())
            queue = get_permission_queue(self.route.queue_session_id)
            prompt_data = {
                "event_type": wire.ITEM_PERMISSION_PROMPT,
                "request_id": request_id,
                "tool_name": self.tool_name,
                "tool_input": self.tool_input,
            }
            if self.route.meeting_agent:
                prompt_data["meeting_agent"] = self.route.meeting_agent
            await queue.put(prompt_data)
            logger.info("Hook permission: %sdashboard blocking for %s, request_id=%s", 'meeting ' + self.route.meeting_agent + ' ' if self.route.meeting_agent else '', self.tool_name, request_id)
            approved = await _wait_on_person(
                queue, request_id,
                wait_for_permission(request_id, self.session_id, timeout=self.wait_s))
            logger.info("Hook permission: dashboard resolved %s, approved=%s",
                        self.tool_name, approved)
            if approved and self.tool_name.startswith("mcp__"):
                # Feed the session allow-memory (checked above before prompting).
                # Critical-tier tools re-prompt per call by design, never
                # remembered.
                if self.mcp_tier != "critical":
                    remember_session_tool_allow(self.session_id, self.tool_name)
            elif approved and self.live and self.role in (tool_roles.SHELL, tool_roles.WRITE):
                key = _call_key(self.role, self.tool_name, self.tool_input)
                if key:
                    _remember_allowed_call(self.session_id, key)
            return {"decision": "allow" if approved else "deny"}
        return None

    def unattended(self) -> dict:
        """Every other session (tasks, phone): allowed, unless the tool needs
        a person to answer, or the task runs in Auto and the call is its
        engine reviewer's."""
        # Non-dashboard sessions (tasks, phone): auto-allow everything — EXCEPT
        # critical-tier MCP tools, which require a human answer that these
        # sessions cannot provide. Deny-and-inform instead of hanging.
        if self.mcp_tier == "critical":
            return {
                "decision": "deny",
                "reason": (
                    f"{self.tool_name} requires interactive user approval and this "
                    "session runs unattended. Ask the user to run it from a chat."
                ),
            }
        if self.mode == AUTO_REVIEW_MODE and not self.auto_allows():
            if self.reviews:
                return {"decision": "defer"}
            return self.refused_without_review()
        return {"decision": "allow"}

    def refused_without_review(self) -> dict:
        """Auto's review set where nobody can answer a card and the engine
        runs no reviewer for this call: refused with the reason."""
        return {
            "decision": "deny",
            "reason": (
                f"{self.tool_name} needs a review in Auto and this run cannot get "
                "one. Report what you wanted to do so a person can run it."
            ),
        }


#: The stages of a tool call's permission decision, in order; the first one
#: that answers decides.
_STAGES = (
    _ToolCall.no_context,
    _ToolCall.meeting_backstop,
    _ToolCall.plan_enter,
    _ToolCall.question,
    _ToolCall.plan_exit,
    # --- Pass 1: Path-based access control ---
    _ToolCall.attached_ssh_hosts,
    _ToolCall.path_gate,
    # --- Pass 2: Mode-based logic ---
    _ToolCall.resolve_mcp_tier,
    _ToolCall.plan_mode,
    _ToolCall.judge_mode,
    _ToolCall.dashboard,
)


async def ask_user_question(
    session_id: str, questions: list, timeout: float = PROMPT_WAIT_S, *,
    tool_name: str = "",
) -> dict:
    """Surface a Codex ``request_user_input`` question set to the dashboard and
    block for the human answer. The single question authority, reused in-process
    by the local Codex layer and over the tunnel by ``/v1/hooks/codex-question``.

    Mirrors the permission block-and-wait: enqueue a ``question_prompt`` on the
    session's permission queue (the pump surfaces the card + the "needs your
    input" ephemeral), then wait for the answer keyed by the VERBATIM question id.
    Returns the answers MAP ``{<id>: {"answers": [...]}}`` (``{}`` on timeout /
    abort, so the held turn unwinds cleanly). ``tool_name`` is the engine's
    question tool as the card records it (the caller's descriptor says).
    """
    # Belt-and-braces: only interactive dashboard chats have a human to answer.
    # The config flag already keeps request_user_input off for autonomous runs;
    # decline empty here too so a task/phone/meeting session never hangs a turn.
    if not session_kind.attended(get_session_client_type(session_id)):
        return {}
    route = routing.resolve_hook_route(session_id)
    request_id = str(uuid.uuid4())
    queue = get_permission_queue(route.queue_session_id)
    await queue.put({
        "event_type": wire.ITEM_QUESTION_PROMPT,
        "request_id": request_id,
        "tool_name": tool_name,
        "tool_input": {"questions": questions},
    })
    logger.info("Codex question: dashboard blocking, request_id=%s", request_id)
    answers = await _wait_on_person(
        queue, request_id, wait_for_question(request_id, session_id, timeout=timeout))
    logger.info("Codex question: resolved request_id=%s "
                "(%s answered)", request_id, len(answers))
    return answers


async def _decide(*args, **kwargs) -> dict:
    return await decide_tool_permission(*args, **kwargs)


async def _ask(*args, **kwargs) -> dict:
    return await ask_user_question(*args, **kwargs)


# Core reaches the authority through these two doors (it does not import the
# hook surface); each looks its function up at call time, so a patch of
# either on this module reaches every in-process caller.
session_events.register_permission_authority(_decide, _ask)

