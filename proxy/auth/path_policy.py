"""Role-based file access policy for the permission hook.

Validates tool calls against a session's SecurityContext (role, username, agent).
Called by the permission hook endpoint (api/hooks/hooks.py) BEFORE the existing
mode-based logic.

Two-pass gate:
  Pass 1 (this module): path allowed for role+agent? → DENY or CONTINUE
  Pass 2 (existing):    mode-based logic (default/acceptEdits/plan/dontAsk)
"""

from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path

import config


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityContext:
    """Immutable security context for a session."""

    role: str  # per-agent effective role: "admin" | "manager" | "editor" | "viewer"
    username: str  # filesystem-safe slug, or "" for tasks/phone without user
    agent: str  # current agent name
    is_admin_agent: bool  # True if agent is in HIGH_CLEARANCE_AGENTS
    display_name: str = ""  # user's full display name
    email: str = ""  # user's email
    # Execution-target metadata (environment-aware prompts + bash tier
    # gating). target_kind drives both the prompt's
    # ``# Execution Environment`` section AND admin-tier bash gating:
    # remote satellites open the admin tier to manager/editor because the
    # admin/user already trusted the agent at pairing time.
    #   - ``"local"`` — bwrap sandbox on the platform host (default)
    #   - ``"admin_remote"`` — admin-paired remote satellite
    #   - ``"user_remote"`` — user-paired remote satellite (user's own
    #     hardware; user-scope sessions only)
    target_kind: str = "local"
    target_label: str = ""  # human-readable machine name (empty when local)
    # Satellite's local agent-tree root, e.g.
    # ``C:/Users/alice/OtoDock/agents`` (Windows) or
    # ``/home/alice/.oto-dock/agents`` (Linux/macOS). Empty for local
    # sessions. Set by config_builder from the machine's reported
    # capabilities so we can decide whether a satellite-absolute path
    # is "inside the synced tree" (apply RBAC) or "elsewhere on the
    # satellite host" (allow under the home/full-FS policy).
    target_agents_dir: str = ""
    # Satellite-host path-policy fields. All empty for local
    # sessions; populated by config_builder for remote sessions.
    #   target_machine_id    — for mid-session revocation detection
    #   target_home_dir      — admits paths under the OS user's home dir
    #                          when allow_full_fs is False
    #   target_allow_full_fs — when True, the policy admits any path
    #                          (system files, other dirs, etc.); when
    #                          False, only sandbox-virtual + home are
    #                          admitted on user-paired satellites
    target_machine_id: str = ""
    target_home_dir: str = ""
    target_allow_full_fs: bool = False
    # otodock-CLI: extra absolute satellite-host roots this ONE session may
    # read/write, beyond the home/sandbox/full-fs matrix — the session's own
    # arbitrary cwd subtree (the folder the user ran `otodock` in). Per-session
    # (never a module global → no cross-session leak), realpath-normalized at
    # build time. Empty for every normal session.
    session_allowed_roots: tuple = ()
    # otodock-CLI: the session's actual working directory on the satellite
    # (the folder the user ran `otodock` in; realpath-normalized by the
    # client, persisted on the chat row). When set, the path policy anchors
    # RELATIVE tool-arg paths here instead of the sandbox /workspace
    # convention — resolve-then-check, so the collapsed absolute target
    # still passes the same roots/home/RBAC admission. Empty for every
    # normal session (dashboard-spawned sessions keep the workspace anchor).
    work_cwd: str = ""
    # The Claude Code CLI's per-user runtime root on the satellite
    # (``<tempdir>/claude-<uid>``), reported concretely in the capabilities
    # probe — scratchpad + background-task outputs live under
    # ``<root>/<cwd-slug>/<session-id>/``. Together with ``cli_session_id``
    # it admits the session's OWN runtime tree even with allow_full_fs off.
    # Empty for local sessions (bwrap gives them a private tmpfs /tmp) and
    # for satellites that haven't reported the capability (fail closed).
    target_claude_runtime_root: str = ""
    # This session's CLI session id (== chats.session_id, the value spawned
    # via --session-id/--resume). Stamped centrally by
    # ``session_state.set_session_security`` when the registration key is a
    # UUID — empty disables the runtime-tree carve. Used ONLY to scope that
    # carve to the session's own subtree.
    cli_session_id: str = ""
    # OS user identity on the satellite (a different namespace from the
    # platform ``username``). Empty for local sessions.
    target_os_user: str = ""
    # Well-known user folders reported in the satellite's capabilities
    # probe (XDG on Linux, fixed layout on macOS, Known Folders on
    # Windows). Empty dict for local sessions. Keys: desktop, downloads,
    # documents, pictures, music, videos.
    target_user_dirs: dict = field(default_factory=dict)
    # Device-control consent set granted to this satellite: the capability
    # keys (``computer`` / ``browser`` / ``app``) the
    # owner permits. Empty for local sessions and ungranted machines. Live-
    # refreshed by ``session_state.refresh_target_device_grants`` on a toggle,
    # so a mid-session revoke takes effect at the next device-tool gate.
    target_device_grants: set = field(default_factory=set)
    # --- Visibility-modes decouple (see core/session/visibility.py) ---
    # ``session_scope`` is the MOUNT scope ("user"|"agent") — distinct from
    # ``username`` (which stays the REAL human, for attribution + the identity
    # prompt line). A Shared-only human chat has ``username`` set but
    # ``session_scope == "agent"``. Prompt sections key folder/scope rendering
    # on ``session_scope`` and human-presence on ``username``.
    session_scope: str = "user"
    # Owner-tier human → mounts /config + curates knowledge. False for service
    # sessions (the admin-only-task /config regression guard). ``None`` = not
    # resolved → derive historically (owner role + a real human), so a builder
    # that hasn't been wired to the resolver yet never regresses.
    config_visible: bool | None = None
    # The agent's mode scopes — drives Personal-only's dropped shared dirs and
    # the scope-aware prompt variants.
    available_scopes: tuple = ("user", "agent")

    @property
    def mount_username(self) -> str:
        """The username for the bwrap mount / path resolution — "" for any
        agent-scope mount (service sessions AND Shared-only human chats)."""
        return self.username if self.session_scope == "user" else ""

    @property
    def mount_shared(self) -> bool:
        """Does this session's mode include the shared /workspace + /knowledge?
        (False only for Personal-only.)"""
        return "agent" in self.available_scopes

    @property
    def effective_config_visible(self) -> bool:
        """Concrete /config visibility for prompt + folder rendering. Honors an
        explicitly-resolved value; otherwise derives from the REAL human +
        owner-tier role (correct for every mode, incl. Shared-only)."""
        if self.config_visible is not None:
            return self.config_visible
        return bool(self.username) and self.role in ("manager", "admin")


