"""Shares of apps and chats (SHARING.md).

All functions are synchronous (call them on the DB executor). A share is
LIVE when it is unrevoked, unexpired, ``active`` and not declined. An
internal share names a person, an agent or a department (``grantee_kind``)
and carries the role cap the sharer picked and the person's decision: a
person's app share is a grant from the moment it exists (it opens) and is
PLACED in the agent they chose once they accept it; an agent's or a
department's share is accepted when made and placed in every agent it names,
resolved at read time from the membership and department rows (nothing is
copied, so a person who leaves an agent, or an agent that leaves a
department, loses the placement at the next read). Access checks read ``internal_grant`` and
``placements_for_user``; the fan-outs read ``internal_grantees`` and
``placement_audience``; nothing here decides WHO may share — that stays
with the routes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from auth import roles
from storage.pg import get_conn

TARGET_KINDS = ("app", "chat")

# A share to people, agents and departments, or a link.
INTERNAL = "internal"
EXTERNAL = "external"
SCOPES = (INTERNAL, EXTERNAL)

# The recipient of an internal share.
PERSON = "person"
AGENT = "agent"
DEPARTMENT = "department"
GRANTEE_KINDS = (PERSON, AGENT, DEPARTMENT)

# The recipient's answer. A link carries "" in both columns.
PENDING = "pending"
ACCEPTED = "accepted"
DECLINED = "declined"
DECISIONS = (PENDING, ACCEPTED, DECLINED)

# The admin switches (SHARING.md "Platform switches"): unset means on; off
# refuses new shares of the kind, the existing ones stand (the operator,
# 2026-10-04).
SETTING_TO_AGENTS = "sharing_to_agents_enabled"
SETTING_TO_DEPARTMENTS = "sharing_to_departments_enabled"


class ShareExists(Exception):
    """An unrevoked, undeclined internal share for that (target, recipient)
    already exists (an expired or suspended one included)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _live_clause(alias: str = "s") -> str:
    """The SQL predicate for a live share (parameter: the current time)."""
    return (f"{alias}.revoked_at IS NULL AND {alias}.state = 'active' "
            f"AND {alias}.decision <> 'declined' "
            f"AND ({alias}.expires_at IS NULL OR {alias}.expires_at > %s)")


def _target_col(target_kind: str) -> str:
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"unknown share target kind {target_kind!r}")
    return "app_id" if target_kind == "app" else "chat_id"


def is_live(row: dict, now: str | None = None) -> bool:
    now = now or _now()
    return (row.get("revoked_at") is None and row.get("state") == "active"
            and row.get("decision") != DECLINED
            and (not row.get("expires_at") or row["expires_at"] > now))


# ───────────────────────── writes ───────────────────────────────────────────


def create_internal_share(*, target_kind: str, target_id: str, created_by: str,
                          grantee_kind: str = PERSON, grantee_sub: str | None = None,
                          grantee_agent: str | None = None,
                          grantee_department: str | None = None,
                          role_cap: str = roles.VIEWER, decision: str = PENDING,
                          decided_by: str | None = None,
                          expires_at: str | None = None) -> dict:
    """A share to a person, an agent or a department. A declined row for
    the same (target, recipient) is revoked first, in the same transaction,
    so the sharer's list never holds two rows for one recipient. Raises
    ShareExists on an unrevoked, undeclined duplicate (expired or suspended
    included)."""
    from psycopg import errors as pg_errors
    if grantee_kind not in GRANTEE_KINDS:
        raise ValueError(f"unknown grantee kind {grantee_kind!r}")
    if decision not in DECISIONS:
        raise ValueError(f"unknown share decision {decision!r}")
    if role_cap not in roles.AGENT_ROLES:
        raise ValueError(f"unknown role cap {role_cap!r}")
    col = _target_col(target_kind)
    share_id = str(uuid.uuid4())
    now = _now()
    recipient_col = {PERSON: "grantee_sub", AGENT: "grantee_agent",
                     DEPARTMENT: "grantee_department"}[grantee_kind]
    recipient = {PERSON: grantee_sub, AGENT: grantee_agent,
                 DEPARTMENT: grantee_department}[grantee_kind]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE shares SET revoked_at = %s WHERE target_kind = %s AND {col} = %s "
            f"AND scope = 'internal' AND grantee_kind = %s AND {recipient_col} = %s "
            "AND revoked_at IS NULL AND decision = %s",
            (now, target_kind, target_id, grantee_kind, recipient, DECLINED),
        )
        try:
            conn.execute(
                f"""INSERT INTO shares
                    (id, target_kind, {col}, scope, grantee_kind, grantee_sub,
                     grantee_agent, grantee_department, role_cap, decision,
                     decided_by, decided_at, created_by, created_at, expires_at)
                    VALUES (%s, %s, %s, 'internal', %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s)""",
                (share_id, target_kind, target_id, grantee_kind, grantee_sub,
                 grantee_agent, grantee_department, role_cap, decision,
                 decided_by, now if decided_by else None, created_by, now, expires_at),
            )
        except pg_errors.UniqueViolation:
            conn.rollback()
            raise ShareExists() from None
        row = dict(conn.execute("SELECT * FROM shares WHERE id=%s", (share_id,)).fetchone())
        conn.commit()
        return row


