"""App steps (APPS.md "Steps").

Load-bearing: a step is declared once beside its wake and the script's
sha256 is inside the signed manifest (an edit voids the approval, a
rollback re-hashes); the runner reads the script from the live release and
refuses one that changed; a step runs in its own lane beside the server's
wakes, one at a time per handler, under the app and platform caps; the
verdict is the exit code, kept on the delivery with the output, and a
finished step wakes the ``step:<name>`` subscribers; the step's claim is a
bearer the platform and the action routes accept only while the delivery
is in flight; the card learns the scripts, their place, the link warning
and who may approve. A step of the platform's place goes to the compute
machine that serves it, with the identity's mounts, never ``/config``, and
its signed script alone. What the machine does with that sandbox is the
satellite's suite (``satellite/tests/test_step_runner.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import app_proxy
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core import placement
from core.session import session_state
from services.apps import (
    app_deploy,
    app_handlers,
    app_steps,
    app_supervisor,
    app_tokens,
    call_rate,
    catalog,
    manifest as _mf,
    releases,
)
from services.scheduler import shared
from services.scripts import runner as script_runner
from storage import database as task_store, db_app_deliveries as deliveries
from storage.pg import get_conn
from tests.apps.conftest import MACHINE

client = TestClient(app)

AGENT = "steps-agent"
SID_SHARED = "sess-steps-shared"

SCRIPT = "#!/bin/sh\necho hi\n"
SHA = hashlib.sha256(SCRIPT.encode()).hexdigest()


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), agent_roles=roles)


ALICE = _user()
CAROL = _user("carol-sub", {AGENT: "editor"})


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    app_supervisor._client = None
    catalog._loop = None
    app_handlers._drains.clear()
    app_handlers._drain_locks.clear()
    app_handlers._step_runs.clear()
    app_handlers.mark_dirty()
    monkeypatch.setattr(call_rate, "RESERVE", 1000.0)
    yield
    app.dependency_overrides.pop(get_current_user, None)
    shared._shutting_down = False
    app_handlers._drains.clear()
    app_handlers._step_runs.clear()
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_supervisor._client = None


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace" / "notes").mkdir(parents=True)
    (agents_root / AGENT / "knowledge").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob"), ("carol-sub", "carol")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
        with get_conn() as conn:
            conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
            conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    task_store.add_user_agent("carol-sub", AGENT, "editor", "test")
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    yield agents_root / AGENT
    session_state._session_security.pop(SID_SHARED, None)


def _hook(op: str, payload: dict, sid: str = SID_SHARED):
    payload.setdefault("session_id", sid)
    with patch("api.hooks.app_deploy.verify_session_match_async"), \
            patch("api.hooks.pins.verify_session_match_async"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _folder(agent_tree: Path, slug: str, manifest: dict, scripts: dict[str, str] | None = None,
            server: str | None = None) -> Path:
    root = agent_tree / "workspace" / "apps" / slug
    (root / "client").mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(manifest))
    (root / "client" / "index.html").write_text("<p>app</p>")
    for rel, text in (scripts or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    if server is not None:
        (root / "server").mkdir(exist_ok=True)
        (root / "server" / "index.ts").write_text(server)
    return root


def _row(slug: str, actions: list[dict], *, handlers: dict | None = None, steps: dict | None = None,
         requires: dict | None = None, username: str = "", owner: str | None = None,
         approve: bool = True, approver: str = "alice-sub") -> dict:
    root = f"users/{username}/workspace" if username else "workspace"
    blocks: dict[str, str] = {}
    for name, block in (("handlers", handlers), ("steps", steps), ("requires", requires)):
        if block:
            blocks[name] = task_store.canonical_actions_json(block)
    actions_json, err = _mf.validate_actions(actions, AGENT, shared=not username)
    assert actions_json is not None, err
    row = task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                rel_path=f"{root}/apps/{slug}", kind="folder",
                                actions_json=actions_json, blocks=blocks or None)
    if approve:
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), approver)
    return task_store.get_app(row["id"])


def _deploy_approved(agent_tree: Path, slug: str, manifest: dict,
                     scripts: dict[str, str] | None = None) -> dict:
    _folder(agent_tree, slug, manifest, scripts)
    body = _hook("deploy", {"slug": slug, "visibility": "agent"}).json()
    assert body["status"] == "pending approval", body
    r = client.post(f"/v1/apps/{body['app_id']}/deploy/approve")
    assert r.status_code == 200, r.text
    return task_store.get_app(body["app_id"])


STEP_MANIFEST = {
    "title": "Stepper",
    "handlers": {"on_trigger": ["sync", "ping"], "on_event": {"after": ["step:sync"]}},
    "steps": {"sync": {"run": "scripts/sync.sh", "timeout": 60}},
}


def test_a_deploy_that_replaces_the_waiting_copy_rings_no_second_time(agent_tree, monkeypatch):
    """One pending-release notification per waiting episode: three deploys
    of one lane rang three times on the internal install, and the approver
    approved twice."""
    sent: list = []

    async def fake_fire(title, body, **kw):
        sent.append(title)
        return [{"id": "x"}]

    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fake_fire)
    monkeypatch.setattr("storage.identity.db_users.get_agent_users",
                        lambda agent: [{"sub": "m-sub", "agent_role": "manager"}])
    _folder(agent_tree, "twice", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    first = _hook("deploy", {"slug": "twice", "visibility": "agent"}).json()
    assert first["status"] == "pending approval" and first["release"] == 1, first
    again = _hook("deploy", {"slug": "twice", "visibility": "agent"}).json()
    assert again["status"] == "pending approval" and again["superseded"] == 1, again
    assert sent == ["Release 1 of Stepper is waiting for approval"]


def _result(**over) -> app_steps.StepResult:
    base = dict(exit_code=0, output="hello", timed_out=False, ran_on=MACHINE, seconds=0.1)
    base.update(over)
    return app_steps.StepResult(**base)


async def _run_step(row: dict, handler: str, payload: dict | None = None) -> dict:
    """Enqueue, let the drain hand the row to its lane, wait for the run."""
    d = await app_handlers.enqueue(row, handler, "trigger:t", payload or {})
    await app_handlers._drains[row["id"]]
    task = app_handlers._step_runs.get((row["id"], handler))
    if task is not None:
        await task
    return deliveries.get(d["id"])


# ── the manifest ────────────────────────────────────────────────────────────


def test_the_steps_block_is_validated_and_hashed_from_the_folder(tmp_path):
    src = tmp_path / "app"
    (src / "scripts").mkdir(parents=True)
    (src / "scripts" / "run.sh").write_text(SCRIPT)
    doc = {"title": "S",
           "handlers": {"on_schedule": {"sync": {"cron": "0 * * * *"}}, "on_event": {"after": ["step:sync"]}},
           "steps": {"sync": {"run": "scripts/run.sh", "timeout": 300}}}
    m = app_deploy.validate_app_json(doc, AGENT, True, source_dir=src)
    steps = json.loads(m.steps_json)
    assert steps == {"sync": {"run": "scripts/run.sh", "timeout": 300, "sha256": SHA}}
    assert m.blocks()["steps"] == m.steps_json
    # Without the folder the shape is checked and the hash left out; the
    # timeout defaults to a minute.
    doc["steps"]["sync"] = {"run": "scripts/run.sh"}
    assert json.loads(app_deploy.validate_app_json(doc, AGENT, True).steps_json) == {
        "sync": {"run": "scripts/run.sh", "timeout": 60}}

    def refused(steps, fragment, handlers=None, **extra):
        bad = {"title": "S", "handlers": handlers or {"on_trigger": ["sync"]}, "steps": steps, **extra}
        with pytest.raises(app_deploy.DeployError, match=fragment):
            app_deploy.validate_app_json(bad, AGENT, True, source_dir=src)

    refused({"sync": {"run": "/etc/passwd"}}, "relative path")
    refused({"sync": {"run": "scripts/../run.sh"}}, "plain file")
    refused({"sync": {"run": "scripts/.hidden.sh"}}, "plain file")
    refused({"sync": {"run": "client/x.sh"}}, "beside client")
    refused({"sync": {"run": "server/index.ts"}}, "beside client")
    refused({"sync": {"run": "data/x.sh"}}, "beside client")
    refused({"sync": {"run": "app.json"}}, "beside client")
    refused({"sync": {"run": "scripts/run.sh", "timeout": 0}}, "whole seconds")
    refused({"sync": {"run": "scripts/run.sh", "timeout": 7201}}, "whole seconds")
    refused({"sync": {"run": "scripts/run.sh", "timeout": "5"}}, "whole seconds")
    refused({"sync": {"run": "scripts/run.sh", "timeout": True}}, "whole seconds")
    refused({"sync": {"run": "scripts/run.sh", "then": "after"}}, "an object with run")
    refused({"other": {"run": "scripts/run.sh"}}, "not a declared handler")
    refused({"sync": {"run": "scripts/missing.sh"}}, "not a file in the app folder")
    refused({"sync": {"run": "scripts/run.sh"}}, "names a step the app does not declare",
            handlers={"on_trigger": ["sync"], "on_event": {"after": ["step:other"]}})
    refused({"sync": {"run": "scripts/run.sh"}}, "a step does not wake on a step",
            handlers={"on_trigger": ["ping"], "on_event": {"sync": ["step:sync"]}})
    # A script its interpreter cannot parse is refused here, not at its
    # first wake; a script for an interpreter the check does not know passes.
    (src / "scripts" / "broken.sh").write_text("#!/usr/bin/env bash\necho 'unclosed\n")
    refused({"sync": {"run": "scripts/broken.sh"}}, "does not parse")
    (src / "scripts" / "broken.py").write_text("#!/usr/bin/env python3\ndef x(:\n    pass\n")
    refused({"sync": {"run": "scripts/broken.py"}}, "does not parse")
    (src / "scripts" / "other.rb").write_text("#!/usr/bin/env ruby\nputs 'not checked\n")
    other = {"title": "S", "handlers": {"on_trigger": ["sync"]}, "steps": {"sync": {"run": "scripts/other.rb"}}}
    assert "sync" in json.loads(app_deploy.validate_app_json(other, AGENT, True, source_dir=src).steps_json)
    (src / "scripts" / "big.sh").write_bytes(b"#!/bin/sh\n" + b"#" * (257 * 1024))
    refused({"sync": {"run": "scripts/big.sh"}}, "larger than 256 KB")
    (src / "scripts" / "link.sh").symlink_to(src / "scripts" / "run.sh")
    refused({"sync": {"run": "scripts/link.sh"}}, "not a file in the app folder")
    # An older proxy refuses the block as an unknown key; this one refuses
    # a key it does not know the same way.
    with pytest.raises(app_deploy.DeployError, match="unknown keys"):
        app_deploy.validate_app_json({"title": "S", "stepz": {}}, AGENT, True)


def test_the_script_hash_is_in_the_signed_text(agent_tree):
    row = _row("hashed", [], handlers={"on_trigger": ["sync"]},
               steps={"sync": {"run": "scripts/x.sh", "timeout": 60, "sha256": "a" * 64}})
    assert task_store.app_actions_approved(row)
    assert '"steps"' in task_store.canonical_manifest(row)
    # The same manifest with another script content is another signature.
    changed = task_store.upsert_app(AGENT, "", None, "hashed", blocks={
        "steps": task_store.canonical_actions_json(
            {"sync": {"run": "scripts/x.sh", "timeout": 60, "sha256": "b" * 64}})})
    assert not task_store.app_actions_approved(task_store.get_app(changed["id"]))
    # A row without steps signs the same text it always did.
    plain = _row("plain", [], handlers={"on_trigger": ["ping"]})
    assert "steps" not in task_store.canonical_manifest(plain)
    assert _mf.parse_steps(plain) == {} and not _mf.is_step(plain, "ping")
    assert _mf.is_step(row, "sync")


def test_the_accounts_a_step_receives_are_in_the_signed_text(agent_tree):
    # ``requires.providers`` names whose tokens a step script receives, so
    # on a row with steps a changed list voids the approval; a row without
    # steps keeps signing the text it always did.
    steps = {"sync": {"run": "scripts/x.sh", "timeout": 60, "sha256": "a" * 64}}
    row = _row("creds", [], handlers={"on_trigger": ["sync"]}, steps=steps)
    assert task_store.app_actions_approved(row)
    widened = task_store.upsert_app(AGENT, "", None, "creds", blocks={
        "requires": task_store.canonical_actions_json({"providers": ["github"]})})
    assert not task_store.app_actions_approved(task_store.get_app(widened["id"]))
    assert '"requires"' in task_store.canonical_manifest(widened)
    plain = _row("plain-needs", [], handlers={"on_trigger": ["ping"]}, requires={"mcps": ["github"]})
    assert "requires" not in task_store.canonical_manifest(plain)
    again = task_store.upsert_app(AGENT, "", None, "plain-needs", blocks={
        "requires": task_store.canonical_actions_json({"mcps": ["github", "gmail"]})})
    assert task_store.app_actions_approved(task_store.get_app(again["id"]))


def test_a_deploy_hashes_the_script_and_a_rollback_rehashes_it(agent_tree):
    row = _deploy_approved(agent_tree, "flow", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    assert _mf.parse_steps(row)["sync"]["sha256"] == SHA
    assert task_store.app_actions_approved(row)
    # An edited script is a new manifest: the deploy waits on the card.
    src = agent_tree / "workspace/apps/flow/scripts/sync.sh"
    src.write_text(SCRIPT + "echo more\n")
    body = _hook("deploy", {"slug": "flow", "visibility": "agent"}).json()
    assert body["status"] == "pending approval" and body["manifest_changed"] is True
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    row = task_store.get_app(row["id"])
    new_sha = hashlib.sha256((SCRIPT + "echo more\n").encode()).hexdigest()
    assert _mf.parse_steps(row)["sync"]["sha256"] == new_sha and task_store.app_actions_approved(row)
    # A rollback serves the previous script again and re-hashes it, so the
    # signature no longer matches and the card comes back.
    asyncio.run(app_deploy.rollback_folder(row, by_person=True))
    back = task_store.get_app(row["id"])
    assert _mf.parse_steps(back)["sync"]["sha256"] == SHA
    assert not task_store.app_actions_approved(back)


# ── the fire ────────────────────────────────────────────────────────────────


def test_a_step_runs_in_its_lane_with_the_verdict_on_the_delivery(agent_tree, placed, monkeypatch):
    row = _deploy_approved(agent_tree, "flow", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    seen: list = []

    async def fake_run(plan, claim, machine_id):
        seen.append((plan, claim, machine_id))
        return _result(output=f"hello\nclaim={claim}\n")

    monkeypatch.setattr(app_steps, "run_remote", fake_run)

    async def go():
        got = await _run_step(row, "sync", {"n": 1})
        assert got["status"] == "done" and got["exit_code"] == 0 and got["ran_on"] == MACHINE
        assert got["started_at"] and got["done_at"]
        # The claim never lands in the output: it is scrubbed.
        assert "hello" in got["output"] and "claim=[redacted]" in got["output"]
        plan, claim, machine_id = seen[0]
        assert plan.name == "sync" and plan.run == "scripts/sync.sh" and plan.sha256 == SHA
        assert plan.script == SCRIPT.encode() and plan.target == machine_id == MACHINE
        assert plan.identity.scope == "agent" and plan.identity.knowledge_rw is True
        assert plan.workspace_relative == "workspace" and plan.knowledge_relative == "knowledge"
        claims = app_tokens.verify(claim, row["id"], app_tokens.PURPOSE_CALLER)
        assert claims["kind"] == "step" and claims["delivery"] == got["id"]
        assert claims["principal"] == "platform" and claims["handler"] == "sync"
        # The finished step woke the subscriber with the verdict; the
        # follow-up drain took that row (no server: it dies with the reason).
        await app_handlers._drains[row["id"]]
        after = [w for w in deliveries.recent(row["id"]) if w["handler"] == "after"]
        assert len(after) == 1 and after[0]["event"] == "step:sync"
        payload = deliveries.get(after[0]["id"])["payload"]
        assert payload["step"] == "sync" and payload["status"] == "done" and payload["exit_code"] == 0
        assert payload["delivery_id"] == got["id"] and "hello" in payload["output"]
        assert deliveries.get(after[0]["id"])["last_error"] == "the app has no server"
        # The trailer in the app log, and the status with the step's columns.
        trailer = releases.log_path(row).read_text()
        assert "step sync: done" in trailer and "on the sandbox" in trailer
        st = app_deploy.status(task_store.get_app(row["id"]))
        mine = [w for w in st["wakes"] if w["handler"] == "sync"][0]
        assert mine["exit_code"] == 0 and mine["ran_on"] == MACHINE

    asyncio.run(go())


def test_verdicts_by_exit_code_timeout_and_refusals(agent_tree, placed, monkeypatch):
    row = _deploy_approved(agent_tree, "flow", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    outcome: dict = {}

    async def fake_run(plan, claim, machine_id):
        if isinstance(outcome.get("raise"), Exception):
            raise outcome["raise"]
        return _result(**outcome.get("result", {}))

    monkeypatch.setattr(app_steps, "run_remote", fake_run)

    async def go():
        outcome["result"] = {"exit_code": 3, "output": "a\nb\nboom\n"}
        got = await _run_step(row, "sync")
        assert got["status"] == "dead" and got["exit_code"] == 3
        assert got["last_error"] == "exit 3: a b boom"
        outcome["result"] = {"exit_code": None, "timed_out": True, "output": ""}
        got = await _run_step(row, "sync")
        assert got["status"] == "dead" and got["last_error"] == "timed out after 60 s"
        # The place is not there: the row waits, uncounted.
        outcome["raise"] = app_steps.StepUnavailable("the machine is offline")
        got = await _run_step(row, "sync")
        assert got["status"] == "pending" and got["attempts"] == 0
        assert got["last_error"] == "the machine is offline"
        with get_conn() as conn:
            conn.execute("DELETE FROM app_handler_deliveries WHERE id=%s", (got["id"],))
            conn.commit()
        outcome.clear()
        # A script that is not the one approved never runs.
        fresh = task_store.upsert_app(AGENT, "", None, "flow", blocks={
            "steps": task_store.canonical_actions_json(
                {"sync": {"run": "scripts/sync.sh", "timeout": 60, "sha256": "0" * 64}})})
        task_store.approve_app_actions(fresh["id"], task_store.manifest_sig(fresh), "alice-sub")
        got = await _run_step(task_store.get_app(fresh["id"]), "sync")
        assert got["status"] == "dead" and "changed since the approval" in got["last_error"]
        # A step whose approver lost the manager role runs with knowledge
        # read-only; the fake runner sees the identity.
        plans: list = []

        async def spy(plan, claim, machine_id):
            plans.append(plan)
            return _result()

        monkeypatch.setattr(app_steps, "run_remote", spy)
        row2 = _deploy_approved(agent_tree, "flow2", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
        task_store.set_user_agent_role("alice-sub", AGENT, "editor")
        await _run_step(row2, "sync")
        assert plans[0].identity.knowledge_rw is False
        task_store.set_user_agent_role("alice-sub", AGENT, "manager")

    asyncio.run(go())


def test_the_step_claim_calls_the_platform_and_presses_buttons_while_in_flight(agent_tree, monkeypatch):
    task_store.create_dynamic_task("task-s1", AGENT, "Report", "write the report", "cli", "trigger",
                                   None, None, None, 600, "alice-sub", scope="agent")
    row = _row("caller", [
        {"id": "n", "label": "Notify", "type": "platform", "method": "notifications.create"},
        {"id": "run", "label": "Run", "type": "fire_task", "task_id": "task-s1", "min_role": "editor"},
    ], handlers={"on_trigger": ["sync"]},
        steps={"sync": {"run": "scripts/x.sh", "timeout": 60, "sha256": SHA}})
    deliveries.enqueue(row["id"], "sync", "trigger:t", {})
    d = deliveries.claim_next(row["id"])
    claim = app_steps.step_claim(row, d, 60)
    hdr = {"Authorization": f"Bearer {claim}"}
    _as(None)
    sent: list = []

    async def fake_fire(title, body, **kw):
        sent.append((title, kw))
        return [{"id": "x"}]

    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fake_fire)
    r = client.post(f"/v1/apps/{row['id']}/platform/notifications.create",
                    json={"args": {"title": "Synced", "body": "b"}}, headers=hdr)
    assert r.status_code == 200 and r.json()["result"]["to"] == "members", r.text
    fired: list = []

    async def fake_now(task_def, **kw):
        fired.append(kw)
        return "run-s"

    monkeypatch.setattr("services.scheduler.scheduler.trigger_task_now", fake_now)
    r = client.post(f"/v1/apps/{row['id']}/actions/run", json={}, headers=hdr)
    assert r.status_code == 200 and r.json()["run_id"] == "run-s", r.text
    assert fired[0]["trigger_type"] == "app_handler"
    assert fired[0]["trigger_source"] == f"app:{row['id']}:sync"
    # A handler's forwarded claim is not a bearer; a step claim opens no
    # other route; a finished delivery's claim is refused.
    handler_claim = app_handlers.claim_for(d, row["id"])
    assert client.post(f"/v1/apps/{row['id']}/actions/run", json={},
                       headers={"Authorization": f"Bearer {handler_claim}"}).status_code == 401
    assert client.get(f"/v1/apps/{row['id']}/api/anything", headers=hdr).status_code in (401, 404)
    deliveries.finish(d["id"], "done")
    r = client.post(f"/v1/apps/{row['id']}/platform/notifications.create",
                    json={"args": {"title": "Late", "body": "b"}}, headers=hdr)
    assert r.status_code == 403 and "in flight" in r.text
    assert client.post(f"/v1/apps/{row['id']}/actions/run", json={}, headers=hdr).status_code == 403


def test_steps_run_beside_the_server_lane_one_per_handler_under_the_caps(agent_tree, placed, monkeypatch):
    manifest = {"title": "Lanes", "handlers": {"on_trigger": ["a", "b", "ping"]},
                "steps": {"a": {"run": "scripts/a.sh"}, "b": {"run": "scripts/b.sh"}}}
    row = _deploy_approved(agent_tree, "lanes", manifest,
                           {"scripts/a.sh": SCRIPT, "scripts/b.sh": SCRIPT})
    gates: dict[str, asyncio.Event] = {}

    async def fake_run(plan, claim, machine_id):
        await gates[plan.name].wait()
        return _result(output=plan.name)

    monkeypatch.setattr(app_steps, "run_remote", fake_run)

    async def go():
        gates["a"], gates["b"] = asyncio.Event(), asyncio.Event()
        a1 = await app_handlers.enqueue(row, "a", "trigger:t", {})
        await app_handlers._drains[row["id"]]
        a2 = await app_handlers.enqueue(row, "a", "trigger:t", {})
        b1 = await app_handlers.enqueue(row, "b", "trigger:t", {})
        ping = await app_handlers.enqueue(row, "ping", "trigger:t", {})
        await app_handlers._drains[row["id"]]
        # Two different steps run at once; the second row of a waits; the
        # server's wake went through while both scripts were still running.
        assert app_handlers.running_steps(row["id"]) == {"a", "b"}
        assert deliveries.get(a1["id"])["status"] == "inflight"
        assert deliveries.get(a2["id"])["status"] == "pending"
        assert deliveries.get(b1["id"])["status"] == "inflight"
        assert deliveries.get(ping["id"])["status"] == "dead"      # no server, but not blocked
        gates["a"].set()
        await app_handlers._step_runs[(row["id"], "a")]
        await app_handlers._drains[row["id"]]                      # the follow-up drain took a2
        assert deliveries.get(a1["id"])["status"] == "done"
        assert deliveries.get(a2["id"])["status"] == "inflight"
        await app_handlers._step_runs[(row["id"], "a")]
        gates["b"].set()
        await app_handlers._step_runs[(row["id"], "b")]
        assert {deliveries.get(x["id"])["status"] for x in (a1, a2, b1)} == {"done"}
        # The per-app cap: with one slot, b waits until a ends.
        monkeypatch.setattr(app_handlers, "MAX_STEPS_PER_APP", 1)
        gates["a"], gates["b"] = asyncio.Event(), asyncio.Event()
        a3 = await app_handlers.enqueue(row, "a", "trigger:t", {})
        b2 = await app_handlers.enqueue(row, "b", "trigger:t", {})
        await app_handlers._drains[row["id"]]
        assert app_handlers.running_steps(row["id"]) == {"a"}
        assert deliveries.get(b2["id"])["status"] == "pending"
        gates["a"].set()
        gates["b"].set()
        await app_handlers._step_runs[(row["id"], "a")]
        await app_handlers._drains[row["id"]]
        await app_handlers._step_runs[(row["id"], "b")]
        assert {deliveries.get(x["id"])["status"] for x in (a3, b2)} == {"done"}
        # A manifest that dropped the step sends a claimed row back to the
        # queue rather than running it in the wrong lane.
        d = await app_handlers.enqueue(row, "a", "trigger:t", {})
        await app_handlers._drains[row["id"]]
        assert app_handlers.running_steps(row["id"]) == {"a"}
        monkeypatch.setattr(_mf, "is_step", lambda row, handler: False)
        gates["a"].set()
        await app_handlers._step_runs[(row["id"], "a")]
        assert deliveries.get(d["id"])["status"] in ("pending", "done", "dead")

    asyncio.run(go())


# ── the card ────────────────────────────────────────────────────────────────


def test_the_card_learns_the_steps_their_place_the_link_and_who_may_approve(agent_tree):
    from api.apps.apps import shape_app_rows
    from storage.sharing import share_store
    shared_row = _row("shared", [], handlers={"on_trigger": ["sync"]},
                      steps={"sync": {"run": "scripts/x.sh", "timeout": 60, "sha256": SHA}},
                      requires={"providers": ["github"]}, approve=False)
    plain = _row("noaccount", [], handlers={"on_trigger": ["sync"]},
                 steps={"sync": {"run": "scripts/x.sh", "timeout": 60, "sha256": SHA}}, approve=False)
    mine = _row("mine", [], handlers={"on_trigger": ["sync"]},
                steps={"sync": {"run": "scripts/x.sh", "timeout": 60, "sha256": SHA}},
                requires={"providers": ["github"]}, username="alice", owner="alice-sub", approve=False)
    by_slug = {r["slug"]: r for r in shape_app_rows([shared_row, plain, mine], ALICE)}
    s = by_slug["shared"]
    assert s["steps"]["sync"]["sha256"] == SHA and s["step_target"] == {"kind": "local"}
    assert s["has_live_link"] is False and s["steps_need_manager"] is True and s["can_approve"]
    assert by_slug["noaccount"]["steps_need_manager"] is False
    assert by_slug["mine"]["steps_need_manager"] is False
    # An editor may build and deploy, not approve the scripts' accounts.
    by_slug = {r["slug"]: r for r in shape_app_rows([shared_row, plain], CAROL)}
    assert by_slug["shared"]["can_approve"] is False
    assert by_slug["noaccount"]["can_approve"] is True
    # A live link shows on the row.
    share_store.create_external_share(target_kind="app", target_id=shared_row["id"],
                                      created_by="alice-sub", token_hash="t" * 64)
    assert shape_app_rows([shared_row], ALICE)[0]["has_live_link"] is True
    # A row without steps carries none of it.
    none = shape_app_rows([_row("bare", [], handlers={"on_trigger": ["ping"]})], ALICE)[0]
    assert none["steps"] == {} and "step_target" not in none and none["steps_need_manager"] is False


# ── small pieces ────────────────────────────────────────────────────────────


def test_the_argv_rule_and_the_scrub():
    assert app_steps.argv_for(b"#!/usr/bin/env bash\necho", "/app/x.sh") == ["/usr/bin/env", "bash", "/app/x.sh"]
    assert app_steps.argv_for(b"#!/usr/bin/python3 -u\nprint(1)", "/app/x.py") == ["/usr/bin/python3", "-u", "/app/x.py"]
    assert app_steps.argv_for(b"echo no shebang\n", "/app/x.sh") == ["/bin/sh", "/app/x.sh"]
    assert app_steps.argv_for(b"#!\n", "/app/x.sh") == ["/bin/sh", "/app/x.sh"]
    assert app_steps.scrub("token=abcdefghij tail short", ["abcdefghij", "short"]) == "token=[redacted] tail short"


# The Windows table, the same on the satellite's twin (satellite/tests/test_step_runner.py).
_WIN = {"bash": r"C:\Git\bin\bash.exe", "sh": r"C:\Git\bin\sh.exe",
        "env": r"C:\Git\usr\bin\env.exe", "python": r"C:\Py\python.exe"}
_WIN_PATH = r"C:\OtoDock\steps\d\check.sh"
_WIN_CASES = [
    (b"echo no shebang\n", [_WIN["sh"], "C:/OtoDock/steps/d/check.sh"]),
    (b"#!/bin/sh -e\n", [_WIN["sh"], "-e", "C:/OtoDock/steps/d/check.sh"]),
    (b"#!/usr/bin/env bash\n", [_WIN["bash"], "C:/OtoDock/steps/d/check.sh"]),
    (b"#!/usr/bin/env -S bash -eu\n", [_WIN["bash"], "-eu", "C:/OtoDock/steps/d/check.sh"]),
    (b"#!/usr/bin/python3 -u\n", [_WIN["python"], "-u", _WIN_PATH]),
    (b"#!/usr/bin/env python\n", [_WIN["python"], _WIN_PATH]),
    (b"#!/usr/bin/env LANG=C bash\n", [_WIN["env"], "LANG=C", _WIN["bash"], "C:/OtoDock/steps/d/check.sh"]),
    (b"#!/usr/bin/env -u FOO bash\n", [_WIN["env"], "-u", "FOO", _WIN["bash"], "C:/OtoDock/steps/d/check.sh"]),
    (b"#!/usr/bin/env node\n", [r"C:\Node\node.exe", _WIN_PATH]),
]


def _which(name):
    return {"node": r"C:\Node\node.exe", "npx": r"C:\Node\npx.cmd"}.get(name)


@pytest.mark.parametrize("script,argv", _WIN_CASES)
def test_the_argv_rule_on_windows(script, argv):
    """A Windows machine has no /bin/sh: a shell line and the default run
    under Git Bash, a Python line under the machine's Python."""
    assert script_runner.argv_for(script, _WIN_PATH, windows=_WIN, which=_which) == argv


