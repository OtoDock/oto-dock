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
from auth.password import HashBusy, check_password_strength_async, generate_temp_password, hash_password_async
from auth.providers import UserContext, get_current_user, mask_email, require_admin
from storage.agents import agent_store
from storage.identity import credential_store
from storage import database as task_store

from api.auth._common import _build_user_response
from api.auth._router import router
from auth import roles as _roles
from services.agents import offboarding, offboarding_sessions

logger = logging.getLogger("claude-proxy")


async def _hash_or_503(plain: str) -> str:
    """The password hash, off the event loop (``auth.password``)."""
    try:
        return await hash_password_async(plain)
    except HashBusy:
        raise HTTPException(status_code=503, detail="Busy. Try again in a few seconds.",
                            headers={"Retry-After": "5"})

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
    agent_roles: dict[str, str] | None = None  # {agent: a member of _roles.AGENT_ROLES}


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
    users = await asyncio.to_thread(task_store.list_users)
    # list_users is SELECT * — strip secret-bearing columns before the wire.
    # ``invite_pending`` = a local account still waiting on its invite link
    # (no password set yet), so the admin UI can badge it.
    for u in users:
        # Pop the hash UNCONDITIONALLY (before the short-circuit): a non-local
        # row skips the `and` operand, so folding the pop into the invite_pending
        # expression left password_hash on any future non-local row that
        # carried one. Compute the badge from the popped value.
        _ph = u.pop("password_hash", None)
        u["invite_pending"] = (
            (u.get("auth_provider") or "").startswith("local") and not _ph
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

    def _load() -> tuple[dict | None, list[str], set[str]]:
        target = task_store.get_user(sub)
        if not target:
            return None, [], set()
        # Security: high-clearance agents only assignable to admins
        blocked = ([] if _roles.is_admin(target["role"])
                   else [a for a in req.agents if agent_store.is_admin_only(a)])
        return target, blocked, set(agent_store.get_agent_slugs())

    target, blocked, valid_agents = await asyncio.to_thread(_load)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if blocked:
        raise HTTPException(
            status_code=403,
            detail=f"Agents {blocked} require admin role",
        )

    # Validate agent names
    invalid = [a for a in req.agents if a not in valid_agents]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown agents: {invalid}")

    # Per-agent roles are independent of the platform role: a platform
    # member (who cannot create agents or reach admin surfaces) may still
    # be manager/editor of specific agents an admin assigns. The only
    # platform↔agent coupling is admin-only agents (checked above).
    roles = req.agent_roles or {}
    # Validate role values (editor added alongside manager/viewer).
    for a, r in roles.items():
        if r not in _roles.AGENT_ROLES:
            raise HTTPException(status_code=400, detail=f"Invalid agent_role '{r}' for {a}")

    # A Shared-only agent takes the editor tier: no new or changed row below
    # it there (a row an older install holds passes while unchanged).
    from services.agents import shared_only_members
    requested = {a: roles.get(a, _roles.VIEWER) for a in req.agents}
    stored = await asyncio.to_thread(task_store.get_user_agent_roles, sub)
    refused = await asyncio.to_thread(shared_only_members.refused_assignments, requested, stored)
    if refused:
        raise HTTPException(status_code=400, detail=shared_only_members.refusal_message(refused))

    # Detect newly-added attachments BEFORE the DELETE+INSERT
    # pattern in set_user_agents wipes the existing rows. The diff lets us
    # fire on_user_added_to_agent only for genuine new attachments — not
    # for unchanged ones (which are technically re-inserted by the
    # implementation, but conceptually unchanged from the admin's view).
    existing_agents = set(await asyncio.to_thread(task_store.get_user_agents, sub))
    added_agents = set(req.agents) - existing_agents
    removed_agents = existing_agents - set(req.agents)

    change = await asyncio.to_thread(
        task_store.set_user_agents, sub, req.agents, u.sub, agent_roles=roles)
    logger.info(f"Admin {mask_email(u.email)} set agents for {sub}: {req.agents} roles={roles}")
    from services.notifications.notification_manager import invalidate_audience
    for added in added_agents:
        invalidate_audience(added)
    agent_losses = offboarding.losses(change.platform_role, change.before,
                                      change.platform_role, change.after)
    from services.agents import offboarding_bindings
    await offboarding_bindings.clear_at_demotion(sub, agent_losses)
    await offboarding.dispatch_losses(
        sub, agent_losses,
        u.sub, person=target, platform_before=change.platform_role,
        platform_after=change.platform_role,
    )

    # The member's template-seeded apps stop and hide with the membership
    # (COMMUNITY-AGENTS-REGISTRY.md); a re-attach brings them back.
    for agent_slug in removed_agents:
        try:
            from services.community import template_app_seeder
            await template_app_seeder.on_user_removed(agent_slug, sub)
        except Exception:
            logger.exception("template apps of (%s, %s) not parked on removal", agent_slug, sub)

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


class AddAgentRequest(BaseModel):
    role: str = "viewer"  # "manager" | "editor" | "viewer"


@router.post("/v1/admin/users/{sub}/agents/{agent}")
async def admin_add_user_agent(
    sub: str,
    agent: str,
    req: AddAgentRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Additively attach ONE agent to a user. Admin only.

    The PUT sibling above is full-set-replace — fine for the admin users
    page that edits the whole set, dangerous for one-click flows (a stale
    client set would clobber other assignments). This is the primitive
    behind the agents-map "add me" CTA: idempotent, touches nothing else,
    and fires the same on-add side effects (dirs/quota via add_user_agent,
    community seeding below).
    """
    u = require_admin(user)

    def _load() -> tuple[dict | None, bool, bool]:
        return (task_store.get_user(sub), agent_store.agent_exists(agent),
                agent_store.is_admin_only(agent))

    target, agent_exists, admin_only = await asyncio.to_thread(_load)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not agent_exists:
        raise HTTPException(status_code=404, detail="Agent not found")
    if req.role not in _roles.AGENT_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid agent_role '{req.role}'")
    if not _roles.is_admin(target["role"]) and admin_only:
        raise HTTPException(
            status_code=403, detail=f"Agent '{agent}' requires admin role"
        )
    from services.agents import shared_only_members
    stored = await asyncio.to_thread(task_store.get_user_agent_roles, sub)
    if await asyncio.to_thread(shared_only_members.refused_assignments, {agent: req.role}, stored):
        raise HTTPException(status_code=400, detail=shared_only_members.refusal_message([agent]))

    inserted = await asyncio.to_thread(
        task_store.add_user_agent, sub, agent, req.role, u.sub
    )
    if inserted:
        logger.info(
            f"Admin {mask_email(u.email)} added agent {agent} ({req.role}) to {sub}"
        )
        from services.notifications.notification_manager import invalidate_audience
        invalidate_audience(agent)
        from services.community import community_agent_installer
        try:
            await asyncio.to_thread(
                community_agent_installer.on_user_added_to_agent,
                agent, sub, req.role,
            )
        except Exception:
            logger.exception(
                "on_user_added_to_agent failed for (%s, %s)", agent, sub,
            )
    return {"status": "added" if inserted else "already_assigned", "agent": agent}


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
    if req.role not in _roles.PLATFORM_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {req.role}")
    target = await asyncio.to_thread(task_store.get_user, sub)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Owner protection
    if target.get("is_owner"):
        raise HTTPException(status_code=403, detail="Cannot change the owner account's role")

    # Last admin protection
    if _roles.is_admin(target["role"]) and not _roles.is_admin(req.role):
        admin_count = await asyncio.to_thread(task_store.count_admins)
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last admin")

    rows_before = await asyncio.to_thread(task_store.get_user_agent_roles, sub)
    await asyncio.to_thread(task_store.update_user_role, sub, req.role)
    await apply_platform_role_change(sub, target, req.role, rows_before, u.sub)
    logger.info(f"Admin {mask_email(u.email)} changed role for {sub} to {req.role}")
    return {"status": "updated", "role": req.role}


async def apply_platform_role_change(sub: str, person: dict, new_role: str,
                                     rows_before: dict, actor_sub: str) -> None:
    """What a platform role change means once the users row carries it, for
    the admin route and a login whose identity provider changed the role
    (``actor_sub=""``: no person made the change, the subscribers fall back to
    the owner). A lowered platform role ends every sign-in and agent session
    token of the person first (their token epoch moves, decision 16; a
    sign-in whose identity provider lowered it keeps the cookie it is being
    issued, minted after the move). Every admin stands in every agent's
    audience; below admin the admin-only agent rows go (the roles of the rows
    that stay are kept) and the person's subscriptions leave the shared
    platform pool; then the losses are dispatched to the offboarding
    subscribers."""
    before = person.get("role")
    if _roles.PLATFORM_RANK.get(new_role, -1) < _roles.PLATFORM_RANK.get(before, -1):
        from api.auth.identity import end_every_sign_in
        await end_every_sign_in(sub, person.get("username") or "", "platform_role_lowered")
    if _roles.is_admin(new_role) != _roles.is_admin(before):
        from services.notifications.notification_manager import invalidate_audience
        invalidate_audience()

    # If downgrading from admin, remove high-clearance agent assignments and pull
    # the user's subscriptions out of the shared platform pool (a non-admin may not
    # contribute). The resolver's owner-is-admin JOIN already excludes them in real
    # time; this is the durable cleanup so the pool view and any later re-promotion
    # stay correct.
    if not _roles.is_admin(new_role):
        def _drop_admin_only() -> tuple[list[str], list[str]]:
            # The rows as they stand now, read in the job that writes them:
            # a snapshot from before the call would write back a row another
            # change removed or re-roled meanwhile.
            current_roles = task_store.get_user_agent_roles(sub)
            current = list(current_roles)
            safe = [a for a in current if not agent_store.is_admin_only(a)]
            if len(safe) != len(current):
                # Keep the roles on the agents that stay (without them every
                # row fell back to viewer).
                task_store.set_user_agents(
                    sub, safe, actor_sub,
                    agent_roles={a: current_roles[a] for a in safe})
            return current, safe

        current_agents, safe_agents = await asyncio.to_thread(_drop_admin_only)
        if len(safe_agents) != len(current_agents):
            logger.info(f"Removed high-clearance agents from {sub} after role change to {new_role}")
            from services.community import template_app_seeder
            for agent_slug in set(current_agents) - set(safe_agents):
                try:
                    await template_app_seeder.on_user_removed(agent_slug, sub)
                except Exception:
                    logger.exception("template apps of (%s, %s) not parked on demotion", agent_slug, sub)
        from storage.billing import subscription_store
        cleared = await asyncio.to_thread(subscription_store.clear_contribute_platform_for_owner, sub)
        if cleared:
            logger.info(f"Cleared platform-pool contribution on {cleared} sub(s) for demoted user {sub}")
            # Agent-scope sessions running on the demoted admin's pool subs are
            # now delisted: re-home them onto the remaining pool (scheduled
            # from the loop: the rebind is a loop task).
            from services.engines import subscription_pool
            subscription_pool.schedule_rebind("admin demotion")

    def _after() -> tuple[dict, list[str]]:
        return task_store.get_user_agent_roles(sub), agent_store.get_agent_slugs()

    rows_after, all_agents = await asyncio.to_thread(_after)
    agent_losses = offboarding.losses(before, rows_before, new_role, rows_after, all_agents)
    if actor_sub:
        # An admin's change clears inline; a sign-in the identity provider
        # demoted does not wait on vendor calls (the binding store already
        # refuses a lender who no longer manages; the subscriber clears).
        from services.agents import offboarding_bindings
        await offboarding_bindings.clear_at_demotion(sub, agent_losses)
    await offboarding.dispatch_losses(
        sub, agent_losses,
        actor_sub, person=person, platform_before=before, platform_after=new_role,
    )


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
    if target and _roles.is_admin(target["role"]):
        admin_count = await asyncio.to_thread(task_store.count_admins)
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin")
    # The user's personal app servers stop BEFORE the rows cascade away
    # (APPS.md "Lifecycle"), and their release copies and databases go
    # with the rows.
    try:
        from services.apps import app_lifecycle
        await app_lifecycle.stop_user_apps(sub)
        await app_lifecycle.remove_user_app_dirs(sub)
    except Exception:
        logger.exception("App servers of user %s did not stop cleanly (continuing)", sub)
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
    # The bindings lending this person's accounts go while their token still
    # exists, so each binding's service-scope subscriptions are unregistered
    # at the vendor through the lender (the offboarding subscriber can only
    # sweep the rows once the token is gone); the owner cleanup below is the
    # safety net. Affected agents revert to their MCP's platform default at
    # next resolve.
    try:
        from services.agents import offboarding_bindings
        for row in await asyncio.to_thread(
                credential_store.list_service_agent_bindings_for_owner, sub):
            await offboarding_bindings.clear_binding(row, sub)
    except Exception:
        logger.exception(
            "Service-binding subscription cleanup raised for user %s "
            "(continuing with user delete)", sub,
        )
    try:
        await asyncio.to_thread(
            credential_store.cleanup_service_agent_bindings_for_owner, sub,
        )
    except Exception:
        logger.exception(
            "Service-agent-binding cleanup raised for user %s "
            "(continuing with user delete)", sub,
        )
    # Revoke the user's API keys and user-scoped triggers. These tables carry
    # NO foreign key to users(sub), so delete_user does NOT cascade them —
    # without this an orphaned `otok_` key kept firing the user's surviving
    # triggers (and, being un-revocable once the user row is gone, deleting the
    # user was NOT the full revocation an operator expects). Best-effort:
    # a failure here must not block the user delete.
    try:
        from storage.identity import api_key_store
        from storage.automation import trigger_store
        n_keys = await asyncio.to_thread(api_key_store.cleanup_user_api_keys, sub)
        n_trig = await asyncio.to_thread(trigger_store.cleanup_user_triggers, sub)
        if n_keys or n_trig:
            logger.info("Revoked %d API key(s) + %d user trigger(s) for deleted "
                        "user %s", n_keys, n_trig, sub)
    except Exception:
        logger.exception(
            "API-key/trigger cleanup raised for user %s "
            "(continuing with user delete)", sub,
        )
    # The user's own usage caps: their pool cap row and their limit rows
    # (``user_self`` set by them, ``user_override`` set by an admin). Keyed by
    # sub with no FK, so a re-created account must not inherit them.
    try:
        from storage.billing import subscription_store
        await asyncio.to_thread(subscription_store.delete_pool_cap, "user", sub)
        for limit_type in ("user_self", "user_override"):
            await asyncio.to_thread(
                task_store.delete_usage_limits_for_target, limit_type, sub,
            )
    except Exception:
        logger.exception(
            "Usage cap cleanup raised for user %s (continuing with user delete)", sub,
        )
    # The user's chat snapshots (their shares cascade with the row; the
    # copies live in the agent trees under their own bucket).
    try:
        from services.sharing import chat_snapshot
        uname = await asyncio.to_thread(task_store.get_username_by_sub, sub) or ""
        await asyncio.to_thread(chat_snapshot.remove_user_snapshots, uname)
    except Exception:
        logger.exception(
            "Chat snapshot cleanup raised for user %s (continuing with user delete)", sub,
        )
    rows_before = await asyncio.to_thread(task_store.get_user_agent_roles, sub)
    all_agents = await asyncio.to_thread(agent_store.get_agent_slugs)
    deleted = await asyncio.to_thread(task_store.delete_user, sub)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    # The row's deletion refuses every cookie and agent session token of the
    # person (a token epoch on a gone row would change nothing); a pass the
    # holder cache already holds must not outlive it.
    from auth import token_holder
    token_holder.forget(sub)
    logger.info(f"Admin {mask_email(u.email)} deleted user {sub}")
    await offboarding.dispatch_losses(
        sub, offboarding.losses(target["role"], rows_before, None, {}, all_agents),
        u.sub, deleted=True, person=target, platform_before=target["role"],
    )
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
    if req.role not in _roles.PLATFORM_ROLES:
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
        ok, msg, _ = await check_password_strength_async(req.password)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        password_hash_val = await _hash_or_503(req.password)
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

    def _created_user() -> dict:
        new_user = task_store.get_user(sub)
        return _build_user_response(new_user) if new_user else {}

    result = {"user": await asyncio.to_thread(_created_user)}
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
    # Owner protection — parity with role-change / delete. Without it a
    # non-owner admin could reset the owner's password and (barring the
    # owner's 2FA) log in as the owner, crossing the owner-above-admin
    # boundary the rest of admin_users enforces. The owner resets their own
    # password via /v1/users/me/password.
    if target.get("is_owner") and not u.is_owner:
        raise HTTPException(status_code=403,
                            detail="Cannot reset the owner's password")
    if not (target.get("auth_provider", "").startswith("local")):
        raise HTTPException(status_code=400, detail="Cannot reset password for OIDC users")

    temp = generate_temp_password()
    pw_hash = await _hash_or_503(temp)
    await asyncio.to_thread(task_store.set_user_password, sub, pw_hash)
    await asyncio.to_thread(task_store.update_user_auth_fields, sub, must_change_password=True)
    logger.info(f"Admin {mask_email(u.email)} reset password for {mask_email(target['email'])}")
    # The person's warm chats hold session tokens the reset just invalidated.
    await offboarding_sessions.close_person_sessions(
        sub, target.get("username") or "", "password_changed")
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
