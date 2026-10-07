"""Per-session secret-FILE broker (SSH keys → admin-paired satellites).

File store + session-files capability token + the ``/v1/hooks/session-files``
endpoint + the admin-paired gating semantics in ``build_session_mcp_config`` /
the ssh-hosts dynamic-context provider. Mirrors test_mcp_broker.py for the
env-secret half of the broker.
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
from core.credentials import mcp_broker  # noqa: E402
from core.credentials.mcp_broker import SessionFile  # noqa: E402
from core import placement


@pytest.fixture(autouse=True)
def clean_stores():
    mcp_broker._store.clear()
    mcp_broker._file_store.clear()
    yield
    mcp_broker._store.clear()
    mcp_broker._file_store.clear()


# ── file store lifecycle ───────────────────────────────────────────────────

def test_provision_get_purge_files():
    mcp_broker.provision_session_files(
        "s1", {"ssh/key1": SessionFile(content_b64="QQ==")},
    )
    files = mcp_broker.get_session_files("s1")
    assert files and files["ssh/key1"].content_b64 == "QQ=="
    assert files["ssh/key1"].mode == 0o600
    assert mcp_broker.get_session_files("s2") is None
    mcp_broker.purge_session("s1")
    assert mcp_broker.get_session_files("s1") is None


def test_purge_session_clears_both_stores():
    from core.credentials.mcp_broker import SecretBundle
    mcp_broker.provision("s1", {"a": SecretBundle(env={"K": "1"})})
    mcp_broker.provision_session_files("s1", {"f": SessionFile(content_b64="QQ==")})
    mcp_broker.purge_session("s1")
    assert mcp_broker.get("s1", "a") is None
    assert mcp_broker.get_session_files("s1") is None


# ── session-files capability token ─────────────────────────────────────────

def test_files_token_roundtrip():
    tok = mcp_broker.mint_files_token("s1")
    assert mcp_broker.verify_files_token(tok) == "s1"


def test_files_token_rejects_other_types():
    # A per-MCP cred token must not open the files endpoint and vice versa.
    cred = mcp_broker.mint_token("s1", "github")
    assert mcp_broker.verify_files_token(cred) is None
    files = mcp_broker.mint_files_token("s1")
    assert mcp_broker.verify_token(files) is None
    # Session JWT / master key / garbage all fail.
    from auth.session_token import create_session_token
    assert mcp_broker.verify_files_token(
        create_session_token("s1", "agent", "user-1")
    ) is None
    assert mcp_broker.verify_files_token(config.API_KEY) is None
    assert mcp_broker.verify_files_token("") is None


def test_files_token_rejects_expired_and_forged():
    expired = jwt.encode(
        {"type": "session_files", "sid": "s1",
         "exp": datetime.now(timezone.utc) - timedelta(seconds=10)},
        config.JWT_SECRET, algorithm="HS256",
    )
    assert mcp_broker.verify_files_token(expired) is None
    forged = jwt.encode(
        {"type": "session_files", "sid": "s1",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "not-the-jwt-secret", algorithm="HS256",
    )
    assert mcp_broker.verify_files_token(forged) is None


# ── endpoint ───────────────────────────────────────────────────────────────

def _call(authorization):
    from api.hooks.permission import hook_session_files
    return asyncio.run(hook_session_files(authorization=authorization))


def test_endpoint_returns_session_files():
    mcp_broker.provision_session_files(
        "s1", {"ssh/prod": SessionFile(content_b64="S0VZ", mode=0o600)},
    )
    out = _call(f"Bearer {mcp_broker.mint_files_token('s1')}")
    assert out == {"files": {"ssh/prod": {"content_b64": "S0VZ", "mode": 0o600}}}


def test_endpoint_rejects_non_files_tokens():
    mcp_broker.provision_session_files(
        "s1", {"ssh/prod": SessionFile(content_b64="S0VZ")},
    )
    from auth.session_token import create_session_token
    for bad in (
        f"Bearer {create_session_token('s1', 'agent', 'user-1')}",
        f"Bearer {mcp_broker.mint_token('s1', 'github')}",
        f"Bearer {config.API_KEY}",
        "Bearer garbage",
        "garbage",
        None,
    ):
        with pytest.raises(HTTPException) as e:
            _call(bad)
        assert e.value.status_code == 401


def test_endpoint_store_miss_is_404():
    tok = mcp_broker.mint_files_token("s-gone")
    with pytest.raises(HTTPException) as e:
        _call(f"Bearer {tok}")
    assert e.value.status_code == 404


# ── OAuth token-file collection (credentials_dir MCPs → broker files) ──────

def _oauth_manifest(name, runtime="python", oauth=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        name=name,
        server=SimpleNamespace(runtime=runtime),
        credentials=SimpleNamespace(oauth=oauth if oauth is not None else {}),
    )


@pytest.fixture
def token_source(monkeypatch, tmp_path):
    """Stub the registry + account binding so collect_oauth_token_files sees
    one google-workspace-shaped MCP plus skip-shaped neighbors."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver as cr

    token_dir = tmp_path / "central" / "google-tokens" / "alice"
    token_dir.mkdir(parents=True)
    (token_dir / "alice@gmail.com.json").write_bytes(b'{"access_token":"x"}')

    manifests = [
        _oauth_manifest("google-workspace", oauth={"provider_id": "google"}),
        # bearer-injected → token travels via the tunnel bearer swap, no file
        _oauth_manifest("github", oauth={"provider_id": "github",
                                         "bearer_required": True}),
        # docker runtime → never spawned on a satellite
        _oauth_manifest("boxed", runtime="docker",
                        oauth={"provider_id": "google"}),
        # no oauth at all
        _oauth_manifest("file-tools"),
    ]
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda a: manifests)
    monkeypatch.setattr(
        mcp_registry, "get_credentials_dirs",
        lambda n: (
            [("WORKSPACE_MCP_CREDENTIALS_DIR", "google-tokens")]
            if n in ("google-workspace", "boxed") else []
        ),
    )
    monkeypatch.setattr(
        cr, "_bound_token_source",
        lambda mcp, pid, *, user_sub, task_scope, agent_name: (
            token_dir, "alice@gmail.com", "alice"
        ),
    )
    return token_dir


