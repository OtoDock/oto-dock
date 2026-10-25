"""A task session's token names the run identity's person, on every engine
and every consumer: the task builder mints it into ``credential_env`` as the
chat builder does (the creator of a user-scope task; nobody for an
agent-scope task or any task on a Shared-only agent). Before, a Codex task's
stdio MCPs got the layer's token naming nobody, so a user-scope Codex task
acted as the agent's no-user principal on every route its tools called.

The builder runs real against the test database (``loop_builder_rig``),
with a real Codex TOML written through ``inject_toml_credentials``. Each
test uses its own session id: the bearer swap's remembered person is
process-global."""
import json
import time
from types import SimpleNamespace

import pytest
import tomllib

from tests.fixtures.loop_builder_rig import AGENT, SHARED, install, run

CREATOR = "user-viewer"   # an editor on AGENT, a viewer on SHARED (the rig)


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    rec = install(monkeypatch, tmp_path)
    from services.mcp import mcp_registry
    from storage.billing import subscription_store
    subscription_store.add_model("codex-cli", "gpt-char-1", "Char", provider="openai", tier=2)

    def _mcp_build(*args, **kwargs):
        # A Codex build gets a real TOML: two stdio MCPs and one HTTP MCP,
        # through the registry's own sidecar and writer.
        if kwargs.get("mcp_config_format") != "toml":
            return (None, {"MCP_CRED": "1"}, {}, {}, set())
        toml = tmp_path / f"{kwargs.get('session_id')}.toml"
        toml.write_text("")
        toml.with_suffix(".servers.json").write_text(json.dumps({
            "display": {"type": "stdio", "command": "python", "args": ["display.py"], "env": {}},
            "memory": {"type": "stdio", "command": "python", "args": ["memory.py"], "env": {}},
            "file-tools": {"type": "http", "url": "http://localhost:8901/mcp/"},
        }))
        return (toml, {"MCP_CRED": "1"}, {}, {}, set())

    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _mcp_build)
    return rec


def _build(scope: str, created_by: str | None, sid: str, *, agent: str = AGENT,
           engine: str = "codex-cli"):
    from core.config.task_config_builder import build_task_agent_config
    task = SimpleNamespace(scope=scope, created_by=created_by, task_type="scheduled",
                           notification_mode="manual", name="t", id=f"dyn-{sid}",
                           override_execution_path=engine)
    return run(build_task_agent_config(agent, task, sid))


def _claims(token: str) -> dict:
    from auth.session_token import validate_session_token
    claims = validate_session_token(token)
    assert claims is not None
    return claims


def _layer_toml(cfg) -> dict:
    """The TOML the Codex layer writes for the session: the builder's file
    after the layer's own injection (``PROXY_URL`` and its person-less
    ``PROXY_API_KEY`` where absent)."""
    from auth.session_token import create_session_token
    from core.layers.codex.layer import _inject_proxy_vars_toml
    text = _inject_proxy_vars_toml(open(cfg.mcp_config_path).read(), {
        "PROXY_URL": "http://localhost:8400",
        "PROXY_API_KEY": create_session_token("layer-sid", cfg.agent_name),
    })
    return tomllib.loads(text)["mcp_servers"]


def test_a_user_scope_codex_task_gives_its_mcps_the_creators_token(rig):
    sid = "tok-user-codex"
    cfg = _build("user", CREATOR, sid)
    servers = _layer_toml(cfg)
    stdio = {name: s for name, s in servers.items() if "command" in s}
    assert set(stdio) == {"display", "memory"}
    # Every stdio MCP names the creator (the layer's own token names nobody).
    for s in stdio.values():
        assert _claims(s["env"]["PROXY_API_KEY"])["user_sub"] == CREATOR
        assert s["env"]["PROXY_URL"] == "http://localhost:8400"
    assert "env" not in servers["file-tools"]
    # It is the builder's token: this session, this agent, its floor.
    token = cfg.credential_env["PROXY_API_KEY"]
    assert {s["env"]["PROXY_API_KEY"] for s in stdio.values()} == {token}
    claims = _claims(token)
    assert claims["sid"] == sid and claims["agent"] == AGENT
    assert "ext" not in claims
    assert claims["iat"] == cfg.token_minted_at > 0


