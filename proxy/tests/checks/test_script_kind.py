"""The script and handler kinds (CHECKS.md) and the shared script runner:
the spec a check builds from the judged session (its identity, the check
folder read-only, the changed set as the payload, the session's machine
folder), the verdict from the exit code or a JSON block, the remote frame
with its fields, and the handler kind's synchronous wake with a check
claim.

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
from core import placement
from services.checks import changed_set as cs, documents as d
from services.checks.kinds import script_kind
from services.checks.render import Verdict
from services.scripts import runner

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
    def __init__(self, *, connected=True, ack=None, own=(), shared=()):
        self.connected = connected
        self.ack = ack or {"status": "ok", "exit_code": 0, "output": "ran\n", "timed_out": False}
        self.sent: list[dict] = []
        self.own = set(own)
        self.shared = set(shared)

    def is_compute(self, mid):
        return mid in self.own or mid in self.shared

    def shares_folders(self, mid):
        return mid in self.shared

    def owns_copy(self, mid):
        return mid in self.own

    def is_connected(self, mid):
        return self.connected

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


def test_a_run_on_a_compute_machine_works_in_the_scopes_workspace(tmp_path, monkeypatch):
    # The sandbox's own default for a named person is their tree's root; a
    # check runs in the workspace of the session's scope.
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / AGENT / "workspace").mkdir(parents=True)
    (tmp_path / "agents" / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    script_dir = tmp_path / "agents" / AGENT / "check"
    script_dir.mkdir()

    def workspace_of(**over):
        frame, _step_dir = runner.compute_frame(
            _spec(script_dir=script_dir, scratch_root=tmp_path / "agents" / AGENT / "scratch", **over))
        assert frame["sandbox"]["cwd"] == frame["env"]["OTODOCK_WORKSPACE_DIR"]
        return frame["sandbox"]["cwd"]

    assert workspace_of(default_scope="user") == "/users/alice/workspace"
    assert workspace_of(default_scope="agent") == "/workspace"
    assert workspace_of() == "/users/alice/workspace"     # an app step: a person means personal
    assert workspace_of(username="") == "/workspace"


def test_the_remote_frame_carries_the_payload_env_and_the_cwd(monkeypatch):
    cm = _CM()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    res = asyncio.run(runner.run_remote(_spec(cwd_absolute="/home/dev/repo"), "m-1"))
    assert res.exit_code == 0 and res.ran_on == "m-1"
    frame = cm.sent[0]
    assert frame["payload_env"] == ["OTODOCK_CHECK_INPUT"] and frame["cwd_absolute"] == "/home/dev/repo"
    assert frame["delivery_id"] == "00000000-0000-4000-8000-000000000001" and frame["app_id"] == ""
    # An app step's spec names no extra payload env and no absolute cwd.
    asyncio.run(runner.run_remote(_spec(payload_env=("OTODOCK_STEP_PAYLOAD",)), "m-1"))
    assert "payload_env" not in cm.sent[1] and "cwd_absolute" not in cm.sent[1]


def test_the_script_kind_runs_on_the_sessions_machine_and_refuses_a_changed_script(temp_db, tmp_path, monkeypatch):
    import config
    from storage.agents import agent_store
    from storage.checks import db_checks
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    agent_store.create_agent(AGENT, "Script Agent", created_by="alice-sub")
    check = _check(tmp_path / "agents" / AGENT, script="#!/bin/sh\nexit 1\n")
    db_checks.upsert_index(AGENT, "", "lint", check.doc, doc_sha256="x",
                           script_sha256=check.script_sha256, updated_by="alice-sub")
    cm = _CM(ack={"status": "ok", "exit_code": 1, "output": "eslint: 2 problems\n",
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


def _serving(monkeypatch, machine):
    """The compute machine that serves the platform's place (None: none)."""
    from services.apps import app_compute
    monkeypatch.setattr(app_compute, "place", lambda: {"id": machine} if machine else None)


