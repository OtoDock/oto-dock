"""Regression tests for `api/hooks/paths.py::_resolve_hook_path` path-form contract.

The MCP framework v2 cleanup changed file-tools to post agents-relative paths
to `/v1/hooks/document-preview`, `/v1/hooks/file`, and `/v1/hooks/file-written`
instead of host-absolute. The resolver must accept all three documented forms:

  1. Real host-absolute (legacy)
  2. Agents-relative (canonical for Docker MCPs post-v2)
  3. Sandbox-virtual (canonical for stdio MCPs with OTO_* env)
"""


import pytest

import config
from api.hooks import paths


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
    resolved = paths._resolve_hook_path("session-x", str(real))
    assert resolved == real


def test_form2_agents_relative_resolves_under_agents_dir(tmp_agents_dir):
    """Form 2: agents-relative path (post-v2 canonical for Docker MCPs)."""
    resolved = paths._resolve_hook_path(
        "session-x",
        "personal-assistant/users/alice/workspace/report.docx",
    )
    expected = tmp_agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    assert resolved == expected
    assert resolved.is_file()


def test_form2_agents_relative_missing_file_falls_through(tmp_agents_dir):
    """Form 2 with a missing file should NOT match — caller will 404."""
    resolved = paths._resolve_hook_path(
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
    resolved = paths._resolve_hook_path(
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
    rel = paths._to_agents_relative(host)
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

    host, resolution = await paths._classify_and_pull(local_session, str(secret))
    assert host is None, f"out-of-tree path leaked: {host}"


@pytest.mark.asyncio
async def test_classify_rejects_absolute_system_path(local_session):
    """A classic traversal target (/etc/passwd) must be denied on a local
    session regardless of whether it exists on the host."""
    host, _resolution = await paths._classify_and_pull(local_session, "/etc/passwd")
    assert host is None


@pytest.mark.asyncio
async def test_classify_allows_in_tree_own_file(local_session, tmp_agents_dir):
    """Positive control: the session's OWN in-tree file still resolves."""
    own = (
        tmp_agents_dir / "personal-assistant" / "users" / "alice"
        / "workspace" / "report.docx"
    )
    host, _resolution = await paths._classify_and_pull(local_session, str(own))
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
    host, _resolution = await paths._classify_and_pull(
        local_session, str(bob_ws / "secret.docx")
    )
    assert host is None


def test_sandbox_to_host_collapses_slash_runs_before_the_join(tmp_agents_dir):
    """``/workspace//etc/passwd`` lands under the workspace, never at
    ``/etc/passwd``: ``pathlib`` restarts a join at an absolute component and
    the policy verdict before the map had judged the collapsed form (core-seams
    phase 10, audit A1)."""
    from auth.path_policy import SecurityContext
    agent_dir = tmp_agents_dir / "personal-assistant"
    ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant",
                          is_admin_agent=False, session_scope="user")
    assert paths._sandbox_to_host("/workspace//etc/passwd", ctx, agent_dir) == \
        str(agent_dir / "workspace" / "etc" / "passwd")
    assert paths._sandbox_to_host("/config//x", ctx, agent_dir) == str(agent_dir / "config" / "x")
    assert paths._sandbox_to_host("/users/alice//workspace/a", ctx, agent_dir) == \
        str(agent_dir / "users" / "alice" / "workspace" / "a")
    # the plain forms answer as they always did
    assert paths._sandbox_to_host("/workspace/a", ctx, agent_dir) == str(agent_dir / "workspace" / "a")


def test_workspace_redirect_follows_the_workspace_tier(tmp_agents_dir):
    """Below the workspace tier a Docker MCP's ``/workspace`` means the
    person's own workspace (the viewer redirect); a contributor's means the
    shared one, exactly like an editor's."""
    from auth.path_policy import SecurityContext
    agent_dir = tmp_agents_dir / "personal-assistant"
    def ctx(role):
        return SecurityContext(role=role, username="alice", agent="personal-assistant",
                               is_admin_agent=False, session_scope="user")
    assert paths._sandbox_to_host("/workspace/a", ctx("viewer"), agent_dir) == \
        str(agent_dir / "users" / "alice" / "workspace" / "a")
    assert paths._sandbox_to_host("/workspace/a", ctx("contributor"), agent_dir) == \
        str(agent_dir / "workspace" / "a")


# ─────────── The gate judges where a path lands and answers that place ─────────
# Stand-in folders inside the test's own tree: two people of one agent, a link
# in one person's folder. The gate must judge the place the link lands and hand
# back that place, never the path as written.


def _people(tmp_agents_dir):
    agent = tmp_agents_dir / "personal-assistant"
    alice = agent / "users" / "alice" / "workspace"
    bob = agent / "users" / "bob" / "workspace"
    bob.mkdir(parents=True, exist_ok=True)
    (bob / "notes.png").write_bytes(b"bob's")
    return agent, alice, bob


@pytest.mark.asyncio
async def test_classify_judges_where_a_link_lands(local_session, tmp_agents_dir):
    """A link in alice's own folder to bob's file is judged as bob's file."""
    _agent, alice, bob = _people(tmp_agents_dir)
    (alice / "pic.png").symlink_to(bob / "notes.png")
    for raw in (str(alice / "pic.png"), "personal-assistant/users/alice/workspace/pic.png",
                "/users/alice/workspace/pic.png"):
        host, _resolution = await paths._classify_and_pull(local_session, raw)
        assert host is None, raw


@pytest.mark.asyncio
async def test_classify_judges_a_linked_folder_where_it_lands(local_session, tmp_agents_dir):
    _agent, alice, bob = _people(tmp_agents_dir)
    (alice / "shared").symlink_to(bob)
    host, _resolution = await paths._classify_and_pull(local_session, str(alice / "shared" / "notes.png"))
    assert host is None


@pytest.mark.asyncio
async def test_classify_answers_the_place_an_in_scope_link_lands(local_session, tmp_agents_dir):
    """An honest link inside alice's own folder still works, and the answer is
    the place it lands, with no link left in it."""
    _agent, alice, _bob = _people(tmp_agents_dir)
    (alice / "drafts").mkdir()
    (alice / "drafts" / "v3.png").write_bytes(b"alice's")
    (alice / "latest.png").symlink_to(alice / "drafts" / "v3.png")
    host, _resolution = await paths._classify_and_pull(local_session, str(alice / "latest.png"))
    assert host == tmp_agents_dir / "personal-assistant/users/alice/workspace/drafts/v3.png"
    assert not any(p.is_symlink() for p in [host, *host.parents])


@pytest.mark.asyncio
async def test_classify_reads_the_host_cache_as_text(local_session, tmp_agents_dir):
    """The session's own cache folder is its own by NAME: a link at it to
    another person's or another agent's folder makes nothing there "own
    cache"."""
    _agent, _alice, bob = _people(tmp_agents_dir)
    other = tmp_agents_dir / "other-agent" / "workspace"
    other.mkdir(parents=True)
    (other / "notes.png").write_bytes(b"theirs")
    cache = tmp_agents_dir / ".remote-host-cache"
    cache.mkdir()
    for target in (bob, other):
        link = cache / local_session
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        host, _resolution = await paths._classify_and_pull(
            local_session, str(cache / local_session / "notes.png"))
        assert host is None, target


@pytest.mark.asyncio
async def test_classify_admits_the_sessions_own_cache(local_session, tmp_agents_dir):
    cache = tmp_agents_dir / ".remote-host-cache" / local_session
    cache.mkdir(parents=True)
    (cache / "desk.png").write_bytes(b"pulled")
    host, _resolution = await paths._classify_and_pull(local_session, str(cache / "desk.png"))
    assert host == cache / "desk.png"
    other = tmp_agents_dir / ".remote-host-cache" / "sess-other"
    other.mkdir()
    (other / "desk.png").write_bytes(b"theirs")
    host, _resolution = await paths._classify_and_pull(local_session, str(other / "desk.png"))
    assert host is None


def _member_session(tmp_agents_dir, monkeypatch, role="viewer"):
    from auth.path_policy import SecurityContext
    from core.session import session_state
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", tmp_agents_dir.resolve())
    ctx = SecurityContext(role=role, username="alice", agent="personal-assistant",
                          is_admin_agent=False, session_scope="user")
    sid = f"sess-route-{role}"
    monkeypatch.setitem(session_state._session_security, sid, ctx)
    return sid


async def _route(sid, raw, *, writing=False):
    from unittest.mock import AsyncMock, patch
    with patch("api.hooks.paths.verify_session_match_async", new_callable=AsyncMock):
        return await paths.resolve_path(
            paths.ResolvePathRequest(session_id=sid, path=raw, writing=writing),
            authorization="Bearer x",
        )


@pytest.mark.asyncio
async def test_resolve_path_judges_a_members_workspace_where_it_lands(tmp_agents_dir, monkeypatch):
    """Below the workspace tier ``/workspace`` is the person's own folder: a
    link there to another person's file is refused, read or write."""
    from fastapi import HTTPException
    _agent, alice, bob = _people(tmp_agents_dir)
    alice.mkdir(parents=True, exist_ok=True)
    (alice / "pic.png").symlink_to(bob / "notes.png")
    sid = _member_session(tmp_agents_dir, monkeypatch)
    for writing in (False, True):
        with pytest.raises(HTTPException) as err:
            await _route(sid, "/workspace/pic.png", writing=writing)
        assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_path_answers_the_place_an_honest_link_lands(tmp_agents_dir, monkeypatch):
    _agent, alice, _bob = _people(tmp_agents_dir)
    (alice / "drafts").mkdir()
    (alice / "drafts" / "v3.docx").write_bytes(b"alice's")
    (alice / "latest.docx").symlink_to("drafts/v3.docx")
    sid = _member_session(tmp_agents_dir, monkeypatch)
    answer = await _route(sid, "/workspace/latest.docx")
    assert answer["agents_relative"] == "/personal-assistant/users/alice/workspace/drafts/v3.docx"
    answer = await _route(sid, "/users/alice/workspace/new/out.docx", writing=True)
    assert answer["agents_relative"] == "/personal-assistant/users/alice/workspace/new/out.docx"


@pytest.mark.asyncio
async def test_classify_judges_a_pulled_answer_where_it_lands(local_session, tmp_agents_dir, monkeypatch):
    """A remote session's pull is judged once more after it answers: an
    answer that leads into another person's folder by then is refused."""
    from core.remote import remote_file_flow
    _agent, alice, bob = _people(tmp_agents_dir)
    (alice / "pic.png").write_bytes(b"alice's")
    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda sid: True)

    async def pulled(session_id, rel, held=None):
        # The name turns into a link while the pull waited.
        (alice / "pic.png").unlink()
        (alice / "pic.png").symlink_to(bob / "notes.png")
        return alice / "pic.png"
    monkeypatch.setattr(remote_file_flow, "pull_through", pulled)
    host, resolution = await paths._classify_and_pull(local_session, "/users/alice/workspace/pic.png")
    assert host is None
    assert resolution is not None and not resolution.allowed


