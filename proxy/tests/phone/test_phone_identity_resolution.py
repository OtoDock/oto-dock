"""Warmup-time route identity resolution (services/phone/phone_identity.py)."""

from __future__ import annotations

import uuid

import pytest

from services.phone import phone_identity as pi
from storage.phone import phone_route_store
from storage.pg import get_conn

SID = "11111111-2222-4333-8444-555555555555"


def _agent(slug="", *, execution_path="claude-code-cli", collaborative=True,
           default_scope="user") -> str:
    slug = slug or f"pi-agent-{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agents (slug, display_name, execution_path, collaborative, "
            "default_scope, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW()::text, NOW()::text)",
            (slug, slug, execution_path, collaborative, default_scope),
        )
        conn.commit()
    return slug


def _server() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO phone_servers (name, adapter_type, host, created_at, updated_at) "
            "VALUES (%s, 'asterisk_manual', '', NOW()::text, NOW()::text) RETURNING id",
            (f"pbx-{uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.commit()
    return row["id"]


def _user(*, role="member", username=None, agents=(), agent_role="viewer") -> str:
    sub = f"user-{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, username, created_at, last_login) "
            "VALUES (%s, %s, %s, %s, %s, NOW()::text, NOW()::text)",
            (sub, f"{sub}@t", sub, role, username if username is not None else f"u{uuid.uuid4().hex[:6]}"),
        )
        for a in agents:
            conn.execute(
                "INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) "
                "VALUES (%s, %s, NOW()::text, 'test', %s)", (sub, a, agent_role),
            )
        conn.commit()
    return sub


def _route(agent, **extra) -> dict:
    return phone_route_store.create_route({
        "direction": extra.pop("direction", "inbound"), "name": "r", "agent": agent,
        "phone_server_id": _server(), "audiosocket_uuid": str(uuid.uuid4()), **extra,
    })


def _resolve(route, agent, phone="+302101234567", **kw):
    return pi.resolve_route_identity(route, agent=agent, caller_phone=phone, session_id=SID, **kw)


class TestFallbacks:
    def test_no_route_is_shared_default_role(self, temp_db):
        agent = _agent()
        ident = _resolve(None, agent)
        assert ident.mode == "shared" and ident.role == pi.EXTERNAL_ROUTE_ROLE == "viewer"
        assert ident.external is not None and not ident.external.has_tree
        assert ident.label == "shared" and ident.fallback_reason == "no route"

    def test_route_of_another_agent_or_disabled(self, temp_db):
        a, b = _agent(), _agent()
        route = _route(b)
        ident = _resolve(route, a)
        assert ident.mode == "shared" and ident.fallback_reason == "route/agent mismatch"
        disabled = _route(a, enabled=False)
        assert _resolve(disabled, a).fallback_reason == "route/agent mismatch"


class TestCallerMode:
    def test_default_route_gives_a_private_tree(self, temp_db):
        agent = _agent()
        route = _route(agent)
        ident = _resolve(route, agent)
        assert ident.mode == "caller" and ident.role == "viewer"
        assert ident.external.slug == "302101234567" and not ident.external.ephemeral
        assert ident.label == "caller:+302101234567"

    def test_pin_verified_counts_only_with_a_stored_pin(self, temp_db):
        agent = _agent()
        route = _route(agent)
        assert _resolve(route, agent, pin_verified=True).external.verified is False
        phone_route_store.set_route_pin(route["id"], "4711")
        ident = _resolve(route, agent, pin_verified=True)
        assert ident.external.verified is True and ident.label == "caller-pin:+302101234567"

    def test_remember_off_and_withheld_are_ephemeral(self, temp_db):
        agent = _agent()
        route = _route(agent, remember_callers=False)
        ident = _resolve(route, agent)
        assert ident.external.ephemeral and ident.external.id == "+302101234567"
        ident = _resolve(_route(agent), agent, phone="anonymous")
        assert ident.external.ephemeral and ident.label == "ephemeral"

    def test_shared_only_agent_keeps_the_id_without_a_tree(self, temp_db):
        agent = _agent(collaborative=False, default_scope="agent")
        ident = _resolve(_route(agent), agent)
        assert ident.mode == "caller" and ident.external.id == "+302101234567"
        assert not ident.external.has_tree and not ident.external.ephemeral

    def test_role_is_always_viewer(self, temp_db):
        """The per-route role selector was removed (2026-09-08): a legacy
        editor/manager column value never reaches the session."""
        agent = _agent()
        assert _resolve(_route(agent, role="manager"), agent).role == "viewer"
        assert _resolve(_route(agent, role="editor"), agent).role == "viewer"