@pytest.mark.parametrize("script,word", [
    (b"echo\n", "bash"), (b"#!/bin/bash\n", "bash"), (b"#!/usr/bin/env python3\n", "Python"),
    (b"#!/usr/bin/env npx tsx\n", "npx"), (b"#!/usr/bin/env ruby\n", "ruby"), (b"#!/usr/bin/env\n", "env"),
])
def test_an_interpreter_the_machine_lacks_is_named(script, word):
    """Without the baseline tools (no Git Bash, no Python) or with a word
    that resolves to nothing or to a .cmd: the interpreter is named, never
    the OS's error code."""
    with pytest.raises(script_runner.MissingInterpreter) as e:
        script_runner.argv_for(script, _WIN_PATH, windows=dict.fromkeys(_WIN, ""), which=_which)
    assert str(e.value) == word


# ── on a compute machine ────────────────────────────────────────────────────


@pytest.mark.parametrize("resolved,place,expected", [
    (placement.LOCAL, {"id": "m-compute"}, "m-compute"),
    ("m-admin", {"id": "m-compute"}, "m-admin"),
    ("m-admin", None, "m-admin"),
])
def test_a_step_takes_the_platforms_place_where_a_session_would(agent_tree, monkeypatch, resolved, place, expected):
    from services.apps import app_compute
    monkeypatch.setattr("services.remote.remote_status.resolve_execution_target", lambda *a: (resolved, None))
    monkeypatch.setattr(app_compute, "place", lambda: place)
    row = _deploy_approved(agent_tree, f"place-{resolved[:3].lower()}-{1 if place else 0}", STEP_MANIFEST,
                           {"scripts/sync.sh": SCRIPT})
    identity, _vis = app_steps.identity_for(row)
    assert app_steps.placement_for(row, identity) == expected


