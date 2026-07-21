"""Tests for per-user timezone architecture.

Covers:
- format_current_time(user_tz) renders in the supplied IANA TZ.
- format_current_time(None) falls back to platform TZ.
- format_current_time("not-a-tz") falls back silently.
- create_dynamic_task / create_notification accept user_tz.
- _register_task / _register_notification use user_tz for cron triggers.
- _register_task naive run_at is interpreted in row's TZ (not UTC).
- update_dynamic_task / update_notification accept user_tz, validate IANA,
  re-snapshot, and re-register.
- session_state.set_user_tz / get_session_user_tz round-trip.
- Standalone scheduler reads user_tz from row.
- Pre-migration rows (user_tz NULL) fall back to platform TZ.

Run: cd proxy && python -m pytest tests/agents/test_user_tz.py -v
"""

import contextlib
import os
import sys
import zoneinfo
from datetime import datetime, timedelta, timezone

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)
# scheduler/standalone_scheduler.py was moved out of proxy/ to keep it
# isolated as a commercial-only component. Add it to sys.path so the
# tests below can still ``import standalone_scheduler``.
_scheduler_root = os.path.join(os.path.dirname(_proxy_root), "scheduler")
if _scheduler_root not in sys.path:
    sys.path.insert(0, _scheduler_root)


# ───────────────────────────────────────────────────────────────────────────
# Helpers (mirrored from test_pause_resume.py)
# ───────────────────────────────────────────────────────────────────────────


def _create_dynamic_task(*, task_id="task-1", agent="support-bot",
                         schedule=None, run_at=None, delay_seconds=None,
                         task_type="scheduled", user_tz=None):
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Test", "do thing", "cli",
        task_type, schedule, run_at, delay_seconds, 600,
        "user-1", None, None, None, None, False,
        scope="user",
        user_tz=user_tz,
    )


def _create_notification(*, nid="notif-1", schedule=None, run_at=None,
                         notification_type="recurring", user_tz=None):
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
        user_tz=user_tz,
    )


def _set_platform_tz(tz: str):
    """Force the platform TZ in DB so format_current_time / fallbacks resolve to it."""
    import config
    from storage import database as db
    db.set_platform_setting("platform_timezone", tz)
    # Invalidate the 60s TZ cache, exactly as the Setup API does on write — else a
    # value cached by an earlier test would shadow this one (order-dependent flake).
    config._tz_cache["tz"] = None


# ───────────────────────────────────────────────────────────────────────────
# format_current_time
# ───────────────────────────────────────────────────────────────────────────


def test_format_current_time_with_user_tz(temp_db):
    import config
    _set_platform_tz("Europe/Athens")

    s = config.format_current_time("America/New_York")
    assert "America/New_York" in s
    assert "(UTC-" in s  # NYC offset is always negative


def test_format_current_time_without_user_tz_uses_platform(temp_db):
    import config
    _set_platform_tz("Europe/Athens")

    s = config.format_current_time(None)
    assert "Europe/Athens" in s


def test_format_current_time_invalid_iana_falls_back(temp_db):
    import config
    _set_platform_tz("Europe/Athens")

    s = config.format_current_time("not-a-tz")
    # Should silently fall back to platform TZ — no exception.
    assert "Europe/Athens" in s


def test_format_current_time_includes_iana_and_offset(temp_db):
    import config
    _set_platform_tz("Europe/Athens")

    s = config.format_current_time("Europe/Athens")
    assert "Europe/Athens" in s
    assert "(UTC+" in s or "(UTC-" in s
    # Format like "Wednesday, April 29, 2026 07:13 (7:13 AM) Europe/Athens (UTC+03:00)"
    assert ":" in s  # offset delimiter


