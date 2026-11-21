"""Credential Management REST API.

User-facing endpoints for per-user MCP credential management.
Admin-facing endpoints for infrastructure + service account credentials.
Credential schemas are read from MCP manifests via mcp_registry.
Auth: OAuth2 session cookie (dashboard users).
"""

import asyncio
import functools
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.mcp import account_sets, service_account_set as service_account_set_api
from auth import github_reach
from auth.oauth_providers import github_app
from auth.providers import (
    UserContext,
    get_current_user,
    require_admin,
    require_auth,
    require_user,
)
from services.mcp import account_context, mcp_registry
from services.oauth import credential_resolver, github_apps, oauth_account_store
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import (
    credential_store,
    oauth_app_kinds,
    service_account_set as service_account_set_store,
)
from storage.mcp import mcp_store

if TYPE_CHECKING:
    from services.mcp.mcp_manifest_types import McpManifest

logger = logging.getLogger("otodock.credential-api")
router = APIRouter(dependencies=[Depends(require_user)])
# An agent's set of a person's accounts (``proxy/app.py`` mounts only this
# router).
router.include_router(account_sets.router)
router.include_router(service_account_set_api.router)


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
    # The person's own GitHub reach for the agent with this account
    # (``auth/github_reach``): absent = keep what the choice holds.
    repositories: list[str] | None = Field(default=None, max_length=github_reach.MAX_REPOSITORIES)
    read_only: bool | None = None


def _get_cred_schema(mcp_name: str) -> dict | None:
    """Get credential schema from manifest."""
    return mcp_registry.get_credential_schema(mcp_name)


def _is_app_credential(name: str) -> bool:
    """Check if name is referenced as app_credential by any manifest."""
    for schema in mcp_registry.get_all_credential_schemas().values():
        if schema.get("app_credential") == name:
            return True
    return False


def _account_token_status(
    user_sub: str, mcp_name: str, account_label: str,
) -> tuple[list[str], str, str]:
    """The scopes recorded in an account's token file, the reason it needs
    a reconnect (``""`` when it does not) and the way it was connected
    (``extra.flow``, ``""`` for an OAuth sign-in), read in one pass.

    ``list_my_integrations`` computes ``missing_scopes`` from the scopes
    (the "Add additional access" affordance) and ``needs_reconnect`` from
    the reason (a grant the vendor ended, an expired token nobody can
    refresh, a file the manifest's way of issuing tokens no longer covers,
    the MCP's own server refusing the token). The flow lets the card offer
    a new personal access token in place of a sign-in. A missing or corrupt
    file counts as no scopes granted, which surfaces every required scope
    as missing: the UI prompts a reconnect.
    """
    from services.oauth import credential_resolver, oauth_account_store
    manifest = mcp_registry.get_manifest(mcp_name)
    provider_id = (
        (manifest.credentials.oauth or {}).get("provider_id", "")
        if manifest else ""
    )
    if not provider_id:
        return [], "", ""
    username = task_store.get_username_by_sub(user_sub)
    if not username:
        return [], "", ""
    token_dir = oauth_account_store.get_token_dir(username, provider_id=provider_id)
    data = oauth_account_store.read_account_token(token_dir, account_label)
    if not data:
        from services.webhooks import relay_notices  # a file the vendor's removal took
        return [], relay_notices.removed_reason(user_sub, mcp_name, account_label), ""
    reason = credential_resolver.account_unusable_reason(data, manifest)
    extra = data.get("extra") or {}
    flow = str(extra.get("flow") or "") if isinstance(extra, dict) else ""
    raw = data.get("scopes", []) or []
    if isinstance(raw, str):
        # Some legacy formats stored as space-separated string.
        return [s for s in raw.split() if s], reason, flow
    return list(raw), reason, flow


def _app_in_use_quietly(mcp_name: str) -> github_apps.AppInUse:
    """The app an MCP's accounts go through, the OAuth App when the relay
    cannot say (the integrations page never fails on it)."""
    try:
        return github_apps.app_in_use(mcp_name)
    except (*github_apps.UNREACHABLE, ValueError):
        return github_apps.AppInUse(kind=oauth_app_kinds.KIND_OAUTH_APP, hosted=True)


def _service_binding_summary(agent_name: str, user: UserContext) -> dict:
    """One ``service_bindings`` entry of an account summary: the agent an
    account serves as service identity, whether the caller manages it and
    whether its visibility mode offers agent scope (a service-scope
    subscription needs both)."""
    from core.session.visibility import available_scopes_for
    row = agent_store.get_agent(agent_name) or {}
    scopes = available_scopes_for(
        bool(row.get("collaborative", True)), row.get("default_scope") or "user",
    )
    return {
        "agent_name": agent_name,
        "display_name": row.get("display_name") or agent_name,
        "can_manage": user.can_manage_agent(agent_name),
        "agent_scope_available": "agent" in scopes,
    }


