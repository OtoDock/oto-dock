"""The record of a department set's install (department_set_installer.py): one
row per job with its members' states and its lines, so a card's outcome
and the dialog read the row, and a restart marks the job it cut off.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import psycopg

from storage.pg import get_conn

# The job's states, spelled here alone: the installer and the routes
# compare through these names.
RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"
STATUSES = (RUNNING, DONE, FAILED, INTERRUPTED)


class AlreadyRunning(Exception):
    """A job runs on the set already (the partial unique index refused
    the second running row)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("members", "lines"):
        raw = out.get(key)
        out[key] = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return out


def create(set_slug: str, set_version: str, department_name: str, started_by: str,
           members: list[dict]) -> dict:
    """A new running job with its members' plan; ``AlreadyRunning`` when
    the set has one (the index is the claim)."""
    job_id = f"setjob-{uuid.uuid4().hex[:12]}"
    now = _now()
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO set_installs (id, set_slug, set_version, department_name, department_id, "
                "started_by, status, members, lines, started_at) "
                "VALUES (%s, %s, %s, %s, '', %s, %s, %s::jsonb, '[]'::jsonb, %s)",
                (job_id, set_slug, set_version, department_name, started_by, RUNNING,
                 json.dumps(members), now),
            )
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            raise AlreadyRunning(set_slug) from exc
        conn.commit()
    return get(job_id)  # type: ignore[return-value]


def get(job_id: str) -> dict | None:
    with get_conn() as conn:
        return _row(conn.execute("SELECT * FROM set_installs WHERE id = %s", (job_id,)).fetchone())


def update(job_id: str, *, status: str | None = None, department_id: str | None = None,
           members: list[dict] | None = None, lines: list[str] | None = None) -> dict | None:
    """Write what changed; an end state stamps ``ended_at``."""
    sets: list[str] = []
    vals: list = []
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        sets.append("status = %s")
        vals.append(status)
        if status != RUNNING:
            sets.append("ended_at = %s")
            vals.append(_now())
    if department_id is not None:
        sets.append("department_id = %s")
        vals.append(department_id)
    if members is not None:
        sets.append("members = %s::jsonb")
        vals.append(json.dumps(members))
    if lines is not None:
        sets.append("lines = %s::jsonb")
        vals.append(json.dumps(lines))
    if not sets:
        return get(job_id)
    vals.append(job_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE set_installs SET {', '.join(sets)} WHERE id = %s", vals)
        conn.commit()
    return get(job_id)


def running_for_set(set_slug: str) -> dict | None:
    """The job still running on a set, if any (one at a time per set)."""
    with get_conn() as conn:
        return _row(conn.execute(
            "SELECT * FROM set_installs WHERE set_slug = %s AND status = %s "
            "ORDER BY started_at DESC LIMIT 1", (set_slug, RUNNING)).fetchone())


def running_jobs() -> list[dict]:
    """Every job still marked running (at boot: the ones the last run cut off)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM set_installs WHERE status = %s ORDER BY started_at",
                            (RUNNING,)).fetchall()
    return [_row(r) for r in rows]  # type: ignore[misc]


def interrupt(job_id: str, line: str) -> None:
    """The job was cut off: its state and the sentence that says what stands."""
    with get_conn() as conn:
        row = conn.execute("SELECT lines FROM set_installs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            return
        lines = row["lines"] if isinstance(row["lines"], list) else json.loads(row["lines"] or "[]")
        conn.execute(
            "UPDATE set_installs SET status = %s, ended_at = %s, lines = %s::jsonb WHERE id = %s",
            (INTERRUPTED, _now(), json.dumps([*lines, line]), job_id))
        conn.commit()
