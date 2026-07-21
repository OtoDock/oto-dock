"""Universal MCP path framework: policy resolver.

Single source of truth for "is this path arg allowed on this session,
and what host path should the caller actually use?". Used by:

  * ``auth.path_policy.check_tool_access`` on every native-tool call
    (Read / Edit / Glob / etc.) — replaces the old remote short-circuit
    so satellite-host paths now get policy-checked.
  * ``api.hooks.hooks`` ``/v1/hooks/resolve-path`` — batched hook
    used by the satellite stdio interceptor and by Docker MCPs.

Edge cases are covered one-for-one by the tests in
``test_path_policy_v2.py``.

The module is intentionally pure: callers build a ``PathPolicyContext``
from fresh DB state and pass it in. The module never queries the DB,
making it easy to unit test exhaustively.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Result + context dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathRef:
    """How a path is rooted. Used by ``cm.push_file`` / ``cm.pull_file``
    and the hook contract to distinguish agent-tree-relative
    writes from satellite-host-absolute writes.

      * ``kind="agent_tree"``  → ``value`` is a sandbox-relative slug
        (e.g. ``users/alice/workspace/foo.png``). Written under the
        satellite's ``agents_dir/{slug}/`` root.
      * ``kind="satellite_host"`` → ``value`` is an absolute forward-slash
        path on the satellite's filesystem (e.g.
        ``/home/alice/Desktop/foo.png``).
    """
    kind: Literal["agent_tree", "satellite_host"]
    value: str


@dataclass(frozen=True)
class PathResolution:
    """Outcome of a single path-arg policy check.

    Callers ALWAYS get a structured result — the resolver never raises
    on user-visible policy decisions. Use ``allowed`` to gate the call,
    and surface ``error`` verbatim to the LLM on reject.
    """
    access_path: str            # Host path the caller should USE on
                                # writes/reads. Empty when allowed=False.
    allowed: bool               # Policy verdict.
    error: str = ""             # Human-readable rejection reason.
    path_ref: PathRef | None = None  # Discriminator for downstream
                                     # push/pull semantics.
    is_remote_pull: bool = False     # True when Docker MCPs must lazy-
                                     # pull the file from satellite
                                     # before reading.
    is_remote_push: bool = False     # True when writes must push back
                                     # to the satellite.
    sandbox_relative: str = ""       # Sandbox-virtual form for logs /
                                     # display. Empty when not
                                     # sandbox-translatable.


@dataclass(frozen=True)
class ResolveItem:
    """Single item in a batched ``resolve_path_batch`` call."""
    raw_path: str
    write: bool = False
    json_path: str = ""          # Echo back for diagnostics; ignored
                                 # by the resolver.
    realpath_verify: bool = False  # Satellite sets True on the
                                   # second-pass check after running
                                   # realpath() on its filesystem.


@dataclass(frozen=True)
class PathPolicyContext:
    """All session state the policy needs.

    Built fresh per request by the caller (hook handler or
    ``check_tool_access``). Pure data, no DB lookups inside the resolver.
    """
    target_kind: str             # "local" | "admin_remote" | "user_remote"
    machine_id: str = ""         # empty for local sessions
    home_dir: str = ""           # OS home on satellite (forward-slash)
    os_user: str = ""            # OS user on satellite
    user_dirs: dict = field(default_factory=dict)  # {desktop, downloads, ...}
    allow_full_fs: bool = False
    target_agents_dir: str = ""  # satellite's agent tree root (forward-slash)
    target_os: str = "linux"     # "linux" | "darwin" | "windows"
    agent_slug: str = ""
    user_sub: str = ""
    role: str = "manager"
    # otodock-CLI: extra absolute satellite-host roots admitted for THIS
    # session only (the arbitrary cwd the user ran `otodock` in), realpath-
    # normalized at build time. Checked after the protected-path / .env denials
    # and before the home/full-fs matrix. Empty for every normal session.
    session_allowed_roots: tuple = ()
    # otodock-CLI: the session's actual working directory (satellite-host
    # absolute). When set, RELATIVE tool-arg paths anchor here instead of
    # the sandbox /workspace convention — the collapsed absolute target then
    # flows through the normal admission chain (no new reach for `..`).
    work_cwd: str = ""
    # Claude-CLI runtime-tree carve inputs: the satellite's reported
    # ``<tempdir>/claude-<uid>`` root + THIS session's CLI session id.
    # Both non-empty → the session's own runtime subtree
    # (``<root>/<cwd-slug>/<session-id>/...``) is admitted read+write even
    # in home-only mode. Either empty → carve disabled (fail closed).
    claude_runtime_root: str = ""
    cli_session_id: str = ""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionTargetRevoked(Exception):
    """Raised when a session's cached target no longer matches the
    current target (admin unpaired the satellite mid-session, user
    cleared their user_remote_target, etc.).

    The hook handler catches this and synthesizes a clean tool-error
    response that ends the turn with an actionable message.
    """
    def __init__(self, old: str, new: str, reason: str = "") -> None:
        self.old = old
        self.new = new
        self.reason = reason or "Remote pairing changed mid-session"
        super().__init__(self.reason)


# ---------------------------------------------------------------------------
# Normalization + classification
# ---------------------------------------------------------------------------


_WINDOWS_DRIVE_RE = re.compile(r"^([a-zA-Z]):[\\/]")

# Sandbox-virtual prefix segments. The trailing-slash variants are
# treated identically (handled in the classifier).
_SANDBOX_VIRTUAL_SEGMENTS = (
    "users", "workspace", "knowledge", "config", "screenshots",
)

# Schemes / prefixes that mean "definitely NOT a filesystem path".
_NON_PATH_PREFIXES = (
    "http://", "https://", "ftp://", "ftps://", "file://",
    "data:", "base64:", "blob:",
)

# Template-literal markers. Bare ``{var}`` is left to MCPs (it's their
# convention to interpret); ``${var}`` and ``{{var}}`` ALWAYS skip
# translation because they would break path comparison.
_TEMPLATE_MARKERS = ("${", "{{")


def is_path_string(value: str) -> bool:
    """Type-detection heuristic.

    Returns True if the resolver should attempt to translate this value
    as a path. False for URLs, data URIs, long base64 blobs, template
    literals.
    """
    if not isinstance(value, str) or not value:
        return False
    lo = value.lower()
    for prefix in _NON_PATH_PREFIXES:
        if lo.startswith(prefix):
            return False
    for marker in _TEMPLATE_MARKERS:
        if marker in value:
            return False
    # Very long single-token strings without slashes — likely base64.
    # 500 chars is empirical; anything longer than a typical absolute
    # path on Windows with a deep tree.
    if len(value) > 500 and "/" not in value and "\\" not in value:
        return False
    return True


def normalize_path(raw: str, target_os: str = "linux") -> str:
    """Normalize a path string for comparison/resolution.

    Performs:
      * Backslash → forward slash (Windows arguments).
      * Lower-case the drive letter (Windows, case-insensitive FS).
      * Collapse repeated ``/`` (except where it would convert ``//host``
        to ``/host``; UNC paths are NOT supported in v1 — they get
        rejected by the policy layer).
      * Strip trailing slash on non-root paths.

    Does NOT do ``..`` collapse — caller controls that (callers that
    need it use ``os.path.normpath``).
    """
    if not raw:
        return raw
    s = raw.replace("\\", "/")
    m = _WINDOWS_DRIVE_RE.match(s)
    if m:
        s = m.group(1).lower() + s[1:]
    # Collapse multi-slashes mid-path (preserve leading ``//`` as-is for
    # potential future UNC; policy layer rejects UNC for now).
    s = re.sub(r"(?<=[^/])/{2,}", "/", s)
    # Strip trailing slash unless root or drive root.
    if len(s) > 1 and s.endswith("/") and not s.endswith(":/"):
        s = s.rstrip("/")
    return s


def expand_tilde(raw: str, home_dir: str) -> tuple[str, bool]:
    """Expand ``~/...`` to ``{home_dir}/...``. Returns ``(expanded,
    was_tilde)``. ``~`` alone expands to ``home_dir`` itself.
    ``~otheruser/...`` is left untouched — the policy layer rejects it
    explicitly (we never resolve another OS user's home).
    """
    if not raw or not home_dir:
        return raw, False
    if raw == "~":
        return home_dir, True
    if raw.startswith("~/"):
        return home_dir.rstrip("/") + raw[1:], True
    return raw, False


def is_other_user_tilde(raw: str) -> bool:
    """Detect ``~otheruser`` / ``~otheruser/...`` forms.

    The policy rejects these regardless of ``allow_full_fs`` —
    cross-OS-user access is never granted.
    """
    return raw.startswith("~") and not (raw == "~" or raw.startswith("~/"))


_PathKind = Literal["sandbox_virtual", "satellite_host", "relative", "invalid"]


def classify_path(path: str) -> _PathKind:
    """Classify a normalized path string. Caller must run
    ``normalize_path`` first.

    Sandbox-virtual: starts with a known platform-virtual segment
    (``/users/``, ``/workspace/``, etc.).

    Satellite-host: any other absolute path (Unix-style ``/...`` or
    Windows drive-rooted).

    Relative: anything else (e.g. ``Desktop/foo.png``). Caller resolves
    against ``OTO_WORKSPACE_DIR`` per the existing convention.

    Invalid: empty, NUL-containing, or otherwise unsafe.
    """
    if not path or "\x00" in path:
        return "invalid"
    if path.startswith("/"):
        # Check sandbox-virtual prefixes first.
        first = path[1:].split("/", 1)[0]
        if first in _SANDBOX_VIRTUAL_SEGMENTS:
            return "sandbox_virtual"
        return "satellite_host"
    if _WINDOWS_DRIVE_RE.match(path):
        return "satellite_host"
    return "relative"


# ---------------------------------------------------------------------------
# Path policy core
# ---------------------------------------------------------------------------


def _agent_tree_subroot(rel: str) -> str:
    """Return the agent-tree sub-root the path is under (workspace,
    knowledge, config, users, screenshots), or ``""`` if not in the
    synced tree. ``rel`` is relative to the agent slug root (no leading
    slash). Used to translate satellite-host paths inside the synced
    tree back to their sandbox-virtual form.
    """
    first = rel.split("/", 1)[0] if rel else ""
    if first in _SANDBOX_VIRTUAL_SEGMENTS:
        return first
    return ""


def _normalize_for_compare(value: str, target_os: str) -> str:
    """Lower-case-normalize a path for FS-correct prefix comparison.

    On Windows + macOS-HFS+ the filesystem is case-insensitive. On
    Linux it's case-sensitive. Over-matching on Linux (when the
    satellite reports a case-insensitive OS but the underlying FS isn't)
    only loosens admission for a case-twin path — harmless.
    """
    s = value.replace("\\", "/")
    if target_os in ("windows", "darwin"):
        return s.lower()
    return s


def _is_under(child: str, parent: str, target_os: str) -> bool:
    """``child`` is equal to or inside ``parent`` (post-normalize).

    Both args should be normalized via ``normalize_path`` first.
    """
    if not parent:
        return False
    c = _normalize_for_compare(child, target_os).rstrip("/")
    p = _normalize_for_compare(parent, target_os).rstrip("/")
    return c == p or c.startswith(p + "/")


def _is_session_runtime_path(normalized: str, ctx: PathPolicyContext) -> bool:
    """``normalized`` lies inside THIS session's Claude-CLI runtime tree.

    The tree is ``<claude_runtime_root>/<cwd-slug>/<session-id>/...`` — the
    root comes from the satellite's capabilities probe (uid-exact, so no
    structural guessing) and the session id must appear as a path segment
    below it. The session-id equality is the capability: another platform
    user's session on the same shared-admin satellite (same OS uid, same
    root) can never name this session's UUID. Widening-only — callers MUST
    run the credential / agent-config / cross-user / .env denies first.
    """
    root = ctx.claude_runtime_root
    sid = ctx.cli_session_id
    if not root or not sid:
        return False
    if not _is_under(normalized, root, ctx.target_os):
        return False
    c = _normalize_for_compare(normalized, ctx.target_os).rstrip("/")
    p = _normalize_for_compare(root, ctx.target_os).rstrip("/")
    rel = c[len(p):].strip("/")
    if not rel:
        return False  # the root itself is not the session's tree
    return sid.lower() in rel.split("/")


def _virtual_to_satellite_host(
    sandbox_virtual: str, ctx: PathPolicyContext,
) -> str:
    """Translate a sandbox-virtual path to its satellite-host equivalent.

    ``/users/alice/workspace/foo.png`` →
    ``{target_agents_dir}/{agent_slug}/users/alice/workspace/foo.png``

    Returns empty string when the context doesn't have the satellite's
    agents_dir or agent_slug (programming error — caller checks).
    """
    if not ctx.target_agents_dir or not ctx.agent_slug:
        return ""
    base = ctx.target_agents_dir.rstrip("/") + "/" + ctx.agent_slug
    return base + sandbox_virtual  # sandbox_virtual already starts with /


def _satellite_host_to_virtual(
    abs_path: str, ctx: PathPolicyContext,
) -> str:
    """Inverse of ``_virtual_to_satellite_host`` — returns the
    sandbox-virtual form when the absolute path sits inside the synced
    tree, or empty string otherwise.
    """
    if not ctx.target_agents_dir or not ctx.agent_slug:
        return ""
    base = ctx.target_agents_dir.rstrip("/") + "/" + ctx.agent_slug
    if not _is_under(abs_path, base, ctx.target_os):
        return ""
    rel = abs_path[len(base):].lstrip("/")
    sub = _agent_tree_subroot(rel)
    if not sub:
        return ""
    return "/" + rel


def _reject(error: str) -> PathResolution:
    return PathResolution(access_path="", allowed=False, error=error)


def _protected_path_denial(normalized: str, *, writing: bool) -> str:
    """Denial reason if ``normalized`` (forward-slash form) targets protected
    credential / secret material, else ``""``.

    Mirrors ``auth.path_policy`` so the MCP path resolvers
    (``/v1/hooks/resolve-path`` for Docker MCPs, ``/v1/hooks/resolve-tool-arg-
    paths`` for the stdio interceptor) enforce the same protections. On remote
    satellites there is NO bwrap to hide these files, so this resolver is the
    only software gate for those callers.

    Universal (read + write, every path kind): OAuth credential token dirs and
    SSH key material — neither has any legitimate agent path-tool use, and both
    live under the OS user's ``$HOME`` so home-only mode would otherwise admit
    them. One exact READ carve-out: ``.ssh/config`` + ``.ssh/known_hosts*``
    (directly under a ``.ssh`` dir) are host/alias metadata a session that may
    RUN ssh legitimately needs — see the inline comment below. ``.env`` files
    are write-protected everywhere (parity with local); ``.env`` *reads* are
    gated separately for pure satellite-host paths so an agent's own in-tree
    workspace ``.env`` stays readable, matching local.

    NOTE (residual): matching is name-based on the normalized path. A symlink
    placed on the satellite (``~/safe -> ~/.ssh``) would defeat it; fully
    closing that needs the satellite to ``realpath()`` then re-call with
    ``realpath_verify=True`` (the second-pass contract exists but isn't yet
    wired to re-screen).
    """
    from services import path_roles
    if path_roles.is_protected_credentials_path(normalized):
        return ("OAuth credentials are protected — manage accounts via "
                "Settings → Integrations")
    # The agent's own CLI config (.claude/*.json, .codex/config.toml|
    # auth.json at a scope root) holds this session's broker cap-token, swapped
    # HTTP bearer, session JWT + model token — deny via the MCP-arg / remote
    # resolver too (no bwrap on satellites; this is the only software gate there).
    if path_roles.is_protected_agent_config_path(normalized):
        return "agent CLI config files are protected"
    segs = [p for p in normalized.split("/") if p]
    if any(s.lower() == ".ssh" for s in segs):
        # Read-only carve-out: ``.ssh/config`` + ``.ssh/known_hosts*`` are
        # host/alias METADATA, not key material — a session that is allowed
        # to RUN ssh (which uses the keys without disclosing them) needs
        # them to know its destinations. Keys and every other file stay
        # denied, and ALL writes stay denied: a config write would plant
        # ``ProxyCommand`` (code execution on the operator's next ssh).
        if (not writing and len(segs) >= 2 and segs[-2].lower() == ".ssh"
                and (segs[-1].lower() == "config"
                     or segs[-1].lower().startswith("known_hosts"))):
            pass
        else:
            return "access to .ssh key material is denied"
    if writing and segs and segs[-1].lower() == ".env":
        return ".env files are write-protected"
    return ""


def resolve_path_for_session(
    ctx: PathPolicyContext,
    raw_path: str,
    *,
    writing: bool = False,
    realpath_verify: bool = False,
) -> PathResolution:
    """Resolve a single tool-arg path against the session's policy.

    Returns a structured ``PathResolution``. Never raises on
    user-visible policy decisions — surface ``error`` verbatim to the
    LLM when ``allowed`` is False.

    The ``realpath_verify`` flag is set by the satellite's interceptor
    on the second-pass check after running ``realpath()``. Semantically
    identical to a normal check; the flag is preserved in logs for
    diagnostics.
    """
    from services import path_roles  # lazy (mirrors _protected_path_denial) —
    # also used below for the Claude bg-output read exception; without this bind
    # any satellite-host read reaching that check NameErrors.
    # 1. Reject empty / NUL / non-string values up-front.
    if not raw_path or not isinstance(raw_path, str):
        return _reject("empty path")
    if "\x00" in raw_path:
        return _reject("path contains NUL character")
    # 2. Reject other-user tilde forms unconditionally.
    if is_other_user_tilde(raw_path):
        return _reject(
            "cannot resolve another OS user's home directory"
        )
    # Fail loud when an LLM-supplied tilde-path arrives but
    # the satellite's capabilities probe never reported a home_dir.
    # Previously expand_tilde silently passed `~/Desktop` through, which
    # then classified as relative and anchored to OTO_WORKSPACE_DIR —
    # confusing for the agent (it thought the path would resolve to the
    # OS Desktop).
    if (
        raw_path.startswith("~/") or raw_path == "~"
    ) and not ctx.home_dir and ctx.target_kind != "local":
        return _reject(
            "satellite has not reported a home directory yet — cannot "
            "expand '~' prefix"
        )
    # 3. Tilde expansion.
    expanded, _was_tilde = expand_tilde(raw_path, ctx.home_dir)
    # 4. Normalize.
    normalized = normalize_path(expanded, ctx.target_os)
    # 5. Collapse `..` segments.
    if "/" in normalized:
        normalized = os.path.normpath(normalized).replace("\\", "/")
    # 6. Classify.
    kind = classify_path(normalized)
    if kind == "invalid":
        return _reject("invalid path")

    # Credential / secret denylist (mirrors auth.path_policy). Runs for EVERY
    # kind here — agent_tree included, because the OAuth token dirs live at
    # users/{u}/.credentials/<provider>-tokens/ which IS an agent-tree path,
    # and the stdio interceptor / Docker-MCP callers trust this verdict
    # verbatim (they don't re-check via auth.path_policy). resolve_path_batch
    # loops this fn, so it's covered too.
    _denial = _protected_path_denial(normalized, writing=writing)
    if _denial:
        return _reject(_denial)

    # ----- Sandbox-virtual paths ---------------------------------------
    if kind == "sandbox_virtual":
        # Inside the agent tree: existing role-based RBAC applies (the
        # caller dispatches to auth.path_policy after we translate).
        # For local sessions, we don't translate — the caller passes the
        # raw path to its existing local-sandbox logic. For remote
        # sessions, we translate to the satellite-host path.
        if ctx.target_kind == "local":
            return PathResolution(
                access_path=normalized,
                allowed=True,
                path_ref=PathRef("agent_tree", normalized.lstrip("/")),
                sandbox_relative=normalized,
            )
        # Remote: translate to satellite-host.
        access = _virtual_to_satellite_host(normalized, ctx)
        if not access:
            return _reject("agent context incomplete for path translation")
        return PathResolution(
            access_path=access,
            allowed=True,
            path_ref=PathRef("agent_tree", normalized.lstrip("/")),
            is_remote_pull=not writing,  # Docker MCP reads pull-through
            is_remote_push=writing,
            sandbox_relative=normalized,
        )

    # ----- Relative paths ----------------------------------------------
    if kind == "relative":
        # otodock-CLI sessions know their REAL working directory (the folder
        # the user ran `otodock` in) — anchor every relative path there,
        # `..` included, exactly like the shell the author is thinking in.
        # posixpath.normpath collapses the dots; the absolute result flows
        # through the normal admission chain (session root → home / full-fs
        # matrix → RBAC), so `..` gains no new reach — it just stops lying
        # about its base. (A hypothetical local-session work_cwd resolves to
        # a host-absolute path, which the local branch below rejects —
        # fail closed.)
        if ctx.work_cwd:
            anchored = posixpath.normpath(
                ctx.work_cwd.rstrip("/") + "/" + normalized)
            return resolve_path_for_session(
                ctx, anchored, writing=writing,
                realpath_verify=realpath_verify,
            )
        # A leading `..` escapes the workspace anchor below, and the policy
        # can't know the caller's real cwd — any resolution we pick is a
        # guess (`../x` used to silently resolve to `/x` and deny with a
        # baffling error, or worse, land on a real sandbox path the author
        # never meant). Deny deterministically and say what to do instead.
        if normalized == ".." or normalized.startswith("../"):
            return _reject(
                "relative '..' paths resolve against the workspace root "
                "here, not your shell's working directory — state the "
                "absolute path instead"
            )
        # Anchor at sandbox-virtual workspace (OTO_WORKSPACE_DIR
        # convention). The MCP / native tool already resolves these
        # locally — we just emit the same form back.
        anchored = "/workspace/" + normalized
        return resolve_path_for_session(
            ctx, anchored, writing=writing, realpath_verify=realpath_verify,
        )

    # ----- Satellite-host absolute paths -------------------------------
    if ctx.target_kind == "local":
        # Local sandbox — no satellite. Reject absolute paths that
        # aren't sandbox-virtual.
        return _reject(
            "absolute paths outside the sandbox are not allowed in "
            "local sessions"
        )

    # First — maybe it's actually inside the synced tree but stated as
    # an absolute satellite-host path. Translate back to sandbox-virtual
    # so role-based RBAC still applies (same admission rule whether the
    # LLM writes /workspace/foo.png or the host-equivalent).
    virtual = _satellite_host_to_virtual(normalized, ctx)
    if virtual:
        return resolve_path_for_session(
            ctx, virtual, writing=writing, realpath_verify=realpath_verify,
        )

    # Pure satellite-host path (outside the agent tree): also block READING
    # the OS user's real .env secret files. Write is already denied above; an
    # agent's own in-tree workspace .env stays readable (that path resolves as
    # sandbox_virtual, never reaching this branch) — parity with local.
    if normalized.rstrip("/").rsplit("/", 1)[-1].lower() == ".env":
        return _reject(".env files are protected")

    # Claude Code CLI background-command output (.../claude-<uid>/.../tasks/
    # <id>.output): the agent's own ephemeral task output. Admit READS regardless
    # of the home / full-FS policy so Linux/macOS satellites reach parity with
    # Windows (whose temp dir happens to sit under $HOME). Ordered AFTER the
    # protected-path + .env denies above, so it can never weaken them.
    if not writing and path_roles.is_claude_bg_output_path(normalized):
        return PathResolution(
            access_path=normalized,
            allowed=True,
            path_ref=PathRef("satellite_host", normalized),
            is_remote_pull=True,
            is_remote_push=False,
        )

    # Claude-CLI session runtime tree (scratchpad, background-task outputs):
    # the session's OWN subtree under the satellite-reported
    # ``<tempdir>/claude-<uid>`` root, admitted read+write so the harness's
    # own machinery works in home-only mode. Session-scoped by the sid path
    # segment (shared-admin satellites host many platform users on one OS
    # uid — they can't name each other's session UUIDs). Ordered AFTER the
    # protected-path + .env denies above, so it can never weaken them; the
    # satellite mirrors this admission with realpath + ownership tightening
    # on the file push/pull channel.
    if _is_session_runtime_path(normalized, ctx):
        return PathResolution(
            access_path=normalized,
            allowed=True,
            path_ref=PathRef("satellite_host", normalized),
            is_remote_pull=not writing,
            is_remote_push=writing,
        )

    # otodock-CLI: extra per-session allowed roots — the arbitrary cwd the
    # user ran `otodock` in (its own subtree). Admitted AFTER the protected-path
    # / .env denials above (so it can never weaken them) and BEFORE the
    # home/full-fs matrix, so an out-of-home work_cwd is reachable without
    # enabling machine-wide full-fs. Roots are realpath-normalized at build time
    # (session_state); admission here is lexical, at exact parity with the home /
    # full-fs branches below.
    for _root in ctx.session_allowed_roots:
        if _root and _is_under(normalized, _root, ctx.target_os):
            return PathResolution(
                access_path=normalized,
                allowed=True,
                path_ref=PathRef("satellite_host", normalized),
                is_remote_pull=not writing,
                is_remote_push=writing,
            )

    # Pure satellite-host path. Apply the home / full-FS policy matrix.
    if ctx.allow_full_fs:
        return PathResolution(
            access_path=normalized,
            allowed=True,
            path_ref=PathRef("satellite_host", normalized),
            is_remote_pull=not writing,
            is_remote_push=writing,
        )

    # Home-only mode. Path must be under the owner's home dir.
    if not ctx.home_dir:
        return _reject(
            "home directory unknown for this satellite; ask the admin "
            "to enable full filesystem access for this machine"
        )
    if _is_under(normalized, ctx.home_dir, ctx.target_os):
        return PathResolution(
            access_path=normalized,
            allowed=True,
            path_ref=PathRef("satellite_host", normalized),
            is_remote_pull=not writing,
            is_remote_push=writing,
        )
    return _reject(
        f"path '{raw_path}' is outside the OS user's home directory; "
        "ask the user to enable full filesystem access for this "
        "machine if broader access is needed"
    )


def resolve_path_batch(
    ctx: PathPolicyContext,
    items: list[ResolveItem],
) -> list[PathResolution]:
    """Batched form. The hook always uses this — single round-trip per
    tool call regardless of arg count. Output order matches input.
    """
    return [
        resolve_path_for_session(
            ctx,
            item.raw_path,
            writing=item.write,
            realpath_verify=item.realpath_verify,
        )
        for item in items
    ]


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def context_from_security(security_ctx: object) -> PathPolicyContext:
    """Build a ``PathPolicyContext`` from an
    ``auth.path_policy.SecurityContext`` instance.

    Used by ``check_tool_access`` so native tools (Read / Edit / Glob)
    flow through the same policy as MCP tool args. Lazy import keeps
    this module free of auth dependencies.
    """
    # Avoid circular import — security_ctx is duck-typed.
    target_kind = getattr(security_ctx, "target_kind", "local")
    target_machine_id = getattr(security_ctx, "target_machine_id", "")
    target_agents_dir = getattr(security_ctx, "target_agents_dir", "")
    target_home_dir = getattr(security_ctx, "target_home_dir", "")
    target_allow_full_fs = bool(
        getattr(security_ctx, "target_allow_full_fs", False)
    )
    role = getattr(security_ctx, "role", "manager") or "manager"
    # NOTE: SecurityContext.username is the filesystem slug, NOT the
    # user_sub. The policy resolver doesn't currently key on user_sub
    # so we leave it empty here; callers that need user_sub for
    # revocation checks fetch it separately.
    user_sub = ""
    agent_slug = getattr(security_ctx, "agent", "") or ""
    session_allowed_roots = tuple(
        getattr(security_ctx, "session_allowed_roots", ()) or ()
    )
    work_cwd = normalize_path(getattr(security_ctx, "work_cwd", "") or "")
    return PathPolicyContext(
        target_kind=target_kind or "local",
        machine_id=target_machine_id or "",
        home_dir=target_home_dir or "",
        target_agents_dir=target_agents_dir or "",
        allow_full_fs=target_allow_full_fs,
        target_os=_infer_target_os(target_agents_dir, target_home_dir),
        agent_slug=agent_slug,
        user_sub=user_sub,
        role=role,
        session_allowed_roots=session_allowed_roots,
        work_cwd=work_cwd,
        claude_runtime_root=getattr(
            security_ctx, "target_claude_runtime_root", "") or "",
        cli_session_id=getattr(security_ctx, "cli_session_id", "") or "",
    )


def _infer_target_os(agents_dir: str, home_dir: str) -> str:
    """Cheap OS inference from path shape — used when the caller
    didn't pre-populate ``target_os``. The full
    ``capabilities.os`` is preferred when available.
    """
    for s in (agents_dir, home_dir):
        if s and _WINDOWS_DRIVE_RE.match(s.replace("\\", "/")):
            return "windows"
    if "/Users/" in (home_dir or ""):
        return "darwin"
    return "linux"


# ---------------------------------------------------------------------------
# Per-turn target revocation check
# ---------------------------------------------------------------------------


def check_target_still_valid(security_ctx: object) -> str:
    """Detect mid-session pairing revocation.

    Returns an empty string when the session's cached target is still
    valid; otherwise a human-readable rejection reason the caller can
    surface to the LLM before tearing the session down.

    v1 scope: detect the most common revocation case — the admin
    deletes the satellite row. Subtler cases (user removes their
    per-agent override while the machine still exists) are detected by
    other downstream signals (the WS reconnect tears the session
    differently).

    Local sessions short-circuit to "valid" — there's nothing to revoke.
    """
    target_kind = getattr(security_ctx, "target_kind", "local")
    if target_kind not in ("admin_remote", "user_remote"):
        return ""
    machine_id = getattr(security_ctx, "target_machine_id", "") or ""
    if not machine_id:
        # An older session warmed up without the machine_id field. Treat
        # as valid to avoid false positives on transition.
        return ""
    # Late import to keep this module free of storage deps for tests.
    from storage import remote_store as _store
    machine = _store.get_remote_machine(machine_id)
    if not machine:
        return (
            "This chat's remote machine has been unpaired. "
            "Please start a new chat to continue."
        )
    return ""
