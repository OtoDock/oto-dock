"""The person's notifications through a key (scope ``notifications``): send
one to yourself now, and read your inbox.

A key names no recipient and makes nothing recurring or delayed: the
notification goes to the key's person at once. A key that names its agents
reads the notifications of those agents and the ones of no agent.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api.public.common import key_user, person_of, reaches, record, require_agent
from auth.providers import UserContext
from storage.automation import notification_store
from storage.pg import run_db

router = APIRouter(tags=["notifications"])

#: How far back a key that names its agents reads to fill a page.
_SCAN_MAX = 1000


class NewNotification(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=4000)
    severity: Literal["info", "success", "warning", "danger"] = "info"
    agent: str = ""


class Sent(BaseModel):
    id: str


class Delivery(BaseModel):
    id: str
    title: str
    body: str
    severity: str
    created_at: str
    read: bool


class Inbox(BaseModel):
    notifications: list[Delivery]


@router.post("/v1/api/notifications", response_model=Sent)
async def send(req: NewNotification, u: UserContext = Depends(key_user)) -> Sent:
    """A notification to yourself, now."""
    from api.notifications.notifications import (
        CreateNotificationRequest,
        create_notification,
    )
    if req.agent:
        require_agent(u, req.agent)
    answer = await create_notification(
        req=CreateNotificationRequest(title=req.title, body=req.body, severity=req.severity,
                                      scope="user", target=None, notification_type="one_time",
                                      source="api", agent_slug=req.agent or None),
        user=person_of(u), x_agent_name=None)
    notif = answer["notification"]
    await run_db(record, u, "notification", str(notif["id"]), label=req.title, agent=req.agent)
    return Sent(id=str(notif["id"]))


@router.get("/v1/api/notifications", response_model=Inbox)
async def inbox(limit: int = Query(50, ge=1, le=200), u: UserContext = Depends(key_user)) -> Inbox:
    """Your newest notifications. A key that names its agents reads theirs,
    and those of no agent."""
    def _job() -> list[dict]:
        if not u.api_key_agents:
            return notification_store.list_deliveries(u.sub, limit)
        rows = notification_store.list_deliveries(u.sub, min(limit * 5, _SCAN_MAX))
        return [r for r in rows
                if not r.get("agent_slug") or reaches(u, r["agent_slug"])][:limit]

    rows = await run_db(_job)
    return Inbox(notifications=[
        Delivery(id=str(r.get("id") or ""), title=r.get("title") or "", body=r.get("body") or "",
                 severity=r.get("severity") or "info",
                 created_at=str(r.get("created_at") or r.get("delivered_at") or ""),
                 read=bool(r.get("read_at") or r.get("read")))
        for r in rows])
