"""Shared helper for the core-auth API modules."""

from storage import database as task_store


def build_feature_flags() -> dict:
    """User-facing feature flags, shipped on EVERY user payload.

    Must ride every response that the dashboard stores as its user object
    (local login, 2FA, passkey, OAuth callback, admin-create, /auth/me):
    the dashboard's gates fail open on a MISSING key (`!== false`), so a
    payload without flags un-hides staged features until the next /auth/me
    refetch — the Remote Machines tab appeared after a fresh login on
    builds that ship without the satellite source (1.3.0 public cut)."""
    from core import execution_mode
    from ws.satellite import satellite_source_available
    allow_user_paired = task_store.get_platform_setting(
        "allow_user_paired_machines",
    )
    return {
        "allow_user_paired_machines": (allow_user_paired or "") != "0",
        "remote_machines_available": satellite_source_available(),
        # Mirrors the global interactive kill-switch so the dashboard hides
        # the interactive-terminal toggles when sessions always run headless.
        "interactive_terminal_enabled": execution_mode.is_interactive_enabled(),
    }


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
        "feature_flags": build_feature_flags(),
    }
