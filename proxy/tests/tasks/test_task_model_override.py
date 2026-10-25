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

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.execution_layer import ResumeVerdict
from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config
from auth.providers import UserContext, get_current_user
from services.scheduler import scheduler
from storage import database as task_store
from storage.agents import agent_store
from storage.pg import get_conn

AGENT = "briefer"
# Enabled on the agent's layer in the stubbed registry below; the agent's own
# default stays MODEL_DEFAULT so "pinned" and "inherited" are distinguishable.
MODEL_DEFAULT = "claude-sonnet-5-5"
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


    def test_a_pin_written_after_the_boot_walk_runs_on_its_successor(self, client):
        """The walk rewrites pins at boot only; a retired id stored later (an
        old MCP call, a restore) runs on its successor, and the chat row and
        the session name the same model (one rule, so billing agrees)."""
        tid = _create_scheduled(client, model=MODEL_STRONG).json()["task_id"]
        with get_conn() as conn:
            conn.execute("UPDATE dynamic_tasks SET override_model = 'claude-opus-5' WHERE id = %s",
                         (tid,))
            conn.commit()
        task = scheduler._row_to_task(task_store.get_dynamic_task(tid))
        assert task.override_model == "claude-opus-5"
        task_store.create_run("run-ov3", tid, AGENT, "scheduled", None, "x")
        chat_id = f"task-run-ov3-{uuid.uuid4().hex[:6]}"
        scheduler._create_task_chat_row(chat_id, "run-ov3", task)
        assert task_store.get_chat(chat_id)["model"] == MODEL_STRONG
        assert config.resolve_run_model(AGENT, task.override_model,
                                        layer="claude-code-cli") == MODEL_STRONG
        listed = client.get("/v1/tasks").json()["tasks"][0]
        assert listed["effective_model"] == MODEL_STRONG and not listed["pin_warnings"]


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


# ───────────────────────────────────────────────────────────────────────────
# The task's permission mode: Don't Ask (the default) or Auto
# ───────────────────────────────────────────────────────────────────────────


class TestPermissionMode:
    def test_auto_round_trips_from_the_request_to_the_run(self, client):
        from core.permission_modes import task_run_mode
        r = _create_scheduled(client, permission_mode="autoReview")
        assert r.status_code == 200, r.text
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["permission_mode"] == "autoReview"
        task = scheduler._row_to_task(row)
        assert task.permission_mode == "autoReview"
        assert task_run_mode(task) == "autoReview"
        assert client.get("/v1/tasks").json()["tasks"][0]["permission_mode"] == "autoReview"

    @pytest.mark.parametrize("extra", [{}, {"permission_mode": "dontAsk"}])
    def test_dont_ask_is_the_default_and_runs_unattended(self, client, extra):
        from core.permission_modes import task_run_mode
        r = _create_one_time(client, **extra)
        assert r.status_code == 200, r.text
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["permission_mode"] == ""          # every row spells Don't Ask the same way
        assert task_run_mode(scheduler._row_to_task(row)) == "auto"
        assert client.get("/v1/tasks").json()["tasks"][0]["permission_mode"] == "dontAsk"

    def test_an_engine_with_no_review_of_its_own_refuses_auto(self, client):
        r = _create_scheduled(client, layer="direct-llm", model="hosted-mini",
                              permission_mode="autoReview")
        assert r.status_code == 400
        assert "Auto needs an engine with its own review" in r.json()["detail"]
        assert client.get("/v1/tasks").json()["tasks"] == []

    def test_only_the_two_task_modes_are_accepted(self, client):
        for mode in ("ask", "plan", "auto"):
            assert _create_scheduled(client, permission_mode=mode).status_code == 422, mode

    def test_an_edit_moves_the_mode_both_ways(self, client):
        tid = _create_scheduled(client).json()["task_id"]
        assert client.post(f"/v1/tasks/{tid}/edit", json={"permission_mode": "autoReview"}).status_code == 200
        assert task_store.get_dynamic_task(tid)["permission_mode"] == "autoReview"
        assert client.post(f"/v1/tasks/{tid}/edit", json={"name": "Renamed"}).status_code == 200
        assert task_store.get_dynamic_task(tid)["permission_mode"] == "autoReview"
        assert client.post(f"/v1/tasks/{tid}/edit", json={"permission_mode": "dontAsk"}).status_code == 200
        assert task_store.get_dynamic_task(tid)["permission_mode"] == ""
        # The task's own engine decides: pinned to Direct LLM, Auto is refused.
        assert client.post(f"/v1/tasks/{tid}/edit", json={
            "layer": "direct-llm", "model": "hosted-mini"}).status_code == 200
        r = client.post(f"/v1/tasks/{tid}/edit", json={"permission_mode": "autoReview"})
        assert r.status_code == 400

    def test_moving_an_auto_task_to_an_engine_without_review_is_refused(self, client):
        tid = _create_scheduled(client, permission_mode="autoReview").json()["task_id"]
        r = client.post(f"/v1/tasks/{tid}/edit", json={"layer": "direct-llm", "model": "hosted-mini"})
        assert r.status_code == 400
        row = task_store.get_dynamic_task(tid)
        assert (row["override_execution_path"], row["permission_mode"]) == ("", "autoReview")
        # Back to Don't Ask in the same edit: allowed.
        r = client.post(f"/v1/tasks/{tid}/edit", json={
            "layer": "direct-llm", "model": "hosted-mini", "permission_mode": "dontAsk"})
        assert r.status_code == 200
        assert task_store.get_dynamic_task(tid)["permission_mode"] == ""


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


