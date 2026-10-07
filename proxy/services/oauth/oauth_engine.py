"""OAuth state-token + code-exchange orchestration.

A state token:
  * Is opaque to the client (random url-safe 32-byte token).
  * Has a TTL (default 5 minutes).
  * Carries metadata: ``user_sub``, ``mcp_name``, ``provider_id``,
    ``services``, ``account_label_hint``, ``mobile``,
    ``redirect_uri``, and (when flow == authorization_code_pkce)
    ``code_verifier``.
  * Is one-shot — consuming via ``validate_state`` deletes the entry.

The exchange orchestrator (``do_oauth_exchange``) drives the full flow:
  1. ``validate_state`` to recover metadata.
  2. ``provider.exchange_code`` to swap code for tokens (passes
     ``code_verifier`` for PKCE flows).
  3. ``provider.fetch_userinfo`` to derive the account_label (from email).
  4. ``services/oauth_account_store.persist_oauth_account`` to persist the
     token file + DB rows.

Returns ``{email, account_label}`` to the caller (API handler builds the
HTML / mobile redirect response).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

# OAuth state TTL — 5 minutes. Matches Authentik's default state TTL.
_STATE_TTL_SECONDS = 300


@dataclass
class OAuthState:
    """In-memory state token payload."""
    user_sub: str
    mcp_name: str
    provider_id: str
    services: list[str] = field(default_factory=list)
    account_label_hint: str = ""
    mobile: bool = False
    redirect_uri: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    # PKCE code verifier — populated when the manifest's flow is
    # `authorization_code_pkce`. Empty for non-PKCE flows. The challenge
    # (derived value) goes on the URL via `extra`; the verifier stays
    # server-side to prove possession during code exchange.
    code_verifier: str = ""
    # A connect through the client this install registered at the MCP
    # server's own authorization server (services/oauth/mcp_authorization.py):
    # the registration row, its client id, the issuer, the resource and the
    # endpoints the start resolved. The state binds the person, the MCP, the
    # provider and this registration; the callback uses exactly these.
    mcp_authorization: dict[str, Any] | None = None
    # monotonic timestamp at which this state expires
    expiry: float = 0.0


# ---------------------------------------------------------------------------
# PKCE helpers — RFC 7636
# ---------------------------------------------------------------------------

def _generate_pkce_verifier() -> str:
    """Generate a 64-char base64url-encoded PKCE code_verifier.

    RFC 7636 requires 43-128 chars from [A-Z][a-z][0-9]-._~.
    Using 48 random bytes → 64 base64url chars (strip padding).
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")