@dataclass(frozen=True)
class PathDecision:
    """Result of a path policy check."""

    allowed: bool
    reason: str = ""
    permission_tier: str = ""  # "", "read", "edit", "extended", "admin", "ask"
    # Bash: the command runs a destructive op (rm / dd / shred / truncate /
    # find -delete / ...). Tracked SEPARATELY from permission_tier (it is NOT
    # an ordinal level) so a destructive command sharing a pipeline with a
    # higher tier — e.g. ``rm x && curl …`` — still forces a prompt in
    # acceptEdits instead of being masked by the pipeline's max tier. Pass-2
    # prompts whenever this is set, except in dontAsk/auto. See _check_bash.
    destructive: bool = False
    # Remote satellites only: the tool input with its sandbox-virtual / `~`
    # path arg rewritten to the satellite-host form the native tool must
    # actually use. The permission hook returns it as PreToolUse
    # ``updatedInput`` so the CLI executes against the real path. None =
    # leave the input untouched.
    updated_input: dict | None = None


_ALLOW = PathDecision(allowed=True)

# ---------------------------------------------------------------------------
# Resolved path constants (computed once at import)
# ---------------------------------------------------------------------------

_AGENTS_DIR = config.AGENTS_DIR.resolve()
_PROXY_DIR = config.BASE_DIR.resolve()
_HOME = Path.home().resolve()
_PLANS_DIR = (_HOME / ".claude" / "plans").resolve()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_under(resolved: Path, parent: Path) -> bool:
    """Check if resolved is equal to or inside parent."""
    try:
        return resolved == parent or resolved.is_relative_to(parent)
    except (TypeError, ValueError):
        return False


def _resolve_path(raw: str) -> Path:
    """Resolve a raw path to absolute, following symlinks and normalizing '..'."""
    p = Path(raw)
    if str(p).startswith("~"):
        p = p.expanduser()
    if not p.is_absolute():
        p = _AGENTS_DIR / p
    return p.resolve()


