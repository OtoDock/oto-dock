"""Sandbox path resolution for hooks: the resolve-path and resolve-tool-arg-paths
routes and the helpers that turn a hook's path form into a host path
(``_classify_and_pull``, ``_sandbox_to_host``, ``_session_scope_root``).

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import re

import config
from auth.path_policy import (
    SecurityContext,
    enforce_agent_tree_rbac,
    check_host_path_access,
)
from api.sessions.sessions import verify_session_match_async
from core.session.session_state import (
    get_session_security,
)
from auth import roles
from core import layout

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
    await verify_session_match_async(authorization, req.session_id)

    ctx = get_session_security(req.session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Session not found")

    raw = req.path
    agent_dir = config.AGENTS_DIR / ctx.agent

    # Model-echoed DISPLAY paths: tool results print agents-relative forms
    # ("<slug>/users/<u>/workspace/x.png") and models echo them back as
    # inputs; classified as "relative" they anchor at /workspace/<display>,
    # double the slug and 403. A slash-less path whose FIRST segment EQUALS
    # this session's agent slug (never merely "a known slug" — that would
    # admit cross-agent display paths) and which resolves to an existing
    # file CONTAINED in this agent's tree (the containment check is what
    # stops <slug>/../other-agent/… and <slug>/../../etc/passwd) is
    # admitted as agents-relative — READS ONLY: a write target would
    # silently create an agents/<slug>/<slug>/ tree. RBAC rides on the
    # FINAL host path via check_host_path_access — NOT
    # enforce_agent_tree_rbac, whose _translate_sandbox_path re-anchoring
    # would validate a different path than the one returned.
    if (
        not req.writing
        and raw
        and "\x00" not in raw
        and not raw.startswith("/")
        and raw.split("/", 1)[0] == ctx.agent
    ):
        candidate = (config.AGENTS_DIR / raw).resolve()
        try:
            contained = candidate.is_relative_to(agent_dir.resolve())
        except (OSError, ValueError):
            contained = False
        if contained and candidate.is_file():
            acc = check_host_path_access(candidate, ctx, writing=False)
            if not acc.allowed:
                raise HTTPException(status_code=403, detail=acc.reason or "access denied")
            return _answer(str(candidate), ctx)

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
        # The cache lives under AGENTS_DIR/.remote-host-cache/ so the
        # Docker MCP's /agents mount resolves it. Returning the
        # agents-relative form lets file-tools' existing
        # `MOUNT_AGENTS_DIR + agents_rel` logic work unchanged — no
        # MCP-side code change needed for satellite-host paths.
        return _answer(str(cached), ctx)

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
            return _answer(str(cached), ctx)
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
                return _answer(str(create_host), ctx)

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
    return _answer(host_path, ctx)


def _answer(host_path: str, ctx) -> dict:
    """The hook's answer: the host path, its agents-relative form and the
    agent whose folder it sits in (the session's own, or the host-cache
    pseudo-agent for a satellite-host pull), which the consumer confines
    its writes to. Judged on the host path the hook itself joined, never
    on the caller's string."""
    from core.remote import remote_file_flow
    agent = (
        ".remote-host-cache" if remote_file_flow.is_host_cache_path(host_path)
        else ctx.agent
    )
    return {
        "host_path": host_path,
        "agents_relative": _to_agents_relative(host_path),
        "agent": agent,
    }


# (removed: _looks_sandbox_virtual — the resolve-path policy-reject branch now
# 403s on ANY denied resolution, so its sandbox-virtual carve-out is obsolete.)


def _session_scope_root(ctx: SecurityContext, agent_dir: Path) -> Path:
    """The session's own workspace on the host — the default save dir and
    the anchor for relative paths: an external caller's tree, else the MOUNT
    user's workspace, else the shared one."""
    from core.session.external_identity import external_home_of
    ext_home = external_home_of(ctx)
    if ext_home:
        return Path(ext_home) / layout.WORKSPACE
    return layout.workspace_dir(agent_dir, ctx.mount_username)


_SLASH_RUN_RE = re.compile(r"/{2,}")


