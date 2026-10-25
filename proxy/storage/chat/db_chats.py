"""Chat, message, media-token, and plan queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import psycopg

from core.permission_modes import ASK_MODE, chat_row
from core.session import session_kind
from storage.pg import get_conn

logger = logging.getLogger("db_chats")

# The persisted role words the store reads back: a person's turn, and the
# two roles whose text the search row indexes.
_ROLE_USER = "user"
_SEARCHED_ROLES = ("user", "assistant")


# --- Chat Search (tsvector) ---


# The most text one chat's search row indexes, its newest. Postgres refuses
# a tsvector past 1 MB (counted in unique lexemes, not characters), and a
# refused rebuild would leave the row stale for good, its new words never
# found: two million characters of prose stay far below it (the internal
# install's largest chat, 1.2 M characters, makes a 140 kB vector), and text
# dense in unique tokens (pasted paths, hashes) that still passes it is
# indexed again on a halved window, down to the floor.
_SEARCH_CONTENT_MAX = 2_000_000
_SEARCH_CONTENT_FLOOR = 20_000


def _newest_text(contents: list[str], window: int) -> str:
    """The newest ``window`` characters of a chat's texts (``contents``
    newest first), in reading order; a text bigger than the window keeps
    its tail."""
    newest: list[str] = []
    size = 0
    for text in contents:
        size += len(text) + 1
        if size > window:
            if not newest:
                newest.append(text[-window:])
            break
        newest.append(text)
    return "\n".join(reversed(newest))


def _rebuild_chat_search_row(conn, chat_id: str) -> None:
    """Rebuild the search row for a single chat from its messages + title:
    the newest ``_SEARCH_CONTENT_MAX`` characters of its text, in order (a
    vector Postgres refuses is built again from half the window).

    Must be called with an open connection (caller manages commit).
    """
    chat = conn.execute(
        "SELECT user_sub, agent, title FROM chats WHERE id=%s", (chat_id,)
    ).fetchone()
    if not chat:
        return
    contents = [m["content"] for m in conn.execute(
        "SELECT content FROM chat_messages WHERE chat_id=%s AND role IN ('user','assistant') "
        "AND content != '' ORDER BY id DESC",
        (chat_id,),
    ).fetchall()]
    window = _SEARCH_CONTENT_MAX
    while True:
        try:
            # A savepoint: a refused vector costs only this try.
            with conn.transaction():
                conn.execute(
                    """INSERT INTO chat_search (chat_id, user_sub, agent, title, content)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (chat_id) DO UPDATE SET
                           title = EXCLUDED.title, content = EXCLUDED.content""",
                    (chat_id, chat["user_sub"], chat["agent"], chat["title"] or "",
                     _newest_text(contents, window)),
                )
            return
        except psycopg.errors.ProgramLimitExceeded:
            if window <= _SEARCH_CONTENT_FLOOR:
                raise
            window //= 2


# The longest query text the searches parse: a longer paste is cut, not
# refused.
SEARCH_QUERY_MAX = 200


def _tsquery_lexeme(lexeme: str) -> str:
    """One lexeme as a quoted tsquery operand with prefix matching: inside the
    quotes a quote is doubled and a backslash escaped, so any lexeme the
    parser yields (a path, an address, an email) stays one operand."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "':*"


def _prefix_tsquery(conn, query: str) -> str:
    """The query as the index reads text: each word parsed by the same parser
    and dictionary as ``chat_search.search_vector`` (english), every lexeme a
    prefix, every word required. ``config.env``, ``1.8.0`` or ``chats.py``
    stay the one token the index holds; a word the parser splits
    (``dashboard_warmup``) also matches as the start of one token
    (``dashboard_warmup.py``); a stop word drops out; a query with no lexeme
    at all answers "" (no rows)."""
    text = (query or "").strip()
    if len(text) > SEARCH_QUERY_MAX:
        # Cut at a word boundary: a word cut short would stem to something
        # that is no prefix of the indexed word.
        cut = text[:SEARCH_QUERY_MAX]
        text = cut if text[SEARCH_QUERY_MAX].isspace() else cut.rsplit(None, 1)[0] if " " in cut.strip() else cut
    words = text.split()
    if not words:
        return ""
    rows = conn.execute(
        """SELECT w.i, l.lexeme
             FROM unnest(%s::text[]) WITH ORDINALITY AS w(word, i),
                  LATERAL ts_debug('english', w.word) d,
                  LATERAL unnest(d.lexemes) AS l(lexeme)""",
        (words,),
    ).fetchall()
    by_word: dict[int, list[str]] = {}
    for r in rows:
        if r["lexeme"]:
            by_word.setdefault(r["i"], []).append(r["lexeme"])
    operands: list[str] = []
    for i, lexemes in sorted(by_word.items()):
        parts = " & ".join(_tsquery_lexeme(lx) for lx in dict.fromkeys(lexemes))
        whole = words[i - 1].lower().strip(".,;:!?()[]{}<>\"'`")
        if len(lexemes) > 1 and whole and whole not in lexemes:
            parts = f"({_tsquery_lexeme(whole)} | ({parts}))"
        operands.append(parts)
    return " & ".join(operands)


# The task kind's ``source_type`` spellings — what chat mode excludes and
# task mode selects (core-seams phase 4: the scheduler writes ``task`` on
# the rows it mints). The ``LIKE 'task-%%'`` halves beside them keep the
# rows minted BEFORE the write (``source_type='chat'`` with a ``task-`` id)
# in the right list; a backfill that stamps those rows retires the halves.
_TASK_SOURCE_TYPES = session_kind.source_types(session_kind.TASK)
_NOT_TASK_ROW = "NOT (c.source_type = ANY(%s)) AND c.id NOT LIKE 'task-%%'"
_TASK_ROW = "(c.source_type = ANY(%s) OR c.id LIKE 'task-%%')"


def _search_failed(kind: str, query: str, exc: Exception) -> list[dict]:
    # The type alone: a tsquery error's message would carry the query text.
    logger.warning("chat search (%s) failed for a %d-character query: %s", kind,
                   len(query or ""), type(exc).__name__)
    return []


