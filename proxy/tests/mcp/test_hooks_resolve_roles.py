"""The hook path gate for every role: a link in the shared workspace to
another person's file is refused and a shared file is served, for a
contributor, an editor, a run with no person and a Shared-only viewer; an
admin on the admin agent stays in its own agent; an editor's resolve-path
answer is judged where it lands. Stand-in folders inside each test's own
temporary folder."""
import pytest

import config
from api.hooks import paths


@pytest.fixture
def tree(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agent = agents / "pa"
    shared = agent / "workspace"
    alice = agent / "users" / "alice" / "workspace"
    bob = agent / "users" / "bob" / "workspace"
    other = agents / "other-agent" / "workspace"
    for d in (shared, alice, bob, other):
        d.mkdir(parents=True)
    (bob / "notes.png").write_bytes(b"bob's")
    (other / "notes.png").write_bytes(b"theirs")
    (shared / "team.png").write_bytes(b"team")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents.resolve())
    return agents, shared, alice, bob, other


def _session(monkeypatch, sid, **kw):
    from auth.path_policy import SecurityContext
    from core.session import session_state
    base = dict(role="viewer", username="alice", agent="pa", is_admin_agent=False,
                session_scope="user")
    base.update(kw)
    monkeypatch.setitem(session_state._session_security, sid, SecurityContext(**base))
    return sid


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    dict(role="contributor"), dict(role="editor"),
    dict(role="editor", username="", session_scope="agent"),
    dict(role="viewer", username="dave", session_scope="agent"),
], ids=["contributor", "editor", "no-person-run", "shared-only-viewer"])
async def test_a_shared_workspace_link_to_another_persons_file_is_refused(tree, monkeypatch, kw):
    _agents, shared, _alice, bob, _other = tree
    (shared / "pic.png").symlink_to(bob / "notes.png")
    sid = _session(monkeypatch, "sess-probe", **kw)
    host, _res = await paths._classify_and_pull(sid, "pa/workspace/pic.png")
    assert host is None
    host, _res = await paths._classify_and_pull(sid, "pa/workspace/team.png")
    assert host is not None and host.read_bytes() == b"team"


@pytest.mark.asyncio
async def test_an_admin_on_the_admin_agent_stays_in_its_own_agent(tree, monkeypatch):
    agents, _shared, alice, _bob, other = tree
    (alice / "far.png").symlink_to(other / "notes.png")
    cache = agents / ".remote-host-cache" / "sess-else"
    cache.mkdir(parents=True)
    (cache / "x.png").write_bytes(b"else")
    (alice / "cache.png").symlink_to(cache / "x.png")
    sid = _session(monkeypatch, "sess-admin", role="admin", is_admin_agent=True)
    for raw in ("pa/users/alice/workspace/far.png", "pa/users/alice/workspace/cache.png"):
        host, _res = await paths._classify_and_pull(sid, raw)
        assert host is None, raw


@pytest.mark.asyncio
async def test_an_editor_route_answer_is_judged_where_it_lands(tree, monkeypatch):
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException
    _agents, shared, _alice, bob, _other = tree
    (shared / "pic.png").symlink_to(bob / "notes.png")
    sid = _session(monkeypatch, "sess-ed", role="editor")
    with patch("api.hooks.paths.verify_session_match_async", new_callable=AsyncMock):
        for writing in (False, True):
            with pytest.raises(HTTPException) as err:
                await paths.resolve_path(paths.ResolvePathRequest(
                    session_id=sid, path="/workspace/pic.png", writing=writing),
                    authorization="Bearer x")
            assert err.value.status_code == 403
