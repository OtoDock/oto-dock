"""What the app webhook's lifecycle events do to the platform's rows: the
installations a GitHub-App account keeps in its meta, the bindings and
subscriptions that name an installation, the token files. Synchronous
(the dispatcher runs it in a thread: the confirmations call GitHub).

GitHub's signature carries no timestamp, so a captured delivery replays:
the events that change standing (``created``, ``deleted``, ``suspend``,
``unsuspend``) are confirmed against ``GET /app/installations/{id}``
before any row changes (the admin's own app). A forwarded delivery
(``confirm=False``, OtoDock's hosted app: the relay holds no key to
confirm with) is never applied from its body: the accounts it touches
re-list with their own tokens instead, and a revoke marks only a token
GitHub now refuses. The sender is matched by id, never by login (logins
are renamed and reclaimed).
"""

from __future__ import annotations

import logging
from typing import Any

from auth.oauth_providers import github_app
from auth.oauth_providers.base import INVALID_GRANT, OAuthTokenError
from services.mcp import mcp_registry
from services.oauth import github_apps, oauth_account_store
from storage import database as task_store
from storage.automation import webhook_subscription_store
from storage.identity import credential_store, oauth_app_kinds as kinds

logger = logging.getLogger("otodock.github-app-lifecycle")

EVENT_INSTALLATION = "installation"
EVENT_REPOSITORIES = "installation_repositories"
EVENT_TARGET = "installation_target"
EVENT_AUTHORIZATION = "github_app_authorization"

IGNORED = "ignored"
UNCONFIRMED = "unconfirmed"
APPLIED = "applied"


def _obj(body: dict, key: str) -> dict[str, Any]:
    value = body.get(key)
    return value if isinstance(value, dict) else {}


def _names(rows: Any) -> list[str]:
    return [str(r.get("full_name") or "") for r in rows if isinstance(r, dict) and r.get("full_name")] \
        if isinstance(rows, list) else []


def _confirm(mcp_names: list[str], installation_id: str, action: str) -> bool:
    """Whether GitHub holds the installation in the state the event claims."""
    if not mcp_names:
        return False
    try:
        in_use = github_apps.app_in_use(mcp_names[0])
    except Exception:  # noqa: BLE001  # the relay or the bundle unreadable: unconfirmed, the event is dropped
        logger.warning("github app lifecycle: the app in use could not be read; %s dropped", action)
        return False
    if in_use.kind != kinds.KIND_GITHUB_APP or in_use.app is None:
        return False
    try:
        live = github_app.get_installation(in_use.app, int(installation_id))
    except (OAuthTokenError, ValueError) as exc:
        logger.warning("github app lifecycle: installation %s not confirmed (%s)", installation_id, exc)
        return False
    if action == "deleted":
        return live is None
    if live is None:
        return False
    if action == "suspend":
        return bool(live.get("suspended_at"))
    return not live.get("suspended_at")


def _update_holders(mcp_names: list[str], installation_id: str, change) -> int:
    """``change(record) -> record | None`` on every account's record of the
    installation (``None`` drops it). Returns the accounts touched."""
    touched = 0
    for account in credential_store.accounts_holding_installation(mcp_names, installation_id):
        def rewrite(meta: dict) -> dict:
            rows = []
            for row in kinds.installations_of(meta):
                if str(row.get("id") or "") == installation_id:
                    new = change(dict(row))
                    if new is None:
                        continue
                    rows.append(new)
                else:
                    rows.append(row)
            return {**meta, "installations": rows}
        credential_store.update_account_meta(
            account["user_sub"], account["mcp_name"], account["account_label"], rewrite,
        )
        touched += 1
    return touched


def _attach(mcp_names: list[str], body: dict, installation_id: str) -> int:
    """``installation.created``: the sender installed it; every GitHub-App
    account of that person (by GitHub id) lists it. Owned only when the
    target is the sender's own account: a repository admin who is no
    owner can install on an organization, so an organization's owner
    standing comes from a listing with the person's own token alone (the
    setup URL, Refresh, the re-check before a mint)."""
    sender_id = int(_obj(body, "sender").get("id") or 0)
    inst = _obj(body, "installation")
    if not sender_id or not inst:
        return 0
    account_id = int(_obj(inst, "account").get("id") or 0)
    own = str(inst.get("target_type") or "") != "Organization" and account_id == sender_id
    record = github_app.installation_record(inst, _names(body.get("repositories")), admin=own)
    touched = 0
    for account in credential_store.accounts_by_github_user(mcp_names, sender_id):
        def rewrite(meta: dict) -> dict:
            rows = [r for r in kinds.installations_of(meta) if str(r.get("id") or "") != installation_id]
            return {**meta, "installations": [*rows, record]}
        credential_store.update_account_meta(
            account["user_sub"], account["mcp_name"], account["account_label"], rewrite,
        )
        touched += 1
    return touched


