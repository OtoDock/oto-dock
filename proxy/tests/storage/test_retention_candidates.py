"""The retention sweep's candidate query takes the engines that keep NO
session files as a resolved list (the engine lane, phase 6): the SQL literal
``execution_path != 'direct-llm'`` became ``execution_path <> ALL(%s)`` over
what the caller reads off the registry (``behaviour.rebuilds_history_from_db``).
The semantics are the old ones: a fileless engine's chats are skipped, every
other path — the empty default included — is a candidate, an empty list
skips nothing."""

from datetime import datetime, timedelta, timezone

from storage import database as db


def _seed():
    db.create_chat("chat-claude", "u", "a", execution_path="claude-code-cli")
    db.update_chat("chat-claude", session_id="sid-claude")
    db.create_chat("chat-codex", "u", "a", execution_path="codex-cli")
    db.update_chat("chat-codex", codex_thread_id="tid-codex")
    db.create_chat("chat-direct", "u", "a", execution_path="direct-llm")
    db.update_chat("chat-direct", session_id="sid-direct")
    db.create_chat("chat-default", "u", "a")            # execution_path '' — the column's default
    db.update_chat("chat-default", session_id="sid-default")
    db.create_chat("chat-no-session", "u", "a", execution_path="claude-code-cli")
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def test_a_fileless_engines_chats_are_skipped_and_every_other_path_is_a_candidate(temp_db):
    cutoff = _seed()
    rows = db.get_retention_candidate_chats(cutoff, fileless_paths=["direct-llm"])
    assert {r["id"] for r in rows} == {"chat-claude", "chat-codex", "chat-default"}


def test_an_empty_list_skips_nothing(temp_db):
    cutoff = _seed()
    rows = db.get_retention_candidate_chats(cutoff, fileless_paths=[])
    assert {r["id"] for r in rows} == {"chat-claude", "chat-codex", "chat-direct", "chat-default"}


def test_the_sweep_resolves_the_list_from_the_registry(temp_db, monkeypatch):
    # The pass hands the store exactly the engines whose descriptor says they
    # rebuild from the DB — today the API engine alone.
    from services.infra import retention
    seen = {}

    def _capture(cutoff, *, fileless_paths):
        seen["fileless"] = list(fileless_paths)
        return []
    monkeypatch.setattr(retention.task_store, "get_retention_candidate_chats", _capture)
    retention._pass_aged_chats(30, retention.LiveSnapshot(), {}, dry_run=True)
    from core.session.session_manager import get_all_layers
    assert seen["fileless"] == [
        p for p, layer in get_all_layers().items()
        if layer.capabilities.behaviour.rebuilds_history_from_db
    ] == ["direct-llm"]


def test_flagging_runs_in_batches_and_keeps_updated_at(temp_db, monkeypatch):
    """The first retention run of a large install flags its chats in
    bounded batches (each inside the statement timeout), every chat is
    flagged, and the chat list's order (updated_at) is untouched."""
    from storage.chat import db_chats
    monkeypatch.setattr(db_chats, "_RETENTION_FLAG_BATCH", 2)
    ids = [f"chat-r{i}" for i in range(5)]
    for cid in ids:
        db.create_chat(cid, "u", "a", execution_path="claude-code-cli")
        db.update_chat(cid, session_id=f"sid-{cid}")
    before = {cid: db.get_chat(cid)["updated_at"] for cid in ids}
    cutoff = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert sorted(db.flag_chats_for_retention(ids, cutoff)) == sorted(ids)
    for cid in ids:
        row = db.get_chat(cid)
        assert row["session_id"] is None
        assert row["pending_history_seed"] == "retention"
        assert row["updated_at"] == before[cid]
