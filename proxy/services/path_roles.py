"""Path role resolution for MCP path env vars.

MCPs declare path-bearing env vars in their manifest's ``path_env`` field,
mapping env var names to predefined role names. The framework auto-resolves
each role to a sandbox-style virtual path (e.g. ``/users/{u}/workspace``)
based on the session's user/agent scope and the user's access level
(viewer/manager/admin). The sandbox-style value is then translated to a real
filesystem path by:

- **Local sandbox**: bwrap mount namespace (already in place)
- **Remote satellite**: ``satellite/path_translator.py`` rewrites to the
  satellite's ``{agent_dir}/...`` paths before the MCP subprocess spawns

This is the single source of truth for "where should this MCP write/read
files in this session". MCP authors declare a role and never have to think
about user-vs-agent scope, viewer-vs-manager, or local-vs-remote target.

A ``path_env`` entry is one of two shapes:

- **Shorthand single-role** — the entry is ``{"role": ..., "subpath": ...}``.
- **Multi-value** — ``{"values": [{"role": ...}, ...], "join": ":"}``. Each
  entry resolves independently; empty resolutions are dropped before the
  remaining values are joined with ``join``. Used for allowlist-style env
  vars (``ALLOWED_FILE_DIRS`` etc.) that need to mirror the full set of
  bwrap mount roots accessible in this session.
"""

from __future__ import annotations

import re
from pathlib import Path

from auth.roles import OWNER_TIER, WORKSPACE_TIER
from core import layout

# Literal token left in path values for session-scoped roles. Expanded at
# subprocess-spawn time by the bwrap launcher (local) and the satellite
# path translator (remote) — both have ``session_id`` available there.
SESSION_ID_TOKEN = "{session_id}"


# ---------------------------------------------------------------------------
# OAuth token protection
# ---------------------------------------------------------------------------

def is_protected_credentials_path(path: Path | str) -> bool:
    """Return True if any segment of ``path`` is a registered
    ``credentials_dir`` subpath.

    Used by both the dashboard file API and the agent permission hook to
    refuse access to OAuth token files (``users/{u}/google-tokens/x.json``,
    ``workspace/google-tokens/x.json``, future ``<provider>-tokens/...``).

    Protection is manifest-driven via
    ``mcp_registry.get_protected_credentials_subpaths()`` — a future MCP
    that declares ``path_env.X.role: "credentials_dir"`` automatically
    inherits the gate, no code change required.

    Robust against:
      * Relative paths (split on '/' OS-independently via ``pathlib``)
      * Absolute paths
      * Path traversal — caller should ``.resolve()`` first when comparing
        against a known-trusted prefix; this function just inspects the
        segments as given.
      * Empty / falsy input (returns False)
      * Lookalike folder names — only EXACT subpath matches count, so
        ``my-design-tokens/`` is safe while ``google-tokens/`` is
        protected. The set is the manifest-declared subpaths, not a
        glob pattern.
    """
    if not path:
        return False
    # Lazy import: mcp_registry imports from this module, would cycle.
    from services.mcp import mcp_registry

    protected = mcp_registry.get_protected_credentials_subpaths()
    if not protected:
        return False
    try:
        parts = Path(str(path)).parts
    except (TypeError, ValueError):
        return False
    return any(part in protected for part in parts)


def command_references_protected_path(command: str) -> bool:
    """Substring-match a bash command for known credentials_dir subpaths.

    Catches the common ``cat /workspace/google-tokens/x.json`` /
    ``cp .../google-tokens/...`` shapes used in prompt-injection
    payloads. Does NOT defeat variable-indirection obfuscation
    (``X=google-tokens; cat $X/x.json``) — accepted residual; a
    determined attacker with full bash already lives inside the
    agent's bwrap and could exfiltrate via legitimate OAuth tool
    calls anyway.

    Boundaries: each subpath must appear preceded by a non-word
    boundary (``/`` ``\\`` ``"`` ``'`` whitespace or start) so a folder
    legitimately named ``my-google-tokens-stuff`` (substring match)
    doesn't trigger.
    """
    if not command:
        return False
    from services.mcp import mcp_registry
    protected = mcp_registry.get_protected_credentials_subpaths()
    if not protected:
        return False
    import re as _re
    for sub in protected:
        # Match the subpath as a complete path component: preceded by
        # path-segment-or-quote boundary, followed by '/' or end-of-arg.
        pattern = (
            r'(?:^|[\s/\\"\'=])'  # boundary before
            + _re.escape(sub)
            + r'(?:/|$|[\s"\']\s*)'  # boundary after
        )
        if _re.search(pattern, command):
            return True
    return False