def _detach(provider_id: str, mcp_names: list[str], installation_id: str) -> None:
    """``installation.deleted``: gone from every meta, its bindings and
    their subscriptions, every subscription targeting it, its token file."""
    _update_holders(mcp_names, installation_id, lambda row: None)
    for binding in credential_store.bindings_naming_installation(mcp_names, installation_id):
        credential_store.remove_service_agent_binding(
            binding["mcp_name"], binding["agent_name"], installation_id=installation_id,
        )
    for row in webhook_subscription_store.list_subscriptions(
        provider_id=provider_id, vendor_target=installation_id,
        target_kind=kinds.INSTALLATION_TARGET_KIND,
    ):
        webhook_subscription_store.delete_subscription(row["id"])
    for path in github_apps.installation_files(provider_id, installation_id):
        path.unlink(missing_ok=True)


# A forwarded revoke judges a token only while it has this much life left:
# an expired one fails at GitHub whatever the person did, and is left to the
# refresh worker.
_JUDGE_MIN_LIFE_S = 60.0


def _revoke_user_tokens(provider_id: str, mcp_names: list[str], body: dict, *, confirm: bool) -> int:
    """``github_app_authorization.revoked``: the person took the app's
    authorization back; their GitHub-App token files are dead (the card
    says reconnect, the sessions leave the account out). Each delivery
    touches the accounts of its own app only: the admin's app's (confirmed)
    the self-managed ones, the relay's forward the hosted ones. Forwarded
    (``confirm=False``), each account first asks GitHub with its own token
    and only a token GitHub refuses is marked: the relay's revoke of one
    install's token for a disconnect must not end another install's account
    of the same person. An account GitHub still accepts re-lists at the
    relay, which dropped the person everywhere at the revoke."""
    sender_id = int(_obj(body, "sender").get("id") or 0)
    if not sender_id:
        return 0
    marked = 0
    for account in credential_store.accounts_by_github_user(mcp_names, sender_id):
        username = task_store.get_username_by_sub(account["user_sub"])
        if not username:
            continue
        token_dir = oauth_account_store.get_token_dir(username, provider_id=provider_id)
        raw = oauth_account_store.read_account_token(token_dir, account["account_label"])
        if not raw:
            continue
        if _hosted_account(account, raw) == confirm:
            # The admin's own app (a confirmed delivery) speaks for its own
            # accounts alone; the relay's forward for OtoDock's app alone.
            continue
        if not confirm and not _refused_by_github(account, raw):
            github_apps.relist_at_relay(account["user_sub"], account["mcp_name"], account["account_label"])
            continue
        extra = dict(raw.get("extra") or {})
        extra["refresh_failed"] = "revoked"
        extra["refresh_failed_at"] = oauth_account_store.now_iso()
        oauth_account_store.write_token_payload(
            oauth_account_store.account_token_path(token_dir, account["account_label"]),
            {**raw, "extra": extra},
        )
        marked += 1
    return marked


def _hosted_account(account: dict, raw: dict) -> bool:
    """Whether an account signed in through OtoDock's relay: its meta says
    so, or its token file does."""
    extra = raw.get("extra") or {}
    return kinds.is_hosted_app_meta(account.get("meta")) or (
        isinstance(extra, dict) and bool(extra.get("via_relay")))


def _refused_by_github(account: dict, raw: dict) -> bool:
    """Whether GitHub refuses the account's own live token now (a listing
    with it: 401). A token past its life, or a listing that could not be
    made, is not judged here."""
    if oauth_account_store.token_dead_reason(raw):
        return False
    remaining = github_apps._remaining_s(raw)
    if remaining is not None and remaining < _JUDGE_MIN_LIFE_S:
        return False
    try:
        github_apps.refresh_installations(account["user_sub"], account["mcp_name"], account["account_label"])
    except OAuthTokenError as exc:
        return exc.code == INVALID_GRANT
    except (LookupError, *github_apps.UNREACHABLE) as exc:
        logger.warning("github app lifecycle: a forwarded revoke was not checked for %s (%s)",
                       account["account_label"], exc)
    return False


