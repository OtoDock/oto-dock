"""What came due while the platform was down fires at boot: the marks the
server leaves (``services/infra/platform_marks.py``), the one-time tasks in
the downtime (``scheduler.due_in_downtime``), the continuations a planned
stop held."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import startup
from services.infra import platform_marks
from services.scheduler import runner, scheduler, shared
from storage import database as task_store
from storage.automation import db_tasks


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.fixture(autouse=True)
def _unstamped(monkeypatch):
    monkeypatch.setattr(platform_marks, "_boot", platform_marks.Boot())


def _one_time(task_id: str, run_at: str | None = None, *, delay: int | None = None,
              task_type: str = "one_time") -> None:
    db_tasks.create_dynamic_task(task_id, "pa", task_id, "do it", "cli", task_type, None,
                                 run_at, delay, 600, None, scope="agent")


def test_a_database_no_server_stamped_has_no_downtime(temp_db):
    boot = platform_marks.read_at_boot()
    assert (boot.stamped, boot.clean_stop, boot.down_since) == (False, False, None)
    # A boot that fails before its answer is in the store reads the same next time.
    assert platform_marks.read_at_boot().stamped is False
    # From here the database is stamped, and the next boot knows it.
    platform_marks.stamp_at_boot()
    assert platform_marks.read_at_boot().stamped is True


def test_the_boot_stamps_only_once_its_answer_is_in_the_store():
    """The stamp reads as caught up to the next boot."""
    source = Path(startup.__file__).read_text()
    steps = ["platform_marks.read_at_boot()", "cut_turns.record_at_boot(",
             "run_recovery.defer_orphaned_runs(", "platform_marks.stamp_at_boot()"]
    assert [source.index(step) for step in steps] == sorted(source.index(step) for step in steps)
    assert "catches_up=marks.catches_up)" in source[source.index(steps[1]):source.index(steps[2])]


def test_the_downtime_starts_a_little_before_the_last_sign_of_life(temp_db):
    task_store.set_platform_setting(platform_marks.ALIVE_AT, _iso(timedelta(minutes=-30)))
    boot = platform_marks.read_at_boot()
    age = datetime.now(timezone.utc) - boot.down_since
    assert boot.stamped and timedelta(minutes=34) < age < timedelta(minutes=36)
    assert boot.clean_stop is False


def test_a_clean_stop_and_a_hold_are_read_once(temp_db):
    platform_marks.note_clean_stop()
    task_store.set_platform_setting(platform_marks.HELD_AT, _iso(timedelta(minutes=-20)))
    boot = platform_marks.read_at_boot()
    assert boot.clean_stop is True
    # The hold began before the last sign of life: what it held is in the window.
    assert datetime.now(timezone.utc) - boot.down_since > timedelta(minutes=19)
    platform_marks.stamp_at_boot()
    again = platform_marks.read_at_boot()
    assert again.clean_stop is False


def test_a_downtime_over_a_day_catches_nothing_up(temp_db):
    """A restored backup carries an old stamp: nothing old fires."""
    task_store.set_platform_setting(platform_marks.ALIVE_AT, _iso(timedelta(days=-3)))
    boot = platform_marks.read_at_boot()
    assert (boot.stamped, boot.down_since) == (True, None)


def test_only_what_came_due_in_the_downtime_and_never_fired_is_due(temp_db, monkeypatch):
    task_store.set_platform_setting(platform_marks.ALIVE_AT, _iso(timedelta(minutes=-30)))
    platform_marks.read_at_boot()
    _one_time("t-in", _iso(timedelta(minutes=-10)))
    _one_time("t-before", _iso(timedelta(hours=-3)))
    _one_time("t-ahead", _iso(timedelta(minutes=10)))
    _one_time("t-fired", _iso(timedelta(minutes=-10)))
    task_store.set_dynamic_task_fired("t-fired")
    _one_time("t-running", _iso(timedelta(minutes=-10)))
    task_store.create_run("run-live", "t-running", "pa", "manual", None, "do it")
    _one_time("t-paused", _iso(timedelta(minutes=-10)))
    task_store.set_dynamic_task_enabled("t-paused", False)
    _one_time("t-wake", _iso(timedelta(minutes=-5)), task_type="continuation")
    assert sorted(t.id for t in scheduler.due_in_downtime()) == ["t-in", "t-wake"]
    # An external scheduler fires what it missed itself.
    monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
    assert scheduler.due_in_downtime() == []


def test_nothing_is_due_on_a_database_with_no_known_downtime(temp_db):
    _one_time("t-old", _iso(timedelta(minutes=-10)))
    assert scheduler.due_in_downtime() == []


def test_a_delay_counts_from_its_creation_on_any_database(temp_db):
    _one_time("t-delay", delay=120)
    row = task_store.get_dynamic_task("t-delay")
    task = scheduler._row_to_task(row)
    created = datetime.fromisoformat(row["created_at"])
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    assert platform_marks.read_at_boot().stamped is False
    assert scheduler._delay_due(task) == created + timedelta(seconds=120)
    task_store.set_platform_setting(platform_marks.ALIVE_AT, _iso(timedelta(minutes=-1)))
    assert platform_marks.read_at_boot().stamped is True
    assert scheduler._delay_due(task) == created + timedelta(seconds=120)


def _registered(monkeypatch, task: shared.TaskDefinition) -> datetime | None:
    """When the boot's registration would start ``task``, None for no job."""
    added: dict[str, datetime] = {}

    class _Scheduler:
        def add_job(self, fn, trigger=None, id=None, **kw):
            added[id] = trigger.run_date
    monkeypatch.setattr(scheduler, "_scheduler", _Scheduler())
    scheduler._register_task(task)
    return added.get(f"task_{task.id}")


