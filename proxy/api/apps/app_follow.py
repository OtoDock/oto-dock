"""The routes of an app that follows its source (services/apps/app_follow.py):
take the source's newer release, ask the agent to merge it, send this app's
release to the source, stop following. A person at the keyboard presses them
(``require_human``, as rollback and approve), whoever may manage the row; the
module's own checks and sentences answer."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.apps.apps import _visible_row
from auth.providers import UserContext, get_current_user, require_human
from services.apps import app_follow

router = APIRouter()


class TakeRequest(BaseModel):
    mode: str = app_follow.MODE_UPDATE


def _row(app_id: str, u: UserContext) -> dict:
    row = _visible_row(app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    return row


@router.post("/v1/apps/{app_id}/follow/take")
async def take_release(app_id: str, req: TakeRequest | None = None,
                       user: UserContext | None = Depends(get_current_user)):
    """``update``: the source's live release onto this app, held for
    approval here; ``merge``: unpacked beside this app's files for the agent."""
    u = require_human(user)
    row = await asyncio.to_thread(_row, app_id, u)
    mode = (req.mode if req else app_follow.MODE_UPDATE) or app_follow.MODE_UPDATE
    if mode not in app_follow.MODES:
        raise HTTPException(status_code=400, detail="The mode is update or merge.")
    try:
        if mode == app_follow.MODE_MERGE:
            out = await app_follow.stage_merge(row, u)
        else:
            out = await app_follow.take(row, u)
    except app_follow.FollowRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"status": "ok", "mode": mode, **out}


@router.post("/v1/apps/{app_id}/follow/send")
async def send_release_to_source(app_id: str, user: UserContext | None = Depends(get_current_user)):
    u = require_human(user)
    row = await asyncio.to_thread(_row, app_id, u)
    try:
        out = await app_follow.send_back(row, u)
    except app_follow.FollowRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"status": "ok", **out}


@router.delete("/v1/apps/{app_id}/follow")
async def stop_following(app_id: str, user: UserContext | None = Depends(get_current_user)):
    u = require_human(user)
    row = await asyncio.to_thread(_row, app_id, u)
    if not app_follow.parse(row):
        raise HTTPException(status_code=409, detail=app_follow.NOT_FOLLOWING)
    if not app_follow.may_manage(row, u):
        raise HTTPException(status_code=403, detail=app_follow.needs_role(row))
    await asyncio.to_thread(app_follow.stop, row)
    return {"status": "ok"}
