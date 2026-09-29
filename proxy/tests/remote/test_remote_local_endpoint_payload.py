"""Remote Codex sessions on a local OpenAI-compatible endpoint — the start
payload contract.

A codex-cli ``local_endpoint`` subscription reaches the layer as
``_CODEX_ENDPOINT_URL`` / ``_CODEX_LOCAL_API_KEY`` (+ ``_CODEX_ENDPOINT_PROVIDER``
and the provider's model rows). The satellite writes its own config.toml, so
the proxy carries the provider as the ``local_model_provider`` payload field
(satellite >= 0.5.116: ``base_url``, ``env_key``; >= 0.5.117 also
``stream_idle_timeout_ms`` and the per-session model ``catalog_json``) and the
key as the child-env variable the provider's ``env_key`` names — never the
private variables, and never to a satellite below 0.5.116 (which would run the
local model name against Codex's built-in OpenAI provider).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.execution_layer import AgentConfig
from core.layers.codex.helpers import (
    LOCAL_ENDPOINT_KEY_ENV, LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS, LOCAL_PROVIDER_TOOL_NOTE,
)
from core.layers.codex.local_model_catalog import LOCAL_MODEL_ROWS_ENV
from core.remote.remote_execution import RemoteExecutionLayer

_URL = "http://192.168.1.8:8080/v1"


def _machine():
    return {
        "capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
        "pairing_scope": "admin",
    }


def _config(**overrides):
    base = dict(
        agent_name="test-agent",
        execution_target="machine-1",
        system_prompt="You are a test agent.",
        model="qwen3.6-35b-a3b",
        effort="medium",
        permission_mode="default",
        client_type="dashboard",
        extra_env={},
    )
    base.update(overrides)
    return AgentConfig(**base)


@pytest.fixture()
def layer():
    # The connection manager is a MagicMock, so every gate is truthy unless
    # pinned — each test sets the local-provider gate EXPLICITLY.
    lay = RemoteExecutionLayer(MagicMock())
    lay._cm.satellite_supports_local_model_provider.return_value = True
    lay._cm.satellite_supports_local_model_catalog.return_value = True
    lay._cm.satellite_version.return_value = "0.5.117"
    lay._cm.satellite_name.return_value = "drill-sat"
    return lay


async def _build(layer, config, execution_path="codex-cli"):
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        return (await layer._build_start_payload("sess-1", config, execution_path)).payload


class TestLocalEndpointPayload:
    @pytest.mark.asyncio
    async def test_keyed_endpoint_travels_as_provider_field_and_env_key(self, layer):
        config = _config(extra_env={
            "_CODEX_ENDPOINT_URL": _URL, "_CODEX_LOCAL_API_KEY": "llama-key",
        })
        payload = await _build(layer, config)
        assert payload["local_model_provider"] == {
            "base_url": _URL, "env_key": LOCAL_ENDPOINT_KEY_ENV,
            "stream_idle_timeout_ms": LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS,
            # No provider named → openai_compatible semantics → no catalog.
            "catalog_json": "",
        }
        assert payload["env"][LOCAL_ENDPOINT_KEY_ENV] == "llama-key"
        # The two private variables never ride the satellite env.
        assert "_CODEX_ENDPOINT_URL" not in payload["env"]
        assert "_CODEX_LOCAL_API_KEY" not in payload["env"]
        # A local endpoint is not API-key auth against the built-in provider.
        assert "CODEX_API_KEY" not in payload["env"]
        assert "auth_json" not in payload
        # The deferred-tools note rides AGENTS.md — one prompt, both fields.
        assert payload["agents_md_content"].endswith(LOCAL_PROVIDER_TOOL_NOTE + "\n")
        assert payload["agents_md_content"] == payload["system_prompt"]
        assert payload["agents_md_content"].startswith(config.system_prompt)

    @pytest.mark.asyncio
    async def test_keyless_endpoint_has_no_env_key(self, layer):
        config = _config(extra_env={"_CODEX_ENDPOINT_URL": _URL})
        payload = await _build(layer, config)
        prov = payload["local_model_provider"]
        assert (prov["base_url"], prov["env_key"]) == (_URL, "")
        assert LOCAL_ENDPOINT_KEY_ENV not in payload["env"]

    @pytest.mark.asyncio
    async def test_note_follows_the_provider_and_the_variable_is_popped(self, layer):
        from core.layers.codex.helpers import OLLAMA_PROVIDER_TOOL_NOTE
        config = _config(extra_env={
            "_CODEX_ENDPOINT_URL": "http://127.0.0.1:11434/v1",
            "_CODEX_ENDPOINT_PROVIDER": "ollama",
        })
        payload = await _build(layer, config)
        assert payload["agents_md_content"].endswith(OLLAMA_PROVIDER_TOOL_NOTE + "\n")
        assert "_CODEX_ENDPOINT_PROVIDER" not in payload["env"]

    @pytest.mark.asyncio
    async def test_ollama_session_ships_the_model_catalog(self, layer):
        # Ollama 0.34+ runs Codex's client-side tool_search, so the payload
        # carries the per-session catalog the satellite writes as models.json:
        # the session's model first, the provider's other rows after it (a
        # same-provider switch keeps its entry), windows from the rows. The
        # rows ride the subscription env and never reach the satellite.
        config = _config(extra_env={
            "_CODEX_ENDPOINT_URL": "http://127.0.0.1:11434/v1",
            "_CODEX_ENDPOINT_PROVIDER": "ollama",
            LOCAL_MODEL_ROWS_ENV: json.dumps([["qwen3.5-9b", 32768], ["qwen3.6-35b-a3b", 131072]]),
        })
        payload = await _build(layer, config)
        models = json.loads(payload["local_model_provider"]["catalog_json"])["models"]
        assert [m["slug"] for m in models] == ["qwen3.6-35b-a3b", "qwen3.5-9b"]
        assert models[0]["supports_search_tool"] is True
        assert models[0]["context_window"] == 131_072
        assert payload["local_model_provider"]["stream_idle_timeout_ms"] == LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS
        assert LOCAL_MODEL_ROWS_ENV not in payload["env"]

    @pytest.mark.asyncio
    async def test_openai_compatible_session_ships_no_catalog(self, layer):
        config = _config(extra_env={
            "_CODEX_ENDPOINT_URL": _URL, "_CODEX_ENDPOINT_PROVIDER": "openai_compatible",
            LOCAL_MODEL_ROWS_ENV: json.dumps([["qwen3.6-35b-a3b", 131072]]),
        })
        payload = await _build(layer, config)
        assert payload["local_model_provider"]["catalog_json"] == ""
        assert payload["local_model_provider"]["stream_idle_timeout_ms"] == LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS
        assert LOCAL_MODEL_ROWS_ENV not in payload["env"]

    @pytest.mark.asyncio
    async def test_satellite_below_0_5_117_gets_the_provider_without_the_catalog(self, layer, caplog):
        # Additive fields: a 0.5.116 satellite ignores them, so the proxy sends
        # the 0.5.116 shape and logs the gap instead of refusing the session.
        layer._cm.satellite_supports_local_model_catalog.return_value = False
        layer._cm.satellite_version.return_value = "0.5.116"
        config = _config(extra_env={
            "_CODEX_ENDPOINT_URL": "http://127.0.0.1:11434/v1",
            "_CODEX_ENDPOINT_PROVIDER": "ollama",
        })
        payload = await _build(layer, config)
        assert payload["local_model_provider"] == {
            "base_url": "http://127.0.0.1:11434/v1", "env_key": "",
        }
        assert any("0.5.117" in r.message and "drill-sat" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_url_is_forwarded_verbatim_no_loopback_rewrite(self, layer):
        # The SATELLITE host dials the URL as configured; a loopback URL means
        # the satellite's own machine (never rewritten for the proxy host).
        config = _config(extra_env={"_CODEX_ENDPOINT_URL": "http://127.0.0.1:8080/v1"})
        payload = await _build(layer, config)
        assert payload["local_model_provider"]["base_url"] == "http://127.0.0.1:8080/v1"

    @pytest.mark.asyncio
    async def test_hosted_session_carries_no_provider(self, layer):
        config = _config(model="gpt-5.6-terra", extra_env={"CODEX_API_KEY": "sk-test"})
        payload = await _build(layer, config)
        assert "local_model_provider" not in payload
        assert LOCAL_ENDPOINT_KEY_ENV not in payload["env"]
        assert payload["env"]["CODEX_API_KEY"] == "sk-test"
        # Hosted models know Codex's deferred-tool convention: no note.
        assert LOCAL_PROVIDER_TOOL_NOTE not in payload["agents_md_content"]
        assert payload["agents_md_content"] == config.system_prompt

    @pytest.mark.asyncio
    async def test_old_satellite_is_refused_with_its_version(self, layer):
        layer._cm.satellite_supports_local_model_provider.return_value = False
        layer._cm.satellite_version.return_value = "0.5.115"
        config = _config(extra_env={
            "_CODEX_ENDPOINT_URL": _URL, "_CODEX_LOCAL_API_KEY": "llama-key",
        })
        with pytest.raises(
            RuntimeError, match=r"0\.5\.116 or newer — drill-sat runs 0\.5\.115",
        ):
            await _build(layer, config)

    @pytest.mark.asyncio
    async def test_old_satellite_unknown_version_is_refused(self, layer):
        layer._cm.satellite_supports_local_model_provider.return_value = False
        layer._cm.satellite_version.return_value = ""
        config = _config(extra_env={"_CODEX_ENDPOINT_URL": _URL})
        with pytest.raises(RuntimeError, match="runs an unknown version"):
            await _build(layer, config)

    @pytest.mark.asyncio
    async def test_claude_payload_pops_the_variables_and_carries_no_provider(self, layer):
        layer._cm.satellite_supports_local_model_provider.return_value = False
        config = _config(model="claude-sonnet-5", extra_env={
            "_CODEX_ENDPOINT_URL": _URL, "_CODEX_LOCAL_API_KEY": "llama-key",
            "_CODEX_ENDPOINT_PROVIDER": "openai_compatible",
            "ANTHROPIC_API_KEY": "sk-ant",
        })
        payload = await _build(layer, config, "claude-code-cli")
        assert "local_model_provider" not in payload
        assert "_CODEX_ENDPOINT_URL" not in payload["env"]
        assert "_CODEX_LOCAL_API_KEY" not in payload["env"]
        assert "_CODEX_ENDPOINT_PROVIDER" not in payload["env"]
        assert LOCAL_ENDPOINT_KEY_ENV not in payload["env"]
        assert payload["env"]["ANTHROPIC_API_KEY"] == "sk-ant"


def test_satellite_supports_local_model_provider_gate():
    from core.remote.satellite_connection import (
        SatelliteConnection, SatelliteConnectionManager,
    )
    cm = SatelliteConnectionManager()
    for mid, ver in (
        ("old", "0.5.115"), ("new", "0.5.116"), ("catalog", "0.5.117"),
        ("future", "0.6.0"), ("blank", ""),
    ):
        cm._connections[mid] = SatelliteConnection(
            machine_id=mid, ws=None, satellite_version=ver,
        )
    assert cm.satellite_supports_local_model_provider("new") is True
    assert cm.satellite_supports_local_model_provider("future") is True
    assert cm.satellite_supports_local_model_provider("old") is False
    assert cm.satellite_supports_local_model_provider("blank") is False
    assert cm.satellite_supports_local_model_provider("offline") is False
    # The soft gate for the 0.5.117 fields (idle timeout + model catalog):
    # 0.5.116 still gets the provider, without them.
    assert cm.satellite_supports_local_model_catalog("new") is False
    assert cm.satellite_supports_local_model_catalog("catalog") is True
    assert cm.satellite_supports_local_model_catalog("future") is True
    assert cm.satellite_supports_local_model_catalog("offline") is False
    assert cm.satellite_version("old") == "0.5.115"
    assert cm.satellite_version("offline") == ""
    # No display name registered → the short machine id.
    assert cm.satellite_name("offline-machine-id") == "offline-"


class TestProviderSwitchBlockerOnRemote:
    """The dashboard's cross-provider switch refusal keys on the session's BOUND
    subscription — remote sessions bind theirs (``_bind_subscription``), so the
    same check covers a remote chat on a local endpoint."""

    def _blocker(self, monkeypatch, bound_provider, wanted_provider):
        import ws.dashboard  # noqa: F401  (owns the import cycle with dashboard_chat)
        from ws import dashboard_chat
        monkeypatch.setattr(
            "services.engines.subscription_pool.get_session_subscription",
            lambda sid: "sub-local",
        )
        monkeypatch.setattr(
            "storage.billing.subscription_store.get_subscription",
            lambda sid: {"id": sid, "provider": bound_provider},
        )
        monkeypatch.setattr(
            dashboard_chat.config, "get_model_provider",
            lambda model, layer="": wanted_provider,
        )
        return dashboard_chat._provider_switch_blocker("sess-remote", "gpt-5.6-terra", layer="codex-cli")

    def test_cross_provider_switch_is_refused(self, monkeypatch):
        msg = self._blocker(monkeypatch, "openai_compatible", "openai")
        assert "Start a new chat" in msg

    def test_same_provider_switch_is_allowed(self, monkeypatch):
        assert self._blocker(monkeypatch, "openai", "openai") == ""
