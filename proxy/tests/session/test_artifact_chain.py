"""The pump's one artifact arm (core-seams phase 9): every kind through
``_handle_perm_event`` on a pump with a live state, each behaving as the
table in ``core/events/artifact_events.py`` says — the eviction of the
LATEST placeholder only, a removal kind leaving the pending text unflushed
and the lists untouched but for the placeholder, the speaker stamped on a
placeholder in a meeting, the deferred preview held for the flush, and the
save dropping the transcode skeleton while keeping the generation one.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_artifact_chain.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from core.events import artifact_events, stream_pump
from core.events.stream_pump import ChatStreamPump, _chat_streaming_state
from ws import wire_events as wire


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )
    pump.ws_queue = pump.attach()  # the subscriber's queue: what the pump forwards
    return pump


def _item(kind: str, **fields) -> dict:
    base = {
        "images": {"images": [{"url": "https://x/1.png"}]},
        "url": {"url": "https://x", "title": "X"},
        "file": {"filename": "a.pdf", "download_url": "/d/1"},
        "ui": {"token": "t", "ui_url": "/v1/ui/t"},
        "document_preview": {"wopi_url": "w", "filename": "f.docx", "file_id": "fid", "download_url": "/d"},
    }.get(kind, {})
    return {"event_type": kind, **base, **fields}


@pytest.fixture
def live(temp_db):
    temp_db.create_chat("art1", "user-admin", "a1")
    _chat_streaming_state["art1"] = {"live_blocks": [], "active_tools": [], "active_agents": []}
    yield _chat_streaming_state["art1"]
    _chat_streaming_state.pop("art1", None)


async def _forwarded(pump: ChatStreamPump) -> list[dict]:
    out = []
    while not pump.ws_queue.empty():
        out.append(pump.ws_queue.get_nowait())
    return out


def _types(blocks: list[dict]) -> list[str]:
    return [b.get("type") for b in blocks]


@pytest.mark.asyncio
async def test_a_gallery_evicts_the_latest_placeholder_only(live):
    pump = _mk_pump("art1")
    try:
        await pump._handle_perm_event(_item("image_generating"))
        await pump._handle_perm_event(_item("image_generating"))
        await pump._handle_perm_event(_item("images"))
        assert _types(pump._turn_blocks) == ["image_generating", "images"]
        assert _types(live["live_blocks"]) == ["image_generating", "images"]
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_removal_kind_evicts_and_leaves_the_pending_text_unflushed(live):
    pump = _mk_pump("art1")
    try:
        await pump._handle_perm_event(_item("media_processing"))
        pump._pending_text.append("half a sentence")
        await pump._handle_perm_event(_item("media_failed", error="bad codec"))
        # the placeholder is gone from both lists, nothing was appended, the
        # text stays pending (a removal never closes a text block)
        assert _types(pump._turn_blocks) == []
        assert _types(live["live_blocks"]) == []
        assert pump._pending_text == ["half a sentence"]
        # and the removal frame reached the socket
        frames = [f.get("event", {}).get("type") for f in await _forwarded(pump) if f.get("event")]
        assert frames == ["media_processing", "media_failed"]
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_block_kind_closes_the_pending_text_and_is_forwarded(live):
    pump = _mk_pump("art1")
    try:
        pump._pending_text.append("before the link")
        await pump._handle_perm_event(_item("url"))
        assert _types(pump._turn_blocks) == [wire.TEXT, "url"]
        assert _types(live["live_blocks"]) == ["url"]
        frames = [f.get("event", {}).get("type") for f in await _forwarded(pump) if f.get("event")]
        assert frames == ["url"]
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_player_replaces_the_latest_transcode_skeleton(live):
    pump = _mk_pump("art1")
    try:
        await pump._handle_perm_event(_item("media_processing", media_kind="video"))
        await pump._handle_perm_event(_item("video", src_kind="url", url="https://x/v.mp4"))
        assert _types(pump._turn_blocks) == ["video"]
        assert _types(live["live_blocks"]) == ["video"]
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_placeholder_carries_the_meeting_speaker(live):
    pump = _mk_pump("art1")
    pump._meeting_agent = "alice"
    try:
        await pump._handle_perm_event(_item("media_processing"))
        await pump._handle_perm_event(_item("image_generating"))
        assert all(b.get("_meeting_agent") == "alice" for b in pump._turn_blocks)
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_deferred_kind_is_held_for_the_flush(live):
    pump = _mk_pump("art1")
    try:
        await pump._handle_perm_event(_item("document_preview", snapshot_id="s1", generation=1))
        await pump._handle_perm_event(_item("document_preview", snapshot_id="s2", generation=2))
        # replaced in place in both lists, nothing forwarded yet
        assert _types(pump._turn_blocks) == ["document_preview"]
        assert pump._turn_blocks[0]["snapshot_id"] == "s2"
        assert _types(live["live_blocks"]) == ["document_preview"]
        assert live["live_blocks"][0]["snapshot_id"] == "s2"
        assert [f for f in await _forwarded(pump) if f.get("event")] == []
        await pump._flush_pending_previews()
        frames = [f["event"] for f in await _forwarded(pump) if f.get("event")]
        assert [f["type"] for f in frames] == ["document_preview"] and frames[0]["snapshot_id"] == "s2"
    finally:
        pump.producer.cancel()


def test_the_save_drops_the_transcode_skeleton_and_keeps_the_generation_one():
    rows = stream_pump._serialize_turn_rows([
        {"type": wire.TEXT, "content": "hi"},
        {"type": "media_processing", "media_kind": "video"},
        {"type": "image_generating", "prompt_preview": "a cat"},
        {"type": "url", "url": "https://x", "title": "X"},
        {"type": wire.PERSISTED_TOOL, "name": "Read", "summary": "", "tool_id": "t1"},
    ])
    kinds = [r[2] or r[0] for r in rows]
    assert "media_processing" not in kinds
    assert "image_generating" in kinds and "url" in kinds and "assistant" in kinds
    assert wire.PERSISTED_TOOL in kinds
    assert not artifact_events.kind_of("media_processing").saved
    assert artifact_events.kind_of("image_generating").saved


def test_the_save_drops_the_live_wopi_token():
    """F57: the document preview's WOPI token rides the live frame only; the
    stored row (what history and a reconnect read) never carries it."""
    import json
    rows = stream_pump._serialize_turn_rows([
        {"type": "document_preview", "wopi_url": "https://c/cool.html?WOPISrc=x",
         "access_token": "secret-token", "access_token_ttl": 1, "filename": "a.docx",
         "file_id": "f", "download_url": "/d"},
    ])
    stored = json.loads(rows[0][3])
    assert "access_token" not in stored and "access_token_ttl" not in stored
    assert stored["wopi_url"] == "https://c/cool.html?WOPISrc=x"
