"""Server-kick rescue at WS close (ws/dashboard._extract_server_kicks).

A chat's server-owned first turn rides the per-connection notify queue; the
queue dies with the connection, so a kick waiting behind a streaming turn was
silently lost on refresh/blip ("my first message never got answered"). The
close handler now drains the queue through this helper and runs every rescued
kick headless. These tests lock the extraction semantics: only `_server_kick`
items are kept (in order), everything else is dropped, the queue ends empty.

The helper is async since 2026-09-04: the queue drain itself is still one
synchronous step, only the delegate-result parking writes await the DB
executor (storage/pg.py's event-loop rule).
"""

import asyncio

import pytest

from ws.dashboard import _extract_server_kicks


def _kick(cid: str) -> dict:
    return {"type": "_server_kick", "chat_id": cid, "session_id": f"s-{cid}",
            "text": "hello", "images": [], "files": []}


@pytest.mark.asyncio
async def test_extracts_only_kicks_in_order():
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"type": "notification", "data": {}})
    q.put_nowait(_kick("c1"))
    q.put_nowait({"type": "bg_nudge", "chat_id": "x"})
    q.put_nowait(_kick("c2"))
    q.put_nowait("garbage-non-dict")

    kicks = await _extract_server_kicks(q)

    assert [k["chat_id"] for k in kicks] == ["c1", "c2"]
    assert all(k["type"] == "_server_kick" for k in kicks)
    assert q.empty()  # non-kick items dropped, exactly as a dead queue did


@pytest.mark.asyncio
async def test_empty_queue():
    q: asyncio.Queue = asyncio.Queue()
    assert await _extract_server_kicks(q) == []
    assert q.empty()


@pytest.mark.asyncio
async def test_kick_payload_preserved():
    q: asyncio.Queue = asyncio.Queue()
    payload = {"type": "_server_kick", "chat_id": "c9", "session_id": "s9",
               "text": "first prompt", "images": [{"x": 1}], "files": [{"f": 2}]}
    q.put_nowait(payload)
    kicks = await _extract_server_kicks(q)
    assert kicks == [payload]


def _result(cid: str) -> dict:
    return {"type": "task_result_prompt", "chat_id": cid,
            "result_prompt": "result!", "task_id": "t", "task_name": "n",
            "delegate_agent": "a", "output_text": "o", "status": "completed"}


@pytest.mark.asyncio
async def test_task_result_prompt_is_parked_off_loop(monkeypatch):
    """An undrained delegate result is persisted (event row + durable wake)
    through the DB executor, never on the loop thread; the wake names the
    closing socket's person at their role on the chat's agent, read there
    too."""
    import threading
    from auth import providers
    from storage import database as task_store

    loop_ident = threading.get_ident()
    seen: dict[str, object] = {}

    def _add(cid, role, text, **kw):
        seen["add_thread"] = threading.get_ident()
        seen["event_type"] = kw.get("event_type")
        return 1

    def _chat(cid):
        seen["chat_thread"] = threading.get_ident()
        return {"id": cid, "agent": "a1"}

    def _role(sub, agent, **kw):
        seen["role_thread"] = threading.get_ident()
        return "editor" if (sub, agent) == ("user-ann", "a1") else "viewer"

    def _park(cid, prompt, **kw):
        seen["park_thread"] = threading.get_ident()
        seen["park"] = (cid, prompt, kw)
        return True

    monkeypatch.setattr(task_store, "add_chat_message", _add)
    monkeypatch.setattr(task_store, "get_chat", _chat)
    monkeypatch.setattr(providers, "acting_role_of", _role)
    monkeypatch.setattr(task_store, "append_pending_delegate_wake", _park)

    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(_result("c1"))
    q.put_nowait(_kick("c2"))

    kicks = await _extract_server_kicks(q, person="user-ann")

    assert [k["chat_id"] for k in kicks] == ["c2"]
    assert seen["event_type"] == "delegate_result"
    assert seen["park"] == ("c1", "result!", {"person": "user-ann", "role": "editor"})
    for key in ("add_thread", "chat_thread", "role_thread", "park_thread"):
        assert seen[key] != loop_ident, key


@pytest.mark.asyncio
async def test_a_parked_result_with_no_person_stores_none(monkeypatch):
    from storage import database as task_store
    parked: list = []
    monkeypatch.setattr(task_store, "add_chat_message", lambda *a, **kw: 1)
    monkeypatch.setattr(task_store, "append_pending_delegate_wake",
                        lambda cid, prompt, **kw: parked.append((cid, prompt, kw)) or True)
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(_result("c1"))
    assert await _extract_server_kicks(q) == []
    assert parked == [("c1", "result!", {"person": "", "role": ""})]


@pytest.mark.asyncio
async def test_a_parked_result_is_stored_as_the_socket_person(temp_db):
    """The stored record carries the person and their role on the agent, so
    the redelivery sweep runs the result as them."""
    from storage.identity import db_users
    temp_db.create_chat("c-ann", "agent::a1", "a1")
    db_users.add_user_agent("user-viewer", "a1", "editor", "user-admin")
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(_result("c-ann"))
    await _extract_server_kicks(q, person="user-viewer")
    assert temp_db.claim_pending_wake_records("c-ann") == [
        {"prompt": "result!", "person": "user-viewer", "role": "editor", "by": ""}]


@pytest.mark.asyncio
async def test_a_parked_result_keeps_its_files_and_ending(temp_db):
    """The close rescue's event row is rebuilt through the same helper as the
    socket rung's: the typed ending, the verdict and the worker's files ride."""
    import json
    temp_db.create_chat("c-files", "user-ann", "a1")
    item = {**_result("c-files"), "reason": "limit", "resets_at": "2026-10-01T15:00:00+00:00",
            "files": [{"path": "workspace/inbox/a/r.md", "bytes": 1}],
            "files_skipped": [], "verdict": {"check": "k", "round": 1, "summary": "s"}}
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(item)
    await _extract_server_kicks(q)
    rows = [m for m in temp_db.get_chat_messages("c-files") if m.get("event_type") == "delegate_result"]
    data = json.loads(rows[-1]["event_data"])
    assert data["reason"] == "limit" and data["resets_at"] == "2026-10-01T15:00:00+00:00"
    assert data["files"] == [{"path": "workspace/inbox/a/r.md", "bytes": 1}]
    assert data["verdict"]["check"] == "k" and data["files_skipped"] == []
