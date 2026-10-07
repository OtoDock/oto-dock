"""The document preview hook (api/hooks/preview.py): the chat a push lands
in, its token's chat claim, the push's version number and the cap on a
file's versions.

The hook resolves the chat before it mints, so the token names the chat
whose pane a save refreshes; the token cache never hands one chat's token
to another; each push is numbered among the file's pushes in the chat and
trims the file's oldest version copies past the cap.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from storage import database as task_store


async def _pass_async(*a, **kw):
    return None


@pytest.fixture
def hook(monkeypatch, tmp_path):
    import config
    from collections import OrderedDict
    from api.hooks import hooks as hooks_mod, paths, preview, routing

    monkeypatch.setattr(preview, "_pushed_generations", OrderedDict())

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents", raising=False)
    monkeypatch.setattr(config, "PREVIEW_SNAPSHOT_DIR", tmp_path / "snaps", raising=False)
    monkeypatch.setattr(config, "COLLABORA_URL", "https://c.example", raising=False)
    monkeypatch.setattr(config, "WOPI_BASE_URL", "https://w.example", raising=False)
    monkeypatch.setattr(config, "WOPI_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(preview, "verify_session_match_async", _pass_async)
    doc = tmp_path / "agents" / "test-agent" / "workspace" / "report.docx"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"doc bytes")

    async def _classify(session_id, path):
        return doc, None
    monkeypatch.setattr(paths, "_classify_and_pull", _classify)
    monkeypatch.setattr(
        "api.hooks.preview.get_session_security",
        lambda sid: SimpleNamespace(role="manager", username="u", mount_username="", agent="test-agent"),
    )
    chats = {"sess-a": "chat-a", "sess-b": "chat-b", "sess-none": None}
    monkeypatch.setattr(routing, "resolve_hook_chat_id",
                        AsyncMock(side_effect=chats.get))
    pushed: list[dict] = []

    class _Queue:
        async def put(self, item):
            pushed.append(item)
    monkeypatch.setattr(preview, "get_permission_queue", lambda sid: _Queue())
    preview._wopi_token_cache.clear()
    app = FastAPI()
    app.include_router(hooks_mod.router)
    client = TestClient(app)

    def push(session_id="sess-a"):
        resp = client.post("/v1/hooks/document-preview", json={
            "session_id": session_id, "file_path": "/workspace/report.docx",
        }, headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200, resp.text
        return pushed[-1]
    yield push
    preview._wopi_token_cache.clear()


def _persist(chat_id, item):
    """What the pump's flush (or the terminal drainer) stores for a push."""
    from core.events.artifact_events import LIVE_ONLY_KEYS, artifact_event_from_perm_item
    evt = {k: v for k, v in artifact_event_from_perm_item(item).items() if k not in LIVE_ONLY_KEYS}
    task_store.add_chat_message(chat_id, "event", "", event_type="document_preview",
                                event_data=json.dumps(evt))


def _claims(item):
    return jwt.decode(item["access_token"], "test-secret", algorithms=["HS256"])


def test_the_token_names_the_chat_and_is_never_shared_across_chats(temp_db, hook):
    task_store.create_chat("chat-a", "user-a", "test-agent")
    task_store.create_chat("chat-b", "user-a", "test-agent")
    a1, a2, b1 = hook("sess-a"), hook("sess-a"), hook("sess-b")
    assert _claims(a1)["chat_id"] == "chat-a"
    assert a2["access_token"] == a1["access_token"]  # reused within a chat
    assert _claims(b1)["chat_id"] == "chat-b"
    assert b1["access_token"] != a1["access_token"]


def test_a_push_with_no_chat_has_no_claim_number_or_version_copy(temp_db, hook, tmp_path):
    item = hook("sess-none")
    assert "chat_id" not in _claims(item)
    assert item["version"] == 0 and item["snapshot_id"] == ""
    assert not (tmp_path / "snaps").exists()