@pytest.mark.parametrize("scope,created_by,agent", [
    ("agent", CREATOR, AGENT),           # an agent-scope task a person made
    ("agent", None, AGENT),              # a task with no creator
    ("user", "user-admin", SHARED),      # a person's own task on a Shared-only agent
])
def test_a_run_with_no_person_gets_a_token_naming_nobody(rig, scope, created_by, agent):
    """No person before the fix either: this guards that the builder's token
    keeps it so (an agent task a person made, one with no creator, a
    person's own task on a Shared-only agent)."""
    sid = f"tok-none-{scope}-{agent}-{created_by or 'x'}"
    cfg = _build(scope, created_by, sid, agent=agent)
    for s in _layer_toml(cfg).values():
        if "command" in s:
            assert _claims(s["env"]["PROXY_API_KEY"])["user_sub"] == ""
    claims = _claims(cfg.credential_env["PROXY_API_KEY"])
    assert claims["user_sub"] == "" and claims["sid"] == sid


def test_the_codex_daemon_its_hooks_and_its_shell_name_the_creator(rig):
    """The headless daemon env (the layer lays ``credential_env`` over it)
    and the shell's command-line token name the creator: before, the daemon
    started with no working dir and minted a token naming nobody."""
    from auth.session_token import validate_cli_token
    from core.layers.codex.session import CodexAppServerSession
    sid = "tok-daemon"
    cfg = _build("user", CREATOR, sid)
    extra = dict(cfg.extra_env)
    extra.update(cfg.credential_env)
    sess = CodexAppServerSession(sid, AGENT, cfg.model, "workspace-write", "", "/tmp/codex",
                                 extra_env=extra, user_role=cfg.security_context.role)
    env = sess._build_env()
    assert _claims(env["PROXY_API_KEY"])["user_sub"] == CREATOR
    cli = validate_cli_token(env["OTO_CLI_TOKEN"])
    assert cli is not None and cli["user_sub"] == CREATOR and cli["sid"] == sid
    assert env["PROXY_API_KEY"] == cfg.credential_env["PROXY_API_KEY"]


@pytest.mark.parametrize("mount_user", ["vic", ""])
def test_the_claude_task_process_env_carries_the_builders_token(rig, mount_user):
    """A Claude task's process (and so its MCPs) carries the builder's token,
    which names the creator. With the mount's username (``vic``) the
    process's own mint already named the creator before the fix; with no
    username it names nobody, so the person here is the builder's."""
    from core.sandbox.env_builder import build_session_env
    sid = f"tok-claude-{mount_user or 'none'}"
    cfg = _build("user", CREATOR, sid, engine="claude-code-cli")
    env = build_session_env(sid, AGENT, cfg.credential_env, username=mount_user,
                            user_role=cfg.security_context.role)
    claims = _claims(env["PROXY_API_KEY"])
    assert claims["user_sub"] == CREATOR and claims["sid"] == sid
    assert env["PROXY_API_KEY"] == cfg.credential_env["PROXY_API_KEY"]
    assert claims["iat"] == cfg.token_minted_at


@pytest.mark.parametrize("scope,person", [("user", CREATOR), ("agent", "")])
def test_the_docker_mcp_bearer_follows_the_builders_person(rig, scope, person):
    from auth.session_token import SESSION_JWT_SENTINEL_BEARER, swap_session_jwt_bearer
    sid = f"tok-bearer-{scope}"
    _build(scope, CREATOR, sid)
    bearer = swap_session_jwt_bearer(SESSION_JWT_SENTINEL_BEARER, sid, AGENT)
    assert bearer is not None
    assert _claims(bearer.removeprefix("Bearer "))["user_sub"] == person