def search_chats(user_sub: str, agent: str, query: str, limit: int = 50) -> list[dict]:
    """Chats whose title or text holds every word of ``query`` (each as a
    prefix), newest first: the sidebar groups the answer by day, and a
    recent chat that names a word once must not lose its place to an old
    one that names it often.

    Chat mode only — task-run chats are excluded (a user-scoped task chat
    carries the creator's sub, so the owner filter alone would leak it here);
    task mode searches through ``search_task_chats``."""
    with get_conn() as conn:
        try:
            tsquery = _prefix_tsquery(conn, query)
            if not tsquery:
                return []
            rows = conn.execute(
                f"""SELECT c.* FROM chat_search s
                   JOIN chats c ON c.id = s.chat_id
                   WHERE s.user_sub = %s AND s.agent = %s
                   AND {_NOT_TASK_ROW}
                   AND s.search_vector @@ %s::tsquery
                   ORDER BY c.updated_at DESC
                   LIMIT %s""",
                (user_sub, agent, _TASK_SOURCE_TYPES, tsquery, limit),
            ).fetchall()
            return [chat_row(r) for r in rows]
        except psycopg.Error as e:
            return _search_failed("chats", query, e)


# Latest-run join for task-run chats: multi-round continues share one chat, so
# the sidebar row reflects the newest run's status/task. LEFT JOIN dynamic_tasks
# for the human task name — NULL when the task row is gone (one-time tasks
# hard-delete after firing), so the client can fall back to the chat title
# instead of labeling rows with a raw task_id.
# A failed search-row rebuild (the 1 MB tsvector cap, a lock timeout) leaves
# the chat's search row stale; the WARNING is rate-limited, with a count.
_REBUILD_WARN_EVERY_S = 60.0
_rebuild_warned_at = 0.0
_rebuild_warn_suppressed = 0
_rebuild_warn_lock = threading.Lock()


def _warn_rebuild_failed(chat_id: str, exc: BaseException) -> None:
    global _rebuild_warned_at, _rebuild_warn_suppressed
    with _rebuild_warn_lock:
        now = time.monotonic()
        if now - _rebuild_warned_at < _REBUILD_WARN_EVERY_S:
            _rebuild_warn_suppressed += 1
            return
        more, _rebuild_warn_suppressed = _rebuild_warn_suppressed, 0
        _rebuild_warned_at = now
    logger.warning(
        "chat_search rebuild failed for %s: %s%s", chat_id, exc,
        f" ({more} more since the last warning)" if more else "",
    )


def rebuild_chat_search(chat_id: str) -> None:
    """Rebuild one chat's search row in its own transaction (a caller that
    wrote rows with ``sync_search=False`` calls this once per batch). A
    failure, the connection's included, is logged and never raised: the rows
    it indexes have already landed."""
    try:
        with get_conn() as conn:
            _rebuild_chat_search_row(conn, chat_id)
            conn.commit()
    except psycopg.Error as e:
        _warn_rebuild_failed(chat_id, e)


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
       tr.task_id AS task_id, tr.scope AS run_scope,
       tr.created_by AS run_created_by,
       dt.name AS task_name, dt.created_by AS task_created_by,
       dt.scope AS task_scope
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
    conditions = ["c.agent=%s", _TASK_ROW]
    params: list[Any] = [agent, _TASK_SOURCE_TYPES]
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
        return [chat_row(r) for r in rows]


def search_task_chats(agent: str, query: str, scope_user_sub: str | None = None,
                      limit: int = 50) -> list[dict]:
    """Task-mode FTS: task-run chats of one agent, gated by the same run rules
    as ``list_task_chats``, newest first like the chat mode. The search row's
    ``user_sub`` is the chat's synthetic owner (``task::``/creator), so
    scoping joins the run instead."""
    with get_conn() as conn:
        try:
            tsquery = _prefix_tsquery(conn, query)
            if not tsquery:
                return []
            conditions = ["s.agent = %s", _TASK_ROW, "s.search_vector @@ %s::tsquery"]
            params: list[Any] = [agent, _TASK_SOURCE_TYPES, tsquery]
            if scope_user_sub is not None:
                conditions.append(_TASK_SCOPE_COND)
                params.append(scope_user_sub)
            params.append(limit)
            rows = conn.execute(
                f"""SELECT c.*, {_TASK_RUN_COLS}
                      FROM chat_search s
                      JOIN chats c ON c.id = s.chat_id {_TASK_RUN_JOIN}
                     WHERE {' AND '.join(conditions)}
                  ORDER BY c.updated_at DESC
                     LIMIT %s""",
                params,
            ).fetchall()
            return [chat_row(r) for r in rows]
        except psycopg.Error as e:
            return _search_failed("tasks", query, e)


# --- Chat CRUD ---


