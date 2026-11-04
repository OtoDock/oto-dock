"""OAuth account persistence — DB + token-file layer.

One stop for every OAuth flow side-effect after a successful exchange:
  * Persist the credentials (DB row + ``user_credential_accounts`` row).
  * Write the per-user token file in the canonical ``generic_oauth_v1``
    shape. When the manifest declares ``token_format.aliases``, those
    legacy keys are emitted alongside the canonical ones so MCPs whose
    upstream reader expects a specific key layout (workspace-mcp's
    ``google.auth`` lib) keep working unchanged.
  * Mark the connected services on the account.

Also owns the central-token-dir path helpers used by
``services/credential_resolver`` and ``core/credentials/credential_files``.

Directory layout — keyed by provider_id, one dir per user:

    sessions/{provider_id}-tokens/{username}/{account_label}.json            # user account

Examples:
    sessions/google-tokens/alice/work.json
    sessions/slack-tokens/alice/personal.json

A manager who binds their personal user account to an agent as its service
identity (see ``service_agent_bindings.account_owner_sub``) doesn't get a
separate dir — the resolver reads from the user's existing ``{username}/``
dir directly. One credential, two routes. There is no platform "service
account" token tree (removed before the public release).

Multiple MCPs sharing a provider_id share the token dir, so one OAuth
grant powers every MCP of that provider. The dir name MUST match the
manifest's ``path_env.<KEY>.subpath`` declaration (e.g. workspace-mcp
declares ``"google-tokens"``) so the resolver's per-session file copy
finds the source.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

import config
from storage.identity import oauth_app_kinds

logger = logging.getLogger("otodock.oauth-account-store")


# ---------------------------------------------------------------------------
# Path helpers (used by resolver + writeback)
# ---------------------------------------------------------------------------

def get_token_base_dir(provider_id: str, *, create: bool = True) -> Path:
    """Central token store root for one provider.

    Returns ``sessions/{provider_id}-tokens/``. Created on demand unless
    ``create`` is false (a scan that only reads).
    """
    if not provider_id or not isinstance(provider_id, str):
        raise ValueError(f"provider_id must be a non-empty string, got {provider_id!r}")
    d = config.SESSIONS_DIR / f"{provider_id}-tokens"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def other_account_files(provider_id: str, own: Path | None = None) -> list[Path]:
    """Every person's token file of ``provider_id`` on this install but
    ``own``, the app's installation files (``_app/``) left out."""
    base = get_token_base_dir(provider_id, create=False)
    if not base.is_dir():
        return []
    own_resolved = own.resolve() if own is not None else None
    return [
        path for user_dir in base.iterdir() if user_dir.is_dir() and user_dir.name != "_app"
        for path in user_dir.glob("*.json") if path.resolve() != own_resolved
    ]


def get_token_dir(
    username: str,
    *,
    provider_id: str,
) -> Path:
    """Return the directory holding a user's token files.

    Creates the directory if it doesn't exist. One ``{account_label}.json``
    file per connected account. ``provider_id`` is required so multiple
    OAuth providers' token dirs don't collide: ``{provider_id}-tokens/{username}/``.

    Agent-scope (service) sessions read the bound user's OWN dir — the caller
    looks up the bound owner's username via ``database.get_username_by_sub``
    and passes it here as ``username``. There is no separate service tree.
    """
    if not username:
        raise ValueError("username is required")
    base = get_token_base_dir(provider_id)
    d = base / username
    d.mkdir(parents=True, exist_ok=True)
    return d


# An account label becomes a FILENAME component; constrain it so a caller-
# supplied '/' or '..' can't escape token_dir (cross-user token read / revoke /
# delete). The label originates from request bodies and from vendor userinfo
# (display name / email), so it is NOT trusted. INTERNAL spaces are allowed
# (users type labels like "OtoDock Marketing"); leading/trailing ones are not
# (invisible in the UI, and "{label}.json" round-trips are whitespace-exact).
_ACCOUNT_LABEL_RE = re.compile(
    r"^[A-Za-z0-9._@+-](?:[A-Za-z0-9._@+ -]{0,126}[A-Za-z0-9._@+-])?$"
)


