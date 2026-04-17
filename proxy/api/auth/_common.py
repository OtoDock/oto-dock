"""Shared helper for the core-auth API modules."""

from storage import database as task_store


def _build_user_response(user_row: dict, agents: list[str] | None = None,
                         agent_roles: dict | None = None) -> dict:
    """Build the standard user response dict."""
    sub = user_row["sub"]
    if agents is None:
        agents = task_store.get_user_agents(sub)
    if agent_roles is None:
        agent_roles = task_store.get_user_agent_roles(sub)
    default_agent = task_store.get_user_default_agent(sub) or ""
    return {
        "sub": sub,
        "email": user_row["email"],
        "name": user_row["name"],
        "role": user_row["role"],
        "agents": agents,
        "default_agent": default_agent,
        "display_name": user_row.get("display_name", ""),
        "agent_roles": agent_roles,
        "auth_provider": user_row.get("auth_provider", "local"),
        "totp_enabled": bool(user_row.get("totp_enabled")),
        "is_owner": bool(user_row.get("is_owner")),
        "must_change_password": bool(user_row.get("must_change_password")),
    }
