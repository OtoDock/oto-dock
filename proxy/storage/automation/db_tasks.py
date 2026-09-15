"""Task-run registry and dynamic-task queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


def create_run(run_id: str, task_id: str, agent: str, trigger_type: str,
               trigger_source: str | None, prompt: str,
               task_type: str | None = None,
               scope: str = "agent", created_by: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO task_runs (id, task_id, agent, trigger_type, trigger_source,
               status, prompt_preview, prompt_text, task_type, scope, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, task_id, agent, trigger_type, trigger_source,
             "pending", prompt[:200], prompt, task_type, scope, created_by),
        )
        conn.commit()


def update_run(run_id: str, *, status: str | None = None, output_text: str | None = None,
               error_message: str | None = None, session_id: str | None = None,
               started_at: str | None = None, completed_at: str | None = None,
               duration_ms: int | None = None, cost_usd: float | None = None,
               chat_id: str | None = None) -> None:
    fields: list[tuple[str, Any]] = []
    if status is not None:
        fields.append(("status", status))
    if output_text is not None:
        fields.append(("output_text", output_text))
    if error_message is not None:
        fields.append(("error_message", error_message))
    if session_id is not None:
        fields.append(("session_id", session_id))
    if started_at is not None:
        fields.append(("started_at", started_at))
    if completed_at is not None:
        fields.append(("completed_at", completed_at))
    if duration_ms is not None:
        fields.append(("duration_ms", duration_ms))
    if cost_usd is not None:
        fields.append(("cost_usd", cost_usd))
    if chat_id is not None:
        fields.append(("chat_id", chat_id))
    if not fields:
        return

    sql = f"UPDATE task_runs SET {', '.join(f'{k}=%s' for k, _ in fields)} WHERE id=%s"
    values = [v for _, v in fields] + [run_id]
    with get_conn() as conn:
        conn.execute(sql, values)
        conn.commit()


def list_orphaned_runs() -> list[dict]:
    """Task runs stuck in running/pending after a proxy restart (recovery)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE status IN ('running', 'pending')"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_orphaned_runs_failed(
    reason: str = "Proxy restarted", exclude_ids: list[str] | None = None,
) -> int:
    """Mark task_runs stuck in running/pending as failed (proxy restart recovery).

    ``exclude_ids`` skips rows the recovery path has parked for re-adoption
    (Mode C) so they aren't blind-failed out from under it.

    Returns the number of rows updated.  Idempotent — safe to call on every
    startup even if no runs are orphaned.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if exclude_ids:
            cur = conn.execute(
                "UPDATE task_runs SET status='failed', "
                "error_message=%s, completed_at=%s "
                "WHERE status IN ('running', 'pending') "
                "AND id != ALL(%s)",
                (reason, now, list(exclude_ids)),
            )
        else:
            cur = conn.execute(
                "UPDATE task_runs SET status='failed', "
                "error_message=%s, completed_at=%s "
                "WHERE status IN ('running', 'pending')",
                (reason, now),
            )
        conn.commit()
        return cur.rowcount


def get_run(run_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM task_runs WHERE id=%s", (run_id,)).fetchone()
        return dict(row) if row else None


def get_run_for_chat(chat_id: str) -> dict | None:
    """The chat's run row, for chat-level permission resolution. Covers BOTH
    id shapes: ``task-{run_id}`` chats and chat-surface delegate workers
    (plain-uuid ids with ``target_chat_id`` runs). ``scope``/``created_by``
    are stable across a multi-run worker chat, so any row serves — newest by
    start for determinism."""
    if not chat_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM task_runs WHERE chat_id=%s
               ORDER BY COALESCE(started_at, '') DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def get_runs_for_chats(chat_ids: list[str]) -> dict[str, dict]:
    """``chat_id → permission-relevant run row`` (newest by start), one query.
    Listing endpoints stamp per-row mutation flags with EXACTLY the logic the
    mutation endpoints enforce (``can_mutate_chat``), so a plain chat list
    containing uuid delegate-worker chats resolves their runs here instead of
    one query per row."""
    ids = [c for c in chat_ids if c]
    if not ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT ON (chat_id)
                      chat_id, id, task_id, scope, created_by, status
                 FROM task_runs WHERE chat_id = ANY(%s)
                ORDER BY chat_id, COALESCE(started_at, '') DESC""",
            (ids,),
        ).fetchall()
        return {r["chat_id"]: dict(r) for r in rows}


