"""Storage layer for community MCP assignment requests.

A "request" is a manager asking the admin to install + enable an MCP for one
of their agents. The flow is:

    pending → approved → installing → installed
                       ↘ install_failed (retry → installing)
            → rejected
            → cancelled  (manager cancels before approval)

The DB constraint enforces "only one open request per (mcp, agent)" via a
partial unique index on the four open-state values (see ``schema.py``). Once
a request is ``installed``, ``rejected``, or ``cancelled``, a fresh request
for the same pair is allowed.

All functions are synchronous — call via ``asyncio.to_thread`` from async
contexts, matching the convention used elsewhere in ``proxy/storage``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


# Status transitions allowed in the engine. Any transition not listed here
# is rejected by ``update_status``. Frontend should ideally match.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending":        {"approved", "rejected", "cancelled"},
    "approved":       {"installing"},
    "installing":     {"installed", "install_failed"},
    "install_failed": {"installing"},  # retry
}

OPEN_STATES = ("pending", "approved", "installing", "install_failed")
TERMINAL_STATES = ("installed", "rejected", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Every read returns the request row plus display-friendly columns for both
# the requester and the resolver: ``requested_by_name`` /
# ``requested_by_email`` and ``resolved_by_name`` / ``resolved_by_email``.
# Joined via ``LEFT JOIN`` so a deleted user doesn't drop the request row —
# the name columns just come back as NULL in that case.
_REQUEST_SELECT = """
    SELECT r.*,
           ru.name  AS requested_by_name,
           ru.email AS requested_by_email,
           reu.name  AS resolved_by_name,
           reu.email AS resolved_by_email
    FROM mcp_assignment_requests r
    LEFT JOIN users ru  ON ru.sub  = r.requested_by
    LEFT JOIN users reu ON reu.sub = r.resolved_by
