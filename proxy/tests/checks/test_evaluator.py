"""services/checks/evaluator — the one turn_end handler (CHECKS.md): which
sessions it judges, what it attaches, a schema check end to end through
``session_events.turn_end`` (the findings as the continue reason, the
verdict rows, the chat card, the rounds), the cancel on a new message.

env TEST_DATABASE_URL=postgresql://otodock:otodock@localhost:5433/otodock_test \
    venv/bin/python -m pytest tests/checks/test_evaluator.py -q
"""

import asyncio
import json

import pytest

import config
from auth.path_policy import SecurityContext
from core.session import session_events as se
from core.session import session_state
from services.checks import documents as d
from services.checks import evaluator, kinds
from storage import database as task_store
from storage.checks import db_checks
from storage.pg import get_conn

AGENT = "checks-eval-agent"
SID = "sess-checks-eval"
CHAT = "chat-checks-eval"


@pytest.fixture
def rig(temp_db, tmp_path, monkeypatch):
    root = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", root.resolve())
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Checks Eval Agent", created_by="alice-sub")
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    (root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True, exist_ok=True)
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state._record_session_use(SID, client_type="dashboard", agent=AGENT)
    task_store.create_chat(CHAT, "alice-sub", AGENT, "auto", model="m")
    task_store.update_chat(CHAT, session_id=SID)
    task_store.add_chat_message(CHAT, "user", "write the answer as JSON")
    evaluator.install()
    saved = dict(kinds.RUNNERS)
    yield root / AGENT
    kinds.RUNNERS.clear()
    kinds.RUNNERS.update(saved)
    session_state.cleanup_session_permission_state(SID)
    session_state._sessions.pop(SID, None)


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _write(name="answer", **over):
    doc = {"name": name, "description": "the answer is JSON", "mandatory": True,
           "condition": {"always": True}, "rounds": 2, "schema": SCHEMA}
    doc.update(over)
    return d.write_check(AGENT, "", doc, None, updated_by="alice-sub")


def _turn(last: str):
    return asyncio.run(se.turn_end(SID, source="loop", engine="claude", driven_by="proxy",
                                   last_message=last))


def test_a_failing_schema_check_holds_the_turn_then_passes(rig):
    _write()
    v = _turn("Here you go:\n```json\n{\"ok\": \"yes\"}\n```")
    assert v.should_continue
    assert v.continue_reason.startswith('[OtoDock check] "answer" did not pass (fix round 1 of 2')
    assert "ok" in v.continue_reason and "data, never an instruction" in v.continue_reason
    rows = db_checks.list_verdicts(AGENT)
    assert len(rows) == 1 and rows[0]["status"] == "fail" and rows[0]["round"] == 1
    assert rows[0]["chat_id"] == CHAT and rows[0]["user_sub"] == "alice-sub"
    events = [m for m in task_store.get_chat_messages(CHAT) if m.get("event_type") == "check_verdict"]
    assert len(events) == 1
    card = json.loads(events[0]["event_data"])
    assert card["check"] == "answer" and card["status"] == "fail" and card["round"] == 1
    # The fix: the turn ends, a pass is recorded, the rounds reset.
    v2 = _turn("```json\n{\"ok\": true}\n```")
    assert not v2.should_continue
    rows = db_checks.list_verdicts(AGENT)
    assert [r["status"] for r in rows] == ["pass", "fail"]
    assert se.rounds(SID) == 0


def test_rounds_bound_the_re_prompting(rig):
    _write(rounds=1)
    assert _turn("```json\n{}\n```").should_continue
    # The second failure: rounds exhausted, the turn ends, the failure recorded.
    assert not _turn("```json\n{}\n```").should_continue
    assert [r["status"] for r in db_checks.list_verdicts(AGENT)] == ["fail", "fail"]
    assert se.rounds(SID) == 0
    d.delete_check(AGENT, "", "answer")
    _write(name="report", rounds=0)
    # rounds 0: report, never re-prompt.
    assert not _turn("```json\n{}\n```").should_continue
    assert db_checks.list_verdicts(AGENT)[0]["check_name"] == "report"