def validate_account_label(account_label: str) -> str:
    """Raise ValueError unless ``account_label`` is a safe filename token."""
    if not _ACCOUNT_LABEL_RE.match(account_label or ""):
        raise ValueError(f"invalid account label: {account_label!r}")
    return account_label


def account_token_path(token_dir: Path, account_label: str) -> Path:
    """Return the canonical path for an account's token file.

    The label is validated first — it becomes a filename component, so an
    unconstrained value ('/' or '..') would let a request-supplied label escape
    ``token_dir`` and read or delete another user's token file.
    """
    validate_account_label(account_label)
    return token_dir / f"{account_label}.json"


# ---------------------------------------------------------------------------
# Token file write — canonical ``generic_oauth_v1`` shape
#
# Optional manifest-declared aliases let legacy-shape MCP readers
# (workspace-mcp's google.auth lib expects `token`/`expiry`/`token_uri`)
# see their expected keys alongside the canonical ones in the same file.
# Most providers declare no aliases — their files are pure canonical.
# ---------------------------------------------------------------------------

def render_generic_oauth_v1_token(
    *,
    provider_id: str,
    account_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    scopes: list[str],
    client_id: str,
    client_secret: str,
    token_url: str,
    extra: dict | None = None,
    aliases: dict[str, str] | None = None,
) -> dict:
    """The canonical ``generic_oauth_v1`` payload (``_write_generic_oauth_v1_token``
    writes it; the refresh worker writes it through its own guarded write).

    Optionally emits alias keys (declared by the MCP's manifest as
    ``token_format.aliases``) so legacy-shape MCP readers (workspace-mcp's
    google.auth lib) see their expected keys alongside the canonical ones.
    Most providers pass no aliases.

    Special case: when an alias targets ``expires_at``, the alias value
    is written in the naive ISO format (``%Y-%m-%dT%H:%M:%S``) that
    google.auth's older API versions accept. The canonical
    ``expires_at`` uses ISO-8601 with ``Z`` suffix.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # expires_in <= 0 = the vendor never expires this token (Slack without
    # rotation, GitHub OAuth apps): write the empty-string sentinel the
    # refresh worker treats as never-expires instead of fabricating +1h.
    if int(expires_in or 0) > 0:
        expires_at_dt = now + timedelta(seconds=int(expires_in))
        expires_at_str = expires_at_dt.isoformat().replace("+00:00", "Z")
        expires_at_naive = expires_at_dt.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        expires_at_str = ""
        expires_at_naive = ""
    payload: dict = {
        "provider": provider_id,
        "account_id": account_id,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "token_url": token_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": list(scopes),
        "expires_at": expires_at_str,
        "extra": dict(extra or {}),
    }
    if aliases:
        for alias_key, canonical_key in aliases.items():
            if canonical_key == "expires_at":
                # Legacy naive ISO (no Z) for google.auth compatibility.
                payload[alias_key] = expires_at_naive
            elif canonical_key in payload:
                payload[alias_key] = payload[canonical_key]
    return payload


def _write_generic_oauth_v1_token(
    token_path: Path,
    *,
    provider_id: str,
    account_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    scopes: list[str],
    client_id: str,
    client_secret: str,
    token_url: str,
    extra: dict | None = None,
    aliases: dict[str, str] | None = None,
) -> None:
    """Persist a token bundle in the canonical ``generic_oauth_v1`` shape
    (``render_generic_oauth_v1_token``), atomically."""
    payload = render_generic_oauth_v1_token(
        provider_id=provider_id, account_id=account_id, access_token=access_token,
        refresh_token=refresh_token, expires_in=expires_in, scopes=scopes,
        client_id=client_id, client_secret=client_secret, token_url=token_url,
        extra=extra, aliases=aliases,
    )
    partial = token_path.with_suffix(token_path.suffix + ".partial")
    partial.write_text(json.dumps(payload, indent=2))
    # Token file holds access/refresh tokens + (self-managed) client_secret —
    # lock to owner-only before the atomic rename so it never lands group/world
    # readable under a permissive umask.
    partial.chmod(0o600)
    partial.replace(token_path)


def now_iso() -> str:
    """The canonical ``expires_at`` form of this moment."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_token_payload(token_path: Path, payload: dict) -> None:
    """Persist a token payload as it is (a rewrite of an existing file by a
    caller that holds its account lock), atomically and owner-only."""
    partial = token_path.with_suffix(token_path.suffix + ".partial")
    partial.write_text(json.dumps(payload, indent=2))
    partial.chmod(0o600)
    partial.replace(token_path)