@pytest.mark.asyncio
async def test_resolve_path_judges_a_new_file_of_a_remote_session_where_it_lands(local_session, tmp_agents_dir,
                                                                                  monkeypatch):
    """A remote session's new output file is answered at the platform's
    creation path only when that place, judged after the pull, is the
    session's to write: a folder turned into a link while the pull waited
    is refused, and nothing is created where it leads."""
    from fastapi import HTTPException

    from core.remote import remote_file_flow
    _agent, alice, bob = _people(tmp_agents_dir)
    (alice / "sub").mkdir()
    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda sid: True)

    async def not_on_the_machine(session_id, rel, held=None):
        (alice / "sub").rmdir()
        (alice / "sub").symlink_to(bob)
        return None
    monkeypatch.setattr(remote_file_flow, "pull_through", not_on_the_machine)
    with pytest.raises(HTTPException) as err:
        await _route(local_session, "/users/alice/workspace/sub/new.docx", writing=True)
    assert err.value.status_code == 403
    assert sorted(p.name for p in bob.iterdir()) == ["notes.png"]


@pytest.mark.asyncio
async def test_resolve_path_judges_a_remote_read_where_it_lands(local_session, tmp_agents_dir, monkeypatch):
    """A remote session's read is answered only when the pull's answer,
    judged after the pull, is the session's to use: a name turned into a
    link while the pull waited is refused, read or write, and an honest
    answer is handed back."""
    from fastapi import HTTPException

    from core.remote import remote_file_flow
    _agent, alice, bob = _people(tmp_agents_dir)
    (alice / "own.png").write_bytes(b"alice's")
    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda sid: True)

    async def pulled(session_id, rel, held=None):
        name = alice / "pic.png"
        if name.is_symlink() or name.exists():
            name.unlink()
        name.symlink_to(bob / "notes.png")
        return name
    monkeypatch.setattr(remote_file_flow, "pull_through", pulled)
    for writing in (False, True):
        with pytest.raises(HTTPException) as err:
            await _route(local_session, "/users/alice/workspace/pic.png", writing=writing)
        assert err.value.status_code == 403

    async def pulled_own(session_id, rel, held=None):
        return alice / "own.png"
    monkeypatch.setattr(remote_file_flow, "pull_through", pulled_own)
    answer = await _route(local_session, "/users/alice/workspace/own.png")
    assert answer["agents_relative"] == "/personal-assistant/users/alice/workspace/own.png"