def create_external_share(*, target_kind: str, target_id: str, created_by: str,
                          token_hash: str, password_hash: str = "", public: bool = False,
                          allow_actions: bool = False, expires_at: str | None = None,
                          include_tools: bool = False) -> dict:
    """A link. The token itself is never stored, only its sha256; a link
    has no recipient kind and no decision."""
    col = _target_col(target_kind)
    share_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO shares
                (id, target_kind, {col}, scope, grantee_kind, decision, created_by,
                 created_at, expires_at, token_hash, password_hash, public, allow_actions,
                 include_tools)
                VALUES (%s, %s, %s, 'external', '', '', %s, %s, %s, %s, %s, %s, %s, %s)""",
            (share_id, target_kind, target_id, created_by, _now(), expires_at,
             token_hash, password_hash or "", bool(public), bool(allow_actions),
             bool(include_tools)),
        )
        row = dict(conn.execute("SELECT * FROM shares WHERE id=%s", (share_id,)).fetchone())
        conn.commit()
        return row


def set_decision(share_id: str, decision: str, decided_by: str,
                 placed_agent: str | None = None, *, expect: str | None = None) -> bool:
    """Record a recipient's answer (and, for a person's app, the agent it
    is placed in). An accepted share may be placed again; a revoked or
    declined row never changes; with ``expect`` the row must still hold
    that decision (a decline asks for ``pending``, so an accept that landed
    first wins). False when nothing changed."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown share decision {decision!r}")
    sql = ("UPDATE shares SET decision = %s, decided_by = %s, decided_at = %s, "
           "placed_agent = %s WHERE id = %s AND revoked_at IS NULL AND decision <> %s")
    params: list = [decision, decided_by, _now(), placed_agent, share_id, DECLINED]
    if expect is not None:
        sql += " AND decision = %s"
        params.append(expect)
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0


def set_placement_hidden(app_id: str, agent: str, user_sub: str, hidden: bool) -> None:
    """A member's own hide of a placed app in one agent. Idempotent."""
    with get_conn() as conn:
        if hidden:
            conn.execute(
                """INSERT INTO share_placement_hides (app_id, agent, user_sub, created_at)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (app_id, agent, user_sub) DO NOTHING""",
                (app_id, agent, user_sub, _now()),
            )
        else:
            conn.execute(
                "DELETE FROM share_placement_hides WHERE app_id=%s AND agent=%s AND user_sub=%s",
                (app_id, agent, user_sub),
            )
        conn.commit()


def hand_over_team_shares(conn, sub: str) -> int:
    """A person about to be deleted: their unrevoked agent and department
    shares (expired or suspended ones too) pass to the install owner (the ``is_owner`` row, else the oldest other
    admin) instead of cascading with their account, since an agent's or a
    department's placement is the team's, not the sharer's; their person
    shares and links go with them. Runs inside the caller's transaction."""
    cur = conn.execute(
        "UPDATE shares SET created_by = COALESCE("
        "  (SELECT u.sub FROM users u WHERE u.is_owner AND u.sub <> %s ORDER BY u.created_at LIMIT 1),"
        "  (SELECT u.sub FROM users u WHERE u.role = %s AND u.sub <> %s ORDER BY u.created_at LIMIT 1))"
        " WHERE created_by = %s AND grantee_kind = ANY(%s) AND revoked_at IS NULL"
        " AND EXISTS (SELECT 1 FROM users u WHERE (u.is_owner OR u.role = %s) AND u.sub <> %s)",
        (sub, roles.ADMIN, sub, sub, [AGENT, DEPARTMENT], roles.ADMIN, sub),
    )
    return cur.rowcount


def reap_person_placements(conn, sub: str, agent: str | None) -> int:
    """A person who lost ``agent`` (every agent when None) has their app
    shares placed there un-placed and pending again, so the share returns
    to their section to place elsewhere. Runs inside the caller's
    transaction (the membership removal)."""
    cur = conn.execute(
        "UPDATE shares SET placed_agent = NULL, decision = %s "
        "WHERE grantee_kind = %s AND grantee_sub = %s AND placed_agent IS NOT NULL "
        "AND (placed_agent = %s OR %s::text IS NULL) AND revoked_at IS NULL",
        (PENDING, PERSON, sub, agent, agent),
    )
    return cur.rowcount


