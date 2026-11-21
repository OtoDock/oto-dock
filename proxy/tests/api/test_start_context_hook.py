"""The start-context hook route (``POST /v1/hooks/start-context``) through
the real app and middleware: an engine's ``SessionStart`` hook asks for its
part of the start's riding part with its session's own token. The token's
session is the only one served: a token for another session is refused,
and a conversation id another session named gets the caller's own data.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import config as app_config
from app import app
from configuration import prompts
from core.session import session_state, start_context as sc
from services.memory import memory_file
from storage.agents import agent_store
from tests.conftest import live_session_token

client = TestClient(app)
AGENT = "sc-route"


@pytest.fixture(autouse=True)
def _fresh_record():
    yield
    sc._pending.clear()
    sc._answered.clear()
    sc._held.clear()
    sc._hook_cache.clear()
    session_state._session_start_args.clear()
    (app_config.SESSIONS_DIR / sc.HELD_FILE).unlink(missing_ok=True)
    sc._file["loaded"] = sc._file["dirty"] = False


def _seed() -> None:
    if not agent_store.agent_exists(AGENT):
        agent_store.create_agent(AGENT, "Route", default_scope="user")
    agent_dir = app_config.AGENTS_DIR / AGENT
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "agent.md").write_text("You are a test agent.")
    for user, fact in (("alice", "ALICE-ONLY-FACT"), ("bob", "BOB-ONLY-FACT")):
        memory_file.op_create(memory_file.scope_root(agent_dir, "user", user), "facts.md",
                              f"# Facts\n\n{fact}\n")


def _post(sid: str, token: str | None, *, conversation: str, source: str = "startup", part: int = 1):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/v1/hooks/start-context", headers=headers, json={
        "session_id": sid, "conversation": conversation, "source": source, "part": part, "parts": 1})


def test_a_session_reads_only_its_own_start(temp_db):
    _seed()
    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    prompts.build_agent_prompt(AGENT, username="alice", role="manager", session_id=alice)
    prompts.build_agent_prompt(AGENT, username="bob", role="manager", session_id=bob)
    alice_token = live_session_token(alice, AGENT, "user-admin")
    bob_token = live_session_token(bob, AGENT, "user-admin")

    mine = _post(alice, alice_token, conversation="conv-a")
    assert mine.status_code == 200 and "ALICE-ONLY-FACT" in mine.json()["text"]
    # Bob's token naming Alice's session is refused before anything renders.
    refused = _post(alice, bob_token, conversation="conv-a")
    assert refused.status_code == 403 and "ALICE" not in refused.text
    # Bob's token naming Alice's conversation, inside the cache window, gets
    # Bob's own start: the cache and the record are keyed by the session.
    own = _post(bob, bob_token, conversation="conv-a")
    assert own.status_code == 200
    assert "BOB-ONLY-FACT" in own.json()["text"] and "ALICE-ONLY-FACT" not in own.json()["text"]
    # No token, the master key: refused.
    assert _post(alice, None, conversation="conv-a").status_code == 401
    assert _post(alice, app_config.API_KEY, conversation="conv-a").status_code == 403


def test_the_part_renders_off_the_loop_and_writes_nothing_there(temp_db, monkeypatch):
    """``hook_part`` runs in a worker thread: it writes neither the security
    index nor the file. The route stores on the loop once the record
    changed, and a store that fails still hands the hook its part (the
    record already says the part's sections are held)."""
    _seed()
    sid = str(uuid.uuid4())
    prompts.build_agent_prompt(AGENT, username="alice", role="manager", session_id=sid)
    token = live_session_token(sid, AGENT, "user-admin")

    def no_write(*a, **k):
        raise AssertionError("hook_part wrote the record")
    real_set = session_state.set_start_args
    monkeypatch.setattr(session_state, "set_start_args", no_write)
    monkeypatch.setattr(sc, "flush", no_write)
    text, changed = sc.hook_part(sid, "conv-x", "startup", 1, 1)
    assert "ALICE-ONLY-FACT" in text and changed
    sc._hook_cache.clear()
    monkeypatch.setattr(session_state, "set_start_args", real_set)
    monkeypatch.undo()
    # Through the route, the start taken is stored (on the loop).
    sc._pending[sid] = None
    assert _post(sid, token, conversation="conv-z").status_code == 200
    assert session_state.get_start_args(sid)["_open"] is False
    sc._hook_cache.clear()

    def broken(_sid, *a, **k):
        raise RuntimeError("dictionary keys changed during iteration")
    monkeypatch.setattr(sc, "store", broken)
    served = _post(sid, token, conversation="conv-y")
    assert served.status_code == 200 and "ALICE-ONLY-FACT" in served.json()["text"]
