"""Admin user-management endpoints.

List/create/delete users, set their agents / role / local-only flag, and
admin password resets. Attaches to the shared core-auth router."""

import asyncio
import logging
import time

import jwt as pyjwt
from fastapi import Depends, HTTPException
from pydantic import BaseModel

import config
from auth.license import check_seat_limit
from auth.password import check_password_strength, generate_temp_password, hash_password
from auth.providers import UserContext, get_current_user, mask_email, require_admin
from storage import agent_store
from storage import credential_store
from storage import database as task_store

from api.auth._common import _build_user_response
from api.auth._router import router

logger = logging.getLogger("claude-proxy")

# Invite links are signed JWTs (purpose="invite"), same pattern as the
# password-reset flow. Single-use is enforced structurally: accept-invite only
# works while the account has no password, and accepting sets one.
_INVITE_TOKEN_TTL = 48 * 3600


def mint_invite_url(sub: str) -> str:
    """Mint a tokenized invite link for a passwordless local account.

    Relative when DASHBOARD_PUBLIC_URL is unset — the dashboard resolves it
    against its own origin for the copy-link flow."""
    token = pyjwt.encode(
        {"sub": sub, "purpose": "invite",
         "iat": int(time.time()), "exp": int(time.time()) + _INVITE_TOKEN_TTL},
        config.JWT_SECRET, algorithm="HS256",
    )
    return f"{config.DASHBOARD_PUBLIC_URL}/accept-invite?token={token}"


class UpdateAgentsRequest(BaseModel):
    agents: list[str]
    agent_roles: dict[str, str] | None = None  # {agent: "manager"|"editor"|"viewer"}


class UpdateRoleRequest(BaseModel):
    role: str


class CreateUserRequest(BaseModel):
    email: str
    display_name: str
    role: str
    password: str | None = None
    send_invite: bool = False


class ResetPasswordAdminRequest(BaseModel):
    pass  # no body needed


class SetLocalOnlyRequest(BaseModel):
    local_only: bool


@router.get("/v1/admin/users")
async def admin_list_users(user: UserContext | None = Depends(get_current_user)):
    """List all users with their agent assignments. Admin only."""
    require_admin(user)
    users = task_store.list_users()
    # list_users is SELECT * — strip secret-bearing columns before the wire.
    # ``invite_pending`` = a local account still waiting on its invite link
    # (no password set yet), so the admin UI can badge it.
    for u in users:
        u["invite_pending"] = (
            (u.get("auth_provider") or "").startswith("local")
            and not u.pop("password_hash", None)
        )
        for k in ("totp_secret_enc", "totp_recovery_enc"):
            u.pop(k, None)
    return {"users": users}


