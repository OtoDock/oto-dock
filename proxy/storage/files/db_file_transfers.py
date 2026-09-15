"""Cross-agent file-transfer rows (delegation-mcp ``send_files``).

One row per transfer call — the audit record and the per-creator quota
counter. Deliberately NOT a notify source: the v1 session-start notice
(fetch-and-stamp on ``seen_at``) was removed 2026-08-14 after its first
live test — warmup/parallel config builds race to consume a once-only
stamp, while the prompt's workspace listing already shows
``workspace/inbox/<sender>/…`` on every session. ``seen_at`` stays in
the schema unused (reserved — a future "since last run" digest for
no-user briefing sessions can rebuild on it).

All functions are synchronous (called via asyncio.to_thread from async
code).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from storage.pg import get_conn

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_transfer(
    *,
    source_agent: str,
    target_agent: str,
    scope: str,
    owner_sub: str,
    dest_dir: str,
    file_count: int,
    total_bytes: int,
    note: str,
    created_by: str,
) -> str:
    """Insert one transfer row; returns its id."""
    tid = f"xfer-{uuid.uuid4().hex[:12]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_file_transfers "
            "(id, source_agent, target_agent, scope, owner_sub, dest_dir, "
            " file_count, total_bytes, note, created_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tid, source_agent, target_agent, scope, owner_sub, dest_dir,
             file_count, total_bytes, note, created_by, _now()),
        )
    return tid


def count_recent_by_creator(created_by: str, *, hours: int = 24) -> int:
    """Transfers this creator made in the trailing window (the daily quota)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_file_transfers "
            "WHERE created_by = %s AND created_at >= %s",
            (created_by, cutoff),
        ).fetchone()
    return int(row["n"]) if row else 0


def get_transfer(transfer_id: str) -> dict | None:
    """One transfer row by id (audit lookups / tests)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_file_transfers WHERE id = %s",
            (transfer_id,),
        ).fetchone()
    return dict(row) if row else None
