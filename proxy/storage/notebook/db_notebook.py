"""Notebook entries and the delegation edges they follow.

Synchronous psycopg calls, run off the loop by the routes and called from
the prompt build's worker thread. The read set's rule lives in
``services/notebook/read_set.py``; this module only reads and writes.
"""

from __future__ import annotations

from datetime import datetime

from storage.pg import get_conn

_COLUMNS = "id, agent, author_sub, surface, text, created_at"

# Rows the retention pass deletes per statement.
_PRUNE_BATCH = 5000


def neighbours(agent: str) -> list[dict]:
    """Every existing agent one delegation edge away from ``agent``, in
    either direction, compiled and manual alike (the rows
    ``agent_store.get_delegation_targets`` reads), with its ``admin_only``
    flag. The edge table has no foreign key, so an edge naming a deleted
    agent is dropped by the join."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT a.slug, a.admin_only FROM ("
            " SELECT target_agent AS slug FROM agent_delegation_targets"
            " WHERE agent_name = %s"
            " UNION"
            " SELECT agent_name AS slug FROM agent_delegation_targets"
            " WHERE target_agent = %s"
            ") n JOIN agents a ON a.slug = n.slug "
            "WHERE a.slug <> %s ORDER BY a.slug",
            (agent, agent, agent),
        ).fetchall()


def department_members(department_id: str) -> list[dict]:
    """The agents placed in the department: slug, display name, level and
    ``admin_only``, the head's level first (an agent with no level last)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT a.slug, a.display_name, a.admin_only, "
            "a.department_level_id AS level_id "
            "FROM agents a LEFT JOIN department_levels l "
            "ON l.id = a.department_level_id "
            "WHERE a.department_id = %s "
            "ORDER BY l.rank NULLS LAST, a.slug",
            (department_id,),
        ).fetchall()


def insert_entry(entry_id: str, agent: str, author_sub: str | None,
                 surface: str, text: str, *, max_per_hour: int) -> dict | None:
    """Store an entry unless the agent already posted ``max_per_hour`` in
    the last hour (None then). The count and the insert hold a lock per
    agent, so two posts racing at the limit cannot both land."""
    with get_conn() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('notebook:' || %s))",
            (agent,),
        )
        row = conn.execute(
            "SELECT count(*) AS n FROM notebook_entries WHERE agent = %s "
            "AND created_at > now() - interval '1 hour'",
            (agent,),
        ).fetchone()
        if row is not None and int(row["n"]) >= max_per_hour:
            conn.rollback()
            return None
        created = conn.execute(
            "INSERT INTO notebook_entries (id, agent, author_sub, surface, text) "
            f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (entry_id, agent, author_sub, surface, text),
        ).fetchone()
        conn.commit()
        return created


def latest(agents: list[str], *, limit: int,
           since: datetime | None = None,
           before: tuple[datetime, str] | None = None) -> list[dict]:
    """The agents' entries that are not deleted, newest first, at most
    ``limit``; ``since`` keeps the newer ones, ``before`` (a row's
    ``created_at`` and ``id``) the older ones, for paging."""
    if not agents:
        return []
    clauses = ["agent = ANY(%s)", "deleted_at IS NULL"]
    params: list[object] = [agents]
    if since is not None:
        clauses.append("created_at >= %s")
        params.append(since)
    if before is not None:
        clauses.append("(created_at, id) < (%s, %s)")
        params.extend(before)
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_COLUMNS} FROM notebook_entries "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            params,
        ).fetchall()


def get_entry(entry_id: str) -> dict | None:
    """One entry, deleted or not."""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_COLUMNS}, deleted_at, deleted_by "
            "FROM notebook_entries WHERE id = %s",
            (entry_id,),
        ).fetchone()


def soft_delete(entry_id: str, deleted_by: str) -> bool:
    """Mark the entry deleted and blank its text; False when it was already
    deleted or does not exist."""
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE notebook_entries SET deleted_at = now(), deleted_by = %s, "
            "text = '' WHERE id = %s AND deleted_at IS NULL RETURNING id",
            (deleted_by, entry_id),
        ).fetchone()
        conn.commit()
        return row is not None


def prune_per_agent(keep: int, *, dry_run: bool = False) -> int:
    """Delete every agent's entries beyond its newest ``keep`` (deleted rows
    count like any other); the number of rows deleted, or that would be."""
    over = (
        "SELECT id FROM (SELECT id, row_number() OVER ("
        "PARTITION BY agent ORDER BY created_at DESC, id DESC) AS rn "
        "FROM notebook_entries) ranked WHERE rn > %s"
    )
    with get_conn() as conn:
        if dry_run:
            row = conn.execute(
                f"SELECT count(*) AS n FROM ({over}) o", (keep,),
            ).fetchone()
            return int(row["n"]) if row else 0
        total = 0
        while True:
            deleted = conn.execute(
                "DELETE FROM notebook_entries WHERE id IN "
                f"({over} LIMIT %s)",
                (keep, _PRUNE_BATCH),
            ).rowcount
            conn.commit()
            total += max(deleted, 0)
            if deleted < _PRUNE_BATCH:
                return total
