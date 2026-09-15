"""Phone-route ``trigger_slug`` tests.

Covers the admin API validator that gates the route → trigger linkage
(the bound trigger must exist for the route's agent in agent scope) and
the warmup-time resolver that turns ``(route, AMI dial event)`` into the
``trigger_payload`` threaded through to ``get_dynamic_contexts``.
"""

from __future__ import annotations

import uuid

import pytest

from storage.automation import trigger_store
from storage.phone import phone_route_store
from storage.pg import get_conn


def _make_agent(slug: str = "") -> str:
    """Insert a real agents row + return slug. Caller cleans up via the
    test fixture's TRUNCATE.
    """
    slug = slug or f"pr-agent-{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agents (slug, display_name, execution_path, "
            "created_at, updated_at) "
            "VALUES (%s, %s, 'claude-code-cli', NOW()::text, NOW()::text)",
            (slug, slug),
        )
        conn.commit()
    return slug


def _make_phone_server(name: str = "") -> int:
    """Insert a phone_servers row + return its id. Phone routes carry a
    NOT NULL FK to it (provisions routes against a server).
    """
    name = name or f"pbx-{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO phone_servers (name, adapter_type, host, "
            "created_at, updated_at) "
            "VALUES (%s, 'asterisk_freepbx', '', NOW()::text, NOW()::text) "
            "RETURNING id",
            (name,),
        ).fetchone()
        conn.commit()
    return row["id"]


def _make_agent_trigger(agent: str, slug: str = "support-call") -> dict:
    return trigger_store.create_trigger(
        slug=slug, name="Support call", scope="agent",
        agent=agent, created_by="user-admin",
        notify_enabled=False,
        notify_target_scope=None, notify_target=None,
    )


class TestPhoneRouteStoreTriggerSlug:
    def test_create_route_persists_trigger_slug(self, temp_db):
        agent = _make_agent()
        server = _make_phone_server()
        _make_agent_trigger(agent, "inbound-call")
        route = phone_route_store.create_route({
            "direction": "inbound",
            "name": "Main",
            "agent": agent,
            "phone_server_id": server,
            "audiosocket_uuid": str(uuid.uuid4()),
            "trigger_slug": "inbound-call",
        })
        assert route["trigger_slug"] == "inbound-call"

    def test_update_clears_trigger_slug_when_empty_string(self, temp_db):
        agent = _make_agent()
        server = _make_phone_server()
        _make_agent_trigger(agent, "x")
        route = phone_route_store.create_route({
            "direction": "inbound", "agent": agent,
            "phone_server_id": server,
            "audiosocket_uuid": str(uuid.uuid4()),
            "trigger_slug": "x",
        })
        # Empty string clears the binding (per phone_route_store contract).
        updated = phone_route_store.update_route(route["id"], {"trigger_slug": ""})
        assert updated["trigger_slug"] is None

    def test_get_route_by_uuid_returns_trigger_slug(self, temp_db):
        agent = _make_agent()
        server = _make_phone_server()
        _make_agent_trigger(agent, "y")
        u = str(uuid.uuid4())
        phone_route_store.create_route({
            "direction": "inbound", "agent": agent,
            "phone_server_id": server,
            "audiosocket_uuid": u, "trigger_slug": "y",
        })
        row = phone_route_store.get_route_by_uuid(u)
        assert row is not None
        assert row["trigger_slug"] == "y"


