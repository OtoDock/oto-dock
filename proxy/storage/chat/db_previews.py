"""Document preview rows: the ``document_preview`` events of a chat.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called through ``run_db`` or a worker thread from async code).

A preview row is a ``chat_messages`` event whose ``event_data`` is the
stored preview frame (``core/events/artifact_events``): ``file_id``,
``filename``, ``download_url``, ``snapshot_id`` (the version's pinned copy,
``""`` when none was taken), ``generation`` (the push time in ms),
``version`` (the number the push was stamped with) and ``dismissed`` on a
row a person closed. Rows are never deleted while the chat lives.
"""

import json
from datetime import datetime

from storage.pg import get_conn

_PREVIEW_EVENT = "document_preview"
_ROLE_USER = "user"


def _rows(conn, chat_id: str) -> list[dict]:
    """Every preview row of a chat in push order, with its parsed data
    (``data``); an unreadable row is skipped."""
    rows = conn.execute(
        "SELECT id, event_data, created_at FROM chat_messages "
        "WHERE chat_id=%s AND event_type=%s ORDER BY id",
        (chat_id, _PREVIEW_EVENT),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            data = json.loads(row["event_data"] or "{}")
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append({"id": row["id"], "data": data, "created_at": row["created_at"]})
    return out


def get_preview_rows(chat_id: str, file_id: str | None = None) -> list[dict]:
    """The chat's non-dismissed preview rows (of one file when ``file_id``
    is given), oldest first: ``{"id", "data", "created_at"}``."""
    with get_conn() as conn:
        rows = _rows(conn, chat_id)
    return [r for r in rows
            if not r["data"].get("dismissed")
            and (file_id is None or r["data"].get("file_id") == file_id)]


def count_preview_rows(chat_id: str, file_id: str) -> int:
    """How many non-dismissed preview rows of a file the chat holds."""
    return len(get_preview_rows(chat_id, file_id))


def get_chat_preview_listing_rows(chat_ids: str | list[str]) -> list[dict]:
    """The non-dismissed preview rows of a chat, or of the chats one page
    shows (a task run's rounds), oldest first, each with its ``chat_id``,
    ``turn``: the number of the person's messages up to it across those
    chats (at least 1), and ``generation`` filled from ``created_at`` for
    a row stored without one. One pass over the chats' rows."""
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, event_data, created_at, turn FROM ("
            "  SELECT id, chat_id, event_type, event_data, created_at,"
            "         count(*) FILTER (WHERE role=%s) OVER (ORDER BY id) AS turn"
            "  FROM chat_messages WHERE chat_id = ANY(%s)"
            ") t WHERE event_type=%s ORDER BY id",
            (_ROLE_USER, list(chat_ids), _PREVIEW_EVENT),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            data = json.loads(row["event_data"] or "{}")
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("dismissed"):
            continue
        generation = data.get("generation") or 0
        if not generation:
            try:
                generation = int(datetime.fromisoformat(row["created_at"]).timestamp() * 1000)
            except (TypeError, ValueError):
                generation = 0
        out.append({"id": row["id"], "chat_id": row["chat_id"], "data": data,
                    "turn": max(1, int(row["turn"] or 0)),
                    "generation": int(generation)})
    return out


def dismiss_document_previews(
    chat_id: str, file_id: str,
    snapshot_id: str | None = None, db_message_id: int | None = None,
) -> tuple[int, list[str]]:
    """Dismiss document_preview events with the given file_id in a chat.

    Instance scoping: ``snapshot_id`` (or ``db_message_id`` for pre-snapshot
    rows) narrows the dismissal to ONE preview instance. With neither, every
    instance for the file is dismissed.

    Returns ``(rows updated, snapshot ids of those rows)`` so the caller can
    delete exactly the newly-unreferenced snapshot files.
    """
    with get_conn() as conn:
        count = 0
        freed_snapshots: list[str] = []
        for row in _rows(conn, chat_id):
            data = row["data"]
            if data.get("file_id") != file_id or data.get("dismissed"):
                continue
            if snapshot_id is not None and data.get("snapshot_id") != snapshot_id:
                continue
            if db_message_id is not None and row["id"] != db_message_id:
                continue
            data["dismissed"] = True
            conn.execute(
                "UPDATE chat_messages SET event_data=%s WHERE id=%s",
                (json.dumps(data), row["id"]),
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
    if not snapshot_id:
        return None
    for row in get_preview_rows(chat_id):
        if row["data"].get("snapshot_id") == snapshot_id:
            return row["data"]
    return None


def get_preview_event_by_file(chat_id: str, file_id: str) -> dict | None:
    """The NEWEST non-dismissed document_preview event data for a file_id,
    or None.

    Re-minting a preview URL at render time requires a live reference in the
    chat — once every instance of a file's preview is dismissed, its URL must
    not be re-mintable through the chat."""
    if not file_id:
        return None
    rows = get_preview_rows(chat_id, file_id)
    return rows[-1]["data"] if rows else None


def get_referenced_preview_snapshot_ids(chat_id: str) -> set[str]:
    """Snapshot ids referenced by NON-dismissed document_preview events in a
    chat — the keep-set for the reference-driven snapshot GC."""
    return {r["data"]["snapshot_id"] for r in get_preview_rows(chat_id)
            if r["data"].get("snapshot_id")}