def test_a_step_of_the_platforms_place_waits_while_no_compute_machine_serves(agent_tree, monkeypatch):
    """The platform runs no step itself: with no compute machine the step
    is not there to run, as for a machine that is offline. Its delivery is
    armed again and nothing is counted against it."""
    from services.apps import app_compute
    monkeypatch.setattr("services.remote.remote_status.resolve_execution_target",
                        lambda *a: (placement.LOCAL, None))
    monkeypatch.setattr(app_compute, "place", lambda: None)
    row = _deploy_approved(agent_tree, "place-none", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    identity, _vis = app_steps.identity_for(row)
    with pytest.raises(app_steps.StepUnavailable) as away:
        app_steps.placement_for(row, identity)
    assert away.value.reason == "the machine is offline"


def test_a_compute_step_ships_its_sandbox_and_never_its_script(agent_tree, placed):
    from services.scripts import runner as _runner
    row = _deploy_approved(agent_tree, "cstep", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    plan = app_steps.plan_for(row, {"id": "d-compute-1", "handler": "sync", "event": "trigger:t", "payload": {"n": 1}})
    assert plan.target == MACHINE
    spec = app_steps._spec_for(plan, "claim")
    try:
        frame, step_dir = _runner.compute_frame(spec)
        assert "script" not in frame and frame["argv"][-1] == "/app/scripts/sync.sh"
        assert frame["sha256"] == plan.sha256 and frame["timeout"] == plan.timeout
        desc = frame["sandbox"]
        mounts = {b["sandbox"]: b for b in desc["extra_binds"]}
        assert mounts["/app"]["root"] == mounts["/step"]["root"] == "agents"
        assert not mounts["/app"]["rw"] and not mounts["/step"]["rw"]
        assert not any(b["root"] == "abs" for b in (*desc["binds"], *desc["extra_binds"]))
        assert desc["cwd"] == "/workspace"
        assert (step_dir / "payload.json").read_text() == '{"n":1}'
        assert frame["env"]["OTODOCK_STEP_PAYLOAD"] == "/step/payload.json"
        assert frame["env"]["OTODOCK_WORKSPACE_DIR"] == "/workspace"
    finally:
        shutil.rmtree(spec.scratch_root / "d-compute-1", ignore_errors=True)


def test_a_compute_steps_sandbox_holds_its_signed_script_alone(agent_tree, placed):
    """The approval signs the step's script alone, so the folder the machine
    binds at ``/app`` is the run's own, with that script and nothing else of
    the release: a sibling the script sources is not there, and a redeploy
    that changes only the sibling changes nothing a step runs."""
    from services.scripts import runner as _runner
    row = _deploy_approved(agent_tree, "staged", STEP_MANIFEST,
                           {"scripts/sync.sh": SCRIPT, "scripts/lib.sh": "echo sibling\n"})
    plan = app_steps.plan_for(row, {"id": "d-staged-1", "handler": "sync", "event": "trigger:t", "payload": {}})
    spec = app_steps._spec_for(plan, "claim")
    try:
        live = releases.live_release_dir(row)
        assert (live / "scripts" / "lib.sh").is_file()
        assert spec.script_dir is not None and spec.mount_at == "/app"
        assert spec.script_dir != live and not spec.script_dir.is_relative_to(live)
        staged = sorted(p.relative_to(spec.script_dir).as_posix()
                        for p in spec.script_dir.rglob("*") if p.is_file())
        assert staged == ["scripts/sync.sh"]
        assert (spec.script_dir / "scripts" / "sync.sh").read_bytes() == SCRIPT.encode()
        assert spec.script_dir.is_relative_to(spec.scratch_root / "d-staged-1")
        frame, _step_dir = _runner.compute_frame(spec)
        app = next(b for b in frame["sandbox"]["extra_binds"] if b["sandbox"] == "/app")
        assert (agent_tree / app["rel"]).resolve() == spec.script_dir.resolve()
        assert frame["argv"][-1] == "/app/scripts/sync.sh"
    finally:
        shutil.rmtree(spec.scratch_root / "d-staged-1", ignore_errors=True)


def test_a_compute_steps_sandbox_is_the_identitys_mount_row_and_never_config(agent_tree, placed):
    """A shared app's step: the agent's workspace writable, the shared
    knowledge writable while its approver is a manager and read-only once
    they are not, no ``/config``, and the claim in the environment alone."""
    from services.scripts import runner as _runner
    row = _deploy_approved(agent_tree, "mounts", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})

    def frame_of(delivery_id: str) -> dict:
        plan = app_steps.plan_for(row, {"id": delivery_id, "handler": "sync", "event": "trigger:t", "payload": {}})
        spec = app_steps._spec_for(plan, "the-step-claim")
        try:
            return _runner.compute_frame(spec)[0]
        finally:
            shutil.rmtree(spec.scratch_root / delivery_id, ignore_errors=True)

    frame = frame_of("d-mounts-1")
    desc = frame["sandbox"]
    binds = {b["sandbox"]: b for b in desc["binds"]}
    assert set(binds) == {"/workspace", "/knowledge"}
    assert binds["/workspace"]["rw"] and binds["/knowledge"]["rw"]
    # The script's folder, the verified bytes over its name, the scratch.
    assert {b["sandbox"] for b in desc["extra_binds"]} == {"/app", "/app/scripts/sync.sh", "/step"}
    assert frame["env"]["OTODOCK_STEP_TOKEN"] == "the-step-claim" and frame["env"]["OTO_CONFIG_DIR"] == ""
    assert "the-step-claim" not in " ".join(frame["argv"])
    task_store.set_user_agent_role("alice-sub", AGENT, "editor")
    try:
        later = {b["sandbox"]: b for b in frame_of("d-mounts-2")["sandbox"]["binds"]}
    finally:
        task_store.set_user_agent_role("alice-sub", AGENT, "manager")
    assert later["/workspace"]["rw"] and not later["/knowledge"]["rw"]


def test_a_compute_steps_scratch_is_removed_after_its_run(agent_tree, placed):
    row = _deploy_approved(agent_tree, "cstep2", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    plan = app_steps.plan_for(row, {"id": "d-compute-2", "handler": "sync", "event": "trigger:t", "payload": {}})
    result = asyncio.run(app_steps.run_remote(plan, "claim", MACHINE))
    assert result.exit_code == 0 and result.ran_on == MACHINE
    assert placed.sent[0]["type"] == "step_run" and "sandbox" in placed.sent[0]
    assert not (releases.app_release_dir(row) / app_steps.STEPS_DIR_NAME / "d-compute-2").exists()


def test_a_compute_step_its_machine_refuses_leaves_no_staged_script(agent_tree, placed):
    row = _deploy_approved(agent_tree, "cstep4", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})
    plan = app_steps.plan_for(row, {"id": "d-compute-4", "handler": "sync", "event": "trigger:t", "payload": {}})
    placed.answer = lambda msg: RuntimeError("Satellite command error: the sandbox could not be rendered")
    with pytest.raises(app_steps.StepRefused, match="the sandbox could not be rendered"):
        asyncio.run(app_steps.run_remote(plan, "claim", MACHINE))
    assert placed.of("step_run")
    assert not (releases.app_release_dir(row) / app_steps.STEPS_DIR_NAME / "d-compute-4").exists()


def test_a_paired_machine_still_gets_the_scripts_bytes(agent_tree, placed, monkeypatch):
    row = _deploy_approved(agent_tree, "cstep3", STEP_MANIFEST, {"scripts/sync.sh": SCRIPT})

    async def settled(*a, **k):
        return None

    monkeypatch.setattr("core.remote.upload_inflight.wait_settled", settled)
    plan = app_steps.plan_for(row, {"id": "d-paired-1", "handler": "sync", "event": "trigger:t", "payload": {}})
    # The same machine, paired: it shares no folder with the platform.
    placed.compute.clear()
    asyncio.run(app_steps.run_remote(plan, "claim", MACHINE))
    assert placed.sent[0]["script"] == SCRIPT and "sandbox" not in placed.sent[0]
