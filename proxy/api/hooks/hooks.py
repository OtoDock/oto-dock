"""Hook callback endpoints -- permission gate, image/url/file display, document
preview, tool results, permission responses, and the location bridge.

Path forms accepted by file-tools-style hooks (``/v1/hooks/file``,
``/v1/hooks/document-preview``, ``/v1/hooks/file-written``):

  * **Agents-relative** (canonical for Docker MCPs):
      ``personal-assistant/users/<user>/workspace/foo.docx``
  * **Sandbox-virtual** (canonical for stdio MCPs running with ``OTO_*`` env):
      ``/users/<user>/workspace/foo.docx``
  * **Satellite-absolute** (for remote sessions):
      ``{satellite_agents_dir}/personal-assistant/users/<user>/workspace/foo.docx``

Host-absolute paths (post-/agents/ mount) are NOT a valid form post-v2 — they
broke remote-satellite sessions where the host path doesn't exist on the
platform side. ``_classify_and_pull`` resolves whichever form arrives
(sandbox-virtual, satellite-absolute, or agent-relative).
"""

import asyncio
import logging
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
from storage import database as task_store
from auth.path_policy import (
    SecurityContext,
    check_tool_access,
    enforce_agent_tree_rbac,
    check_host_path_access,
    _SHELL_COMMAND_TOOLS,
)
from api.sessions.sessions import verify_api_key, verify_session_match
from core.session.session_state import (
    _sessions,
    _dashboard_notify_queues,
    get_session_mode,
    set_session_mode,
    remember_session_tool_allow,
    is_session_tool_allowed,
    get_session_client_type,
    record_hook_activity,
    get_permission_queue,
    wait_for_permission,
    wait_for_question,
    resolve_permission,
    get_permission_request_session,
    get_session_security,
    push_pump_event,
    wait_for_location,
    get_meeting_session_info,
    get_subagent_registry,
    mark_subagent_done,
)

if TYPE_CHECKING:
    from services.path_policy_v2 import PathResolution

logger = logging.getLogger("claude-proxy")
router = APIRouter()


# ---------------------------------------------------------------------------
# Sandbox path resolution — used by Docker MCPs (file-tools, etc.)
# ---------------------------------------------------------------------------

class ResolvePathRequest(BaseModel):
    session_id: str
    path: str
    # True when the caller resolves a WRITE target (output/save path). Write
    # targets tolerate a missing file: on remote sessions the pull miss
    # resolves to the platform creation path instead of falling through to
    # the lexical translator (or 404 for satellite-host paths).
    writing: bool = False


@router.post("/v1/hooks/resolve-path")
async def resolve_path(req: ResolvePathRequest, authorization: str | None = Header(None)):
    """Translate a Docker MCP path arg to a host-absolute path.

    Docker MCPs (file-tools, camoufox) call this to resolve paths that
    agents send from inside their bwrap sandbox AND
    absolute satellite-host paths the LLM may pass on remote-paired
    sessions.

    Returns ``{host_path, agents_relative}`` so Docker MCPs that mount
    ``agents/`` as ``/agents/`` resolve correctly:

      * Sandbox-virtual paths (``/users/...``, ``/workspace/...``,
        ``/knowledge/...``, ``/config/...``):
          - Local session → translated to ``AGENTS_DIR/{agent}/...``.
          - Remote session → lazy-pulled from satellite into a
            per-session platform cache at ``AGENTS_DIR/.remote-host-cache/``.
            The hook returns the cache path; subsequent writes flush
            back via ``/v1/hooks/file-written``.
      * Satellite-host absolute paths (remote only):
          - Policy-gated via ``path_policy_v2`` (home-only / full-FS
            per ``remote_machines.allow_full_fs``).
          - Lazy-pulled into ``AGENTS_DIR/.remote-host-cache/`` with a
            metadata sidecar so write-back targets the original
            absolute path on the satellite.
    """
    verify_session_match(authorization, req.session_id)

    ctx = get_session_security(req.session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Session not found")

    raw = req.path
    agent_dir = config.AGENTS_DIR / ctx.agent

    from core.remote import remote_file_flow
    is_remote = remote_file_flow.is_remote_session(req.session_id)

    # Classify the input via path_policy_v2 so satellite-host
    # paths take the lazy-pull branch on remote sessions.
    from services import path_policy_v2 as _v2
    policy_ctx = _v2.context_from_security(ctx)
    resolution = _v2.resolve_path_for_session(policy_ctx, raw, writing=req.writing)

    # path_policy_v2 admits any in-tree path incl. /users/OTHER — re-impose
    # cross-user + role RBAC for agent_tree resolutions (local + remote). A
    # satellite-host path is already home/full-FS-gated.
    _rbac = enforce_agent_tree_rbac(resolution, ctx, writing=req.writing)
    if not _rbac.allowed:
        raise HTTPException(status_code=403, detail=_rbac.reason or "access denied")

    # Diagnostic: pinpoint why a satellite-host path (e.g. a Desktop
    # file) does/doesn't take the lazy-pull branch. Shows is_remote + policy
    # verdict + path_ref kind + home_dir/full_fs so a repro is decisive.
    logger.info(
        "resolve-path: path=%r is_remote=%s allowed=%s ref_kind=%s "
        "home_dir=%r full_fs=%s err=%r",
        raw, is_remote, resolution.allowed,
        getattr(resolution.path_ref, "kind", None),
        getattr(policy_ctx, "home_dir", ""),
        getattr(policy_ctx, "allow_full_fs", None),
        resolution.error,
    )

    # Satellite-host paths (remote only) → policy-gate + lazy-pull into
    # the dedicated host-cache so the Docker MCP can read.
    if (
        is_remote
        and resolution.allowed
        and resolution.path_ref is not None
        and resolution.path_ref.kind == "satellite_host"
    ):
        cached = await remote_file_flow.pull_through_host_path(
            req.session_id, resolution.access_path,
        )
        if cached is None:
            if req.writing:
                # A write target that doesn't exist yet: there is no cache
                # file to hand back, and creating NEW files at arbitrary
                # satellite-host paths isn't supported for Docker MCPs —
                # only in-place edits of existing files push back.
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "cannot create a new file outside the synced agent "
                        f"tree on the satellite: {resolution.access_path} — "
                        "write into the workspace instead"
                    ),
                )
            raise HTTPException(
                status_code=404,
                detail=f"file not reachable on satellite: {resolution.access_path}",
            )
        host_path = str(cached)
        # The cache lives under AGENTS_DIR/.remote-host-cache/ so the
        # Docker MCP's /agents mount resolves it. Returning the
        # agents-relative form lets file-tools' existing
        # `MOUNT_AGENTS_DIR + agents_rel` logic work unchanged — no
        # MCP-side code change needed for satellite-host paths.
        agents_rel = _to_agents_relative(host_path)
        return {"host_path": host_path, "agents_relative": agents_rel}

    # ANY policy denial → 403 with the resolver's reason. This must precede both
    # the remote pull-through and the local _sandbox_to_host translate below: a
    # rejected sandbox-virtual path (OAuth creds, .claude/.codex config, .ssh)
    # carries path_ref=None so enforce_agent_tree_rbac can't catch it — without
    # this it would fall through and hand the caller the protected file. Mirrors
    # resolve-tool-arg-paths, which never resolves a denied item.
    if not resolution.allowed:
        raise HTTPException(status_code=403, detail=resolution.error or "access denied")

    # Remote-session branch: file lives on the satellite. Lazy-pull into
    # the per-session platform cache and return the cache path so the
    # Docker MCP reads it locally. Cache lives under AGENTS_DIR/.remote-host-cache/
    # so the existing /agents mount inside the Docker MCP resolves it
    # without extra mounts.
    if is_remote:
        # Prefer the resolver's agent-tree slug. A satellite-host-absolute path
        # that sits inside the synced tree folds back to agent_tree with
        # path_ref.value = the slug (e.g. users/<u>/workspace/foo.docx); using
        # the raw path here would hand pull_through the whole
        # "C:/Users/.../foo.docx" string as the agent-relative key and miss the
        # file (the in-tree-absolute "file not found" bug). For a sandbox-virtual
        # raw, path_ref.value == raw.lstrip("/") — identical behavior.
        if (
            resolution.allowed
            and resolution.path_ref is not None
            and resolution.path_ref.kind == "agent_tree"
        ):
            rel = resolution.path_ref.value
        else:
            rel = raw.lstrip("/")
        cached = await remote_file_flow.pull_through(req.session_id, rel)
        if cached is not None and cached.is_file():
            host_path = str(cached)
            agents_rel = _to_agents_relative(host_path)
            return {"host_path": host_path, "agents_relative": agents_rel}
        # WRITE target that doesn't exist on the satellite yet (new output
        # file): resolve to the platform creation path — the Docker MCP
        # writes there and /v1/hooks/file-written pushes it back. Without
        # this, a fresh output_path fell through to the lexical translator
        # below, which mangles satellite-absolute forms. The pull above ran
        # first on purpose: an EXISTING file must be re-materialized so a
        # read-modify-write tool edits the satellite's current bytes.
        if (
            req.writing
            and resolution.allowed
            and resolution.path_ref is not None
            and resolution.path_ref.kind == "agent_tree"
        ):
            from core.remote.file_sync import is_canonical_rel_path
            if is_canonical_rel_path(rel):
                create_host = (agent_dir / rel).resolve()
                try:
                    create_host.relative_to(agent_dir.resolve())
                except ValueError:
                    raise HTTPException(status_code=403, detail="access denied")
                host_path = str(create_host)
                agents_rel = _to_agents_relative(host_path)
                return {"host_path": host_path, "agents_relative": agents_rel}

    # Translate sandbox-internal paths to host paths (local sessions). Re-gate
    # the TRANSLATED host path: _sandbox_to_host is purely lexical, and an
    # expansion like /.claude/x.json -> users/{u}/.claude/x.json only becomes
    # protected after translation. Mirrors _gate_in_tree_host on the pull path.
    host_path = _sandbox_to_host(raw, ctx, agent_dir)
    try:
        in_tree = Path(host_path).resolve().is_relative_to(agent_dir.resolve())
    except (OSError, ValueError):
        in_tree = False
    if in_tree:
        acc = check_host_path_access(Path(host_path), ctx, writing=req.writing)
        if not acc.allowed:
            raise HTTPException(status_code=403, detail=acc.reason or "access denied")
    agents_rel = _to_agents_relative(host_path)
    return {"host_path": host_path, "agents_relative": agents_rel}


# (removed: _looks_sandbox_virtual — the resolve-path policy-reject branch now
# 403s on ANY denied resolution, so its sandbox-virtual carve-out is obsolete.)


