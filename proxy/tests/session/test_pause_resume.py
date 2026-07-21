"""Tests for pause/resume on tasks and notifications.

Covers:
- scheduler.pause_dynamic_task / resume_dynamic_task: DB flag flip + APScheduler add/remove
- notification_manager.pause_notification / resume_notification / delete_notification
- Past run_at on resume: row enabled, no APScheduler job
- Idempotency
- Default of list_notifications now includes paused rows
- _fire_scheduled_notification short-circuits on enabled=FALSE
- Hard delete actually removes the row

Run: cd proxy && python -m pytest tests/session/test_pause_resume.py -v
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


def _create_recurring_task(task_id="task-recur", agent="support-bot", schedule="*/10 * * * *"):
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Recurring", "do thing", "cli",
        "scheduled", schedule, None, None, 600,
        "user-1", None, None, None, None, False,
        scope="user",
    )


def _create_one_time_task(task_id="task-onetime", agent="support-bot",
                          run_at=None, delay_seconds=None):
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "OneTime", "do thing", "cli",
        "one_time", None, run_at, delay_seconds, 600,
        "user-1", None, None, None, None, False,
        scope="user",
    )


def _create_notification(nid="notif-1", schedule="*/10 * * * *",
                          notification_type="recurring", run_at=None):
    from storage import notification_store
    return notification_store.create_notification(
        notification_id=nid,
        title="Test",
        body="Body",
        severity="info",
        scope="user",
        target="user-1",
        source="mcp",
        notification_type=notification_type,
        schedule=schedule,
        run_at=run_at,
        created_by="user-1",
    )


# ───────────────────────────────────────────────────────────────────────────
# scheduler.pause_dynamic_task / resume_dynamic_task
# ───────────────────────────────────────────────────────────────────────────


class TestSchedulerPauseResume:
    def test_pause_flips_flag_and_removes_job(self, temp_db):
        """Pause sets enabled=FALSE in DB and removes the APScheduler job."""
        from services.scheduler import scheduler

        _create_recurring_task("task-1")
        # Register with APScheduler manually (simulates `add_dynamic_task` flow)
        row = temp_db.get_dynamic_task("task-1")
        task = scheduler._row_to_task(row)
        scheduler._register_task(task)
        assert scheduler._scheduler.get_job("task_task-1") is not None

        # Pause
        ok = asyncio.run(scheduler.pause_dynamic_task("task-1"))
        assert ok is True

        # DB flag flipped
        assert temp_db.get_dynamic_task("task-1")["enabled"] is False
        # APScheduler job gone
        assert scheduler._scheduler.get_job("task_task-1") is None

    def test_resume_flips_flag_and_re_registers(self, temp_db):
        from services.scheduler import scheduler

        _create_recurring_task("task-2")
        # Manually disable in DB to simulate paused state
        temp_db.set_dynamic_task_enabled("task-2", False)
        assert scheduler._scheduler.get_job("task_task-2") is None

        # Resume
        ok = asyncio.run(scheduler.resume_dynamic_task("task-2"))
        assert ok is True

        assert temp_db.get_dynamic_task("task-2")["enabled"] is True
        assert scheduler._scheduler.get_job("task_task-2") is not None

    def test_resume_one_time_past_run_at_no_job_registered(self, temp_db):
        """Resuming a one-time task whose run_at is past leaves the row
        enabled but does not register an APScheduler job."""
        from services.scheduler import scheduler

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _create_one_time_task("task-past", run_at=past)
        temp_db.set_dynamic_task_enabled("task-past", False)

        ok = asyncio.run(scheduler.resume_dynamic_task("task-past"))
        assert ok is True

        assert temp_db.get_dynamic_task("task-past")["enabled"] is True
        # _register_task returns early — no job
        assert scheduler._scheduler.get_job("task_task-past") is None

    def test_pause_missing_task_returns_false(self, temp_db):
        from services.scheduler import scheduler
        ok = asyncio.run(scheduler.pause_dynamic_task("does-not-exist"))
        assert ok is False

    def test_resume_missing_task_returns_false(self, temp_db):
        from services.scheduler import scheduler
        ok = asyncio.run(scheduler.resume_dynamic_task("does-not-exist"))
        assert ok is False

    def test_pause_idempotent(self, temp_db):
        """Pausing twice is harmless."""
        from services.scheduler import scheduler

        _create_recurring_task("task-idem")
        row = temp_db.get_dynamic_task("task-idem")
        scheduler._register_task(scheduler._row_to_task(row))

        asyncio.run(scheduler.pause_dynamic_task("task-idem"))
        # Second call must not raise even though the job is already gone
        asyncio.run(scheduler.pause_dynamic_task("task-idem"))
        assert temp_db.get_dynamic_task("task-idem")["enabled"] is False

    def test_resume_replaces_existing_job(self, temp_db):
        """Resuming an already-active task is idempotent (replace_existing)."""
        from services.scheduler import scheduler

        _create_recurring_task("task-resume-twice")
        # First resume
        asyncio.run(scheduler.resume_dynamic_task("task-resume-twice"))
        # Second resume — must not raise
        asyncio.run(scheduler.resume_dynamic_task("task-resume-twice"))
        assert scheduler._scheduler.get_job("task_task-resume-twice") is not None
        # Cleanup
        scheduler._scheduler.remove_job("task_task-resume-twice")


# ───────────────────────────────────────────────────────────────────────────
# notification_manager.pause/resume/delete
# ───────────────────────────────────────────────────────────────────────────


class TestNotificationPauseResume:
    def test_pause_flips_flag(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store

        _create_notification("notif-pause")
        ok = asyncio.run(notification_manager.pause_notification("notif-pause"))
        assert ok is True
        assert notification_store.get_notification("notif-pause")["enabled"] is False

    def test_resume_flips_flag(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store

        _create_notification("notif-resume")
        notification_store.set_notification_enabled("notif-resume", False)

        ok = asyncio.run(notification_manager.resume_notification("notif-resume"))
        assert ok is True
        assert notification_store.get_notification("notif-resume")["enabled"] is True

    def test_pause_missing_returns_false(self, temp_db):
        from services.notifications import notification_manager
        ok = asyncio.run(notification_manager.pause_notification("nope"))
        assert ok is False

    def test_resume_missing_returns_false(self, temp_db):
        from services.notifications import notification_manager
        ok = asyncio.run(notification_manager.resume_notification("nope"))
        assert ok is False

    def test_resume_one_time_past_run_at(self, temp_db):
        """Resuming a past one-time notification keeps it enabled but doesn't fire."""
        from services.notifications import notification_manager
        from storage import notification_store

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _create_notification(
            "notif-past", schedule=None,
            notification_type="one_time", run_at=past,
        )
        notification_store.set_notification_enabled("notif-past", False)

        ok = asyncio.run(notification_manager.resume_notification("notif-past"))
        assert ok is True
        # Row enabled, but _register_notification will have returned False —
        # we don't strictly assert no job (scheduler may not be running in this test
        # class; the contract is enabled=TRUE and no exception raised).
        assert notification_store.get_notification("notif-past")["enabled"] is True

    def test_delete_hard_removes_row(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store

        _create_notification("notif-del")
        assert notification_store.get_notification("notif-del") is not None

        ok = asyncio.run(notification_manager.delete_notification("notif-del"))
        assert ok is True
        assert notification_store.get_notification("notif-del") is None


# ───────────────────────────────────────────────────────────────────────────
# notification_store: list default + fire short-circuit
# ───────────────────────────────────────────────────────────────────────────


class TestNotificationListAndFire:
    def test_list_default_includes_paused(self, temp_db):
        """list_notifications now defaults to enabled_only=False — paused rows show."""
        from storage import notification_store

        _create_notification("notif-active")
        _create_notification("notif-paused")
        notification_store.set_notification_enabled("notif-paused", False)

        items = notification_store.list_notifications()
        ids = {n["id"] for n in items}
        assert "notif-active" in ids
        assert "notif-paused" in ids

    def test_list_enabled_only_excludes_paused(self, temp_db):
        from storage import notification_store

        _create_notification("notif-a2")
        _create_notification("notif-b2")
        notification_store.set_notification_enabled("notif-b2", False)

        items = notification_store.list_notifications(enabled_only=True)
        ids = {n["id"] for n in items}
        assert "notif-a2" in ids
        assert "notif-b2" not in ids

    def test_fire_scheduled_short_circuits_on_paused(self, temp_db):
        """Defence-in-depth: paused notifications never fire even if a stale job triggers."""
        from services.notifications import notification_manager
        from storage import notification_store

        _create_notification("notif-fire")
        notification_store.set_notification_enabled("notif-fire", False)

        async def _run():
            # Patch fire_notification so we can detect if it was called
            called = {"n": 0}
            real_fire = notification_manager.fire_notification

            async def fake_fire(*args, **kwargs):
                called["n"] += 1
                return await real_fire(*args, **kwargs)

            notification_manager.fire_notification = fake_fire
            try:
                await notification_manager._fire_scheduled_notification("notif-fire")
            finally:
                notification_manager.fire_notification = real_fire
            return called["n"]

        n = asyncio.run(_run())
        assert n == 0, "paused notification must not fire"


# ───────────────────────────────────────────────────────────────────────────
# One-time notifications are hard-deleted after firing (parity with tasks)
# ───────────────────────────────────────────────────────────────────────────


class TestOneTimeAutoCleanup:
    def test_one_time_deleted_after_fire(self, temp_db):
        """A one-time notification's row is hard-deleted once it fires.

        Matches one-time task auto-cleanup. Deliveries in
        notification_deliveries are independent and remain.
        """
        from services.notifications import notification_manager
        from storage import notification_store
        from datetime import datetime, timezone

        # Seed a target user so resolve_targets has someone to deliver to
        from storage.pg import get_conn
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (sub, email, name, role, created_at, last_login) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                ("user-1", "u1@test.com", "u1", "member", now, now),
            )
            conn.commit()

        _create_notification(
            "notif-onetime-fire", schedule=None,
            notification_type="one_time", run_at=None,
        )
        assert notification_store.get_notification("notif-onetime-fire") is not None

        async def _fire():
            await notification_manager.fire_notification(
                title="t", body="b", severity="info",
                scope="user", target="user-1", source="mcp",
                notification_id="notif-onetime-fire",
            )

        asyncio.run(_fire())

        # Row should be gone (hard-deleted)
        assert notification_store.get_notification("notif-onetime-fire") is None

    def test_recurring_kept_after_fire(self, temp_db):
        """A recurring notification stays in the DB after firing — only fired_count bumps."""
        from services.notifications import notification_manager
        from storage import notification_store
        from datetime import datetime, timezone

        # Seed a target user
        from storage.pg import get_conn
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (sub, email, name, role, created_at, last_login) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                ("user-1", "u1@test.com", "u1", "member", now, now),
            )
            conn.commit()

        _create_notification(
            "notif-recur-fire", schedule="0 9 * * *",
            notification_type="recurring",
        )

        async def _fire():
            await notification_manager.fire_notification(
                title="t", body="b", severity="info",
                scope="user", target="user-1", source="mcp",
                notification_id="notif-recur-fire",
            )

        asyncio.run(_fire())

        row = notification_store.get_notification("notif-recur-fire")
        assert row is not None, "recurring notifications must persist after fire"
        assert row["fired_count"] == 1


