"""Regression tests for `api/hooks._resolve_hook_path` path-form contract.

The MCP framework v2 cleanup changed file-tools to post agents-relative paths
to `/v1/hooks/document-preview`, `/v1/hooks/file`, and `/v1/hooks/file-written`
instead of host-absolute. The resolver must accept all three documented forms:

  1. Real host-absolute (legacy)
  2. Agents-relative (canonical for Docker MCPs post-v2)
  3. Sandbox-virtual (canonical for stdio MCPs with OTO_* env)
"""


import pytest

import config
from api.hooks import hooks


@pytest.fixture
def tmp_agents_dir(tmp_path, monkeypatch):
    """Redirect AGENTS_DIR to a temp tree with a fake agent + workspace file."""
    agents = tmp_path / "agents"
    workspace = agents / "personal-assistant" / "users" / "alice" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.docx").write_text("dummy")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    return agents


def test_form1_real_host_path_returned(tmp_agents_dir):
    """Form 1: real host path should be returned as-is."""
    real = tmp_agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    resolved = hooks._resolve_hook_path("session-x", str(real))
    assert resolved == real


def test_form2_agents_relative_resolves_under_agents_dir(tmp_agents_dir):
    """Form 2: agents-relative path (post-v2 canonical for Docker MCPs)."""
    resolved = hooks._resolve_hook_path(
        "session-x",
        "personal-assistant/users/alice/workspace/report.docx",
    )
    expected = tmp_agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    assert resolved == expected
    assert resolved.is_file()


def test_form2_agents_relative_missing_file_falls_through(tmp_agents_dir):
    """Form 2 with a missing file should NOT match — caller will 404."""
    resolved = hooks._resolve_hook_path(
        "session-x",
        "personal-assistant/users/alice/workspace/nonexistent.docx",
    )
    # Falls through; not a real file → caller's .is_file() check raises 404.
    assert not resolved.is_file()


def test_form2_does_not_double_agent_dir(tmp_agents_dir):
    """Form 2 must NOT prepend the agent dir twice (was the original bug).

    Before the fix, agents-relative paths fell through to `_sandbox_to_host`'s
    fallback which returned `<agent_dir>/<input>`, producing
    `<AGENTS_DIR>/personal-assistant/personal-assistant/users/...` — file
    not found, /v1/hooks/document-preview returned 400.
    """
    # Simulate a session ctx for the agent so _sandbox_to_host would be invoked
    # if form 2 wasn't handled first.
    resolved = hooks._resolve_hook_path(
        "session-x",
        "personal-assistant/users/alice/workspace/report.docx",
    )
    expected = tmp_agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    assert resolved == expected
    # The bug would have produced this path:
    bug_path = tmp_agents_dir / "personal-assistant" / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    assert resolved != bug_path


def test_to_agents_relative_strips_prefix(tmp_agents_dir):
    """`_to_agents_relative` strips the AGENTS_DIR prefix to produce form 2."""
    host = str(tmp_agents_dir) + "/personal-assistant/users/alice/workspace/report.docx"
    rel = hooks._to_agents_relative(host)
    assert rel == "/personal-assistant/users/alice/workspace/report.docx"


# ─────────── _classify_and_pull: out-of-tree host paths on LOCAL sessions ──────
# The stdio interceptor's resolve-tool-arg-paths gate is wired for REMOTE
# sessions only, so on a local (bwrap) session the raw LLM-supplied path reaches
# _classify_and_pull ungated. _resolve_hook_path form-1 (`Path(raw).is_file()`)
# will happily return an existing proxy-host file OUTSIDE the agent tree, so the
# gate MUST reject it — otherwise display/preview/media hooks serve arbitrary
# host files (config.env, other users' tokens) straight to the dashboard.


@pytest.fixture
def local_session(tmp_agents_dir, monkeypatch):
    """A local (non-remote) session whose ctx.agent owns the fixture tree."""
    from auth.path_policy import SecurityContext
    from core.session import session_state

    monkeypatch.setattr(
        "auth.path_policy._AGENTS_DIR", tmp_agents_dir.resolve()
    )
    ctx = SecurityContext(
        role="manager", username="alice", agent="personal-assistant",
        is_admin_agent=False,
    )
    sid = "sess-local-1"
    session_state.set_session_security(sid, ctx)
    yield sid
    session_state._session_security.pop(sid, None)


@pytest.mark.asyncio
async def test_classify_rejects_out_of_tree_host_path(local_session, tmp_path):
    """An existing proxy-host file outside AGENTS_DIR must be denied, even
    though _resolve_hook_path form-1 returns it (the file really exists)."""
    secret = tmp_path / "config.env"          # sibling of AGENTS_DIR, out of tree
    secret.write_text("JWT_SECRET=super-secret")
    assert secret.is_file()                   # form-1 would return it verbatim

    host, resolution = await hooks._classify_and_pull(local_session, str(secret))
    assert host is None, f"out-of-tree path leaked: {host}"


@pytest.mark.asyncio
async def test_classify_rejects_absolute_system_path(local_session):
    """A classic traversal target (/etc/passwd) must be denied on a local
    session regardless of whether it exists on the host."""
    host, _resolution = await hooks._classify_and_pull(local_session, "/etc/passwd")
    assert host is None


@pytest.mark.asyncio
async def test_classify_allows_in_tree_own_file(local_session, tmp_agents_dir):
    """Positive control: the session's OWN in-tree file still resolves."""
    own = (
        tmp_agents_dir / "personal-assistant" / "users" / "alice"
        / "workspace" / "report.docx"
    )
    host, _resolution = await hooks._classify_and_pull(local_session, str(own))
    assert host == own


@pytest.mark.asyncio
async def test_classify_rejects_cross_user_in_tree_file(local_session, tmp_agents_dir):
    """A DIFFERENT user's in-tree file is denied by the cross-user RBAC branch
    (alice may not read bob's workspace)."""
    bob_ws = (
        tmp_agents_dir / "personal-assistant" / "users" / "bob" / "workspace"
    )
    bob_ws.mkdir(parents=True, exist_ok=True)
    (bob_ws / "secret.docx").write_text("bob's")
    host, _resolution = await hooks._classify_and_pull(
        local_session, str(bob_ws / "secret.docx")
    )
    assert host is None
