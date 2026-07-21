"""Per-session MCP credential broker core.

Store + capability token + the ``/v1/hooks/mcp-credentials`` endpoint. The
endpoint accepts ONLY a per-(session, mcp) capability token — never the session
JWT and never the master key — and derives the ``mcp`` from the token so a token
for one MCP can't fetch another's. Pure in-memory + JWT; no spawn-path wiring yet.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config  # noqa: E402
from core.credentials import mcp_broker # noqa: E402
from core.credentials.mcp_broker import SecretBundle  # noqa: E402


@pytest.fixture(autouse=True)
def clean_store():
    mcp_broker._store.clear()
    yield
    mcp_broker._store.clear()


# ── store lifecycle ────────────────────────────────────────────────────────

def test_provision_get_purge():
    mcp_broker.provision("s1", {"github": SecretBundle(env={"GH_TOKEN": "x"})})
    assert mcp_broker.get("s1", "github").env == {"GH_TOKEN": "x"}
    assert mcp_broker.get("s1", "slack") is None    # other mcp on same session
    assert mcp_broker.get("s2", "github") is None    # other session
    mcp_broker.purge_session("s1")
    assert mcp_broker.get("s1", "github") is None


def test_provision_replaces_and_empty_clears():
    mcp_broker.provision("s1", {"a": SecretBundle(env={"K": "1"})})
    mcp_broker.provision("s1", {"b": SecretBundle(env={"K": "2"})})  # whole-session replace
    assert mcp_broker.get("s1", "a") is None
    assert mcp_broker.get("s1", "b").env == {"K": "2"}
    mcp_broker.provision("s1", {})                                    # empty clears
    assert mcp_broker.get("s1", "b") is None


def test_provision_empty_session_id_is_noop():
    mcp_broker.provision("", {"a": SecretBundle(env={"K": "1"})})
    assert mcp_broker._store == {}


# ── capability token ───────────────────────────────────────────────────────

def test_mint_verify_roundtrip():
    tok = mcp_broker.mint_token("s1", "github")
    assert mcp_broker.verify_token(tok) == ("s1", "github")


def test_verify_rejects_session_jwt():
    from auth.session_token import create_session_token
    sess = create_session_token("s1", "agent", "user-1")  # type == "session"
    assert mcp_broker.verify_token(sess) is None


def test_verify_rejects_master_key_and_garbage():
    assert mcp_broker.verify_token(config.API_KEY) is None
    assert mcp_broker.verify_token("not-a-token") is None
    assert mcp_broker.verify_token("") is None


def test_verify_rejects_expired():
    expired = jwt.encode(
        {"type": "mcp_cred", "sid": "s1", "mcp": "github",
         "exp": datetime.now(timezone.utc) - timedelta(seconds=10)},
        config.JWT_SECRET, algorithm="HS256",
    )
    assert mcp_broker.verify_token(expired) is None


def test_verify_rejects_wrong_secret():
    forged = jwt.encode(
        {"type": "mcp_cred", "sid": "s1", "mcp": "github",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "not-the-jwt-secret", algorithm="HS256",
    )
    assert mcp_broker.verify_token(forged) is None


def test_verify_rejects_missing_claims():
    bad = jwt.encode(
        {"type": "mcp_cred", "sid": "s1",  # no mcp
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        config.JWT_SECRET, algorithm="HS256",
    )
    assert mcp_broker.verify_token(bad) is None


# ── endpoint: cap-token-only, mcp-derived-from-token ───────────────────────

def _call(authorization):
    from api.hooks.hooks import hook_mcp_credentials
    return asyncio.run(hook_mcp_credentials(authorization=authorization))


def test_endpoint_returns_only_the_tokens_mcp():
    mcp_broker.provision("s1", {
        "github": SecretBundle(env={"GH_TOKEN": "gh"}, http_bearer="B"),
        "slack": SecretBundle(env={"SLACK_TOKEN": "sk"}),
    })
    gh = _call(f"Bearer {mcp_broker.mint_token('s1', 'github')}")
    assert gh == {"env": {"GH_TOKEN": "gh"}, "http_bearer": "B"}
    sk = _call(f"Bearer {mcp_broker.mint_token('s1', 'slack')}")
    assert sk == {"env": {"SLACK_TOKEN": "sk"}, "http_bearer": None}


def test_endpoint_rejects_session_jwt_and_master_key():
    from auth.session_token import create_session_token
    mcp_broker.provision("s1", {"github": SecretBundle(env={"GH_TOKEN": "gh"})})
    for bad in (create_session_token("s1", "agent"), config.API_KEY):
        with pytest.raises(HTTPException) as ei:
            _call(f"Bearer {bad}")
        assert ei.value.status_code == 401


def test_endpoint_missing_or_malformed_auth():
    for bad in (None, "Basic xyz", "garbage"):
        with pytest.raises(HTTPException) as ei:
            _call(bad)
        assert ei.value.status_code == 401


def test_endpoint_store_miss_is_404():
    tok = mcp_broker.mint_token("ghost", "github")  # valid token, never provisioned
    with pytest.raises(HTTPException) as ei:
        _call(f"Bearer {tok}")
    assert ei.value.status_code == 404


# ── close purges the store ─────────────────────────────────────────────────

def test_cleanup_session_permission_state_purges_broker():
    from core.session import session_state
    mcp_broker.provision("s1", {"github": SecretBundle(env={"GH_TOKEN": "gh"})})
    session_state.cleanup_session_permission_state("s1")
    assert mcp_broker.get("s1", "github") is None
