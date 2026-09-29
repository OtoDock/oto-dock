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

from storage.agents import agent_store  # noqa: E402
from storage import database as task_store  # noqa: E402
from storage.automation import trigger_store  # noqa: E402

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


# ---------------------------------------------------------------------------
# The listing off the loop: one job, a bulk MCP view, one model per engine
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from auth.providers import UserContext  # noqa: E402


def _manifest(name, *, assignment_mode="auto", category="custom", cap=None):
    from services.mcp.mcp_registry import CredentialConfig, McpManifest, ServerConfig
    return McpManifest(
        name=name, label=name, description="", version="1.0.0", category=category,
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(type="none"), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], assignment_mode=assignment_mode,
        requires_capability=cap,
    )


def _member(*slugs: str) -> UserContext:
    return UserContext(sub=V, email="v@t.com", name="Viewer", role="member",
                       agents=list(slugs), agent_roles={s: "viewer" for s in slugs})


class TestListingOffLoop:
    def _seed_mcps(self, monkeypatch, *, tts_available: bool):
        from services.mcp import mcp_registry
        from services.media import audio_service
        from storage.mcp import mcp_store
        manifests = {
            "auto-a": _manifest("auto-a"),
            "expl-b": _manifest("expl-b", assignment_mode="explicit"),
            "skill-c": _manifest("skill-c", category="skill"),
            "cap-d": _manifest("cap-d", cap="audio_tts"),
            "off-e": _manifest("off-e"),
        }
        monkeypatch.setattr(mcp_registry, "_manifests", manifests)
        monkeypatch.setattr(audio_service, "tts_capability_available", lambda: tts_available)
        for name in manifests:
            mcp_store.set_mcp_enabled(name, name != "off-e")
        for slug in ("ag1", "ag2", "ag3"):
            agent_store.create_agent(slug, slug.upper(), created_by=A)
        mcp_store.set_manager_enabled_mcps("ag1", list(manifests) + ["unknown-z"])
        mcp_store.set_manager_enabled_mcps("ag2", ["expl-b", "auto-a"])
        mcp_store.upsert_mcp_instance("expl-b", {"instance_name": "i1", "field_values": {},
                                                 "agents": ["ag1"]})

    def test_bulk_mcp_view_matches_the_per_agent_badge(self, temp_db, monkeypatch):
        from api.agents import discovery
        from services.media import audio_service
        self._seed_mcps(monkeypatch, tts_available=True)
        for available in (True, False):
            monkeypatch.setattr(audio_service, "tts_capability_available", lambda: available)
            slugs = ["ag1", "ag2", "ag3"]
            bulk = discovery._mcp_names_by_agent(slugs)
            assert bulk == {s: discovery._get_mcp_info(s)[1] for s in slugs}
            assert bulk["ag1"] == (["auto-a", "cap-d", "expl-b"] if available
                                   else ["auto-a", "expl-b"])
            assert bulk["ag2"] == ["auto-a"]  # expl-b: no instance names ag2
            assert bulk["ag3"] == []

    def test_listing_runs_off_the_loop_and_answers_the_same_rows(self, temp_db, monkeypatch, loop_db_guard):
        """The handler is one executor job; awaited on this thread
        with the guard armed it answers what the on-loop listing did."""
        from api.agents import discovery
        self._seed_mcps(monkeypatch, tts_available=True)
        u = _member("ag1", "ag2")
        admin = UserContext(sub=A, email="a@t.com", name="Admin", role="admin")

        async def scenario():
            before = await discovery.list_agents(all=False, user=u)
            with loop_db_guard.active():
                after = await discovery.list_agents(all=False, user=u)
                everyone = await discovery.list_agents(all=True, user=admin)
                info = await discovery.get_agent_info("ag1", user=u)
            return before, after, everyone, info

        before, after, everyone, info = asyncio.run(scenario())
        assert after == before
        assert [a["name"] for a in after["agents"]] == ["ag1", "ag2"]
        by = {a["name"]: a for a in after["agents"]}
        assert by["ag1"]["mcp_names"] == ["auto-a", "cap-d", "expl-b"]
        assert by["ag1"]["mcp_count"] == 3 and by["ag2"]["mcp_count"] == 1
        assert [a["name"] for a in everyone["agents"]] == ["ag1", "ag2", "ag3"]
        assert info["mcps"] == ["auto-a", "cap-d", "expl-b"]

    def test_the_layer_default_model_is_resolved_once_per_engine(self, temp_db, monkeypatch):
        import config
        from api.agents import discovery
        for slug, model in (("un1", ""), ("un2", ""), ("pin", "claude-fable-5-1")):
            agent_store.create_agent(slug, slug, created_by=A, default_model=model)
        agent_store.create_agent("other", "other", created_by=A, execution_path="codex-cli")
        calls: list[str] = []

        def fake_default(layer, *, agent_name=""):
            calls.append(layer)
            if layer == "codex-cli":
                raise RuntimeError("nothing enabled")
            return "m-default"

        monkeypatch.setattr(config, "resolve_layer_default_model", fake_default)
        u = _member("un1", "un2", "pin", "other")
        rows = {a["name"]: a["default_model"]
                for a in asyncio.run(discovery.list_agents(all=False, user=u))["agents"]}
        assert rows == {"un1": "m-default", "un2": "m-default",
                        "pin": "claude-fable-5-1", "other": ""}
        assert sorted(calls) == ["claude-code-cli", "codex-cli"]
