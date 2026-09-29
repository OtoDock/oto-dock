"""Internal service-to-service API endpoints.

Used by the standalone scheduler to fire tasks and notifications on the proxy.
Protected by PROXY_API_KEY — only accepts API key auth, rejects session cookies.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from storage import database as task_store
from services.notifications import notification_manager
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
)

logger = logging.getLogger("claude-proxy.internal-api")
router = APIRouter()


@router.post("/v1/internal/fire-task")
async def internal_fire_task(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Fire a task immediately. Called by the standalone scheduler.

    Body: {"task_id": "...", "trigger_type": "scheduled"}
    Returns: {"run_id": "run-...", "task_id": "..."}
    """
    u = require_auth(user)
    if not u.is_service:
        raise HTTPException(403, "Internal endpoint requires the master service key")

    from services.scheduler import scheduler, task_kinds

    body = await request.json()
    task_id = body.get("task_id")
    trigger_type = body.get("trigger_type", task_kinds.TRIGGER_SCHEDULED)

    if not task_id:
        raise HTTPException(400, "task_id is required")

    from storage.pg import run_db
    dyn = await run_db(task_store.get_dynamic_task, task_id)
    if not dyn:
        raise HTTPException(404, f"Task not found: {task_id}")
    if not dyn.get("enabled", True):
        # The runner drops a paused row's fire; this channel follows it.
        raise HTTPException(409, f"Task is paused: {task_id}")
    task_def = scheduler._row_to_task(dyn)

    run_id = await scheduler.trigger_task_now(
        task_def,
        trigger_type=trigger_type,
        trigger_source="standalone-scheduler",
    )
    logger.info(f"Internal fire-task: task={task_id}, run={run_id}")
    return {"run_id": run_id, "task_id": task_id}


@router.post("/v1/internal/fire-notification")
async def internal_fire_notification(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Fire a scheduled notification. Called by the standalone scheduler.

    Body: {"notification_id": "..."}
    Returns: {"status": "fired"}
    """
    u = require_auth(user)
    if not u.is_service:
        raise HTTPException(403, "Internal endpoint requires the master service key")

    body = await request.json()
    notification_id = body.get("notification_id")

    if not notification_id:
        raise HTTPException(400, "notification_id is required")

    # Reuse the existing scheduled notification fire path
    await notification_manager._fire_scheduled_notification(notification_id)
    logger.info(f"Internal fire-notification: {notification_id}")
    return {"status": "fired", "notification_id": notification_id}