def _continuation(created: timedelta | None, delay: int) -> shared.TaskDefinition:
    return shared.TaskDefinition(
        id="dyn-c", name="c", agent="pa", prompt="go on", task_type="continuation", delay_seconds=delay,
        created_at=_iso(created) if created is not None else "")


@pytest.mark.parametrize("stamp_days", [None, 90])
def test_a_restore_gives_a_dumps_delayed_task_no_new_countdown(temp_db, monkeypatch, stamp_days):
    """A continuation of the dump's day, an hour's delay, never fired: its time
    passed, and on a restored database or a first boot it does not start on
    the old chat, as it does not on any other boot."""
    if stamp_days:
        task_store.set_platform_setting(platform_marks.ALIVE_AT, _iso(timedelta(days=-stamp_days)))
    assert platform_marks.read_at_boot().catches_up is False
    assert _registered(monkeypatch, _continuation(timedelta(days=-30), 3600)) is None
    assert scheduler.due_in_downtime() == []


def test_on_a_first_boot_a_task_created_a_minute_ago_still_waits_its_delay(temp_db, monkeypatch):
    """A new install's first boot, and its first process: what is left of the
    delay, never less and never a new countdown."""
    boot = platform_marks.read_at_boot()
    assert boot.stamped is False and boot.catches_up is False
    task = _continuation(timedelta(minutes=-1), 3600)
    due = _registered(monkeypatch, task)
    assert due == datetime.fromisoformat(task.created_at) + timedelta(seconds=3600)
    assert timedelta(minutes=58) < due - datetime.now(timezone.utc) < timedelta(minutes=60)


def test_a_task_being_created_counts_its_delay_from_now_on_any_boot(temp_db, monkeypatch):
    """The route registers it before it has a row, so it has no creation yet."""
    for stamp in ("", _iso(timedelta(minutes=-1))):
        task_store.set_platform_setting(platform_marks.ALIVE_AT, stamp)
        platform_marks.read_at_boot()
        due = _registered(monkeypatch, _continuation(None, 120))
        assert timedelta(seconds=118) < due - datetime.now(timezone.utc) <= timedelta(seconds=120)


def test_the_boot_fires_each_due_task_once(temp_db, monkeypatch):
    task_store.set_platform_setting(platform_marks.ALIVE_AT, _iso(timedelta(minutes=-30)))
    platform_marks.read_at_boot()
    _one_time("t-due", _iso(timedelta(minutes=-10)))
    fired: list[str] = []

    async def _fire(task):
        fired.append(task.id)
    monkeypatch.setattr(runner, "_fire_task", _fire)
    assert asyncio.run(scheduler.fire_downtime()) == 1
    assert fired == ["t-due"]


def test_a_continuation_a_hold_skipped_fires_at_the_resume(temp_db, monkeypatch):
    from core import drain
    _one_time("t-held-wake", _iso(timedelta(minutes=5)), task_type="continuation")
    task = scheduler._row_to_task(task_store.get_dynamic_task("t-held-wake"))
    fired: list[str] = []

    async def _fire(task):
        fired.append(task.id)
    monkeypatch.setattr(runner, "_fire_task", _fire)

    async def scene():
        await drain.drain(60)
        assert await runner._execute_task(task) == ""
        assert shared._held_wakes == ["t-held-wake"]
        await drain.resume()
        return await scheduler.fire_held()

    try:
        assert asyncio.run(scene()) == 1
        assert fired == ["t-held-wake"] and shared._held_wakes == []
    finally:
        shared._held_wakes.clear()
