"""Per-scheduled-task model + engine override.

A schedule fires long after its creating session is gone, so unlike a
delegate's in-memory pin the choice has to PERSIST. Covers the whole round
trip: request → envelope validation → ``dynamic_tasks`` columns →
``_row_to_task`` → the config the run is actually built with → the chat row
billing reads from → the listing the dashboard renders.

Effort is deliberately NOT overridable — there is no override path for it
anywhere in the stack — so nothing here asserts one.

Run: cd proxy && python -m pytest tests/tasks/test_task_model_override.py -v
"""

import sys
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.providers import UserContext, get_current_user  # noqa: E402
from services.scheduler import scheduler  # noqa: E402
from storage.agents import agent_store  # noqa: E402
from storage import database as task_store  # noqa: E402


AGENT = "briefer"
# Enabled on the agent's layer in the stubbed registry below; the agent's own
# default stays MODEL_DEFAULT so "pinned" and "inherited" are distinguishable.
MODEL_DEFAULT = "claude-sonnet-5"
MODEL_STRONG = "claude-opus-5-5"


def _admin():
    return UserContext(
        sub="user-admin", email="admin@test.com", name="Admin User",
        role="admin", agents=[AGENT], agent_roles={AGENT: "manager"},
    )


@pytest.fixture
def client(temp_db, monkeypatch):
    """Tasks router + an agent with two enabled layers and a known model set."""
    from api.tasks import tasks as tasks_api
    from storage.billing import subscription_store

    agent_store.create_agent(AGENT, "Briefer", collaborative=True,
                             default_scope="user")
    agent_store.update_agent(
        AGENT,
        execution_path="claude-code-cli",
        execution_paths='["claude-code-cli", "direct-llm"]',
        default_model=MODEL_DEFAULT,
    )

    def _list_models(layer=None, **kw):
        if layer == "direct-llm":
            return [{"model_id": "hosted-mini", "enabled": True}]
        return [
            {"model_id": MODEL_DEFAULT, "enabled": True},
            {"model_id": MODEL_STRONG, "enabled": True},
            {"model_id": "claude-haiku-4-5", "enabled": False},
        ]

    monkeypatch.setattr(subscription_store, "list_models", _list_models)

    # Registration touches APScheduler; the row is what these tests care about.
    monkeypatch.setattr(scheduler, "_register_task", lambda task: None)

    app = FastAPI()
    app.include_router(tasks_api.router)

    async def _current_user():
        return _admin()

    app.dependency_overrides[get_current_user] = _current_user
    return TestClient(app)


def _create_scheduled(client, **overrides):
    body = {
        "name": "Daily briefing", "agent": AGENT, "prompt": "brief me",
        "schedule": "0 7 * * *", "scope": "user", "notification_mode": "none",
    }
    body.update(overrides)
    return client.post("/v1/tasks/scheduled", json=body)


def _create_one_time(client, **overrides):
    body = {
        "name": "One shot", "agent": AGENT, "prompt": "do it",
        "delay_seconds": 600, "scope": "user", "notification_mode": "none",
    }
    body.update(overrides)
    return client.post("/v1/tasks/one-time", json=body)


# ───────────────────────────────────────────────────────────────────────────
# Persistence — the half that did not exist before
# ───────────────────────────────────────────────────────────────────────────