def _sandbox_to_host(sandbox_path: str, ctx: SecurityContext, agent_dir: Path) -> str:
    """Map a sandbox-internal path to a host-absolute path.

    Every per-user expansion keys on ``ctx.mount_username`` — the MOUNT
    identity ("" for agent-scope mounts, including Shared-only human chats,
    whose ``ctx.username`` stays set for attribution). Keying on the raw
    username misdirected Shared-only sessions' paths into per-user dirs
    that their mode doesn't even mount (found live 2026-07-10)."""
    p = sandbox_path

    # /.claude/ → session's .claude/ dir
    if p.startswith("/.claude/") or p == "/.claude":
        if ctx.mount_username:
            return str(agent_dir / "users" / ctx.mount_username / ".claude" / p[9:])
        return str(agent_dir / "workspace" / ".claude" / p[9:])

    # Viewer redirect: for viewers, Docker MCPs that say `/workspace/foo`
    # mean THEIR personal workspace (matches their `OTO_WORKSPACE_DIR =
    # /users/{u}/workspace` for stdio MCPs). The viewer's bwrap mount ALSO
    # exposes the shared `/workspace` RO (expanded viewer reads),
    # so stdio Read sees shared content, but Docker MCPs see per-user —
    # this is the intentional asymmetry: writes via Docker MCPs land in
    # the user's own dir, not the shared workspace.
    if ctx.role == "viewer" and ctx.mount_username:
        if p.startswith("/workspace/") or p == "/workspace":
            return str(agent_dir / "users" / ctx.mount_username / "workspace" / p[11:])
        if p.startswith("/context/") or p == "/context":
            return str(agent_dir / "users" / ctx.mount_username / "context" / p[9:])

    # Editor / Manager / Admin: /workspace/ → shared workspace.
    # /config/ is owner-only — editor's bwrap doesn't mount it
    # and path_policy denies their reads. If a Docker MCP somehow sends
    # /config for an editor session, the host path resolves but the file
    # is owner-curated; documented residual (Docker MCPs bypass bwrap +
    # path_policy hook — same gap as `satellite has no bwrap`).
    if p.startswith("/config/") or p == "/config":
        return str(agent_dir / "config" / p[8:])
    if p.startswith("/workspace/") or p == "/workspace":
        return str(agent_dir / "workspace" / p[11:])

    # /users/{username}/ → users/{username}/
    if p.startswith("/users/"):
        return str(agent_dir / p[1:])  # strip leading /

    # /context/ for viewer (already handled above, but safety)
    if p.startswith("/context/") or p == "/context":
        if ctx.mount_username:
            return str(agent_dir / "users" / ctx.mount_username / "context" / p[9:])

    # NOTE: the legacy "/includes/<sub-agent>/" cross-agent translation was
    # removed. No component produced it, and it let a session map ANY agent's
    # tree (cross-agent / cross-user file + OAuth-token read). If cross-agent
    # include is reintroduced, it MUST validate <sub-agent> against the session
    # agent's delegation targets and re-impose the per-user RBAC.

    # /screenshots/ → passthrough (MCP mount)
    if p.startswith("/screenshots"):
        return p

    # Fallback: treat as relative to agent dir
    return str(agent_dir / p.lstrip("/"))


def _to_agents_relative(host_path: str) -> str:
    """Convert host-absolute path to agents-dir-relative (for Docker MCP /agents/ mount).

    Robust against trailing-slash variants of ``AGENTS_DIR``
    (previously a trailing slash would silently strip the leading
    ``/`` from the relative form).
    """
    agents_dir = str(config.AGENTS_DIR).rstrip("/")
    if host_path == agents_dir:
        return ""
    if host_path.startswith(agents_dir + "/"):
        return host_path[len(agents_dir):]  # keeps leading "/"
    return host_path


# ---------------------------------------------------------------------------
# Batched path resolution — used by the satellite stdio
# interceptor (one batched call per stdio MCP tools/call request).
# ---------------------------------------------------------------------------


class ResolveToolArgItem(BaseModel):
    value: str
    write: bool = False
    json_path: str = ""
    realpath_verify: bool = False


class ResolveToolArgPathsRequest(BaseModel):
    session_id: str
    tool: str = ""  # echoed back for diagnostics; never used for resolution
    items: list[ResolveToolArgItem]