def test_the_builders_token_is_the_sessions_floor(rig):
    from core.session import session_state
    sid = "tok-floor"
    cfg = _build("user", CREATOR, sid)
    session_state.register_session_state(sid, cfg.permission_mode, cfg.security_context,
                                         token_minted_at=cfg.token_minted_at)
    session_state.mark_starting(sid, 60.0)
    try:
        assert session_state.session_token_refusal(_claims(cfg.credential_env["PROXY_API_KEY"])) == ""
    finally:
        session_state.cleanup_session_permission_state(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli"])
async def test_a_task_on_a_machine_carries_the_creators_token(temp_db, monkeypatch, tmp_path, engine):
    """The remote start payload lays ``credential_env`` over its own mint
    (here for "alice", the security context's username, so the builder's
    person is told apart), and its Codex TOML rewrite injects a token only
    where none is. That layering predates the builder's token: this pins
    it with a hand-made config; the built chain is the test below."""
    from unittest.mock import patch

    from auth.session_token import create_session_token
    from tests.remote.test_remote_engine_contract import (
        _snapshot_config,
        _snapshot_layer,
    )
    sid = f"tok-remote-{engine}"
    minted = int(time.time())
    token = create_session_token(sid, "test-agent", CREATOR, external="", issued_at=minted)
    toml = tmp_path / "config.toml"
    toml.write_text('[mcp_servers.display]\ncommand = "python"\nargs = ["display.py"]\n'
                    f'env = {{ PROXY_API_KEY = "{token}" }}\n')
    cfg = _snapshot_config(tmp_path, client_type="task", token_minted_at=minted,
                           credential_env={"PROXY_API_KEY": token},
                           model="gpt-5.6-sol" if engine == "codex-cli" else "claude-sonnet-5",
                           **({"mcp_config_path": str(toml)} if engine == "codex-cli" else {}))
    machine = {"capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
               "pairing_scope": "admin"}
    with patch("storage.remote_store.get_remote_machine", return_value=machine), \
         patch("storage.database.get_user_sub_by_username", return_value="sub-alice"), \
         patch("core.remote.remote_start_payload._load_hook_scripts", return_value={}):
        plan = await _snapshot_layer()._build_start_payload(sid, cfg, engine)
    env = plan.payload["env"]
    assert env["PROXY_API_KEY"] == token
    assert _claims(env["PROXY_API_KEY"])["user_sub"] == CREATOR
    if engine == "codex-cli":
        text = json.dumps(plan.payload)
        assert token in text
        assert text.count('PROXY_API_KEY') >= 2   # the env and the TOML's stdio MCP


@pytest.mark.parametrize("scope,person", [("user", CREATOR), ("agent", "")])
def test_a_built_task_started_on_a_machine_names_the_builders_person(rig, scope, person):
    """The whole chain: a real task build (Codex, with its TOML), handed to
    the remote start as a run on a machine. The payload's own mint names
    the security context's person (here a stand-in, ``sub-elsewhere``); the
    builder's token is laid over it, in the env and in every stdio MCP."""
    import dataclasses
    import re
    from unittest.mock import patch

    from auth.session_token import validate_session_token
    from services.mcp import mcp_registry
    from tests.remote.test_remote_engine_contract import _snapshot_layer
    sid = f"tok-machine-{scope}"
    cfg = dataclasses.replace(_build(scope, CREATOR, sid), execution_target="machine-1")
    machine = {"capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
               "pairing_scope": "admin"}
    with patch.dict(mcp_registry._manifests, clear=True), \
         patch("storage.remote_store.get_remote_machine", return_value=machine), \
         patch("storage.database.get_user_sub_by_username", return_value="sub-elsewhere"), \
         patch("core.remote.remote_start_payload._load_hook_scripts", return_value={}):
        plan = run(_snapshot_layer()._build_start_payload(sid, cfg, "codex-cli"))
    claims = _claims(plan.payload["env"]["PROXY_API_KEY"])
    assert claims["user_sub"] == person and claims["sid"] == sid
    # Every session token the machine receives (the env's, each stdio MCP's
    # in the TOML) names the same person: none the payload's own mint.
    found = [c for c in (validate_session_token(t) for t in
                         re.findall(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", json.dumps(plan.payload)))
             if c is not None]
    assert len(found) >= 3
    assert {c["user_sub"] for c in found} == {person}
