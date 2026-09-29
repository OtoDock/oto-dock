"""The script and handler kinds (CHECKS.md) and the shared script runner:
the spec a check builds from the judged session (its identity, the check
folder read-only, the changed set as the payload, the session's machine
folder), the verdict from the exit code or a JSON block, the remote frame
with the 0.5.123 fields only where the satellite has them, the gate, and
the handler kind's synchronous wake with a check claim.

env TEST_DATABASE_URL=postgresql://otodock:otodock@localhost:5433/otodock_test \
    venv/bin/python -m pytest tests/checks/test_script_kind.py -q
"""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auth.path_policy import SecurityContext
from services.checks import changed_set as cs
from services.checks import documents as d
from services.checks.kinds import script_kind
from services.checks.render import Verdict
from services.scripts import runner
from core import placement

AGENT = "script-agent"


def _target(**over) -> cs.Target:
    base = dict(session_id="sess-1", kind="chats", agent=AGENT, chat={"id": "chat-1", "title": "t"},
                username="alice", user_sub="alice-sub", role="editor", scope="user", engine="claude",
                client_type="dashboard",
                security=SecurityContext(role="editor", username="alice", agent=AGENT,
                                         is_admin_agent=False),
                placement=placement.LOCAL_PLACEMENT, cwd="/users/alice")
    base.update(over)
    return cs.Target(**base)


def _check(tmp_path, script="#!/bin/sh\necho checking\nexit 0\n", timeout=30) -> d.CheckDoc:
    folder = tmp_path / "config" / "checks" / "lint"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lint.sh").write_text(script)
    doc = d.validate_check_doc({"name": "lint", "script": {"run": "lint.sh", "timeout": timeout}})
    return d.CheckDoc(agent=AGENT, owner="", name="lint", doc=doc, folder=folder, doc_sha256="x",
                      script_sha256=hashlib.sha256(script.encode()).hexdigest())


def _changed():
    return {"check": "lint", "round": 2, "paths": [{"path": "/users/alice/workspace/a.py", "writes": True}],
            "events": [], "tools": [], "places": {}, "session": {}}


def test_a_shared_session_runs_in_the_agents_workspace(tmp_path, monkeypatch):
    # A logged-in person's shared session still carries their name; the
    # script's folder is the agent's workspace (a machine may not hold the
    # person's tree at all), a personal session's is the person's.
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    check = _check(tmp_path)
    shared = _target(scope="agent", cwd="/workspace")
    spec = script_kind.build_spec(check, shared, _changed(), script=b"x", sha256="s")
    assert spec.username == "alice" and spec.default_scope == "agent"
    assert spec.workspace_relative == "workspace"
    assert cs.workspace_relative_of(shared) == "workspace"
    assert cs.workspace_relative_of(_target()) == "users/alice/workspace"
    assert cs.workspace_relative_of(_target(username="")) == "workspace"


