"""Role-based file access policy for the permission hook.

Validates tool calls against a session's SecurityContext (role, username, agent).
Called by the permission hook endpoint (api/hooks/hooks.py) BEFORE the existing
mode-based logic.

Two-pass gate:
  Pass 1 (this module): path allowed for role+agent? → DENY or CONTINUE
  Pass 2 (existing):    mode-based logic (default/acceptEdits/plan/dontAsk)
"""

import posixpath
import re
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path

import config
from core.events import tool_roles
from core.placement import PlacementCapabilities
# str-typed readers: a duck-typed / mocked context must never look external
# (a truthy stand-in attribute would otherwise become a host path).
from core.session.external_identity import external_home_of, is_external_ctx
from auth import roles
from core import layout


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityContext:
    """Immutable security context for a session."""

    role: str  # the effective per-agent role (auth/roles.EFFECTIVE_ROLES)
    username: str  # filesystem-safe slug, or "" for tasks/phone without user
    agent: str  # current agent name
    is_admin_agent: bool  # True if agent is in HIGH_CLEARANCE_AGENTS
    display_name: str = ""  # user's full display name
    email: str = ""  # user's email
    # Where the session runs — the resolved placement (``core.placement``):
    # the kind (the local sandbox, the agent's admin-paired default machine,
    # the person's own paired machine) and the machine's facts the path
    # policy, the prompt's ``# Execution Environment`` section, the bash
    # tier gating and the device-tool gate ask. Built once per session by
    # ``remote_store.placement_of``; persisted under the security index's
    # own ``target_*`` keys by ``session_state``'s codec; ``allow_full_fs``
    # and ``device_grants`` are live-refreshed on a machine toggle. The
    # local placement for local sessions (every fact empty).
    placement: PlacementCapabilities = field(default_factory=PlacementCapabilities)
    # otodock-CLI: extra absolute satellite-host roots this ONE session may
    # read/write, beyond the home/sandbox/full-FS matrix — the session's own
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
    # This session's CLI session id (== chats.session_id, the value spawned
    # via --session-id/--resume). Stamped centrally by
    # ``session_state.set_session_security`` when the registration key is a
    # UUID — empty disables the runtime-tree carve (the placement's
    # ``claude_runtime_root`` is the other half). Used ONLY to scope that
    # carve to the session's own subtree.
    cli_session_id: str = ""
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
    # Attached knowledge libraries: ``((source_slug, subdir, writable), …)``
    # — the session's snapshot of knowledge_library_attachments, baked at
    # build time like the mounts themselves (``subdir == ''`` = whole-folder
    # share; the mirror path is ``knowledge/shared/<slug>/<subdir>/``).
    # Drives the mirror-subtree write gate (clean denials where the kernel
    # would give a bare EROFS) and the prompt's shared-knowledge rows.
    knowledge_libraries: tuple = ()
    # Manager-provenance knowledge writes for AGENT-SCOPE task fires: True
    # only when ``resolve_task_identity`` computed it from the dynamic
    # task's creator (platform admin / per-agent manager) under the
    # scheduler's explicit opt-in (scheduled / one-time / trigger fires).
    # Chats, meetings, phone, headless apps and delegate workers stay False
    # by construction. Grants ``/knowledge`` RW (mount + hook + satellite
    # write-back); ``/config`` is deliberately untouched (human-manager-only).
    knowledge_rw: bool = False
    # The judge profile (CHECKS.md): a check's judge session — every mount
    # row flipped read-only (the CLI state dirs excepted), the ``judge``
    # gate mode, the write tools off the CLI. Set by the task config builder
    # for a ``task_type='check'`` run only; False for every other session.
    read_only: bool = False
    # --- External routes (core/session/external_identity.py) ---
    # ``principal`` says what kind of session this is: "user" (a human, or
    # any session that is not external — the default), "agent" (reserved for
    # platform-internal service sessions), "external" (a phone caller who is
    # not a platform user). An external session has its own caller tree at
    # ``external_home`` (host path; "" when the mode has no personal scope or
    # the target is remote), mounted at /caller; no shared memory; none of
    # the platform-management MCPs; no shell; and a proxy API confined to
    # the endpoints its tools use (auth/external_endpoints.py). The JWT
    # minted into its processes carries ``external_claim`` — derived from
    # this context at mint time, which is why the layers register the
    # context BEFORE the spawn.
    principal: str = "user"
    external_channel: str = ""
    external_id: str = ""            # normalised caller id ("" when withheld)
    external_home: str = ""
    external_ephemeral: bool = False
    external_verified: bool = False  # the daemon's PIN gate passed
    external_claim: str = ""

    @property
    def is_external(self) -> bool:
        return self.principal == "external"

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
        return bool(self.username) and roles.can_manage(self.role)


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

