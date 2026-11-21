"""The person's tasks through a key (scope ``tasks``): list them, create one
that runs when asked, run one now, read a run and stream it.

What a key reaches: the person's own user-scope tasks, and the agent-scope
tasks of an agent the key reaches where the person is at the editor tier,
never a colleague's personal task (an admin's key included). It runs an
agent-scope task as the dashboard lets its person: any where they manage
the agent, the ones they made where they are an editor. It runs only
tasks whose run is a session of its own (one-time, scheduled, run when
asked), never a continuation, a delegated worker, an app's handler or a
check. A key creates nothing that runs later by itself: a task it makes
runs when it is asked to (by the key, or a trigger the person links).

A task's run has nobody to ask, so every route here takes only a key made
to run without asking (``tasks_user``): the mint refuses the ``tasks``
permission without it, and the routes check the key's row again.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.public.common import (
    key_user,
    person_of,
    reaches,
    record,
    require_agent,
    take_wait,
)
from auth.providers import UserContext
from core.permission_modes import AUTO_REVIEW_MODE
from core.session import visibility as _vis
from services.scheduler import task_kinds
from storage import database as task_store
from storage.pg import run_db

router = APIRouter(tags=["tasks"])

_RUNNABLE = frozenset({task_kinds.ONE_TIME, task_kinds.SCHEDULED, task_kinds.TRIGGER})
NOBODY_TO_ASK = "A task runs with nobody to ask. This key was not made to run without asking."
MANAGERS_TASK = "Only a manager of this agent can run this task. An editor runs the tasks they made."


async def tasks_user(u: UserContext = Depends(key_user)) -> UserContext:
    """The key of a task route: one made to run without asking (module
    docstring)."""
    if not u.api_key_runs_unasked:
        raise HTTPException(403, NOBODY_TO_ASK)
    return u


class TaskView(BaseModel):
    id: str
    name: str
    agent: str
    scope: str
    kind: str
    schedule: str
    runs_without_asking: bool


class TasksAnswer(BaseModel):
    tasks: list[TaskView]


class NewTask(BaseModel):
    agent: str
    name: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=100_000)
    scope: Literal["user", "agent"] = "user"
    run_now: bool = True
    notification_mode: Literal["auto", "manual", "none"] = "none"
    timeout_seconds: int = Field(600, ge=30, le=7200)
    model: str | None = None
    layer: str | None = None


class Started(BaseModel):
    task_id: str
    run_id: str | None


class RunView(BaseModel):
    id: str
    task_id: str
    agent: str
    status: str
    started_at: str | None
    completed_at: str | None
    output_text: str
    error_message: str
    chat_id: str | None


def _task_view(row: dict) -> TaskView:
    return TaskView(id=row["id"], name=row.get("name") or "", agent=row.get("agent") or "",
                    scope=row.get("scope") or _vis.SCOPE_AGENT,
                    kind=row.get("task_type") or "",
                    schedule=row.get("schedule") or "",
                    runs_without_asking=(row.get("permission_mode") or "") != AUTO_REVIEW_MODE)


def _sees_task(u: UserContext, row: dict) -> bool:
    """Whether the key reaches this task (module docstring). Synchronous."""
    agent = row.get("agent") or ""
    if not reaches(u, agent):
        return False
    if (row.get("scope") or _vis.SCOPE_AGENT) == _vis.SCOPE_USER:
        return row.get("created_by") == u.sub
    return u.can_edit_agent(agent)


def _may_run(u: UserContext, row: dict) -> bool:
    """Whether the person may run a task the key reaches: the dashboard's
    rule (``api/tasks/tasks.run_task_now``). An agent-scope task is a
    manager's to run, and its maker's. A user-scope one the key reaches is
    the person's own. Synchronous."""
    if (row.get("scope") or _vis.SCOPE_AGENT) == _vis.SCOPE_USER:
        return True
    return u.can_manage_agent(row.get("agent") or "") or row.get("created_by") == u.sub


def _runnable(row: dict) -> bool:
    kind = task_kinds.of_word(row.get("task_type") or "")
    return (kind is not None and kind.name in _RUNNABLE and not row.get("use_persistent")
            and not row.get("on_complete_agent") and not row.get("on_complete_session_id")
            and not row.get("target_chat_id"))


