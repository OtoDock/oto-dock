"""Credential-dir writeback — per-session refreshed tokens → central store.

Locks/refresh-worker interplay is covered elsewhere; these tests pin the
per-session SOURCE layout: the writeback must read from exactly the dirs
the resolver copies into (``users/{u}/.credentials/{subpath}`` for
user-scope sessions, ``knowledge/.credentials/{subpath}`` for agent-scope).
This layout drifted once (pre-fix the writeback looked at the pre-role-v2
locations and silently no-opped) — keep these green.
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config  # noqa: E402
from core.credentials.credential_writeback import writeback_credential_dirs  # noqa: E402


@pytest.fixture
def writeback_env(monkeypatch, tmp_path):
    """Stub everything around the copy: session ctx, registry, binding,
    central store dir. Returns (agents_dir, central_dir, set_ctx)."""
    agents_dir = tmp_path / "agents"
    central_dir = tmp_path / "central"
    central_dir.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)

    manifest = SimpleNamespace(
        name="google-workspace",
        credentials=SimpleNamespace(oauth={"provider_id": "google"}),
    )
    from services.mcp import mcp_registry
    monkeypatch.setattr(
        mcp_registry, "get_agent_mcps_all_placements", lambda a: [manifest],
    )
    monkeypatch.setattr(
        mcp_registry, "get_credentials_dirs",
        lambda n: [("WORKSPACE_MCP_CREDENTIALS_DIR", "google-tokens")],
    )

    from services.oauth import credential_resolver, oauth_account_store
    monkeypatch.setattr(
        credential_resolver, "pick_account",
        lambda mcp, agent, user_sub=None: SimpleNamespace(
            label="alice@gmail.com", owner_sub="sub-1",
        ),
    )
    monkeypatch.setattr(
        oauth_account_store, "get_token_dir",
        lambda username, *, provider_id: central_dir,
    )

    from storage import database
    monkeypatch.setattr(database, "get_username_by_sub", lambda sub: "alice")

    import storage.pg
    class _FakeConn:
        def execute(self, *_a, **_k):
            return SimpleNamespace(fetchone=lambda: {"sub": "sub-1"})
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(storage.pg, "get_conn", _FakeConn)

    def set_ctx(session_scope, username):
        ctx = SimpleNamespace(
            session_scope=session_scope, username=username, agent="agentx",
        )
        monkeypatch.setattr(
            "core.session.session_state.get_session_security",
            lambda sid: ctx,
        )
    return agents_dir, central_dir, set_ctx


def test_user_scope_writes_back_from_credentials_dir(writeback_env):
    agents_dir, central_dir, set_ctx = writeback_env
    set_ctx("user", "alice")
    src = (
        agents_dir / "agentx" / "users" / "alice" / ".credentials"
        / "google-tokens"
    )
    src.mkdir(parents=True)
    (src / "alice@gmail.com.json").write_text('{"access_token":"refreshed"}')

    asyncio.run(writeback_credential_dirs("sess-1"))

    assert (
        central_dir / "alice@gmail.com.json"
    ).read_text() == '{"access_token":"refreshed"}'


def test_agent_scope_writes_back_from_knowledge_credentials(writeback_env):
    agents_dir, central_dir, set_ctx = writeback_env
    set_ctx("agent", "")
    src = (
        agents_dir / "agentx" / "knowledge" / ".credentials" / "google-tokens"
    )
    src.mkdir(parents=True)
    (src / "alice@gmail.com.json").write_text('{"access_token":"svc"}')

    asyncio.run(writeback_credential_dirs("sess-2"))

    assert (
        central_dir / "alice@gmail.com.json"
    ).read_text() == '{"access_token":"svc"}'
