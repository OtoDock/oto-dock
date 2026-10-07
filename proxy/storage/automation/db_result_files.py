"""The files a delegated worker attached to its result, per run
(``delegate_result_files``; PROJECTS.md "Result files").

One row per file the attach landed in the delegator's tree, named where it
already was (the same tree), or skipped with its reason. The attach writes
the rows while the run is live; the delivery reads them once the run ended
and names them in the callback. The foreign key cleans the rows with their
run. All functions are synchronous (called via ``run_db`` from async code).
"""

from datetime import datetime, timezone

from storage.pg import get_conn

LANDED = "landed"
NAMED = "named"
SKIPPED = "skipped"
STATUSES = (LANDED, NAMED, SKIPPED)

_COLUMNS = ("run_id", "target_agent", "path", "landed_path", "ws_path",
            "bytes", "status", "reason", "created_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(run_id: str, target_agent: str, rows: list[dict]) -> None:
    """Insert the rows of one attach call: each ``{path, landed_path, ws_path,
    bytes, status, reason}`` (missing keys take the column defaults)."""
    if not rows:
        return
    now = _now()
    values = [
        (run_id, target_agent, r["path"],
         r.get("landed_path", ""), r.get("ws_path", ""), int(r.get("bytes", 0) or 0),
         r["status"], r.get("reason", ""), now)
        for r in rows
    ]
    with get_conn() as conn:
        conn.cursor().executemany(
            f"INSERT INTO delegate_result_files ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join(['%s'] * len(_COLUMNS))})",
            values,
        )
        conn.commit()


def list_for_run(run_id: str) -> list[dict]:
    """Every row of the run, in the order the attaches recorded them."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM delegate_result_files WHERE run_id = %s "
            "ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_for_run(run_id: str) -> int:
    """How many rows the run holds (every status counts toward the cap)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM delegate_result_files WHERE run_id = %s",
            (run_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


# A skipped row at or under a path the worker names again is replaced by the
# new call's rows (a directory re-attached re-classifies every entry under it).
_UNDER = ("run_id = %s AND status = %s AND (path = ANY(%s) OR EXISTS ("
          "SELECT 1 FROM unnest(%s::text[]) AS d WHERE starts_with(path, d || '/')))")


def count_skipped_under(run_id: str, paths: list[str]) -> int:
    """The run's skipped rows at or under these paths (the rows a new call
    naming them would replace, so the caps do not count them twice)."""
    if not paths:
        return 0
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM delegate_result_files WHERE {_UNDER}",
            (run_id, SKIPPED, list(paths), list(paths)),
        ).fetchone()
    return int(row["n"]) if row else 0


def drop_skipped_under(run_id: str, paths: list[str]) -> int:
    """Delete the run's skipped rows at or under these paths. Returns the
    count deleted."""
    if not paths:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM delegate_result_files WHERE {_UNDER}",
            (run_id, SKIPPED, list(paths), list(paths)),
        )
        conn.commit()
        return cur.rowcount or 0


def named_paths(run_id: str) -> set[str]:
    """The ``landed_path`` values already recorded as named on the run."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT landed_path FROM delegate_result_files "
            "WHERE run_id = %s AND status = %s",
            (run_id, NAMED),
        ).fetchall()
    return {r["landed_path"] for r in rows}
