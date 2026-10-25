"""Credential Management REST API.

User-facing endpoints for per-user MCP credential management.
Admin-facing endpoints for infrastructure + service account credentials.
Credential schemas are read from MCP manifests via mcp_registry.
Auth: OAuth2 session cookie (dashboard users).
"""

import asyncio
import functools
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import github_reach
from auth.oauth_providers import github_app
from auth.providers import (
    UserContext,
    get_current_user,
    require_admin,
    require_auth,
    require_user,
)
from services.mcp import mcp_registry
from services.oauth import credential_resolver, github_apps, oauth_account_store
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import credential_store, oauth_app_kinds
from storage.mcp import mcp_store

if TYPE_CHECKING:
    from services.mcp.mcp_manifest_types import McpManifest

logger = logging.getLogger("otodock.credential-api")
router = APIRouter(dependencies=[Depends(require_user)])


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


class SetServiceBindingRequest(BaseModel):
    """Body for the agent → service-account binding endpoint.

    Binds the agent to the CALLER's own connected account (``account_label``)
    as its service identity — agent-scope sessions then read the caller's
    tokens. The owner is always the caller (derived server-side); the caller
    must have per-agent manager/admin role on the agent.
    """

    account_label: str
    # A GitHub App installation of that account as the identity; absent =
    # keep what the binding holds, "" = the account itself.
    installation_id: str | None = None
    # The agent's GitHub reach (``auth/github_reach``): the repositories it may
    # reach as ``owner/name`` ([] = every one) and whether it may only read;
    # absent = keep what the binding holds.
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
) -> tuple[list[str], str]:
    """The scopes recorded in an account's token file and the reason it
    needs a reconnect (``""`` when it does not), read in one pass.

    ``list_my_integrations`` computes ``missing_scopes`` from the scopes
    (the "Add additional access" affordance) and ``needs_reconnect`` from
    the reason (a grant the vendor ended, an expired token nobody can
    refresh, a file the manifest's way of issuing tokens no longer covers).
    A missing or corrupt file counts as no scopes granted, which surfaces
    every required scope as missing: the UI prompts a reconnect.
    """
    from services.oauth import credential_resolver, oauth_account_store
    manifest = mcp_registry.get_manifest(mcp_name)
    provider_id = (
        (manifest.credentials.oauth or {}).get("provider_id", "")
        if manifest else ""
    )
    if not provider_id:
        return [], ""
    username = task_store.get_username_by_sub(user_sub)
    if not username:
        return [], ""
    token_dir = oauth_account_store.get_token_dir(username, provider_id=provider_id)
    data = oauth_account_store.read_account_token(token_dir, account_label)
    if not data:
        return [], ""
    reason = credential_resolver.account_unusable_reason(data, manifest)
    raw = data.get("scopes", []) or []
    if isinstance(raw, str):
        # Some legacy formats stored as space-separated string.
        return [s for s in raw.split() if s], reason
    return list(raw), reason


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
    mcps_with_overridable_config: set[str] = set()
    manifest: McpManifest | None
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
        reaches_by_account: dict[str, list[dict]] = {}
        for b in agent_bindings:
            bindings_by_account.setdefault(b["account_label"], []).append(
                b["agent_name"]
            )
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
        # Agents whose service identity is one of THIS user's accounts: the
        # Connected Accounts card offers "Subscribe as <agent>" from them,
        # so the option never rests on a client-side role guess.
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
            reconnect_reason = ""
            if cred_info and cred_info.get("oauth"):
                required_scopes = await asyncio.to_thread(
                    mcp_registry.build_oauth_scopes, mcp_name, connected_services,
                )
                granted, reconnect_reason = await asyncio.to_thread(
                    _account_token_status,
                    user.sub, mcp_name, acc["account_label"],
                )
                missing_scopes = [s for s in required_scopes if s not in granted]

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
                "agent_overrides": sorted(
                    bindings_by_account.get(acc["account_label"], [])
                ),
                # The person's own GitHub reach for each agent of
                # ``agent_overrides`` (``[]`` and false: everything).
                "agent_reaches": reaches_by_account.get(acc["account_label"], []),
                "missing_scopes": missing_scopes,
                "service_bindings": service_bindings,
                # The card says "Reconnect needed" with the reason; the
                # resolver leaves such an account out of new sessions.
                "needs_reconnect": bool(reconnect_reason),
                "reconnect_reason": reconnect_reason,
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

        # A Shared-only agent's chats resolve as the agent (its own service
        # binding and its reach): a person's choice for it never applies, so
        # the card offers no repositories for it.
        shared_only_agents: list[str] = []
        if _reach_supported(mcp_name):
            from core.session.visibility import is_shared_only
            shared_only_agents = [a for a in candidate_agents if await asyncio.to_thread(is_shared_only, a)]

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
            "shared_only_agents": shared_only_agents,
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
    if require_auth(user).is_api_key:
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
    accounts = await asyncio.to_thread(credential_store.list_user_accounts, user.sub, mcp_name)
    if not any(a["account_label"] == body.account_label for a in accounts):
        raise HTTPException(
            404, f"Account '{body.account_label}' not found for {mcp_name}",
        )
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
    )
    return {
        "status": "ok",
        "repositories": list(row.repositories) if row else [],
        "read_only": row.read_only if row else False,
    }