def _read_oauth_token(token_path: Path) -> dict | None:
    """Read an OAuth token file (``generic_oauth_v1`` shape)."""
    if not token_path.exists():
        return None
    try:
        return json.loads(token_path.read_text())
    except (OSError, ValueError):
        logger.warning("Corrupt OAuth token file: %s", token_path)
        return None


def resolve_account_credential_keys(
    oauth_block: dict, provider_id: str,
) -> tuple[str, str]:
    """Return ``(email_key, services_key)`` for this provider's user_credentials rows.

    Reads ``credentials.oauth.account_credential_keys`` from the manifest when
    declared (workspace-mcp uses ``{email: "GOOGLE_EMAIL", services: "GOOGLE_SERVICES"}``
    so its container reads the legacy env-var names). Defaults to
    ``{PROVIDER_ID_UPPER}_EMAIL`` / ``{PROVIDER_ID_UPPER}_SERVICES``.

    Used by ``persist_oauth_account`` to write the keys and by
    ``api/mcp/credentials.py`` to read them back when building the integrations
    list for the dashboard.
    """
    ack = (oauth_block or {}).get("account_credential_keys") or {}
    email_key = ack.get("email") or f"{provider_id.upper()}_EMAIL"
    services_key = ack.get("services") or f"{provider_id.upper()}_SERVICES"
    return email_key, services_key


def get_canonical_access_token(raw: dict) -> str:
    """Extract the access token from a ``generic_oauth_v1`` token dict."""
    return raw.get("access_token") or ""


# The ``extra.flow`` of a token issued by the MCP server's own authorization
# server to the client this install registered (services/oauth/mcp_authorization.py).
MCP_AUTHORIZATION_FLOW = "mcp_authorization"
# The flows whose files carry no refresh token by design.
_FLOWS_WITHOUT_REFRESH = ("client_credentials", "personal_access_token", oauth_app_kinds.INSTALLATION_FLOW)


def _parse_expiry(expiry: str) -> datetime | None:
    """A file's ``expires_at`` as an aware datetime, ``None`` when empty or
    unreadable."""
    if not expiry:
        return None
    try:
        dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def token_dead_reason(raw: dict) -> str:
    """Why a token file can no longer serve a session, or ``""``: the worker
    recorded a permanent refresh failure (``extra.refresh_failed``), or the
    access token expired with no refresh token to renew it. A never-expiring
    file (empty ``expires_at``), a client-credentials or PAT file, and a
    file with a live refresh token are left to the vendor's verdict."""
    extra = raw.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    failed = str(extra.get("refresh_failed") or "")
    if failed:
        return "revoked" if failed in ("invalid_grant", "invalid_client") else failed
    expiry = str(raw.get("expires_at") or "")
    if not expiry or raw.get("refresh_token"):
        return ""
    if str(extra.get("flow") or "") in _FLOWS_WITHOUT_REFRESH:
        return ""
    expiry_dt = _parse_expiry(expiry)
    if expiry_dt is None:
        return ""
    return "expired" if expiry_dt <= datetime.now(timezone.utc) else ""


def read_account_token(token_dir: Path, account_label: str) -> dict | None:
    """Read a specific account's token file."""
    return _read_oauth_token(account_token_path(token_dir, account_label))


def delete_account_token(token_dir: Path, account_label: str) -> bool:
    """Delete a specific account's token file. Returns True if deleted."""
    path = account_token_path(token_dir, account_label)
    if path.exists():
        path.unlink()
        logger.info("Deleted OAuth token file: %s", path)
        return True
    return False


# ---------------------------------------------------------------------------
# High-level persist — used by oauth_engine after a successful exchange
# ---------------------------------------------------------------------------