def _translate_sandbox_path(raw_path: str, ctx: SecurityContext) -> str:
    """Translate sandbox-internal path to host path for defense-in-depth checks.

    Inside the sandbox, the CLI sees paths like /workspace/, /config/,
    /knowledge/, /users/alice/. This maps them back to host paths so
    _check_read_path and _check_write_path can validate correctly.

    All sessions are sandboxed.
    """
    agent_dir = _AGENTS_DIR / ctx.agent

    # /config/ — agent config dir (manager/admin RW; editor/viewer RO)
    if raw_path.startswith("/config/") or raw_path == "/config":
        return str(agent_dir / raw_path[1:])

    # /knowledge/ — agent reference library (manager/admin RW; everyone else RO).
    # Universal across user-scope and agent-scope sessions.
    if raw_path.startswith("/knowledge/") or raw_path == "/knowledge":
        return str(agent_dir / raw_path[1:])

    # /workspace/ — agent workspace (manager/admin/editor RW; viewer RO; agent-scoped RW)
    if raw_path.startswith("/workspace/") or raw_path == "/workspace":
        return str(agent_dir / raw_path[1:])

    # /users/{username}/ — all user roles (viewer/editor/manager/admin)
    if raw_path.startswith("/users/"):
        return str(agent_dir / raw_path[1:])

    # /screenshots/ — MCP conditional mount
    if raw_path.startswith("/screenshots/") or raw_path == "/screenshots":
        return raw_path  # will be resolved against actual mount path

    return raw_path


def _is_other_users_dir(resolved: Path, username: str) -> bool:
    """Return True if path is inside another user's directory.

    Detects paths matching .../users/{someone}/... and checks if {someone}
    matches the current username. Returns True if:
    - Path is in any user dir and username is empty (agent-scoped tasks)
    - Path is in a different user's dir
    """
    parts = resolved.parts
    for i, part in enumerate(parts):
        if part == "users" and i + 1 < len(parts):
            dir_owner = parts[i + 1]
            if not username or dir_owner != username:
                return True
    return False


# ---------------------------------------------------------------------------
# Read path validation
# ---------------------------------------------------------------------------


def _check_read_path(resolved: Path, ctx: SecurityContext) -> PathDecision:
    """Validate a read operation against the security context."""
    # OAuth credential files are protected for EVERY role (including
    # admin-on-admin-agent). The OAuth connect/disconnect UI is the
    # intended management surface; raw token JSON has no UX value and
    # exposing it via Read enables prompt-injection exfiltration. See
    # path_roles.is_protected_credentials_path docstring.
    from services import path_roles
    if path_roles.is_protected_credentials_path(resolved):
        return PathDecision(
            False,
            "Read denied: OAuth credentials are protected. "
            "Manage accounts via Settings → Integrations.",
        )

    # The agent's OWN CLI config (.claude/*.json, .codex/config.toml|
    # auth.json at a scope root) carries this session's secrets — the broker
    # cap-token, the swapped-in HTTP bearer, the session JWT, the model
    # token. Deny reads (Read / cat / grep route here) so a prompt-injected
    # agent can't read its own config and leak the token in chat. Protected for
    # EVERY role incl. admin-on-admin-agent (no UX value, only exfil risk).
    if path_roles.is_protected_agent_config_path(resolved):
        return PathDecision(
            False,
            "Read denied: agent CLI config files are protected.",
        )

    # Admin on admin agent: unrestricted
    if ctx.is_admin_agent and ctx.role == "admin":
        return _ALLOW

    # Block other users' dirs first. MOUNT identity, not attribution: a
    # Shared-only human chat (username set, session_scope "agent") has NO
    # per-user dirs at all, so every users/ path is foreign to it.
    if _is_other_users_dir(resolved, ctx.mount_username):
        return PathDecision(False, "Access denied: cannot read other users' files")

    # Claude Code CLI background-command output: the agent's own ephemeral task
    # output (HOME=/tmp → /tmp/claude-<uid>/.../tasks/<id>.output). Safe to read
    # — its own command output in its per-session tmpfs, no cross-user surface.
    # Ordered AFTER the OAuth-credential, agent-config, and cross-user denies so
    # it can never weaken them (a .output file can't match those anyway). Covers
    # Read + bash cat/tail/head/grep — their path args all route through here.
    if path_roles.is_claude_bg_output_path(resolved):
        return _ALLOW

    # Helper: check access for a single agent dir based on role
    def _check_agent_read(agent_name: str) -> bool:
        agent_dir = (_AGENTS_DIR / agent_name).resolve()

        # Agent-scoped context (no username): workspace/ + knowledge/
        # (NOT config — config is human-owner curation only).
        if not ctx.username:
            if _path_under(resolved, (agent_dir / "workspace").resolve()):
                return True
            if _path_under(resolved, (agent_dir / "knowledge").resolve()):
                return True
            return False

        # Read access:
        # - own user dir (RW for self; the write check enforces self-only —
        #   MOUNT identity: no personal grant on an agent-scope mount)
        # - workspace/ (collaborative space — all roles)
        # - knowledge/ (reference library — all roles)
        # - config/ (agent behavior — OWNER ONLY: manager/admin)
        # The write check (_check_write_path) discriminates further on
        # workspace + knowledge.
        if ctx.mount_username and _path_under(
                resolved, (agent_dir / "users" / ctx.mount_username).resolve()):
            return True
        if _path_under(resolved, (agent_dir / "workspace").resolve()):
            return True
        if _path_under(resolved, (agent_dir / "knowledge").resolve()):
            return True
        if _path_under(resolved, (agent_dir / "config").resolve()):
            # OWNER tier only: manager / admin. Editor + viewer denied.
            return ctx.role in ("manager", "admin")
        return False

    # Current agent
    if _check_agent_read(ctx.agent):
        return _ALLOW

    # Plan files — handled by sandbox per-user isolation

    # Camoufox screenshots flow through `mcp_output_relocation` into a
    # per-session subdir under the agent's workspace
    # (`/users/{u}/workspace/.screenshots/{sid}/` or
    # `/workspace/.screenshots/{sid}/`), already covered by the workspace
    # mount checks above — no dedicated screenshot allowlist is needed.

    return PathDecision(False, "Read access denied: path outside allowed scope for your role")