def _sandbox_to_host(sandbox_path: str, ctx: SecurityContext, agent_dir: Path) -> str:
    """Map a sandbox-internal path to a host-absolute path.

    Every per-user expansion keys on ``ctx.mount_username`` — the MOUNT
    identity ("" for agent-scope mounts, including Shared-only human chats,
    whose ``ctx.username`` stays set for attribution). Keying on the raw
    username misdirected Shared-only sessions' paths into per-user dirs
    that their mode doesn't even mount (found live 2026-07-10).

    A run of slashes is collapsed first: the ``Path`` joins below slice the
    root off (``p[11:]``), and ``pathlib`` restarts a join at an absolute
    component — ``/workspace//etc/passwd`` mapped to ``/etc/passwd`` while
    the policy verdict before it had judged the collapsed form (core-seams
    phase 10, audit A1)."""
    p = _SLASH_RUN_RE.sub("/", sandbox_path)

    # External caller with a private tree (SecurityContext.external_home):
    # /caller, /.claude, /.codex and /context ARE that tree, and a viewer's
    # /workspace redirects there too (the same asymmetry as the user viewer
    # below). RBAC re-gates every result (check_host_path_access), so an
    # editor/manager caller's /workspace still lands in the shared one.
    from core.session.external_identity import external_home_of
    ext_home = external_home_of(ctx)
    if ext_home:
        home = Path(ext_home)
        if p == "/caller" or p.startswith("/caller/"):
            rest = p[len("/caller"):].lstrip("/")
            return str(home / rest) if rest else str(home)
        if p.startswith("/.claude/") or p == "/.claude":
            return str(home / ".claude" / p[9:])
        if p.startswith("/.codex/") or p == "/.codex":
            return str(home / ".codex" / p[8:])
        if p.startswith("/context/") or p == "/context":
            return str(home / layout.CONTEXT / p[9:])
        if ctx.role == roles.VIEWER and layout.under(p, layout.V_WORKSPACE):
            return str(home / layout.WORKSPACE / p[11:])

    # /.claude/ → session's .claude/ dir
    if p.startswith("/.claude/") or p == "/.claude":
        if ctx.mount_username:
            return str(layout.user_dir(agent_dir, ctx.mount_username) / ".claude" / p[9:])
        return str(agent_dir / layout.WORKSPACE / ".claude" / p[9:])

    # Viewer redirect: below the workspace tier, Docker MCPs that say
    # `/workspace/foo` mean THEIR personal workspace (matches their
    # `OTO_WORKSPACE_DIR = /users/{u}/workspace` for stdio MCPs). The
    # viewer's bwrap mount ALSO exposes the shared `/workspace` RO (expanded
    # viewer reads), so stdio Read sees shared content, but Docker MCPs see
    # per-user — this is the intentional asymmetry: writes via Docker MCPs
    # land in the user's own dir, not the shared workspace.
    if not roles.can_write_workspace(ctx.role) and ctx.mount_username:
        if layout.under(p, layout.V_WORKSPACE):
            return str(layout.workspace_dir(agent_dir, ctx.mount_username) / p[11:])
        if p.startswith("/context/") or p == "/context":
            return str(layout.context_dir(agent_dir, ctx.mount_username) / p[9:])

    # Editor / Manager / Admin: /workspace/ → shared workspace.
    # /config/ is owner-only — editor's bwrap doesn't mount it
    # and path_policy denies their reads. If a Docker MCP somehow sends
    # /config for an editor session, the host path resolves but the file
    # is owner-curated; documented residual (Docker MCPs bypass bwrap +
    # path_policy hook — same gap as `satellite has no bwrap`).
    if layout.under(p, layout.V_CONFIG):
        return str(agent_dir / layout.CONFIG / p[8:])
    if layout.under(p, layout.V_WORKSPACE):
        return str(agent_dir / layout.WORKSPACE / p[11:])

    # /users/{username}/ → users/{username}/ (a bare ``/users`` takes the
    # fallback below, as it always did)
    if p.startswith(layout.V_USERS + "/"):
        return str(agent_dir / p[1:])  # strip leading /

    # /context/ for viewer (already handled above, but safety)
    if p.startswith("/context/") or p == "/context":
        if ctx.mount_username:
            return str(layout.context_dir(agent_dir, ctx.mount_username) / p[9:])

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
    await verify_session_match_async(authorization, req.session_id)

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
        # deny. Truly outside AGENTS_DIR → deny: on the local branch below the
        # interceptor's resolve-tool-arg-paths gate is NOT wired (it's remote-
        # only), so a form-1 host path that ``_resolve_hook_path`` accepted
        # verbatim (``Path(raw).is_file()``) would otherwise be served straight
        # from the proxy host — an arbitrary-file read of anything uid 1000 can
        # reach (config.env, other users' tokens). path_policy_v2's own local
        # contract rejects every non-sandbox-virtual absolute path; mirror it.
        rh = host.resolve()
        if rh.is_relative_to(agent_root):
            return check_host_path_access(host, ctx, writing=writing).allowed
        # The session's OWN lazy-pull host cache (a Desktop/Downloads file the
        # resolve-path hook already policy-gated and pulled). Session-scoped:
        # another session's cache stays denied. Without this carve-out the
        # agent-tree confinement (c59e551f) 400'd EVERY satellite-host
        # preview — the cache lives beside, not inside, the agent trees.
        try:
            cache_root = remote_file_flow.host_cache_session_root(
                session_id,
            ).resolve()
            if rh.is_relative_to(cache_root):
                return True
        except OSError:
            pass
        return False

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