# ---------------------------------------------------------------------------
# Agent-config protection — the agent's OWN session config files
# ---------------------------------------------------------------------------

# The platform's own hook and interceptor scripts, copied into every
# session's ``.claude`` / ``.codex`` dir, executable, and run on every tool
# call: writable, they would let an agent remove the permission authority
# for the rest of the session (no bwrap behind it on a satellite).
_HOOK_SCRIPTS = frozenset({
    "permission_gate.py", "tool_result_forwarder.py", "subagent_tracker.py",
    "stop_tracker.py", "stdio_path_interceptor.py",
})
_CODEX_SECRET_FILES = frozenset({"config.toml", "auth.json"})


def is_protected_agent_config_path(path: Path | str, *, writing: bool = False) -> bool:
    """True if ``path`` is a platform-generated CLI config file carrying THIS
    session's own secrets — the broker capability token (``OTO_MCP_FETCH_TOKEN``),
    the swapped-in HTTP bearer (local), the session JWT (``PROXY_API_KEY``),
    the Codex model token (``auth.json``), and instance-config field values —
    or, for a WRITE, one of the files the permission system itself runs from.

    Matched: ``.claude/*.json`` and ``.codex/{config.toml,auth.json}`` located at
    a session SCOPE ROOT — i.e. the ``.claude``/``.codex`` dir sits directly under
    ``users/<u>/``, ``workspace/`` or ``knowledge/`` (where the platform points
    ``CLAUDE_CONFIG_DIR``/``CODEX_HOME``), or directly under ``/tmp``, the
    sandbox's ``HOME`` (``/tmp/.claude.json`` included), in case a CLI ever
    falls back to it. With ``writing`` the set widens to the scope-root dir
    itself and EVERY direct child of it, plus a package's ``__init__`` one
    level down: the hook scripts run as ``python3 <dir>/permission_gate.py``,
    which puts ``<dir>`` first on ``sys.path``, so a planted ``json.py`` (or
    ``json/__init__.py``) there would run inside the gate; and a directive
    file there (``CLAUDE.md``, ``AGENTS.md``) is read by every session the
    dir configures. Everything under a scope-root ``.claude/dev-mods`` (or
    ``.codex/dev-mods``: the rule needs no engine branch) is a write too: Claude Code 2.1.287+ loads a mod Claude writes there after a
    hot-reload prompt, and a mod's handlers run ahead of the platform's
    gate. A ``.claude``/``.codex``
    nested deeper (e.g. ``workspace/<repo>/.claude/settings.json``) is NOT
    matched, so an agent working on a repo that itself uses Claude Code /
    Codex can still read and edit that repo's config — those files hold none
    of the session's secrets and run none of its hooks.

    Prompt-injection / casual-read defense: blocks "read your own config
    and paste the token in chat" plus accidental reads via Read / cat / grep /
    MCP path-args. NOT a malicious-MCP boundary — a native same-uid MCP can
    ``open()`` the file directly, unmediated by path policy (everything in
    one session shares a single trust domain).

    Handles Path or str (host, sandbox-virtual, or satellite-absolute form);
    callers should ``.resolve()`` host paths first.
    """
    if not path:
        return False
    try:
        parts = Path(str(path)).parts
    except (TypeError, ValueError):
        return False
    if len(parts) < 2:
        return False
    dotdir, name = parts[-2], parts[-1].lower()
    if dotdir == "tmp" and name == ".claude.json":
        return len(parts) == 3 and parts[0] == "/"
    if writing:
        if parts[-1] in _STATE_DIRS:
            return _at_scope_root(parts[:-1])
        for i in range(len(parts) - 2):
            if (parts[i] in _STATE_DIRS and parts[i + 1] == _DEV_MODS_DIR
                    and _at_scope_root(parts[:i])):
                return True
        if (len(parts) >= 3 and parts[-3] in _STATE_DIRS and name.startswith("__init__.")
                and _at_scope_root(parts[:-3])):
            return True
        return dotdir in _STATE_DIRS and _at_scope_root(parts[:-2])
    if dotdir == ".claude":
        if not name.endswith(".json"):
            return False
    elif dotdir == ".codex":
        if name not in _CODEX_SECRET_FILES:
            return False
    else:
        return False
    return _at_scope_root(parts[:-2])


