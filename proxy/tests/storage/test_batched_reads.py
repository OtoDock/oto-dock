"""The batched ``= ANY(%s)`` reads behind the page-load routes.

The Active-now seed needs one chat row, one run row and one dynamic-task
row per live id, and the notification definitions view one user row per
user-scoped row. These helpers answer
the same maps in one query each, so the handlers can run as one executor
job. Each one answers ``{}`` for an empty list without touching the pool.

Run: cd proxy && venv/bin/pytest tests/storage/test_batched_reads.py -v
"""

import uuid

from storage import database as task_store
from storage.automation import notification_store
from storage.chat import db_chats
from storage.automation import db_tasks
from storage.pg import get_conn

AGENT = "agent-batched"


def _no_conn(monkeypatch, module):
    def boom():
        raise AssertionError("a query ran for an empty id list")
    monkeypatch.setattr(module, "get_conn", boom)


# --- chats -----------------------------------------------------------------

def test_chats_by_ids_maps_the_rows_that_exist(temp_db):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    task_store.create_chat(a, "user-alice", AGENT)
    task_store.create_chat(b, "agent::" + AGENT, AGENT)
    rows = task_store.get_chats_by_ids([a, "missing", b, a, ""])
    assert set(rows) == {a, b}
    assert rows[a]["user_sub"] == "user-alice"
    assert rows[b] == task_store.get_chat(b)


def test_chats_by_ids_with_no_ids_makes_no_query(temp_db, monkeypatch):
    _no_conn(monkeypatch, db_chats)
    assert task_store.get_chats_by_ids([]) == {}
    assert task_store.get_chats_by_ids(["", None]) == {}


# --- runs and dynamic-task names ------------------------------------------

def test_runs_by_ids_maps_the_rows_that_exist(temp_db):
    r1, r2 = f"run-{uuid.uuid4().hex[:12]}", f"run-{uuid.uuid4().hex[:12]}"
    task_store.create_run(r1, "t-1", AGENT, "schedule", None, "p", scope="agent",
                          created_by="user-alice")
    task_store.create_run(r2, "t-2", AGENT, "manual", None, "p", scope="user",
                          created_by="user-bob")
    rows = task_store.get_runs_by_ids([r1, "run-missing", r2, ""])
    assert set(rows) == {r1, r2}
    assert rows[r2]["scope"] == "user" and rows[r2]["created_by"] == "user-bob"
    assert rows[r1] == task_store.get_run(r1)


def test_runs_by_ids_with_no_ids_makes_no_query(temp_db, monkeypatch):
    _no_conn(monkeypatch, db_tasks)
    assert task_store.get_runs_by_ids([]) == {}


def test_dynamic_task_names_skip_rows_without_a_name(temp_db):
    task_store.create_dynamic_task("dyn-named", AGENT, "Nightly report", "p", "cli",
                                   "scheduled", "0 9 * * *", None, None, 3600,
                                   "user-alice", scope="agent")
    task_store.create_dynamic_task("dyn-blank", AGENT, "", "p", "cli",
                                   "scheduled", "0 9 * * *", None, None, 3600,
                                   "user-alice", scope="agent")
    names = task_store.get_dynamic_task_names(["dyn-named", "dyn-blank", "dyn-gone"])
    assert names == {"dyn-named": "Nightly report"}


def test_dynamic_task_names_with_no_ids_makes_no_query(temp_db, monkeypatch):
    _no_conn(monkeypatch, db_tasks)
    assert task_store.get_dynamic_task_names([]) == {}


# --- user display names ----------------------------------------------------

def test_display_names_follow_the_single_resolver(temp_db):
    with get_conn() as conn:
        conn.execute("UPDATE users SET display_name='Root' WHERE sub='user-admin'")
        conn.execute("UPDATE users SET display_name='', name='', username='viewer1' "
                     "WHERE sub='user-viewer'")
        conn.commit()
    subs = ["user-admin", "user-manager", "user-viewer", "user-nobody", ""]
    names = notification_store.resolve_subs_to_display_names(subs)
    assert names == {s: notification_store.resolve_sub_to_display_name(s)
                     for s in subs if s and s != "user-nobody"}
    assert names["user-admin"] == "Root"
    assert names["user-manager"] == "Manager User"
    assert names["user-viewer"] == "viewer1"
    assert "user-nobody" not in names


def test_display_names_with_no_subs_makes_no_query(temp_db, monkeypatch):
    _no_conn(monkeypatch, notification_store)
    assert notification_store.resolve_subs_to_display_names([]) == {}
