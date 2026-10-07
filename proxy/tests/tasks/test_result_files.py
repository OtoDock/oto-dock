"""Worker results carry files (PROJECTS.md "Result files").

A delegated worker attaches deliverables to its result with the delegation
MCP's ``attach_result_files``: ``POST /v1/delegation/attach_result_files``
resolves the RUNNING delegate run from the caller's session token alone,
computes the destination from the run (the delegating chat's workspace by the
delivery's own who-wakes rule), copies the files with ``safe_fs`` into
``inbox/<worker agent>/``, records one row per file on the run, and the
delivery names the rows in the callback, the event and the frame.

Run: cd proxy && python -m pytest tests/tasks/test_result_files.py -v
"""

from __future__ import annotations

import pytest

from storage import database as task_store
from storage.automation import db_result_files, run_status


RUN = "run-rf0000000001"
OTHER_RUN = "run-rf0000000002"


def _run(run_id: str = RUN, agent: str = "worker-a") -> None:
    task_store.create_run(run_id, "dyn-rf1", agent, "manual", "deleg", "p",
                          task_type="delegate", scope="user", created_by="user-1")
    task_store.update_run(run_id, status=run_status.RUNNING, session_id=f"sid-{run_id}")


class TestStore:
    def test_rows_are_recorded_and_listed_in_order(self, temp_db):
        _run()
        db_result_files.record(RUN, "head", [
            {"path": "a.md", "landed_path": "workspace/inbox/worker-a/a.md",
             "ws_path": "inbox/worker-a/a.md", "bytes": 3, "status": db_result_files.LANDED},
            {"path": "gone.md", "status": db_result_files.SKIPPED, "reason": "not in your workspace"},
        ])
        rows = db_result_files.list_for_run(RUN)
        assert [(r["path"], r["status"], r["bytes"], r["reason"]) for r in rows] == [
            ("a.md", "landed", 3, ""), ("gone.md", "skipped", 0, "not in your workspace")]
        assert rows[0]["target_agent"] == "head"
        assert rows[0]["landed_path"] == "workspace/inbox/worker-a/a.md"
        assert db_result_files.count_for_run(RUN) == 2
        assert db_result_files.list_for_run(OTHER_RUN) == []

    def test_a_named_path_again_replaces_its_refusal(self, temp_db):
        _run()
        db_result_files.record(RUN, "head", [
            {"path": "report.md", "status": db_result_files.SKIPPED, "reason": "not in your workspace"},
            {"path": "other.md", "status": db_result_files.SKIPPED, "reason": "symlink"},
            {"path": "out/deep/l", "status": db_result_files.SKIPPED, "reason": "symlink"},
            {"path": "outer.md", "status": db_result_files.SKIPPED, "reason": "symlink"},
        ])
        assert db_result_files.count_skipped_under(RUN, ["report.md", "out", "never-named.md"]) == 2
        assert db_result_files.drop_skipped_under(RUN, ["report.md", "out", "never-named.md"]) == 2
        assert [r["path"] for r in db_result_files.list_for_run(RUN)] == ["other.md", "outer.md"]
        db_result_files.drop_skipped_under(RUN, ["outer.md"])
        assert [r["path"] for r in db_result_files.list_for_run(RUN)] == ["other.md"]
        # A landed row is never dropped by a later call naming the same path.
        db_result_files.record(RUN, "head", [
            {"path": "other.md", "landed_path": "workspace/inbox/worker-a/other.md",
             "ws_path": "inbox/worker-a/other.md", "bytes": 1, "status": db_result_files.LANDED}])
        assert db_result_files.drop_skipped_under(RUN, ["other.md"]) == 1
        assert [r["status"] for r in db_result_files.list_for_run(RUN)] == ["landed"]

    def test_named_paths_are_the_same_tree_rows(self, temp_db):
        _run()
        db_result_files.record(RUN, "worker-a", [
            {"path": "reports/q3.md", "landed_path": "users/alice/workspace/reports/q3.md",
             "ws_path": "reports/q3.md", "bytes": 9, "status": db_result_files.NAMED},
            {"path": "x.md", "landed_path": "workspace/inbox/w/x.md", "status": db_result_files.LANDED},
        ])
        assert db_result_files.named_paths(RUN) == {"users/alice/workspace/reports/q3.md"}

    def test_the_rows_leave_with_their_run(self, temp_db):
        from storage.pg import get_conn
        _run()
        db_result_files.record(RUN, "head", [
            {"path": "a.md", "status": db_result_files.SKIPPED, "reason": "symlink"}])
        with get_conn() as conn:
            conn.execute("DELETE FROM task_runs WHERE id = %s", (RUN,))
            conn.commit()
        assert db_result_files.list_for_run(RUN) == []

    def test_a_row_needs_its_run(self, temp_db):
        import psycopg
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db_result_files.record("run-missing", "head", [
                {"path": "a.md", "status": db_result_files.SKIPPED, "reason": "x"}])


