"""Agent configuration store — DB operations for agent metadata and auto-context.

All functions are synchronous (called via asyncio.to_thread from async code).
Uses an in-memory cache for hot-path reads (agent lookups on every request).
"""

import json
import logging
import re
import threading
from datetime import datetime, timezone

import config
from storage.pg import get_conn

logger = logging.getLogger(__name__)

# In-memory cache: slug -> agent dict. Invalidated on every write.
_cache: dict[str, dict] | None = None
_cache_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invalidate_cache():
    global _cache
    _cache = None


def _get_cached() -> dict[str, dict]:
    """Return cached agents dict, populating from DB if needed."""
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        agents = get_all_agents()
        _cache = {a["slug"]: a for a in agents}
        return _cache


def _row_to_dict(row: dict) -> dict:
    d = dict(row)
    d["admin_only"] = bool(d["admin_only"])
    # ``collaborative`` ships with the visibility-modes column; coerce defensively
    # (older rows / partial selects default to True = collaborative).
    d["collaborative"] = bool(d.get("collaborative", True))
    return d


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_agent(slug: str) -> dict | None:
    """Get a single agent by slug (cached)."""
    return _get_cached().get(slug)


def get_all_agents() -> list[dict]:
    """Get all agents from DB, sorted by slug."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY slug"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_agent_slugs() -> list[str]:
    """Return all agent slugs (cached)."""
    return list(_get_cached().keys())


def agent_exists(slug: str) -> bool:
    """Check if agent exists (cached)."""
    return slug in _get_cached()


def is_admin_only(slug: str) -> bool:
    """Check if agent is admin-only (cached)."""
    agent = _get_cached().get(slug)
    return agent["admin_only"] if agent else False


def get_admin_only_slugs() -> set[str]:
    """Return set of admin-only agent slugs (cached)."""
    return {slug for slug, a in _get_cached().items() if a["admin_only"]}


# ---------------------------------------------------------------------------
# Delegation targets
# ---------------------------------------------------------------------------

def get_delegation_targets(agent_name: str) -> list[str]:
    """Return list of agent slugs this agent can delegate to."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT target_agent FROM agent_delegation_targets WHERE agent_name = %s ORDER BY target_agent",
            (agent_name,),
        ).fetchall()
        return [r["target_agent"] for r in rows]


def set_delegation_targets(agent_name: str, targets: list[str]) -> None:
    """Replace all delegation targets for an agent."""
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_delegation_targets WHERE agent_name = %s", (agent_name,))
        for target in targets:
            conn.execute(
                "INSERT INTO agent_delegation_targets (agent_name, target_agent) VALUES (%s, %s)",
                (agent_name, target),
            )
        conn.commit()


def get_all_delegation_targets() -> dict[str, list[str]]:
    """Return {agent: [targets]} for all agents with delegation targets."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, target_agent FROM agent_delegation_targets ORDER BY agent_name, target_agent"
        ).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r["agent_name"], []).append(r["target_agent"])
        return result


# ---------------------------------------------------------------------------
# Browser-control allowed origins
# ---------------------------------------------------------------------------

def get_browser_allowed_origins(agent_name: str) -> list[str]:
    """Return the web origins this agent's browser-control MCP may visit.

    Empty = no allow-list (any origin not hit by the manifest's blocked-origins
    default is reachable). Injected as PLAYWRIGHT_MCP_ALLOWED_ORIGINS.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT origin FROM agent_browser_origins WHERE agent_name = %s ORDER BY origin",
            (agent_name,),
        ).fetchall()
        return [r["origin"] for r in rows]


