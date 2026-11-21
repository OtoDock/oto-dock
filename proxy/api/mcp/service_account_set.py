"""An agent's own accounts of a tool as a set, written from Agent Settings,
MCPs, in every mode: add one of the caller's own accounts (on GitHub the
account itself or one of its installations, with the agent's reach on it),
change the reach of one of the caller's own, make one the default (the
account a run with no person and a chat that chose none run on), take one
out. A manager of the agent, from the dashboard, with the gates of
``credentials._service_gates``; ``api/mcp/credentials.py``'s router includes
this one (``proxy/app.py`` mounts only that one; ``credentials.py`` imports
this module, so the handlers import it in turn when they run).

Who uses which account is the resolver's (``credential_resolver.pick_account``):
a run with no person takes the default, a chat in the agent's space the
account it switched to (``services/remote/chat_reload.py``), a person's own
chat the person's own accounts. Each account keeps its own event
subscriptions; taking one out takes them with it
(``chat_reload.remove_service_account``, the one way out).
"""

from __future__ import annotations

import asyncio
import functools
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import github_reach
from auth.providers import UserContext, get_current_user, require_user
from storage import database as task_store
from storage.identity import credential_store, oauth_app_kinds, service_account_set

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_user)])

AGENTS_OWN = ("This agent's chats use its own accounts. A manager sets them in Agent "
              "Settings, MCPs.")
IDENTITY = ("This account is already one of this agent's. Remove it, then add it again the "
            "way you want.")
NOT_STANDING = "This account's owner no longer manages this agent, so it cannot be the default."
GONE = "This account is no longer one of this agent's."
STOPPED = "This installation no longer works for this agent. Remove it, then add the account again."
HELD = "This account is already one of this agent's. Change its repositories under it."
NOT_YOURS = "Only the person who added this account can change its repositories."
HOSTED = "Hosted sign-in has no app identity. Add the account itself."


class ServiceAccountRequest(BaseModel):
    account_label: str = Field(min_length=1, max_length=128)
    make_default: bool = False
    # A GitHub App installation of the account as the way it acts; "" = the
    # account itself.
    installation_id: str = Field(default="", max_length=64)
    # The agent's GitHub reach (``auth/github_reach``): the repositories it
    # may reach as ``owner/name`` ([] = every one) and whether it may only
    # read; absent = none named (a new GitHub row may start from the caller's
    # own reach for the agent).
    repositories: list[str] | None = Field(default=None, max_length=github_reach.MAX_REPOSITORIES)
    read_only: bool | None = None


class ServiceReachRequest(BaseModel):
    account_label: str = Field(min_length=1, max_length=128)
    # The way the row acts, as the page shows it: the reach is written only
    # while the row still acts so.
    installation_id: str = Field(default="", max_length=64)
    # Absent keeps what the row holds.
    repositories: list[str] | None = Field(default=None, max_length=github_reach.MAX_REPOSITORIES)
    read_only: bool | None = None


class ServiceDefaultRequest(BaseModel):
    account_label: str = Field(min_length=1, max_length=128)


def _tool_label(mcp_name: str) -> str:
    from services.mcp import mcp_registry
    manifest = mcp_registry.get_manifest(mcp_name)
    return str((manifest.label if manifest is not None else "") or mcp_name)


def _provider_id(mcp_name: str) -> str:
    from services.mcp import mcp_registry
    manifest = mcp_registry.get_manifest(mcp_name)
    oauth = (manifest.credentials.oauth or {}) if manifest is not None and manifest.credentials else {}
    return str(oauth.get("provider_id") or "")


def _not_in_the_set(mcp_name: str, account_label: str) -> str:
    return f"Account '{account_label}' is not one of this agent's {_tool_label(mcp_name)} accounts."


def _row_of(mcp_name: str, agent_name: str, account_label: str) -> credential_store.ServiceBinding | None:
    """The set's row of this label, whoever lent it and whether it stands."""
    return next((r for r in service_account_set.list_service_agent_set(
        mcp_name, agent_name, standing_only=False) if r.account_label == account_label), None)