@router.put("/v1/admin/users/{sub}/agents")
async def admin_set_user_agents(
    sub: str,
    req: UpdateAgentsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Set agent assignments for a user. Admin only."""
    u = require_admin(user)
    target = task_store.get_user(sub)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Security: high-clearance agents only assignable to admins
    if target["role"] != "admin":
        blocked = [a for a in req.agents if agent_store.is_admin_only(a)]
        if blocked:
            raise HTTPException(
                status_code=403,
                detail=f"Agents {blocked} require admin role",
            )

    # Validate agent names
    valid_agents = set(agent_store.get_agent_slugs())
    invalid = [a for a in req.agents if a not in valid_agents]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown agents: {invalid}")

    # Enforce: platform members cannot be promoted to agent editor/manager.
    # Platform role is the ceiling — a platform member stays a per-agent
    # viewer everywhere.
    roles = req.agent_roles or {}
    if target["role"] == "member":
        roles = {a: "viewer" for a in req.agents}
    # Validate role values (editor added alongside manager/viewer).
    for a, r in roles.items():
        if r not in ("manager", "editor", "viewer"):
            raise HTTPException(status_code=400, detail=f"Invalid agent_role '{r}' for {a}")

    # Detect newly-added attachments BEFORE the DELETE+INSERT
    # pattern in set_user_agents wipes the existing rows. The diff lets us
    # fire on_user_added_to_agent only for genuine new attachments — not
    # for unchanged ones (which are technically re-inserted by the
    # implementation, but conceptually unchanged from the admin's view).
    existing_agents = set(await asyncio.to_thread(task_store.get_user_agents, sub))
    added_agents = set(req.agents) - existing_agents

    task_store.set_user_agents(sub, req.agents, u.sub, agent_roles=roles)
    logger.info(f"Admin {mask_email(u.email)} set agents for {sub}: {req.agents} roles={roles}")

    if added_agents:
        from services.community import community_agent_installer
        for agent_slug in added_agents:
            try:
                await asyncio.to_thread(
                    community_agent_installer.on_user_added_to_agent,
                    agent_slug, sub, roles.get(agent_slug, "viewer"),
                )
            except Exception:
                logger.exception(
                    "on_user_added_to_agent failed for (%s, %s)", agent_slug, sub,
                )

    return {"status": "updated", "agents": req.agents, "agent_roles": roles}


@router.put("/v1/admin/users/{sub}/role")
async def admin_update_role(
    sub: str,
    req: UpdateRoleRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Change a user's role. Admin only. Cannot change own role."""
    u = require_admin(user)
    if sub == u.sub:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    if req.role not in config.ROLE_PRIORITY:
        raise HTTPException(status_code=400, detail=f"Invalid role: {req.role}")
    target = task_store.get_user(sub)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Owner protection
    if target.get("is_owner"):
        raise HTTPException(status_code=403, detail="Cannot change the owner account's role")

    # Last admin protection
    if target["role"] == "admin" and req.role != "admin":
        admin_count = await asyncio.to_thread(task_store.count_admins)
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last admin")

    task_store.update_user_role(sub, req.role)

    # If downgrading from admin, remove high-clearance agent assignments and pull
    # the user's subscriptions out of the shared platform pool (a non-admin may not
    # contribute). The resolver's owner-is-admin JOIN already excludes them in real
    # time; this is the durable cleanup so the pool view and any later re-promotion
    # stay correct.
    if req.role != "admin":
        current_agents = task_store.get_user_agents(sub)
        safe_agents = [a for a in current_agents if not agent_store.is_admin_only(a)]
        if len(safe_agents) != len(current_agents):
            task_store.set_user_agents(sub, safe_agents, u.sub)
            logger.info(f"Removed high-clearance agents from {sub} after role change to {req.role}")
        from storage import subscription_store
        cleared = subscription_store.clear_contribute_platform_for_owner(sub)
        if cleared:
            logger.info(f"Cleared platform-pool contribution on {cleared} sub(s) for demoted user {sub}")
            # Agent-scope sessions running on the demoted admin's pool subs are
            # now delisted — re-home them onto the remaining pool.
            from services.engines import subscription_pool
            subscription_pool.schedule_rebind("admin demotion")

    logger.info(f"Admin {mask_email(u.email)} changed role for {sub} to {req.role}")
    return {"status": "updated", "role": req.role}


@router.delete("/v1/admin/users/{sub}")
async def admin_delete_user(
    sub: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a user. Admin only. Cannot delete self."""
    u = require_admin(user)
    if sub == u.sub:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    # Owner protection
    target = await asyncio.to_thread(task_store.get_user, sub)
    if target and target.get("is_owner"):
        raise HTTPException(status_code=403, detail="Cannot delete the owner account")
    # Last admin protection
    if target and target["role"] == "admin":
        admin_count = await asyncio.to_thread(task_store.count_admins)
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin")
    # Best-effort vendor DELETE for all webhook subscriptions
    # this user owns. Must happen BEFORE task_store.delete_user (which
    # cascades to triggers/credentials and revokes the OAuth tokens we
    # need to talk to vendors). Failures are logged but don't block.
    try:
        from services.webhooks import subscription_manager
        await subscription_manager.cleanup_user_subscriptions(sub)
    except Exception:
        logger.exception(
            "Subscription cleanup raised for user %s (continuing with user delete)",
            sub,
        )
    # Drop any `service_agent_bindings` that pointed at this user's accounts.
    # Affected agents revert to their MCP's platform default at next resolve.
    # No tokens to clean up here — the user's own `user_credentials` rows +
    # token files are removed by the FK cascade on `task_store.delete_user`.
    try:
        await asyncio.to_thread(
            credential_store.cleanup_service_agent_bindings_for_owner, sub,
        )
    except Exception:
        logger.exception(
            "Service-agent-binding cleanup raised for user %s "
            "(continuing with user delete)", sub,
        )
    deleted = task_store.delete_user(sub)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info(f"Admin {mask_email(u.email)} deleted user {sub}")
    return {"status": "deleted"}


@router.post("/v1/admin/users/{sub}/delete")
async def admin_delete_user_post(
    sub: str,
    user: UserContext | None = Depends(get_current_user),
):
    """POST-based delete -- avoids IPS rules that block HTTP DELETE."""
    return await admin_delete_user(sub, user)


@router.post("/v1/admin/users")
async def admin_create_user(
    req: CreateUserRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Create a new local user. Admin only."""
    u = require_admin(user)
    if req.role not in config.ROLE_PRIORITY:
        raise HTTPException(status_code=400, detail=f"Invalid role: {req.role}")

    # Seat-limit check (deployment-aware; two-stage grace on self-host expiry).
    allowed, current, max_users = await asyncio.to_thread(check_seat_limit)
    if not allowed:
        raise HTTPException(
            status_code=402,
            detail=f"User limit reached ({current}/{max_users}). Upgrade your license.",
        )

    # Check email uniqueness
    existing = await asyncio.to_thread(task_store.get_user_by_email, req.email.strip().lower())
    if existing:
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    temp_password = None
    password_hash_val = ""
    must_change = False

    if req.password:
        # Admin sets a temporary password
        ok, msg, _ = check_password_strength(req.password)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        password_hash_val = hash_password(req.password)
        must_change = True
        temp_password = req.password
    elif req.send_invite:
        # Fail BEFORE creating the user so a misconfigured install doesn't
        # leave an inert account behind.
        from services.notifications.smtp import is_smtp_configured
        if not await asyncio.to_thread(is_smtp_configured):
            raise HTTPException(status_code=400, detail="SMTP not configured — cannot send invite")
        if not config.DASHBOARD_PUBLIC_URL:
            raise HTTPException(
                status_code=400,
                detail="DASHBOARD_PUBLIC_URL is not set — an emailed invite link would not resolve",
            )

    sub = await asyncio.to_thread(
        task_store.create_local_user,
        req.email.strip().lower(),
        req.display_name.strip(),
        req.display_name.strip(),
        req.role,
        password_hash_val,
        must_change_password=must_change,
    )

    # Auto-attach the new user to every agent whose admin enabled
    # the default-for-new-users toggle. Idempotent — repeat invocations
    # (e.g. via OIDC callback) short-circuit on users.default_agents_assigned.
    from services.community import default_agent_assigner
    await asyncio.to_thread(default_agent_assigner.assign_default_agents, sub)

    # No password → invite mode: the account is inert (login impossible) until
    # the invite is accepted at /auth/accept-invite. The link is returned ONCE
    # for copy-out, and emailed too when the admin asked for it.
    invite_url = None
    if not req.password:
        invite_url = mint_invite_url(sub)
        if req.send_invite:
            from services.notifications.smtp import send_invite_email
            await asyncio.to_thread(
                send_invite_email, req.email, invite_url, u.display_name or u.name,
            )

    new_user = await asyncio.to_thread(task_store.get_user, sub)
    result = {"user": _build_user_response(new_user) if new_user else {}}
    if temp_password:
        result["temp_password"] = temp_password
    if invite_url:
        result["invite_url"] = invite_url
    if req.send_invite:
        result["invite_sent"] = True

    logger.info(f"Admin {mask_email(u.email)} created user {mask_email(req.email)} role={req.role}")
    return result


@router.post("/v1/admin/users/{sub}/reset-password")
async def admin_reset_password(
    sub: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Reset a local user's password. Admin only. Returns temp password."""
    u = require_admin(user)
    target = await asyncio.to_thread(task_store.get_user, sub)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not (target.get("auth_provider", "").startswith("local")):
        raise HTTPException(status_code=400, detail="Cannot reset password for OIDC users")

    temp = generate_temp_password()
    pw_hash = hash_password(temp)
    await asyncio.to_thread(task_store.set_user_password, sub, pw_hash)
    await asyncio.to_thread(task_store.update_user_auth_fields, sub, must_change_password=True)
    logger.info(f"Admin {mask_email(u.email)} reset password for {mask_email(target['email'])}")
    return {"temp_password": temp}


@router.put("/v1/admin/users/{sub}/local-only")
async def admin_set_local_only(
    sub: str,
    req: SetLocalOnlyRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Toggle local-only network restriction for a user. Admin only."""
    u = require_admin(user)
    target = await asyncio.to_thread(task_store.get_user, sub)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    await asyncio.to_thread(
        task_store.update_user_auth_fields, sub, local_only=bool(req.local_only),
    )
    logger.info(f"Admin {mask_email(u.email)} set local_only={req.local_only} for {mask_email(target['email'])}")
    return {"status": "updated", "local_only": req.local_only}
