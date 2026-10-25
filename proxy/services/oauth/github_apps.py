"""GitHub Apps on the platform: the app an install uses for a provider (the
admin's bundle, or OtoDock's through the hosted relay, user tokens only),
what a GitHub-App account knows about its installations
(``user_credential_accounts.meta``), and the installation access tokens an
installation binding of the admin's app serves sessions with
(``sessions/<provider>-tokens/_app/<installation_id>.json``).

Synchronous: every function here reads the store and calls GitHub, so a
caller on the event loop runs it in a thread. The names are in
``storage/identity/oauth_app_kinds.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from auth import github_reach
from auth.github_reach import Reach
from auth.oauth_providers import github_app
from auth.oauth_providers.base import INVALID_GRANT, OAuthTokenError, TokenSet
from services.billing import relay_client
from services.mcp import mcp_registry
from services.oauth import oauth_account_store
from storage import database as task_store
from storage.identity import credential_store, oauth_app_kinds as kinds

logger = logging.getLogger("otodock.github-apps")

# An installation token file is reused while this much life remains; an
# app step's static token needs more (the step may run for a while).
# The owner's standing over an installation is re-confirmed with their own
# token at most this often (a stale meta, with the webhook off, corrects).
_RELIST_INTERVAL_S = 3600.0
_TRANSIENT = "GitHub could not be reached; try again in a minute"
# A narrowed mint whose installation GitHub did not describe, or whose
# read permissions GitHub refused (passing failures: no repository to
# re-pick), and GitHub's status for a mint naming what the installation lacks.
_PERMISSIONS_UNKNOWN = "permissions_unknown"
_PERMISSIONS_REFUSED = "permissions_refused"
_UNPROCESSABLE = 422
# GitHub's refusal of a repository a mint names reads so ("There is at least
# one repository that does not exist or is not accessible to the parent
# installation").
_REPOSITORY_WORD = "repositor"
# What a caller catches around the relay and GitHub: a passing failure.
UNREACHABLE = (relay_client.RelayNotConfigured, relay_client.RelayError, httpx.HTTPError, OSError)


def _reconnect_cls():
    from services.oauth.credential_resolver import AccountNeedsReconnect
    return AccountNeedsReconnect


def _unavailable_cls():
    from services.oauth.credential_resolver import CredentialUnavailable
    return CredentialUnavailable


@dataclass
class AppInUse:
    """The GitHub App a provider's accounts go through: the admin's
    (``app`` holds the key) or OtoDock's (``hosted``: through the relay,
    user tokens only, no installation identity)."""

    kind: str
    hosted: bool
    app_id: str = ""
    slug: str = ""
    app: github_app.GitHubApp | None = None


def hosted(mcp_name: str) -> bool:
    """Whether ``mcp_name`` signs in through OtoDock's relay right now: the
    admin's mode, read locally (no relay call)."""
    manifest = mcp_registry.get_manifest(mcp_name)
    return manifest is not None and relay_client.hosted_oauth_active(mcp_name, manifest)


def oauth_block_of(mcp_name: str) -> dict[str, Any]:
    manifest = mcp_registry.get_manifest(mcp_name)
    return (manifest.credentials.oauth or {}) if manifest else {}


def app_in_use(mcp_name: str) -> AppInUse:
    """The app ``mcp_name``'s accounts connect through right now: the kind
    the admin's bundle is in (self-managed), or the relay's app (hosted).
    The OAuth App kind with nothing else when no GitHub App is set."""
    manifest = mcp_registry.get_manifest(mcp_name)
    oauth = oauth_block_of(mcp_name)
    if manifest is not None and relay_client.hosted_oauth_active(mcp_name, manifest):
        info = relay_client.oauth_app_info(str(oauth.get("provider_id") or ""))
        kind = str(info.get("kind") or "")
        return AppInUse(
            kind=kind if kind in kinds.KINDS else kinds.KIND_OAUTH_APP, hosted=True,
            app_id=str(info.get("app_id") or ""), slug=str(info.get("slug") or ""),
        )
    bundle = str(oauth.get("app_credential") or "")
    creds = credential_store.get_infra_credentials(bundle) if bundle else {}
    kind = kinds.active_kind(oauth, creds)
    if kind != kinds.KIND_GITHUB_APP:
        return AppInUse(kind=kind, hosted=False)
    app = github_app.app_from_bundle(creds, kinds.kind_fields(oauth, kind))
    return AppInUse(kind=kind, hosted=False, app_id=app.app_id, slug=app.slug, app=app)


