"""Shares of apps and chats (SHARING.md).

All functions are synchronous (call them on the DB executor). A share is
LIVE when it is unrevoked, unexpired and ``active``; a grant is what a
live internal share gives its grantee. Access checks read ``internal_grant``;
the fan-outs read ``internal_grantees``; nothing here decides WHO may
share — that stays with the routes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from storage.pg import get_conn

TARGET_KINDS = ("app", "chat")


class ShareExists(Exception):
    """A live internal share for that (target, user) already exists."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _live_clause(alias: str = "s") -> str:
    """The SQL predicate for a live share (parameter: the current time)."""
    return (f"{alias}.revoked_at IS NULL AND {alias}.state = 'active' "
            f"AND ({alias}.expires_at IS NULL OR {alias}.expires_at > %s)")


def _target_col(target_kind: str) -> str:
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"unknown share target kind {target_kind!r}")
    return "app_id" if target_kind == "app" else "chat_id"


def is_live(row: dict, now: str | None = None) -> bool:
    now = now or _now()
    return (row.get("revoked_at") is None and row.get("state") == "active"
            and (not row.get("expires_at") or row["expires_at"] > now))


def create_internal_share(*, target_kind: str, target_id: str, grantee_sub: str,
                          created_by: str, expires_at: str | None = None) -> dict:
    """A grant to one platform user. Raises ShareExists on a live duplicate."""
    from psycopg import errors as pg_errors
    col = _target_col(target_kind)
    share_id = str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        try:
            conn.execute(
                f"""INSERT INTO shares
                    (id, target_kind, {col}, scope, grantee_sub, created_by,
                     created_at, expires_at)
                    VALUES (%s, %s, %s, 'internal', %s, %s, %s, %s)""",
                (share_id, target_kind, target_id, grantee_sub, created_by, now,
                 expires_at),
            )
        except pg_errors.UniqueViolation:
            raise ShareExists() from None
        row = dict(conn.execute("SELECT * FROM shares WHERE id=%s", (share_id,)).fetchone())
        conn.commit()
        return row


def create_external_share(*, target_kind: str, target_id: str, created_by: str,
                          token_hash: str, password_hash: str = "", public: bool = False,
                          allow_actions: bool = False, expires_at: str | None = None,
                          include_tools: bool = False) -> dict:
    """A link. The token itself is never stored, only its sha256."""
    col = _target_col(target_kind)
    share_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO shares
                (id, target_kind, {col}, scope, created_by, created_at, expires_at,
                 token_hash, password_hash, public, allow_actions, include_tools)
                VALUES (%s, %s, %s, 'external', %s, %s, %s, %s, %s, %s, %s, %s)""",
            (share_id, target_kind, target_id, created_by, _now(), expires_at,
             token_hash, password_hash or "", bool(public), bool(allow_actions),
             bool(include_tools)),
        )
        row = dict(conn.execute("SELECT * FROM shares WHERE id=%s", (share_id,)).fetchone())
        conn.commit()
        return row


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


def list_target_shares(target_kind: str, target_id: str) -> list[dict]:
    """Every unrevoked share of one target, oldest first, with the grantee's
    and the creator's names."""
    col = _target_col(target_kind)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.*, g.name AS grantee_name, g.display_name AS grantee_display_name,
                       g.username AS grantee_username, c.name AS creator_name
                FROM shares s
                LEFT JOIN users g ON g.sub = s.grantee_sub
                LEFT JOIN users c ON c.sub = s.created_by
                WHERE s.target_kind = %s AND s.{col} = %s AND s.revoked_at IS NULL
                ORDER BY s.created_at""",
            (target_kind, target_id),
        ).fetchall()
        return [dict(r) for r in rows]


def internal_grant(target_kind: str, target_id: str, user_sub: str) -> dict | None:
    """The live internal share giving ``user_sub`` access to the target."""
    if not user_sub:
        return None
    col = _target_col(target_kind)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM shares s WHERE s.target_kind = %s AND s.{col} = %s "
            f"AND s.scope = 'internal' AND s.grantee_sub = %s AND {_live_clause()}",
            (target_kind, target_id, user_sub, _now()),
        ).fetchone()
        return dict(row) if row else None


def internal_grantees(app_id: str) -> list[str]:
    """Users who hold a live grant on the app and have not hidden it."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT s.grantee_sub FROM shares s WHERE s.target_kind = 'app' "
            f"AND s.app_id = %s AND s.scope = 'internal' AND NOT s.hidden_by_grantee "
            f"AND {_live_clause()}",
            (app_id, _now()),
        ).fetchall()
        return [r["grantee_sub"] for r in rows]