def _pkce_challenge(verifier: str) -> str:
    """Compute the S256 PKCE challenge for a verifier (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# Module-level state store. Process-local — fine for single-proxy deploys
# (which is everything we support). Multi-replica deploys (future) need
# a shared backend; the OAuth flow has a <5min lifetime so a quick
# round-trip to Redis or DB is acceptable then.
_states: dict[str, OAuthState] = {}


def create_state(
    *,
    user_sub: str,
    mcp_name: str,
    provider_id: str,
    services: list[str] | None = None,
    account_label_hint: str = "",
    mobile: bool = False,
    redirect_uri: str = "",
    extra: dict[str, str] | None = None,
    mcp_authorization: dict[str, Any] | None = None,
) -> str:
    """Mint and store a new OAuth state token. Returns the opaque token.

    For ``authorization_code_pkce`` flow (detected via manifest lookup),
    also generates a PKCE code_verifier, stores it on the state, and
    merges the derived ``code_challenge`` + ``code_challenge_method=S256``
    into the ``extra`` dict (so ``provider.build_auth_url`` adds them
    to the URL). The verifier itself never leaves the server until it's
    used in the token exchange.
    """
    from services.mcp import mcp_registry  # local import to avoid cycle

    token = secrets.token_urlsafe(32)
    extra_merged = dict(extra or {})
    code_verifier = ""

    # PKCE: look up the manifest's default flow (flows[0]) to decide
    # whether we need a verifier. Non-authorization_code flows (device_code,
    # client_credentials, personal_access_token) bypass create_state entirely.
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest and manifest.credentials.oauth:
        flows = manifest.credentials.oauth.get("flows", [])
        if flows and flows[0] == "authorization_code_pkce":
            code_verifier = _generate_pkce_verifier()
            extra_merged["code_challenge"] = _pkce_challenge(code_verifier)
            extra_merged["code_challenge_method"] = "S256"

    _states[token] = OAuthState(
        user_sub=user_sub,
        mcp_name=mcp_name,
        provider_id=provider_id,
        services=list(services or []),
        account_label_hint=account_label_hint,
        mobile=mobile,
        redirect_uri=redirect_uri,
        extra=extra_merged,
        code_verifier=code_verifier,
        mcp_authorization=dict(mcp_authorization) if mcp_authorization else None,
        expiry=time.monotonic() + _STATE_TTL_SECONDS,
    )
    _purge_expired()
    return token


def validate_state(token: str) -> OAuthState | None:
    """Consume a state token. Returns the payload, or None if missing/expired.

    One-shot: a valid token can only be redeemed once. This is the CSRF
    defense — an attacker who steals a state token only gets to use it
    if they beat the legitimate callback to it (and even then they don't
    have the auth code).
    """
    state = _states.pop(token, None)
    if state is None:
        return None
    if time.monotonic() > state.expiry:
        return None
    return state


def peek_state_extra(token: str) -> dict[str, str]:
    """Non-consuming read of a state's ``extra`` dict.

    Used by the API caller immediately after ``create_state`` to merge
    engine-injected URL params (PKCE ``code_challenge`` /
    ``code_challenge_method``) with the caller's own URL extras
    (``include_granted_scopes``, ``prompt=admin_consent``) before passing
    the combined dict to ``provider.build_auth_url``.

    Returns an empty dict if the token is missing or expired. Does NOT
    consume the state — the subsequent ``validate_state`` call (in the
    OAuth callback) is the one-shot redeem.
    """
    state = _states.get(token)
    if state is None:
        return {}
    if time.monotonic() > state.expiry:
        return {}
    return dict(state.extra)


def peek_state_issuer(token: str) -> tuple[str, bool]:
    """Non-consuming read of the issuer a registered-client state named and
    whether that issuer advertises the ``iss`` response parameter, or
    ``("", False)``: the callback checks an error response's ``iss`` against
    it without burning the state."""
    state = _states.get(token)
    if state is None or time.monotonic() > state.expiry or not state.mcp_authorization:
        return "", False
    auth = state.mcp_authorization
    return str(auth.get("issuer") or ""), bool(auth.get("iss_parameter_supported"))


def _purge_expired() -> None:
    now = time.monotonic()
    expired = [k for k, v in _states.items() if v.expiry < now]
    for k in expired:
        _states.pop(k, None)


# ---------------------------------------------------------------------------
# Admin-consent state (Microsoft tenant-wide grant)
# ---------------------------------------------------------------------------
#
# The Microsoft `/{tenant}/v2.0/adminconsent` endpoint performs a tenant-wide
# scope grant. Its callback shape differs from the standard auth-code flow:
# the response is `?admin_consent=True&tenant=<guid>&state=<token>` — NO
# `code`. We use a SEPARATE state store from the auth-code _states so the
# two flows can never collide on one-shot consumption (an admin granting
# tenant consent doesn't burn a user's pending auth-code state).

@dataclass
class AdminConsentState:
    """In-memory state token payload for admin-consent flow."""
    user_sub: str
    mcp_name: str
    provider_id: str
    mobile: bool = False
    expiry: float = 0.0


_admin_consent_states: dict[str, AdminConsentState] = {}


def create_admin_consent_state(
    *,
    user_sub: str,
    mcp_name: str,
    provider_id: str,
    mobile: bool = False,
) -> str:
    """Mint a state token for the admin-consent flow. One-shot, 5min TTL."""
    token = secrets.token_urlsafe(32)
    _admin_consent_states[token] = AdminConsentState(
        user_sub=user_sub,
        mcp_name=mcp_name,
        provider_id=provider_id,
        mobile=mobile,
        expiry=time.monotonic() + _STATE_TTL_SECONDS,
    )
    # Cheap purge while we're here.
    _purge_expired_admin_consent()
    return token


def validate_admin_consent_state(token: str) -> AdminConsentState | None:
    """Consume an admin-consent state token. Returns payload or None."""
    state = _admin_consent_states.pop(token, None)
    if state is None:
        return None
    if time.monotonic() > state.expiry:
        return None
    return state


def _purge_expired_admin_consent() -> None:
    now = time.monotonic()
    expired = [k for k, v in _admin_consent_states.items() if v.expiry < now]
    for k in expired:
        _admin_consent_states.pop(k, None)


# --- diagnostics ---------------------------------------------------------

def active_state_count() -> int:
    """Diagnostic: number of unexpired states currently held in memory."""
    _purge_expired()
    return len(_states)


# --- app credential resolution (manifest-driven) ------------------------

def _resolve_app_credentials(
    oauth: dict[str, Any], creds: dict[str, str],
) -> tuple[str, str]:
    """Extract (client_id, client_secret) from infra credentials.

    Walks the manifest's ``app_credential_fields`` and picks the field
    whose key contains ``CLIENT_ID`` / ``CLIENT_SECRET`` (case-insensitive)
    as the substring. New providers just declare their admin-form field
    keys with those substrings present (e.g. ``GOOGLE_OAUTH_CLIENT_ID``,
    ``SLACK_CLIENT_ID``, ``LINEAR_CLIENT_ID``); this resolver finds the
    right two automatically.
    """
    client_id = ""
    client_secret = ""
    for field_def in oauth.get("app_credential_fields", []) or []:
        key = field_def.get("key", "")
        key_u = key.upper()
        val = creds.get(key, "")
        if not val:
            continue
        if "CLIENT_SECRET" in key_u and not client_secret:
            client_secret = val
        elif "CLIENT_ID" in key_u and not client_id:
            client_id = val
    return client_id, client_secret


# --- code-exchange orchestration ----------------------------------------

@dataclass
class ExchangeResult:
    """Returned by ``do_oauth_exchange`` to the API layer."""
    email: str
    name: str
    account_label: str
    state: OAuthState


async def do_oauth_exchange(*, code: str, state_token: str, iss: str = "") -> ExchangeResult:
    """Drive the full exchange + persist flow.

    Steps:
      1. ``validate_state`` to recover provider_id, user_sub, services, etc.
      2. ``provider.exchange_code`` to swap code → tokens.
      3. ``provider.fetch_userinfo`` to derive the account's identity.
      4. Persist token + account row + credential metadata (delegates to
         ``services/oauth_account_store.persist_oauth_account``).

    Raises:
        RuntimeError on invalid/expired state, vendor exchange failure,
        or missing identity from userinfo.
    """
    state = validate_state(state_token)
    if state is None:
        raise RuntimeError("Invalid or expired OAuth state")

    # Provider + app credentials
    from auth.oauth_providers import get_provider
    from storage.identity import credential_store
    from services.billing import relay_client
    from services.mcp import mcp_registry

    provider = get_provider(state.provider_id)

    manifest = mcp_registry.get_manifest(state.mcp_name)
    if manifest is None or not manifest.credentials.oauth:
        raise RuntimeError(
            f"Manifest '{state.mcp_name}' has no oauth credential block"
        )

    if state.mcp_authorization:
        return await _exchange_registered_client(
            state, code=code, iss=iss, provider=provider, manifest=manifest,
        )

    if relay_client.hosted_oauth_active(state.mcp_name, manifest):
        # HOSTED: the relay performed the code→token exchange with OtoDock's
        # client_secret and returns only the user's own tokens. No OtoDock
        # secret ever touches this install — client_id/client_secret/token_url
        # are persisted empty, and token_set.raw carries {"via_relay": True}
        # (which lands in the token file's `extra` so the refresh worker uses
        # the relay refresh arm). Stub raises RelayNotConfigured until built.
        token_set = await relay_client.oauth_exchange(
            provider_id=state.provider_id,
            code=code,
            redirect_uri=state.redirect_uri,
            code_verifier=state.code_verifier or None,
        )
        # The relay's TokenSet.raw is the vendor's response verbatim (plus
        # the via_relay marker) — re-run the provider's normalizer over it so
        # provider-specific flattening (Slack team_id/user_id → extra.*)
        # happens exactly as on the self-managed path. For standard vendors
        # this is a lossless rebuild; via_relay rides along inside raw.
        if token_set.raw:
            token_set = provider.normalize_token_response(token_set.raw)
        client_id = ""
        client_secret = ""
        token_url = ""
    else:
        # SELF-MANAGED: app credentials live in infra_credentials keyed by the
        # manifest's `credentials.oauth.app_credential` slug. Walk the declared
        # `app_credential_fields` to find the CLIENT_ID / CLIENT_SECRET keys.
        app_cred_name = manifest.credentials.oauth.get("app_credential", "")
        creds = credential_store.get_infra_credentials(app_cred_name) if app_cred_name else {}
        client_id, client_secret = _resolve_app_credentials(
            manifest.credentials.oauth, creds,
        )
        if not client_id or not client_secret:
            raise RuntimeError(
                f"OAuth app credentials for '{app_cred_name}' are not configured"
            )
        token_set = await provider.exchange_code(
            code=code,
            redirect_uri=state.redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=state.code_verifier or None,
        )
        token_url = provider.token_url

    # Identity probe uses the user's own access token — no OtoDock secret
    # needed, so it works the same for hosted and self-managed.
    userinfo = await provider.fetch_userinfo(access_token=token_set.access_token)
    if not userinfo.email:
        # Some providers (GitHub) omit email when the user keeps it private.
        # Fall back to the stable id (GitHub `login`) / name so the account has a
        # consistent identity — mirrors the PAT path. Require something stable.
        userinfo.email = userinfo.account_id or userinfo.name
        if not userinfo.email:
            raise RuntimeError(
                f"{state.provider_id}: userinfo returned no email/id/name "
                "— cannot derive account label"
            )

    # Account label resolution:
    #   - If state carries an explicit hint (e.g. user typed "work"), use it
    #     verbatim (rare; v1 always auto-labels by email).
    #   - Otherwise default to userinfo.email (login-backed for GitHub privacy).
    account_label = state.account_label_hint.strip() or userinfo.email

    # Persist — writes the user_credential_accounts row, the credential
    # rows, and the on-disk token file as one orchestrated operation, under
    # the account lock the refresh worker holds across its own rewrite.
    from core.credentials import credential_locks
    from services.oauth import oauth_account_store
    async with credential_locks.get_lock(state.user_sub, state.provider_id, account_label):
        await asyncio.to_thread(
            oauth_account_store.persist_oauth_account,
            user_sub=state.user_sub,
            mcp_name=state.mcp_name,
            provider_id=state.provider_id,
            account_label=account_label,
            services=state.services,
            token_set=token_set,
            userinfo=userinfo,
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
        )

    return ExchangeResult(
        email=userinfo.email,
        name=userinfo.name,
        account_label=account_label,
        state=state,
    )


async def _exchange_registered_client(
    state: OAuthState, *, code: str, iss: str, provider, manifest,
) -> ExchangeResult:
    """The exchange of a connect through the client this install registered
    at the MCP server's own authorization server (``state.mcp_authorization``).

    The registration row is re-read and must be live and carry the client id
    and issuer the state names (a code for another registration is
    refused); an ``iss`` on the callback must name the state's issuer and
    must be present when the issuer advertises it (RFC 9207). The identity
    comes from a userinfo probe only when the manifest's ``userinfo_url``
    sits on the resource host; else from the token response's declared
    fields, the typed label, or the provider id. The persist runs under the
    account lock; when it fails after the exchange the grant is revoked.
    """
    from urllib.parse import urlsplit

    from auth.oauth_providers.base import OAuthTokenError
    from core.credentials import credential_locks
    from services.oauth import mcp_authorization, oauth_account_store
    from storage.identity import oauth_client_registrations as regs
    from storage.pg import run_db

    auth = state.mcp_authorization or {}
    issuer = str(auth.get("issuer") or "")
    if iss and iss.rstrip("/") != issuer.rstrip("/"):
        raise RuntimeError("The authorization response names another issuer")
    if not iss and auth.get("iss_parameter_supported"):
        raise RuntimeError("The authorization response carries no issuer")
    row = await run_db(regs.get, int(auth.get("registration_id") or 0))
    if (
        row is None or row.get("revoked_at")
        or row.get("client_id") != auth.get("client_id")
        or row.get("issuer") != issuer
    ):
        raise RuntimeError("The client registration this sign-in used is no longer valid")
    method = str(row.get("token_endpoint_auth_method") or "none")
    secret = ""
    if method != "none":
        secret = await run_db(regs.client_secret, row["id"])
        if secret is None:
            raise RuntimeError("The client registration's secret cannot be read")
    try:
        token_set = await mcp_authorization.exchange_code(
            state.provider_id,
            token_endpoint=str(auth.get("token_endpoint") or ""),
            code=code, redirect_uri=state.redirect_uri,
            code_verifier=state.code_verifier, resource=str(auth.get("resource") or ""),
            method=method, client_id=str(row["client_id"]), client_secret=secret,
        )
    except OAuthTokenError as exc:
        if exc.code == "invalid_client":
            await run_db(regs.revoke, row["id"], "vendor_revoked")
        raise

    block = (manifest.credentials.oauth or {}).get("authorization_server") or {}
    resource_host = (urlsplit(str(auth.get("resource") or "")).hostname or "").lower()
    userinfo_parts = urlsplit(provider.userinfo_url or "")
    userinfo_host = (userinfo_parts.hostname or "").lower()

    async def _revoke_grant() -> None:
        await mcp_authorization.revoke(
            str(auth.get("revocation_endpoint") or ""), issuer=issuer,
            token=token_set.refresh_token or token_set.access_token,
            method=method, client_id=str(row["client_id"]), client_secret=secret,
        )

    try:
        # The identity probe goes to the resource host over https or not at
        # all: a token issued for the MCP server reaches nothing else.
        if provider.userinfo_url and userinfo_parts.scheme == "https" and userinfo_host == resource_host:
            userinfo = await provider.fetch_userinfo(access_token=token_set.access_token)
            if not userinfo.email:
                userinfo.email = userinfo.account_id or userinfo.name
            account_label = state.account_label_hint.strip() or userinfo.email or state.provider_id
            if not userinfo.email:
                userinfo.email = account_label
        else:
            account_label, userinfo = mcp_authorization.identity_from_token_response(
                token_set.raw, block, provider_id=state.provider_id,
                label_hint=state.account_label_hint,
            )
        oauth_account_store.validate_account_label(account_label)
    except Exception:
        await _revoke_grant()
        raise

    token_set.raw = {
        **token_set.raw,
        "flow": oauth_account_store.MCP_AUTHORIZATION_FLOW,
        "registration_id": row["id"],
        "issuer": issuer,
        "resource": str(auth.get("resource") or ""),
        "revocation_endpoint": str(auth.get("revocation_endpoint") or ""),
        "token_endpoint_auth_method": method,
    }
    async with credential_locks.get_lock(state.user_sub, state.provider_id, account_label):
        try:
            await asyncio.to_thread(
                oauth_account_store.persist_oauth_account,
                user_sub=state.user_sub,
                mcp_name=state.mcp_name,
                provider_id=state.provider_id,
                account_label=account_label,
                services=state.services,
                token_set=token_set,
                userinfo=userinfo,
                client_id=str(row["client_id"]),
                client_secret="",
                token_url=str(auth.get("token_endpoint") or ""),
            )
        except Exception:
            await _revoke_grant()
            raise
    await run_db(regs.touch, row["id"])
    return ExchangeResult(
        email=userinfo.email, name=userinfo.name, account_label=account_label, state=state,
    )
