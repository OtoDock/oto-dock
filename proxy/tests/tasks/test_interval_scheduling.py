"""Tests for `interval_seconds` recurring scheduling on tasks + notifications.

Covers:
- scheduler._register_task / notification_manager._register_notification build an
  IntervalTrigger with seconds=N and start_date=created_at + N.
- _validate_interval_seconds bounds (60 ≤ N ≤ 31536000).
- update_dynamic_task / update_notification mutual exclusivity:
  setting `interval_seconds` clears `schedule` + `run_at`, and vice versa.
- _row_to_task pulls `interval_seconds` + `created_at` from the DB row.
- task_type / notification_type auto-derive `scheduled` / `recurring` for interval.

Run: cd proxy && python -m pytest tests/tasks/test_interval_scheduling.py -v
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone


from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _create_interval_task(task_id="task-interval", agent="support-bot",
                          interval_seconds=61200):
    """Insert a dynamic_tasks row with interval_seconds set."""
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Interval", "do thing", "cli",
        "scheduled", None, None, None, 600,
        "user-1", None, None, None, None, False,
        scope="user",
        interval_seconds=interval_seconds,
    )


def _create_cron_task(task_id="task-cron", agent="support-bot",
                     schedule="*/10 * * * *"):
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Cron", "do thing", "cli",
        "scheduled", schedule, None, None, 600,
        "user-1", None, None, None, None, False,
        scope="user",
    )


def _create_interval_notification(nid="notif-interval", interval_seconds=61200):
    from storage import notification_store
    return notification_store.create_notification(
        notification_id=nid,
        title="Interval Notif",
        body="Body",
        severity="info",
        scope="user",
        target="user-1",
        source="mcp",
        notification_type="recurring",
        interval_seconds=interval_seconds,
        created_by="user-1",
    )


# ───────────────────────────────────────────────────────────────────────────
# Bounds validation
# ───────────────────────────────────────────────────────────────────────────


class TestIntervalBounds:
    def test_validate_min(self):
        from services.scheduler.scheduler import _validate_interval_seconds
        assert _validate_interval_seconds(60) is None
        assert _validate_interval_seconds(59) is not None  # below min
        assert _validate_interval_seconds(0) is not None
        assert _validate_interval_seconds(-1) is not None

    def test_validate_max(self):
        from services.scheduler.scheduler import _validate_interval_seconds
        assert _validate_interval_seconds(31_536_000) is None  # 1 year
        assert _validate_interval_seconds(31_536_001) is not None  # above max

    def test_validate_type(self):
        from services.scheduler.scheduler import _validate_interval_seconds
        assert _validate_interval_seconds("3600") is not None  # string rejected
        assert _validate_interval_seconds(3600.5) is not None  # float rejected
        assert _validate_interval_seconds(True) is not None  # bool rejected (bool is int subclass)
        assert _validate_interval_seconds(None) is not None  # None rejected


# ───────────────────────────────────────────────────────────────────────────
# scheduler._register_task with interval_seconds
# ───────────────────────────────────────────────────────────────────────────


class TestSchedulerRegisterInterval:
    def test_register_builds_interval_trigger(self, temp_db):
        """_register_task with interval_seconds builds an IntervalTrigger."""
        from apscheduler.triggers.interval import IntervalTrigger
        from services.scheduler import scheduler

        _create_interval_task("task-i1", interval_seconds=61200)
        row = temp_db.get_dynamic_task("task-i1")
        task = scheduler._row_to_task(row)

        assert task.interval_seconds == 61200
        assert task.created_at is not None

        scheduler._register_task(task)
        job = scheduler._scheduler.get_job("task_task-i1")
        assert job is not None
        assert isinstance(job.trigger, IntervalTrigger)
        assert int(job.trigger.interval.total_seconds()) == 61200

        # start_date anchor: should be ~ created_at + interval
        created_at = datetime.fromisoformat(task.created_at)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        expected_start = created_at + timedelta(seconds=61200)
        # Both timestamps should round-trip; allow ±1s for serialisation slack.
        delta = abs((job.trigger.start_date - expected_start).total_seconds())
        assert delta < 1.0

    def test_register_skips_when_no_timing(self, temp_db):
        """A task with neither schedule nor interval_seconds nor run_at skips registration."""
        from services.scheduler import scheduler

        # Build a TaskDefinition manually with no timing fields
        task = scheduler.TaskDefinition(
            id="task-empty",
            name="Empty",
            agent="support-bot",
            prompt="x",
            notification_mode="manual",
        )
        scheduler._register_task(task)
        assert scheduler._scheduler.get_job("task_task-empty") is None


# ───────────────────────────────────────────────────────────────────────────
# update_dynamic_task mutual exclusivity
# ───────────────────────────────────────────────────────────────────────────


class TestUpdateMutualExclusivity:
    def test_setting_interval_clears_schedule_and_run_at(self, temp_db):
        from services.scheduler import scheduler

        _create_cron_task("task-swap1", schedule="0 9 * * *")
        # Pre-condition: schedule is set
        row = temp_db.get_dynamic_task("task-swap1")
        assert row["schedule"] == "0 9 * * *"
        assert row["interval_seconds"] is None

        # Switch to interval
        ok, err = asyncio.run(scheduler.update_dynamic_task(
            "task-swap1", {"interval_seconds": 3600}
        ))
        assert ok is True
        assert err is None

        row = temp_db.get_dynamic_task("task-swap1")
        assert row["schedule"] is None
        assert row["interval_seconds"] == 3600
        assert row["run_at"] is None
        assert row["task_type"] == "scheduled"

    def test_setting_schedule_clears_interval(self, temp_db):
        from services.scheduler import scheduler

        _create_interval_task("task-swap2", interval_seconds=61200)
        ok, err = asyncio.run(scheduler.update_dynamic_task(
            "task-swap2", {"schedule": "0 9 * * *"}
        ))
        assert ok is True
        assert err is None

        row = temp_db.get_dynamic_task("task-swap2")
        assert row["schedule"] == "0 9 * * *"
        assert row["interval_seconds"] is None
        assert row["task_type"] == "scheduled"

    def test_setting_run_at_clears_interval(self, temp_db):
        from services.scheduler import scheduler

        _create_interval_task("task-swap3", interval_seconds=61200)
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        ok, err = asyncio.run(scheduler.update_dynamic_task(
            "task-swap3", {"run_at": future}
        ))
        assert ok is True

        row = temp_db.get_dynamic_task("task-swap3")
        assert row["interval_seconds"] is None
        assert row["schedule"] is None
        assert row["run_at"] is not None
        assert row["task_type"] == "one_time"

    def test_update_validates_interval_bounds(self, temp_db):
        from services.scheduler import scheduler

        _create_interval_task("task-bounds", interval_seconds=3600)
        ok, err = asyncio.run(scheduler.update_dynamic_task(
            "task-bounds", {"interval_seconds": 30}
        ))
        assert ok is False
        assert err is not None and "interval_seconds" in err


# ───────────────────────────────────────────────────────────────────────────
# add_dynamic_task end-to-end (DB + APScheduler)
# ───────────────────────────────────────────────────────────────────────────


class TestAddDynamicInterval:
    def test_add_dynamic_interval_registers_trigger(self, temp_db):
        """add_dynamic_task with interval_seconds persists + registers IntervalTrigger."""
        from apscheduler.triggers.interval import IntervalTrigger
        from services.scheduler import scheduler

        task = scheduler.TaskDefinition(
            id="task-add-i",
            name="Interval Add",
            agent="support-bot",
            prompt="x",
            interval_seconds=7200,
            scope="user",
            source="dynamic",
            created_by="user-1",
            notification_mode="manual",
        )
        asyncio.run(scheduler.add_dynamic_task(task))

        row = temp_db.get_dynamic_task("task-add-i")
        assert row is not None
        assert row["interval_seconds"] == 7200
        assert row["task_type"] == "scheduled"
        assert row["schedule"] in (None, "")

        job = scheduler._scheduler.get_job("task_task-add-i")
        assert job is not None
        assert isinstance(job.trigger, IntervalTrigger)
        assert int(job.trigger.interval.total_seconds()) == 7200


# ───────────────────────────────────────────────────────────────────────────
# notification_manager._register_notification with interval_seconds
# ───────────────────────────────────────────────────────────────────────────


class TestNotificationInterval:
    def test_register_recurring_interval(self, temp_db):
        from apscheduler.triggers.interval import IntervalTrigger
        from services.notifications import notification_manager
        from services.scheduler import scheduler

        # notification_manager.start() wires _scheduler_ref to the proxy's scheduler.
        # Mirror that for the test.
        notification_manager._scheduler_ref = scheduler._scheduler

        notif = _create_interval_notification("n-i1", interval_seconds=3600)
        assert notif["interval_seconds"] == 3600
        ok = notification_manager._register_notification(notif)
        assert ok is True
        job = scheduler._scheduler.get_job("notif_n-i1")
        assert job is not None
        assert isinstance(job.trigger, IntervalTrigger)
        assert int(job.trigger.interval.total_seconds()) == 3600

    def test_update_notification_swaps_to_interval(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store

        # Start with cron
        notification_store.create_notification(
            notification_id="n-swap",
            title="t", body="b", severity="info",
            scope="user", target="user-1", source="mcp",
            notification_type="recurring",
            schedule="0 9 * * *",
            created_by="user-1",
        )
        ok, err = asyncio.run(notification_manager.update_notification(
            "n-swap", {"interval_seconds": 1800}
        ))
        assert ok is True
        assert err is None
        row = notification_store.get_notification("n-swap")
        assert row["schedule"] is None
        assert row["interval_seconds"] == 1800
        assert row["notification_type"] == "recurring"

    def test_update_notification_bounds(self, temp_db):
        from services.notifications import notification_manager

        _create_interval_notification("n-bounds", interval_seconds=3600)
        ok, err = asyncio.run(notification_manager.update_notification(
            "n-bounds", {"interval_seconds": 30}
        ))
        assert ok is False
        assert err is not None and "interval_seconds" in err


# ───────────────────────────────────────────────────────────────────────────
# _row_to_task hydration
# ───────────────────────────────────────────────────────────────────────────


class TestRowToTaskHydration:
    def test_row_to_task_pulls_interval_and_created_at(self, temp_db):
        from services.scheduler import scheduler

        _create_interval_task("task-hydrate", interval_seconds=900)
        row = temp_db.get_dynamic_task("task-hydrate")
        task = scheduler._row_to_task(row)
        assert task.interval_seconds == 900
        assert task.created_at is not None
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(task.created_at)
        # Within the last few seconds
        delta = abs((datetime.now(timezone.utc) - (
            dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        )).total_seconds())
        assert delta < 60