class TestContinuedLaneModelChange:
    """A continue round that changed the worker's model
    (``respawn_for_model``, set by the delegation route) closes the chat's
    live session after its last lane gate, under the holding layer's session
    lock, and never rides the warm session — so the round resumes the
    conversation on the new model. A plain continue round keeps today's path."""

    SID = "44444444-4444-4444-8444-444444444444"
    CHAT = "worker-model-change"

    def _drive(self, monkeypatch, *, respawn: bool) -> dict:
        import asyncio
        import contextlib
        from types import SimpleNamespace

        from core import concurrency
        from core.config import task_config_builder
        from core.session import session_manager, session_state
        from services.remote import remote_status
        from services.scheduler import lanes, runner, shared

        task_store.create_chat(self.CHAT, "user-admin", AGENT, "auto",
                               origin="delegated", model=MODEL_STRONG,
                               execution_path="claude-code-cli")
        task_store.update_chat(self.CHAT, session_id=self.SID)
        events: list = []
        seen: dict = {"events": events, "reuse": 0, "acquire": []}

        class _Holder:
            """The layer holding the chat's live session."""

            @contextlib.asynccontextmanager
            async def session_lock(self, sid):
                events.append(("lock", sid))
                yield
                events.append(("unlock", sid))

            async def close_session(self, sid):
                events.append(("close", sid))

        holder = _Holder()
        monkeypatch.setattr(session_manager, "find_layer_for_session",
                            lambda sid: holder if sid == self.SID else None)

        class _Stop(Exception):
            pass

        class _RoundLayer:
            async def can_resume_session(self, sid, **kw):
                return ResumeVerdict.RESUMABLE

            async def start_session(self, sid, cfg):
                events.append(("start", sid, cfg.resume, cfg.model))
                raise _Stop("the test ends the round at the spawn")

            async def close_session(self, sid):
                events.append(("failure-close", sid))

        cfg = SimpleNamespace(execution_target="local", execution_path="claude-code-cli",
                              interactive=False, resume=False, extra_env={}, chat_id="",
                              subscription_id="", model=MODEL_STRONG)

        async def _cfg(agent, task_def, sid, **kw):
            return cfg

        async def _reuse(*a, **k):
            seen["reuse"] += 1
            return False

        async def _acquire(sid, kind, **kw):
            seen["acquire"].append((sid, kind, kw.get("blocking")))

        @contextlib.asynccontextmanager
        async def _instant_slot(session_id, target="", execution_path=None, ring_key=""):
            yield

        monkeypatch.setattr(concurrency, "task_slot", _instant_slot)
        monkeypatch.setattr(concurrency, "acquire", _acquire)
        monkeypatch.setattr(task_config_builder, "build_task_agent_config", _cfg)
        monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                            lambda *a, **k: task_config_builder.TaskIdentity(
                                "", "manager", "agent", None))
        monkeypatch.setattr(session_manager, "get_execution_layer",
                            lambda *a, **k: _RoundLayer())
        monkeypatch.setattr(remote_status, "resolve_execution_target",
                            lambda *a, **k: ("local", None))
        monkeypatch.setattr(lanes, "_try_reuse_warm_session", _reuse)
        monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

        task = shared.TaskDefinition(
            id="t-model-change", name="follow", agent=AGENT, prompt="go on",
            scope="agent", notification_mode="none", continue_session=self.SID,
            target_chat_id=self.CHAT, override_model=MODEL_STRONG,
            override_execution_path="claude-code-cli", respawn_for_model=respawn,
        )
        run_id = "run-model-change"
        task_store.create_run(run_id, task.id, task.agent, "manual", None, task.prompt,
                              "delegate", task.scope, None)
        shared._active_task_ids[task.id] = run_id
        try:
            asyncio.run(asyncio.wait_for(
                runner._run_task(run_id, self.SID, task, task.prompt, "manual", None, 1),
                timeout=30))
        finally:
            session_state._sessions.pop(self.SID, None)
            shared._active_task_ids.pop(task.id, None)
        return seen

    def test_a_model_change_round_closes_the_live_session_first(self, temp_db, monkeypatch):
        seen = self._drive(monkeypatch, respawn=True)
        # Closed under the holder's session lock, before the round's spawn,
        # which resumes the conversation on the new model.
        assert seen["events"][:4] == [
            ("lock", self.SID), ("close", self.SID), ("unlock", self.SID),
            ("start", self.SID, True, MODEL_STRONG),
        ]
        # Never the warm session; the close's slot release is taken back.
        assert seen["reuse"] == 0
        assert seen["acquire"] == [(self.SID, "task", True)]

    def test_a_plain_continue_round_keeps_the_live_session(self, temp_db, monkeypatch):
        seen = self._drive(monkeypatch, respawn=False)
        assert ("close", self.SID) not in seen["events"]
        assert seen["events"][0] == ("start", self.SID, True, MODEL_STRONG)
        assert seen["reuse"] == 1
        assert seen["acquire"] == []


