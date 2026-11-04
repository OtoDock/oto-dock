"""A vendor account that goes for good: its token revoked at the vendor, its
token file deleted, its route at the event relay ended.

Two places end an account: the disconnect (``api/auth/oauth.py``) and a
person's deletion (``release_user_accounts``). A binding cleared, an agent
deleted or an installation detached end no account: a service binding only
lends a person's account, which stays theirs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from auth.oauth_providers import get_provider
from services.billing import relay_client
from services.mcp import mcp_registry
from services.oauth import mcp_authorization, oauth_account_store, oauth_engine
from storage import database as task_store
from storage.identity import credential_store, oauth_app_kinds
from storage.pg import run_db

logger = logging.getLogger("otodock.oauth")

# Never revoked: a personal access token is the person's own credential made
# outside OtoDock, an installation token belongs to the app.
_NOT_REVOKED_FLOWS = ("personal_access_token", oauth_app_kinds.INSTALLATION_FLOW)


def _login_held_elsewhere(provider_id: str, own: Path | None, account_id: str) -> bool:
    """Whether another token file of the provider on this install carries the
    same login (a shared mailbox, a page admin's login, a person's second
    label): a vendor revoke ends the grant behind it, theirs too."""
    for path in oauth_account_store.other_account_files(provider_id, own):
        if str((oauth_account_store._read_oauth_token(path) or {}).get("account_id") or "") == account_id:
            return True
    return False


async def revoke_account(provider_id: str, token_data: dict, manifest: Any,
                         *, path: Path | None = None) -> bool:
    """Revoke the account's token at the vendor: the refresh token, else the
    access token (Slack without token rotation, Notion). A hosted account
    through the relay, which holds the client secret; one connected through
    the MCP server's own authorization server at that server; else with the
    client that issued it. Never a personal access token or an installation
    token, and never a login another token file here still uses (``path`` is
    the file being released). Whether the vendor took it. Best effort: never
    raises."""
    raw_extra = token_data.get("extra")
    extra: dict = raw_extra if isinstance(raw_extra, dict) else {}
    if extra.get("flow") in _NOT_REVOKED_FLOWS:
        return False
    token = str(token_data.get("refresh_token") or token_data.get("token")
                or token_data.get("access_token") or "")
    if not token:
        return False
    # A GitHub App user token's revoke ends that token alone (the relay's
    # unbind of the person is followed by the other accounts' re-list).
    account_id = str(token_data.get("account_id") or "")
    if account_id and extra.get("app_kind") != oauth_app_kinds.KIND_GITHUB_APP \
            and await asyncio.to_thread(_login_held_elsewhere, provider_id, path, account_id):
        logger.info("Revoke skipped for a %s account: another account here uses the same login", provider_id)
        return False
    try:
        if extra.get("via_relay"):
            await relay_client.oauth_revoke(provider_id=provider_id, token=token)
            return True
        if extra.get("flow") == oauth_account_store.MCP_AUTHORIZATION_FLOW:
            await _revoke_registered_client(token_data, token)
            return True
        # The client that issued the token is in the file; the bundle's
        # active kind is the fallback for an older file (the admin may have
        # switched kinds since).
        oauth_block = (manifest.credentials.oauth or {}) if manifest is not None and manifest.credentials else {}
        app_cred = oauth_block.get("app_credential", "")
        creds = await asyncio.to_thread(credential_store.get_infra_credentials, app_cred) if app_cred else {}
        client_id, client_secret = oauth_engine._resolve_app_credentials(oauth_block, creds)
        return bool(await get_provider(provider_id).revoke(
            token=token,
            client_id=str(token_data.get("client_id") or client_id),
            client_secret=str(token_data.get("client_secret") or client_secret),
        ))
    except Exception as e:  # noqa: BLE001  # the vendor revoke may fail, the local delete must still run
        logger.warning("Best-effort revoke failed for %s: %s", provider_id, e)
        return False


async def _revoke_registered_client(token_data: dict, token: str) -> None:
    """Revoke a token the MCP server's own authorization server issued to
    the client this install registered: at the endpoint the file names, on
    the issuer's origin, with the registration's secret when it has one."""
    from storage.identity import oauth_client_registrations as regs

    extra = token_data.get("extra") or {}
    method = str(extra.get("token_endpoint_auth_method") or "none")
    secret = ""
    if method != "none":
        reg_id = int(extra.get("registration_id") or 0)
        row = await run_db(regs.get, reg_id)
        if row is not None and row.get("issuer") == str(extra.get("issuer") or ""):
            secret = await run_db(regs.client_secret, reg_id) or ""
    await mcp_authorization.revoke(
        str(extra.get("revocation_endpoint") or ""),
        issuer=str(extra.get("issuer") or ""), token=token,
        method=method, client_id=str(token_data.get("client_id") or ""),
        client_secret=secret,
    )


async def release_user_accounts(user_sub: str) -> set[str]:
    """Every vendor account of a person being deleted, before the rows go:
    revoked, its token file deleted (nothing else removes it, and the refresh
    worker would keep refreshing it), its relay route ended. A GitHub-App
    account's other holders of the same GitHub person re-list, as at a
    disconnect. The providers of the hosted GitHub-App accounts it held: the
    caller registers them with the relay once the rows are gone."""
    from core.credentials import credential_locks
    from services.webhooks import relay_events

    username = await asyncio.to_thread(task_store.get_username_by_sub, user_sub)
    if not username:
        return set()
    seen: set[tuple[str, str]] = set()
    hosted_app_providers: set[str] = set()
    for account in await asyncio.to_thread(credential_store.list_all_user_accounts, user_sub):
        mcp_name, label = str(account.get("mcp_name") or ""), str(account.get("account_label") or "")
        manifest = mcp_registry.get_manifest(mcp_name)
        oauth_block = (manifest.credentials.oauth or {}) if manifest is not None and manifest.credentials else {}
        provider_id = str(oauth_block.get("provider_id") or "")
        if not provider_id or not label or (provider_id, label) in seen:
            continue
        seen.add((provider_id, label))
        try:
            token_dir = oauth_account_store.get_token_base_dir(provider_id, create=False) / username
            async with credential_locks.get_lock(user_sub, provider_id, label):
                token_data = await asyncio.to_thread(
                    oauth_account_store.read_account_token, token_dir, label) if token_dir.is_dir() else None
                if not token_data:
                    continue
                revoked = await revoke_account(
                    provider_id, token_data, manifest,
                    path=oauth_account_store.account_token_path(token_dir, label))
                await asyncio.to_thread(oauth_account_store.delete_account_token, token_dir, label)
            await relay_events.unbind_account(provider_id, token_data, revoked=revoked,
                                              leaving_user_sub=user_sub)
            meta = account.get("meta") or {}
            if oauth_app_kinds.is_github_app_meta(meta):
                if meta.get("hosted"):
                    hosted_app_providers.add(provider_id)
                if meta.get("user_id"):
                    await _relist_other_holders(provider_id, user_sub, int(meta["user_id"]))
        except Exception:
            logger.exception("Releasing the %s account %r of a deleted user failed (continuing)", provider_id, label)
    return hosted_app_providers


async def _relist_other_holders(provider_id: str, user_sub: str, github_user_id: int) -> None:
    """The relay unbinds a GitHub person for this whole install at the
    revoke: their accounts held by other people here re-list."""
    from services.oauth import github_apps
    names = [m.name for m in (mcp_registry.get_mcps_by_provider(provider_id) or [])]
    others = await asyncio.to_thread(credential_store.accounts_by_github_user, names, github_user_id)
    for a in others:
        if a["user_sub"] != user_sub:
            await asyncio.to_thread(github_apps.relist_at_relay, a["user_sub"], a["mcp_name"], a["account_label"])
