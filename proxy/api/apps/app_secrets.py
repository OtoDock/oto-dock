"""The values behind an app's declared secrets (APPS.md "Secrets"): set,
rotated and removed by a person — the owner of a personal app, an editor or
manager of a shared one, an admin — never by an agent (``require_human``
refuses every bearer principal) and never by the platform's own render
principal. No route returns a value; the listing never decrypts.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.apps.apps import _visible_row
from auth.providers import UserContext, get_current_user, require_auth, require_human, require_user
from services.apps import app_deploy, app_secrets
from storage import db_app_secrets, db_apps

logger = logging.getLogger("claude-proxy.apps")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])


class SecretValue(BaseModel):
    value: str = ""


def _can_set_secrets(row: dict, user: UserContext) -> bool:
    """``_can_approve_surface`` without the render principal: an admin, the
    owner of a personal app, an editor or a manager of a shared one."""
    if user.is_admin:
        return True
    if row.get("username"):
        return (row.get("owner_sub") or "") == user.sub
    return user.can_edit_agent(row.get("agent") or "")


async def _row_for(app_id: str, u: UserContext) -> dict:
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row or not db_apps.app_kind_of(row).has_settings:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_set_secrets(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to manage this app's secrets")
    return row


def _check_value(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="a value is required")
    if len(value.encode("utf-8")) > db_app_secrets.VALUE_MAX_BYTES:
        raise HTTPException(status_code=400, detail="a value is at most 8 KB")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise HTTPException(status_code=400, detail="a value is text (a key may span lines)")
    return value.strip("\r\n")


@router.get("/v1/apps/{app_id}/secrets")
async def list_secrets(app_id: str, user: UserContext | None = Depends(get_current_user)):
    """The declared names with whether each is set, who set it and when —
    for the settings panel. Names only; nothing is decrypted."""
    u = require_auth(user)
    row = await _row_for(app_id, u)
    return {"secrets": await asyncio.to_thread(app_secrets.status_for, row),
            "waiting": await asyncio.to_thread(app_secrets.waiting_reason, row)}


@router.put("/v1/apps/{app_id}/secrets/{name}")
async def set_secret(app_id: str, name: str, req: SecretValue,
                     user: UserContext | None = Depends(get_current_user)):
    """Set or replace one declared secret's value. The running live and
    preview instances are stopped so the next request relaunches them with
    the new value; the answer never carries it."""
    u = require_human(user)
    row = await _row_for(app_id, u)
    if not app_deploy.SECRET_NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="not a valid secret name")
    if name not in {s["name"] for s in app_secrets.declared(row)}:
        raise HTTPException(status_code=400,
                            detail=f"{name} is not declared in the app's manifest")
    value = _check_value(req.value)
    await asyncio.to_thread(db_app_secrets.set_value, row["id"], name, value, u.sub)
    restarted = await app_secrets.restart_after_change(row)
    logger.info("App secret set: app=%s name=%s by=%s restarted=%d", row["slug"], name,
                u.sub[:8], restarted)
    return {"status": "ok", "name": name, "restarted": restarted}


@router.delete("/v1/apps/{app_id}/secrets/{name}")
async def delete_secret(app_id: str, name: str,
                        user: UserContext | None = Depends(get_current_user)):
    u = require_human(user)
    row = await _row_for(app_id, u)
    if not app_deploy.SECRET_NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="not a valid secret name")
    removed = await asyncio.to_thread(db_app_secrets.delete_value, row["id"], name)
    restarted = await app_secrets.restart_after_change(row) if removed else 0
    logger.info("App secret removed: app=%s name=%s by=%s removed=%s", row["slug"], name,
                u.sub[:8], removed)
    return {"status": "ok", "name": name, "removed": removed, "restarted": restarted}
