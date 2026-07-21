"""Agents-grid correctness.

- ``maybe_autoset_default_agent``: a user's only (non-internal) agent becomes
  their favorite automatically; more than one agent, or an existing favorite,
  leaves it untouched.
- ``count_user_visible_*``: the per-agent schedule/trigger numbers shown on the
  grid count ONLY what the calling user may see (agent-scoped + their own),
  never another user's user-scoped items.

Run: cd proxy && venv/bin/pytest tests/agents/test_agent_grid_counts.py -v
"""

from __future__ import annotations

import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from storage import agent_store  # noqa: E402
from storage import database as task_store  # noqa: E402
from storage import trigger_store  # noqa: E402

A = "user-admin"      # seeded by conftest._seed_users
M = "user-manager"
V = "user-viewer"


class TestAutoFavoriteSoleAgent:
    def test_single_agent_becomes_favorite(self, temp_db):
        agent_store.create_agent("solo", "Solo", created_by=A)
        assert task_store.get_user_default_agent(V) is None
        task_store.set_user_agents(V, ["solo"], A, agent_roles={"solo": "viewer"})
        assert task_store.get_user_default_agent(V) == "solo"

    def test_two_agents_no_autofavorite(self, temp_db):
        agent_store.create_agent("a1", "A1", created_by=A)
        agent_store.create_agent("a2", "A2", created_by=A)
        task_store.set_user_agents(
            V, ["a1", "a2"], A, agent_roles={"a1": "viewer", "a2": "viewer"}
        )
        assert task_store.get_user_default_agent(V) is None

    def test_existing_favorite_not_overridden(self, temp_db):
        agent_store.create_agent("a1", "A1", created_by=A)
        agent_store.create_agent("a2", "A2", created_by=A)
        task_store.set_user_agents(
            V, ["a1", "a2"], A, agent_roles={"a1": "viewer", "a2": "viewer"}
        )
        task_store.set_user_default_agent(V, "a2")
        # Dropping back to a single, different agent must NOT steal the favorite.
        task_store.set_user_agents(V, ["a1"], A, agent_roles={"a1": "viewer"})
        assert task_store.get_user_default_agent(V) == "a2"

    def test_shared_only_agent_excluded(self, temp_db):
        agent_store.create_agent("real", "Real", created_by=A)
        agent_store.create_agent("svc", "Svc", created_by=A,
                                 default_scope="agent", collaborative=False)
        task_store.set_user_agents(
            V, ["real", "svc"], A, agent_roles={"real": "viewer", "svc": "viewer"}
        )
        # Only one user-facing agent (the Shared-only svc doesn't count) → favorite.
        assert task_store.get_user_default_agent(V) == "real"

    def test_add_user_agent_path_autofavorites(self, temp_db):
        # The new-user auto-attach path (add_user_agent) must also adopt the sole agent.
        agent_store.create_agent("only", "Only", created_by=A)
        assert task_store.add_user_agent(V, "only", "viewer", "system") is True
        assert task_store.get_user_default_agent(V) == "only"


class TestUserVisibleCounts:
    def _seed(self):
        agent_store.create_agent("shared-agent", "Shared", created_by=A)
        # 2 agent-scoped tasks + manager's own + viewer's own (different users).
        for tid, name, by, scope in [
            ("t-ag1", "ag1", None, "agent"),
            ("t-ag2", "ag2", None, "agent"),
            ("t-m", "m", M, "user"),
            ("t-v", "v", V, "user"),
        ]:
            task_store.create_dynamic_task(
                tid, "shared-agent", name, "p", "proxy", "manual",
                None, None, None, 0, by, scope=scope,
            )
        trigger_store.create_trigger(slug="tr-ag", name="ag", scope="agent", agent="shared-agent", created_by=A)
        trigger_store.create_trigger(slug="tr-m", name="m", scope="user", agent="shared-agent", created_by=M)
        trigger_store.create_trigger(slug="tr-v", name="v", scope="user", agent="shared-agent", created_by=V)

    def test_task_count_is_agent_scoped_plus_own(self, temp_db):
        self._seed()
        # Each user: 2 agent-scoped + their OWN 1 = 3 — never the other user's.
        assert task_store.count_user_visible_dynamic_tasks_by_agent(M).get("shared-agent") == 3
        assert task_store.count_user_visible_dynamic_tasks_by_agent(V).get("shared-agent") == 3
        # Global (admin / API-key path) counts all 4.
        assert task_store.count_dynamic_tasks_by_agent().get("shared-agent") == 4

    def test_trigger_count_is_agent_scoped_plus_own(self, temp_db):
        self._seed()
        # Each user: 1 agent-scoped + their OWN 1 = 2 — never the other user's.
        assert trigger_store.count_user_visible_triggers_by_agent(M).get("shared-agent") == 2
        assert trigger_store.count_user_visible_triggers_by_agent(V).get("shared-agent") == 2
        # Global (admin / API-key path) counts all 3.
        assert trigger_store.count_triggers_by_agent().get("shared-agent") == 3