def reap_unheld_placements(sub: str) -> int:
    """A person whose standing changed (a demoted admin held every agent):
    their app shares placed in an agent they no longer hold are un-placed
    and pending again, back in their section to place elsewhere, as a
    membership removal does (``reap_person_placements``). Its own
    transaction; nothing changes while they still hold the agent."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE shares s SET placed_agent = NULL, decision = %s "
            "WHERE s.grantee_kind = %s AND s.grantee_sub = %s AND s.placed_agent IS NOT NULL "
            "AND s.revoked_at IS NULL "
            f"AND NOT {_holds_agent_clause('s.grantee_sub', 's.placed_agent')}",
            (PENDING, PERSON, sub, roles.ADMIN),
        )
        conn.commit()
        return cur.rowcount


def get_share(share_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM shares WHERE id=%s", (share_id,)).fetchone()
        return dict(row) if row else None


def get_share_by_token_hash(token_hash: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM shares WHERE token_hash=%s AND scope='external'", (token_hash,),
        ).fetchone()
        return dict(row) if row else None


def set_allow_actions(share_id: str, on: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE shares SET allow_actions=%s WHERE id=%s AND revoked_at IS NULL",
            (bool(on), share_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_password_hash(share_id: str, password_hash: str) -> bool:
    """A new link password; every unlock cookie bound to the old one dies."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE shares SET password_hash=%s WHERE id=%s AND revoked_at IS NULL "
            "AND scope='external'",
            (password_hash, share_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_snapshot_ref(share_id: str, ref: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE shares SET snapshot_ref=%s WHERE id=%s", (ref, share_id))
        conn.commit()


def list_revoked_before(cutoff_iso: str, target_kind: str = "chat") -> list[dict]:
    """Revoked shares older than ``cutoff_iso`` (the snapshot reaper)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM shares WHERE target_kind=%s AND revoked_at IS NOT NULL "
            "AND revoked_at < %s AND snapshot_ref <> ''",
            (target_kind, cutoff_iso),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_snapshot_ref(share_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE shares SET snapshot_ref='' WHERE id=%s", (share_id,))
        conn.commit()


# A revoked row, and a declined one, is kept this long after its revoke or
# its decision (the sharer reads "declined by X" meanwhile), then the daily
# retention sweep deletes it (``delete_stale_shares``).
STALE_SHARE_DAYS = 30


def delete_stale_shares(*, dry_run: bool = False) -> int:
    """Delete the rows revoked, or declined (and not revoked), more than
    ``STALE_SHARE_DAYS`` ago; the count (what would go, on a dry run).
    No list shows a revoked row (the ``share-snapshots`` pass, which runs
    first, reads one to find a chat share's copy); the sharer's dialog and
    Admin → Shares show a declined one. No foreign key points at a share."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_SHARE_DAYS)).isoformat()
    where = ("(revoked_at IS NOT NULL AND revoked_at < %s) OR "
             "(revoked_at IS NULL AND decision = %s AND decided_at IS NOT NULL "
             "AND decided_at < %s)")
    params = (cutoff, DECLINED, cutoff)
    with get_conn() as conn:
        if dry_run:
            return conn.execute(f"SELECT COUNT(*) AS n FROM shares WHERE {where}",
                                params).fetchone()["n"]
        cur = conn.execute(f"DELETE FROM shares WHERE {where}", params)
        conn.commit()
        return cur.rowcount


def list_all_share_ids() -> set[str]:
    with get_conn() as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM shares").fetchall()}


def touch_access(share_id: str) -> None:
    """Once per document fetch of a link, never per sub-request."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE shares SET access_count = access_count + 1, last_access_at = %s WHERE id=%s",
            (_now(), share_id),
        )
        conn.commit()


def bump_actions_today(share_id: str, day: str, cap: int) -> int | None:
    """Count one action against the link's daily cap in one statement; the
    new count, or None when the cap is reached (or the share is not live)."""
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE shares
               SET actions_today = CASE WHEN actions_day = %s THEN actions_today + 1 ELSE 1 END,
                   actions_day = %s
               WHERE id = %s AND state = 'active' AND revoked_at IS NULL
                 AND (actions_day <> %s OR actions_today < %s)
               RETURNING actions_today""",
            (day, day, share_id, day, cap),
        ).fetchone()
        conn.commit()
        return int(row["actions_today"]) if row else None


def revoke_share(share_id: str, *, decisions: tuple[str, ...] | None = None) -> bool:
    """Revoke a share; with ``decisions`` only while its decision is one of
    them (a person's own revoke must not take a row a decline just
    answered). False when nothing changed."""
    sql = "UPDATE shares SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL"
    params: list = [_now(), share_id]
    if decisions is not None:
        sql += " AND decision = ANY(%s)"
        params.append(list(decisions))
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0


def set_share_state(share_id: str, state: str) -> bool:
    if state not in ("active", "suspended"):
        raise ValueError(f"unknown share state {state!r}")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE shares SET state = %s WHERE id = %s AND revoked_at IS NULL",
            (state, share_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_share_expiry(share_id: str, expires_at: str | None) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE shares SET expires_at = %s WHERE id = %s AND revoked_at IS NULL",
            (expires_at, share_id),
        )
        conn.commit()
        return cur.rowcount > 0


def suspend_app_shares(conn, app_id: str) -> int:
    """Mark the app's live shares suspended (a soft unpin), whatever their
    recipient. Runs inside the caller's transaction so it lands with the
    row's hide."""
    cur = conn.execute(
        "UPDATE shares SET state = 'suspended' WHERE target_kind = 'app' "
        "AND app_id = %s AND state = 'active' AND revoked_at IS NULL",
        (app_id,),
    )
    return cur.rowcount


# ───────────────────────── the sharer's view ────────────────────────────────

_NAMES_JOIN = """
    LEFT JOIN users g ON g.sub = s.grantee_sub
    LEFT JOIN users c ON c.sub = s.created_by
    LEFT JOIN users d ON d.sub = s.decided_by
    LEFT JOIN agents ta ON ta.slug = s.grantee_agent
    LEFT JOIN departments td ON td.id = s.grantee_department
"""
_NAMES_COLS = """
    g.name AS grantee_name, g.display_name AS grantee_display_name,
    g.username AS grantee_username, c.name AS creator_name,
    d.name AS decider_name, d.display_name AS decider_display_name,
    ta.display_name AS to_agent_name, td.name AS to_department_name
"""


def list_target_shares(target_kind: str, target_id: str) -> list[dict]:
    """Every unrevoked share of one target, oldest first, with the names of
    the recipient, the creator and whoever decided it."""
    col = _target_col(target_kind)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.*, {_NAMES_COLS}
                FROM shares s {_NAMES_JOIN}
                WHERE s.target_kind = %s AND s.{col} = %s AND s.revoked_at IS NULL
                ORDER BY s.created_at""",
            (target_kind, target_id),
        ).fetchall()
        return [dict(r) for r in rows]


# The admin list's one word for an unrevoked share, the first that holds:
# declined, expired, paused (suspended by a soft unpin), waiting (an app
# share a person has not decided), else live.
ADMIN_STANDINGS = ("live", "waiting", "paused", "expired", "declined")
_STANDING_CASE = (
    "CASE WHEN s.decision = 'declined' THEN 'declined' "
    "WHEN s.expires_at IS NOT NULL AND s.expires_at <= %s THEN 'expired' "
    "WHEN s.state = 'suspended' THEN 'paused' "
    "WHEN s.target_kind = 'app' AND s.decision = 'pending' THEN 'waiting' "
    "ELSE 'live' END"
)


def list_admin_shares(scope: str, *, agent: str = "", standing: str = "",
                      limit: int = 500) -> list[dict]:
    """The admin list: the unrevoked shares of one scope (``internal`` or
    ``external``), newest first, at most ``limit``, with the names, the
    target's title and own agent, and ``standing`` (``ADMIN_STANDINGS``,
    read with one clock so the filter and the column agree). ``agent``
    keeps a share of an app or chat of that agent, a share to that agent
    and a department share whose department holds it now (never a person's
    placed agent, which is theirs alone); ``standing`` keeps one word."""
    if scope not in SCOPES:
        raise ValueError(f"unknown share scope {scope!r}")
    now = _now()
    where = "s.scope = %s AND s.revoked_at IS NULL"
    params: list = [now, scope]
    if agent:
        where += (" AND (COALESCE(a.agent, ch.agent) = %s OR s.grantee_agent = %s"
                  " OR (s.grantee_kind = 'department' AND s.grantee_department <> ''"
                  " AND s.grantee_department = (SELECT department_id FROM agents"
                  " WHERE slug = %s)))")
        params += [agent, agent, agent]
    if standing:
        where += f" AND ({_STANDING_CASE}) = %s"
        params += [now, standing]
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.*, {_NAMES_COLS},
                       COALESCE(a.title, ch.title, '') AS target_title,
                       COALESCE(a.agent, ch.agent, '') AS agent,
                       {_STANDING_CASE} AS standing
                FROM shares s {_NAMES_JOIN}
                LEFT JOIN pinned_apps a ON a.id = s.app_id
                LEFT JOIN chats ch ON ch.id = s.chat_id
                WHERE {where}
                ORDER BY s.created_at DESC
                LIMIT %s""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def count_live_shares_by(user_sub: str, scope: str | None = None,
                         target_kind: str | None = None) -> int:
    with get_conn() as conn:
        extra = ""
        params: list = [user_sub, _now()]
        if scope:
            extra += " AND s.scope = %s"
            params.append(scope)
        if target_kind:
            extra += " AND s.target_kind = %s"
            params.append(target_kind)
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM shares s WHERE s.created_by = %s "
            f"AND {_live_clause()} {extra}",
            params,
        ).fetchone()["c"]


# ───────────────────────── a person's own shares ───────────────────────────


def internal_grant(target_kind: str, target_id: str, user_sub: str) -> dict | None:
    """The live person share giving ``user_sub`` access to the target (a
    pending app share included: it opens while it waits to be placed)."""
    if not user_sub:
        return None
    col = _target_col(target_kind)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM shares s WHERE s.target_kind = %s AND s.{col} = %s "
            f"AND s.scope = 'internal' AND s.grantee_kind = %s AND s.grantee_sub = %s "
            f"AND {_live_clause()}",
            (target_kind, target_id, PERSON, user_sub, _now()),
        ).fetchone()
        return dict(row) if row else None


def internal_grantees(app_id: str) -> list[str]:
    """People who hold a live share on the app and have not hidden it."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT s.grantee_sub FROM shares s WHERE s.target_kind = 'app' "
            f"AND s.app_id = %s AND s.scope = 'internal' AND s.grantee_kind = %s "
            f"AND NOT s.hidden_by_grantee AND {_live_clause()}",
            (app_id, PERSON, _now()),
        ).fetchall()
        return [r["grantee_sub"] for r in rows]


