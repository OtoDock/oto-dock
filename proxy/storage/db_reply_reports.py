"""A person's reports of agents' replies, for the install's admins: one row
per (reporter, reply) in ``reply_reports``, the reply named by its first row,
with its text as the server stored it. The route finds the reply's rows and
composes the text; this module only reads and writes.

Synchronous psycopg calls, run through ``run_db`` like the chat stores.
"""

from __future__ import annotations

from storage.pg import get_conn

_ROW_COLUMNS = "id, role, content, event_type, event_data, created_at"


def message_in_chat(chat_id: str, message_id: int) -> dict | None:
    """The message row when it belongs to the chat, else None."""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM chat_messages WHERE id = %s AND chat_id = %s",
            (message_id, chat_id),
        ).fetchone()


def rows_before(chat_id: str, message_id: int, limit: int) -> list[dict]:
    """The chat's rows before the message, newest first, at most ``limit``."""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM chat_messages "
            "WHERE chat_id = %s AND id < %s ORDER BY id DESC LIMIT %s",
            (chat_id, message_id, limit),
        ).fetchall()


def rows_from(chat_id: str, message_id: int, limit: int) -> list[dict]:
    """The chat's rows from the message on, in order, at most ``limit``."""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM chat_messages "
            "WHERE chat_id = %s AND id >= %s ORDER BY id LIMIT %s",
            (chat_id, message_id, limit),
        ).fetchall()


def has_report(reporter_sub: str, message_id: int) -> bool:
    """Whether the person already reported the reply this row starts."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM reply_reports WHERE reporter_sub = %s AND message_id = %s",
            (reporter_sub, message_id),
        ).fetchone() is not None


def count_since(reporter_sub: str, seconds: int) -> int:
    """How many reports the person filed in the last ``seconds``."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM reply_reports WHERE reporter_sub = %s "
            "AND created_at > now() - make_interval(secs => %s)",
            (reporter_sub, seconds),
        ).fetchone()
    return int(row["n"]) if row else 0


def notice_due(reporter_sub: str, gap_seconds: int) -> bool:
    """Whether the admins may be told of this person's next report: none of
    their reports was notified in the last ``gap_seconds``."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM reply_reports WHERE reporter_sub = %s "
            "AND notified_at > now() - make_interval(secs => %s) LIMIT 1",
            (reporter_sub, gap_seconds),
        ).fetchone()
    return row is None


def mark_notified(report_id: int) -> int:
    """Mark the report as told to the admins; how many of the person's
    reports since their previous notice were never told (the "N more")."""
    with get_conn() as conn:
        row = conn.execute(
            """WITH me AS (
                   UPDATE reply_reports SET notified_at = now() WHERE id = %s
                   RETURNING reporter_sub
               ), last AS (
                   SELECT max(r.notified_at) AS at FROM reply_reports r, me
                   WHERE r.reporter_sub = me.reporter_sub AND r.id <> %s
               )
               SELECT count(*) AS n FROM reply_reports r, me, last
               WHERE r.reporter_sub = me.reporter_sub AND r.id <> %s
                 AND r.notified_at IS NULL
                 AND (last.at IS NULL OR r.created_at > last.at)""",
            (report_id, report_id, report_id),
        ).fetchone()
        conn.commit()
    return int(row["n"]) if row else 0


def add(reporter_sub: str, chat_id: str, message_id: int, *, agent: str,
        reason: str, note: str, reply_text: str) -> int | None:
    """Store a report; the new row's id, or None when the person already
    reported this reply (the first report stands)."""
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO reply_reports
                   (reporter_sub, chat_id, message_id, agent, reason, note, reply_text)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (reporter_sub, message_id) DO NOTHING
               RETURNING id""",
            (reporter_sub, chat_id, message_id, agent, reason, note, reply_text),
        ).fetchone()
        conn.commit()
    return int(row["id"]) if row else None


def list_page(limit: int, before_id: int | None = None, *,
              dismissed: bool = False) -> tuple[list[dict], bool]:
    """The newest ``limit`` open reports, or dismissed ones (older than
    ``before_id`` when paging), each with its chat's title and agent and the
    reply's time (empty when its first row is gone), and whether older ones
    remain."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, r.created_at, r.reporter_sub, r.chat_id, c.title AS chat_title,
                      c.agent AS chat_agent, r.message_id, m.created_at AS reply_at,
                      r.agent, r.reason, r.note, r.reply_text, r.dismissed_at, r.dismissed_by
               FROM reply_reports r JOIN chats c ON c.id = r.chat_id
               LEFT JOIN chat_messages m ON m.id = r.message_id
               WHERE (%s::int IS NULL OR r.id < %s)
                 AND (r.dismissed_at IS NOT NULL) = %s
               ORDER BY r.id DESC LIMIT %s""",
            (before_id, before_id, dismissed, limit + 1),
        ).fetchall()
    return rows[:limit], len(rows) > limit


def count_dismissed() -> int:
    """How many reports sit in the page's dismissed history."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM reply_reports WHERE dismissed_at IS NOT NULL"
        ).fetchone()
    return int(row["n"]) if row else 0


def set_dismissed(report_id: int, by_sub: str | None) -> bool:
    """Dismiss a report (``by_sub``) or restore it (None); False for an
    unknown id. Dismissing a dismissed report keeps who did it first."""
    with get_conn() as conn:
        if by_sub is None:
            row = conn.execute(
                "UPDATE reply_reports SET dismissed_at = NULL, dismissed_by = '' "
                "WHERE id = %s RETURNING id",
                (report_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "UPDATE reply_reports SET "
                "dismissed_by = CASE WHEN dismissed_at IS NULL THEN %s ELSE dismissed_by END, "
                "dismissed_at = COALESCE(dismissed_at, now()) "
                "WHERE id = %s RETURNING id",
                (by_sub, report_id),
            ).fetchone()
        conn.commit()
    return row is not None
