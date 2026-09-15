"""Dock file-pin queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).

A row pins a workspace FILE to a chat/project Dock
(``files/schema.py::init_pinned_files``): ``rel_path`` is agent-dir-relative and the dashboard
reads the content through the files API, so path policy and per-viewer role
enforcement never live here. Exactly one scope column is set; pins are
unique per (scope, rel_path) — a re-pin of the same path updates the title.
The per-scope cap is enforced at the pin hook (the only writer), not here.
"""

import uuid
from datetime import datetime, timezone

from storage.pg import get_conn

MAX_FILE_PINS_PER_SCOPE = 6


def upsert_file_pin(
    agent: str,
    rel_path: str,
    *,
    scope_chat_id: str = "",
    scope_project_id: str = "",
    title: str = "",
) -> dict:
    """Create the (scope, rel_path) pin or update its title/agent in place."""
    now = datetime.now(timezone.utc).isoformat()
    col = "scope_chat_id" if scope_chat_id else "scope_project_id"
    scope_id = scope_chat_id or scope_project_id
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM pinned_files WHERE {col}=%s AND rel_path=%s",
            (scope_id, rel_path),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE pinned_files SET title=%s, agent=%s, updated_at=%s "
                "WHERE id=%s",
                (title, agent, now, row["id"]),
            )
            pin_id = row["id"]
        else:
            pin_id = f"pf-{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO pinned_files
                   (id, agent, rel_path, title,
                    scope_chat_id, scope_project_id, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pin_id, agent, rel_path, title,
                 scope_chat_id or None, scope_project_id or None, now, now),
            )
        out = dict(conn.execute(
            "SELECT * FROM pinned_files WHERE id=%s", (pin_id,),
        ).fetchone())
        conn.commit()
        return out


def list_file_pins(*, chat_id: str = "", project_id: str = "") -> list[dict]:
    """The scope's pins, oldest first (stable Dock order). Pass exactly one
    scope id; both is a caller bug (chat pins and project pins are separate
    sections — the pins endpoint queries each on its own)."""
    col = "scope_chat_id" if chat_id else "scope_project_id"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM pinned_files WHERE {col}=%s "
            "ORDER BY created_at, id",
            (chat_id or project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_file_pins(
    *, chat_id: str = "", project_id: str = "", rel_path: str = "",
) -> int:
    """Delete the scope's pin for ``rel_path``, or ALL the scope's pins when
    ``rel_path`` is empty. Returns the number of rows removed."""
    col = "scope_chat_id" if chat_id else "scope_project_id"
    sql = f"DELETE FROM pinned_files WHERE {col}=%s"
    vals: list = [chat_id or project_id]
    if rel_path:
        sql += " AND rel_path=%s"
        vals.append(rel_path)
    with get_conn() as conn:
        cur = conn.execute(sql, vals)
        n = cur.rowcount or 0
        conn.commit()
        return n
