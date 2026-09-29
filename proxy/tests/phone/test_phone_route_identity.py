"""Phone-route identity settings (external routes, item 1).

Covers the four route columns (``identity_mode`` / ``identity_user_sub`` /
``role`` / ``remember_callers``) end to end at the storage + API layer: the
schema defaults for fresh AND upgraded installs, the store round-trip, the
save-time validators, the server-computed ``warnings``, the daemon push
strip, and the manifest-only MCP preview.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from storage.phone import phone_route_store
from storage import schema as pg_schema
from storage.pg import get_conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_agent(slug: str = "", *, execution_path: str = "claude-code-cli",
                collaborative: bool = True, default_scope: str = "user") -> str:
    slug = slug or f"pr-agent-{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agents (slug, display_name, execution_path, collaborative, "
            "default_scope, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW()::text, NOW()::text)",
            (slug, slug, execution_path, collaborative, default_scope),
        )
        conn.commit()
    return slug


def _make_phone_server() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO phone_servers (name, adapter_type, host, created_at, updated_at) "
            "VALUES (%s, 'asterisk_manual', '', NOW()::text, NOW()::text) RETURNING id",
            (f"pbx-{uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.commit()
    return row["id"]


def _make_user(sub: str = "", *, role: str = "member", username: str | None = None,
               agents: tuple[str, ...] = ()) -> str:
    """Insert a users row (+ optional agent assignments). ``username=""`` =
    a user who never logged in (usernames are unique, so default to a fresh
    slug per user)."""
    sub = sub or f"user-{uuid.uuid4().hex[:8]}"
    if username is None:
        username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, username, created_at, last_login) "
            "VALUES (%s, %s, %s, %s, %s, NOW()::text, NOW()::text)",
            (sub, f"{sub}@test.com", sub, role, username),
        )
        for agent in agents:
            conn.execute(
                "INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) "
                "VALUES (%s, %s, NOW()::text, 'test', 'viewer')",
                (sub, agent),
            )
        conn.commit()
    return sub


def _make_route(agent: str, direction: str = "inbound", **extra) -> dict:
    return phone_route_store.create_route({
        "direction": direction,
        "name": f"route-{uuid.uuid4().hex[:6]}",
        "agent": agent,
        "phone_server_id": extra.pop("phone_server_id", None) or _make_phone_server(),
        "audiosocket_uuid": str(uuid.uuid4()) if direction == "inbound" else None,
        **extra,
    })


@pytest.fixture
def client(temp_db):
    from api.phone import phone as phone_router

    app = FastAPI()
    app.include_router(phone_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


# ---------------------------------------------------------------------------
# Schema: fresh and upgraded installs get the same defaults
# ---------------------------------------------------------------------------

class TestSchema:
    def test_defaults_on_fresh_install(self, temp_db):
        agent = _make_agent()
        route = _make_route(agent)
        assert route["identity_mode"] == "caller"
        assert route["identity_user_sub"] is None
        assert route["role"] == "viewer"
        assert route["remember_callers"] is True
        with get_conn() as conn:
            log_cols = {
                r["column_name"] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'phone_call_log'"
                ).fetchall()
            }
        assert {"session_id", "identity", "tools_run"} <= log_cols

    def test_migration_adds_the_columns_idempotently(self, temp_db):
        from storage import pg
        # Simulate a pre-upgrade install: the tables exist without the columns.
        with get_conn() as conn:
            conn.execute(
                "ALTER TABLE phone_routes DROP COLUMN identity_mode, "
                "DROP COLUMN identity_user_sub, DROP COLUMN role, "
                "DROP COLUMN remember_callers"
            )
            conn.execute(
                "ALTER TABLE phone_call_log DROP COLUMN session_id, "
                "DROP COLUMN identity, DROP COLUMN tools_run"
            )
            conn.commit()
        # A pooled connection may hold a prepared ``SELECT *`` plan from an
        # earlier test; a changed result type would fail it ("cached plan
        # must not change result type") — at runtime the migration runs at
        # startup before any query. Fresh pool = no cached plans.
        pg.close_pool()
        for _ in range(2):  # idempotent: safe on every startup
            with get_conn() as conn:
                pg_schema.run_migrations(conn)
                conn.commit()
        pg.close_pool()
        agent = _make_agent()
        route = _make_route(agent)
        assert (route["identity_mode"], route["role"], route["remember_callers"]) == (
            "caller", "viewer", True,
        )
        # The FK re-created by the migration: deleting the user nulls the tie.
        sub = _make_user(agents=(agent,))
        phone_route_store.update_route(route["id"], {"identity_mode": "user", "identity_user_sub": sub})
        with get_conn() as conn:
            conn.execute("DELETE FROM user_agents WHERE sub = %s", (sub,))
            conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
            conn.commit()
        assert phone_route_store.get_route(route["id"])["identity_user_sub"] is None

    def test_no_drift_between_create_and_migration(self, temp_db):
        with get_conn() as conn:
            assert pg_schema.check_schema_drift(conn) == []


# ---------------------------------------------------------------------------
# Store round-trip
# ---------------------------------------------------------------------------

class TestStore:
    def test_round_trip_and_clear(self, temp_db):
        agent = _make_agent()
        sub = _make_user(agents=(agent,))
        route = _make_route(
            agent, identity_mode="user", identity_user_sub=sub, role="editor",
            remember_callers=False,
        )
        assert route["identity_mode"] == "user"
        assert route["identity_user_sub"] == sub
        assert route["role"] == "editor"
        assert route["remember_callers"] is False
        # Empty string clears the tied user (trigger_slug contract).
        updated = phone_route_store.update_route(
            route["id"], {"identity_mode": "caller", "identity_user_sub": ""},
        )
        assert updated["identity_mode"] == "caller"
        assert updated["identity_user_sub"] is None
        # A None value means "not supplied" — nothing changes.
        same = phone_route_store.update_route(route["id"], {"role": None})
        assert same["role"] == "editor"

    def test_daemon_push_strips_identity_columns(self, temp_db):
        from services.phone.phone_config import assemble_phone_config
        agent = _make_agent()
        sub = _make_user(agents=(agent,))
        route = _make_route(agent, identity_mode="user", identity_user_sub=sub, role="manager")
        pushed = {r["id"]: r for r in assemble_phone_config()["routes"]}
        row = pushed[route["id"]]
        for key in ("identity_mode", "identity_user_sub", "role", "remember_callers",
                    "acknowledge_no_pin"):
            assert key not in row
        assert sub not in str(row)


# ---------------------------------------------------------------------------
# API validators + warnings
# ---------------------------------------------------------------------------

class TestValidators:
    def _put(self, client, route_id: str, body: dict):
        return client.put(f"/v1/admin/phone/routes/{route_id}", json=body)

    def test_enums(self, client):
        agent = _make_agent()
        route = _make_route(agent)
        assert self._put(client, route["id"], {"identity_mode": "anonymous"}).status_code == 400

    def test_role_is_accepted_and_ignored(self, client):
        """The per-route role selector was removed (2026-09-08): the API
        still takes ``role`` from older clients but every save writes
        viewer, and a legacy editor/manager row converges on its next save."""
        agent = _make_agent()
        route = _make_route(agent, role="manager")          # store-level legacy row
        assert phone_route_store.get_route(route["id"])["role"] == "manager"
        r = self._put(client, route["id"], {"role": "admin"})
        assert r.status_code == 200 and r.json()["role"] == "viewer"
        assert self._put(client, route["id"], {"role": "manager"}).json()["role"] == "viewer"
        # (The create path is covered in test_phone_route_cascade.py, which
        # has the verified server + fake adapter a POST needs.)

    def test_shared_is_not_an_identity_any_more(self, client):
        """The 'shared' option was removed (2026-09-07): per-caller on a
        Shared-only agent IS the shared space. The API refuses it, the preview
        refuses it, and a legacy stored value is upgraded on the next save."""
        agent = _make_agent()
        route = _make_route(agent)
        r = self._put(client, route["id"], {"identity_mode": "shared"})
        assert r.status_code == 400 and "caller" in r.json()["detail"]
        assert client.post("/v1/admin/phone/routes", json={
            "direction": "inbound", "agent": agent, "phone_server_id": route["phone_server_id"],
            "identity_mode": "shared",
        }).status_code == 400
        assert client.get("/v1/admin/phone/routes/mcp-preview",
                          params={"agent": agent, "identity_mode": "shared"}).status_code == 400
        legacy = _make_route(agent, identity_mode="shared")     # store-level, pre-removal row
        assert self._put(client, legacy["id"], {"name": "renamed"}).status_code == 200
        assert phone_route_store.get_route(legacy["id"])["identity_mode"] == "caller"

    def test_user_mode_needs_a_qualifying_user(self, client):
        agent = _make_agent()
        route = _make_route(agent)
        # No sub at all.
        assert self._put(client, route["id"], {"identity_mode": "user"}).status_code == 400
        # Unknown user.
        r = self._put(client, route["id"], {"identity_mode": "user", "identity_user_sub": "nobody"})
        assert r.status_code == 400 and "no longer exists" in r.json()["detail"]
        # Known user without access to the agent.
        stranger = _make_user()
        r = self._put(client, route["id"], {"identity_mode": "user", "identity_user_sub": stranger})
        assert r.status_code == 400 and "no access" in r.json()["detail"]
        # Known user who never logged in (no username → no personal space).
        nameless = _make_user(username="", agents=(agent,))
        r = self._put(client, route["id"], {"identity_mode": "user", "identity_user_sub": nameless})
        assert r.status_code == 400 and "username" in r.json()["detail"]
        # Assigned user: OK; a platform admin needs no assignment. (An inbound
        # user line without a PIN needs the acknowledgement, below.)
        member = _make_user(agents=(agent,))
        assert self._put(client, route["id"], {
            "identity_mode": "user", "identity_user_sub": member,
            "acknowledge_no_pin": True,
        }).status_code == 200
        admin = _make_user(role="admin", username="root")
        assert self._put(client, route["id"], {
            "identity_user_sub": admin, "acknowledge_no_pin": True,
        }).status_code == 200

    def test_user_mode_inbound_without_pin_needs_the_acknowledgement(self, client):
        """A user-mode inbound route saves without a PIN only
        when the request says so; the refusal names the user and the flag,
        and the row stays as it was."""
        agent = _make_agent()
        member = _make_user(agents=(agent,))
        route = _make_route(agent)
        body = {"identity_mode": "user", "identity_user_sub": member}
        r = self._put(client, route["id"], body)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "acknowledge_no_pin" in detail and member in detail
        assert "tools and credentials" in detail
        assert phone_route_store.get_route(route["id"])["identity_mode"] == "caller"
        r = self._put(client, route["id"], {**body, "acknowledge_no_pin": True})
        assert r.status_code == 200, r.text
        assert any("no PIN" in w for w in r.json()["warnings"])
        assert "acknowledge_no_pin" not in r.json()
        # An outbound user route needs no flag (there is no PIN to set).
        outbound = _make_route(agent, "outbound")
        assert self._put(client, outbound["id"], body).status_code == 200

    def test_a_pin_in_the_save_satisfies_the_rule(self, client):
        agent = _make_agent()
        member = _make_user(agents=(agent,))
        route = _make_route(agent)
        body = {"identity_mode": "user", "identity_user_sub": member}
        # A malformed or empty PIN is refused before anything changes.
        for bad in ("12", "", "12345678", "abcd"):
            r = self._put(client, route["id"], {**body, "pin": bad})
            assert r.status_code == 400 and "4 to 6 digits" in r.json()["detail"], bad
        assert phone_route_store.get_route(route["id"])["identity_mode"] == "caller"
        assert phone_route_store.get_route_pin(route["id"]) == ""
        r = self._put(client, route["id"], {**body, "pin": "907162"})
        assert r.status_code == 200, r.text
        assert "pin" not in r.json() and "907162" not in r.text
        assert not any("no PIN" in w for w in r.json()["warnings"])
        assert phone_route_store.get_route_pin(route["id"]) == "907162"
        listed = {x["id"]: x for x in client.get("/v1/admin/phone/routes").json()["routes"]}
        assert listed[route["id"]]["pin_configured"] is True
        # `pin: null` means no change.
        r = self._put(client, route["id"], {**body, "pin": None})
        assert r.status_code == 200
        assert phone_route_store.get_route_pin(route["id"]) == "907162"

    def test_a_pin_needs_an_inbound_post_save_direction(self, client):
        agent = _make_agent()
        route = _make_route(agent)
        r = self._put(client, route["id"], {"direction": "outbound", "pin": "1234"})
        assert r.status_code == 400 and "inbound" in r.json()["detail"]
        assert phone_route_store.get_route(route["id"])["direction"] == "inbound"
        # With a stored PIN the direction check still answers before the 409.
        phone_route_store.set_route_pin(route["id"], "1234")
        r = self._put(client, route["id"], {"direction": "outbound", "pin": "1234"})
        assert r.status_code == 400 and "inbound" in r.json()["detail"]
        outbound = _make_route(agent, "outbound")
        assert self._put(client, outbound["id"], {"pin": "1234"}).status_code == 400

    def test_direction_is_an_enum(self, client):
        agent = _make_agent()
        route = _make_route(agent)
        r = self._put(client, route["id"], {"direction": "sideways"})
        assert r.status_code == 400 and "direction" in r.json()["detail"]
        assert phone_route_store.get_route(route["id"])["direction"] == "inbound"

    def test_edits_without_identity_keys_never_need_the_flag(self, client):
        """A route saved before the rule keeps working: the table's enable
        toggle and a rename never carry identity keys; re-submitting its
        identity (the form sends every field) is judged."""
        agent = _make_agent()
        member = _make_user(agents=(agent,))
        route = _make_route(agent, identity_mode="user", identity_user_sub=member)
        assert self._put(client, route["id"], {"enabled": False}).status_code == 200
        assert self._put(client, route["id"], {"name": "renamed"}).status_code == 200
        r = self._put(client, route["id"], {"identity_mode": "user", "identity_user_sub": member})
        assert r.status_code == 400

    def test_explicit_nulls_never_skip_the_rules(self, client):
        """A null in the body is "no change" (the store drops it): the rules
        judge the row that will be written, so a null direction or identity
        mode cannot stand in for a value that would fail them."""
        agent = _make_agent()
        member = _make_user(agents=(agent,))
        route = _make_route(agent)
        r = self._put(client, route["id"], {
            "direction": None, "identity_mode": "user", "identity_user_sub": member,
        })
        assert r.status_code == 400 and "acknowledge_no_pin" in r.json()["detail"]
        assert phone_route_store.get_route(route["id"])["identity_mode"] == "caller"
        user_route = _make_route(agent, identity_mode="user", identity_user_sub=member)
        stranger = _make_user()
        r = self._put(client, user_route["id"], {
            "identity_mode": None, "identity_user_sub": stranger,
        })
        assert r.status_code == 400 and "no access" in r.json()["detail"]
        assert phone_route_store.get_route(user_route["id"])["identity_user_sub"] == member

    def test_pin_removal_on_a_user_route_needs_the_flag(self, client):
        agent = _make_agent()
        member = _make_user(agents=(agent,))
        route = _make_route(agent, identity_mode="user", identity_user_sub=member)
        phone_route_store.set_route_pin(route["id"], "4711")
        r = client.delete(f"/v1/admin/phone/routes/{route['id']}/pin")
        assert r.status_code == 400 and "acknowledge_no_pin" in r.json()["detail"]
        assert phone_route_store.get_route_pin(route["id"]) == "4711"
        r = client.delete(f"/v1/admin/phone/routes/{route['id']}/pin",
                          params={"acknowledge_no_pin": "true"})
        assert r.status_code == 200 and any("no PIN" in w for w in r.json()["warnings"])
        assert phone_route_store.get_route_pin(route["id"]) == ""
        # Nothing stored any more: a repeat needs no flag.
        assert client.delete(f"/v1/admin/phone/routes/{route['id']}/pin").status_code == 200
        # A caller-mode route never needs it.
        caller = _make_route(agent)
        phone_route_store.set_route_pin(caller["id"], "4711")
        assert client.delete(f"/v1/admin/phone/routes/{caller['id']}/pin").status_code == 200
        assert phone_route_store.get_route_pin(caller["id"]) == ""

    def test_the_pin_lands_before_the_row_and_is_restored_on_a_later_failure(
            self, client, monkeypatch):
        agent = _make_agent()
        member = _make_user(agents=(agent,))
        route = _make_route(agent)
        body = {"identity_mode": "user", "identity_user_sub": member, "pin": "4711"}

        real_set, real_update = phone_route_store.set_route_pin, phone_route_store.update_route

        def _boom(*a, **kw):
            raise RuntimeError("store down")
        # The credential store fails: 500, nothing changed.
        monkeypatch.setattr(phone_route_store, "set_route_pin", _boom)
        r = self._put(client, route["id"], body)
        assert r.status_code == 500 and "4711" not in r.text
        assert phone_route_store.get_route(route["id"])["identity_mode"] == "caller"
        assert phone_route_store.get_route_pin(route["id"]) == ""
        monkeypatch.setattr(phone_route_store, "set_route_pin", real_set)
        # The row write fails after the PIN landed: the PIN state is restored.
        monkeypatch.setattr(phone_route_store, "update_route", _boom)
        with pytest.raises(RuntimeError):
            self._put(client, route["id"], body)
        assert phone_route_store.get_route_pin(route["id"]) == ""
        monkeypatch.setattr(phone_route_store, "update_route", real_update)
        phone_route_store.set_route_pin(route["id"], "2468")
        monkeypatch.setattr(phone_route_store, "update_route", _boom)
        with pytest.raises(RuntimeError):
            self._put(client, route["id"], body)
        assert phone_route_store.get_route_pin(route["id"]) == "2468"

    def test_user_mode_warnings(self, client):
        agent = _make_agent()
        member = _make_user(agents=(agent,))
        inbound = _make_route(agent, identity_mode="user", identity_user_sub=member)
        listed = {r["id"]: r for r in client.get("/v1/admin/phone/routes").json()["routes"]}
        assert any("no PIN" in w for w in listed[inbound["id"]]["warnings"])
        # Setting a PIN clears the advisory; removing it brings it back.
        client.put(f"/v1/admin/phone/routes/{inbound['id']}/pin", json={"value": "4711"})
        listed = {r["id"]: r for r in client.get("/v1/admin/phone/routes").json()["routes"]}
        assert listed[inbound["id"]]["warnings"] == []
        r = client.delete(f"/v1/admin/phone/routes/{inbound['id']}/pin",
                          params={"acknowledge_no_pin": "true"})
        assert any("no PIN" in w for w in r.json()["warnings"])
        # Outbound user-tied route: whoever answers acts as the user.
        outbound = _make_route(agent, "outbound", identity_mode="user", identity_user_sub=member)
        listed = {r["id"]: r for r in client.get("/v1/admin/phone/routes").json()["routes"]}
        assert any("whoever answers" in w for w in listed[outbound["id"]]["warnings"])
        # A tied user that lost the agent: advisory, not a broken row.
        with get_conn() as conn:
            conn.execute("DELETE FROM user_agents WHERE sub = %s", (member,))
            conn.commit()
        listed = {r["id"]: r for r in client.get("/v1/admin/phone/routes").json()["routes"]}
        assert any("no access" in w for w in listed[inbound["id"]]["warnings"])

    def test_codex_agent_external_route_carries_no_placement_advisory(self, client):
        """Codex takes external calls in the local sandbox (2026-09-08), and
        an external call never runs on a paired machine on any engine (the
        viewer rule in the target resolver) — so a Codex agent pinned to a
        remote machine gets NO advisory on an external route (the
        "refused on a remote machine" warning was dead code, removed
        2026-09-09), and a user-tied route keeps its own advisories only."""
        codex = _make_agent(execution_path="codex-cli")
        with get_conn() as conn:
            conn.execute("UPDATE agents SET execution_target = %s WHERE slug = %s",
                         ("machine-remote-1", codex))
            conn.commit()
        route = _make_route(codex)
        listed = {r["id"]: r for r in client.get("/v1/admin/phone/routes").json()["routes"]}
        assert listed[route["id"]]["warnings"] == []
        admin = _make_user(role="admin", username="root")
        r = self._put(client, route["id"], {
            "identity_mode": "user", "identity_user_sub": admin, "acknowledge_no_pin": True,
        })
        assert r.status_code == 200
        assert not any("Codex" in w for w in r.json()["warnings"])

    def test_update_response_carries_warnings(self, client):
        agent = _make_agent()
        route = _make_route(agent)
        r = self._put(client, route["id"], {"remember_callers": False})
        assert r.status_code == 200
        assert r.json()["remember_callers"] is False
        assert r.json()["warnings"] == []


# ---------------------------------------------------------------------------
# MCP preview (manifest-only)
# ---------------------------------------------------------------------------

def _manifest(name: str, exclude_from: list[str] | None = None):
    return SimpleNamespace(name=name, label=name.replace("-mcp", "").title(),
                           exclude_from=exclude_from or [])


class TestMcpPreview:
    def test_external_vs_user_identity(self, client, monkeypatch):
        from services.mcp import mcp_registry
        agent = _make_agent()
        fake = [
            _manifest("memory-mcp"),
            _manifest("file-tools"),
            _manifest("schedules-mcp"),                       # hard-denied for external
            _manifest("display-mcp", ["phone", "terminal"]),  # phone exclusion
            _manifest("helpdesk-mcp", ["external"]),          # manifest opt-out
        ]
        monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda a: fake)
        monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: next((m for m in fake if m.name == n), None))

        r = client.get("/v1/admin/phone/routes/mcp-preview", params={"agent": agent})
        assert r.status_code == 200
        body = r.json()
        assert [m["name"] for m in body["attached"]] == ["memory-mcp", "file-tools"]
        reasons = {m["name"]: m["reason"] for m in body["excluded"]}
        assert reasons["schedules-mcp"] == "Not available on external routes"
        assert reasons["display-mcp"] == "Excluded in phone mode"
        assert reasons["helpdesk-mcp"] == "Excluded in external mode"

        r = client.get("/v1/admin/phone/routes/mcp-preview",
                       params={"agent": agent, "identity_mode": "user"})
        names = [m["name"] for m in r.json()["attached"]]
        # A user-tied route keeps the platform MCPs; only the phone exclusion applies.
        assert names == ["memory-mcp", "file-tools", "schedules-mcp", "helpdesk-mcp"]

    def test_unknown_agent_or_mode(self, client):
        assert client.get("/v1/admin/phone/routes/mcp-preview",
                          params={"agent": "ghost"}).status_code == 404
        agent = _make_agent()
        assert client.get("/v1/admin/phone/routes/mcp-preview",
                          params={"agent": agent, "identity_mode": "x"}).status_code == 400

    def test_hard_set_covers_the_management_mcps(self):
        from services.mcp.mcp_registry import EXTERNAL_DENIED_MCPS
        assert {
            "delegation-mcp", "schedules-mcp", "triggers-mcp", "notifications-mcp",
            "meetings-mcp", "agent-config-mcp", "agent-creator-mcp", "mcps-mcp",
            "ssh-hosts", "phone-mcp",
        } <= EXTERNAL_DENIED_MCPS
        assert "memory-mcp" not in EXTERNAL_DENIED_MCPS