_NO_OWN_LIST = (
    "This account has no list of repositories to pick from. You can still make an "
    "agent read only."
)


async def _check_own_reach(body: SetAgentBindingRequest, mcp_name: str, user: UserContext) -> None:
    """The person's own reach for an agent, refused in plain words when it
    cannot hold, as the service PUT refuses one: a narrowing on GitHub only,
    a list only on a GitHub-App account, a name the account does not reach.
    Only names the choice does not hold yet are looked up; a list kept onto
    another account is looked up on it (a token account keeps it as it is:
    it only narrows)."""
    names = body.repositories or []
    if not _reach_supported(mcp_name):
        if names or body.read_only:
            raise HTTPException(400, _ONLY_GITHUB)
        return
    _refuse_unreadable(names)
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
    """Drop the per-agent override (agent reverts to user's default account).

    Same assigned-agents gate as ``set_agent_binding`` — admin role does
    NOT bypass; the binding is owned by the caller and can only be removed
    by the caller if they're assigned to the agent. Safe to call when no
    binding exists (idempotent).
    """
    if require_auth(user).is_api_key:
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
    if require_auth(user).is_api_key:
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
        bound_label, bound_owner = binding.account_label, binding.account_owner_sub
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
        bound_meta = (bound_row or {}).get("meta") or {}
        installation = oauth_app_kinds.installation_in(bound_meta, binding.installation_id) or {}
        current_binding = {
            "label": bound_label,
            "owner_sub": bound_owner,
            "owner_name": owner_name,
            "owner_email": owner_email or bound_display_email,
            "set_by": "",
            "set_at": "",
            "installation_id": binding.installation_id,
            "installation_account": str(installation.get("account") or ""),
            "repositories": list(binding.repositories),
            "read_only": binding.read_only,
            # A hosted installation binding left from before: what the card says.
            "problem": (
                credential_resolver.reconnect_wording(oauth_app_kinds.INSTALLATION_HOSTED)
                if binding.installation_id and (
                    oauth_app_kinds.is_hosted_app_meta(bound_meta)
                    or await asyncio.to_thread(github_apps.hosted, mcp_name))
                else ""
            ),
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
        "current_binding": current_binding,
        # The binding may carry a GitHub reach (a repository list, read only).
        "reach_supported": _reach_supported(mcp_name),
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
    body: SetServiceBindingRequest, mcp_name: str, agent_name: str, user: UserContext,
) -> None:
    """The reach a PUT asks for, refused in plain words when it cannot
    hold: a narrowing reach on an MCP that is not GitHub, a list on an
    account with none, a name the identity does not reach. Only names the
    same identity does not hold yet are looked up (a narrowing change never
    waits on GitHub); a new identity has the list it keeps looked up too
    (GitHub narrows an installation's token to names of its own account)."""
    names = body.repositories or []
    if not _reach_supported(mcp_name):
        if names or body.read_only:
            raise HTTPException(400, _ONLY_GITHUB)
        return
    _refuse_unreadable(names)
    prior = await asyncio.to_thread(credential_store.get_service_agent_binding, mcp_name, agent_name)
    same_account = prior is not None and (prior.account_owner_sub, prior.account_label) == (
        user.sub, body.account_label)
    installation = body.installation_id if body.installation_id is not None else (
        prior.installation_id if prior is not None and same_account else "")
    same_identity = prior is not None and same_account and prior.installation_id == installation
    kept = body.repositories is None
    if kept:
        names = [] if same_identity or prior is None else list(prior.repositories)
    if not names:
        return
    meta = await asyncio.to_thread(credential_store.get_account_meta, user.sub, mcp_name, body.account_label)
    if not oauth_app_kinds.is_github_app_meta(meta):
        if kept:
            return                          # a token account keeps the list as it is
        raise HTTPException(400, _NO_LIST)
    held = {github_reach.normalize(n) for n in prior.repositories} if prior and same_identity else set()
    added = [n for n in names if github_reach.normalize(n) not in held]
    if not added:
        return
    missing = await _unreached(user.sub, mcp_name, body.account_label, installation or "", added, no_list=_NO_LIST)
    if missing and kept:
        raise HTTPException(400, f"The agent's GitHub reach names {missing[0]}, which this choice does not "
                                 "reach. Change the repositories first, then choose it again.")
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
    if not _reach_supported(mcp_name):
        raise HTTPException(400, _ONLY_GITHUB_LIST)
    return await _repositories_answer(user.sub, mcp_name, account_label, installation_id)


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
    if require_auth(user).is_api_key:
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

    if body.installation_id:
        meta = await asyncio.to_thread(
            credential_store.get_account_meta, user.sub, mcp_name, body.account_label,
        )
        # OtoDock's app through the relay serves user tokens only.
        if oauth_app_kinds.is_hosted_app_meta(meta) or await asyncio.to_thread(github_apps.hosted, mcp_name):
            raise HTTPException(400, "Hosted sign-in has no app identity. Bind the account itself.")
        # An installation stands only while the caller owns its target.
        standing = oauth_app_kinds.installation_standing(meta, body.installation_id)
        if standing:
            raise HTTPException(
                404, f"Installation {body.installation_id} cannot be this agent's identity: "
                f"{credential_resolver.bind_wording(standing)}",
            )
    await _check_reach(body, mcp_name, agent_name, user)
    repositories, read_only = body.repositories, body.read_only
    own = await asyncio.to_thread(_own_reach_to_copy, body, mcp_name, agent_name, user.sub)
    if own is not None:
        repositories, read_only = list(own.repositories), own.read_only
    prior = await asyncio.to_thread(
        credential_store.get_service_agent_binding, mcp_name, agent_name,
    )
    ok = await asyncio.to_thread(
        functools.partial(
            credential_store.set_service_agent_binding,
            mcp_name, agent_name,
            account_label=body.account_label,
            owner_sub=user.sub,
            set_by=user.sub,
            installation_id=body.installation_id,
            repositories=repositories,
            read_only=read_only,
        ),
    )
    if not ok:
        raise HTTPException(
            404,
            f"Account '{body.account_label}' not found among your connected "
            f"{mcp_name} accounts",
        )
    binding = await asyncio.to_thread(
        credential_store.get_service_agent_binding, mcp_name, agent_name,
    )
    answer = {
        "status": "ok",
        "agent_name": agent_name,
        "mcp_name": mcp_name,
        "account_label": body.account_label,
        "owner_sub": user.sub,
        "installation_id": binding.installation_id if binding else "",
        "repositories": list(binding.repositories) if binding else [],
        "read_only": binding.read_only if binding else False,
        # The binding started from the caller's own reach for the agent.
        "reach_copied": own is not None,
    }
    provider_id = str(((mcp_registry.get_manifest(mcp_name) or _NO_MANIFEST).credentials.oauth or {}).get("provider_id") or "")
    if prior is not None and binding is not None and prior.installation_id != binding.installation_id:
        # The agent's installation subscriptions followed the old identity.
        await _drop_other_installation_subscriptions(
            mcp_name, agent_name, keep=binding.installation_id,
        )
    if prior is not None and prior.installation_id and (
            binding is None or (prior.installation_id, github_apps.binding_reach_of(prior).digest())
            != (binding.installation_id, github_apps.binding_reach_of(binding).digest())):
        # The file the old identity or reach was served from goes with it.
        await asyncio.to_thread(github_apps.reap_if_orphan, provider_id, prior.installation_id)
    if binding is not None and binding.installation_id:
        # The first token now, so the first session does not wait on GitHub.
        try:
            reach = github_apps.binding_reach_of(binding)
            await asyncio.to_thread(
                functools.partial(
                    github_apps.ensure_installation_token,
                    mcp_name, user.sub, body.account_label, binding.installation_id,
                    reach=reach if reach.narrows else None,
                ),
            )
        except (credential_resolver.AccountNeedsReconnect, credential_resolver.CredentialUnavailable) as why:
            answer["warning"] = (
                credential_resolver.reconnect_wording(str(why))
                if isinstance(why, credential_resolver.AccountNeedsReconnect) else str(why)
            )
    return answer