def set_browser_allowed_origins(agent_name: str, origins: list[str]) -> None:
    """Replace the browser allowed-origins list for an agent."""
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_browser_origins WHERE agent_name = %s", (agent_name,))
        for origin in origins:
            conn.execute(
                "INSERT INTO agent_browser_origins (agent_name, origin) VALUES (%s, %s)",
                (agent_name, origin),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_agent(
    slug: str,
    display_name: str,
    *,
    admin_only: bool = False,
    execution_path: str = "claude-code-cli",
    default_model: str = "",
    default_effort: str = "",
    created_by: str = "",
    color: str = "",
    description: str = "",
    community_template: str | None = None,
    community_template_version: str | None = None,
    default_scope: str = "user",
    collaborative: bool = True,
) -> dict:
    """Insert a new agent and return its record.

    ``community_template`` + ``community_template_version`` are set when the
    agent was installed from a community-agents template. NULL for
    agents created via the dashboard "Create New Agent" flow.

    ``default_scope`` drives the default scope arg for every scope-aware MCP
    (memory, tasks, notifications, triggers, meetings) when this agent runs
    a user-scope session. Operational agents (``system-admin``, ``caller``)
    seed to ``"agent"``; everything else seeds to ``"user"``.

    ``collaborative`` is the second visibility axis. With ``default_scope`` it
    selects the agent's mode: collaborative agents mount both the per-user and
    the shared agent spaces; non-collaborative agents are single-space
    (Personal only when scope=user, Shared only when scope=agent). See
    ``core/session/visibility.py``.
    """
    if default_scope not in ("user", "agent"):
        raise ValueError(f"default_scope must be 'user' or 'agent', got {default_scope!r}")
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agents
               (slug, display_name, admin_only, execution_path,
                default_model, default_effort, created_by,
                color, description, created_at, updated_at,
                community_template, community_template_version, default_scope,
                collaborative)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (slug, display_name, admin_only,
             execution_path, default_model, default_effort,
             created_by, color, description, now, now,
             community_template, community_template_version, default_scope,
             collaborative),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agents WHERE slug = %s", (slug,)).fetchone()
        result = _row_to_dict(row)
    _invalidate_cache()
    # Initialize git repo for the agent's config dir so agent.md /
    # context files have an audit trail from day one.
    # Also create the knowledge/ dir up front — bwrap mounts fail if the
    # source path doesn't exist, and the SandboxBuilder defensively mkdirs
    # it too, but creating here at agent-creation time keeps a clean
    # filesystem audit (knowledge/ visible immediately in the workspace
    # tree, even before any session has run).
    # Best-effort: failures are logged but don't block agent creation.
    try:
        from services.infra import git_writer
        agent_dir = config.AGENTS_DIR / slug
        (agent_dir / "config" / "context").mkdir(parents=True, exist_ok=True)
        (agent_dir / "knowledge").mkdir(parents=True, exist_ok=True)
        # Create workspace/ up front too. It otherwise materializes lazily at
        # first session (core/sandbox/sandbox.py), which is too late to stamp the XFS
        # project-inherit flag BEFORE the agent's first write — see ensure_scope.
        (agent_dir / "workspace").mkdir(parents=True, exist_ok=True)
        git_writer.init_if_missing(agent_dir / "config")
    except Exception:
        pass
    # Bind the shared bucket (workspace + knowledge + config) to its XFS project
    # ID and apply the current limit now, so lazily-created children (.claude/ …)
    # inherit it. No-op unless the kernel quota tier is enabled. Every agent's
    # shared workspace is metered the same, regardless of visibility mode.
    try:
        from services.infra import storage_quota
        storage_quota.ensure_scope(slug, "shared")
    except Exception:
        pass  # best-effort; the quota monitor sweep re-asserts assignment
    return result


def set_community_template_data(slug: str, data: dict) -> None:
    """Persist the parsed community-agent template as JSONB.

    The template is fetched from the catalog at install time but
    never written to disk. Persisting the parsed shape here lets
    ``on_user_added_to_agent`` re-load the template later (e.g. when a new
    user is attached to the agent post-install) and seed their per-user
    items without going back to the catalog.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE agents SET community_template_data = %s::jsonb, updated_at = %s WHERE slug = %s",
            (json.dumps(data), _now(), slug),
        )
        conn.commit()
    _invalidate_cache()


def get_community_template_data(slug: str) -> dict | None:
    """Return the persisted template JSON for an agent, or None if absent.

    Returns ``None`` for non-existent agents and for the default empty
    object so callers can treat the empty-template case as "no data".
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT community_template_data FROM agents WHERE slug = %s",
            (slug,),
        ).fetchone()
    if not row:
        return None
    data = row["community_template_data"]
    return data if data else None


