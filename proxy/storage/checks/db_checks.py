"""The checks stores (CHECKS.md): the document index, the verdicts and the
per-agent settings. Synchronous; callers use ``run_db`` or ``to_thread``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from storage.pg import get_conn

VERDICT_LIST_MAX = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shape_index(row) -> dict:
    d = dict(row)
    try:
        d["doc"] = json.loads(d.get("doc") or "{}")
    except ValueError:
        d["doc"] = {}
    try:
        d["applies"] = json.loads(d.get("applies") or "[]")
    except ValueError:
        d["applies"] = []
    return d


# ── the index ───────────────────────────────────────────────────────────────


def upsert_index(agent: str, owner: str, name: str, doc: dict, *, doc_sha256: str,
                 script_sha256: str, updated_by: str) -> dict:
    """The row for a document as found on disk (or just written)."""
    applies = json.dumps(list(doc.get("applies") or []))
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_checks (agent, owner, name, doc, doc_sha256, script_sha256,
                                         mandatory, applies, updated_by, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (agent, owner, name) DO UPDATE SET
                 doc = EXCLUDED.doc, doc_sha256 = EXCLUDED.doc_sha256,
                 script_sha256 = EXCLUDED.script_sha256, mandatory = EXCLUDED.mandatory,
                 applies = EXCLUDED.applies, updated_by = EXCLUDED.updated_by,
                 updated_at = EXCLUDED.updated_at""",
            (agent, owner, name, json.dumps(doc, sort_keys=True), doc_sha256, script_sha256,
             bool(doc.get("mandatory")), applies, updated_by, _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM agent_checks WHERE agent=%s AND owner=%s AND name=%s",
            (agent, owner, name)).fetchone()
        return _shape_index(row)


def get_index(agent: str, owner: str, name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_checks WHERE agent=%s AND owner=%s AND name=%s",
            (agent, owner, name)).fetchone()
        return _shape_index(row) if row else None