@pytest.mark.asyncio
class TestPhoneWarmupTriggerResolver:
    """The ``_resolve_trigger_payload`` warmup helper builds the payload
    dict that downstream ``get_dynamic_contexts`` reads ``${trigger.*}``
    tokens from."""

    async def test_returns_none_without_audiosocket_uuid(self):
        from ws.phone import _resolve_trigger_payload
        result = await _resolve_trigger_payload(
            agent_name="x", audiosocket_uuid="",
            phone_route_id="",
            caller_phone="+1234", caller_did="", dial_event={},
        )
        assert result is None

    async def test_returns_none_when_route_missing(self):
        from ws.phone import _resolve_trigger_payload
        result = await _resolve_trigger_payload(
            agent_name="x", audiosocket_uuid=str(uuid.uuid4()),
            phone_route_id="",
            caller_phone="+1234", caller_did="", dial_event={},
        )
        assert result is None

    async def test_returns_none_when_route_has_no_trigger_slug(self, temp_db):
        agent = _make_agent()
        server = _make_phone_server()
        u = str(uuid.uuid4())
        route = phone_route_store.create_route({
            "direction": "inbound", "agent": agent,
            "phone_server_id": server, "audiosocket_uuid": u,
        })
        from ws.phone import _resolve_trigger_payload
        result = await _resolve_trigger_payload(
            agent_name=agent, audiosocket_uuid=u,
            phone_route_id=route["id"],
            caller_phone="+1234", caller_did="", dial_event={},
        )
        assert result is None

    async def test_assembles_payload_when_route_and_trigger_match(self, temp_db):
        agent = _make_agent()
        server = _make_phone_server()
        _make_agent_trigger(agent, "support")
        u = str(uuid.uuid4())
        route = phone_route_store.create_route({
            "direction": "inbound", "agent": agent,
            "phone_server_id": server, "audiosocket_uuid": u,
            "name": "Main Line", "trigger_slug": "support",
        })
        from ws.phone import _resolve_trigger_payload
        result = await _resolve_trigger_payload(
            agent_name=agent, audiosocket_uuid=u,
            phone_route_id=route["id"],
            caller_phone="+15551234", caller_did="+18005550100",
            dial_event={"channel": "PJSIP/trunk-xyz"},
        )
        assert result is not None
        # ``source`` is the trigger-payload session-type token.
        assert result["source"] == "phone"
        assert result["route"] == "Main Line"
        assert result["phone"] == "+15551234"
        assert result["did"] == "+18005550100"
        assert result["body"] == {"channel": "PJSIP/trunk-xyz"}

    async def test_rejects_route_bound_to_different_agent(self, temp_db):
        # Defence-in-depth: route bound to agent A shouldn't leak its trigger
        # to a warmup for agent B (shouldn't happen normally — admin UI
        # blocks it — but if it does, we degrade gracefully).
        agent_a = _make_agent("pr-agent-a")
        agent_b = _make_agent("pr-agent-b")
        server = _make_phone_server()
        _make_agent_trigger(agent_a, "leak")
        u = str(uuid.uuid4())
        route = phone_route_store.create_route({
            "direction": "inbound", "agent": agent_a,
            "phone_server_id": server, "audiosocket_uuid": u,
            "trigger_slug": "leak",
        })
        from ws.phone import _resolve_trigger_payload
        result = await _resolve_trigger_payload(
            agent_name=agent_b, audiosocket_uuid=u,
            phone_route_id=route["id"],
            caller_phone="+1234", caller_did="", dial_event={},
        )
        assert result is None


class TestBackgroundSound:
    def test_round_trip_and_default(self, temp_db):
        agent = _make_agent()
        server = _make_phone_server()

        # default → 'off'
        r1 = phone_route_store.create_route({
            "direction": "inbound", "agent": agent,
            "phone_server_id": server, "audiosocket_uuid": str(uuid.uuid4()),
        })
        assert r1["background_sound"] == "off"

        # explicit template survives create + update + list (the list is what
        # the config push ships to the phone daemon)
        r2 = phone_route_store.create_route({
            "direction": "inbound", "agent": agent,
            "phone_server_id": server, "audiosocket_uuid": str(uuid.uuid4()),
            "background_sound": "call_center",
        })
        assert r2["background_sound"] == "call_center"

        updated = phone_route_store.update_route(
            r2["id"], {"background_sound": "office"})
        assert updated["background_sound"] == "office"
        by_id = {r["id"]: r for r in phone_route_store.get_all_routes()}
        assert by_id[r2["id"]]["background_sound"] == "office"

    def test_api_validator_rejects_unknown_template(self):
        from fastapi import HTTPException
        from api.phone.phone import _validate_background_sound

        _validate_background_sound("off")
        _validate_background_sound("nature")
        with pytest.raises(HTTPException) as e:
            _validate_background_sound("disco")
        assert e.value.status_code == 400