# ---------------------------------------------------------------------------
# Write path validation
# ---------------------------------------------------------------------------

# Subdirs of a user's own ``users/{u}/`` dir that accept writes. The dir
# ROOT is reserved — mirrors the RO-root + RW-subdir bwrap mount in
# core/sandbox/sandbox.py::_workspace_mounts (keep the two sets in step).
# ``.credentials`` is deliberately absent: its writes are already denied
# above via ``is_protected_credentials_path`` (MCP processes write tokens
# in place, but they bypass this hook entirely).
_USER_DIR_WRITABLE_SUBDIRS: tuple[str, ...] = (
    "workspace", "context", ".claude", ".codex",
)

# Paths that are NEVER writable (even admin on admin agent).
# Protects the permission system itself and sensitive infrastructure.
_ALWAYS_DENY_WRITE: list[Path] = [
    (_HOME / ".claude" / "settings.json").resolve(),
    (_HOME / ".claude" / "settings.local.json").resolve(),
    (_HOME / ".claude" / "rules").resolve(),
    _PROXY_DIR,  # entire proxy dir (source, .env, sessions, hooks)
    (_HOME / ".ssh").resolve(),
]


def _is_memory_file(resolved: Path) -> bool:
    """True when the path lives inside a memory scope dir — any path under
    ``knowledge/memory/`` (agent scope) or ``users/{u}/context/memory/``
    (user scope), including the generated ``MEMORY.md`` index.

    Agents write memory ONLY through the ``memory`` MCP tool → proxy
    ``/v1/internal/memory/op`` (role matrix, locking, git attribution,
    index regen) — never via their regular Write/Edit tools. Reads stay
    allowed (the content is in their prompt anyway). Humans hand-edit
    these files via the dashboard / satellites under the normal folder
    roles; the index self-heals.
    """
    parts = resolved.parts
    for i, seg in enumerate(parts):
        if seg != "memory" or i == 0:
            continue
        prev = parts[i - 1]
        if prev == "knowledge":
            return True
        if prev == "context" and i >= 2 and "users" in parts[:i]:
            return True
    return False


