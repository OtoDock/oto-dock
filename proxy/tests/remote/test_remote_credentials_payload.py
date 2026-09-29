"""Remote OAuth file delivery — the start payload + fan-out registration.

The satellite writes each session's CLI credential file from the start
payload (``credentials_json`` for Claude, ``auth_json`` for Codex); no OAuth
token may ride the payload env (env is frozen at exec and outranks the
credential file, which would defeat rotation fan-out). These tests pin the
payload contract and the proxy-side subscription binding that makes remote
sessions visible to the turn guard + fan-out.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.execution_layer import AgentConfig
from core.remote.remote_execution import RemoteExecutionLayer


def _machine():
    return {
        "capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
        "pairing_scope": "admin",
    }


def _config(**overrides):
    base = dict(
        agent_name="test-agent",
        execution_target="machine-1",
        model="claude-sonnet-5",
        effort="high",
        permission_mode="auto",
        client_type="dashboard",
        extra_env={},
    )
    base.update(overrides)
    return AgentConfig(**base)


@pytest.fixture()
def layer():
    return RemoteExecutionLayer(MagicMock())


async def _build(layer, config, execution_path):
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        return await layer._build_start_payload("sess-1", config, execution_path)


async def _payload(layer, config, execution_path):
    return (await _build(layer, config, execution_path)).payload


class TestClaudePayload:
    @pytest.mark.asyncio
    async def test_credentials_file_becomes_credentials_json_and_leaves_env(self, layer):
        # The env carries the FULL file content the engine's adapter built
        # (its credential_file_payload); the payload ships it verbatim under
        # the engine's declared start-payload key.
        blob = {"accessToken": "at", "refreshToken": "", "expiresAt": 5,
                "scopes": [], "subscriptionType": "", "rateLimitTier": ""}
        config = _config(extra_env={"_CLAUDE_CREDS_BLOB": json.dumps({"claudeAiOauth": blob})})
        plan = await _build(layer, config, "claude-code-cli")
        payload = plan.payload
        assert plan.credential_file_delivered is True
        assert payload["credentials_json"] == {"claudeAiOauth": blob}
        # No token in the spawned env — the file is the only carrier.
        assert "_CLAUDE_CREDS_BLOB" not in payload["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in payload["env"]
        # The 401-recovery poll window bridges the WS propagation gap.
        assert payload["env"]["CLAUDE_CODE_OAUTH_401_WAIT_MS"] == "20000"

    @pytest.mark.asyncio
    async def test_api_key_session_has_no_credentials_json(self, layer):
        config = _config(extra_env={"ANTHROPIC_API_KEY": "sk-test"})
        plan = await _build(layer, config, "claude-code-cli")
        payload = plan.payload
        assert plan.credential_file_delivered is False
        assert "credentials_json" not in payload
        assert "CLAUDE_CODE_OAUTH_401_WAIT_MS" not in payload["env"]
        assert payload["env"]["ANTHROPIC_API_KEY"] == "sk-test"


class TestCodexPayload:
    @pytest.mark.asyncio
    async def test_auth_json_carries_neutralized_refresh(self, layer):
        # The Codex adapter built the full auth.json from the login blob and
        # the issued token (refresh neutralized); the payload ships it.
        from core.session.session_manager import get_layer_by_path
        blob = {"auth_mode": "chatgpt",
                "tokens": {"id_token": "ID", "access_token": "OLD",
                           "refresh_token": "RFR", "account_id": "A"}}
        auth_json = get_layer_by_path("codex-cli").credential_file_payload(
            "NEW", 0, {"codex_auth_blob": blob})
        config = _config(extra_env={"_CODEX_AUTH_JSON": json.dumps(auth_json)})
        plan = await _build(layer, config, "codex-cli")
        payload = plan.payload
        assert plan.credential_file_delivered is True
        assert plan.start_timeout_s == 60.0
        assert payload["auth_json"]["tokens"]["access_token"] == "NEW"
        assert payload["auth_json"]["tokens"]["refresh_token"] == ""
        assert payload["auth_json"]["tokens"]["id_token"] == "ID"
        assert "_CODEX_AUTH_JSON" not in payload["env"]


class TestBindSubscription:
    def setup_method(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        pool._session_subscriptions.clear()
        pool._session_token_expiry.clear()
        pool._issued_token_expiry.clear()
        token_fanout._targets.clear()

    def test_binds_and_registers_claude_target(self):
        from types import SimpleNamespace
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        # The target's dir is the scope root the payload builder used (the
        # SecurityContext's mount username) + the engine's declared dirname.
        config = _config(subscription_id="sub-1",
                         security_context=SimpleNamespace(mount_username="alice"))
        RemoteExecutionLayer._bind_subscription(
            "sess-1", config, "claude-code-cli", True,
        )
        assert pool.get_session_subscription("sess-1") == "sub-1"
        target = token_fanout.session_target("sess-1")
        assert target.layer == "claude-code-cli"
        assert target.machine_id == "machine-1"
        assert target.agent_name == "test-agent"
        assert target.dir_relative == "users/alice/.claude"

    def test_binds_and_registers_codex_target(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        config = _config(subscription_id="sub-2")     # agent-scope mount → workspace
        RemoteExecutionLayer._bind_subscription(
            "sess-2", config, "codex-cli", True,
        )
        assert pool.get_session_subscription("sess-2") == "sub-2"
        target = token_fanout.session_target("sess-2")
        assert target.layer == "codex-cli" and target.dir_relative == "workspace/.codex"

    def test_api_key_session_binds_without_target(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        config = _config(subscription_id="sub-3")
        RemoteExecutionLayer._bind_subscription(       # no credential file delivered
            "sess-3", config, "claude-code-cli", False,
        )
        assert pool.get_session_subscription("sess-3") == "sub-3"
        assert token_fanout.session_target("sess-3") is None

    def test_no_subscription_is_a_noop(self):
        from services.engines import subscription_pool as pool
        config = _config(subscription_id="")
        RemoteExecutionLayer._bind_subscription(
            "sess-4", config, "claude-code-cli", True,
        )
        assert pool.get_session_subscription("sess-4") is None