class TestAFreshContinueRound:
    """A continue round whose conversation is gone (the resume probe answers
    FRESH) takes a new session id, and everything built for the old one is
    built again: the MCP config, ``OTO_SESSION_ID``, the meetings MCP's
    session. The first build's seat goes back once, the new build's is the
    one the failure path returns."""

    SID = "55555555-5555-4555-8555-555555555555"
    CHAT = "worker-fresh-round"

    def _drive(self, monkeypatch, *, second_build_fails: bool = False,
               verdict: ResumeVerdict = ResumeVerdict.FRESH) -> dict:
        import asyncio
        import contextlib
        from types import SimpleNamespace

        from core import concurrency
        from core.config import config_builder, task_config_builder
        from core.session import session_manager, session_state
        from services.remote import remote_status
        from services.scheduler import lanes, runner, shared

        task_store.create_chat(self.CHAT, "user-admin", AGENT, "auto",
                               origin="delegated", model=MODEL_STRONG,
                               execution_path="claude-code-cli")
        task_store.update_chat(self.CHAT, session_id=self.SID)
        seen: dict = {"builds": [], "starts": [], "released": []}

        class _Stop(Exception):
            pass

        class _RoundLayer:
            async def can_resume_session(self, sid, **kw):
                return verdict

            async def start_session(self, sid, cfg):
                seen["starts"].append((sid, cfg.extra_env.get("MEETINGS_MCP_SESSION_ID"),
                                       cfg.credential_env["OTO_SESSION_ID"], cfg.mcp_config_path))
                seen["resume_at_start"] = cfg.resume
                seen["released_before_start"] = list(seen["released"])
                raise _Stop("the test ends the round at the spawn")

            async def close_session(self, sid):
                pass

        async def _cfg(agent, task_def, sid, **kw):
            if second_build_fails and seen["builds"]:
                seen["builds"].append(sid)
                raise RuntimeError("the second build failed")
            seen["builds"].append(sid)
            return SimpleNamespace(
                execution_target="local", execution_path="claude-code-cli", interactive=False,
                resume=False, extra_env={}, chat_id="", subscription_id="sub-1", model=MODEL_STRONG,
                credential_env={"OTO_SESSION_ID": sid}, mcp_config_path=f"/configs/{sid}.json")

        @contextlib.asynccontextmanager
        async def _instant_slot(session_id, target="", execution_path=None, ring_key=""):
            yield

        async def _reuse(*a, **k):
            return False

        monkeypatch.setattr(concurrency, "task_slot", _instant_slot)
        monkeypatch.setattr(task_config_builder, "build_task_agent_config", _cfg)
        monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                            lambda *a, **k: task_config_builder.TaskIdentity(
                                "", "manager", "agent", None))
        monkeypatch.setattr(session_manager, "get_execution_layer",
                            lambda *a, **k: _RoundLayer())
        monkeypatch.setattr(remote_status, "resolve_execution_target",
                            lambda *a, **k: ("local", None))
        monkeypatch.setattr(lanes, "_try_reuse_warm_session", _reuse)
        monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
        monkeypatch.setattr(config_builder, "release_config_seat",
                            lambda sid, cfg, **kw: seen["released"].append((sid, cfg.credential_env["OTO_SESSION_ID"])))

        task = shared.TaskDefinition(
            id="t-fresh-round", name="follow", agent=AGENT, prompt="go on",
            scope="agent", notification_mode="none", continue_session=self.SID,
            target_chat_id=self.CHAT,
        )
        run_id = "run-fresh-round"
        task_store.create_run(run_id, task.id, task.agent, "manual", None, task.prompt,
                              "delegate", task.scope, None)
        shared._active_task_ids[task.id] = run_id
        try:
            asyncio.run(asyncio.wait_for(
                runner._run_task(run_id, self.SID, task, task.prompt, "manual", None, 1),
                timeout=30))
        finally:
            for sid in seen["builds"]:
                session_state._sessions.pop(sid, None)
            shared._active_task_ids.pop(task.id, None)
        return seen

    def test_the_fresh_session_runs_on_a_config_built_for_it(self, temp_db, monkeypatch):
        seen = self._drive(monkeypatch)
        first, second = seen["builds"]
        assert first == self.SID and second != self.SID
        assert seen["starts"] == [(second, second, second, f"/configs/{second}.json")]
        # The first build's seat once, the second's on the failure path.
        assert seen["released"] == [(self.SID, self.SID), (second, second)]
        assert task_store.get_chat(self.CHAT)["session_id"] == second

    def test_a_resumable_round_keeps_the_config_it_built(self, temp_db, monkeypatch):
        """No FRESH verdict: one build, the session resumed on it, no seat
        returned before the start."""
        seen = self._drive(monkeypatch, verdict=ResumeVerdict.RESUMABLE)
        assert seen["builds"] == [self.SID]
        assert seen["starts"] == [(self.SID, self.SID, self.SID, f"/configs/{self.SID}.json")]
        assert seen["resume_at_start"] is True
        assert seen["released_before_start"] == []
        assert task_store.get_chat(self.CHAT)["session_id"] == self.SID

    def test_a_failed_rebuild_returns_no_seat_twice(self, temp_db, monkeypatch):
        seen = self._drive(monkeypatch, second_build_fails=True)
        assert seen["starts"] == []
        assert seen["released"] == [(self.SID, self.SID)]


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
            raise psycopg.OperationalError("db down")
        monkeypatch.setattr(task_store, "get_dynamic_task", _boom)
        asyncio.run(_fire())
        assert fired == [gone.id, paused.id]


