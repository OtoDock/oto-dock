"""The meetings-mcp prompt block follows the USER's access (C4).

``build_meetings_access`` resolves {agents × role} for the acting user
off-loop; ``_meetings_mcp_context`` renders it — falling back to the
delegation roster only for no-user sessions (whose create-endpoint reach IS
the roster).

Run: cd proxy && venv/bin/pytest tests/mcp/test_meetings_context.py -v
"""

from __future__ import annotations

from services.mcp import dynamic_context
from storage import agent_store, db_users


def _seed_agents():
    agent_store.create_agent("pa", "Personal Assistant", created_by="admin",
                             description="Daily driver")
    agent_store.create_agent("ops", "Ops", created_by="admin",
                             description="Infra agent")
    agent_store.create_agent("hidden", "Hidden", created_by="admin")


class TestBuildMeetingsAccess:
    def test_member_gets_assigned_agents_with_roles(self, temp_db):
        _seed_agents()
        db_users.upsert_user("u-1", "u@x.test", "U", "member")
        db_users.set_user_agents("u-1", ["pa", "ops"], assigned_by="admin")
        db_users.set_user_agent_role("u-1", "pa", "editor")
        rows = dynamic_context.build_meetings_access("u-1", "member")
        by_slug = {r["slug"]: r for r in rows}
        assert set(by_slug) == {"pa", "ops"}
        assert by_slug["pa"]["role"] == "editor"
        assert by_slug["pa"]["display_name"] == "Personal Assistant"
        assert by_slug["pa"]["description"] == "Daily driver"

    def test_admin_gets_every_agent(self, temp_db):
        _seed_agents()
        rows = dynamic_context.build_meetings_access("u-admin", "admin")
        assert {r["slug"] for r in rows} >= {"pa", "ops", "hidden"}
        assert all(r["role"] == "admin" for r in rows)

    def test_no_user_is_empty(self, temp_db):
        _seed_agents()
        assert dynamic_context.build_meetings_access("", "") == []


class TestMeetingsProvider:
    def _access(self):
        return [
            {"slug": "pa", "display_name": "Personal Assistant",
             "description": "Daily driver", "role": "editor"},
            {"slug": "ops", "display_name": "Ops",
             "description": "", "role": "viewer"},
        ]

    def test_user_access_rendering(self, temp_db):
        out = dynamic_context._meetings_mcp_context(
            "pa", delegation_targets=["pa"], meetings_access=self._access())
        assert out is not None
        assert "YOUR USER'S access" in out
        assert "**Ops** (`ops`) (viewer)" in out
        # Self excluded from the participant list.
        assert "`pa`" not in out.split("You can invite:")[1]
        assert "delegate()" in out

    def test_self_only_access_renders_nothing(self, temp_db):
        out = dynamic_context._meetings_mcp_context(
            "pa", delegation_targets=["pa"],
            meetings_access=[self._access()[0]])
        assert out is None

    def test_cap_appends_overflow_line(self, temp_db):
        many = [
            {"slug": f"a{i}", "display_name": f"A{i}",
             "description": "", "role": "viewer"}
            for i in range(dynamic_context._MEETINGS_ACCESS_CAP + 5)
        ]
        out = dynamic_context._meetings_mcp_context(
            "pa", delegation_targets=["pa"], meetings_access=many)
        assert "and 5 more" in out

    def test_no_user_falls_back_to_roster(self, temp_db):
        _seed_agents()
        out = dynamic_context._meetings_mcp_context(
            "pa", delegation_targets=["pa", "ops"], meetings_access=[])
        assert out is not None
        assert "no user" in out
        assert "Ops (`ops`)" in out
        assert "YOUR USER'S access" not in out

    def test_empty_everything_renders_nothing(self, temp_db):
        assert dynamic_context._meetings_mcp_context(
            "pa", delegation_targets=None, meetings_access=[]) is None