def test_pushes_are_numbered_per_file_in_the_chat(temp_db, hook):
    task_store.create_chat("chat-a", "user-a", "test-agent")
    first = hook()
    assert first["version"] == 1
    # Within one turn nothing is persisted yet: the same number (the pump
    # keeps the last push of the file).
    assert hook()["version"] == 1
    _persist("chat-a", first)
    assert hook()["version"] == 2


def test_a_push_trims_the_files_oldest_version_copies(temp_db, hook, monkeypatch, tmp_path):
    from services.media import preview_snapshots
    monkeypatch.setattr(preview_snapshots, "MAX_VERSIONS_PER_FILE", 2)
    task_store.create_chat("chat-a", "user-a", "test-agent")
    first = hook()
    _persist("chat-a", first)
    second = hook()
    _persist("chat-a", second)
    third = hook()
    assert third["version"] == 3
    snaps = tmp_path / "snaps" / "chat-a"
    assert not (snaps / first["snapshot_id"]).exists()
    assert (snaps / second["snapshot_id"]).exists()
    assert (snaps / third["snapshot_id"]).exists()


def test_expired_tokens_leave_the_cache(temp_db, hook):
    from api.hooks import preview
    task_store.create_chat("chat-a", "user-a", "test-agent")
    preview._wopi_token_cache[("old", "edit", "chat-x")] = {"token": "t", "ttl": 0, "expires": 0}
    hook()
    assert ("old", "edit", "chat-x") not in preview._wopi_token_cache


def _opened(item):
    import urllib.parse
    from api.media import wopi
    query = urllib.parse.parse_qs(urllib.parse.urlparse(item["wopi_url"]).query)
    assert "_t" not in query
    return wopi.decode_wopi_id(query["WOPISrc"][0].rsplit("/", 1)[-1])


def test_the_pushed_url_opens_the_push_generations_document(temp_db, hook, tmp_path):
    import os
    from api.hooks import preview
    from api.media import wopi
    task_store.create_chat("chat-a", "user-a", "test-agent")
    rel = "test-agent/workspace/report.docx"
    doc = tmp_path / "agents" / rel
    item = hook()
    assert _opened(item) == (rel, item["generation"])
    assert item["file_id"] == wopi.encode_file_id(rel)
    assert preview.pushed_generation(item["file_id"]) == item["generation"]
    # The same bytes pushed again (preview_document) open the same document,
    # from any chat, while the event keeps its own push time. On a remote
    # session the resolve has just rewritten them: a new mtime, same bytes.
    st = doc.stat()
    doc.write_bytes(b"doc bytes")
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000))
    again = hook("sess-none")
    assert _opened(again) == (rel, item["generation"])
    assert again["generation"] >= item["generation"]
    # A rewrite opens a new document, strictly after the noted one even
    # within the same millisecond.
    doc.write_bytes(b"new doc bytes")
    preview._pushed_generations[item["file_id"]] = (10**15, preview._pushed_generations[item["file_id"]][1])
    rewritten = hook()
    assert _opened(rewritten) == (rel, 10**15 + 1)
    assert preview.pushed_generation(item["file_id"]) == 10**15 + 1


def test_a_push_of_the_bytes_a_person_saved_keeps_the_document(temp_db, hook, tmp_path):
    # A pane save since the push: the document's base is the saved bytes,
    # and a preview_document of them opens the same document.
    import hashlib
    from api.hooks import preview
    from api.media import wopi
    task_store.create_chat("chat-a", "user-a", "test-agent")
    rel = "test-agent/workspace/report.docx"
    first = hook()
    (tmp_path / "agents" / rel).write_bytes(b"the person's save")
    wopi._note_doc_base(wopi.encode_wopi_id(rel, first["generation"]),
                        hashlib.sha256(b"the person's save").hexdigest())
    try:
        assert _opened(hook()) == (rel, first["generation"])
    finally:
        wopi._doc_bases.clear()
    assert preview.unchanged_since_push(first["file_id"], rel,
                                        hashlib.sha256(b"something else").hexdigest()) is False