# The bash command tiers a decision carries (``_BASH_COMMAND_TIER`` in
# auth/path_shell.py); the permission hook allows or prompts by them.
TIER_READ = "read"
TIER_EDIT = "edit"
TIER_EXTENDED = "extended"
TIER_ADMIN = "admin"
TIER_ASK = "ask"

# External sessions (a phone caller who is not a platform user) never get a
# shell — a shell reaches what the caller cannot hear: the MCP processes'
# credentials (config.toml, ``/proc/*/environ``), token files, the proxy
# with the session token. Three layers enforce it: the permission-hook
# floor (``api/hooks/permission.py``: every tool whose ROLE is ``shell`` —
# ``core/events/tool_roles``), the CLI argv ``--disallowedTools`` and the
# settings.json deny list (the engine's own shell names, from its
# descriptor: ``core/layers/cli``). Direct LLM has no shell; an external
# Codex session has no shell tool at all (``[features] shell_tool = false``)
# and runs this gate as its PreToolUse hook — its file writes arrive as
# ``apply_patch`` (``_check_apply_patch``). A check's judge session
# (CHECKS.md "The judge profile") never writes: the engine's write tools are
# denied the same three ways (the ``judge`` gate mode by role, the argv, the
# settings deny list); the shell stays for its read tier, which the gate
# polices per command.
#
# ``WebSearch`` and ``WebFetch`` are NOT floored (2026-09-08): a caller can
# already hear anything the session can read (the shared space as viewer,
# their own tree), so a query or a URL carries nothing the phone line does
# not, and the direct engine already gives callers the provider's web
# tools. ``WebFetch`` keeps its SSRF gate (``path_shell._check_webfetch``)
# and the local sandbox netns still blackholes private ranges; on a remote
# target, where there is no netns and the gate is literal-URL only, the
# gate denies ``WebFetch`` to external sessions outright.

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

    # ``~`` — the sandbox's HOME is /tmp for every local session (the shell
    # expands it there; the file tools do not expand it at all). A remote
    # session never reaches here with a tilde: path_policy_v2 expands it
    # to the satellite's home.
    if ctx.placement.isolates_with_bwrap and (raw_path == "~" or raw_path.startswith("~/")):
        return "/tmp" + raw_path[1:]

    # /caller/ — an external caller's private tree (SecurityContext.
    # external_home). A session without one has no /caller at all: the raw
    # path is returned untranslated and resolves outside every allowed root.
    if raw_path == "/caller" or raw_path.startswith("/caller/"):
        if not external_home_of(ctx):
            return raw_path
        rest = raw_path[len("/caller"):].lstrip("/")
        return str(Path(ctx.external_home) / rest) if rest else ctx.external_home

    # /config/ — agent config dir (manager/admin RW; editor/viewer RO)
    if layout.under(raw_path, layout.V_CONFIG):
        return str(agent_dir / raw_path[1:])

    # /knowledge/ — agent reference library (manager/admin RW; everyone else RO).
    # Universal across user-scope and agent-scope sessions.
    if layout.under(raw_path, layout.V_KNOWLEDGE):
        return str(agent_dir / raw_path[1:])

    # /workspace/ — agent workspace (manager/admin/editor RW; viewer RO; agent-scoped RW)
    if layout.under(raw_path, layout.V_WORKSPACE):
        return str(agent_dir / raw_path[1:])

    # /users/{username}/ — all user roles (viewer/editor/manager/admin); a
    # bare ``/users`` is deliberately not a root here (it resolves outside
    # every allowed dir, as it always did)
    if raw_path.startswith(layout.V_USERS + "/"):
        return str(agent_dir / raw_path[1:])

    # /screenshots/ — MCP conditional mount
    if raw_path.startswith("/screenshots/") or raw_path == "/screenshots":
        return raw_path  # will be resolved against actual mount path

    return raw_path


def _session_cwd_anchor(ctx: SecurityContext) -> Path:
    """Host equivalent of the sandbox session cwd — ``/users/{u}`` for user
    mounts, ``/workspace`` for agent-scope (sandbox.get_cwd pins these)."""
    agent_dir = _AGENTS_DIR / ctx.agent
    if external_home_of(ctx):
        return Path(ctx.external_home)
    if ctx.mount_username:
        return layout.user_dir(agent_dir, ctx.mount_username)
    return agent_dir / layout.WORKSPACE


