"""Persisted platform last-writer per file — the durable form of the in-memory
``_last_writer`` (``core/remote/satellite_connection.py``).

Read at merge time to classify a concurrent divergence: if the platform copy's
last writer differs from the satellite session's user, the divergence is
**cross-user** (capture the loser + notify); otherwise it is same-user
(newest-wins, no capture). Stores the username **slug** — what both the live
write-back path and the session-start merge hold natively, so neither pays a
sub-resolution on the hot path (a sub is resolved only when actually firing a
notification). Updated on every platform-side write (dashboard write/upload,
local file-tools + WOPI via ``propagate_write``, applied satellite write-back,
initial-sync pull) and cleared on delete.

**Best-effort by design**: an unknown author degrades a shared-file
divergence to a *silent* safety capture — never to data loss — so missing a
write-path here costs at most a missed notification, never a clobber. Coverage
matters only for notification quality, not correctness.

Synchronous (called via ``asyncio.to_thread``); commits on clean context exit.
"""

import logging
from datetime import datetime, timezone

from storage.pg import get_conn

logger = logging.getLogger("claude-proxy.file-author")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(agent_slug: str, rel_path: str, last_writer: str) -> None:
    """Upsert the platform last-writer (username slug) for a file. A falsy
    ``last_writer`` (agent-scope / system write with no user identity) is
    ignored — keep the last *known* human writer rather than blanking it."""
    if not last_writer:
        return
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO file_author (agent_slug, rel_path, last_writer, updated_at)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (agent_slug, rel_path)
               DO UPDATE SET last_writer=EXCLUDED.last_writer,
                             updated_at=EXCLUDED.updated_at""",
            (agent_slug, rel_path, last_writer, _now_iso()),
        )


def get(agent_slug: str, rel_path: str) -> str | None:
    """Return the last-writer username slug for a file, or ``None`` if unknown."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_writer FROM file_author "
            "WHERE agent_slug=%s AND rel_path=%s",
            (agent_slug, rel_path),
        ).fetchone()
    return row["last_writer"] if row is not None else None


def clear(agent_slug: str, rel_path: str) -> None:
    """Forget the author for a file (it was deleted)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM file_author WHERE agent_slug=%s AND rel_path=%s",
            (agent_slug, rel_path),
        )


def purge_prefix(agent_slug: str, rel_prefix: str) -> int:
    """Forget every author row under a directory prefix (a removed caller
    tree — services/infra/external_retention.py). Returns rows removed."""
    from storage.files.file_tombstones_store import _like_prefix
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM file_author WHERE agent_slug=%s "
            "AND rel_path LIKE %s ESCAPE '\\'",
            (agent_slug, _like_prefix(rel_prefix)),
        )
        conn.commit()
        return cur.rowcount
