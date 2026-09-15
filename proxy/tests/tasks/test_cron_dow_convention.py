"""Standard-cron day-of-week convention for schedules + notifications.

The platform stores and accepts STANDARD cron (day-of-week 0 or 7 = Sunday).
APScheduler's numeric day_of_week is 0=Monday — even via
``CronTrigger.from_crontab`` — so numeric weekdays passed through verbatim
fired one day late. ``build_cron_trigger`` now remaps at trigger construction
(services/scheduler/scheduler_triggers.py) and a one-shot startup migration
(storage/schema.py::run_migrations) rewrites pre-existing rows one day
forward so their FIRE DAYS never change.

Covers:
- build_cron_trigger fires numeric weekdays on the standard day (the
  ``0 9 * * 5`` → Saturday regression from the launch-video staging).
- standard_dow_to_apscheduler / apscheduler_dow_to_standard token forms.
- Migration round-trip: for every dow form, the day set pre-fix rows actually
  fired on == the day set the migrated row fires on under the new builder.
- run_migrations rewrites dynamic_tasks + notifications rows exactly once
  (platform_settings flag) and leaves convention-agnostic fields untouched.

Run: cd proxy && python -m pytest tests/tasks/test_cron_dow_convention.py -v
"""

import sys
import zoneinfo
from datetime import datetime, timedelta, timezone

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from services.scheduler.scheduler_triggers import (  # noqa: E402
    apscheduler_dow_to_standard,
    build_cron_trigger,
    standard_dow_to_apscheduler,
)

UTC = zoneinfo.ZoneInfo("UTC")


def _fire_days(trigger) -> set[str]:
    """Weekday names a trigger fires on, sampled over two weeks."""
    cur = datetime(2026, 7, 13, tzinfo=timezone.utc)  # a Monday
    out: set[str] = set()
    for _ in range(14):
        nxt = trigger.get_next_fire_time(None, cur)
        if nxt is None:
            break
        out.add(nxt.strftime("%a"))
        cur = nxt + timedelta(seconds=1)
    return out


def _std_days(dow_field: str) -> set[str]:
    return _fire_days(build_cron_trigger(f"0 9 * * {dow_field}", UTC))


# ───────────────────────────────────────────────────────────────────────────
# build_cron_trigger — standard convention end to end
# ───────────────────────────────────────────────────────────────────────────


class TestBuildCronTriggerStandardDow:
    def test_numeric_weekdays_are_standard(self):
        # The filmed regression: '5' rendered Friday but fired Saturday.
        assert _std_days("5") == {"Fri"}
        assert _std_days("0") == {"Sun"}
        assert _std_days("7") == {"Sun"}  # both Sunday spellings
        assert _std_days("1") == {"Mon"}
        assert _std_days("6") == {"Sat"}

    def test_names_mean_the_same_day(self):
        assert _std_days("fri") == {"Fri"}
        assert _std_days("mon-fri") == {"Mon", "Tue", "Wed", "Thu", "Fri"}

    def test_ranges_and_lists(self):
        assert _std_days("1-5") == {"Mon", "Tue", "Wed", "Thu", "Fri"}
        # Crosses APScheduler's Monday week start → expanded internally.
        assert _std_days("0-2") == {"Sun", "Mon", "Tue"}
        assert _std_days("5-7") == {"Fri", "Sat", "Sun"}
        assert _std_days("0,3") == {"Sun", "Wed"}
        assert _std_days("6,0") == {"Sat", "Sun"}
        # Name range crossing the week start — raw APScheduler rejects it.
        assert _std_days("sun-tue") == {"Sun", "Mon", "Tue"}

    def test_steps_anchor_at_sunday(self):
        assert _std_days("*/2") == {"Sun", "Tue", "Thu", "Sat"}
        assert _std_days("1-5/2") == {"Mon", "Wed", "Fri"}

    def test_star_and_hours_step_untouched(self):
        assert _std_days("*") == {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
        # Non-dow fields pass through unmodified.
        t = build_cron_trigger("0 */6 * * *", UTC)
        nxt = t.get_next_fire_time(None, datetime(2026, 7, 13, 1, tzinfo=timezone.utc))
        assert nxt.hour == 6

    def test_malformed_fields_still_raise(self):
        with pytest.raises(Exception):
            build_cron_trigger("0 9 * * 8", UTC)
        with pytest.raises(Exception):
            build_cron_trigger("not a cron", UTC)


# ───────────────────────────────────────────────────────────────────────────
# Field remap helpers — token forms
# ───────────────────────────────────────────────────────────────────────────


class TestDowFieldRemap:
    @pytest.mark.parametrize("std,aps", [
        ("*", "*"),
        ("0", "6"), ("7", "6"), ("1", "0"), ("5", "4"), ("6", "5"),
        ("1-5", "0-4"), ("5-7", "4-6"),
        ("0-2", "0,1,6"),        # wraps Monday start → day list
        ("*/2", "1,3,5,6"),      # Sun,Tue,Thu,Sat in APScheduler numbering
        ("1-5/2", "0,2,4"),
        ("mon", "0"), ("sun", "6"), ("mon-fri", "0-4"),
        ("sun-tue", "0,1,6"),
        ("1,3,5", "0,2,4"),
        ("bogus", "bogus"),      # unrecognized → untouched, fails in CronTrigger
    ])
    def test_standard_to_apscheduler(self, std, aps):
        assert standard_dow_to_apscheduler(std) == aps

    @pytest.mark.parametrize("aps,std", [
        ("*", "*"),
        ("0", "1"), ("4", "5"), ("5", "6"), ("6", "0"),
        ("0-4", "1-5"), ("4-6", "5-7"),
        ("*/2", "1-7/2"),        # Monday-anchored step
        ("1-5/2", "2-6/2"),
        ("0,2,4", "1,3,5"),
        ("mon-fri", "mon-fri"),  # names are convention-agnostic
        ("bogus", "bogus"),
    ])
    def test_apscheduler_to_standard(self, aps, std):
        assert apscheduler_dow_to_standard(aps) == std

    @pytest.mark.parametrize("field", [
        "1", "5", "6", "0", "0-4", "1-5", "4-6", "*/2", "0,2,4", "1-5/2",
        "mon-fri", "sat,sun", "*",
    ])
    def test_migration_preserves_fire_days(self, field):
        """THE safety property: a migrated row fires on exactly the weekdays
        the pre-fix code fired it on — schedules must not silently shift."""
        from apscheduler.triggers.cron import CronTrigger
        # What pre-existing rows actually did: field handed to APScheduler raw.
        old = _fire_days(CronTrigger.from_crontab(f"0 9 * * {field}", timezone=UTC))
        migrated = apscheduler_dow_to_standard(field)
        assert _std_days(migrated) == old


# ───────────────────────────────────────────────────────────────────────────
# One-shot startup migration
# ───────────────────────────────────────────────────────────────────────────


def _create_dynamic_task(*, task_id, schedule, agent="support-bot"):
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Test", "do thing", "cli",
        "scheduled", schedule, None, None, 600,
        "user-1", None, None, None, None, False,
        scope="user",
    )