def test_collect_oauth_token_files_user_scope(token_source):
    from services.oauth import credential_resolver as cr
    out = cr.collect_oauth_token_files(
        "agent", user_sub="sub-1", session_scope="user",
    )
    # Only the stdio credentials_dir MCP contributes; the key is the
    # sandbox-virtual per-session path the MCP's env already points at.
    assert out == {
        "/users/alice/.credentials/google-tokens/alice@gmail.com.json":
            b'{"access_token":"x"}',
    }
    # The local broker's form: the same file under the manifest it was
    # collected for.
    assert cr.collect_oauth_token_files_by_manifest(
        "agent", user_sub="sub-1", session_scope="user",
    ) == {"google-workspace": out}


def test_collect_oauth_token_files_agent_scope(token_source):
    from services.oauth import credential_resolver as cr
    out = cr.collect_oauth_token_files("agent", session_scope="agent")
    # Agent-scope sessions read from knowledge/.credentials — even though
    # the bound (service) account belongs to a real user.
    assert out == {
        "/knowledge/.credentials/google-tokens/alice@gmail.com.json":
            b'{"access_token":"x"}',
    }


def test_collect_oauth_token_files_missing_file_skipped(token_source):
    from services.oauth import credential_resolver as cr
    (token_source / "alice@gmail.com.json").unlink()
    assert cr.collect_oauth_token_files("agent", user_sub="s") == {}


@pytest.mark.parametrize("body", [
    # the worker recorded a permanent refresh failure (a revoked grant)
    b'{"access_token":"x","refresh_token":"r","expires_at":"2099-01-01T00:00:00Z",'
    b'"extra":{"refresh_failed":"invalid_grant"}}',
    # expired, with no refresh token to renew it
    b'{"access_token":"x","expires_at":"2020-01-01T00:00:00Z"}',
])
def test_a_token_file_that_needs_a_reconnect_is_not_delivered(token_source, body):
    """The resolver leaves such an MCP out of the session
    (``AccountNeedsReconnect``); its dead file is not delivered either, to a
    satellite (``collect_oauth_token_files``) or to a local session's secret
    bundle (``credential_files.token_file_env``)."""
    from core.credentials import credential_files
    from services.oauth import credential_resolver as cr
    (token_source / "alice@gmail.com.json").write_bytes(body)
    assert cr.collect_oauth_token_files("agent", user_sub="sub-1", session_scope="user") == {}
    assert credential_files.token_file_env("agent", user_sub="sub-1", session_scope="user") == {}


# ── _collect_session_files gating (pairing scope × session scope) ──────────

def _remote_cfg(scope="user", username="alice", user_sub="sub-1"):
    from types import SimpleNamespace
    from core.execution_layer import AgentConfig
    return AgentConfig(
        agent_name="agent", user_sub=user_sub,
        # A manager's session: the keys take the editor tier as well as the
        # admin-paired machine these cases are about.
        security_context=SimpleNamespace(
            session_scope=scope, username=username, role="manager",
        ),
    )


@pytest.fixture
def stub_collectors(monkeypatch, tmp_path):
    key = tmp_path / "prod_key"
    key.write_bytes(b"PRIVATE")
    monkeypatch.setattr(
        "core.sandbox.session_config_dir.collect_authorized_ssh_keys",
        lambda agent: {"prod_key": key},
    )
    monkeypatch.setattr(
        "services.oauth.credential_resolver.collect_oauth_token_files",
        lambda agent, *, user_sub, session_scope: {
            "/users/alice/.credentials/google-tokens/a.json": b"TOKEN",
        },
    )


def test_admin_paired_gets_ssh_and_tokens(stub_collectors):
    from core.remote.remote_execution import _collect_session_files
    files = _collect_session_files(
        _remote_cfg(), {"pairing_scope": "admin"}, None,
    )
    assert set(files) == {
        "ssh/prod_key",
        "/users/alice/.credentials/google-tokens/a.json",
    }
    import base64
    assert base64.b64decode(files["ssh/prod_key"].content_b64) == b"PRIVATE"


