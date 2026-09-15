"""The scheduler's subscription pool cap gate (services/scheduler/runner.py
``_pool_cap_block``): a blocked run is recorded ``limit_exceeded`` with the
cap's reason, ``continue`` with a key proceeds, and the engine is the task's
override else the agent's.

Run: cd proxy && python -m pytest tests/tasks/test_pool_cap_gate.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from services.billing import pool_caps
from services.engines import subscription_pool
from services.scheduler import runner, scheduler
from storage import database as task_store
from storage.agents import agent_store


def _blocked(scope, layer, on_reached="stop"):
    s = pool_caps.CapStatus(scope=scope, target="u-1" if scope == "user" else "",
                            layer=layer, configured=True, accounts=1, allowed=False,
                            on_reached=on_reached, hits=["week_usd"])
    s.caps["week_usd"], s.readings["week_usd"] = 40.0, 41.2
    return s


def _task(**over):
    base = dict(id="task-cap", name="cap", agent="pa", prompt="p", scope="agent")
    base.update(over)
    return scheduler.TaskDefinition(**base)


class TestTaskLayer:
    def test_override_beats_the_agent_default(self, temp_db):
        agent_store.create_agent("pa", "PA", execution_path="codex-cli")
        assert runner._task_layer(_task()) == "codex-cli"
        assert runner._task_layer(_task(override_execution_path="claude-code-cli")) == "claude-code-cli"
        assert runner._task_layer(_task(agent="missing")) == "claude-code-cli"


class TestGate:
    def test_agent_scope_stop_records_limit_exceeded(self, temp_db, monkeypatch):
        agent_store.create_agent("pa", "PA")
        with patch.object(pool_caps, "evaluate",
                          return_value=_blocked("platform", "claude-code-cli")) as ev:
            run_id = asyncio.run(runner._execute_task(_task(), trigger_type="manual"))
        ev.assert_called_once_with("platform", "", "claude-code-cli")
        run = task_store.get_run(run_id)
        assert run["status"] == "limit_exceeded"
        assert run["error_message"] == "Subscription pool cap reached (the week is at $41.20 of the $40 cap)"

    def test_user_scope_reads_the_creators_pool(self, temp_db):
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        task_store.upsert_user("u-1", "u1@x.test", "U", "member")
        with patch.object(pool_caps, "evaluate",
                          return_value=_blocked("user", "claude-code-cli")) as ev:
            run_id = asyncio.run(runner._execute_task(
                _task(scope="user", created_by="u-1"), trigger_type="manual"))
        ev.assert_called_once_with("user", "u-1", "claude-code-cli")
        assert task_store.get_run(run_id)["status"] == "limit_exceeded"

    def test_a_shared_only_agent_reads_the_platform_pool(self, temp_db):
        # resolve_task_identity spawns every task on a shared-only agent with
        # agent-scope credentials whatever the run row stores, so the gate
        # must read the pool the spawn will draw on, not the creator's.
        from core.session.visibility import is_shared_only
        agent_store.create_agent("pa", "PA", collaborative=False, default_scope="agent")
        assert is_shared_only("pa")
        task_store.upsert_user("u-1", "u1@x.test", "U", "member")
        with patch.object(pool_caps, "evaluate",
                          return_value=pool_caps.CapStatus("platform", "", "claude-code-cli")) as ev:
            assert asyncio.run(runner._pool_cap_block(
                _task(scope="user", created_by="u-1"))) == ""
        ev.assert_called_once_with("platform", "", "claude-code-cli")

    def test_continue_with_a_key_proceeds_and_without_one_blocks(self, temp_db):
        agent_store.create_agent("pa", "PA")
        status = _blocked("platform", "claude-code-cli", on_reached="continue")
        with patch.object(pool_caps, "evaluate", return_value=status), \
             patch.object(subscription_pool, "cap_continue_available", return_value=True):
            assert asyncio.run(runner._pool_cap_block(_task())) == ""
        with patch.object(pool_caps, "evaluate", return_value=status), \
             patch.object(subscription_pool, "cap_continue_available", return_value=False):
            assert asyncio.run(runner._pool_cap_block(_task())).startswith(
                "Subscription pool cap reached")

    def test_allowed_and_creatorless_user_tasks_pass(self, temp_db):
        agent_store.create_agent("pa", "PA")
        with patch.object(pool_caps, "evaluate",
                          return_value=pool_caps.CapStatus("platform", "", "claude-code-cli")):
            assert asyncio.run(runner._pool_cap_block(_task())) == ""
        with patch.object(pool_caps, "evaluate") as ev:
            assert asyncio.run(runner._pool_cap_block(_task(scope="user", created_by=None))) == ""
        ev.assert_not_called()