def _create_notification(*, nid, schedule):
    from storage.automation import notification_store
    return notification_store.create_notification(
        notification_id=nid,
        title="Test",
        body="Body",
        severity="info",
        scope="user",
        target="user-1",
        source="mcp",
        notification_type="recurring",
        schedule=schedule,
        created_by="user-1",
    )


def _get_schedule(table: str, row_id: str) -> str | None:
    from storage import pg as pg_pool
    with pg_pool.get_conn() as conn:
        row = conn.execute(
            f"SELECT schedule FROM {table} WHERE id = %s", (row_id,)
        ).fetchone()
        return row["schedule"] if row else None


def _run_migrations():
    from storage import pg as pg_pool
    from storage import schema as pg_schema
    with pg_pool.get_conn() as conn:
        pg_schema.run_migrations(conn)
        conn.commit()


class TestCronDowMigration:
    def test_rewrites_rows_once_and_flags(self, temp_db):
        _create_dynamic_task(task_id="task-fri", schedule="0 9 * * 5")
        _create_dynamic_task(task_id="task-week", schedule="30 8 * * 0-4")
        _create_dynamic_task(task_id="task-name", schedule="0 9 * * mon")
        _create_dynamic_task(task_id="task-star", schedule="*/10 * * * *")
        _create_dynamic_task(task_id="task-bad", schedule="whenever")
        _create_notification(nid="notif-sat", schedule="0 7 * * 5,6")

        _run_migrations()

        # Numeric weekdays shift one day forward (same fire days as before).
        assert _get_schedule("dynamic_tasks", "task-fri") == "0 9 * * 6"
        assert _get_schedule("dynamic_tasks", "task-week") == "30 8 * * 1-5"
        assert _get_schedule("notifications", "notif-sat") == "0 7 * * 6,0"
        # Convention-agnostic / malformed fields stay untouched.
        assert _get_schedule("dynamic_tasks", "task-name") == "0 9 * * mon"
        assert _get_schedule("dynamic_tasks", "task-star") == "*/10 * * * *"
        assert _get_schedule("dynamic_tasks", "task-bad") == "whenever"

        from storage import database as db
        assert db.get_platform_setting("cron_dow_standardized") == "done"

        # One-shot: rows created AFTER the flag are already standard and must
        # never be rewritten by a later startup.
        _create_dynamic_task(task_id="task-post", schedule="0 9 * * 5")
        _run_migrations()
        assert _get_schedule("dynamic_tasks", "task-post") == "0 9 * * 5"
        assert _get_schedule("dynamic_tasks", "task-fri") == "0 9 * * 6"
