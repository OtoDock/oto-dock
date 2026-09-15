"""Credential Management REST API.

User-facing endpoints for per-user MCP credential management.
Admin-facing endpoints for infrastructure + service account credentials.
Credential schemas are read from MCP manifests via mcp_registry.
Auth: OAuth2 session cookie (dashboard users).
"""

import asyncio
import functools
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from storage.identity import credential_store
from storage.agents import agent_store
from storage.mcp import mcp_store
from storage import database as task_store
from services.mcp import mcp_registry
from services.oauth import oauth_account_store
from auth.providers import UserContext, get_current_user

logger = logging.getLogger("claude-proxy.credential-api")
router = APIRouter()


# --- Request models ---

class SetCredentialRequest(BaseModel):
    credentials: dict[str, str]  # {key: value}
    account_label: str = "default"


class SetEmailServerConfigRequest(BaseModel):
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_secure: str = ""
    imap_host: str = ""
    imap_port: str = ""
    imap_secure: str = ""


# --- Multi-account models ---

class SetDefaultAccountRequest(BaseModel):
    account_label: str


class SetAgentBindingRequest(BaseModel):
    agent_name: str
    account_label: str


class SetServiceBindingRequest(BaseModel):
    """Body for the agent → service-account binding endpoint.

    Binds the agent to the CALLER's own connected account (``account_label``)
    as its service identity — agent-scope sessions then read the caller's
    tokens. The owner is always the caller (derived server-side); the caller
    must have per-agent manager/admin role on the agent.
    """

    account_label: str


def _get_cred_schema(mcp_name: str) -> dict | None:
    """Get credential schema from manifest."""
    return mcp_registry.get_credential_schema(mcp_name)


def _is_app_credential(name: str) -> bool:
    """Check if name is referenced as app_credential by any manifest."""
    for schema in mcp_registry.get_all_credential_schemas().values():
        if schema.get("app_credential") == name:
            return True
    return False


def _read_account_token_scopes(
    user_sub: str, mcp_name: str, account_label: str,
) -> list[str]:
    """Return the list of scopes recorded in an account's token file.

    Used by ``list_my_integrations`` to compute ``missing_scopes`` for
    the "Add additional access" UI affordance (incremental scope grant).
    Returns an empty list if the token file is missing/corrupt; that's
    treated as "no scopes granted" which surfaces every required scope
    as missing — UI prompts re-connect, which is correct.
    """
    from services.oauth import oauth_account_store
    manifest = mcp_registry.get_manifest(mcp_name)
    provider_id = (
        (manifest.credentials.oauth or {}).get("provider_id", "")
        if manifest else ""
    )
    if not provider_id:
        return []
    username = task_store.get_username_by_sub(user_sub)
    if not username:
        return []
    token_dir = oauth_account_store.get_token_dir(username, provider_id=provider_id)
    data = oauth_account_store.read_account_token(token_dir, account_label)
    if not data:
        return []
    raw = data.get("scopes", []) or []
    if isinstance(raw, str):
        # Some legacy formats stored as space-separated string.
        return [s for s in raw.split() if s]
    return list(raw)


# --- MCP Schema ---

@router.get("/v1/mcp-credential-schema")
async def get_credential_schema(
    user: UserContext = Depends(get_current_user),
):
    """Return all MCP credential schemas (drives dashboard forms)."""
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")
    return mcp_registry.get_all_credential_schemas()


# --- User Credentials (any authenticated user) ---

