"""PostgreSQL-backed log for the weekly automatic MCP-update job.

One row per MCP touched in a run (``services/mcp/mcp_autoupdate.py``). All functions
are synchronous — call via ``asyncio.to_thread`` from async code. The table is
created in ``storage/mcp/schema.py::init_mcp_autoupdate`` and pruned by the daily retention
sweep (``services/infra/retention.py``).
"""

import uuid
from datetime import datetime, timezone

from storage.pg import get_conn

# Statuses written to mcp_auto_update_log.status
STATUS_UPDATED = "updated"
STATUS_NO_CHANGE = "no_change"
STATUS_SKIPPED_IN_USE = "skipped_in_use"
STATUS_FAILED = "failed"
STATUS_HELD = "held"
# The catalog source of the MCP changed: never applied by the job, shown to
# admins for their switch (services/community/mcp_source_swap.py).
STATUS_NEEDS_APPROVAL = "needs_approval"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_result(
    run_id: str,
    mcp_name: str,
    *,
    runtime: str = "",
    old_version: str = "",
    new_version: str = "",
    status: str,
    error: str = "",
    trigger: str = "auto",
) -> None:
    """Insert one per-MCP result row for a run."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mcp_auto_update_log
               (id, run_id, mcp_name, runtime, old_version, new_version,
                status, error, trigger, ts)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                str(uuid.uuid4()), run_id, mcp_name, runtime or "",
                old_version or "", new_version or "", status,
                (error or "")[:2000], trigger, _now(),
            ),
        )
        conn.commit()


def recent_runs(limit: int = 50) -> list[dict]:
    """Most recent result rows (newest first), capped at ``limit``.

    The dashboard groups these by ``run_id`` to render the run history.
    """
    limit = max(1, min(int(limit), 500))
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT run_id, mcp_name, runtime, old_version, new_version,
                      status, error, trigger, ts
               FROM mcp_auto_update_log
               ORDER BY ts DESC
               LIMIT %s""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def prune(*, keep_days: int = 90, keep_rows: int = 500) -> int:
    """Delete rows older than ``keep_days`` while always retaining the newest
    ``keep_rows``. Returns the number deleted. Called from the daily sweep."""
    cutoff = (
        datetime.now(timezone.utc) - _timedelta_days(keep_days)
    ).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM mcp_auto_update_log
               WHERE ts < %s
                 AND id NOT IN (
                     SELECT id FROM mcp_auto_update_log
                     ORDER BY ts DESC LIMIT %s
                 )""",
            (cutoff, max(0, int(keep_rows))),
        )
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return deleted


def _timedelta_days(days: int):
    from datetime import timedelta
    return timedelta(days=max(0, int(days)))
