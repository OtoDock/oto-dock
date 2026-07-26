"""Platform-level engine-enablement gate + availability flags.

PATCH/POST /v1/agents: a non-admin manager may only NEWLY enable an execution
engine when the platform POOL has a subscription for it
(``subscription_pool.layer_platform_configured`` — agent-scoped background
runs can only use the pool, so personal connections never qualify).
Grandfathering: already-enabled engines always keep
(reorders/removals pass untouched). The admin bypass is COOKIE-principal only
— a session-token identity carrying an admin sub (the config-MCP inside an
admin-created session) must NOT bypass (prompt-injection surface).

Also: the per-layer ``configured`` flag on GET /v1/execution-layers (now
authenticated) and the per-user ``can_run`` flag on
GET /v1/users/me/execution-layers.

Run: cd proxy && venv/bin/pytest tests/agents/test_engine_gate.py -v
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

# Platform roles per conftest._seed_users
ADMIN = "user-admin"      # admin
CREATOR = "user-manager"  # creator
MEMBER = "user-viewer"    # member

SLUG = "engine-gate-demo"


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


@pytest.fixture
def agent(temp_db):
    from storage import agent_store
    # Primary claude-code-cli (the create default), nothing configured.
    agent_store.create_agent(SLUG, "Engine Gate Demo", created_by=ADMIN)
    return SLUG


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


def _bearer_session(user_sub: str) -> dict[str, str]:
    """A session-scoped JWT (the config-MCP's credential) resolving back to
    a REAL user row — is_api_key=True, so the cookie-only admin bypass must
    not apply."""
    from auth.session_token import create_session_token
    return {"Authorization":
            f"Bearer {create_session_token('sess-1', SLUG, user_sub)}"}


def _assign_manager(sub: str) -> None:
    from storage import database as task_store
    task_store.set_user_agents(sub, [SLUG], ADMIN, agent_roles={SLUG: "manager"})


def _add_sub(layer: str, *, owner: str = "", personal: bool = True,
             pool: bool = False, auth_type: str = "api_key") -> None:
    from storage import subscription_store
    subscription_store.add_subscription(
        layer, "openai" if layer == "codex-cli" else "anthropic", auth_type,
        owner_sub=owner, use_personal=personal, contribute_platform=pool,
    )


def _enabled_paths(slug: str) -> list[str]:
    from api.agents._common import _get_execution_paths
    from storage import agent_store
    return _get_execution_paths(agent_store.get_agent(slug) or {})


class TestEnableGate:
    def test_manager_add_unconfigured_refused(self, client, agent):
        _assign_manager(MEMBER)
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["claude-code-cli", "codex-cli"]},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403
        assert "No Codex subscription" in r.json()["detail"]
        assert _enabled_paths(SLUG) == ["claude-code-cli"]

    def test_admin_pool_sub_counts(self, client, agent):
        # The gate keys on the PLATFORM POOL: agent-scoped background work
        # (tasks/phone/triggers) can only run there.
        _assign_manager(MEMBER)
        _add_sub("codex-cli", owner=ADMIN, personal=False, pool=True)
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["claude-code-cli", "codex-cli"]},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 200
        assert set(_enabled_paths(SLUG)) == {"claude-code-cli", "codex-cli"}

    def test_personal_sub_alone_does_not_count(self, client, agent):
        # A user's personal connection backs only THEIR user-scope chats —
        # never the pool that agent-scoped runs need — so it must not make
        # an engine enableable (operator decision 2026-07-25).
        _assign_manager(MEMBER)
        _add_sub("codex-cli", owner=CREATOR, personal=True)
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["claude-code-cli", "codex-cli"]},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403

    def test_non_admin_pool_flag_does_not_count(self, client, agent):
        # contribute_platform on a NON-admin owner's row never feeds the pool
        # (list_platform_pool's owner-is-admin JOIN) → not "configured".
        _assign_manager(MEMBER)
        _add_sub("codex-cli", owner=CREATOR, personal=False, pool=True)
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["claude-code-cli", "codex-cli"]},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403

    def test_grandfathered_reorder_passes(self, client, agent):
        # A stored set the platform can no longer "configure" must stay
        # editable: reorders are a set-identity save (the MCP's
        # update_default_layer and the UI's toggle both send them).
        from storage import agent_store
        agent_store.update_agent(SLUG, execution_path="claude-code-cli",
                                 execution_paths=json.dumps(["codex-cli"]))
        _assign_manager(MEMBER)
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["codex-cli", "claude-code-cli"]},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 200
        assert _enabled_paths(SLUG)[0] == "codex-cli"

    def test_uncheck_always_allowed(self, client, agent):
        from storage import agent_store
        agent_store.update_agent(SLUG, execution_path="claude-code-cli",
                                 execution_paths=json.dumps(["codex-cli"]))
        _assign_manager(MEMBER)
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["claude-code-cli"]},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 200
        assert _enabled_paths(SLUG) == ["claude-code-cli"]

    def test_cookie_admin_bypasses(self, client, agent):
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["claude-code-cli", "codex-cli"]},
            headers=_cookie(ADMIN, "admin@test.com", "admin"),
        )
        assert r.status_code == 200
        assert "codex-cli" in _enabled_paths(SLUG)

    def test_session_token_admin_does_not_bypass(self, client, agent):
        # The config-MCP authenticates with the session JWT of the session's
        # human owner — an admin-created trigger/task session resolves to
        # admin role but is_api_key=True, so the gate still applies.
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"execution_paths": ["claude-code-cli", "codex-cli"]},
            headers=_bearer_session(ADMIN),
        )
        assert r.status_code == 403
        assert "No Codex subscription" in r.json()["detail"]

    def test_create_explicit_path_gated_for_non_admin(self, client, temp_db):
        r = client.post(
            "/v1/agents",
            json={"display_name": "Gated Create", "slug": "gated-create",
                  "execution_path": "codex-cli"},
            headers=_cookie(CREATOR, "manager@test.com", "creator"),
        )
        assert r.status_code == 403
        _add_sub("codex-cli", owner=ADMIN, personal=False, pool=True)
        r = client.post(
            "/v1/agents",
            json={"display_name": "Gated Create", "slug": "gated-create",
                  "execution_path": "codex-cli"},
            headers=_cookie(CREATOR, "manager@test.com", "creator"),
        )
        assert r.status_code == 200

    def test_create_auto_default_ungated(self, client, temp_db):
        # No explicit engine → the auto-default (claude fallback) is NOT
        # gated, so a bare platform can always create its first agent.
        r = client.post(
            "/v1/agents",
            json={"display_name": "Bare Create", "slug": "bare-create"},
            headers=_cookie(CREATOR, "manager@test.com", "creator"),
        )
        assert r.status_code == 200
        assert r.json()["execution_path"] == "claude-code-cli"


class TestAvailabilityFlags:
    def test_execution_layers_requires_auth(self, client, temp_db):
        assert client.get("/v1/execution-layers").status_code == 401

    def test_execution_layers_configured_flag(self, client, temp_db):
        headers = _cookie(MEMBER, "viewer@test.com", "member")
        data = client.get("/v1/execution-layers", headers=headers).json()
        assert data["claude-code-cli"]["configured"] is False
        # A personal connection is NOT platform configuration…
        _add_sub("claude-code-cli", owner=MEMBER, personal=True)
        data = client.get("/v1/execution-layers", headers=headers).json()
        assert data["claude-code-cli"]["configured"] is False
        # …an admin pool contribution is.
        _add_sub("claude-code-cli", owner=ADMIN, personal=False, pool=True)
        data = client.get("/v1/execution-layers", headers=headers).json()
        assert data["claude-code-cli"]["configured"] is True
        assert data["codex-cli"]["configured"] is False

    def test_user_layers_can_run_flag(self, client, temp_db):
        headers = _cookie(MEMBER, "viewer@test.com", "member")

        def flags():
            data = client.get("/v1/users/me/execution-layers",
                              headers=headers).json()
            return {l["name"]: l["can_run"] for l in data["layers"]}

        # Nothing anywhere → nothing runnable.
        assert flags()["claude-code-cli"] is False
        assert flags()["codex-cli"] is False
        # Own personal sub → runnable.
        _add_sub("claude-code-cli", owner=MEMBER, personal=True)
        assert flags()["claude-code-cli"] is True
        # A borrowable ADMIN pool sub alone is NOT enough — the user's
        # Platform-Auth toggle (schema default FALSE) gates the borrow.
        _add_sub("codex-cli", owner=ADMIN, personal=False, pool=True)
        assert flags()["codex-cli"] is False
        from storage import subscription_store
        subscription_store.set_user_allow_platform_auth(MEMBER, True)
        assert flags()["codex-cli"] is True
