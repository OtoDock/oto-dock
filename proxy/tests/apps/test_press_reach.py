"""As whom an app button's press reads and writes: a value the presser chose never
reaches past the presser. The reach each presser gets
(``headless_exec.press_reach``), the session a press below the identity is
built in, and the judges that read it: the path policy, the files API's session
grant and the routes a confined press's session may call."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
from auth import roles
from auth.path_policy import (
    PRESS_MEMBER,
    PRESS_NONE,
    SecurityContext,
    check_host_path_access,
)
from services.apps import headless_exec as hx

AGENT = "reach-agent"


def _user(sub="u-1", *, role="viewer", member=True, admin=False, key=False, external=False):
    return SimpleNamespace(
        sub=sub, is_admin=admin, is_api_key=key, is_external=external,
        can_access_agent=lambda agent: member or admin,
        acting_role=lambda agent: roles.ADMIN if admin else role,
    )


SHARED = {"id": "app-s", "agent": AGENT, "username": "", "owner_sub": None}
PERSONAL = {"id": "app-p", "agent": AGENT, "username": "olga", "owner_sub": "u-owner"}


@pytest.fixture
def plain_agent(monkeypatch):
    monkeypatch.setattr(hx, "_admin_only", lambda agent: False)


MEMBER_VIEWER = (PRESS_MEMBER, roles.VIEWER)
NONE = (PRESS_NONE, roles.VIEWER)


@pytest.mark.parametrize(("row", "user", "chose", "actor", "handler", "want"), [
    # A shared app: a press that chooses nothing is the approver's delegation.
    (SHARED, _user(role="viewer"), False, "", "", hx.FULL),
    (SHARED, _user(member=False), False, "", "", hx.FULL),
    (SHARED, None, False, "share:s1", "", hx.FULL),
    # A choice: the editor tier and an admin are full, a viewer and a
    # contributor run at their own role, a non-member and a link at none.
    (SHARED, _user(role="editor"), True, "", "", hx.FULL),
    (SHARED, _user(role="manager"), True, "", "", hx.FULL),
    (SHARED, _user(admin=True, member=False), True, "", "", hx.FULL),
    (SHARED, _user(role="viewer"), True, "", "", MEMBER_VIEWER),
    (SHARED, _user(role="contributor"), True, "", "", (PRESS_MEMBER, roles.CONTRIBUTOR)),
    (SHARED, _user(member=False), True, "", "", NONE),
    (SHARED, None, True, "share:s1", "", NONE),
    (SHARED, _user(external=True), True, "", "", NONE),
    (SHARED, _user(role="manager", key=True), True, "", "", MEMBER_VIEWER),
    # The app's own handler or step chose the values: the approval is the authority.
    (SHARED, None, True, "delivery:d1", "digest", hx.FULL),
    # A personal app: its owner is full; anyone else never reaches the owner's folder.
    (PERSONAL, _user("u-owner", role="viewer"), True, "", "", hx.FULL),
    (PERSONAL, _user(role="editor"), False, "", "", (PRESS_MEMBER, roles.EDITOR)),
    (PERSONAL, _user(admin=True), False, "", "", (PRESS_MEMBER, roles.MANAGER)),
    (PERSONAL, _user(member=False), False, "", "", NONE),
    (PERSONAL, None, False, "share:s2", "", NONE),
    (PERSONAL, _user("u-owner", key=True), True, "", "", MEMBER_VIEWER),
])
def test_the_reach_of_each_presser(plain_agent, row, user, chose, actor, handler, want):
    assert hx.press_reach(row, user, actor=actor, handler=handler, chose=chose) == want


def test_an_admin_only_agent_is_full_for_admins_alone(monkeypatch):
    monkeypatch.setattr(hx, "_admin_only", lambda agent: True)
    assert hx.press_reach(SHARED, _user(admin=True), chose=True) == hx.FULL
    assert hx.press_reach(SHARED, _user(role="manager"), chose=True) == (PRESS_MEMBER, roles.MANAGER)
    assert hx.press_reach(SHARED, _user(member=False), chose=True) == NONE


def test_what_a_choice_is():
    action = {"args_schema": {"type": "object", "properties": {
        "text": {"type": "string", "maxLength": 9},
        "path": {"type": "string", "maxLength": 300, "file": "write"},
        "pick": {"type": "string", "enum": ["a", "b"]},
        "n": {"type": "integer"},
        "on": {"type": "boolean"},
        "ops": {"type": "array", "maxItems": 2, "items": {"oneOf": []}},
    }}}
    assert not hx.chooses(action, {})
    assert not hx.chooses(action, {"pick": "a", "n": 3, "on": True})
    assert hx.chooses(action, {"text": "x"})
    assert hx.chooses(action, {"path": "workspace/a"})
    assert hx.chooses(action, {"ops": []})


# ── The session a confined press is built in ─────────────────────────────


@pytest.fixture
def built(temp_db, tmp_path, monkeypatch):
    from services.mcp import mcp_registry
    from storage.agents import agent_store
    from tests._compute import serve
    if not agent_store.agent_exists(AGENT):
        agent_store.create_agent(AGENT, "Reach Agent")
    agents = tmp_path / "agents"
    (agents / AGENT / "workspace").mkdir(parents=True)
    (agents / AGENT / "knowledge").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", lambda *a, **kw: ({}, {}, {}, {}, set()))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **kw: [])
    serve(monkeypatch)

    def build(row, reach):
        return hx._build_session_parts(AGENT, row, frozenset({"test-mcp"}), "appx-r", reach=reach)
    return build


def test_a_full_press_keeps_the_apps_identity(built):
    _cfg, _b, _env, _builder, ctx = built(SHARED, hx.FULL)
    assert (ctx.role, ctx.username, ctx.press_reach) == (roles.MANAGER, "", "")


def test_a_member_press_runs_in_the_agent_scope_at_the_lower_role(built):
    _cfg, _b, _env, builder, ctx = built(SHARED, MEMBER_VIEWER)
    assert (ctx.role, ctx.username, ctx.session_scope, ctx.press_reach) == (
        roles.VIEWER, "", "agent", PRESS_MEMBER)
    assert ctx.config_visible is False and ctx.knowledge_rw is False
    binds = {b.sandbox: b.rw for b in builder.describe().binds}
    assert binds["/workspace"] is False and binds["/knowledge"] is False


def test_a_member_press_on_a_personal_app_never_names_the_owners_folder(built):
    _cfg, _b, _env, builder, ctx = built(PERSONAL, (PRESS_MEMBER, roles.MANAGER))
    assert ctx.username == "" and ctx.session_scope == "agent"
    assert not [b for b in builder.describe().binds if b.sandbox.startswith("/users")]


def test_a_member_press_never_runs_above_the_apps_own_identity(built, monkeypatch):
    """The lower of the two roles: a personal app whose owner is a
    contributor, pressed with a choice by a manager of the agent, builds at
    contributor."""
    from core.config import task_config_builder
    monkeypatch.setattr(task_config_builder, "resolve_task_identity", lambda agent, scope, sub: (
        task_config_builder.TaskIdentity(username="olga", role=roles.CONTRIBUTOR, scope="user",
                                         creds_user_sub="u-owner", person="u-owner")))
    _cfg, _b, _env, builder, ctx = built(PERSONAL, (PRESS_MEMBER, roles.MANAGER))
    assert (ctx.role, ctx.username, ctx.session_scope) == (roles.CONTRIBUTOR, "", "agent")
    binds = {b.sandbox: b.rw for b in builder.describe().binds}
    assert binds["/workspace"] is True and binds["/knowledge"] is False


def test_a_member_reach_at_admin_builds_at_manager(built, monkeypatch):
    """A second guard: whatever reach arrives, a confined build never
    carries the admin role (``press_reach`` already turns it into manager).
    The button's own identity is an admin here, an admin owner's personal
    app, so the lower of the two roles is admin and only the guard stops it."""
    from core.config import task_config_builder
    monkeypatch.setattr(task_config_builder, "resolve_task_identity", lambda agent, scope, sub: (
        task_config_builder.TaskIdentity(username="olga", role=roles.ADMIN, scope="user",
                                         creds_user_sub="u-owner", person="u-owner")))
    _cfg, _b, _env, _builder, ctx = built(PERSONAL, (PRESS_MEMBER, roles.ADMIN))
    assert (ctx.role, ctx.press_reach) == (roles.MANAGER, PRESS_MEMBER)


def test_a_confined_press_never_shows_the_config(built, monkeypatch):
    """A personal app whose owner is a manager sees the agent's config in
    the owner's own session. A confined press of another member never does."""
    from core.config import task_config_builder
    monkeypatch.setattr(task_config_builder, "resolve_task_identity", lambda agent, scope, sub: (
        task_config_builder.TaskIdentity(username="olga", role=roles.MANAGER, scope="user",
                                         creds_user_sub="u-owner", person="u-owner")))
    _cfg, _b, _env, own_builder, own = built(PERSONAL, hx.FULL)
    assert own.config_visible is True
    _cfg, _b, _env, builder, ctx = built(PERSONAL, (PRESS_MEMBER, roles.MANAGER))
    assert ctx.config_visible is False
    assert not [b for b in builder.describe().binds if b.sandbox == "/config"]