def list_live_run_chats(task_id: str) -> list[str]:
    """chat_ids of the task's running|pending runs — the definition-rename
    broadcast targets (live rows re-label instantly; idle rows poll)."""
    if not task_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT chat_id FROM task_runs
                WHERE task_id=%s AND status IN ('running','pending')
                  AND chat_id IS NOT NULL AND chat_id <> ''""",
            (task_id,),
        ).fetchall()
        return [r["chat_id"] for r in rows]


def has_live_run(chat_id: str) -> bool:
    """True when ANY of the chat's runs is running or pending — the guard
    against deleting a run chat out from under a live/queued run. Deliberately
    an EXISTS over all runs, never "the latest run's status": a pending run
    has no ``started_at`` and sorts LAST on a multi-run worker chat."""
    if not chat_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 AS live FROM task_runs
                WHERE chat_id=%s AND status IN ('running','pending') LIMIT 1""",
            (chat_id,),
        ).fetchone()
        return row is not None


def get_session_cost(session_id: str) -> tuple[float, int]:
    """Return (total_cost_usd, turn_count) for all runs in a session."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total_cost, COUNT(*) AS turn_count FROM task_runs WHERE session_id=%s",
            (session_id,),
        ).fetchone()
        return (row["total_cost"], row["turn_count"]) if row else (0.0, 0)


def list_runs(limit: int = 50, offset: int = 0, agent: str | None = None,
              status: str | None = None, task_id: str | None = None,
              session_id: str | None = None, chat_id: str | None = None,
              scope_user_sub: str | None = None,
              created_by: str | None = None,
              exclude_task_type: str | None = None,
              agents_filter: list[str] | None = None,
              edge_agents_filter: list[str] | None = None) -> list[dict]:
    """List task runs with optional scope filtering.

    scope_user_sub: when set, only returns runs where scope='agent' OR
    (scope='user' AND created_by matches). Pass None for unfiltered (admin/API key).
    created_by: explicit filter on created_by field (admin user filter dropdown).
    exclude_task_type: drop one run class from the listing (the dashboard
    excludes 'delegate' — delegations live in the chat history, not the
    Tasks page; direct task_id queries and schedules-mcp include them).
    agents_filter: restrict to these agents IN SQL (the caller's accessible
    set) — must constrain the same query that pages/counts, or LIMIT/total
    go wrong. None = unconstrained; [] = no reach, no rows.
    edge_agents_filter: extra agents a NO-USER caller may read via its
    delegation edges — agent-scope rows only (never other users' user-scope
    runs), OR-ed onto agents_filter in the same paged query. Only
    meaningful alongside agents_filter.

    Each row carries a computed ``unread`` bool — the run chat's last response
    landed after its read marker (same rule as ``list_chats``; the marker is
    keyed by the chat's own history-owner identity, which is exactly what
    ``chat_read`` upserts for personal AND shared-only chats). Runs without a
    chat are never unread.
    """
    conditions = []
    params: list[Any] = []
    if exclude_task_type:
        conditions.append("tr.task_type IS DISTINCT FROM %s")
        params.append(exclude_task_type)
    if agent:
        conditions.append("tr.agent=%s")
        params.append(agent)
    if agents_filter is not None:
        if edge_agents_filter:
            conditions.append(
                "(tr.agent = ANY(%s) OR (tr.agent = ANY(%s) "
                "AND tr.scope IS DISTINCT FROM 'user'))"
            )
            params.append(list(agents_filter))
            params.append(list(edge_agents_filter))
        else:
            conditions.append("tr.agent = ANY(%s)")
            params.append(list(agents_filter))
    if status:
        conditions.append("tr.status=%s")
        params.append(status)
    if task_id:
        conditions.append("tr.task_id=%s")
        params.append(task_id)
    if session_id:
        conditions.append("tr.session_id=%s")
        params.append(session_id)
    if chat_id:
        conditions.append("tr.chat_id=%s")
        params.append(chat_id)
    if scope_user_sub is not None:
        conditions.append("(tr.scope='agent' OR (tr.scope='user' AND tr.created_by=%s))")
        params.append(scope_user_sub)
    if created_by:
        conditions.append("tr.created_by=%s")
        params.append(created_by)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params += [limit, offset]
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT tr.*,
                       COALESCE(c.last_response_at IS NOT NULL
                                AND (r.last_read_at IS NULL
                                     OR c.last_response_at > r.last_read_at),
                                FALSE) AS unread
                  FROM task_runs tr
             LEFT JOIN chats c ON c.id = tr.chat_id
             LEFT JOIN chat_reads r ON r.chat_id = c.id AND r.user_sub = c.user_sub
                {where} ORDER BY COALESCE(tr.started_at, '') DESC LIMIT %s OFFSET %s""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def update_latest_run_status_for_chat(
    chat_id: str, status: str, only_from: tuple[str, ...] | None = None,
) -> str | None:
    """Flip the LATEST run row of a task-run chat to ``status``.

    Dashboard-resumed task conversations run through the chat pump — no new
    run row is created — so without this the Task History freezes on the
    pre-resume terminal state. The scheduler's own runs are never clobbered:
    a chat-sourced pump only exists on a task chat while no scheduler run
    drives it. ``only_from`` guards the transition: the pump's terminal flip
    passes ``("running",)`` so it only closes a turn IT opened — a wedged-pump
    reap stamps ``failed`` + reason first, and that richer verdict must win.
    Returns the flipped run id (None: no runs / guard didn't match)."""
    if not chat_id:
        return None
    guard = ""
    params: list[Any] = [status, chat_id]
    if only_from:
        guard = " AND status = ANY(%s)"
        params.append(list(only_from))
    with get_conn() as conn:
        row = conn.execute(
            f"""UPDATE task_runs SET status=%s WHERE id = (
                   SELECT id FROM task_runs WHERE chat_id=%s
                    ORDER BY COALESCE(started_at, '') DESC LIMIT 1
               ){guard} RETURNING id""",
            params,
        ).fetchone()
        conn.commit()
        return row["id"] if row else None


def count_active_delegate_runs(created_by: str) -> int:
    """Active (pending/running) delegated-worker runs attributed to one
    creator — the input to the per-creator spawn cap."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_runs "
            "WHERE task_type='delegate' AND status IN ('pending','running') "
            "AND created_by=%s",
            (created_by,),
        ).fetchone()
        return row["cnt"] if row else 0


def get_run_count(agent: str | None = None, status: str | None = None,
                  scope_user_sub: str | None = None,
                  created_by: str | None = None,
                  exclude_task_type: str | None = None,
                  agents_filter: list[str] | None = None,
                  edge_agents_filter: list[str] | None = None) -> int:
    conditions = []
    params: list[Any] = []
    if exclude_task_type:
        conditions.append("task_type IS DISTINCT FROM %s")
        params.append(exclude_task_type)
    if agent:
        conditions.append("agent=%s")
        params.append(agent)
    if agents_filter is not None:
        if edge_agents_filter:
            conditions.append(
                "(agent = ANY(%s) OR (agent = ANY(%s) "
                "AND scope IS DISTINCT FROM 'user'))"
            )
            params.append(list(agents_filter))
            params.append(list(edge_agents_filter))
        else:
            conditions.append("agent = ANY(%s)")
            params.append(list(agents_filter))
    if status:
        conditions.append("status=%s")
        params.append(status)
    if scope_user_sub is not None:
        conditions.append("(scope='agent' OR (scope='user' AND created_by=%s))")
        params.append(scope_user_sub)
    if created_by:
        conditions.append("created_by=%s")
        params.append(created_by)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS cnt FROM task_runs {where}", params).fetchone()
        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Run statistics
# ---------------------------------------------------------------------------


def get_stats() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        total_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_runs WHERE started_at LIKE %s", (f"{today}%",)
        ).fetchone()["cnt"]
        running = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_runs WHERE status='running'"
        ).fetchone()["cnt"]
        failed_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_runs WHERE status='failed' AND started_at LIKE %s",
            (f"{today}%",),
        ).fetchone()["cnt"]
        return {"total_today": total_today, "running": running, "failed_today": failed_today}


# --- Dynamic tasks ---


def create_dynamic_task(task_id: str, agent: str, name: str, prompt: str, llm_mode: str,
                        task_type: str, schedule: str | None, run_at: str | None,
                        delay_seconds: int | None, timeout_seconds: int,
                        created_by: str | None,
                        on_complete_agent: str | None = None,
                        on_complete_prompt: str | None = None,
                        on_complete_session_id: str | None = None,
                        on_complete_chat_id: str | None = None,
                        continue_session: str | None = None,
                        use_persistent: bool = False,
                        scope: str = "user",
                        notification_mode: str = "manual",
                        notify_severity: str = "info",
                        user_tz: str | None = None,
                        interval_seconds: int | None = None,
                        community_template: str | None = None,
                        community_template_item_slug: str | None = None,
                        target_chat_id: str | None = None,
                        max_runs: int | None = None,
                        until_at: str | None = None,
                        override_model: str | None = None,
                        override_execution_path: str | None = None) -> None:
    """Insert a dynamic_tasks row.

    ``community_template`` + ``community_template_item_slug`` are populated
    when the row is seeded by the community-agents installer. The
    unique partial index on ``(agent, item_slug[, created_by])`` makes the
    seed call idempotent — re-running the installer is safe.

    ``target_chat_id`` runs the task's turn inside that existing chat
    (chat-surface delegation + continuations); ``max_runs``/``until_at``
    bound recurring continuations.

    ``override_model`` / ``override_execution_path`` pin this task's runs to
    a specific model / execution layer. Empty (the default) = inherit the
    agent's default at fire time; the caller validates against the agent's
    envelope before writing.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO dynamic_tasks
               (id, agent, name, prompt, llm_mode, task_type, schedule, run_at,
                delay_seconds, interval_seconds, timeout_seconds, created_at, created_by,
                on_complete_agent, on_complete_prompt, on_complete_session_id,
                on_complete_chat_id, continue_session, use_persistent, scope,
                notification_mode, notify_severity, user_tz,
                community_template, community_template_item_slug,
                target_chat_id, max_runs, until_at,
                override_model, override_execution_path)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (task_id, agent, name, prompt, llm_mode, task_type, schedule, run_at,
             delay_seconds, interval_seconds, timeout_seconds, now, created_by,
             on_complete_agent, on_complete_prompt, on_complete_session_id,
             on_complete_chat_id, continue_session, use_persistent, scope,
             notification_mode, notify_severity, user_tz,
             community_template, community_template_item_slug,
             target_chat_id, max_runs, until_at,
             override_model or "", override_execution_path or ""),
        )
        conn.commit()