@pytest.mark.asyncio
async def test_an_agent_whose_name_starts_like_the_sessions_own_is_another_agent(tmp_agents_dir, monkeypatch):
    """The session's own agent is its folder, never a folder whose name only
    starts the same: a link there from the session's own agent is refused,
    even for an admin on the admin agent."""
    from auth.path_policy import SecurityContext
    from core.session import session_state
    _agent, alice, _bob = _people(tmp_agents_dir)
    twin = tmp_agents_dir / "personal-assistant-two" / "workspace"
    twin.mkdir(parents=True)
    (twin / "notes.png").write_bytes(b"theirs")
    (alice / "far.png").symlink_to(twin / "notes.png")
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", tmp_agents_dir.resolve())
    monkeypatch.setitem(session_state._session_security, "sess-admin", SecurityContext(
        role="admin", username="alice", agent="personal-assistant", is_admin_agent=True))
    host, _resolution = await paths._classify_and_pull("sess-admin", str(alice / "far.png"))
    assert host is None


def test_a_session_id_with_a_separator_names_no_cache():
    assert paths.in_own_host_cache("a/b", ".remote-host-cache/a/b/f.docx") is False
    assert paths.in_own_host_cache("a", ".remote-host-cache/a/b/f.docx") is True


@pytest.mark.asyncio
async def test_resolve_path_a_read_only_session_writes_nothing(tmp_agents_dir, monkeypatch):
    """A check's judge (a read-only session) reads through the route and
    writes nothing there, as through the files routes."""
    from fastapi import HTTPException

    from auth.path_policy import SecurityContext
    from core.session import session_state
    shared = tmp_agents_dir / "personal-assistant" / "workspace"
    shared.mkdir()
    (shared / "brief.docx").write_bytes(b"x")
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", tmp_agents_dir.resolve())
    monkeypatch.setitem(session_state._session_security, "sess-judge", SecurityContext(
        role="manager", username="", agent="personal-assistant", is_admin_agent=False,
        session_scope="agent", read_only=True))
    answer = await _route("sess-judge", "/workspace/brief.docx")
    assert answer["agents_relative"] == "/personal-assistant/workspace/brief.docx"
    for raw in ("/workspace/brief.docx", "/workspace/new.docx"):
        with pytest.raises(HTTPException) as err:
            await _route("sess-judge", raw, writing=True)
        assert err.value.status_code == 403
        assert err.value.detail == "This session writes no files"


