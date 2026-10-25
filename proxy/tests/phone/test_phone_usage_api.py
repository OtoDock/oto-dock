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