def _agents_check(tmp_path, monkeypatch):
    """A check whose folder sits where a real one does, in its agent's own."""
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    return _check(tmp_path / "agents" / AGENT)


def _nothing_starts_here(monkeypatch):
    async def _spawn(*argv, **kw):
        raise AssertionError("the platform started a process for the script")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


def test_a_check_of_a_turn_on_the_platforms_place_runs_on_the_compute_machine(tmp_path, monkeypatch):
    # The turn ran on the compute machine that serves the platform's place:
    # its check's script runs there too, in the sandbox the platform
    # describes, never in the proxy. Every source of that sandbox is inside
    # the agent's own folder (a machine refuses any other), the script never
    # travels, and the card still says the platform: a compute machine is no
    # place a person picked.
    check = _agents_check(tmp_path, monkeypatch)
    cm = _CM(shared={"c-1"})
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    _serving(monkeypatch, "c-1")
    _nothing_starts_here(monkeypatch)
    on_compute = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute")
    for where in (on_compute, placement.LOCAL_PLACEMENT):
        cm.sent.clear()
        v = asyncio.run(script_kind.run(check, _target(placement=where), _changed(), round_no=1))
        assert v.status == "pass" and v.ran_on == placement.LOCAL
        [frame] = cm.sent
        assert frame["type"] == "step_run" and "script" not in frame and frame["argv"]
        binds = frame["sandbox"]["binds"] + frame["sandbox"]["extra_binds"]
        assert binds and all(b["root"] != "abs" for b in binds)
        assert any(b["sandbox"] == "/check" for b in binds)
    # The scratch folder of each run is gone again.
    assert not list((tmp_path / "agents" / AGENT / script_kind.SCRATCH_DIR_NAME).glob("*"))


def test_with_no_compute_machine_a_check_does_not_run(tmp_path, monkeypatch):
    # The platform runs no script itself: with nothing serving the
    # platform's place the check's verdict says the place is not there, and
    # nothing is started here or sent anywhere.
    check = _agents_check(tmp_path, monkeypatch)
    cm = _CM()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    _serving(monkeypatch, None)
    _nothing_starts_here(monkeypatch)
    v = asyncio.run(script_kind.run(check, _target(), _changed(), round_no=1))
    assert v.status == "error" and v.reason == "the machine is offline" and v.ran_on == placement.LOCAL
    assert not cm.sent


def test_a_compute_machine_that_left_before_the_script_reached_it_runs_it_nowhere(tmp_path, monkeypatch):
    # The machine was picked and dropped before the frame went out: the
    # verdict is the place's absence, never a run inside the platform.
    check = _agents_check(tmp_path, monkeypatch)
    cm = _CM(shared={"c-1"}, connected=False)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    _serving(monkeypatch, "c-1")
    _nothing_starts_here(monkeypatch)
    v = asyncio.run(script_kind.run(check, _target(), _changed(), round_no=1))
    assert v.status == "error" and v.reason and v.ran_on == placement.LOCAL and not cm.sent


