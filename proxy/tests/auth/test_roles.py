"""The role authority (``auth/roles``): the members and the frozen store
spellings, the questions, the resolver over every principal shape, the
mutation ladder against the three copies it replaced, the principal
module's delegation, the MCP processes that carry no role table and the
dashboard mirror in lock-step. Core-seams phase 5."""

from __future__ import annotations

import ast
import re
import sys

import pytest

from tests._paths import CUSTOM_MCPS, PROXY_DIR, REPO_ROOT

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from auth import roles  # noqa: E402

_MIRROR = REPO_ROOT / "dashboard" / "src" / "lib" / "permissions.ts"


def test_the_members_and_the_frozen_spellings():
    assert roles.PLATFORM_ROLES == ("admin", "creator", "member")
    assert roles.AGENT_ROLES == ("manager", "editor", "contributor", "viewer")
    assert roles.EFFECTIVE_ROLES == ("viewer", "contributor", "editor", "manager", "admin")
    assert roles.PLATFORM_BY_RANK == ("member", "creator", "admin")
    assert roles.SERVICE == "agent" and roles.NO_ACCESS == ""
    assert roles.OWNER_TIER == (roles.MANAGER, roles.ADMIN)
    assert roles.EDITOR_TIER == (roles.MANAGER, roles.EDITOR, roles.ADMIN)
    assert roles.WORKSPACE_TIER == (roles.MANAGER, roles.EDITOR, roles.CONTRIBUTOR, roles.ADMIN)
    assert roles.CREATOR_TIER == (roles.ADMIN, roles.CREATOR)
    assert roles.SHARED_ONLY_ROLES == (roles.MANAGER, roles.EDITOR)
    assert [r for r in roles.AGENT_ROLES if roles.allowed_on_shared_only(r)] == ["manager", "editor"]
    assert not roles.allowed_on_shared_only(None) and not roles.allowed_on_shared_only("admin")
    assert roles.RANK == {"viewer": 0, "contributor": 1, "editor": 2, "manager": 3, "admin": 4}
    assert roles.PLATFORM_RANK == {"member": 0, "creator": 1, "admin": 2}
    # The DB CHECKs are the frozen spellings: the constants are built from
    # them, never the other way round.
    identity = (PROXY_DIR / "storage" / "identity" / "schema.py").read_text(encoding="utf-8")
    assert "CHECK (role IN ('admin', 'creator', 'member'))" in identity
    assert "CHECK (agent_role IN ('manager', 'editor', 'contributor', 'viewer'))" in identity
    assert "agent_role TEXT DEFAULT 'viewer'" in identity
    phone = (PROXY_DIR / "storage" / "phone" / "schema.py").read_text(encoding="utf-8")
    assert "CHECK (role IN ('viewer', 'editor', 'manager'))" in phone
    agents = (PROXY_DIR / "storage" / "agents" / "schema.py").read_text(encoding="utf-8")
    assert "CHECK (default_for_new_users_role IN ('', 'viewer', 'contributor', 'editor', 'manager'))" in agents
    sharing = (PROXY_DIR / "storage" / "sharing" / "schema.py").read_text(encoding="utf-8")
    assert "role_cap IN ('manager', 'editor', 'contributor', 'viewer')" in sharing
    # The validators read the authority, never a literal set of their own.
    from storage.agents import agent_store, community_agent_template_store
    assert community_agent_template_store.VALID_DEFAULT_USER_ROLES == set(roles.AGENT_ROLES)
    assert agent_store._DEFAULT_ROLE_CHOICES == (roles.NO_ACCESS, *roles.AGENT_ROLES)
    # The migration's words are the CREATEs' words.
    from storage import schema as pg_schema
    by_name = {name: words for _t, name, _c, words in pg_schema._ROLE_CHECKS}
    assert by_name["user_agents_agent_role_check"] == roles.AGENT_ROLES
    assert by_name["agents_default_for_new_users_role_check"] == ("", "viewer", "contributor", "editor", "manager")
    assert by_name["shares_role_cap_check"] == roles.AGENT_ROLES


