"""Setup wizard endpoint — creates the first admin user on fresh install."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from storage import database as task_store
from auth.password import hash_password, check_password_strength
from auth.providers import create_session_jwt, apply_session_cookie

logger = logging.getLogger("claude-proxy")
router = APIRouter()


class SetupRequest(BaseModel):
    email: str
    password: str
    display_name: str


@router.post("/auth/setup")
async def setup_first_user(req: SetupRequest):
    """Create the first admin user. Only works when no users exist.

    This is the setup wizard endpoint — called once on fresh install.
    After the first user is created, this endpoint returns 403.
    """
    # Guard: only when no users exist
    user_count = await asyncio.to_thread(task_store.count_users)
    if user_count > 0:
        raise HTTPException(status_code=403, detail="Setup already completed")

    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    display_name = req.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name required")

    # Password strength check
    ok, msg, _ = check_password_strength(req.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    pw_hash = hash_password(req.password)

    try:
        sub = await asyncio.to_thread(
            task_store.create_local_user,
            email, display_name, display_name, "admin", pw_hash,
            is_owner=True, must_change_password=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    user = await asyncio.to_thread(task_store.get_user, sub)
    if not user:
        raise HTTPException(status_code=500, detail="Failed to create user")

    # Auto-install personal-assistant-lite from the community catalog as the
    # new owner's agent. Runs WITH the admin's identity + admin role so the
    # install pipeline's admin branch (services/community/community_agent_installer.py)
    # auto-installs/enables every required MCP inline — no admin-
    # approval requests, no permission prompts. The admin lands on the
    # dashboard with PA-lite ready to chat.
    #
    # Network failure (offline / GitHub unreachable) is non-fatal: the admin
    # can install PA-lite manually via Browse Community Agents later.
    try:
        from services.community import community_agent_installer
        result = await community_agent_installer.install_from_catalog(
            template_slug="personal-assistant-lite",
            target_slug="personal-assistant-lite",
            installer_user_sub=sub,
            installer_role="admin",
        )
        logger.info(
            "Setup wizard: PA-lite installed for new owner: %s",
            {k: v for k, v in result.items() if k != "agent"},
        )
    except Exception:
        logger.exception("Setup wizard: PA-lite catalog install failed (non-fatal)")

    # Walk the default-for-new-users agents and attach the new
    # admin as the manifest-declared role. PA-lite (just installed above)
    # declares ``role: viewer`` — but admin is already the manager via
    # _assign_installer_as_manager, so the ON CONFLICT branch keeps the
    # higher role. No-op if install failed: there are simply no defaults
    # to walk. Always marks the user default_agents_assigned=TRUE so future
    # OIDC re-logins skip the pass.
    from services.community import default_agent_assigner
    await asyncio.to_thread(default_agent_assigner.assign_default_agents, sub)

    # Re-fetch user data after agent install
    user = await asyncio.to_thread(task_store.get_user, sub)

    # Build response
    user_data = {
        "sub": sub, "email": email, "name": display_name,
        "role": "admin", "agents": user.get("agents", []),
        "default_agent": (user.get("agents") or [""])[0] if user.get("agents") else "",
        "display_name": display_name, "agent_roles": user.get("agent_roles", {}),
        "auth_provider": "local", "totp_enabled": False,
        "is_owner": True, "must_change_password": False,
    }

    token = create_session_jwt(sub, email, display_name, "admin", auth_provider="local")
    response = JSONResponse(content={"user": user_data})
    apply_session_cookie(response, token)
    logger.info(f"Setup wizard: created owner admin {email}")
    return response
