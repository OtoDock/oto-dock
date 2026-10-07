"""User and user-agent membership queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

import logging
from datetime import datetime, timezone
from typing import NamedTuple

from storage.pg import get_conn
from auth import roles
from core import layout

logger = logging.getLogger("claude-proxy.db-users")


def _make_username_slug(name: str, conn, taken: frozenset[str] = frozenset()) -> str:
    """Generate a filesystem-safe username slug from a display name.

    Lowercase, replace spaces/special chars with hyphens, deduplicate if
    collision. A name is taken by a live user, by a person deleted since
    (``retired_usernames``: their archived tree keeps the name for ever)
    and by anything in ``taken``, the names with a ``users/<name>``
    directory under an agent (``usernames_on_disk``, built by the caller
    BEFORE its connection opens: a newcomer must never inherit a tree).
    """
    import re
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        slug = "user"
    # Deduplicate
    base = slug
    counter = 1
    while True:
        existing = slug in taken or conn.execute(
            "SELECT 1 FROM users WHERE username=%s "
            "UNION ALL SELECT 1 FROM retired_usernames WHERE username=%s",
            (slug, slug),
        ).fetchone()
        if not existing:
            break
        counter += 1
        slug = f"{base}-{counter}"
    return slug


def usernames_on_disk() -> frozenset[str]:
    """Every name with a ``users/<name>`` directory under some agent
    directory (dot entries of ``AGENTS_DIR`` skipped: the offboarding
    archive lives in one). A scan that fails is logged and reads as none."""
    import os
    import config as _cfg
    names: set[str] = set()
    try:
        with os.scandir(_cfg.AGENTS_DIR) as agents:
            for agent in agents:
                if agent.name.startswith(".") or not agent.is_dir(follow_symlinks=False):
                    continue
                users = os.path.join(agent.path, layout.USERS)
                try:
                    with os.scandir(users) as homes:
                        names.update(h.name for h in homes)
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("The agent trees could not be scanned for usernames", exc_info=True)
    return frozenset(names)


def upsert_user(sub: str, email: str, name: str, role: str,
                display_name: str = "") -> str | None:
    """Create or refresh a user row; returns the platform role it had
    before (None for a new user), so a login that lowers it can run the
    offboarding hook."""
    # The slug dedup in _make_username_slug is SELECT-then-INSERT — under two
    # concurrent first-logins with the same display name both can see a slug
    # as free. The uq_users_username partial unique index is the arbiter; the
    # loser lands here again and re-dedupes against the winner's row.
    # The names in use on disk, read before the transaction opens (a
    # scan must not hold a pooled connection idle) and only when a slug is
    # about to be minted.
    taken = frozenset() if get_username_by_sub(sub) else usernames_on_disk()
    for _attempt in range(3):
        try:
            return _upsert_user_once(sub, email, name, role, display_name, taken)
        except Exception as exc:
            if "uq_users_username" in str(exc) and _attempt < 2:
                continue
            raise


def _upsert_user_once(sub: str, email: str, name: str, role: str,
                      display_name: str = "", taken: frozenset[str] = frozenset()) -> str | None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT created_at, default_agent, username, display_name, role FROM users WHERE sub=%s",
            (sub,),
        ).fetchone()
        created = existing["created_at"] if existing else now
        default_agent = existing["default_agent"] if existing else ""
        username = existing["username"] if existing and existing["username"] else ""
        # Keep existing display_name if not provided in this call
        existing_display = existing["display_name"] if existing else ""
        final_display = display_name or existing_display or ""
        # Generate username slug on first login
        if not username:
            username = _make_username_slug(name, conn, taken)
        conn.execute(
            """INSERT INTO users
               (sub, email, name, role, created_at, last_login, default_agent, username, display_name)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (sub) DO UPDATE SET email=EXCLUDED.email, name=EXCLUDED.name, role=EXCLUDED.role,
                   last_login=EXCLUDED.last_login, default_agent=EXCLUDED.default_agent,
                   username=EXCLUDED.username, display_name=EXCLUDED.display_name""",
            (sub, email, name, role, created, now, default_agent, username, final_display),
        )
        conn.commit()
    return existing["role"] if existing else None


def update_user_display_name(sub: str, display_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET display_name = %s WHERE sub = %s",
            (display_name, sub),
        )
        conn.commit()


def get_user(sub: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE sub=%s", (sub,)).fetchone()
        return dict(row) if row else None


def get_username_by_sub(sub: str) -> str | None:
    """Return filesystem-safe username slug for a user_sub, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE sub=%s", (sub,)
        ).fetchone()
        return row["username"] if row and row["username"] else None


