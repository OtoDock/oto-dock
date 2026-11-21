"""The standalone scheduler end to end, on this worker's own database.

The scheduler runs as its own process, as it does beside a hosted install:
it reads the tasks and notifications the platform's stores wrote, and at the
time it posts to the proxy's internal routes with the master key, through the
platform's middleware (the master key's allowlist). The proxy side is the
real router served on a loopback port; only the run's execution is a
recorder (no engine). ``scheduler/`` is not in the public cut: this test
skips where it is absent.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

import config
from storage import database as task_store
from storage.automation import notification_store
from storage.pg import get_conn
from tests._paths import PROXY_DIR

SCRIPT = Path(PROXY_DIR).parent / "scheduler" / "standalone_scheduler.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="the standalone scheduler is not in this tree")

STARTED = "Standalone scheduler started"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Proxy:
    """The real internal router behind the platform's middleware, on a
    loopback port, in a thread of its own."""

    def __init__(self, monkeypatch):
        from api.internal import internal
        from middleware import register_middlewares
        from services.notifications import notification_manager
        from services.scheduler import runner
        self.fired_notifications: list[str] = []

        async def record_run(task, trigger_type="scheduled", trigger_source=None, **_kw):
            run_id = f"run-{uuid.uuid4().hex[:12]}"
            task_store.create_run(run_id, task.id, task.agent, trigger_type, trigger_source,
                                  task.prompt, "scheduled", task.scope, task.created_by)
            return run_id

        async def record_notification(notification_id):
            self.fired_notifications.append(notification_id)

        monkeypatch.setattr(runner, "_execute_task", record_run)
        monkeypatch.setattr(notification_manager, "_fire_scheduled_notification", record_notification)
        app = FastAPI()
        register_middlewares(app)
        app.include_router(internal.router)
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert time.monotonic() < deadline, "the proxy's router never listened"
            time.sleep(0.05)
        return self

    def __exit__(self, *_exc):
        self.server.should_exit = True
        self.thread.join(10)


class _Scheduler:
    """The standalone scheduler as its own process, its output collected."""

    def __init__(self, proxy_url: str):
        env = {**os.environ, "PROXY_INTERNAL_URL": proxy_url, "PROXY_API_KEY": config.API_KEY,
               "SCHEDULER_SYNC_INTERVAL": "1", "OTO_PROXY_ROOT": str(PROXY_DIR)}
        self.lines: list[str] = []
        self.proc = subprocess.Popen(  # the tree's own script, by its own interpreter
            [sys.executable, str(SCRIPT)], env=env, cwd=str(SCRIPT.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())

    def wait_for(self, text: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while not any(text in line for line in self.lines):
            assert self.proc.poll() is None, "the scheduler exited:\n" + "\n".join(self.lines[-30:])
            assert time.monotonic() < deadline, f"no {text!r}:\n" + "\n".join(self.lines[-30:])
            time.sleep(0.1)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.reader.join(5)


def _one_time_task(task_id: str, seconds: float) -> None:
    run_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    task_store.create_dynamic_task(
        task_id, "pa", f"e2e {task_id}", "say hi", "cli", "one-time", None, run_at, None, 60,
        "user-admin", scope="agent", notification_mode="none")


def _runs_of(task_id: str) -> list[tuple]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT trigger_type, trigger_source FROM task_runs WHERE task_id = %s",
            (task_id,)).fetchall()
    return [(r["trigger_type"], r["trigger_source"]) for r in rows]


def test_the_standalone_scheduler_fires_on_a_real_database(monkeypatch):
    with _Proxy(monkeypatch) as proxy:
        scheduler = _Scheduler(f"http://127.0.0.1:{proxy.port}")
        try:
            scheduler.wait_for(STARTED)
            # Created after it started: a run_at already past at its first
            # sync is never registered.
            _one_time_task("e2e-due", 5)
            _one_time_task("e2e-paused", 5)
            task_store.set_dynamic_task_enabled("e2e-paused", False)
            run_at = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
            notification = notification_store.create_notification(
                "e2e", "a scheduled notification", run_at=run_at, created_by="user-admin")
            deadline = time.monotonic() + 30
            while not (_runs_of("e2e-due") and proxy.fired_notifications):
                assert time.monotonic() < deadline, "nothing fired:\n" + "\n".join(scheduler.lines[-30:])
                time.sleep(0.2)
            time.sleep(3)  # the paused task's time came and went
        finally:
            scheduler.stop()
    assert _runs_of("e2e-due") == [("scheduled", "standalone-scheduler")]
    assert _runs_of("e2e-paused") == []
    assert proxy.fired_notifications == [notification["id"]]
    assert any("Task fired: e2e-due" in line for line in scheduler.lines)
