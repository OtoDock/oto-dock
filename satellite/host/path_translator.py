"""Sandbox-style path translator for satellite-spawned MCP processes.

The proxy ships sandbox-style virtual paths to MCPs in env vars
(``/users/{u}/workspace``, ``/workspace``, ``/config``, ``/.claude``,
``/users/{u}/workspace/.screenshots/{session_id}``) — same convention as
local-sandboxed agents see via bwrap. The satellite has no bwrap, so this
module rewrites those values to real satellite filesystem paths
(``{agent_dir}/...``) before subprocess spawn.

This mirrors ``proxy/auth/path_policy._sandbox_to_host`` rules but resolves
against the satellite's per-agent directory instead of the platform host's.

Also handles the literal ``{session_id}`` token left in screenshots-style
roles so per-session subdirs end up correctly scoped.

In addition to env-var values (``translate_env``), the same translation is
applied to ``send_message`` prompt content (``translate_paths_in_text``) so
that platform-injected sandbox paths in attachments work uniformly across
local-sandboxed agents (bwrap-mounted) and remote-unsandboxed agents
(satellite filesystem).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Mirror of `proxy/services/path_roles.SESSION_ID_TOKEN`.
_SESSION_ID_TOKEN = "{session_id}"

# Matches sandbox-style absolute paths anywhere in a string. Six top-level
# prefixes — same set translated by ``translate_path``. The negative lookbehind
# excludes embeddings inside larger tokens (e.g. URLs ``http://x/users/...``,
# already-translated absolute paths ``/agent_dir/users/...``). The trailing
# character class delimits where a path ends — whitespace, common punctuation,
# and quoting characters break the match.
_BODY_RE = r"[^\s,;:\'\"`)(\]\[}{]+"
_SANDBOX_PATH_RE = re.compile(
    r"(?<![\w./-])"
    r"("
    rf"/users/{_BODY_RE}"
    rf"|/workspace(?:/{_BODY_RE})?"
    rf"|/knowledge(?:/{_BODY_RE})?"
    rf"|/config(?:/{_BODY_RE})?"
    rf"|/\.claude(?:/{_BODY_RE})?"
    rf"|/\.codex(?:/{_BODY_RE})?"
    r")"
)


def translate_path(value: str, agent_dir: Path, username: str) -> str:
    """Translate one sandbox-style virtual path to a satellite-absolute path.

    Args:
        value: env-var value as received from the proxy. May or may not be
            a sandbox-style path (only path-shaped values get rewritten;
            anything else passes through unchanged).
        agent_dir: satellite's agent dir, e.g. ``~/.oto-dock/agents/{slug}``
            (already resolved).
        username: session's username; "" for agent-scoped sessions
            (tasks/meetings/voice).

    Returns:
        The translated path, or the original ``value`` if it doesn't match
        any sandbox prefix.
    """
    p = value
    base = str(agent_dir).rstrip("/")

    # /.claude/x → {agent_dir}/users/{u}/.claude/x  (or workspace/.claude for agent-scoped)
    if p == "/.claude" or p.startswith("/.claude/"):
        suffix = p[len("/.claude") :]  # "" or "/x"
        if username:
            return f"{base}/users/{username}/.claude{suffix}"
        return f"{base}/workspace/.claude{suffix}"

    # /.codex/x → {agent_dir}/users/{u}/.codex/x  (parallel structure)
    if p == "/.codex" or p.startswith("/.codex/"):
        suffix = p[len("/.codex") :]
        if username:
            return f"{base}/users/{username}/.codex{suffix}"
        return f"{base}/workspace/.codex{suffix}"

    # /users/{u}/x → {agent_dir}/users/{u}/x
    if p == "/users" or p.startswith("/users/"):
        return f"{base}{p}"

    # /workspace/x → {agent_dir}/workspace/x
    if p == "/workspace" or p.startswith("/workspace/"):
        return f"{base}{p}"

    # /knowledge/x → {agent_dir}/knowledge/x  (universal — all roles see it;
    # RW only for owner per bwrap mount table on the proxy side, but
    # satellite has no bwrap so the satellite owner controls access at
    # the unix filesystem level)
    if p == "/knowledge" or p.startswith("/knowledge/"):
        return f"{base}{p}"

    # /config/x → {agent_dir}/config/x  (manager/admin only; if mounted)
    if p == "/config" or p.startswith("/config/"):
        return f"{base}{p}"

    # Anything else: pass through (URLs, opaque values, already-absolute paths
    # the MCP will use verbatim).
    return p


def expand_session_id(value: str, session_id: str) -> str:
    """Replace the literal ``{session_id}`` token, if present.

    The proxy emits this token for session-scoped path values (e.g. a custom
    `path_env` role) because session_id isn't known at config-build time. The
    satellite expands it here at process-spawn time.
    """
    if _SESSION_ID_TOKEN in value:
        return value.replace(_SESSION_ID_TOKEN, session_id)
    return value


def derive_username_from_cwd_relative(cwd_relative: str) -> str:
    """Recover the session's username from the proxy-supplied cwd_relative.

    Mirrors the convention in ``proxy/core/remote/remote_execution.py`` where:
      - user-scoped sessions: cwd_relative = "users/{username}"
      - agent-scoped sessions: cwd_relative = "workspace"

    Returns empty string for agent-scoped sessions.
    """
    if not cwd_relative:
        return ""
    parts = cwd_relative.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "users":
        return parts[1]
    return ""


def translate_env(
    env: dict,
    *,
    agent_dir: Path,
    username: str,
    session_id: str,
    multi_value_envs: dict | None = None,
) -> dict:
    """Translate every string value in an env dict.

    Returns a NEW dict — does not mutate the input. Non-string values pass
    through unchanged.

    Order of operations per value:
      1. Expand ``{session_id}`` token
      2. If the env var name is in ``multi_value_envs`` → split by the
         declared separator, translate each segment, drop empties, rejoin.
      3. Otherwise → translate the whole value as a single sandbox path.

    Args:
        env: env dict to translate.
        agent_dir: satellite's agent dir.
        username: session username; "" for agent-scoped.
        session_id: literal session id (replaces ``{session_id}`` tokens).
        multi_value_envs: ``{env_var: separator}`` map declaring which env
            vars carry separator-joined sandbox-path lists. Built by the
            proxy from manifest ``path_env`` decls + standard
            ``OTO_ALLOWED_ROOTS`` (see ``proxy/core/sandbox/oto_env.py``).
    """
    multi = multi_value_envs or {}
    out = dict(env)
    for key, value in list(out.items()):
        if not isinstance(value, str):
            continue
        expanded = expand_session_id(value, session_id)
        sep = multi.get(key)
        if sep:
            # Multi-value path-list env: split, translate each, drop empties.
            segments = [s for s in expanded.split(sep) if s]
            translated = [translate_path(s, agent_dir, username) for s in segments]
            translated = [t for t in translated if t]
            out[key] = sep.join(translated)
        else:
            out[key] = translate_path(expanded, agent_dir, username)
    return out


def translate_satellite_to_virtual_in_text(
    text: str,
    *,
    agents_dir: Path,
) -> str:
    """Reverse direction of ``translate_paths_in_text``: walk ``text`` and
    rewrite every satellite-host path that lives inside the synced agent
    tree back to its sandbox-virtual form (``/workspace/...``,
    ``/users/{u}/...``, ``/knowledge/...``, ``/config/...``).

    Used by the satellite's HTTP tunnel to translate path arguments in
    outgoing MCP tool-call request bodies. The proxy-side MCP only
    knows the platform-host agent tree; forwarding the satellite-native
    path (``C:\\Users\\X\\OtoDock\\agents\\slug\\workspace\\foo``) would
    fail its own ``_resolve_path`` check, so we rewrite to the
    sandbox-virtual form the MCP already understands.

    Tolerant of both backslash and forward-slash separators (Windows
    JSON serializers vary), case-insensitive prefix match for NTFS.
    Paths outside the synced tree pass through unchanged — the
    MCP will fail to find them, which is the correct behavior for v1
    (auto-fetching outside-tree files via the tunnel is a follow-up).
    """
    if not text or not agents_dir:
        return text
    # Build a forward-slash, case-normalized base for matching, but keep
    # the original separator-form in the suffix replacement so paths the
    # MCP sees use forward slashes (universal).
    base_fwd = str(agents_dir).replace("\\", "/").rstrip("/")
    base_norm = os.path.normcase(base_fwd)

    # Match any path that starts with the agents dir + slug + one of the
    # known sandbox subdirs. Slug can contain hyphens, underscores,
    # alnum. The trailing capture ends at whitespace / quoting chars.
    # We match both `/` and `\\` (escaped backslash from JSON) as path seps.
    pattern = re.compile(
        r"(?<![\w./\\-])"   # not in the middle of another token
        r"([A-Za-z]:[\\/]|/)"  # drive prefix `C:\` or `C:/` OR root `/`
        r"([^\s,;:'\"`)({}\[\]]+?)"  # body up to next break
        r"(?=[\s,;:'\"`)({}\[\]]|$)"
    )

    def _sub(match: re.Match) -> str:
        raw = match.group(0)
        # Normalize for comparison
        raw_fwd = raw.replace("\\\\", "/").replace("\\", "/")
        raw_norm = os.path.normcase(raw_fwd)
        if not raw_norm.startswith(base_norm + "/"):
            return raw
        # Strip base + agent-slug segment, keep the rest
        after_base = raw_fwd[len(base_fwd) + 1 :]  # "slug/users/X/foo"
        parts = after_base.split("/", 1)
        if len(parts) < 2:
            return raw
        rel = parts[1]  # "users/X/foo" / "workspace/foo" / etc.
        first = rel.split("/", 1)[0]
        if first not in ("users", "workspace", "knowledge", "config"):
            return raw  # not inside the synced subtree we care about
        return "/" + rel

    return pattern.sub(_sub, text)


def translate_paths_in_text(
    text: str,
    *,
    agent_dir: Path,
    username: str,
) -> str:
    """Rewrite all sandbox-style absolute paths in ``text`` to satellite-absolute.

    Applied to ``send_message`` prompt content right before it's written to the
    CLI/Codex subprocess. The proxy injects sandbox-virtual paths (``/users/{u}/...``,
    ``/workspace/...``) for chat-attached photos and files (see
    ``proxy/ws/dashboard.py::_handle_chat``). On local-sandboxed agents bwrap
    handles the mapping; on remote satellites we have no bwrap, so this
    function does the equivalent rewrite on the satellite side.

    Same translation rules as ``translate_path`` — applied via regex on the
    whole string. Sandbox-style absolute paths are unique enough that false
    positives are very unlikely; if a user types a literal ``/users/foo/...``
    in chat referring to a real platform path, translating it is the correct
    behavior (the agent would be working against that path anyway).

    Args:
        text: prompt content from the proxy.
        agent_dir: satellite's agent dir, e.g. ``~/.oto-dock/agents/{slug}``.
        username: session username; ``""`` for agent-scoped sessions.

    Returns:
        ``text`` with sandbox-style paths rewritten to satellite-absolute.
    """
    if not text:
        return text

    def _sub(match: re.Match) -> str:
        return translate_path(match.group(1), agent_dir, username)

    return _SANDBOX_PATH_RE.sub(_sub, text)


def translate_codex_mcp_env_paths(
    toml_text: str,
    *,
    agent_dir: Path,
    username: str,
    session_id: str,
) -> str:
    """Rewrite sandbox-virtual paths in a Codex MCP ``config.toml`` to
    satellite-absolute.

    Codex — unlike Claude CLI — does NOT propagate the daemon's process env to
    its MCP subprocesses, so the ``[mcp_servers.*]`` ``env = {…}`` inline tables
    are the ONLY copy of those vars the MCP ever sees. The proxy ships them in
    sandbox-virtual form (``/users/{u}/…``, ``/workspace``, ``/config``) — the
    same convention bwrap maps for local agents and ``translate_env`` maps into
    the Claude process env. On a remote satellite there is no bwrap and Codex
    skips the translated daemon env, so an untranslated value like
    ``WORKSPACE_MCP_CREDENTIALS_DIR=/users/{u}/.credentials/…`` points at a
    nonexistent root and the MCP dies at startup (e.g. google-workspace verifies
    its credentials dir on boot → ``[Errno 13] Permission denied: '/users'`` →
    Codex reports ``connection closed: initialize response``).

    Applied to the shipped MCP TOML before the interactive header / interceptor
    wrap. The shared ``_SANDBOX_PATH_RE`` only matches the six sandbox prefixes
    (so already-real ``command``/``args`` paths, loopback URLs, and JWT/token
    values are untouched), and ``OTO_TOOL_ARG_PATHS`` carries only tool names +
    JSONPath expressions (no sandbox filesystem paths), so it is preserved.
    """
    if not toml_text:
        return toml_text
    if session_id:
        toml_text = toml_text.replace(_SESSION_ID_TOKEN, session_id)
    # Substitute the sandbox-virtual prefixes → satellite-absolute, emitting
    # FORWARD slashes. config.toml is TOML, where a Windows backslash path inside a
    # basic ("...") string is parsed as escape sequences — ``C:\Users\…`` → ``\U``
    # → "too few unicode value digits, expected unicode hexadecimal value" → the
    # whole config fails to load. ``translate_path`` builds ``f"{str(agent_dir)}…"``
    # whose base is backslash-separated on Windows, so we normalise the substituted
    # value to forward slashes: TOML-safe AND a valid path for the (Python) MCP
    # subprocess on Windows — the same forward-slash convention codex_session /
    # codex_pty_session already use for ``~/.oto-dock``. No-op on Unix
    # (``translate_path`` already returns forward-slash paths there). NOTE: this is
    # TOML-specific — the plain ``translate_paths_in_text`` (used for prompt text,
    # not TOML) must NOT forward-slash, so we don't route through it here.
    def _sub(match: "re.Match") -> str:
        return translate_path(match.group(1), agent_dir, username).replace("\\", "/")

    return _SANDBOX_PATH_RE.sub(_sub, toml_text)