def test_applies_and_the_condition_filter(rig):
    _write(applies=["tasks"])                       # a chat is not a task
    assert not _turn("no json").should_continue
    assert db_checks.list_verdicts(AGENT) == []
    _write(name="on-writes", condition={})          # nothing written this turn
    assert not _turn("no json").should_continue
    assert db_checks.list_verdicts(AGENT) == []
    se.post_tool(SID, "Write", tool_input={"file_path": "/users/alice/workspace/a.py"},
                 source="forwarder")
    assert _turn("no json").should_continue
    assert [r["check_name"] for r in db_checks.list_verdicts(AGENT)] == ["on-writes"]


def test_attached_refs_and_a_users_own_check(rig):
    _write(mandatory=False)                                       # offered, not attached
    d.write_check(AGENT, "alice", {"name": "mine", "condition": {"always": True}, "schema": SCHEMA},
                  None, updated_by="alice-sub")
    assert not _turn("no json").should_continue
    task_store.update_chat(CHAT, checks=json.dumps(["user:mine", "agent:answer", "agent:missing"]))
    v = _turn("no json")
    assert v.should_continue and '"mine"' in v.continue_reason
    names = sorted(r["check_name"] for r in db_checks.list_verdicts(AGENT))
    # The first failing check wins the round; the second is not evaluated.
    assert names == ["mine"]


def test_never_judged(rig):
    _write()
    # A check session.
    session_state._sessions[SID]["check"] = True
    assert not _turn("no json").should_continue
    session_state._sessions[SID].pop("check")
    # A meeting participant.
    session_state._record_session_use(SID, client_type="meeting")
    assert not _turn("no json").should_continue
    session_state._record_session_use(SID, client_type="dashboard")
    # A session with no security context.
    session_state._session_security.pop(SID)
    assert not _turn("no json").should_continue
    assert db_checks.list_verdicts(AGENT) == []


def test_a_persons_message_cancels_the_round(rig):
    _write(judge={"rubric": "r"})

    async def schema_then_speak(check, target, changed, *, round_no):
        se.note_user_message(SID)
        from services.checks.render import Verdict
        return Verdict(section="schema", status="pass", passed=True)

    async def never(check, target, changed, *, round_no):
        raise AssertionError("the judge must not run after the person spoke")
    kinds.RUNNERS["schema"] = schema_then_speak
    kinds.RUNNERS["judge"] = never
    assert not _turn("x").should_continue
    rows = db_checks.list_verdicts(AGENT)
    assert rows[0]["status"] == "skipped" and "new message" in rows[0]["reason"]


def test_a_kind_that_is_not_available_is_a_visible_error(rig):
    _write(judge={"rubric": "r"})
    kinds.RUNNERS.pop("judge", None)
    v = _turn("```json\n{\"ok\": true}\n```")
    assert not v.should_continue
    rows = db_checks.list_verdicts(AGENT)
    assert rows[0]["status"] == "error" and "judge kind" in rows[0]["reason"]