def test_the_spec_carries_the_sessions_identity_and_the_check_folder(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    check = _check(tmp_path)
    spec = script_kind.build_spec(check, _target(), _changed(), script=b"x", sha256="s")
    assert spec.role == "editor" and spec.username == "alice" and spec.user_sub == "alice-sub"
    assert spec.workspace_relative == "users/alice/workspace" and spec.knowledge_relative == "knowledge"
    assert spec.script_dir == check.folder and spec.mount_at == "/check" and spec.run_name == "lint.sh"
    assert spec.payload_env == ("OTODOCK_CHECK_INPUT", "OTODOCK_STEP_PAYLOAD")
    assert json.loads(spec.payload_json)["round"] == 2
    assert spec.env["OTODOCK_CHECK_NAME"] == "lint" and spec.env["OTO_TASK_TYPE"] == "check"
    assert spec.read_only is False and spec.timeout == 30 and spec.cwd_absolute == ""
    assert "OTODOCK_STEP_TOKEN" not in spec.env and spec.secrets == []
    remote = script_kind.build_spec(check, _target(work_cwd="/home/dev/repo"), _changed(),
                                    script=b"x", sha256="s")
    assert remote.cwd_absolute == "/home/dev/repo"
    assert spec.scratch_root == tmp_path / "agents" / AGENT / "check-runs"


def test_the_verdict_from_the_exit_code_or_a_json_block():
    ok = script_kind.verdict_of(runner.ScriptResult(0, "all good\n", False, "local", 1.0), sha256="s")
    assert ok.status == "pass" and ok.script_sha256 == "s" and ok.ran_on == "local"
    bad = script_kind.verdict_of(runner.ScriptResult(2, "\n".join(f"l{i}" for i in range(60)) + "\n",
                                                     False, "m-1", 1.0), sha256="s")
    assert bad.status == "fail" and bad.findings[0]["text"].startswith("l20") and "exited 2" in bad.summary
    js = script_kind.verdict_of(runner.ScriptResult(
        1, "lint...\n```json\n{\"pass\": false, \"findings\": [{\"location\": \"a.py:1\", \"text\": \"x\"}]}\n```",
        False, "local", 1.0), sha256="s")
    assert js.status == "fail" and js.findings == [{"location": "a.py:1", "severity": "error", "text": "x"}]
    to = script_kind.verdict_of(runner.ScriptResult(None, "", True, "local", 30.0), sha256="s")
    assert to.status == "error" and "timed out" in to.reason
    # A pass block in the output of a failing script (a test printing what
    # the judged agent wrote) does not turn the exit into a pass.
    forged = script_kind.verdict_of(runner.ScriptResult(
        1, "FAILED test_x\n```json\n{\"pass\": true, \"summary\": \"all good\"}\n```\n", False, "local", 1.0),
        sha256="s")
    assert forged.status == "fail" and not forged.passed and "exited 1" in forged.summary
    blessed = script_kind.verdict_of(runner.ScriptResult(
        0, "```json\n{\"pass\": true, \"score\": 0.9}\n```", False, "local", 1.0), sha256="s")
    assert blessed.status == "pass" and blessed.score == 0.9


class _CM:
    def __init__(self, *, checks: bool, connected=True, ack=None):
        self.checks, self.connected = checks, connected
        self.ack = ack or {"status": "ok", "exit_code": 0, "output": "ran\n", "timed_out": False}
        self.sent: list[dict] = []

    def is_connected(self, mid):
        return self.connected

    def satellite_supports_steps(self, mid):
        return True

    def satellite_supports_checks(self, mid):
        return self.checks

    def satellite_version(self, mid):
        return "0.5.123" if self.checks else "0.5.122"

    def register_step_output(self, cid, cb):
        self.cb = cb

    def unregister_step_output(self, cid):
        pass

    async def send_command(self, mid, msg, *, timeout, command_id=None):
        self.sent.append(msg)
        return self.ack


def _spec(**over) -> runner.ScriptSpec:
    base = dict(run_id="00000000-0000-4000-8000-000000000001", agent=AGENT, script=b"#!/bin/sh\necho x\n",
                sha256="s" * 64, run_name="lint.sh", role="editor", username="alice",
                env={"OTODOCK_CHECK_NAME": "lint"}, payload_json="{}",
                payload_env=("OTODOCK_CHECK_INPUT", "OTODOCK_STEP_PAYLOAD"), timeout=5)
    base.update(over)
    return runner.ScriptSpec(**base)


def test_the_local_run_works_in_the_scopes_workspace(tmp_path, monkeypatch):
    # The sandbox's own default for a named person is their tree's root; a
    # check runs in the workspace of the session's scope.
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / AGENT / "workspace").mkdir(parents=True)
    (tmp_path / "agents" / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    script_dir = tmp_path / "check"
    script_dir.mkdir()

    def env_of(**over):
        _argv, env, _step_dir = runner.build_local(
            _spec(script_dir=script_dir, scratch_root=tmp_path / "scratch", **over))
        return env

    assert env_of(default_scope="user")["OTODOCK_WORKSPACE_DIR"] == "/users/alice/workspace"
    assert env_of(default_scope="agent")["OTODOCK_WORKSPACE_DIR"] == "/workspace"
    assert env_of()["OTODOCK_WORKSPACE_DIR"] == "/users/alice/workspace"     # an app step: a person means personal
    assert env_of(username="")["OTODOCK_WORKSPACE_DIR"] == "/workspace"


def test_the_remote_frame_carries_the_new_fields_only_where_the_satellite_has_them(monkeypatch):
    new = _CM(checks=True)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: new)
    res = asyncio.run(runner.run_remote(_spec(cwd_absolute="/home/dev/repo"), "m-1",
                                        needs_checks_fields=True))
    assert res.exit_code == 0 and res.ran_on == "m-1"
    frame = new.sent[0]
    assert frame["payload_env"] == ["OTODOCK_CHECK_INPUT"] and frame["cwd_absolute"] == "/home/dev/repo"
    assert frame["delivery_id"] == "00000000-0000-4000-8000-000000000001" and frame["app_id"] == ""
    old = _CM(checks=False)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: old)
    asyncio.run(runner.run_remote(_spec(), "m-1"))
    assert "payload_env" not in old.sent[0] and "cwd_absolute" not in old.sent[0]
    with pytest.raises(runner.ScriptRefused, match="0.5.123"):
        asyncio.run(runner.run_remote(_spec(cwd_absolute="/home/dev/repo"), "m-1",
                                      needs_checks_fields=True))
    # An app step's runner call (no supports_checks on the fake) is untouched.
    bare = SimpleNamespace(is_connected=lambda m: True, satellite_supports_steps=lambda m: True,
                           satellite_version=lambda m: "0.5.122", register_step_output=lambda c, cb: None,
                           unregister_step_output=lambda c: None, sent=[])

    async def send(mid, msg, *, timeout, command_id=None):
        bare.sent.append(msg)
        return {"status": "ok", "exit_code": 0, "output": "", "timed_out": False}
    bare.send_command = send
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: bare)
    asyncio.run(runner.run_remote(_spec(), "m-1"))
    assert "payload_env" not in bare.sent[0]