def install_url(slug: str, state: str) -> str:
    """GitHub's page to install the app, returning ``state`` to the setup URL."""
    return f"https://github.com/apps/{slug}/installations/new?state={state}"


def reach_label(record: dict) -> str:
    """What an installation reaches, as a person reads it: ``all
    repositories``, ``3 repositories``, ``200+ repositories`` (the listing
    stopped at its cap)."""
    if str(record.get("repository_selection") or "") == "all":
        return "all repositories"
    n = len(record.get("repositories") or [])
    if record.get("repositories_truncated"):
        return f"{n}+ repositories"
    return f"{n} {'repository' if n == 1 else 'repositories'}"


# ---------------------------------------------------------------------------
# The account meta
# ---------------------------------------------------------------------------

def _account_file(user_sub: str, mcp_name: str, account_label: str) -> dict:
    """The account's token file as stored, ``{}`` when there is none."""
    username = task_store.get_username_by_sub(user_sub)
    if not username:
        return {}
    oauth = oauth_block_of(mcp_name)
    provider_id = str(oauth.get("provider_id") or "")
    if not provider_id:
        return {}
    token_dir = oauth_account_store.get_token_dir(username, provider_id=provider_id)
    return oauth_account_store.read_account_token(token_dir, account_label) or {}


def _live_token(raw: dict) -> str:
    if not raw or oauth_account_store.token_dead_reason(raw):
        return ""
    return oauth_account_store.get_canonical_access_token(raw)


def account_user_token(user_sub: str, mcp_name: str, account_label: str) -> str:
    """The account's current user token, ``""`` when the file is gone or
    cannot serve (the resolver's reasons)."""
    return _live_token(_account_file(user_sub, mcp_name, account_label))


def refresh_installations(user_sub: str, mcp_name: str, account_label: str) -> dict:
    """Re-list the installations a GitHub-App account can reach with its
    own user token and rewrite the meta. Returns the meta as written.
    Raises ``OAuthTokenError`` (``invalid_grant``: the user token died;
    ``unreachable``) and ``LookupError`` for an account that is not a
    GitHub-App one."""
    meta = credential_store.get_account_meta(user_sub, mcp_name, account_label)
    if not kinds.is_github_app_meta(meta):
        raise LookupError("not a GitHub-App account")
    token = account_user_token(user_sub, mcp_name, account_label)
    if not token:
        raise OAuthTokenError(github_app.PROVIDER_ID, "list", INVALID_GRANT, "the account's token cannot serve")
    in_use = app_in_use(mcp_name)
    listed = github_app.user_installations(token, app_id=in_use.app_id)
    new_meta = {
        "kind": kinds.KIND_GITHUB_APP, "login": listed.login or meta.get("login", ""),
        "user_id": listed.user_id or meta.get("user_id", 0),
        "installations": listed.installations, "hosted": bool(meta.get("hosted")),
    }
    credential_store.set_account_meta(user_sub, mcp_name, account_label, new_meta)
    return new_meta