def test_the_questions():
    assert roles.is_admin("admin") and not roles.is_admin("creator")
    assert not roles.is_admin("") and not roles.is_admin(None) and not roles.is_admin("agent")
    assert roles.is_creator_or_above("admin") and roles.is_creator_or_above("creator")
    assert not roles.is_creator_or_above("member") and not roles.is_creator_or_above("agent")
    assert [r for r in roles.EFFECTIVE_ROLES if roles.can_manage(r)] == ["manager", "admin"]
    assert [r for r in roles.EFFECTIVE_ROLES if roles.can_edit(r)] == ["editor", "manager", "admin"]
    assert [r for r in roles.EFFECTIVE_ROLES if roles.can_write_workspace(r)] == [
        "contributor", "editor", "manager", "admin"]
    assert not roles.can_manage("") and not roles.can_edit(None) and not roles.can_edit("agent")
    assert not roles.can_write_workspace("") and not roles.can_write_workspace("none")
    # The rank: an unknown word ranks 0 — the viewer floor and no other,
    # which is how the app broker lets a stranger (``none``) reach an
    # app's default floor on the edge alone.
    assert roles.rank("none") == 0 and roles.rank("") == 0 and roles.rank(None) == 0
    for floor in (None, "", "viewer"):
        assert roles.meets_floor("none", floor) and roles.meets_floor("viewer", floor)
    for floor in ("contributor", "editor", "manager"):
        assert not roles.meets_floor("none", floor) and not roles.meets_floor("viewer", floor)
    assert roles.meets_floor("contributor", "contributor") and roles.meets_floor("editor", "contributor")
    assert not roles.meets_floor("contributor", "editor")
    assert roles.meets_floor("editor", "editor") and not roles.meets_floor("editor", "manager")
    assert roles.meets_floor("manager", "manager") and roles.meets_floor("admin", "manager")
    # A floor the table does not know is unreachable (the parsers refuse one
    # at deploy; today's rank table read it as 0 — everyone passed).
    assert not roles.meets_floor("admin", "owner") and not roles.meets_floor("none", "owner")
    # The phone cap: never the admin policy on a phone line.
    assert roles.capped("admin", "manager") == "manager"
    assert roles.capped("manager", "manager") == "manager"
    assert roles.capped("editor", "manager") == "editor"
    assert roles.capped("contributor", "manager") == "contributor"
    assert roles.capped("viewer", "manager") == "viewer"
    # The OIDC fold: the strongest platform role among the groups' answers.
    assert roles.highest_platform_role(["member", "admin", "creator"]) == "admin"
    assert roles.highest_platform_role(["member", "creator"]) == "creator"
    assert roles.highest_platform_role(["member"]) == "member"
    assert roles.highest_platform_role([]) is None
    assert roles.highest_platform_role([None, "owner"]) is None
    # The label a prompt or a badge shows: the members as they are.
    assert roles.label("editor") == "editor" and roles.label("admin") == "admin"
    assert roles.label("contributor") == "contributor"
    assert roles.label("creator") == "creator" and roles.label("member") == "member"
    assert roles.label("") == "viewer" and roles.label("agent") == "viewer" and roles.label(None) == "viewer"
    # The row alone: the platform role ignored, NO_ACCESS without a row.
    assert roles.row_role({"a": "editor"}, "a") == "editor"
    assert roles.row_role({"a": "editor"}, "b") == "" and roles.row_role(None, "a") == ""


@pytest.mark.parametrize("shape, platform_role, agent_roles, effective, acting", [
    ("a user with no platform row", "", {}, "", "viewer"),
    ("a platform role and no per-agent row", "member", {"other": "manager"}, "", "viewer"),
    ("a per-agent viewer", "member", {"a": "viewer"}, "viewer", "viewer"),
    ("a per-agent contributor", "member", {"a": "contributor"}, "contributor", "contributor"),
    ("a per-agent editor", "creator", {"a": "editor"}, "editor", "editor"),
    ("a per-agent manager", "member", {"a": "manager"}, "manager", "manager"),
    ("a no-user service session", "agent", {}, "", "viewer"),
    ("the admin (a cookie, or the master key)", "admin", {}, "admin", "admin"),
    ("the admin with a row (the row never lowers an admin)", "admin", {"a": "viewer"}, "admin", "admin"),
])
def test_the_resolver_over_the_principal_shapes(shape, platform_role, agent_roles, effective, acting):
    assert roles.effective_role(platform_role, agent_roles, "a") == effective, shape
    assert roles.acting_role(platform_role, agent_roles, "a") == acting, shape
    assert roles.acting_role(platform_role, None, "a") in ("admin", "viewer")


