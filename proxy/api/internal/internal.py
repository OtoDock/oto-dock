"""Internal service-to-service API endpoints.

Used by the standalone scheduler to fire tasks and notifications on the proxy.
Protected by PROXY_API_KEY — only accepts API key auth, rejects session cookies.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
)
from services.notifications import notification_manager
from storage import database as task_store

logger = logging.getLogger("otodock.internal-api")
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
    logger.info("Internal fire-task: task=%s, run=%s", task_id, run_id)
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
    logger.info("Internal fire-notification: %s", notification_id)
    return {"status": "fired", "notification_id": notification_id}


def _from_this_machine(request: Request) -> bool:
    """The caller sits on the server's own machine and nothing forwarded it:
    the peer is loopback, or the one address the server binds, and the
    request carries no forwarding header (an edge on this machine is a
    loopback peer too)."""
    import ipaddress

    import config
    if any(request.headers.get(h) for h in ("x-forwarded-for", "forwarded", "x-real-ip")):
        return False
    peer = request.client.host if request.client else ""
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    host = (config.HOST or "").strip()
    try:
        return bool(host) and addr == ipaddress.ip_address(host) and not addr.is_unspecified
    except ValueError:
        return False


def _require_admin_command(request: Request, user: UserContext | None) -> None:
    """The admin command's caller: the master key, from this machine. A
    service on another host holds the key too and must not stop the
    platform's work."""
    u = require_auth(user)
    if not u.is_service:
        raise HTTPException(403, "Internal endpoint requires the master service key")
    if not _from_this_machine(request):
        raise HTTPException(403, "This endpoint answers only the server's own machine")


@router.get("/v1/internal/work")
async def internal_work(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """The planned stop's state and what is working right now. Called by the
    admin command (``status``, and ``drain`` while it waits)."""
    _require_admin_command(request, user)
    from core import drain
    return drain.work()


@router.post("/v1/internal/drain")
async def internal_drain(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Hold new work and wait for what is open, or call that off. Called by
    the admin command before a stop or an update.

    Body: {"action": "drain", "bound_s": 600, "away_s": 900} or
    {"action": "resume"}. Returns the same block as ``/v1/internal/work``,
    at once: a drain's wait is read there.
    """
    _require_admin_command(request, user)
    import config
    from core import drain

    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "A JSON object is required")
    action = body.get("action")
    if action == "resume":
        work = await drain.resume()
        logger.info("Internal drain: resumed")
        return work
    if action != "drain":
        raise HTTPException(400, 'action is "drain" or "resume"')

    def seconds(key: str, default: int) -> int:
        value = body.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise HTTPException(400, f"{key} is a number of seconds, 0 or more")
        return int(value)

    bound_s = seconds("bound_s", config.DRAIN_BOUND_S)
    away_s = seconds("away_s", drain.DEFAULT_AWAY_S)
    work = await drain.drain(bound_s, away_s)
    logger.info("Internal drain: asked with a bound of %d s, state %s", bound_s, work["state"])
    return work
