"""Durable handler deliveries (APPS.md "Handlers"): one row per wake of an
app's server, written BEFORE anything wakes, so a restart, an idle app or a
crash mid-POST loses nothing. The drain (``services/apps/app_handlers.py``)
claims a row with a lease (``inflight``, ``next_at`` = the lease end),
retries on 5xx and timeouts, marks 4xx and failed pre-checks ``dead``.

Timestamps are ISO UTC text like the other automation tables, so
``next_at <= now`` compares as text. The proxy is one process, so a claim
is a conditional UPDATE, never a row lock.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from storage.pg import get_conn

# The status vocabulary (``app_handler_deliveries.status``, named once —
# core-seams phase 8; no dashboard reads it): ``pending`` (queued, ``next_at``
# says when), ``inflight`` (claimed under a lease), ``done`` and ``dead``
# (the verdicts). ``finish`` / ``finish_step`` refuse another word. ``gone``
# is ``retry``'s answer for a row that vanished, never a stored status.
PENDING = "pending"
INFLIGHT = "inflight"
DONE = "done"
DEAD = "dead"
STATUSES: frozenset[str] = frozenset({PENDING, INFLIGHT, DONE, DEAD})
#: Queued or claimed — the rows the queue cap and ``count_active`` count.
ACTIVE: frozenset[str] = frozenset({PENDING, INFLIGHT})
VERDICTS: frozenset[str] = frozenset({DONE, DEAD})
GONE = "gone"

PAYLOAD_MAX_BYTES = 256 * 1024
# Over the cap a payload is cut down to its small top-level fields rather
# than dropped (a big GitHub push carries hundreds of file names; the wake
# still matters): every field whose own JSON fits this stays.
PAYLOAD_KEY_KEEP_BYTES = 4 * 1024
PENDING_CAP = 1000
# The first fire plus one retry per delay: five fires over about eighteen
# minutes, then the row is dead.
RETRY_DELAYS_S = (30, 120, 300, 600)
MAX_ATTEMPTS = len(RETRY_DELAYS_S) + 1
PENDING_MAX_AGE_S = 24 * 3600
LEASE_S = 90
DONE_KEEP_DAYS = 7
DEAD_KEEP_DAYS = 30
PURGE_BATCH = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_payload(payload) -> str:
    return json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True,
                      separators=(",", ":"), default=str)


def _shape(row) -> dict:
    d = dict(row)
    p = d.get("payload")
    if isinstance(p, (str, bytes)):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    d["payload"] = p if isinstance(p, dict) else {}
    return d


def trim_payload(payload: dict, size: int) -> dict:
    """The payload over the cap, cut down: the top-level fields whose own
    JSON is small stay, the rest go, and the row says so — ``payload_
    truncated``, the ``payload_dropped`` keys and ``payload_bytes`` of the
    original — so the handler can fetch what it needs by id."""
    kept: dict = {}
    dropped: list[str] = []
    for key, value in payload.items():
        if len(canonical_payload({key: value}).encode("utf-8")) <= PAYLOAD_KEY_KEEP_BYTES:
            kept[key] = value
        else:
            dropped.append(str(key))
    kept["payload_truncated"] = True
    kept["payload_dropped"] = sorted(dropped)
    kept["payload_bytes"] = size
    return kept


def enqueue(app_id: str, handler: str, event: str, payload, *, event_id: str = "",
            trigger_id: str | None = None, task_run_id: str | None = None) -> dict | None:
    """Write one pending delivery; the row, or None when ``event_id`` was
    seen before for this handler (the caller answers "duplicate"). A
    payload over the cap is trimmed to its small fields (``trim_payload``);
    one that stays over it even so, and a full queue, land as ``dead``
    rows, so the reason shows where the wakes are listed — without the
    event id, so the sender's retry of a refused event is a new event, not
    a duplicate of the refusal."""
    text = canonical_payload(payload)
    size = len(text.encode("utf-8"))
    status, error = PENDING, ""
    if size > PAYLOAD_MAX_BYTES:
        text = canonical_payload(trim_payload(payload if isinstance(payload, dict) else {}, size))
        if len(text.encode("utf-8")) > PAYLOAD_MAX_BYTES:
            status, error, text = DEAD, f"payload over {PAYLOAD_MAX_BYTES // 1024} KB", "{}"
    now = _now().isoformat()
    with get_conn() as conn:
        if status == PENDING:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM app_handler_deliveries "
                "WHERE app_id=%s AND status = ANY(%s)", (app_id, sorted(ACTIVE)),
            ).fetchone()["n"]
            if n >= PENDING_CAP:
                status, error = DEAD, "queue full"
        row = conn.execute(
            """INSERT INTO app_handler_deliveries
               (id, app_id, handler, event, payload, event_id, trigger_id, task_run_id,
                status, next_at, last_error, created_at, done_at)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (app_id, handler, event_id) WHERE event_id <> '' DO NOTHING
               RETURNING *""",
            (str(uuid.uuid4()), app_id, handler, event, text, "" if status == DEAD else event_id or "",
             trigger_id, task_run_id, status, now, error, now, now if status == DEAD else None),
        ).fetchone()
        conn.commit()
        return _shape(row) if row else None


def get(delivery_id: str) -> dict | None:
    if not delivery_id:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM app_handler_deliveries WHERE id=%s",
                           (delivery_id,)).fetchone()
        return _shape(row) if row else None


def find_event(app_id: str, handler: str, event_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM app_handler_deliveries WHERE app_id=%s AND handler=%s AND event_id=%s",
            (app_id, handler, event_id),
        ).fetchone()
        return _shape(row) if row else None


def due_app_ids() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT app_id FROM app_handler_deliveries "
            "WHERE status=%s AND next_at <= %s", (PENDING, _now().isoformat()),
        ).fetchall()
        return [r["app_id"] for r in rows]


def claim_next(app_id: str, *, lease_s: int = LEASE_S,
               exclude_handlers: list[str] | None = None) -> dict | None:
    """Take the app's oldest due row: ``inflight`` with the lease end in
    ``next_at`` (a row past its lease is reclaimed by ``reclaim_expired``).
    ``exclude_handlers`` leaves the named handlers' rows in the queue — the
    drain skips a step handler whose script is still running (APPS.md
    "Steps": one at a time per step, the server lane never waits)."""
    now = _now()
    skip = [h for h in (exclude_handlers or []) if isinstance(h, str)]
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE app_handler_deliveries SET status=%s, claimed_at=%s, next_at=%s
               WHERE id = (SELECT id FROM app_handler_deliveries
                           WHERE app_id=%s AND status=%s AND next_at <= %s
                             AND NOT (handler = ANY(%s))
                           ORDER BY next_at, created_at, id LIMIT 1)
               RETURNING *""",
            (INFLIGHT, now.isoformat(), (now + timedelta(seconds=lease_s)).isoformat(),
             app_id, PENDING, now.isoformat(), skip),
        ).fetchone()
        conn.commit()
        return _shape(row) if row else None