def test_a_write_back_to_the_pushed_bytes_after_a_persons_save_opens_a_new_document(temp_db, hook, tmp_path):
    # Push X, the person saves Y (the document's base), the agent writes X
    # again: the document holds Y, so X is a change to it, a new document.
    import hashlib
    from api.hooks import preview
    from api.media import wopi
    task_store.create_chat("chat-a", "user-a", "test-agent")
    rel = "test-agent/workspace/report.docx"
    first = hook()
    pushed = (tmp_path / "agents" / rel).read_bytes()
    wopi._note_doc_base(wopi.encode_wopi_id(rel, first["generation"]),
                        hashlib.sha256(b"the person's save").hexdigest())
    try:
        assert preview.unchanged_since_push(first["file_id"], rel,
                                            hashlib.sha256(pushed).hexdigest()) is False
        again = hook()
        assert _opened(again) == (rel, again["generation"])
        assert again["generation"] > first["generation"]
    finally:
        wopi._doc_bases.clear()


@pytest.fixture
def people(monkeypatch):
    """Two people of test-agent, a manager and a viewer, and the pump of a
    chat whose turn runs as one of them."""
    import auth.providers
    from core.events import stream_pump
    users = {
        "bob-sub": ({"name": "Bob Builder", "display_name": "Bob"}, "bob", "manager"),
        "vi-sub": ({"name": "Vi Viewer"}, "vi", "viewer"),
    }
    monkeypatch.setattr(task_store, "get_user", lambda sub: users.get(sub, (None,))[0])
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: users.get(sub, (None, None))[1])
    monkeypatch.setattr(auth.providers, "acting_role_of", lambda sub, agent: users[sub][2])

    def run_as(chat_id, sub, *, session="sess-a", done=False):
        monkeypatch.setitem(stream_pump._active_pumps, chat_id,
                            SimpleNamespace(wake_person=sub, session_id=session, is_done=done))
    return run_as


def test_the_pushed_token_names_the_person_the_turn_runs_as(temp_db, hook, people):
    # A shared chat: the row's owner is the agent, the turn runs as Bob.
    task_store.create_chat("chat-a", "agent::test-agent", "test-agent")
    people("chat-a", "bob-sub")
    claims = _claims(hook())
    assert (claims["user_sub"], claims["user_name"], claims["permissions"]) == ("bob-sub", "Bob Builder", "edit")


def test_the_persons_own_role_decides_the_write(temp_db, hook, people):
    # The session's context is a manager's, the turn runs as a viewer: the
    # token they receive cannot save the shared workspace file.
    task_store.create_chat("chat-a", "user-a", "test-agent")
    people("chat-a", "vi-sub")
    claims = _claims(hook())
    assert (claims["user_sub"], claims["permissions"]) == ("vi-sub", "view")


def test_a_turn_without_a_person_keeps_the_agents_token(temp_db, hook, people):
    task_store.create_chat("chat-a", "user-a", "test-agent")
    claims = _claims(hook())
    assert (claims["user_sub"], claims["user_name"], claims["permissions"]) == ("agent", "Agent", "edit")
    people("chat-a", "")
    assert _claims(hook())["user_sub"] == "agent"


def test_the_token_cache_is_per_person(temp_db, hook, people):
    task_store.create_chat("chat-a", "user-a", "test-agent")
    people("chat-a", "bob-sub")
    first = hook()
    assert hook()["access_token"] == first["access_token"]
    people("chat-a", "")
    agents = hook()
    assert _claims(agents)["user_sub"] == "agent"
    assert agents["access_token"] != first["access_token"]


def test_only_the_live_pump_of_the_push_names_the_person(temp_db, hook, people):
    # A finished pump, or one of another session, is not the turn the push
    # belongs to: the agent's token.
    task_store.create_chat("chat-a", "user-a", "test-agent")
    people("chat-a", "bob-sub", done=True)
    assert _claims(hook())["user_sub"] == "agent"
    people("chat-a", "bob-sub", session="sess-other")
    assert _claims(hook())["user_sub"] == "agent"
    people("chat-a", "bob-sub")
    assert _claims(hook())["user_sub"] == "bob-sub"