def get_user_sub_by_username(username: str) -> str | None:
    """Reverse lookup: return user_sub for a filesystem username slug, or None."""
    if not username:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sub FROM users WHERE username=%s", (username,)
        ).fetchone()
        return row["sub"] if row else None


def list_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY email").fetchall()
        users = []
        for r in rows:
            u = dict(r)
            agents = conn.execute(
                "SELECT agent, COALESCE(agent_role, 'viewer') AS agent_role FROM user_agents WHERE sub=%s ORDER BY agent",
                (u["sub"],),
            ).fetchall()
            u["agents"] = [a["agent"] for a in agents]
            u["agent_roles"] = {a["agent"]: a["agent_role"] for a in agents}
            users.append(u)
        return users


def get_user_agents(sub: str) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent FROM user_agents WHERE sub=%s ORDER BY agent", (sub,)
        ).fetchall()
        return [r["agent"] for r in rows]


def get_user_agent_roles(sub: str) -> dict[str, str]:
    """Return {agent_name: role} for a user's agent assignments."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COALESCE(agent_role, 'viewer') AS agent_role FROM user_agents WHERE sub=%s ORDER BY agent",
            (sub,),
        ).fetchall()
        return {r["agent"]: r["agent_role"] for r in rows}


def get_agent_users(agent_slug: str) -> list[dict]:
    """Return all users attached to an agent with their per-agent role.

    ``POST /v1/admin/agents/{slug}/reseed-template-items`` uses
    this to iterate every (user, role) pair. Each dict has ``sub`` +
    ``agent_role`` keys.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sub, COALESCE(agent_role, 'viewer') AS agent_role "
            "FROM user_agents WHERE agent = %s ORDER BY sub",
            (agent_slug,),
        ).fetchall()
        return [{"sub": r["sub"], "agent_role": r["agent_role"]} for r in rows]