def _relist_holders(mcp_names: list[str], installation_id: str, *, sender_id: int) -> list[dict]:
    """Every account whose meta lists the installation, and every
    GitHub-App account of the sender, re-listed with its own token (a
    failure logged and passed: the card's Refresh corrects). Returns the
    metas the listings that succeeded wrote."""
    rows = credential_store.accounts_holding_installation(mcp_names, installation_id)
    if sender_id:
        rows += credential_store.accounts_by_github_user(mcp_names, sender_id)
    seen: set[tuple[str, str, str]] = set()
    listed: list[dict] = []
    for a in rows:
        key = (a["user_sub"], a["mcp_name"], a["account_label"])
        if key in seen:
            continue
        seen.add(key)
        try:
            listed.append(github_apps.refresh_installations(*key))
        except (OAuthTokenError, LookupError, *github_apps.UNREACHABLE) as exc:
            logger.warning("github app lifecycle: an account of installation %s was not re-listed (%s)",
                           installation_id, exc)
    return listed


def apply(
    provider_id: str, manifest: Any, event_type: str, body: dict, *,
    installation_id: str, confirm: bool,
) -> str:
    """Apply one delivery's lifecycle meaning; returns what happened
    (``ignored``, ``unconfirmed``, ``applied``)."""
    mcp_names = [m.name for m in (mcp_registry.get_mcps_by_provider(provider_id) or [])]
    if manifest is not None and manifest.name not in mcp_names:
        mcp_names.append(manifest.name)
    action = str(body.get("action") or "")
    if event_type == EVENT_AUTHORIZATION:
        if action != "revoked":
            return IGNORED
        _revoke_user_tokens(provider_id, mcp_names, body, confirm=confirm)
        return APPLIED
    if not installation_id or not mcp_names:
        return IGNORED
    standing = (
        (event_type == EVENT_INSTALLATION
         and action in ("created", "deleted", "suspend", "unsuspend", "new_permissions_accepted"))
        or event_type in (EVENT_REPOSITORIES, EVENT_TARGET)
    )
    if standing and not confirm:
        # A forwarded delivery: the accounts it touches re-list with their
        # own tokens instead of trusting the body.
        listed = _relist_holders(mcp_names, installation_id, sender_id=int(_obj(body, "sender").get("id") or 0))
        if event_type == EVENT_INSTALLATION and action == "deleted" and not any(
            kinds.installation_in(meta, installation_id) is not None for meta in listed
        ):
            # GitHub signed the removal and no listing made now shows it: a
            # holder whose listing failed keeps no stale copy.
            _detach(provider_id, mcp_names, installation_id)
        return APPLIED
    if event_type == EVENT_INSTALLATION:
        if action in ("created", "deleted", "suspend", "unsuspend") and confirm \
                and not _confirm(mcp_names, installation_id, action):
            logger.warning("github app lifecycle: installation %s %s not confirmed by GitHub; dropped",
                           installation_id, action)
            return UNCONFIRMED
        if action == "created":
            _attach(mcp_names, body, installation_id)
        elif action == "deleted":
            _detach(provider_id, mcp_names, installation_id)
        elif action in ("suspend", "unsuspend"):
            stamp = ""
            if action == "suspend":
                stamp = str(_obj(body, "installation").get("suspended_at") or "") or oauth_account_store.now_iso()
            _update_holders(mcp_names, installation_id,
                            lambda row: {**row, "suspended_at": (row.get("suspended_at") or stamp) if action == "suspend" else None})
        elif action == "new_permissions_accepted":
            permissions = _obj(_obj(body, "installation"), "permissions")
            _update_holders(mcp_names, installation_id, lambda row: {**row, "permissions": permissions})
        else:
            return IGNORED
        return APPLIED
    if event_type == EVENT_REPOSITORIES:
        added = _names(body.get("repositories_added"))
        removed = set(_names(body.get("repositories_removed")))
        selection = str(body.get("repository_selection") or "")

        def change(row: dict) -> dict:
            names = [n for n in row.get("repositories") or [] if n not in removed]
            names += [n for n in added if n not in names]
            return {**row, "repositories": names,
                    "repository_selection": selection or row.get("repository_selection", "")}
        _update_holders(mcp_names, installation_id, change)
        return APPLIED
    if event_type == EVENT_TARGET:
        login = str(_obj(body, "account").get("login") or "")
        if not login:
            return IGNORED
        _update_holders(mcp_names, installation_id, lambda row: {**row, "account": login})
        return APPLIED
    return IGNORED