def grant_hidden(target_kind: str, target_id: str, user_sub: str) -> bool:
    col = _target_col(target_kind)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT 1 FROM shares WHERE target_kind = %s AND {col} = %s "
            "AND scope = 'internal' AND grantee_sub = %s AND revoked_at IS NULL "
            "AND hidden_by_grantee",
            (target_kind, target_id, user_sub),
        ).fetchone()
        return row is not None


def set_grant_hidden(target_kind: str, target_id: str, user_sub: str, hidden: bool) -> bool:
    col = _target_col(target_kind)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE shares SET hidden_by_grantee = %s WHERE target_kind = %s "
            f"AND {col} = %s AND scope = 'internal' AND grantee_sub = %s "
            "AND revoked_at IS NULL",
            (hidden, target_kind, target_id, user_sub),
        )
        conn.commit()
        return cur.rowcount > 0


def revoke_share(share_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE shares SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
            (_now(), share_id),
        )
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
    """Mark the app's live shares suspended (a soft unpin). Runs inside the
    caller's transaction so it lands with the row's hide."""
    cur = conn.execute(
        "UPDATE shares SET state = 'suspended' WHERE target_kind = 'app' "
        "AND app_id = %s AND state = 'active' AND revoked_at IS NULL",
        (app_id,),
    )
    return cur.rowcount


def list_grants_for(user_sub: str) -> list[dict]:
    """The live internal shares granted TO ``user_sub`` ("Shared with me"),
    newest first, with the target's title and agent and the sharer's name.
    A suspended share (the app is soft-unpinned) is left out; a hidden one
    is returned flagged so the viewer can restore it."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.*, c.name AS creator_name,
                       COALESCE(a.title, ch.title, '') AS target_title,
                       COALESCE(a.agent, ch.agent, '') AS agent,
                       a.slug AS app_slug, a.hidden AS app_hidden
                FROM shares s
                LEFT JOIN users c ON c.sub = s.created_by
                LEFT JOIN pinned_apps a ON a.id = s.app_id
                LEFT JOIN chats ch ON ch.id = s.chat_id
                WHERE s.scope = 'internal' AND s.grantee_sub = %s AND {_live_clause()}
                ORDER BY s.created_at DESC""",
            (user_sub, _now()),
        ).fetchall()
        return [dict(r) for r in rows if not r["app_hidden"]]


def granted_app_rows(agent: str, viewer_sub: str) -> list[dict]:
    """The standing personal apps of OTHER users on ``agent`` that
    ``viewer_sub`` holds a live grant on, shaped like ``list_apps`` rows
    (``hidden_for_me`` from the grant, ``granted`` set), newest grant first.
    Shared rows are never here: a member already sees them."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT a.*, s.hidden_by_grantee AS hidden_for_me, s.id AS share_id,
                       s.created_by AS shared_by
                FROM shares s JOIN pinned_apps a ON a.id = s.app_id
                WHERE s.target_kind = 'app' AND s.scope = 'internal'
                  AND s.grantee_sub = %s AND {_live_clause()}
                  AND a.agent = %s AND a.username <> '' AND NOT a.hidden
                  AND a.scope_chat_id IS NULL AND a.scope_project_id IS NULL
                ORDER BY s.created_at DESC""",
            (viewer_sub, _now(), agent),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["granted"] = True
            out.append(d)
        return out


def list_external_shares() -> list[dict]:
    """Every unrevoked external link (the admin list), newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.*, c.name AS creator_name,
                      COALESCE(a.title, ch.title, '') AS target_title,
                      COALESCE(a.agent, ch.agent, '') AS agent
               FROM shares s
               LEFT JOIN users c ON c.sub = s.created_by
               LEFT JOIN pinned_apps a ON a.id = s.app_id
               LEFT JOIN chats ch ON ch.id = s.chat_id
               WHERE s.scope = 'external' AND s.revoked_at IS NULL
               ORDER BY s.created_at DESC""",
        ).fetchall()
        return [dict(r) for r in rows]


def count_live_shares_by(user_sub: str, scope: str | None = None) -> int:
    with get_conn() as conn:
        extra = "AND s.scope = %s" if scope else ""
        params: list = [user_sub, _now()] + ([scope] if scope else [])
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM shares s WHERE s.created_by = %s "
            f"AND {_live_clause()} {extra}",
            params,
        ).fetchone()["c"]