def get_agent_users_with_profile(agent_slug: str) -> list[dict]:
    """Like :func:`get_agent_users` but enriched with each user's profile
    (name / display_name / username / email / platform role) for the agent
    settings "Users" overview. LEFT JOIN so an orphaned ``user_agents`` row
    (no matching ``users`` row) still shows up rather than silently vanishing.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ua.sub, COALESCE(ua.agent_role, 'viewer') AS agent_role, "
            "       u.name, u.display_name, u.username, u.email, u.role AS platform_role "
            "FROM user_agents ua LEFT JOIN users u ON u.sub = ua.sub "
            "WHERE ua.agent = %s "
            "ORDER BY COALESCE(NULLIF(u.display_name, ''), NULLIF(u.name, ''), ua.sub)",
            (agent_slug,),
        ).fetchall()
        return [
            {
                "sub": r["sub"],
                "agent_role": r["agent_role"],
                "name": r["name"] or "",
                "display_name": r["display_name"] or "",
                "username": r["username"] or "",
                "email": r["email"] or "",
                "platform_role": r["platform_role"] or "",
            }
            for r in rows
        ]


class MembershipChange(NamedTuple):
    """What ``set_user_agents`` replaced and wrote, read under the person's
    row lock: the platform role and the rows (``{agent: role}``) the
    offboarding hook compares (``services/agents/offboarding.py``)."""
    platform_role: str | None
    before: dict[str, str]
    after: dict[str, str]


def set_user_agents(sub: str, agents: list[str], assigned_by: str,
                    agent_roles: dict[str, str] | None = None) -> MembershipChange:
    """Set agent assignments. agent_roles maps agent→role
    ('manager'|'editor'|'viewer')

    Also ensures user workspace/context directories exist for each assigned agent.

    Cleanup on removal: when a user loses access to an agent, hard-delete that
    user's user-scope scheduled items (tasks/triggers/notifications) under
    that agent. Added with community-agents — both manually-created
    user-scope items and template-seeded items would otherwise keep firing
    against a user no longer permitted to use the agent.

    The person's ``users`` row is locked for the transaction: every other
    writer of their rows or role (``add_user_agent``'s foreign-key check,
    ``update_user_role``, ``delete_user``) waits, so the returned diff is
    exact. Blocking: call it from a worker thread. Nothing after the commit
    raises (a change the caller cannot see would skip the hook).
    """
    now = datetime.now(timezone.utc).isoformat()
    username = get_username_by_sub(sub)
    after = {agent: (agent_roles or {}).get(agent, roles.VIEWER) for agent in agents}
    with get_conn() as conn:
        urow = conn.execute("SELECT role FROM users WHERE sub=%s FOR UPDATE", (sub,)).fetchone()
        before = _agent_roles_in(conn, sub)
        removed = set(before) - set(after)
        for agent in removed:
            _cascade_remove_user_scope_items(conn, sub, agent)
        conn.execute("DELETE FROM user_agents WHERE sub=%s", (sub,))
        for agent, role in after.items():
            conn.execute(
                "INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) VALUES (%s,%s,%s,%s,%s)",
                (sub, agent, now, assigned_by, role),
            )
        conn.commit()
    try:
        for agent in removed:
            _remove_user_scope_memory(sub, username, agent)
        # Create user workspace/context dirs for newly assigned agents
        _ensure_user_agent_dirs(sub, agents)
        # If this left the user with a single agent and no favorite, adopt it.
        maybe_autoset_default_agent(sub)
    except Exception:
        logger.exception("Post-commit steps of the membership change of %s failed", sub)
    return MembershipChange(urow["role"] if urow else None, before, after)


def _agent_roles_in(conn, sub: str) -> dict[str, str]:
    return {
        row["agent"]: row["agent_role"]
        for row in conn.execute(
            "SELECT agent, COALESCE(agent_role, 'viewer') AS agent_role "
            "FROM user_agents WHERE sub=%s", (sub,)
        ).fetchall()
    }


def _cascade_remove_user_scope_items(conn, sub: str, agent: str | None) -> None:
    """Hard-delete user-scope tasks/triggers/notifications owned by ``sub``
    under ``agent`` (None: under every agent, a deletion). Called from
    set_user_agents when (sub, agent) is removed.

    Catches both manually-created user-scope items and template-seeded items.
    Agent-scope items are owned by the agent itself and stay.
    """
    # Drop any user_remote_targets row pointing at this (sub, agent)
    # so the user's machine is no longer targeted by an agent they no longer
    # have access to.
    conn.execute(
        "DELETE FROM user_remote_targets WHERE user_sub = %s AND (agent_slug = %s OR %s::text IS NULL)",
        (sub, agent, agent),
    )
    conn.execute(
        "DELETE FROM dynamic_tasks WHERE (agent = %s OR %s::text IS NULL) AND created_by = %s "
        "AND scope = 'user'",
        (agent, agent, sub),
    )
    conn.execute(
        "DELETE FROM triggers WHERE (agent = %s OR %s::text IS NULL) AND created_by = %s "
        "AND scope = 'user'",
        (agent, agent, sub),
    )
    # User-scope notifications target the specific user (target = sub) for the
    # given agent. Delete their deliveries first to avoid orphaned rows.
    conn.execute(
        "DELETE FROM notification_deliveries "
        "WHERE user_sub = %s "
        "AND notification_id IN ("
        "  SELECT id FROM notifications "
        "  WHERE (agent_slug = %s OR %s::text IS NULL) AND scope = 'user' AND target = %s"
        ")",
        (sub, agent, agent, sub),
    )
    conn.execute(
        "DELETE FROM notifications WHERE (agent_slug = %s OR %s::text IS NULL) AND scope = 'user' "
        "AND target = %s",
        (agent, agent, sub),
    )
    # An app the person accepted into the agent returns to their "Shared
    # with you" section to place elsewhere (SHARING.md); the read-time
    # membership join already hides the placement, this keeps the row true.
    if agent is not None:
        from storage.sharing import share_store
        share_store.reap_person_placements(conn, sub, agent)


def _remove_user_scope_memory(sub: str, username: str | None, agent: str) -> None:
    """Delete ``agents/{agent}/users/{username}/context/memory/`` after the
    removal commits, so a later reassignment doesn't resurrect ghost
    memories (git history in the per-user context repo still allows
    recovery). Beneath the agent root with no symlink followed: the person's
    own sessions write under ``users/{username}``. Best-effort: a missing
    dir or username is silently ignored."""
    if not username:
        return
    try:
        import config as _cfg
        from services.infra import safe_fs
        # From AGENTS_DIR with the agent as the first component: the agent's
        # own folder is not a root (a sidecar can swap it for a link).
        rel = f"{agent}/{layout.USERS}/{username}/{layout.CONTEXT}/memory"
        safe_fs.rmtree_beneath(_cfg.AGENTS_DIR, rel)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"User-scope memory cleanup of {sub} on agent {agent} failed: {exc}")


def _ensure_user_agent_dirs(sub: str, agents: list[str]) -> None:
    """Create users/{username}/workspace and users/{username}/context dirs
    for each agent the user is assigned to. Safe to call repeatedly.
    Skips Shared-only agents — they have no per-user directories.
    Also initializes a git repo in users/{username}/context/ so
    per-user docs + memory/ are audit-tracked from day one.
    """
    try:
        username = get_username_by_sub(sub)
        if not username:
            return
        import config as _cfg
        from core.session.visibility import is_shared_only
        for agent_name in agents:
            if is_shared_only(agent_name):
                continue  # Shared-only agents are agent-scope; no per-user dirs
            agent_dir = _cfg.get_agent_dir(agent_name)
            if not agent_dir.exists():
                continue
            user_dir = layout.user_dir(agent_dir, username)
            (user_dir / layout.WORKSPACE).mkdir(parents=True, exist_ok=True)
            (user_dir / "context").mkdir(parents=True, exist_ok=True)
            try:
                from services.infra import git_writer
                git_writer.init_if_missing(user_dir / "context")
            except Exception as exc:
                # Non-critical
                logger.debug(f"Git init of user context dir for {agent_name} failed: {exc}")
            # Bind this user's folder to its XFS project ID + limit before any
            # write (no-op unless the kernel quota tier is enabled).
            try:
                from services.infra import storage_quota
                storage_quota.ensure_scope(agent_name, "user", username)
            except Exception as exc:
                # Non-critical — the quota sweep re-asserts assignment
                logger.debug(f"Quota scope binding for {agent_name} failed: {exc}")
    except Exception as exc:
        # Non-critical — dirs will be created on first access anyway
        logger.debug(f"Ensuring user-agent dirs failed: {exc}")


def set_user_agent_role(sub: str, agent: str, role: str) -> None:
    """Set per-agent role for a single agent assignment."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_agents SET agent_role=%s WHERE sub=%s AND agent=%s",
            (role, sub, agent),
        )
        conn.commit()


