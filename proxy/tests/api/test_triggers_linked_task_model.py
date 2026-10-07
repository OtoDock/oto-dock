"""A trigger has no model of its own: the list and detail routes carry the
linked task's effective model, resolved like the Scheduled Tasks list and
batched once per listing."""

import asyncio

import pytest

from auth.providers import UserContext
from storage import database as task_store
from storage.agents import agent_store
from storage.automation import trigger_store

AGENT = "briefer"


def _admin():
    return UserContext(sub="user-admin", email="admin@test.com", name="Admin",
                       role="admin", agents=[AGENT], agent_roles={AGENT: "manager"})


def _seed(task_model: str | None) -> tuple[str, dict]:
    agent_store.create_agent(AGENT, "Briefer", collaborative=True, default_scope="user")
    agent_store.update_agent(AGENT, execution_path="claude-code-cli",
                             execution_paths='["claude-code-cli"]')
    task_id = "dyn-trig1234"
    task_store.create_dynamic_task(
        task_id, AGENT, "On push", "review the push", "cli", "trigger",
        None, None, None, 600, "user-admin", scope="user",
        override_model=task_model,
    )
    row = trigger_store.create_trigger(
        slug="on-push", name="On push", scope="user", agent=AGENT,
        created_by="user-admin", task_id=task_id,
    )
    return task_id, row


@pytest.mark.usefixtures("temp_db")
class TestLinkedTaskModel:
    def test_list_and_detail_carry_the_pinned_model_with_its_tier(self):
        from api.events.triggers import get_trigger_endpoint, list_triggers_endpoint
        _task_id, row = _seed("claude-opus-5-5")
        listed = asyncio.run(list_triggers_endpoint(agent=None, scope=None, audit=False, user=_admin()))
        (t,) = listed["triggers"]
        assert t["task_name"] == "On push"
        assert t["task_effective_model"] == "claude-opus-5-5"
        assert t["task_override_model"] == "claude-opus-5-5"
        assert t["task_effective_model_source"] == "pinned"
        assert t["task_effective_model_tier"] == 2 and t["task_tier_label"] == "strong"
        detail = asyncio.run(get_trigger_endpoint(row["id"], user=_admin()))
        assert detail["task_effective_model"] == "claude-opus-5-5"
        assert detail["task_effective_execution_path"] == "claude-code-cli"

    def test_unresolvable_default_is_empty_and_an_unlinked_trigger_is_blank(self):
        from api.events.triggers import list_triggers_endpoint
        # No enabled model on the install: the default resolves to "", never
        # a wrong claim; a notify-only trigger carries the empty shape.
        _seed(None)
        trigger_store.create_trigger(
            slug="notify-only", name="Notify only", scope="user", agent=AGENT,
            created_by="user-admin", notify_enabled=True,
        )
        rows = asyncio.run(list_triggers_endpoint(agent=None, scope=None, audit=False, user=_admin()))["triggers"]
        by_slug = {r["slug"]: r for r in rows}
        assert by_slug["on-push"]["task_effective_model"] == ""
        assert by_slug["on-push"]["task_effective_model_source"] == "agent default"
        assert by_slug["notify-only"]["task_name"] is None
        assert by_slug["notify-only"]["task_effective_model"] == ""


@pytest.mark.usefixtures("temp_db")
class TestDecorationReadsOnce:
    def test_the_listing_reads_its_names_and_apps_in_batches_off_the_loop(self, monkeypatch):
        """However many rows: the creators' names and usernames and the apps
        come from one batched read each, in a worker thread; the per-row
        readers are never called."""
        from api.events import triggers as triggers_api
        from api.events.triggers import list_triggers_endpoint
        from storage.automation import notification_store
        task_id, _ = _seed(None)
        trigger_store.create_trigger(
            slug="on-push-2", name="On push 2", scope="user", agent=AGENT,
            created_by="user-admin", task_id=task_id,
        )
        batches = []

        def off_loop(name, fn):
            def guarded(*a, **kw):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    batches.append(name)
                    return fn(*a, **kw)
                raise AssertionError(f"{name} ran on the event loop")
            return guarded

        monkeypatch.setattr(notification_store, "resolve_subs_to_display_names",
                            off_loop("names", notification_store.resolve_subs_to_display_names))
        monkeypatch.setattr(notification_store, "resolve_subs_to_usernames",
                            off_loop("usernames", notification_store.resolve_subs_to_usernames))
        monkeypatch.setattr(task_store, "get_apps_by_ids", off_loop("apps", task_store.get_apps_by_ids))
        monkeypatch.setattr(notification_store, "resolve_sub_to_display_name",
                            lambda sub: (_ for _ in ()).throw(AssertionError("per-row name read")))
        monkeypatch.setattr(task_store, "get_app",
                            lambda app_id: (_ for _ in ()).throw(AssertionError("per-row app read")))
        monkeypatch.setattr(triggers_api, "trigger_store",
                            type("S", (), {"list_triggers": staticmethod(
                                off_loop("rows", trigger_store.list_triggers)),
                                "list_triggers_for_user_view": staticmethod(
                                    off_loop("rows", trigger_store.list_triggers_for_user_view)),
                                "get_trigger": staticmethod(trigger_store.get_trigger)})())
        out = asyncio.run(list_triggers_endpoint(agent=None, scope=None, audit=True, user=_admin()))
        assert len(out["triggers"]) >= 2
        assert sorted(batches) == ["apps", "names", "rows", "usernames"]
