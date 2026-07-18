"""Document-preview buffering on the pump: version-pinned snapshot identity.

The pump buffers one preview per file per turn (replace-in-place). With
push-time snapshots, the replaced intra-turn push's snapshot was never exposed
to the dashboard (previews only forward at flush) — the pump must delete it —
and the flushed event must persist ITS OWN snapshot_id, which the dashboard's
frozen "previous version" block later renders. The flush also arms the
reference-driven snapshot GC, which runs only after the rows persist.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_preview_pump.py -q
"""

import asyncio

import pytest

from core.events.common_events import CommonEvent, DONE
from core.events.stream_pump import ChatStreamPump
from services.media import preview_snapshots
from storage import database as task_store


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )


def _preview_item(file_id: str, snapshot_id: str, generation: int = 1) -> dict:
    return {
        "event_type": "document_preview",
        "wopi_url": f"/cool?f={file_id}&_t={generation}",
        "filename": f"{file_id}.xlsx",
        "file_id": file_id,
        "download_url": f"/d/{file_id}",
        "snapshot_id": snapshot_id,
        "generation": generation,
    }


@pytest.mark.asyncio
async def test_intra_turn_supersede_deletes_replaced_snapshot(temp_db, monkeypatch):
    temp_db.create_chat("pv1", "user-admin", "a1")
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        preview_snapshots, "delete_snapshot",
        lambda chat_id, sid: deleted.append((chat_id, sid)),
    )
    pump = _mk_pump("pv1")
    try:
        await pump._handle_perm_event(_preview_item("f1", "snap-a", 1))
        await pump._handle_perm_event(_preview_item("f1", "snap-b", 2))
        assert deleted == [("pv1", "snap-a")]
        # One buffered preview per file, carrying the newest identity.
        assert pump._pending_previews["f1"]["snapshot_id"] == "snap-b"
        previews = [b for b in pump._turn_blocks if b.get("type") == "document_preview"]
        assert len(previews) == 1 and previews[0]["snapshot_id"] == "snap-b"
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_distinct_files_keep_their_snapshots(temp_db, monkeypatch):
    temp_db.create_chat("pv2", "user-admin", "a1")
    deleted: list[str] = []
    monkeypatch.setattr(
        preview_snapshots, "delete_snapshot",
        lambda chat_id, sid: deleted.append(sid),
    )
    pump = _mk_pump("pv2")
    try:
        await pump._handle_perm_event(_preview_item("f1", "snap-a"))
        await pump._handle_perm_event(_preview_item("f2", "snap-b"))
        assert deleted == []
        assert set(pump._pending_previews) == {"f1", "f2"}
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_flush_forwards_and_persists_snapshot_identity(temp_db, monkeypatch):
    temp_db.create_chat("pv3", "user-admin", "a1")
    gc_calls: list[str] = []
    monkeypatch.setattr(preview_snapshots, "gc_chat", lambda cid: gc_calls.append(cid))
    pump = _mk_pump("pv3")
    try:
        q = pump.attach()
        await pump._handle_perm_event(_preview_item("f1", "snap-live", 7))
        await pump._process_event(CommonEvent(DONE, {}))
        # Forwarded frame carries the snapshot identity.
        frames = []
        while True:
            try:
                frames.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        preview_frames = [
            f["event"] for f in frames
            if f.get("pump_type") == "ws_event"
            and f.get("event", {}).get("type") == "document_preview"
        ]
        assert len(preview_frames) == 1
        assert preview_frames[0]["snapshot_id"] == "snap-live"
        assert preview_frames[0]["generation"] == 7
        # Persisted row carries it too (the frozen block's history source)...
        assert task_store.get_referenced_preview_snapshot_ids("pv3") == {"snap-live"}
        event = task_store.get_preview_event_by_snapshot("pv3", "snap-live")
        assert event is not None and event["file_id"] == "f1"
        # ...and the reference GC ran AFTER the rows persisted.
        assert gc_calls == ["pv3"]
    finally:
        pump.producer.cancel()