def record_account_kind(
    user_sub: str, mcp_name: str, account_label: str, *, app_kind: str, userinfo_raw: dict,
    hosted: bool = False,
) -> None:
    """After a connect: the meta says which kind issued the account's
    token, and ``hosted`` whether OtoDock's relay did (its installations'
    events then reach the install through the relay). A GitHub-App account
    keeps its installations across a reconnect of the same kind by the same
    GitHub user (another user's start from none: the listing that follows
    is theirs); any other kind clears the meta, so an installation binding
    on the account stops resolving instead of widening to the new token."""
    def change(current: dict) -> dict | None:
        if app_kind != kinds.KIND_GITHUB_APP:
            return {} if current else None
        user_id = int(userinfo_raw.get("id") or current.get("user_id") or 0)
        same_user = int(current.get("user_id") or 0) == user_id
        prior = kinds.installations_of(current) if kinds.is_github_app_meta(current) and same_user else []
        return {
            "kind": kinds.KIND_GITHUB_APP,
            "login": str(userinfo_raw.get("login") or current.get("login") or ""),
            "user_id": user_id,
            "installations": prior,
            "hosted": hosted,
        }
    credential_store.update_account_meta(user_sub, mcp_name, account_label, change)


def record_installations_after_connect(user_sub: str, mcp_name: str, account_label: str) -> int | None:
    """The listing right after a GitHub-App connect; returns how many
    installations it found. A failure keeps the account (its token is good)
    with no installations and returns ``None``; the card offers Refresh."""
    try:
        meta = refresh_installations(user_sub, mcp_name, account_label)
    except (OAuthTokenError, LookupError, *UNREACHABLE) as exc:
        logger.warning("github app: the installations of %s/%s were not listed after the connect (%s)",
                       mcp_name, account_label, exc)
        return None
    return len(kinds.installations_of(meta))


def relist_at_relay(user_sub: str, mcp_name: str, account_label: str) -> None:
    """A hosted account: ask the relay to re-read what this person owns (it
    forwards their installations' events by it). The setup page, the card's
    Refresh, a disconnect's other accounts and a forwarded revoke GitHub
    did not confirm call it; the hourly re-check does not (the relay
    re-lists at every token refresh itself, and caps an install at 20 calls
    a minute with the install page). Only a token the relay issued is sent
    (``extra.via_relay``): an account of an admin's own app, left from
    before the MCP turned hosted, never reaches OtoDock's relay, and its
    listing of the wrong app never unbinds the person there. Best-effort."""
    if not hosted(mcp_name):
        return
    raw = _account_file(user_sub, mcp_name, account_label)
    extra = raw.get("extra")
    if not isinstance(extra, dict) or not extra.get("via_relay"):
        return
    token = _live_token(raw)
    if not token:
        return
    try:
        relay_client.oauth_app_relist(
            provider_id=str(oauth_block_of(mcp_name).get("provider_id") or github_app.PROVIDER_ID),
            access_token=token,
        )
    except relay_client.RelayError as exc:
        # A person sees nothing: the page that asked already listed the
        # account itself, and the relay re-lists at the next token refresh.
        why = ("over the relay's limit a minute, shared with the install page"
               if exc.code == relay_client.RATE_LIMITED else exc.code)
        logger.info("github app: the relay did not re-list %s/%s (%s)", mcp_name, account_label, why)
    except (relay_client.RelayNotConfigured, httpx.HTTPError, OSError, ValueError) as exc:
        logger.info("github app: the relay did not re-list %s/%s (%s)", mcp_name, account_label, exc)


# ---------------------------------------------------------------------------
# What an agent's reach may name
# ---------------------------------------------------------------------------

_LISTED_S = 60.0
_listed: dict[tuple[str, str, str, str], tuple[float, list[dict]]] = {}


