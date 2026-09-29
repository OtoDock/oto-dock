"""Tests for `scheduler.get_scheduled_jobs()` — the source for the dashboard's
"Upcoming" schedules list and the "Scheduled Tasks" stat-card count.

Only real user/agent dynamic tasks (job id prefixed ``task_``) must be reported.
Internal APScheduler housekeeping jobs — e.g. ``_tz_sync`` (the per-minute
platform-timezone watcher registered in ``scheduler.start()``) — are
implementation details and must never surface in the dashboard.

Run: cd proxy && python -m pytest tests/tasks/test_scheduled_jobs_listing.py -v
"""

import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


def _create_cron_task(task_id, agent="support-bot", schedule="*/10 * * * *"):
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Cron", "do thing", "cli",
        "scheduled", schedule, None, None, 600,
        "user-1", None, None, None, None, False,
        scope="user",
    )


class TestScheduledJobsExcludeInternal:
    def test_tz_sync_is_not_listed(self, temp_db):
        """The internal _tz_sync housekeeping job must not appear as a schedule."""
        from services.scheduler import scheduler

        # Register the internal job exactly like scheduler.start() does.
        scheduler._scheduler.add_job(
            scheduler._check_timezone_change, "interval", minutes=1,
            id="_tz_sync", replace_existing=True,
        )
        try:
            # And a real user task alongside it.
            _create_cron_task("task-real-1")
            row = temp_db.get_dynamic_task("task-real-1")
            scheduler._register_task(scheduler._row_to_task(row))

            jobs = scheduler.get_scheduled_jobs()
            ids = {j["id"] for j in jobs}

            assert "_tz_sync" not in ids
            assert not any(j["name"] == "_tz_sync" for j in jobs)
            # The real task is still reported, with its DB-resolved name/agent.
            assert "task_task-real-1" in ids
            real = next(j for j in jobs if j["id"] == "task_task-real-1")
            assert real["task_id"] == "task-real-1"
            assert real["name"] == "Cron"
            assert real["agent"] == "support-bot"
        finally:
            scheduler._scheduler.remove_job("_tz_sync")
            if scheduler._scheduler.get_job("task_task-real-1"):
                scheduler._scheduler.remove_job("task_task-real-1")

    def test_only_task_prefixed_jobs_reported(self, temp_db):
        """Any non-`task_` job id is treated as internal and filtered out."""
        from services.scheduler import scheduler

        scheduler._scheduler.add_job(
            scheduler._check_timezone_change, "interval", minutes=5,
            id="_some_future_internal_job", replace_existing=True,
        )
        try:
            jobs = scheduler.get_scheduled_jobs()
            assert all(j["id"].startswith("task_") for j in jobs)
        finally:
            scheduler._scheduler.remove_job("_some_future_internal_job")


class TestNextRunTimes:
    """The listing's one next-fire map: the in-memory jobs in embedded mode,
    one computation over the definitions in standalone mode, and internal
    jobs in neither."""

    def test_embedded_reads_the_jobs_once(self, temp_db):
        from services.scheduler import scheduler
        scheduler._scheduler.add_job(
            scheduler._check_timezone_change, "interval", minutes=1,
            id="_tz_sync", replace_existing=True,
        )
        try:
            _create_cron_task("task-map-1")
            scheduler._register_task(scheduler._row_to_task(temp_db.get_dynamic_task("task-map-1")))
            fires = scheduler.next_run_times()
            assert set(fires) >= {"task-map-1"} and "_tz_sync" not in fires
            assert fires["task-map-1"] == scheduler.next_run_time("task-map-1")
        finally:
            scheduler._scheduler.remove_job("_tz_sync")
            if scheduler._scheduler.get_job("task_task-map-1"):
                scheduler._scheduler.remove_job("task_task-map-1")

    def test_standalone_computes_from_the_definitions(self, temp_db, monkeypatch):
        import config
        from services.scheduler import scheduler
        monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
        _create_cron_task("task-map-2")
        _create_cron_task("task-map-off")
        temp_db.set_dynamic_task_enabled("task-map-off", False)
        fires = scheduler.next_run_times()
        assert "task-map-2" in fires and fires["task-map-2"]
        assert "task-map-off" not in fires
