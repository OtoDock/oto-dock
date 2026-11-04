"""A search result's way to its match: the newest message of the chat whose
own text holds every word of the query, and the words around it. The sidebar
shows the words under the result's title, and opening the result scrolls the
chat to that message (``?message=``).

Part of the chat search in ``db_chats``; the pass runs on the search's own
connection, after its rows are read, and never costs the search its rows.
"""

import logging
import re
import time

from core.session import session_kind
from storage.chat.chat_titles import written_text

logger = logging.getLogger("db_chats")

# The persisted role words the pass reads: a person's row is shown cleaned,
# and the two roles the search row indexes are the ones looked at.
_ROLE_USER = "user"
_SEARCHED_ROLES = [_ROLE_USER, "assistant"]

# The matched words inside a snippet sit between these two characters, so
# the sidebar highlights exactly what the server matched (stems included).
MARK_START = "\x02"
MARK_END = "\x03"

# The search answer's first rows only: the newest results are the ones read.
_ANCHOR_ROWS = 20
# The whole pass, and one lookup: a chat whose words sit in different
# messages scans its window to the end, and the search waits on the pass.
_BUDGET_S = 0.4
_LOOKUP_TIMEOUT_MS = 150
# How far back a chat is looked at: about what the chat page pages back to
# for an anchor (20 pages of 50 rows); a task-run chat opens with its newest
# 500 rows and no paging.
_WINDOW_ROWS = 1000
_TASK_WINDOW_ROWS = 500
# A match that sits only in text the chat does not show (a time prelude, a
# wrapper) is passed over for the next one.
_CANDIDATES = 3
# The text a message is matched and quoted on: the same window for both, so
# the quote always holds the match.
_MESSAGE_CHARS = 100_000
_SNIPPET_MAX = 160
_HEADLINE_OPTIONS = (f"StartSel={MARK_START}, StopSel={MARK_END}, "
                     "MaxFragments=1, MaxWords=16, MinWords=8, ShortWord=2")
_MARKDOWN_RUNS = re.compile(r"[*`#]+")

_CONTENT_HOLDS_QUERY = """
    SELECT chat_id FROM chat_search
     WHERE chat_id = ANY(%s) AND search_vector @@ %s::tsquery
"""

_CANDIDATE_ROWS = """
    SELECT id, role, left(content, %s) AS content FROM chat_messages
     WHERE chat_id = %s
       AND id >= COALESCE((SELECT id FROM chat_messages WHERE chat_id = %s
                            ORDER BY id DESC OFFSET %s LIMIT 1), 0)
       AND role = ANY(%s) AND content <> ''
       AND to_tsvector('english', left(content, %s)) @@ %s::tsquery
  ORDER BY id DESC
     LIMIT %s
"""


def _snippet(headline: str) -> str:
    text = " ".join(_MARKDOWN_RUNS.sub("", headline).split())
    if len(text) > _SNIPPET_MAX:
        text = text[:_SNIPPET_MAX].rstrip()
        if text.count(MARK_START) > text.count(MARK_END):
            text += MARK_END
    return text


def _shown_text(role: str, content: str) -> str:
    return written_text(content) if role == _ROLE_USER else content


def _anchor(conn, chat_id: str, query: str) -> tuple[int, str] | None:
    window = _TASK_WINDOW_ROWS if session_kind.is_task_chat_id(chat_id) else _WINDOW_ROWS
    rows = conn.execute(_CANDIDATE_ROWS, (
        _MESSAGE_CHARS, chat_id, chat_id, window - 1, _SEARCHED_ROLES,
        _MESSAGE_CHARS, query, _CANDIDATES,
    )).fetchall()
    for r in rows:
        text = _shown_text(r["role"], r["content"])
        if not text.strip():
            continue
        headline = conn.execute(
            "SELECT ts_headline('english', %s, %s::tsquery, %s) AS h",
            (text, query, _HEADLINE_OPTIONS),
        ).fetchone()["h"]
        if MARK_START in headline:
            return r["id"], _snippet(headline)
    return None


def stamp_match_anchors(conn, rows: list[dict], query: str, content_query: str) -> None:
    """Set ``match_message_id`` (the newest message holding every word of
    ``query``, or None) and ``match_snippet`` (its words around the match,
    the matched ones marked, or "") on every row. ``content_query`` is the
    same query held to the chat's text (weight B of the search vector): a
    chat found by its title alone is never scanned. Each lookup has its own
    savepoint and time limit, the pass a budget; whatever stops it (a
    cancelled lookup, the budget, an error) leaves the rest without an
    anchor and the rows as they were."""
    for r in rows:
        r["match_message_id"] = None
        r["match_snippet"] = ""
    head = rows[:_ANCHOR_ROWS]
    if not head or not query or not content_query:
        return
    deadline = time.monotonic() + _BUDGET_S
    try:
        holding = {h["chat_id"] for h in conn.execute(
            _CONTENT_HOLDS_QUERY, ([r["id"] for r in head], content_query)).fetchall()}
        if not holding:
            return
        # Until the search's transaction ends: the search reads nothing after.
        conn.execute(f"SET LOCAL statement_timeout = {_LOOKUP_TIMEOUT_MS}")
        for r in head:
            if r["id"] not in holding:
                continue
            if time.monotonic() >= deadline:
                break
            try:
                with conn.transaction():   # a savepoint: a cancelled lookup undoes itself alone
                    found = _anchor(conn, r["id"], query)
            except Exception as e:  # noqa: BLE001  # a cancelled or failed lookup costs only its row
                logger.info("search anchor skipped for a result: %s", type(e).__name__)
                continue
            if found:
                r["match_message_id"], r["match_snippet"] = found
    except Exception as e:  # noqa: BLE001  # the anchors are extra: the rows stand without them
        logger.info("search anchors stopped: %s", type(e).__name__)
