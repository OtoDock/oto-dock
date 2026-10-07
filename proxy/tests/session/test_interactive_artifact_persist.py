"""Interactive display-artifact persistence (PiP replay-on-open seam).

The drainer persists every FINAL display/file-tools artifact it drains as a
pump-shaped ``chat_messages`` event row (``persist_drained_artifact``) and
stamps the row id on the forwarded item (``db_message_id``) — the dashboard's
stable key for replay dedupe + X-dismiss memory. Placeholders / failures /
blocking prompts never persist (parity with the pump's ``_save_turn_blocks``).
"""
import asyncio
import json
import os

import pytest
import pytest_asyncio

import config  # noqa: F401  (ensures conftest path/env setup ran)
from core import concurrency
from core.events.artifact_events import (
    REPLAYABLE_ARTIFACT_EVENT_TYPES,
    artifact_event_from_perm_item,
)
from core.session import interactive_session as isess
from core.session.session_state import get_permission_queue

_ENV = {"TERM": "xterm-256color", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


# ─────────────────────────── persist function (unit) ─────────────────────────


class TestPersistDrainedArtifact:
    def test_replayable_item_persists_pump_shaped_row(self, temp_db):
        temp_db.create_chat("chat-art-1", "user-admin", "agent-x")
        item = {
            "event_type": "video", "src_kind": "token", "token": "tok",
            "media_url": "/v1/media/tok", "mime": "video/mp4",
            "caption": "clip", "title": "Clip",
        }
        row_id = isess.persist_drained_artifact("chat-art-1", item)
        assert isinstance(row_id, int)

        rows = temp_db.get_chat_messages("chat-art-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == row_id
        assert row["role"] == "event" and row["event_type"] == "video"
        # Byte-identical to what the headless pump would persist — the
        # dashboard re-renders both through the same eventToBlock contract.
        assert json.loads(row["event_data"]) == artifact_event_from_perm_item(item)

    def test_every_replayable_type_persists(self, temp_db):
        temp_db.create_chat("chat-art-2", "user-admin", "agent-x")
        items = {
            "images": {"event_type": "images", "images": [{"url": "https://x/i.png"}]},
            "url": {"event_type": "url", "url": "https://x", "title": "X"},
            "file": {"event_type": "file", "filename": "a.pdf", "download_url": "/d/1"},
            "video": {"event_type": "video", "src_kind": "url", "url": "https://x/v.mp4"},
            "audio": {"event_type": "audio", "src_kind": "token", "token": "t",
                      "media_url": "/v1/media/t"},
            "document_preview": {"event_type": "document_preview", "wopi_url": "w",
                                 "filename": "f.docx", "file_id": "fid",
                                 "download_url": "/d"},
            "ui": {"event_type": "ui", "token": "t", "ui_url": "/v1/ui/t"},
        }
        assert set(items) == set(REPLAYABLE_ARTIFACT_EVENT_TYPES)
        for item in items.values():
            assert isess.persist_drained_artifact("chat-art-2", item) is not None
        rows = temp_db.get_chat_messages("chat-art-2")
        assert [r["event_type"] for r in rows] == list(items)

    def test_a_document_previews_token_is_never_stored(self, temp_db):
        temp_db.create_chat("chat-art-4", "user-admin", "agent-x")
        item = {"event_type": "document_preview", "wopi_url": "w", "filename": "f.docx",
                "file_id": "fid", "download_url": "/d", "access_token": "wopi-tok",
                "access_token_ttl": 1790000000000}
        assert artifact_event_from_perm_item(item)["access_token"] == "wopi-tok"
        assert isess.persist_drained_artifact("chat-art-4", item) is not None
        (row,) = temp_db.get_chat_messages("chat-art-4")
        stored = json.loads(row["event_data"])
        assert "access_token" not in stored and "access_token_ttl" not in stored
        assert "wopi-tok" not in row["event_data"] and stored["wopi_url"] == "w"

    def test_transient_and_blocking_items_never_persist(self, temp_db):
        temp_db.create_chat("chat-art-3", "user-admin", "agent-x")
        for et in ("image_generating", "image_gen_failed", "media_processing",
                   "media_failed", "permission_prompt", "plan_review",
                   "question", "tool_result", ""):
            assert isess.persist_drained_artifact("chat-art-3", {"event_type": et}) is None
        assert temp_db.get_chat_messages("chat-art-3") == []

    def test_missing_chat_id_and_db_failure_return_none(self, temp_db):
        item = {"event_type": "url", "url": "https://x", "title": "X"}
        assert isess.persist_drained_artifact("", item) is None
        # FK violation (no such chat row) → swallowed, never raises into the
        # drain loop (persistence must not break live delivery).
        assert isess.persist_drained_artifact("no-such-chat", item) is None


# ─────────────────────────── drainer integration ────────────────────────────


def _ensure_concurrency():
    concurrency.init()


@pytest_asyncio.fixture
async def _clean_registry():
    _ensure_concurrency()
    isess._lock = None
    isess._sessions.clear()
    concurrency._sessions.clear()
    concurrency._session_added_at.clear()
    real_live = concurrency._live_available_mb
    concurrency._live_available_mb = lambda: 32768
    yield
    concurrency._live_available_mb = real_live
    await isess.close_all(reason="test-teardown")
    isess._sessions.clear()
    concurrency._sessions.clear()
    concurrency._session_added_at.clear()


@pytest.mark.asyncio
class TestDrainerPersistence:
    async def test_drainer_persists_and_stamps_db_message_id(
        self, temp_db, _clean_registry,
    ):
        temp_db.create_chat("chat-drain", "user-admin", "agent-x")
        s = await isess.register(
            session_id="sid-art-drain", chat_id="chat-drain",
            agent_name="agent-x", argv=["cat"], env=dict(_ENV),
        )
        try:
            got: asyncio.Queue = asyncio.Queue()
            s.on_perm_event = got.put_nowait
            q = get_permission_queue("sid-art-drain")
            q.put_nowait({"event_type": "file", "filename": "x.txt",
                          "download_url": "/d/x"})
            q.put_nowait({"event_type": "permission_prompt",
                          "request_id": "r1", "tool_name": "Bash",
                          "tool_input": {}})

            artifact = await asyncio.wait_for(got.get(), 5)
            prompt = await asyncio.wait_for(got.get(), 5)

            # The artifact item was persisted first and carries the row id …
            assert artifact["event_type"] == "file"
            rows = temp_db.get_chat_messages("chat-drain")
            assert len(rows) == 1 and rows[0]["event_type"] == "file"
            assert artifact["db_message_id"] == rows[0]["id"]
            # … while blocking prompts pass through untouched, row-free.
            assert prompt["event_type"] == "permission_prompt"
            assert "db_message_id" not in prompt
        finally:
            await s.close()

    async def test_drainer_persists_with_no_viewer_attached(
        self, temp_db, _clean_registry,
    ):
        # The whole point of the seam: the artifact row exists even when
        # nobody is watching, so a later open can replay the popup.
        temp_db.create_chat("chat-noview", "user-admin", "agent-x")
        s = await isess.register(
            session_id="sid-art-noview", chat_id="chat-noview",
            agent_name="agent-x", argv=["cat"], env=dict(_ENV),
        )
        try:
            assert s.on_perm_event is None
            get_permission_queue("sid-art-noview").put_nowait(
                {"event_type": "url", "url": "https://x", "title": "X"}
            )
            for _ in range(50):
                if temp_db.get_chat_messages("chat-noview"):
                    break
                await asyncio.sleep(0.1)
            rows = temp_db.get_chat_messages("chat-noview")
            assert len(rows) == 1 and rows[0]["event_type"] == "url"
        finally:
            await s.close()
