"""Admin API: every API key of the install, its revocation, and the switch
that turns keys off for the install.

GET lists every person's and every agent's key (never a hash) and the
switch. DELETE revokes one: a person's key answers what it created, and its
person is told. PUT sets the switch; off refuses every key at once, the
webhook fires included (``api_key_manager.key_row_refusal``). Dashboard
admins only (``require_admin`` refuses every bearer).
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_admin, require_user
from services.infra import api_key_manager
from services.infra.api_key_manager import KEY_KIND_AGENT, KEY_KIND_USER
from storage import database as task_store
from storage.identity import api_key_store
from storage.pg import run_db

logger = logging.getLogger("otodock.api-keys")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])


class KeysSwitchRequest(BaseModel):
    enabled: bool


def _view(r: dict) -> dict:
    out = {k: r.get(k) for k in ("id", "kind", "name", "prefix", "created_at", "last_used_at",
                                 "revoked_at")}
    out["permissions"] = api_key_store.parse_permissions(r.get("permissions"))
    if r["kind"] == KEY_KIND_USER:
        out.update(owner=r.get("owner_name") or r.get("owner_email") or "",
                   owner_email=r.get("owner_email") or "",
                   agents=api_key_store.parse_agents(r.get("agents")),
                   expires_at=r.get("expires_at"),
                   run_without_asking=bool(r.get("run_without_asking")))
    else:
        out.update(agent=r.get("agent") or "")
    return out


@router.get("/v1/admin/api-keys")
async def list_api_keys(include_revoked: bool = False,
                        user: UserContext | None = Depends(get_current_user)):
    require_admin(user)
    rows = await run_db(api_key_store.list_all_api_keys, include_revoked=include_revoked)
    enabled = not await asyncio.to_thread(api_key_manager.keys_switched_off)
    return {"enabled": enabled, "keys": [_view(r) for r in rows]}


@router.put("/v1/admin/api-keys/settings")
async def set_api_keys_switch(req: KeysSwitchRequest,
                              user: UserContext | None = Depends(get_current_user)):
    u = require_admin(user)
    await asyncio.to_thread(task_store.set_platform_setting, api_key_manager.KEYS_OFF_SETTING,
                            "0" if req.enabled else "1")
    api_key_manager.clear_keys_off_cache()
    logger.info("Admin %s switched API keys %s", u.sub[:8], "on" if req.enabled else "off")
    return {"enabled": req.enabled}


@router.delete("/v1/admin/api-keys/{kind}/{key_id}")
async def revoke_api_key(kind: str, key_id: str,
                         user: UserContext | None = Depends(get_current_user)):
    u = require_admin(user)
    if kind not in (KEY_KIND_USER, KEY_KIND_AGENT):
        raise HTTPException(404, "There is no such key.")

    def _job() -> tuple[dict | None, bool]:
        row = (api_key_store.get_user_api_key(key_id) if kind == KEY_KIND_USER
               else api_key_store.get_agent_api_key(key_id))
        if not row:
            return None, False
        if row.get("revoked_at"):
            return row, False
        revoke = (api_key_manager.revoke_user_key if kind == KEY_KIND_USER
                  else api_key_manager.revoke_agent_key)
        return row, revoke(key_id)

    row, revoked = await run_db(_job)
    if row is None:
        raise HTTPException(404, "There is no such key.")
    created, total = ([], 0)
    if kind == KEY_KIND_USER:
        created, total = await run_db(api_key_store.key_created, key_id)
    if revoked:
        logger.info("Admin %s revoked %s api key %s", u.sub[:8], kind, key_id[:8])
        if kind == KEY_KIND_USER and row.get("user_sub") != u.sub:
            from services.notifications import notification_manager
            await notification_manager.fire_notification(
                "An admin revoked one of your API keys",
                f"The key \"{row.get('name')}\" no longer works. Make a new one in your "
                "settings if you still need it.",
                severity="warning", scope="user", target=row.get("user_sub"), source="api_keys",
                href="/user-settings/integrations",
            )
    return {"status": "revoked" if revoked else "already_revoked", "id": key_id,
            "created": created, "created_total": total}