def _verdict(status: str) -> str:
    if status not in VERDICTS:
        raise ValueError(f"a delivery ends done or dead, not {status!r}")
    return status


def _text(value: str) -> str:
    """Postgres refuses NUL in a text column; a script or a server that
    prints one must not turn its verdict into an error (and a retry)."""
    return (value or "").replace("\x00", "")


def finish(delivery_id: str, status: str, *, error: str = "") -> None:
    """``done`` or ``dead``."""
    status = _verdict(status)
    with get_conn() as conn:
        conn.execute(
            "UPDATE app_handler_deliveries SET status=%s, done_at=%s, last_error=%s WHERE id=%s",
            (status, _now().isoformat(), _text(error)[:2000], delivery_id),
        )
        conn.commit()


# ── steps (APPS.md "Steps") ─────────────────────────────────────────────────

STEP_OUTPUT_KEEP_BYTES = 32 * 1024


def extend_lease(delivery_id: str, lease_s: int) -> None:
    """A step may run far longer than a handler's POST: its lease is its
    timeout plus a margin, set once the runner knows the step, so the sweep
    never reclaims a row whose script is still running."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE app_handler_deliveries SET next_at=%s WHERE id=%s AND status=%s",
            ((_now() + timedelta(seconds=max(1, int(lease_s)))).isoformat(), delivery_id, INFLIGHT),
        )
        conn.commit()


def mark_started(delivery_id: str, ran_on: str) -> None:
    """Where the step runs (``local`` or a machine id) and when it started."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE app_handler_deliveries SET ran_on=%s, started_at=%s WHERE id=%s",
            (ran_on[:64], _now().isoformat(), delivery_id),
        )
        conn.commit()