class TestPersistence:
    def test_scheduled_task_persists_both_pins(self, client):
        r = _create_scheduled(client, model=MODEL_STRONG, layer="claude-code-cli")
        assert r.status_code == 200
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["override_model"] == MODEL_STRONG
        assert row["override_execution_path"] == "claude-code-cli"

    def test_one_time_task_persists_both_pins(self, client):
        r = _create_one_time(client, model=MODEL_STRONG, layer="claude-code-cli")
        assert r.status_code == 200
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["override_model"] == MODEL_STRONG
        assert row["override_execution_path"] == "claude-code-cli"

    def test_omitted_means_inherit(self, client):
        r = _create_scheduled(client)
        assert r.status_code == 200
        row = task_store.get_dynamic_task(r.json()["task_id"])
        # "" not NULL: every row spells inherit the same way.
        assert row["override_model"] == ""
        assert row["override_execution_path"] == ""

    def test_row_to_task_restores_pins(self, client):
        """The DB→runtime hop all 14 fire paths share."""
        r = _create_scheduled(client, model=MODEL_STRONG)
        row = task_store.get_dynamic_task(r.json()["task_id"])
        task = scheduler._row_to_task(row)
        assert task.override_model == MODEL_STRONG
        # Unset stays None (not "") so `override or default` reads uniformly.
        assert task.override_execution_path is None

    def test_row_to_task_normalises_empty_to_none(self, client):
        r = _create_scheduled(client)
        task = scheduler._row_to_task(
            task_store.get_dynamic_task(r.json()["task_id"]))
        assert task.override_model is None
        assert task.override_execution_path is None


# ───────────────────────────────────────────────────────────────────────────
# Validation — same envelope as delegate spawns
# ───────────────────────────────────────────────────────────────────────────


class TestValidation:
    def test_layer_outside_agent_envelope_400(self, client):
        r = _create_scheduled(client, layer="codex-cli")
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"]

    def test_model_foreign_to_layer_400(self, client):
        # hosted-mini is enabled on direct-llm, not on the agent's primary.
        r = _create_scheduled(client, model="hosted-mini")
        assert r.status_code == 400
        assert "not available" in r.json()["detail"]

    def test_model_valid_on_explicitly_chosen_layer(self, client):
        r = _create_scheduled(client, model="hosted-mini", layer="direct-llm")
        assert r.status_code == 200
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["override_execution_path"] == "direct-llm"

    def test_disabled_model_rejected(self, client):
        """Enabled-only, matching what the prompt roster advertises."""
        r = _create_scheduled(client, model="claude-haiku-4-5")
        assert r.status_code == 400

    def test_one_time_validates_too(self, client):
        assert _create_one_time(client, layer="codex-cli").status_code == 400

    def test_rejected_task_is_not_created(self, client):
        _create_scheduled(client, layer="codex-cli")
        assert task_store.list_dynamic_tasks(enabled_only=False) == []


# ───────────────────────────────────────────────────────────────────────────
# edit_task — retune or clear a LIVE schedule
# ───────────────────────────────────────────────────────────────────────────


