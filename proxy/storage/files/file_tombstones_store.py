"""Delete tombstones — the EXPLICIT, timestamped record that the platform
deleted a file.

Deletes are **never inferred from absence**. A file the platform
lacks but a satellite still has is PULLED (healed), *unless* a tombstone
authorizes deleting the satellite's copy. This is what stops a wiped/re-imaged/
divergent satellite from mass-deleting platform data, and what lets an offline
satellite apply a missed delete when it reconnects.

A tombstone is written whenever the platform deletes a file — a dashboard delete
(including each file under a deleted directory), or an applied live satellite
delete. ``deleted_at_mtime`` (epoch seconds) orders the tombstone against a
satellite re-create of the same path (clock-offset-adjusted at merge time).

One row per ``(agent_slug, rel_path)`` — re-deleting refreshes it. Reaped after
``FILE_TOMBSTONE_TTL_DAYS`` (30d): past that, a re-created file at the path is
simply re-adopted (the delete is assumed long-since propagated to every machine).

Synchronous (called via ``asyncio.to_thread``); commits on clean context exit.
"""

import logging
from datetime import datetime, timedelta, timezone

import config
from storage.pg import get_conn

logger = logging.getLogger("claude-proxy.tombstones")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(
    agent_slug: str, rel_path: str, deleted_at_mtime: float,
    *, origin: str = "", ttl_days: int = config.FILE_TOMBSTONE_TTL_DAYS,
) -> None:
    """Write (or refresh) a tombstone for a platform-side delete.

    ``deleted_at_mtime`` should be the wall-clock epoch seconds of the delete
    (``time.time()``) so it can be ordered against a satellite re-create.
    ``origin`` is a free-text breadcrumb (``"dashboard"`` / ``"live-delete"``).
    """
    now = _now()
    expires_at = (now + timedelta(days=ttl_days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO file_tombstones
                   (agent_slug, rel_path, deleted_at_mtime, deleted_at, origin, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (agent_slug, rel_path)
               DO UPDATE SET deleted_at_mtime=EXCLUDED.deleted_at_mtime,
                             deleted_at=EXCLUDED.deleted_at,
                             origin=EXCLUDED.origin,
                             expires_at=EXCLUDED.expires_at""",
            (agent_slug, rel_path, deleted_at_mtime, now.isoformat(), origin, expires_at),
        )


def get(agent_slug: str, rel_path: str) -> dict | None:
    """Return a single live (unexpired) tombstone row, or ``None``."""
    now_iso = _now().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM file_tombstones "
            "WHERE agent_slug=%s AND rel_path=%s AND expires_at > %s",
            (agent_slug, rel_path, now_iso),
        ).fetchone()
    return dict(row) if row is not None else None


def load_for_agent(agent_slug: str) -> dict[str, float]:
    """Return ``{rel_path: deleted_at_mtime}`` for all LIVE tombstones of an agent.

    Expired tombstones are excluded — they no longer authorize a delete, so a
    satellite file at that path is re-adopted (pulled) instead.
    """
    now_iso = _now().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rel_path, deleted_at_mtime FROM file_tombstones "
            "WHERE agent_slug=%s AND expires_at > %s",
            (agent_slug, now_iso),
        ).fetchall()
    return {r["rel_path"]: r["deleted_at_mtime"] for r in rows}


def drop(agent_slug: str, rel_path: str) -> None:
    """Remove a tombstone — the path was re-created/re-adopted and is live again."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM file_tombstones WHERE agent_slug=%s AND rel_path=%s",
            (agent_slug, rel_path),
        )


def purge_prefix(agent_slug: str, rel_prefix: str) -> int:
    """Drop every tombstone under a directory prefix (``"externals/phone/x/"``)
    — the caller-data retention removes whole trees that never sync, so
    their bookkeeping goes with them. Returns rows removed."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM file_tombstones WHERE agent_slug=%s "
            "AND rel_path LIKE %s ESCAPE '\\'",
            (agent_slug, _like_prefix(rel_prefix)),
        )
        conn.commit()
        return cur.rowcount


def _like_prefix(prefix: str) -> str:
    """A LIKE pattern matching ``prefix*`` literally (``_`` / ``%`` escaped)."""
    return (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")) + "%"


def delete_expired() -> int:
    """Drop tombstones past their TTL. Returns rows removed (for the reaper log)."""
    now_iso = _now().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "DELETE FROM file_tombstones WHERE expires_at <= %s RETURNING rel_path",
            (now_iso,),
        ).fetchall()
    return len(rows)
