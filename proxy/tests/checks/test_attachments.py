"""Attachments and the loops (CHECKS.md "Governance", "The feedback loop"):
checks named on a task's create and edit, on a delegation's spawn and on an
app's button, copied onto the run's chat row; a failed check fails the run
with its report and rides a delegation's hand-back; run_check by hand.

env TEST_DATABASE_URL=postgresql://otodock:otodock@localhost:5433/otodock_test \
    venv/bin/python -m pytest tests/checks/test_attachments.py -q
"""

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_events as se
from core.session import session_state
from services.checks import documents as d
from services.checks import evaluator
from services.scheduler import scheduler, shared
from storage import database as task_store
from storage.agents import agent_store
from storage.checks import db_checks
from storage.pg import get_conn

AGENT = "att-agent"
SID = "sess-att"
CHAT = "chat-att"
SCHEMA = {"type": "object", "required": ["ok"]}


@pytest.fixture
def rig(temp_db, tmp_path, monkeypatch):
    root = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", root.resolve())
    agent_store.create_agent(AGENT, "Att Agent", created_by="alice-sub", collaborative=True,
                             default_scope="user")
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    d.write_check(AGENT, "", {"name": "answer", "condition": {"always": True}, "schema": SCHEMA,
                              "rounds": 1}, None, updated_by="alice-sub")
    d.write_check(AGENT, "", {"name": "must", "mandatory": True, "applies": ["tasks"],
                              "condition": {"always": True}, "schema": SCHEMA}, None,
                  updated_by="alice-sub")
    d.write_check(AGENT, "alice", {"name": "mine", "condition": {"always": True}, "schema": SCHEMA},
                  None, updated_by="alice-sub")
    monkeypatch.setattr(scheduler, "_register_task", lambda task: None)
    evaluator.install()
    yield root / AGENT
    session_state.cleanup_session_permission_state(SID)
    session_state._sessions.pop(SID, None)


def _client(user: UserContext):
    from api.tasks import delegation as delegation_api
    from api.tasks import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router)
    app.include_router(delegation_api.router)

    async def _u():
        return user
    app.dependency_overrides[get_current_user] = _u
    return TestClient(app)


def _alice(**over):
    base = dict(sub="alice-sub", email="alice@test.com", name="Alice", role="member",
                agents=[AGENT], agent_roles={AGENT: "manager"})
    base.update(over)
    return UserContext(**base)


def test_a_task_names_its_checks_on_create_and_edit(rig):
    c = _client(_alice())
    body = {"name": "t", "agent": AGENT, "prompt": "p", "schedule": "0 9 * * *",
            "notification_mode": "none", "checks": ["answer", "user:mine", "must"]}
    r = c.post("/v1/tasks/scheduled", json=body)
    assert r.status_code == 200, r.text
    row = task_store.get_dynamic_task(r.json()["task_id"])
    # The mandatory one needs no naming; the private one resolves to the caller's own.
    assert json.loads(row["checks"]) == ["agent:answer", "user:mine"]
    assert scheduler._row_to_task(row).checks == ["agent:answer", "user:mine"]
    r2 = c.post("/v1/tasks/scheduled", json={**body, "checks": ["nope"]})
    assert r2.status_code == 400 and "nope" in r2.json()["detail"]
    r3 = c.patch(f"/v1/tasks/{row['id']}", json={"checks": []})
    assert r3.status_code == 200
    assert task_store.get_dynamic_task(row["id"])["checks"] == ""
    r4 = c.post("/v1/tasks/one-time", json={"name": "o", "agent": AGENT, "prompt": "p",
                                            "delay_seconds": 60, "notification_mode": "none",
                                            "checks": ["answer"]})
    assert r4.status_code == 200
    assert json.loads(task_store.get_dynamic_task(r4.json()["task_id"])["checks"]) == ["agent:answer"]


def test_a_delegation_names_its_checks(rig, monkeypatch):
    from storage.mcp import mcp_store
    mcp_store.set_mcp_enabled("delegation-mcp", True)
    parent = str(uuid.uuid4())
    task_store.create_chat(parent, "alice-sub", AGENT)
    task_store.update_chat(parent, session_id="11111111-1111-1111-1111-111111111111")
    fired: list = []

    async def _fake(task, trigger_type="manual", trigger_source=None, prompt_override=None,
                    trigger_payload=None):
        fired.append(task)
        return "run-x"
    monkeypatch.setattr(scheduler, "trigger_task_now", _fake)
    c = _client(_alice(is_api_key=True, session_id="11111111-1111-1111-1111-111111111111", agent=AGENT))
    r = c.post("/v1/delegation/spawn", json={"name": "L", "prompt": "do", "surface": "task",
                                            "agent": AGENT, "source_agent": AGENT, "scope": "user",
                                            "checks": ["answer"]})
    assert r.status_code == 200, r.text
    assert fired[0].checks == ["agent:answer"]
    assert json.loads(task_store.get_dynamic_task(fired[0].id)["checks"]) == ["agent:answer"]