def _resolve_candidates(raw_path: str, ctx: SecurityContext) -> list[Path]:
    """Host-path candidates for one tool path argument.

    A RELATIVE path is what the tool will resolve against the sandbox
    session cwd — ``base64 'workspace/downloads/x'`` in local Bash reads
    ``/users/{u}/workspace/downloads/x`` — so that anchor comes first. The
    historical agents-relative anchor stays as the second candidate: it is
    what resolves the display form ("<slug>/users/…") and must not break.
    Each candidate is checked independently by the caller; the sandbox
    mount, not this hook, is the hard local boundary.
    """
    translated = _translate_sandbox_path(raw_path, ctx)
    primary = _resolve_path(translated)
    if (
        translated != raw_path
        or raw_path.startswith("~")
        or Path(raw_path).is_absolute()
    ):
        return [primary]
    cwd_anchored = (_session_cwd_anchor(ctx) / raw_path).resolve()
    if cwd_anchored == primary:
        return [primary]
    # A ``..``-leading path is never the display form (that starts with the
    # agent's slug): the agents-relative anchor would only climb out of the
    # agents dir into whatever sits next to it.
    if posixpath.normpath(raw_path).startswith(".."):
        return [cwd_anchored]
    return [cwd_anchored, primary]


def _check_path_arg(
    raw_path: str, ctx: SecurityContext, *, writing: bool,
) -> PathDecision:
    """Role-check one tool path argument across its resolution candidates.
    Allows when any candidate passes; a denial reports the cwd-anchored
    interpretation (the one the tool actually accesses)."""
    first: PathDecision | None = None
    for cand in _resolve_candidates(raw_path, ctx):
        decision = (
            _check_write_path(cand, ctx) if writing
            else _check_read_path(cand, ctx)
        )
        if decision.allowed:
            return decision
        if first is None:
            first = decision
    return first or PathDecision(False, "path could not be resolved")


#: Codex apply_patch file headers (``codex-rs/apply-patch``): every file the
#: patch touches is announced on one of these lines. The path runs to the end
#: of the line and is stripped by the caller: a lazy ``.+?`` before ``\s*$``
#: backtracks quadratically on a long run of spaces (CodeQL py/polynomial-redos).
_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$", re.M,
)


def _patch_files(tool_input: dict | str) -> list[tuple[str, str, tuple[bool, ...]]]:
    """``(kind, path, checks)`` for every file a Codex ``apply_patch`` names.

    ``Update File`` reads the original before writing it back (a zero-chunk
    update with ``Move to`` copies the file verbatim), so it needs the read
    AND the write check; ``Add File`` / ``Delete File`` / ``Move to`` are
    writes. A patch that names no file is left to Codex to reject.
    """
    text: object = tool_input
    if isinstance(tool_input, dict):
        text = (
            tool_input.get("command") or tool_input.get("patch")
            or tool_input.get("input") or tool_input.get("patch_text") or ""
        )
    if not isinstance(text, str) or not text:
        return []
    files = []
    for kind, raw in _PATCH_FILE_RE.findall(text):
        raw = raw.strip()
        if raw:
            files.append((kind, raw, (False, True) if kind == "Update File" else (True,)))
    return files


def _check_apply_patch(tool_input: dict | str, ctx: SecurityContext) -> PathDecision:
    """Role-check every path a Codex ``apply_patch`` call names on a LOCAL
    sandbox. Relative paths resolve the way the file tools' do (cwd-anchored
    candidates — ``_check_path_arg``)."""
    for kind, raw, checks in _patch_files(tool_input):
        for writing in checks:
            decision = _check_path_arg(raw, ctx, writing=writing)
            if not decision.allowed:
                return PathDecision(False, f"apply_patch denied for {kind} {raw}: {decision.reason}")
    return _ALLOW


_REMOTE_BANDS = (
    "inside the agent tree write under workspace/ or your own folder; outside "
    "it only under the OS user's home, unless the machine allows the full "
    "filesystem"
)


def _is_virtual_form(raw_path: str, policy_ctx) -> bool:
    """A sandbox-virtual (``/workspace/…``, ``/users/{u}/…``) or ``~`` path:
    what the LLM writes, not what a satellite's native tools can open."""
    from services import path_policy_v2 as _v2
    return (
        _v2.classify_path(_v2.normalize_path(raw_path))
        == "sandbox_virtual"
        or raw_path == "~" or raw_path.startswith("~/")
    )


