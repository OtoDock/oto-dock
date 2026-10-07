"""The revoked sign-in ids (``auth/session_revocation.py``): one row per
logout, kept until the cookie it revoked could no longer be presented."""

from __future__ import annotations

from datetime import datetime, timezone

from storage.pg import get_conn


def add(cookie_id: str, user_sub: str, expires_at: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO revoked_session_ids (id, user_sub, expires_at, revoked_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET expires_at = GREATEST("
            "revoked_session_ids.expires_at, EXCLUDED.expires_at)",
            (cookie_id, user_sub, expires_at, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def load_live(now: float) -> list[tuple[str, float]]:
    """The ids not yet expired at ``now``; the expired rows are deleted."""
    with get_conn() as conn:
        conn.execute("DELETE FROM revoked_session_ids WHERE expires_at < %s", (now,))
        rows = conn.execute(
            "SELECT id, expires_at FROM revoked_session_ids"
        ).fetchall()
        conn.commit()
    return [(r["id"], float(r["expires_at"])) for r in rows]