def test_the_runner_copies_the_checks_onto_the_runs_chat(rig, monkeypatch):
    from services.scheduler import runner
    monkeypatch.setattr(config, "get_cli_model", lambda *a, **k: "m")
    task = shared.TaskDefinition(id="dyn-c1", name="t", agent=AGENT, prompt="p", scope="user",
                                 created_by="alice-sub", checks=["agent:answer"])
    task_store.create_run("run-c1", "dyn-c1", AGENT, "scheduled", None, "p", task_type="scheduled",
                          scope="user", created_by="alice-sub")
    runner._create_task_chat_row("task-run-c1", "run-c1", task)
    assert json.loads(task_store.get_chat("task-run-c1")["checks"]) == ["agent:answer"]
    # A judge's chat is titled after the check.
    judge = shared.TaskDefinition(id="check-1", name="Check: answer — t", agent=AGENT, prompt="p",
                                  scope="user", created_by="alice-sub", task_type="check")
    task_store.create_run("run-j1", "check-1", AGENT, "check", "check:answer", "p", task_type="check",
                          scope="user", created_by="alice-sub")
    runner._create_task_chat_row("task-run-j1", "run-j1", judge)
    assert task_store.get_chat("task-run-j1")["title"] == "Check: answer — t"


def test_the_final_failure_and_the_latest_failure(rig):
    task_store.create_chat(CHAT, "alice-sub", AGENT, "auto")
    since = "2026-09-17T00:00:00+00:00"
    assert evaluator.final_failure(CHAT, since) is None
    common = dict(agent=AGENT, owner="", section="schema", passed=False, score=None,
                  findings=[{"location": "x", "severity": "error", "text": "t"}], reason="",
                  session_id=SID, chat_id=CHAT, run_id="", judge_run_id="", user_sub="alice-sub",
                  ran_on="local", engine="", model="", cost_usd=0.0, duration_ms=1, script_sha256="")
    db_checks.insert_verdict(check_name="answer", status="fail", summary="no json", round_no=1, **common)
    report = evaluator.final_failure(CHAT, since)
    assert report and "answer did not pass after 1 round" in report and "no json" in report
    latest = evaluator.latest_failure(CHAT)
    assert latest["check"] == "answer" and latest["findings"][0]["text"] == "t"
    # A later run on the same (reused) worker chat does not inherit it.
    assert evaluator.latest_failure(CHAT, "2999-01-01T00:00:00+00:00") is None
    assert evaluator.latest_failure(CHAT, since)["check"] == "answer"
    # A later pass on the same check clears it.
    db_checks.insert_verdict(check_name="answer", status="pass", summary="ok", round_no=2,
                             **{**common, "passed": True, "findings": []})
    assert evaluator.final_failure(CHAT, since) is None and evaluator.latest_failure(CHAT) is None


def test_a_button_carries_its_checks_in_the_signed_text_and_the_press_merges_them(rig, monkeypatch):
    from api.apps import app_actions
    from api.apps import manifest as _mf
    monkeypatch.setattr(_mf, "check_task_target", lambda *a, **k: "")
    good = [{"id": "go", "label": "Go", "type": "fire_task", "task_id": "dyn-x",
             "checks": ["answer", "answer"]}]
    text, err = _mf.validate_actions(good, AGENT, shared=True)
    assert err == "" and json.loads(text)[0]["checks"] == ["answer"]
    for bad in (["Bad Name"], ["a"] * 9, "answer"):
        text, err = _mf.validate_actions([{**good[0], "checks": bad}], AGENT, shared=True)
        assert text is None and "checks" in err
    # No checks key: the entry is as before (the signature of an old approval holds).
    text, err = _mf.validate_actions([{k: v for k, v in good[0].items() if k != "checks"}], AGENT,
                                     shared=True)
    assert "checks" not in json.loads(text)[0]
    assert app_actions.merge_checks(["user:mine", "agent:answer"], ["answer", "other"]) == [
        "user:mine", "agent:answer", "agent:other"]
    assert app_actions.merge_checks([], []) == []


