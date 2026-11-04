"""Phone turn-classifier usage endpoint (local per-agent cost tracking).

POST /v1/phone/usage/turn-classifier records ONE usage_records row per call
(source_type='turn-classifier', scope='agent', user_sub='phone'), priced from the
Groq adapter. Auth = the internal master key (verify_api_key).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config


@pytest.fixture
def client(temp_db):
    from api.phone import phone_usage as phone_usage_router
    app = FastAPI()
    app.include_router(phone_usage_router.router)
    return TestClient(app)


def _rows():
    from storage.pg import get_conn
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_sub, agent, scope, source_type, source_id, provider, model, "
            "input_tokens, output_tokens, message_count, cost_usd "
            "FROM usage_records WHERE source_type = 'turn-classifier'"
        ).fetchall()


def _auth():
    return {"Authorization": f"Bearer {config.API_KEY}"}


def test_records_one_agent_scoped_row(client):
    r = client.post(
        "/v1/phone/usage/turn-classifier", headers=_auth(),
        json={"agent": "personal-assistant", "model": "openai/gpt-oss-120b",
              "input_tokens": 1000, "output_tokens": 4, "session_id": "sess-1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is True
    assert body["cost_usd"] == pytest.approx(1000 / 1e6 * 0.15 + 4 / 1e6 * 0.60)

    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["user_sub"] == "phone"
    assert row["agent"] == "personal-assistant"
    assert row["scope"] == "agent"
    assert row["provider"] == "groq"
    assert row["model"] == "openai/gpt-oss-120b"
    assert row["input_tokens"] == 1000 and row["output_tokens"] == 4
    assert row["message_count"] == 0
    assert row["source_id"] == "sess-1"
    assert row["cost_usd"] > 0


def test_auth_required(client):
    r = client.post("/v1/phone/usage/turn-classifier",
                    json={"agent": "x", "input_tokens": 10, "output_tokens": 1})
    assert r.status_code == 401
    assert _rows() == []


def test_zero_tokens_records_nothing(client):
    r = client.post(
        "/v1/phone/usage/turn-classifier", headers=_auth(),
        json={"agent": "x", "input_tokens": 0, "output_tokens": 0},
    )
    assert r.status_code == 200
    assert r.json()["recorded"] is False
    assert _rows() == []


def test_empty_model_defaults_to_gpt_oss(client):
    r = client.post(
        "/v1/phone/usage/turn-classifier", headers=_auth(),
        json={"agent": "a", "input_tokens": 50, "output_tokens": 2},  # no model
    )
    assert r.status_code == 200 and r.json()["recorded"] is True
    assert _rows()[0]["model"] == "openai/gpt-oss-120b"


# ── A call's second listen (POST /v1/phone/usage/relisten) ──────────

def _relisten_rows():
    from storage.pg import get_conn
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_sub, agent, scope, source_type, source_id, provider, audio_seconds, cost_usd "
            "FROM usage_records WHERE source_type = 'phone-stt-relisten'"
        ).fetchall()


def test_a_calls_second_listen_is_a_usage_row_of_its_own(client):
    r = client.post(
        "/v1/phone/usage/relisten", headers=_auth(),
        json={"agent": "personal-assistant", "provider": "deepgram", "seconds": 12.5, "session_id": "sess-2"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is True
    rows = _relisten_rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row["user_sub"], row["agent"], row["scope"]) == ("phone", "personal-assistant", "agent")
    assert row["provider"] == "deepgram" and row["source_id"] == "sess-2"
    assert row["audio_seconds"] == pytest.approx(12.5) and row["cost_usd"] > 0


def test_a_second_listen_report_needs_the_key_and_a_known_provider(client):
    assert client.post("/v1/phone/usage/relisten",
                       json={"provider": "deepgram", "seconds": 3}).status_code == 401
    r = client.post("/v1/phone/usage/relisten", headers=_auth(),
                    json={"provider": "no-such-stt", "seconds": 3})
    assert r.status_code == 200 and r.json()["recorded"] is False
    r = client.post("/v1/phone/usage/relisten", headers=_auth(), json={"provider": "deepgram", "seconds": 0})
    assert r.json()["recorded"] is False
    assert _relisten_rows() == []


def test_the_second_listen_report_is_on_the_service_key_allowlist():
    """The daemon calls it with the master key: the service-key confinement
    lets only listed paths through, method-scoped."""
    from auth.service_endpoints import is_service_endpoint_allowed
    assert is_service_endpoint_allowed("POST", "/v1/phone/usage/relisten")
    assert not is_service_endpoint_allowed("GET", "/v1/phone/usage/relisten")


# ── A spoken conversation's speech (POST /v1/phone/usage/duplex) ─────

def _duplex_rows():
    from storage.pg import get_conn
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_sub, agent, scope, source_type, source_id, provider, audio_seconds, cost_usd "
            "FROM usage_records WHERE source_type LIKE 'audio-stt-duplex%' ORDER BY source_type"
        ).fetchall()


def test_a_conversations_speech_is_the_speakers_rows_with_the_chats_agent(client):
    """The speaker (the bridge's sub), never the chat's owner: an agent-scope
    chat is owned by the shared owner, a task chat by its creator."""
    import storage.database as task_store
    task_store.create_chat("chat-dx", "agent::personal-assistant", "personal-assistant")
    r = client.post(
        "/v1/phone/usage/duplex", headers=_auth(),
        json={"chat_id": "chat-dx", "sub": "user-speaker", "provider": "deepgram",
              "stream_seconds": 42.5, "relisten_seconds": 3.25, "session_id": "dx-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is True
    rows = _duplex_rows()
    assert [(row["source_type"], row["user_sub"], row["agent"], row["scope"]) for row in rows] == [
        ("audio-stt-duplex", "user-speaker", "personal-assistant", "user"),
        ("audio-stt-duplex-relisten", "user-speaker", "personal-assistant", "user"),
    ]
    assert [row["audio_seconds"] for row in rows] == [pytest.approx(42.5), pytest.approx(3.25)]
    assert all(row["provider"] == "deepgram" and row["source_id"] == "dx-1" and row["cost_usd"] > 0 for row in rows)


def test_a_conversation_without_a_second_listen_writes_one_row_and_the_seconds_are_clipped(client):
    import storage.database as task_store
    task_store.create_chat("chat-dx2", "user-speaker", "personal-assistant")
    r = client.post("/v1/phone/usage/duplex", headers=_auth(),
                    json={"chat_id": "chat-dx2", "sub": "user-speaker", "provider": "deepgram",
                          "stream_seconds": 10 * 3600.0})
    assert r.json()["recorded"] is True
    rows = _duplex_rows()
    assert len(rows) == 1 and rows[0]["source_type"] == "audio-stt-duplex"
    assert rows[0]["audio_seconds"] == pytest.approx(4 * 3600.0)


def test_a_conversation_whose_chat_is_gone_records_nothing(client, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="otodock"):
        r = client.post("/v1/phone/usage/duplex", headers=_auth(),
                        json={"chat_id": "chat-gone", "sub": "user-speaker", "provider": "deepgram",
                              "stream_seconds": 5.0})
    assert r.status_code == 200 and r.json()["recorded"] is False
    assert _duplex_rows() == []
    assert any("chat-gone is gone" in rec.getMessage() for rec in caplog.records)


def test_a_duplex_report_needs_the_master_key_never_a_session_token(client):
    import storage.database as task_store
    from tests.conftest import live_session_token
    task_store.create_chat("chat-dx3", "user-speaker", "personal-assistant")
    body = {"chat_id": "chat-dx3", "sub": "user-speaker", "provider": "deepgram", "stream_seconds": 5.0}
    assert client.post("/v1/phone/usage/duplex", json=body).status_code == 401
    token = live_session_token("sess-dx", "personal-assistant", "user-speaker")
    assert client.post("/v1/phone/usage/duplex", headers={"Authorization": f"Bearer {token}"},
                       json=body).status_code == 401
    # A report that names nobody, no chat or no provider is dropped, never guessed.
    for missing in ({"sub": ""}, {"chat_id": ""}, {"provider": ""}, {"stream_seconds": 0}):
        r = client.post("/v1/phone/usage/duplex", headers=_auth(), json={**body, **missing})
        assert r.status_code == 200 and r.json()["recorded"] is False
    assert _duplex_rows() == []


def test_the_duplex_report_is_on_the_service_key_allowlist():
    from auth.service_endpoints import is_service_endpoint_allowed
    assert is_service_endpoint_allowed("POST", "/v1/phone/usage/duplex")
    assert not is_service_endpoint_allowed("GET", "/v1/phone/usage/duplex")
    assert not is_service_endpoint_allowed("POST", "/v1/phone/usage/duplex/other")