_STATE_DIRS = (".claude", ".codex")
# Where Claude Code 2.1.287+ keeps the mods Claude writes during a session
# (judged under either state dir: the rule needs no engine branch).
_DEV_MODS_DIR = "dev-mods"


def _at_scope_root(before: tuple[str, ...]) -> bool:
    """Whether a ``.claude``/``.codex`` dir under ``before`` is a session's
    own (a scope root), not one nested in a repo under workspace/ (a
    third-party config, secret-free)."""
    if not before:
        return False
    if before[-1] in (layout.WORKSPACE, layout.KNOWLEDGE):
        return True
    if len(before) >= 2 and before[-2] == layout.USERS:  # users/<username>/.<dir>
        return True
    if before == ("/", "tmp"):  # the sandbox HOME
        return True
    # An external caller's tree — CLAUDE_CONFIG_DIR / CODEX_HOME of a phone
    # session that is not a platform user: sandbox-virtual ``/caller/.<dir>``,
    # host ``externals/<channel>/<id>/.<dir>`` or the ephemeral
    # ``externals/<channel>/_ephemeral/<session>/.<dir>``.
    if before[-1] == "caller" and len(before) <= 2:
        return True
    if "externals" in before:
        rest = before[before.index("externals") + 1:]
        if len(rest) in (2, 3):
            return True
    return False


def in_session_state_dir(path: Path | str) -> bool:
    """True for a scope-root ``.claude``/``.codex`` dir or anything under it:
    the engines' own state (the subscription login, the MCP config with the
    session token, the hook scripts), which the files API serves to nobody."""
    if not path:
        return False
    parts = Path(str(path)).parts
    return any(part in _STATE_DIRS and _at_scope_root(parts[:i])
               for i, part in enumerate(parts))


_BG_PATH_SPLIT = re.compile(r"[\\/]+")


def is_claude_bg_output_path(path: Path | str, session_id: str) -> bool:
    """True for THIS session's Claude Code CLI background-command output file.

    The CLI writes ``run_in_background`` Bash output to
    ``$HOME/claude-<uid>/<cwd-hash>/<session>/tasks/<id>.output``. Under the
    sandbox's ``HOME=/tmp`` that's ``/tmp/claude-1000/.../tasks/<id>.output``
    (Linux); on a satellite it's ``…/AppData/Local/Temp/claude/…/tasks/<id>.output``
    (Windows) or the macOS ``$TMPDIR`` equivalent. Structural match: a
    ``claude``/``claude-*`` segment, then ``session_id`` as a later segment,
    then a ``tasks`` segment, and a ``.output`` suffix, so it holds across
    OSes (and raw vs forward-slash forms). The session-id equality is the
    capability, as in ``is_session_runtime_path``: another session on the
    same host (another platform user on a shared-admin satellite) can never
    name this session's UUID, so the carve admits only the caller's own
    ephemeral task output. An empty id matches nothing. Callers MUST still
    run the credential / agent-config / cross-user denies first; this only
    widens, never narrows.
    """
    sid = (session_id or "").lower()
    if not sid:
        return False
    parts = [p.lower() for p in _BG_PATH_SPLIT.split(str(path)) if p]
    if not parts or not parts[-1].endswith(".output"):
        return False
    try:
        claude_i = next(i for i, p in enumerate(parts)
                        if p == "claude" or p.startswith("claude-"))
        sid_i = parts.index(sid, claude_i + 1)
    except (StopIteration, ValueError):
        return False
    return "tasks" in parts[sid_i + 1:-1]