def test_may_mutate_shared_matches_the_three_ladders():
    """The agent-scope half of ``_task_mutation_allowed`` (chats), the task
    permission check and the trigger permission check, as they read before
    the phase: owner tier any, editor own, viewer and a stranger nothing."""
    for own in (True, False):
        assert roles.may_mutate_shared("admin", own=own)
        assert roles.may_mutate_shared("manager", own=own)
        assert not roles.may_mutate_shared("viewer", own=own)
        assert not roles.may_mutate_shared("contributor", own=own)
        assert not roles.may_mutate_shared("", own=own)
        assert not roles.may_mutate_shared(None, own=own)
    assert roles.may_mutate_shared("editor", own=True)
    assert not roles.may_mutate_shared("editor", own=False)


def test_the_principal_module_delegates(monkeypatch):
    from auth import providers
    from auth.providers import UserContext

    def ctx(role, agent_roles=None, **kw):
        return UserContext(sub="u", email="u@x", name="U", role=role, agent_roles=agent_roles or {}, **kw)

    admin, member = ctx("admin"), ctx("member", {"a": "editor"})
    assert admin.is_admin and not member.is_admin
    assert admin.effective_role("a") == "admin" and admin.acting_role("zzz") == "admin"
    assert member.effective_role("a") == "editor" and member.effective_role("b") == ""
    assert member.acting_role("a") == "editor" and member.acting_role("b") == "viewer"
    assert member.can_edit_agent("a") and not member.can_manage_agent("a")
    assert not member.can_edit_agent("b") and admin.can_manage_agent("b")
    contributor = ctx("member", {"a": "contributor"})
    assert contributor.can_write_workspace("a") and not contributor.can_edit_agent("a")
    assert not contributor.can_write_workspace("b") and member.can_write_workspace("a")
    assert not ctx("member", {"a": "viewer"}).can_write_workspace("a")
    assert ctx("creator").can_write_files() and not member.can_manage_tasks()
    service = ctx("agent", is_api_key=True, session_id="s", agent="a")
    service.sub = "session:s"
    assert service.effective_role("a") == "" and service.acting_role("a") == "viewer"
    assert not service.can_edit_agent("a") and not service.is_admin
    # The store-backed pair: the live row first, the connect-time dict a
    # socket kept when the row is gone, then the per-agent map.
    rows = {"u1": {"sub": "u1", "role": "member"}, "root": {"sub": "root", "role": "admin"}}
    maps = {"u1": {"a": "manager"}}
    monkeypatch.setattr(providers.task_store, "get_user", rows.get)
    monkeypatch.setattr(providers.task_store, "get_user_agent_roles", lambda sub: dict(maps.get(sub, {})))
    assert providers.effective_role_of("u1", "a") == "manager"
    assert providers.effective_role_of("u1", "b") == ""
    assert providers.acting_role_of("u1", "b") == "viewer"
    assert providers.effective_role_of("root", "b") == "admin"
    assert providers.effective_role_of("gone", "a") == ""
    assert providers.acting_role_of("gone", "a") == "viewer"
    assert providers.effective_role_of("gone", "a", fallback_user={"role": "admin"}) == "admin"
    assert providers.acting_role_of("gone", "a", fallback_user={"role": "member"}) == "viewer"
    assert providers.effective_role_of("", "a") == "" and providers.acting_role_of("", "a") == "viewer"


