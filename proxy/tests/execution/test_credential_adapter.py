"""The credential adapter each engine implements (engine-contract lane,
phase 3c-i): how an acquired subscription reaches a session of that engine.

The pool asks the layer — ``subscription_env`` maps a ``SubscriptionHandle``
to the engine's own env names; ``credential_file_payload`` is the full
content of the engine's credential file for an OAuth token; and
``credential_file_from_env`` is the consumer half the layer's start_session
and the remote start payload use. Vendor knowledge lives in the engine
package; the pool only decides which account and when to rotate.
"""

import json

import pytest

from core.execution_layer import ExecutionLayer, SubscriptionHandle
from core.session.session_manager import get_layer_by_path


def _handle(layer, **kw):
    base = dict(subscription_id="sub-1", layer=layer, provider="", auth_type="api_key",
                api_key=None, oauth_access_token=None, endpoint_url=None)
    base.update(kw)
    return SubscriptionHandle(**base)


class TestClaude:
    layer = get_layer_by_path("claude-code-cli")

    def test_api_key_rides_the_vendor_variable(self):
        env = self.layer.subscription_env(_handle("claude-code-cli", provider="anthropic",
                                                  api_key="sk-ant"))
        assert env == {"ANTHROPIC_API_KEY": "sk-ant"}

    def test_login_rides_as_the_credentials_file_never_as_a_token(self):
        stored = {"oauth_token": {"accessToken": "old", "refreshToken": "SECRET",
                                  "scopes": ["user:inference"], "subscriptionType": "max",
                                  "rateLimitTier": "tier5", "refreshTokenExpiresAt": 42}}
        env = self.layer.subscription_env(_handle(
            "claude-code-cli", provider="anthropic", auth_type="oauth",
            oauth_access_token="tok", oauth_expires_at_ms=777, credential=stored))
        assert set(env) == {"_CLAUDE_CREDS_BLOB"}
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        payload = json.loads(env["_CLAUDE_CREDS_BLOB"])
        assert payload == {"claudeAiOauth": {
            "accessToken": "tok", "refreshToken": "", "expiresAt": 777,
            "scopes": ["user:inference"], "subscriptionType": "max",
            "rateLimitTier": "tier5", "refreshTokenExpiresAt": 42,
        }}
        # The consumer half pops exactly that payload and leaves the rest.
        env["OTHER"] = "x"
        assert self.layer.credential_file_from_env(env) == payload
        assert env == {"OTHER": "x"}
        assert self.layer.credential_file_from_env({"OTHER": "x"}) is None

    def test_file_payload_is_the_token_and_expiry_the_caller_passes(self):
        # The pool snapshots (token, expiry) per session; the file must carry
        # the same pair, never the store's latest.
        payload = self.layer.credential_file_payload("issued", 123, {"oauth_token": {}})
        blob = payload["claudeAiOauth"]
        assert blob["accessToken"] == "issued" and blob["expiresAt"] == 123
        assert blob["refreshToken"] == "" and "refreshTokenExpiresAt" not in blob


class TestCodex:
    layer = get_layer_by_path("codex-cli")
    blob = {"auth_mode": "chatgpt", "tokens": {"id_token": "ID", "access_token": "OLD",
                                              "refresh_token": "RFR", "account_id": "ACC"}}

    def test_api_key_and_local_key_ride_different_variables(self):
        assert self.layer.subscription_env(_handle("codex-cli", provider="openai",
                                                   api_key="sk-o")) == {"CODEX_API_KEY": "sk-o"}
        env = self.layer.subscription_env(_handle(
            "codex-cli", provider="openai_compatible", auth_type="local_endpoint",
            api_key="k", endpoint_url="http://h:8080/v1"))
        assert env["_CODEX_LOCAL_API_KEY"] == "k" and "CODEX_API_KEY" not in env
        assert env["_CODEX_ENDPOINT_URL"] == "http://h:8080/v1"
        assert env["_CODEX_ENDPOINT_PROVIDER"] == "openai_compatible"

    def test_login_rides_as_the_full_auth_json(self):
        env = self.layer.subscription_env(_handle(
            "codex-cli", provider="openai", auth_type="oauth", oauth_access_token="NEW",
            credential={"codex_auth_blob": self.blob}))
        assert set(env) == {"_CODEX_AUTH_JSON"}
        auth = json.loads(env["_CODEX_AUTH_JSON"])
        assert auth["tokens"] == {"id_token": "ID", "access_token": "NEW",
                                  "refresh_token": "", "account_id": "ACC"}
        assert self.layer.credential_file_from_env(env) == auth and env == {}

    def test_a_row_without_the_login_blob_has_no_file(self):
        # auth.json needs the login's id_token / account_id: no blob, no file
        # — the spawn writes nothing and a rotation skips the row, the same
        # answer on both paths.
        assert self.layer.credential_file_payload("tok", 0, {}) is None
        env = self.layer.subscription_env(_handle(
            "codex-cli", provider="openai", auth_type="oauth", oauth_access_token="tok"))
        assert env == {}


class TestDirect:
    layer = get_layer_by_path("direct-llm")

    def test_provider_generic_env_and_no_file(self):
        env = self.layer.subscription_env(_handle(
            "direct-llm", provider="groq", api_key="gsk", endpoint_url="http://relay/v1"))
        assert env == {"_PROVIDER": "groq", "_API_KEY": "gsk", "_ENDPOINT_URL": "http://relay/v1"}
        assert self.layer.credential_file_payload("t", 0, {}) is None
        assert self.layer.credential_file_from_env({"_API_KEY": "x"}) is None


def test_the_default_adapter_refuses_rather_than_returning_nothing():
    # The ABC's own default, called unbound: an engine that does not map a
    # subscription must fail loudly, not spawn with an empty env.
    with pytest.raises(NotImplementedError):
        ExecutionLayer.subscription_env(object(), _handle("x"))