def _rows(mcp_name: str, agent_name: str, caller_sub: str) -> list[dict]:
    """The set for the page: each account with whose it is, its address, the
    way it acts and its GitHub reach, the default first."""
    from services.oauth import credential_resolver, github_apps
    out = []
    owners: dict[str, dict] = {}
    accounts: dict[tuple[str, str], dict] = {}
    hosted = github_apps.hosted(mcp_name)
    for row in service_account_set.list_service_agent_set(mcp_name, agent_name, standing_only=False):
        owner = row.account_owner_sub
        if owner not in owners:
            owners[owner] = task_store.get_user(owner) or {}
            for acc in credential_store.list_user_accounts(owner, mcp_name):
                accounts[(owner, acc["account_label"])] = acc
        person = owners[owner]
        account = accounts.get((owner, row.account_label), {})
        meta = account.get("meta") or {}
        installation = oauth_app_kinds.installation_in(meta, row.installation_id) or {}
        hosted_row = bool(row.installation_id) and (oauth_app_kinds.is_hosted_app_meta(meta) or hosted)
        # An installation suspended, gone or no longer its lender's: every
        # session on it fails, whoever looks at the page.
        stopped = (bool(row.installation_id) and not hosted_row
                   and bool(oauth_app_kinds.installation_standing(meta, row.installation_id)))
        out.append({
            "label": row.account_label,
            "display_email": account.get("display_email") or "",
            "owner_sub": owner,
            "owner_name": person.get("display_name") or person.get("name") or person.get("username") or "",
            "is_default": row.is_default,
            "stands": credential_store.service_binding_stands(row, agent_name),
            "mine": owner == caller_sub,
            "installation_id": row.installation_id,
            "installation_account": str(installation.get("account") or ""),
            "kind": oauth_app_kinds.KIND_GITHUB_APP if oauth_app_kinds.is_github_app_meta(meta) else "",
            "repositories": list(row.repositories),
            "read_only": row.read_only,
            # An installation row left from before hosted sign-in, or one that
            # stopped working: what to do.
            "problem": (credential_resolver.reconnect_wording(oauth_app_kinds.INSTALLATION_HOSTED)
                        if hosted_row else STOPPED if stopped else ""),
        })
    return out


async def set_answer(agent_name: str, mcp_name: str, caller_sub: str) -> dict:
    """The set's part of the options route's answer."""
    return {"accounts": await asyncio.to_thread(_rows, mcp_name, agent_name, caller_sub)}


async def accounts_in_use(mcp_name: str, agents: list[str]) -> dict[str, str]:
    """``{agent: the address or label of its default account}`` for each
    agent that has one: the account a Shared-only agent's chats start on."""
    out: dict[str, str] = {}
    for agent in agents:
        binding = await asyncio.to_thread(credential_store.get_service_agent_binding, mcp_name, agent)
        if binding is None:
            continue
        accounts = await asyncio.to_thread(
            credential_store.list_user_accounts, binding.account_owner_sub, mcp_name)
        row = next((a for a in accounts if a["account_label"] == binding.account_label), {})
        out[agent] = row.get("display_email") or binding.account_label
    return out


async def _default_moved(mcp_name: str, agent_name: str,
                         prior: credential_store.ServiceBinding | None) -> None:
    """The chats on the default are told when it moved to another account;
    nothing else goes (each account keeps its own subscriptions)."""
    from services.remote import chat_reload
    now = await asyncio.to_thread(credential_store.get_service_agent_binding, mcp_name, agent_name)
    if prior is not None and now is not None and (now.account_owner_sub, now.account_label) != (
            prior.account_owner_sub, prior.account_label):
        await chat_reload.default_moved(mcp_name, agent_name)


async def _first_token(mcp_name: str, owner_sub: str, row: credential_store.ServiceBinding) -> str:
    """An installation row's first token now, so its first session does not
    wait on GitHub: ``""``, or the warning when it could not be minted (the
    row stands, a session tries again)."""
    from services.oauth import credential_resolver, github_apps
    if not row.installation_id:
        return ""
    reach = github_apps.binding_reach_of(row)
    try:
        await asyncio.to_thread(functools.partial(
            github_apps.ensure_installation_token, mcp_name, owner_sub, row.account_label,
            row.installation_id, reach=reach if reach.narrows else None))
    except credential_resolver.AccountNeedsReconnect as why:
        return credential_resolver.reconnect_wording(str(why))
    except credential_resolver.CredentialUnavailable as why:
        return str(why)
    return ""