def _check_write_path(resolved: Path, ctx: SecurityContext) -> PathDecision:
    """Validate a write operation against the security context."""
    # Memory dirs are agent-writable ONLY through the memory tool (the
    # internal API writes server-side and bypasses path policy).
    if _is_memory_file(resolved):
        return PathDecision(
            False,
            "Write denied: memory files are managed by the platform. "
            "Use the `memory` tool to save or revise memories.",
        )

    # OAuth credential files are protected for EVERY role. See
    # _check_read_path for rationale.
    from services import path_roles
    if path_roles.is_protected_credentials_path(resolved):
        return PathDecision(
            False,
            "Write denied: OAuth credentials are protected. "
            "Manage accounts via Settings → Integrations.",
        )

    # Always-deny targets (even admin on admin agent)
    for deny_path in _ALWAYS_DENY_WRITE:
        if _path_under(resolved, deny_path):
            return PathDecision(False, "Write denied: protected system path")
    # Also deny any file named .env regardless of location
    if resolved.name == ".env":
        return PathDecision(False, "Write denied: .env files are protected")

    # Admin on admin agent: allow everything else
    if ctx.is_admin_agent and ctx.role == "admin":
        return _ALLOW

    # Block other users' dirs (MOUNT identity — see the read-path comment).
    if _is_other_users_dir(resolved, ctx.mount_username):
        return PathDecision(False, "Write denied: cannot write to other users' files")

    # Plan files — handled by sandbox per-user isolation

    # Own user dir: writable by all roles, but only within the known
    # subdirs — the dir ROOT is reserved (the dashboard file browser shows
    # it; strays land next to workspace/ and context/). Mirrors the
    # RO-root + RW-subdir bwrap mount, which is what actually stops the
    # hook-bypassing writers (Codex native tools, Bash, MCP processes).
    if ctx.mount_username:
        own_dir = (_AGENTS_DIR / ctx.agent / "users" / ctx.mount_username).resolve()
        if _path_under(resolved, own_dir) and resolved != own_dir:
            for sub in _USER_DIR_WRITABLE_SUBDIRS:
                if _path_under(resolved, own_dir / sub):
                    return _ALLOW
            return PathDecision(
                False,
                "Write denied: the root of your personal folder is "
                "reserved. Write files under its workspace/ subfolder "
                "(or context/ for personal context documents).",
            )

    # Helper: check write access for a single agent dir based on role
    def _check_agent_write(agent_name: str) -> bool:
        agent_dir = (_AGENTS_DIR / agent_name).resolve()

        # Agent-scoped context (no username): only workspace/ is writable.
        # Knowledge is RO for agent-scope (curated by owners; agent-scope
        # sessions consume it but don't curate).
        if not ctx.username:
            return _path_under(resolved, (agent_dir / "workspace").resolve())

        # Viewer: only own user dir + plans (no workspace, no config, no knowledge)
        if ctx.role == "viewer":
            return False

        # Editor: workspace/ is writable (collaborative); config/ and knowledge/
        # stay owner-only (they shape agent BEHAVIOR, not workspace state).
        if ctx.role == "editor":
            return _path_under(resolved, (agent_dir / "workspace").resolve())

        # Manager / admin (owner tier): workspace/ + config/ + knowledge/
        if _path_under(resolved, (agent_dir / "workspace").resolve()):
            return True
        if _path_under(resolved, (agent_dir / "config").resolve()):
            return True
        if _path_under(resolved, (agent_dir / "knowledge").resolve()):
            return True
        return False

    # Current agent
    if _check_agent_write(ctx.agent):
        return _ALLOW

    if ctx.role == "viewer":
        return PathDecision(False, "Write denied: viewers can only write to personal folders and plans")
    if ctx.role == "editor":
        return PathDecision(False, "Write denied: editors cannot modify agent knowledge (owner-only). Agent config is not visible to editors at all.")
    return PathDecision(False, "Write denied: path outside allowed scope for your role")


def check_host_path_access(
    host_path: Path, ctx: SecurityContext, *, writing: bool = False,
) -> PathDecision:
    """Per-role + cross-user RBAC for an already-resolved HOST path under the
    agent tree (``AGENTS_DIR/{agent}/...``). Public wrapper over the read/write
    path checks so hook handlers that resolve an agent-supplied path can
    re-impose the role matrix + the cross-user boundary."""
    return _check_write_path(host_path, ctx) if writing else _check_read_path(host_path, ctx)


def enforce_agent_tree_rbac(
    resolution, ctx: SecurityContext, *, writing: bool = False,
) -> PathDecision:
    """Re-impose per-user/role RBAC on a ``path_policy_v2`` resolution.

    ``path_policy_v2`` is deliberately DB/auth-free — it applies only the
    credential / home-dir / full-FS bands and ADMITS any in-tree path including
    ``/users/OTHER/...``. So EVERY caller that resolves an agent-supplied path
    (the resolve-path / resolve-tool-arg-paths hooks, the display/preview/media
    pull, remote Bash) MUST run this to restore the cross-user + role boundary:
    a viewer still can't read ``/config`` or write ``/knowledge``, and NO role
    may touch another user's ``/users/{other}`` dir. A non-``agent_tree``
    resolution (a satellite-host path) is already gated by the home/full-FS
    policy upstream → ``_ALLOW``."""
    ref = getattr(resolution, "path_ref", None)
    if ref is not None and ref.kind == "agent_tree":
        sandbox_virtual = resolution.sandbox_relative or ("/" + ref.value)
        resolved = _resolve_path(_translate_sandbox_path(sandbox_virtual, ctx))
        return check_host_path_access(resolved, ctx, writing=writing)
    return _ALLOW