class TestEdit:
    def test_editable_columns_whitelist_contains_both(self):
        """Missing here = the update silently no-ops (helper drops unknowns)."""
        from storage.automation.db_tasks import _EDITABLE_TASK_COLUMNS
        assert "override_model" in _EDITABLE_TASK_COLUMNS
        assert "override_execution_path" in _EDITABLE_TASK_COLUMNS

    def test_edit_pins_an_existing_task(self, client):
        tid = _create_scheduled(client).json()["task_id"]
        r = client.post(f"/v1/tasks/{tid}/edit", json={"model": MODEL_STRONG})
        assert r.status_code == 200
        assert task_store.get_dynamic_task(tid)["override_model"] == MODEL_STRONG

    def test_edit_clears_a_pin(self, client):
        tid = _create_scheduled(client, model=MODEL_STRONG).json()["task_id"]
        r = client.post(f"/v1/tasks/{tid}/edit", json={"model": ""})
        assert r.status_code == 200
        assert task_store.get_dynamic_task(tid)["override_model"] == ""

    def test_edit_validates_against_the_tasks_own_layer(self, client):
        """A model-only edit is checked against the layer the task already
        pins — not the agent's primary one."""
        tid = _create_scheduled(client, layer="direct-llm",
                                model="hosted-mini").json()["task_id"]
        # Valid on the agent's primary layer, NOT on this task's direct-llm.
        r = client.post(f"/v1/tasks/{tid}/edit", json={"model": MODEL_STRONG})
        assert r.status_code == 400
        assert task_store.get_dynamic_task(tid)["override_model"] == "hosted-mini"

    def test_edit_rejects_bad_layer(self, client):
        tid = _create_scheduled(client).json()["task_id"]
        assert client.post(f"/v1/tasks/{tid}/edit",
                           json={"layer": "codex-cli"}).status_code == 400

    def test_a_scheduled_fire_runs_the_edited_definition(self, client, monkeypatch):
        """An edit that leaves the timing alone does not re-register the
        job, which holds the definition it was registered with: the fire
        reads the row again."""
        import asyncio
        from services.scheduler import runner
        tid = _create_scheduled(client).json()["task_id"]
        registered = scheduler._row_to_task(task_store.get_dynamic_task(tid))
        assert client.post(f"/v1/tasks/{tid}/edit", json={
            "prompt": "brief me on the new thing", "model": MODEL_STRONG,
        }).status_code == 200
        fired: list = []

        async def _capture(task, trigger_type="scheduled", **kw):
            fired.append(task)
            return "run-x"
        monkeypatch.setattr(runner, "_execute_task", _capture)

        async def _fire():
            await runner._fire_task(registered)
            await asyncio.sleep(0)
        asyncio.run(_fire())
        assert fired[0].prompt == "brief me on the new thing"
        assert fired[0].override_model == MODEL_STRONG

    def test_edit_leaves_pins_alone_when_omitted(self, client):
        tid = _create_scheduled(client, model=MODEL_STRONG).json()["task_id"]
        r = client.post(f"/v1/tasks/{tid}/edit", json={"name": "Renamed"})
        assert r.status_code == 200
        row = task_store.get_dynamic_task(tid)
        assert row["name"] == "Renamed"
        assert row["override_model"] == MODEL_STRONG


# ───────────────────────────────────────────────────────────────────────────
# The pin actually reaches the run
# ───────────────────────────────────────────────────────────────────────────


class TestConsumption:
    def test_chat_row_is_stamped_with_the_override(self, client):
        """Billing reads the model off the chat row (stream_pump →
        usage_records), so the default here would mis-attribute the spend."""
        tid = _create_scheduled(client, model=MODEL_STRONG,
                                layer="claude-code-cli").json()["task_id"]
        task = scheduler._row_to_task(task_store.get_dynamic_task(tid))
        task_store.create_run("run-ov1", tid, AGENT, "scheduled", None, "x")
        chat_id = f"task-run-ov1-{uuid.uuid4().hex[:6]}"
        scheduler._create_task_chat_row(chat_id, "run-ov1", task)
        chat = task_store.get_chat(chat_id)
        assert chat["model"] == MODEL_STRONG
        assert chat["execution_path"] == "claude-code-cli"

    def test_chat_row_falls_back_to_agent_default(self, client):
        tid = _create_scheduled(client).json()["task_id"]
        task = scheduler._row_to_task(task_store.get_dynamic_task(tid))
        task_store.create_run("run-ov2", tid, AGENT, "scheduled", None, "x")
        chat_id = f"task-run-ov2-{uuid.uuid4().hex[:6]}"
        scheduler._create_task_chat_row(chat_id, "run-ov2", task)
        chat = task_store.get_chat(chat_id)
        assert chat["model"] == MODEL_DEFAULT
        assert chat["execution_path"] == ""


# ───────────────────────────────────────────────────────────────────────────
# GET /v1/tasks — what both schedules pages render
# ───────────────────────────────────────────────────────────────────────────