def create_chat(chat_id: str, user_sub: str, agent: str, permission_mode: str = ASK_MODE, model: str = "", execution_path: str = "", source_type: str = "chat", execution_mode: str = "", origin: str = "dashboard", work_cwd: str = "", parent_chat_id: str = "", project_id: str = "", delegate_role: str = "", title: str = "", checks: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO chats (id, user_sub, agent, title, session_id, permission_mode, model, execution_path, source_type, execution_mode, origin, work_cwd, parent_chat_id, project_id, delegate_role, checks, created_at, updated_at)
               VALUES (%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (chat_id, user_sub, agent, title, permission_mode, model, execution_path, source_type, execution_mode, origin, work_cwd, parent_chat_id, project_id, delegate_role, checks or "", now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM chats WHERE id=%s", (chat_id,)).fetchone()
        return chat_row(row)


def get_chat(chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chats WHERE id=%s", (chat_id,)).fetchone()
        return chat_row(row) if row else None


def get_chats_by_ids(chat_ids: list[str]) -> dict[str, dict]:
    """``chat_id → row`` for the ids that exist, one query. The Active-now
    seed reads every live id this way instead of one ``get_chat`` per id
    (``api/agents/chats.py`` ``_active_rows``). An empty list answers ``{}``
    with no query."""
    ids = list(dict.fromkeys(c for c in chat_ids if c))
    if not ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chats WHERE id = ANY(%s)", (ids,),
        ).fetchall()
        return {r["id"]: chat_row(r) for r in rows}


def list_chats(user_sub: str, agent: str | None = None, limit: int = 50) -> list[dict]:
    """List a chat-history owner's chats, newest first. Each row carries a
    computed ``unread`` bool: the last assistant response landed after the
    owner identity's read marker (``chat_reads`` is keyed by the same owner
    identity as this listing, so shared-only chats clear for everyone once
    any user opens them)."""
    # Task-run chats never list in chat mode — their single home is the
    # sidebar's task mode (list_task_chats), delegate workers included.
    conditions = ["c.user_sub=%s", _NOT_TASK_ROW]
    params: list[Any] = [user_sub, user_sub, _TASK_SOURCE_TYPES]
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
        return [chat_row(r) for r in rows]


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
            f"""SELECT c.*, TRUE AS unread
                 FROM chats c
            LEFT JOIN chat_reads r ON r.chat_id = c.id AND r.user_sub = c.user_sub
                WHERE c.last_response_at IS NOT NULL
                  AND c.last_response_at > %s
                  AND {_NOT_TASK_ROW}
                  AND (r.last_read_at IS NULL OR c.last_response_at > r.last_read_at)
             ORDER BY c.last_response_at DESC LIMIT %s""",
            (since, _TASK_SOURCE_TYPES, limit),
        ).fetchall()
        return [chat_row(r) for r in rows]


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
        return [chat_row(r) for r in rows]


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
        return [chat_row(r) for r in rows]


def get_agent_conversations(agent: str, *, source_type: str = "", source_types=(), offset: int = 0, limit: int = 50) -> list[dict]:
    """Get all chats for an agent, ordered by most recent. Used by Conversations tab.

    ``source_types`` is the resolved list of the kinds the tab lists (the
    externally driven ones — ``session_kind.EXTERNAL_DRIVEN_SOURCE_TYPES``;
    dashboard and task chats live in the sidebar); ``source_type`` narrows to
    one of them.
    """
    sql = "SELECT * FROM chats WHERE agent=%s"
    params: list[Any] = [agent]
    if source_type:
        sql += " AND source_type=%s"
        params.append(source_type)
    if source_types:
        sql += " AND source_type = ANY(%s)"
        params.append(list(source_types))
    sql += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [chat_row(r) for r in rows]


def count_agent_conversations(agent: str, *, source_type: str = "", source_types=()) -> int:
    """Count all chats for an agent. Used by Conversations tab pagination."""
    sql = "SELECT COUNT(*) AS cnt FROM chats WHERE agent=%s"
    params: list[Any] = [agent]
    if source_type:
        sql += " AND source_type=%s"
        params.append(source_type)
    if source_types:
        sql += " AND source_type = ANY(%s)"
        params.append(list(source_types))
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()["cnt"]


def update_chat(chat_id: str, **fields: Any) -> bool:
    allowed = {"title", "session_id", "permission_mode", "model", "execution_path", "execution_target", "total_cost", "context_used", "context_max", "cache_read", "cache_write", "output_tokens", "last_turn_aborted", "last_abort_graceful", "codex_thread_id", "thread_goal", "pending_history_seed", "execution_mode", "title_generated", "tui_theme", "last_response_at", "parent_chat_id", "project_id", "delegate_role", "checks", "engine_cost_total", "engine_cost_session_id"}
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
            except psycopg.Error:
                # A stale FTS row keeps matching the old title until the next
                # rebuild — recoverable, but worth a trace instead of silence.
                logger.warning("chat_search rebuild failed for %s", chat_id,
                               exc_info=True)
        return cur.rowcount > 0


def rebind_chat_for_engine_switch(
    chat_id: str,
    new_path: str,
    new_model: str,
    expected_old_path: str,
    expected_session_id: str | None,
    pending_seed: str,
) -> bool:
    """Flip ONE chat to a different execution engine — the cross-engine
    resume rebind (WS ``switch_engine``).

    Compare-and-swap: the WHERE clause re-asserts the exact (execution_path,
    session_id) pair the handler's gates observed, so a concurrent warmup /
    dead-session adopt that re-bound the row between gate-check and commit
    makes this a clean no-op (rowcount 0 → the op is denied, never half
    applied). Shape mirrors ``remote_store.rebind_chat_to_target`` (the
    move's reseed rebind) minus the new pin — an engine switch does NOT move
    machines — plus the engine/model flip. ``pending_seed`` is ``'engine_switch:<old_path>'`` for
    CLI targets and ``''`` for direct-llm (which never consumes seeds and
    rebuilds full DB history every turn)."""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE chats
                  SET execution_path = %s, model = %s, session_id = NULL,
                      codex_thread_id = NULL, last_turn_aborted = FALSE,
                      last_abort_graceful = FALSE, context_used = 0,
                      pending_history_seed = %s, updated_at = %s
                WHERE id = %s AND execution_path = %s
                  AND session_id IS NOT DISTINCT FROM %s""",
            (new_path, new_model, pending_seed,
             datetime.now(timezone.utc).isoformat(), chat_id,
             expected_old_path, expected_session_id),
        )
        conn.commit()
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


# A record that waits for the chat's next typed message (a concluded
# meeting's note): it never runs as a turn of its own, so the redelivery
# sweeps and a terminal's start leave it stored and only a typed turn takes
# it. A record without a kind is a wake the sweeps deliver.
WAKE_NOTE = "note"


def _is_note(record: dict) -> bool:
    return record.get("kind") == WAKE_NOTE


def wake_records(raw: str) -> list[dict]:
    """The wake records of a ``pending_delegate_wake`` value, oldest first:
    ``{"prompt", "person", "role", "by"}`` each, plus ``"kind"`` on a note
    (``WAKE_NOTE``). A plain string (the form 1.7.0 stored) is a wake with no
    person; anything else is dropped."""
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if isinstance(item, str):
            item = {"prompt": item}
        if not isinstance(item, dict):
            continue
        prompt = item.get("prompt")
        if isinstance(prompt, str) and prompt:
            record = {"prompt": prompt, "person": str(item.get("person") or ""),
                      "role": str(item.get("role") or ""), "by": str(item.get("by") or "")}
            if item.get("kind") == WAKE_NOTE:
                record["kind"] = WAKE_NOTE
            out.append(record)
    return out


def append_pending_delegate_wake(chat_id: str, wake_prompt: str, *,
                                 person: str = "", role: str = "", by: str = "",
                                 note: bool = False) -> bool:
    """Append an undeliverable wake to the chat's durable replay store
    (``chats.pending_delegate_wake``: a JSON array of ``{"prompt", "person",
    "role", "by"}``: the person and role the delivery ran as, "" for no
    person, and for a wake that runs as nobody at its creator's role, that
    creator, so a replay reads their role again). ``note``: the record waits
    for the next typed message instead (``WAKE_NOTE``).
    Written when every delivery-ladder rung failed; claimed and injected at
    the chat's next warmup/turn, or redelivered by the sweep as its person.
    Row-locked read-modify-write so two concurrent failed deliveries can't
    drop each other's wake.
    """
    if not wake_prompt:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pending_delegate_wake FROM chats WHERE id = %s FOR UPDATE",
            (chat_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        wakes = wake_records(row["pending_delegate_wake"])
        record = {"prompt": wake_prompt, "person": person or "", "role": role or "", "by": by or ""}
        if note:
            record["kind"] = WAKE_NOTE
        wakes.append(record)
        conn.execute(
            "UPDATE chats SET pending_delegate_wake = %s WHERE id = %s",
            (json.dumps(wakes), chat_id),
        )
        conn.commit()
        return True


def claim_pending_wake_records(chat_id: str, *, notes: bool = False,
                               wakes: bool = True) -> list[dict]:
    """Atomically claim-and-clear the chat's pending wakes (``wakes``) and
    notes (``notes``), each with the person and role it was stored for
    (oldest first; ``[]`` when none); the records of the kind not asked for
    stay stored, in order. One locked read-modify-write: exactly one of two
    racing claimers gets each record.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pending_delegate_wake FROM chats WHERE id = %s FOR UPDATE",
            (chat_id,),
        ).fetchone()
        if row is None or not row["pending_delegate_wake"]:
            conn.commit()
            return []
        records = wake_records(row["pending_delegate_wake"])
        taken = [r for r in records if (notes if _is_note(r) else wakes)]
        kept = [r for r in records if not (notes if _is_note(r) else wakes)]
        conn.execute("UPDATE chats SET pending_delegate_wake = %s WHERE id = %s",
                     (json.dumps(kept) if kept else "", chat_id))
        conn.commit()
    return taken


def claim_pending_delegate_wake(chat_id: str, *, notes: bool = False) -> list[str]:
    """The prompts of :func:`claim_pending_wake_records`, for a claimer whose
    own turn carries them (it runs as its own person): the wakes, and with
    ``notes`` (a typed turn, or a drop) the notes too."""
    return [w["prompt"] for w in claim_pending_wake_records(chat_id, notes=notes)]


def claim_pending_notes(chat_id: str) -> list[str]:
    """The notes waiting for this chat's next typed message, claimed; its
    wakes stay stored for their own delivery."""
    return [w["prompt"] for w in claim_pending_wake_records(chat_id, notes=True, wakes=False)]


def _wake_of(wake: dict, person: str) -> bool:
    return person in (wake["person"], wake["by"])


def pending_wake_agents_of(person: str) -> list[str]:
    """The agents whose chats hold a stored wake ``person`` runs as or
    scheduled."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, pending_delegate_wake FROM chats WHERE pending_delegate_wake <> ''"
        ).fetchall()
    return sorted({r["agent"] for r in rows
                   if any(_wake_of(w, person) for w in wake_records(r["pending_delegate_wake"]))})


def drop_pending_wakes_of(person: str, agents: list[str] | None) -> int:
    """Remove the stored wakes ``person`` runs as or scheduled from the chats
    of ``agents`` (every agent for None), the other wakes kept in order.
    Returns how many went."""
    q = "SELECT id, pending_delegate_wake FROM chats WHERE pending_delegate_wake <> ''"
    params: tuple = ()
    if agents is not None:
        q += " AND agent = ANY(%s)"
        params = (list(agents),)
    dropped = 0
    with get_conn() as conn:
        for r in conn.execute(q + " FOR UPDATE", params).fetchall():
            wakes = wake_records(r["pending_delegate_wake"])
            kept = [w for w in wakes if not _wake_of(w, person)]
            if len(kept) == len(wakes):
                continue
            dropped += len(wakes) - len(kept)
            conn.execute("UPDATE chats SET pending_delegate_wake = %s WHERE id = %s",
                         (json.dumps(kept) if kept else "", r["id"]))
        conn.commit()
    return dropped


def list_chats_with_pending_wakes(execution_target: str | None = None,
                                  session_ids: list[str] | tuple[str, ...] = ()) -> list[dict]:
    """Chats holding undelivered delegate wakes (see
    ``append_pending_delegate_wake``). ``execution_target`` scopes the list to
    one machine's chats (satellite-reconnect redelivery): pinned to it, or
    running one of ``session_ids`` (the sessions the platform holds there; a
    chat with an empty target). None = all — the startup sweep. Claiming
    stays per-chat and atomic, so this list is a candidate set, not a lock."""
    q = ("SELECT id, agent, user_sub, session_id, execution_target, pending_delegate_wake "
         "FROM chats WHERE pending_delegate_wake <> ''")
    params: tuple = ()
    if execution_target:
        q += " AND (execution_target = %s OR session_id = ANY(%s))"
        params = (execution_target, list(session_ids))
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    # A chat holding notes alone has nothing for a sweep to deliver.
    out: list[dict] = []
    for r in rows:
        row = chat_row(r)
        if any(not _is_note(w) for w in wake_records(row.pop("pending_delegate_wake"))):
            out.append(row)
    return out


def list_chats_with_waiting_input(execution_target: str,
                                  session_ids: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Chats holding messages in ``chat_input_queue`` that run on one machine:
    pinned to it by ``execution_target``, or running one of ``session_ids``
    (the sessions the platform holds there; a chat with an empty target).
    A candidate set for the reconnect pass, not a claim."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT c.id FROM chat_input_queue q JOIN chats c ON c.id = q.chat_id "
            "WHERE c.execution_target = %s OR c.session_id = ANY(%s)",
            (execution_target, list(session_ids)),
        ).fetchall()
        return sorted(r["id"] for r in rows)


def claim_title_generation(chat_id: str) -> tuple[bool, str | None]:
    """Atomically claim the one-time LLM chat-title upgrade.

    Returns ``(True, pre_claim_title)`` for exactly ONE caller — the first to
    flip ``title_generated`` FALSE→TRUE — and ``(False, None)`` for every
    subsequent call. This is the once-only guard for
    ``services/title_generator.py``: the headless pump may fire at the
    text/tool thresholds AND again at turn-end, the interactive funnel fires
    from batch counters + timer + turn-complete, and a later turn could fire
    again — all funnel here and only the winner generates a title. The
    returned title is the snapshot the winner must CAS against on its final
    write (``update_chat_title_cas``), so a manual rename landing AFTER the
    claim always beats the generated title. Single UPDATE..FROM..FOR UPDATE
    (mirrors ``claim_pending_history_seed``) so concurrent callers can't both win.
    """
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE chats c SET title_generated = TRUE
               FROM (SELECT id, title_generated, title FROM chats
                      WHERE id = %s FOR UPDATE) old
               WHERE c.id = old.id AND old.title_generated = FALSE
               RETURNING old.title""",
            (chat_id,),
        ).fetchone()
        conn.commit()
        if row is None:
            return False, None
        return True, row["title"]