def add_user_agent(
    sub: str, agent: str, role: str, assigned_by: str,
) -> bool:
    """Idempotently attach ``sub`` to ``agent`` with ``role``.

    Returns True when a new row was inserted, False if the (sub, agent) pair
    already existed (PK conflict). Does NOT remove any other user_agents
    rows — unlike :func:`set_user_agents`, this is purely additive, which is
    what ``services/community/default_agent_assigner.py`` needs to avoid disturbing
    pre-existing assignments while looping over default agents.

    Creates the user's per-agent workspace + context dirs as a side effect
    (same as ``set_user_agents``).
    """
    now = datetime.now(timezone.utc).isoformat()
    inserted = False
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (sub, agent) DO NOTHING",
            (sub, agent, now, assigned_by, role),
        )
        conn.commit()
        inserted = cur.rowcount > 0
    if inserted:
        _ensure_user_agent_dirs(sub, [agent])
        # New attach (e.g. new-user auto-attach): if this is now the user's only
        # agent and they have no favorite yet, adopt it.
        maybe_autoset_default_agent(sub)
    return inserted


def update_user_role(sub: str, role: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET role=%s WHERE sub=%s", (role, sub))
        conn.commit()


def delete_user(sub: str) -> bool:
    """Delete the person. Their ``user_agents`` rows cascade with the row;
    their user-scope tasks, triggers and notifications (no foreign key to
    ``users``) go in the same transaction on every agent (a platform admin
    holds them on agents without a row), so none fires for a person who no
    longer exists. Their username retires with them (``retired_usernames``):
    never minted again, and the offboarding archive finds their trees by
    it. A name retired once before (an older dump restored, the person
    deleted again) is stamped afresh."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM users WHERE sub=%s FOR UPDATE", (sub,)).fetchone()
        for agent in _agent_roles_in(conn, sub):
            _cascade_remove_user_scope_items(conn, sub, agent)
        _cascade_remove_user_scope_items(conn, sub, None)
        # The apps they placed in agents and departments stay with the
        # team (SHARING.md): the shares pass to the install owner before
        # the row cascades the rest.
        from storage.sharing import share_store
        share_store.hand_over_team_shares(conn, sub)
        if row and row["username"]:
            conn.execute(
                """INSERT INTO retired_usernames (username, sub, retired_at, archived_at)
                   VALUES (%s, %s, %s, '')
                   ON CONFLICT (username) DO UPDATE
                   SET sub=EXCLUDED.sub, retired_at=EXCLUDED.retired_at, archived_at=''""",
                (row["username"], sub, now),
            )
        cur = conn.execute("DELETE FROM users WHERE sub=%s", (sub,))
        conn.commit()
        return cur.rowcount > 0


def remove_user_scope_items(sub: str, agents: list[str] | None) -> None:
    """The person's user-scope tasks, triggers and notifications on
    ``agents`` (None: every agent) go, as a removal from an agent cascades
    them. The offboarding transfer calls this for the agents a person lost
    without a membership row there."""
    with get_conn() as conn:
        for agent in ([None] if agents is None else agents):
            _cascade_remove_user_scope_items(conn, sub, agent)
        conn.commit()


def list_retired_usernames(*, unarchived_only: bool = False) -> list[dict]:
    with get_conn() as conn:
        where = "WHERE archived_at = ''" if unarchived_only else ""
        rows = conn.execute(
            f"SELECT username, sub, retired_at, archived_at FROM retired_usernames {where} "
            "ORDER BY retired_at").fetchall()
        return [dict(r) for r in rows]


def retired_subs() -> set[str]:
    with get_conn() as conn:
        return {r["sub"] for r in conn.execute("SELECT sub FROM retired_usernames").fetchall()}


def mark_username_archived(username: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE retired_usernames SET archived_at=%s WHERE username=%s",
                     (datetime.now(timezone.utc).isoformat(), username))
        conn.commit()


def drop_retired_username(username: str) -> bool:
    """The person is back (a dump from before their deletion was restored):
    the name is theirs again."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM retired_usernames WHERE username=%s", (username,))
        conn.commit()
        return cur.rowcount > 0