def _check_apply_patch_remote(tool_input: dict | str, ctx: SecurityContext) -> PathDecision:
    """Role-check a Codex ``apply_patch`` on a satellite the way Write/Edit
    are checked there: every patched path goes through the remote resolver,
    so a satellite-host path inside the synced tree translates back and takes
    the per-role RBAC, and one outside it takes the home band and
    ``allow_full_fs``. The patch text cannot be rewritten (the paths are
    inside it, and Codex's deny-only hook never receives an updated input),
    so a sandbox-virtual or ``~`` path is denied with the satellite-host
    form named. Relative paths anchor at the session's own root; ``..``
    takes the resolver's deterministic deny. An admin on an admin agent
    keeps the fast path's reach (no home band) but not the protected set.
    """
    files = _patch_files(tool_input)
    if not files:
        return _ALLOW
    from services import path_policy_v2 as _v2
    policy_ctx = _v2.context_from_security(ctx)
    if ctx.is_admin_agent and roles.is_admin(ctx.role):
        policy_ctx = _dc_replace(
            policy_ctx, placement=_dc_replace(policy_ctx.placement, allow_full_fs=True))
    for kind, raw, checks in files:
        for writing in checks:
            res = _v2.resolve_path_for_session(policy_ctx, raw, writing=writing)
            if not res.allowed:
                return PathDecision(False, f"apply_patch denied for {kind} {raw}: {res.error}")
            if _is_virtual_form(raw, policy_ctx):
                return PathDecision(
                    False,
                    f"apply_patch denied for {kind} {raw}: Codex edits the "
                    f"machine's own filesystem; state the path as {res.access_path}",
                )
            decision = enforce_agent_tree_rbac(res, ctx, writing=writing)
            if not decision.allowed:
                return PathDecision(
                    False,
                    f"apply_patch denied for {kind} {raw}: {decision.reason} "
                    f"({_REMOTE_BANDS})",
                )
    return _ALLOW


def _is_other_users_dir(resolved: Path, username: str) -> bool:
    """Return True if path is inside another user's directory.

    Detects paths matching .../users/{someone}/... and checks if {someone}
    matches the current username. Returns True if:
    - Path is in any user dir and username is empty (agent-scoped tasks)
    - Path is in a different user's dir
    """
    parts = resolved.parts
    for i, part in enumerate(parts):
        if part == layout.USERS and i + 1 < len(parts):
            dir_owner = parts[i + 1]
            if not username or dir_owner != username:
                return True
    return False


#: The sandbox's ``/tmp``: a private per-sandbox tmpfs that is also the
#: agent's ``HOME`` (``core/sandbox/sandbox.py``), resolved once so a host
#: whose ``/tmp`` is a symlink compares equal to a resolved candidate.
_SANDBOX_TMP = Path("/tmp").resolve()