def grant_hidden(target_kind: str, target_id: str, user_sub: str) -> bool:
    col = _target_col(target_kind)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT 1 FROM shares WHERE target_kind = %s AND {col} = %s "
            "AND scope = 'internal' AND grantee_kind = %s AND grantee_sub = %s "
            "AND revoked_at IS NULL AND hidden_by_grantee",
            (target_kind, target_id, PERSON, user_sub),
        ).fetchone()
        return row is not None


def set_grant_hidden(target_kind: str, target_id: str, user_sub: str, hidden: bool) -> bool:
    col = _target_col(target_kind)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE shares SET hidden_by_grantee = %s WHERE target_kind = %s "
            f"AND {col} = %s AND scope = 'internal' AND grantee_kind = %s "
            "AND grantee_sub = %s AND revoked_at IS NULL",
            (hidden, target_kind, target_id, PERSON, user_sub),
        )
        conn.commit()
        return cur.rowcount > 0


def set_share_hidden(share_id: str, user_sub: str, hidden: bool) -> bool:
    """A person's own hide of one of their shares (an app or a chat)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE shares SET hidden_by_grantee = %s WHERE id = %s AND grantee_kind = %s "
            "AND grantee_sub = %s AND revoked_at IS NULL",
            (hidden, share_id, PERSON, user_sub),
        )
        conn.commit()
        return cur.rowcount > 0


def granted_app_rows(agent: str, viewer_sub: str) -> list[dict]:
    """The standing personal apps of OTHER users on ``agent`` that
    ``viewer_sub`` holds a live person share on, shaped like ``list_apps``
    rows (``hidden_for_me`` from the share, ``granted`` set), newest share
    first. Shared rows are never here: a member already sees them."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT a.*, s.hidden_by_grantee AS hidden_for_me, s.id AS share_id,
                       s.created_by AS shared_by, s.role_cap
                FROM shares s JOIN pinned_apps a ON a.id = s.app_id
                WHERE s.target_kind = 'app' AND s.scope = 'internal'
                  AND s.grantee_kind = %s AND s.grantee_sub = %s AND {_live_clause()}
                  AND a.agent = %s AND a.username <> '' AND NOT a.hidden
                  AND a.scope_chat_id IS NULL AND a.scope_project_id IS NULL
                ORDER BY s.created_at DESC""",
            (PERSON, viewer_sub, _now(), agent),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["granted"] = True
            out.append(d)
        return out


# ───────────────────────── placements (read-time) ───────────────────────────

# The agents an accepted agent or department share places its app in, read
# from the department rows as they stand now; ``''`` never names a
# department (an unassigned agent carries it).
_PLACED_AGENTS_JOIN = (
    "JOIN agents ag ON (s.grantee_kind = 'agent' AND ag.slug = s.grantee_agent) "
    "OR (s.grantee_kind = 'department' AND s.grantee_department <> '' "
    "AND ag.department_id = s.grantee_department)"
)
_PLACED_ROW = (
    "s.target_kind = 'app' AND s.scope = 'internal' AND s.decision = 'accepted' "
    "AND NOT a.hidden AND a.scope_chat_id IS NULL AND a.scope_project_id IS NULL"
)


def _holds_agent_clause(sub_expr: str, agent_expr: str) -> str:
    """SQL: the person named by ``sub_expr`` holds the agent named by
    ``agent_expr`` right now (a membership row, or the platform admin role;
    parameter: ``roles.ADMIN``)."""
    return (f"(EXISTS (SELECT 1 FROM user_agents ua WHERE ua.sub = {sub_expr} "
            f"AND ua.agent = {agent_expr}) "
            f"OR EXISTS (SELECT 1 FROM users pu WHERE pu.sub = {sub_expr} AND pu.role = %s))")


def _strongest(rows: list[dict], key: str) -> list[dict]:
    """One row per ``key``: the highest cap by rank, a person's own
    placement on a tie (it is theirs to hide)."""
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r[key])
        if cur is None:
            best[r[key]] = r
            continue
        rank, cur_rank = roles.rank(r["role_cap"]), roles.rank(cur["role_cap"])
        if rank > cur_rank or (rank == cur_rank and r["placement_kind"] == PERSON):
            best[r[key]] = r
    return list(best.values())


# A panel row's identity when several shares place one app in one agent:
# the agent share first (the receiving agent's editors remove it), then the
# department's, then the person's own.
_IDENTITY_ORDER = {AGENT: 0, DEPARTMENT: 1, PERSON: 2}


def _merged(rows: list[dict], key: str) -> list[dict]:
    """One panel row per ``key``: the identity share by ``_IDENTITY_ORDER``
    (its id, kind and hide), ``role_cap`` the highest cap among them and
    ``person_cap`` the person's own cap when one of them is theirs (never
    capped by their role on the agent)."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    out = []
    for group in groups.values():
        group.sort(key=lambda r: (_IDENTITY_ORDER.get(r["placement_kind"], 9),
                                  -roles.rank(r["role_cap"])))
        row = group[0]
        row["role_cap"] = max((r["role_cap"] for r in group), key=roles.rank)
        row["person_cap"] = next((r["role_cap"] for r in group
                                  if r["placement_kind"] == PERSON), None)
        out.append(row)
    return out