async def _answer(agent_name: str, mcp_name: str, caller_sub: str, **extra) -> dict:
    return {"status": "ok", **extra, **await set_answer(agent_name, mcp_name, caller_sub)}


@router.put("/v1/agents/{agent_name}/mcps/{mcp_name}/service-accounts")
async def add_service_account(
    agent_name: str, mcp_name: str, body: ServiceAccountRequest,
    user: UserContext = Depends(get_current_user),
):
    """Add one of the caller's own accounts to the agent's own set of the
    tool, as the account itself or one of its installations, with the
    agent's GitHub reach on it (the first is the default, ``make_default``
    makes it the default). An account the set already holds keeps what it
    holds. Answers the set as the options route does."""
    from api.mcp import credentials as credentials_api
    from services.oauth import credential_resolver, github_apps
    await credentials_api._service_gates(user, agent_name, mcp_name)
    if not user.sub:
        raise HTTPException(403, "A user session is required to add an account")
    held = await asyncio.to_thread(_row_of, mcp_name, agent_name, body.account_label)
    if held is not None and held.account_owner_sub == user.sub and (
            body.repositories is not None or body.read_only is not None):
        # The add never changes a held row's reach: the reach route does.
        raise HTTPException(409, HELD)
    # A label another lender holds now may be gone by the store's lock, and
    # the caller's row is then written new: judge it as a new row now.
    new_row = held is None or held.account_owner_sub != user.sub
    if new_row and body.installation_id:
        meta = await asyncio.to_thread(credential_store.get_account_meta, user.sub, mcp_name, body.account_label)
        # OtoDock's app through the relay serves user tokens only.
        if oauth_app_kinds.is_hosted_app_meta(meta) or await asyncio.to_thread(github_apps.hosted, mcp_name):
            raise HTTPException(400, HOSTED)
        # An installation stands only while the caller owns its target.
        standing = oauth_app_kinds.installation_standing(meta, body.installation_id)
        if standing:
            raise HTTPException(404, f"Installation {body.installation_id} cannot act for this agent: "
                                     f"{credential_resolver.bind_wording(standing)}")
    repositories, read_only = body.repositories, body.read_only
    own = None
    if new_row:
        await credentials_api._check_reach(body, mcp_name, user, held=None)
        own = await asyncio.to_thread(credentials_api._own_reach_to_copy, body, mcp_name, agent_name, user.sub)
        if own is not None:
            repositories, read_only = list(own.repositories), own.read_only
    prior = await asyncio.to_thread(credential_store.get_service_agent_binding, mcp_name, agent_name)
    why = await asyncio.to_thread(functools.partial(
        service_account_set.add_service_account_to_agent, mcp_name, agent_name,
        account_label=body.account_label, owner_sub=user.sub, set_by=user.sub,
        make_default=body.make_default, installation_id=body.installation_id,
        repositories=repositories or [], read_only=bool(read_only)))
    if why == service_account_set.MISSING:
        raise HTTPException(404, f"Account '{body.account_label}' not found among your connected "
                            f"{_tool_label(mcp_name)} accounts.")
    if why == service_account_set.TAKEN:
        raise HTTPException(409, (
            f"This agent already has an account named {body.account_label} for "
            f"{_tool_label(mcp_name)}. Connect yours under another name in Settings, "
            "Integrations, then add it."))
    if why == service_account_set.IDENTITY:
        raise HTTPException(409, IDENTITY)
    await _default_moved(mcp_name, agent_name, prior)
    warning = ""
    row = await asyncio.to_thread(_row_of, mcp_name, agent_name, body.account_label)
    if new_row and row is not None:
        warning = await _first_token(mcp_name, user.sub, row)
    return await _answer(agent_name, mcp_name, user.sub, reach_copied=own is not None,
                         **({"warning": warning} if warning else {}))