@pytest.mark.parametrize("raw", ["/workspace/a.png/.", "/workspace/a.png/..", ".", "..", "C:\\Users\\alice\\..", ""])
def test_the_asked_name_is_never_a_dot_name(raw):
    """A path whose last part is a dot name, or no part at all, gives no name:
    the callers then show the file's own."""
    assert paths._asked_name(raw) == ""


# ─────────── The four routes refuse a path that climbs out of the person's folder ─────────
# The path names the person's own folder and then climbs out of it, written in
# each form a tool sends. Every route must refuse it and hand the other place to
# nothing that serves a file.

_HOOK_ROUTES = {
    "/v1/images/temp": ("api.media.images", "abs_path", {}),
    "/v1/hooks/file": ("api.hooks.artifacts", "path", {}),
    "/v1/hooks/media": ("api.hooks.artifacts", "source", {"media_kind": "video"}),
    "/v1/hooks/document-preview": ("api.hooks.preview", "file_path", {}),
}


def _climb(form: str, agents, tail: str) -> str:
    """``tail`` reached from alice's own folder, in one of the three forms."""
    return {
        "agents-relative": f"personal-assistant/users/alice/{tail}",
        "host": str(agents / "personal-assistant" / "users" / "alice") + f"/{tail}",
        "sandbox": f"/users/alice/{tail}",
    }[form]