def placements_for_agent(agent: str, person_sub: str | None = None, *,
                         hide_sub: str | None = None) -> list[dict]:
    """The apps placed in ``agent`` by live accepted shares, as ``list_apps``
    rows with ``hidden_for_me`` (the viewer's hide here) and a ``placement``
    dict (``kind``, ``share_id``, ``from_agent``, ``from_agent_name``,
    ``agent``, ``role_cap``, ``person_cap``, ``shared_by``,
    ``shared_by_name``): the agent
    shares to it, the department shares to the department it is in now and,
    with ``person_sub``, that person's app shares placed here while they
    still hold the agent. ``hide_sub`` names whose hide ``hidden_for_me``
    reads when it is not ``person_sub``'s (a Shared-only chat carries a
    person but sees no person placement). The agent's own apps are never
    placements; an app placed twice is one row (``_merged``: the agent
    share's identity, the strongest cap, the person's own cap beside it).
    The Apps panel's list, the agent-facing list hook and the describe and
    open hooks read it."""
    now = _now()
    hide_sub = hide_sub if hide_sub is not None else (person_sub or "")
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT a.*, s.id AS share_id, s.grantee_kind AS placement_kind,
                       s.role_cap, s.created_by AS shared_by, s.created_at AS shared_at,
                       s.hidden_by_grantee,
                       COALESCE(NULLIF(cu.display_name, ''), cu.name, '') AS shared_by_name,
                       COALESCE(NULLIF(src.display_name, ''), '') AS from_agent_name,
                       EXISTS (SELECT 1 FROM share_placement_hides h WHERE h.app_id = a.id
                               AND h.agent = %s AND h.user_sub = %s) AS placement_hidden
                FROM shares s JOIN pinned_apps a ON a.id = s.app_id
                LEFT JOIN users cu ON cu.sub = s.created_by
                LEFT JOIN agents src ON src.slug = a.agent
                WHERE {_PLACED_ROW} AND {_live_clause()} AND a.agent <> %s
                  AND ((s.grantee_kind = 'agent' AND s.grantee_agent = %s)
                    OR (s.grantee_kind = 'department' AND s.grantee_department <> ''
                        AND s.grantee_department = (SELECT department_id FROM agents
                                                    WHERE slug = %s))
                    OR (s.grantee_kind = 'person' AND s.grantee_sub = %s
                        AND s.placed_agent = %s
                        AND {_holds_agent_clause('s.grantee_sub', 's.placed_agent')}))
                ORDER BY s.created_at DESC""",
            (agent, hide_sub, now, agent, agent, agent, person_sub or "", agent, roles.ADMIN),
        ).fetchall()
    from storage import db_apps
    out = []
    for r in _merged([dict(x) for x in rows], "id"):
        if db_apps.personal_row_dormant(r):
            continue
        kind = r.pop("placement_kind")
        hidden = r.pop("hidden_by_grantee") if kind == PERSON else r.pop("placement_hidden")
        r.pop("placement_hidden", None)
        r.pop("hidden_by_grantee", None)
        r["hidden_for_me"] = bool(hidden)
        r["placement"] = {
            "kind": kind, "share_id": r.pop("share_id"), "from_agent": r["agent"],
            "from_agent_name": r.pop("from_agent_name"), "agent": agent,
            "role_cap": r.pop("role_cap"), "person_cap": r.pop("person_cap"),
            "shared_by": r.pop("shared_by"), "shared_by_name": r.pop("shared_by_name"),
        }
        r.pop("shared_at", None)
        out.append(r)
    return out


def _placement_rows(app_id: str, agent: str, person_sub: str | None) -> list[dict]:
    """The live accepted shares placing ``app_id`` in ``agent``: the agent
    shares to it, the department shares to the department it is in now and,
    with ``person_sub``, that person's own placement while they hold the
    agent. Each ``{share_id, placement_kind, role_cap, id}``."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.id AS share_id, s.grantee_kind AS placement_kind, s.role_cap, a.id
                FROM shares s JOIN pinned_apps a ON a.id = s.app_id
                WHERE a.id = %s AND {_PLACED_ROW} AND {_live_clause()} AND a.agent <> %s
                  AND ((s.grantee_kind = 'agent' AND s.grantee_agent = %s)
                    OR (s.grantee_kind = 'department' AND s.grantee_department <> ''
                        AND s.grantee_department = (SELECT department_id FROM agents
                                                    WHERE slug = %s))
                    OR (s.grantee_kind = 'person' AND s.grantee_sub = %s
                        AND s.placed_agent = %s
                        AND {_holds_agent_clause('s.grantee_sub', 's.placed_agent')}))""",
            (app_id, _now(), agent, agent, agent, person_sub or "", agent, roles.ADMIN),
        ).fetchall()
    return [dict(r) for r in rows]


def placement_for(app_id: str, agent: str, person_sub: str | None = None) -> dict | None:
    """Whether a live accepted share places ``app_id`` in ``agent`` (for
    ``person_sub``, their own placement too, while they hold the agent):
    ``{share_id, source, role_cap}`` at the strongest cap, else None. The
    share routes' per-agent hide asks it; the app proxy's caller and the
    describe hook take ``placement_role``."""
    best = _strongest(_placement_rows(app_id, agent, person_sub), "id")
    if not best:
        return None
    r = best[0]
    return {"share_id": r["share_id"], "source": r["placement_kind"], "role_cap": r["role_cap"]}


def effective_placement_role(own: str, role_cap: str, person_cap: str | None) -> str:
    """The role a session acts at on a placed app: its person's row on the
    receiving agent capped by the team cap (``own`` is the no-user word for
    a session with no person, which ranks below every floor), or the
    person's own share's cap uncapped, whichever ranks higher."""
    role = roles.capped(own, role_cap)
    if person_cap and roles.rank(person_cap) > roles.rank(role):
        return person_cap
    return role


def placement_role(app_id: str, agent: str, person_sub: str | None, agent_role: str) -> dict | None:
    """The placement a session of ``agent`` acts through on ``app_id`` and
    the role it acts at (APPS.md "Agents call apps"): ``{share_id, source,
    role_cap, role}`` for the share giving the strongest role, the person's
    own on a tie; None when nothing places the app there. ``agent_role`` is
    the session's row on the receiving agent (``viewer`` without one; the
    no-user word for a session with no person)."""
    best: dict | None = None
    for r in _placement_rows(app_id, agent, person_sub):
        own_share = r["placement_kind"] == PERSON
        role = effective_placement_role(agent_role, r["role_cap"],
                                        r["role_cap"] if own_share else None)
        if best is None or roles.rank(role) > roles.rank(best["role"]) \
                or (roles.rank(role) == roles.rank(best["role"]) and own_share):
            best = {"share_id": r["share_id"], "source": r["placement_kind"],
                    "role_cap": r["role_cap"], "role": role}
    return best


def describe_placements(app_id: str) -> list[dict]:
    """Every agent a live accepted agent or department share places the app
    in, with its members (APPS.md "Platform catalog", the audience method):
    one entry per receiving agent, the identity share by ``_IDENTITY_ORDER``
    and the strongest cap when both kinds land on one agent, each
    ``{agent, agent_name, kind, share_id, role_cap, department, members}``
    with ``members`` at ``roles.capped(their row there, role_cap)``. Hides
    are not subtracted; emails are never read."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT ag.slug AS agent, COALESCE(NULLIF(ag.display_name, ''), ag.slug) AS agent_name,
                       s.id AS share_id, s.grantee_kind AS placement_kind, s.role_cap,
                       s.grantee_department AS department_id, td.name AS department_name,
                       ua.sub, COALESCE(ua.agent_role, %s) AS agent_role,
                       COALESCE(u.username, '') AS username,
                       COALESCE(u.display_name, '') AS display_name, COALESCE(u.name, '') AS name
                FROM shares s JOIN pinned_apps a ON a.id = s.app_id {_PLACED_AGENTS_JOIN}
                LEFT JOIN departments td ON td.id = s.grantee_department
                LEFT JOIN user_agents ua ON ua.agent = ag.slug
                LEFT JOIN users u ON u.sub = ua.sub
                WHERE a.id = %s AND {_PLACED_ROW} AND {_live_clause()} AND ag.slug <> a.agent
                ORDER BY ag.slug, ua.sub""",
            (roles.VIEWER, app_id, _now()),
        ).fetchall()
    by_agent: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        entry = by_agent.get(r["agent"])
        share = {"kind": r["placement_kind"], "share_id": r["share_id"], "role_cap": r["role_cap"],
                 "department": ({"id": r["department_id"], "name": r["department_name"] or ""}
                                if r["placement_kind"] == DEPARTMENT else None)}
        if entry is None:
            entry = by_agent[r["agent"]] = {"agent": r["agent"], "agent_name": r["agent_name"],
                                            "shares": {}, "members": {}}
        entry["shares"][r["share_id"]] = share
        if r["sub"]:
            entry["members"][r["sub"]] = {"sub": r["sub"], "username": r["username"],
                                          "display_name": r["display_name"], "name": r["name"],
                                          "row": r["agent_role"]}
    out = []
    for entry in by_agent.values():
        shares = sorted(entry["shares"].values(),
                        key=lambda s: (_IDENTITY_ORDER.get(s["kind"], 9), -roles.rank(s["role_cap"])))
        identity = shares[0]
        cap = max((s["role_cap"] for s in shares), key=roles.rank)
        members = [{"sub": m["sub"], "username": m["username"], "display_name": m["display_name"],
                    "name": m["name"], "role": roles.capped(m.pop("row"), cap)}
                   for m in entry["members"].values()]
        out.append({"agent": entry["agent"], "agent_name": entry["agent_name"],
                    "kind": identity["kind"], "share_id": identity["share_id"], "role_cap": cap,
                    "department": identity["department"], "members": members})
    return out


def placements_for_user(app_id: str, user_sub: str, agents: list[str],
                        is_admin: bool = False) -> list[dict]:
    """The agent and department placements that admit ``user_sub`` to one
    app through an agent they hold (``agents``; a platform admin holds every
    agent), strongest first, each ``{share_id, kind, agent, role_cap,
    shared_by}`` with the receiving agent and the sharer. A hide changes nothing here: a hidden placement
    still admits, as a hidden shared app does."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.id AS share_id, s.grantee_kind AS kind, ag.slug AS agent, s.role_cap,
                       s.created_by AS shared_by
                FROM shares s JOIN pinned_apps a ON a.id = s.app_id {_PLACED_AGENTS_JOIN}
                WHERE a.id = %s AND {_PLACED_ROW} AND {_live_clause()} AND ag.slug <> a.agent
                  AND (%s OR ag.slug = ANY(%s))""",
            (app_id, _now(), bool(is_admin), list(agents)),
        ).fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda r: -roles.rank(r["role_cap"]))
    return out


def effective_share_role(app_id: str, user_sub: str, agent_roles: dict[str, str],
                         is_admin: bool = False) -> str | None:
    """The role a share gives ``user_sub`` on the app, when one admits them:
    a person share's cap; an agent or department placement's cap, capped by
    their role on the receiving agent (``roles.capped``); the strongest of
    them. None when no share admits them."""
    best: str | None = None
    grant = internal_grant("app", app_id, user_sub)
    if grant:
        best = grant["role_cap"]
    for p in placements_for_user(app_id, user_sub, list(agent_roles), is_admin):
        own = roles.ADMIN if is_admin else (agent_roles.get(p["agent"]) or roles.VIEWER)
        role = roles.capped(own, p["role_cap"])
        if best is None or roles.rank(role) > roles.rank(best):
            best = role
    return best


def placement_audience(app_id: str) -> list[tuple[str, str]]:
    """``(sub, role)`` for every member of every agent a live accepted agent
    or department share places the app in, minus their own hide in that
    agent. One join; a person in two such agents appears twice."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT ua.sub, COALESCE(ua.agent_role, %s) AS agent_role
                FROM shares s JOIN pinned_apps a ON a.id = s.app_id {_PLACED_AGENTS_JOIN}
                JOIN user_agents ua ON ua.agent = ag.slug
                WHERE a.id = %s AND {_PLACED_ROW} AND {_live_clause()} AND ag.slug <> a.agent
                  AND NOT EXISTS (SELECT 1 FROM share_placement_hides h
                                  WHERE h.app_id = a.id AND h.agent = ag.slug
                                  AND h.user_sub = ua.sub)""",
            (roles.VIEWER, app_id, _now()),
        ).fetchall()
        return [(r["sub"], r["agent_role"]) for r in rows]


def placed_agents_of(share: dict) -> list[str]:
    """The agents a share places its app in right now: the receiving
    agent, the department's agents, or a person's ``placed_agent``."""
    if share.get("grantee_kind") == AGENT:
        return [share["grantee_agent"]] if share.get("grantee_agent") else []
    if share.get("grantee_kind") == DEPARTMENT and share.get("grantee_department"):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT slug FROM agents WHERE department_id = %s ORDER BY slug",
                (share["grantee_department"],),
            ).fetchall()
            return [r["slug"] for r in rows]
    if share.get("grantee_kind") == PERSON and share.get("placed_agent"):
        return [share["placed_agent"]]
    return []


# ───────────────────────── the "Shared with you" section ────────────────────

_INBOX_COLS = """
    s.*, c.name AS creator_name, c.display_name AS creator_display_name,
    COALESCE(a.title, ch.title, '') AS target_title,
    COALESCE(a.agent, ch.agent, '') AS agent,
    a.slug AS app_slug, a.hidden AS app_hidden, a.username AS app_username,
    a.owner_sub AS app_owner_sub,
    sa.display_name AS agent_display_name, sa.color AS agent_color