def test_a_press_with_no_folder_gets_an_empty_table_and_never_admin(built):
    _cfg, _b, _env, builder, ctx = built(SHARED, NONE)
    assert (ctx.role, ctx.press_reach) == (roles.VIEWER, PRESS_NONE)
    desc = builder.describe()
    assert desc.binds == () and desc.cwd == "/tmp"


def _masked(builder) -> set[str]:
    from core.sandbox import sandbox_render as _render
    return {b.sandbox for b in builder.describe().binds if b.root == _render.ROOT_EMPTY and not b.rw}


@pytest.mark.parametrize("reach", [hx.FULL, MEMBER_VIEWER, (PRESS_MEMBER, roles.MANAGER), NONE])
def test_every_press_is_an_app_press(built, reach):
    _cfg, _b, _env, _builder, ctx = built(SHARED, reach)
    assert ctx.app_press is True


@pytest.mark.parametrize("reach", [hx.FULL, (PRESS_MEMBER, roles.MANAGER), MEMBER_VIEWER])
def test_a_button_sandbox_masks_the_agents_engine_state_at_every_reach(built, reach):
    """A press that chooses nothing runs at the app's reach, a manager's
    table: the engine state stays masked there too, so content a member wrote
    cannot name it."""
    _cfg, _b, _env, builder, _ctx = built(SHARED, reach)
    assert {"/workspace/.claude", "/workspace/.codex"} <= _masked(builder)


