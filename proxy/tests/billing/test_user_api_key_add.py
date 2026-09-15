"""A user connects their own API key (POST
/v1/users/me/execution-layers/{layer}/subscriptions): api_key only, on the
two CLI engines with the engine's own vendor, a key required, one per
engine, and the agent-pool flag honoured for an admin and forced off for
everyone else.

Run: cd proxy && python -m pytest tests/billing/test_user_api_key_add.py -v
"""

import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from storage.billing import subscription_store

URL = "/v1/users/me/execution-layers/{layer}/subscriptions"


@pytest.fixture
def client(temp_db):
    from fastapi.testclient import TestClient
    from app import app
    from auth.providers import UserContext, get_current_user

    state = {"user": UserContext(sub="user-viewer", email="v@t.com", name="V", role="member")}

    async def _current():
        return state["user"]

    app.dependency_overrides[get_current_user] = _current
    c = TestClient(app)
    c.as_user = lambda sub, role="member": state.__setitem__(  # type: ignore[attr-defined]
        "user", UserContext(sub=sub, email=f"{sub}@t.com", name=sub, role=role))
    try:
        yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _post(client, layer, **body):
    payload = {"provider": "anthropic" if layer == "claude-code-cli" else "openai",
               "auth_type": "api_key", "api_key": "sk-test-1"}
    payload.update(body)
    return client.post(URL.format(layer=layer), json=payload)


class TestUserAddApiKey:
    def test_adds_one_key_per_engine_for_the_caller(self, client):
        r = _post(client, "claude-code-cli", label="my key")
        assert r.status_code == 200, r.text
        row = r.json()
        assert (row["layer"], row["provider"], row["auth_type"], row["owner_sub"]) == (
            "claude-code-cli", "anthropic", "api_key", "user-viewer")
        assert row["use_personal"] is True and row["contribute_platform"] is False
        assert subscription_store.get_credential_data(row["id"]) == {"api_key": "sk-test-1"}
        # The other engine takes its own key; a second on the same engine is a 409.
        assert _post(client, "codex-cli").status_code == 200
        r = _post(client, "claude-code-cli", api_key="sk-test-2")
        assert r.status_code == 409
        assert len(subscription_store.list_personal(None, "user-viewer")) == 2
        # Another user's key on the same engine is a different row.
        client.as_user("user-viewer2")
        assert _post(client, "claude-code-cli").status_code == 200

    def test_rejects_other_shapes(self, client):
        assert _post(client, "direct-llm").status_code == 400
        assert _post(client, "claude-code-cli", auth_type="local_endpoint",
                     endpoint_url="http://x:11434").status_code == 400
        assert _post(client, "claude-code-cli", auth_type="oauth").status_code == 400
        assert _post(client, "claude-code-cli", provider="openai").status_code == 400
        assert _post(client, "codex-cli", provider="ollama").status_code == 400
        assert _post(client, "claude-code-cli", api_key="").status_code == 400
        assert _post(client, "claude-code-cli", api_key=None).status_code == 400
        assert _post(client, "claude-code-cli", api_key="   ").status_code == 400
        assert subscription_store.list_personal(None, "user-viewer") == []

    def test_agent_pool_flag(self, client):
        # A member can never contribute to the agent pool.
        r = _post(client, "claude-code-cli", contribute_platform=True)
        assert r.json()["contribute_platform"] is False
        # An admin defaults on and may opt out explicitly (the user card's form does).
        client.as_user("user-admin", "admin")
        assert _post(client, "claude-code-cli").json()["contribute_platform"] is True
        assert _post(client, "codex-cli", contribute_platform=False).json()["contribute_platform"] is False

    def test_store_raises_a_typed_error_on_a_duplicate(self, temp_db):
        subscription_store.add_subscription("codex-cli", "openai", "api_key", owner_sub="u")
        with pytest.raises(subscription_store.SubscriptionExists):
            subscription_store.add_subscription("codex-cli", "openai", "api_key", owner_sub="u")
        # The connection is usable after the rollback.
        assert len(subscription_store.list_personal("codex-cli", "u")) == 1
