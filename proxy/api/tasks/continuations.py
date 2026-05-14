"""Scheduled self-continuations — ``POST /v1/continuations``.

A continuation wakes the CALLING session's chat at a future time: the prompt
is delivered into that very conversation as a new turn (dead sessions resume).
Authority is the session token itself — a continuation can only target the
chat the token drives, so no scope/role matrix applies. Backs the
schedules-mcp ``schedule_continuation`` tool.

One-shot: exactly one of ``at`` / ``in_seconds``. Recurring: ``repeat_cron``
or ``repeat_interval_seconds``, ALWAYS bounded — ``max_runs`` (default 5)
and/or an ``until`` time; a chat must never wake itself forever. Rows are
ordinary dynamic tasks (task_type='continuation'): they show in list_tasks,
cancel via delete_task, pause via pause_task, and auto-cancel when the chat
is deleted.
"""

import asyncio
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_auth
from core.session.session_state import get_user_tz
from services.scheduler import scheduler
from storage import database as task_store

logger = logging.getLogger("claude-proxy.continuations")
router = APIRouter()

_IN_SECONDS_MIN = 30
_MAX_RUNS_CAP = 100
_DEFAULT_MAX_RUNS = 5


class ContinuationRequest(BaseModel):
    prompt: str
    name: str = ""
    at: str | None = None
    in_seconds: int | None = None
    repeat_cron: str | None = None
    repeat_interval_seconds: int | None = None
    max_runs: int | None = None
    until: str | None = None


@router.post("/v1/continuations")
async def create_continuation(
    req: ContinuationRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if not (req.prompt or "").strip():
        raise HTTPException(400, "prompt is required.")

    timing = [k for k in ("at", "in_seconds", "repeat_cron", "repeat_interval_seconds")
              if getattr(req, k) is not None]
    if len(timing) != 1:
        raise HTTPException(
            400,
            "Provide exactly ONE timing field: at | in_seconds (one-shot) or "
            "repeat_cron | repeat_interval_seconds (recurring).",
        )
    recurring = timing[0] in ("repeat_cron", "repeat_interval_seconds")

    if req.at is not None:
        try:
            datetime.fromisoformat(req.at)
        except ValueError:
            raise HTTPException(400, f"Invalid ISO datetime for at: {req.at!r}")
    if req.in_seconds is not None and not (
            _IN_SECONDS_MIN <= req.in_seconds <= scheduler.INTERVAL_MAX_SECONDS):
        raise HTTPException(
            400, f"in_seconds must be between {_IN_SECONDS_MIN} and "
                 f"{scheduler.INTERVAL_MAX_SECONDS}.")
    if req.repeat_interval_seconds is not None:
        err = scheduler._validate_interval_seconds(req.repeat_interval_seconds)
        if err:
            raise HTTPException(400, err)
    if req.until is not None:
        try:
            datetime.fromisoformat(req.until)
        except ValueError:
            raise HTTPException(400, f"Invalid ISO datetime for until: {req.until!r}")

    max_runs = req.max_runs
    if max_runs is not None and not (1 <= max_runs <= _MAX_RUNS_CAP):
        raise HTTPException(400, f"max_runs must be between 1 and {_MAX_RUNS_CAP}.")
    if recurring and max_runs is None and req.until is None:
        max_runs = _DEFAULT_MAX_RUNS
    if not recurring:
        # One-shot rows fire once by construction; bounds don't apply.
        max_runs = None

    # The session token IS the authority: a continuation targets the chat the
    # calling session drives, nothing else.
    if not u.session_id:
        raise HTTPException(400, "Continuations require a session caller.")
    chat = await asyncio.to_thread(task_store.get_chat_by_session, u.session_id)
    if not chat:
        raise HTTPException(
            404, "This session has no chat yet — nothing to continue.")

    owner = chat.get("user_sub") or ""
    synthetic = "::" in owner or owner == "phone"
    scope = "agent" if synthetic else "user"
    created_by = (u.acting_sub or u.agent or chat["agent"]) if synthetic else owner
    user_tz = get_user_tz(created_by) if scope == "user" else None

    task_id = f"dyn-{uuid.uuid4().hex[:8]}"
    task = scheduler.TaskDefinition(
        id=task_id,
        name=req.name or f"Continuation: {req.prompt[:40]}",
        agent=chat["agent"],
        prompt=req.prompt,
        task_type="continuation",
        target_chat_id=chat["id"],
        run_at=req.at,
        delay_seconds=req.in_seconds,
        schedule=req.repeat_cron or "",
        interval_seconds=req.repeat_interval_seconds,
        max_runs=max_runs,
        until_at=req.until,
        created_by=created_by,
        scope=scope,
        notification_mode="none",
        user_tz=user_tz,
    )
    await scheduler.add_dynamic_task(task)

    fires = {
        "at": f"at {req.at}",
        "in_seconds": f"in {req.in_seconds}s",
        "repeat_cron": f"cron '{req.repeat_cron}'",
        "repeat_interval_seconds": f"every {req.repeat_interval_seconds}s",
    }[timing[0]]
    logger.info(
        f"Continuation created: {task_id} chat={chat['id'][:8]} {fires} "
        f"max_runs={max_runs or '-'} until={req.until or '-'} by={created_by}"
    )
    return {
        "task_id": task_id,
        "chat_id": chat["id"],
        "fires": fires,
        "max_runs": max_runs,
        "until": req.until,
    }