def test_a_personal_apps_button_masks_its_owners_engine_state(tmp_path, monkeypatch):
    from core.sandbox.sandbox import SandboxBuilder, resolve_sandbox_config
    agents = tmp_path / "agents"
    for rel in ("workspace", "knowledge", "users/olga/.claude", "users/olga/.codex"):
        (agents / AGENT / rel).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    cfg = resolve_sandbox_config(roles.MANAGER, "olga", AGENT, False, tmp_path / "claude", app_press=True)
    masked = _masked(SandboxBuilder(cfg))
    assert {"/users/olga/.claude", "/users/olga/.codex",
            "/workspace/.claude", "/workspace/.codex"} <= masked
    # A person's own chat runs from that state: unchanged.
    plain = resolve_sandbox_config(roles.MANAGER, "olga", AGENT, False, tmp_path / "claude")
    assert "/users/olga/.claude" not in _masked(SandboxBuilder(plain))


# ── The judges ───────────────────────────────────────────────────────────


def _ctx(role, reach="", agent=AGENT):
    return SecurityContext(role=role, username="", agent=agent, is_admin_agent=False,
                           session_scope="agent", press_reach=reach)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    import auth.path_policy as pp
    agents = tmp_path / "agents"
    for rel in ("workspace/a.txt", "knowledge/k.md", "workspace/.claude/own/x.json",
                "workspace/.codex/sessions/s.jsonl", "config/agent.md"):
        f = agents / AGENT / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    monkeypatch.setattr(pp, "_AGENTS_DIR", agents.resolve())
    return agents / AGENT