def get_user_default_agent(sub: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT default_agent FROM users WHERE sub=%s", (sub,)
        ).fetchone()
        return row["default_agent"] if row and row["default_agent"] else None


def set_user_default_agent(sub: str, agent: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET default_agent=%s WHERE sub=%s", (agent, sub)
        )
        conn.commit()


def maybe_autoset_default_agent(sub: str) -> None:
    """Adopt a user's sole agent as their favorite (default) when they have no
    favorite yet.

    A user with exactly ONE accessible agent and an empty
    ``default_agent`` gets that agent set as default — so they land straight in
    its chat (with prewarmup + the star pre-filled) with zero clicks. Once they
    have more than one agent, nothing happens automatically; they pick.

    Idempotent and conservative: never overrides an existing favorite, and no-ops
    for 0 or >1 agents. Called from the assignment chokepoints (set_user_agents /
    add_user_agent) so it covers admin-assign AND new-user auto-attach.
    """
    with get_conn() as conn:
        urow = conn.execute(
            "SELECT default_agent FROM users WHERE sub=%s", (sub,)
        ).fetchone()
        if not urow or (urow["default_agent"] or ""):
            return  # user gone, or already has a favorite
        # Count user-facing agents the user is assigned to that still exist.
        # Shared-only service agents (collaborative=FALSE, scope=agent — e.g. the
        # phone caller) aren't sensible default landing agents, so they don't
        # auto-become a favorite.
        rows = conn.execute(
            "SELECT ua.agent FROM user_agents ua "
            "JOIN agents a ON a.slug = ua.agent "
            "WHERE ua.sub=%s "
            "AND NOT (a.collaborative = FALSE AND a.default_scope = 'agent')",
            (sub,),
        ).fetchall()
        if len(rows) == 1:
            conn.execute(
                "UPDATE users SET default_agent=%s WHERE sub=%s",
                (rows[0]["agent"], sub),
            )
            conn.commit()


