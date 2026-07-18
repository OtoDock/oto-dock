"""Shared environment builder for agent subprocesses.

Builds a minimal, allowlisted environment for sandboxed agent sessions.
Proxy-internal secrets (JWT_SECRET, VAPID keys, OIDC credentials, DATABASE_URL)
are never passed. PROXY_API_KEY is replaced with a session-scoped JWT token.

Used by all 3 execution layers: CLI, Codex, Direct LLM.
"""

import os

import config
from auth.session_token import create_session_token

# Prefixes of env vars safe to pass to agent subprocesses.
# Everything else is excluded (proxy secrets, OIDC, DB credentials).
_ALLOWLIST_PREFIXES = (
    "PATH", "HOME", "USER", "SHELL",
    "LANG", "LC_", "TERM", "TZ",
    "NODE", "NPM_", "NVM_",
    "PYTHON", "SSL_CERT", "REQUESTS_CA",
    "http_proxy", "https_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)


def build_session_env(
    session_id: str,
    agent_name: str,
    credential_env: dict[str, str] | None = None,
    username: str = "",
    user_role: str = "",
    user_sub: str = "",
) -> dict[str, str]:
    """Build a minimal env for an agent subprocess with session-scoped auth.

    - Allowlisted system vars only (PATH, LANG, NODE_*, etc.)
    - Session JWT token as PROXY_API_KEY (not the master key)
    - PROXY_URL for hook callbacks
    - Standard ``OTO_*`` platform env vars (incl. ``OTO_SESSION_ID``) auto-
      injected for every stdio MCP — community MCPs read these for scope-
      aware behavior with zero manifest declaration. See
      ``proxy/core/sandbox/oto_env.py``.
    - Per-user/infra MCP credentials from credential_resolver + manifest-
      declared ``path_env`` values from config_builder. All workspace path
      env vars come from manifest declarations or the OTO_* set — no
      hardcoded defaults here. See proxy/services/path_roles.py.
    """
    # Start with allowlisted system vars
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(key.startswith(prefix) for prefix in _ALLOWLIST_PREFIXES):
            env[key] = value

    # Session-scoped auth (replaces master PROXY_API_KEY).
    #
    # ``user_sub`` is encoded inside the JWT so when an agent subprocess
    # (e.g. mcps-mcp, schedules-mcp, notifications-mcp) calls back into the
    # proxy, the auth path resolves the call to the real user owning the
    # session instead of a synthetic ``session:<sid>`` placeholder. When
    # ``user_sub`` is empty (agent-scope service sessions with no human
    # owner) the auth path falls back to the legacy placeholder.
    #
    # If the caller doesn't pass ``user_sub`` and we have ``username``,
    # look up the user_sub by username — most local-session call sites
    # don't have user_sub on hand but always have username.
    if not user_sub and username:
        from storage.pg import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT sub FROM users WHERE username = %s", (username,),
            ).fetchone()
        if row:
            user_sub = row["sub"]
    env["PROXY_URL"] = f"http://127.0.0.1:{config.PORT}"
    env["PROXY_API_KEY"] = create_session_token(session_id, agent_name, user_sub)

    # Prevent "nested session" detection in dev
    env.pop("CLAUDECODE", None)

    # Belt-and-braces disable of Claude Code's built-in auto-memory
    # subsystem. Paired with ``autoMemoryEnabled: false`` in the
    # session ``settings.json`` (see ``core/sandbox/sandbox.py``) and the
    # session-start memory-dir wipe in ``ensure_persistent_claude_dir``.
    # Otodock memory-mcp is the single source of memory truth.
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

    # Pin/freeze the CLI version fleet-wide: disable Claude Code's auto-updater so
    # an install can't drift off the platform pin (the platform reconciles the
    # pinned version). Belt-and-braces with ``autoUpdates: false`` in settings.json
    # (core/sandbox/sandbox.py). See VERSIONS.md.
    env["DISABLE_AUTOUPDATER"] = "1"

    # The platform is the only skill SOURCE: keep
    # Claude Code's own bundled skills/workflows out of platform sessions —
    # they ship outside our install/approval/version-pinning. Platform skills
    # arrive via .claude/skills/ (skills_materializer). Mirrored on satellite
    # CLI spawns (cli_session/pty_session).
    env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"

    # Standard OTO_* platform env vars (incl. OTO_SESSION_ID) — community MCPs
    # read these for scope-aware paths without per-manifest declarations.
    from core.sandbox.oto_env import build_oto_env, resolve_memory_and_scope
    memory_user, memory_agent, default_scope = resolve_memory_and_scope(
        agent_name, username=username, user_role=user_role,
    )
    env.update(build_oto_env(
        agent_name=agent_name,
        username=username,
        user_sub=user_sub,
        user_role=user_role,
        session_id=session_id,
        memory_user_enabled=memory_user,
        memory_agent_enabled=memory_agent,
        default_scope=default_scope,
        task_type="",  # env_builder is for chat / non-task sessions
    ))

    # Per-user/infra MCP credentials from credential_resolver +
    # manifest-declared path_env values from config_builder. These come
    # AFTER OTO_* so a manifest can override an OTO_ var if it really
    # wants (rare; typically manifests use distinct env names).
    if credential_env:
        env.update(credential_env)

    return env