def persist_oauth_account(
    *,
    user_sub: str,
    mcp_name: str,
    provider_id: str,
    account_label: str,
    services: list[str],
    token_set,          # auth.oauth_providers.base.TokenSet
    userinfo,           # auth.oauth_providers.base.UserInfo
    client_id: str,
    client_secret: str,
    token_url: str,
) -> None:
    """Persist a freshly-exchanged OAuth grant (user account).

    Steps:
      1. Resolve the user's username.
      2. Compute scopes list from manifest + ``services`` selection
         (so the token file records what was actually granted).
      3. Write the token file to ``token_dir / {account_label}.json``.
      4. Upsert the ``user_credential_accounts`` row + display_email +
         is_default (first account → TRUE, otherwise unchanged).
      5. Insert credentials rows: GOOGLE_EMAIL + GOOGLE_SERVICES under
         the account_label.
      6. Clear the account-removed mark on every row of the provider's
         MCPs that names the label: they all read the file just written.

    This single entry point keeps the on-disk token, the DB account row,
    and the credentials row consistent.
    """
    from services.mcp import mcp_registry
    from storage import database as task_store
    from storage.identity import credential_store

    # --- target directory (per-provider) ---
    username = task_store.get_username_by_sub(user_sub)
    if not username:
        raise RuntimeError(
            f"User {user_sub[:8]} has no username — cannot persist OAuth tokens"
        )
    token_dir = get_token_dir(username, provider_id=provider_id)

    # --- recompute scopes from manifest for stable on-disk recording ---
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or not manifest.credentials.oauth:
        raise RuntimeError(
            f"MCP '{mcp_name}' has no oauth credential block in manifest"
        )
    oauth = manifest.credentials.oauth
    base_scopes = list(oauth.get("base_scopes", []))
    by_key = {s["key"]: s.get("scopes", []) for s in oauth.get("services", [])}
    seen: set[str] = set()
    final_scopes: list[str] = []
    for s in base_scopes:
        if s not in seen:
            seen.add(s)
            final_scopes.append(s)
    for svc in services:
        for s in by_key.get(svc, []):
            if s not in seen:
                seen.add(s)
                final_scopes.append(s)

    # Incremental-auth providers (google): oauth_start always sends the
    # incremental param, so a re-consent by the same vendor account grants
    # the UNION of previously granted + newly requested scopes. When the
    # target token file already exists — same provider + label, possibly
    # connected through a DIFFERENT MCP sharing the provider_id — record
    # that union, not just this MCP's slice: readers like workspace-mcp's
    # google.auth pass the file's `scopes` on refresh, and a narrowed
    # record would narrow the refreshed token and break the other MCP.
    # A vendor whose grants accumulate per app and person (``grants_accumulate``,
    # Meta's) returns the union too, so the same is recorded, but only over a
    # file that holds the same login: a sibling's card would otherwise ask
    # for access the token already has.
    _incremental = _accumulates = False
    try:
        from auth.oauth_providers import get_provider
        _impl = get_provider(provider_id)
        _incremental = getattr(_impl, "supports_incremental_auth", False)
        _accumulates = getattr(_impl, "grants_accumulate", False)
    except KeyError:
        pass
    prior = (read_account_token(token_dir, account_label) or {}) if (_incremental or _accumulates) else {}
    if _accumulates and not _incremental:
        prior_extra = prior.get("extra")
        prior_extra = prior_extra if isinstance(prior_extra, dict) else {}
        same_login = bool(userinfo.account_id) and str(prior.get("account_id") or "") == userinfo.account_id \
            and issued_by(prior_extra, str(prior.get("client_id") or "")) == issued_by(token_set.raw, client_id)
        if not same_login:
            prior = {}
    if prior:
        for s in prior.get("scopes") or []:
            if isinstance(s, str) and s and s not in seen:
                seen.add(s)
                final_scopes.append(s)

    # --- write token file (canonical generic_oauth_v1 + optional aliases) ---
    token_format = oauth.get("token_format", {}) or {}
    aliases = token_format.get("aliases") or None
    # `extra` carries vendor-specific fields (Slack team_id, Microsoft
    # tenant_id, Zoom account_id) collected by the provider's
    # normalize_token_response and parked in TokenSet.raw. Providers
    # control what lands there.
    extra = {k: v for k, v in token_set.raw.items() if k != "access_token"}
    _write_generic_oauth_v1_token(
        account_token_path(token_dir, account_label),
        provider_id=provider_id,
        account_id=userinfo.account_id or userinfo.email,
        access_token=token_set.access_token,
        refresh_token=token_set.refresh_token,
        expires_in=token_set.expires_in,
        scopes=final_scopes,
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
        extra=extra,
        aliases=aliases,
    )

    # --- DB writes ---
    email_key, services_key = resolve_account_credential_keys(oauth, provider_id)

    credential_store.set_user_credentials(
        user_sub, mcp_name,
        {
            email_key: userinfo.email,
            services_key: ",".join(services),
        },
        account_label=account_label,
    )
    credential_store.set_account_display_email(
        user_sub, mcp_name, account_label, userinfo.email,
    )
    # Which kind of app issued the token: a GitHub-App account keeps its
    # installations, any other kind clears them (services/oauth/github_apps.py).
    from services.oauth import github_apps
    github_apps.record_account_kind(
        user_sub, mcp_name, account_label,
        app_kind=str(token_set.raw.get("app_kind") or ""),
        userinfo_raw=userinfo.raw if isinstance(userinfo.raw, dict) else {},
        hosted=bool(token_set.raw.get("via_relay")),
    )
    from services.webhooks import relay_notices
    try:
        relay_notices.clear_mark(user_sub, provider_id, account_label)
    except psycopg.Error:
        # The grant is written: a mark left on a sibling's row is read only
        # while the file is gone, so it never fails the sign-in.
        logger.warning("OAuth account %s/%s: the account-removed mark was not cleared",
                       provider_id, account_label, exc_info=True)

    logger.info(
        "Persisted OAuth account: provider=%s mcp=%s user=%s account=%s",
        provider_id, mcp_name, user_sub[:8], account_label,
    )