# ───────────────────────────────────────────────────────────────────────────
# The attach: the route, the destination, the copy
# ───────────────────────────────────────────────────────────────────────────

import os  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import config  # noqa: E402
from auth.path_policy import SecurityContext  # noqa: E402
from auth.providers import UserContext  # noqa: E402
from core.session import session_state  # noqa: E402
from services.delegation import file_transfer, result_files  # noqa: E402
from storage.agents import agent_store  # noqa: E402
from storage.identity import db_users  # noqa: E402
from storage.mcp import mcp_store  # noqa: E402
from tests.tasks.test_send_files import _FakeSatellite, _endpoint_client, _remote, flow_reset  # noqa: E402,F401

WORKER = "rf-worker"
HEAD = "rf-head"
SHARED = "rf-shared"
ALICE = "user-rf-alice"
SID = "sid-rf-worker"
HEAD_CHAT = "chat-rf-head"
TASK = "dyn-rf1"


def _person(sub: str, username: str, *, platform_role: str = "member", **agent_roles: str) -> None:
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login, username) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (sub, f"{username}@test.com", username, platform_role, now, now, username))
        conn.commit()
    for agent, role in agent_roles.items():
        db_users.add_user_agent(sub, agent.replace("_", "-"), role, "user-admin")


def _worker_tree(agent: str, username: str = "alice") -> Path:
    ws = config.get_agent_dir(agent) / "users" / username / "workspace" if username \
        else config.get_agent_dir(agent) / "workspace"
    (ws / "data").mkdir(parents=True, exist_ok=True)
    (ws / "report.md").write_text("the report")
    (ws / "data" / "a.csv").write_text("a,b")
    (ws / "data" / "b.txt").write_text("bee")
    (ws / "data" / "torn.partial").write_text("x")
    (ws / "link").symlink_to("/etc/hostname")
    (ws / ".codex").mkdir(exist_ok=True)
    (ws / ".codex" / "state.json").write_text("{}")
    return ws


def _delegation(*, worker: str = WORKER, head: str = HEAD, head_chat_owner: str = ALICE,
                scope: str = "user", created_by: str = ALICE, callback: bool = True,
                head_chat: str = HEAD_CHAT) -> None:
    """A delegating chat on ``head`` and a RUNNING delegate run of ``worker``
    reporting to it, as the spawn route leaves them."""
    task_store.create_chat(head_chat, head_chat_owner, head)
    task_store.update_chat(head_chat, session_id="sid-rf-head")
    task_store.create_dynamic_task(
        TASK, worker, "lane", "p", "cli", "delegate", None, None, None, 1800, created_by,
        on_complete_agent=head if callback else None,
        on_complete_prompt="[tpl]" if callback else None,
        on_complete_session_id="sid-rf-head" if callback else None,
        on_complete_chat_id=head_chat if callback else None,
        scope=scope, target_chat_id="chat-rf-worker")
    task_store.create_run(RUN, TASK, worker, "manual", head, "p",
                          task_type="delegate", scope=scope, created_by=created_by)
    task_store.update_run(RUN, status=run_status.RUNNING, session_id=SID, chat_id="chat-rf-worker")