def _is_local_tmp_path(resolved: Path, ctx: SecurityContext) -> bool:
    """``resolved`` is under the sandbox's ``/tmp`` on a LOCAL target: the
    agent's ``HOME``, the CLI's runtime tree (scratchpad, background-task
    outputs) and any scratch file. The tmpfs is private to the sandbox, so
    no other session's or user's data is there; the CLI state that would
    live under it if a CLI fell back to ``$HOME`` is kept by the protected
    set (``path_roles.is_protected_agent_config_path`` knows ``/tmp`` as a
    scope root). Widening-only: callers run the credential / agent-config /
    cross-user / ``.env`` denies first. A remote target never takes it — the
    OS user's home boundary is the operator's pairing decision, and the
    session's own runtime tree is admitted by ``path_policy_v2`` there.

    The check runs on the PROXY-host resolution of the path, and the
    sandbox's ``/tmp`` shares only its name with the host's: an install (or
    the test suite) whose data dir sits under ``/tmp`` must keep the role
    matrix for the agent tree, so a path inside it, or inside the proxy
    dir, never takes this rule."""
    if not ctx.placement.isolates_with_bwrap or not _path_under(resolved, _SANDBOX_TMP):
        return False
    return not (_path_under(resolved, _AGENTS_DIR) or _path_under(resolved, _PROXY_DIR))


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
    if ctx.is_admin_agent and roles.is_admin(ctx.role):
        return _ALLOW

    # Block other users' dirs first. MOUNT identity, not attribution: a
    # Shared-only human chat (username set, session_scope "agent") has NO
    # per-user dirs at all, so every users/ path is foreign to it.
    if _is_other_users_dir(resolved, ctx.mount_username):
        return PathDecision(False, "Access denied: cannot read other users' files")

    # External sessions (a phone caller who is not a platform user): the
    # shared agent memory is not theirs (it is masked out of the mount as
    # well), and under externals/ only their own tree is theirs.
    if is_external_ctx(ctx):
        denied = _external_read_denial(resolved, ctx)
        if denied is not None:
            return denied

    # Claude Code CLI background-command output: the agent's own ephemeral task
    # output (HOME=/tmp → /tmp/claude-<uid>/.../tasks/<id>.output). Safe to read
    # — its own command output in its per-session tmpfs, no cross-user surface.
    # Ordered AFTER the OAuth-credential, agent-config, and cross-user denies so
    # it can never weaken them (a .output file can't match those anyway). Covers
    # Read + bash cat/tail/head/grep — their path args all route through here.
    if path_roles.is_claude_bg_output_path(resolved, ctx.cli_session_id):
        return _ALLOW

    # The sandbox's private /tmp on a local target (HOME, the CLI's runtime
    # tree, scratch files) — same slot and same reasoning as the rule above;
    # see _is_local_tmp_path. Remote sessions get their own runtime tree
    # from path_policy_v2 (satellite-reported root) instead.
    if _is_local_tmp_path(resolved, ctx):
        return _ALLOW

    # Helper: check access for a single agent dir based on role
    def _check_agent_read(agent_name: str) -> bool:
        agent_dir = (_AGENTS_DIR / agent_name).resolve()

        # Agent-scoped context (no username): workspace/ + knowledge/
        # (NOT config — config is human-owner curation only). An external
        # caller additionally reads their own tree.
        if not ctx.username:
            if _path_under(resolved, (agent_dir / layout.WORKSPACE).resolve()):
                return True
            if _path_under(resolved, (agent_dir / layout.KNOWLEDGE).resolve()):
                return True
            if external_home_of(ctx) and _path_under(
                    resolved, Path(ctx.external_home).resolve()):
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
                resolved, layout.user_dir(agent_dir, ctx.mount_username).resolve()):
            return True
        if _path_under(resolved, (agent_dir / layout.WORKSPACE).resolve()):
            return True
        if _path_under(resolved, (agent_dir / layout.KNOWLEDGE).resolve()):
            return True
        if _path_under(resolved, (agent_dir / layout.CONFIG).resolve()):
            # OWNER tier only: manager / admin. Editor + viewer denied.
            return roles.can_manage(ctx.role)
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
    *layout.USER_SUBDIRS, ".claude", ".codex",
)
#: An external caller (a phone caller who is not a platform user) writes
#: only its files and context. Its CLI config dir (``/caller/.claude`` /
#: ``/caller/.codex``) holds the permission hook itself, the settings deny
#: list and the session's config — never a tool-write target (the CLI's
#: own process state writes there without going through a tool).
_EXTERNAL_WRITABLE_SUBDIRS: tuple[str, ...] = layout.USER_SUBDIRS

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
        if prev == layout.KNOWLEDGE:
            return True
        # users/{u}/context/memory and externals/<channel>/<caller>/context/memory
        if prev == layout.CONTEXT and i >= 2 and (
                layout.USERS in parts[:i] or "externals" in parts[:i]):
            return True
    return False


