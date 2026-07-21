"""Tests for event-driven platform-timezone re-application.

When an admin changes the platform timezone on the Setup page, the Setup API
calls ``scheduler.apply_platform_timezone_change()`` directly, which re-registers
every recurring cron/interval task whose ``user_tz`` is NULL (so they pick up the
new platform TZ) — immediately, with no polling job.

Run: cd proxy && python -m pytest tests/agents/test_timezone_reapply.py -v
"""

import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


def _create_cron_task(task_id, agent="support-bot", schedule="0 9 * * *"):
    """Cron task with user_tz=NULL → its trigger TZ follows the platform TZ."""
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Cron", "do thing", "cli",
        "scheduled", schedule, None, None, 600,
        "user-1", None, None, None, None, False,
        scope="user",
    )


class TestApplyTimezoneChange:
    def test_changing_timezone_reregisters_recurring_jobs(self, temp_db, monkeypatch):
        from datetime import datetime, timedelta, timezone as _tz

        import config
        from services.scheduler import scheduler
        from storage import database as task_store

        # Start at TZ-A. A recurring (cron) task plus a one-time task; only the
        # recurring one should be re-registered when the platform TZ changes.
        task_store.set_platform_setting("platform_timezone", "America/New_York")
        config._tz_cache["tz"] = None
        scheduler._current_tz = "America/New_York"

        _create_cron_task("task-cron-tz")
        future = (datetime.now(_tz.utc) + timedelta(days=2)).isoformat()
        task_store.create_dynamic_task(
            "task-once-tz", "support-bot", "Once", "do", "cli",
            "one_time", None, future, None, 600,
            "user-1", None, None, None, None, False, scope="user",
        )

        # Spy on _register_task (it's a module global, so _check_timezone_change
        # resolves the patched version at call time) — capture which tasks get
        # re-registered and the platform TZ each resolves to.
        reregistered: dict[str, str] = {}

        def _spy(task):
            reregistered[task.id] = str(scheduler._resolve_task_tz(task))

        monkeypatch.setattr(scheduler, "_register_task", _spy)

        try:
            task_store.set_platform_setting("platform_timezone", "Asia/Tokyo")
            config._tz_cache["tz"] = None
            scheduler.apply_platform_timezone_change()

            assert scheduler._current_tz == "Asia/Tokyo"
            assert "task-cron-tz" in reregistered       # recurring → re-registered
            assert "task-once-tz" not in reregistered    # one-time → left alone
            # A user_tz=NULL task re-registers under the NEW platform TZ.
            assert "Asia/Tokyo" in reregistered["task-cron-tz"]
        finally:
            config._tz_cache["tz"] = None
            scheduler._current_tz = config.SCHEDULER_TIMEZONE

    def test_same_timezone_is_a_noop(self, temp_db):
        import config
        from services.scheduler import scheduler
        from storage import database as task_store

        task_store.set_platform_setting("platform_timezone", "Europe/Athens")
        config._tz_cache["tz"] = None
        scheduler._current_tz = "Europe/Athens"
        try:
            # No change → _current_tz stays put, nothing re-registers.
            scheduler.apply_platform_timezone_change()
            assert scheduler._current_tz == "Europe/Athens"
        finally:
            config._tz_cache["tz"] = None
            scheduler._current_tz = config.SCHEDULER_TIMEZONE

    def test_standalone_mode_is_a_noop(self, temp_db, monkeypatch):
        """In standalone mode an external scheduler owns registration."""
        import config
        from services.scheduler import scheduler

        monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
        scheduler._current_tz = "America/New_York"
        try:
            scheduler.apply_platform_timezone_change()
            # Guard returns before touching _current_tz.
            assert scheduler._current_tz == "America/New_York"
        finally:
            scheduler._current_tz = config.SCHEDULER_TIMEZONE