def is_session_runtime_path(
    path: Path | str, root: str, session_id: str, *,
    case_insensitive: bool = False,
) -> bool:
    """True when ``path`` lies inside THIS session's Claude-CLI runtime tree.

    The tree is ``<root>/<cwd-slug>/<session_id>/…`` — scratchpad and
    background-task outputs. ``root`` is the CLI's per-user runtime root
    (``<tempdir>/claude-<uid>``: reported by a satellite's capabilities
    probe, or ``core.sandbox.sandbox.claude_runtime_root()`` for a local
    sandbox) and ``session_id`` must appear as a path segment below it —
    the root itself is not the tree. The session-id equality is the
    capability: another session (another platform user on a shared-admin
    satellite, another sandbox on the proxy) can never name this session's
    UUID. Forward- and back-slash forms match; ``case_insensitive`` is for
    Windows / macOS roots. Widening-only — callers MUST run the credential /
    agent-config / cross-user / ``.env`` denies first.
    """
    if not root or not session_id:
        return False
    c = str(path).replace("\\", "/").rstrip("/")
    p = str(root).replace("\\", "/").rstrip("/")
    if case_insensitive:
        c, p = c.lower(), p.lower()
    if not p or not (c == p or c.startswith(p + "/")):
        return False
    rel = c[len(p):].strip("/")
    if not rel:
        return False
    return session_id.lower() in rel.split("/")


# Scope-root-anchored match for the raw-command backstop below — same boundary
# as ``is_protected_agent_config_path`` (a repo's nested ``.claude``/``.codex``
# config is NOT matched), so the two never diverge. The raw text cannot tell
# a read from a write, so it carries the WRITE set: a Bash reference to a
# hook script is refused in both directions.
_HOOK_SCRIPTS_ALT = "|".join(re.escape(s) for s in sorted(_HOOK_SCRIPTS))
_AGENT_CONFIG_CMD_RE = re.compile(
    r'(?:^|[\s/\\"\'=])'                                   # boundary before
    r'(?:(?:users/[^/\s"\']+|workspace|knowledge|tmp)/'    # session scope root
    r'\.(?:claude/(?:[^/\s"\']*\.json|dev-mods(?:/[^\s"\';|&)<>]*)?|' + _HOOK_SCRIPTS_ALT + r')'
    r'|codex/(?:config\.toml|auth\.json|hooks\.json|dev-mods(?:/[^\s"\';|&)<>]*)?|' + _HOOK_SCRIPTS_ALT + r'))'
    r'|tmp/\.claude\.json)'                                # the sandbox HOME's own
    r'(?:$|[\s"\';|&)<>])'                                 # boundary after
)


def command_references_protected_agent_config(command: str) -> bool:
    """Raw-text backstop for ``is_protected_agent_config_path``: catch a
    bash command referencing the agent's own scope-root CLI config
    (``users/<u>/.claude/*.json``, ``workspace/.codex/config.toml``, …) or
    the hook scripts and settings the permission system runs from BEFORE
    the admin-on-admin-agent bash fast-path, so the deny is universal —
    mirrors the credentials ``command_references_protected_path`` placement.
    This matches on the RAW command BEFORE any unwrap, so a literal reference
    wrapped in ``bash -c "cat .codex/auth.json"`` / ``$(cat …/auth.json)`` is
    still caught (the path is a substring of the raw command). The accepted
    residual is STRING-ASSEMBLY indirection only (``X=.claude; cat /users/u/$X/
    c.json`` / ``$(printf …)``-assembled paths), which no static regex can
    catch. The exec-env command policy no longer hard-blocks ``eval``/``bash -c``
    (it unwraps + re-checks them), so the boundary here is the raw-string
    regex + the single-trust-domain contract, NOT an allowlist that forbids
    indirection primitives. An ``open()`` from an interpreter (``python3 -c``)
    was always an unguarded indirection channel — the residual is unchanged."""
    if not command:
        return False
    return bool(_AGENT_CONFIG_CMD_RE.search(command))