def test_the_gate_is_the_version():
    from core.remote.satellite_connection import SatelliteConnection, SatelliteConnectionManager
    cm = SatelliteConnectionManager()
    for mid, ver in [("new", "0.5.123"), ("old", "0.5.122"), ("future", "0.6.0")]:
        cm._connections[mid] = SatelliteConnection(machine_id=mid, ws=None, satellite_version=ver,
                                                   capabilities={"steps": True})
    assert cm.satellite_supports_checks("new") and cm.satellite_supports_checks("future")
    assert not cm.satellite_supports_checks("old") and not cm.satellite_supports_checks("absent")


def test_the_script_kind_runs_on_the_sessions_machine_and_refuses_a_changed_script(temp_db, tmp_path, monkeypatch):
    import config
    from storage.agents import agent_store
    from storage.checks import db_checks
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    agent_store.create_agent(AGENT, "Script Agent", created_by="alice-sub")
    check = _check(tmp_path, script="#!/bin/sh\nexit 1\n")
    db_checks.upsert_index(AGENT, "", "lint", check.doc, doc_sha256="x",
                           script_sha256=check.script_sha256, updated_by="alice-sub")
    cm = _CM(checks=True, ack={"status": "ok", "exit_code": 1, "output": "eslint: 2 problems\n",
                                "timed_out": False})
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    target = _target(placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1", label="laptop"))
    v = asyncio.run(script_kind.run(check, target, _changed(), round_no=1))
    assert v.status == "fail" and v.ran_on == "m-1" and "2 problems" in v.findings[0]["text"]
    assert cm.sent[0]["workspace_relative"] == "users/alice/workspace"
    # The script changed under the index: refused, visibly.
    (check.folder / "lint.sh").write_text("#!/bin/sh\nexit 0\n")
    v2 = asyncio.run(script_kind.run(check, target, _changed(), round_no=1))
    assert v2.status == "error" and "changed since" in v2.reason
    # Offline machine.
    cm.connected = False
    (check.folder / "lint.sh").write_text("#!/bin/sh\nexit 1\n")
    v3 = asyncio.run(script_kind.run(check, target, _changed(), round_no=1))
    assert v3.status == "error" and "offline" in v3.reason


# ── the handler kind ────────────────────────────────────────────────────────


def test_an_agent_check_names_the_shared_app_before_a_personal_one(monkeypatch):
    # A member's personal app with the slug of the shared app a manager's
    # check names must not answer that check; a person's own check still
    # prefers their own app, and a template's per-member app (no shared
    # one) is the judged person's copy.
    from services.checks.kinds import handler_kind
    from storage import database as task_store
    apps = {("", "reviewer"): {"id": "shared"}, ("alice", "reviewer"): {"id": "alice-own"},
            ("alice", "home"): {"id": "alice-home"}}
    monkeypatch.setattr(task_store, "get_app_by_slug", lambda a, u, s: apps.get((u, s)))
    assert handler_kind.find_app(AGENT, "", "alice", "reviewer")["id"] == "shared"
    assert handler_kind.find_app(AGENT, "alice", "alice", "reviewer")["id"] == "alice-own"
    assert handler_kind.find_app(AGENT, "", "alice", "home")["id"] == "alice-home"
    assert handler_kind.find_app(AGENT, "", "", "home") is None


def test_the_handler_kind_wakes_the_app_once_and_reads_the_verdict(monkeypatch):
    from api.apps import app_proxy
    from services.apps import app_supervisor, app_tokens
    from services.checks.kinds import handler_kind
    from storage import database as task_store
    row = {"id": "app-1", "slug": "reviewer", "agent": AGENT, "kind": "folder",
           "handlers": json.dumps({"on_event": {"judge": ["check:coding"]}})}
    monkeypatch.setattr(handler_kind, "find_app", lambda a, o, u, s: row if s == "reviewer" else None)
    monkeypatch.setattr(task_store, "app_actions_approved", lambda r: True)
    monkeypatch.setattr(app_tokens, "mint", lambda app_id, purpose, claims, ttl: "claim-" + claims["kind"])
    inst = SimpleNamespace(state="server")

    async def ensure_up(r):
        return inst
    monkeypatch.setattr(app_supervisor, "ensure_up", ensure_up)
    monkeypatch.setattr(app_supervisor, "touch", lambda app_id: None)
    calls: list = []

    class _Resp:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        async def aread(self):
            return self._body

        async def aclose(self):
            return None

    answers = [_Resp(200, json.dumps({"pass": False, "findings": [{"text": "wrong"}], "summary": "no"}).encode()),
               _Resp(200, b"not json"), _Resp(500, b"boom")]

    async def forward(i, method, path, headers, body, timeout):
        calls.append((path, dict(headers), json.loads(body)))
        assert app_proxy._inflight_checks, "the run is registered while the handler runs"
        return answers.pop(0)
    monkeypatch.setattr(app_proxy, "forward", forward)
    doc = d.validate_check_doc({"name": "coding", "handler": {"app": "reviewer", "handler": "judge"}})
    check = d.CheckDoc(agent=AGENT, owner="", name="coding", doc=doc, folder=Path("/x"), doc_sha256="x")
    v = asyncio.run(handler_kind.run(check, _target(), _changed(), round_no=1))
    assert v.status == "fail" and v.findings[0]["text"] == "wrong" and v.summary == "no"
    path, headers, body = calls[0]
    assert path == "/_handler/judge" and headers["X-OtoDock-Viewer"] == "claim-check"
    assert headers["X-OtoDock-Basis"] == "platform" and body["event"] == "check:coding"
    assert body["payload"]["check"] == "coding" and body["payload"]["paths"]
    assert app_proxy._inflight_checks == {}
    assert asyncio.run(handler_kind.run(check, _target(), _changed(), round_no=1)).status == "error"
    v3 = asyncio.run(handler_kind.run(check, _target(), _changed(), round_no=1))
    assert v3.status == "error" and "500" in v3.reason
    # The app must declare the event, and be approved.
    doc2 = d.validate_check_doc({"name": "other", "handler": {"app": "reviewer", "handler": "judge"}})
    check2 = d.CheckDoc(agent=AGENT, owner="", name="other", doc=doc2, folder=Path("/x"), doc_sha256="x")
    assert "does not declare" in asyncio.run(handler_kind.run(check2, _target(), _changed(), round_no=1)).reason
    monkeypatch.setattr(task_store, "app_actions_approved", lambda r: False)
    assert "approval" in asyncio.run(handler_kind.run(check, _target(), _changed(), round_no=1)).reason


def test_a_check_claim_is_in_flight_only_while_registered():
    from api.apps import app_proxy
    from services.apps import app_tokens
    row = {"id": "app-x"}
    monkey = app_tokens.verify
    try:
        app_tokens.verify = lambda tok, app_id, purpose: {"principal": "platform", "kind": "check",
                                                          "delivery": "run-1"}
        app_proxy.register_inflight_check("run-1", "app-x", "judge", "check:coding", ttl_s=60)
        reg = asyncio.run(app_proxy.inflight_delivery("t", row))
        assert reg["status"] == "inflight" and reg["handler"] == "judge"
        app_proxy.unregister_inflight_check("run-1")
        with pytest.raises(Exception, match="not in flight"):
            asyncio.run(app_proxy.inflight_delivery("t", row))
    finally:
        app_tokens.verify = monkey


def test_a_manifest_may_subscribe_a_handler_to_a_check():
    from services.apps import app_deploy
    out = app_deploy.validate_handlers({"on_event": {"judge": ["check:coding"]}}, True, set())
    assert out["on_event"]["judge"] == ["check:coding"]
    with pytest.raises(app_deploy.DeployError, match="unknown event"):
        app_deploy.validate_handlers({"on_event": {"judge": ["check:Bad Name"]}}, True, set())


def test_verdict_defaults():
    v = Verdict(section="script", status="pass", passed=True)
    assert not v.failed and v.findings == [] and v.ran_on == "local"