def reachable_repositories(
    user_sub: str, mcp_name: str, account_label: str, installation_id: str = "",
) -> tuple[list[dict], bool]:
    """The repositories an agent bound to a GitHub-App account may be
    narrowed to, by installation (``{id, account, repositories,
    truncated}``): an installation binding's installation alone, the
    account bound as itself every installation it reaches. Listed with the
    account's own token, memoised a minute; the second value says the lists
    came from the meta instead because GitHub could not be reached.
    ``LookupError`` for an account that is not a GitHub-App one."""
    meta = credential_store.get_account_meta(user_sub, mcp_name, account_label)
    if not kinds.is_github_app_meta(meta):
        raise LookupError("not a GitHub-App account")
    rows = [r for r in kinds.installations_of(meta)
            if not installation_id or str(r.get("id") or "") == str(installation_id)]
    key = (user_sub, mcp_name, account_label, str(installation_id))
    now = time.monotonic()
    hit = _listed.get(key)
    if hit is not None and now - hit[0] < _LISTED_S:
        return hit[1], False
    token = account_user_token(user_sub, mcp_name, account_label)
    out: list[dict] = []
    from_meta = False
    for row in rows:
        names: list[str] = []
        truncated = False
        try:
            if not token:
                raise OAuthTokenError(github_app.PROVIDER_ID, "list", INVALID_GRANT)
            names, _selection, truncated = github_app.list_installation_repositories(token, int(row.get("id") or 0))
        except (OAuthTokenError, ValueError, *UNREACHABLE):
            names = [str(n) for n in row.get("repositories") or []]
            truncated = bool(row.get("repositories_truncated")) or str(row.get("repository_selection") or "") == "all"
            from_meta = True
        out.append({"id": row.get("id"), "account": str(row.get("account") or ""),
                    "repositories": names, "truncated": truncated})
    if not from_meta:
        _listed[key] = (now, out)
    return out, from_meta


def unreachable_names(listed: list[dict], names: list[str]) -> list[str]:
    """The ``names`` no listed installation shows. A name on the account of
    an installation whose list was cut at its cap is taken (it may be past
    the cut)."""
    known = {github_reach.normalize(n) for inst in listed for n in inst["repositories"]}
    cut = {str(inst["account"]).lower() for inst in listed if inst.get("truncated")}
    out: list[str] = []
    for name in names:
        norm = github_reach.normalize(name)
        if norm in known or (norm and norm.split("/", 1)[0] in cut):
            continue
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# The installation token files
# ---------------------------------------------------------------------------

def app_token_dir(provider_id: str):
    """``sessions/<provider>-tokens/_app/``, the installation token files."""
    return oauth_account_store.get_token_dir(kinds.APP_TOKEN_DIR, provider_id=provider_id)


def mark_installation_tokens_for_remint(provider_id: str) -> int:
    """After an admin saved new app credentials: every installation token
    file of the provider expires now and loses its dead mark, so the
    worker's next tick re-mints it with the new key while every session
    keeps its file. Returns how many were marked."""
    if not provider_id:
        return 0
    marked = 0
    for path in app_token_dir(provider_id).glob("*.json"):
        raw = oauth_account_store.read_account_token(path.parent, path.stem)
        if not raw or str((raw.get("extra") or {}).get("flow") or "") != kinds.INSTALLATION_FLOW:
            continue
        extra = dict(raw.get("extra") or {})
        extra.pop("refresh_failed", None)
        extra.pop("refresh_failed_at", None)
        oauth_account_store.write_token_payload(path, {**raw, "expires_at": oauth_account_store.now_iso(), "extra": extra})
        marked += 1
    return marked


_mint_locks: dict[str, threading.Lock] = {}
_mint_locks_guard = threading.Lock()
_relisted: dict[tuple[str, str, str], float] = {}


def _lock_for(installation_id: str) -> threading.Lock:
    with _mint_locks_guard:
        lock = _mint_locks.get(installation_id)
        if lock is None:
            lock = _mint_locks[installation_id] = threading.Lock()
        return lock


def installation_file(provider_id: str, installation_id: str | int, reach: Reach | None = None) -> Path:
    """An installation's token file: ``<id>.json``, or ``<id>-<digest>.json``
    for a binding whose reach narrows it (GitHub narrows that token to the
    reach, so two agents with different reaches never share one)."""
    stem = str(int(installation_id))
    if reach is not None and reach.narrows:
        stem = f"{stem}-{reach.digest()}"
    return oauth_account_store.account_token_path(app_token_dir(provider_id), stem)


