"""The one credential-injection step through the phone builder: a Codex
(TOML) phone config with a credential environment and a bash-only key
reaches ``inject_toml_credentials`` with that key as ``exclude_keys``, the
MCP's env table gets the rest, and the build completes. The builder once
called a function that did not exist here, so such a call failed to start.
"""

from __future__ import annotations

import json

import pytest

from core.config import phone_config_builder as pcb
from core.session import session_manager
from services.mcp import mcp_registry
from tests.phone.test_phone_config_builder_identity import (  # noqa: F401 (fixtures)
    SID,
    _identity,
    agent,
    stubs,
)


@pytest.mark.asyncio
async def test_a_codex_phone_config_gets_its_credentials_without_the_bash_only_keys(
    agent, stubs, monkeypatch, tmp_path,  # noqa: F811
):
    toml = tmp_path / "mcp.toml"
    toml.write_text("")
    toml.with_suffix(".servers.json").write_text(json.dumps({
        "github-mcp": {"type": "stdio", "command": "gh-mcp", "args": [], "env": {}},
    }))
    env = {"GH_TOKEN": "ghp_phone", "GIT_CONFIG_COUNT": "2"}

    def _build(agent_name, user_sub, **kw):
        stubs["mcp_kwargs"] = kw
        return toml, dict(env), {}, {}, {"GIT_CONFIG_COUNT"}
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _build)
    codex = next(p for p in session_manager.valid_execution_paths()
                 if session_manager.capabilities_for_path(p).mcp_config_format == "toml")
    real_caps = session_manager.capabilities_for_path
    monkeypatch.setattr(session_manager, "capabilities_for_path", lambda path: real_caps(codex))
    seen: dict = {}
    real_inject = mcp_registry.inject_toml_credentials

    def _inject(mcp_config, credential_env, *, mcp_format, exclude_keys=None):
        seen.update(mcp_config=mcp_config, credential_env=dict(credential_env or {}),
                    mcp_format=mcp_format, exclude_keys=set(exclude_keys or ()))
        return real_inject(mcp_config, credential_env, mcp_format=mcp_format, exclude_keys=exclude_keys)
    monkeypatch.setattr(mcp_registry, "inject_toml_credentials", _inject)

    ident = _identity(agent)
    cfg = await pcb.build_phone_agent_config(agent, route_identity=ident, session_id=SID)

    assert stubs["mcp_kwargs"]["mcp_config_format"] == "toml"
    # The session's token rides the same environment: it names nobody and
    # carries the caller's claim.
    from auth.session_token import validate_session_token
    claims = validate_session_token(seen["credential_env"].pop("PROXY_API_KEY"))
    assert claims["sid"] == SID and claims["user_sub"] == ""
    assert claims["ext"] == ident.external.claim
    assert seen == {"mcp_config": toml, "credential_env": env, "mcp_format": "toml",
                    "exclude_keys": {"GIT_CONFIG_COUNT"}}
    text = toml.read_text()
    assert "ghp_phone" in text and "GIT_CONFIG_COUNT" not in text
    assert not toml.with_suffix(".servers.json").exists()
    assert cfg.mcp_config_path == str(toml)