def _post_hook(route: str, sid: str, raw: str, monkeypatch):
    """POST ``raw`` to ``route`` for session ``sid``; returns the response and
    every answer the path gate handed the route."""
    import importlib
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from api.media import images
    from app import app
    module, field, extra = _HOOK_ROUTES[route]
    monkeypatch.setattr(importlib.import_module(module), "verify_session_match_async", AsyncMock())
    monkeypatch.setattr(config, "COLLABORA_URL", "https://office.example")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://platform.example")
    real = paths._classify_and_pull
    handed: list = []

    async def _spy(session_id, path, **kw):
        answer, resolution = await real(session_id, path, **kw)
        handed.append(answer)
        return answer, resolution

    monkeypatch.setattr(paths, "_classify_and_pull", _spy)
    monkeypatch.setattr(images, "_classify_and_pull", _spy)
    images._temp_image_tokens.clear()
    resp = TestClient(app).post(route, json={"session_id": sid, field: raw, **extra},
                                headers={"Authorization": "Bearer test"})
    return resp, handed


@pytest.mark.parametrize("form", ["agents-relative", "host", "sandbox"])
@pytest.mark.parametrize("route", sorted(_HOOK_ROUTES))
def test_each_hook_route_refuses_a_climb_into_another_persons_folder(
        route, form, local_session, tmp_agents_dir, monkeypatch):
    from api.media import images
    _people(tmp_agents_dir)
    raw = _climb(form, tmp_agents_dir, "../bob/workspace/notes.png")
    resp, handed = _post_hook(route, local_session, raw, monkeypatch)
    assert 400 <= resp.status_code < 500, resp.text
    assert handed == [None]
    assert b"bob's" not in resp.content
    assert not images._temp_image_tokens


@pytest.mark.parametrize("form", ["agents-relative", "host", "sandbox"])
@pytest.mark.parametrize("route", sorted(_HOOK_ROUTES))
def test_each_hook_route_refuses_a_viewers_climb_into_the_settings_folder(
        route, form, tmp_agents_dir, monkeypatch):
    from api.media import images
    settings = tmp_agents_dir / "personal-assistant" / "config"
    settings.mkdir(parents=True)
    (settings / "agent.png").write_bytes(b"settings")
    sid = _member_session(tmp_agents_dir, monkeypatch, role="viewer")
    raw = _climb(form, tmp_agents_dir, "../../config/agent.png")
    resp, handed = _post_hook(route, sid, raw, monkeypatch)
    assert 400 <= resp.status_code < 500, resp.text
    assert handed == [None]
    assert b"settings" not in resp.content
    assert not images._temp_image_tokens


def _token_folder() -> str:
    """One folder name the account token refusal guards, or "" when this
    checkout ships no manifest naming one."""
    from services.mcp import mcp_registry
    return min(mcp_registry.get_protected_credentials_subpaths(), default="")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["engine-config", "engine-sign-in", "account-tokens"])
async def test_the_engine_and_account_refusals_judge_where_a_climb_lands(local_session, tmp_agents_dir, kind):
    """The refusals for the engine's own files at a scope root and for an
    account's token folder are judged on the place the path lands, so a
    climb that reaches them from the side is refused as they are."""
    rel = {"engine-config": ".claude/settings.json", "engine-sign-in": ".codex/auth.json",
           "account-tokens": f"{_token_folder()}/token.json"}[kind]
    if rel.startswith("/"):
        pytest.skip("no manifest in this checkout names a token folder")
    target = tmp_agents_dir / "personal-assistant" / "users" / "alice" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret")
    host, _resolution = await paths._classify_and_pull(
        local_session, f"personal-assistant/users/alice/workspace/../{rel}")
    assert host is None