# --- MCP Schema ---

@router.get("/v1/mcp-credential-schema")
async def get_credential_schema(
    user: UserContext = Depends(get_current_user),
):
    """Return all MCP credential schemas (drives dashboard forms)."""
    if require_auth(user).is_api_key:
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
    if require_auth(user).is_api_key:
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
    manifest: McpManifest | None
    # Only surface MCPs that are ACTIVELY ENABLED (manager-enabled + admin-
    # authorized + platform-enabled) on at least one of the user's agents.
    # An MCP that's merely visible-but-not-enabled doesn't need user creds
    # yet — surfacing it adds noise to the Integrations page. Every placement:
    # a tool that loads only on a remote machine (an app connector) needs the
    # person's account as much as any other. A tool with no account of its
    # own is not listed: a person has nothing to set on its card (a
    # ``user_overridable`` config row is listed on an account's card but
    # nothing stores a person's value for it).
    for agent_name in user_agents:
        enabled = await asyncio.to_thread(
            mcp_registry.get_agent_mcps_all_placements, agent_name,
        )
        for manifest in enabled:
            cred_schema = _get_cred_schema(manifest.name)
            if cred_schema and cred_schema.get("type") == "per_user":
                needed_mcps.add(manifest.name)

    result = []
    for mcp_name in sorted(needed_mcps):
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
        # group the agents' set rows by account_label (an agent whose set
        # holds several accounts is under each of them)
        bindings_by_account: dict[str, list[str]] = {}
        defaults_by_account: dict[str, list[str]] = {}
        reaches_by_account: dict[str, list[dict]] = {}
        for b in agent_bindings:
            bindings_by_account.setdefault(b["account_label"], []).append(
                b["agent_name"]
            )
            if b["is_default"]:
                defaults_by_account.setdefault(b["account_label"], []).append(b["agent_name"])
            reaches_by_account.setdefault(b["account_label"], []).append({
                "agent": b["agent_name"], "repositories": b["repositories"], "read_only": b["read_only"],
            })
        # Per-provider credential key names (workspace-mcp uses GOOGLE_*,
        # slack-mcp uses SLACK_*, etc.). Resolves via the manifest so adding
        # a new OAuth MCP doesn't need a code change here.
        oauth_block = (manifest.credentials.oauth or {}) if manifest else {}
        provider_id = oauth_block.get("provider_id", "")
        email_key, services_key = oauth_account_store.resolve_account_credential_keys(
            oauth_block, provider_id,
        )
        # Agents whose own set holds one of THIS user's accounts: the
        # Connected Accounts card offers "Subscribe as <agent>" on each, since
        # every account of the set keeps its own subscriptions, so the option
        # never rests on a client-side role guess.
        service_rows = await asyncio.to_thread(
            credential_store.list_service_agent_bindings, mcp_name,
        )
        service_rows = [b for b in service_rows if b.get("account_owner_sub") == user.sub]
        for acc in accounts_raw:
            acc_creds = await asyncio.to_thread(
                credential_store.get_user_credentials,
                user.sub, mcp_name, acc["account_label"],
            )
            service_bindings = [
                _service_binding_summary(b["agent_name"], user)
                for b in service_rows
                if b.get("account_label") == acc["account_label"]
            ]
            connected_services = [
                s for s in (acc_creds.get(services_key, "") or "").split(",") if s
            ]

            # Scope incremental-add support: compare the account's
            # token-recorded scopes vs the manifest's required scopes for
            # the user's currently-enabled services. Missing scopes
            # surface a "Grant additional access" UI hint without forcing
            # a full reconnect.
            missing_scopes: list[str] = []
            reconnect_reason = flow = ""
            if cred_info and cred_info.get("oauth"):
                required_scopes = await asyncio.to_thread(
                    mcp_registry.build_oauth_scopes, mcp_name, connected_services,
                )
                granted, reconnect_reason, flow = await asyncio.to_thread(
                    _account_token_status,
                    user.sub, mcp_name, acc["account_label"],
                )
                missing_scopes = [] if reconnect_reason else [s for s in required_scopes if s not in granted]

            meta = acc.get("meta") or {}
            accounts.append({
                "account_label": acc["account_label"],
                "display_email": acc.get("display_email", "") or acc_creds.get(email_key, ""),
                "is_default": acc["is_default"],
                "created_at": acc["created_at"],
                "configured_keys": list(acc_creds.keys()),
                # A GitHub-App account: the kind and the installations the
                # person can reach (the card lists them and offers the
                # install page); absent on every other account.
                **({"kind": oauth_app_kinds.KIND_GITHUB_APP,
                    "installations": oauth_app_kinds.installations_of(meta)}
                   if oauth_app_kinds.is_github_app_meta(meta) else {}),
                # Required fields the saved account lacks (a field the
                # manifest added later): the MCP stays out of its sessions.
                "missing_required": (
                    [] if not cred_info or cred_info.get("oauth")
                    else credential_resolver.missing_required_labels(cred_info, acc_creds)
                ),
                "connected_services": connected_services,
                # Every agent whose set holds this account.
                "agent_overrides": sorted(
                    bindings_by_account.get(acc["account_label"], [])
                ),
                # The agents of ``agent_overrides`` whose set's default it is.
                "agent_defaults": sorted(defaults_by_account.get(acc["account_label"], [])),
                # The person's own GitHub reach for each agent of
                # ``agent_overrides`` with this account (``[]`` and false:
                # everything).
                "agent_reaches": reaches_by_account.get(acc["account_label"], []),
                "missing_scopes": missing_scopes,
                "service_bindings": service_bindings,
                # The card says "Reconnect needed" with the reason; the
                # resolver leaves such an account out of new sessions.
                "needs_reconnect": bool(reconnect_reason),
                "reconnect_reason": reconnect_reason,
                # How the account was connected (a personal access token's
                # card takes a new token in place of a sign-in).
                "flow": flow,
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
                mcp_registry.get_agent_mcps_all_placements, agent,
            )
            if any(m.name == mcp_name for m in enabled):
                candidate_agents.append(agent)

        # An agent whose people's chats run on its own accounts (a Shared-only
        # one, a team account): a person's choice for it never applies, so
        # the card offers none and names the account its chats use.
        from services.mcp import account_context
        agents_on_own_accounts = [
            a for a in candidate_agents
            if await asyncio.to_thread(account_context.people_use_agents_accounts, a)]
        agent_accounts = await service_account_set_api.accounts_in_use(mcp_name, agents_on_own_accounts)
        # Of those, the agents whose team account floor this person is
        # below: their chats there get no account at all.
        from auth.providers import acting_role_of
        agents_below_floor = []
        for a in agents_on_own_accounts:
            role = await asyncio.to_thread(acting_role_of, user.sub, a)
            if await asyncio.to_thread(account_context.team_floor_refusal, a, role):
                agents_below_floor.append(a)

        oauth_meta = dict(cred_info.get("oauth_meta", {}) if cred_info else {})
        if cred_info and cred_info.get("app_kinds"):
            # The kind a connect goes through now, and whether the install
            # page can be offered (a GitHub App with its slug known).
            in_use = await asyncio.to_thread(_app_in_use_quietly, mcp_name)
            oauth_meta["app_kind"] = in_use.kind
            oauth_meta["app_install_available"] = (
                in_use.kind == oauth_app_kinds.KIND_GITHUB_APP and bool(in_use.slug)
            )
            # The kinds a person may pick, the default first: OtoDock's
            # relay keeps its OAuth App beside the GitHub App it offers, an
            # admin registered one.
            oauth_meta["app_hosted"] = in_use.hosted
            oauth_meta["connect_kinds"] = (
                [oauth_app_kinds.KIND_GITHUB_APP, oauth_app_kinds.KIND_OAUTH_APP]
                if in_use.hosted and in_use.kind == oauth_app_kinds.KIND_GITHUB_APP
                else [in_use.kind]
            )
        if oauth_meta.get("authorization_server") and manifest is not None:
            # Whether a connect goes through the registered client right now
            # (hosted mode off, no app credentials): the form words itself by it.
            from services.oauth import mcp_authorization
            oauth_meta["authorization_server"] = {
                **oauth_meta["authorization_server"],
                "active": await asyncio.to_thread(
                    mcp_authorization.registered_client_mode, manifest,
                ),
            }
        entry = {
            "mcp_name": mcp_name,
            "display_name": (cred_info.get("label", "") if cred_info else "") or (manifest.label if manifest else mcp_name),
            "description": (cred_info.get("description", "") if cred_info else "") or (manifest.description if manifest else ""),
            "configured": bool(accounts),  # any account counts as configured
            "required_keys": credential_resolver.required_field_keys(cred_info) if cred_info else [],
            "fields": cred_info.get("fields", []) if cred_info else [],
            "oauth": cred_info.get("oauth", False) if cred_info else False,
            "oauth_services": cred_info.get("oauth_services", []) if cred_info else [],
            "oauth_meta": oauth_meta,
            "supports_multi_account": (
                (cred_info.get("oauth_meta") or {}).get("supports_multi_account", True)
                if cred_info else True
            ),
            "overridable_config": overridable_config,
            "accounts": accounts,
            "candidate_agents": candidate_agents,
            # A choice of account for an agent may narrow its GitHub reach.
            "reach_supported": _reach_supported(mcp_name),
            "agents_on_own_accounts": agents_on_own_accounts,
            "agent_accounts": agent_accounts,
            "agents_below_floor": agents_below_floor,
        }

        result.append(entry)

    return result


# A new account's label: the person's own word for it, which their agents
# read in their prompt (the accounts a chat may switch between).
_LABEL_MAX = 64


def _check_new_label(label: str) -> None:
    if (not label or len(label) > _LABEL_MAX or "`" in label
            or any(not ch.isprintable() for ch in label)):
        raise HTTPException(
            400, f"An account's label is one line of up to {_LABEL_MAX} characters, "
                 "with no backtick.")


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
    if require_auth(user).is_api_key:
        raise HTTPException(403, "Dashboard only")

    cred_info = _get_cred_schema(mcp_name)
    if not cred_info or cred_info.get("type") != "per_user":
        raise HTTPException(400, f"'{mcp_name}' is not a per-user MCP")
    if cred_info.get("oauth"):
        raise HTTPException(400, f"'{mcp_name}' uses OAuth — connect via Settings page")

    # Every required field must be in this save or already on the account
    # (an edit that adds an optional field alone keeps the rest); a field
    # declaring "required": false may stay unset.
    label = (body.account_label or "default").strip()
    stored = await asyncio.to_thread(
        credential_store.get_user_credentials, user.sub, mcp_name, label,
    )
    if not stored:
        _check_new_label(label)
    required = set(credential_resolver.required_field_keys(cred_info))
    missing = required - set(body.credentials) - set(stored or {})
    if missing:
        raise HTTPException(400, f"Missing required credentials: {', '.join(sorted(missing))}")

    # Filter to only allow known keys
    allowed_keys = {f["key"] for f in cred_info.get("fields", [])} | {
        f["key"] for f in cred_info.get("server_config_fields", [])
    }
    filtered = {k: v for k, v in body.credentials.items() if k in allowed_keys}

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
    if require_auth(user).is_api_key:
        raise HTTPException(403, "Dashboard only")

    # An agent's own set lends this account: it leaves as at an OAuth
    # disconnect, while its values still exist.
    from services.agents import offboarding_bindings
    for row in await asyncio.to_thread(credential_store.list_service_agent_bindings_for_owner, user.sub):
        if row["mcp_name"] == mcp_name and row["account_label"] == account_label:
            await offboarding_bindings.clear_binding(row, user.sub)
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
    if require_auth(user).is_api_key:
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
    """Make the agent's set of the caller's accounts exactly this one, its
    default (the meaning an open 1.9.1 page gives it; a set of several is
    written through ``api/mcp/account_sets.py``).

    The gates are ``_own_choice_gates``'s.
    """
    await _own_choice_gates(user, mcp_name, body.agent_name, body.account_label)
    await _check_own_reach(body, mcp_name, user)
    ok = await asyncio.to_thread(
        functools.partial(
            credential_store.set_account_agent_binding,
            user.sub, mcp_name, body.agent_name, body.account_label,
            repositories=body.repositories, read_only=body.read_only,
        ),
    )
    if not ok:
        raise HTTPException(
            404, f"Account '{body.account_label}' not found for {mcp_name}",
        )
    row = await asyncio.to_thread(
        credential_store.get_account_agent_binding, user.sub, mcp_name, body.agent_name,
        body.account_label,
    )
    return {
        "status": "ok",
        "repositories": list(row.repositories) if row else [],
        "read_only": row.read_only if row else False,
    }


async def _assigned_gate(user: UserContext, agent_name: str) -> None:
    """A person's own choice of account is limited to the agents they are
    DIRECTLY assigned. Admin role does NOT extend this check: binding a
    personal account to an agent the admin isn't assigned to is a
    personal-use decision, not an administrative one."""
    if require_auth(user).is_api_key:
        raise HTTPException(403, "Dashboard only")
    user_assigned_agents = await asyncio.to_thread(
        task_store.get_user_agents, user.sub,
    )
    if agent_name not in user_assigned_agents:
        raise HTTPException(
            403,
            f"Agent {agent_name!r} is not in your assigned agents. "
            f"You choose accounts only for the agents you use.",
        )


async def _own_choice_gates(user: UserContext, mcp_name: str, agent_name: str, account_label: str) -> None:
    """The gates of a write of a person's own account for an agent
    (matching the candidate-agents filter in ``list_my_integrations``,
    applied server-side too as defense in depth against a UI bypass): the
    assigned-agent gate (``_assigned_gate``); the MCP ENABLED on that agent
    (a manager turned it on: a choice for an agent where the MCP isn't
    running would never resolve at session start); the account one of the
    caller's own."""
    await _assigned_gate(user, agent_name)
    from services.mcp import account_context
    if await asyncio.to_thread(account_context.people_use_agents_accounts, agent_name):
        raise HTTPException(409, service_account_set_api.AGENTS_OWN)
    enabled = await asyncio.to_thread(
        mcp_registry.get_agent_mcps_all_placements, agent_name,
    )
    if not any(m.name == mcp_name for m in enabled):
        raise HTTPException(
            400,
            f"MCP {mcp_name!r} is not enabled on agent {agent_name!r}",
        )
    accounts = await asyncio.to_thread(credential_store.list_user_accounts, user.sub, mcp_name)
    if not any(a["account_label"] == account_label for a in accounts):
        raise HTTPException(
            404, f"Account '{account_label}' not found for {mcp_name}",
        )


_NO_OWN_LIST = (
    "This account has no list of repositories to pick from. You can still make an "
    "agent read only."
)


async def _check_own_reach(
    body: SetAgentBindingRequest | account_sets.AgentAccountRequest, mcp_name: str, user: UserContext, *,
    replaces_set: bool = True,
) -> None:
    """The person's own reach for an agent, refused in plain words when it
    cannot hold, as the service PUT refuses one: a narrowing on GitHub only,
    a list only on a GitHub-App account, a name the account does not reach.
    Only names the choice does not hold yet are looked up; a list kept onto
    another account is looked up on it (a token account keeps it as it is:
    it only narrows). The choice held is the named account's own row; with
    ``replaces_set`` (the write that makes the set this one account), the
    set's default when the set does not hold the account, whose reach that
    write keeps."""
    names = body.repositories or []
    if not _reach_supported(mcp_name):
        if names or body.read_only:
            raise HTTPException(400, _ONLY_GITHUB)
        return
    _refuse_unreadable(names)
    prior = await asyncio.to_thread(
        credential_store.get_account_agent_binding, user.sub, mcp_name, body.agent_name,
        body.account_label,
    )
    if prior is None and replaces_set:
        prior = await asyncio.to_thread(
            credential_store.get_account_agent_binding, user.sub, mcp_name, body.agent_name,
        )
    same_account = prior is not None and prior.account_label == body.account_label
    kept = body.repositories is None
    if kept:
        names = [] if prior is None or same_account else list(prior.repositories)
    if not names:
        return
    meta = await asyncio.to_thread(credential_store.get_account_meta, user.sub, mcp_name, body.account_label)
    if not oauth_app_kinds.is_github_app_meta(meta):
        if kept:
            return
        raise HTTPException(400, _NO_OWN_LIST)
    held = {github_reach.normalize(n) for n in prior.repositories} if prior is not None and same_account else set()
    added = [n for n in names if github_reach.normalize(n) not in held]
    if not added:
        return
    missing = await _unreached(user.sub, mcp_name, body.account_label, "", added, no_list=_NO_OWN_LIST)
    if missing and kept:
        raise HTTPException(400, f"{missing[0]} is one of the repositories you chose for this agent, and this "
                                 "account does not reach it. Change the repositories first, then choose this "
                                 "account again.")
    if missing:
        raise HTTPException(400, _NOT_REACHED.format(name=missing[0]))


@router.get("/v1/users/me/integrations/{mcp_name}/repositories")
async def get_my_account_repositories(
    mcp_name: str,
    account_label: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """The repositories one of the caller's own GitHub-App accounts reaches
    (every installation of it), for the person's own choice of
    repositories per agent: the service route's answer, ``{pickable,
    installations, from_meta}``. A token or OAuth account has no list
    (``pickable: false``)."""
    if require_auth(user).is_api_key:
        raise HTTPException(403, "Dashboard only")
    if not _reach_supported(mcp_name):
        raise HTTPException(400, _ONLY_GITHUB_LIST)
    return await _repositories_answer(user.sub, mcp_name, account_label, "")


@router.delete("/v1/users/me/integrations/{mcp_name}/agent-binding/{agent_name}")
async def remove_agent_binding(
    mcp_name: str,
    agent_name: str,
    user: UserContext = Depends(get_current_user),
):
    """Empty the agent's set of the caller's accounts (the agent reverts to
    the user's default account), the meaning an open 1.9.1 page gives it.

    Same assigned-agents gate as ``set_agent_binding`` — admin role does
    NOT bypass; the set is owned by the caller and can only be emptied
    by the caller if they're assigned to the agent. Safe to call when the
    set is empty (idempotent).
    """
    await _assigned_gate(user, agent_name)
    await asyncio.to_thread(
        credential_store.remove_account_agent_binding,
        user.sub, mcp_name, agent_name,
    )
    return {"status": "ok"}


# --- Agent-binding for service accounts (per-agent UI) ---
# These endpoints replace the legacy admin agent-binding routes. Caller must
# be a manager of the target agent (or admin); the write rule is enforced
# against the bound account's owner_sub.


async def _service_gates(user: UserContext, agent_name: str, mcp_name: str) -> None:
    """The gates of every write and read of an agent's own accounts: the
    dashboard, a manager of the agent, an agent with accounts of its own (not
    Personal only), a tool that takes a service account and is in the
    agent's visible set."""
    if require_auth(user).is_api_key:
        raise HTTPException(403, "Dashboard only")
    if not user.can_manage_agent(agent_name):
        raise HTTPException(403, f"Manager role on agent {agent_name!r} required")
    if not await asyncio.to_thread(account_context.holds_own_accounts, agent_name):
        raise HTTPException(409, account_context.PERSONAL_ONLY)
    cred_info = _get_cred_schema(mcp_name)
    if not cred_info or not cred_info.get("has_service_account"):
        raise HTTPException(400, f"'{mcp_name}' is not a service-account-capable MCP")
    visible = await asyncio.to_thread(mcp_registry.get_visible_mcps_for_agent, agent_name)
    if not any(m.name == mcp_name for m in visible):
        raise HTTPException(400, f"MCP {mcp_name!r} is not visible to agent {agent_name!r}")


@router.get("/v1/agents/{agent_name}/mcps/{mcp_name}/service-account-options")
async def get_agent_service_account_options(
    agent_name: str,
    mcp_name: str,
    user: UserContext = Depends(get_current_user),
):
    """What the agent's accounts panel shows for one tool: the agent's own
    set (``accounts``, each with whose it is, how it acts and its GitHub
    reach, the default first), the caller's connected accounts it may add
    (``my_accounts``, with a GitHub-App account's installations) and whether
    the tool takes a GitHub reach. Manager of the agent only."""
    await _service_gates(user, agent_name, mcp_name)

    # Caller's own connected user accounts for this MCP.
    my_accounts = (
        await asyncio.to_thread(
            credential_store.list_user_accounts, user.sub, mcp_name,
        )
        if user.sub else []
    )

    def _shape(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            meta = r.get("meta") or {}
            entry = {
                "label": r["account_label"],
                "display_email": r.get("display_email", ""),
                "is_default": r.get("is_default", False),
            }
            if oauth_app_kinds.is_hosted_app_meta(meta) or (hosted and oauth_app_kinds.is_github_app_meta(meta)):
                # Hosted sign-in has no app identity: the account itself only.
                entry["kind"] = oauth_app_kinds.KIND_GITHUB_APP
                entry["hosted"] = True
            elif oauth_app_kinds.is_github_app_meta(meta):
                entry["kind"] = oauth_app_kinds.KIND_GITHUB_APP
                entry["installations"] = [
                    {"id": i.get("id"), "account": i.get("account", ""),
                     "target_type": i.get("target_type", ""),
                     "repository_selection": i.get("repository_selection", ""),
                     "repositories_count": len(i.get("repositories") or []),
                     "repositories_truncated": bool(i.get("repositories_truncated")),
                     "suspended": bool(i.get("suspended_at")), "admin": bool(i.get("admin"))}
                    for i in oauth_app_kinds.installations_of(meta)
                ]
            out.append(entry)
        return out

    hosted = await asyncio.to_thread(github_apps.hosted, mcp_name)
    return {
        "my_accounts": _shape(my_accounts),
        # Each account may carry a GitHub reach (a repository list, read only).
        "reach_supported": _reach_supported(mcp_name),
        # The agent's own set (``api/mcp/service_account_set.py``).
        **await service_account_set_api.set_answer(agent_name, mcp_name, user.sub or ""),
    }


def _reach_supported(mcp_name: str) -> bool:
    manifest = mcp_registry.get_manifest(mcp_name)
    oauth = (manifest.credentials.oauth or {}) if manifest and manifest.credentials else {}
    return str(oauth.get("provider_id") or "") == github_app.PROVIDER_ID


_NO_LIST = (
    "This account has no list of repositories to pick from. An agent bound to it "
    "can still be made read only."
)
_ONLY_GITHUB = "Only GitHub can be narrowed to some repositories or to reading."
_ONLY_GITHUB_LIST = "Only GitHub can be narrowed to some repositories."
_NOT_REACHED = "{name} is not among the repositories this account reaches."


def _refuse_unreadable(names: list[str]) -> None:
    bad = [n for n in names if not github_reach.normalize(n)]
    if bad:
        raise HTTPException(400, f"{bad[0]} is not a repository name. Write it as owner/name.")


async def _unreached(
    user_sub: str, mcp_name: str, account_label: str, installation: str, added: list[str], *, no_list: str,
) -> list[str]:
    """The ``added`` names the account (or its installation) does not
    reach, looked up live; 503 when GitHub cannot be asked and a name is
    not in the list OtoDock saw last."""
    try:
        listed, from_meta = await asyncio.to_thread(
            github_apps.reachable_repositories, user_sub, mcp_name, account_label, installation,
        )
    except LookupError as e:
        raise HTTPException(400, no_list) from e
    missing = github_apps.unreachable_names(listed, added)
    if missing and from_meta:
        raise HTTPException(503, "GitHub could not be reached to check the repositories. Try again in a minute.")
    return missing


async def _repositories_answer(user_sub: str, mcp_name: str, account_label: str, installation: str) -> dict:
    try:
        listed, from_meta = await asyncio.to_thread(
            github_apps.reachable_repositories, user_sub, mcp_name, account_label, installation,
        )
    except LookupError:
        return {"pickable": False, "installations": [], "from_meta": False}
    return {"pickable": True, "installations": listed, "from_meta": from_meta}


async def _check_reach(
    body: service_account_set_api.ServiceAccountRequest | service_account_set_api.ServiceReachRequest,
    mcp_name: str, user: UserContext, *, held: credential_store.ServiceBinding | None,
) -> None:
    """The reach a write of one of the caller's own accounts of the agent asks
    for, refused in plain words when it cannot hold: a narrowing reach on an
    MCP that is not GitHub, a list on an account with none, a name the
    account (or the installation it acts as) does not reach. Only names the
    row does not hold yet are looked up (a narrowing change never waits on
    GitHub); ``held`` is the row (the same account and identity), None for a
    new one."""
    names = body.repositories or []
    if not _reach_supported(mcp_name):
        if names or body.read_only:
            raise HTTPException(400, _ONLY_GITHUB)
        return
    _refuse_unreadable(names)
    if not names:
        return
    meta = await asyncio.to_thread(credential_store.get_account_meta, user.sub, mcp_name, body.account_label)
    if not oauth_app_kinds.is_github_app_meta(meta):
        raise HTTPException(400, _NO_LIST)
    kept = {github_reach.normalize(n) for n in held.repositories} if held is not None else set()
    added = [n for n in names if github_reach.normalize(n) not in kept]
    if not added:
        return
    missing = await _unreached(user.sub, mcp_name, body.account_label, body.installation_id, added,
                               no_list=_NO_LIST)
    if missing:
        raise HTTPException(400, _NOT_REACHED.format(name=missing[0]))


@router.get("/v1/agents/{agent_name}/mcps/{mcp_name}/service-account-repositories")
async def get_agent_service_repositories(
    agent_name: str,
    mcp_name: str,
    account_label: str = Query(...),
    installation_id: str = Query(""),
    user: UserContext = Depends(get_current_user),
):
    """The repositories the agent's GitHub reach may name, for one of the
    caller's own GitHub-App accounts (an installation of it, or the account
    itself): ``{pickable, installations: [{id, account, repositories,
    truncated}], from_meta}``. A token or OAuth account has no list
    (``pickable: false``). Manager of the agent only."""
    if require_auth(user).is_api_key:
        raise HTTPException(403, "Dashboard only")
    if not user.can_manage_agent(agent_name):
        raise HTTPException(403, f"Manager role on agent {agent_name!r} required")
    if not await asyncio.to_thread(account_context.holds_own_accounts, agent_name):
        raise HTTPException(409, account_context.PERSONAL_ONLY)
    if not _reach_supported(mcp_name):
        raise HTTPException(400, _ONLY_GITHUB_LIST)
    return await _repositories_answer(user.sub, mcp_name, account_label, installation_id)


def _own_reach_to_copy(
    body: service_account_set_api.ServiceAccountRequest, mcp_name: str, agent_name: str, user_sub: str,
) -> credential_store.AccountAgentBinding | None:
    """The caller's own reach for the agent a NEW row of the agent's set
    starts from: an account the set holds no row of (a row whose owner
    stopped managing the agent still holds a reach of its own), a body
    naming no reach, the account itself (an installation is another
    identity, narrowed by GitHub to its own account's repositories), and the
    row of that account in the caller's own set for the agent, narrowing (a
    non-default account's own reach, never the default's). A copy, never a
    link: each reach is changed where it lives. No lookup: the names are the
    same account's, looked up when the person chose them."""
    if body.repositories is not None or body.read_only is not None or body.installation_id:
        return None
    if not _reach_supported(mcp_name):
        return None
    if any(r.account_label == body.account_label for r in service_account_set_store.list_service_agent_set(
            mcp_name, agent_name, standing_only=False)):
        return None
    own = credential_store.get_account_agent_binding(user_sub, mcp_name, agent_name, body.account_label)
    if own is None:
        return None
    return own if github_reach.reach_of(own.repositories, own.read_only).narrows else None


# --- Admin: Infrastructure Credentials ---

@router.get("/v1/admin/integrations")
async def list_admin_integrations(
    user: UserContext = Depends(get_current_user),
):
    """List all infrastructure MCPs with their configured-key status, plus
    the email-server config block.
    """
    require_admin(user)

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
    require_admin(user)

    cred_info = _get_cred_schema(mcp_name)
    is_infra = cred_info and cred_info.get("type") == "infra"
    if not is_infra and not _is_app_credential(mcp_name):
        raise HTTPException(400, f"'{mcp_name}' is not an infrastructure MCP")

    credentials = dict(body.credentials)
    github_app_provider = ""
    if not is_infra:
        credentials, github_app_provider = await asyncio.to_thread(_verified_app_bundle, mcp_name, credentials)
    await asyncio.to_thread(
        credential_store.set_infra_credentials, mcp_name, credentials
    )
    # oauth_engine reads app credentials per-request via
    # credential_store.get_infra_credentials — no in-memory cache to
    # invalidate.
    if github_app_provider:
        # Only once the new key is stored: a worker tick between the mark
        # and the save would re-mint with the old key.
        from auth.oauth_providers import github_app
        github_app.forget_jwts()
        await asyncio.to_thread(github_apps.mark_installation_tokens_for_remint, github_app_provider)

    return {"status": "ok", "mcp_name": mcp_name}


def _verified_app_bundle(bundle: str, posted: dict[str, str]) -> tuple[dict[str, str], str]:
    """An admin's app bundle as it is about to be stored, and the provider
    whose installation token files are to be marked for a re-mint after
    the save (``""`` for another kind): when the merged bundle's kind is a
    GitHub App, GitHub must answer the app's own request with the entered
    App ID (the key normalized, the slug and the name filled; 400 with the
    sentence otherwise)."""
    from auth.oauth_providers import github_app
    manifest = next(
        (m for m in mcp_registry.get_all_manifests().values()
         if (m.credentials.oauth or {}).get("app_credential") == bundle),
        None,
    )
    oauth = (manifest.credentials.oauth or {}) if manifest else {}
    merged = {**credential_store.get_infra_credentials(bundle), **posted}
    kind = oauth_app_kinds.active_kind(oauth, merged)
    if kind != oauth_app_kinds.KIND_GITHUB_APP:
        return posted, ""
    try:
        verified = github_app.verify_app_credentials(merged, oauth_app_kinds.kind_fields(oauth, kind))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = {k: verified[k] for k in posted if k in verified}
    out[github_app.APP_SLUG_KEY] = verified[github_app.APP_SLUG_KEY]
    out[github_app.APP_NAME_KEY] = verified[github_app.APP_NAME_KEY]
    return out, str(oauth.get("provider_id") or "")


@router.delete("/v1/admin/integrations/infra/{mcp_name}")
async def delete_infra_integration(
    mcp_name: str,
    user: UserContext = Depends(get_current_user),
):
    """Remove infrastructure MCP credentials."""
    require_admin(user)

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
    require_admin(user)

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


# --- The OAuth clients the install registered at vendors (admin) ---------
#
# A manifest whose MCP server names its own authorization server
# (``credentials.oauth.authorization_server``) makes the install register
# itself there once per callback URL (services/oauth/mcp_authorization.py).
# Admins see those registrations and can forget one: the row is revoked,
# never deleted (token files point at it by id), and the next connect
# registers afresh. A person at the keyboard only: an agent's session token
# in an admin's chat must not forget the install's registrations.


def _registration_mcps(issuer: str, resources: str = "") -> list[str]:
    """The installed MCPs whose authorization server is ``issuer``: by the
    MCP server URLs recorded on the row when a connect started through it
    (``resources``, which outlive a restart), by the cached discovery of
    their MCP server, by the issuer the manifest declares, or, for a
    manifest that declares none, by an issuer host equal to the MCP
    server's host."""
    from urllib.parse import urlsplit

    from services.oauth import mcp_authorization

    issuer_host = (urlsplit(issuer).hostname or "").lower()
    discovered = set(mcp_authorization.resources_of_issuer(issuer)) | set(resources.split())
    names: list[str] = []
    for m in mcp_registry.get_all_manifests().values():
        oauth = (m.credentials.oauth or {}) if m.credentials else {}
        block = oauth.get("authorization_server")
        if not isinstance(block, dict):
            continue
        resource = mcp_authorization.canonical_resource(m.server.url_template or "")
        declared = str(block.get("issuer") or "").rstrip("/")
        if (
            resource in discovered
            or (declared and declared == issuer.rstrip("/"))
            or (not declared and (urlsplit(resource).hostname or "").lower() == issuer_host)
        ):
            names.append(m.name)
    return sorted(names)


@router.get("/v1/admin/oauth-client-registrations")
async def list_client_registrations(
    user: UserContext = Depends(get_current_user),
):
    """Every registration, live rows first, without the encrypted columns."""
    require_admin(user)
    from storage.identity import oauth_client_registrations as regs
    from storage.pg import run_db
    rows = await run_db(regs.list_all)
    for row in rows:
        row["mcps"] = _registration_mcps(row["issuer"], row.get("resources") or "")
    return {"registrations": rows}


@router.post("/v1/admin/oauth-client-registrations/{row_id}/forget")
async def forget_client_registration(
    row_id: int,
    user: UserContext = Depends(get_current_user),
):
    """Revoke a live registration: connected accounts keep their tokens
    (a refresh needs no registration for a public client), the next connect
    registers the install afresh."""
    admin = require_admin(user)
    from storage.identity import oauth_client_registrations as regs
    from storage.pg import run_db
    ok = await run_db(regs.revoke, row_id, f"forgotten by {admin.sub[:8]}")
    if not ok:
        raise HTTPException(404, "No live registration with that id")
    return {"status": "ok"}