def _ctx(monkeypatch, *, agent: str = WORKER, username: str = "alice", scope: str = "user") -> None:
    monkeypatch.setitem(session_state._session_security, SID, SecurityContext(
        role="editor", username=username, agent=agent, is_admin_agent=False,
        session_scope=scope))


def _worker_user(agent: str = WORKER, sid: str = SID) -> UserContext:
    return UserContext(sub=ALICE, email="a@test.com", name="Alice", role="member",
                       agents=[WORKER, HEAD], agent_roles={WORKER: "editor", HEAD: "editor"},
                       is_api_key=True, session_id=sid, agent=agent)


@pytest.fixture
def rig(temp_db, monkeypatch):
    import shutil
    for slug in (WORKER, HEAD, SHARED):
        shutil.rmtree(config.get_agent_dir(slug), ignore_errors=True)
    agent_store.create_agent(WORKER, "W", collaborative=True, default_scope="user")
    agent_store.create_agent(HEAD, "H", collaborative=True, default_scope="user")
    agent_store.create_agent(SHARED, "S", collaborative=False, default_scope="agent")
    mcp_store.set_mcp_enabled("delegation-mcp", True)
    _person(ALICE, "alice", rf_worker="editor", rf_head="editor", rf_shared="editor")
    (config.get_agent_dir(HEAD) / "users" / "alice" / "workspace").mkdir(parents=True, exist_ok=True)
    (config.get_agent_dir(SHARED) / "workspace").mkdir(parents=True, exist_ok=True)
    _worker_tree(WORKER)
    pushes: list = []
    monkeypatch.setattr(file_transfer, "schedule_inbox_fanout",
                        lambda agent, rels, *, origin_user_sub: pushes.append((agent, rels, origin_user_sub)))
    return pushes


def _attach(client, paths, dest_dir=""):
    return client.post("/v1/delegation/attach_result_files",
                       json={"paths": paths, "dest_dir": dest_dir})


def _inbox(agent=HEAD, username="alice") -> Path:
    base = config.get_agent_dir(agent)
    ws = base / "users" / username / "workspace" if username else base / "workspace"
    return ws / "inbox" / WORKER