# --- Local Auth User Functions ---


def email_taken_by_other(email: str, sub: str) -> bool:
    """Whether another account than ``sub`` already uses ``email`` (any
    case)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM users WHERE LOWER(email) = LOWER(%s) AND sub <> %s LIMIT 1",
            (email, sub),
        ).fetchone() is not None


def get_user_by_email(email: str) -> dict | None:
    """Look up user by email (for local login)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(email) = LOWER(%s)", (email,)
        ).fetchone()
        return dict(row) if row else None


def create_local_user(
    email: str, name: str, display_name: str, role: str,
    password_hash: str, *, is_owner: bool = False,
    must_change_password: bool = False,
) -> str:
    """Create a local user. Returns the generated sub (UUID).

    Thread-safe: holds connection for the entire check+insert.
    """
    taken = usernames_on_disk()
    with get_conn() as conn:
        # Check email uniqueness
        existing = conn.execute(
            "SELECT sub FROM users WHERE LOWER(email) = LOWER(%s)", (email,)
        ).fetchone()
        if existing:
            raise ValueError(f"Email already in use: {email}")
        try:
            sub = _insert_local_user(conn, email, name, display_name, role, password_hash,
                                     is_owner=is_owner,
                                     must_change_password=must_change_password, taken=taken)
            conn.commit()
        except Exception as e:
            conn.rollback()
            # The single-owner partial unique index (uq_users_single_owner) fired
            # — a second fresh-install setup raced this one. Surface as a clean
            # ValueError so the caller returns 409, not an opaque 500.
            if "uq_users_single_owner" in str(e):
                raise ValueError("Setup already completed") from e
            raise
        return sub


def create_first_owner(email: str, display_name: str, password_hash: str) -> str | None:
    """Create the setup's owner admin, only while no user exists: the
    owner's sub, or None when a user exists already.

    The count and the insert are one transaction under a table lock that
    conflicts with every insert into ``users`` (and with itself): a second
    setup waits for this one and finds its owner, a first SSO login whose
    insert is in flight is waited for and counted, and one arriving after
    the lock lands once the owner exists. Readers are not blocked. A writer
    holding ``users`` past the lock timeout raises ``LockNotAvailable``."""
    taken = usernames_on_disk()
    with get_conn() as conn:
        conn.execute("SET LOCAL lock_timeout = '10s'")
        conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        if conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"] > 0:
            conn.rollback()
            return None
        sub = _insert_local_user(conn, email, display_name, display_name, "admin", password_hash,
                                 is_owner=True, must_change_password=False, taken=taken)
        conn.commit()
        return sub


def _insert_local_user(conn, email: str, name: str, display_name: str, role: str,
                       password_hash: str, *, is_owner: bool, must_change_password: bool,
                       taken: frozenset[str]) -> str:
    """Insert one local account on ``conn`` (the caller commits); returns
    the generated sub."""
    import uuid
    sub = f"local:{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()
    username = _make_username_slug(name or display_name or email.split("@")[0], conn, taken)
    conn.execute(
        """INSERT INTO users
           (sub, email, name, role, created_at, last_login, default_agent,
            username, display_name, password_hash, auth_provider,
            is_owner, must_change_password, password_changed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (sub, email, name or display_name, role, now, now, "",
         username, display_name, password_hash, "local",
         is_owner, must_change_password, now),
    )
    return sub


def mark_default_agents_assigned(sub: str) -> None:
    """Set ``users.default_agents_assigned = TRUE`` so subsequent OIDC
    re-logins skip the default-agent assignment pass.

    Idempotency guard for ``default_agent_assigner.assign_default_agents``.
    Never reset — even if admin removes the user from the auto-attached
    agent, the bool stays true so the user isn't re-attached on next login.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET default_agents_assigned = TRUE WHERE sub = %s",
            (sub,),
        )
        conn.commit()