def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"{name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def _ts_union(text: str, name: str) -> list[str]:
    m = re.search(rf"export type {name}\s*=\s*(.+?)\n", text)
    assert m, f"type {name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def test_the_dashboard_mirror_equals_the_authority():
    text = _MIRROR.read_text(encoding="utf-8")
    assert set(_ts_const_strings(text, "ROLE")) == set(roles.PLATFORM_ROLES) | set(roles.AGENT_ROLES)
    assert _ts_const_strings(text, "PLATFORM_ROLES") == list(roles.PLATFORM_ROLES)
    assert _ts_const_strings(text, "AGENT_ROLES") == list(roles.AGENT_ROLES)
    assert _ts_const_strings(text, "EFFECTIVE_ROLES") == list(roles.EFFECTIVE_ROLES)
    assert _ts_const_strings(text, "PLATFORM_BY_RANK") == list(roles.PLATFORM_BY_RANK)
    assert _ts_const_strings(text, "OWNER_TIER") == list(roles.OWNER_TIER)
    assert _ts_const_strings(text, "EDITOR_TIER") == list(roles.EDITOR_TIER)
    assert _ts_const_strings(text, "WORKSPACE_TIER") == list(roles.WORKSPACE_TIER)
    assert _ts_const_strings(text, "CREATOR_TIER") == list(roles.CREATOR_TIER)
    assert _ts_const_strings(text, "SHARED_ONLY_ROLES") == list(roles.SHARED_ONLY_ROLES)
    assert _ts_union(text, "PlatformRole") == list(roles.PLATFORM_ROLES)
    assert _ts_union(text, "AgentRole") == list(roles.AGENT_ROLES)
    for fn in ("isAdmin", "isCreatorOrAbove", "canManageAgent", "canEditAgent", "canWriteWorkspace",
               "actingRole", "meetsFloor", "roleBadge", "roleLabel", "allowedOnSharedOnly"):
        assert f"export function {fn}(" in text, fn


class TestRoleCheckMigration:
    """An install created before the contributor role carries the older
    CHECKs: ``run_migrations`` rewrites each once, and the drift guard names
    a stale one until it has."""

    def test_an_older_check_is_rewritten_once_and_reported_until_then(self, temp_db):
        from storage import database as task_store
        from storage import pg
        from storage import schema as pg_schema
        from storage.agents import agent_store
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute("ALTER TABLE user_agents DROP CONSTRAINT user_agents_agent_role_check")
            conn.execute("ALTER TABLE user_agents ADD CONSTRAINT user_agents_agent_role_check "
                         "CHECK (agent_role IN ('manager', 'editor', 'viewer'))")
            conn.commit()
        pg.close_pool()
        with get_conn() as conn:
            drift = pg_schema.check_schema_drift(conn)
        assert ("user_agents", "CHECK user_agents_agent_role_check") in drift
        for _ in range(2):  # idempotent: a no-op once the words are in
            with get_conn() as conn:
                pg_schema.run_migrations(conn)
                conn.commit()
        pg.close_pool()
        with get_conn() as conn:
            assert pg_schema.check_schema_drift(conn) == []
            assert pg_schema._check_constraint_words(conn, "user_agents", "user_agents_agent_role_check") \
                == set(roles.AGENT_ROLES)
        agent_store.create_agent("rc-agent", "Rc Agent", created_by="user-admin")
        task_store.set_user_agents("user-viewer", ["rc-agent"], "user-admin",
                                   agent_roles={"rc-agent": "contributor"})
        assert task_store.get_user_agent_roles("user-viewer") == {"rc-agent": "contributor"}


_TIER_NAMES = {"OWNER_TIER", "EDITOR_TIER", "WORKSPACE_TIER", "CREATOR_TIER",
               "ADMIN", "CREATOR", "MEMBER", "MANAGER", "EDITOR", "CONTRIBUTOR", "VIEWER"}


@pytest.mark.parametrize("server", ["agent-config-mcp", "checks-mcp", "mcps-mcp", "agent-creator-mcp"])
def test_no_mcp_server_carries_a_role_table(server):
    """A separate process cannot import the proxy, so it asks the env the
    tier questions (the ``OTO_CAN_*`` flags ``core/sandbox/oto_env.py``
    answers from the authority) and spells no role word of its own."""
    text = (CUSTOM_MCPS / server / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    assigned = {t.id for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert not assigned & _TIER_NAMES, (server, assigned & _TIER_NAMES)
    assert "OTO_CAN_" in text, server