def set_default_for_new_users_role(slug: str, role: str) -> None:
    """Set agents.default_for_new_users_role.

    Empty string disables the auto-attach. Non-empty must be one of
    ``viewer`` / ``editor`` / ``manager`` — the agents table CHECK
    constraint enforces this; the Python guard here gives a friendlier
    error message.
    """
    if role not in ("", "viewer", "editor", "manager"):
        raise ValueError(
            f"role must be one of '', 'viewer', 'editor', 'manager'; got {role!r}"
        )
    with get_conn() as conn:
        conn.execute(
            "UPDATE agents SET default_for_new_users_role = %s, updated_at = %s WHERE slug = %s",
            (role, _now(), slug),
        )
        conn.commit()
    _invalidate_cache()


def list_default_for_new_users_agents() -> list[dict]:
    """Return every agent with a non-empty default_for_new_users_role.

    ``services/community/default_agent_assigner.py`` calls this to find the
    set of agents new users should be auto-attached to.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agents "
            "WHERE default_for_new_users_role IS NOT NULL "
            "AND default_for_new_users_role <> '' "
            "ORDER BY slug"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def mark_setup_completed(slug: str) -> dict | None:
    """Set agents.setup_completed_at = NOW(). Idempotent."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE agents SET setup_completed_at = %s, updated_at = %s WHERE slug = %s",
            (now, now, slug),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agents WHERE slug = %s", (slug,)).fetchone()
        result = _row_to_dict(row) if row else None
    _invalidate_cache()
    return result


