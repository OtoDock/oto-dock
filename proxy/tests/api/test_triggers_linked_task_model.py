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