def test_a_check_of_a_turn_on_a_paired_machine_never_asks_for_a_compute_machine(tmp_path, monkeypatch):
    check = _agents_check(tmp_path, monkeypatch)
    cm = _CM()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    from services.apps import app_compute
    monkeypatch.setattr(app_compute, "place", lambda: pytest.fail("a paired machine's check asked for compute"))
    target = _target(placement=placement.PlacementCapabilities(
        kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1", label="laptop"))
    v = asyncio.run(script_kind.run(check, target, _changed(), round_no=1))
    assert v.ran_on == "m-1" and "sandbox" not in cm.sent[0]


def test_a_check_of_a_turn_on_a_machine_with_its_own_copy_runs_beside_the_platform(tmp_path, monkeypatch):
    # The machine has neither the platform's scratch folder nor the staged
    # script: the check waits for its file changes to arrive, then runs on
    # the compute machine that shares the platform's folders. Never on the
    # machine with its own copy.
    from services.checks.kinds import judge_kind
    check = _agents_check(tmp_path, monkeypatch)
    cm = _CM(own={"c-2"}, shared={"c-1"})
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    waited: list[str] = []

    async def _waited(machine_id, agent):
        waited.append(f"quiet:{machine_id}:{agent}")
    monkeypatch.setattr(judge_kind, "_wait_sync_quiet", _waited)
    own = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-2", label="Rack two",
                                          own_copy=True)
    _serving(monkeypatch, "c-1")
    _nothing_starts_here(monkeypatch)
    v = asyncio.run(script_kind.run(check, _target(placement=own), _changed(), round_no=1))
    assert v.status == "pass" and v.ran_on == placement.LOCAL
    assert waited == [f"quiet:c-2:{AGENT}"] and len(cm.sent) == 1 and "sandbox" in cm.sent[0]


def test_a_check_of_a_turn_on_a_machine_with_its_own_copy_waits_then_needs_a_shared_machine(tmp_path, monkeypatch):
    # With no compute machine beside the platform, the check still waits
    # for the machine's file changes to arrive, then finds no place to run:
    # never on the machine with its own copy, never inside the platform.
    from services.checks.kinds import judge_kind
    check = _agents_check(tmp_path, monkeypatch)
    cm = _CM(own={"c-2"})
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    _serving(monkeypatch, None)
    _nothing_starts_here(monkeypatch)
    order: list[str] = []

    async def _quiet(machine_id, agent):
        order.append(f"quiet:{machine_id}:{agent}")

    monkeypatch.setattr(judge_kind, "_wait_sync_quiet", _quiet)
    own = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-2", label="Rack two",
                                          own_copy=True)
    v = asyncio.run(script_kind.run(check, _target(placement=own), _changed(), round_no=1))
    assert v.status == "error" and v.reason == "the machine is offline" and v.ran_on == placement.LOCAL
    assert order == [f"quiet:c-2:{AGENT}"] and not cm.sent