# ───────────────────────────────────────────────────────────────────────────
# The task's machine (dynamic_tasks.target_machine): validated on write for
# the run person, shown in the views, decided again at every run
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def machines(client, monkeypatch):
    """Four paired machines, all online until a test takes one down: the
    viewer's own laptop, another member's laptop, an admin machine and the
    admin's own laptop. The viewer is an editor of the agent."""
    from services.remote import remote_status
    from storage import remote_store
    from storage.identity import db_users

    remote_store.create_remote_machine("m-own", "viewer-laptop", "user-viewer", pairing_scope="user")
    remote_store.create_remote_machine("m-other", "other-laptop", "user-viewer2", pairing_scope="user")
    remote_store.create_remote_machine("m-rack", "rack-1", "user-admin", pairing_scope="admin")
    remote_store.create_remote_machine("m-adm", "admin-laptop", "user-admin", pairing_scope="user")
    db_users.add_user_agent("user-viewer", AGENT, "editor", "user-admin")
    online = {"m-own", "m-other", "m-rack", "m-adm"}

    def _live(machine_id, machine=None):
        up = machine_id in online
        return {"state": "online" if up else "disconnected", "last_heartbeat_age_s": 1 if up else None,
                "last_seen_iso": "", "reachable": up}

    monkeypatch.setattr(remote_status, "get_live_machine_status", _live)
    return online