def set_chat_title_if_unset(chat_id: str, title: str) -> bool:
    """The deterministic first-message title: ONE conditional write, so a
    rename that landed meanwhile (a REST rename, the LLM upgrade) wins over
    it. Returns whether the row was written; the FTS row is rebuilt only on
    an actual write."""
    if not title:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chats SET title=%s, updated_at=%s "
            "WHERE id=%s AND (title IS NULL OR title='')",
            (title, now, chat_id),
        )
        conn.commit()
        if cur.rowcount > 0:
            try:
                _rebuild_chat_search_row(conn, chat_id)
                conn.commit()
            except psycopg.Error:
                logger.warning("chat_search rebuild failed for %s", chat_id,
                               exc_info=True)
        return cur.rowcount > 0


def update_chat_title_cas(chat_id: str, new_title: str,
                          expected_title: str | None) -> bool:
    """Conditional title write for the LLM upgrade's final step.

    Succeeds only while the title is still exactly the claim-time snapshot
    (NULL-safe compare) — a manual rename that landed between the claim and
    this write wins, and the generated title is discarded. Returns whether
    the row was written; the FTS row is rebuilt only on an actual write.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE chats SET title=%s, updated_at=%s
                WHERE id=%s AND title IS NOT DISTINCT FROM %s""",
            (new_title, now, chat_id, expected_title),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False
        try:
            _rebuild_chat_search_row(conn, chat_id)
            conn.commit()
        except psycopg.Error:
            logger.warning("chat_search rebuild failed for %s", chat_id,
                           exc_info=True)
        return True