# Public role names. Every manifest path_env entry must reference one of
# these. The list is intentionally small — keeping community MCPs aligned
# with platform conventions and avoiding ad-hoc paths.
ROLES = (
    "workspace",
    "user_root",
    "shared_workspace",
    "config",
    "knowledge_dir",
    "credentials_dir",
)


# Access levels recognized when resolving role-gated paths.
# ``""`` denotes either an agent-scoped session (no user) or an unknown
# access level — both treated restrictively.
#
# Two distinct tiers (`/config/` is OWNER-ONLY, but `/workspace/`
# is collaborative).
#   - ``WORKSPACE_TIER`` (contributor+editor+manager+admin) gates ``shared_workspace`` —
#     they need the path rendered so MCPs writing to /workspace/ work.
#   - ``OWNER_TIER`` (manager+admin) gates ``config`` — editor/viewer
#     get empty (no /config/ in their session at all).
# Both are the authority's (``auth/roles``).

# Where an external caller's private tree is mounted (one fixed name — a
# session only ever sees its own caller). Owned by external_identity.
_EXTERNAL_HOME = "/caller"


def resolve_role(
    role: str,
    *,
    username: str = "",
    user_role: str = "",
    subpath: str = "",
    force_config: bool = False,
    external: bool = False,
) -> str:
    """Resolve a role name (+optional subpath) to a sandbox-style virtual path.

    ``external`` = an external caller's session WITH a private tree mounted
    at /caller (``core/session/external_identity.py``): ``workspace`` and
    ``user_root`` resolve there, ``shared_workspace`` follows the route role
    like a user session, ``config`` is never offered, credentials stay
    agent-scope. ``username`` is "" on such sessions.

    The return value contains the literal token ``{session_id}`` for
    session-scoped roles; the caller (bwrap launcher or satellite path
    translator) is responsible for expanding it at process-spawn time.

    May return an empty string when the role does not apply to the current
    session (e.g. ``config`` for a viewer, ``user_root`` for an agent-scoped
    task). Callers using multi-value ``path_env`` drop empty values before
    joining; callers using shorthand single-role entries get the empty
    string as-is and SHOULD treat it as "this MCP cannot be configured for
    this session" — but the expectation is that MCPs match a role to their
    scope correctly via the manifest.

    Args:
        role: one of ``ROLES``.
        username: session's username; empty string for agent-scoped sessions
            (tasks/meetings/phone).
        user_role: the user's access level (``"viewer"``, ``"manager"``,
            ``"admin"``, or ``""`` for agent-scoped / unknown). Only the
            ``shared_workspace`` and ``config`` roles consult this — the
            other roles ignore it.
        subpath: required for ``credentials_dir``. Optional for
            ``workspace``, ``user_root``, ``shared_workspace`` — appended
            under the role's directory when set. Ignored for ``config``.

    Returns:
        Sandbox-style virtual path (always absolute, leading ``/``) or ``""``.

    Raises:
        ValueError if ``role`` is unknown or ``credentials_dir`` is given
        without a subpath.
    """
    # Subpath is optional for workspace/user_root/shared_workspace (subdir
    # within the role's directory). Required for credentials_dir.
    # Ignored for config.
    sp = subpath.lstrip("/") if subpath else ""

    if role == "workspace":
        if external:
            base = f"{_EXTERNAL_HOME}/{layout.WORKSPACE}"
        else:
            base = layout.virtual_workspace(username)
        return f"{base}/{sp}" if sp else base

    if role == "user_root":
        # User-scoped sessions: the user's own dir (under which workspace,
        # context, .keys, etc. live). An external caller's tree plays the
        # same part. Agent-scoped sessions have no user dir; return empty so
        # multi-value callers drop the entry.
        if external:
            base = _EXTERNAL_HOME
        elif not username:
            return ""
        else:
            base = layout.virtual_user_root(username)
        return f"{base}/{sp}" if sp else base

    if role == "shared_workspace":
        # The agent-shared workspace mount. Available to:
        #   - workspace-tier user-scoped sessions (mounted alongside their
        #     own user dir)
        #   - agent-scoped sessions (the only workspace they have)
        #   - external callers with a private tree, per the route role
        # Viewer user-scoped / viewer external: no access; return empty.
        if external:
            if user_role not in WORKSPACE_TIER:
                return ""
            base = layout.V_WORKSPACE
        elif not username:
            base = layout.V_WORKSPACE  # agent-scoped
        elif user_role in WORKSPACE_TIER:
            base = layout.V_WORKSPACE
        else:
            return ""
        return f"{base}/{sp}" if sp else base

    if role == "config":
        if external:
            return ""  # never an owner-tier surface for a caller
        # Agent config dir. OWNER-only (manager/admin sessions). Editor +
        # viewer get empty — config is not mounted in their bwrap, not visible
        # in their dashboard tree. config shapes agent BEHAVIOR (prompt, MCP
        # wiring, auto-loaded context) which is owner curation.
        #
        # ``force_config`` is the shared-only path: an owner-tier human chatting
        # with a Shared-only agent mounts in the AGENT scope (``username==""``
        # here), but is still the manager and DOES get /config. The visibility
        # resolver passes ``force_config=config_visible`` so this stays in
        # lock-step with the bwrap mount.
        if force_config or (username and user_role in OWNER_TIER):
            return layout.V_CONFIG
        return ""

    if role == "knowledge_dir":
        # Agent knowledge dir. Central reference library curated by owners
        # (RW for manager/admin; RO for editor/viewer; RO for agent-scope
        # sessions). Universally available — unlike workspace, knowledge
        # reads from the SAME dir regardless of session scope.
        return f"{layout.V_KNOWLEDGE}/{sp}" if sp else layout.V_KNOWLEDGE

    if role == "credentials_dir":
        if not sp:
            raise ValueError("credentials_dir role requires a non-empty subpath")
        # Per-session OAuth token copy destination. Lives under
        # ``.credentials/`` so the file API + workspace tree can hide the
        # whole subtree cleanly + so adding any future privacy-sensitive
        # per-session asset has an obvious home.
        # - User-scope sessions: under the user's own dir.
        # - Agent-scope sessions (phone/task/trigger): under the agent's
        #   knowledge dir (universally visible to agent-scope sessions of
        #   this agent; never visible to user-scope sessions).
        if username:
            return f"{layout.virtual_user_root(username)}/{layout.CREDENTIALS_DIR}/{sp}"
        return f"{layout.V_KNOWLEDGE}/{layout.CREDENTIALS_DIR}/{sp}"

    raise ValueError(f"Unknown path role: {role!r}; valid roles: {ROLES}")