def list_index(agent: str, owner: str | None = None) -> list[dict]:
    """Every indexed check of the agent (``owner`` narrows to one tree:
    ``''`` the agent's, a username the user's)."""
    with get_conn() as conn:
        if owner is None:
            rows = conn.execute(
                "SELECT * FROM agent_checks WHERE agent=%s ORDER BY owner, name", (agent,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_checks WHERE agent=%s AND owner=%s ORDER BY name",
                (agent, owner)).fetchall()
        return [_shape_index(r) for r in rows]


def delete_index(agent: str, owner: str, name: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM agent_checks WHERE agent=%s AND owner=%s AND name=%s",
            (agent, owner, name))
        conn.commit()
        return cur.rowcount > 0


def delete_index_missing(agent: str, owner: str, present: set[str]) -> int:
    """Drop the rows of documents that left the disk."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM agent_checks WHERE agent=%s AND owner=%s", (agent, owner),
        ).fetchall()
        gone = [r["name"] for r in rows if r["name"] not in present]
        for name in gone:
            conn.execute("DELETE FROM agent_checks WHERE agent=%s AND owner=%s AND name=%s",
                         (agent, owner, name))
        conn.commit()
        return len(gone)


# ── the verdicts ────────────────────────────────────────────────────────────


def _shape_verdict(row) -> dict:
    d = dict(row)
    try:
        d["findings"] = json.loads(d.get("findings") or "[]")
    except ValueError:
        d["findings"] = []
    return d


def insert_verdict(*, agent: str, owner: str, check_name: str, section: str, status: str,
                   passed: bool, score: float | None, findings: list, summary: str,
                   reason: str, session_id: str, chat_id: str, run_id: str, judge_run_id: str,
                   user_sub: str, round_no: int, ran_on: str, engine: str, model: str,
                   cost_usd: float, duration_ms: int, script_sha256: str) -> dict:
    vid = f"cv-{uuid.uuid4().hex[:16]}"
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO check_verdicts (id, agent, owner, check_name, section, status, pass,
                 score, findings, summary, reason, session_id, chat_id, run_id, judge_run_id,
                 user_sub, round, ran_on, engine, model, cost_usd, duration_ms, script_sha256,
                 created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (vid, agent, owner, check_name, section, status, bool(passed), score,
             json.dumps(findings or [], default=str), summary or "", reason or "",
             session_id or "", chat_id or "", run_id or "", judge_run_id or "", user_sub or "",
             int(round_no), ran_on or "", engine or "", model or "", float(cost_usd or 0),
             int(duration_ms or 0), script_sha256 or "", _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM check_verdicts WHERE id=%s", (vid,)).fetchone()
        return _shape_verdict(row)


def get_verdict(verdict_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM check_verdicts WHERE id=%s", (verdict_id,)).fetchone()
        return _shape_verdict(row) if row else None


def list_verdicts(agent: str, *, user_sub: str | None = None, chat_id: str = "",
                  check_name: str = "", limit: int = VERDICT_LIST_MAX, before: str = "") -> list[dict]:
    """Newest first. ``user_sub`` set → that user's own verdicts plus the
    agent-scope ones (a member's slice); None → every verdict (a manager).
    ``before`` (an ISO time) keeps the rows older than it — the page
    cursor of the Checks tab."""
    clauses = ["agent=%s"]
    args: list = [agent]
    if user_sub is not None:
        clauses.append("(user_sub=%s OR user_sub='')")
        args.append(user_sub)
    if chat_id:
        clauses.append("chat_id=%s")
        args.append(chat_id)
    if check_name:
        clauses.append("check_name=%s")
        args.append(check_name)
    if before:
        clauses.append("created_at < %s")
        args.append(before)
    args.append(max(1, min(int(limit), VERDICT_LIST_MAX)))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM check_verdicts WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT %s", args).fetchall()
        return [_shape_verdict(r) for r in rows]


# Aged verdicts go in committed batches of this many rows: the first sweep of
# a large install stays well inside the statement timeout.
_VERDICT_DELETE_BATCH = 5000


def delete_verdicts_before(cutoff_iso: str, *, dry_run: bool = False) -> int:
    """The retention sweep (SESSIONS.md): verdicts older than the session
    retention window go with the session files, in committed batches; the
    count, or what would go on a dry run."""
    with get_conn() as conn:
        if dry_run:
            row = conn.execute("SELECT COUNT(*) AS n FROM check_verdicts WHERE created_at < %s",
                               (cutoff_iso,)).fetchone()
            return int(row["n"] if row else 0)
        total = 0
        while True:
            cur = conn.execute(
                """DELETE FROM check_verdicts
                    WHERE id IN (SELECT id FROM check_verdicts
                                  WHERE created_at < %s LIMIT %s)""",
                (cutoff_iso, _VERDICT_DELETE_BATCH),
            )
            n = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            conn.commit()
            total += n
            if n < _VERDICT_DELETE_BATCH:
                return total


def last_verdict_for_chat(chat_id: str, check_name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM check_verdicts WHERE chat_id=%s AND check_name=%s "
            "ORDER BY created_at DESC LIMIT 1", (chat_id, check_name)).fetchone()
        return _shape_verdict(row) if row else None


def judge_spend_today(agent: str, day_start_iso: str) -> float:
    """The agent's judge spend since ``day_start_iso``: the runs of
    ``task_type='check'`` joined to the usage ledger through the run's chat
    (USAGE.md, the unattended grouping)."""
    from services.scheduler import task_kinds
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(ur.cost_usd),0) AS total FROM task_runs tr "
            "JOIN usage_records ur ON ur.source_id = tr.chat_id "
            "WHERE tr.agent=%s AND tr.task_type=%s AND ur.created_at>=%s",
            (agent, task_kinds.RUN_CHECK, day_start_iso)).fetchone()
        return float(row["total"] or 0) if row else 0.0


# ── the settings ────────────────────────────────────────────────────────────


def get_settings(agent: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM check_settings WHERE agent=%s", (agent,)).fetchone()
        if not row:
            return {"agent": agent, "daily_cap_usd": None, "updated_by": "", "updated_at": ""}
        return dict(row)


def set_settings(agent: str, *, daily_cap_usd: float | None, updated_by: str) -> dict:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO check_settings (agent, daily_cap_usd, updated_by, updated_at)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (agent) DO UPDATE SET daily_cap_usd = EXCLUDED.daily_cap_usd,
                 updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at""",
            (agent, daily_cap_usd, updated_by, _now()))
        conn.commit()
    return get_settings(agent)