def get_dynamic_task(task_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM dynamic_tasks WHERE id=%s", (task_id,)).fetchone()
        return dict(row) if row else None


def list_dynamic_tasks(agent: str | None = None, enabled_only: bool = False) -> list[dict]:
    conditions = []
    params: list[Any] = []
    if agent:
        conditions.append("agent=%s")
        params.append(agent)
    if enabled_only:
        conditions.append("enabled=TRUE")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM dynamic_tasks {where} ORDER BY created_at DESC", params
        ).fetchall()
        return [dict(r) for r in rows]


def count_dynamic_tasks_by_agent() -> dict[str, int]:
    """Return {agent_slug: task_count} for all agents with at least one task.

    GLOBAL count (every scope + every user). Only for admin/API-key callers; the
    user-facing agents grid uses :func:`count_user_visible_dynamic_tasks_by_agent`
    so the per-agent number matches what that user sees on the Scheduled Tasks tab.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS cnt FROM dynamic_tasks GROUP BY agent"
        ).fetchall()
        return {r["agent"]: r["cnt"] for r in rows}


def count_user_visible_dynamic_tasks_by_agent(user_sub: str) -> dict[str, int]:
    """Return {agent_slug: task_count} of tasks ``user_sub`` may see: agent-scoped
    (shared) + their OWN user-scoped. Never counts other users' user-scoped tasks.
    Mirrors the Scheduled Tasks tab's filter (api/tasks/tasks.py::list_tasks)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS cnt FROM dynamic_tasks "
            "WHERE scope='agent' OR (scope='user' AND created_by=%s) "
            "GROUP BY agent",
            (user_sub,),
        ).fetchall()
        return {r["agent"]: r["cnt"] for r in rows}