def resolve_path_env_entry(
    decl,
    *,
    username: str = "",
    user_role: str = "",
) -> str:
    """Resolve a single ``path_env`` entry (shorthand or multi-value) to a string.

    For shorthand entries (``decl.role`` set), this returns the role's
    resolved path verbatim — possibly the empty string if the role doesn't
    apply to the session.

    For multi-value entries (``decl.values`` non-empty), each item resolves
    independently; empty resolutions are dropped before the remaining items
    are joined with ``decl.join``. If every item resolves empty, the entry
    resolves to the empty string.

    Args:
        decl: a ``PathEnvDecl`` (or dict-shaped raw manifest data).
        username: session's username; empty for agent-scoped sessions.
        user_role: access level; see ``resolve_role``.

    Returns:
        Resolved env var value, possibly empty.
    """
    # Accept either the PathEnvDecl dataclass (production) or a raw dict
    # (tests, ad-hoc). NOTE: don't use hasattr(decl, "values") to distinguish
    # — every dict has a `.values()` method, which would falsely resolve to
    # the builtin instead of a missing values key. Dispatch on isinstance.
    is_dict = isinstance(decl, dict)

    if is_dict:
        raw_values = decl.get("values") or []
    else:
        raw_values = getattr(decl, "values", None) or []

    if raw_values:
        join = decl.get("join") if is_dict else getattr(decl, "join", None)
        if not join:
            join = ":"
        resolved: list[str] = []
        for item in raw_values:
            if isinstance(item, dict):
                item_role = item.get("role", "")
                item_subpath = item.get("subpath", "")
            else:
                item_role = getattr(item, "role", "")
                item_subpath = getattr(item, "subpath", "")
            if not item_role:
                continue
            value = resolve_role(
                item_role,
                username=username,
                user_role=user_role,
                subpath=item_subpath,
            )
            if value:
                resolved.append(value)
        return join.join(resolved)

    if is_dict:
        role = decl.get("role", "")
        subpath = decl.get("subpath", "")
    else:
        role = getattr(decl, "role", "")
        subpath = getattr(decl, "subpath", "")
    if not role:
        return ""
    return resolve_role(
        role, username=username, user_role=user_role, subpath=subpath,
    )