@router.post("/v1/hooks/resolve-tool-arg-paths")
async def resolve_tool_arg_paths(
    req: ResolveToolArgPathsRequest,
    authorization: str | None = Header(None),
):
    """Batched policy + path resolution for stdio MCP tool-call args.

    The satellite's stdio interceptor calls this once per
    ``tools/call`` JSON-RPC message regardless of how many path args
    are declared. Input order is preserved in the response so the
    interceptor can re-stitch values back into the JSON tree.

    Always returns 200 with structured per-item ``allowed``/``error``
    fields — the interceptor synthesizes a JSON-RPC tool-error to the
    LLM when any item is rejected.
    """
    verify_session_match(authorization, req.session_id)

    ctx = get_session_security(req.session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Session not found")

    from services import path_policy_v2 as _v2
    policy_ctx = _v2.context_from_security(ctx)
    resolutions = _v2.resolve_path_batch(
        policy_ctx,
        [
            _v2.ResolveItem(
                raw_path=item.value,
                write=item.write,
                json_path=item.json_path,
                realpath_verify=item.realpath_verify,
            )
            for item in req.items
        ],
    )

    out: list[dict] = []
    for item, r in zip(req.items, resolutions):
        allowed, error = r.allowed, r.error
        # path_policy_v2 admits any in-tree path incl. /users/OTHER —
        # re-impose cross-user + role RBAC per item (honouring its write flag).
        if allowed:
            _rbac = enforce_agent_tree_rbac(r, ctx, writing=item.write)
            if not _rbac.allowed:
                allowed, error = False, (_rbac.reason or "access denied")
        path_ref = (
            {"kind": r.path_ref.kind, "value": r.path_ref.value}
            if r.path_ref is not None else None
        )
        out.append({
            "access_path":      r.access_path,
            "allowed":          allowed,
            "error":            error,
            "path_ref":         path_ref,
            "is_remote_pull":   r.is_remote_pull,
            "is_remote_push":   r.is_remote_push,
            "sandbox_relative": r.sandbox_relative,
        })
    return {"items": out, "tool": req.tool}


def _resolve_hook_path(session_id: str, raw_path: str) -> Path:
    """Resolve a path from an MCP hook callback to a host-absolute path.

    Accepts three input forms (see top-of-file path-form contract):
      1. **Real host-absolute path** (legacy stdio / direct callers): returned
         unchanged if the file exists.
      2. **Agents-relative** (canonical for Docker MCPs post-v2):
         ``personal-assistant/users/<u>/workspace/foo.docx`` →
         ``${AGENTS_DIR}/personal-assistant/users/<u>/workspace/foo.docx``.
      3. **Sandbox-virtual** (canonical for stdio MCPs with ``OTO_*`` env):
         ``/users/<u>/workspace/foo.docx`` → translated via the session's
         ``SecurityContext`` (same rules as ``/v1/hooks/resolve-path``).

    For non-sandboxed sessions form 1 typically wins. For sandboxed local
    sessions, forms 2 and 3 both reach the right file — agents-relative is
    cheaper (no security-context lookup) and is what file-tools posts.

    **Remote sessions**: use ``_classify_and_pull`` from async FastAPI
    handlers — this sync function cannot await the WS file pull, so it only
    handles local translation.
    """
    host = Path(raw_path)
    if host.is_file():
        return host  # form 1

    # Form 2: agents-relative (no leading "/"). Cheap O(1) check before
    # falling through to the sandbox-virtual translator.
    if raw_path and not raw_path.startswith("/"):
        candidate = config.AGENTS_DIR / raw_path
        if candidate.is_file():
            return candidate

    # Form 3: sandbox-virtual — translate via session SecurityContext
    ctx = get_session_security(session_id)
    if ctx:
        agent_dir = config.AGENTS_DIR / ctx.agent
        resolved = Path(_sandbox_to_host(raw_path, ctx, agent_dir))
        if resolved.is_file():
            return resolved

    return host  # return original — caller will raise 404


async def _classify_and_pull(
    session_id: str, raw_path: str, *, writing: bool = False,
) -> tuple["Path | None", "PathResolution | None"]:
    """Resolve an LLM-supplied path to an existing proxy-local file.

    Remote sessions: classify via ``path_policy_v2`` — the same gate the
    Docker-MCP ``resolve-path`` hook uses — and route by ``path_ref.kind``:
      * ``agent_tree`` → ``pull_through`` (synced workspace; resolves by the
        platform path regardless of the satellite's OS username).
      * ``satellite_host`` → ``pull_through_host_path`` (lazy pull of a
        Desktop/Downloads file into the proxy cache).
    The resolver folds a satellite-absolute path that sits inside the synced
    tree back to ``agent_tree`` — both the per-user layout
    (``{sat_agents_dir}/{slug}/users/<u>/workspace/foo.png``) and the
    shared-only layout (``{sat_agents_dir}/{slug}/workspace/foo.png``, no
    ``users/<u>`` segment) — and applies the credential/`.ssh`/`.env`
    denylist + home-vs-full-FS policy.

    Local sessions: delegate to the sync ``_resolve_hook_path`` (bwrap + the
    stdio interceptor already gate local paths).

    Returns ``(host_path | None, resolution | None)``. ``host_path`` exists on
    the proxy; ``None`` means unresolved. On a policy denial the returned
    ``resolution`` carries ``.error`` for the caller to surface verbatim.
    """
    from core.remote import remote_file_flow
    ctx = get_session_security(session_id)
    if ctx is None:
        return None, None
    agent_root = (config.AGENTS_DIR / ctx.agent).resolve()

    def _gate_in_tree_host(host: "Path") -> bool:
        # In the session's OWN agent tree → full cross-user + role RBAC.
        # Under AGENTS_DIR but a DIFFERENT agent's tree → cross-agent escape →
        # deny. Truly outside AGENTS_DIR (a form-1 real host path an MCP
        # resolved itself) → left to the caller's own under-dir check.
        rh = host.resolve()
        if rh.is_relative_to(agent_root):
            return check_host_path_access(host, ctx, writing=writing).allowed
        try:
            if rh.is_relative_to(config.AGENTS_DIR.resolve()):
                return False
        except (OSError, ValueError):
            return False
        return True

    if not remote_file_flow.is_remote_session(session_id):
        host = _resolve_hook_path(session_id, raw_path)
        # The local form-2 resolver skips the security context →
        # re-gate so a session can't read another user's file via the
        # display / preview / media / temp-image hooks.
        if not host.is_file() or not _gate_in_tree_host(host):
            return None, None
        return host, None

    # Docker MCPs (file-tools) post an ALREADY-RESOLVED agents-relative path —
    # the exact form the resolve-path hook handed back: "<agent>/users/.../f.docx"
    # for a synced workspace file, or ".remote-host-cache/<hash>/f.docx" for a
    # lazy-pulled satellite-host file (e.g. a Desktop doc). Both already exist
    # on the proxy under AGENTS_DIR and were RBAC-gated at resolve-path time.
    # Use them directly — re-classifying as an LLM path would mis-anchor the
    # (slash-less) relative path to /workspace and try to pull a proxy-only
    # cache path FROM the satellite → None → 400 on document-preview.
    if raw_path and not raw_path.startswith("/"):
        agents_root = config.AGENTS_DIR.resolve()
        direct = (agents_root / raw_path).resolve()
        if direct.is_file() and (direct == agents_root or agents_root in direct.parents):
            if not _gate_in_tree_host(direct):
                return None, None
            return direct, None

    from services import path_policy_v2 as _v2
    policy_ctx = _v2.context_from_security(ctx)
    resolution = _v2.resolve_path_for_session(policy_ctx, raw_path, writing=writing)
    if not resolution.allowed or resolution.path_ref is None:
        return None, resolution

    # path_policy_v2 admits any in-tree path incl. /users/OTHER — re-impose
    # cross-user + role RBAC (satellite-host paths are already home/full-FS-gated).
    _rbac = enforce_agent_tree_rbac(resolution, ctx, writing=writing)
    if not _rbac.allowed:
        import dataclasses
        return None, dataclasses.replace(
            resolution, allowed=False, error=_rbac.reason or "access denied",
        )

    ref = resolution.path_ref
    if ref.kind == "satellite_host":
        pulled = await remote_file_flow.pull_through_host_path(session_id, ref.value)
    else:  # agent_tree
        pulled = await remote_file_flow.pull_through(session_id, ref.value)
    if pulled is not None and pulled.is_file():
        return pulled, resolution
    return None, resolution


# ---------------------------------------------------------------------------
# Hook routing — meeting-aware session rebinding
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class HookRoute:
    """Where a hook callback's out-of-band event belongs.

    ``queue_session_id`` keys the permission queue the event is pushed to
    (``get_permission_queue``). ``chat_id`` is the chat for chat-keyed side
    effects — filled only for meeting participants (the meeting's parent
    chat), empty otherwise; use :func:`resolve_hook_chat_id` when a chat row
    is actually needed (keeps the per-tool-call hook paths free of DB
    lookups).
    """
    queue_session_id: str
    chat_id: str = ""
    meeting_agent: str = ""
    parent_session_id: str = ""
    is_meeting: bool = False


def resolve_hook_route(session_id: str) -> HookRoute:
    """The single meeting-awareness chokepoint for every hook family.

    Meeting participants run their own CLI sessions, but their output streams
    through the MEETING's pump (session ``meeting-<id>``) into the parent
    chat — while every hook posts the PARTICIPANT's session_id. Consulting
    this resolver rebinds them: events go to the pump's permission queue,
    chat-keyed side effects to the parent chat, and ``meeting_agent`` carries
    the speaker identity for badges. Normal sessions fall back to identity
    (their own queue, their own chat).
    """
    info = get_meeting_session_info(session_id)
    if info:
        return HookRoute(
            queue_session_id=info["pump_session_id"],
            chat_id=info["parent_chat_id"],
            meeting_agent=info["agent_slug"],
            parent_session_id=info["parent_session_id"],
            is_meeting=True,
        )
    return HookRoute(queue_session_id=session_id)


async def resolve_hook_chat_id(session_id: str) -> str:
    """Effective chat for a hook's chat-keyed side effects: the meeting's
    parent chat for participants, else the session's own chat row (empty
    string if none)."""
    route = resolve_hook_route(session_id)
    if route.chat_id:
        return route.chat_id
    chat = await asyncio.to_thread(task_store.get_chat_by_session, session_id)
    return chat["id"] if chat else ""


# ---------------------------------------------------------------------------
# Permission hook
# ---------------------------------------------------------------------------

class HookPermissionRequest(BaseModel):
    session_id: str
    tool_name: str
    tool_input: dict = {}


@router.post("/v1/hooks/permission")
async def hook_permission(req: HookPermissionRequest, authorization: str | None = Header(None)):
    """Permission gate for the Claude CLI PreToolUse hook, the satellite stdio
    MCP transport gate, and the satellite Codex approval bridge.

    Thin transport wrapper: it only verifies the session, then delegates to
    ``decide_tool_permission`` — the single decision authority, reused in-process
    by the local Codex app-server approval handler.
    """
    verify_session_match(authorization, req.session_id)
    return await decide_tool_permission(req.session_id, req.tool_name, req.tool_input)


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
    a token — the gate is at provisioning time in ``remote_execution``.
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


async def decide_tool_permission(
    session_id: str, tool_name: str, tool_input: dict | None = None,
) -> dict:
    """The single permission-decision authority for every execution layer.

    Pass-1 = per-role path policy (``check_tool_access``) + target-revocation +
    OAuth-credential denylist; Pass-2 = mode branching (default / acceptEdits /
    dontAsk / auto / plan) with a dashboard block-and-wait for the prompt cases.
    Returns ``{"decision": "allow"|"deny", "reason"?: str, "updated_input"?: dict}``.

    ``updated_input`` rides along on ALLOW when Pass-1 rewrote a native tool's
    path arg for a remote satellite (sandbox-virtual / ``~`` form → the
    satellite-host path). The Claude PreToolUse hook returns it as
    ``updatedInput`` so the tool executes against the real path; other callers
    (Codex approval, stdio MCP gate) ignore the key.

    Consulted by:
      * the Claude CLI PreToolUse hook (via ``/v1/hooks/permission``),
      * the **local** Codex app-server approval handler (in-process),
      * the **remote** Codex approval bridge + the stdio MCP transport gate
        (both via ``/v1/hooks/permission`` over the loopback tunnel).

    No transport auth here — the caller (the endpoint / interceptor) verifies the
    session. ``AskUserQuestion`` is denied after surfacing the question; in plan
    mode non-plan tools are denied (read-only planning).
    """
    _pass1_out: dict = {}
    result = await _decide_tool_permission(
        session_id, tool_name, tool_input, _pass1_out,
    )
    if result.get("decision") == "allow" and _pass1_out.get("updated_input"):
        return {**result, "updated_input": _pass1_out["updated_input"]}
    return result


async def _decide_tool_permission(
    session_id: str, tool_name: str, tool_input: dict | None,
    _pass1_out: dict,
) -> dict:
    """Implementation of :func:`decide_tool_permission`. ``_pass1_out``
    carries Pass-1 side data (currently ``updated_input``) back to the
    wrapper without threading it through every mode-branch return."""
    tool_input = tool_input or {}
    mode = get_session_mode(session_id)
    client_type = get_session_client_type(session_id)

    # Meeting agents inherit the parent chat's permission mode, and every
    # block-and-wait prompt below posts to route.queue_session_id — the
    # meeting pump's queue for participants, the session's own otherwise.
    route = resolve_hook_route(session_id)
    if route.is_meeting:
        mode = get_session_mode(route.parent_session_id)
        client_type = "dashboard"  # apply dashboard mode logic

    # Track hook activity so settle mode knows agents are still working
    record_hook_activity(session_id)
    logger.info(f"Hook permission: session={session_id}, tool={tool_name}, mode={mode}, client={client_type}")

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
        if mode in ("dontAsk", "auto"):
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
            from services.mcp import mcp_registry
            parts = tool_name.split("__", 2)
            server = parts[1] if len(parts) >= 2 else ""
            tool_only = parts[2] if len(parts) >= 3 else ""
            cap = mcp_registry.device_capability_for_server(server)
            granted = getattr(security_ctx, "target_device_grants", None) or set()
            if cap and cap in granted:
                # High-risk app-connector tools (e.g. execute_blender_code = raw
                # RCE inside the app, bypassing the bash-tier system) are EXCLUDED
                # from the blanket device auto-approve: they still prompt even
                # though the capability is granted.
                if mcp_registry.is_high_risk_device_tool(server, tool_only):
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

        # Interactive TUI: DEFER the residual ask-tier to Claude's own
        # permission system — its native in-terminal prompt + Shift+Tab modes
        # (acceptEdits/bypass) decide. "defer" makes the hook emit NO decision
        # (vs "ask", which would force a prompt and defeat Shift+Tab). The hard
        # denies above already returned "deny"; this is only the prompt case.
        # The dashboard block-and-wait is for headless -p.
        if _is_interactive_session(session_id):
            return {"decision": "defer"}

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
            # High-risk device tools re-prompt per call by design — never
            # remembered. `server`/`tool_only` are bound by the mcp__ branch.
            if not mcp_registry.is_high_risk_device_tool(server, tool_only):
                remember_session_tool_allow(session_id, tool_name)
        return {"decision": "allow" if approved else "deny"}

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
    if get_session_client_type(session_id) in ("task", "phone", "meeting", "trigger", "internal"):
        return {}
    route = resolve_hook_route(session_id)
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


class HookImagesItem(BaseModel):
    """One image inside a `/v1/hooks/images` payload.

    Exactly one of ``url`` (external CDN — browser fetches directly) or
    ``image_data`` (base64, for local files the MCP already read) must be set.
    """
    url: str = ""
    image_data: str = ""
    mime_type: str = "image/jpeg"
    caption: str = ""
    attribution: str = ""
    link_url: str = ""
    download_url: str = ""


class HookImagesRequest(BaseModel):
    session_id: str
    images: list[HookImagesItem]


@router.post("/v1/hooks/images")
async def hook_images(req: HookImagesRequest, authorization: str | None = Header(None)):
    """Called by display-mcp / image-gen-mcp / file-tools-mcp / image-search-mcp
    to push an inline image gallery (1-N images) to the chat.

    The dashboard renders 1 image as a single card, 2-3 as a row, 4+ as a
    horizontal scroll-snap carousel — that decision lives in the renderer,
    not here.
    """
    verify_session_match(authorization, req.session_id)
    if not req.images:
        raise HTTPException(status_code=400, detail="images list cannot be empty")
    for idx, item in enumerate(req.images):
        has_url = bool(item.url)
        has_data = bool(item.image_data)
        if has_url == has_data:
            # Either both set or neither — both are invalid.
            raise HTTPException(
                status_code=400,
                detail=f"images[{idx}] must have exactly one of url, image_data",
            )
    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": "images",
        "images": [item.model_dump() for item in req.images],
    })
    logger.info(
        f"Hook images: session={req.session_id}, count={len(req.images)}, "
        f"first_caption={req.images[0].caption[:50] if req.images else ''}"
    )
    return {"status": "ok"}