@router.put("/v1/agents/{agent_name}/mcps/{mcp_name}/service-accounts/reach")
async def set_service_reach(
    agent_name: str, mcp_name: str, body: ServiceReachRequest,
    user: UserContext = Depends(get_current_user),
):
    """Change the agent's GitHub reach on one of the caller's own accounts of
    the set, in place: the repositories it may reach and whether it may only
    read. Written only while the row still acts as the installation the
    page names, so a page left open after a removal never puts it back."""
    from api.mcp import credentials as credentials_api
    from services.oauth import github_apps
    await credentials_api._service_gates(user, agent_name, mcp_name)
    if not user.sub:
        raise HTTPException(403, "A user session is required to change an account")
    held = await asyncio.to_thread(_row_of, mcp_name, agent_name, body.account_label)
    if held is None:
        raise HTTPException(404, GONE)
    if held.account_owner_sub != user.sub:
        raise HTTPException(403, NOT_YOURS)
    if held.installation_id != body.installation_id:
        raise HTTPException(409, IDENTITY)
    await credentials_api._check_reach(body, mcp_name, user, held=held)
    # What the body leaves out is kept from the row as the store reads it
    # under the set's lock, never from this earlier read.
    why = await asyncio.to_thread(functools.partial(
        service_account_set.set_service_reach, mcp_name, agent_name, owner_sub=user.sub,
        account_label=body.account_label, installation_id=body.installation_id,
        repositories=body.repositories, read_only=body.read_only))
    if why == service_account_set.MISSING:
        raise HTTPException(404, GONE)
    if why == service_account_set.IDENTITY:
        raise HTTPException(409, IDENTITY)
    warning = ""
    row = await asyncio.to_thread(_row_of, mcp_name, agent_name, body.account_label)
    if row is not None and row.installation_id and (
            github_apps.binding_reach_of(row).digest() != github_apps.binding_reach_of(held).digest()):
        # The file the old reach was served from goes when no row uses it.
        await asyncio.to_thread(github_apps.reap_if_orphan, _provider_id(mcp_name), held.installation_id)
        warning = await _first_token(mcp_name, user.sub, row)
    return await _answer(agent_name, mcp_name, user.sub, **({"warning": warning} if warning else {}))


@router.put("/v1/agents/{agent_name}/mcps/{mcp_name}/service-accounts/default")
async def set_service_default(
    agent_name: str, mcp_name: str, body: ServiceDefaultRequest,
    user: UserContext = Depends(get_current_user),
):
    """Make one account of the agent's own set its default, whoever lent it.
    The chats on the default are told; every subscription stays."""
    from api.mcp import credentials as credentials_api
    await credentials_api._service_gates(user, agent_name, mcp_name)
    row = await asyncio.to_thread(_row_of, mcp_name, agent_name, body.account_label)
    if row is not None and not await asyncio.to_thread(
            credential_store.service_binding_stands, row, agent_name):
        raise HTTPException(409, NOT_STANDING)
    prior = await asyncio.to_thread(credential_store.get_service_agent_binding, mcp_name, agent_name)
    if row is None or not await asyncio.to_thread(
            service_account_set.set_service_default_account, mcp_name, agent_name, body.account_label):
        raise HTTPException(404, _not_in_the_set(mcp_name, body.account_label))
    await _default_moved(mcp_name, agent_name, prior)
    return await _answer(agent_name, mcp_name, user.sub or "")


@router.delete("/v1/agents/{agent_name}/mcps/{mcp_name}/service-accounts")
async def remove_service_account(
    agent_name: str, mcp_name: str, account_label: str = Query(..., min_length=1, max_length=128),
    user: UserContext = Depends(get_current_user),
):
    """Take one account out of the agent's own set, whoever lent it, through
    the one remover: its event subscriptions go first, the sessions built on
    it are closed and their chats told, the oldest standing account left
    becomes the default when it was, and its installation's token file goes
    once no row names it."""
    from api.mcp import credentials as credentials_api
    from services.remote import chat_reload
    await credentials_api._service_gates(user, agent_name, mcp_name)
    row = await asyncio.to_thread(_row_of, mcp_name, agent_name, account_label)
    if row is None:
        raise HTTPException(404, _not_in_the_set(mcp_name, account_label))
    await chat_reload.remove_service_account(
        mcp_name, agent_name, owner_sub=row.account_owner_sub, account_label=account_label)
    return await _answer(agent_name, mcp_name, user.sub or "")