def installation_files(provider_id: str, installation_id: str | int) -> list[Path]:
    """Every token file of an installation, narrowed or not."""
    iid = str(int(installation_id))
    return [p for p in app_token_dir(provider_id).glob(f"{iid}*.json")
            if p.stem == iid or p.stem.startswith(f"{iid}-")]


def binding_reach_of(row: dict | Any) -> Reach:
    """The reach of a binding row (``bindings_naming_installation``) or a
    ``ServiceBinding``."""
    if isinstance(row, dict):
        return github_reach.reach_of(github_reach.parse_stored(row.get("repositories")), bool(row.get("read_only")))
    return github_reach.reach_of(row.repositories, row.read_only)


def _file_reach(raw: dict | None) -> Reach | None:
    extra = (raw or {}).get("extra") or {}
    return Reach.from_dict(extra.get("reach")) if isinstance(extra, dict) else None


def _remaining_s(raw: dict | None) -> float | None:
    """Seconds of life left in a file, ``None`` when it has none to read."""
    if not raw:
        return None
    expiry = oauth_account_store._parse_expiry(str(raw.get("expires_at") or ""))
    if expiry is None:
        return None
    return (expiry - datetime.now(timezone.utc)).total_seconds()


def file_serves(raw: dict | None, *, app_id: str, min_remaining_s: float) -> bool:
    """Whether an installation file can be served as it is: the configured
    app's, alive for long enough, not marked dead, not one the relay minted
    (OtoDock's app has no installation identity)."""
    if not raw:
        return False
    extra = raw.get("extra") or {}
    if not isinstance(extra, dict) or str(extra.get("app_id") or "") != app_id:
        return False
    if extra.get("refresh_failed") or extra.get("via_relay"):
        return False
    remaining = _remaining_s(raw)
    return remaining is not None and remaining > min_remaining_s


def mint_installation(in_use: AppInUse, installation_id: str | int, reach: Reach | None = None) -> TokenSet:
    """A fresh installation token by the admin's app key, narrowed by GitHub
    to ``reach``: its repositories, each of the installation's own account
    (a name of another owner refuses the mint, never cut to a name of this
    one), and under read-only the installation's permissions as GitHub
    holds them now, each at read (never minted wide: with none known the
    mint is refused, a passing failure). A narrowed mint GitHub refuses as
    unprocessable is a repository that left the installation
    (``installation_reach_outdated``) when its message says so, else the
    read permissions (a passing failure, GitHub's message logged). OtoDock's
    app through the relay has no key anywhere, so a hosted installation
    binding left from before stops (the card words it)."""
    if in_use.hosted:
        raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.INSTALLATION_HOSTED)
    if in_use.app is None:
        raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.APP_CHANGED)
    if reach is None or not reach.narrows:
        return github_app.mint_installation_token(in_use.app, int(installation_id))
    live = github_app.get_installation(in_use.app, int(installation_id))
    if live is None:
        raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.INSTALLATION_GONE)
    permissions: dict[str, str] | None = None
    if reach.read_only:
        granted = live.get("permissions")
        permissions = github_reach.read_only_permissions(granted if isinstance(granted, dict) else {})
        if not permissions:
            raise OAuthTokenError(github_app.PROVIDER_ID, "mint", _PERMISSIONS_UNKNOWN,
                                  "the installation's permissions are not known")
    names: list[str] | None = None
    if reach.repositories:
        account = live.get("account")
        login = str(account.get("login") or "") if isinstance(account, dict) else ""
        if not login:
            raise OAuthTokenError(github_app.PROVIDER_ID, "mint", _PERMISSIONS_UNKNOWN,
                                  "the installation's account is not known")
        try:
            names = github_reach.repository_names(reach, login)
        except ValueError as exc:
            raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.INSTALLATION_REACH_OUTDATED,
                                  str(exc)) from exc
    try:
        ts = github_app.mint_installation_token(
            in_use.app, int(installation_id), repositories=names, permissions=permissions,
        )
    except OAuthTokenError as exc:
        if exc.status != _UNPROCESSABLE:
            raise
        message = str(exc.description or "")
        if names and (permissions is None or _REPOSITORY_WORD in message.lower()):
            raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.INSTALLATION_REACH_OUTDATED,
                                  message, exc.status) from exc
        logger.warning("github app: GitHub refused the read permissions of installation %s: %s",
                       installation_id, message[:300])
        raise OAuthTokenError(github_app.PROVIDER_ID, "mint", _PERMISSIONS_REFUSED,
                              message, exc.status) from exc
    ts.raw["reach"] = reach.to_dict()
    return ts