class HookImageGeneratingRequest(BaseModel):
    session_id: str
    prompt_preview: str = ""
    model: str = "nano-banana"


@router.post("/v1/hooks/image-generating")
async def hook_image_generating(req: HookImageGeneratingRequest, authorization: str | None = Header(None)):
    """Called by image-gen-mcp before starting generation. Shows skeleton placeholder."""
    verify_session_match(authorization, req.session_id)
    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": "image_generating",
        "prompt_preview": req.prompt_preview,
        "model": req.model,
    })
    return {"status": "ok"}


class HookImageGenFailedRequest(BaseModel):
    session_id: str


@router.post("/v1/hooks/image-gen-failed")
async def hook_image_gen_failed(req: HookImageGenFailedRequest, authorization: str | None = Header(None)):
    """Called by image-gen-mcp when generation fails. Removes skeleton placeholder."""
    verify_session_match(authorization, req.session_id)
    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({"event_type": "image_gen_failed"})
    return {"status": "ok"}


class HookUrlRequest(BaseModel):
    session_id: str
    url: str
    title: str
    description: str = ""


@router.post("/v1/hooks/url")
async def hook_url(req: HookUrlRequest, authorization: str | None = Header(None)):
    """Called by display-mcp to push a clickable link to the chat."""
    verify_session_match(authorization, req.session_id)
    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": "url",
        "url": req.url,
        "title": req.title,
        "description": req.description,
    })
    logger.info(f"Hook url: session={req.session_id}, url={req.url}")
    return {"status": "ok"}


class HookFileRequest(BaseModel):
    session_id: str
    path: str
    filename: str = ""
    description: str = ""


@router.post("/v1/hooks/file")
async def hook_file(req: HookFileRequest, authorization: str | None = Header(None)):
    """Called by display-mcp to push a downloadable file to the chat.

    Routes through the session's adapter to handle delivery (e.g. dashboard
    serves from proxy via token URLs). For remote sessions, the file is
    lazily pulled from the satellite into the platform-side cache first so
    the adapter can serve it directly.
    """
    verify_session_match(authorization, req.session_id)

    file_path, resolution = await _classify_and_pull(req.session_id, req.path)
    if file_path is None:
        detail = (
            resolution.error
            if resolution is not None and not resolution.allowed
            else f"File not found: {req.path}"
        )
        raise HTTPException(status_code=400, detail=detail)

    filename = req.filename or file_path.name

    # Route through adapter based on session's client_type. Meeting
    # participants carry client_type "meeting" (no adapter of its own) but
    # render into the parent DASHBOARD chat — serve them there.
    from adapters import get_adapter, get_session_adapter
    route = resolve_hook_route(req.session_id)
    if route.is_meeting:
        adapter = get_adapter("dashboard")
    else:
        adapter = get_session_adapter(req.session_id)
    if adapter is None:
        raise HTTPException(
            status_code=400,
            detail=f"No adapter found for session {req.session_id} -- client_type not set",
        )

    # The hook owns download-token minting (it holds the chat + security
    # context the adapter doesn't); the adapter only shapes the event. Durable
    # media_tokens row → the download button in chat HISTORY keeps working
    # across restarts (the old in-memory token died after 1h). Served by
    # /v1/media (cookie-gated; media_kind "file" is attachment-forced).
    download_url = ""
    if getattr(adapter, "serves_file_downloads", False):
        chat_id = await resolve_hook_chat_id(req.session_id) or None
        sec = get_session_security(req.session_id)
        download_token = secrets.token_urlsafe(32)
        task_store.create_media_token(
            download_token,
            str(file_path),
            media_kind="file",
            chat_id=chat_id,
            session_id=req.session_id,
            # cache_owned False even for satellite-pulled cache copies: several
            # tokens may share one cached file; the cache has its own lifecycle.
            cache_owned=False,
            expires_at="",  # durable until the chat is deleted
            agent=sec.agent if sec else "",
        )
        # No fn= here: DisplayFile/DocumentPreview append it client-side from
        # their filename prop (baking it in too would duplicate the param).
        download_url = f"/v1/media/{download_token}?download=1"

    result = await adapter.handle_file_display(
        req.session_id, file_path, filename, req.description, download_url,
    )

    queue = get_permission_queue(route.queue_session_id)
    await queue.put(result)
    logger.info(f"Hook file: session={req.session_id}, file={filename}")
    return {"status": "ok", **result}


class HookMediaRequest(BaseModel):
    session_id: str
    source: str
    media_kind: str  # "video" | "audio"
    caption: str = ""
    title: str = ""
    poster: str = ""  # video only; URL passthrough (local poster paths ignored)


@router.post("/v1/hooks/media")
async def hook_media(req: HookMediaRequest, authorization: str | None = Header(None)):
    """Called by display-mcp's display_video / display_audio to render a media
    player in the chat.

    Unlike images (base64-inlined into the event), media is served over HTTP
    with Range support via a capability token, so the file is never embedded in
    the event/DB. Web URLs pass straight through to ``<video src>``. Local /
    agent-tree / satellite-host paths are resolved to a proxy-local file (the
    satellite-host case is lazily pulled ≤50MB), made browser-playable, and
    minted a durable ``media_tokens`` row.
    """
    verify_session_match(authorization, req.session_id)

    kind = req.media_kind.strip().lower()
    if kind not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="media_kind must be 'video' or 'audio'")
    source = (req.source or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source is required")

    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)

    # Web URL → browser fetches the origin directly (Range handled there).
    if source.startswith(("http://", "https://")):
        await queue.put({
            "event_type": kind,
            "src_kind": "url",
            "url": source,
            "mime": "",
            "caption": req.caption,
            "title": req.title,
            "poster": req.poster if req.poster.startswith(("http://", "https://")) else "",
        })
        logger.info(f"Hook media({kind}): session={req.session_id}, url")
        return {"status": "ok"}

    # Resolve to a proxy-local file via the shared classify-first resolver:
    # synced-tree paths pull_through into the workspace, satellite-host paths
    # (Desktop/Downloads) lazy-pull into the proxy cache (≤100MB), and the
    # credential/.ssh/.env denylist applies for remote sessions.
    from services.media import media_pipeline
    from core.remote import remote_file_flow

    local_path, resolution = await _classify_and_pull(req.session_id, source)
    if local_path is None:
        detail = (
            resolution.error
            if resolution is not None and not resolution.allowed
            else f"media not reachable: {source} "
                 "(remote non-workspace files are limited to 100MB)"
        )
        raise HTTPException(status_code=400, detail=detail)
    # Satellite-host pulls land in the throwaway host cache (purged on chat
    # delete); synced/local files live in the workspace and are not cache-owned.
    cache_owned = remote_file_flow.is_host_cache_path(str(local_path))

    # Probe codecs; if we'll transcode, show a skeleton first so the chat isn't
    # blank while ffmpeg runs (the real block replaces it on completion).
    codecs = await media_pipeline.probe(local_path)
    if media_pipeline.needs_transcode(local_path, codecs) and media_pipeline.ffmpeg_available():
        await queue.put({
            "event_type": "media_processing",
            "media_kind": kind,
            "caption": req.caption,
        })
    # Satellite-host (Desktop/Downloads) media is re-pullable, not retained:
    # route its derivatives to the TTL'd host cache and record the origin path
    # so serve_media can re-pull from the laptop on replay.
    origin_path = ""
    media_dest = None
    if (
        resolution is not None
        and resolution.path_ref is not None
        and resolution.path_ref.kind == "satellite_host"
    ):
        origin_path = resolution.path_ref.value
        media_dest = media_pipeline.host_cache_dir()
    served_path, mime, transcode_cache = await media_pipeline.ensure_playable_async(
        local_path, codecs=codecs, media_kind=kind, dest_dir=media_dest,
    )
    cache_owned = cache_owned or transcode_cache
    resolved_kind = media_pipeline.media_kind_from_mime(mime) or kind

    chat_id = await resolve_hook_chat_id(req.session_id) or None
    info = remote_file_flow._get_remote_session_info(req.session_id)
    machine_id = getattr(info, "machine_id", None)
    sec = get_session_security(req.session_id)

    token = secrets.token_urlsafe(32)
    task_store.create_media_token(
        token,
        str(served_path),
        mime=mime,
        media_kind=resolved_kind,
        chat_id=chat_id,
        session_id=req.session_id,
        machine_id=machine_id,
        cache_owned=cache_owned,
        expires_at="",  # durable until the chat is deleted
        origin_path=origin_path,
        agent=sec.agent if sec else "",
    )

    await queue.put({
        "event_type": resolved_kind,
        "src_kind": "token",
        "token": token,
        "media_url": f"/v1/media/{token}",
        "mime": mime,
        "caption": req.caption,
        "title": req.title,
        "poster": req.poster if req.poster.startswith(("http://", "https://")) else "",
    })
    logger.info(
        f"Hook media({resolved_kind}): session={req.session_id}, "
        f"token={token[:8]}..., cache_owned={cache_owned}"
    )
    return {"status": "ok"}


class HookUiRequest(BaseModel):
    session_id: str
    html: str
    title: str = ""
    height: int | None = None  # fixed pixel height hint; None = auto-size
    save_path: str = ""  # sandbox-virtual; MCP sends its scope-correct default
    display: bool = True  # False = save the file only, no chat block/token


# Explicit cap under the global MAX_REQUEST_BODY_BYTES backstop: an artifact
# is a chat block, not a file transfer.
_UI_MAX_HTML_BYTES = 2 * 1024 * 1024


def _ui_slug(title: str) -> str:
    """Filename slug from an artifact title ('Tip calculator' → 'tip-calculator')."""
    s = "".join(c if c.isalnum() else "-" for c in title.lower())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:40] or "ui"


