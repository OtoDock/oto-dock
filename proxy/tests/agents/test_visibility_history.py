"""Tests for Shared-only chat history + per-message attribution.

Shared-only agents collapse every assigned user's dashboard chats into ONE
shared list per agent (synthetic ``agent::{slug}`` owner). Every other mode is
per-user. Attribution of who sent each message lives on
``chat_messages.author_sub``.
"""

from __future__ import annotations

import pytest

from core.session.visibility import chat_history_owner, is_shared_chat_owner
from storage.agents import agent_store
from storage import database


def _seed_modes():
    agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
    agent_store.create_agent("po", "PO", collaborative=False, default_scope="user")
    agent_store.create_agent("ps", "PS", collaborative=True, default_scope="user")


# ---------------------------------------------------------------------------
# Owner resolution
# ---------------------------------------------------------------------------

def test_shared_only_owner_is_synthetic_and_user_independent(temp_db):
    _seed_modes()
    assert chat_history_owner("so", "alice-sub") == "agent::so"
    assert chat_history_owner("so", "bob-sub") == "agent::so"   # same for everyone


def test_other_modes_owner_is_the_real_user(temp_db):
    _seed_modes()
    assert chat_history_owner("ps", "alice-sub") == "alice-sub"
    assert chat_history_owner("po", "alice-sub") == "alice-sub"


def test_is_shared_chat_owner(temp_db):
    assert is_shared_chat_owner("agent::so") is True
    assert is_shared_chat_owner("alice-sub") is False
    assert is_shared_chat_owner("") is False


# ---------------------------------------------------------------------------
# One shared list vs per-user lists
# ---------------------------------------------------------------------------

def test_two_users_share_one_shared_only_chat_list(temp_db):
    _seed_modes()
    database.create_chat("c1", chat_history_owner("so", "alice-sub"), "so")
    alice = database.list_chats(chat_history_owner("so", "alice-sub"), agent="so")
    bob = database.list_chats(chat_history_owner("so", "bob-sub"), agent="so")
    assert [c["id"] for c in alice] == ["c1"]
    assert [c["id"] for c in bob] == ["c1"]   # bob sees alice's chat (shared)


def test_personal_only_lists_are_per_user(temp_db):
    _seed_modes()
    database.create_chat("a1", chat_history_owner("po", "alice-sub"), "po")
    database.create_chat("b1", chat_history_owner("po", "bob-sub"), "po")
    assert [c["id"] for c in database.list_chats("alice-sub", agent="po")] == ["a1"]
    assert [c["id"] for c in database.list_chats("bob-sub", agent="po")] == ["b1"]


# ---------------------------------------------------------------------------
# Unread markers (chat_reads keyed by the same owner identity as the listing)
# ---------------------------------------------------------------------------

def test_unread_lifecycle(temp_db):
    _seed_modes()
    database.create_chat("c1", "alice-sub", "po")
    # No response yet → never unread.
    assert database.list_chats("alice-sub", agent="po")[0]["unread"] is False
    # A response lands → unread until the owner identity reads. Timestamps
    # must be in the PAST — mark_chat_read stamps now(), and the unread expr
    # compares it against these.
    database.update_chat("c1", last_response_at="2020-01-01T00:00:00+00:00")
    assert database.list_chats("alice-sub", agent="po")[0]["unread"] is True
    database.mark_chat_read("c1", "alice-sub")
    assert database.list_chats("alice-sub", agent="po")[0]["unread"] is False
    # A newer response re-arms it (timestamps are compared, not flags).
    database.update_chat("c1", last_response_at="2126-01-01T00:00:00+00:00")
    assert database.list_chats("alice-sub", agent="po")[0]["unread"] is True


def test_shared_only_read_clears_for_everyone(temp_db):
    _seed_modes()
    owner = chat_history_owner("so", "alice-sub")   # agent::so
    database.create_chat("c1", owner, "so")
    database.update_chat("c1", last_response_at="2020-01-01T00:00:00+00:00")
    assert database.list_chats(owner, agent="so")[0]["unread"] is True
    # Alice opens the chat — the marker is keyed by the SHARED owner identity,
    # so bob's listing (same identity) clears too.
    database.mark_chat_read("c1", chat_history_owner("so", "alice-sub"))
    assert database.list_chats(chat_history_owner("so", "bob-sub"), agent="so")[0]["unread"] is False


def test_delete_chat_drops_read_markers(temp_db):
    _seed_modes()
    database.create_chat("c1", "alice-sub", "po")
    database.mark_chat_read("c1", "alice-sub")
    database.delete_chat("c1")
    from storage.pg import get_conn
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM chat_reads WHERE chat_id='c1'").fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# Per-message attribution
# ---------------------------------------------------------------------------

def test_author_sub_persisted_per_message(temp_db):
    _seed_modes()
    database.create_chat("c1", chat_history_owner("so", "alice-sub"), "so")
    database.add_chat_message("c1", "user", "hi from alice", author_sub="alice-sub")
    database.add_chat_message("c1", "user", "hi from bob", author_sub="bob-sub")
    msgs = database.get_chat_messages("c1")
    by_text = {m["content"]: m.get("author_sub") for m in msgs}
    assert by_text["hi from alice"] == "alice-sub"
    assert by_text["hi from bob"] == "bob-sub"


def test_author_sub_defaults_empty(temp_db):
    _seed_modes()
    database.create_chat("c1", "alice-sub", "ps")
    database.add_chat_message("c1", "assistant", "ok")
    msgs = database.get_chat_messages("c1")
    assert msgs[0].get("author_sub") == ""


# ---------------------------------------------------------------------------
# otodock-CLI sessions land in the scope-aware history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("agent,expected_owner", [
    ("so", "agent::so"),   # Shared-only → the synthetic shared owner
    ("ps", "admin-sub"),   # collaborative → the human's own history
])
async def test_otodock_fresh_chat_lands_in_scope_aware_history(
    temp_db, monkeypatch, agent, expected_owner,
):
    # A fresh `otodock` terminal session must persist its chat row under the
    # SAME owner the dashboard list + the --resume picker query
    # (visibility.chat_history_owner) — writing the human's sub made a
    # Shared-only agent's otodock chats invisible in the dashboard and
    # unresumable ("that chat was not found").
    _seed_modes()
    from core.session import otodock_session as osess
    from storage import remote_store

    async def fake_owner_for_machine(machine_id):
        return {"pairing_scope": "admin"}, "admin-sub", {"sub": "admin-sub"}

    async def fake_role(owner_sub, owner, agent_name):
        return "admin"

    monkeypatch.setattr(osess, "_owner_for_machine", fake_owner_for_machine)
    monkeypatch.setattr(osess, "_owner_role_for_agent", fake_role)
    monkeypatch.setattr(osess, "_model_for_path", lambda a, p, m: "claude-fable-5")
    monkeypatch.setattr(
        remote_store, "resolve_execution_target", lambda a, s, r: ("m1", ""),
    )

    class _Stop(Exception):
        pass

    captured = {}

    def fake_create_chat(chat_id, owner, *a, **k):
        captured["owner"] = owner
        raise _Stop  # stop before the heavy spawn path

    monkeypatch.setattr(database, "create_chat", fake_create_chat)

    with pytest.raises(_Stop):
        await osess.open_local_session(
            "m1", {"agent": agent, "cwd": "/home/admin/proj"},
        )
    assert captured["owner"] == expected_owner