def installation_payload(
    token_set: TokenSet, *, provider_id: str, prior: dict | None = None,
) -> dict:
    """The ``generic_oauth_v1`` file of an installation token: no refresh
    token, no client, GitHub's own ``expires_at`` when it gave one, the
    mint's facts in ``extra`` (and what the prior file knew: the account)."""
    extra = {k: v for k, v in (prior or {}).get("extra", {}).items() if k == "account"} if prior else {}
    extra.update({k: v for k, v in token_set.raw.items() if k != "access_token"})
    extra.pop("refresh_failed", None)
    extra.pop("refresh_failed_at", None)
    payload = oauth_account_store.render_generic_oauth_v1_token(
        provider_id=provider_id, account_id=str(extra.get("installation_id") or ""),
        access_token=token_set.access_token, refresh_token="", expires_in=token_set.expires_in,
        scopes=[], client_id="", client_secret="", token_url="", extra=extra,
    )
    given = str(token_set.raw.get("expires_at") or "")
    if given and oauth_account_store._parse_expiry(given) is not None:
        payload["expires_at"] = given
    return payload


def _mcp_names_of(provider_id: str) -> list[str]:
    return [m.name for m in (mcp_registry.get_mcps_by_provider(provider_id) or [])]


def standing_bindings(
    mcp_names: list[str], installation_id: str | int, *, reach: Reach | None = None,
) -> list[dict]:
    """The bindings that still hand out ``installation_id``: their owner
    manages the agent (the store's rule) and still owns the installation
    (the meta). With ``reach``, only the bindings of that reach (the ones
    a narrowed file serves; ``Reach()`` = the unnarrowed ones)."""
    out: list[dict] = []
    want = reach.digest() if reach is not None and reach.narrows else ""
    for row in credential_store.bindings_naming_installation(mcp_names, installation_id):
        live = credential_store.get_service_agent_binding(row["mcp_name"], row["agent_name"])
        if live is None or live.installation_id != str(installation_id):
            continue
        if reach is not None:
            mine = binding_reach_of(live)
            if (mine.digest() if mine.narrows else "") != want:
                continue
        meta = credential_store.get_account_meta(live.account_owner_sub, row["mcp_name"], live.account_label)
        if kinds.installation_standing(meta, installation_id):
            continue
        out.append(row)
    return out


def reap_if_orphan(provider_id: str, installation_id: str | int) -> bool:
    """Delete an installation's token files no standing binding uses any
    more (a clear, a rebind, a reach changed, a disconnect, a removal on
    GitHub). Returns whether a file went."""
    if not provider_id or not str(installation_id):
        return False
    names = _mcp_names_of(provider_id)
    gone = False
    with _lock_for(str(int(installation_id))):
        for path in installation_files(provider_id, installation_id):
            raw = oauth_account_store.read_account_token(path.parent, path.stem)
            if standing_bindings(names, installation_id, reach=_file_reach(raw) or Reach()):
                continue
            path.unlink(missing_ok=True)
            logger.info("github app: a token file of installation %s is gone (no binding uses it)",
                        installation_id)
            gone = True
    return gone


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _file_app_id(raw: dict | None) -> str:
    extra = (raw or {}).get("extra") or {}
    return str(extra.get("app_id") or "") if isinstance(extra, dict) else ""