@router.post("/v1/hooks/ui")
async def hook_ui(req: HookUiRequest, authorization: str | None = Header(None)):
    """Called by display-mcp's display_ui to render an HTML artifact in the chat.

    The raw agent content is written VERBATIM to the caller's workspace
    (wrapping/theming happens at serve time in ``/v1/ui/{token}`` — the agent
    can Read/Edit the file it just created, and historical artifacts pick up
    wrapper improvements automatically), pushed to active remote sessions, and
    minted a durable ``media_tokens`` row (``media_kind="ui"``) that only the
    sandboxed ``/v1/ui`` route will serve.
    """
    verify_session_match(authorization, req.session_id)

    if not req.html.strip():
        raise HTTPException(status_code=400, detail="html is required")
    if len(req.html.encode("utf-8", errors="ignore")) > _UI_MAX_HTML_BYTES:
        raise HTTPException(status_code=400, detail="html exceeds the 2MB artifact cap")
    title = req.title.strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="title exceeds 200 characters")

    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    agent_dir = config.get_agent_dir(ctx.agent)

    # The caller's own scope workspace: the default save dir, the anchor for
    # relative paths, and the re-anchor target for denied/escaping explicit
    # paths. The hook owns the default (NOT the MCP): on satellites the MCP's
    # OTO_WORKSPACE_DIR is rewritten to a satellite-absolute path the proxy
    # could not resolve.
    # MOUNT identity, not attribution: a Shared-only human chat keeps
    # ctx.username for attribution but works in the AGENT scope — its
    # artifacts belong in the shared workspace (same rule as the MCP
    # framework's OTO_WORKSPACE_DIR injection).
    scope_root = (
        agent_dir / "users" / ctx.mount_username / "workspace"
        if ctx.mount_username else agent_dir / "workspace"
    )
    raw = req.save_path.strip()
    if not raw:
        raw = f"generated-ui/{_ui_slug(title)}-{secrets.token_hex(4)}.html"
    if raw.startswith("/"):
        # Sandbox-virtual — resolved in the CALLER'S scope (a viewer's
        # /workspace/x.html lands in THEIR users/{u}/workspace).
        target = Path(_sandbox_to_host(raw, ctx, agent_dir))
    else:
        # Documented workspace-relative form.
        target = scope_root / raw
    if target.suffix.lower() != ".html":
        target = target.with_suffix(".html")
    # Re-gate the resolved host path — _sandbox_to_host is purely lexical and
    # the write below is otherwise un-RBAC-gated. Anything escaping the tree
    # or denied by the role matrix re-anchors to the caller's generated-ui/.
    allowed = False
    try:
        if target.resolve().is_relative_to(agent_dir.resolve()):
            allowed = check_host_path_access(target, ctx, writing=True).allowed
    except (OSError, ValueError):
        allowed = False
    if not allowed:
        target = scope_root / "generated-ui" / Path(raw).name
        if target.suffix.lower() != ".html":
            target = target.with_suffix(".html")

    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_text, req.html, "utf-8")
    rel = target.relative_to(agent_dir).as_posix()
    sandbox_path = "/" + rel

    # Same-turn push so a satellite CLI can immediately Read the file it just
    # created (call-time import: uploads ↔ hooks would otherwise cycle).
    from api.media.uploads import _push_upload_to_active_remote_sessions
    await _push_upload_to_active_remote_sessions(ctx.agent, rel, target)

    # Live update-in-place (mirrors pin_app): /v1/ui serves the FILE at
    # request time, so overwriting a prior artifact's save_path already
    # changes what every minted token serves — this broadcast makes open
    # renders (inline blocks, PiP windows) reload NOW. With display=False
    # that's the whole delivery: a standing artifact updated silently across
    # turns, no new chat block.
    from services.notifications import notification_manager
    await notification_manager.broadcast_file_updated(ctx.agent, rel, source="disk")

    if not req.display:
        logger.info(f"Hook ui (save-only): session={req.session_id}, path={rel}")
        return {"status": "ok", "path": sandbox_path}

    chat_id = await resolve_hook_chat_id(req.session_id) or None
    token = secrets.token_urlsafe(32)
    # cache_owned MUST stay False: True would make the chat-delete reap unlink
    # the user's workspace .html — the artifact file outlives its chat.
    task_store.create_media_token(
        token,
        str(target),
        mime="text/html",
        media_kind="ui",
        chat_id=chat_id,
        session_id=req.session_id,
        machine_id=None,
        cache_owned=False,
        expires_at="",  # durable until the chat is deleted
        agent=ctx.agent,
    )

    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": "ui",
        "token": token,
        "ui_url": f"/v1/ui/{token}",
        "title": title,
        "height": req.height,
        "path": rel,
    })
    logger.info(f"Hook ui: session={req.session_id}, token={token[:8]}..., path={rel}")
    return {"status": "ok", "path": sandbox_path, "ui_url": f"/v1/ui/{token}"}


class HookAppPinRequest(BaseModel):
    session_id: str
    slug: str
    title: str = ""       # empty on create → slug; empty on update → keep
    html: str = ""        # required on first pin; optional on update
    actions: list | None = None  # None = leave unchanged; [] = clear
    make_default: bool = False
    # "standing" (default, the apps strip) | "chat" | "project" — scoped pins
    # surface on the Dock overlay. Scope ids are SESSION-DERIVED only (the
    # session's chat / its project), never caller-supplied.
    scope: str = "standing"


class HookAppSlugRequest(BaseModel):
    session_id: str
    slug: str = ""


def _app_scope(ctx) -> tuple[str, str | None]:
    """(username, owner_sub) for the caller's mini-app scope. Keyed on the
    MOUNT identity: agent-scope sessions — service sessions AND Shared-only
    human chats (whose ctx.username stays set for attribution) — pin SHARED
    rows with owner_sub NULL (see schema.init_pinned_apps)."""
    mu = ctx.mount_username
    if not mu:
        return "", None
    return mu, task_store.get_user_sub_by_username(mu)


@router.post("/v1/hooks/apps/pin")
async def hook_app_pin(req: HookAppPinRequest, authorization: str | None = Header(None)):
    """Called by display-mcp's pin_app to create/update a pinned mini-app.

    Upsert by (agent, caller scope, slug): the HTML (verbatim, wrapped at
    serve time like /v1/ui) lives at the FIXED path ``apps/<slug>.html``
    under the caller's scope workspace — no caller path input, deterministic
    ``file_updated`` matching, in-place update. Re-pinning with new html IS
    the live-refresh path for scheduled tasks (a native Write doesn't
    broadcast file_updated on local sandboxes). A changed actions manifest
    silently breaks the approval sig — buttons stay dead until the user
    re-approves (api/apps/manifest.py has the authority rules).

    Restore paths: pinning a slug the user soft-unpinned from the dashboard
    revives the hidden row (manifest + approval intact — the ack says so);
    with no row at all, html may still be omitted when ``apps/<slug>.html``
    already exists in the caller's scope (unpin keeps the file by design).

    ``scope="chat"|"project"`` pins a Dock dashboard instead of a standing
    app: the scope id resolves from THIS session's chat row (its id / its
    ``project_id``) — never from caller input, so there is nothing to forge.
    A scoped pin REPLACES the scope's existing pin (scope is the identity,
    slug is cosmetic); approval carries iff the manifest is unchanged.
    """
    from api.apps import manifest as _mf

    verify_session_match(authorization, req.session_id)
    slug = req.slug.strip().lower()
    if not _mf.APP_SLUG_RE.match(slug):
        raise HTTPException(status_code=400,
                            detail="slug must be 1-40 chars of [a-z0-9-], starting alphanumeric")
    title = req.title.strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="title exceeds 200 characters")
    if req.html and len(req.html.encode("utf-8", errors="ignore")) > _UI_MAX_HTML_BYTES:
        raise HTTPException(status_code=400, detail="html exceeds the 2MB artifact cap")
    scope = (req.scope or "standing").strip().lower()
    if scope not in ("standing", "chat", "project"):
        raise HTTPException(status_code=400,
                            detail='scope must be "standing", "chat" or "project"')

    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    agent_dir = config.get_agent_dir(ctx.agent)
    username, owner_sub = _app_scope(ctx)

    # Session-derived scope resolution (DECIDED: no caller-supplied ids —
    # a refresh task re-pinning a project app runs as a continuation of a
    # project chat and inherits the right ids from ITS chat).
    scope_chat_id, scope_project_id = "", ""
    if scope != "standing":
        chat = await asyncio.to_thread(
            task_store.get_chat_by_session, req.session_id,
        )
        if not chat:
            raise HTTPException(
                status_code=400,
                detail=f'scope="{scope}" needs a chat-bound session — this '
                       "session has no chat (pin from a chat or task turn)",
            )
        if scope == "chat":
            scope_chat_id = chat["id"]
        else:
            scope_project_id = chat.get("project_id") or ""
            if not scope_project_id:
                raise HTTPException(
                    status_code=400,
                    detail='scope="project" needs a chat that belongs to a '
                           "delegation project — this session's chat has none "
                           "(delegate with project_id first, or pin from a "
                           "project lane)",
                )

    scope_root = (
        agent_dir / "users" / username / "workspace"
        if username else agent_dir / "workspace"
    )
    target = scope_root / "apps" / f"{slug}.html"
    # Path derived from the validated slug only, but assert confinement
    # anyway — this write is otherwise un-RBAC-gated.
    if not target.resolve().is_relative_to(agent_dir.resolve()):
        raise HTTPException(status_code=400, detail="invalid slug")
    rel = target.relative_to(agent_dir).as_posix()

    existing = await asyncio.to_thread(task_store.get_app_by_slug, ctx.agent, username, slug)
    if existing is not None:
        # Slugs share one namespace per (agent, caller scope): a slug held by
        # a pin of a DIFFERENT scope is refused instead of silently converted
        # (converting would yank a standing app off the strip, or move a
        # dashboard between chats).
        held = (existing.get("scope_chat_id") or "",
                existing.get("scope_project_id") or "")
        if held != (scope_chat_id, scope_project_id):
            kind = ("a chat-scoped" if held[0] else
                    "a project-scoped" if held[1] else "a standing")
            raise HTTPException(
                status_code=400,
                detail=f"slug '{slug}' already names {kind} pin in your "
                       "scope — pick another slug (scoped pins may reuse "
                       "their own slug to update)",
            )
    reused_file = False
    if existing is None:
        if not req.html.strip():
            # No row, no html — but the FIXED path may still hold the file
            # from a hard-unpinned registration: registering over it makes
            # re-pin a one-liner (unpin keeps the file by design).
            if not await asyncio.to_thread(target.is_file):
                raise HTTPException(
                    status_code=400,
                    detail=f"html is required on first pin (no apps/{slug}.html in your scope yet)",
                )
            reused_file = True
        if scope == "standing":
            # Scoped pins skip the cap: one-per-scope by construction.
            count = await asyncio.to_thread(task_store.count_apps, ctx.agent, username)
            if count >= task_store.MAX_APPS_PER_SCOPE:
                raise HTTPException(
                    status_code=400,
                    detail=f"app limit reached ({task_store.MAX_APPS_PER_SCOPE}) — unpin one first",
                )

    actions_json: str | None = None
    if req.actions is not None:
        actions_json, err = await asyncio.to_thread(
            _mf.validate_actions, req.actions, ctx.agent, not username,
        )
        if actions_json is None:
            raise HTTPException(status_code=400, detail=err)

    if req.html.strip():
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, req.html, "utf-8")
        # Same-turn satellite push (call-time import: uploads ↔ hooks cycle).
        from api.media.uploads import _push_upload_to_active_remote_sessions
        await _push_upload_to_active_remote_sessions(ctx.agent, rel, target)

    restored = bool(existing and existing.get("hidden"))
    replaced = ""
    if scope == "standing":
        row = await asyncio.to_thread(
            task_store.upsert_app,
            ctx.agent, username, owner_sub, slug,
            title=title or (slug if existing is None else None),
            rel_path=rel,
            actions_json=actions_json,
            make_default=req.make_default,
        )
    else:
        old = await asyncio.to_thread(
            task_store.get_scoped_app,
            chat_id=scope_chat_id, project_id=scope_project_id,
        )
        if old and (old["agent"], old["username"], old["slug"]) \
                != (ctx.agent, username, slug):
            replaced = old["slug"]
        row = await asyncio.to_thread(
            task_store.upsert_scoped_app,
            ctx.agent, username, owner_sub, slug,
            scope_chat_id=scope_chat_id,
            scope_project_id=scope_project_id,
            title=title or None,
            rel_path=rel,
            actions_json=actions_json,
        )
    # Broadcast on EVERY pin, not just html writes: a revived/registered row
    # must refresh open overlay tab strips (they invalidate the registry on
    # any apps/*.html file_updated), and re-wrapping an unchanged file is
    # harmless.
    from services.notifications import notification_manager
    await notification_manager.broadcast_file_updated(ctx.agent, rel, source="disk")
    approved = task_store.app_actions_approved(row)
    has_actions = bool(_mf.parse_actions(row))
    logger.info(
        f"Hook app pin: session={req.session_id}, agent={ctx.agent}, "
        f"slug={slug}, scope={'shared' if not username else username}, "
        f"pin_scope={scope}, approved={approved}, restored={restored}, "
        f"replaced={replaced or '-'}, reused_file={reused_file}"
    )
    out = {
        "status": "ok",
        "app_id": row["id"],
        "path": "/" + rel,
        "scope": "shared" if not username else "personal",
        "pin_scope": scope,
        "actions_approved": approved,
        "approval": ("approved" if approved else "pending user approval")
                    if has_actions else "none",
    }
    if replaced:
        out["replaced"] = (
            f"replaced the {scope}'s previous pin '{replaced}' — the scope "
            "holds exactly one dashboard"
            + (" (approval carried over — same manifest)" if approved and
               has_actions else "")
        )
    elif restored:
        out["restored"] = ("re-pinned the app the user had unpinned from the "
                           "dashboard — manifest and approval carried over")
    elif reused_file:
        out["reused_file"] = f"registered over the existing apps/{slug}.html"
    return out


