"""Usage tracking and limits API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import get_current_user, require_auth, require_admin, UserContext
from services.billing import pool_caps, usage_service
from storage import database as task_store
from storage.billing import subscription_store

import asyncio
from auth import roles

router = APIRouter()


def _require_dashboard_user(user: UserContext | None) -> UserContext:
    """The caller's own budget controls change from their dashboard session
    only. The session token every agent subprocess holds resolves to the
    session owner (``require_admin`` rejects it for the same reason), and
    these caps exist to bound that agent's spend: reading them is fine,
    lifting them is not."""
    u = require_auth(user)
    if getattr(u, "is_api_key", False):
        raise HTTPException(status_code=403, detail="User authentication required (not API key)")
    return u


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SetLimitRequest(BaseModel):
    limit_type: str   # 'role_default' | 'user_override' | 'agent'
    target: str       # role name, user_sub, or agent name
    period: str       # 'weekly' | 'monthly'
    cost_limit_usd: float | None = None  # None = no limit


class DeleteLimitRequest(BaseModel):
    limit_type: str
    target: str
    period: str


class SetMyLimitRequest(BaseModel):
    period: str                          # 'weekly' | 'monthly'
    cost_limit_usd: float | None = None  # None = no cap (the row is removed)


class DeleteMyLimitRequest(BaseModel):
    period: str


class PoolCapRequest(BaseModel):
    """A field left out keeps its value; ``null`` clears it."""
    week_pct: float | None = None
    day_pct: float | None = None
    week_usd: float | None = None
    day_usd: float | None = None
    on_reached: str | None = None


# ---------------------------------------------------------------------------
# Pool caps (services/billing/pool_caps.py): one row for the caller's own
# accounts, one admin-only row for the platform pool.
# ---------------------------------------------------------------------------

def _pool_cap_payload(scope: str, target: str) -> dict:
    row = subscription_store.get_pool_cap(scope, target) or {}
    return {
        "caps": {f: row.get(f) for f in pool_caps.CAP_FIELDS},
        "on_reached": row.get("on_reached") or "stop",
        "engines": pool_caps.evaluate_engines(scope, target),
    }


def _write_pool_cap(scope: str, target: str, req: PoolCapRequest, by: str) -> None:
    current = subscription_store.get_pool_cap(scope, target) or {}
    values = {f: current.get(f) for f in pool_caps.CAP_FIELDS}
    on_reached = current.get("on_reached") or "stop"
    for f in req.model_fields_set:
        if f == "on_reached":
            on_reached = req.on_reached or "stop"
        else:
            values[f] = getattr(req, f)
    for f in ("week_pct", "day_pct"):
        if values[f] is not None and not (0 < values[f] <= 100):
            raise HTTPException(400, f"{f} must be above 0 and at most 100")
    for f in ("week_usd", "day_usd"):
        if values[f] is not None and values[f] <= 0:
            raise HTTPException(400, f"{f} must be above 0")
    if on_reached not in pool_caps.ON_REACHED:
        raise HTTPException(400, "on_reached must be stop or continue")
    subscription_store.upsert_pool_cap(
        scope, target, on_reached=on_reached, updated_by=by, **values,
    )
    pool_caps.invalidate(scope, target)


@router.get("/v1/usage/me/pool-cap")
async def get_my_pool_cap(user: UserContext = Depends(get_current_user)):
    require_auth(user)
    return await asyncio.to_thread(_pool_cap_payload, "user", user.sub)


@router.put("/v1/usage/me/pool-cap")
async def set_my_pool_cap(
    req: PoolCapRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_dashboard_user(user)
    await asyncio.to_thread(_write_pool_cap, "user", user.sub, req, user.sub)
    return await asyncio.to_thread(_pool_cap_payload, "user", user.sub)


@router.get("/v1/admin/usage/pool-cap")
async def admin_get_pool_cap(user: UserContext = Depends(get_current_user)):
    require_admin(user)
    return await asyncio.to_thread(_pool_cap_payload, "platform", "")


@router.put("/v1/admin/usage/pool-cap")
async def admin_set_pool_cap(
    req: PoolCapRequest,
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    await asyncio.to_thread(_write_pool_cap, "platform", "", req, user.sub)
    return await asyncio.to_thread(_pool_cap_payload, "platform", "")


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------

@router.get("/v1/usage/me")
async def get_my_usage(
    days: int = 30,
    user: UserContext = Depends(get_current_user),
):
    require_auth(user)
    summary = await asyncio.to_thread(
        usage_service.get_user_summary, user.sub, user.role, days
    )
    summary["pool"] = await asyncio.to_thread(pool_caps.evaluate_engines, "user", user.sub)
    return summary


@router.get("/v1/usage/me/check")
async def check_my_usage(
    user: UserContext = Depends(get_current_user),
):
    require_auth(user)
    result = await asyncio.to_thread(
        usage_service.check_user_limit, user.sub, user.role
    )
    return result


# The user's OWN dollar cap on their own API keys (``limit_type='user_self'``,
# target = the caller): set, read and cleared by the user alone. An admin's
# role/user limits are a separate budget (platform-paid spend).

@router.get("/v1/usage/me/limits")
async def get_my_limits(user: UserContext = Depends(get_current_user)):
    require_auth(user)
    limits = await asyncio.to_thread(
        task_store.get_usage_limits_for_target, "user_self", user.sub,
    )
    return {"limits": limits}


@router.put("/v1/usage/me/limits")
async def set_my_limit(
    req: SetMyLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_dashboard_user(user)
    if req.period not in ("weekly", "monthly"):
        raise HTTPException(400, "period must be weekly or monthly")
    if req.cost_limit_usd is None:
        await asyncio.to_thread(
            task_store.delete_usage_limit, "user_self", user.sub, req.period,
        )
        return {"ok": True}
    if req.cost_limit_usd < 0:
        raise HTTPException(400, "cost_limit_usd must be 0 or more")
    await asyncio.to_thread(
        task_store.upsert_usage_limit,
        "user_self", user.sub, req.period, req.cost_limit_usd, user.sub,
    )
    return {"ok": True}


@router.post("/v1/usage/me/limits/delete")
async def delete_my_limit(
    req: DeleteMyLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_dashboard_user(user)
    deleted = await asyncio.to_thread(
        task_store.delete_usage_limit, "user_self", user.sub, req.period,
    )
    if not deleted:
        raise HTTPException(404, "Limit not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.get("/v1/admin/usage/overview")
async def admin_usage_overview(
    days: int = 30,
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    overview = await asyncio.to_thread(usage_service.get_admin_overview, days)
    overview["pool"] = await asyncio.to_thread(pool_caps.evaluate_engines, "platform", "")
    return overview


@router.get("/v1/admin/usage/limits")
async def admin_get_limits(
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    limits = await asyncio.to_thread(task_store.get_usage_limits_all)
    return {"limits": limits}


@router.put("/v1/admin/usage/limits")
async def admin_set_limit(
    req: SetLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    if req.limit_type not in ("role_default", "user_override", "agent"):
        raise HTTPException(400, "limit_type must be role_default, user_override, or agent")
    if req.period not in ("weekly", "monthly"):
        raise HTTPException(400, "period must be weekly or monthly")
    if req.limit_type == "role_default" and req.target not in roles.PLATFORM_ROLES:
        raise HTTPException(400, "target must be a valid role name")
    await asyncio.to_thread(
        task_store.upsert_usage_limit,
        req.limit_type, req.target, req.period, req.cost_limit_usd, user.sub,
    )
    return {"ok": True}


@router.delete("/v1/admin/usage/limits")
async def admin_delete_limit(
    req: DeleteLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    deleted = await asyncio.to_thread(
        task_store.delete_usage_limit, req.limit_type, req.target, req.period,
    )
    if not deleted:
        raise HTTPException(404, "Limit not found")
    return {"ok": True}


@router.post("/v1/admin/usage/limits/delete")
async def admin_delete_limit_post(
    req: DeleteLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    """POST variant to avoid IPS rules blocking HTTP DELETE."""
    return await admin_delete_limit(req, user)