def _mark_file_dead(path: Path, raw: dict | None, code: str) -> None:
    if not raw:
        return
    extra = dict(raw.get("extra") or {})
    extra["refresh_failed"] = code
    extra["refresh_failed_at"] = oauth_account_store.now_iso()
    with contextlib.suppress(OSError):
        oauth_account_store.write_token_payload(path, {**raw, "extra": extra})


def _reconfirm_owner(mcp_name: str, owner_sub: str, label: str, installation_id: str) -> dict:
    """The owner's standing re-read with their own token, at most once an
    hour per lender and installation; the meta as it stands after."""
    AccountNeedsReconnect = _reconnect_cls()
    now = time.monotonic()
    key = (owner_sub, label, installation_id)
    if now - _relisted.get(key, -_RELIST_INTERVAL_S) >= _RELIST_INTERVAL_S:
        try:
            meta = refresh_installations(owner_sub, mcp_name, label)
            _relisted[key] = now
            return meta
        except OAuthTokenError as exc:
            if exc.code == INVALID_GRANT:
                raise AccountNeedsReconnect("revoked") from exc
            logger.warning("github app: the owner's installations were not re-listed (%s)", exc.code)
        except LookupError as exc:
            raise AccountNeedsReconnect(kinds.APP_CHANGED) from exc
    return credential_store.get_account_meta(owner_sub, mcp_name, label)


def ensure_installation_token(
    mcp_name: str, owner_sub: str, label: str, installation_id: str, *,
    min_remaining_s: float = 300, reach: Reach | None = None,
) -> Path:
    """The token file an installation binding serves sessions with, minted
    or renewed when it must be: the owner still owns the installation (the
    meta, re-confirmed with their token at most hourly), this install's
    GitHub credentials are still the app, the file has life left. On the
    event loop nothing is minted: an unexpired file is served, else the
    MCP waits for the next try. A binding whose ``reach`` narrows it has
    its own file, minted narrowed by GitHub. A permanent refusal raises
    ``AccountNeedsReconnect`` (the file marked), a passing one
    ``CredentialUnavailable`` unless the file still serves."""
    AccountNeedsReconnect = _reconnect_cls()
    CredentialUnavailable = _unavailable_cls()

    installation_id = str(int(installation_id))
    if hosted(mcp_name):
        # OtoDock's app has no installation identity: a binding left from
        # before stops here, before the loop's file or the relay is read.
        raise AccountNeedsReconnect(kinds.INSTALLATION_HOSTED)
    meta = credential_store.get_account_meta(owner_sub, mcp_name, label)
    standing = kinds.installation_standing(meta, installation_id)
    if standing:
        raise AccountNeedsReconnect(standing)
    provider_id = str(oauth_block_of(mcp_name).get("provider_id") or github_app.PROVIDER_ID)
    path = installation_file(provider_id, installation_id, reach)
    raw = oauth_account_store.read_account_token(path.parent, path.stem)
    if _on_loop():
        # Nothing asks the relay or GitHub here: a file with life left
        # serves by the app id it was minted with.
        if file_serves(raw, app_id=_file_app_id(raw), min_remaining_s=0):
            return path
        raise CredentialUnavailable(_TRANSIENT)
    try:
        in_use = app_in_use(mcp_name)
    except UNREACHABLE as exc:
        raise CredentialUnavailable(_TRANSIENT) from exc
    if in_use.kind != kinds.KIND_GITHUB_APP:
        raise AccountNeedsReconnect(kinds.APP_CHANGED)
    if file_serves(raw, app_id=in_use.app_id, min_remaining_s=min_remaining_s):
        return path
    with _lock_for(installation_id):
        raw = oauth_account_store.read_account_token(path.parent, path.stem)
        if file_serves(raw, app_id=in_use.app_id, min_remaining_s=min_remaining_s):
            return path
        meta = _reconfirm_owner(mcp_name, owner_sub, label, installation_id)
        standing = kinds.installation_standing(meta, installation_id)
        if standing:
            raise AccountNeedsReconnect(standing)
        try:
            ts = mint_installation(in_use, installation_id, reach)
        except OAuthTokenError as exc:
            if exc.code in kinds.INSTALLATION_REASONS:
                _mark_file_dead(path, raw, exc.code)
                raise AccountNeedsReconnect(exc.code) from exc
            if file_serves(raw, app_id=in_use.app_id, min_remaining_s=0):
                return path
            raise CredentialUnavailable(_TRANSIENT) from exc
        account = kinds.installation_in(meta, installation_id) or {}
        ts.raw.setdefault("account", account.get("account", ""))
        oauth_account_store.write_token_payload(path, installation_payload(ts, provider_id=provider_id, prior=raw))
        logger.info("github app: minted the token of installation %s for %s", installation_id, mcp_name)
    return path