@router.post("/v1/hooks/apps/unpin")
async def hook_app_unpin(req: HookAppSlugRequest, authorization: str | None = Header(None)):
    """Retire a pinned mini-app: the HARD delete (registration, manifest and
    approval all go; the workspace ``.html`` is the user's artifact and
    stays). The dashboard's X is the soft variant — it only hides the row,
    and this hook still finds hidden rows so "delete it entirely" works."""
    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    username, _ = _app_scope(ctx)
    row = await asyncio.to_thread(
        task_store.get_app_by_slug, ctx.agent, username, req.slug.strip().lower(),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no pinned app with that slug in your scope")
    await asyncio.to_thread(task_store.delete_app, row["id"])
    logger.info(f"Hook app unpin: session={req.session_id}, slug={req.slug}")
    return {"status": "ok", "kept_file": row["rel_path"]}


@router.post("/v1/hooks/apps/list")
async def hook_app_list(req: HookAppSlugRequest, authorization: str | None = Header(None)):
    """The caller-scope app list (shared + the session user's personal rows)
    so the agent reuses slugs deliberately instead of guessing. Includes
    soft-unpinned rows flagged ``unpinned`` — pin_app(slug) restores one
    with its manifest and approval intact. Chat/project-scoped Dock pins are
    appended after the standing list with their ``pin_scope``."""
    from api.apps import manifest as _mf

    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    username, _ = _app_scope(ctx)
    rows = await asyncio.to_thread(
        task_store.list_apps, ctx.agent, username, include_hidden=True,
    )
    rows += await asyncio.to_thread(
        task_store.list_scoped_apps, ctx.agent, username,
    )
    return {"apps": [{
        "slug": r["slug"],
        "title": r["title"],
        "scope": "shared" if not r["username"] else "personal",
        "pin_scope": ("chat" if r.get("scope_chat_id")
                      else "project" if r.get("scope_project_id")
                      else "standing"),
        "path": "/" + r["rel_path"],
        "position": r["position"],
        "actions": [{k: a.get(k) for k in ("id", "label", "type")}
                    for a in _mf.parse_actions(r)],
        "actions_approved": task_store.app_actions_approved(r),
        "updated_at": r["updated_at"],
        **({"unpinned": "user removed it from the dashboard — "
                        "pin_app(slug) restores it (approval intact)"}
           if r.get("hidden") else {}),
    } for r in rows]}


class HookFilePinRequest(BaseModel):
    session_id: str
    path: str = ""        # workspace-relative ("projects/x/plan.md"); an
    #                       agent-root path (workspace/…, users/…, knowledge/…)
    #                       is accepted too. Empty on unpin = the whole scope.
    title: str = ""       # empty → the filename
    # "chat" (default) | "project" — which Dock the file rides. Scope ids are
    # SESSION-DERIVED only (the session's chat / its project), never
    # caller-supplied — same rule as the app pin hook.
    scope: str = "chat"


async def _file_pin_scope(req_session_id: str, scope: str) -> tuple[str, str]:
    """Session-derived (scope_chat_id, scope_project_id) for the file-pin
    hooks — the exact resolution the app pin hook uses."""
    if scope not in ("chat", "project"):
        raise HTTPException(status_code=400,
                            detail='scope must be "chat" or "project"')
    chat = await asyncio.to_thread(task_store.get_chat_by_session, req_session_id)
    if not chat:
        raise HTTPException(
            status_code=400,
            detail=f'scope="{scope}" needs a chat-bound session — this '
                   "session has no chat (pin from a chat or task turn)",
        )
    if scope == "chat":
        return chat["id"], ""
    project_id = chat.get("project_id") or ""
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail='scope="project" needs a chat that belongs to a '
                   "delegation project — this session's chat has none "
                   "(delegate with project_id first, or pin from a "
                   "project lane)",
        )
    return "", project_id


def _file_pin_candidates(ctx, path: str) -> list[str]:
    """Agent-root-relative candidate rel_paths for a caller-supplied pin
    path, LEXICAL only (no existence check — unpin must resolve for files
    deleted since). First candidate: relative to the caller's scope
    workspace (shared ``workspace/`` or ``users/<u>/workspace/`` — the way
    agents write paths); second: the path taken as agent-root-relative when
    it names a readable top-level area. Confinement + OAuth-dir denial
    applied to every candidate; traversal is refused."""
    from api.agents.files import _check_oauth_protected

    agent_dir = config.get_agent_dir(ctx.agent)
    p = (path or "").strip().strip("/")
    if not p or "\x00" in p:
        raise HTTPException(status_code=400, detail="path is required")
    candidates: list[str] = []
    scope_root = (
        agent_dir / "users" / ctx.mount_username / "workspace"
        if ctx.mount_username else agent_dir / "workspace"
    )
    for base in (scope_root, agent_dir):
        if base is agent_dir and p.split("/", 1)[0] not in (
                "workspace", "users", "knowledge"):
            continue
        resolved = (base / p).resolve()
        if not resolved.is_relative_to(agent_dir.resolve()):
            raise HTTPException(status_code=403,
                                detail="path traversal not allowed")
        rel = resolved.relative_to(agent_dir.resolve()).as_posix()
        _check_oauth_protected(rel)
        if rel not in candidates:
            candidates.append(rel)
    return candidates


@router.post("/v1/hooks/files/pin")
async def hook_file_pin(req: HookFilePinRequest,
                        authorization: str | None = Header(None)):
    """Called by display-mcp's pin_file: pin a workspace FILE to the chat/
    project Dock. The Dock renders it read-only with board-file semantics
    (collapsed row → expand → markdown), reading the content through the
    files API — per-viewer path policy is enforced there, this hook only
    validates the AGENT-side reference (confinement, OAuth-dir denial,
    existence, text extension). Re-pinning the same path updates the title.
    Remote sessions pin the platform MIRROR path — content refreshes when
    the satellite syncs (end of turn)."""
    from api.agents.files import TEXT_EXTENSIONS

    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    title = req.title.strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="title exceeds 200 characters")
    scope = (req.scope or "chat").strip().lower()
    scope_chat_id, scope_project_id = await _file_pin_scope(req.session_id, scope)

    agent_dir = config.get_agent_dir(ctx.agent)
    rel = ""
    for cand in _file_pin_candidates(ctx, req.path):
        if await asyncio.to_thread((agent_dir / cand).is_file):
            rel = cand
            break
    if not rel:
        raise HTTPException(
            status_code=404,
            detail=f"no file at '{req.path}' in your workspace — pin an "
                   "existing file (the Dock renders it, it can't create it)",
        )
    ext = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    if ext not in TEXT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"'{ext or rel}' is not a renderable text type — file pins "
                   "render markdown/text only "
                   f"({', '.join(sorted(TEXT_EXTENSIONS))})",
        )

    pins = await asyncio.to_thread(
        task_store.list_file_pins,
        chat_id=scope_chat_id, project_id=scope_project_id,
    )
    if (rel not in {p["rel_path"] for p in pins}
            and len(pins) >= task_store.MAX_FILE_PINS_PER_SCOPE):
        raise HTTPException(
            status_code=400,
            detail=f"file-pin limit reached "
                   f"({task_store.MAX_FILE_PINS_PER_SCOPE} per {scope}) — "
                   "unpin one first",
        )
    row = await asyncio.to_thread(
        task_store.upsert_file_pin,
        ctx.agent, rel,
        scope_chat_id=scope_chat_id, scope_project_id=scope_project_id,
        title=title or rel.rsplit("/", 1)[-1],
    )
    from services.notifications import notification_manager
    await notification_manager.broadcast_file_updated(
        ctx.agent, rel, source="disk", pin=True,
    )
    logger.info(
        f"Hook file pin: session={req.session_id}, agent={ctx.agent}, "
        f"rel={rel}, pin_scope={scope}"
    )
    return {
        "status": "ok",
        "pin_id": row["id"],
        "path": "/" + rel,
        "pin_scope": scope,
        "title": row["title"],
        "note": "read-only Dock row — collapsed by default, renders live "
                "from the file (edits show within seconds; remote files on "
                "the next sync)",
    }