def get_retention_candidate_chats(cutoff_iso: str, *,
                                  fileless_paths: list[str]) -> list[dict]:
    """Chats whose LOCAL on-disk session may be aged out by the retention
    sweep (services/infra/retention.py). Remote-pinned chats are the satellite's
    business; ''-target rows (post-#11 machine deletion) already have NULL
    session ids. ``fileless_paths`` is the resolved list of engines that keep
    NO session files (the caller reads it off the registry:
    ``behaviour.rebuilds_history_from_db`` — a SQL literal cannot read a
    descriptor); their chats are skipped, every other ``execution_path`` —
    the empty default included — is a candidate, and an empty list skips
    nothing.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, user_sub, agent, session_id, codex_thread_id
                 FROM chats
                WHERE execution_target = 'local'
                  AND execution_path <> ALL(%s)
                  AND (session_id IS NOT NULL OR codex_thread_id IS NOT NULL)
                  AND updated_at < %s""",
            (list(fileless_paths), cutoff_iso),
        ).fetchall()
        return [chat_row(r) for r in rows]


def get_remote_retention_candidate_chats(cutoff_iso: str, *,
                                         fileless_paths: list[str]) -> list[dict]:
    """The remote half of ``get_retention_candidate_chats``: chats pinned to
    a machine (``execution_target`` neither ``'local'`` nor ``''``) whose
    session there may be aged out, with the machine they are pinned to.
    The same engine and age rules."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, user_sub, agent, session_id, codex_thread_id, execution_target
                 FROM chats
                WHERE execution_target NOT IN ('local', '')
                  AND execution_path <> ALL(%s)
                  AND (session_id IS NOT NULL OR codex_thread_id IS NOT NULL)
                  AND updated_at < %s""",
            (list(fileless_paths), cutoff_iso),
        ).fetchall()
        return [chat_row(r) for r in rows]


def get_session_refs_by_target() -> dict[str, set[str]]:
    """Every session and thread id a row still points at, keyed by the
    row's stored ``execution_target`` as it is (the caller asks
    ``core.placement`` which machine, if any, a value names);
    ``dynamic_tasks``' refs are under ``""``. The remote retention pass
    reads it after the flagging, so an aged chat's old ids are in no
    list."""
    by_target: dict[str, set[str]] = {"": set()}
    with get_conn() as conn:
        for r in conn.execute(
            "SELECT execution_target, session_id, codex_thread_id FROM chats "
            "WHERE session_id IS NOT NULL OR codex_thread_id IS NOT NULL"
        ).fetchall():
            bucket = by_target.setdefault(r["execution_target"] or "", set())
            for ref in (r["session_id"], r["codex_thread_id"]):
                if ref:
                    bucket.add(ref)
        for sql in (
            "SELECT DISTINCT continue_session AS r FROM dynamic_tasks "
            "WHERE continue_session IS NOT NULL AND continue_session != ''",
            "SELECT DISTINCT on_complete_session_id AS r FROM dynamic_tasks "
            "WHERE on_complete_session_id IS NOT NULL AND on_complete_session_id != ''",
        ):
            by_target[""].update(r["r"] for r in conn.execute(sql).fetchall())
    return by_target