def _sees_run(u: UserContext, run: dict) -> bool:
    agent = run.get("agent") or ""
    if not reaches(u, agent):
        return False
    if (run.get("scope") or _vis.SCOPE_AGENT) == _vis.SCOPE_USER:
        return run.get("created_by") == u.sub
    return u.can_edit_agent(agent)


@router.get("/v1/api/tasks", response_model=TasksAnswer)
async def list_tasks(agent: str = Query(""), u: UserContext = Depends(tasks_user)) -> TasksAnswer:
    """The tasks the key may run."""
    if agent:
        require_agent(u, agent)

    def _job() -> list[TaskView]:
        rows = task_store.list_dynamic_tasks(agent or None)
        return [_task_view(r) for r in rows
                if _sees_task(u, r) and _may_run(u, r) and _runnable(r)]

    return TasksAnswer(tasks=await run_db(_job))


@router.post("/v1/api/tasks", response_model=Started)
async def create_task(req: NewTask, u: UserContext = Depends(tasks_user)) -> Started:
    """A task that runs when asked, run now unless ``run_now`` is false."""
    from api.tasks import tasks as tasks_api
    require_agent(u, req.agent)
    created = await tasks_api.create_one_time_task(
        req=tasks_api.CreateOneTimeTaskRequest(
            name=req.name, agent=req.agent, prompt=req.prompt, scope=req.scope,
            notification_mode=req.notification_mode, timeout_seconds=req.timeout_seconds,
            task_type=task_kinds.TRIGGER, model=req.model, layer=req.layer),
        user=person_of(u), x_agent_name=None)
    task_id = created["task_id"]
    await run_db(record, u, "task", task_id, label=req.name, agent=req.agent)
    run_id = await _start(u, task_id) if req.run_now else None
    return Started(task_id=task_id, run_id=run_id)


async def _start(u: UserContext, task_id: str) -> str:
    from services.scheduler import scheduler
    row = await run_db(task_store.get_dynamic_task, task_id)
    if not row or not _sees_task(u, row):
        raise HTTPException(404, "This key reaches no such task.")
    if not _may_run(u, row):
        raise HTTPException(403, MANAGERS_TASK)
    if not _runnable(row):
        raise HTTPException(409, "An API key cannot run this kind of task.")
    return await scheduler.trigger_task_now(scheduler._row_to_task(row),
                                            trigger_type=task_kinds.TRIGGER_MANUAL,
                                            trigger_source="api_key")


@router.post("/v1/api/tasks/{task_id}/run", response_model=Started)
async def run_task(task_id: str, u: UserContext = Depends(tasks_user)) -> Started:
    """Run a task now."""
    return Started(task_id=task_id, run_id=await _start(u, task_id))


async def _reached_run(u: UserContext, run_id: str) -> dict:
    run = await run_db(task_store.get_run, run_id)
    if not run or not _sees_run(u, run):
        raise HTTPException(404, "This key reaches no such run.")
    return run


@router.get("/v1/api/runs/{run_id}", response_model=RunView)
async def read_run(run_id: str, u: UserContext = Depends(tasks_user)) -> RunView:
    """A run's state and, once it ended, its result."""
    run = await _reached_run(u, run_id)
    return RunView(id=run["id"], task_id=run.get("task_id") or "", agent=run.get("agent") or "",
                   status=run.get("status") or "", started_at=run.get("started_at"),
                   completed_at=run.get("completed_at"), output_text=run.get("output_text") or "",
                   error_message=run.get("error_message") or "", chat_id=run.get("chat_id"))


@router.get("/v1/api/runs/{run_id}/stream",
            responses={200: {"description": "Server-sent events, to the run's end",
                             "content": {"text/event-stream": {}}}})
async def stream_run(run_id: str, u: UserContext = Depends(tasks_user)) -> StreamingResponse:
    """The run's output as server-sent events, to its end."""
    from api.tasks.tasks import run_event_stream
    await _reached_run(u, run_id)
    give_back = take_wait(u)

    async def _held():
        try:
            async for frame in run_event_stream(run_id):
                yield frame
        finally:
            give_back()

    return StreamingResponse(_held(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
