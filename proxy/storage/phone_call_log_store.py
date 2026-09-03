"""Phone call-log store — per-call outcome rows for the admin dashboard.

All functions are synchronous (called via asyncio.to_thread from async code).
Rows are written once per call by the daemon's teardown report and read by
the route call-log viewer. Retention is opportunistic: every insert prunes
rows older than ``RETENTION_DAYS`` (indexed on ``started_at``; ISO-8601 UTC
strings compare lexicographically).
"""

from datetime import datetime, timedelta, timezone

from storage.pg import get_conn

RETENTION_DAYS = 30

#: The daemon reports these; anything else is coerced to "failed" at the API.
VALID_OUTCOMES = {
    "completed", "pin_failed", "pin_cooldown", "pin_timeout", "hangup",
    "no_answer", "busy", "failed", "error", "rejected_capacity",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_call(data: dict) -> int:
    """Insert one call row (and prune expired rows). Returns the row id."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=RETENTION_DAYS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM phone_call_log WHERE started_at < %s", (cutoff,))
        row = conn.execute(
            """INSERT INTO phone_call_log
               (route_id, route_name, phone_server_id, agent, direction,
                from_number, to_number, transport, call_uuid, outcome,
                pin_attempts, started_at, ended_at, duration_s, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s)
               RETURNING id""",
            (
                data.get("route_id") or None,
                data.get("route_name", ""),
                data.get("phone_server_id"),
                data.get("agent", ""),
                data.get("direction", "inbound"),
                data.get("from_number", ""),
                data.get("to_number", ""),
                data.get("transport", ""),
                data.get("call_uuid", ""),
                data.get("outcome", "failed"),
                int(data.get("pin_attempts") or 0),
                data.get("started_at") or _now(),
                data.get("ended_at") or None,
                data.get("duration_s"),
                _now(),
            ),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def list_calls(
    route_id: str | None = None, *, offset: int = 0, limit: int = 50,
) -> tuple[list[dict], int]:
    """Newest-first page of call rows (optionally one route's) + total."""
    where, params = "", []
    if route_id:
        where = "WHERE route_id = %s"
        params = [route_id]
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM phone_call_log {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT * FROM phone_call_log {where}
                ORDER BY started_at DESC, id DESC
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], int(total)