# ---------------------------------------------------------------------------
# The label a sign-in lands on
#
# A token file is one vendor login, shared by every MCP of the provider whose
# account row names its label (one login, the union of its scopes). So a
# sign-in never writes into a file another MCP's row names unless the file
# already holds the same login: choosing an account for one MCP must not move
# another's.
# ---------------------------------------------------------------------------

_LABEL_DROPPED = re.compile(r"[^A-Za-z0-9._@+ -]+")
_PAT_FLOW = "personal_access_token"
# What a label's file says about a sign-in's login (``account_label_for``).
_FREE, _SAME, _OTHER, _UNCOMPARED = "free", "same", "other", "uncompared"


def issued_by(extra: dict, client_id: str) -> str:
    """How a token was issued, from a token response's raw fields or a token
    file's ``extra`` and ``client_id``. Ids from two issuers say nothing
    about each other (Meta's user ids differ per app), so logins compare
    only between files issued the same way."""
    if extra.get("via_relay"):
        return f"relay:{extra.get('app_kind') or ''}"
    flow = str(extra.get("flow") or "")
    if flow == MCP_AUTHORIZATION_FLOW:
        return f"registered:{str(extra.get('issuer') or '').rstrip('/')}"
    if flow == _PAT_FLOW:
        return "pat"
    return f"app:{client_id}"


def clean_label(text: str, *, provider_id: str, identity: str) -> str:
    """A vendor-given name as an account label: unchanged when the label rule
    takes it; else the characters it refuses (a Greek or accented name, an
    apostrophe) dropped and the spaces collapsed; the provider id and the
    login id's tail when nothing is left."""
    if _ACCOUNT_LABEL_RE.match(text or ""):
        return text
    label = " ".join(_LABEL_DROPPED.sub(" ", text or "").split())[:128].strip(" .")
    if _ACCOUNT_LABEL_RE.match(label):
        return label
    tail = re.sub(r"[^A-Za-z0-9]", "", identity)[-8:]
    return f"{provider_id} {tail}" if tail else provider_id


def _numbered(base: str, n: int) -> str:
    suffix = f" {n}"
    return base[: 128 - len(suffix)].rstrip() + suffix