def get_phone_chats(cutoff_iso: str | None = None) -> list[dict]:
    """Phone conversations (``source_type='phone'``) — all of them, or only
    those untouched since ``cutoff_iso``. The caller-data retention sweep and
    "Forget all" delete them whole (rows, messages, session files) —
    services/infra/external_retention.py."""
    sql = ("SELECT id, agent, session_id, codex_thread_id, updated_at FROM chats "
           "WHERE source_type = 'phone'")
    params: list = []
    if cutoff_iso:
        sql += " AND updated_at < %s"
        params.append(cutoff_iso)
    with get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY updated_at", params).fetchall()
        return [chat_row(r) for r in rows]


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


# Chats per retention UPDATE: the first sweep of a large install flags
# thousands at once, and one statement for all of them could outlast the
# pool's statement timeout.
_RETENTION_FLAG_BATCH = 500


def flag_chats_for_retention(chat_ids: list[str], cutoff_iso: str) -> list[str]:
    """Transition aged-out chats to the reseed flow, in committed batches;
    returns the ids actually flagged. ``cutoff_iso`` is the candidate
    query's cutoff: a chat resumed since that query (its ``updated_at``
    moved past the cutoff) is left alone, and the sweep keeps its files.

    Mirrors remote_store.delete_remote_machine's chat transition. Deliberately
    NOT update_chat: that helper bumps updated_at, which would float every
    aged chat to the top of the chat list the moment the sweep runs.
    """
    if not chat_ids:
        return []
    flagged: list[str] = []
    with get_conn() as conn:
        for i in range(0, len(chat_ids), _RETENTION_FLAG_BATCH):
            rows = conn.execute(
                """UPDATE chats
                      SET session_id = NULL, codex_thread_id = NULL,
                          pending_history_seed = 'retention',
                          last_turn_aborted = FALSE, last_abort_graceful = FALSE,
                          context_used = 0
                    WHERE id = ANY(%s) AND updated_at < %s
                RETURNING id""",
                (chat_ids[i:i + _RETENTION_FLAG_BATCH], cutoff_iso),
            ).fetchall()
            conn.commit()
            flagged.extend(r["id"] for r in rows)
    return flagged