def _check_remote_bash_path(
    raw_path: str, ctx: SecurityContext, *, writing: bool,
) -> PathDecision:
    """Validate one Bash path argument on a REMOTE satellite via the remote
    path framework — the SAME two-band policy the file tools use:

      * sandbox-virtual (``/workspace``, ``/users/{u}``, ``/knowledge``,
        ``/config``) → translate to the agent tree and apply per-role RBAC
        (a viewer still can't write ``/knowledge``, etc.).
      * satellite-host absolute (``C:/Users/...``, ``/home/...``, ``/etc/...``)
        → admit per ``remote_machines.allow_full_fs`` (home-only vs full-FS).

    Without this, remote Bash used the proxy-local agent-tree check, which
    ignores the satellite's home dir + allow_full_fs entirely — so ``cat
    ~/Desktop/x`` was denied even though ``Read ~/Desktop/x`` was allowed.
    Local sessions never call this (their bwrap mount is the real boundary).
    """
    from services import path_policy_v2 as _v2
    policy_ctx = _v2.context_from_security(ctx)
    res = _v2.resolve_path_for_session(policy_ctx, raw_path, writing=writing)
    if not res.allowed:
        return PathDecision(False, res.error or "path denied by machine policy")
    # Inside the synced agent tree → per-role + cross-user RBAC; a satellite-host
    # path is already home/full-FS-gated (shared with every hook path handler).
    return enforce_agent_tree_rbac(res, ctx, writing=writing)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Tools with file_path argument (Read/Write/Edit)
_FILE_PATH_TOOLS = {"Read", "Write", "Edit", "NotebookEdit"}
# Tools with path argument (Glob/Grep — optional, defaults to cwd)
_SEARCH_PATH_TOOLS = {"Glob", "Grep"}
# Read-operation tools
_READ_TOOLS = {"Read", "Glob", "Grep"}
# Write-operation tools
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# Shell command-execution tools, routed through the command gate.
#   * _BASH_TOOLS → _check_bash. "Monitor" is Claude Code's background-command
#     runner ("same permission rules as Bash", bash-flavored on all platforms);
#     it presents tool_name="Monitor" to the hook (NOT "Bash"), so a Bash-only
#     branch misses it.
#   * _POWERSHELL_TOOLS → _check_powershell. Windows-native (also Lin/Mac opt-in).
# Without routing these, every PowerShell / Monitor command would hit the _ALLOW
# catch-all below → no dangerous-deny / cross-user / credential gate (a real hole
# in dontAsk/auto, where tasks + phone run). tool_input.command carries the
# command for all three.
_BASH_TOOLS = {"Bash", "Monitor"}
_POWERSHELL_TOOLS = {"PowerShell"}
_SHELL_COMMAND_TOOLS = _BASH_TOOLS | _POWERSHELL_TOOLS

# Tools whose payloads are structured / natural-language (NOT shell commands) —
# exempt from the unknown-tool dangerous backstop, else a benign Agent /
# TodoWrite / WebSearch arg mentioning a path or an `rm -rf` example would be
# false-denied with no recourse. The backstop is a cross-platform CATASTROPHE net
# for a FUTURE/unknown command-execution tool we don't yet route — NOT a content
# filter over ordinary tool args.
_KNOWN_STRUCTURED_TOOLS = (
    _FILE_PATH_TOOLS | _SEARCH_PATH_TOOLS | _SHELL_COMMAND_TOOLS | {
        "WebFetch", "WebSearch", "Agent", "Task", "TaskGet", "TaskList",
        "TaskOutput", "TaskCreate", "TaskUpdate", "TaskStop", "TodoWrite",
        "TodoRead", "ToolSearch", "AskUserQuestion", "ExitPlanMode",
        "EnterPlanMode", "CronList", "CronCreate", "CronDelete",
        "CodexEscalation",
    }
)


def _unknown_tool_dangerous_scan(tool_input: dict) -> "PathDecision":
    """Cross-platform catastrophe net for an UNRECOGNIZED command-execution tool
    (a future shell-like tool we don't yet route). Scans string arg values for the
    POSIX + PowerShell dangerous patterns ONLY — never the credential / agent-config
    regex (that would false-deny natural-language tool args). Allows otherwise."""
    for value in tool_input.values():
        if not isinstance(value, str) or not value:
            continue
        for pattern, reason in _DANGEROUS_PATTERNS:
            if pattern.search(value):
                return PathDecision(False, f"Denied (dangerous command): {reason}")
        for pattern, reason in _POWERSHELL_DANGEROUS_PATTERNS:
            if pattern.search(value):
                return PathDecision(False, f"Denied (dangerous command): {reason}")
    return _ALLOW