class TestListing:
    def test_effective_model_is_the_override(self, client):
        _create_scheduled(client, model=MODEL_STRONG)
        t = client.get("/v1/tasks").json()["tasks"][0]
        assert t["effective_model"] == MODEL_STRONG
        assert t["override_model"] == MODEL_STRONG

    def test_effective_model_falls_back_to_agent_default(self, client):
        _create_scheduled(client)
        t = client.get("/v1/tasks").json()["tasks"][0]
        assert t["effective_model"] == MODEL_DEFAULT
        # The raw pin stays empty — that's what dims the chip.
        assert not t["override_model"]

    def test_effective_execution_path(self, client):
        _create_scheduled(client, layer="direct-llm", model="hosted-mini")
        t = client.get("/v1/tasks").json()["tasks"][0]
        assert t["effective_execution_path"] == "direct-llm"
        assert t["override_execution_path"] == "direct-llm"

    def test_unresolvable_default_is_empty_not_an_error(self, client, monkeypatch):
        """resolve_agent_model raises on an install with nothing enabled — the
        listing must still render, with no model claim at all."""
        import config as app_config
        _create_scheduled(client)

        def _boom(agent, layer=None):
            raise RuntimeError("no enabled model")

        monkeypatch.setattr(app_config, "resolve_agent_model", _boom)
        r = client.get("/v1/tasks")
        assert r.status_code == 200
        assert r.json()["tasks"][0]["effective_model"] == ""

    def test_layer_pin_without_model_resolves_the_layer_default(self, client):
        """The runner resolves the default WITH the pinned engine: an agent
        default the engine does not serve is skipped for the engine's own
        first choice, and the listing must say the same."""
        _create_scheduled(client, layer="direct-llm")
        t = client.get("/v1/tasks").json()["tasks"][0]
        assert t["effective_execution_path"] == "direct-llm"
        assert t["effective_model"] == "hosted-mini"
        assert t["effective_model_source"] == "layer default"

    def test_app_handler_rows_claim_no_model(self, client):
        task_store.create_dynamic_task(
            "app-kanban-republish", AGENT, "Kanban: republish", "", "cli", "app",
            "0 6 * * 1-5", None, None, 600, "user-admin", scope="agent",
            app_id="app-1", app_handler="republish",
        )
        rows = {t["id"]: t for t in client.get("/v1/tasks").json()["tasks"]}
        row = rows["app-kanban-republish"]
        assert row["effective_model"] == "" and row["effective_model_tier"] is None
        one = client.get("/v1/tasks/app-kanban-republish").json()
        assert one["effective_model"] == "" and one["triggers"] == []

    def test_tier_and_source_ride_along(self, client):
        _create_scheduled(client, model=MODEL_STRONG)
        t = client.get("/v1/tasks").json()["tasks"][0]
        assert t["effective_model_source"] == "pinned"
        assert t["effective_model_tier"] == 2 and t["tier_label"] == "strong"
        assert t["pin_warnings"] == []


# ───────────────────────────────────────────────────────────────────────────
# GET /v1/tasks/{task_id} — the full definition an agent reads back
# ───────────────────────────────────────────────────────────────────────────


def _set_row(task_id: str, **cols):
    from storage.database import get_conn
    sets = ", ".join(f"{k} = %s" for k in cols)
    with get_conn() as conn:
        conn.execute(f"UPDATE dynamic_tasks SET {sets} WHERE id = %s",
                     [*cols.values(), task_id])
        conn.commit()