"""


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_request(
    mcp_name: str,
    agent_slug: str,
    requested_by: str,
    reason: str = "",
    batch_id: str | None = None,
    kind: str = "mcp",
) -> dict:
    """Create a new pending request. Caller must check no open request exists.

    Args:
        mcp_name: catalog slug (an MCP or, with ``kind="skill"``, a skill
            package — the endpoint validates against the matching catalog
            and approval dispatches to the matching installer).
        agent_slug: target agent.
        requested_by: requester's user_sub.
        reason: optional human justification surfaced on the admin Requests
            page and in the per-admin notification body. Always stored as a
            string (empty string when omitted) — never NULL.
        batch_id: optional UUID tagging requests that originate from a single
            community-agent install cascade. NULL for single-MCP requests
            via mcps-mcp or the Browse drawer. The batch_id lets the admin
            notification dispatcher collapse N rows into one notification
            and the resolution path fire a single "your agent is ready"
            follow-up notification.

    Returns the inserted row as a dict (with joined user-name columns).
    Raises ``ValueError`` if the partial unique index is violated.
    """
    now = _now()
    with get_conn() as conn:
        try:
            row = conn.execute(
                """WITH inserted AS (
                    INSERT INTO mcp_assignment_requests
                    (mcp_name, agent_slug, requested_by, reason, batch_id,
                     kind, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                    RETURNING *
                )
                SELECT i.*,
                       ru.name  AS requested_by_name,
                       ru.email AS requested_by_email,
                       NULL::text AS resolved_by_name,
                       NULL::text AS resolved_by_email
                FROM inserted i
                LEFT JOIN users ru ON ru.sub = i.requested_by""",
                (mcp_name, agent_slug, requested_by, reason or "", batch_id,
                 kind, now, now),
            ).fetchone()
            return dict(row)
        except Exception as exc:
            # psycopg surfaces unique-violations as ``UniqueViolation``; we
            # collapse to ValueError for the endpoint layer to translate.
            if "idx_mcp_requests_open" in str(exc):
                raise ValueError(
                    f"An open request for {mcp_name} on {agent_slug} already exists",
                )
            raise


# ---------------------------------------------------------------------------
# Batch grouping
# ---------------------------------------------------------------------------

def list_requests_by_batch(batch_id: str) -> list[dict]:
    """All requests for one batch, ordered by ID (creation order). Used for
    batch-aware notifications (one notification per admin per batch) and the
    admin Requests page's batch-grouping UI."""
    with get_conn() as conn:
        rows = conn.execute(
            f"{_REQUEST_SELECT} WHERE r.batch_id = %s ORDER BY r.id ASC",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def all_in_batch_terminal(batch_id: str) -> bool:
    """True if every row in batch is in a terminal state
    (``installed`` / ``rejected`` / ``cancelled``). ``install_failed`` is
    NOT terminal — admin may retry.

    Used by the post-resolution hook to decide whether to fire the single
    follow-up notification to the requester ("your community agent X is
    ready"). False while at least one row is still in flight.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status FROM mcp_assignment_requests WHERE batch_id = %s",
            (batch_id,),
        ).fetchall()
        if not rows:
            return False
        return all(r["status"] in TERMINAL_STATES for r in rows)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_request(request_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"{_REQUEST_SELECT} WHERE r.id=%s", (request_id,),
        ).fetchone()
        return dict(row) if row else None


def list_open_requests() -> list[dict]:
    """All open requests sorted by oldest-first (admin's queue)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"{_REQUEST_SELECT} WHERE r.status = ANY(%s) ORDER BY r.created_at ASC",
            (list(OPEN_STATES),),
        ).fetchall()
        return [dict(r) for r in rows]


def list_requests_for_agent(agent_slug: str, requested_by: str | None = None) -> list[dict]:
    """All requests for one agent, newest first. Optionally scope to a user."""
    where = "r.agent_slug=%s"
    params: list[Any] = [agent_slug]
    if requested_by:
        where += " AND r.requested_by=%s"
        params.append(requested_by)
    with get_conn() as conn:
        rows = conn.execute(
            f"{_REQUEST_SELECT} WHERE {where} ORDER BY r.created_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_install_failed_for_mcp(mcp_name: str) -> list[dict]:
    """All ``install_failed`` requests for one MCP, newest first.

    Used by the auto-retry-on-instance-save hook: when admin creates or
    updates an instance, we look up the failed requests for this MCP and
    retry the ones whose agent is now authorized via the saved instance.
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"{_REQUEST_SELECT} WHERE r.mcp_name=%s AND r.status='install_failed' "
            f"ORDER BY r.created_at DESC",
            (mcp_name,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_requests(limit: int = 200) -> list[dict]:
    """Admin's full request log including resolved entries."""
    with get_conn() as conn:
        rows = conn.execute(
            f"{_REQUEST_SELECT} ORDER BY r.created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def open_requests_by_pair() -> dict[tuple[str, str], int]:
    """Map ``(mcp_name, agent_slug) → request_id`` for every currently-open
    request. Used to annotate the Browse Community card grid with pending-state
    badges without N+1 queries."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, mcp_name, agent_slug FROM mcp_assignment_requests
               WHERE status = ANY(%s)""",
            (list(OPEN_STATES),),
        ).fetchall()
        return {(r["mcp_name"], r["agent_slug"]): r["id"] for r in rows}


def count_pending() -> int:
    """Pending requests only — used for the admin nav badge."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM mcp_assignment_requests WHERE status='pending'",
        ).fetchone()
        return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

def update_status(
    request_id: int,
    new_status: str,
    *,
    resolved_by: str | None = None,
    admin_note: str | None = None,
    install_log: str | None = None,
) -> dict:
    """Apply a status transition. Raises ``ValueError`` if not allowed.

    ``resolved_by`` / ``resolved_at`` get filled in once the request leaves
    the open-state set. ``admin_note`` / ``install_log`` are appended only
    when supplied (otherwise existing values are preserved).
    """
    now = _now()
    with get_conn() as conn:
        current = conn.execute(
            "SELECT status FROM mcp_assignment_requests WHERE id=%s FOR UPDATE",
            (request_id,),
        ).fetchone()
        if not current:
            raise ValueError(f"Request {request_id} not found")
        cur_status = current["status"]
        allowed = _ALLOWED_TRANSITIONS.get(cur_status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Cannot transition from {cur_status!r} to {new_status!r}",
            )

        sets = ["status=%s", "updated_at=%s"]
        params: list[Any] = [new_status, now]
        if admin_note is not None:
            sets.append("admin_note=%s")
            params.append(admin_note)
        if install_log is not None:
            sets.append("install_log=%s")
            params.append(install_log)
        if new_status in TERMINAL_STATES:
            sets.append("resolved_at=%s")
            params.append(now)
            if resolved_by is not None:
                sets.append("resolved_by=%s")
                params.append(resolved_by)
        params.append(request_id)
        conn.execute(
            f"UPDATE mcp_assignment_requests SET {', '.join(sets)} WHERE id=%s",
            params,
        )
        # Re-select via the join so the caller gets the same shape as the
        # other reads (including the resolver's name/email after a terminal
        # transition).
        row = conn.execute(
            f"{_REQUEST_SELECT} WHERE r.id=%s", (request_id,),
        ).fetchone()
        return dict(row)