def test_the_runner_sends_no_script_to_a_machine_with_its_own_copy(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    cm = _CM(own={"c-2"})
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    with pytest.raises(runner.ScriptRefused, match="own copy"):
        asyncio.run(runner.run_remote(_spec(script_dir=tmp_path, scratch_root=tmp_path / "scratch"), "c-2"))
    assert not cm.sent


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
    from services.apps import app_handlers, app_supervisor, app_tokens
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
        assert app_handlers._inflight_checks, "the run is registered while the handler runs"
        return answers.pop(0)
    monkeypatch.setattr(app_supervisor, "forward", forward)
    doc = d.validate_check_doc({"name": "coding", "handler": {"app": "reviewer", "handler": "judge"}})
    check = d.CheckDoc(agent=AGENT, owner="", name="coding", doc=doc, folder=Path("/x"), doc_sha256="x")
    v = asyncio.run(handler_kind.run(check, _target(), _changed(), round_no=1))
    assert v.status == "fail" and v.findings[0]["text"] == "wrong" and v.summary == "no"
    path, headers, body = calls[0]
    assert path == "/_handler/judge" and headers["X-OtoDock-Viewer"] == "claim-check"
    assert headers["X-OtoDock-Basis"] == "platform" and body["event"] == "check:coding"
    assert body["payload"]["check"] == "coding" and body["payload"]["paths"]
    assert app_handlers._inflight_checks == {}
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
    from services.apps import app_handlers, app_tokens
    row = {"id": "app-x"}
    monkey = app_tokens.verify
    try:
        app_tokens.verify = lambda tok, app_id, purpose: {"principal": "platform", "kind": "check",
                                                          "delivery": "run-1"}
        app_handlers.register_inflight_check("run-1", "app-x", "judge", "check:coding", ttl_s=60)
        reg = asyncio.run(app_proxy.inflight_delivery("t", row))
        assert reg["status"] == "inflight" and reg["handler"] == "judge"
        app_handlers.unregister_inflight_check("run-1")
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


_HOME_SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


@pytest.mark.parametrize("username", ["", "alice"])
def test_a_scripts_sandbox_shows_no_sessions_home(tmp_path, monkeypatch, username):
    """A script run (a check's, an app step's) sees the stores its identity
    sees, and in each the ``own`` folder of the sessions' homes (their
    sign-ins) is masked empty and read-only, rendered and run under bwrap."""
    import os
    import shutil
    import subprocess

    import config
    from core.sandbox import sandbox_render as render
    agents = tmp_path / "agents"
    for store in (agents / "pa" / "workspace" / ".claude", agents / "pa" / "users" / "alice" / ".claude"):
        (store / "own" / _HOME_SID).mkdir(parents=True)
        (store / "own" / _HOME_SID / ".credentials.json").write_text("token")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    spec = runner.ScriptSpec(run_id="r-1", agent="pa", script=b"#!/bin/sh\n", sha256="", run_name="s.sh",
                             role="manager", username=username, script_dir=agents / "pa" / "script",
                             scratch_root=agents / "pa" / "scratch")
    spec.script_dir.mkdir()
    (spec.script_dir / "s.sh").write_bytes(spec.script)
    desc, _inner, _env, _step = runner._build_sandbox(spec)
    # A person's view shows the agent scope's engine folders masked empty
    # already: no mask inside that mask.
    seen = {m.sandbox for m in desc.home_masks}
    assert seen == ({"/users/alice/.claude/own"} if username else {"/workspace/.claude/own"})
    assert desc.home is None
    if shutil.which("bwrap") is None:
        return
    empty = tmp_path / "empty"
    empty.mkdir()
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(tmp_path / "mcps"), empty_dir=str(empty),
                             launcher="/bin/true", uid=os.getuid(), gid=os.getgid())
    script = "for d in /workspace/.claude/own /users/alice/.claude/own; do [ -d $d ] && ls -A $d; done; echo end"
    cmd = render.render(desc, host, ["/bin/sh", "-c", script])
    out = subprocess.run(cmd[cmd.index("bwrap"):], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert _HOME_SID not in out.stdout and out.stdout.strip().endswith("end")


def test_a_script_changed_after_the_load_is_refused_with_no_index_row(temp_db, tmp_path, monkeypatch):
    # An index sync that failed leaves no row (or one without the script's
    # hash): the hash the check's own load took still holds the run.
    check = _agents_check(tmp_path, monkeypatch)
    assert script_kind._read_script(check)[1] == check.script_sha256
    (check.folder / "lint.sh").write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(runner.ScriptRefused) as e:
        script_kind._read_script(check)
    assert "changed since" in str(e.value)


def test_a_linked_script_is_never_read(temp_db, tmp_path, monkeypatch):
    check = _agents_check(tmp_path, monkeypatch)
    other = tmp_path / "agents" / AGENT / "workspace" / "other.sh"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("#!/bin/sh\necho checking\nexit 0\n")
    (check.folder / "lint.sh").unlink()
    (check.folder / "lint.sh").symlink_to(other)
    with pytest.raises(runner.ScriptRefused) as e:
        script_kind._read_script(check)
    assert "missing" in str(e.value)


def _agent_spec(tmp_path, monkeypatch, *, script=b"#!/bin/sh\necho judged\n", **over):
    """A check's spec with its folder and scratch root inside the agent's
    folder, where a check run puts them."""
    import config
    agents = tmp_path / "agents"
    (agents / AGENT / "workspace").mkdir(parents=True)
    (agents / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    folder = agents / AGENT / "config" / "checks" / "lint"
    folder.mkdir(parents=True)
    (folder / "lint.sh").write_text("#!/bin/sh\necho folder\n")
    (folder / "helper.txt").write_text("beside\n")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    return _spec(script=script, script_dir=folder, mount_at="/check",
                 scratch_root=agents / AGENT / script_kind.SCRATCH_DIR_NAME, **over)


def test_the_script_that_runs_is_the_script_that_was_judged(tmp_path, monkeypatch):
    """The hashed bytes are staged in the run's own folder and bound over the
    script's name, after the check's folder: the folder keeps its place for
    ``$0`` and the script's siblings, and the bytes run are the bytes read."""
    import os
    import shutil
    import subprocess

    from core.sandbox import sandbox_render as render
    spec = _agent_spec(tmp_path, monkeypatch)
    desc, inner, _env, step_dir = runner._build_sandbox(spec)
    order = [b.sandbox for b in desc.extra_binds]
    assert order.index("/check") < order.index("/check/lint.sh")
    staged = next(b for b in desc.extra_binds if b.sandbox == "/check/lint.sh")
    assert not staged.rw
    assert (step_dir / "script").read_bytes() == b"#!/bin/sh\necho judged\n"
    assert inner[-1] == "/check/lint.sh"
    if shutil.which("bwrap") is None:
        return
    empty = tmp_path / "empty"
    empty.mkdir()
    host = render.RenderHost(agents_root=str(tmp_path / "agents"), mcps_root=str(tmp_path / "mcps"),
                             empty_dir=str(empty), launcher="/bin/true", uid=os.getuid(), gid=os.getgid())
    cmd = render.render(desc, host, ["/bin/sh", "-c", "sh /check/lint.sh; cat /check/helper.txt"])
    out = subprocess.run(cmd[cmd.index("bwrap"):], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["judged", "beside"]


def test_a_run_whose_staged_script_is_not_bound_is_refused(tmp_path, monkeypatch):
    """The staged file's bind is skipped where its source is missing; the run
    then would execute the folder's own name, so the build refuses it."""
    from core.sandbox import sandbox
    spec = _agent_spec(tmp_path, monkeypatch)
    original = sandbox.SandboxBuilder._extra_binds

    def without_the_staged(self, root):
        return [b for b in original(self, root) if b.sandbox != "/check/lint.sh"]
    monkeypatch.setattr(sandbox.SandboxBuilder, "_extra_binds", without_the_staged)
    with pytest.raises(runner.ScriptRefused):
        runner._build_sandbox(spec)


def test_the_runs_folder_is_never_made_or_written_through_a_link(tmp_path, monkeypatch):
    spec = _agent_spec(tmp_path, monkeypatch)
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    (tmp_path / "agents" / AGENT / script_kind.SCRATCH_DIR_NAME).symlink_to(stand_in)
    with pytest.raises(runner.ScriptRefused):
        runner._build_sandbox(spec)
    assert list(stand_in.iterdir()) == []


def test_the_runs_folder_is_removed_without_following_a_link(tmp_path, monkeypatch):
    """The scratch root swapped for a link while the run was out: the
    removal follows no link on the way, so what the link points at stays."""
    import shutil as _shutil
    spec = _agent_spec(tmp_path, monkeypatch)
    stand_in = tmp_path / "stand-in"
    (stand_in / spec.run_id).mkdir(parents=True)
    (stand_in / spec.run_id / "keep.txt").write_text("kept")

    class _Swapping(_CM):
        async def send_command(self, mid, msg, timeout=None, command_id=None):
            _shutil.rmtree(spec.scratch_root)
            spec.scratch_root.symlink_to(stand_in)
            return {"exit_code": 0, "output": ""}
    cm = _Swapping(shared=("c-1",))
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    asyncio.run(runner.run_remote(spec, "c-1"))
    assert (stand_in / spec.run_id / "keep.txt").read_text() == "kept"


def test_a_run_refused_after_its_folder_was_staged_leaves_no_folder(tmp_path, monkeypatch):
    """A refusal after the payload and the script were staged removes the
    run's folder: the run never reached the step that removes it after."""
    from core.sandbox import sandbox
    spec = _agent_spec(tmp_path, monkeypatch)
    original = sandbox.SandboxBuilder._extra_binds

    def without_the_staged(self, root):
        return [b for b in original(self, root) if b.sandbox != "/check/lint.sh"]
    monkeypatch.setattr(sandbox.SandboxBuilder, "_extra_binds", without_the_staged)
    with pytest.raises(runner.ScriptRefused):
        runner.compute_frame(spec)
    assert not (spec.scratch_root / spec.run_id).exists()
    assert spec.scratch_root.is_dir()


def test_an_agent_behind_the_operators_link_stages_and_binds_its_run(tmp_path, monkeypatch):
    """An agent folder an admin moved behind a link (the operator's own
    layout) stages the run in its real folder and binds the staged script,
    as any other agent's."""
    spec = _agent_spec(tmp_path, monkeypatch)
    agents = tmp_path / "agents"
    real = tmp_path / "disk2" / AGENT
    real.parent.mkdir()
    (agents / AGENT).rename(real)
    (agents / AGENT).symlink_to(real)
    desc, _inner, _env, step_dir = runner._build_sandbox(spec)
    assert (real / script_kind.SCRATCH_DIR_NAME / spec.run_id / "script").read_bytes() == spec.script
    assert any(b.sandbox == "/check/lint.sh" for b in desc.extra_binds)
    runner.remove_run_folder(spec.agent, step_dir)
    assert not (real / script_kind.SCRATCH_DIR_NAME / spec.run_id).exists()


@pytest.mark.parametrize("where", ["inside", "outside"])
def test_a_check_folder_that_is_a_link_at_the_build_is_refused(tmp_path, monkeypatch, where):
    """The check's folder is bound as it was read: at its own place beneath
    the agent's real folder. A link at it, to a stand-in inside the test's
    own agent or outside it, refuses the run and leaves no run folder."""
    spec = _agent_spec(tmp_path, monkeypatch)
    stand_in = (tmp_path / "agents" / AGENT / "users" / "alice" / "workspace" / "stand-in"
                if where == "inside" else tmp_path / "stand-in")
    stand_in.mkdir()
    (stand_in / "lint.sh").write_text("#!/bin/sh\necho stand-in\n")
    folder = spec.script_dir
    folder.rename(tmp_path / "lint-aside")
    folder.symlink_to(stand_in, target_is_directory=True)
    with pytest.raises(runner.ScriptRefused):
        runner.compute_frame(spec)
    assert not (spec.scratch_root / spec.run_id).exists()


def test_the_frame_carries_the_description_that_was_checked(tmp_path, monkeypatch):
    """The sandbox is described once: the binds that were checked are the
    binds the frame sends."""
    from core.sandbox import sandbox
    spec = _agent_spec(tmp_path, monkeypatch)
    calls = []
    original = sandbox.SandboxBuilder.describe

    def counted(self):
        calls.append(1)
        return original(self)
    monkeypatch.setattr(sandbox.SandboxBuilder, "describe", counted)
    frame, _step = runner.compute_frame(spec)
    assert len(calls) == 1
    binds = {b["sandbox"]: b for b in frame["sandbox"]["extra_binds"]}
    assert binds["/check"]["rel"] == "config/checks/lint"


def test_a_staging_that_fails_midway_leaves_no_run_folder(tmp_path, monkeypatch):
    """The run's folder was made and a write after it failed (a full disk):
    no run folder is left behind."""
    from services.infra import safe_fs
    spec = _agent_spec(tmp_path, monkeypatch)
    original = safe_fs.atomic_write_beneath

    def full_disk(root, rel, data, **kw):
        if str(rel).endswith("/" + runner.STAGED_SCRIPT):
            raise OSError(28, "No space left on device")
        return original(root, rel, data, **kw)
    monkeypatch.setattr(safe_fs, "atomic_write_beneath", full_disk)
    with pytest.raises(runner.ScriptRefused):
        runner.compute_frame(spec)
    assert not (spec.scratch_root / spec.run_id).exists()