def get_chat_by_session(session_id: str) -> dict | None:
    """Reverse lookup: find the most recent chat for a session_id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chats WHERE session_id=%s ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return chat_row(row) if row else None


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
            with contextlib.suppress(OSError):
                os.unlink(p)
    return deleted


def add_chat_message(chat_id: str, role: str, content: str = "",
                     event_type: str = "", event_data: str = "",
                     author_sub: str = "", *, sync_search: bool = True) -> int:
    """Persist one chat message. ``author_sub`` is the REAL sender's user_sub —
    set for Shared-only chats (where the chat row owner is a synthetic
    ``agent::{slug}``) so each message keeps its true attribution. Empty for
    single-owner chats (owner == sender) and assistant/event rows.

    ``sync_search=False`` skips the search-row rebuild (O(chat) work): a
    caller writing many rows rebuilds once with ``rebuild_chat_search``."""
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
        if role == _ROLE_USER and content and session_kind.is_task_chat_id(chat_id):
            _stamp_task_title(conn, chat_id, content)
            conn.commit()
        # Sync search index for user/assistant text messages
        if sync_search and role in _SEARCHED_ROLES and content:
            try:
                _rebuild_chat_search_row(conn, chat_id)
                conn.commit()
            except psycopg.Error as e:
                conn.rollback()
                _warn_rebuild_failed(chat_id, e)  # the row itself is committed
        return row_id


def _stamp_task_title(conn, chat_id: str, first_prompt: str) -> None:
    """The deterministic title of a scheduler-driven task chat, from its first
    prompt (never overwrites a title). Runs inside a savepoint: its failure
    never costs the rows it rides with."""
    try:
        with conn.transaction():
            title_row = conn.execute(
                "SELECT title FROM chats WHERE id=%s", (chat_id,),
            ).fetchone()
            if title_row is not None and not (title_row["title"] or "").strip():
                from storage.chat.chat_titles import deterministic_title
                conn.execute(
                    "UPDATE chats SET title=%s WHERE id=%s",
                    (deterministic_title(first_prompt), chat_id),
                )
    except psycopg.Error:
        logger.debug("task title stamp failed for %s", chat_id, exc_info=True)


def _row_refused(exc: Exception) -> bool:
    """A failure one row can cause: a value the database refuses (a NUL byte
    is refused by the client with no SQLSTATE, a lone surrogate cannot be
    encoded at all) or a constraint the row breaks. Never the connection,
    the pool, a cancel or the commit, whose outcome may be unknown, and never
    a deleted chat, whose foreign key every row breaks alike."""
    if isinstance(exc, (psycopg.DataError, UnicodeEncodeError)):
        return True
    return (isinstance(exc, psycopg.IntegrityError)
            and not isinstance(exc, psycopg.errors.ForeignKeyViolation))


def add_chat_messages_batch(chat_id: str,
                            rows: list[tuple[str, str, str, str]], *,
                            ids: list[int] | None = None,
                            in_txn: Callable[[psycopg.Connection, list[int]], None] | None = None,
                            search: bool = True) -> int:
    """Persist a turn's rows, ``(role, content, event_type, event_data)``
    each, in order: one transaction for the inserts and ``updated_at``, then
    ONE search-row rebuild for the lot (not one per text row). When the
    insert transaction fails on a row the database refuses, it is rolled
    back and the rows go in one by one, so a bad row costs only itself. A
    connection or pool failure before the commit sends the batch once more
    (nothing was committed; a cancel or a server shutdown is the server's
    answer and is not); any other failure is raised, with nothing written
    again. Returns the last inserted id (0 when nothing landed).

    ``ids`` receives each row's id in order (0 for a row refused one by
    one). ``in_txn(conn, ids)`` runs inside the insert transaction before its
    commit, so what it writes lands with the rows or not at all; after the
    rows one by one it runs in a transaction of its own. ``search=False``
    leaves the search row to a later rebuild (a turn's checkpoint)."""
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    needs_search = any(role in _SEARCHED_ROLES and content
                       for role, content, _t, _d in rows)
    first_prompt = next((content for role, content, _t, _d in rows
                         if role == _ROLE_USER and content), "")
    committing = False

    written: list[int] = []

    def _write() -> int:
        nonlocal committing
        written.clear()
        with get_conn() as conn:
            for role, content, event_type, event_data in rows:
                written.append(conn.execute(
                    """INSERT INTO chat_messages (chat_id, role, content, event_type, event_data, author_sub, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (chat_id, role, content or "", event_type or "",
                     event_data or "", "", now),
                ).fetchone()["id"])
            conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (now, chat_id))
            if first_prompt and session_kind.is_task_chat_id(chat_id):
                _stamp_task_title(conn, chat_id, first_prompt)
            if in_txn is not None:
                in_txn(conn, written)
            committing = True
            conn.commit()
        return written[-1]

    last_id = 0
    for attempt in (1, 2):
        try:
            last_id = _write()
            break
        except Exception as e:
            if _row_refused(e):
                logger.warning("chat %s: a %d-row batch failed (%s); writing row by row",
                               chat_id, len(rows), e)
                written[:] = _write_rows_one_by_one(chat_id, rows)
                last_id = max(written, default=0)
                if in_txn is not None:
                    with get_conn() as conn:
                        in_txn(conn, written)
                        conn.commit()
                break
            if (committing or attempt == 2
                    or not isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError))
                    or isinstance(e, (psycopg.errors.QueryCanceled,
                                      psycopg.errors.AdminShutdown))):
                raise
            logger.warning("chat %s: a %d-row batch failed before its commit (%s); "
                           "sending it once more", chat_id, len(rows), e)
    if ids is not None:
        ids[:] = written
    if needs_search and search:
        rebuild_chat_search(chat_id)
    return last_id


def _write_rows_one_by_one(chat_id: str, rows: list[tuple[str, str, str, str]]) -> list[int]:
    """Each row in its own transaction; a refused row's id is 0."""
    written: list[int] = []
    for role, content, event_type, event_data in rows:
        try:
            written.append(add_chat_message(chat_id, role, content or "",
                                            event_type=event_type or "",
                                            event_data=event_data or "",
                                            sync_search=False))
        except (psycopg.Error, ValueError) as row_err:
            written.append(0)
            logger.warning("chat %s: a %s row was refused: %s",
                           chat_id, role, row_err)
    return written


def update_chat_message_event_data(chat_id: str, message_id: int, event_data: str) -> None:
    """Rewrite one event row's payload in place (a block a turn saved early
    and changed afterwards). Event rows are not in the search row."""
    with get_conn() as conn:
        conn.execute("UPDATE chat_messages SET event_data=%s WHERE id=%s AND chat_id=%s",
                     (event_data, message_id, chat_id))
        conn.commit()


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


def get_chat_messages_since(floors: dict[str, int]) -> list[dict]:
    """The rows of several chats above a per-chat floor, ascending by id:
    the task chat's delta (``ws/dashboard_chat_resume.py``). One query with
    each chat's own floor (a sibling run at floor 0 fetches its rows alone,
    not every chat's since id 0). An empty map answers ``[]`` with no
    query."""
    floors = {c: int(f) for c, f in floors.items() if c}
    if not floors:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT m.* FROM chat_messages m
               JOIN unnest(%s::text[], %s::bigint[]) AS f(chat_id, floor)
                 ON m.chat_id = f.chat_id AND m.id > f.floor
               ORDER BY m.id""",
            (list(floors), list(floors.values())),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_chat_message_id(chat_id: str) -> int:
    """Highest message id in a chat (0 if empty). The pump records this at a turn
    boundary as an id-based cutoff so a *windowed* resume can still withhold the
    in-flight tail (a row COUNT can't index a 50-row window)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM chat_messages WHERE chat_id=%s", (chat_id,),
        ).fetchone()
    return int(row["m"]) if row and row["m"] is not None else 0


def get_last_user_message_author(chat_id: str) -> str:
    """``author_sub`` of the most recent user message ('' if none/unset).

    Shared-only chats use this to detect a change of hands: when the
    current sender differs from the previous author, the turn gets a
    speaker-transition note so the agent doesn't attribute the earlier
    conversation to the new sender.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT author_sub FROM chat_messages WHERE chat_id=%s AND role='user' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return (row["author_sub"] or "") if row else ""


def enqueue_chat_input(chat_id: str, queue_id: str, author_sub: str, *, text: str,
                       cli_text: str, event_data: str, images: str,
                       origin_conn: str) -> dict | None:
    """Insert one waiting message (``chat_input_queue``), or None when the
    chat already holds that ``queue_id`` (a re-send after a reconnect)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO chat_input_queue
                   (chat_id, queue_id, author_sub, text, cli_text, event_data, images,
                    origin_conn, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (chat_id, queue_id) DO NOTHING
               RETURNING *""",
            (chat_id, queue_id, author_sub, text, cli_text, event_data, images,
             origin_conn, now),
        ).fetchone()
        conn.commit()
    return dict(row) if row else None


def queued_chats_of(author_sub: str) -> list[dict]:
    """The chats holding messages ``author_sub`` queued: ``id``, ``agent``,
    ``user_sub`` (the owner)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT c.id, c.agent, c.user_sub FROM chat_input_queue q "
            "JOIN chats c ON c.id = q.chat_id WHERE q.author_sub = %s",
            (author_sub,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_chat_input_queue(chat_id: str) -> list[dict]:
    """The chat's waiting messages in queue order."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_input_queue WHERE chat_id=%s ORDER BY id",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_chat_input(chat_id: str, queue_id: str) -> dict | None:
    """Remove one waiting message; the row it was, or None when none."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM chat_input_queue WHERE chat_id=%s AND queue_id=%s RETURNING *",
            (chat_id, queue_id),
        ).fetchone()
        conn.commit()
    return dict(row) if row else None


def delete_chat_inputs_where(*, chat_id: str = "", author_sub: str = "",
                             origin_conn: str = "") -> list[dict]:
    """Remove the waiting messages a connection queued (``origin_conn``) or a
    person queued (``author_sub``), in one chat when ``chat_id`` is given:
    the rows that went, for the chats' frames. Nothing without a person or
    a connection to match."""
    if not (author_sub or origin_conn):
        return []
    where, args = [], []
    for col, val in (("chat_id", chat_id), ("author_sub", author_sub),
                     ("origin_conn", origin_conn)):
        if val:
            where.append(f"{col}=%s")
            args.append(val)
    with get_conn() as conn:
        rows = conn.execute(
            f"DELETE FROM chat_input_queue WHERE {' AND '.join(where)} "
            "RETURNING chat_id, queue_id",
            tuple(args),
        ).fetchall()
        conn.commit()
    return [dict(r) for r in rows]


def delete_message_ranges_in(conn: psycopg.Connection, chat_id: str,
                             ranges: list[tuple[int, int]]) -> int:
    """Delete, inside the caller's transaction, the assistant and event rows
    of ``chat_id`` whose id falls in one of ``ranges`` (each ``(after,
    upto)``: above ``after``, at most ``upto``), user rows kept: the blocks a
    turn saved early that its replay after a restart writes again. The search
    row follows when anything went. Returns the rows deleted."""
    deleted = 0
    for after, upto in ranges:
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE chat_id=%s AND id > %s AND id <= %s "
            "AND role <> %s",
            (chat_id, after, upto, _ROLE_USER),
        )
        deleted += cur.rowcount or 0
    if deleted:
        _rebuild_chat_search_row(conn, chat_id)
    return deleted


def delete_chat_message_ranges(chat_id: str, ranges: list[tuple[int, int]]) -> int:
    """``delete_message_ranges_in`` in a transaction of its own."""
    if not ranges:
        return 0
    with get_conn() as conn:
        deleted = delete_message_ranges_in(conn, chat_id, ranges)
        conn.commit()
    return deleted


def accept_chat_inputs(chat_id: str, rows: list[dict]) -> list[int]:
    """The turn accepted ``rows``: one transaction deletes their queue rows
    (a steered message was never queued: no row to delete) and inserts one
    user message per row, in order, its ``event_data`` carrying the
    ``queue_id`` with the attachment meta and its ``author_sub`` kept.
    Returns the message ids. ``rows``: ``{queue_id, text, event_data (a
    JSON string, "" for none), author_sub}`` each."""
    if not rows:
        return []
    now = datetime.now(timezone.utc).isoformat()
    ids: list[int] = []
    with get_conn() as conn:
        for r in rows:
            qid = r.get("queue_id") or ""
            if qid:
                conn.execute(
                    "DELETE FROM chat_input_queue WHERE chat_id=%s AND queue_id=%s",
                    (chat_id, qid),
                )
            meta = json.loads(r["event_data"]) if r.get("event_data") else {}
            if qid:
                meta["queue_id"] = qid
            ids.append(conn.execute(
                """INSERT INTO chat_messages (chat_id, role, content, event_type, event_data, author_sub, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (chat_id, _ROLE_USER, r.get("text") or "", "",
                 json.dumps(meta) if meta else "", r.get("author_sub") or "", now),
            ).fetchone()["id"])
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (now, chat_id))
        first = next((r.get("text") for r in rows if r.get("text")), "")
        if first and session_kind.is_task_chat_id(chat_id):
            _stamp_task_title(conn, chat_id, first)
        conn.commit()
        if any(r.get("text") for r in rows):
            try:
                _rebuild_chat_search_row(conn, chat_id)
                conn.commit()
            except psycopg.Error as e:
                conn.rollback()
                _warn_rebuild_failed(chat_id, e)
    return ids


def list_tool_names(chat_id: str) -> list[str]:
    """Distinct tool names a chat ran, in first-use order — the persisted
    tool blocks (``event_type='tool'``; the phone call log's audit trail).
    Same TEXT-column caution as ``get_last_todo_snapshot``: parse in Python."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_data FROM chat_messages "
            "WHERE chat_id=%s AND event_type='tool' ORDER BY id",
            (chat_id,),
        ).fetchall()
    names: list[str] = []
    for row in rows:
        try:
            name = json.loads(row["event_data"] or "{}").get("name") or ""
        except (ValueError, TypeError, AttributeError):
            continue
        if name and name not in names:
            names.append(str(name))
    return names


def get_last_todo_snapshot(chat_id: str, tool_name: str) -> list[dict]:
    """The most recent checklist snapshot in a chat, for the idle-reload panel
    restore — searched over FULL history so it is independent of the loaded
    message window. ``tool_name`` is the persisted block name every engine's
    snapshot carries (``core/events/tool_roles.TODO_SNAPSHOT`` — the caller
    resolves it; a SQL literal reads no descriptor): native checklist rows AND
    the synthesized snapshots of Codex's ``update_plan`` and the CLI's
    ``TaskCreate`` / ``TaskUpdate`` family are all stored under it.

    ``event_data`` is TEXT and ``''`` on most rows, so a blind ``::jsonb`` cast can
    throw (Postgres doesn't guarantee predicate short-circuit) — prefilter with a
    LIKE that can't trip a cast (the default ``json.dumps`` separators are
    load-bearing: ``"name": "<tool>"``), then parse in Python.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT event_data FROM chat_messages "
            "WHERE chat_id=%s AND event_type='tool' AND event_data LIKE %s "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, f'%"name": {json.dumps(tool_name)}%'),
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
                with contextlib.suppress(OSError):
                    os.unlink(r["abs_path"])
        conn.execute(
            "DELETE FROM media_tokens WHERE expires_at <> '' AND expires_at < %s",
            (now_iso,),
        )
        conn.commit()
        return len(rows)


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