_NO_MANIFEST = SimpleNamespace(credentials=SimpleNamespace(oauth={}))


def _own_reach_to_copy(
    body: SetServiceBindingRequest, mcp_name: str, agent_name: str, user_sub: str,
) -> credential_store.AccountAgentBinding | None:
    """The caller's own reach for the agent a NEW service binding starts
    from: no binding row at all (a row whose owner stopped managing the
    agent still holds a reach of its own), a PUT naming no reach, the
    account itself (an installation is another identity, narrowed by GitHub
    to its own account's repositories), and the caller's own choice for the
    agent naming that account and narrowing. A copy, never a link: each
    reach is changed where it lives. No lookup: the names are the same
    account's, looked up when the person chose them."""
    if body.repositories is not None or body.read_only is not None or body.installation_id:
        return None
    if not _reach_supported(mcp_name):
        return None
    if credential_store.service_agent_binding_row_exists(mcp_name, agent_name):
        return None
    own = credential_store.get_account_agent_binding(user_sub, mcp_name, agent_name)
    if own is None or own.account_label != body.account_label:
        return None
    return own if github_reach.reach_of(own.repositories, own.read_only).narrows else None


async def _drop_other_installation_subscriptions(mcp_name: str, agent_name: str, *, keep: str) -> None:
    """The agent's service-scope subscriptions on another installation
    than its identity go: they would keep firing for the old one."""
    from services.webhooks import subscription_manager
    from storage.automation import webhook_subscription_store
    rows = await asyncio.to_thread(
        webhook_subscription_store.list_subscriptions, scope="service", agent=agent_name, mcp_name=mcp_name,
    )
    for row in rows:
        if row.get("target_kind") != oauth_app_kinds.INSTALLATION_TARGET_KIND:
            continue
        if keep and str(row.get("vendor_target") or "") == keep:
            continue
        try:
            await subscription_manager.delete_subscription(subscription_id=row["id"])
        except Exception:
            logger.exception("the installation subscription %s of %s was not removed", row.get("id"), agent_name)


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
    if require_auth(user).is_api_key:
        raise HTTPException(403, "Dashboard only")
    if not user.can_manage_agent(agent_name):
        raise HTTPException(
            403, f"Manager role on agent {agent_name!r} required",
        )
    # The agent's service-scope subscriptions resolve their vendor token
    # through this binding: unregister them while it still resolves, or a
    # later re-bind makes their delete read the new owner's dir at the old
    # label and the vendor hook leaks.
    binding = await asyncio.to_thread(
        credential_store.get_service_agent_binding, mcp_name, agent_name,
    )
    if binding is not None:
        bound_label, bound_owner = binding.account_label, binding.account_owner_sub
        try:
            from services.webhooks import subscription_manager
            await subscription_manager.cleanup_account_subscriptions(
                scope="service", owner=bound_owner, mcp_name=mcp_name,
                account_label=bound_label, agent=agent_name,
            )
        except Exception:
            logger.exception(
                "Subscription cleanup raised while clearing the %s binding of %s "
                "(continuing)", mcp_name, agent_name,
            )
    await asyncio.to_thread(
        credential_store.remove_service_agent_binding,
        mcp_name, agent_name,
    )
    if binding is not None and binding.installation_id:
        provider_id = str(((mcp_registry.get_manifest(mcp_name) or _NO_MANIFEST).credentials.oauth or {}).get("provider_id") or "")
        await asyncio.to_thread(github_apps.reap_if_orphan, provider_id, binding.installation_id)
    return {"status": "ok", "agent_name": agent_name, "mcp_name": mcp_name}


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