def _as(client, user):
    async def _current_user():
        return user
    client.app.dependency_overrides[get_current_user] = _current_user


def _viewer():
    return UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User",
                       role="member", agents=[AGENT], agent_roles={AGENT: "editor"})


def _edit(client, tid, **body):
    return client.post(f"/v1/tasks/{tid}/edit", json=body)


class TestTaskMachineOnWrite:
    def test_own_machine_is_stored_by_name_or_id(self, client, machines):
        _as(client, _viewer())
        r = _create_scheduled(client, machine="viewer-laptop")
        assert r.status_code == 200, r.text
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["target_machine"] == "m-own"
        assert scheduler._row_to_task(row).target_machine == "m-own"
        # By id, and offline now: the run falls back then, the write stands.
        machines.discard("m-own")
        r = _create_one_time(client, machine="m-own")
        assert r.status_code == 200, r.text
        assert task_store.get_dynamic_task(r.json()["task_id"])["target_machine"] == "m-own"

    def test_omitted_and_default_store_the_agents_default(self, client, machines):
        for extra in ({}, {"machine": "default"}, {"machine": ""}):
            r = _create_scheduled(client, **extra)
            assert r.status_code == 200, r.text
            assert task_store.get_dynamic_task(r.json()["task_id"])["target_machine"] == ""

    def test_someone_elses_machine_is_refused(self, client, machines):
        _as(client, _viewer())
        r = _create_one_time(client, machine="other-laptop")
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail.startswith("other-laptop is not a machine this work can run on.")
        assert "viewer-laptop" in detail and "rack-1" not in detail
        assert task_store.list_dynamic_tasks(enabled_only=False) == []

    def test_an_admin_machine_is_refused_for_a_non_admin(self, client, machines):
        _as(client, _viewer())
        assert _create_scheduled(client, machine="rack-1").status_code == 400

    def test_an_admin_picks_admin_machines_and_agent_work_never_runs_on_a_personal_one(
            self, client, machines):
        r = _create_scheduled(client, scope="agent", machine="rack-1")
        assert r.status_code == 200, r.text
        assert task_store.get_dynamic_task(r.json()["task_id"])["target_machine"] == "m-rack"
        # Agent-scope runs have no person: never a personal machine, the
        # admin's own included.
        assert _create_scheduled(client, scope="agent", machine="admin-laptop").status_code == 400
        r = _create_scheduled(client, machine="admin-laptop")
        assert r.status_code == 200, r.text
        assert task_store.get_dynamic_task(r.json()["task_id"])["target_machine"] == "m-adm"

    def test_a_caller_without_a_person_names_only_the_default(self, client, machines):
        from auth import roles
        _as(client, UserContext(sub="session:abc", email="", name="", role=roles.SERVICE,
                                is_api_key=True, agent=AGENT, session_id="abc"))
        r = _create_scheduled(client, scope="agent", machine="rack-1")
        assert r.status_code == 400
        assert r.json()["detail"].startswith("Only a person can pick a machine")
        r = _create_scheduled(client, scope="agent", machine="default")
        assert r.status_code == 200, r.text
        assert task_store.get_dynamic_task(r.json()["task_id"])["target_machine"] == ""
        _as(client, UserContext(sub="api-key", email="", name="", role=roles.ADMIN, is_api_key=True))
        r = client.post("/v1/tasks/scheduled", headers={"X-Agent-Name": AGENT}, json={
            "name": "n", "agent": AGENT, "prompt": "p", "schedule": "0 7 * * *",
            "scope": "agent", "notification_mode": "none", "machine": "rack-1"})
        assert r.status_code == 400

    def test_edit_sets_keeps_and_clears_the_machine(self, client, machines):
        _as(client, _viewer())
        tid = _create_scheduled(client).json()["task_id"]
        assert _edit(client, tid, machine="viewer-laptop").status_code == 200
        assert task_store.get_dynamic_task(tid)["target_machine"] == "m-own"
        assert _edit(client, tid, name="Renamed").status_code == 200
        assert task_store.get_dynamic_task(tid)["target_machine"] == "m-own"
        assert _edit(client, tid, machine="other-laptop").status_code == 400
        assert task_store.get_dynamic_task(tid)["target_machine"] == "m-own"
        assert _edit(client, tid, machine="default").status_code == 200
        assert task_store.get_dynamic_task(tid)["target_machine"] == ""
        assert _edit(client, tid, machine="local").status_code == 200
        assert task_store.get_dynamic_task(tid)["target_machine"] == "local"
        assert _edit(client, tid, machine="").status_code == 200
        assert task_store.get_dynamic_task(tid)["target_machine"] == ""

    def test_an_agent_task_edit_follows_the_writers_authority(self, client, machines):
        _as(client, _viewer())
        tid = _create_scheduled(client, scope="agent").json()["task_id"]
        assert _edit(client, tid, machine="rack-1").status_code == 400
        _as(client, _admin())
        assert _edit(client, tid, machine="rack-1").status_code == 200
        assert task_store.get_dynamic_task(tid)["target_machine"] == "m-rack"

    def test_unpairing_the_machine_returns_its_tasks_to_the_default(self, client, machines):
        from storage import remote_store
        _as(client, _viewer())
        tid = _create_scheduled(client, machine="viewer-laptop").json()["task_id"]
        keep = _create_scheduled(client, machine="local").json()["task_id"]
        assert remote_store.delete_remote_machine("m-own")
        assert task_store.get_dynamic_task(tid)["target_machine"] == ""
        assert task_store.get_dynamic_task(keep)["target_machine"] == "local"

    def test_a_task_bound_to_a_chat_takes_no_machine(self, client, machines):
        tid = _create_scheduled(client).json()["task_id"]
        _set_row(tid, target_chat_id="chat-bound")
        r = _edit(client, tid, machine="admin-laptop")
        assert r.status_code == 400
        assert r.json()["detail"] == "This task runs in its chat, which keeps its own machine."
        assert _edit(client, tid, machine="default").status_code == 200