def _external_read_denial(resolved: Path, ctx: SecurityContext) -> PathDecision | None:
    """External-session read rules that precede every grant: no shared agent
    memory, and no other caller's tree. ``None`` = nothing to deny here."""
    agent_dir = (_AGENTS_DIR / ctx.agent).resolve()
    if _path_under(resolved, (agent_dir / layout.KNOWLEDGE / "memory").resolve()):
        return PathDecision(
            False,
            "Read denied: shared agent memory is not available on external routes",
        )
    if _path_under(resolved, (agent_dir / "externals").resolve()):
        home = Path(ctx.external_home).resolve() if external_home_of(ctx) else None
        if home is None or not _path_under(resolved, home):
            return PathDecision(False, "Access denied: another caller's files")
    return None


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

    # The agent's own CLI config, and the hook scripts and settings the
    # permission system runs from (``.claude/*.json``, the hook scripts,
    # ``.codex/{config.toml,auth.json,hooks.json}`` at a scope root): a
    # session that could rewrite them would remove its own gate for the
    # rest of the session. Every role, admin on admin agent included; the
    # platform writes them proxy-side, never through a tool.
    if path_roles.is_protected_agent_config_path(resolved, writing=True):
        return PathDecision(False, "Write denied: agent CLI config files are protected.")

    # Always-deny targets (even admin on admin agent)
    for deny_path in _ALWAYS_DENY_WRITE:
        if _path_under(resolved, deny_path):
            return PathDecision(False, "Write denied: protected system path")
    # Also deny any file named .env regardless of location
    if resolved.name == ".env":
        return PathDecision(False, "Write denied: .env files are protected")

    # Admin on admin agent: allow everything else
    if ctx.is_admin_agent and roles.is_admin(ctx.role):
        return _ALLOW

    # Block other users' dirs (MOUNT identity — see the read-path comment).
    if _is_other_users_dir(resolved, ctx.mount_username):
        return PathDecision(False, "Write denied: cannot write to other users' files")

    # External sessions: only their own caller tree under externals/, and
    # only its known subdirs (the root is reserved, like a user dir's root).
    if is_external_ctx(ctx):
        denied = _external_read_denial(resolved, ctx)
        if denied is not None:
            return PathDecision(False, denied.reason.replace("Read denied", "Write denied"))
        if external_home_of(ctx):
            home = Path(ctx.external_home).resolve()
            if _path_under(resolved, home):
                if resolved != home:
                    for sub in _EXTERNAL_WRITABLE_SUBDIRS:
                        if _path_under(resolved, home / sub):
                            return _ALLOW
                return PathDecision(
                    False,
                    "Write denied: the root of the caller folder is reserved; "
                    "only its workspace/ and context/ subfolders are writable "
                    "on this line.",
                )

    # The sandbox's private /tmp on a local target (HOME, the CLI's runtime
    # tree, scratch files): admitted read + write. Ordered AFTER the memory /
    # OAuth-credential / agent-config / always-deny / .env / cross-user
    # denies above so it can never weaken them. See _is_local_tmp_path;
    # remote gets its own runtime tree from path_policy_v2.
    if _is_local_tmp_path(resolved, ctx):
        return _ALLOW

    # Plan files — handled by sandbox per-user isolation

    # Own user dir: writable by all roles, but only within the known
    # subdirs — the dir ROOT is reserved (the dashboard file browser shows
    # it; strays land next to workspace/ and context/). Mirrors the
    # RO-root + RW-subdir bwrap mount, which is what actually stops the
    # hook-bypassing writers (Codex native tools, Bash, MCP processes).
    if ctx.mount_username:
        own_dir = layout.user_dir(_AGENTS_DIR / ctx.agent, ctx.mount_username).resolve()
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

        def _mirror_verdict() -> bool | None:
            """Knowledge-library mirrors (knowledge/shared/<source>/…) sit
            inside knowledge but are decided by the covering attachment's
            writable flag, not the session's knowledge tier alone — a
            read-only mirror gets a clean policy denial here instead of
            the kernel's bare EROFS. The shared/ root itself is
            projector-owned: never hand-writable. Libraries are per
            SUBTREE — matching is segment-wise (a library ``marketing``
            never authorizes ``marketing-extra``); a path under the slug
            but outside every attached subtree is denied.
            ``None`` = not a mirror-namespace path."""
            shared_root = (agent_dir / layout.KNOWLEDGE / "shared").resolve()
            if not _path_under(resolved, shared_root):
                return None
            try:
                rel_parts = resolved.relative_to(shared_root).parts
            except ValueError:
                return False
            if not rel_parts:
                return False
            src, sub_parts = rel_parts[0], list(rel_parts[1:])
            for entry in (ctx.knowledge_libraries or ()):
                e_src, e_subdir, e_writable = entry[0], entry[1], bool(entry[2])
                if e_src != src or not e_writable:
                    continue
                prefix = e_subdir.split("/") if e_subdir else []
                if sub_parts[: len(prefix)] == prefix:
                    return True
            return False

        # Agent-scoped context (no username): workspace/ is writable, and —
        # ONLY for manager-provenance task fires (ctx.knowledge_rw) — the
        # knowledge tree, under the same per-attachment mirror rule as the
        # owner tier (attachment writable AND provenance, both required).
        # /config stays denied (human-manager-only), memory/ was denied
        # earlier (_is_memory_file precedes this branch).
        if not ctx.username:
            if _path_under(resolved, (agent_dir / layout.WORKSPACE).resolve()):
                # An external caller never writes the shared workspace: the
                # builder always makes it a viewer (phone_identity.
                # EXTERNAL_ROUTE_ROLE); the role check stays as defence in
                # depth should another producer ever hand out a wider role.
                return not (is_external_ctx(ctx) and ctx.role == roles.VIEWER)
            if ctx.knowledge_rw:
                mirror = _mirror_verdict()
                if mirror is not None:
                    return mirror
                return _path_under(resolved, (agent_dir / layout.KNOWLEDGE).resolve())
            return False

        # Viewer — and any role the table does not know — only own user dir +
        # plans (no workspace, no config, no knowledge). Closed, never open.
        if not roles.can_write_workspace(ctx.role):
            return False

        # Editor / contributor: workspace/ is writable (collaborative);
        # config/ and knowledge/ stay owner-only (they shape agent BEHAVIOR,
        # not workspace state).
        if not roles.can_manage(ctx.role):
            return _path_under(resolved, (agent_dir / layout.WORKSPACE).resolve())

        # Manager / admin (owner tier): workspace/ + config/ + knowledge/
        if _path_under(resolved, (agent_dir / layout.WORKSPACE).resolve()):
            return True
        if _path_under(resolved, (agent_dir / layout.CONFIG).resolve()):
            return True
        mirror = _mirror_verdict()
        if mirror is not None:
            return mirror
        if _path_under(resolved, (agent_dir / layout.KNOWLEDGE).resolve()):
            return True
        return False

    # Current agent
    if _check_agent_write(ctx.agent):
        return _ALLOW

    if ctx.role == roles.VIEWER:
        return PathDecision(False, "Write denied: viewers can only write to personal folders and plans")
    if ctx.role == roles.CONTRIBUTOR:
        return PathDecision(False, "Write denied: contributors write the shared workspace and their personal folders only (knowledge is owner-only)")
    if ctx.role == roles.EDITOR:
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