class TestGetTask:
    def test_returns_the_definition_with_prompt_and_model(self, client):
        r = _create_scheduled(client, model=MODEL_STRONG)
        tid = r.json()["task_id"]
        t = client.get(f"/v1/tasks/{tid}").json()
        assert t["id"] == tid and t["prompt"] == "brief me"
        assert t["schedule"] == "0 7 * * *" and t["notification_mode"] == "none"
        assert t["effective_model"] == MODEL_STRONG
        assert t["effective_model_source"] == "pinned"
        assert t["run_count"] == 0 and t["fired"] is False
        assert t["triggers"] == [] and t["pin_warnings"] == []
        assert t["can_run"] is True

    def test_pin_warnings_name_a_disabled_model_and_a_dropped_engine(self, client, monkeypatch):
        from storage.billing import subscription_store
        r = _create_scheduled(client, model="hosted-mini", layer="direct-llm")
        assert r.status_code == 200, r.text
        tid = r.json()["task_id"]
        # After the fact the admin disabled the model and removed the engine.
        monkeypatch.setattr(subscription_store, "list_models",
                            lambda layer=None, **kw: [{"model_id": "hosted-mini", "enabled": False}])
        agent_store.update_agent(AGENT, execution_paths='["claude-code-cli"]')
        t = client.get(f"/v1/tasks/{tid}").json()
        assert any("disabled" in w for w in t["pin_warnings"])
        assert any("not enabled for this agent" in w for w in t["pin_warnings"])

    def test_missing_foreign_and_delegate_rows_are_404(self, client):
        assert client.get("/v1/tasks/dyn-nope").status_code == 404
        tid = _create_scheduled(client).json()["task_id"]
        _set_row(tid, created_by="someone-else")
        assert client.get(f"/v1/tasks/{tid}").status_code == 404
        # The admin audit view reads it, as the listing's audit view does.
        assert client.get(f"/v1/tasks/{tid}?audit=true").status_code == 200
        tid2 = _create_scheduled(client).json()["task_id"]
        _set_row(tid2, use_persistent=True)
        assert client.get(f"/v1/tasks/{tid2}").status_code == 404

    def test_trigger_type_row_lists_the_triggers_pointing_at_it(self, client):
        from storage.automation import trigger_store
        r = _create_one_time(client, task_type="trigger", delay_seconds=None)
        assert r.status_code == 200, r.text
        tid = r.json()["task_id"]
        trigger_store.create_trigger(
            slug="on-push", name="On push", scope="user", agent=AGENT,
            created_by="user-admin", task_id=tid, subscription_id=None,
        )
        t = client.get(f"/v1/tasks/{tid}").json()
        assert t["task_type"] == "trigger"
        (trig,) = t["triggers"]
        assert trig["name"] == "On push" and trig["slug"] == "on-push"
        assert trig["scope"] == "user" and trig["enabled"] is True
        assert trig["fired_count"] == 0

    def test_static_task_routes_still_win(self, client):
        # /v1/tasks/runs and /v1/tasks/stats are registered before the
        # parameterised route and must keep matching themselves.
        assert client.get("/v1/tasks/runs").status_code == 200
        assert client.get("/v1/tasks/stats").status_code == 200


class TestFireOfAGoneRow:
    def test_a_fire_of_a_deleted_or_paused_row_removes_the_job_and_runs_nothing(
            self, client, monkeypatch):
        """A row deleted in plain SQL (a removal cascade) or paused fires
        nothing: the job goes with it. Only a read that FAILED fires the
        registered definition."""
        import asyncio
        from services.scheduler import runner
        gone = scheduler._row_to_task(task_store.get_dynamic_task(
            _create_scheduled(client).json()["task_id"]))
        paused = scheduler._row_to_task(task_store.get_dynamic_task(
            _create_scheduled(client).json()["task_id"]))
        for t in (gone, paused):
            scheduler._register_task(t)
        task_store.delete_dynamic_task(gone.id)
        task_store.set_dynamic_task_enabled(paused.id, False)
        fired: list = []

        async def _capture(task, trigger_type="scheduled", **kw):
            fired.append(task.id)
            return "run-x"
        monkeypatch.setattr(runner, "_execute_task", _capture)

        async def _fire():
            await runner._fire_task(gone)
            await runner._fire_task(paused)
            await asyncio.sleep(0)
        asyncio.run(_fire())
        assert fired == []
        assert scheduler._scheduler.get_job(f"task_{gone.id}") is None
        assert scheduler._scheduler.get_job(f"task_{paused.id}") is None

        # A read that raised fires as registered (the job's own definition).
        def _boom(task_id):
            raise RuntimeError("db down")
        monkeypatch.setattr(task_store, "get_dynamic_task", _boom)
        asyncio.run(_fire())
        assert fired == [gone.id, paused.id]
