"""The command-line credential hook (``POST /v1/hooks/cli-credential``):
the session's own token opens it and gets the host's credential from the
current token file with the names the wrapper sets; another session's
token, the master key, a stdio MCP's capability token and a dead session
are refused; a session without the host answers 404; an account that needs
a reconnect answers 409 with the resolver's wording.
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

import config
from app import app
from auth.session_token import create_session_token
from core.credentials import mcp_broker
from core.credentials.mcp_gateway import CliCredential, TokenRef
from tests.conftest import live_session_token

client = TestClient(app)


def _provision(sid: str, tmp_path, raw: dict | None = None) -> CliCredential:
    (tmp_path / "acct.json").write_text(json.dumps(
        raw if raw is not None else {"access_token": "ghp_live", "expires_at": "", "extra": {}}))
    cred = CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"),
                         env_names=("GH_TOKEN", "GITHUB_TOKEN"), manifest="")
    mcp_broker.provision(sid, {"github-mcp": mcp_broker.SecretBundle(cli=cred)})
    return cred


def _post(sid: str, token: str, host: str = "github.com"):
    return client.post("/v1/hooks/cli-credential", json={"session_id": sid, "host": host},
                       headers={"Authorization": f"Bearer {token}"})


def test_the_sessions_token_gets_the_hosts_credential(tmp_path):
    sid = str(uuid.uuid4())
    _provision(sid, tmp_path)
    try:
        r = _post(sid, live_session_token(sid, "agent", "user-admin"))
        assert r.status_code == 200
        assert r.json() == {"username": "x-access-token", "password": "ghp_live",
                            "env": ["GH_TOKEN", "GITHUB_TOKEN"]}
        # the current file, read at the call
        (tmp_path / "acct.json").write_text(json.dumps({"access_token": "ghp_rotated", "expires_at": "", "extra": {}}))
        r = _post(sid, live_session_token(sid, "agent", "user-admin"), host="GitHub.com")
        assert r.json()["password"] == "ghp_rotated"
    finally:
        mcp_broker.purge_session(sid)


def test_another_sessions_token_the_master_key_and_a_capability_token_are_refused(tmp_path):
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    _provision(sid, tmp_path)
    try:
        r = _post(sid, live_session_token(other, "agent", "user-admin"))
        assert r.status_code == 403
        r = _post(sid, config.API_KEY)
        assert r.status_code == 403
        r = _post(sid, mcp_broker.mint_token(sid, "github-mcp"))
        assert r.status_code == 401
        r = client.post("/v1/hooks/cli-credential", json={"session_id": sid, "host": "github.com"})
        assert r.status_code == 401
        dead = create_session_token(str(uuid.uuid4()), "agent", "user-1")
        r = _post(str(uuid.uuid4()), dead)
        assert r.status_code == 401
    finally:
        mcp_broker.purge_session(sid)


def test_no_credential_for_the_host_is_404_and_a_dead_account_409(tmp_path):
    sid = str(uuid.uuid4())
    token = live_session_token(sid, "agent", "user-admin")
    r = _post(sid, token)
    assert r.status_code == 404 and "github.com" in r.json()["detail"]
    _provision(sid, tmp_path)
    try:
        r = _post(sid, token, host="gitlab.com")
        assert r.status_code == 404
        (tmp_path / "acct.json").write_text(json.dumps({
            "access_token": "ghp_old", "expires_at": "", "extra": {"refresh_failed": "invalid_grant"},
        }))
        r = _post(sid, token)
        assert r.status_code == 409 and "ended the account's grant" in r.json()["detail"]
        assert "ghp_old" not in r.text
    finally:
        mcp_broker.purge_session(sid)
    # purged: the store is empty again
    r = _post(sid, token)
    assert r.status_code == 404