def _check_remote_protected_path(
    raw_path: str, ctx: SecurityContext, *, writing: bool,
) -> PathDecision | None:
    """The universal denials of one shell path on a REMOTE machine, for the
    admin floor: the remote resolver's ``protected`` refusals (the machine's
    own OtoDock state, the OAuth credentials, the agent's CLI config) and
    nothing else: the role matrix and the home band stay skipped there."""
    from services import path_policy_v2 as _v2
    res = _v2.resolve_path_for_session(
        _v2.context_from_security(ctx), raw_path, writing=writing,
    )
    if not res.allowed and res.protected:
        return PathDecision(False, res.error)
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# The tool vocabulary is ``core/events/tool_roles``: this gate dispatches on
# a tool's ROLE (shell / read / glob / search / write / delete / web_fetch /
# …), its PAYLOAD kind (a command, a file path, a search path, a patch, a
# URL) and, for a shell, its DIALECT — never on a name. "Monitor" is Claude
# Code's background-command runner ("same permission rules as Bash",
# bash-flavored on all platforms) and presents tool_name="Monitor" to the
# hook; PowerShell is Windows-native (also Lin/Mac opt-in): both are role
# ``shell``, dialects posix and powershell, and route through the command
# gate (dangerous-deny / tier / cross-user path / credential backstops) in
# every mode. A tool the vocabulary does not know is refused outright when
# it carries a command (``tool_roles.SHELL_PAYLOAD_KEYS`` — the fail-closed
# rule: a future shell-like tool must be declared, never run ungated) and
# otherwise gets the catastrophe net below.


def _unknown_tool_dangerous_scan(tool_input: dict) -> "PathDecision":
    """Cross-platform catastrophe net for an UNDECLARED tool with a
    structured payload (no command key — those are refused before this
    runs). Scans string arg values for the POSIX + PowerShell dangerous
    patterns ONLY — never the credential / agent-config regex (that would
    false-deny natural-language tool args). Allows otherwise."""
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


