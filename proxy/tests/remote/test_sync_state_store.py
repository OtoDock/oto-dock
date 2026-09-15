"""Tests for storage/files/sync_state_store.py — the per-(machine, agent, file) merge base.

Run individually (the conftest DB pool exhausts if test files run together):
    proxy/venv/bin/python -m pytest tests/remote/test_sync_state_store.py
"""

from storage.files import sync_state_store as ss

M, A = "machine-1", "agent-x"


def test_record_many_and_load():
    ss.record_synced_many(M, A, [
        ("workspace/a.txt", "sha256:aa", 111.0),
        ("users/x/b.txt", "sha256:bb", 222.0),
    ])
    base = ss.load_for_machine_agent(M, A)
    assert base == {
        "workspace/a.txt": ("sha256:aa", 111.0),
        "users/x/b.txt": ("sha256:bb", 222.0),
    }


def test_record_one_upserts():
    ss.record_one(M, A, "workspace/a.txt", "sha256:aa", 1.0)
    ss.record_one(M, A, "workspace/a.txt", "sha256:bb", 2.0)  # upsert
    assert ss.load_for_machine_agent(M, A)["workspace/a.txt"] == ("sha256:bb", 2.0)


def test_get_one():
    assert ss.get_one(M, A, "missing") is None
    ss.record_one(M, A, "workspace/a.txt", "sha256:aa", 5.0)
    assert ss.get_one(M, A, "workspace/a.txt") == ("sha256:aa", 5.0)


def test_clear_one():
    ss.record_one(M, A, "workspace/a.txt", "sha256:aa", 1.0)
    ss.clear_one(M, A, "workspace/a.txt")
    assert ss.get_one(M, A, "workspace/a.txt") is None


def test_isolated_per_machine_and_agent():
    ss.record_one(M, A, "p", "h1", 1.0)
    ss.record_one("machine-2", A, "p", "h2", 1.0)
    ss.record_one(M, "agent-y", "p", "h3", 1.0)
    assert ss.load_for_machine_agent(M, A) == {"p": ("h1", 1.0)}


def test_record_many_empty_is_noop():
    ss.record_synced_many(M, A, [])
    assert ss.load_for_machine_agent(M, A) == {}


def test_agents_for_machine():
    ss.record_one(M, "agent-1", "p", "h", 1.0)
    ss.record_one(M, "agent-2", "p", "h", 1.0)
    ss.record_one("machine-2", "agent-3", "p", "h", 1.0)
    assert ss.agents_for_machine(M) == {"agent-1", "agent-2"}
    assert ss.agents_for_machine("machine-2") == {"agent-3"}
    assert ss.agents_for_machine("absent") == set()
