"""Run-stream wait mode (tour finding 2, fixed 2026-07-19).

- The schedules-mcp Timeout construction: httpx >= 0.28 requires a default (or
  all four params) — the old ``Timeout(connect=..., read=...)`` form raised
  ValueError before the SSE ever opened. Pin both shapes.
- ``/v1/tasks/runs/{id}/stream`` emits one ``status`` frame before
  subscribing, so a parked ("pending") run explains itself instead of
  streaming only keep-alives.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from storage import database as task_store


class TestTimeoutConstruction:
    def test_old_partial_kwargs_form_raises(self):
        with pytest.raises(ValueError):
            httpx.Timeout(connect=5.0, read=630.0)

    def test_fixed_default_plus_override_form_works(self):
        t = httpx.Timeout(630.0, connect=5.0)
        assert t.read == 630.0 and t.connect == 5.0


@pytest.fixture()
def stream_client(temp_db, monkeypatch):
    from api.tasks import tasks as tasks_api

    class _Q:
        async def get(self):
            return {"type": "done", "status": "completed"}

    async def _subscribe(run_id):
        return _Q()

    monkeypatch.setattr(tasks_api.scheduler, "subscribe_run", _subscribe)
    monkeypatch.setattr(tasks_api.scheduler, "unsubscribe_run",
                        lambda run_id, q: None)
    monkeypatch.setattr(config, "is_master_key", lambda k: k == "test-key")

    app = FastAPI()
    app.include_router(tasks_api.router)
    return TestClient(app)


def _frames(resp_text: str) -> list[dict]:
    out = []
    for line in resp_text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


class TestStreamStatusFrame:
    def test_pending_run_emits_status_frame_before_events(self, stream_client):
        task_store.create_run("run-s1", "task-x", "pa", "manual", None, "p")
        r = stream_client.get("/v1/tasks/runs/run-s1/stream?key=test-key")
        assert r.status_code == 200
        frames = _frames(r.text)
        assert frames[0] == {"type": "status", "status": "pending"}
        assert frames[-1]["type"] == "done"

    def test_terminal_run_has_no_status_frame(self, stream_client):
        task_store.create_run("run-s2", "task-x", "pa", "manual", None, "p")
        task_store.update_run("run-s2", status="completed", output_text="hi")
        r = stream_client.get("/v1/tasks/runs/run-s2/stream?key=test-key")
        frames = _frames(r.text)
        assert [f["type"] for f in frames] == ["text", "done"]


class TestQueuedRunVisibility:
    """The run's chat row + runs.chat_id exist BEFORE the admission slot —
    a parked run must render as an honest 'queued' page, not 'Chat not
    found' (tour finding 3)."""

    def test_chat_row_exists_while_run_still_pending(self, temp_db, monkeypatch):
        import asyncio
        from services.scheduler import scheduler

        started = asyncio.Event()
        release = asyncio.Event()

        async def _parked_run_task(run_id, session_id, task, *a, **kw):
            started.set()
            await release.wait()

        monkeypatch.setattr(scheduler, "_run_task", _parked_run_task)
        monkeypatch.setattr(config, "get_cli_model", lambda agent: "test-model")

        async def _drive():
            task = scheduler.TaskDefinition(
                id="task-q1", name="q", agent="pa", prompt="p", scope="agent",
            )
            run_id = await scheduler._execute_task(task, trigger_type="manual")
            await asyncio.wait_for(started.wait(), timeout=5)
            run = task_store.get_run(run_id)
            chat = task_store.get_chat(f"task-{run_id}")
            release.set()
            return run, chat

        run, chat = asyncio.run(_drive())
        assert run["status"] == "pending"
        assert run["chat_id"] == f"task-{run['id']}"
        assert chat is not None
        assert chat["user_sub"] == "task::pa"