def update_agent(slug: str, **fields) -> dict | None:
    """Partial update of agent fields. Returns updated record or None."""
    allowed = {
        "display_name", "admin_only", "execution_path",
        "execution_paths", "default_model", "default_effort",
        "color", "description", "execution_target", "default_scope",
        "default_for_new_users_role", "collaborative",
        "default_execution_mode",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "default_scope" in updates and updates["default_scope"] not in ("user", "agent"):
        raise ValueError(
            f"default_scope must be 'user' or 'agent', got {updates['default_scope']!r}"
        )
    if "default_for_new_users_role" in updates and \
            updates["default_for_new_users_role"] not in ("", "viewer", "editor", "manager"):
        raise ValueError(
            f"default_for_new_users_role must be one of '', 'viewer', 'editor', 'manager'; "
            f"got {updates['default_for_new_users_role']!r}"
        )
    if "default_execution_mode" in updates and \
            updates["default_execution_mode"] not in ("", "interactive", "-p"):
        raise ValueError(
            f"default_execution_mode must be one of '', 'interactive', '-p'; "
            f"got {updates['default_execution_mode']!r}"
        )
    if not updates:
        return get_agent(slug)

    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [slug]

    with get_conn() as conn:
        conn.execute(f"UPDATE agents SET {set_clause} WHERE slug = %s", values)
        conn.commit()
        row = conn.execute("SELECT * FROM agents WHERE slug = %s", (slug,)).fetchone()
        result = _row_to_dict(row) if row else None
    _invalidate_cache()
    return result


def delete_agent(slug: str) -> bool:
    """Delete an agent and ALL data tied to it. Returns True if deleted.

    Deleting an agent is a full, clean teardown — after it (and the folder +
    recover-bin removal the API endpoint does), no row, file, session, chat, or
    machine attachment for this slug survives, so reinstalling the same slug
    starts from a blank slate (no stale remote-machine binding, no resurrectable
    chat URL). The whole thing runs in ONE transaction.

    Removed here:
      - MCP + RBAC: agent_mcps, agent_skills, user_agents,
        agent_delegation_targets, agent_browser_origins, agent_account_bindings
        (per-user MCP creds), service_agent_bindings, mcp_assignment_requests.
      - Scheduled items: dynamic_tasks, triggers, notifications +
        notification_deliveries; task_runs (execution history).
      - Chats: chat_search (no FK) + chats — the latter cascades to
        chat_messages / chat_plans / media_tokens via their chat_id FK. Covers
        both dashboard chats AND task-run rows (id ``task-…``), so a deleted
        agent's chat URLs (``/chat/<slug>/<id>``) 404 instead of replaying.
        Chatless media_tokens rows stamped with the agent (workspace mints,
        task-session artifacts) are deleted directly. pinned_apps rows
        (mini-app registry) and pinned_files rows (Dock file pins) likewise.
      - Remote-machine attachments: user_remote_targets (personal overrides);
        agent_remote_targets (admin defaults) cascade via their agents-FK.
      - File-sync state: sync_state, file_tombstones, file_author.
      - recover_bin metadata rows (the captured bytes under
        ``RECOVER_BIN_DIR/<slug>/`` are unlinked at the FS layer by the caller).
      - agent_api_keys (programmatic keys) + usage_records (per-agent cost
        ledger).
      - webhook_subscriptions: any remaining rows (the endpoint already ran the
        vendor-side DELETE for service-scope rows via cleanup_agent_subscriptions
        before this).
      - agent_memory_settings cascades via its agents-FK. Memory/workspace file
        content lives under the agent folder and is removed when it's unlinked.

    DETACHED (not deleted): phone_routes — telephony routes are admin-provisioned
    resources (DID + PBX binding), so the row survives with ``agent=''`` and
    ``enabled=FALSE`` for an admin to reassign, rather than tearing down the PBX.
    """
    with get_conn() as conn:
        # MCP + RBAC
        conn.execute("DELETE FROM agent_mcps WHERE agent_name = %s", (slug,))
        conn.execute("DELETE FROM agent_skills WHERE agent_name = %s", (slug,))
        conn.execute("DELETE FROM user_agents WHERE agent = %s", (slug,))
        conn.execute("DELETE FROM agent_delegation_targets WHERE agent_name = %s", (slug,))
        conn.execute("DELETE FROM agent_delegation_targets WHERE target_agent = %s", (slug,))
        conn.execute("DELETE FROM agent_browser_origins WHERE agent_name = %s", (slug,))
        conn.execute("DELETE FROM agent_account_bindings WHERE agent_name = %s", (slug,))
        conn.execute("DELETE FROM service_agent_bindings WHERE agent_name = %s", (slug,))
        conn.execute("DELETE FROM mcp_assignment_requests WHERE agent_slug = %s", (slug,))
        # Scheduled items + their delivery rows.
        conn.execute("DELETE FROM dynamic_tasks WHERE agent = %s", (slug,))
        conn.execute("DELETE FROM triggers WHERE agent = %s", (slug,))
        conn.execute(
            "DELETE FROM notification_deliveries "
            "WHERE notification_id IN (SELECT id FROM notifications WHERE agent_slug = %s)",
            (slug,),
        )
        conn.execute("DELETE FROM notifications WHERE agent_slug = %s", (slug,))
        # Chats + their search index. chat_messages / chat_plans / media_tokens
        # cascade off the chats delete via their chat_id FK; chat_search has no FK.
        conn.execute("DELETE FROM chat_search WHERE agent = %s", (slug,))
        conn.execute("DELETE FROM chats WHERE agent = %s", (slug,))
        # Chatless capability tokens stamped with this agent (workspace mints,
        # task-session artifacts) — their files live under the agent folder
        # being removed, so the rows would only ever serve 404s.
        conn.execute("DELETE FROM media_tokens WHERE agent = %s", (slug,))
        # Pinned mini-apps registry (shared + every user's personal rows —
        # their HTML files live under the agent folder being removed).
        conn.execute("DELETE FROM pinned_apps WHERE agent = %s", (slug,))
        # Dock file pins — references into the agent folder being removed
        # (scope rows for this agent's chats already died with the chats
        # delete above; this covers pins this agent placed on OTHER agents'
        # project docks too, since the files they point at are gone).
        conn.execute("DELETE FROM pinned_files WHERE agent = %s", (slug,))
        # Remote-machine attachments (personal; admin defaults cascade via FK).
        conn.execute("DELETE FROM user_remote_targets WHERE agent_slug = %s", (slug,))
        # File-sync bookkeeping.
        conn.execute("DELETE FROM sync_state WHERE agent_slug = %s", (slug,))
        conn.execute("DELETE FROM file_tombstones WHERE agent_slug = %s", (slug,))
        conn.execute("DELETE FROM file_author WHERE agent_slug = %s", (slug,))
        # Recover-bin metadata (captured bytes unlinked separately by the caller).
        conn.execute("DELETE FROM recover_bin WHERE agent_slug = %s", (slug,))
        # Programmatic agent API keys (dead once the agent is gone).
        conn.execute("DELETE FROM agent_api_keys WHERE agent = %s", (slug,))
        # Task-run history + per-agent usage/cost ledger.
        conn.execute("DELETE FROM task_runs WHERE agent = %s", (slug,))
        conn.execute("DELETE FROM usage_records WHERE agent = %s", (slug,))
        # Webhook trigger subscriptions. The endpoint's cleanup_agent_subscriptions
        # already ran the vendor-side DELETE for service-scope rows before we got
        # here; this drops any remaining rows so none reference the dead slug.
        conn.execute("DELETE FROM webhook_subscriptions WHERE agent = %s", (slug,))
        # Telephony routes are admin-provisioned resources (a DID + PBX binding),
        # so they are NOT deleted — just DETACH the agent and park the route
        # (disabled) for an admin to reassign. No PBX de-provisioning is needed:
        # the route and its provisioning survive, only the agent binding is cleared.
        conn.execute(
            "UPDATE phone_routes SET agent = '', enabled = FALSE, updated_at = %s "
            "WHERE agent = %s",
            (_now(), slug),
        )
        cursor = conn.execute("DELETE FROM agents WHERE slug = %s", (slug,))
        conn.commit()
        deleted = cursor.rowcount > 0
    _invalidate_cache()
    # Release the agent's storage-quota enforcement + alert state (the project
    # rows are kept as tombstones so IDs are never reused). Best-effort; no-op
    # unless the kernel quota tier is enabled.
    if deleted:
        try:
            from services.infra import storage_quota
            storage_quota.reclaim_agent(slug)
        except Exception:
            pass
    return deleted


# ---------------------------------------------------------------------------
# Persona filename migration (prompt.md → agent.md, pre-1.4 installs)
# ---------------------------------------------------------------------------

def migrate_persona_filenames() -> int:
    """One-shot startup sweep: converge every agent on ``config/agent.md``.

    ``prompt.md`` is the pre-1.4 persona filename. Per agent: only
    ``prompt.md`` → git-committed rename to ``agent.md``; both present and
    byte-identical → drop the duplicate ``prompt.md``; both and divergent →
    keep both (readers prefer ``agent.md``) and warn. Every removal records
    a sync tombstone so an idle satellite APPLIES the delete at next merge
    instead of resurrecting the old file, and the change is committed to the
    config audit repo so revert-from-history keeps a coherent trail.

    Runs in the lifespan before the app serves traffic (single process, no
    readers racing). Best-effort per agent — a failure leaves that agent on
    ``prompt.md``, which every reader still accepts. Returns the number of
    agents changed.
    """
    import time as _time

    from services.infra import git_writer
    from storage import file_author_store, file_tombstones_store

    migrated = 0
    for slug in get_agent_slugs():
        config_dir = config.AGENTS_DIR / slug / "config"
        old = config_dir / "prompt.md"
        new = config_dir / "agent.md"
        try:
            if not old.is_file():
                continue
            if new.is_file():
                if new.read_bytes() != old.read_bytes():
                    logger.warning(
                        "Agent %s has both agent.md and prompt.md with "
                        "different content — keeping both; agent.md wins at "
                        "read time", slug,
                    )
                    continue
                old.unlink()
                action = "removed duplicate config/prompt.md"
            else:
                old.rename(new)
                action = "renamed config/prompt.md to agent.md"
            file_tombstones_store.record(
                slug, "config/prompt.md", _time.time(),
                origin="persona-migration",
            )
            file_author_store.clear(slug, "config/prompt.md")
            file_tombstones_store.drop(slug, "config/agent.md")
            commit_targets = [new]
            if git_writer.is_tracked(config_dir, "prompt.md"):
                commit_targets.append(old)
            git_writer.commit_paths(
                config_dir, commit_targets, "Rename persona file to agent.md",
            )
            logger.info("Agent %s: %s", slug, action)
            migrated += 1
        except OSError as exc:
            logger.warning("Persona migration skipped for %s: %s", slug, exc)
    return migrated


# ---------------------------------------------------------------------------
# Slug Utilities
# ---------------------------------------------------------------------------

def sanitize_slug(name: str) -> str:
    """Convert a display name to a valid slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:40]