def _protected_tool_path_denial(
    tool_name: str, tool_input: dict, ctx: SecurityContext,
) -> PathDecision | None:
    """The universal denies of a file tool's path, for the caller that skips
    the role matrix: OAuth credentials, the agent's own CLI config (read and
    write) and, on a write, the platform's hook scripts and settings. A local
    path is checked on its resolved candidates; a satellite path through the
    remote resolver, whose ``protected`` flag marks exactly these denials."""
    if tool_roles.payload_of(tool_name) not in (tool_roles.FILE_PATH, tool_roles.SEARCH_PATH):
        return None
    raw_path = tool_roles.payload_value(tool_name, tool_input)
    if not raw_path:
        return None
    writing = tool_roles.writes(tool_name)
    verb = "Write" if writing else "Read"
    if ctx.placement.needs_path_translation:
        from services import path_policy_v2 as _v2
        res = _v2.resolve_path_for_session(
            _v2.context_from_security(ctx), raw_path, writing=writing,
        )
        if not res.allowed and res.protected:
            return PathDecision(False, f"{verb} denied: {res.error}")
        return None
    from services import path_roles
    for cand in _resolve_candidates(raw_path, ctx):
        if path_roles.is_protected_credentials_path(cand):
            return PathDecision(
                False,
                f"{verb} denied: OAuth credentials are protected. "
                "Manage accounts via Settings → Integrations.",
            )
        if path_roles.is_protected_agent_config_path(cand, writing=writing):
            return PathDecision(False, f"{verb} denied: agent CLI config files are protected.")
    return None


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

    role = tool_roles.role_of(tool_name)
    payload = tool_roles.payload_of(tool_name)

    # An UNDECLARED tool, ahead of everything (the admin fast path included):
    # with a command payload it is refused — a shell-like tool the platform
    # does not route must never run ungated, on any placement, in any mode;
    # with a structured payload it gets the catastrophe net.
    if not role:
        if tool_roles.carries_command(tool_input):
            return PathDecision(
                False,
                f"{tool_name} is not a tool this platform knows; a command tool "
                "must be declared in core/events/tool_roles.py",
            ), None
        return _unknown_tool_dangerous_scan(tool_input or {}), None

    # Shell tools — full command gate (dangerous-deny + tier + cross-user
    # path + credential / agent-config backstops). Placed BEFORE the admin
    # fast-path + remote _ALLOW so the backstops fire for every caller; the
    # admin-on-admin fast path lives INSIDE _check_bash / _check_powershell
    # (after the backstops), matching the pre-existing contract. bwrap
    # restricts the filesystem locally; the gate restricts command types +
    # path args (the only cross-user gate on a satellite).
    if role == tool_roles.SHELL:
        command = tool_roles.payload_value(tool_name, tool_input)
        if tool_roles.dialect_of(tool_name) == tool_roles.POWERSHELL:
            return _check_powershell(command, ctx), None
        return _check_bash(command, ctx), None

    # A URL fetch: SSRF prevention
    if role == tool_roles.WEB_FETCH:
        return _check_webfetch(tool_roles.payload_value(tool_name, tool_input), ctx), None

    # A patch (Codex's apply_patch): the patch text names its files. Each
    # one gets the Read/Write role check, so a patch cannot read a protected
    # file (an "Update" with a "Move to" is a copy) or write outside the
    # caller's writable subfolders. On a satellite the paths resolve the way
    # Write/Edit resolve there (home band, allow_full_fs, in-tree RBAC).
    if payload == tool_roles.PATCH:
        if ctx.placement.needs_path_translation:
            return _check_apply_patch_remote(tool_input, ctx), None
        return _check_apply_patch(tool_input, ctx), None

    # The Codex approval bridge gates a multi-file change as one file-path
    # write whose ``_codex_paths`` lists every file (``file_path`` is the
    # first). Each extra path takes the same decision as a write of its own,
    # so no file of the change escapes the gate; the mode logic then runs
    # once.
    extra_paths = (tool_input.get("_codex_paths")
                   if role == tool_roles.WRITE and payload == tool_roles.FILE_PATH else None)
    if isinstance(extra_paths, list):
        for extra in extra_paths[1:]:
            if isinstance(extra, str) and extra:
                decision, _ = check_tool_access(tool_name, {"file_path": extra}, ctx)
                if not decision.allowed:
                    return decision, None

    # Admin on admin agent: skip the role matrix — never the universal
    # protected set, which the read and write checks below enforce for
    # everyone else (Bash gets it from its own raw-command backstops).
    if ctx.is_admin_agent and roles.is_admin(ctx.role):
        return (_protected_tool_path_denial(tool_name, tool_input, ctx) or _ALLOW), None

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
    if ctx.placement.needs_path_translation:
        if payload not in (tool_roles.FILE_PATH, tool_roles.SEARCH_PATH):
            # The shells, the URL fetch, the patch and the MCP tools were
            # checked above; every other declared tool is structured.
            return _ALLOW, None
        # A notebook edit carries its path as `notebook_path`: the payload
        # table names the key, so notebook writes get the same remote policy.
        _path_key = tool_roles.payload_key(tool_name, tool_input)
        raw_path = tool_input.get(_path_key, "") if _path_key else ""
        if not raw_path:
            return _ALLOW, None
        # Late import to avoid circular dependency on services/.
        from services import path_policy_v2 as _v2
        policy_ctx = _v2.context_from_security(ctx)
        is_write = tool_roles.writes(tool_name)
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
        if resolution.access_path and _path_key and _is_virtual_form(raw_path, policy_ctx):
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

    # A file-path tool (a read, a write, a delete; a notebook edit names its
    # arg `notebook_path` — the payload table knows)
    if payload == tool_roles.FILE_PATH:
        raw_path = tool_roles.payload_value(tool_name, tool_input)
        if not raw_path:
            return _ALLOW, None  # no path = tool will error naturally
        # Translate sandbox-internal paths to host paths for validation
        translated = _translate_sandbox_path(raw_path, ctx)
        resolved = _resolve_path(translated)

        # Plan file isolation: sandbox isolates plans per-user — allow all
        # plan file ops; a write there registers the plan's name.
        if _path_under(resolved, _PLANS_DIR):
            new_plan = resolved.name if tool_roles.writes(tool_name) else None
            return _ALLOW, new_plan

        return _check_path_arg(raw_path, ctx, writing=tool_roles.writes(tool_name)), None

    # A search tool (a glob, a grep) — the 'path' key, optional
    if payload == tool_roles.SEARCH_PATH:
        raw_path = tool_roles.payload_value(tool_name, tool_input)
        if not raw_path:
            return _ALLOW, None  # defaults to cwd, within scope
        return _check_path_arg(raw_path, ctx, writing=False), None

    # Every other declared tool (a subagent, the task list, the checklist,
    # tool discovery, …) carries a structured payload: allow.
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
