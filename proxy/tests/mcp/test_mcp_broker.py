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
    from api.hooks.permission import hook_mcp_credentials
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


# ── OAuth token files ride the bundle ────────────────

def _token_world(monkeypatch):
    """Two stdio OAuth MCPs with a credentials_dir, one bound file each in
    the collector's virtual-path shape, plus neighbours that contribute
    nothing (no credentials_dir, no bound file)."""
    from types import SimpleNamespace
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver as cr

    manifests = [
        SimpleNamespace(name="google-workspace", server_name=""),
        SimpleNamespace(name="google-analytics-mcp", server_name="analytics"),
        SimpleNamespace(name="unbound-mcp", server_name=""),
        SimpleNamespace(name="file-tools", server_name=""),
    ]
    dirs = {
        "google-workspace": [("WORKSPACE_MCP_CREDENTIALS_DIR", "google-tokens")],
        "google-analytics-mcp": [("GA_TOKENS_DIR", "google-analytics-tokens")],
        "unbound-mcp": [("X_DIR", "x-tokens")],
    }
    seen = {}

    def _collect(agent, *, user_sub=None, session_scope="user"):
        seen.update(agent=agent, user_sub=user_sub, session_scope=session_scope)
        base = "/users/alice/.credentials" if session_scope == "user" else "/knowledge/.credentials"
        return {
            f"{base}/google-tokens/a@b.com.json": b'{"access_token": "x"}',
            f"{base}/google-analytics-tokens/a@b.com.json": b'{"access_token": "y"}',
        }

    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda a, **k: manifests)
    monkeypatch.setattr(mcp_registry, "get_credentials_dirs", lambda n: dirs.get(n, []))
    monkeypatch.setattr(cr, "collect_oauth_token_files", _collect)
    return seen


def test_token_file_env_is_keyed_by_config_key_and_is_a_json_string(monkeypatch):
    import json
    from core.credentials import credential_files as cf
    _token_world(monkeypatch)
    out = cf.token_file_env("agent", user_sub="sub-1", session_scope="user")
    assert set(out) == {"google-workspace", "analytics"}
    val = out["google-workspace"][cf.CREDENTIAL_FILES_ENV]
    assert isinstance(val, str)
    assert json.loads(val) == {"WORKSPACE_MCP_CREDENTIALS_DIR": {
        "subpath": "google-tokens", "files": {"a@b.com.json": '{"access_token": "x"}'}}}
    assert json.loads(out["analytics"][cf.CREDENTIAL_FILES_ENV]) == {"GA_TOKENS_DIR": {
        "subpath": "google-analytics-tokens", "files": {"a@b.com.json": '{"access_token": "y"}'}}}
    # The agent scope reads the service account's files the same way.
    assert set(cf.token_file_env("agent", session_scope="agent")) == {"google-workspace", "analytics"}


def test_merge_token_files_extends_and_creates_bundles(monkeypatch):
    from core.credentials import credential_files as cf
    bundles = {"analytics": SecretBundle(env={"GOOGLE_PROJECT_ID": "p"}, http_bearer=None)}
    cf.merge_token_files(bundles, {"analytics": {"OTO_CREDENTIAL_FILES": "{}"},
                                   "google-workspace": {"OTO_CREDENTIAL_FILES": "{}"}})
    assert bundles["analytics"].env == {"GOOGLE_PROJECT_ID": "p", "OTO_CREDENTIAL_FILES": "{}"}
    assert bundles["google-workspace"].env == {"OTO_CREDENTIAL_FILES": "{}"}


def test_attach_token_files_reads_the_session_off_the_config(monkeypatch):
    from types import SimpleNamespace
    from core.credentials import credential_files as cf
    from core.execution_layer import AgentConfig
    seen = _token_world(monkeypatch)
    cfg = AgentConfig(agent_name="agent", user_sub="sub-1",
                      security_context=SimpleNamespace(session_scope="agent"))
    cf.attach_token_files(cfg)
    assert seen == {"agent": "agent", "user_sub": "sub-1", "session_scope": "agent"}
    assert set(cfg.mcp_secret_bundles) == {"google-workspace", "analytics"}
    # A collector failure delivers nothing and the session still builds.
    from services.oauth import credential_resolver as cr

    def _boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(cr, "collect_oauth_token_files", _boom)
    cfg2 = AgentConfig(agent_name="agent", security_context=SimpleNamespace(session_scope="user"))
    cf.attach_token_files(cfg2)
    assert cfg2.mcp_secret_bundles == {}


def test_token_files_round_trip_through_the_interceptor(monkeypatch):
    import json
    from core import stdio_path_interceptor as icpt
    from core.credentials import credential_files as cf
    _token_world(monkeypatch)
    env = cf.token_file_env("agent", user_sub="sub-1")["google-workspace"]
    monkeypatch.setattr(icpt, "_fetch_mcp_credentials", lambda tok: {"env": env})
    child = {"OTO_MCP_FETCH_TOKEN": "cap", "PATH": "/usr/bin"}
    icpt._apply_broker_credentials(child)
    assert "OTO_MCP_FETCH_TOKEN" not in child
    spec = json.loads(child[cf.CREDENTIAL_FILES_ENV])
    assert spec["WORKSPACE_MCP_CREDENTIALS_DIR"]["files"] == {"a@b.com.json": '{"access_token": "x"}'}