class TestTaskMachineViews:
    def test_the_views_name_the_machine_and_its_live_state(self, client, machines):
        _as(client, _viewer())
        tid = _create_scheduled(client, machine="viewer-laptop").json()["task_id"]
        machines.discard("m-own")
        listed = {t["id"]: t for t in client.get("/v1/tasks").json()["tasks"]}
        one = client.get(f"/v1/tasks/{tid}").json()
        for t in (listed[tid], one):
            assert (t["target_machine"], t["target_machine_label"], t["target_machine_state"]) == (
                "m-own", "viewer-laptop", "disconnected")
        local = _create_scheduled(client, machine="local").json()["task_id"]
        plain = _create_scheduled(client).json()["task_id"]
        listed = {t["id"]: t for t in client.get("/v1/tasks").json()["tasks"]}
        assert (listed[local]["target_machine_label"], listed[local]["target_machine_state"]) == (
            "OtoDock platform", "")
        assert (listed[plain]["target_machine"], listed[plain]["target_machine_label"],
                listed[plain]["target_machine_state"]) == ("", "", "")

    def test_the_runs_list_names_where_each_run_ran(self, client, machines):
        tid = _create_scheduled(client).json()["task_id"]
        task_store.create_run("run-m1", tid, AGENT, "scheduled", None, "x")
        task_store.create_run("run-m2", tid, AGENT, "scheduled", None, "x")
        task_store.update_run("run-m2", execution_target="m-rack")
        runs = {r["id"]: r for r in client.get("/v1/tasks/runs", params={"task_id": tid}).json()["runs"]}
        assert runs["run-m1"]["execution_target"] == "local"
        assert runs["run-m1"]["execution_target_label"] == "OtoDock platform"
        assert runs["run-m2"]["execution_target"] == "m-rack"
        assert runs["run-m2"]["execution_target_label"] == "rack-1"