# --- OAuth Bearer-Allowlist (admin) ---


class AddBearerAllowlistRequest(BaseModel):
    provider_id: str
    host_pattern: str


@router.get("/v1/admin/oauth-bearer-allowlist")
async def list_bearer_allowlist(
    user: UserContext = Depends(get_current_user),
):
    """List approved (provider, host) pairs for OAuth bearer-token injection."""
    require_admin(user)
    from storage.identity import bearer_allowlist
    rows = await asyncio.to_thread(bearer_allowlist.list_allowed)
    return {"entries": rows}


@router.post("/v1/admin/oauth-bearer-allowlist")
async def add_bearer_allowlist(
    body: AddBearerAllowlistRequest,
    user: UserContext = Depends(get_current_user),
):
    """Approve a (provider, host_pattern) pair. Idempotent on conflict."""
    require_admin(user)
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
    """Revoke a (provider, host) pair. The gateway refuses the host at the
    next request (the table's generation moved), and a paired machine
    holding a pushed token for it is told now, not at the lease's end."""
    require_admin(user)
    from core.remote import mcp_gateway_push
    from storage.identity import bearer_allowlist
    ok = await asyncio.to_thread(bearer_allowlist.delete_allowed, row_id)
    if not ok:
        raise HTTPException(404, "Allowlist entry not found")
    mcp_gateway_push.tick_soon()
    return {"status": "ok"}


@router.post("/v1/admin/oauth-bearer-allowlist/restore-defaults")
async def restore_bearer_allowlist_defaults(
    user: UserContext = Depends(get_current_user),
):
    """Re-add any deleted vendor-official defaults. Idempotent — admin-added
    entries and existing defaults are untouched. Returns the refreshed list."""
    require_admin(user)
    from storage.identity import bearer_allowlist
    rows = await asyncio.to_thread(bearer_allowlist.restore_defaults)
    return {"entries": rows}


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