def test_a_judge_is_never_handed_the_ssh_keys(stub_collectors):
    """A check's judge (read_only) reads: on an admin-paired machine it gets
    the token files its MCPs may need, never the agent's SSH keys."""
    from types import SimpleNamespace
    from core.remote.remote_execution import _collect_session_files
    cfg = _remote_cfg()
    cfg.security_context = SimpleNamespace(session_scope="user", username="alice", read_only=True)
    files = _collect_session_files(cfg, {"pairing_scope": "admin"}, None)
    assert set(files) == {"/users/alice/.credentials/google-tokens/a.json"}


def test_session_takes_ssh_keys():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from core.sandbox.session_config_dir import session_takes_ssh_keys
    # The editor tier, read off the context's role; anything else is no keys.
    assert session_takes_ssh_keys(SimpleNamespace(role="editor", read_only=False)) is True
    assert session_takes_ssh_keys(SimpleNamespace(role="manager", read_only=True)) is False
    assert session_takes_ssh_keys(SimpleNamespace(role="manager", principal="external")) is False
    assert session_takes_ssh_keys(SimpleNamespace(role="contributor")) is False
    assert session_takes_ssh_keys(SimpleNamespace(read_only=False)) is False  # no role: no keys
    assert session_takes_ssh_keys(None) is False
    assert session_takes_ssh_keys(MagicMock()) is False  # a mock is not a role


def test_user_paired_owner_gets_only_own_tokens(stub_collectors):
    from core.remote.remote_execution import _collect_session_files
    # Owner's own user-scope session: tokens yes, SSH keys never.
    files = _collect_session_files(
        _remote_cfg(), {"pairing_scope": "user"}, "alice",
    )
    assert set(files) == {"/users/alice/.credentials/google-tokens/a.json"}


def test_user_paired_non_owner_and_agent_scope_get_nothing(stub_collectors):
    from core.remote.remote_execution import _collect_session_files
    # Session user isn't the machine owner → nothing.
    assert _collect_session_files(
        _remote_cfg(), {"pairing_scope": "user"}, "bob",
    ) == {}
    # Agent-scope session (no creds sub — task/phone) on user hardware →
    # service tokens never land.
    assert _collect_session_files(
        _remote_cfg(scope="agent", username="", user_sub=""),
        {"pairing_scope": "user"}, "alice",
    ) == {}


def test_shared_only_chat_resolves_as_service_scope(monkeypatch, tmp_path):
    # A human chat with a Shared-only agent mounts the AGENT scope
    # (SecurityContext.session_scope == "agent"), and credentials follow the
    # MOUNT: the config builders resolve its per-user MCPs via the agent's
    # bound SERVICE account (config_builder passes task_scope=vis.mount_scope)
    # with the token at /knowledge/.credentials — the collector must key on
    # the same scope so the delivered file and the MCP's credentials_dir env
    # point at the same path (the system-admin google-workspace failure was
    # this pair disagreeing).
    monkeypatch.setattr(
        "core.sandbox.session_config_dir.collect_authorized_ssh_keys",
        lambda agent: {},
    )
    seen = {}

    def _collect(agent, *, user_sub, session_scope):
        seen.update(user_sub=user_sub, session_scope=session_scope)
        return {"/knowledge/.credentials/google-tokens/svc@gmail.com.json": b"TOKEN"}

    monkeypatch.setattr(
        "services.oauth.credential_resolver.collect_oauth_token_files",
        _collect,
    )
    from core.remote.remote_execution import _collect_session_files
    files = _collect_session_files(
        _remote_cfg(scope="agent", username="alice", user_sub="sub-1"),
        {"pairing_scope": "admin"}, None,
    )
    assert set(files) == {"/knowledge/.credentials/google-tokens/svc@gmail.com.json"}
    # Agent scope → the service binding decides the account; the engaging
    # user's sub rides along but the resolver's service branch ignores it.
    assert seen == {"user_sub": "sub-1", "session_scope": "agent"}


# ── admin-paired gating: registry exclusion + provider ─────────────────────

def test_context_only_mcp_allowed_on_admin_paired_remote(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    from tests.mcp.test_ssh_hosts import _context_only_manifest
    from tests.mcp.test_mcp_broker_activation import _stub_assembly
    _stub_assembly(
        monkeypatch, [_context_only_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )

    _p, _e, excluded, _b, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m"),
    )
    assert "ssh-hosts" not in excluded

    _p, _e, excluded, _b, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m"),
    )
    assert "ssh-hosts" in excluded
    assert "admin-paired" in excluded["ssh-hosts"]


def test_provider_renders_on_admin_paired_remote():
    from unittest.mock import patch
    from services.mcp.dynamic_context import _ssh_hosts_context

    rows = [{"id": 1, "field_values": {"name": "x", "host": "10.0.0.5",
                                       "username": "u", "key_name": "k"},
             "agents": ["agent"], "assigned_to_all": False}]
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        assert _ssh_hosts_context(
            "agent", placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m"),
            user_role="manager",
        ) is not None
        assert _ssh_hosts_context(
            "agent", placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m"),
            user_role="manager",
        ) is None
