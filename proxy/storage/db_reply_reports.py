"""A person's reports of agents' replies, for the install's admins: one row
per (reporter, reply) in ``reply_reports``, the reply named by its first row,
with its text as the server stored it. The route finds the reply's rows and
composes the text; this module only reads and writes.

A report is open, handled or dismissed (at most one of the two stamps),
outlives its chat (``chat_id`` NULL) and goes ``KEEP_DAYS`` after it was
filed (the retention sweep's ``reply-reports`` pass).

Synchronous psycopg calls, run through ``run_db`` like the chat stores.
"""

from __future__ import annotations

from storage.pg import get_conn

#: How long a report is kept after it was filed, open or closed, its chat
#: there or not (the retention sweep; the privacy texts name it).
KEEP_DAYS = 365
#: A report's states, as the admins' page and its routes name them.
OPEN, HANDLED, DISMISSED = "open", "handled", "dismissed"
#: The most report ids one chat's "Reported" read returns.
MINE_MAX = 500

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
        reason: str, note: str, reply_text: str, chat_title: str = "") -> int | None:
    """Store a report; the new row's id, or None when the person already
    reported this reply (the first report stands). A chat deleted since the
    caller read it fails the foreign key (``psycopg.errors.ForeignKeyViolation``)."""
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO reply_reports
                   (reporter_sub, chat_id, message_id, agent, reason, note, reply_text, chat_title)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (reporter_sub, message_id) DO NOTHING
               RETURNING id""",
            (reporter_sub, chat_id, message_id, agent, reason, note, reply_text, chat_title),
        ).fetchone()
        conn.commit()
    return int(row["id"]) if row else None


def list_page(limit: int, before_id: int | None = None, *,
              closed: bool = False) -> tuple[list[dict], bool]:
    """The newest ``limit`` open reports, or closed ones (handled and
    dismissed together), newest filed first (older than ``before_id`` when
    paging), each with its chat's title and agent while the chat exists
    (else the title stored with the report and no agent), the reply's time
    (empty when its first row is gone), its state, who closed it and when;
    and whether older ones remain."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, r.created_at, r.reporter_sub, r.chat_id,
                      CASE WHEN c.id IS NOT NULL THEN COALESCE(c.title, '')
                           ELSE r.chat_title END AS chat_title,
                      c.agent AS chat_agent, r.message_id, m.created_at AS reply_at,
                      r.agent, r.reason, r.note, r.reply_text,
                      CASE WHEN r.handled_at IS NOT NULL THEN 'handled'
                           WHEN r.dismissed_at IS NOT NULL THEN 'dismissed'
                           ELSE 'open' END AS state,
                      COALESCE(r.handled_at, r.dismissed_at) AS closed_at,
                      CASE WHEN r.handled_at IS NOT NULL THEN r.handled_by
                           ELSE r.dismissed_by END AS closed_by
               FROM reply_reports r LEFT JOIN chats c ON c.id = r.chat_id
               LEFT JOIN chat_messages m ON m.id = r.message_id AND m.chat_id = r.chat_id
               WHERE (%s::int IS NULL OR r.id < %s)
                 AND (r.handled_at IS NOT NULL OR r.dismissed_at IS NOT NULL) = %s
               ORDER BY r.id DESC LIMIT %s""",
            (before_id, before_id, closed, limit + 1),
        ).fetchall()
    return rows[:limit], len(rows) > limit


def count_closed() -> int:
    """How many reports sit in the page's closed history."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM reply_reports "
            "WHERE handled_at IS NOT NULL OR dismissed_at IS NOT NULL"
        ).fetchone()
    return int(row["n"]) if row else 0


def set_state(report_id: int, state: str, by_sub: str) -> str | None:
    """Mark a report handled or dismissed (``by_sub`` is the admin), or open
    it again; the state the report has after, None for an unknown id.
    Closing applies to an open report only: a closed one keeps its state,
    its admin and its time."""
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE reply_reports SET
                   handled_at = CASE WHEN %(state)s = 'open' THEN NULL
                       WHEN %(state)s = 'handled' AND handled_at IS NULL AND dismissed_at IS NULL
                           THEN now() ELSE handled_at END,
                   handled_by = CASE WHEN %(state)s = 'open' THEN ''
                       WHEN %(state)s = 'handled' AND handled_at IS NULL AND dismissed_at IS NULL
                           THEN %(by)s ELSE handled_by END,
                   dismissed_at = CASE WHEN %(state)s = 'open' THEN NULL
                       WHEN %(state)s = 'dismissed' AND handled_at IS NULL AND dismissed_at IS NULL
                           THEN now() ELSE dismissed_at END,
                   dismissed_by = CASE WHEN %(state)s = 'open' THEN ''
                       WHEN %(state)s = 'dismissed' AND handled_at IS NULL AND dismissed_at IS NULL
                           THEN %(by)s ELSE dismissed_by END
               WHERE id = %(id)s
               RETURNING handled_at, dismissed_at""",
            {"state": state, "by": by_sub, "id": report_id},
        ).fetchone()
        conn.commit()
    if row is None:
        return None
    if row["handled_at"] is not None:
        return HANDLED
    return DISMISSED if row["dismissed_at"] is not None else OPEN


def delete(report_id: int) -> bool:
    """Delete a report and its copy of the reply; False for an unknown id."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM reply_reports WHERE id = %s RETURNING id", (report_id,),
        ).fetchone()
        conn.commit()
    return row is not None


def reported_in(reporter_sub: str, chat_id: str) -> list[int]:
    """The first rows of the replies the person reported in a chat, newest
    first, at most ``MINE_MAX``."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT message_id FROM reply_reports WHERE reporter_sub = %s AND chat_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (reporter_sub, chat_id, MINE_MAX),
        ).fetchall()
    return [int(r["message_id"]) for r in rows]


def delete_older_than(days: int, *, dry_run: bool = False) -> int:
    """The retention pass: every report filed more than ``days`` days ago,
    whatever its state; how many (counted only on a dry run)."""
    with get_conn() as conn:
        if dry_run:
            row = conn.execute(
                "SELECT count(*) AS n FROM reply_reports "
                "WHERE created_at < now() - make_interval(days => %s)",
                (days,),
            ).fetchone()
            return int(row["n"]) if row else 0
        cur = conn.execute(
            "DELETE FROM reply_reports WHERE created_at < now() - make_interval(days => %s)",
            (days,),
        )
        conn.commit()
        return cur.rowcount


def delete_for_chat(chat_id: str) -> int:
    """A phone conversation's reports, before the caller-data pass deletes
    the conversation: what its caller was promised to have forgotten."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM reply_reports WHERE chat_id = %s", (chat_id,))
        conn.commit()
        return cur.rowcount