def _reconfirm_lenders(names: list[str], installation_id: str) -> list[dict]:
    """Every standing binding's owner re-checked with their own token (the
    hourly throttle is per lender), then the bindings that still stand: the
    worker re-mints a file long before a session asks, so this is where a
    demoted or departed owner stops lending the installation. A re-check
    the token cannot make (the user token died) drops that lender."""
    for row in standing_bindings(names, installation_id):
        live = credential_store.get_service_agent_binding(row["mcp_name"], row["agent_name"])
        if live is None:
            continue
        try:
            _reconfirm_owner(row["mcp_name"], live.account_owner_sub, live.account_label, installation_id)
        except _reconnect_cls():
            continue
    return standing_bindings(names, installation_id)


def remint_file(path: Path, raw: dict, *, provider_id: str) -> dict | None:
    """The refresh worker's arm for an installation file: with no standing
    binding the file is an orphan and goes (``None``); when every lender's
    re-check says the owner no longer owns it, ``OAuthTokenError``
    (``installation_not_owned``, the file marked dead by the worker); else a
    fresh token is minted (the app in use must still be the file's) and the
    new payload returned for the worker's guarded write. ``OAuthTokenError``
    carries the permanent codes."""
    extra = raw.get("extra") or {}
    installation_id = str(extra.get("installation_id") or path.stem.split("-", 1)[0])
    reach = _file_reach(raw) or Reach()
    names = _mcp_names_of(provider_id)
    if not names or not standing_bindings(names, installation_id, reach=reach):
        with contextlib.suppress(OSError):
            path.unlink()
        logger.info("github app: a token file of installation %s is gone (no binding uses it)", installation_id)
        return None
    if hosted(names[0]):
        raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.INSTALLATION_HOSTED)
    if not _reconfirm_lenders(names, installation_id):
        raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.INSTALLATION_NOT_OWNED)
    in_use = app_in_use(names[0])
    if in_use.kind != kinds.KIND_GITHUB_APP or in_use.app_id != str(extra.get("app_id") or ""):
        raise OAuthTokenError(github_app.PROVIDER_ID, "mint", kinds.APP_CHANGED)
    ts = mint_installation(in_use, installation_id, reach)
    return installation_payload(ts, provider_id=provider_id, prior=raw)


def mcp_gets_installation_token(mcp_name: str) -> bool:
    """Whether the MCP server itself gets the installation's token for an
    installation binding (the kind's ``mcp_token``), or the owner's user
    token (GitHub narrows that one to the app's installations). The manifest
    answers alone when only one kind can say ``installation``; the relay is
    never asked on the event loop (the user token then)."""
    oauth = oauth_block_of(mcp_name)
    saying = {
        str(entry.get("kind") or "") for entry in kinds.declared_kinds(oauth)
        if kinds.mcp_token_of(oauth, str(entry.get("kind") or "")) == kinds.MCP_TOKEN_INSTALLATION
    }
    if not saying:
        return False
    if _on_loop():
        return False
    try:
        in_use = app_in_use(mcp_name)
    except UNREACHABLE:
        return False
    return in_use.kind in saying
