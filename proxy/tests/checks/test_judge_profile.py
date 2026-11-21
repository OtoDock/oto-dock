"""The judge profile (CHECKS.md): the identity's mount table flipped
read-only (never /config, the CLI state dirs kept), the gate's ``judge`` mode
on the CLI hook and the direct layer, Codex read-only without plan, the
task config builder's judge branch, the remote payload, and the judge kind
end to end against a faked scheduler.

env TEST_DATABASE_URL=postgresql://otodock:otodock@localhost:5433/otodock_test \
    venv/bin/python -m pytest tests/checks/test_judge_profile.py -q
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import placement
from core.sandbox.sandbox import SandboxBuilder, SandboxConfig
from core.session import session_state
from tests._compute import serve

AGENT = "judge-agent"


# ── the mount table ─────────────────────────────────────────────────────────


@pytest.fixture
def tree(tmp_path):
    agents = tmp_path / "agents"
    a = agents / AGENT
    for d in ("workspace", "knowledge", "config", "users/alice/workspace", "users/alice/context",
              "users/alice/.claude", "users/alice/.codex", "knowledge/.credentials"):
        (a / d).mkdir(parents=True)
    (tmp_path / "mcps").mkdir()
    return agents, tmp_path / "mcps"


def _cfg(tree, role="manager", username="alice", *, read_only=False, config_visible=None,
         mount_shared=True):
    agents, mcps = tree
    claude = agents / AGENT / (f"users/{username}/.claude" if username else "workspace/.claude")
    claude.mkdir(parents=True, exist_ok=True)
    return SandboxConfig(role=role, username=username, agent_name=AGENT, is_admin_agent=False,
                         host_agents_dir=agents.resolve(), host_mcps_dir=mcps.resolve(),
                         host_claude_dir=claude.resolve(), config_visible=config_visible,
                         mount_shared=mount_shared, read_only=read_only, net_forwards=["8400"])


def _rw(cfg) -> dict[str, bool]:
    return {m.sandbox: m.rw for m in SandboxBuilder(cfg).workspace_mount_table()}


@pytest.mark.parametrize("role,username,mount_shared", [
    ("manager", "alice", True), ("editor", "alice", True), ("viewer", "alice", True),
    ("manager", "", True), ("viewer", "", True), ("manager", "alice", False),
])
def test_a_judge_sees_what_the_session_sees_and_writes_nothing(tree, role, username, mount_shared):
    session = _rw(_cfg(tree, role, username, config_visible=False, mount_shared=mount_shared))
    judge = _rw(_cfg(tree, role, username, config_visible=False, mount_shared=mount_shared,
                     read_only=True))
    # The same rows, nothing more; every row read-only except the CLI state.
    assert set(judge) == set(session)
    for dest, rw in judge.items():
        last = dest.rstrip("/").rsplit("/", 1)[-1]
        if last in (".claude", ".codex", ".credentials"):
            assert rw == session[dest]
        else:
            assert rw is False, dest
    assert "/config" not in judge


def test_a_judge_runs_from_a_home_of_its_own_and_may_write_there(tree):
    """A judge gets a home like any session that runs from its scope's folder
    (``session_home_for``), so on a machine that makes homes no session
    writes the store's own files. The read-only profile flips the identity's
    table; the home is bound apart from it and stays writable."""
    import dataclasses

    from core.sandbox import session_home
    from core.sandbox.session_config_dir import session_home_for
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    cfg = _cfg(tree, read_only=True, config_visible=False)
    store = cfg.host_claude_dir
    pl = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1",
                                         session_homes=frozenset({".claude", ".codex"}))
    ctx = SimpleNamespace(placement=pl, external_home="", wrapup="", open_line=False, read_only=True)
    home = session_home_for(str(store), sid, execution_path="claude-code-cli", ctx=ctx)
    assert home == f"{store}/own/{sid}"
    session_home.prepare(store, sid, ("projects",))
    desc = SandboxBuilder(dataclasses.replace(cfg, session_home=home)).describe()
    assert desc.home is not None and desc.home.rw is True
    assert desc.home.sandbox == f"/users/alice/{store.name}/own/{sid}"


def test_a_judge_never_mounts_config_whatever_the_role(tree):
    # A manager's ordinary session mounts /config RW; the judge's build passes
    # config_visible=False and gets none of it.
    assert _rw(_cfg(tree, "manager", "alice", config_visible=True))["/config"] is True
    assert "/config" not in _rw(_cfg(tree, "manager", "alice", config_visible=False, read_only=True))


def test_the_direct_layer_refuses_writes_through_the_same_table(tree, monkeypatch):
    import config
    from core.layers.direct import files as df
    monkeypatch.setattr(config, "AGENTS_DIR", tree[0])
    cfg = _cfg(tree, "manager", "alice", config_visible=False, read_only=True)
    mounts = df.mount_table(cfg)
    cwd = df.session_cwd(cfg)
    with pytest.raises(df.FileToolError) as e:
        df.locate(mounts, "/users/alice/workspace/a.md", cwd=cwd, writing=True)
    assert "read-only" in str(e.value)
    assert df.locate(mounts, "/users/alice/workspace/a.md", cwd=cwd)[0] == "/users/alice/workspace/a.md"


# ── the gate ────────────────────────────────────────────────────────────────


@pytest.fixture
def judge_session(monkeypatch):
    from auth.path_policy import SecurityContext
    sid = "sess-judge-gate"
    monkeypatch.setattr("api.hooks.permission.verify_session_match_async", AsyncMock(return_value=None))
    session_state._sessions[sid] = {"client_type": "task", "check": True}
    session_state._session_security[sid] = SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True, read_only=True)
    session_state.set_session_mode(sid, "judge")
    yield sid
    session_state._sessions.pop(sid, None)
    session_state._session_modes.pop(sid, None)
    session_state._session_security.pop(sid, None)
    session_state._session_tool_allows.pop(sid, None)
    session_state._permission_emitters.pop(sid, None)


async def _decide(sid, tool, tool_input=None):
    from api.hooks.permission import HookPermissionRequest, hook_permission
    req = HookPermissionRequest(session_id=sid, tool_name=tool, tool_input=tool_input or {})
    return await hook_permission(req, authorization=None)


@pytest.mark.asyncio
async def test_the_judge_mode_reads_and_never_writes(judge_session):
    sid = judge_session
    assert (await _decide(sid, "Read", {"file_path": "/workspace/a.py"}))["decision"] == "allow"
    assert (await _decide(sid, "Grep", {"pattern": "x", "path": "/workspace"}))["decision"] == "allow"
    out = await _decide(sid, "Write", {"file_path": "/workspace/a.py", "content": "x"})
    assert out["decision"] == "deny" and "judge reads" in out.get("reason", "")
    assert (await _decide(sid, "Edit", {"file_path": "/workspace/a.py"}))["decision"] == "deny"
    out = await _decide(sid, "Bash", {"command": "cat /workspace/a.py"})
    assert out["decision"] == "allow", out
    out = await _decide(sid, "Bash", {"command": "git status"})
    assert out["decision"] == "allow", out
    assert (await _decide(sid, "Bash", {"command": "rm /workspace/a.py"}))["decision"] == "deny"
    assert (await _decide(sid, "Bash", {"command": "echo hi > /workspace/b.txt"}))["decision"] == "deny"
    assert (await _decide(sid, "Bash", {"command": "npm run build"}))["decision"] == "deny"
    # An MCP tool the check listed (the session has no other) runs.
    assert (await _decide(sid, "mcp__demo__do_thing"))["decision"] == "allow"


def test_the_direct_gate_agrees():
    from core.layers.direct import builtins as b
    assert b.decide(b.TIER_OPEN, "judge") == "allow"
    assert b.decide(b.TIER_EDIT, "judge") == "deny"
    assert b.decide(b.TIER_DESTRUCTIVE, "judge") == "deny"
    assert b.decide(b.TIER_EDIT, "plan") == "deny"
    assert b.decide(b.TIER_EDIT, "autoReview") == "allow"


def test_a_judges_codex_sandbox_is_read_only():
    """On a machine paired with full access too. The machine runs a judge in
    the default collaboration mode, never plan, by the mode it is shipped
    (``satellite/tests/test_codex_session.py``,
    ``test_plan_follows_the_live_sandbox_unless_a_judge``)."""
    from core.layers.codex.helpers import permission_to_sandbox
    assert permission_to_sandbox("judge") == "read-only"
    assert permission_to_sandbox("plan") == "read-only"
    assert permission_to_sandbox("judge", allow_full_fs=True) == "read-only"


def test_a_judge_leaves_the_scopes_settings_as_every_session_has_them(tmp_path, monkeypatch):
    """The judge's config dir is the judged person's (same scope), and the
    CLI reloads settings.json live: a judge's write denials there took Write
    and Edit off that person's live chats, the fix round included. They ride
    the judge's argv and the hook floor instead."""
    import config
    from core.session.session_manager import get_layer_by_path
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / "demo" / "users" / "alice").mkdir(parents=True)
    layer = get_layer_by_path("claude-code-cli")
    d2 = layer.prepare_config_dir("demo", username="alice", scope="user")
    chat = (Path(d2) / "settings.json").read_text()
    d = layer.prepare_config_dir("demo", username="alice", scope="user", read_only=True)
    assert d == d2
    assert (Path(d) / "settings.json").read_text() == chat
    assert "Write" not in json.loads(chat)["permissions"]["deny"]
    # The same for an external caller's no-shell rule in a shared scope.
    layer.prepare_config_dir("demo", username="alice", scope="user", no_shell=True)
    assert (Path(d) / "settings.json").read_text() == chat
    # The judge's own process still loses them (the argv's list).
    assert {"Write", "Edit", "MultiEdit", "NotebookEdit"} <= set(
        layer.session_denied_tools(external=False, read_only=True))