@router.get("/v1/users/me/integrations")
async def list_my_integrations(
    user: UserContext = Depends(get_current_user),
):
    """List per-user MCPs the user needs, with configured status.

    Scans the user's assigned agents, collects all per-user MCPs, and returns
    status for each.
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")

    # Two separate agent sets:
    #   `user_agents` — admin-extended set used to LIST which per-user MCPs
    #     to surface. Admins see every per-user MCP on the platform so they
    #     can configure their own credentials before delegating.
    #   `user_assigned_agents` — the user's DIRECTLY ASSIGNED agents (not
    #     admin-extended). Used to compute `candidate_agents` for the
    #     per-agent OAuth-account binding UI. An admin who isn't assigned
    #     to an agent shouldn't see it as a binding candidate — binding a
    #     personal OAuth account to an agent is a personal-use decision,
    #     not an administrative one.
    user_assigned_agents = await asyncio.to_thread(
        task_store.get_user_agents, user.sub,
    )
    user_agents = list(user_assigned_agents)
    if user.is_admin:
        user_agents = agent_store.get_agent_slugs()

    needed_mcps: set[str] = set()
    mcps_with_overridable_config: set[str] = set()
    # Only surface MCPs that are ACTIVELY ENABLED (manager-enabled + admin-
    # authorized + platform-enabled) on at least one of the user's agents.
    # An MCP that's merely visible-but-not-enabled doesn't need user creds
    # yet — surfacing it adds noise to the Integrations page.
    for agent_name in user_agents:
        enabled = await asyncio.to_thread(
            mcp_registry.get_agent_mcps, agent_name,
        )
        for manifest in enabled:
            mcp_name = manifest.name
            cred_schema = _get_cred_schema(mcp_name)
            if cred_schema and cred_schema.get("type") == "per_user":
                needed_mcps.add(mcp_name)
            if any(f.user_overridable for f in manifest.config):
                mcps_with_overridable_config.add(mcp_name)

    # Merge: MCPs with per-user creds OR user-overridable config
    all_mcps = needed_mcps | mcps_with_overridable_config

    result = []
    for mcp_name in sorted(all_mcps):
        cred_info = _get_cred_schema(mcp_name)
        manifest = mcp_registry.get_manifest(mcp_name)

        # List ALL connected accounts for this MCP. Each account carries
        # its own configured_keys (for the UI to render per-account
        # status). For OAuth accounts, the GOOGLE_EMAIL credential row
        # populates display_email; otherwise the user-set label stands.
        accounts_raw = await asyncio.to_thread(
            credential_store.list_user_accounts, user.sub, mcp_name,
        )
        accounts = []
        agent_bindings = await asyncio.to_thread(
            credential_store.list_agent_account_bindings, user.sub, mcp_name,
        )
        # group bindings by account_label
        bindings_by_account: dict[str, list[str]] = {}
        for b in agent_bindings:
            bindings_by_account.setdefault(b["account_label"], []).append(
                b["agent_name"]
            )
        # Per-provider credential key names (workspace-mcp uses GOOGLE_*,
        # slack-mcp uses SLACK_*, etc.). Resolves via the manifest so adding
        # a new OAuth MCP doesn't need a code change here.
        oauth_block = (manifest.credentials.oauth or {}) if manifest else {}
        provider_id = oauth_block.get("provider_id", "")
        email_key, services_key = oauth_account_store.resolve_account_credential_keys(
            oauth_block, provider_id,
        )
        for acc in accounts_raw:
            acc_creds = await asyncio.to_thread(
                credential_store.get_user_credentials,
                user.sub, mcp_name, acc["account_label"],
            )
            connected_services = [
                s for s in (acc_creds.get(services_key, "") or "").split(",") if s
            ]

            # Scope incremental-add support: compare the account's
            # token-recorded scopes vs the manifest's required scopes for
            # the user's currently-enabled services. Missing scopes
            # surface a "Grant additional access" UI hint without forcing
            # a full reconnect.
            missing_scopes: list[str] = []
            if cred_info and cred_info.get("oauth"):
                required_scopes = await asyncio.to_thread(
                    mcp_registry.build_oauth_scopes, mcp_name, connected_services,
                )
                granted = await asyncio.to_thread(
                    _read_account_token_scopes,
                    user.sub, mcp_name, acc["account_label"],
                )
                missing_scopes = [s for s in required_scopes if s not in granted]

            accounts.append({
                "account_label": acc["account_label"],
                "display_email": acc.get("display_email", "") or acc_creds.get(email_key, ""),
                "is_default": acc["is_default"],
                "created_at": acc["created_at"],
                "configured_keys": list(acc_creds.keys()),
                "connected_services": connected_services,
                "agent_overrides": sorted(
                    bindings_by_account.get(acc["account_label"], [])
                ),
                "missing_scopes": missing_scopes,
            })

        # Build user-overridable config fields with admin defaults
        overridable_config = []
        if manifest:
            admin_defaults = mcp_store.get_mcp_config_values(mcp_name)
            for f in manifest.config:
                if f.user_overridable:
                    overridable_config.append({
                        "key": f.key,
                        "label": f.label,
                        "input_type": f.input_type,
                        "default_value": admin_defaults.get(f.key, f.default),
                    })

        # Per-agent binding candidates. Two filters, both required:
        # 1. Agent must be in the user's DIRECTLY ASSIGNED list (admins
        #    are NOT extended to all platform agents here — binding a
        #    personal OAuth account to an agent the user doesn't actively
        #    use is meaningless).
        # 2. The MCP must be ENABLED on that agent (manager has actively
        #    turned it on via `agent_mcps`). VISIBILITY alone isn't enough —
        #    binding to an agent where the MCP isn't running would never
        #    take effect at session start.
        candidate_agents = []
        for agent in user_assigned_agents:
            enabled = await asyncio.to_thread(
                mcp_registry.get_agent_mcps, agent,
            )
            if any(m.name == mcp_name for m in enabled):
                candidate_agents.append(agent)

        entry = {
            "mcp_name": mcp_name,
            "display_name": (cred_info.get("label", "") if cred_info else "") or (manifest.label if manifest else mcp_name),
            "description": (cred_info.get("description", "") if cred_info else "") or (manifest.description if manifest else ""),
            "configured": bool(accounts),  # any account counts as configured
            "required_keys": [f["key"] for f in cred_info.get("fields", [])] if cred_info else [],
            "fields": cred_info.get("fields", []) if cred_info else [],
            "oauth": cred_info.get("oauth", False) if cred_info else False,
            "oauth_services": cred_info.get("oauth_services", []) if cred_info else [],
            "oauth_meta": cred_info.get("oauth_meta", {}) if cred_info else {},
            "supports_multi_account": (
                (cred_info.get("oauth_meta") or {}).get("supports_multi_account", True)
                if cred_info else True
            ),
            "overridable_config": overridable_config,
            "accounts": accounts,
            "candidate_agents": candidate_agents,
        }

        result.append(entry)

    return result


@router.put("/v1/users/me/integrations/{mcp_name}")
async def set_my_integration(
    mcp_name: str,
    body: SetCredentialRequest,
    user: UserContext = Depends(get_current_user),
):
    """Set credentials for one labeled account of a per-user MCP.

    ``body.account_label`` (default ``'default'``) selects which labeled
    account to upsert. Adding a second account = same endpoint with a
    different ``account_label`` and the same field values.
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")

    cred_info = _get_cred_schema(mcp_name)
    if not cred_info or cred_info.get("type") != "per_user":
        raise HTTPException(400, f"'{mcp_name}' is not a per-user MCP")
    if cred_info.get("oauth"):
        raise HTTPException(400, f"'{mcp_name}' uses OAuth — connect via Settings page")

    # Validate required keys are present
    required = {f["key"] for f in cred_info.get("fields", [])}
    provided = set(body.credentials.keys())
    missing = required - provided
    if missing:
        raise HTTPException(400, f"Missing required credentials: {', '.join(missing)}")

    # Filter to only allow known keys
    allowed_keys = required | {
        f["key"] for f in cred_info.get("server_config_fields", [])
    }
    filtered = {k: v for k, v in body.credentials.items() if k in allowed_keys}

    label = (body.account_label or "default").strip()
    await asyncio.to_thread(
        credential_store.set_user_credentials,
        user.sub, mcp_name, filtered, label,
    )
    # For plain-credential MCPs that carry an email field, mirror to
    # display_email so the account list UI shows a friendly identifier.
    email_keys = ("EMAIL_USER", "NEXTCLOUD_USER", "NEXTCLOUD_USERNAME", "USERNAME")
    for k in email_keys:
        if k in filtered:
            await asyncio.to_thread(
                credential_store.set_account_display_email,
                user.sub, mcp_name, label, filtered[k],
            )
            break

    return {"status": "ok", "mcp_name": mcp_name, "account_label": label}