"""
_INBOX_JOIN = """
    FROM shares s
    LEFT JOIN users c ON c.sub = s.created_by
    LEFT JOIN pinned_apps a ON a.id = s.app_id
    LEFT JOIN chats ch ON ch.id = s.chat_id
    LEFT JOIN agents sa ON sa.slug = COALESCE(a.agent, ch.agent)
"""
INBOX_RECEIVED_MAX = 200


def inbox_for(user_sub: str) -> list[dict]:
    """The rows of a person's "Shared with you" section: their own live
    person shares (hidden ones left out), with ``placed_now`` naming the
    agent an accepted app sits in while they still hold it. Pending first,
    newest first; the received rows capped. Soft-unpinned and dormant
    targets are left out."""
    with get_conn() as conn:
        mine = conn.execute(
            f"""SELECT {_INBOX_COLS},
                       CASE WHEN s.placed_agent IS NOT NULL
                             AND {_holds_agent_clause('s.grantee_sub', 's.placed_agent')}
                            THEN s.placed_agent END AS placed_now
                {_INBOX_JOIN}
                WHERE s.scope = 'internal' AND s.grantee_kind = %s AND s.grantee_sub = %s
                  AND NOT s.hidden_by_grantee AND {_live_clause()}
                ORDER BY s.created_at DESC""",
            (roles.ADMIN, PERSON, user_sub, _now()),
        ).fetchall()
    from storage import db_apps
    pending: list[dict] = []
    received: list[dict] = []
    for r in [dict(x) for x in mine]:
        if r.get("app_hidden"):
            continue
        if r.get("app_id") and db_apps.personal_row_dormant(
                {"username": r.get("app_username"), "agent": r.get("agent"),
                 "owner_sub": r.get("app_owner_sub")}):
            continue
        waiting = r["target_kind"] == "app" and r["decision"] == PENDING
        (pending if waiting else received).append(r)
    pending.sort(key=lambda r: r["created_at"], reverse=True)
    received.sort(key=lambda r: r["created_at"], reverse=True)
    return pending + received[:INBOX_RECEIVED_MAX]


def pending_count_for(subs: list[str]) -> dict[str, int]:
    """Each person's count of decisions waiting on them: the pending app
    rows of their own section, by the section's one rule (a hidden share
    and a soft-unpinned or dormant target count for nothing)."""
    return {sub: sum(1 for r in inbox_for(sub)
                     if r["target_kind"] == "app" and r["decision"] == PENDING)
            for sub in subs}


def members_of_agents(agents: list[str]) -> set[str]:
    """Every person with a membership row on any of ``agents``."""
    if not agents:
        return set()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT sub FROM user_agents WHERE agent = ANY(%s)", (list(agents),),
        ).fetchall()
        return {r["sub"] for r in rows}

