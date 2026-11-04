"""The relay client's events calls: the install's signature over the exact
bytes it sends, ``kinds`` on a register, the unbind, and a refusal whose
``detail`` is not a string (FastAPI's 422 list)."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

import config
from services.billing import relay_client


@pytest.fixture
def relay(monkeypatch):
    """A relay answering from ``answers`` (path → (status, body)) and the
    requests it received."""
    seen: list[httpx.Request] = []
    answers: dict[str, tuple[int, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = answers.get(request.url.path, (200, {"ok": True}))
        return httpx.Response(status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_client():
        return client

    monkeypatch.setattr(relay_client, "_relay_http_client", fake_client)
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.test")
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    monkeypatch.setattr(relay_client, "_relay_identity",
                        lambda: {"install_id": "inst-1", "license": "lic"})
    return seen, answers


@pytest.mark.asyncio
async def test_a_signed_post_signs_the_exact_bytes_it_sends(relay):
    seen, _ = relay
    await relay_client._relay_post("/v1/events/register", {"b": 1, "a": "x"}, sign_with="fs")
    request = seen[-1]
    ts = request.headers["x-otodock-install-timestamp"]
    expected = "v0=" + hmac.new(b"fs", f"v0:{ts}:".encode() + request.content,
                                hashlib.sha256).hexdigest()
    assert request.headers["x-otodock-install-signature"] == expected
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"b": 1, "a": "x"}


@pytest.mark.asyncio
async def test_an_unsigned_post_carries_no_signature(relay):
    seen, _ = relay
    await relay_client._relay_post("/v1/oauth/revoke", {"a": 1})
    assert "x-otodock-install-signature" not in seen[-1].headers
    assert "x-otodock-install-timestamp" not in seen[-1].headers


@pytest.mark.asyncio
async def test_a_refusal_whose_detail_is_a_list_reads_as_its_status(relay):
    _, answers = relay
    answers["/v1/events/register"] = (422, {"detail": [{"loc": ["body", "kinds", 0]}]})
    with pytest.raises(relay_client.RelayError) as exc:
        await relay_client.events_register(provider_id="slack", events_url="https://x/e")
    assert exc.value.code == "http_422"
    # The sentence of an unknown code, never a TypeError on an unhashable key.
    assert relay_client.relay_error_message(exc.value.code)
    assert relay_client.relay_error_message(["not", "a", "code"]) == relay_client._UNKNOWN_REFUSAL


@pytest.mark.asyncio
async def test_a_string_detail_is_the_code(relay):
    _, answers = relay
    answers["/v1/events/register"] = (401, {"detail": "bad_signature"})
    with pytest.raises(relay_client.RelayError) as exc:
        await relay_client.events_register(provider_id="slack", events_url="https://x/e",
                                           sign_with="fs")
    assert exc.value.code == "bad_signature"


@pytest.mark.asyncio
async def test_register_sends_kinds_only_when_given(relay):
    seen, _ = relay
    await relay_client.events_register(provider_id="slack", events_url="https://x/e")
    assert "kinds" not in json.loads(seen[-1].content)
    await relay_client.events_register(provider_id="slack", events_url="https://x/e", kinds=[])
    assert json.loads(seen[-1].content)["kinds"] == []
    await relay_client.events_register(provider_id="slack", events_url="https://x/e",
                                       kinds=["message"], sign_with="fs")
    body = json.loads(seen[-1].content)
    assert body["kinds"] == ["message"] and body["install_id"] == "inst-1"
    assert seen[-1].headers["x-otodock-install-signature"].startswith("v0=")


@pytest.mark.asyncio
async def test_unbind_is_signed_and_names_the_pair(relay):
    seen, answers = relay
    answers["/v1/events/unbind"] = (200, {"ok": True, "removed": 1})
    out = await relay_client.events_unbind(provider_id="slack", scope_id="T1", user_key="U1",
                                           sign_with="fs")
    assert out == {"ok": True, "removed": 1}
    body = json.loads(seen[-1].content)
    assert body == {"install_id": "inst-1", "license": "lic", "provider_id": "slack",
                    "scope_id": "T1", "user_key": "U1"}
    await relay_client.events_unbind(provider_id="github", sign_with="fs")
    assert "scope_id" not in json.loads(seen[-1].content)
    with pytest.raises(ValueError):
        await relay_client.events_unbind(provider_id="slack", scope_id="T1", user_key="U1",
                                         sign_with="")


def test_the_events_sentences_read_as_sentences():
    for code in ("bad_signature", "events_reset_needed", "rate_limited"):
        sentence = relay_client.relay_error_message(code)
        assert sentence and sentence != relay_client._UNKNOWN_REFUSAL
        assert ";" not in sentence and "—" not in sentence and "–" not in sentence
    assert "clock" in relay_client.relay_error_message("bad_signature")
    assert "clock" not in relay_client.relay_error_message("events_reset_needed")