@router.delete("/v1/users/me/integrations/{mcp_name}")
async def delete_my_integration(
    mcp_name: str,
    account_label: str,
    user: UserContext = Depends(get_current_user),
):
    """Remove the credentials for one labeled account.

    ``?account_label=<label>`` is required. To remove every account for an
    MCP, the dashboard issues one DELETE per account row.
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")

    await asyncio.to_thread(
        credential_store.delete_user_credentials,
        user.sub, mcp_name, account_label,
    )
    return {"status": "ok", "mcp_name": mcp_name, "account_label": account_label}


# --- Multi-account: default + per-agent binding ---


@router.put("/v1/users/me/integrations/{mcp_name}/default-account")
async def set_default_account(
    mcp_name: str,
    body: SetDefaultAccountRequest,
    user: UserContext = Depends(get_current_user),
):
    """Mark ``body.account_label`` as the ⭐ default account for this MCP.

    Unsets any other account's default — partial unique index enforces
    one default per (user_sub, mcp_name).
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")
    ok = await asyncio.to_thread(
        credential_store.set_default_account,
        user.sub, mcp_name, body.account_label,
    )
    if not ok:
        raise HTTPException(
            404, f"Account '{body.account_label}' not found for {mcp_name}",
        )
    return {"status": "ok"}