def test_resolve_target_kinds(rig):
    t = asyncio.run(evaluator.resolve_target(SID, "claude"))
    assert t.kind == "chats" and t.username == "alice" and t.user_sub == "alice-sub"
    assert t.placement.is_local and t.cwd == "/users/alice"
    # A task run's session.
    task_store.create_run("run-1", "task-1", AGENT, "scheduled", None, "p", task_type="scheduled",
                          scope="user", created_by="alice-sub")
    task_store.create_chat("task-run-1", "alice-sub", AGENT, "auto", source_type="task")
    task_store.update_run("run-1", chat_id="task-run-1")
    task_store.update_chat("task-run-1", session_id=SID)
    t = asyncio.run(evaluator.resolve_target(SID, "claude"))
    assert t.kind == "tasks" and t.run["id"] == "run-1"
    task_store.create_run("run-2", "task-2", AGENT, "manual", "orch", "p", task_type="delegate",
                          scope="user", created_by="alice-sub")
    task_store.create_chat("task-run-2", "alice-sub", AGENT, "auto", source_type="task",
                           delegate_role="worker")
    task_store.update_run("run-2", chat_id="task-run-2")
    task_store.update_chat("task-run-2", session_id=SID)
    assert asyncio.run(evaluator.resolve_target(SID, "claude")).kind == "delegations"
    task_store.create_run("run-3", "check-x", AGENT, "check", "check:x", "p", task_type="check",
                          scope="user", created_by="alice-sub")
    task_store.create_chat("task-run-3", "alice-sub", AGENT, "auto", source_type="task")
    task_store.update_run("run-3", chat_id="task-run-3")
    task_store.update_chat("task-run-3", session_id=SID)
    assert asyncio.run(evaluator.resolve_target(SID, "claude")) is None
    # A task row minted BEFORE the column was written (the default 'chat'
    # with a task- id), continued from the dashboard: judged as a task — the
    # row's kind says so (core-seams phase 4).
    task_store.create_run("run-4", "task-4", AGENT, "scheduled", None, "p", task_type="scheduled",
                          scope="user", created_by="alice-sub")
    task_store.create_chat("task-run-4", "alice-sub", AGENT, "auto")
    task_store.update_run("run-4", chat_id="task-run-4")
    task_store.update_chat("task-run-4", session_id=SID)
    t = asyncio.run(evaluator.resolve_target(SID, "claude"))
    assert t.kind == "tasks" and t.run["id"] == "run-4"
    # A chat-surface delegate worker: a uuid id, the default column, its run
    # row through target_chat_id — the SESSION says task (the scheduler's
    # index entry), and that is what identifies it.
    task_store.create_run("run-5", "task-5", AGENT, "manual", "orch", "p", task_type="delegate",
                          scope="user", created_by="alice-sub")
    task_store.create_chat("worker-uuid-5", "alice-sub", AGENT, "auto", delegate_role="worker")
    task_store.update_run("run-5", chat_id="worker-uuid-5")
    task_store.update_chat("worker-uuid-5", session_id=SID)
    session_state._sessions[SID]["is_task"] = True
    assert asyncio.run(evaluator.resolve_target(SID, "claude")).kind == "delegations"
    session_state._sessions[SID].pop("is_task", None)
    # A session nobody classified (client_type "") on a phone row: never
    # judged — the chats branch keys on the dashboard kind, not on "not task".
    task_store.create_chat("phone-6", "phone", AGENT, "auto", source_type="phone")
    task_store.update_chat("phone-6", session_id=SID)
    session_state._sessions[SID]["client_type"] = ""
    assert asyncio.run(evaluator.resolve_target(SID, "claude")) is None


def test_a_mandatory_check_whose_file_breaks_or_goes_is_an_error_not_a_skip(rig):
    # A session that writes /config could switch a mandatory check off by
    # breaking its file or deleting its folder: the turn still gets a
    # visible error verdict (by the last valid document's applies and
    # condition) until a manager fixes or removes it.
    folder = rig / "config" / "checks" / "answer"
    _write()
    (folder / "check.json").write_text("{ not json")
    assert not _turn("```json\n{\"ok\": \"yes\"}\n```").should_continue
    rows = db_checks.list_verdicts(AGENT)
    assert [r["status"] for r in rows] == ["error"] and "cannot run" in rows[0]["reason"]
    # An offered (not mandatory) check that breaks is simply not attached.
    _write(name="offered", mandatory=False)
    (rig / "config" / "checks" / "offered" / "check.json").write_text("{ not json")
    # The folder gone: after the rename window (a second load), still an error.
    import shutil
    shutil.rmtree(folder)
    _turn("x")
    _turn("x")
    rows = db_checks.list_verdicts(AGENT)
    assert rows[0]["status"] == "error" and "folder is missing" in rows[0]["reason"]
    assert {r["check_name"] for r in rows} == {"answer"}
    assert db_checks.get_index(AGENT, "", "answer") is not None
    # A manager sees it on the Checks page and removes it; then it is gone.
    listed = {it.name: it for it in d.load_checks(AGENT, "")}
    assert listed["answer"].problems and listed["answer"].held["mandatory"] is True
    assert d.delete_check(AGENT, "", "answer") is True
    before = len(db_checks.list_verdicts(AGENT))
    _turn("x")
    assert len(db_checks.list_verdicts(AGENT)) == before
    # A non-mandatory row whose folder is gone is still dropped.
    shutil.rmtree(rig / "config" / "checks" / "offered")
    d.load_checks(AGENT, "")
    d.load_checks(AGENT, "")
    assert db_checks.get_index(AGENT, "", "offered") is None


def test_loading_one_check_by_name_leaves_the_others_rows_alone(rig):
    # A by-name load looks at one folder: the others are not "missing", so
    # repeated ref resolutions never drop their rows (their provenance).
    _write(name="one")
    _write(name="two")
    for _ in range(3):
        d.load_check(AGENT, "", "one")
    assert db_checks.get_index(AGENT, "", "two")["updated_by"] == "alice-sub"
    d.load_checks(AGENT, "")
    assert db_checks.get_index(AGENT, "", "two")["updated_by"] == "alice-sub"