def test_a_member_press_reads_what_its_role_reads_and_writes_nothing_more(tree):
    viewer = _ctx(roles.VIEWER, PRESS_MEMBER)
    assert check_host_path_access(tree / "workspace/a.txt", viewer).allowed
    assert check_host_path_access(tree / "knowledge/k.md", viewer).allowed
    assert not check_host_path_access(tree / "workspace/a.txt", viewer, writing=True).allowed
    contributor = _ctx(roles.CONTRIBUTOR, PRESS_MEMBER)
    assert check_host_path_access(tree / "workspace/a.txt", contributor, writing=True).allowed
    assert not check_host_path_access(tree / "config/agent.md", contributor).allowed


def test_a_press_with_no_folder_reaches_none_of_the_agents_files(tree):
    none = _ctx(roles.VIEWER, PRESS_NONE)
    for rel in ("workspace/a.txt", "knowledge/k.md"):
        assert not check_host_path_access(tree / rel, none).allowed
        assert not check_host_path_access(tree / rel, none, writing=True).allowed
    # No role carried in the context widens it, not even the admin shortcut.
    admin = SecurityContext(role=roles.ADMIN, username="", agent=AGENT, is_admin_agent=True,
                            session_scope="agent", press_reach=PRESS_NONE)
    assert not check_host_path_access(tree / "workspace/a.txt", admin).allowed


@pytest.mark.parametrize("rel", ["workspace/.claude/own/x.json", "workspace/.codex/sessions/s.jsonl"])
def test_the_agents_engine_state_is_shut_below_the_editor_tier(tree, rel):
    for role in (roles.VIEWER, roles.CONTRIBUTOR):
        ctx = _ctx(role)
        assert not check_host_path_access(tree / rel, ctx).allowed
        assert not check_host_path_access(tree / rel, ctx, writing=True).allowed
    # The editor tier runs from that state: unchanged.
    assert check_host_path_access(tree / rel, _ctx(roles.MANAGER)).allowed


@pytest.mark.parametrize("rel", ["workspace/.claude/projects/p/s.jsonl", "workspace/.codex/sessions/s.jsonl",
                                 "users/olga/.claude/projects/p/s.jsonl"])
def test_a_buttons_session_reads_no_engine_state_at_any_reach(tree, rel):
    """A full press at the app's own manager identity, a personal app's owner
    and a confined press: none reads or writes an engine's state."""
    f = tree / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    for ctx in (SecurityContext(role=roles.MANAGER, username="", agent=AGENT, is_admin_agent=False,
                                session_scope="agent", app_press=True),
                SecurityContext(role=roles.MANAGER, username="olga", agent=AGENT, is_admin_agent=False,
                                session_scope="user", app_press=True),
                SecurityContext(role=roles.VIEWER, username="", agent=AGENT, is_admin_agent=False,
                                session_scope="agent", press_reach=PRESS_MEMBER, app_press=True),
                # A full press on an admin-only agent, built at the admin role:
                # the admin shortcut never opens the engine state to a button.
                SecurityContext(role=roles.ADMIN, username="", agent=AGENT, is_admin_agent=True,
                                session_scope="agent", app_press=True)):
        assert not check_host_path_access(f, ctx).allowed
        assert not check_host_path_access(f, ctx, writing=True).allowed
    admin = SecurityContext(role=roles.ADMIN, username="", agent=AGENT, is_admin_agent=True,
                            session_scope="agent", app_press=True)
    assert check_host_path_access(tree / "workspace/a.txt", admin).allowed
    full = SecurityContext(role=roles.MANAGER, username="", agent=AGENT, is_admin_agent=False,
                           session_scope="agent", app_press=True)
    assert check_host_path_access(tree / "workspace/a.txt", full, writing=True).allowed


def test_the_files_api_grant_of_a_press_with_no_folder_refuses_everything():
    from fastapi import HTTPException

    from api.agents.files import _SessionGrant
    grant = _SessionGrant.of("appx-n", _ctx(roles.VIEWER, PRESS_NONE))
    for path in ("workspace/a.txt", "knowledge/k.md"):
        with pytest.raises(HTTPException):
            grant.check(path, writing=False)
    member = _SessionGrant.of("appx-m", _ctx(roles.VIEWER, PRESS_MEMBER))
    member.check("workspace/a.txt", writing=False)
    with pytest.raises(HTTPException):
        member.check("workspace/a.txt", writing=True)