@router.put("/v1/users/me/integrations/{mcp_name}/agent-binding")
async def set_agent_binding(
    mcp_name: str,
    body: SetAgentBindingRequest,
    user: UserContext = Depends(get_current_user),
):
    """Pin an agent to a specific account.

    Two gates (matching the candidate-agents filter in
    ``list_my_integrations`` — applied server-side too as defense in depth
    against a UI bypass):

    1. The agent must be in the caller's DIRECTLY ASSIGNED list. Admin
       role does NOT extend this check — binding a personal OAuth account
       to an agent the admin isn't assigned to is a personal-use decision
       that shouldn't be triggered by management role alone.
    2. The MCP must be ENABLED on that agent (manager has actively turned
       it on). A binding to an agent where the MCP isn't running would
       never resolve at session start, so reject up-front.
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")
    user_assigned_agents = await asyncio.to_thread(
        task_store.get_user_agents, user.sub,
    )
    if body.agent_name not in user_assigned_agents:
        raise HTTPException(
            403,
            f"Agent {body.agent_name!r} is not in your assigned agents. "
            f"Per-agent account bindings are limited to agents you actively use.",
        )
    enabled = await asyncio.to_thread(
        mcp_registry.get_agent_mcps, body.agent_name,
    )
    if not any(m.name == mcp_name for m in enabled):
        raise HTTPException(
            400,
            f"MCP {mcp_name!r} is not enabled on agent {body.agent_name!r}",
        )
    ok = await asyncio.to_thread(
        credential_store.set_account_agent_binding,
        user.sub, mcp_name, body.agent_name, body.account_label,
    )
    if not ok:
        raise HTTPException(
            404, f"Account '{body.account_label}' not found for {mcp_name}",
        )
    return {"status": "ok"}


@router.delete("/v1/users/me/integrations/{mcp_name}/agent-binding/{agent_name}")
async def remove_agent_binding(
    mcp_name: str,
    agent_name: str,
    user: UserContext = Depends(get_current_user),
):
    """Drop the per-agent override (agent reverts to user's default account).

    Same assigned-agents gate as ``set_agent_binding`` — admin role does
    NOT bypass; the binding is owned by the caller and can only be removed
    by the caller if they're assigned to the agent. Safe to call when no
    binding exists (idempotent).
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")
    user_assigned_agents = await asyncio.to_thread(
        task_store.get_user_agents, user.sub,
    )
    if agent_name not in user_assigned_agents:
        raise HTTPException(
            403,
            f"Agent {agent_name!r} is not in your assigned agents.",
        )
    await asyncio.to_thread(
        credential_store.remove_account_agent_binding,
        user.sub, mcp_name, agent_name,
    )
    return {"status": "ok"}


# --- Agent-binding for service accounts (per-agent UI) ---
# These endpoints replace the legacy admin agent-binding routes. Caller must
# be a manager of the target agent (or admin); the write rule is enforced
# against the bound account's owner_sub.


@router.get("/v1/agents/{agent_name}/mcps/{mcp_name}/service-account-options")
async def get_agent_service_account_options(
    agent_name: str,
    mcp_name: str,
    user: UserContext = Depends(get_current_user),
):
    """Return the dropdown data for binding an agent to a service account.

    Manager+ of the agent only. The dropdown shows ``my_accounts`` — the
    caller's connected ``user_credential_accounts`` rows for this MCP. A
    manager binding their own account makes it double as the agent's service
    identity.

    Response shape:
      {
        "my_accounts":     [...],
        "current_binding": {label, owner_sub, owner_name, owner_email,
                            set_by, set_at} | null,
      }
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")
    if not user.can_manage_agent(agent_name):
        raise HTTPException(
            403, f"Manager role on agent {agent_name!r} required",
        )

    cred_info = _get_cred_schema(mcp_name)
    if not cred_info or not cred_info.get("has_service_account"):
        raise HTTPException(
            400, f"'{mcp_name}' is not a service-account-capable MCP",
        )

    # Visibility gate — the MCP must be in the agent's visible set.
    visible = await asyncio.to_thread(
        mcp_registry.get_visible_mcps_for_agent, agent_name,
    )
    if not any(m.name == mcp_name for m in visible):
        raise HTTPException(
            400, f"MCP {mcp_name!r} is not visible to agent {agent_name!r}",
        )

    # Caller's own connected user accounts for this MCP.
    my_accounts = (
        await asyncio.to_thread(
            credential_store.list_user_accounts, user.sub, mcp_name,
        )
        if user.sub else []
    )

    binding = await asyncio.to_thread(
        credential_store.get_service_agent_binding, mcp_name, agent_name,
    )
    current_binding = None
    if binding is not None:
        bound_label, bound_owner = binding
        owner_name = bound_owner
        owner_email = ""
        bound_display_email = ""
        owner_row = await asyncio.to_thread(task_store.get_user, bound_owner)
        if owner_row:
            owner_name = (
                owner_row.get("display_name")
                or owner_row.get("name")
                or owner_row.get("username")
                or bound_owner
            )
            owner_email = owner_row.get("email") or ""
        # Display email comes from user_credential_accounts(bound_owner, mcp, bound_label).
        bound_accounts = await asyncio.to_thread(
            credential_store.list_user_accounts, bound_owner, mcp_name,
        )
        bound_row = next(
            (a for a in bound_accounts if a["account_label"] == bound_label),
            None,
        )
        bound_display_email = (bound_row or {}).get("display_email", "")
        current_binding = {
            "label": bound_label,
            "owner_sub": bound_owner,
            "owner_name": owner_name,
            "owner_email": owner_email or bound_display_email,
            "set_by": "",
            "set_at": "",
        }
        # Reach back into the full bindings list to surface set_by/set_at.
        agent_bindings = await asyncio.to_thread(
            credential_store.list_service_agent_bindings, mcp_name,
        )
        for b in agent_bindings:
            if b["agent_name"] == agent_name:
                current_binding["set_by"] = b.get("set_by") or ""
                current_binding["set_at"] = b.get("set_at") or ""
                break

    def _shape(rows: list[dict]) -> list[dict]:
        return [
            {
                "label": r["account_label"],
                "display_email": r.get("display_email", ""),
                "is_default": r.get("is_default", False),
            }
            for r in rows
        ]

    return {
        "my_accounts": _shape(my_accounts),
        "current_binding": current_binding,
    }


@router.put("/v1/agents/{agent_name}/mcps/{mcp_name}/service-binding")
async def set_agent_service_binding(
    agent_name: str,
    mcp_name: str,
    body: SetServiceBindingRequest,
    user: UserContext = Depends(get_current_user),
):
    """Pin an agent to the caller's OWN connected account as its service identity.

    The caller must have per-agent manager/admin role on ``agent_name``. The
    bound account is always the caller's own ``user_credential_accounts`` row
    (``owner_sub = caller.sub``) — agent-scope sessions then read the caller's
    tokens directly.

    Visibility gate: the MCP must be in the agent's visible set.
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")
    if not user.can_manage_agent(agent_name):
        raise HTTPException(
            403, f"Manager role on agent {agent_name!r} required",
        )
    if not user.sub:
        raise HTTPException(403, "A user session is required to bind an account")

    cred_info = _get_cred_schema(mcp_name)
    if not cred_info or not cred_info.get("has_service_account"):
        raise HTTPException(
            400, f"'{mcp_name}' is not a service-account-capable MCP",
        )
    visible = await asyncio.to_thread(
        mcp_registry.get_visible_mcps_for_agent, agent_name,
    )
    if not any(m.name == mcp_name for m in visible):
        raise HTTPException(
            400, f"MCP {mcp_name!r} is not visible to agent {agent_name!r}",
        )

    ok = await asyncio.to_thread(
        functools.partial(
            credential_store.set_service_agent_binding,
            mcp_name, agent_name,
            account_label=body.account_label,
            owner_sub=user.sub,
            set_by=user.sub,
        ),
    )
    if not ok:
        raise HTTPException(
            404,
            f"Account '{body.account_label}' not found among your connected "
            f"{mcp_name} accounts",
        )
    return {
        "status": "ok",
        "agent_name": agent_name,
        "mcp_name": mcp_name,
        "account_label": body.account_label,
        "owner_sub": user.sub,
    }


@router.delete("/v1/agents/{agent_name}/mcps/{mcp_name}/service-binding")
async def clear_agent_service_binding(
    agent_name: str,
    mcp_name: str,
    user: UserContext = Depends(get_current_user),
):
    """Clear the agent → service-account binding (the agent then has no bound
    credential for this MCP in agent scope; there is no platform default).

    Same per-agent manager/admin gate as the PUT. Idempotent.
    """
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")
    if not user.can_manage_agent(agent_name):
        raise HTTPException(
            403, f"Manager role on agent {agent_name!r} required",
        )
    await asyncio.to_thread(
        credential_store.remove_service_agent_binding,
        mcp_name, agent_name,
    )
    return {"status": "ok", "agent_name": agent_name, "mcp_name": mcp_name}


# --- Admin: Infrastructure Credentials ---

def _require_admin(user: UserContext):
    if user.is_service:
        return  # the trusted master key is admin-equivalent (service-to-service)
    if not user.is_admin:
        raise HTTPException(403, "Admin only")


@router.get("/v1/admin/integrations")
async def list_admin_integrations(
    user: UserContext = Depends(get_current_user),
):
    """List all infrastructure MCPs with their configured-key status, plus
    the email-server config block.
    """
    _require_admin(user)

    all_infra = await asyncio.to_thread(credential_store.get_all_infra_credentials)

    infra_list = []

    all_schemas = mcp_registry.get_all_credential_schemas()
    for mcp_name, cred_info in sorted(all_schemas.items()):
        mcp_type = cred_info.get("type")

        if mcp_type == "infra":
            infra_list.append({
                "mcp_name": mcp_name,
                "display_name": cred_info.get("label", mcp_name),
                "description": cred_info.get("description", ""),
                "configured": mcp_name in all_infra,
                "configured_keys": list(all_infra.get(mcp_name, {}).keys()),
                "fields": cred_info.get("fields", []),
            })

    # Email server config
    email_cfg = all_infra.get("email-server", {})

    return {
        "infrastructure": infra_list,
        "email_server_config": {
            "smtp_host": email_cfg.get("SMTP_HOST", ""),
            "smtp_port": email_cfg.get("SMTP_PORT", ""),
            "smtp_secure": email_cfg.get("SMTP_SECURE", ""),
            "imap_host": email_cfg.get("IMAP_HOST", ""),
            "imap_port": email_cfg.get("IMAP_PORT", ""),
            "imap_secure": email_cfg.get("IMAP_SECURE", ""),
        },
    }


@router.put("/v1/admin/integrations/infra/{mcp_name}")
async def set_infra_integration(
    mcp_name: str,
    body: SetCredentialRequest,
    user: UserContext = Depends(get_current_user),
):
    """Set infrastructure MCP credentials."""
    _require_admin(user)

    cred_info = _get_cred_schema(mcp_name)
    is_infra = cred_info and cred_info.get("type") == "infra"
    if not is_infra and not _is_app_credential(mcp_name):
        raise HTTPException(400, f"'{mcp_name}' is not an infrastructure MCP")

    await asyncio.to_thread(
        credential_store.set_infra_credentials, mcp_name, body.credentials
    )
    # oauth_engine reads app credentials per-request via
    # credential_store.get_infra_credentials — no in-memory cache to
    # invalidate.

    return {"status": "ok", "mcp_name": mcp_name}


@router.delete("/v1/admin/integrations/infra/{mcp_name}")
async def delete_infra_integration(
    mcp_name: str,
    user: UserContext = Depends(get_current_user),
):
    """Remove infrastructure MCP credentials."""
    _require_admin(user)

    await asyncio.to_thread(
        credential_store.delete_infra_credentials, mcp_name
    )
    return {"status": "ok", "mcp_name": mcp_name}


@router.put("/v1/admin/integrations/email-server-config")
async def set_email_server_config(
    body: SetEmailServerConfigRequest,
    user: UserContext = Depends(get_current_user),
):
    """Set default email server configuration (SMTP/IMAP host/port)."""
    _require_admin(user)

    creds = {}
    if body.smtp_host:
        creds["SMTP_HOST"] = body.smtp_host
    if body.smtp_port:
        creds["SMTP_PORT"] = body.smtp_port
    if body.smtp_secure:
        creds["SMTP_SECURE"] = body.smtp_secure
    if body.imap_host:
        creds["IMAP_HOST"] = body.imap_host
    if body.imap_port:
        creds["IMAP_PORT"] = body.imap_port
    if body.imap_secure:
        creds["IMAP_SECURE"] = body.imap_secure

    if creds:
        await asyncio.to_thread(
            credential_store.set_infra_credentials, "email-server", creds
        )
    return {"status": "ok"}


# --- OAuth Bearer-Allowlist (admin) ---


class AddBearerAllowlistRequest(BaseModel):
    provider_id: str
    host_pattern: str


@router.get("/v1/admin/oauth-bearer-allowlist")
async def list_bearer_allowlist(
    user: UserContext = Depends(get_current_user),
):
    """List approved (provider, host) pairs for OAuth bearer-token injection."""
    _require_admin(user)
    from storage.identity import bearer_allowlist
    rows = await asyncio.to_thread(bearer_allowlist.list_allowed)
    return {"entries": rows}


@router.post("/v1/admin/oauth-bearer-allowlist")
async def add_bearer_allowlist(
    body: AddBearerAllowlistRequest,
    user: UserContext = Depends(get_current_user),
):
    """Approve a (provider, host_pattern) pair. Idempotent on conflict."""
    _require_admin(user)
    from storage.identity import bearer_allowlist
    if not body.provider_id.strip() or not body.host_pattern.strip():
        raise HTTPException(400, "provider_id and host_pattern required")
    added_by = "api-key" if user.is_api_key else user.sub
    row_id = await asyncio.to_thread(
        bearer_allowlist.add_allowed,
        body.provider_id.strip(), body.host_pattern.strip(), added_by,
    )
    return {"status": "ok", "id": row_id}


@router.delete("/v1/admin/oauth-bearer-allowlist/{row_id}")
async def remove_bearer_allowlist(
    row_id: int,
    user: UserContext = Depends(get_current_user),
):
    """Revoke a (provider, host) pair. Future bearer injections to that
    host are dropped (existing sessions keep their cached header until
    refresh)."""
    _require_admin(user)
    from storage.identity import bearer_allowlist
    ok = await asyncio.to_thread(bearer_allowlist.delete_allowed, row_id)
    if not ok:
        raise HTTPException(404, "Allowlist entry not found")
    return {"status": "ok"}


@router.post("/v1/admin/oauth-bearer-allowlist/restore-defaults")
async def restore_bearer_allowlist_defaults(
    user: UserContext = Depends(get_current_user),
):
    """Re-add any deleted vendor-official defaults. Idempotent — admin-added
    entries and existing defaults are untouched. Returns the refreshed list."""
    _require_admin(user)
    from storage.identity import bearer_allowlist
    rows = await asyncio.to_thread(bearer_allowlist.restore_defaults)
    return {"entries": rows}