def test_run_check_by_hand(rig):
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state._record_session_use(SID, client_type="dashboard", agent=AGENT)
    task_store.create_chat(CHAT, "alice-sub", AGENT, "auto", model="m")
    task_store.update_chat(CHAT, session_id=SID)
    task_store.add_chat_message(CHAT, "user", "answer as json")
    task_store.add_chat_message(CHAT, "assistant", "here: ```json\n{\"ok\": 1}\n```")
    out = asyncio.run(evaluator.run_by_hand(SID, "answer"))
    assert out["status"] == "pass" and out["check"] == "answer" and out["verdict_id"]
    task_store.add_chat_message(CHAT, "assistant", "no json")
    out = asyncio.run(evaluator.run_by_hand(SID, "user:mine"))
    assert out["status"] == "fail" and out["text"].startswith('[OtoDock check] "mine"')
    with pytest.raises(LookupError):
        asyncio.run(evaluator.run_by_hand(SID, "nope"))
    assert len(db_checks.list_verdicts(AGENT)) == 2
    assert se.rounds(SID) == 0


def _checks_client(user: UserContext):
    from api.checks import checks as checks_api
    app = FastAPI()
    app.include_router(checks_api.router)

    async def _u():
        return user
    app.dependency_overrides[get_current_user] = _u
    return TestClient(app)


def test_the_session_routes_attach_detach_and_guard_what_they_must(rig):
    # A session attaches an offered check to its own chat and detaches it;
    # a mandatory one is never detached; a task's or a delegation's run
    # never drops the checks its maker named.
    task_store.create_chat(CHAT, "alice-sub", AGENT, "auto", model="m")
    task_store.update_chat(CHAT, session_id=SID)
    c = _checks_client(_alice(session_id=SID, agent=AGENT))
    r = c.post("/v1/checks/attach", json={"ref": "answer"})
    assert r.status_code == 200 and r.json()["attached"] == ["agent:answer"]
    assert c.post("/v1/checks/detach", json={"ref": "must"}).status_code == 403
    r = c.post("/v1/checks/detach", json={"ref": "agent:answer"})
    assert r.status_code == 200 and r.json()["attached"] == []
    assert c.post("/v1/checks/attach", json={"ref": "user:nope"}).status_code == 404
    assert _checks_client(_alice()).post("/v1/checks/attach", json={"ref": "answer"}).status_code == 400
    # The run's chat of a task, and a chat-surface delegation worker.
    task_store.create_chat("task-run-att", "alice-sub", AGENT, "auto", model="m", source_type="task")
    task_store.update_chat("task-run-att", session_id="sess-task-att", checks=json.dumps(["agent:answer"]))
    r = _checks_client(_alice(session_id="sess-task-att", agent=AGENT)).post(
        "/v1/checks/detach", json={"ref": "agent:answer"})
    assert r.status_code == 403
    assert json.loads(task_store.get_chat("task-run-att")["checks"]) == ["agent:answer"]
    task_store.create_chat("worker-att", "alice-sub", AGENT, "auto", model="m")
    task_store.update_chat("worker-att", session_id="sess-worker-att", delegate_role="worker",
                           checks=json.dumps(["agent:answer"]))
    r = _checks_client(_alice(session_id="sess-worker-att", agent=AGENT)).post(
        "/v1/checks/detach", json={"ref": "agent:answer"})
    assert r.status_code == 403


def test_a_tasks_run_does_not_change_the_agents_checks(rig):
    # The manager's own task run holds a token with the manager's role:
    # the agent's checks and the spend cap still change only with a person
    # there (a chat's session or the dashboard).
    doc = {"doc": {"name": "must", "mandatory": False, "applies": ["tasks"], "condition": {"always": True},
                   "schema": SCHEMA}}
    task_store.create_chat("task-run-gov", "alice-sub", AGENT, "auto", model="m", source_type="task")
    task_store.update_chat("task-run-gov", session_id="sess-task-gov")
    run = _checks_client(_alice(session_id="sess-task-gov", agent=AGENT))
    assert run.put(f"/v1/agents/{AGENT}/checks/must", json=doc).status_code == 403
    assert run.delete(f"/v1/agents/{AGENT}/checks/must").status_code == 403
    assert run.patch(f"/v1/agents/{AGENT}/check-settings", json={"daily_cap_usd": None}).status_code == 403
    assert d.load_check(AGENT, "", "must").mandatory is True
    task_store.create_chat(CHAT, "alice-sub", AGENT, "auto", model="m")
    task_store.update_chat(CHAT, session_id=SID)
    chat = _checks_client(_alice(session_id=SID, agent=AGENT))
    assert chat.put(f"/v1/agents/{AGENT}/checks/must", json=doc).status_code == 200
    assert d.load_check(AGENT, "", "must").mandatory is False
    assert _checks_client(_alice()).patch(f"/v1/agents/{AGENT}/check-settings",
                                          json={"daily_cap_usd": 5}).status_code == 200