def account_label_for(
    token_dir: Path, *, hint: str, identity_label: str, identity: str, email: str,
    issued: str, named_elsewhere,
) -> str:
    """The label a sign-in of the login ``identity`` (the vendor's id, ``""``
    when it gave none) lands on. ``hint`` is the label of the card the
    sign-in started from (a Reconnect), ``identity_label`` the login's own
    (its email or name); ``named_elsewhere(label)`` says whether another MCP
    of the provider names that label for this person.

    A Reconnect keeps its card's label unless another MCP names it and its
    file holds another login (or one that cannot be compared). An add, or a
    Reconnect sent elsewhere, takes the file that already holds this login,
    else the login's own label, numbered (``Name 2``) while that label's file
    holds another login, or another MCP names it and its file holds no
    comparable login or is gone (that row waits for its own card's
    Reconnect). A file with no login recorded, or issued another way, cannot
    be compared; a Reconnect's missing file is free."""
    def state(label: str) -> str:
        raw = read_account_token(token_dir, label)
        if raw is None:
            return _FREE
        recorded = str(raw.get("account_id") or "")
        extra = raw.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        if not identity or not recorded or issued_by(extra, str(raw.get("client_id") or "")) != issued:
            return _UNCOMPARED
        # A file written when the vendor gave no id recorded the email.
        if recorded == identity or ("@" in email and recorded == email):
            return _SAME
        return _OTHER

    hint = (hint or "").strip()
    if hint:
        held = state(hint)
        if held in (_SAME, _FREE) or not named_elsewhere(hint):
            return hint
    if identity:
        for path in sorted(token_dir.glob("*.json")):
            if path.stem != hint and _ACCOUNT_LABEL_RE.match(path.stem) and state(path.stem) == _SAME:
                return path.stem
    # A label another MCP names is never taken unless its file holds this
    # login: a row whose file is gone (a sibling's disconnect before 1.9.1)
    # waits for the person's own Reconnect of that card.
    for n in range(1, 100):
        label = identity_label if n == 1 else _numbered(identity_label, n)
        held = state(label)
        if held == _SAME or (held in (_FREE, _UNCOMPARED) and not named_elsewhere(label)):
            return label
    raise ValueError("no free account label")


def other_mcps_naming(user_sub: str, provider_id: str, mcp_name: str, label: str) -> list[str]:
    """The installed MCPs of ``provider_id`` other than ``mcp_name`` whose
    account rows name ``label`` for this person (they share its file)."""
    from services.mcp import mcp_registry
    from storage.identity import credential_store
    siblings = {m.name for m in mcp_registry.get_mcps_by_provider(provider_id)} - {mcp_name}
    if not siblings:
        return []
    return sorted(siblings & set(credential_store.mcps_naming_label(user_sub, label)))


def _pick_label(*, user_sub: str, provider_id: str, mcp_name: str, hint: str, own_label: str,
                userinfo, identity: str, issued: str) -> str:
    from storage import database as task_store
    username = task_store.get_username_by_sub(user_sub)
    if not username:
        raise RuntimeError(f"User {user_sub[:8]} has no username — cannot persist OAuth tokens")
    return account_label_for(
        get_token_dir(username, provider_id=provider_id), hint=hint,
        identity_label=clean_label(own_label, provider_id=provider_id, identity=identity),
        identity=identity, email=userinfo.email, issued=issued,
        named_elsewhere=lambda label: bool(other_mcps_naming(user_sub, provider_id, mcp_name, label)),
    )


@contextlib.asynccontextmanager
async def signin_label(*, user_sub: str, provider_id: str, mcp_name: str, hint: str,
                       own_label: str, userinfo, identity: str, issued: str):
    """The label a sign-in persists under (``account_label_for``; ``own_label``
    is the login's own, its email or the name the manifest reads), chosen and
    written under the person's provider lock and then the label's own lock
    (the one the refresh worker holds across a rewrite). The provider lock
    is the key ``(person, provider, "")``, which no label can be; it is
    always taken before a label lock and never while one is held."""
    from core.credentials import credential_locks
    async with credential_locks.get_lock(user_sub, provider_id, ""):
        label = await asyncio.to_thread(functools.partial(
            _pick_label, user_sub=user_sub, provider_id=provider_id, mcp_name=mcp_name,
            hint=hint, own_label=own_label, userinfo=userinfo, identity=identity, issued=issued,
        ))
        async with credential_locks.get_lock(user_sub, provider_id, label):
            yield label
