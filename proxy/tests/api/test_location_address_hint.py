"""The location answer's address hint reverse-geocodes with the Maps key the
agent's google-maps MCP runs with: the GOOGLE_MAPS_API_KEY field of the
env-delivered instance the agent takes, while that MCP is on the agent. No
key, no call (the hint is optional)."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from api.hooks import lifecycle

KEY_FIELD = "GOOGLE_MAPS_API_KEY"


def _maps_manifest():
    from services.mcp.mcp_registry import (
        CredentialConfig, InstanceConfig, McpManifest, ServerConfig,
    )
    return McpManifest(
        name="google-maps", label="Google Maps", description="", version="0.3.2",
        category="community", server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(type="none"), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], assignment_mode="explicit",
        instances=InstanceConfig(delivery="env", fields=[], max_instances=0),
    )


def _seed_agent(slug: str) -> None:
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agents (slug, display_name, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (slug, slug.title(), now, now),
        )
        conn.commit()


@pytest.fixture
def maps(temp_db, monkeypatch):
    """google-maps installed and on for the platform; agents alpha and beta,
    with the MCP enabled on alpha by its manager."""
    from services.mcp import mcp_registry
    from storage.mcp import mcp_store
    monkeypatch.setitem(mcp_registry._manifests, "google-maps", _maps_manifest())
    mcp_store.set_mcp_enabled("google-maps", True)
    for slug in ("alpha", "beta"):
        _seed_agent(slug)
    mcp_store.add_agent_mcp("alpha", "google-maps")
    return mcp_store


def _instance(store, name: str, key: str, *, agents=(), assigned_to_all=False) -> None:
    store.upsert_mcp_instance("google-maps", {
        "instance_name": name, "field_values": {KEY_FIELD: key},
        "agents": list(agents), "assigned_to_all": assigned_to_all,
    })


def _geocoder(monkeypatch, answer: dict) -> list[httpx.Request]:
    sent: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(lifecycle.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    return sent


ADDRESS = {"status": "OK", "results": [{"formatted_address": "1 Odos, Athens"}]}


def test_the_instance_assigned_to_the_agent_gives_the_key(maps):
    _instance(maps, "default", "k-alpha", agents=["alpha"])
    assert lifecycle._maps_geocode_key("alpha") == "k-alpha"


def test_an_instance_assigned_to_another_agent_gives_none(maps):
    _instance(maps, "default", "k-beta", agents=["beta"])
    assert lifecycle._maps_geocode_key("alpha") == ""


def test_an_instance_assigned_to_all_gives_the_key(maps):
    _instance(maps, "shared", "k-all", assigned_to_all=True)
    assert lifecycle._maps_geocode_key("alpha") == "k-all"


def test_assigned_to_all_is_not_enough_without_the_mcp_on_the_agent(maps):
    _instance(maps, "shared", "k-all", assigned_to_all=True)
    assert lifecycle._maps_geocode_key("beta") == ""
    maps.set_mcp_enabled("google-maps", False)
    assert lifecycle._maps_geocode_key("alpha") == ""


def test_no_maps_mcp_installed_gives_none(maps, monkeypatch):
    from services.mcp import mcp_registry
    _instance(maps, "default", "k-alpha", agents=["alpha"])
    monkeypatch.delitem(mcp_registry._manifests, "google-maps", raising=False)
    assert lifecycle._maps_geocode_key("alpha") == ""


def test_the_hint_asks_the_geocoding_api_with_the_agents_key(maps, monkeypatch):
    _instance(maps, "default", "k-alpha", agents=["alpha"])
    sent = _geocoder(monkeypatch, ADDRESS)
    assert asyncio.run(lifecycle._address_hint("alpha", 37.97, 23.72)) == "1 Odos, Athens"
    assert len(sent) == 1
    assert sent[0].url.host == "maps.googleapis.com"
    assert sent[0].url.params["key"] == "k-alpha"
    assert sent[0].url.params["latlng"] == "37.97,23.72"


def test_no_key_on_the_instance_means_no_call(maps, monkeypatch):
    _instance(maps, "default", "", agents=["alpha"])
    sent = _geocoder(monkeypatch, ADDRESS)
    assert asyncio.run(lifecycle._address_hint("alpha", 37.97, 23.72)) == ""
    assert sent == []


def test_an_agent_without_the_mcp_means_no_call(maps, monkeypatch):
    _instance(maps, "default", "k-beta", agents=["beta"])
    sent = _geocoder(monkeypatch, ADDRESS)
    assert asyncio.run(lifecycle._address_hint("beta", 37.97, 23.72)) == ""
    assert sent == []


def test_a_refused_key_gives_no_hint_and_says_why(maps, monkeypatch, caplog):
    _instance(maps, "default", "k-alpha", agents=["alpha"])
    sent = _geocoder(monkeypatch, {"status": "REQUEST_DENIED", "results": [],
                                   "error_message": "This API project is not authorized to use this API."})
    with caplog.at_level("INFO", logger="otodock"):
        assert asyncio.run(lifecycle._address_hint("alpha", 37.97, 23.72)) == ""
    assert len(sent) == 1
    assert "REQUEST_DENIED" in caplog.text
    assert "k-alpha" not in caplog.text


def test_no_position_on_the_sea_gives_no_hint_and_no_refusal_line(maps, monkeypatch, caplog):
    _instance(maps, "default", "k-alpha", agents=["alpha"])
    sent = _geocoder(monkeypatch, {"status": "ZERO_RESULTS", "results": []})
    with caplog.at_level("INFO", logger="otodock"):
        assert asyncio.run(lifecycle._address_hint("alpha", 0.0, -30.0)) == ""
    assert len(sent) == 1
    assert "REQUEST_DENIED" not in caplog.text


def test_a_position_without_a_longitude_is_not_sent(maps, monkeypatch):
    _instance(maps, "default", "k-alpha", agents=["alpha"])
    sent = _geocoder(monkeypatch, ADDRESS)
    assert asyncio.run(lifecycle._address_hint("alpha", 37.97, None)) == ""
    assert sent == []


@pytest.mark.asyncio
async def test_the_request_hints_with_the_calling_sessions_agent(monkeypatch):
    """The session token's agent picks the key, and a zero latitude is a
    position like any other."""
    from auth.session_token import create_session_token

    async def _allow(_authorization):
        return None

    async def _answer(request_id, timeout, session_id):
        return {"lat": 0.0, "lng": 23.72, "accuracy": 10}

    asked = []

    async def _hint(agent, lat, lng):
        asked.append((agent, lat, lng))
        return "Gulf of Guinea"

    monkeypatch.setattr(lifecycle, "verify_api_key_async", _allow)
    monkeypatch.setattr(lifecycle, "wait_for_location", _answer)
    monkeypatch.setattr(lifecycle, "_address_hint", _hint)
    monkeypatch.setattr(lifecycle.task_store, "get_chat_by_session", lambda sid: None)
    monkeypatch.setitem(lifecycle._dashboard_notify_queues, "sid-loc", asyncio.Queue())
    token = create_session_token("sid-loc", "alpha", "user-1")

    result = await lifecycle.request_user_location(
        authorization=f"Bearer {token}", x_agent_name="beta")

    assert asked == [("alpha", 0.0, 23.72)]
    assert result["address_hint"] == "Gulf of Guinea"