@pytest.mark.parametrize(("method", "path", "ok"), [
    ("POST", "/v1/hooks/resolve-path", True),
    ("POST", "/v1/hooks/file-written", True),
    ("POST", "/v1/hooks/media", True),
    ("GET", "/v1/mcp-gateway/github/mcp", True),
    ("POST", "/v1/internal/memory/op", False),
    ("POST", "/v1/hooks/files/pin", False),
    ("POST", "/v1/hooks/ui", False),
    ("POST", "/v1/tasks", False),
    ("POST", "/v1/notifications", False),
    ("POST", "/v1/delegation/spawn", False),
    ("POST", "/v1/hooks/resolve-path/../ui", False),
    # The gateway's pattern takes any tail: only the traversal check
    # refuses a parent segment there.
    ("POST", "/v1/mcp-gateway/x/../../internal/memory/op", False),
    ("GET", "/v1/mcp-gateway/github/../y", False),
])
def test_the_routes_a_confined_press_reaches(method, path, ok):
    from auth.external_endpoints import is_press_endpoint_allowed
    assert is_press_endpoint_allowed(method, path) is ok


def test_every_route_a_confined_press_reaches_is_on_the_session_list():
    """The press's list is a narrower list of the same tokens."""
    from auth.external_endpoints import (
        is_press_endpoint_allowed,
        is_session_endpoint_allowed,
    )
    from tests.api.test_session_refusal import _UUID, _concrete, _table
    for method, path in _table():
        concrete = _concrete(path)
        if is_press_endpoint_allowed(method, concrete):
            assert is_session_endpoint_allowed(method, concrete, _UUID), (method, path)


def test_the_middleware_holds_a_confined_press_to_its_list(monkeypatch):
    """A session token of a confined press: the memory API is refused with
    the press's sentence, the path hook passes the press's rule."""
    import middleware
    from auth import external_endpoints as ee
    from core.session import session_state

    ctx = _ctx(roles.VIEWER, PRESS_MEMBER)
    monkeypatch.setattr(ee, "session_token_claims", lambda request: {"sid": "appx-p1", "agent": AGENT})
    monkeypatch.setattr(session_state, "session_token_refusal", lambda claims: "")
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr(middleware, "off_session_listener", lambda scope: False)

    def request(method, path):
        return SimpleNamespace(method=method, url=SimpleNamespace(path=path),
                               scope={"path": path, "method": method, "root_path": ""},
                               headers={}, client=None)
    monkeypatch.setattr(middleware, "routed_path", lambda r: r.url.path)
    refused = middleware._session_refusal(request("POST", "/v1/internal/memory/op"))
    assert refused is not None and refused.status_code == 403
    assert ee.PRESS_BLOCKED_DETAIL.encode() in refused.body
    # True for every confined press: a personal app's is confined with or
    # without typed text, an editor's too there, so the sentence names no
    # typed value and points at the owner or, on a shared app, an editor.
    assert ee.PRESS_BLOCKED_DETAIL == ("This button cannot use that tool when you press it. On a personal "
                                       "app, ask its owner. On a shared app, ask an editor of the agent or "
                                       "an admin.")
    assert middleware._session_refusal(request("POST", "/v1/hooks/resolve-path")) is None
    # The same session without a press reach is judged by the session list alone.
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: _ctx(roles.MANAGER))
    assert middleware._session_refusal(request("POST", "/v1/internal/memory/op")) is None


def test_a_confined_press_is_persisted_with_its_reach():
    """The reach is a field of the context, stored and read back like every
    field, so a restart never widens a confined session."""
    from core.session import session_state
    for reach in (PRESS_MEMBER, PRESS_NONE):
        ctx = _ctx(roles.VIEWER, reach)
        back = session_state._deserialize_security_ctx(session_state._serialize_security_ctx(ctx))
        assert back.press_reach == reach and back == ctx
    ctx = SecurityContext(role=roles.MANAGER, username="", agent=AGENT, is_admin_agent=False,
                          session_scope="agent", app_press=True)
    back = session_state._deserialize_security_ctx(session_state._serialize_security_ctx(ctx))
    assert back.app_press is True and back == ctx
