"""The webhook fire routes' key check.

The key's bcrypt and its store read never run on the event loop; a key
verified recently is served from a digest cache that re-reads its row (a
revocation is immediate); a miss is gated per key prefix before any bcrypt
and waits in a bounded queue (503 past it). Handlers are driven directly
inside ``asyncio.run`` (``TestClient`` runs the app on another thread).
"""

import asyncio
import secrets

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import config
from api.events import triggers
from auth import lan_check, rate_limiter
from services.infra import api_key_manager
from storage.automation import trigger_store


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    lan_check.reset_state()
    rate_limiter._attempts.clear()
    triggers._verified_keys.clear()
    monkeypatch.setattr(triggers, "_verify_gate", triggers._VerifyGate())
    yield
    rate_limiter._attempts.clear()
    triggers._verified_keys.clear()


def _request(token: str, ip="198.51.100.20"):
    headers = [(b"authorization", f"Bearer {token}".encode()),
               (b"content-type", b"application/json")]

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/v1/webhooks/agent/a/s",
                    "headers": headers, "client": (ip, 1234), "server": ("testserver", 80),
                    "scheme": "http", "query_string": b""}, receive)


@pytest.fixture
def agent_key(monkeypatch):
    row, raw = api_key_manager.create_agent_key(
        agent="agent-x", name="k", permissions=["triggers"], created_by="user-admin")
    trigger_store.create_trigger(slug="deploy", name="Deploy", scope="agent", agent="agent-x",
                                 created_by="user-admin")
    fired = []

    async def fake_fire(trigger, body, **kw):
        fired.append(trigger["slug"])
        return {"status": "fired"}

    monkeypatch.setattr(triggers.trigger_manager, "fire_trigger", fake_fire)
    return row, raw, fired


def _counting_bcrypt(monkeypatch):
    calls = []
    real = api_key_manager._check_key

    def counting(raw, hashed):
        calls.append(1)
        return real(raw, hashed)

    monkeypatch.setattr(api_key_manager, "_check_key", counting)
    return calls


async def _fire(token, ip="198.51.100.20"):
    try:
        await triggers.fire_agent_trigger("agent-x", "deploy", _request(token, ip))
        return 200
    except HTTPException as e:
        return e.status_code


def test_no_store_call_and_no_bcrypt_on_the_loop(agent_key, loop_db_guard, monkeypatch):
    _row, raw, fired = agent_key
    forged = "otok_" + secrets.token_urlsafe(32)

    async def scenario():
        with loop_db_guard.active():
            assert await _fire(raw) == 200
            assert await _fire(raw) == 200
            assert await _fire(forged) == 403

    asyncio.run(scenario())
    assert fired == ["deploy", "deploy"]


def test_the_cache_spares_the_bcrypt_and_a_revocation_is_immediate(agent_key, monkeypatch):
    row, raw, fired = agent_key
    calls = _counting_bcrypt(monkeypatch)

    async def scenario():
        codes = [await _fire(raw) for _ in range(5)]
        await asyncio.to_thread(api_key_manager.revoke_agent_key, row["id"])
        codes.append(await _fire(raw))
        return codes

    codes = asyncio.run(scenario())
    assert codes == [200] * 5 + [403]
    assert len(calls) == 1 and len(fired) == 5


def test_the_prefix_gate_holds_before_bcrypt_and_a_cached_key_passes_it(agent_key, monkeypatch):
    _row, raw, fired = agent_key
    prefix = raw[len("otok_"):len("otok_") + api_key_manager.KEY_INDEX_PREFIX_LEN]
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "webhook_prefix",
                        {"max": 3, "window": 300, "base_block": 300, "max_block": 3600})
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "webhook_auth",
                        {"max": 1000, "window": 300, "base_block": 300, "max_block": 3600})

    async def scenario():
        assert await _fire(raw) == 200                       # cached from here on
        junk = [await _fire("otok_" + prefix + secrets.token_urlsafe(20), ip=f"203.0.113.{i}")
                for i in range(5)]
        calls = _counting_bcrypt(monkeypatch)
        blocked = await _fire("otok_" + prefix + secrets.token_urlsafe(20), ip="203.0.113.9")
        live = await _fire(raw)
        return junk, blocked, live, calls

    junk, blocked, live, calls = asyncio.run(scenario())
    assert junk[:3] == [403, 403, 403] and junk[3:] == [429, 429]
    assert blocked == 429 and not calls
    assert live == 200


def test_a_full_queue_answers_503(agent_key, monkeypatch):
    import time as _time
    _row, raw, _fired = agent_key
    monkeypatch.setattr(config, "WEBHOOK_KEY_VERIFY_CONCURRENCY", 1)
    monkeypatch.setattr(config, "WEBHOOK_KEY_VERIFY_MAX_WAITERS", 1)
    real = api_key_manager.verify_bearer_for_agent

    def slow(*a, **kw):
        _time.sleep(0.3)
        return real(*a, **kw)

    monkeypatch.setattr(api_key_manager, "verify_bearer_for_agent", slow)

    async def scenario():
        tokens = ["otok_" + secrets.token_urlsafe(32) for _ in range(3)]
        return await asyncio.gather(*[_fire(t, ip=f"203.0.113.{40 + i}") for i, t in enumerate(tokens)])

    codes = asyncio.run(scenario())
    assert sorted(codes) == [403, 403, 503]


def test_a_concurrent_burst_of_one_key_shares_one_verification(agent_key, monkeypatch):
    """The first burst of a legitimate sender finds the cache empty: its fires
    wait for the one verification in flight instead of filling the queue
    (filled, 22 of 40 concurrent fires would answer 503)."""
    import time as _time
    _row, raw, fired = agent_key
    monkeypatch.setattr(config, "WEBHOOK_KEY_VERIFY_CONCURRENCY", 1)
    monkeypatch.setattr(config, "WEBHOOK_KEY_VERIFY_MAX_WAITERS", 1)
    real = api_key_manager.verify_bearer_for_agent
    calls = []

    def slow(*a, **kw):
        calls.append(1)
        _time.sleep(0.2)
        return real(*a, **kw)

    monkeypatch.setattr(api_key_manager, "verify_bearer_for_agent", slow)

    async def scenario():
        return await asyncio.gather(*[_fire(raw, ip=f"203.0.113.{100 + i}") for i in range(30)])

    codes = asyncio.run(scenario())
    assert codes == [200] * 30 and len(calls) == 1 and len(fired) == 30
