"""Every session builder mints the session's token with one helper
(``session_token.mint_session_env``), naming the session's person: the chat's
and the task's, and now a meeting participant's (the creator of a
user-scope meeting; nobody for an agent-scope one and for a Shared-only
participant) and an external call's (nobody, with the caller's claim).
Before, the meeting and external phone builders minted none, so on Codex
the layer's own token, which names nobody, reached their stdio MCPs.

The builders run real against the test database (``loop_builder_rig``),
each Codex build with a real TOML written through
``inject_toml_credentials``."""
import json

import pytest
import tomllib

from tests.fixtures.loop_builder_rig import AGENT, SHARED, install, run


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    rec = install(monkeypatch, tmp_path)
    from services.mcp import mcp_registry
    from storage.agents import agent_store
    from storage.billing import subscription_store
    subscription_store.add_model("codex-cli", "gpt-char-1", "Char", provider="openai", tier=2)
    for agent in (AGENT, SHARED):
        agent_store.update_agent(agent, execution_path="codex-cli", execution_paths='["codex-cli"]')

    def _mcp_build(*args, **kwargs):
        # A Codex build gets a real TOML with one stdio MCP, through the
        # registry's own sidecar and writer.
        if kwargs.get("mcp_config_format") != "toml":
            return (None, {"MCP_CRED": "1"}, {}, {}, set())
        toml = tmp_path / f"{kwargs.get('session_id')}.toml"
        toml.write_text("")
        toml.with_suffix(".servers.json").write_text(json.dumps({
            "memory": {"type": "stdio", "command": "python", "args": ["memory.py"], "env": {}},
        }))
        return (toml, {"MCP_CRED": "1"}, {}, {}, set())

    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _mcp_build)
    return rec


def _claims(token: str) -> dict:
    from auth.session_token import validate_session_token
    claims = validate_session_token(token)
    assert claims is not None
    return claims


def _stdio_token(cfg) -> str:
    """The token the Codex layer's TOML gives the stdio MCP: the builder's,
    or the layer's own (naming nobody) where the builder wrote none."""
    from auth.session_token import create_session_token
    from core.layers.codex.layer import _inject_proxy_vars_toml
    text = _inject_proxy_vars_toml(open(cfg.mcp_config_path).read(), {
        "PROXY_URL": "http://localhost:8400",
        "PROXY_API_KEY": create_session_token("layer-sid", cfg.agent_name),
    })
    return tomllib.loads(text)["mcp_servers"]["memory"]["env"]["PROXY_API_KEY"]


def _meeting(agent: str, scope: str, creator: str, sid: str):
    from services.meetings.meeting_context import build_meeting_agent_config
    meeting = {"id": f"meet-{sid}", "scope": scope, "created_by": creator}
    return run(build_meeting_agent_config(agent, meeting, sid))


def test_mint_session_env_writes_the_token_and_returns_its_floor():
    from auth.session_token import mint_session_env
    env: dict[str, str] = {}
    floor = mint_session_env(env, "sid-mint", "char-agent", "user-viewer", external="")
    claims = _claims(env["PROXY_API_KEY"])
    assert claims["user_sub"] == "user-viewer" and claims["sid"] == "sid-mint"
    assert claims["iat"] == floor > 0 and "ext" not in claims


@pytest.mark.parametrize("agent,scope,creator,person", [
    (AGENT, "user", "user-viewer", "user-viewer"),   # a user-scope meeting: its creator
    (AGENT, "agent", "user-manager", ""),            # an agent-scope meeting: nobody
    (SHARED, "user", "user-admin", ""),              # a Shared-only participant: nobody
])
def test_a_meeting_participant_names_the_meetings_person(rig, agent, scope, creator, person):
    sid = f"tok-meet-{scope}-{agent}"
    cfg = _meeting(agent, scope, creator, sid)
    token = cfg.credential_env["PROXY_API_KEY"]
    claims = _claims(token)
    assert claims["user_sub"] == person and claims["sid"] == sid and claims["agent"] == agent
    assert "ext" not in claims
    # Codex's stdio MCP gets the builder's token, never the layer's own.
    assert _stdio_token(cfg) == token
    assert claims["iat"] == cfg.token_minted_at > 0


def test_an_external_call_names_nobody_and_carries_the_callers_claim(rig):
    from core.config.phone_config_builder import build_phone_agent_config
    from core.session import external_identity
    sid = "tok-phone-ext"
    cfg = run(build_phone_agent_config(AGENT, call_type="inbound", session_id=sid))
    token = cfg.credential_env["PROXY_API_KEY"]
    claims = _claims(token)
    claim = external_identity.resolve(external_identity.PHONE, "", session_id="", shared=True).claim
    assert claims["user_sub"] == "" and claims["sid"] == sid and claims["ext"] == claim
    assert claim == cfg.security_context.external_claim
    assert _stdio_token(cfg) == token
    assert claims["iat"] == cfg.token_minted_at > 0


@pytest.mark.parametrize("build", ["meeting", "phone"])
def test_the_builders_token_is_the_sessions_floor(rig, build):
    """The config carries the token's iat, so the session's floor admits
    the token the builder minted (a floor set later would refuse it)."""
    from core.config.phone_config_builder import build_phone_agent_config
    from core.session import session_state
    sid = f"tok-floor-{build}"
    cfg = (_meeting(AGENT, "user", "user-viewer", sid) if build == "meeting"
           else run(build_phone_agent_config(AGENT, call_type="inbound", session_id=sid)))
    session_state.register_session_state(sid, cfg.permission_mode, cfg.security_context,
                                         token_minted_at=cfg.token_minted_at)
    session_state.mark_starting(sid, 60.0)
    try:
        assert session_state.session_token_refusal(_claims(cfg.credential_env["PROXY_API_KEY"])) == ""
    finally:
        session_state.cleanup_session_permission_state(sid)