# ── the task config builder ────────────────────────────────────────────────


def _stub_builder(monkeypatch, captured: dict, *, target=("local", None)):
    import core.layers.cli.config_dir as cli_cd
    import core.sandbox.oto_env as oe
    import core.session.visibility as v
    from core.config import task_config_builder as tcb

    def _claude_dir(agent_name, *, username="", scope="user", **kw):
        captured["dir_kwargs"] = kw
        return Path("/tmp/agents/x/users/u/.claude")
    monkeypatch.setattr(cli_cd, "ensure_persistent_claude_dir", _claude_dir)
    monkeypatch.setattr(tcb, "resolve_task_identity", lambda *a, **k: tcb.TaskIdentity(
        username="alice", role="manager", scope="user", creds_user_sub="alice-sub"))
    # The judged turn's person: a contributor who holds the agent (a judge is
    # paid like the turn it judges, ``core/config/spender.py``).
    from auth import roles
    from storage import database as task_store
    task_store.upsert_user("alice-sub", "alice@judge.test", "Alice", "member")
    task_store.add_user_agent("alice-sub", AGENT, roles.CONTRIBUTOR, "alice-sub")
    monkeypatch.setattr(tcb.agent_store, "get_delegation_targets", lambda *a, **k: [])
    monkeypatch.setattr(tcb.agent_store, "is_admin_only", lambda *a, **k: False)
    monkeypatch.setattr(tcb.agent_store, "get_agent", lambda *a, **k: {
        "execution_path": "claude-code-cli", "default_execution_mode": "interactive"})

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(tcb.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(tcb.remote_status, "resolve_execution_target", lambda *a, **k: target)
    # A compute machine serves the platform's place: a run resolved there is
    # built on it (the platform runs no session itself).
    serve(monkeypatch)

    def _placement_of(value, *a, compute=None, **k):
        if compute:
            return placement.from_machine(placement.KIND_COMPUTE, compute)
        return (placement.LOCAL_PLACEMENT if placement.is_local(value)
                else placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, label="laptop", has_display=False))
    monkeypatch.setattr(tcb.remote_store, "placement_of", _placement_of)
    monkeypatch.setattr(tcb.remote_store, "get_target_browser_settings", lambda *a, **k: None)

    def _build_mcp(*a, **k):
        captured["only_mcps"] = k.get("only_mcps", "absent")
        return (None, {}, {}, {}, set())
    monkeypatch.setattr(tcb.mcp_registry, "build_session_mcp_config", _build_mcp)
    monkeypatch.setattr(tcb.mcp_registry, "get_agent_mcps", lambda *a, **k: [])
    vis = SimpleNamespace(mount_username="alice", mount_scope="user", config_visible=True,
                          available_scopes=("user", "agent"), memory_user_enabled=False,
                          memory_agent_enabled=True, effective_default_scope="user",
                          mount_shared=True)
    monkeypatch.setattr(v, "resolve_visibility", lambda *a, **k: vis)

    async def _no_dyn(*a, **k):
        return []
    monkeypatch.setattr(tcb.dynamic_context, "get_dynamic_contexts", _no_dyn)
    monkeypatch.setattr(tcb.config, "build_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr("configuration.models.get_cli_model", lambda *a, **k: "m")
    monkeypatch.setattr(tcb.config, "get_cli_effort", lambda *a, **k: "")
    monkeypatch.setattr(tcb.subscription_pool, "resolve_subscription_env",
                        lambda *a, **k: ("sub-test", {}))
    monkeypatch.setattr(oe, "build_oto_env", lambda **k: {})
    monkeypatch.setattr(oe, "OTO_MULTI_VALUE_ENVS", {}, raising=False)
    from core.remote import satellite_connection as sc, satellite_link
    monkeypatch.setattr(sc, "get_connection_manager",
                        lambda: SimpleNamespace(satellite_supports_pty=lambda *_: True,
                                                compute_machine_ids=frozenset,
                                                is_compute=lambda _mid: False,
                                                exit_state=lambda _mid: (0, False),
                                                exit_report=lambda _mid: {},
                                                is_connected=lambda _mid: True,
                                                links=satellite_link.MachineLinks(None)))


def _task(**over):
    base = dict(scope="user", created_by="alice-sub", task_type="check", notification_mode="none",
                override_model=None, override_execution_path=None, override_execution_mode=None,
                judge={"check": "coding", "mcps": ["file-tools"], "judge_on": "auto", "for_chat": "c1"},
                execution_target_override="", agent=AGENT, id="check-1", name="Check: coding",
                prompt="p", timeout_seconds=600, extra_context=[])
    base.update(over)
    return SimpleNamespace(**base)


def test_the_builders_judge_branch(monkeypatch):
    from core.config import task_config_builder as tcb
    captured: dict = {}
    _stub_builder(monkeypatch, captured)
    cfg = asyncio.run(tcb.build_task_agent_config(AGENT, _task(), "sess-j"))
    assert cfg.permission_mode == "judge" and cfg.client_type == "task"
    assert cfg.security_context.read_only is True
    assert cfg.interactive is False                      # never a terminal
    assert captured["only_mcps"] == ["file-tools"]
    # The scope's shared settings.json never carries the judge's denials.
    assert "extra_deny" not in captured["dir_kwargs"]
    assert "You are judging" in cfg.system_prompt
    # /config never mounts for a judge, whatever the creator's tier (the
    # visibility stub says config_visible=True — an owner-tier creator).
    assert cfg.security_context.config_visible is False
    # The memory and checks MCPs are dropped even when a hand-edited
    # document lists them (the routes refuse them before this).
    cfg3 = asyncio.run(tcb.build_task_agent_config(
        AGENT, _task(judge={"check": "coding", "mcps": ["memory-mcp", "file-tools", "checks-mcp"],
                           "judge_on": "auto", "for_chat": "c1"}), "sess-k"))
    assert captured["only_mcps"] == ["file-tools"] and cfg3.security_context.read_only is True
    # An ordinary task keeps everything as it was.
    cfg2 = asyncio.run(tcb.build_task_agent_config(AGENT, _task(judge=None, task_type="scheduled"), "sess-t"))
    assert cfg2.permission_mode == "auto" and cfg2.security_context.read_only is False
    assert captured["only_mcps"] is None and cfg2.interactive is True
    assert "You are judging" not in cfg2.system_prompt


def test_a_judges_contexts_follow_the_checks_mcps(monkeypatch):
    """The prompt contexts and the permission block cover the judge's MCPs
    only, as its MCP config does: a judge of an ssh-hosts agent is not
    handed the hosts."""
    from core.config import task_config_builder as tcb
    captured: dict = {}
    _stub_builder(monkeypatch, captured)
    monkeypatch.setattr(tcb.mcp_registry, "get_agent_mcps", lambda *a, **k: [
        SimpleNamespace(name=n, path_env=None) for n in ("ssh-hosts", "file-tools", "memory-mcp")])
    seen: list[list[str]] = []

    async def _dyn(agent, names, **k):
        seen.append(list(names))
        return []
    monkeypatch.setattr(tcb.dynamic_context, "get_dynamic_contexts", _dyn)
    asyncio.run(tcb.build_task_agent_config(AGENT, _task(), "sess-j"))
    assert seen[-1] == ["file-tools"]
    asyncio.run(tcb.build_task_agent_config(AGENT, _task(judge=None, task_type="scheduled"), "sess-t"))
    assert seen[-1] == ["ssh-hosts", "file-tools", "memory-mcp"]


def test_the_target_override_wins(monkeypatch):
    from core.config import task_config_builder as tcb
    captured: dict = {}
    _stub_builder(monkeypatch, captured, target=("machine-9", None))
    cfg = asyncio.run(tcb.build_task_agent_config(AGENT, _task(execution_target_override="local"), "s"))
    assert cfg.execution_target == "local"
    cfg2 = asyncio.run(tcb.build_task_agent_config(AGENT, _task(), "s2"))
    assert cfg2.execution_target == "machine-9"
    assert cfg.fallback_reason is None and cfg2.fallback_reason is None


def test_a_judges_machine_the_guard_refuses_runs_on_the_resolvers_answer(monkeypatch):
    """The judge mirrors the judged session's machine only when the guard
    lets the judge's run there: the creator's own machine, yes; another
    person's machine (or a personal one for an agent-scope judge), no."""
    from core.config import task_config_builder as tcb
    from services.remote import chat_targets
    captured: dict = {}
    _stub_builder(monkeypatch, captured, target=("machine-9", None))
    rows = {"m-alice": {"id": "m-alice", "name": "alice-laptop", "pairing_scope": "user",
                        "registered_by": "alice-sub"},
            "m-bob": {"id": "m-bob", "name": "bob-laptop", "pairing_scope": "user",
                      "registered_by": "bob-sub"}}
    monkeypatch.setattr(tcb.remote_store, "get_remote_machine", rows.get)
    monkeypatch.setattr(chat_targets, "user_machines_allowed", lambda: True)
    cfg = asyncio.run(tcb.build_task_agent_config(AGENT, _task(execution_target_override="m-alice"), "s"))
    assert cfg.execution_target == "m-alice"
    cfg2 = asyncio.run(tcb.build_task_agent_config(AGENT, _task(execution_target_override="m-bob"), "s2"))
    assert cfg2.execution_target == "machine-9" and cfg2.fallback_reason is None


# ── the remote payload ─────────────────────────────────────────────────────


def _remote_layer():
    from core.remote.remote_execution import RemoteExecutionLayer
    lay = RemoteExecutionLayer(MagicMock())
    lay._cm.satellite_version.return_value = "0.5.171"
    return lay


def _remote_config(**over):
    from auth.path_policy import SecurityContext
    from core.execution_layer import AgentConfig
    base = dict(agent_name="test-agent", execution_target="machine-1", system_prompt="p",
                model="m", effort="medium", permission_mode="judge", client_type="task", extra_env={},
                security_context=SecurityContext(role="manager", username="alice", agent="test-agent",
                                                 is_admin_agent=False, read_only=True))
    base.update(over)
    return AgentConfig(**base)


@pytest.mark.asyncio
async def test_a_judge_on_a_machine_ships_the_read_only_sandbox():
    machine = {"capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
               "pairing_scope": "admin"}
    with patch("storage.remote_store.get_remote_machine", return_value=machine):
        codex = (await _remote_layer()._build_start_payload("s1", _remote_config(), "codex-cli")).payload
        claude = (await _remote_layer()._build_start_payload("s3", _remote_config(), "claude-code-cli")).payload
    assert codex["sandbox_mode"] == "read-only" and codex["permission_mode"] == "judge"
    # The satellite writes this list into the scope's shared settings.json:
    # the judge's write tools stay off it (the gate over the tunnel refuses them).
    assert not {"Write", "Edit", "MultiEdit", "NotebookEdit"} & set(claude["disallowed_tools"])


# ── the judge kind ──────────────────────────────────────────────────────────


@pytest.fixture
def judged(temp_db, tmp_path, monkeypatch):
    import config
    from auth.path_policy import SecurityContext
    from storage import database as task_store
    from storage.agents import agent_store
    from storage.pg import get_conn
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", (tmp_path / "agents").resolve())
    agent_store.create_agent(AGENT, "Judge Agent", created_by="alice-sub")
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    (tmp_path / "agents" / AGENT / "knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / AGENT / "knowledge" / "style.md").write_text("Be terse.\n")
    chat = task_store.create_chat("chat-judged", "alice-sub", AGENT, "auto", model="m", title="Fix the parser")
    from services.checks import changed_set as cs
    target = cs.Target(session_id="sess-judged", kind="chats", agent=AGENT, chat=chat,
                       username="alice", user_sub="alice-sub", role="manager", scope="user",
                       engine="claude", client_type="dashboard",
                       security=SecurityContext(role="manager", username="alice", agent=AGENT,
                                                is_admin_agent=False),
                       cwd="/users/alice")
    return target


def _check(**judge_over):
    from services.checks import documents as d
    judge = {"rubric": "Is the code correct?", "timeout": 60}
    judge.update(judge_over)
    doc = d.validate_check_doc({"name": "coding", "inputs": ["knowledge/style.md"], "judge": judge})
    return d.CheckDoc(agent=AGENT, owner="", name="coding", doc=doc, folder=Path("/nowhere"),
                      doc_sha256="x")


_RUN_COUNTER = [0]


def _fake_scheduler(monkeypatch, answers: list[str]):
    """``trigger_task_now`` replaced by a run that answers with the next text
    (a run row, a chat, an assistant message, the terminal write that fires
    the on_run_finished hook)."""
    from services.scheduler import scheduler
    from storage import database as task_store
    seen: list = []
    counter = _RUN_COUNTER

    async def fake_trigger(task, trigger_type="manual", trigger_source=None, prompt_override=None,
                           trigger_payload=None):
        seen.append(task)
        counter[0] += 1
        run_id = f"run-{counter[0]}"
        chat_id = task.target_chat_id or f"task-{run_id}"
        task_store.create_run(run_id, task.id, task.agent, trigger_type, trigger_source, task.prompt,
                              task_type="check", scope=task.scope, created_by=task.created_by)
        if not task.target_chat_id:
            task_store.create_chat(chat_id, task.created_by or f"task::{task.agent}", task.agent, "auto",
                                   model="judge-model", execution_path="claude-code-cli",
                                   source_type="task", title=task.name)
        task_store.update_run(run_id, chat_id=chat_id, session_id="sess-judge-1",
                              started_at="2026-09-17T10:00:00+00:00")
        text = answers.pop(0) if answers else ""
        if text:
            task_store.add_chat_message(chat_id, "assistant", text)
        task_store.update_run(run_id, status="completed", cost_usd=1.25,
                              completed_at="2026-09-17T10:00:05+00:00")
        task._run_id = run_id
        return run_id
    monkeypatch.setattr(scheduler, "trigger_task_now", fake_trigger)
    return seen


def _changed(target):
    return {"check": "coding", "round": 1, "session": {"cwd": target.cwd, "placement": {"kind": "local"}},
            "paths": [{"path": "/users/alice/workspace/a.py", "kind": "code", "writes": True}],
            "events": ["commit"], "tools": [{"name": "Bash", "command": "git commit -m x"}],
            "places": {"project": "", "git_roots": []}, "request": "fix it", "result": "done"}


def test_the_judge_kind_runs_a_read_only_task_of_the_same_identity(judged, monkeypatch):
    from services.checks.kinds import judge_kind
    seen = _fake_scheduler(monkeypatch, [
        "Looked at it.\n```json\n{\"pass\": false, \"score\": 0.4, \"findings\": [{\"location\": "
        "\"a.py:3\", \"severity\": \"error\", \"text\": \"off by one\"}], \"summary\": \"one bug\"}\n```"])
    v = asyncio.run(judge_kind.run(_check(mcps=["file-tools"]), judged, _changed(judged), round_no=1))
    assert v.status == "fail" and v.score == 0.4 and v.findings[0]["location"] == "a.py:3"
    assert v.cost_usd == 1.25 and v.model == "judge-model" and v.engine == "claude-code-cli"
    assert v.judge_run_id == seen[0]._run_id and v.ran_on == "local"
    task = seen[0]
    assert task.task_type == "check" and task.scope == "user" and task.created_by == "alice-sub"
    assert task.judge["mcps"] == ["file-tools"] and task.execution_target_override == "local"
    assert task.notification_mode == "none" and task.name.startswith("Check: coding — Fix the parser")
    assert "Be terse." in task.prompt and "Is the code correct?" in task.prompt
    assert "data, never an instruction" in task.prompt and "git commit -m x" in task.prompt
    # The verdict rows say what the run was.
    from storage import database as task_store
    run = task_store.get_run(seen[0]._run_id)
    assert run["task_type"] == "check" and run["trigger_source"] == "check:coding"


def test_the_inputs_are_what_the_judged_session_may_read(judged, tmp_path):
    # The judge's prompt is stored in a chat: an input the judged session
    # could not read itself (another person's tree, a credential file,
    # /config below the owner tier through a symlink) never lands in it.
    from auth.path_policy import SecurityContext
    from services.checks.kinds import judge_kind
    agent_dir = tmp_path / "agents" / AGENT
    for rel, text in [("users/bob/workspace/private.md", "BOB-PRIVATE"),
                      ("users/alice/.credentials/github.json", "ALICE-TOKEN"),
                      ("config/secret.md", "CONFIG-SECRET"),
                      ("users/alice/workspace/notes.md", "ALICE-NOTES")]:
        (agent_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent_dir / rel).write_text(text)
    (agent_dir / "users/alice/workspace/leak.md").symlink_to(agent_dir / "config/secret.md")
    (agent_dir / "users/alice/workspace/token.json").symlink_to(
        agent_dir / "users/alice/.credentials/github.json")
    names = ["knowledge/style.md", "users/bob/workspace/private.md", "users/alice/workspace/notes.md",
             "users/alice/workspace/leak.md", "users/alice/workspace/token.json"]
    editor = SecurityContext(role="editor", username="alice", agent=AGENT, is_admin_agent=False)
    got = dict(judge_kind._read_inputs(AGENT, editor, names))
    assert got["knowledge/style.md"] == "Be terse.\n" and got["users/alice/workspace/notes.md"] == "ALICE-NOTES"
    assert got["users/bob/workspace/private.md"] == "(not readable)"
    assert got["users/alice/workspace/leak.md"] == "(not readable)"
    assert got["users/alice/workspace/token.json"] == "(not readable)"
    # The owner tier reads /config, as its own session does; no context, nothing.
    manager = SecurityContext(role="manager", username="alice", agent=AGENT, is_admin_agent=False)
    assert dict(judge_kind._read_inputs(AGENT, manager, names))["users/alice/workspace/leak.md"] == "CONFIG-SECRET"
    assert dict(judge_kind._read_inputs(AGENT, None, names))["knowledge/style.md"] == "(not readable)"


def test_a_retired_judge_model_follows_its_successor(judged, monkeypatch):
    # judge.model is a pin the boot walk never sees (it lives in the check
    # document): a retired id resolves through MODEL_SUCCESSORS at read time,
    # a current or custom id passes through unchanged.
    from services.checks.kinds import judge_kind
    from storage.billing import subscription_store
    monkeypatch.setattr(subscription_store, "list_models", lambda layer=None: [
        {"model_id": "claude-opus-5-5", "enabled": True}, {"model_id": "judge-model", "enabled": True}])
    ok = "```json\n{\"pass\": true, \"summary\": \"fine\"}\n```"
    seen = _fake_scheduler(monkeypatch, [ok])
    asyncio.run(judge_kind.run(_check(model="claude-opus-5"), judged, _changed(judged), round_no=1))
    assert seen[0].override_model == "claude-opus-5-5"
    seen = _fake_scheduler(monkeypatch, [ok])
    asyncio.run(judge_kind.run(_check(model="judge-model"), judged, _changed(judged), round_no=1))
    assert seen[0].override_model == "judge-model"
    seen = _fake_scheduler(monkeypatch, [ok])
    asyncio.run(judge_kind.run(_check(), judged, _changed(judged), round_no=1))
    assert seen[0].override_model is None
    # F67: a pin the agent does not enable ends as an error, never a run.
    seen = _fake_scheduler(monkeypatch, [ok])
    v = asyncio.run(judge_kind.run(_check(model="not-enabled"), judged, _changed(judged), round_no=1))
    assert v.status == "error" and "not available" in v.reason and seen == []


def test_a_missing_verdict_is_asked_for_once_then_an_error(judged, monkeypatch):
    from services.checks.kinds import judge_kind
    seen = _fake_scheduler(monkeypatch, ["no json here", "```json\n{\"pass\": true, \"summary\": \"fine\"}\n```"])
    v = asyncio.run(judge_kind.run(_check(), judged, _changed(judged), round_no=1))
    assert v.status == "pass" and len(seen) == 2
    assert seen[1].continue_session == "sess-judge-1"
    assert seen[1].target_chat_id == f"task-{seen[0]._run_id}"
    assert v.cost_usd == 2.5
    seen2 = _fake_scheduler(monkeypatch, ["nothing", "still nothing"])
    v2 = asyncio.run(judge_kind.run(_check(), judged, _changed(judged), round_no=1))
    assert v2.status == "error" and "no verdict" in v2.reason and len(seen2) == 2


_PASS = "```json\n{\"pass\": true, \"summary\": \"fine\"}\n```"


def _closes(monkeypatch) -> list:
    """The one closer replaced by a record of what it was asked to close."""
    from core.session import chat_rebuild
    closes: list = []

    async def close(chat, *, reason, require_idle, lock, fallback_layer=None):
        closes.append((chat.get("id"), chat.get("session_id"), reason, require_idle, lock))
        return True
    monkeypatch.setattr(chat_rebuild, "close_chat_session", close)
    return closes


def _judge(check, judged):
    """The judge section, then the close it started beside its verdict."""
    from core import background
    from services.checks.kinds import judge_kind

    async def go():
        v = await judge_kind.run(check, judged, _changed(judged), round_no=1)
        await asyncio.gather(*background.running("checks"))
        return v
    return asyncio.run(go())


def test_a_judges_session_closes_with_its_verdict(judged, monkeypatch):
    # Nothing continues a judge's run: its session closes once the verdict
    # is read, through the one closer, which keeps the judge's chat. The
    # judged session, which gets the findings, is never the one closed.
    closes = _closes(monkeypatch)
    seen = _fake_scheduler(monkeypatch, [
        "```json\n{\"pass\": false, \"findings\": [{\"location\": \"a.py:3\", \"severity\": \"error\", "
        "\"text\": \"off by one\"}], \"summary\": \"one bug\"}\n```"])
    v = _judge(_check(), judged)
    assert v.status == "fail" and v.findings[0]["location"] == "a.py:3"
    assert closes == [(f"task-{seen[0]._run_id}", "sess-judge-1", "judged", True, True)]
    assert judged.session_id not in {c[1] for c in closes}


def test_a_judge_asked_again_closes_after_its_second_answer(judged, monkeypatch):
    # The re-prompt continues the judge's open session: nothing is closed
    # before it, and the one close comes after the second answer, also when
    # that answer still carries no verdict.
    from core import background
    from services.scheduler import scheduler
    for answers, status in ((["no json here", _PASS], "pass"), (["nothing", "still nothing"], "error")):
        closes = _closes(monkeypatch)
        seen = _fake_scheduler(monkeypatch, answers)
        inner = scheduler.trigger_task_now

        async def trigger(task, inner=inner, closes=closes, **kw):
            assert not closes and not background.running("checks"), "closed before the re-prompt"
            return await inner(task, **kw)
        monkeypatch.setattr(scheduler, "trigger_task_now", trigger)
        v = _judge(_check(), judged)
        assert v.status == status and len(seen) == 2
        assert seen[1].continue_session == "sess-judge-1"
        assert closes == [(f"task-{seen[0]._run_id}", "sess-judge-1", "judged", True, True)]


def test_a_reprompt_that_started_a_fresh_session_closes_that_one(judged, monkeypatch):
    # A continue whose conversation could not be resumed runs in a fresh
    # session of the same chat (TaskRun, "no resumable conversation"): the
    # session left warm is that one.
    from services.scheduler import scheduler
    from storage import database as task_store
    closes = _closes(monkeypatch)
    seen = _fake_scheduler(monkeypatch, ["no json here", _PASS])
    inner = scheduler.trigger_task_now

    async def trigger(task, **kw):
        run_id = await inner(task, **kw)
        if task.continue_session:
            task_store.update_run(run_id, session_id="sess-judge-2")
        return run_id
    monkeypatch.setattr(scheduler, "trigger_task_now", trigger)
    v = _judge(_check(), judged)
    assert v.status == "pass" and len(seen) == 2
    assert closes == [(f"task-{seen[0]._run_id}", "sess-judge-2", "judged", True, True)]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_judge_run_that_did_not_complete_is_closed_by_its_run(judged, monkeypatch, status):
    # A failed or cancelled run's session is closed by the run itself
    # (TaskRun.failed, TaskRun.cancelled): the judge asks no second close.
    from services.checks.kinds import judge_kind
    closes = _closes(monkeypatch)

    async def one_run(task, *, timeout, cancelled, trigger_source):
        return {"id": "run-x", "status": status, "chat_id": "task-run-x", "session_id": "sess-x"}
    monkeypatch.setattr(judge_kind, "_one_run", one_run)
    v = _judge(_check(), judged)
    assert v.status == "error" and closes == []


def test_a_slow_close_never_holds_the_verdict(judged, monkeypatch):
    # The close runs beside the verdict, tracked as a background task: the
    # evaluator stores the verdict without waiting for it, and a planned stop
    # gives it its grace (core.background.cancel_all).
    from core import background
    from core.session import chat_rebuild
    from services.checks.kinds import judge_kind
    _fake_scheduler(monkeypatch, [_PASS])
    started: list = []

    async def close(chat, **kw):
        started.append(chat["session_id"])
        await asyncio.Event().wait()
    monkeypatch.setattr(chat_rebuild, "close_chat_session", close)

    async def go():
        v = await asyncio.wait_for(judge_kind.run(_check(), judged, _changed(judged), round_no=1), 5)
        pending = background.running("checks")
        await asyncio.sleep(0)
        for t in pending:
            t.cancel()
        return v, len(pending)
    v, pending = asyncio.run(go())
    assert v.status == "pass" and pending == 1 and started == ["sess-judge-1"]


def test_a_turn_open_on_the_judges_chat_keeps_its_session_for_the_reaper(judged, monkeypatch):
    # A person who opened the judge's chat and spoke in it: the closer
    # refuses (ChatBusy), the refusal is no failure, the idle reaper takes
    # the session later as before.
    from core import background
    from core.session import chat_rebuild
    from services.checks.kinds import judge_kind
    _fake_scheduler(monkeypatch, [_PASS])

    async def close(chat, **kw):
        raise chat_rebuild.ChatBusy(chat["id"])
    monkeypatch.setattr(chat_rebuild, "close_chat_session", close)

    async def go():
        v = await judge_kind.run(_check(), judged, _changed(judged), round_no=1)
        tasks = background.running("checks")
        await asyncio.gather(*tasks)
        return v, tasks
    v, tasks = asyncio.run(go())
    assert v.status == "pass" and len(tasks) == 1 and tasks[0].exception() is None


def test_the_threshold_and_the_cap_and_the_platform_placement(judged, monkeypatch):
    from services.checks.kinds import judge_kind
    from storage.checks import db_checks
    _fake_scheduler(monkeypatch, ["```json\n{\"pass\": false, \"score\": 0.8}\n```"])
    v = asyncio.run(judge_kind.run(_check(threshold=0.7), judged, _changed(judged), round_no=1))
    assert v.status == "pass"                       # the score decides
    # The cap: spent today from the judge runs' usage.
    from storage import database as task_store
    from storage.billing import db_usage
    last_chat = task_store.get_run(f"run-{_RUN_COUNTER[0]}")["chat_id"]
    db_usage.insert_usage_record("alice-sub", AGENT, "user", "chat", last_chat, 3.0)
    db_checks.set_settings(AGENT, daily_cap_usd=2.0, updated_by="alice-sub")
    v2 = asyncio.run(judge_kind.run(_check(), judged, _changed(judged), round_no=1))
    assert v2.status == "skipped" and "budget" in v2.reason
    db_checks.set_settings(AGENT, daily_cap_usd=None, updated_by="alice-sub")
    # judge_on platform on a session outside the synced trees: a visible error.
    judged.work_cwd = "/home/dev/repo"
    v3 = asyncio.run(judge_kind.run(_check(judge_on="platform"), judged, _changed(judged), round_no=1))
    assert v3.status == "error" and "outside the synced" in v3.reason


def test_a_cut_evaluation_stops_the_judge_run(judged, monkeypatch):
    # The evaluator's wall-clock budget holds every judge wait (the run, the
    # re-prompt, the sync pause); when it cuts anyway, the run is cancelled
    # rather than left spending for a verdict nobody reads.
    from services.checks import evaluator
    from services.scheduler import scheduler
    from storage import database as task_store
    check = _check(timeout=600)
    assert evaluator._budget_s(check) >= evaluator.EVALUATION_MARGIN_S + 600 + 30 + 300 + 30 + 30
    task_store.create_run("run-hung", "check-hung", AGENT, "check", "check:coding", "p",
                          task_type="check", scope="user", created_by="alice-sub")
    cancelled: list = []
    monkeypatch.setattr(scheduler, "platform_cancel_run", lambda rid, reason: cancelled.append(rid))

    from services.scheduler import run_wait

    async def cut():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(run_wait.wait_run(
                "run-hung", timeout=60, cancelled=lambda: False, what="the check's judge"), 0.3)
    asyncio.run(cut())
    assert cancelled == ["run-hung"] and "run-hung" not in run_wait._run_futures


@pytest.mark.parametrize("command,reads", [
    ("git status", True),
    ("git -C /workspace/repo log --oneline -5", True),
    ("git --no-pager diff HEAD~1", True),
    ("git branch -a", True),
    ("git remote -v", True),
    ("git stash list", True),
    ("git worktree list", True),
    # A second command after a newline, a pipe or a redirect.
    ("git status\npython3 /tmp/x.py", False),
    ("git log | sh", False),
    ("git diff > out.txt", False),
    # Flags that write a file, run a program or read outside the repository.
    ("git diff --output=/workspace/x", False),
    ("git log --output /workspace/x", False),
    ("git grep -Oless pattern", False),
    ("git grep --open-files-in-pager=vi x", False),
    ("git diff --ext-diff", False),
    ("git show --textconv HEAD:a", False),
    ("git cat-file --filters HEAD:a", False),
    ("git diff --no-index /etc/passwd a", False),
    ("git -c core.pager=sh log", False),
    # The changing forms of the listing subcommands.
    ("git branch -D main", False),
    ("git branch newname", False),
    ("git remote add x https://example.com/r.git", False),
    ("git remote set-url origin https://example.com/r.git", False),
    ("git tag -d v1", False),
    ("git stash drop", False),
    ("git push", False),
    ("git commit -m x", False),
    ("git", False),
    # git takes an unambiguous abbreviation of a long option and bundles short
    # ones: each of these ran a program in a probe on git 2.34.1.
    ("git grep --open=/x a", False),
    ("git grep --open-files=/x a", False),
    ("git grep -iO/x a", False),
    ("git grep -nO/x a", False),
    ("git cat-file --textc HEAD:f", False),
    # Abbreviations a newer git may take.
    ("git diff --ext HEAD", False),
    ("git diff --no-ind a b", False),
    ("git log --outp=x", False),
    # A bare -- ends the options; what follows is still read (the safe side).
    ("git log -- a.py", True),
    ("git diff HEAD -- a.py", True),
    ("git log -- --output=x", False),
    ("git log --no-ext-diff -p", True),
    ("git log --no-textconv -p", True),
    ("git log -5", True),
    ("git diff --exit-code", True),
])
def test_a_judge_reads_git_only_in_its_reading_forms(command, reads):
    from auth.git_reads import read_only_git
    assert read_only_git(command) is reads


def test_an_input_is_read_as_it_was_authorized(judged, tmp_path, monkeypatch):
    """The answer authorized is the answer opened: an input swapped for a
    link after its check is not read, and its target never reaches the
    judge's prompt."""
    from auth import path_policy
    from auth.path_policy import SecurityContext
    from services.checks.kinds import judge_kind
    agent_dir = tmp_path / "agents" / AGENT
    notes = agent_dir / "users/alice/workspace/notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("ALICE-NOTES")
    secret = agent_dir / "config/secret.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("CONFIG-SECRET")
    real_check = path_policy.check_host_path_access

    def check_then_swap(path, security, **kw):
        answer = real_check(path, security, **kw)
        if path.name == "notes.md" and not notes.is_symlink():
            notes.unlink()
            notes.symlink_to(secret)
        return answer
    monkeypatch.setattr(path_policy, "check_host_path_access", check_then_swap)
    editor = SecurityContext(role="editor", username="alice", agent=AGENT, is_admin_agent=False)
    got = dict(judge_kind._read_inputs(AGENT, editor, ["users/alice/workspace/notes.md"]))
    assert "CONFIG-SECRET" not in got["users/alice/workspace/notes.md"]