def check_tool_access(
    tool_name: str,
    tool_input: dict,
    ctx: SecurityContext,
) -> tuple["PathDecision", str | None]:
    """Check whether a tool call is allowed given the security context.

    Returns (PathDecision, new_plan_filename_or_None).
    The second element is non-None when a Write creates a new plan file
    that should be added to the session's allowed plan set.

    MCP tools (mcp__*), internal tools, and tools without file paths
    are always allowed (they're gated by per-agent mcp-config.json
    and mode-based logic respectively).
    """
    # MCP tools: separate processes, gated by per-agent mcp-config.json
    if tool_name.startswith("mcp__"):
        return _ALLOW, None

    # Shell command-execution tools — full command gate (dangerous-deny + tier +
    # cross-user path + credential / agent-config backstops). Placed BEFORE the
    # admin fast-path + remote _ALLOW so the backstops fire for every caller; the
    # admin-on-admin fast path lives INSIDE _check_bash / _check_powershell (after
    # the backstops), matching the pre-existing contract.
    #   Bash / Monitor: bwrap restricts the filesystem locally; the gate restricts
    #   command types + path args (the only cross-user gate on a satellite).
    if tool_name in _BASH_TOOLS:
        return _check_bash(tool_input.get("command", ""), ctx), None
    if tool_name in _POWERSHELL_TOOLS:
        return _check_powershell(tool_input.get("command", ""), ctx), None

    # WebFetch: SSRF prevention
    if tool_name == "WebFetch":
        return _check_webfetch(tool_input.get("url", ""), ctx), None

    # Admin on admin agent: skip all path checks
    if ctx.is_admin_agent and ctx.role == "admin":
        return _ALLOW, None

    # Remote satellites: delegate to the remote path-policy framework.
    # The satellite hosts its own filesystem the proxy has no direct
    # access to; the framework applies a two-band policy:
    #
    #   - INSIDE the satellite's synced agent tree: translate the
    #     satellite-host path back to sandbox-virtual form and apply
    #     normal per-role RBAC so a viewer still can't write to
    #     /knowledge/, an editor still can't write to /config/, etc.
    #   - OUTSIDE the synced tree: admit paths under the OS user's
    #     home directory by default, plus any path when the machine's
    #     ``allow_full_fs`` policy is True. Reject everything else.
    if ctx.target_kind in ("admin_remote", "user_remote"):
        if tool_name not in _FILE_PATH_TOOLS and tool_name not in _SEARCH_PATH_TOOLS:
            # Bash / Monitor / PowerShell / WebFetch / MCP already checked above.
            # A genuinely-unknown command-execution tool gets the cross-platform
            # dangerous-pattern backstop (catastrophe net) instead of a bare allow.
            if tool_name not in _KNOWN_STRUCTURED_TOOLS:
                return _unknown_tool_dangerous_scan(tool_input or {}), None
            return _ALLOW, None
        # NotebookEdit carries its path as `notebook_path` — include it so
        # notebook writes get the same remote policy as Write/Edit.
        _path_key = next(
            (k for k in ("file_path", "notebook_path", "path")
             if tool_input.get(k)),
            "",
        )
        raw_path = tool_input.get(_path_key, "") if _path_key else ""
        if not raw_path:
            return _ALLOW, None
        # Late import to avoid circular dependency on services/.
        from services import path_policy_v2 as _v2
        policy_ctx = _v2.context_from_security(ctx)
        is_write = tool_name in _WRITE_TOOLS
        resolution = _v2.resolve_path_for_session(
            policy_ctx, raw_path, writing=is_write,
        )
        if not resolution.allowed:
            return PathDecision(allowed=False, reason=resolution.error), None
        # Native CLI tools (Read/Write/Edit/Glob/Grep) execute on the
        # satellite and pass the LLM's path verbatim to the OS — a
        # sandbox-virtual form (`/workspace/...`, `/users/{u}/...`) misses
        # there (Linux/macOS: ENOENT; Windows: a silent drive-rooted
        # miswrite at `C:\users\...`), and `~/...` is shell syntax the file
        # tools don't expand. The resolver already computed the
        # satellite-host equivalent, so hand it back as a rewritten tool
        # input: the permission hook returns it as PreToolUse
        # ``updatedInput`` and the call runs against the real path. This
        # replaces the old Windows-only "use `C:/x` instead" deny-nudge —
        # translating is strictly better than denying, on every OS.
        _updated_input = None
        if (
            resolution.access_path
            and _path_key
            and (
                _v2.classify_path(
                    _v2.normalize_path(raw_path, policy_ctx.target_os)
                ) == "sandbox_virtual"
                or raw_path == "~"
                or raw_path.startswith("~/")
            )
        ):
            _updated_input = {**tool_input, _path_key: resolution.access_path}
        # In-tree paths: defer to local RBAC against the sandbox-virtual
        # form (same admission semantics whether the LLM wrote
        # /workspace/foo.png or the host equivalent under the agent's
        # synced root).
        #
        # Note: this validates against the PROXY-side host path
        # (`_AGENTS_DIR/<slug>/...`), not the satellite-host path the
        # native tool will actually use. This works correctly because
        # the role-based RBAC checks are STRUCTURAL — they ask "is this
        # path under /workspace/ or /knowledge/ or /config/?" and apply
        # the per-role allow/deny rules. The directory structure under
        # the agent root is identical on the proxy and the satellite
        # (`workspace/`, `users/{u}/`, `config/`, `knowledge/`), so a
        # path that the proxy classifies as `/knowledge/foo` IS in the
        # `/knowledge/` subtree on the satellite too. No FS-ACL checks
        # against the actual disk happen here — those would require the
        # path to literally exist on the proxy which it usually doesn't
        # for remote sessions. Don't refactor this without preserving
        # the structural-equivalence invariant.
        if resolution.path_ref and resolution.path_ref.kind == "agent_tree":
            sandbox_virtual = resolution.sandbox_relative or (
                "/" + resolution.path_ref.value
            )
            translated = _translate_sandbox_path(sandbox_virtual, ctx)
            resolved = _resolve_path(translated)
            decision = (
                _check_write_path(resolved, ctx) if is_write
                else _check_read_path(resolved, ctx)
            )
            if decision.allowed and _updated_input is not None:
                decision = _dc_replace(decision, updated_input=_updated_input)
            return decision, None
        # Satellite-host path admitted by home / full-FS policy — no
        # additional role check (the per-machine policy IS the gate).
        # (`~/...` still lands here after expansion — return the expanded
        # host path so the file tools, which don't do tilde expansion,
        # actually reach it.)
        if _updated_input is not None:
            return PathDecision(allowed=True, updated_input=_updated_input), None
        return _ALLOW, None

    # File path tools (Read, Write, Edit, NotebookEdit — the latter names
    # its arg `notebook_path`)
    if tool_name in _FILE_PATH_TOOLS:
        raw_path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
        if not raw_path:
            return _ALLOW, None  # no path = tool will error naturally
        # Translate sandbox-internal paths to host paths for validation
        translated = _translate_sandbox_path(raw_path, ctx)
        resolved = _resolve_path(translated)

        # Plan file isolation: sandbox isolates plans per-user — allow all plan file ops
        if _path_under(resolved, _PLANS_DIR):
            new_plan = resolved.name if tool_name in ("Write",) else None
            return _ALLOW, new_plan

        if tool_name in _READ_TOOLS:
            return _check_read_path(resolved, ctx), None
        return _check_write_path(resolved, ctx), None

    # Search tools (Glob, Grep) — use 'path' key, optional
    if tool_name in _SEARCH_PATH_TOOLS:
        raw_path = tool_input.get("path", "")
        if not raw_path:
            return _ALLOW, None  # defaults to cwd, within scope
        translated = _translate_sandbox_path(raw_path, ctx)
        resolved = _resolve_path(translated)

        return _check_read_path(resolved, ctx), None

    # All other tools (Agent, TaskGet, TodoWrite, ToolSearch, etc.): allow. A
    # genuinely-unknown command-execution tool (not in the known structured set)
    # gets the cross-platform dangerous-pattern backstop (catastrophe net) first.
    if tool_name not in _KNOWN_STRUCTURED_TOOLS:
        return _unknown_tool_dangerous_scan(tool_input or {}), None
    return _ALLOW, None


# ---------------------------------------------------------------------------
# Sibling-module facade
# ---------------------------------------------------------------------------
# The shell command-gate and the prompt builder live in sibling modules to keep
# this file readable; re-export the symbols the dispatcher above + external
# callers reference. (Imported at the bottom so the sibling modules can import
# this module's core symbols without a cycle.)
from auth.path_shell import (  # noqa: E402,F401
    _DANGEROUS_PATTERNS,
    _POWERSHELL_DANGEROUS_PATTERNS,
    _check_bash,
    _check_powershell,
    _check_webfetch,
)
from auth.path_prompt import (  # noqa: E402,F401
    _build_execution_environment_section,
    build_permission_context,
)