class TestLegacySharedValue:
    def test_stored_shared_resolves_as_per_caller(self, temp_db):
        """The 'shared' option was removed (2026-09-07); a stored value keeps
        the route working as per-caller — on a Shared-only agent that IS the
        shared space (id kept, no tree)."""
        shared_only = _agent(collaborative=False, default_scope="agent")
        ident = _resolve(_route(shared_only, identity_mode="shared", role="editor"), shared_only)
        assert ident.mode == "caller" and ident.role == "viewer"
        assert ident.external.id == "+302101234567" and not ident.external.has_tree
        collaborative = _agent()
        ident = _resolve(_route(collaborative, identity_mode="shared"), collaborative)
        assert ident.mode == "caller" and ident.external.has_tree


class TestUserMode:
    def test_tied_user_session(self, temp_db):
        agent = _agent()
        sub = _user(agents=(agent,), agent_role="editor")
        route = _route(agent, identity_mode="user", identity_user_sub=sub)
        ident = _resolve(route, agent, pin_verified=False)
        assert ident.mode == "user" and ident.external is None
        assert ident.user["sub"] == sub and ident.user_role == "editor"
        assert ident.caller_claim == "phone:+302101234567"
        assert ident.label == f"user:{ident.user['username']}"

    def test_admin_is_capped_at_manager(self, temp_db):
        agent = _agent()
        sub = _user(role="admin")
        ident = _resolve(_route(agent, identity_mode="user", identity_user_sub=sub), agent)
        assert ident.mode == "user" and ident.user_role == "manager"

    def test_lost_access_falls_back_to_caller(self, temp_db):
        agent = _agent()
        sub = _user(agents=(agent,))
        route = _route(agent, identity_mode="user", identity_user_sub=sub)
        with get_conn() as conn:
            conn.execute("DELETE FROM user_agents WHERE sub = %s", (sub,))
            conn.commit()
        ident = _resolve(route, agent)
        assert ident.mode == "caller" and ident.external is not None
        assert "no access" in ident.fallback_reason

    def test_locked_or_nameless_user_falls_back(self, temp_db):
        agent = _agent()
        sub = _user(agents=(agent,))
        with get_conn() as conn:
            conn.execute("UPDATE users SET locked_until = '2999-01-01' WHERE sub = %s", (sub,))
            conn.commit()
        ident = _resolve(_route(agent, identity_mode="user", identity_user_sub=sub), agent)
        assert ident.mode == "caller" and "locked" in ident.fallback_reason
        nameless = _user(username="", agents=(agent,))
        ident = _resolve(_route(agent, identity_mode="user", identity_user_sub=nameless), agent)
        assert ident.mode == "caller" and "username" in ident.fallback_reason


class TestExternalCallsRunOnTheServer:
    """Codex takes external calls in the local sandbox (2026-09-08: no shell
    tool + the hook floor under the app-server). An external call never runs
    on a paired machine, on ANY engine: the external principal is a viewer and
    the target resolver forces every non-owner role to local (no bwrap on a
    satellite). The remote-Codex refusal that guarded this until 2026-09-09
    could therefore never fire and is gone — this test pins the invariant it
    relied on, so a resolver change surfaces here."""

    def test_external_routes_on_codex_resolve_and_run_locally(self, temp_db):
        agent = _agent(execution_path="codex-cli")
        ident = _resolve(_route(agent), agent)
        assert ident.mode == "caller" and ident.external.has_tree
        assert _resolve(None, agent).mode == "shared"

    @pytest.mark.parametrize("execution_path", ["codex-cli", "claude-code-cli"])
    def test_external_calls_stay_local_when_the_agent_is_pinned_remote(
        self, temp_db, execution_path,
    ):
        from core.config.phone_config_builder import resolve_phone_execution_target
        agent = _agent(execution_path=execution_path)
        with get_conn() as conn:
            conn.execute("UPDATE agents SET execution_target = %s WHERE slug = %s",
                         ("machine-remote-1", agent))
            conn.commit()
        # The viewer rule: local before reachability is even consulted.
        assert resolve_phone_execution_target(agent) == "local"
        assert resolve_phone_execution_target(agent, role=pi.EXTERNAL_ROUTE_ROLE) == "local"
        # And the identity resolves normally — nothing refuses the call.
        ident = _resolve(_route(agent), agent)
        assert ident.mode == "caller" and ident.external.has_tree
        assert _resolve(None, agent).mode == "shared"

    def test_user_routes_on_codex_are_fine(self, temp_db):
        agent = _agent(execution_path="codex-cli")
        sub = _user(agents=(agent,))
        ident = _resolve(_route(agent, identity_mode="user", identity_user_sub=sub), agent)
        assert ident.mode == "user"