def is_default_agents_assigned(sub: str) -> bool:
    """Return whether the user has already had default agents assigned.

    False for missing users (defensive — gate caller should still check
    user-existence explicitly).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT default_agents_assigned FROM users WHERE sub = %s",
            (sub,),
        ).fetchone()
    if not row:
        return False
    return bool(row["default_agents_assigned"])


def set_user_password(sub: str, password_hash: str) -> None:
    """Update password hash, clear must_change_password flag. The change
    ends the person's every sign-in and agent session token minted before
    it: the password timeline and the token epoch move together."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash=%s, must_change_password=FALSE, "
            "password_changed_at=%s, token_epoch_at=%s WHERE sub=%s",
            (password_hash, now, now, sub),
        )
        conn.commit()


def bump_token_epoch(sub: str) -> str:
    """Move the person's token epoch to now (an admin's "sign out
    everywhere"): every session cookie and agent session token minted
    before it is refused from then on. Returns the stamp."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE users SET token_epoch_at=%s WHERE sub=%s", (now, sub))
        conn.commit()
    return now


def update_user_email(sub: str, email: str) -> None:
    with get_conn() as conn:
        # Check uniqueness
        existing = conn.execute(
            "SELECT sub FROM users WHERE LOWER(email) = LOWER(%s) AND sub!=%s",
            (email, sub),
        ).fetchone()
        if existing:
            raise ValueError(f"Email already in use: {email}")
        conn.execute("UPDATE users SET email=%s WHERE sub=%s", (email, sub))
        conn.commit()


def update_user_auth_fields(sub: str, **kwargs) -> None:
    """Generic update for auth-related fields (totp_*, local_only, etc.)."""
    if not kwargs:
        return
    allowed = {
        "auth_provider", "totp_secret_enc", "totp_enabled", "totp_recovery_enc",
        "failed_login_attempts", "last_failed_login", "locked_until",
        "local_only", "must_change_password",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [sub]
    with get_conn() as conn:
        conn.execute(f"UPDATE users SET {set_clause} WHERE sub=%s", values)
        conn.commit()


def clear_failed_logins(sub: str) -> None:
    """Reset the tarpit counter after a successful login — without this a
    user's lifetime typos accumulate and the exponential delay ratchets
    permanently toward the cap."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET failed_login_attempts = 0, "
            "last_failed_login = NULL WHERE sub=%s",
            (sub,),
        )
        conn.commit()


def record_failed_login(sub: str) -> None:
    """Atomically increment failed_login_attempts."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET failed_login_attempts = failed_login_attempts + 1, "
            "last_failed_login=%s WHERE sub=%s",
            (now, sub),
        )
        conn.commit()


def undo_failed_login(sub: str) -> None:
    """Give back one failure the login counted before a password check that
    never ran (the hash gate was full): one less, and no last failure when
    it was the only one. One statement, so a failure another attempt
    counts meanwhile is kept."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET failed_login_attempts = GREATEST(failed_login_attempts - 1, 0), "
            "last_failed_login = CASE WHEN failed_login_attempts <= 1 THEN NULL "
            "ELSE last_failed_login END WHERE sub=%s",
            (sub,),
        )
        conn.commit()


def reset_login_attempts(sub: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE sub=%s",
            (sub,),
        )
        conn.commit()


def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]


def count_admins() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role='admin'"
        ).fetchone()["cnt"]


def get_owner_sub() -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sub FROM users WHERE is_owner=TRUE"
        ).fetchone()
        return row["sub"] if row else None
