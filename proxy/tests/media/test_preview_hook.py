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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import tokens
from storage import database as task_store


async def _pass_async(*a, **kw):
    return None


@pytest.fixture
def hook(monkeypatch, tmp_path):
    from collections import OrderedDict

    import config
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
        def put_nowait(self, item):
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
    from core.events.artifact_events import (
        LIVE_ONLY_KEYS,
        artifact_event_from_perm_item,
    )
    evt = {k: v for k, v in artifact_event_from_perm_item(item).items() if k not in LIVE_ONLY_KEYS}
    task_store.add_chat_message(chat_id, "event", "", event_type="document_preview",
                                event_data=json.dumps(evt))


def _claims(item):
    return tokens.decode(item["access_token"], tokens.WOPI)


def test_the_token_names_the_chat_and_is_never_shared_across_chats(temp_db, hook, people):
    task_store.create_chat("chat-a", "user-a", "test-agent")
    task_store.create_chat("chat-b", "user-a", "test-agent")
    people("chat-a", "bob-sub")
    people("chat-b", "bob-sub", session="sess-b")
    a1, a2, b1 = hook("sess-a"), hook("sess-a"), hook("sess-b")
    assert _claims(a1)["chat_id"] == "chat-a"
    assert a2["access_token"] == a1["access_token"]  # reused within a chat
    assert _claims(b1)["chat_id"] == "chat-b"
    assert b1["access_token"] != a1["access_token"]


def test_a_push_with_no_chat_has_no_token_number_or_version_copy(temp_db, hook, tmp_path):
    # No chat, no turn of a person to reach: no token to carry a claim.
    item = hook("sess-none")
    assert "access_token" not in item
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


def test_expired_tokens_leave_the_cache(temp_db, hook, people):
    from api.hooks import preview
    task_store.create_chat("chat-a", "user-a", "test-agent")
    people("chat-a", "bob-sub")
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


def test_a_turn_without_a_person_pushes_no_token(temp_db, hook, people):
    # A task run, a wake: the push reaches nobody's connection in
    # particular, so it mints nothing and every viewer's pane mints its own.
    from api.hooks import preview
    task_store.create_chat("chat-a", "user-a", "test-agent")
    assert "access_token" not in hook()
    people("chat-a", "")
    assert "access_token" not in hook()
    assert preview._wopi_token_cache == {}


def test_the_token_cache_is_per_person(temp_db, hook, people):
    from api.hooks import preview
    task_store.create_chat("chat-a", "user-a", "test-agent")
    people("chat-a", "bob-sub")
    first = hook()
    assert hook()["access_token"] == first["access_token"]
    people("chat-a", "vi-sub")
    vis = hook()
    assert _claims(vis)["user_sub"] == "vi-sub"
    assert vis["access_token"] != first["access_token"]
    assert len(preview._wopi_token_cache) == 2


def test_only_the_live_pump_of_the_push_names_the_person(temp_db, hook, people):
    # A finished pump, or one of another session, is not the turn the push
    # belongs to: no person, no token.
    task_store.create_chat("chat-a", "user-a", "test-agent")
    people("chat-a", "bob-sub", done=True)
    assert "access_token" not in hook()
    people("chat-a", "bob-sub", session="sess-other")
    assert "access_token" not in hook()
    people("chat-a", "bob-sub")
    assert _claims(hook())["user_sub"] == "bob-sub"




def test_the_pushed_token_opens_only_its_push_and_a_new_push_mints_again(temp_db, hook, people, tmp_path):
    from api.hooks import preview
    task_store.create_chat("chat-a", "user-a", "test-agent")
    people("chat-a", "bob-sub")
    first = hook()
    assert _claims(first)["generation"] == _opened(first)[1]
    assert hook()["access_token"] == first["access_token"]   # same document: reused
    (tmp_path / "agents" / "test-agent" / "workspace" / "report.docx").write_bytes(b"new bytes")
    second = hook()
    assert _opened(second)[1] > _opened(first)[1]
    assert _claims(second)["generation"] == _opened(second)[1]
    assert second["access_token"] != first["access_token"]
    assert len(preview._wopi_token_cache) == 1               # replaced, not grown


def test_a_meeting_participants_push_carries_no_token(temp_db, hook, people, monkeypatch):
    """A participant's push streams through the meeting's pump (no person)
    into the parent chat, where a person's own turn may have a pump of
    another session: no token."""
    from api.hooks import routing
    task_store.create_chat("chat-a", "user-a", "test-agent")
    monkeypatch.setattr(routing, "get_meeting_session_info", lambda sid: {
        "pump_session_id": "meeting-1", "parent_chat_id": "chat-a", "agent_slug": "test-agent",
        "parent_session_id": "sess-a"} if sid == "sess-p" else None)
    monkeypatch.setattr(routing, "resolve_hook_chat_id",
                        AsyncMock(side_effect={"sess-p": "chat-a", "sess-a": "chat-a"}.get))
    people("chat-a", "bob-sub", session="sess-a")
    assert "access_token" not in hook("sess-p")
    people("chat-a", "", session="meeting-1")
    assert "access_token" not in hook("sess-p")