def resolve_path_env(
    path_env: dict,
    *,
    username: str = "",
    user_role: str = "",
) -> dict[str, str]:
    """Resolve every entry in a manifest's ``path_env`` field to env var values.

    Empty resolutions (e.g. ``shared_workspace`` for a viewer) are still
    included as empty-string env vars so the MCP can detect "this scope has
    no value for X" and react accordingly. If you want only non-empty
    values, filter the result.

    Args:
        path_env: dict mapping env var name → ``PathEnvDecl`` (or a raw
            manifest dict).
        username: session's username; empty for agent-scoped sessions.
        user_role: access level (``"viewer"``/``"manager"``/``"admin"``/``""``).

    Returns:
        ``{env_var_name: virtual_path_or_empty}`` ready to inject into MCP env.
        Values may contain the ``{session_id}`` literal token.
    """
    out: dict[str, str] = {}
    for env_var, decl in path_env.items():
        out[env_var] = resolve_path_env_entry(
            decl, username=username, user_role=user_role,
        )
    return out


def get_multi_value_envs(path_env: dict) -> dict[str, str]:
    """Return ``{env_var: separator}`` for every multi-value path_env entry.

    Used by the proxy's start_session payload to tell the satellite which
    env vars to split during translation. See
    ``satellite/path_translator.py::translate_env``.
    """
    out: dict[str, str] = {}
    for env_var, decl in path_env.items():
        if isinstance(decl, dict):
            raw_values = decl.get("values") or []
            if not raw_values:
                continue
            join = decl.get("join") or ":"
        else:
            raw_values = getattr(decl, "values", None) or []
            if not raw_values:
                continue
            join = getattr(decl, "join", None) or ":"
        out[env_var] = join
    return out


def expand_session_id(value: str, session_id: str) -> str:
    """Expand the ``{session_id}`` literal token in a resolved path value.

    Called by:
      - the local bwrap launcher when preparing the agent's session env
      - ``satellite/path_translator.py`` when preparing satellite-spawned MCP env

    Idempotent if the token isn't present.
    """
    if SESSION_ID_TOKEN in value:
        return value.replace(SESSION_ID_TOKEN, session_id)
    return value


def expand_session_id_in_env(env: dict[str, str], session_id: str) -> dict[str, str]:
    """Apply ``expand_session_id`` to every value in an env dict.

    Returns a NEW dict (does not mutate the input). Non-string values are
    passed through unchanged.
    """
    out = dict(env)
    for key, value in list(out.items()):
        if isinstance(value, str):
            out[key] = expand_session_id(value, session_id)
    return out
