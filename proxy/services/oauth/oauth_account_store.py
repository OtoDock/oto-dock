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
``services/credential_resolver`` and ``core/credential_writeback``.

Directory layout — keyed by provider_id, one dir per user:

    sessions/{provider_id}-tokens/{username}/{account_label}.json            # user account

Examples:
    sessions/google-tokens/alice/work.json
    sessions/slack-tokens/alice/personal.json

A manager who binds their personal user account to an agent as its service
identity (see ``service_agent_bindings.account_owner_sub``) doesn't get a
separate dir — the resolver reads from the user's existing ``{username}/``
dir directly. One credential, two routes. There is no platform "service
account" token tree (removed for open source).

Multiple MCPs sharing a provider_id share the token dir, so one OAuth
grant powers every MCP of that provider. The dir name MUST match the
manifest's ``path_env.<KEY>.subpath`` declaration (e.g. workspace-mcp
declares ``"google-tokens"``) so the resolver's per-session file copy
finds the source.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

logger = logging.getLogger("claude-proxy.oauth-account-store")


# ---------------------------------------------------------------------------
# Path helpers (used by resolver + writeback)
# ---------------------------------------------------------------------------

def get_token_base_dir(provider_id: str) -> Path:
    """Central token store root for one provider.

    Returns ``sessions/{provider_id}-tokens/``. Created on demand.
    """
    if not provider_id or not isinstance(provider_id, str):
        raise ValueError(f"provider_id must be a non-empty string, got {provider_id!r}")
    d = config.SESSIONS_DIR / f"{provider_id}-tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    """Persist a token bundle in the canonical ``generic_oauth_v1`` shape.

    Optionally emits alias keys (declared by the MCP's manifest as
    ``token_format.aliases``) into the same file so legacy-shape MCP
    readers (workspace-mcp's google.auth lib) see their expected keys
    alongside the canonical ones. Most providers pass no aliases.

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
    partial = token_path.with_suffix(token_path.suffix + ".partial")
    partial.write_text(json.dumps(payload, indent=2))
    # Token file holds access/refresh tokens + (self-managed) client_secret —
    # lock to owner-only before the atomic rename so it never lands group/world
    # readable under a permissive umask.
    partial.chmod(0o600)
    partial.replace(token_path)


def _read_oauth_token(token_path: Path) -> dict | None:
    """Read an OAuth token file (``generic_oauth_v1`` shape)."""
    if not token_path.exists():
        return None
    try:
        return json.loads(token_path.read_text())
    except Exception:
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

    This single entry point keeps the on-disk token, the DB account row,
    and the credentials row consistent.
    """
    from storage import credential_store
    from storage import database as task_store
    from services.mcp import mcp_registry

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
    _incremental = False
    try:
        from auth.oauth_providers import get_provider
        _incremental = getattr(
            get_provider(provider_id), "supports_incremental_auth", False,
        )
    except KeyError:
        pass
    if _incremental:
        prior = read_account_token(token_dir, account_label) or {}
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

    logger.info(
        "Persisted OAuth account: provider=%s mcp=%s user=%s account=%s",
        provider_id, mcp_name, user_sub[:8], account_label,
    )