# ───────────────────────────────────────────────────────────────────────────
# Edit task / notification (partial update + reschedule)
# ───────────────────────────────────────────────────────────────────────────


class TestEditTask:
    def test_edit_name_only_no_reschedule(self, temp_db):
        """Editing name/prompt only does not touch APScheduler."""
        from services.scheduler import scheduler

        _create_recurring_task("task-edit-name", schedule="0 9 * * *")
        row = temp_db.get_dynamic_task("task-edit-name")
        scheduler._register_task(scheduler._row_to_task(row))
        original_trigger = scheduler._scheduler.get_job("task_task-edit-name").trigger

        ok, err = asyncio.run(
            scheduler.update_dynamic_task("task-edit-name", {"name": "renamed"})
        )
        assert ok is True
        assert err is None
        assert temp_db.get_dynamic_task("task-edit-name")["name"] == "renamed"
        # Trigger unchanged because timing fields were not touched
        new_trigger = scheduler._scheduler.get_job("task_task-edit-name").trigger
        assert str(original_trigger) == str(new_trigger)

    def test_edit_schedule_replaces_trigger(self, temp_db):
        from services.scheduler import scheduler

        _create_recurring_task("task-edit-sched", schedule="0 9 * * *")
        row = temp_db.get_dynamic_task("task-edit-sched")
        scheduler._register_task(scheduler._row_to_task(row))
        original = str(scheduler._scheduler.get_job("task_task-edit-sched").trigger)

        ok, err = asyncio.run(
            scheduler.update_dynamic_task(
                "task-edit-sched", {"schedule": "*/5 * * * *"},
            )
        )
        assert ok is True
        assert err is None
        new = str(scheduler._scheduler.get_job("task_task-edit-sched").trigger)
        assert new != original
        assert temp_db.get_dynamic_task("task-edit-sched")["schedule"] == "*/5 * * * *"

    def test_switch_recurring_to_one_time_clears_schedule(self, temp_db):
        from services.scheduler import scheduler
        from datetime import datetime, timedelta, timezone

        _create_recurring_task("task-mode-switch", schedule="0 9 * * *")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        ok, err = asyncio.run(
            scheduler.update_dynamic_task(
                "task-mode-switch", {"run_at": future},
            )
        )
        assert ok is True
        row = temp_db.get_dynamic_task("task-mode-switch")
        assert row["schedule"] is None
        assert row["run_at"] is not None
        assert row["task_type"] == "one_time"

    def test_switch_one_time_to_recurring_clears_run_at(self, temp_db):
        from services.scheduler import scheduler
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        _create_one_time_task("task-mode-switch-2", run_at=future)

        ok, err = asyncio.run(
            scheduler.update_dynamic_task(
                "task-mode-switch-2", {"schedule": "0 9 * * *"},
            )
        )
        assert ok is True
        row = temp_db.get_dynamic_task("task-mode-switch-2")
        assert row["schedule"] == "0 9 * * *"
        assert row["run_at"] is None
        assert row["task_type"] == "scheduled"

    def test_edit_invalid_cron_returns_error(self, temp_db):
        from services.scheduler import scheduler

        _create_recurring_task("task-bad-cron")
        ok, err = asyncio.run(
            scheduler.update_dynamic_task(
                "task-bad-cron", {"schedule": "not a cron"},
            )
        )
        assert ok is False
        assert err is not None
        assert "Invalid cron" in err

    def test_edit_invalid_run_at_returns_error(self, temp_db):
        from services.scheduler import scheduler

        _create_recurring_task("task-bad-runat")
        ok, err = asyncio.run(
            scheduler.update_dynamic_task(
                "task-bad-runat", {"run_at": "not-a-date"},
            )
        )
        assert ok is False
        assert err is not None
        assert "Invalid run_at" in err

    def test_edit_missing_task_returns_false(self, temp_db):
        from services.scheduler import scheduler
        ok, err = asyncio.run(
            scheduler.update_dynamic_task("nope", {"name": "x"})
        )
        assert ok is False
        assert err is None  # 404 path, not validation

    def test_edit_past_run_at_no_job_registered(self, temp_db):
        """Editing run_at to a past time: row updated, no APScheduler job."""
        from services.scheduler import scheduler
        from datetime import datetime, timedelta, timezone

        _create_recurring_task("task-edit-past")
        row = temp_db.get_dynamic_task("task-edit-past")
        scheduler._register_task(scheduler._row_to_task(row))

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        ok, err = asyncio.run(
            scheduler.update_dynamic_task("task-edit-past", {"run_at": past})
        )
        assert ok is True
        assert err is None
        assert scheduler._scheduler.get_job("task_task-edit-past") is None
        assert temp_db.get_dynamic_task("task-edit-past")["run_at"] is not None

    def test_edit_paused_task_does_not_re_register(self, temp_db):
        """Editing a paused task updates DB but leaves scheduler alone."""
        from services.scheduler import scheduler

        _create_recurring_task("task-edit-paused")
        # Pause first
        asyncio.run(scheduler.pause_dynamic_task("task-edit-paused"))
        assert scheduler._scheduler.get_job("task_task-edit-paused") is None

        ok, err = asyncio.run(
            scheduler.update_dynamic_task(
                "task-edit-paused", {"schedule": "*/5 * * * *"},
            )
        )
        assert ok is True
        assert err is None
        # Still no job — task is paused
        assert scheduler._scheduler.get_job("task_task-edit-paused") is None
        # DB has the new schedule
        assert temp_db.get_dynamic_task("task-edit-paused")["schedule"] == "*/5 * * * *"

    def test_edit_in_standalone_mode_only_touches_db(self, temp_db, monkeypatch):
        from services.scheduler import scheduler

        _create_recurring_task("task-edit-standalone")
        monkeypatch.setattr("config.SCHEDULER_MODE", "standalone")

        called = {"add_job": False, "remove_job": False}
        monkeypatch.setattr(
            scheduler._scheduler, "add_job",
            lambda *a, **kw: called.__setitem__("add_job", True) or None,
        )
        monkeypatch.setattr(
            scheduler._scheduler, "remove_job",
            lambda *a, **kw: called.__setitem__("remove_job", True) or None,
        )

        ok, err = asyncio.run(
            scheduler.update_dynamic_task(
                "task-edit-standalone", {"schedule": "*/5 * * * *"},
            )
        )
        assert ok is True
        assert err is None
        assert called["add_job"] is False
        assert called["remove_job"] is False
        assert temp_db.get_dynamic_task("task-edit-standalone")["schedule"] == "*/5 * * * *"


