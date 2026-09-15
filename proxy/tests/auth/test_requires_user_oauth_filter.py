"""``services[i].requires_user_oauth`` UI filter tests.

The filter is two pieces:

* Manifest validator accepts ``requires_user_oauth: true`` on a service.
* ``GET /v1/oauth/{provider}/accounts`` returns a
  ``has_service_credentials_only`` field. Platform service accounts were
  removed, so this is now ALWAYS False (every user connects their own
  account) — the field is retained for response-shape compatibility.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from storage.identity import credential_store
from storage.pg import get_conn


# ---------------------------------------------------------------------------
# Validator sanity (covered in 3a, repeated here as a defence-in-depth gate)
# ---------------------------------------------------------------------------


class TestManifestValidator:
    def test_validator_accepts_requires_user_oauth_true(self):
        from services.mcp import mcp_registry

        raw = {
            "provider_id": "test-prov",
            "flows": ["authorization_code"],
            "authorization_url": "https://test/auth",
            "token_url": "https://test/token",
            "scopes_per_service": True,
            "services": [
                {
                    "key": "premium",
                    "label": "Premium",
                    "description": "User-acting only",
                    "scopes": [],
                    "requires_user_oauth": True,
                },
                {
                    "key": "standard",
                    "label": "Standard",
                    "description": "Either path works",
                    "scopes": [],
                },
            ],
        }
        # Validator must accept without raising.
        mcp_registry._validate_oauth_services(raw, "test-mcp", server_raw={})

    def test_validator_rejects_non_bool_requires_user_oauth(self):
        from services.mcp import mcp_registry

        raw = {
            "provider_id": "test-prov",
            "flows": ["authorization_code"],
            "authorization_url": "https://test/auth",
            "token_url": "https://test/token",
            "services": [
                {
                    "key": "broken",
                    "label": "X",
                    "description": "Y",
                    "scopes": [],
                    "requires_user_oauth": "yes",  # not a bool
                },
            ],
        }
        with pytest.raises(ValueError, match="requires_user_oauth"):
            mcp_registry._validate_oauth_services(raw, "test-mcp", server_raw={})


# ---------------------------------------------------------------------------
# Accounts endpoint: has_service_credentials_only
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_name(request):
    name = f"test-mcp-{uuid.uuid4().hex[:10]}"

    def cleanup():
        with get_conn() as conn:
            conn.execute("DELETE FROM service_agent_bindings WHERE mcp_name=%s", (name,))
            conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (name,))
            conn.execute("DELETE FROM user_credentials WHERE mcp_name=%s", (name,))
            conn.commit()

    request.addfinalizer(cleanup)
    return name


@pytest.fixture
def user_sub(request):
    sub = f"test-user-{uuid.uuid4().hex[:12]}"
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, "
            "auth_provider, created_at, last_login) "
            "VALUES (%s, %s, 'Test', %s, 'creator', 'local', "
            "NOW()::text, NOW()::text)",
            (sub, f"{username}@test.example", username),
        )
        conn.commit()

    def cleanup():
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
            conn.commit()

    request.addfinalizer(cleanup)
    return sub


def _build_client(user_sub: str, role: str = "creator") -> TestClient:
    """Mount the oauth router with a stubbed user."""
    from api.auth import oauth as oauth_api
    from auth.providers import UserContext, get_current_user

    user = UserContext(
        sub=user_sub,
        email="alice@test.example",
        name="Alice",
        role=role,
        agents=[],
        agent_roles={},
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(oauth_api.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return TestClient(app)


class TestAccountsEndpointServiceOnlyFlag:
    def test_no_accounts_at_all_flag_is_false(self, mcp_name, user_sub):
        """Brand-new MCP with no accounts of any kind — the flag is False
        because there's no S2S to gate against."""
        # mcp_registry needs the MCP to be a known manifest; rather than
        # writing a fixture manifest to disk, register one in-memory.
        from services.mcp import mcp_registry
        from dataclasses import dataclass, field

        @dataclass
        class _Cred:
            oauth: dict = field(default_factory=lambda: {"provider_id": "test-prov"})

        @dataclass
        class _Srv:
            transport: str = "streamable_http"
            url_template: str = "https://test.example/mcp"

        @dataclass
        class _Manifest:
            name: str = mcp_name
            credentials: _Cred = field(default_factory=_Cred)
            server: _Srv = field(default_factory=_Srv)

        mcp_registry._manifests[mcp_name] = _Manifest(name=mcp_name)
        try:
            client = _build_client(user_sub)
            r = client.get(
                f"/v1/oauth/test-prov/accounts?mcp_name={mcp_name}",
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["accounts"] == []
            assert body["has_service_credentials_only"] is False
        finally:
            mcp_registry._manifests.pop(mcp_name, None)

    def test_user_oauth_present_flag_is_false(self, mcp_name, user_sub):
        """With a user-OAuth account connected, the flag is False and the
        account is listed."""
        from services.mcp import mcp_registry
        from dataclasses import dataclass, field

        @dataclass
        class _Cred:
            oauth: dict = field(default_factory=lambda: {"provider_id": "test-prov"})

        @dataclass
        class _Srv:
            transport: str = "streamable_http"
            url_template: str = "https://test.example/mcp"

        @dataclass
        class _Manifest:
            name: str = mcp_name
            credentials: _Cred = field(default_factory=_Cred)
            server: _Srv = field(default_factory=_Srv)

        mcp_registry._manifests[mcp_name] = _Manifest(name=mcp_name)
        try:
            credential_store.set_user_credentials(
                user_sub, mcp_name, {"OAUTH_TOKEN": "abc"},
                account_label="my-user-account",
            )
            client = _build_client(user_sub)
            r = client.get(
                f"/v1/oauth/test-prov/accounts?mcp_name={mcp_name}",
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["accounts"]) == 1
            assert body["has_service_credentials_only"] is False
        finally:
            mcp_registry._manifests.pop(mcp_name, None)