@router.post("/v1/hooks/files/unpin")
async def hook_file_unpin(req: HookFilePinRequest,
                          authorization: str | None = Header(None)):
    """Remove a Dock file pin (the file itself is never touched). With
    ``path`` empty, clears every pin of the scope."""
    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    scope = (req.scope or "chat").strip().lower()
    scope_chat_id, scope_project_id = await _file_pin_scope(req.session_id, scope)

    removed = 0
    rels: list[str] = []
    if req.path.strip():
        # Lexical resolution — the pinned file may be gone from disk.
        rels = _file_pin_candidates(ctx, req.path)
        for rel in rels:
            removed += await asyncio.to_thread(
                task_store.delete_file_pins,
                chat_id=scope_chat_id, project_id=scope_project_id,
                rel_path=rel,
            )
    else:
        pins = await asyncio.to_thread(
            task_store.list_file_pins,
            chat_id=scope_chat_id, project_id=scope_project_id,
        )
        rels = [p["rel_path"] for p in pins]
        removed = await asyncio.to_thread(
            task_store.delete_file_pins,
            chat_id=scope_chat_id, project_id=scope_project_id,
        )
    if not removed:
        raise HTTPException(status_code=404,
                            detail="no matching file pin in that scope")
    from services.notifications import notification_manager
    for rel in rels:
        await notification_manager.broadcast_file_updated(
            ctx.agent, rel, source="disk", pin=True,
        )
    logger.info(
        f"Hook file unpin: session={req.session_id}, agent={ctx.agent}, "
        f"removed={removed}, pin_scope={scope}"
    )
    return {"status": "ok", "removed": removed, "kept_files": True}


# Cache WOPI URLs per file_id+user so Collabora sees one session, not many.
# Key: (file_id, user_sub) → {"wopi_url": str, "token_ttl": int, "expires": float}
_wopi_url_cache: dict[tuple[str, str], dict] = {}


class HookDocumentPreviewRequest(BaseModel):
    session_id: str
    file_path: str
    filename: str = ""


@router.post("/v1/hooks/document-preview")
async def hook_document_preview(req: HookDocumentPreviewRequest,
                                 authorization: str | None = Header(None)):
    """Called by file-tools MCP to push a live Collabora preview to the chat."""
    verify_session_match(authorization, req.session_id)

    import config as cfg
    from api.media.wopi import encode_file_id, create_wopi_token

    if not cfg.COLLABORA_URL:
        # Match the loud-fail behaviour of /v1/documents/wopi-url. Without this,
        # an empty COLLABORA_URL silently produces a relative iframe src and the
        # SPA catch-all serves the dashboard's index.html — user sees the home
        # page inside the preview iframe with no clue why.
        raise HTTPException(
            status_code=503,
            detail="COLLABORA_URL is not configured — set it in config.env and restart the proxy.",
        )

    file_path, resolution = await _classify_and_pull(req.session_id, req.file_path)
    if file_path is None:
        detail = (
            resolution.error
            if resolution is not None and not resolution.allowed
            else f"File not found: {req.file_path}"
        )
        raise HTTPException(status_code=400, detail=detail)

    filename = req.filename or file_path.name

    # Compute relative path from AGENTS_DIR
    try:
        rel_path = str(file_path.resolve().relative_to(cfg.AGENTS_DIR.resolve()))
    except ValueError:
        raise HTTPException(status_code=400, detail="File must be within agents directory")

    file_id = encode_file_id(rel_path)

    # Role-gate WRITE capability on the inline Collabora token. The
    # session's authenticated SecurityContext carries the previewing human's
    # effective per-agent role + username — the same values the satellite
    # write-back guard uses. A viewer iterating on a SHARED workspace file gets a
    # view-only inline preview (can't save); an editor/manager — or ANY role on
    # their OWN users/{u}/ files — gets edit. Fail-closed to "view" when there's
    # no security context.
    #
    # The token still mints user_sub="agent" / user_name="Agent" so the existing
    # two-participant UX (the agent + the human's own workspace-tab session) is
    # unchanged — ONLY write capability is gated.
    session_meta = _sessions.get(req.session_id, {})
    user_sub = "agent"
    user_name = "Agent"
    from core.remote.file_sync import can_write_back
    sec = get_session_security(req.session_id)
    tree_rel = rel_path.partition("/")[2]  # strip "<agent>/" → agent-tree-relative
    if sec is not None and can_write_back(
        tree_rel, getattr(sec, "role", "") or "", getattr(sec, "username", "") or "",
        mount_username=getattr(sec, "mount_username", None),
    ):
        permissions = "edit"
    else:
        permissions = "view"

    # Reuse an existing WOPI URL if still valid (avoids spawning multiple
    # Collabora sessions). Keyed by permission too, so a viewer never receives a
    # cached edit token — or vice-versa — for the same shared file_id.
    cache_key = (file_id, permissions)
    cached = _wopi_url_cache.get(cache_key)
    if cached and cached["expires"] > time.time():
        wopi_url = cached["wopi_url"]
    else:
        wopi_token, token_ttl = create_wopi_token(
            rel_path, user_sub, user_name, permissions, session_meta.get("agent", "")
        )

        wopi_src = urllib.parse.quote(
            f"{cfg.WOPI_BASE_URL.rstrip('/')}/wopi/files/{file_id}",
            safe="",
        )
        wopi_url = (
            f"{cfg.COLLABORA_URL}/browser/dist/cool.html"
            f"?WOPISrc={wopi_src}"
            f"&access_token={wopi_token}"
            f"&access_token_ttl={token_ttl}"
            f"&closebutton=0&homebutton=0"
            f"&ui_defaults=UIMode%3Dcompact%3BTextSidebar%3Dfalse"
            f"%3BSpreadsheetSidebar%3Dfalse%3BPresentationSidebar%3Dfalse"
        )
        _wopi_url_cache[cache_key] = {
            "wopi_url": wopi_url,
            "expires": time.time() + 12600,  # cache for 3.5 hours (token lasts 4 hours)
        }

    # Append a timestamp so the iframe reloads on file changes (same token, new URL key)
    wopi_url_with_ts = f"{wopi_url}&_t={int(time.time())}"

    chat_id = await resolve_hook_chat_id(req.session_id) or None

    # Version-pinned snapshot: copy the file AS DELIVERED — right here, at
    # push time, before the agent can touch it again — into the proxy-private
    # snapshot cache. When a later push supersedes this preview, the dashboard
    # swaps the old block to a view-only render of this copy ("previous
    # version"). Best-effort: with no snapshot the superseded block degrades
    # to the "preview moved" chip.
    snapshot_id = ""
    generation = int(time.time() * 1000)
    if chat_id:
        from services.media import preview_snapshots
        snapshot_id = await asyncio.to_thread(
            preview_snapshots.create_snapshot, chat_id, file_path,
        ) or ""

    # Download token: durable media_tokens row (media_kind "file" →
    # attachment-forced by /v1/media's inline allowlist), so the preview's
    # download button survives restarts like the rest of the chat history.
    download_token = secrets.token_urlsafe(32)
    task_store.create_media_token(
        download_token,
        str(file_path),
        media_kind="file",
        chat_id=chat_id,
        session_id=req.session_id,
        cache_owned=False,
        expires_at="",  # durable until the chat is deleted
        agent=getattr(sec, "agent", "") or "",
    )
    # No fn= here: DocumentPreview appends it client-side from its filename
    # prop (baking it in too would duplicate the param).
    download_url = f"/v1/media/{download_token}?download=1"

    # Push to permission queue → stream pump → WS
    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": "document_preview",
        "wopi_url": wopi_url_with_ts,
        "filename": filename,
        "file_id": file_id,
        "download_url": download_url,
        "snapshot_id": snapshot_id,
        "generation": generation,
    })

    logger.info(f"Hook document-preview: session={req.session_id}, file={filename}")
    return {"status": "ok", "wopi_url": wopi_url, "file_id": file_id}


class HookToolResultRequest(BaseModel):
    session_id: str
    tool_name: str
    # Exact correlation key from the PostToolUse hook input (empty on older
    # CLIs) — lets the pump attach results to the right block when several
    # same-name tools run in parallel, and Agent reports to their task_spawn.
    tool_use_id: str = ""
    summary: str
    result_content: str = ""
    # Whether the tool call failed — the MCP cost engine skips charging for
    # failed calls (see stream_pump TOOL_RESULT handler).
    is_error: bool = False


@router.post("/v1/hooks/tool-result")
async def hook_tool_result(req: HookToolResultRequest, authorization: str | None = Header(None)):
    """Called by PostToolUse hook to push a tool result summary to the chat."""
    verify_session_match(authorization, req.session_id)
    queue = get_permission_queue(resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": "tool_result",
        "tool_name": req.tool_name,
        "tool_use_id": req.tool_use_id,
        "summary": req.summary,
        "result_content": req.result_content,
        "is_error": req.is_error,
    })
    return {"status": "ok"}


class HookStopRequest(BaseModel):
    session_id: str
    transcript_path: str = ""
    hook_event_name: str = "Stop"


@router.post("/v1/hooks/stop")
async def hook_stop(req: HookStopRequest, authorization: str | None = Header(None)):
    """Called by the Stop hook at turn end.

    For an INTERACTIVE session (PTY-backed, no pump) this is the only turn-end
    signal + transcript pointer, so the proxy reads the JSONL at
    ``transcript_path`` and appends new user/assistant messages to chat_messages.
    Headless ``-p`` sessions are persisted by the
    pump, so this no-ops for them. Never blocks the agent (fire-and-forget hook).
    """
    verify_session_match(authorization, req.session_id)
    record_hook_activity(req.session_id)

    from core.session import interactive_session
    sess = interactive_session.get(req.session_id)
    if sess is None:
        return {"status": "ok", "interactive": False}

    # Blocking file read + DB writes → off the event loop.
    from core.session import transcript_tailer
    stats = await asyncio.to_thread(
        transcript_tailer.tail_transcript,
        req.session_id, sess.chat_id, req.transcript_path,
    )
    return {"status": "ok", "interactive": True, **stats}