@pytest.fixture
def terminal(monkeypatch):
    """A live interactive terminal registered under a session id: no pump,
    its drainer hands the push to the socket of its sole driver."""
    from core.session import interactive_session

    def open_terminal(sub, *, chat_id="chat-a", session="sess-a"):
        sess = interactive_session.InteractiveSession.__new__(interactive_session.InteractiveSession)
        sess.session_id, sess.chat_id, sess.user_sub = session, chat_id, sub
        sess._closing = sess._closed = False
        sess.pty = SimpleNamespace(closed=False)
        monkeypatch.setitem(interactive_session._sessions, session, sess)
        return sess
    return open_terminal


def test_a_terminal_chats_push_names_the_person_at_the_terminal(temp_db, hook, people, terminal):
    # A shared chat: the row's owner is the agent, Bob warmed the terminal.
    task_store.create_chat("chat-a", "agent::test-agent", "test-agent")
    terminal("bob-sub")
    item = hook()
    claims = _claims(item)
    assert (claims["user_sub"], claims["user_name"], claims["permissions"]) == ("bob-sub", "Bob Builder", "edit")
    assert item["token_person"] == "bob-sub"  # who may receive it (artifact_events.for_viewer)
    # Their own role decides the write: a viewer at the terminal views.
    terminal("vi-sub")
    claims = _claims(hook())
    assert (claims["user_sub"], claims["permissions"]) == ("vi-sub", "view")


def test_a_terminal_without_one_driver_pushes_no_token(temp_db, hook, people, terminal, monkeypatch):
    """A terminal anyone may drive (no owner recorded, a task chat), one of
    another chat, a closing or closed one, an owner who is no person (a
    delegated lane's source agent) and a session id a pump reads too: the
    push names nobody, so it carries no token and mints none."""
    from api.hooks import preview, routing
    task_store.create_chat("chat-a", "user-a", "test-agent")
    task_store.create_chat("task-run-1", "user-a", "test-agent")
    monkeypatch.setattr(routing, "resolve_hook_chat_id",
                        AsyncMock(side_effect={"sess-a": "chat-a", "sess-t": "task-run-1"}.get))
    terminal("bob-sub", chat_id="task-run-1", session="sess-t")
    assert "access_token" not in hook("sess-t")
    for sub, chat_id in (("", "chat-a"), ("bob-sub", "chat-other"), ("personal-assistant", "chat-a")):
        terminal(sub, chat_id=chat_id)
        assert "access_token" not in hook(), (sub, chat_id)
    terminal("bob-sub")._closing = True
    assert "access_token" not in hook()
    terminal("bob-sub")._closed = True
    assert "access_token" not in hook()
    terminal("bob-sub")
    people("chat-a", "bob-sub")
    assert "access_token" not in hook()
    assert preview._wopi_token_cache == {}


def test_a_resumed_task_names_the_person_who_resumed_it(temp_db, hook, people, monkeypatch):
    # An agent-scope run pushes with no person; a manager resumes its chat
    # and the turn runs as them, so the push names them.
    from api.hooks import routing
    task_store.create_chat("task-run-1", "agent::test-agent", "test-agent")
    monkeypatch.setattr(routing, "resolve_hook_chat_id",
                        AsyncMock(side_effect={"sess-t": "task-run-1"}.get))
    assert "access_token" not in hook("sess-t")
    people("task-run-1", "bob-sub", session="sess-t")
    assert _claims(hook("sess-t"))["user_sub"] == "bob-sub"


def test_a_takeover_switches_the_person(temp_db, hook, people, terminal):
    from api.hooks import preview
    from core.session import interactive_session
    task_store.create_chat("chat-a", "agent::test-agent", "test-agent")
    terminal("bob-sub")
    bobs = hook()
    # Vi takes over: Bob's session closes and leaves the registry ...
    interactive_session._sessions.pop("sess-a")
    assert "access_token" not in hook()
    # ... and Vi's re-warm runs the same chat under the same session id.
    terminal("vi-sub")
    vis = hook()
    assert (_claims(bobs)["user_sub"], _claims(vis)["user_sub"]) == ("bob-sub", "vi-sub")
    assert len(preview._wopi_token_cache) == 2


