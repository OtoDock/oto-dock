"""Chat, message, media-token, and plan queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

import os
from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


# --- Chat Search (tsvector) ---


def _rebuild_chat_search_row(conn, chat_id: str) -> None:
    """Rebuild the search row for a single chat from its messages + title.

    Must be called with an open connection (caller manages commit).
    """
    chat = conn.execute(
        "SELECT user_sub, agent, title FROM chats WHERE id=%s", (chat_id,)
    ).fetchone()
    if not chat:
        return
    messages = conn.execute(
        "SELECT content FROM chat_messages WHERE chat_id=%s AND role IN ('user','assistant') AND content != ''",
        (chat_id,),
    ).fetchall()
    combined = "\n".join(m["content"] for m in messages)
    conn.execute(
        """INSERT INTO chat_search (chat_id, user_sub, agent, title, content)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (chat_id) DO UPDATE SET
               title = EXCLUDED.title, content = EXCLUDED.content""",
        (chat_id, chat["user_sub"], chat["agent"], chat["title"] or "", combined),
    )


def _sanitize_fts_query(query: str) -> str:
    """Convert user query to tsquery format with prefix matching."""
    tokens = query.strip().split()
    if not tokens:
        return ""
    clean_tokens = []
    for t in tokens:
        cleaned = "".join(c for c in t if c.isalnum() or c in "-_")
        if cleaned:
            clean_tokens.append(cleaned)
    if not clean_tokens:
        return ""
    return " & ".join(f"{t}:*" for t in clean_tokens)


def search_chats(user_sub: str, agent: str, query: str, limit: int = 50) -> list[dict]:
    """Search chats by title or content using tsvector. Returns matching chat rows.

    Chat mode only — task-run chats are excluded (a user-scoped task chat
    carries the creator's sub, so the owner filter alone would leak it here);
    task mode searches through ``search_task_chats``."""
    tsquery = _sanitize_fts_query(query)
    if not tsquery:
        return []
    with get_conn() as conn:
        try:
            rows = conn.execute(
                """SELECT c.* FROM chat_search s
                   JOIN chats c ON c.id = s.chat_id
                   WHERE s.user_sub = %s AND s.agent = %s
                   AND c.id NOT LIKE 'task-%%'
                   AND s.search_vector @@ to_tsquery('english', %s)
                   ORDER BY ts_rank(s.search_vector, to_tsquery('english', %s)) DESC
                   LIMIT %s""",
                (user_sub, agent, tsquery, tsquery, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


# Latest-run join for task-run chats: multi-round continues share one chat, so
# the sidebar row reflects the newest run's status/task. LEFT JOIN dynamic_tasks
# for the human task name — NULL when the task row is gone (one-time tasks
# hard-delete after firing), so the client can fall back to the chat title
# instead of labeling rows with a raw task_id.
_TASK_RUN_JOIN = """
      JOIN LATERAL (
           SELECT id, task_id, status, task_type, scope, created_by
             FROM task_runs
            WHERE chat_id = c.id
            ORDER BY COALESCE(started_at, '') DESC LIMIT 1
      ) tr ON TRUE
 LEFT JOIN dynamic_tasks dt ON dt.id = tr.task_id
"""

_TASK_RUN_COLS = """
       FALSE AS unread,
       tr.id AS run_id, tr.status AS run_status, tr.task_type AS run_task_type,
       dt.name AS task_name
"""

# The run-visibility rule of /v1/tasks/runs' user-view: agent-scoped runs for
# anyone with agent access (the API layer gates that), user-scoped runs only
# for their creator. NULL scope (legacy rows) counts as agent-scoped.
_TASK_SCOPE_COND = ("(tr.scope IS DISTINCT FROM 'user' "
                    "OR tr.created_by = %s)")


def list_task_chats(agent: str, scope_user_sub: str | None = None,
                    limit: int = 50) -> list[dict]:
    """Task-run chats for an agent (the sidebar's task mode), newest first,
    each joined with its latest run. ``scope_user_sub=None`` skips the
    user-scope filter (service callers only). Tasks carry no unread state —
    every row reports ``unread=false``."""
    conditions = ["c.agent=%s", "c.id LIKE 'task-%%'"]
    params: list[Any] = [agent]
    if scope_user_sub is not None:
        conditions.append(_TASK_SCOPE_COND)
        params.append(scope_user_sub)
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT c.*, {_TASK_RUN_COLS}
                  FROM chats c {_TASK_RUN_JOIN}
                 WHERE {' AND '.join(conditions)}
              ORDER BY c.updated_at DESC LIMIT %s""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def search_task_chats(agent: str, query: str, scope_user_sub: str | None = None,
                      limit: int = 50) -> list[dict]:
    """Task-mode FTS: task-run chats of one agent, gated by the same run rules
    as ``list_task_chats``. The search row's ``user_sub`` is the chat's
    synthetic owner (``task::``/creator), so scoping joins the run instead."""
    tsquery = _sanitize_fts_query(query)
    if not tsquery:
        return []
    conditions = ["s.agent = %s", "c.id LIKE 'task-%%'",
                  "s.search_vector @@ to_tsquery('english', %s)"]
    params: list[Any] = [agent, tsquery]
    if scope_user_sub is not None:
        conditions.append(_TASK_SCOPE_COND)
        params.append(scope_user_sub)
    params.extend([tsquery, limit])
    with get_conn() as conn:
        try:
            rows = conn.execute(
                f"""SELECT c.*, {_TASK_RUN_COLS}
                      FROM chat_search s
                      JOIN chats c ON c.id = s.chat_id {_TASK_RUN_JOIN}
                     WHERE {' AND '.join(conditions)}
                  ORDER BY ts_rank(s.search_vector, to_tsquery('english', %s)) DESC
                     LIMIT %s""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


# --- Chat CRUD ---


def create_chat(chat_id: str, user_sub: str, agent: str, permission_mode: str = "default", model: str = "", execution_path: str = "", source_type: str = "chat", execution_mode: str = "", origin: str = "dashboard", work_cwd: str = "", parent_chat_id: str = "", project_id: str = "", delegate_role: str = "", title: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO chats (id, user_sub, agent, title, session_id, permission_mode, model, execution_path, source_type, execution_mode, origin, work_cwd, parent_chat_id, project_id, delegate_role, created_at, updated_at)
               VALUES (%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (chat_id, user_sub, agent, title, permission_mode, model, execution_path, source_type, execution_mode, origin, work_cwd, parent_chat_id, project_id, delegate_role, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM chats WHERE id=%s", (chat_id,)).fetchone()
        return dict(row)


def get_chat(chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chats WHERE id=%s", (chat_id,)).fetchone()
        return dict(row) if row else None


def list_chats(user_sub: str, agent: str | None = None, limit: int = 50) -> list[dict]:
    """List a chat-history owner's chats, newest first. Each row carries a
    computed ``unread`` bool: the last assistant response landed after the
    owner identity's read marker (``chat_reads`` is keyed by the same owner
    identity as this listing, so shared-only chats clear for everyone once
    any user opens them)."""
    # Task-run chats never list in chat mode — their single home is the
    # sidebar's task mode (list_task_chats), delegate workers included.
    conditions = ["c.user_sub=%s", "c.id NOT LIKE 'task-%%'"]
    params: list[Any] = [user_sub, user_sub]
    if agent:
        conditions.append("c.agent=%s")
        params.append(agent)
    where = f"WHERE {' AND '.join(conditions)}"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT c.*,
                       (c.last_response_at IS NOT NULL
                        AND (r.last_read_at IS NULL
                             OR c.last_response_at > r.last_read_at)) AS unread
                  FROM chats c
             LEFT JOIN chat_reads r ON r.chat_id = c.id AND r.user_sub = %s
                {where} ORDER BY c.updated_at DESC LIMIT %s""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_unread_finished_chats(since: str, limit: int = 30) -> list[dict]:
    """Chats whose last assistant response landed after ``since`` and has not
    been opened yet — the finished-unread backfill for the cross-agent
    "Active now" widget (a finished result must stay visible until someone
    actually opens it; without this a page reload dropped it from the panel).

    Unread is judged against the chat's own history-owner identity (real
    user_sub, or ``agent::<slug>`` for shared-only chats) — the same rule as
    ``list_chats``, so a shared chat clears for everyone once any user opens
    it. Task-run chats are excluded — tasks carry no unread state anywhere
    (notifications cover completion); the widget keeps its in-session task
    rows via the client store instead. Caller applies per-row access."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.*, TRUE AS unread
                 FROM chats c
            LEFT JOIN chat_reads r ON r.chat_id = c.id AND r.user_sub = c.user_sub
                WHERE c.last_response_at IS NOT NULL
                  AND c.last_response_at > %s
                  AND c.id NOT LIKE 'task-%%'
                  AND (r.last_read_at IS NULL OR c.last_response_at > r.last_read_at)
             ORDER BY c.last_response_at DESC LIMIT %s""",
            (since, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_chat_read(chat_id: str, owner_identity: str) -> None:
    """Upsert the read marker for a chat under its history-owner identity
    (real user_sub, or ``agent::<slug>`` for shared-only chats)."""
    if not chat_id or not owner_identity:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO chat_reads (chat_id, user_sub, last_read_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (chat_id, user_sub)
               DO UPDATE SET last_read_at = EXCLUDED.last_read_at""",
            (chat_id, owner_identity, now),
        )
        conn.commit()


def list_chats_by_parent(parent_chat_id: str, limit: int = 50) -> list[dict]:
    """Worker chats spawned by this chat via delegate(surface="chat") — any
    agent, any owner (lineage IS the authority: the parent chat drove them)."""
    if not parent_chat_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chats WHERE parent_chat_id=%s "
            "ORDER BY updated_at DESC LIMIT %s",
            (parent_chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_chats_by_project(project_id: str, limit: int = 100) -> list[dict]:
    """All chats stamped with this delegation project slug — orchestrator plus
    worker lanes, across agents/owners; the caller filters visibility per row."""
    if not project_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chats WHERE project_id=%s "
            "ORDER BY updated_at DESC LIMIT %s",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_agent_conversations(agent: str, *, source_type: str = "", exclude_sources: tuple = (), offset: int = 0, limit: int = 50) -> list[dict]:
    """Get all chats for an agent, ordered by most recent. Used by Conversations tab.

    ``exclude_sources`` drops rows by ``source_type`` (e.g. ``("chat",)`` keeps
    only phone/external conversations — dashboard chats live on the separate
    chat-history page).
    """
    sql = "SELECT * FROM chats WHERE agent=%s"
    params: list[Any] = [agent]
    if source_type:
        sql += " AND source_type=%s"
        params.append(source_type)
    for ex in exclude_sources:
        sql += " AND source_type <> %s"
        params.append(ex)
    sql += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def count_agent_conversations(agent: str, *, source_type: str = "", exclude_sources: tuple = ()) -> int:
    """Count all chats for an agent. Used by Conversations tab pagination."""
    sql = "SELECT COUNT(*) AS cnt FROM chats WHERE agent=%s"
    params: list[Any] = [agent]
    if source_type:
        sql += " AND source_type=%s"
        params.append(source_type)
    for ex in exclude_sources:
        sql += " AND source_type <> %s"
        params.append(ex)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()["cnt"]


def update_chat(chat_id: str, **fields: Any) -> bool:
    allowed = {"title", "session_id", "permission_mode", "model", "execution_path", "execution_target", "total_cost", "context_used", "context_max", "cache_read", "cache_write", "output_tokens", "last_turn_aborted", "last_abort_graceful", "codex_thread_id", "thread_goal", "pending_history_seed", "execution_mode", "title_generated", "tui_theme", "last_response_at", "parent_chat_id", "project_id", "delegate_role"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sql = f"UPDATE chats SET {', '.join(f'{k}=%s' for k in updates)} WHERE id=%s"
    values = list(updates.values()) + [chat_id]
    with get_conn() as conn:
        cur = conn.execute(sql, values)
        conn.commit()
        # Sync search index when title changes
        if "title" in fields:
            try:
                _rebuild_chat_search_row(conn, chat_id)
                conn.commit()
            except Exception:
                pass
        return cur.rowcount > 0


def claim_pending_history_seed(chat_id: str) -> str:
    """Atomically claim-and-clear the chat's pending history-seed reason.

    Returns the reason payload ('machine_removed:<name>' / 'retention') or ''
    when nothing is pending. Single UPDATE..FROM..FOR UPDATE statement so two
    concurrent turns (e.g. a server turn racing a user turn) can never both
    inject the digest — exactly one caller gets a non-empty return.
    """
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE chats c SET pending_history_seed = ''
               FROM (SELECT id, pending_history_seed FROM chats
                      WHERE id = %s FOR UPDATE) old
               WHERE c.id = old.id AND old.pending_history_seed <> ''
               RETURNING old.pending_history_seed""",
            (chat_id,),
        ).fetchone()
        conn.commit()
        return row["pending_history_seed"] if row else ""


def claim_title_generation(chat_id: str) -> bool:
    """Atomically claim the one-time LLM chat-title upgrade.

    Returns True for exactly ONE caller — the first to flip ``title_generated``
    FALSE→TRUE — and False for every subsequent call. This is the once-only
    guard for ``services/title_generator.py``: the headless pump may fire at the
    response-length threshold AND again at turn-end, the interactive tailer fires
    on debounce + close + reaper, and a later turn could fire again — all funnel
    here and only the winner generates a title. Single UPDATE..FROM..FOR UPDATE
    (mirrors ``claim_pending_history_seed``) so concurrent callers can't both win.
    """
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE chats c SET title_generated = TRUE
               FROM (SELECT id, title_generated FROM chats
                      WHERE id = %s FOR UPDATE) old
               WHERE c.id = old.id AND old.title_generated = FALSE
               RETURNING old.title_generated""",
            (chat_id,),
        ).fetchone()
        conn.commit()
        return row is not None


def get_retention_candidate_chats(cutoff_iso: str) -> list[dict]:
    """Chats whose LOCAL on-disk session may be aged out by the retention
    sweep (services/infra/retention.py). Remote-pinned chats are the satellite's
    business; direct-llm has no session files; ''-target rows (post-#11
    machine deletion) already have NULL session ids.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, user_sub, agent, session_id, codex_thread_id
                 FROM chats
                WHERE execution_target = 'local'
                  AND execution_path != 'direct-llm'
                  AND (session_id IS NOT NULL OR codex_thread_id IS NOT NULL)
                  AND updated_at < %s""",
            (cutoff_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_protected_session_refs(cutoff_iso: str) -> tuple[set[str], set[str]]:
    """Session/thread ids referenced by ANY chat fresher than the cutoff.

    Sessions are shared across chat rows (continue_session delegation runs
    reuse one session id on a fresh task chat each round) — an aged sibling
    row must never get the shared file deleted out from under the live chain.
    """
    with get_conn() as conn:
        sids = {
            r["session_id"] for r in conn.execute(
                "SELECT DISTINCT session_id FROM chats "
                "WHERE updated_at >= %s AND session_id IS NOT NULL",
                (cutoff_iso,),
            ).fetchall()
        }
        tids = {
            r["codex_thread_id"] for r in conn.execute(
                "SELECT DISTINCT codex_thread_id FROM chats "
                "WHERE updated_at >= %s AND codex_thread_id IS NOT NULL",
                (cutoff_iso,),
            ).fetchall()
        }
        return sids, tids


def get_all_session_refs() -> set[str]:
    """Every session/thread id any DB row still points at — the orphan-pass
    reference set. Includes dynamic_tasks' session refs (continue_session
    delegation + on-complete notify), which outlive their chat rows.
    """
    refs: set[str] = set()
    with get_conn() as conn:
        for sql in (
            "SELECT DISTINCT session_id AS r FROM chats WHERE session_id IS NOT NULL",
            "SELECT DISTINCT codex_thread_id AS r FROM chats WHERE codex_thread_id IS NOT NULL",
            "SELECT DISTINCT continue_session AS r FROM dynamic_tasks "
            "WHERE continue_session IS NOT NULL AND continue_session != ''",
            "SELECT DISTINCT on_complete_session_id AS r FROM dynamic_tasks "
            "WHERE on_complete_session_id IS NOT NULL AND on_complete_session_id != ''",
        ):
            refs.update(r["r"] for r in conn.execute(sql).fetchall())
    return refs


def flag_chats_for_retention(chat_ids: list[str]) -> int:
    """Transition aged-out chats to the reseed flow.

    Mirrors remote_store.delete_remote_machine's chat transition. Deliberately
    NOT update_chat: that helper bumps updated_at, which would float every
    aged chat to the top of the chat list the moment the sweep runs.
    """
    if not chat_ids:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE chats
                  SET session_id = NULL, codex_thread_id = NULL,
                      pending_history_seed = 'retention',
                      last_turn_aborted = FALSE, last_abort_graceful = FALSE,
                      context_used = 0
                WHERE id = ANY(%s)""",
            (chat_ids,),
        )
        conn.commit()
        return cur.rowcount


def get_chat_by_session(session_id: str) -> dict | None:
    """Reverse lookup: find the most recent chat for a session_id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chats WHERE session_id=%s ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_chat(chat_id: str) -> bool:
    # Collect proxy-cache media files to unlink before the chat row (and its
    # media_tokens, via CASCADE) disappear. In-place agent-tree files are NOT
    # cache_owned and are left untouched.
    cache_paths = get_cache_owned_media_paths_for_chat(chat_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM chats WHERE id=%s", (chat_id,),
        ).fetchone()
        project_id = (row["project_id"] or "") if row else ""
        cur = conn.execute("DELETE FROM chats WHERE id=%s", (chat_id,))
        conn.execute("DELETE FROM chat_search WHERE chat_id=%s", (chat_id,))
        conn.execute("DELETE FROM chat_reads WHERE chat_id=%s", (chat_id,))
        # Scoped Dock pins die with their scope: the chat's own pin always;
        # the project's pin when this was the LAST chat of that project. The
        # workspace .html files stay (user artifacts), as with every unpin.
        conn.execute(
            "DELETE FROM pinned_apps WHERE scope_chat_id=%s", (chat_id,),
        )
        if project_id:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM chats WHERE project_id=%s",
                (project_id,),
            ).fetchone()["c"]
            if not remaining:
                conn.execute(
                    "DELETE FROM pinned_apps WHERE scope_project_id=%s",
                    (project_id,),
                )
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        for p in cache_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
    return deleted


def add_chat_message(chat_id: str, role: str, content: str = "",
                     event_type: str = "", event_data: str = "",
                     author_sub: str = "") -> int:
    """Persist one chat message. ``author_sub`` is the REAL sender's user_sub —
    set for Shared-only chats (where the chat row owner is a synthetic
    ``agent::{slug}``) so each message keeps its true attribution. Empty for
    single-owner chats (owner == sender) and assistant/event rows."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (chat_id, role, content, event_type, event_data, author_sub, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (chat_id, role, content, event_type, event_data, author_sub, now),
        )
        row_id = cur.fetchone()["id"]
        conn.commit()
        # Also touch the chat's updated_at
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (now, chat_id))
        conn.commit()
        # Task-run chats are created untitled by the scheduler; stamp the
        # deterministic title from the first prompt here — the chat layer's
        # send-time titling never runs for scheduler-driven chats. First
        # message only (non-empty titles are never overwritten).
        if role == "user" and content and chat_id.startswith("task-"):
            try:
                title_row = conn.execute(
                    "SELECT title FROM chats WHERE id=%s", (chat_id,),
                ).fetchone()
                if title_row is not None and not (title_row["title"] or "").strip():
                    from services.title_generator import deterministic_title
                    conn.execute(
                        "UPDATE chats SET title=%s WHERE id=%s",
                        (deterministic_title(content), chat_id),
                    )
                    conn.commit()
            except Exception:
                pass  # title stamp failure should not break message insert
        # Sync search index for user/assistant text messages
        if role in ("user", "assistant") and content:
            try:
                _rebuild_chat_search_row(conn, chat_id)
                conn.commit()
            except Exception:
                pass  # search sync failure should not break message insert
        return row_id


def get_chat_messages(chat_id: str, limit: int = 500, *, before_id: int | None = None) -> list[dict]:
    """Newest ``limit`` rows in chronological order.

    With ``before_id`` set, the newest ``limit`` rows OLDER than that id — the
    scroll-back cursor for the lazy-loading chat view (``id`` is the monotonic
    SERIAL PK). DESC + reverse: a windowed view must lose its OLDEST rows, never
    the newest — the old ASC LIMIT silently hid every turn past the cap, surfacing
    as "the chat lost its recent messages after a reload".
    """
    with get_conn() as conn:
        if before_id is None:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE chat_id=%s ORDER BY id DESC LIMIT %s",
                (chat_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE chat_id=%s AND id<%s ORDER BY id DESC LIMIT %s",
                (chat_id, before_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_chat_messages_page(
    chat_id: str, limit: int, before_id: int | None = None,
) -> tuple[list[dict], bool]:
    """A paged window for the lazy-loading chat view: the newest ``limit`` rows
    (older than ``before_id`` when scrolling back) plus ``has_more`` — whether
    still-older rows exist. Fetches ``limit + 1`` and drops the single oldest
    extra (rows are chronological), so ``has_more`` costs no separate COUNT."""
    rows = get_chat_messages(chat_id, limit + 1, before_id=before_id)
    has_more = len(rows) > limit
    if has_more:
        rows = rows[1:]  # drop the oldest extra; keep the newest `limit`
    return rows, has_more


def get_last_chat_message_id(chat_id: str) -> int:
    """Highest message id in a chat (0 if empty). The pump records this at a turn
    boundary as an id-based cutoff so a *windowed* resume can still withhold the
    in-flight tail (a row COUNT can't index a 50-row window)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM chat_messages WHERE chat_id=%s", (chat_id,),
        ).fetchone()
    return int(row["m"]) if row and row["m"] is not None else 0


def get_last_todo_snapshot(chat_id: str) -> list[dict]:
    """The most recent TodoWrite checklist snapshot in a chat, for the idle-reload
    panel restore — searched over FULL history so it is independent of the loaded
    message window. Covers native TodoWrite tool rows AND the synthesized snapshots
    from the Codex ``update_plan`` / CLI ``TaskCreate``/``TaskUpdate`` paths (all
    persisted as a tool block named TodoWrite).

    ``event_data`` is TEXT and ``''`` on most rows, so a blind ``::jsonb`` cast can
    throw (Postgres doesn't guarantee predicate short-circuit) — prefilter with a
    LIKE that can't trip a cast, then parse in Python.
    """
    import json
    with get_conn() as conn:
        row = conn.execute(
            "SELECT event_data FROM chat_messages "
            "WHERE chat_id=%s AND event_type='tool' AND event_data LIKE %s "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, '%"name": "TodoWrite"%'),
        ).fetchone()
    if not row or not row["event_data"]:
        return []
    try:
        todos = json.loads(row["event_data"]).get("tool_input", {}).get("todos", [])
        return todos if isinstance(todos, list) else []
    except (ValueError, TypeError, AttributeError):
        return []


# --- Media tokens (audio/video capability URLs; see schema.media_tokens) ---


def create_media_token(
    token: str,
    abs_path: str,
    *,
    mime: str = "",
    media_kind: str = "",
    chat_id: str | None = None,
    session_id: str = "",
    machine_id: str | None = None,
    cache_owned: bool = False,
    expires_at: str = "",
    origin_path: str = "",
    owner_sub: str = "",
    agent: str = "",
) -> None:
    """Persist a media capability token. `expires_at` empty = no expiry (the
    row lives until its chat is deleted via CASCADE); set it for workspace
    (chat_id=None) tokens so the TTL sweep can reap them. `origin_path` is the
    satellite-host abs path (Desktop/Downloads) so `serve_media` can re-pull it
    from the laptop on replay instead of retaining a copy. `owner_sub`/`agent`
    are the serve-time access stamps (see `api.media.access`)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO media_tokens
               (token, abs_path, mime, media_kind, chat_id, session_id,
                machine_id, cache_owned, created_at, expires_at, origin_path,
                owner_sub, agent)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (token, abs_path, mime, media_kind, chat_id, session_id,
             machine_id, 1 if cache_owned else 0, now, expires_at, origin_path,
             owner_sub, agent),
        )
        conn.commit()


def update_media_token_path(token: str, abs_path: str, *, mime: str | None = None) -> None:
    """Repoint a token at a freshly re-pulled / transcoded file (satellite-host
    replay). Updates abs_path, and mime when given."""
    with get_conn() as conn:
        if mime is not None:
            conn.execute(
                "UPDATE media_tokens SET abs_path=%s, mime=%s WHERE token=%s",
                (abs_path, mime, token),
            )
        else:
            conn.execute(
                "UPDATE media_tokens SET abs_path=%s WHERE token=%s",
                (abs_path, token),
            )
        conn.commit()


def get_media_token(token: str) -> dict | None:
    """Return the token row, or None if missing/expired. Purges on expiry."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM media_tokens WHERE token=%s", (token,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        exp = d.get("expires_at") or ""
        if exp:
            try:
                if datetime.now(timezone.utc) > datetime.fromisoformat(exp):
                    conn.execute("DELETE FROM media_tokens WHERE token=%s", (token,))
                    conn.commit()
                    return None
            except ValueError:
                pass  # malformed expiry → treat as non-expiring
        return d


def get_cache_owned_media_paths_for_chat(chat_id: str) -> list[str]:
    """Cache-owned (proxy-side copy) media file paths for a chat — safe to
    unlink when the chat is deleted. Agent-tree files are not cache_owned."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT abs_path FROM media_tokens WHERE chat_id=%s AND cache_owned=1",
            (chat_id,),
        ).fetchall()
        return [r["abs_path"] for r in rows]


def sweep_expired_media_tokens() -> int:
    """Delete expired media_tokens, unlinking any cache-owned files. Returns
    the number of rows removed. Best-effort; safe to call periodically."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT abs_path, cache_owned FROM media_tokens "
            "WHERE expires_at <> '' AND expires_at < %s",
            (now_iso,),
        ).fetchall()
        for r in rows:
            if r["cache_owned"]:
                try:
                    os.unlink(r["abs_path"])
                except OSError:
                    pass
        conn.execute(
            "DELETE FROM media_tokens WHERE expires_at <> '' AND expires_at < %s",
            (now_iso,),
        )
        conn.commit()
        return len(rows)


def dismiss_document_previews(
    chat_id: str, file_id: str,
    snapshot_id: str | None = None, db_message_id: int | None = None,
) -> tuple[int, list[str]]:
    """Dismiss document_preview events with the given file_id in a chat.

    Instance scoping: ``snapshot_id`` (or ``db_message_id`` for pre-snapshot
    rows) narrows the dismissal to ONE preview instance — the dashboard's
    "previous version" block dismisses only itself. With neither, every
    instance for the file is dismissed (the live block's close).

    Returns ``(rows updated, snapshot ids of those rows)`` so the caller can
    delete exactly the newly-unreferenced snapshot files.
    """
    import json as _json
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, event_data FROM chat_messages "
            "WHERE chat_id=%s AND event_type='document_preview'",
            (chat_id,),
        ).fetchall()
        count = 0
        freed_snapshots: list[str] = []
        for row in rows:
            data = _json.loads(row["event_data"] or "{}") if row["event_data"] else {}
            if data.get("file_id") != file_id or data.get("dismissed"):
                continue
            if snapshot_id is not None and data.get("snapshot_id") != snapshot_id:
                continue
            if db_message_id is not None and row["id"] != db_message_id:
                continue
            data["dismissed"] = True
            conn.execute(
                "UPDATE chat_messages SET event_data=%s WHERE id=%s",
                (_json.dumps(data), row["id"]),
            )
            count += 1
            if data.get("snapshot_id"):
                freed_snapshots.append(data["snapshot_id"])
        if count:
            conn.commit()
        return count, freed_snapshots


def get_preview_event_by_snapshot(chat_id: str, snapshot_id: str) -> dict | None:
    """The non-dismissed document_preview event data referencing a snapshot,
    or None. Serving a snapshot requires a live reference — a dismissed row's
    snapshot is deleted and must not be re-mintable."""
    import json as _json
    if not snapshot_id:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_data FROM chat_messages "
            "WHERE chat_id=%s AND event_type='document_preview'",
            (chat_id,),
        ).fetchall()
    for row in rows:
        try:
            data = _json.loads(row["event_data"] or "{}")
        except ValueError:
            continue
        if data.get("snapshot_id") == snapshot_id and not data.get("dismissed"):
            return data
    return None


def get_referenced_preview_snapshot_ids(chat_id: str) -> set[str]:
    """Snapshot ids referenced by NON-dismissed document_preview events in a
    chat — the keep-set for the reference-driven snapshot GC."""
    import json as _json
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_data FROM chat_messages "
            "WHERE chat_id=%s AND event_type='document_preview'",
            (chat_id,),
        ).fetchall()
    out: set[str] = set()
    for row in rows:
        try:
            data = _json.loads(row["event_data"] or "{}")
        except ValueError:
            continue
        if not data.get("dismissed") and data.get("snapshot_id"):
            out.add(data["snapshot_id"])
    return out


def get_chat_message_count(chat_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM chat_messages WHERE chat_id=%s",
            (chat_id,),
        ).fetchone()
        return row["cnt"] if row else 0


# --- Chat plans ---


def add_chat_plan(chat_id: str, filename: str, content: str,
                  status: str = "pending") -> int:
    """Insert or update a plan. If a plan with the same filename exists for this
    chat, update its content and status instead of creating a duplicate."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM chat_plans WHERE chat_id=%s AND filename=%s",
            (chat_id, filename),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE chat_plans SET content=%s, status=%s, created_at=%s WHERE id=%s",
                (content, status, now, existing["id"]),
            )
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO chat_plans (chat_id, filename, content, status, created_at)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (chat_id, filename, content, status, now),
        )
        row_id = cur.fetchone()["id"]
        conn.commit()
        return row_id


def update_chat_plan_status(chat_id: str, filename: str, status: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_plans SET status=%s WHERE chat_id=%s AND filename=%s",
            (status, chat_id, filename),
        )
        conn.commit()
        return cur.rowcount > 0


def get_chat_plans(chat_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_plans WHERE chat_id=%s ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def touch_chat(chat_id: str) -> None:
    """Bump ``updated_at`` only — sidebar recency for a chat mid-turn.

    ``update_chat`` refuses empty field sets (and every allowed field carries
    semantics we must not disturb mid-turn), so the pump's throttled activity
    touch gets its own single-column write.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE chats SET updated_at=%s WHERE id=%s",
            (datetime.now(timezone.utc).isoformat(), chat_id),
        )
        conn.commit()