def _viewer_identity():
    from core.config.task_config_builder import TaskIdentity
    return TaskIdentity(username="viewer", role="editor", scope="user", creds_user_sub="user-viewer")


def _agent_identity():
    from core.config.task_config_builder import TaskIdentity
    return TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None)


class TestTaskMachineAtRunTime:
    """``task_config_builder.task_target``: a judge's machine the guard
    passes, then the task's machine while the guard passes it and it is
    reachable, then the resolver; and the run's record of the outcome."""

    @staticmethod
    def _task(**kw):
        from services.scheduler import shared
        return shared.TaskDefinition(id="dyn-m", name="t", agent=AGENT, prompt="p",
                                     notification_mode="none", **kw)

    @pytest.fixture
    def resolver(self, monkeypatch):
        from services.remote import remote_status
        monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *a, **k: ("local", None))

    def test_a_reachable_machine_the_guard_passes_takes_the_run(self, client, machines, resolver):
        from core.config import task_config_builder as tcb
        assert tcb.task_target(self._task(target_machine="m-own"), _viewer_identity()) == (
            "m-own", None, "")
        assert tcb.task_target(self._task(target_machine="local"), _viewer_identity()) == (
            "local", None, "")
        assert tcb.task_target(self._task(), _viewer_identity()) == ("local", None, "")
        # An admin machine runs a task with no person.
        assert tcb.task_target(self._task(target_machine="m-rack"), _agent_identity()).target == "m-rack"

    def test_an_offline_machine_gives_way_to_the_resolver(self, client, machines, resolver):
        from core.config import task_config_builder as tcb
        machines.discard("m-own")
        task = self._task(target_machine="m-own")
        assert tcb.task_target(task, _viewer_identity()) == (
            "local", tcb.FALLBACK_TASK_MACHINE, "viewer-laptop")
        assert tcb.task_machine_note(task, _viewer_identity(), "local") == (
            "viewer-laptop was offline, so this run used the OtoDock platform.")

    def test_a_machine_the_guard_refuses_gives_way_too(self, client, machines, resolver):
        from core.config import task_config_builder as tcb
        task = self._task(target_machine="m-own")
        # A run with no person never lands on a personal machine.
        assert tcb.task_target(task, _agent_identity()) == (
            "local", tcb.FALLBACK_TASK_MACHINE, "viewer-laptop")
        assert tcb.task_machine_note(task, _agent_identity(), "local") == (
            "viewer-laptop is not a machine this task may use now, so this run used the OtoDock platform.")
        # Nor does anyone's run while the platform turns personal machines off.
        task_store.set_platform_setting("allow_user_paired_machines", "0")
        assert tcb.task_target(task, _viewer_identity()).fallback_reason == tcb.FALLBACK_TASK_MACHINE

    def test_a_judges_machine_the_guard_refuses_falls_back_to_the_resolver(
            self, client, machines, resolver):
        from core.config import task_config_builder as tcb
        judge_task = self._task(execution_target_override="m-own", target_machine="m-rack")
        assert tcb.task_target(judge_task, _viewer_identity()) == ("m-own", None, "")
        # An agent-scope judge of a chat on a personal machine: the resolver.
        assert tcb.task_target(judge_task, _agent_identity()) == ("local", None, "")
        assert tcb.task_target(self._task(execution_target_override="local"),
                               _agent_identity()) == ("local", None, "")

    def test_the_run_records_where_it_ran_and_says_why_once(self, client, machines, monkeypatch):
        import asyncio
        import contextlib
        from types import SimpleNamespace

        from core import concurrency
        from core.config import config_builder, task_config_builder as tcb
        from core.session import session_manager, session_state
        from services.remote import remote_status
        from services.scheduler import runner, shared, task_run

        machines.discard("m-own")
        monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *a, **k: ("local", None))
        monkeypatch.setattr(tcb, "resolve_task_identity", lambda *a, **k: _viewer_identity())
        slots: list = []

        @contextlib.asynccontextmanager
        async def _slot(session_id, target="", execution_path=None, ring_key=""):
            slots.append(target)
            yield

        class _Stop(Exception):
            pass

        class _Layer:
            async def start_session(self, sid, cfg):
                raise _Stop("the test ends the run at the spawn")

            async def close_session(self, sid):
                pass

        async def _cfg(agent, task_def, sid, **kw):
            # The builder's own decision, as build_task_agent_config takes it.
            chosen = await asyncio.to_thread(tcb.task_target, task_def, _viewer_identity())
            return SimpleNamespace(
                execution_target=chosen.target, fallback_reason=chosen.fallback_reason,
                execution_path="claude-code-cli", interactive=False, resume=False, extra_env={},
                chat_id="", subscription_id="", model=MODEL_DEFAULT, credential_env={},
                mcp_config_path="")

        monkeypatch.setattr(concurrency, "task_slot", _slot)
        monkeypatch.setattr(tcb, "build_task_agent_config", _cfg)
        monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **k: _Layer())
        monkeypatch.setattr(config_builder, "release_config_seat", lambda *a, **k: None)
        monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

        task = shared.TaskDefinition(id="dyn-fb", name="fb", agent=AGENT, prompt="go",
                                     scope="user", created_by="user-viewer",
                                     notification_mode="none", target_machine="m-own")
        run_id, sid = "run-fb", "66666666-6666-4666-8666-666666666666"
        task_store.create_run(run_id, task.id, AGENT, "manual", None, task.prompt,
                              "one_time", "user", "user-viewer")
        shared._active_task_ids[task.id] = run_id
        try:
            asyncio.run(asyncio.wait_for(
                runner._run_task(run_id, sid, task, task.prompt, "manual", None, 1), timeout=30))
        finally:
            session_state._sessions.pop(sid, None)
            shared._active_task_ids.pop(task.id, None)
        assert slots == ["local"]          # the slot counted where the run went
        assert task_store.get_run(run_id)["execution_target"] == "local"
        chat_id = task_store.get_run(run_id)["chat_id"]
        import json as _json
        notes = [_json.loads(m["event_data"]) for m in task_store.get_chat_messages(chat_id)
                 if m.get("event_type") == "system"]
        assert [(n["subtype"], n["message"]) for n in notes] == [(
            "machine_not_answering", "viewer-laptop was offline, so this run used the OtoDock platform.")]

        # A second bind of the same run (a fresh continue round) writes the
        # record again and no second line.
        run = task_run.TaskRun(run_id, sid, task, task.prompt, "manual", None, 1, None,
                               _viewer_identity(), "local")
        run.chat_id = chat_id
        run.agent_cfg = SimpleNamespace(execution_target="m-rack",
                                        fallback_reason=tcb.FALLBACK_TASK_MACHINE)

        async def _twice():
            await run._record_target()
            await run._record_target()
        from core.events import chat_writer
        chat_writer.reset_for_tests()      # the lanes of the first loop are gone
        asyncio.run(_twice())
        assert task_store.get_run(run_id)["execution_target"] == "m-rack"
        rows = [m for m in task_store.get_chat_messages(chat_id) if m.get("event_type") == "system"]
        assert len(rows) == 2              # the first run's line, then one line for this run