def test_a_takeover_during_the_push_keeps_the_token_from_the_next_person(
        temp_db, hook, people, terminal, monkeypatch):
    """The hook awaits after it mints (the version copy, its number, the
    download link): a takeover meanwhile starts closing Bob's terminal and
    hands the session id and its queue to the next person, so Bob's token
    stays off the push. A re-warm of Bob's own keeps it."""
    from services.media import preview_snapshots
    task_store.create_chat("chat-a", "agent::test-agent", "test-agent")
    real = preview_snapshots.stamp_and_cap
    meanwhile: list = []

    def _taken_over(*a, **kw):
        meanwhile.pop()()
        return real(*a, **kw)
    monkeypatch.setattr(preview_snapshots, "stamp_and_cap", _taken_over)
    bobs = terminal("bob-sub")
    meanwhile.append(lambda: setattr(bobs, "_closing", True))
    item = hook()
    assert "access_token" not in item and "token_person" not in item
    terminal("bob-sub")
    meanwhile.append(lambda: terminal("vi-sub"))
    assert "access_token" not in hook()
    terminal("bob-sub")
    meanwhile.append(lambda: terminal("bob-sub"))
    assert _claims(hook())["user_sub"] == "bob-sub"


def test_the_relay_admits_the_socket_of_the_person_at_the_terminal(temp_db, hook, terminal, monkeypatch):
    """End to end: the token a terminal push carries opens the editor socket
    for the person at the terminal (their sign-in cookie), and for nobody
    else."""
    import asyncio
    from urllib.parse import quote

    import auth.providers
    from api.media import collabora_proxy, wopi
    monkeypatch.setattr(auth.providers, "acting_role_of", lambda sub, agent: "editor")
    pat = task_store.create_local_user("pat@t.com", "Pat", "Pat", "member", "x")
    kim = task_store.create_local_user("kim@t.com", "Kim", "Kim", "member", "x")
    task_store.create_chat("chat-a", "agent::test-agent", "test-agent")
    terminal(pat)
    token = hook()["access_token"]
    src = quote("http://otodock-proxy:8400/wopi/files/YQ", safe="")
    scope = {"type": "websocket", "raw_path": b"/collabora/cool/ws",
             "query_string": f"WOPISrc={src}&access_token={token}".encode()}

    def opened_by(sub):
        cookie = tokens.mint(tokens.SIGN_IN, {"purpose": "session", "sub": sub, "jti": f"in-{sub}"},
                             ttl_s=3600)
        return asyncio.run(collabora_proxy._collabora_authorized(scope, {"session": cookie}))
    wopi._holder_answers.clear()
    try:
        assert opened_by(pat) == pat
        assert opened_by(kim) is None
    finally:
        wopi._holder_answers.clear()



@pytest.mark.parametrize("remote", [False, True])
def test_the_write_notes_the_next_document_before_it_announces_the_change(
    temp_db, hook, tmp_path, monkeypatch, remote,
):
    """A pane that mints on the write's file_updated opens the document of
    the push that follows (noted first), and that push reuses it, so the
    pane loads once. On a remote session the note comes before the push to
    the machine, the announcement after it."""
    from api.hooks import hooks as hooks_mod, lifecycle, preview
    from core.remote import remote_file_flow
    from services.notifications import notification_manager
    task_store.create_chat("chat-a", "user-a", "test-agent")
    first = hook()
    doc = tmp_path / "agents" / "test-agent" / "workspace" / "report.docx"
    doc.write_bytes(b"the agent's next write")
    order = []

    async def _broadcast(agent, rel, **kw):
        order.append(("file_updated", preview.pushed_generation(first["file_id"])))

    async def _push_back(sid, rel):
        order.append(("push_back", preview.pushed_generation(first["file_id"])))
        return True

    from auth.path_policy import SecurityContext
    monkeypatch.setattr(lifecycle, "verify_session_match_async", _pass_async)
    # The remote push is refused for a path the session may not write: an
    # editor writes the shared workspace, judged under the same agents root.
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", (tmp_path / "agents").resolve())
    monkeypatch.setattr(lifecycle, "get_session_security", lambda sid: SecurityContext(
        role="editor", username="u", agent="test-agent", is_admin_agent=False))
    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda sid: remote)
    monkeypatch.setattr(remote_file_flow, "push_back", _push_back)
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", _broadcast)
    app = FastAPI()
    app.include_router(hooks_mod.router)
    resp = TestClient(app).post("/v1/hooks/file-written", json={
        "session_id": "sess-a", "path": "test-agent/workspace/report.docx",
    }, headers={"Authorization": "Bearer test"})
    assert resp.status_code == 200
    noted = preview.pushed_generation(first["file_id"])
    assert noted > first["generation"]
    expected = [("push_back", noted)] if remote else []
    assert order == expected + [("file_updated", noted)]
    assert _opened(hook()) == ("test-agent/workspace/report.docx", noted)
    # The same bytes written again: no change, nothing announced.
    order.clear()
    TestClient(app).post("/v1/hooks/file-written", json={
        "session_id": "sess-a", "path": "test-agent/workspace/report.docx",
    }, headers={"Authorization": "Bearer test"})
    assert [o for o in order if o[0] == "file_updated"] == []