class TestEditNotification:
    def test_edit_title_only_no_reschedule(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store

        _create_notification("notif-edit-title")

        ok, err = asyncio.run(
            notification_manager.update_notification(
                "notif-edit-title", {"title": "New Title"},
            )
        )
        assert ok is True
        assert err is None
        assert notification_store.get_notification("notif-edit-title")["title"] == "New Title"

    def test_edit_schedule_changes_db(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store

        _create_notification("notif-edit-sched")
        ok, err = asyncio.run(
            notification_manager.update_notification(
                "notif-edit-sched", {"schedule": "0 12 * * *"},
            )
        )
        assert ok is True
        row = notification_store.get_notification("notif-edit-sched")
        assert row["schedule"] == "0 12 * * *"

    def test_switch_to_one_time_clears_schedule_and_sets_type(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store
        from datetime import datetime, timedelta, timezone

        _create_notification("notif-switch")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        ok, err = asyncio.run(
            notification_manager.update_notification(
                "notif-switch", {"run_at": future},
            )
        )
        assert ok is True
        row = notification_store.get_notification("notif-switch")
        assert row["schedule"] is None
        assert row["run_at"] is not None
        assert row["notification_type"] == "one_time"

    def test_switch_to_recurring_clears_run_at_and_sets_type(self, temp_db):
        from services.notifications import notification_manager
        from storage import notification_store
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        _create_notification(
            "notif-switch-2", schedule=None,
            notification_type="one_time", run_at=future,
        )

        ok, err = asyncio.run(
            notification_manager.update_notification(
                "notif-switch-2", {"schedule": "0 9 * * *"},
            )
        )
        assert ok is True
        row = notification_store.get_notification("notif-switch-2")
        assert row["schedule"] == "0 9 * * *"
        assert row["run_at"] is None
        assert row["notification_type"] == "recurring"

    def test_edit_invalid_cron_returns_error(self, temp_db):
        from services.notifications import notification_manager
        _create_notification("notif-bad-cron")
        ok, err = asyncio.run(
            notification_manager.update_notification(
                "notif-bad-cron", {"schedule": "garbage"},
            )
        )
        assert ok is False
        assert err is not None
        assert "Invalid cron" in err

    def test_edit_missing_returns_false(self, temp_db):
        from services.notifications import notification_manager
        ok, err = asyncio.run(
            notification_manager.update_notification("nope", {"title": "x"})
        )
        assert ok is False
        assert err is None


# ───────────────────────────────────────────────────────────────────────────
# Standalone-mode behavior (DB-only path, no APScheduler interaction)
# ───────────────────────────────────────────────────────────────────────────


class TestStandaloneMode:
    def test_pause_in_standalone_mode_only_touches_db(self, temp_db, monkeypatch):
        """In standalone mode the helper must not call _scheduler.remove_job."""
        from services.scheduler import scheduler

        _create_recurring_task("task-standalone-pause")
        monkeypatch.setattr("config.SCHEDULER_MODE", "standalone")

        called = {"removed": False}

        def fake_remove_job(job_id):
            called["removed"] = True

        monkeypatch.setattr(scheduler._scheduler, "remove_job", fake_remove_job)

        asyncio.run(scheduler.pause_dynamic_task("task-standalone-pause"))

        assert called["removed"] is False
        assert temp_db.get_dynamic_task("task-standalone-pause")["enabled"] is False

    def test_resume_in_standalone_mode_only_touches_db(self, temp_db, monkeypatch):
        from services.scheduler import scheduler

        _create_recurring_task("task-standalone-resume")
        temp_db.set_dynamic_task_enabled("task-standalone-resume", False)
        monkeypatch.setattr("config.SCHEDULER_MODE", "standalone")

        called = {"added": False}

        def fake_add_job(*a, **kw):
            called["added"] = True

        monkeypatch.setattr(scheduler._scheduler, "add_job", fake_add_job)

        asyncio.run(scheduler.resume_dynamic_task("task-standalone-resume"))

        assert called["added"] is False
        assert temp_db.get_dynamic_task("task-standalone-resume")["enabled"] is True
