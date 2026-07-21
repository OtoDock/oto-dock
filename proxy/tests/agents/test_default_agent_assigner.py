"""Tests for ``services/community/default_agent_assigner.py`` — auto-attach.

Covers the edge cases:

- single + multi default agents
- PK-conflict short-circuit for the setup-wizard admin
- ``default_agents_assigned`` bool gates OIDC re-logins
- per-agent role respected (role filter)
- per-agent failure doesn't abort the loop
- assigning a user with no defaults still marks them assigned
- explicit absence of backfill: defaults installed AFTER user exists do
  NOT auto-attach

Run: cd proxy && venv/bin/pytest tests/agents/test_default_agent_assigner.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import patch


from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_user(sub: str, email: str, role: str = "member") -> None:
    """Insert a users row directly so FK from user_agents clears."""
    from storage import database as db
    db.upsert_user(sub, email, email.split("@")[0], role)


def _make_default_agent(slug: str, role: str = "viewer") -> None:
    """Create an agent + flip its default-for-new-users role.

    Skips the community-template install machinery — we exercise the
    assigner in isolation. Tests that need a real template install
    live in ``test_community_agent_installer.py::TestUserJoinHook``.
    """
    from storage import agent_store
    agent_store.create_agent(slug, slug.replace("-", " ").title())
    agent_store.set_default_for_new_users_role(slug, role)


# ---------------------------------------------------------------------------


class TestAssignDefaultAgents:
    def test_attaches_user_to_single_default_agent(self, temp_db):
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        _make_default_agent("default-a", role="viewer")

        result = assign_default_agents("u1")

        assert result == {"default-a": "attached"}
        assert db.get_user_agents("u1") == ["default-a"]
        roles = db.get_user_agent_roles("u1")
        assert roles["default-a"] == "viewer"
        assert db.is_default_agents_assigned("u1") is True

    def test_attaches_to_multiple_default_agents(self, temp_db):
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        _make_default_agent("default-a", role="viewer")
        _make_default_agent("default-b", role="editor")

        assign_default_agents("u1")

        roles = db.get_user_agent_roles("u1")
        assert roles == {"default-a": "viewer", "default-b": "editor"}

    def test_no_defaults_still_marks_user_assigned(self, temp_db):
        """When no agent has default_for_new_users_role set, the user is
        still flagged so OIDC re-logins skip the (cheap) pass."""
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        assign_default_agents("u1")
        assert db.is_default_agents_assigned("u1") is True
        assert db.get_user_agents("u1") == []

    def test_second_call_short_circuits_via_assigned_bool(self, temp_db):
        """OIDC re-login should not re-attach after admin
        removes the user from the default agent."""
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        _make_default_agent("default-a", role="viewer")
        assign_default_agents("u1")
        assert db.get_user_agents("u1") == ["default-a"]

        # Admin removes the user from the agent.
        db.set_user_agents("u1", [], "admin")
        assert db.get_user_agents("u1") == []

        # OIDC re-login fires again — bool short-circuits the whole loop.
        result = assign_default_agents("u1")
        assert result == {"_all_": "skipped-already-assigned"}
        assert db.get_user_agents("u1") == []  # NOT re-attached

    def test_pk_conflict_keeps_existing_higher_role(self, temp_db):
        """Admin is already manager → default-as-viewer
        attempt hits the PK and no-ops; admin stays manager."""
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("admin1", "admin@example.com", role="admin")
        _make_default_agent("default-a", role="viewer")
        # Pre-existing manager assignment.
        db.add_user_agent("admin1", "default-a", "manager", "system")

        result = assign_default_agents("admin1")

        assert result == {"default-a": "already-attached"}
        roles = db.get_user_agent_roles("admin1")
        assert roles["default-a"] == "manager"  # NOT downgraded

    def test_fires_on_user_added_to_agent_hook(self, temp_db):
        """Newly-attached pair triggers seeding via the hook."""
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        _make_default_agent("default-a", role="viewer")

        with patch(
            "services.community.community_agent_installer.on_user_added_to_agent",
        ) as hook:
            hook.return_value = {"tasks": 0, "triggers": 0, "notifications": 0}
            assign_default_agents("u1")
            hook.assert_called_once_with("default-a", "u1", "viewer")

    def test_pk_conflict_skips_hook_fire(self, temp_db):
        """Hook only fires on NEW attaches — PK-conflict path doesn't.

        Matters because the hook also runs from ``set_user_agents`` in the
        installer path; firing again on the conflict path would double the
        idempotent-guard work for the installer's own pair.
        """
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        _make_default_agent("default-a", role="viewer")
        db.add_user_agent("u1", "default-a", "manager", "system")

        with patch(
            "services.community.community_agent_installer.on_user_added_to_agent",
        ) as hook:
            assign_default_agents("u1")
            hook.assert_not_called()

    def test_hook_failure_keeps_attach(self, temp_db):
        """Hook raises → attach stays, error logged, other agents still run."""
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        _make_default_agent("default-a", role="viewer")
        _make_default_agent("default-b", role="editor")

        with patch(
            "services.community.community_agent_installer.on_user_added_to_agent",
            side_effect=RuntimeError("simulated seeding failure"),
        ):
            assign_default_agents("u1")

        # Both agents were attached even though the hook raised for both.
        assert set(db.get_user_agents("u1")) == {"default-a", "default-b"}
        # User is still marked assigned (hook errors don't undo the pass).
        assert db.is_default_agents_assigned("u1") is True

    def test_per_agent_attach_error_doesnt_abort_loop(self, temp_db):
        """One add_user_agent failure shouldn't block the other agents."""
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        _make_default_agent("default-a", role="viewer")
        _make_default_agent("default-b", role="editor")

        # Make the first add_user_agent call raise. patch the bound name
        # the assigner uses (it imported ``database as user_store``).
        real_add = db.add_user_agent
        calls = {"n": 0}

        def flaky_add(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated DB hiccup")
            return real_add(*args, **kwargs)

        with patch(
            "services.community.default_agent_assigner.user_store.add_user_agent",
            side_effect=flaky_add,
        ):
            result = assign_default_agents("u1")

        # First call errored; second succeeded.
        errored = [k for k, v in result.items() if v.startswith("error")]
        attached = [k for k, v in result.items() if v == "attached"]
        assert len(errored) == 1
        assert len(attached) == 1

    def test_backfill_not_supported(self, temp_db):
        """Default agent installed AFTER existing user → user
        is NOT retroactively attached. assign_default_agents was already
        called on user creation (when there were no defaults), and the
        bool prevents a re-run from doing anything."""
        from storage import database as db
        from services.community.default_agent_assigner import assign_default_agents

        _make_user("u1", "u1@example.com")
        # User creation path — no defaults exist yet — marks them assigned.
        assign_default_agents("u1")
        assert db.is_default_agents_assigned("u1") is True

        # Admin installs a default-attach agent later.
        _make_default_agent("late-default", role="viewer")

        # Re-running the assigner explicitly (admin debug action) is still
        # a no-op because the bool gates everything.
        result = assign_default_agents("u1")
        assert result == {"_all_": "skipped-already-assigned"}
        assert db.get_user_agents("u1") == []