def test_format_current_time_includes_ampm_gloss(temp_db):
    """Output contains the 12-hour AM/PM equivalent in parentheses after H:M.

    LLMs drift on bare 24-hour numbers in the morning window — the gloss
    forces unambiguous reading. Mock datetime.now to hit specific clock
    points across the day plus the noon/midnight boundaries.
    """
    import config
    from unittest import mock
    from datetime import datetime
    _set_platform_tz("Europe/Athens")
    tz = zoneinfo.ZoneInfo("Europe/Berlin")

    cases = [
        # (hour, minute, expected fragment in output)
        (4, 2, "04:02 (4:02 AM)"),    # early morning — the user's Berlin bug
        (11, 59, "11:59 (11:59 AM)"), # last minute before noon
        (12, 0, "12:00 (12:00 PM)"),  # noon boundary
        (12, 30, "12:30 (12:30 PM)"), # afternoon proper
        (17, 0, "17:00 (5:00 PM)"),   # late afternoon / "5 o'clock"
        (23, 59, "23:59 (11:59 PM)"), # last minute before midnight
        (0, 0, "00:00 (12:00 AM)"),   # midnight boundary
        (0, 30, "00:30 (12:30 AM)"),  # late night / early morning
    ]

    for hour, minute, expected in cases:
        fake = datetime(2026, 5, 8, hour, minute, 0, tzinfo=tz)
        with mock.patch.object(
            config, "datetime",
            mock.Mock(now=fake.astimezone),
        ):
            out = config.format_current_time("Europe/Berlin")
        assert expected in out, (
            f"hour={hour} minute={minute}: expected substring '{expected}' "
            f"in output but got: {out!r}"
        )
        # 24-hour and AM/PM must both be present.
        assert "AM" in out or "PM" in out


# ───────────────────────────────────────────────────────────────────────────
# Storage helpers — create + edit accept user_tz
# ───────────────────────────────────────────────────────────────────────────


def test_create_dynamic_task_persists_user_tz(temp_db):
    from storage import database as task_store
    _create_dynamic_task(task_id="task-tz", schedule="0 9 * * *",
                         user_tz="America/New_York")
    row = task_store.get_dynamic_task("task-tz")
    assert row is not None
    assert row["user_tz"] == "America/New_York"


def test_create_notification_persists_user_tz(temp_db):
    from storage import notification_store
    _create_notification(nid="notif-tz", schedule="0 9 * * *",
                         user_tz="Asia/Tokyo")
    row = notification_store.get_notification("notif-tz")
    assert row is not None
    assert row["user_tz"] == "Asia/Tokyo"


def test_update_dynamic_task_can_change_user_tz(temp_db):
    from storage import database as task_store
    _create_dynamic_task(task_id="task-edit-tz", schedule="0 9 * * *",
                         user_tz="America/New_York")
    ok = task_store.update_dynamic_task(
        "task-edit-tz", {"user_tz": "Asia/Tokyo"},
    )
    assert ok
    row = task_store.get_dynamic_task("task-edit-tz")
    assert row["user_tz"] == "Asia/Tokyo"


def test_update_notification_can_change_user_tz(temp_db):
    from storage import notification_store
    _create_notification(nid="notif-edit-tz", schedule="0 9 * * *",
                         user_tz="America/New_York")
    ok = notification_store.update_notification(
        "notif-edit-tz", {"user_tz": "Europe/Athens"},
    )
    assert ok
    row = notification_store.get_notification("notif-edit-tz")
    assert row["user_tz"] == "Europe/Athens"


# ───────────────────────────────────────────────────────────────────────────
# Service layer — _register_task / _register_notification respect user_tz
# ───────────────────────────────────────────────────────────────────────────


def test_register_task_uses_user_tz_for_cron(temp_db):
    """The cron trigger's timezone should match the row's user_tz."""
    from services.scheduler import scheduler
    _set_platform_tz("Europe/Athens")

    task = scheduler.TaskDefinition(
        id="t-cron-ny", name="t", agent="a", prompt="p",
        schedule="0 9 * * *",
        user_tz="America/New_York",
        source="dynamic",
    )
    # Use the in-process scheduler directly. start() is not required for add_job.
    if scheduler._scheduler.state == 0:  # not started
        with contextlib.suppress(Exception):
            scheduler._scheduler.start()
    try:
        scheduler._register_task(task)
        job = scheduler._scheduler.get_job(f"task_{task.id}")
        assert job is not None
        # CronTrigger.timezone is the ZoneInfo we passed in.
        assert str(job.trigger.timezone) == "America/New_York"
    finally:
        with contextlib.suppress(Exception):
            scheduler._scheduler.remove_job(f"task_{task.id}")