def finish_step(delivery_id: str, status: str, *, exit_code: int | None, output: str,
                error: str = "") -> None:
    """A step's verdict: ``done`` on exit 0, ``dead`` otherwise, with the
    exit code and the first 32 KB of what the script printed."""
    status = _verdict(status)
    text = _text(output)
    if len(text.encode("utf-8")) > STEP_OUTPUT_KEEP_BYTES:
        text = text.encode("utf-8")[:STEP_OUTPUT_KEEP_BYTES].decode("utf-8", "replace")
    with get_conn() as conn:
        conn.execute(
            "UPDATE app_handler_deliveries SET status=%s, done_at=%s, last_error=%s, "
            "exit_code=%s, output=%s WHERE id=%s",
            (status, _now().isoformat(), _text(error)[:2000], exit_code, text, delivery_id),
        )
        conn.commit()


def retry(delivery_id: str, *, error: str, count: bool = True,
          delay_s: float | None = None) -> str:
    """Put the row back in the queue: a counted attempt takes the next delay
    of the schedule and dies after the last one; an uncounted re-arm (the
    app was unavailable) waits ``delay_s``. A row older than a day dies
    either way. A row that already has its verdict keeps it (an error
    raised after the verdict was written must not fire the wake again).
    Returns the new status."""
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, attempts, created_at FROM app_handler_deliveries WHERE id=%s",
            (delivery_id,),
        ).fetchone()
        if not row:
            return GONE
        if row["status"] in VERDICTS:
            return row["status"]
        attempts = int(row["attempts"] or 0) + (1 if count else 0)
        try:
            created = datetime.fromisoformat(str(row["created_at"]))
        except ValueError:
            created = now
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        status = PENDING
        reason = _text(error)[:2000]
        if count and attempts >= MAX_ATTEMPTS:
            status = DEAD
        elif (now - created).total_seconds() > PENDING_MAX_AGE_S:
            status, reason = DEAD, f"the app never came up: {error}"[:2000]
        if status == DEAD:
            conn.execute(
                "UPDATE app_handler_deliveries SET status=%s, attempts=%s, done_at=%s, "
                "last_error=%s WHERE id=%s",
                (DEAD, attempts, now.isoformat(), reason, delivery_id),
            )
        else:
            delay = float(delay_s) if delay_s is not None else float(RETRY_DELAYS_S[attempts - 1])
            conn.execute(
                "UPDATE app_handler_deliveries SET status=%s, attempts=%s, next_at=%s, "
                "last_error=%s WHERE id=%s",
                (PENDING, attempts, (now + timedelta(seconds=max(1.0, delay))).isoformat(),
                 reason, delivery_id),
            )
        conn.commit()
        return status


def reset_inflight() -> int:
    """Boot: a row left in flight was mid-POST when the proxy died."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE app_handler_deliveries SET status=%s, next_at=%s WHERE status=%s",
            (PENDING, _now().isoformat(), INFLIGHT),
        )
        conn.commit()
        return cur.rowcount


def reclaim_expired() -> int:
    """A lease that passed without a verdict (a cancelled drain) goes back
    to the queue, not counted."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE app_handler_deliveries SET status=%s "
            "WHERE status=%s AND next_at <= %s", (PENDING, INFLIGHT, _now().isoformat()),
        )
        conn.commit()
        return cur.rowcount


def recent(app_id: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, handler, event, status, attempts, last_error, created_at, done_at, "
            "exit_code, ran_on, started_at "
            "FROM app_handler_deliveries WHERE app_id=%s ORDER BY created_at DESC LIMIT %s",
            (app_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(r) for r in rows]


def count_active(app_id: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM app_handler_deliveries "
            "WHERE app_id=%s AND status = ANY(%s)", (app_id, sorted(ACTIVE)),
        ).fetchone()["n"]


def purge_old() -> int:
    """Retention: ``done`` after a week, ``dead`` after a month, a batch at
    a time (the sweep calls it)."""
    now = _now()
    done_before = (now - timedelta(days=DONE_KEEP_DAYS)).isoformat()
    dead_before = (now - timedelta(days=DEAD_KEEP_DAYS)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM app_handler_deliveries WHERE id IN (
                   SELECT id FROM app_handler_deliveries
                   WHERE (status=%s AND done_at < %s) OR (status=%s AND done_at < %s)
                   LIMIT %s)""",
            (DONE, done_before, DEAD, dead_before, PURGE_BATCH),
        )
        conn.commit()
        return cur.rowcount
