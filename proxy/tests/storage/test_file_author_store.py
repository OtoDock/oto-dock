"""Tests for storage/files/file_author_store.py — persisted platform last-writer.

Run individually (the conftest DB pool exhausts if test files run together):
    proxy/venv/bin/python -m pytest tests/storage/test_file_author_store.py
"""

from storage.files import file_author_store as fa

A = "agent-x"


def test_record_and_get():
    assert fa.get(A, "workspace/a.txt") is None
    fa.record(A, "workspace/a.txt", "alice")
    assert fa.get(A, "workspace/a.txt") == "alice"


def test_falsy_writer_ignored():
    fa.record(A, "workspace/a.txt", "alice")
    fa.record(A, "workspace/a.txt", "")  # keep last known, don't blank it
    assert fa.get(A, "workspace/a.txt") == "alice"


def test_record_upserts():
    fa.record(A, "workspace/a.txt", "alice")
    fa.record(A, "workspace/a.txt", "bob")
    assert fa.get(A, "workspace/a.txt") == "bob"


def test_clear():
    fa.record(A, "workspace/a.txt", "alice")
    fa.clear(A, "workspace/a.txt")
    assert fa.get(A, "workspace/a.txt") is None


def test_isolated_per_agent():
    fa.record(A, "p", "alice")
    fa.record("agent-y", "p", "bob")
    assert fa.get(A, "p") == "alice"
    assert fa.get("agent-y", "p") == "bob"