class HookSubagentRequest(BaseModel):
    session_id: str
    agent_id: str
    agent_type: str = ""
    hook_event_name: str = "SubagentStop"


@router.post("/v1/hooks/subagent")
async def hook_subagent(req: HookSubagentRequest, authorization: str | None = Header(None)):
    """Called by the SubagentStop hook — the deterministic, idle-safe subagent
    completion signal (foreground AND background).

    Marks the agent done in the per-session SubagentRegistry (keyed by the CLI
    ``agent_id`` == ``task_id``), then forwards a per-agent ``bg_agent_done``
    (keyed by the spawning ``tool_use_id``) so the dashboard clears that one
    widget regardless of finish order. When the cohort is complete the
    _bg_agent_monitor (awaiting the registry's event) fires the nudge — this
    handler never blocks on delivery.
    """
    verify_session_match(authorization, req.session_id)
    # SubagentStop counts as hook activity (settle's lost-Stop safety net).
    record_hook_activity(req.session_id)

    reg = get_subagent_registry(req.session_id)
    # buffer=True parks a Stop that raced ahead of its task_started; mark_done
    # returns True only on the transition to completed (dedups vs the stdout
    # task_notification backup, which may also fire for the same agent).
    if not reg.mark_done(req.agent_id, buffer=True):
        return {"status": "ok", "duplicate": True}

    tool_use_id = reg.tuid_for(req.agent_id)
    # Meeting participants resolve to the meeting's parent chat; normal
    # sessions to their own chat row, with the pump-stamped registry binding
    # as the last resort.
    chat_id = await resolve_hook_chat_id(req.session_id) or reg.chat_id or ""

    # Live state — clear this agent's widget by id (reconnect accuracy).
    if chat_id and tool_use_id:
        mark_subagent_done(chat_id, tool_use_id)

    # Per-agent WS completion. Pump path (turn still streaming → fg agents)
    # first, else the session notify queue (turn ended → bg agents, no pump).
    event = {"type": "bg_agent_done", "tool_use_id": tool_use_id}
    pushed = push_pump_event(chat_id, event) if chat_id else False
    if not pushed:
        nq = _dashboard_notify_queues.get(req.session_id)
        if nq:
            try:
                nq.put_nowait({"type": "bg_agent_done", "tool_use_id": tool_use_id})
            except Exception:
                pass
    return {"status": "ok"}


class HookFileWrittenRequest(BaseModel):
    session_id: str
    path: str


async def _fan_out_local_file_write(session_id: str, raw_path: str) -> None:
    """Propagate a LOCAL-session file-tools write to every remote satellite
    running the same agent.

    The file is already on the platform disk (the Docker MCP wrote the agent dir
    directly), so we just fan the current bytes out under the global
    per-(agent, rel) lock. Closes the gap where a local user's file-tools edit to
    a shared file never reached remote satellites (local edits emit no
    ``file_changed`` and skip ``push_back``). Best-effort — no-op if the agent /
    file can't be resolved; never raises into the hook."""
    try:
        sec = get_session_security(session_id)
        agent_slug = getattr(sec, "agent", "") if sec else ""
        rel = (raw_path or "").lstrip("/")
        if not agent_slug or not rel:
            return
        base = (config.AGENTS_DIR / agent_slug).resolve()
        host_path = (base / rel).resolve()
        try:
            host_path.relative_to(base)
        except ValueError:
            return  # path traversal — ignore
        if not host_path.is_file():
            return
        content = await asyncio.to_thread(host_path.read_bytes)
        from core.remote.remote_file_flow import _acquire_global_path_lock
        from services.remote import workspace_fanout
        lock = await _acquire_global_path_lock(agent_slug, rel)
        async with lock:
            await workspace_fanout.fan_out_write(
                agent_slug, rel, content, exclude_machine_id=None,
            )
        from services.notifications import notification_manager
        await notification_manager.broadcast_file_updated(
            agent_slug, rel, source="disk",
        )
    except Exception:
        logger.debug(
            "local file-tools fan-out skipped for session=%s",
            (session_id[:8] if session_id else "?"), exc_info=True,
        )


@router.post("/v1/hooks/file-written")
async def hook_file_written(
    req: HookFileWrittenRequest,
    authorization: str | None = Header(None),
):
    """Called by Docker MCPs (file-tools) after they write a file.

    For remote sessions, this flushes the newly-written platform-cache file
    back to the satellite so subsequent reads (from the agent CLI on the
    satellite, or from display-mcp running there, or from another Docker
    MCP) see the updated content. No-op for local sessions.

    Returns {"ok": bool} indicating whether the push to satellite succeeded.
    Docker MCPs should log but not fail on a False result — the write has
    already happened on the platform side.
    """
    verify_session_match(authorization, req.session_id)
    from core.remote import remote_file_flow
    if not remote_file_flow.is_remote_session(req.session_id):
        return {"ok": True, "local": True}
    # Satellite-host cache paths push back to the original
    # absolute path via the sidecar metadata instead of the agent-tree
    # flush. file-tools posts the agents-relative form; the proxy
    # normalizes back to absolute under AGENTS_DIR before checking.
    abs_form = req.path
    if not abs_form.startswith("/"):
        abs_form = str(config.AGENTS_DIR / abs_form)
    if remote_file_flow.is_host_cache_path(abs_form):
        ok = await remote_file_flow.push_back_host_path(
            req.session_id, abs_form,
        )
        return {"ok": bool(ok), "kind": "satellite_host"}
    rel = req.path.lstrip("/")
    # file-tools posts the SLUG-PREFIXED agents-relative form
    # ("<agent>/users/.../f.png" — the same form the resolve-path hook hands
    # back for reads), but push_back's canonical gate requires the slug-LESS
    # agent-tree rel. Fold the session's OWN slug off (never a blind strip —
    # a foreign slug stays as-is and fails the canonical gate downstream).
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel):
        ctx = get_session_security(req.session_id)
        slug = getattr(ctx, "agent", "") if ctx is not None else ""
        prefix = f"{slug}/"
        if slug and rel.startswith(prefix) and is_canonical_rel_path(rel[len(prefix):]):
            rel = rel[len(prefix):]
    ok = await remote_file_flow.push_back(req.session_id, rel)
    return {"ok": bool(ok)}


class PermissionResponseRequest(BaseModel):
    request_id: str
    approved: bool = True


@router.post("/v1/sessions/{session_id}/permission-response")
async def permission_response(
    session_id: str,
    req: PermissionResponseRequest,
    authorization: str | None = Header(None),
):
    """Called by the pipe function when the user responds to a permission dialog."""
    verify_session_match(authorization, session_id)

    # The session JWT authorizes THIS session only — it must not resolve
    # another session's pending request. (404, not 403, so a guessed
    # request_id can't be probed for existence.)
    bound_sid = get_permission_request_session(req.request_id)
    if bound_sid is not None and bound_sid != session_id:
        raise HTTPException(status_code=404, detail="No pending permission request with that ID")

    found = resolve_permission(req.request_id, req.approved)
    if not found:
        raise HTTPException(status_code=404, detail="No pending permission request with that ID")

    logger.info(f"Permission resolved: session={session_id}, request={req.request_id}, approved={req.approved}")
    return {"status": "ok"}


# --- Location bridge (MCP -> proxy -> WS -> dashboard -> WS -> proxy -> MCP) ---


@router.post("/v1/location/request")
async def request_user_location(
    authorization: str | None = Header(None),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Called by location-mcp to request the user's GPS location via the dashboard.

    Routes to the CALLER'S OWN session (resolved from its session token's
    ``sid``) so a location_request can never be pushed to — or GPS harvested
    from — a DIFFERENT user's dashboard. Only a trusted master-key caller (no
    session sid) falls back to the explicit X-Agent-Name lookup. Then pushes a
    location_request event to that session's WS and blocks for the response.
    """
    verify_api_key(authorization)

    # Resolve the caller's OWN session id from its session token. Empty for a
    # master-key caller (trusted s2s — may use the agent-name fallback below).
    from auth.session_token import validate_session_token
    _token = authorization.split(" ", 1)[1] if authorization and " " in authorization else ""
    caller_sid = (validate_session_token(_token) or {}).get("sid", "")

    if caller_sid:
        latest_sid = caller_sid
    else:
        if not x_agent_name:
            return {"error": "X-Agent-Name header required"}
        # Master-key fallback: most-recently-active non-task session for the agent.
        agent_sessions = [
            (sid, meta) for sid, meta in _sessions.items()
            if meta.get("agent") == x_agent_name and not meta.get("is_task")
        ]
        if not agent_sessions:
            return {"error": "No active dashboard session -- user may not be online"}
        dashboard_sessions = [
            (sid, meta) for sid, meta in agent_sessions if sid in _dashboard_notify_queues
        ]
        pool = dashboard_sessions if dashboard_sessions else agent_sessions
        latest_sid, _ = max(pool, key=lambda x: x[1].get("last_active", ""))

    if latest_sid not in _dashboard_notify_queues:
        return {"error": "No active dashboard session -- user may not be online"}

    # Find chat_id for this session
    chat = await asyncio.to_thread(task_store.get_chat_by_session, latest_sid)
    chat_id = chat["id"] if chat else None

    # Generate request ID and push to dashboard
    request_id = str(uuid.uuid4())
    location_event = {"type": "location_request", "request_id": request_id}

    # Try pump first (works during streaming), fallback to notify queue
    pushed = push_pump_event(chat_id, location_event) if chat_id else False
    if not pushed:
        notify_queue = _dashboard_notify_queues.get(latest_sid)
        if notify_queue:
            await notify_queue.put({"type": "location_request", "data": location_event})
        else:
            return {"error": "No active dashboard session -- user may not be online"}

    logger.info(f"Location request: session={latest_sid[:8]}, request_id={request_id}")

    # Block waiting for dashboard response
    result = await wait_for_location(request_id, timeout=30.0)

    # Optional: reverse geocode for address hint
    if result.get("lat") and not result.get("error"):
        try:
            from storage import credential_store
            maps_creds = await asyncio.to_thread(credential_store.get_infra_credentials, "google-maps")
            api_key = (maps_creds or {}).get("GOOGLE_MAPS_API_KEY", "")
            if api_key:
                async with httpx.AsyncClient(timeout=5) as gc:
                    resp = await gc.get(
                        "https://maps.googleapis.com/maps/api/geocode/json",
                        params={"latlng": f"{result['lat']},{result['lng']}", "key": api_key},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("results"):
                            result["address_hint"] = data["results"][0]["formatted_address"]
        except Exception:
            pass  # Non-critical

    return result
