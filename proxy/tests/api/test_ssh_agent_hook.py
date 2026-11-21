"""The SSH agent hook (``POST /v1/hooks/ssh-agent``): a machine's satellite
forwards each message of the session's agent socket here with the session's
own token, and the one signer answers it from the session's record. The
route is plumbing: it decodes one message, runs the signer, encodes the
answer; the signer's judgment is tested in ``tests/credentials``. Refused:
no token, another session's token, the master key, a tool's callback token,
a dead session, a malformed or empty or oversized message.
"""

from __future__ import annotations

import base64
import uuid

from fastapi.testclient import TestClient

import config
from app import app
from auth.session_token import create_callback_token, create_session_token
from core.credentials import ssh_signer
from tests.conftest import live_session_token

client = TestClient(app)
AGENT = "ops-agent"
REQUEST_IDENTITIES = bytes([11])


def _post(sid: str, token: str | None, message: bytes | str = REQUEST_IDENTITIES):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    encoded = message if isinstance(message, str) else base64.b64encode(message).decode("ascii")
    return client.post("/v1/hooks/ssh-agent", json={"session_id": sid, "message_b64": encoded},
                       headers=headers)


def test_the_sessions_token_gets_the_signers_answer_for_that_session(monkeypatch):
    sid = str(uuid.uuid4())
    asked: list[tuple[str, bytes]] = []

    def answer(session_id: str, message: bytes) -> bytes:
        asked.append((session_id, message))
        return bytes([12]) + b"\x00\x00\x00\x00"

    monkeypatch.setattr(ssh_signer, "answer", answer)
    r = _post(sid, live_session_token(sid, AGENT, "user-admin"))
    assert r.status_code == 200
    assert base64.b64decode(r.json()["message_b64"]) == bytes([12]) + b"\x00\x00\x00\x00"
    # The signer judges the session the token names, with the message as sent.
    assert asked == [(sid, REQUEST_IDENTITIES)]
    # A session with no person (an agent-scope task) has its token too.
    assert _post(sid, live_session_token(sid, AGENT, "")).status_code == 200


def test_the_signers_own_refusal_passes_back_as_an_answer():
    """An unknown live session gets the agent protocol's failure, not an
    HTTP error: the client sees a refused signature and moves on."""
    sid = str(uuid.uuid4())
    r = _post(sid, live_session_token(sid, AGENT, "user-admin"))
    assert r.status_code == 200
    assert base64.b64decode(r.json()["message_b64"]) == ssh_signer.FAILURE


def test_every_other_bearer_is_refused(monkeypatch):
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(ssh_signer, "answer", lambda s, m: (_ for _ in ()).throw(AssertionError("signed")))
    assert _post(sid, None).status_code == 401
    assert _post(sid, live_session_token(other, AGENT, "user-admin")).status_code == 403
    assert _post(sid, config.API_KEY).status_code == 403
    assert _post(sid, create_callback_token(sid, AGENT, "user-admin")).status_code == 401
    dead = str(uuid.uuid4())
    assert _post(dead, create_session_token(dead, AGENT, "user-admin")).status_code == 401
    assert _post(sid, live_session_token(sid, AGENT, "user-nobody-has")).status_code == 401


def test_a_malformed_empty_or_oversized_message_is_refused_before_the_signer(monkeypatch):
    sid = str(uuid.uuid4())
    monkeypatch.setattr(ssh_signer, "answer", lambda s, m: (_ for _ in ()).throw(AssertionError("signed")))
    token = live_session_token(sid, AGENT, "user-admin")
    assert _post(sid, token, "not*base64").status_code == 400
    assert _post(sid, token, b"").status_code == 400
    assert _post(sid, token, b"\x0b" * (ssh_signer.MAX_MESSAGE_BYTES + 1)).status_code == 400
    r = client.post("/v1/hooks/ssh-agent", json={"session_id": sid, "message_b64": "A" * 350_001},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422