def count_recent_runs_by_agent(since: str, user_sub: str) -> dict[str, int]:
    """{agent: run_count} for task runs started since ``since`` that this
    user may see — agent-scoped (shared) + their OWN user-scoped, the same
    predicate as :func:`count_user_visible_dynamic_tasks_by_agent`. Feeds the
    agents-map heat (task half); ``idx_runs_started_at`` keeps it cheap."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS cnt FROM task_runs "
            "WHERE started_at >= %s "
            "AND (scope='agent' OR (scope='user' AND created_by=%s)) "
            "GROUP BY agent",
            (since, user_sub),
        ).fetchall()
        return {r["agent"]: r["cnt"] for r in rows}


def delete_dynamic_task(task_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM dynamic_tasks WHERE id=%s", (task_id,))
        conn.commit()
        return cur.rowcount > 0


def list_running_task_runs() -> list[dict]:
    """Currently-running task runs joined with their task's name — the
    sibling-awareness "background tasks" feed."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT r.id AS run_id, r.task_id, r.agent, r.task_type, r.scope, "
            "       r.created_by, r.chat_id, COALESCE(d.name, '') AS name "
            "FROM task_runs r LEFT JOIN dynamic_tasks d ON d.id = r.task_id "
            "WHERE r.status='running'",
        ).fetchall()
        return [dict(r) for r in rows]