def test_register_task_naive_run_at_uses_row_tz(temp_db):
    """Naive ISO run_at should be interpreted in user_tz, not UTC.

    Bypasses APScheduler's job machinery and asserts the trigger's run_date
    directly — that's what determines fire-time semantics.
    """
    from services.scheduler import scheduler
    from apscheduler.triggers.date import DateTrigger
    _set_platform_tz("Europe/Athens")

    # 1 hour from now in NY local. Naive ISO.
    ny = zoneinfo.ZoneInfo("America/New_York")
    target = datetime.now(ny) + timedelta(hours=1)
    naive_iso = target.replace(tzinfo=None).isoformat()

    task = scheduler.TaskDefinition(
        id="t-runat-ny", name="t", agent="a", prompt="p",
        run_at=naive_iso,
        user_tz="America/New_York",
        source="dynamic",
    )
    captured: dict = {}

    def fake_add_job(*args, **kwargs):
        captured["trigger"] = kwargs.get("trigger") or (args[1] if len(args) > 1 else None)

    monkey_target = scheduler._scheduler
    orig_add_job = monkey_target.add_job
    try:
        monkey_target.add_job = fake_add_job
        scheduler._register_task(task)
    finally:
        monkey_target.add_job = orig_add_job

    trigger = captured.get("trigger")
    assert isinstance(trigger, DateTrigger)
    # DateTrigger.run_date is timezone-aware; should equal target moment.
    delta = abs((trigger.run_date - target).total_seconds())
    assert delta < 1, f"run_date {trigger.run_date} != target {target}"
    # Verify NOT interpreted as UTC: a UTC interpretation of the same naive ISO
    # would land 4 or 5 hours earlier (NY offset).
    bad = target.replace(tzinfo=None).replace(tzinfo=timezone.utc)
    assert abs((trigger.run_date - bad).total_seconds()) > 3 * 3600


def test_register_task_user_tz_null_falls_back_to_platform(temp_db):
    from services.scheduler import scheduler
    _set_platform_tz("Europe/Athens")

    task = scheduler.TaskDefinition(
        id="t-null-tz", name="t", agent="a", prompt="p",
        schedule="0 9 * * *",
        user_tz=None,
        source="dynamic",
    )
    if scheduler._scheduler.state == 0:
        with contextlib.suppress(Exception):
            scheduler._scheduler.start()
    try:
        scheduler._register_task(task)
        job = scheduler._scheduler.get_job(f"task_{task.id}")
        assert job is not None
        assert str(job.trigger.timezone) == "Europe/Athens"
    finally:
        with contextlib.suppress(Exception):
            scheduler._scheduler.remove_job(f"task_{task.id}")


# ───────────────────────────────────────────────────────────────────────────
# update_dynamic_task / update_notification — validation
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_dynamic_task_rejects_invalid_user_tz(temp_db):
    from services.scheduler import scheduler
    _create_dynamic_task(task_id="t-invalid-tz", schedule="0 9 * * *",
                         user_tz="Europe/Athens")
    ok, err = await scheduler.update_dynamic_task(
        "t-invalid-tz", {"user_tz": "Mars/Olympus"},
    )
    assert not ok
    assert err and "user_tz" in err.lower()