class TestAttachRoute:
    def test_files_and_a_directory_land_under_the_inbox(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        r = _attach(_endpoint_client(_worker_user()), ["report.md", "data"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["same_tree"] is False and body["target_agent"] == HEAD
        assert sorted(f["ws_path"] for f in body["files"]) == [
            f"inbox/{WORKER}/data/a.csv", f"inbox/{WORKER}/data/b.txt", f"inbox/{WORKER}/report.md"]
        assert body["skipped"] == []
        assert (_inbox() / "report.md").read_text() == "the report"
        assert (_inbox() / "data" / "a.csv").read_text() == "a,b"
        assert not (_inbox() / "data" / "torn.partial").exists()
        rows = db_result_files.list_for_run(RUN)
        assert [(r["status"], r["path"]) for r in rows] == [
            ("landed", "report.md"), ("landed", "data/a.csv"), ("landed", "data/b.txt")]
        assert rows[0]["landed_path"] == f"users/alice/workspace/inbox/{WORKER}/report.md"
        assert rows[0]["bytes"] == len("the report")
        # The landed rels fan out to the delegator's machines, as the person.
        assert rig == [(HEAD, [r["landed_path"] for r in rows], ALICE)]

    def test_a_second_attach_lands_beside_the_first(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        client = _endpoint_client(_worker_user())
        assert _attach(client, ["report.md"]).status_code == 200
        r = _attach(client, ["report.md"])
        assert r.json()["files"][0]["ws_path"] == f"inbox/{WORKER}/report_1.md"
        assert (_inbox() / "report.md").read_text() == (_inbox() / "report_1.md").read_text()
        assert r.json()["attached"] == 2

    def test_dest_dir_is_a_subfolder_of_the_slot(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        client = _endpoint_client(_worker_user())
        r = _attach(client, ["report.md"], dest_dir="round-2")
        assert r.status_code == 200
        assert (_inbox() / "round-2" / "report.md").exists()
        assert r.json()["files"][0]["ws_path"] == f"inbox/{WORKER}/round-2/report.md"
        assert _attach(client, ["report.md"], dest_dir="../x").status_code == 400
        assert _attach(client, ["report.md"], dest_dir=".hidden").status_code == 400

    def test_links_engine_state_and_missing_paths_are_skipped_with_reasons(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        r = _attach(_endpoint_client(_worker_user()), ["link", ".codex/state.json", "missing.md"])
        assert r.status_code == 200
        assert r.json()["files"] == []
        assert r.json()["skipped"] == [
            {"path": "link", "reason": "symlink"},
            {"path": ".codex/state.json", "reason": "engine state is not a deliverable"},
            {"path": "missing.md", "reason": "not in your workspace"},
        ]
        assert not _inbox().exists()
        assert [r["status"] for r in db_result_files.list_for_run(RUN)] == ["skipped"] * 3

    def test_a_link_in_a_middle_component_lists_nothing_behind_it(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        ws = config.get_agent_dir(WORKER) / "users" / "alice" / "workspace"
        (ws / "d").mkdir()
        (ws / "d" / "own.txt").write_text("mine")
        (ws / "d" / "esc").symlink_to("/etc")
        client = _endpoint_client(_worker_user())
        r = _attach(client, ["d", "d/esc/hostname"])
        assert r.status_code == 200
        body = r.json()
        assert [f["ws_path"] for f in body["files"]] == [f"inbox/{WORKER}/d/own.txt"]
        assert body["skipped"] == [{"path": "d/esc", "reason": "symlink"},
                                   {"path": "d/esc/hostname", "reason": "symlink"}]
        assert not any("hostname" in p or "passwd" in p
                       for p in os.listdir(_inbox() / "d"))

    def test_a_malformed_path_refuses_the_whole_call(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        r = _attach(_endpoint_client(_worker_user()), ["report.md", "../escape"])
        assert r.status_code == 400
        assert not _inbox().exists()
        assert db_result_files.list_for_run(RUN) == []

    def test_a_refusal_is_replaced_by_a_later_landing(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        client = _endpoint_client(_worker_user())
        assert _attach(client, ["late.md"]).json()["skipped"][0]["reason"] == "not in your workspace"
        (config.get_agent_dir(WORKER) / "users" / "alice" / "workspace" / "late.md").write_text("now")
        assert _attach(client, ["late.md"]).status_code == 200
        assert [(r["status"], r["path"]) for r in db_result_files.list_for_run(RUN)] == [
            ("landed", "late.md")]

    def test_a_cookie_or_a_plain_session_is_refused(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        cookie = UserContext(sub=ALICE, email="a@test.com", name="Alice", role="member",
                             agents=[WORKER], agent_roles={WORKER: "editor"})
        assert _attach(_endpoint_client(cookie), ["report.md"]).status_code == 400
        r = _attach(_endpoint_client(_worker_user(sid="sid-plain-chat")), ["report.md"])
        assert r.status_code == 409
        assert r.json()["detail"] == result_files.NO_RUN
        # Another agent's token on the worker's session id finds no run.
        r = _attach(_endpoint_client(_worker_user(agent=HEAD)), ["report.md"])
        assert r.status_code == 409

    def test_a_run_that_ended_is_refused(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        task_store.update_run(RUN, status=run_status.COMPLETED)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 409 and r.json()["detail"] == result_files.NO_RUN

    def test_a_run_without_a_callback_is_refused(self, rig, monkeypatch):
        _delegation(callback=False)
        _ctx(monkeypatch)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 409 and r.json()["detail"] == result_files.NO_CALLBACK

    def test_two_live_runs_on_one_session_refuse(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        task_store.create_run(OTHER_RUN, TASK, WORKER, "manual", HEAD, "p",
                              task_type="delegate", scope="user", created_by=ALICE)
        task_store.update_run(OTHER_RUN, status=run_status.RUNNING, session_id=SID)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 409 and r.json()["detail"] == result_files.TWO_RUNS

    def test_the_caps_refuse_the_whole_call(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        client = _endpoint_client(_worker_user())
        monkeypatch.setattr(result_files, "max_per_call", lambda: 1)
        r = _attach(client, ["data"])
        assert r.status_code == 413 and "max 1 per call" in r.json()["detail"]
        assert not _inbox().exists()
        assert db_result_files.list_for_run(RUN) == []
        monkeypatch.setattr(result_files, "max_per_call", lambda: 20)
        monkeypatch.setattr(result_files, "max_per_run", lambda: 3)
        assert _attach(client, ["data"]).status_code == 200          # 2 rows
        r = _attach(client, ["report.md", "link"])                   # 2 more would be 4
        assert r.status_code == 413 and "would hold 4" in r.json()["detail"]
        assert _attach(client, ["report.md"]).status_code == 200     # 3 rows
        r = _attach(client, ["report.md"])
        assert r.status_code == 413 and "cap of 3" in r.json()["detail"]

    def test_the_destination_follows_the_chat_not_the_worker(self, rig, monkeypatch):
        """An agent-scope worker's files land in the delegating person's own
        tree on the delegating agent."""
        _delegation(scope="agent")
        _ctx(monkeypatch, username="", scope="agent")
        _worker_tree(WORKER, username="")
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 200, r.text
        assert (_inbox(HEAD, "alice") / "report.md").exists()
        assert r.json()["files"][0]["landed_path"].startswith("users/alice/workspace/inbox/")

    def test_a_shared_only_delegator_lands_in_its_shared_tree(self, rig, monkeypatch):
        from core.session.visibility import shared_chat_owner
        _delegation(head=SHARED, head_chat_owner=shared_chat_owner(SHARED))
        _ctx(monkeypatch)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 200, r.text
        assert (config.get_agent_dir(SHARED) / "workspace" / "inbox" / WORKER / "report.md").exists()
        assert r.json()["files"][0]["landed_path"] == f"workspace/inbox/{WORKER}/report.md"
        assert rig[0][2] == ""  # the shared tree: no person on the push

    def test_a_person_who_lost_the_agent_copies_nothing(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        db_users.set_user_agents(ALICE, [WORKER], "user-admin")
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 409 and "no longer holds" in r.json()["detail"]
        assert not _inbox().exists()

    def test_a_shared_mount_needs_the_workspace_tier(self, rig, monkeypatch):
        """A person's own chat on an agent that became Shared-only mounts the
        shared tree: a viewer's result may not write it."""
        agent_store.update_agent(HEAD, collaborative=False, default_scope="agent")
        db_users.set_user_agents(ALICE, [WORKER, HEAD], "user-admin",
                                 agent_roles={WORKER: "editor", HEAD: "viewer"})
        _delegation()
        _ctx(monkeypatch)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 409 and "shared workspace" in r.json()["detail"]

    def test_the_same_tree_names_the_files_and_copies_nothing(self, rig, monkeypatch):
        _delegation(head=WORKER)
        _ctx(monkeypatch)
        r = _attach(_endpoint_client(_worker_user()), ["report.md", "data", "link"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["same_tree"] is True and body["files"] == []
        assert [n["ws_path"] for n in body["named"]] == ["report.md", "data/a.csv", "data/b.txt"]
        assert body["named"][0]["landed_path"] == "users/alice/workspace/report.md"
        assert body["skipped"] == [{"path": "link", "reason": "symlink"}]
        assert not (config.get_agent_dir(WORKER) / "users" / "alice" / "workspace" / "inbox").exists()
        assert rig == []
        # Named twice stays one row.
        _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert [r["status"] for r in db_result_files.list_for_run(RUN)] == ["named"] * 3 + ["skipped"]

    def test_the_source_mount_falls_back_to_the_run_identity(self, rig):
        """No live security context (a session the index dropped): the run's
        own identity names the tree."""
        _delegation()
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 200, r.text
        assert (_inbox() / "report.md").exists()


@pytest.mark.usefixtures("flow_reset")
class TestRemoteWorker:
    def test_a_same_turn_write_is_read_through_before_the_copy(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        sat = _FakeSatellite({"users/alice/workspace/today.md": b"fresh"}, name="drill-sat")
        with _remote(sat, agent=WORKER):
            r = _attach(_endpoint_client(_worker_user()), ["today.md", "nowhere.md"])
        assert r.status_code == 200, r.text
        assert (_inbox() / "today.md").read_bytes() == b"fresh"
        assert "users/alice/workspace/today.md" in sat.pulls
        assert r.json()["skipped"] == [{
            "path": "nowhere.md",
            "reason": "not in your workspace, and the remote machine 'drill-sat' could not provide it",
        }]

    def test_an_offline_machine_keeps_the_platform_copy(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        sat = _FakeSatellite({}, online=False)
        monkeypatch.setattr("storage.remote_store.get_remote_machine",
                            lambda mid: {"id": mid, "name": "my laptop"})
        with _remote(sat, agent=WORKER):
            r = _attach(_endpoint_client(_worker_user()), ["report.md", "nowhere.md"])
        assert r.status_code == 200, r.text
        assert (_inbox() / "report.md").read_text() == "the report"
        assert r.json()["skipped"][0]["reason"].endswith("'my laptop' could not provide it")


class TestDeliveryLists:
    def test_the_note_names_landed_and_skipped_rows(self, temp_db):
        _run()
        db_result_files.record(RUN, "head", [
            {"path": "report.md", "landed_path": "users/u/workspace/inbox/w/report.md",
             "ws_path": "inbox/w/report.md", "bytes": 12600, "status": "landed"},
            {"path": "data/a.csv", "landed_path": "users/u/workspace/inbox/w/data/a.csv",
             "ws_path": "inbox/w/data/a.csv", "bytes": 300, "status": "landed"},
            {"path": "x.md", "status": "skipped", "reason": "symlink"},
        ])
        files, skipped, note = result_files.delivery_lists(RUN)
        assert files == [{"path": "users/u/workspace/inbox/w/report.md", "bytes": 12600},
                         {"path": "users/u/workspace/inbox/w/data/a.csv", "bytes": 300}]
        assert skipped == [{"path": "x.md", "reason": "symlink"}]
        assert note == ("[Files the worker attached, relative to your workspace: "
                        "inbox/w/report.md (12.3 KB), inbox/w/data/a.csv (300 B). "
                        "Not attached: x.md (symlink).]")

    def test_named_rows_read_already_in_your_workspace(self, temp_db):
        _run()
        db_result_files.record(RUN, "worker-a", [
            {"path": "reports/q3.md", "landed_path": "users/u/workspace/reports/q3.md",
             "ws_path": "reports/q3.md", "bytes": 2 * 1024 * 1024, "status": "named"}])
        files, skipped, note = result_files.delivery_lists(RUN)
        assert files == [{"path": "users/u/workspace/reports/q3.md", "bytes": 2 * 1024 * 1024}]
        assert note == "[Files the worker attached, already in your workspace: reports/q3.md (2.0 MB).]"

    def test_only_skipped_rows_say_so(self, temp_db):
        _run()
        db_result_files.record(RUN, "head", [
            {"path": "a.md", "status": "skipped", "reason": "not in your workspace"}])
        files, skipped, note = result_files.delivery_lists(RUN)
        assert files == [] and note == "[Not attached: a.md (not in your workspace).]"

    def test_no_rows_name_nothing(self, temp_db):
        _run()
        assert result_files.delivery_lists(RUN) == ([], [], "")
        assert result_files.delivery_lists("") == ([], [], "")


class TestRefusalsReplaced:
    def test_a_directory_named_again_replaces_the_refusals_under_it(self, rig, monkeypatch):
        """Every skipped row at or under a path named again is replaced by this
        call's rows, so a re-attached directory never lists an entry twice."""
        _delegation()
        _ctx(monkeypatch)
        ws = config.get_agent_dir(WORKER) / "users" / "alice" / "workspace"
        (ws / "out").mkdir()
        (ws / "out" / "a.txt").write_text("a")
        (ws / "out" / "l").symlink_to("/etc/hostname")
        os.mkfifo(ws / "out" / "gone.fifo")
        client = _endpoint_client(_worker_user())
        assert _attach(client, ["out"]).status_code == 200
        (ws / "out" / "gone.fifo").unlink()
        assert _attach(client, ["out"]).status_code == 200
        _, skipped, _ = result_files.delivery_lists(RUN)
        assert [s["path"] for s in skipped] == ["out/l"]
        assert [r["ws_path"] for r in db_result_files.list_for_run(RUN)
                if r["status"] == "landed"] == [f"inbox/{WORKER}/out/a.txt", f"inbox/{WORKER}/out/a_1.txt"]

    def test_a_retry_at_the_cap_replaces_its_refusal(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        monkeypatch.setattr(result_files, "max_per_run", lambda: 1)
        client = _endpoint_client(_worker_user())
        assert _attach(client, ["late.md"]).json()["skipped"][0]["reason"] == "not in your workspace"
        (config.get_agent_dir(WORKER) / "users" / "alice" / "workspace" / "late.md").write_text("now")
        r = _attach(client, ["late.md"])
        assert r.status_code == 200, r.text
        assert [(x["status"], x["path"]) for x in db_result_files.list_for_run(RUN)] == [("landed", "late.md")]
        # The cap still holds for a new path.
        assert _attach(client, ["report.md"]).status_code == 413


class TestCopyEdges:
    def test_a_file_over_the_size_cap_is_skipped(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_BYTES", 2)
        r = _attach(_endpoint_client(_worker_user()), ["report.md", "data/a.csv"])
        assert r.status_code == 200, r.text
        assert [s["reason"] for s in r.json()["skipped"]] == ["over the 0 MB cap", "over the 0 MB cap"]
        assert r.json()["files"] == [] and not [f for f in _inbox().rglob("*") if f.is_file()]

    def test_a_person_without_a_username_copies_nothing(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: None)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 409 and "no username" in r.json()["detail"]
        assert not _inbox().exists()

    def test_an_unreadable_directory_is_a_row_not_a_failure(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        ws = config.get_agent_dir(WORKER) / "users" / "alice" / "workspace"
        (ws / "d" / "locked").mkdir(parents=True)
        (ws / "d" / "locked" / "x.txt").write_text("x")
        (ws / "d" / "ok.txt").write_text("ok")
        (ws / "d" / "locked").chmod(0)
        try:
            r = _attach(_endpoint_client(_worker_user()), ["d", "d/locked"])
        finally:
            (ws / "d" / "locked").chmod(0o755)
        assert r.status_code == 200, r.text
        assert [f["ws_path"] for f in r.json()["files"]] == [f"inbox/{WORKER}/d/ok.txt"]
        assert r.json()["skipped"] == [{"path": "d/locked", "reason": "not readable"},
                                       {"path": "d/locked", "reason": "not readable"}]

    def test_no_free_name_left_is_a_row(self, rig, monkeypatch):
        _delegation()
        _ctx(monkeypatch)
        inbox = _inbox()
        inbox.mkdir(parents=True)
        (inbox / "report.md").write_text("taken")
        for i in range(1, 100):
            (inbox / f"report_{i}.md").write_text("taken")
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.json()["skipped"] == [{"path": "report.md", "reason": "too many files of that name"}]

    def test_a_race_on_the_name_takes_the_next_free_one(self, rig, monkeypatch):
        from services.infra import safe_fs
        _delegation()
        _ctx(monkeypatch)
        real = safe_fs.copy_file_beneath
        calls: list[str] = []

        def racy(src_root, src_rel, dst_root, dst_rel, **kw):
            calls.append(dst_rel)
            if len(calls) == 1:
                raise FileExistsError(dst_rel)
            return real(src_root, src_rel, dst_root, dst_rel, **kw)
        monkeypatch.setattr(safe_fs, "copy_file_beneath", racy)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 200, r.text
        assert len(calls) == 2 and (_inbox() / "report.md").read_text() == "the report"

    def test_a_full_bucket_ends_the_batch(self, rig, monkeypatch):
        import errno
        from services.infra import safe_fs
        _delegation()
        _ctx(monkeypatch)

        def full(*a, **kw):
            raise OSError(errno.ENOSPC, "no space")
        monkeypatch.setattr(safe_fs, "copy_file_beneath", full)
        r = _attach(_endpoint_client(_worker_user()), ["report.md", "data"])
        assert r.status_code == 200, r.text
        assert r.json()["files"] == []
        assert {s["reason"] for s in r.json()["skipped"]} == {"no room in the target's bucket"}
        assert len(r.json()["skipped"]) == 3

    def test_a_landed_rel_is_pushed_under_the_upload_barrier(self, rig, monkeypatch):
        import asyncio as _asyncio
        from core.remote import upload_inflight
        from services.remote import workspace_fanout
        _delegation()
        _ctx(monkeypatch)
        monkeypatch.setattr(file_transfer, "schedule_inbox_fanout",
                            file_transfer.__dict__["schedule_inbox_fanout"].__wrapped__
                            if hasattr(file_transfer.schedule_inbox_fanout, "__wrapped__")
                            else _real_schedule_inbox_fanout())
        pushed: list = []
        tracked: list = []

        async def fake_push(agent, rel, source, **kw):
            pushed.append((agent, rel, kw.get("include_idle"), kw.get("origin_user_sub")))
        monkeypatch.setattr(workspace_fanout, "has_fanout_candidates", lambda *a, **kw: True)
        monkeypatch.setattr(workspace_fanout, "fan_out_write", fake_push)

        def track(agent_slug, coro):
            tracked.append(agent_slug)
            return _asyncio.ensure_future(coro)
        monkeypatch.setattr(upload_inflight, "track", track)
        r = _attach(_endpoint_client(_worker_user()), ["report.md"])
        assert r.status_code == 200, r.text
        assert tracked == [HEAD]
        assert pushed == [(HEAD, f"users/alice/workspace/inbox/{WORKER}/report.md", True, ALICE)]


def _real_schedule_inbox_fanout():
    import importlib.util
    spec = importlib.util.find_spec("services.delegation.file_transfer")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh.schedule_inbox_fanout


class TestRunLock:
    def test_attaches_of_one_run_serialize_and_the_lock_leaves_with_the_last(self):
        import asyncio as _asyncio
        order: list[str] = []

        async def holder(tag: str, hold: float):
            async with result_files.run_lock("run-lock"):
                order.append(f"{tag}:in")
                await _asyncio.sleep(hold)
                order.append(f"{tag}:out")

        async def run():
            first = _asyncio.create_task(holder("a", 0.05))
            await _asyncio.sleep(0.01)
            second = _asyncio.create_task(holder("b", 0.01))
            await _asyncio.sleep(0.02)
            third = _asyncio.create_task(holder("c", 0.0))
            await _asyncio.gather(first, second, third)

        _asyncio.run(run())
        assert order == ["a:in", "a:out", "b:in", "b:out", "c:in", "c:out"]
        assert "run-lock" not in result_files._locks