def list_continuations_for_chat(chat_id: str) -> list[dict]:
    """Pending continuation rows targeting this chat — cancelled alongside a
    chat delete (a deleted chat must never be woken)."""
    if not chat_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dynamic_tasks "
            "WHERE task_type='continuation' AND target_chat_id=%s",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_dynamic_task_fired(task_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET fired=TRUE WHERE id=%s", (task_id,))
        conn.commit()


def increment_dynamic_task_run_count(task_id: str) -> int:
    """Bump run_count and return the new value (0 if the row is gone).

    Used by recurring continuations to enforce their max_runs bound —
    atomic so concurrent fires can't double-count."""
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE dynamic_tasks SET run_count = run_count + 1 "
            "WHERE id=%s RETURNING run_count",
            (task_id,),
        ).fetchone()
        conn.commit()
        return row["run_count"] if row else 0


def update_dynamic_task_on_complete(
    task_id: str,
    on_complete_agent: str | None,
    on_complete_prompt: str | None,
    on_complete_session_id: str | None,
    on_complete_chat_id: str | None = None,
) -> None:
    """Set or clear the on_complete callback fields for a running task.

    Called by create_task_and_wait when the SSE wait times out — registers
    a late callback so the agent is still notified when the task finishes.
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE dynamic_tasks
               SET on_complete_agent=%s, on_complete_prompt=%s, on_complete_session_id=%s,
                   on_complete_chat_id=%s
               WHERE id=%s""",
            (on_complete_agent, on_complete_prompt, on_complete_session_id,
             on_complete_chat_id, task_id),
        )
        conn.commit()


def set_dynamic_task_enabled(task_id: str, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET enabled=%s WHERE id=%s", (enabled, task_id))
        conn.commit()


# Columns the edit_task API and MCP tool may touch. Anything outside this
# whitelist (id, agent, scope, created_by, source-internal fields, callback
# fields) is rejected at the helper level.
_EDITABLE_TASK_COLUMNS = {
    "name",
    "prompt",
    "schedule",
    "run_at",
    "interval_seconds",  # recurring every N seconds — mutually exclusive with schedule/run_at
    "timeout_seconds",
    "notification_mode",
    "notify_severity",
    "task_type",  # auto-derived by the service helper when timing fields change
    "user_tz",  # IANA timezone snapshot — change forces re-register with new trigger TZ
    # Per-task execution overrides — retune a live schedule onto another
    # model / engine, or pass "" to fall back to the agent's default. Both
    # are re-validated against the agent's envelope by the API layer.
    "override_model",
    "override_execution_path",
}


def update_dynamic_task(task_id: str, fields: dict) -> bool:
    """Apply a partial update to a dynamic_tasks row.

    Only columns in ``_EDITABLE_TASK_COLUMNS`` are accepted. Unknown columns
    are silently dropped (the API layer validates the request schema before
    we get here, so unknown columns shouldn't reach this function in normal
    flow). Pass ``None`` for a field to set it to NULL (used to clear the
    opposing timing field on mode switch — e.g., setting ``run_at=None``
    when switching from one-time to recurring).

    Returns True if the row exists and was updated.
    """
    safe = {k: v for k, v in fields.items() if k in _EDITABLE_TASK_COLUMNS}
    if not safe:
        return False
    set_clause = ", ".join(f"{k}=%s" for k in safe.keys())
    params = list(safe.values()) + [task_id]
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE dynamic_tasks SET {set_clause} WHERE id=%s",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def get_task_session(task_id: str) -> str | None:
    """Return session_id from the most recent completed run of a task."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM task_runs WHERE task_id=%s AND status='completed' "
            "ORDER BY started_at DESC LIMIT 1", (task_id,)
        ).fetchone()
        return row["session_id"] if row else None


# --- Session → run lookup ---


def get_run_by_session(session_id: str) -> dict | None:
    """Return the most recent ``task_runs`` row for ``session_id``, or None.

    Pulls the latest run for the sid so this works for both in-flight
    (``running``/``pending``) and just-completed runs.
    """
    if not session_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM task_runs WHERE session_id=%s "
            "ORDER BY started_at DESC NULLS LAST LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