@pytest.mark.asyncio
async def test_update_dynamic_task_accepts_valid_user_tz(temp_db):
    from services.scheduler import scheduler
    from storage import database as task_store

    _create_dynamic_task(task_id="t-good-tz", schedule="0 9 * * *",
                         user_tz="America/New_York")
    ok, err = await scheduler.update_dynamic_task(
        "t-good-tz", {"user_tz": "Asia/Tokyo"},
    )
    assert ok
    assert err is None
    row = task_store.get_dynamic_task("t-good-tz")
    assert row["user_tz"] == "Asia/Tokyo"


@pytest.mark.asyncio
async def test_update_notification_rejects_invalid_user_tz(temp_db):
    from services.notifications import notification_manager
    _create_notification(nid="n-bad", schedule="0 9 * * *",
                         user_tz="Europe/Athens")
    ok, err = await notification_manager.update_notification(
        "n-bad", {"user_tz": "Foo/Bar"},
    )
    assert not ok
    assert err and "user_tz" in err.lower()


# ───────────────────────────────────────────────────────────────────────────
# session_state — user_tz round-trip
# ───────────────────────────────────────────────────────────────────────────


def test_set_get_user_tz(temp_db):
    from core.session import session_state

    session_state.set_user_tz("user-1", "America/New_York")
    assert session_state.get_user_tz("user-1") == "America/New_York"
    assert session_state.get_user_tz("nobody") is None


def test_set_get_session_user_tz_persists(temp_db):
    from core.session import session_state

    sid = "session-tz-test"
    session_state.set_session_user_tz(sid, "Asia/Tokyo")
    assert session_state.get_session_user_tz(sid) == "Asia/Tokyo"

    # Round-trip via _save / _load: read what's on disk.
    session_state._save_sessions()
    # Force reload by clearing in-memory map and re-reading.
    session_state._sessions.clear()
    session_state._load_sessions()
    assert session_state.get_session_user_tz(sid) == "Asia/Tokyo"


def test_get_session_user_tz_missing_returns_none(temp_db):
    from core.session import session_state
    assert session_state.get_session_user_tz("does-not-exist") is None


# ───────────────────────────────────────────────────────────────────────────
# Standalone scheduler — reads user_tz from row
# ───────────────────────────────────────────────────────────────────────────


def test_standalone_register_task_uses_user_tz(temp_db):
    """Standalone scheduler's _register_task_job mirrors embedded behaviour."""
    # scheduler/ is commercial-only and absent from the public cut.
    standalone_scheduler = pytest.importorskip("standalone_scheduler")

    _set_platform_tz("Europe/Athens")

    if standalone_scheduler._scheduler.state == 0:
        with contextlib.suppress(Exception):
            standalone_scheduler._scheduler.start()
    try:
        ok = standalone_scheduler._register_task_job(
            "task-standalone-tz",
            "0 9 * * *",
            None,  # run_at
            None,  # delay_seconds
            None,  # created_at
            "America/New_York",  # user_tz
        )
        assert ok
        job = standalone_scheduler._scheduler.get_job("task_task-standalone-tz")
        assert job is not None
        assert str(job.trigger.timezone) == "America/New_York"
    finally:
        with contextlib.suppress(Exception):
            standalone_scheduler._scheduler.remove_job("task_task-standalone-tz")


def test_standalone_register_notification_uses_user_tz(temp_db):
    standalone_scheduler = pytest.importorskip("standalone_scheduler")

    _set_platform_tz("Europe/Athens")

    notif = {
        "id": "notif-standalone-tz",
        "notification_type": "recurring",
        "schedule": "0 9 * * *",
        "user_tz": "Asia/Tokyo",
    }

    if standalone_scheduler._scheduler.state == 0:
        with contextlib.suppress(Exception):
            standalone_scheduler._scheduler.start()
    try:
        ok = standalone_scheduler._register_notification_job(notif)
        assert ok
        job = standalone_scheduler._scheduler.get_job("notif_notif-standalone-tz")
        assert job is not None
        assert str(job.trigger.timezone) == "Asia/Tokyo"
    finally:
        with contextlib.suppress(Exception):
            standalone_scheduler._scheduler.remove_job("notif_notif-standalone-tz")
