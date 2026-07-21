"""Client seam to the OtoDock commercial relay (``api.otodock.io``).

LOCKED ARCHITECTURE. OtoDock's hosted OAuth ``client_secret`` and
vendor API keys live **only in the relay** — never in any install, self-hosted
*or* OtoDock-cloud. Every hosted operation routes through here; the relay holds
the secret, performs the privileged call, and returns only the user's own
tokens / data. This is the single chokepoint to the relay — nothing else in the
app talks to ``api.otodock.io`` directly.

The relay + website are NOT built yet. Until this install is connected (not
``OTODOCK_AIR_GAPPED``) **and** ``OTODOCK_RELAY_BASE`` is set, every network
method raises :class:`RelayNotConfigured` so the API layer surfaces a clean
"Hosted via OtoDock isn't available yet" message. When the relay ships we
implement the HTTP bodies here (against the contract documented per-method) —
no other app changes. End-to-end testing waits for the relay.

RELAY ACCESS ≠ PAID LICENSE. Being connected (not ``OTODOCK_AIR_GAPPED``) and
NOT holding an ``offline_term`` key means "this install may talk to the relay."
Hosted OAuth is FREE for every TIER (community→enterprise) — near-zero cost; the
relay only brokers the handshake/refresh, API calls go user→vendor with the
user's own token. The one exception is the offline MODE: an ``offline_term``
(hand-issued enterprise / air-gapped) license gets NO relay at all — hosted OAuth
and ``api_key_relay`` are disabled (see :func:`relay_offered` /
:func:`license_allows_relay`). ``api_key_relay`` is additionally credit-gated.
The paid seat tier is the separate signed ``license_key`` (``auth/license.py``),
enforced LOCALLY and independent of the relay.

Two-level gating:
  * :func:`is_available` — connected (:func:`relay_offered`) AND a relay base is
    configured. The runtime (``mcp_registry`` env injection, refresh worker)
    checks this and falls back to clean sentinels when False.
  * ``hosted_oauth_active`` — gates on :func:`relay_offered` alone, so a
    connected-but-relay-unbuilt install still ROUTES a hosted connect into this
    seam and gets the explicit "not available yet" error (rather than silently
    falling back to self-managed and demanding an app credential it shouldn't).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

import config

logger = logging.getLogger("claude-proxy.relay-client")


class RelayNotConfigured(RuntimeError):
    """A hosted operation was attempted but the relay isn't available yet.

    Either the install is ``OTODOCK_AIR_GAPPED`` or ``OTODOCK_RELAY_BASE`` is
    unset (the relay service is not built yet).
    """


class RelayError(RuntimeError):
    """The relay rejected a request. ``code`` is the relay's machine-readable
    reason (e.g. ``license_invalid`` / ``seat_exceeded`` / ``no_credit``)."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


# Relay rejection codes → clear admin/user-facing messages. When the
# relay returns one of these, callers surface the mapped text instead of a raw
# 4xx so the operator knows exactly what to fix.
RELAY_ERROR_MESSAGES = {
    "license_invalid": (
        "Your OtoDock license is invalid or expired. Renew it in "
        "Settings → Platform to keep using hosted features."
    ),
    "seat_exceeded": (
        "Your plan's user limit has been reached. Upgrade your OtoDock plan to "
        "add more seats."
    ),
    "no_credit": (
        "Out of OtoDock credits. Add credits at otodock.io/dashboard/credits to "
        "keep using this hosted feature."
    ),
    "not_connected": (
        "This install isn't connected to an OtoDock account. Connect it in "
        "Settings → Platform to use hosted credits."
    ),
    "token_install_mismatch": (
        "This install's OtoDock connection is invalid — reconnect it in "
        "Settings → Platform."
    ),
    "link_revoked": (
        "This install was disconnected from its OtoDock account — reconnect it in "
        "Settings → Platform."
    ),
    "model_not_allowed": (
        "This model isn't available on the OtoDock hosted relay — pick a hosted "
        "model, or use your own API key (BYO) for it."
    ),
    "activation_limit_reached": (
        "This OtoDock license is already active on another install — deactivate "
        "it there first, or use 'Move license'."
    ),
    "invalid_grant": (
        "The vendor revoked this account's authorization — reconnect the "
        "account in Settings → Integrations."
    ),
    "events_provider_unsupported": (
        "Hosted event delivery isn't available for this integration yet — "
        "vendor-console (manual) webhook setup still works."
    ),
    "bad_events_url": (
        "The OtoDock relay rejected this install's events URL — "
        "DASHBOARD_PUBLIC_URL must be a publicly reachable https address."
    ),
}


def relay_error_message(code: str, default: str = "") -> str:
    """Map a relay rejection code to a user-facing message."""
    return RELAY_ERROR_MESSAGES.get(code, default or f"OtoDock relay error: {code}")


# Where the install stores the relay's per-install event forward secret
# (infra_credentials bundle slug + key). Synthetic slug — never collides with
# manifest-driven credential bundles and stays invisible to the manifest-
# driven admin credentials UI.
EVENTS_FORWARD_SECRET_SLUG = "otodock-relay"
EVENTS_FORWARD_SECRET_KEY = "EVENTS_FORWARD_SECRET"

# The per-install OtoDock account-connect token lives in the SAME synthetic bundle
# under a distinct key. It authenticates the install to its OtoDock account for the
# API/credit relay (mint + credits) — established by a paid license's activation
# (auto-link) or the browser connect handshake. Distinct from the OAuth-vendor
# tokens in services/oauth/oauth_account_store.py.
ACCOUNT_TOKEN_KEY = "account_token"


def get_account_token() -> str:
    """The per-install OtoDock account-connect token, or '' if not connected."""
    from storage import credential_store

    return (credential_store.get_infra_credentials(EVENTS_FORWARD_SECRET_SLUG) or {}).get(
        ACCOUNT_TOKEN_KEY, "",
    )


def store_account_token(token: str) -> None:
    """Persist the account-connect token (per-key upsert — leaves the event-forward
    secret in the shared bundle untouched)."""
    from storage import credential_store

    credential_store.set_infra_credentials(
        EVENTS_FORWARD_SECRET_SLUG, {ACCOUNT_TOKEN_KEY: token},
    )


def clear_account_token() -> None:
    """Drop ONLY the account-connect token (not the whole otodock-relay bundle)."""
    from storage import credential_store

    credential_store.delete_infra_credential_key(
        EVENTS_FORWARD_SECRET_SLUG, ACCOUNT_TOKEN_KEY,
    )


def is_connected() -> bool:
    """True iff this install holds an OtoDock account-connect token (the API/credit
    relay needs it to resolve the billed account)."""
    return bool(get_account_token())


def license_allows_relay() -> bool:
    """False iff an OFFLINE (``offline_term``) license key is installed.

    Offline licenses get NO relay at all — no hosted OAuth, no ``api_key_relay``,
    no hosted Direct-LLM. An offline / air-gapped enterprise self-manages every
    credential. No key (community) and ``subscription`` / ``lifetime`` keys all
    permit the relay: hosted OAuth stays free for every TIER, the restriction is
    on the offline MODE only, and the mode is signed so a config flag can't
    re-enable it.

    Reads the raw signed key and verifies it directly via
    ``auth.license.validate_license_key`` (which performs NO relay call) — NOT
    ``get_current_license``, which would recurse back through ``is_available``.
    Fail-open: a missing/unverifiable key (incl. the placeholder-key era) or any
    DB hiccup is treated as community → relay permitted (the relay re-verifies
    the license server-side and rejects an ``offline_term`` key there too).
    """
    try:
        import auth.license as L

        key = L.get_license_key()
        if not key:
            return True
        lic = L.validate_license_key(key)
        if lic is None:
            return True
        # `lifetime` overrides the mode; only a non-lifetime offline_term blocks.
        return lic.lifetime or lic.license_mode != "offline_term"
    except Exception:
        return True


def relay_offered() -> bool:
    """True iff this install talks to OtoDock at all (relay + license server).

    Two gates, both required:

    * **Connectivity** — ``OTODOCK_AIR_GAPPED`` is a no-outbound switch;
      ``OTODOCK_CLOUD`` always forces connectivity (the control plane manages it).
    * **License mode** — an ``offline_term`` license gets NO relay
      (:func:`license_allows_relay`). Offline = self-managed only; the signed
      ``license_mode`` decides this, so a config flag can't re-enable it.

    An ``offline_term`` install therefore makes zero outbound to OtoDock (it
    never phones home for the license either), so the dashboard ``air_gapped``
    flag (``not relay_offered()``) correctly hides every hosted feature.
    """
    if not ((not config.OTODOCK_AIR_GAPPED) or config.OTODOCK_CLOUD):
        return False
    return license_allows_relay()


def is_available() -> bool:
    """True iff this install talks to OtoDock AND a relay base is configured."""
    return bool(relay_offered() and config.OTODOCK_RELAY_BASE)


def _require_relay() -> None:
    if not is_available():
        raise RelayNotConfigured(
            "Hosted via OtoDock isn't available yet — the OtoDock relay is not "
            "configured for this install."
        )


def api_relay_enabled() -> bool:
    """The admin's master 'use OtoDock hosted relay' toggle (opt-in, default off).

    Gates the 'Hosted by OtoDock' system MCP instances + the hosted Direct-LLM
    providers. A managed (cloud) install can force it via ``OTODOCK_FORCED_SETTINGS``.
    """
    from storage import database as db

    return db.get_platform_setting("otodock_api_relay_enabled") == "1"


def system_relay_active() -> bool:
    """True iff the hosted relay is usable AND turned on AND connected:
    :func:`relay_offered` AND the master toggle AND an ``account_token``.

    Requiring the token (not just the toggle) avoids orphaned system instances when
    the toggle is force-on (cloud) but the install never connected. This is the gate
    for system MCP instance creation + hosted Direct-LLM availability.
    """
    return relay_offered() and api_relay_enabled() and is_connected()


# ---------------------------------------------------------------------------
# Install identity — every relay call carries these so the relay can
# enforce license / seat / credit server-side.
# ---------------------------------------------------------------------------

def get_install_id() -> str:
    """Stable per-install identifier, generated once and persisted.

    Sent (with the license) on every relay call so the relay can tie usage +
    enforcement to this install. Not a secret — it's an opaque correlation id.
    """
    import uuid
    from storage import database as db

    iid = db.get_platform_setting("install_id")
    if not iid:
        iid = uuid.uuid4().hex
        db.set_platform_setting("install_id", iid)
    return iid


def _relay_identity() -> dict:
    """Identity payload included with every relay request: the install id + the
    signed license key (the relay verifies the license and enforces seat/credit).
    The real client methods attach this once the relay ships."""
    import auth.license as L

    return {
        "install_id": get_install_id(),
        "license": L.get_license_key(),
    }


def _link_identity() -> dict:
    """Identity for the API/credit relay (mint + credits): install id + the
    per-install ``account_token`` — NO license (the relay resolves the billed
    account from the install_link). Distinct from :func:`_relay_identity` (license-
    based), used by license activate/check, OAuth, push, and events."""
    return {"install_id": get_install_id(), "account_token": get_account_token()}


# ---------------------------------------------------------------------------
# License activation + liveness — subscription keys bind once + check in.
# Each attaches _relay_identity() and POSTs to the commercial license server
# (``OTODOCK_RELAY_BASE`` = ``api.otodock.io``). The oauth_* / mint_session_token
# methods further below are stubs — the hosted OAuth/API relay is not live yet.
# ---------------------------------------------------------------------------

# License calls are tiny + infrequent (one-time activate, weekly check); a short
# timeout keeps the worker responsive and lets callers fail-open on a slow relay.
_RELAY_TIMEOUT_SECONDS = 15.0


async def _relay_post(path: str, payload: dict) -> dict:
    """POST ``payload`` to ``{OTODOCK_RELAY_BASE}{path}`` → parsed JSON.

    A 4xx ``{"detail": "<code>"}`` becomes ``RelayError(code)`` — the relay's
    stable machine codes (see :data:`RELAY_ERROR_MESSAGES`), a deterministic
    rejection the caller acts on (e.g. ``activation_limit_reached``). 5xx /
    network / timeout raise (httpx errors) so callers FAIL-OPEN rather than
    downgrade a paying customer on a transient outage. :func:`_require_relay`
    must gate the call first (guarantees a configured base).
    """
    import httpx

    base = config.OTODOCK_RELAY_BASE.rstrip("/")
    async with httpx.AsyncClient(timeout=_RELAY_TIMEOUT_SECONDS) as client:
        resp = await client.post(f"{base}{path}", json=payload)
    if 400 <= resp.status_code < 500:
        code = ""
        with contextlib.suppress(Exception):
            code = (resp.json() or {}).get("detail", "")
        raise RelayError(code or f"http_{resp.status_code}")
    resp.raise_for_status()  # 5xx → httpx.HTTPStatusError → caller fails open
    return resp.json()


async def activate_license(license_key: str) -> str:
    """Bind ``license_key`` to this install (subscription / lifetime keys).

    The relay binds ``key → install_id`` enforcing the key's ``activation_limit``
    (stops one key powering unlimited installs) and returns a **relay-signed**
    activation receipt — same Ed25519 key + ``<payload>.<sig>`` envelope as a
    license key — which ``auth/license`` caches and later verifies offline (must
    bind to this key + install_id). Returns the receipt token string — its signed
    payload carries the **current** ``license_key`` (the relay resolves a superseded
    key it was called with), so the worker can adopt a re-issued key from it. A maxed
    key raises ``RelayError("activation_limit_reached")``.
    """
    _require_relay()
    data = await _relay_post("/v1/licenses/activate", _relay_identity())
    # The relay auto-links this install to the license's account for the API/credit
    # relay and returns a per-install account_token ONCE — store it so a paid admin
    # is connected without the browser handshake. (Using hosted credits still needs
    # the admin to flip the master 'OtoDock hosted relay' toggle.)
    token = data.get("account_token") or ""
    if token:
        store_account_token(token)
    return data.get("receipt", "")


async def license_check(license_key: str) -> dict:
    """Periodic liveness check for an activated subscription key.

    Sends ``_relay_identity()``; the relay returns a status (``active`` /
    ``past_due`` / ``canceled`` — payment-grace/dunning is a server concern) plus
    the **current** signed ``license`` key and a fresh ``receipt`` over it. When the
    relay has re-issued the key (expiry refresh / plan change) the returned
    ``license`` differs from the install's stored key, so the worker **adopts** it
    (+ the receipt) — no re-paste. Returns ``{"status", "license", "receipt"}``.
    **Fail-open** at the call site — an unreachable relay must never downgrade a
    paying customer (the client's unreachable-grace window handles outages).
    """
    _require_relay()
    data = await _relay_post("/v1/licenses/check", _relay_identity())
    return {
        "status": data.get("status", ""),
        "license": data.get("license", ""),
        "receipt": data.get("receipt", ""),
    }


async def deactivate_license(license_key: str) -> None:
    """Release this install's binding for ``license_key`` (admin "Move license",
    or on key change/clear). Frees an ``activation_limit`` slot so the key can be
    activated on another install. Best-effort — both callers wrap this in
    ``try/except``, so a relay rejection / outage is non-fatal.
    """
    _require_relay()
    await _relay_post("/v1/licenses/deactivate", _relay_identity())


async def relay_credits(user_sub: str) -> dict:
    """Fetch the calling user's account credit balance from the relay.

    Returns the OSS ``GET /v1/user/credits`` shape (``balance_usd`` /
    ``balance_eur_approx`` / ``low_threshold`` / ``recent_transactions``). Gated by
    :func:`is_available`; the caller (``api/billing/billing.py``) falls back to the
    zero-balance stub when the relay isn't configured or errors.
    """
    _require_relay()
    if not is_connected():
        # Not connected → no account to bill; return the zero-balance stub rather
        # than a doomed relay call (the caller shows zero / hides credits).
        return {"balance_usd": 0.0, "balance_eur_approx": 0.0,
                "low_threshold": 0.0, "recent_transactions": []}
    return await _relay_post(
        "/v1/relay/credits", {**_link_identity(), "user_sub": user_sub or ""},
    )


# ---------------------------------------------------------------------------
# Push relay — native mobile push (FCM, Android + iOS) brokered by the relay.
# The relay holds OtoDock's FCM service account; the install ships only the
# Firebase *client* config. Web Push (VAPID) stays LOCAL (push_sender).
# ---------------------------------------------------------------------------

async def push_send(*, platform: str, device_token: str, payload: dict) -> dict:
    """Send a native mobile push through the relay (FCM — Android + iOS).

    The relay injects OtoDock's FCM service account (a server secret that never
    ships in any install) and forwards the data-only message. Push is free for
    every tier; an ``offline_term`` / air-gapped install is relay-excluded
    (``_require_relay`` / the relay's offline gate). Raises ``RelayError`` on a
    relay rejection — notably ``token_invalid`` (the device token is no longer
    registered → the caller drops the subscription). **Web Push stays local and
    never routes here.**
    """
    _require_relay()
    return await _relay_post("/v1/push/send", {
        **_relay_identity(),
        "platform": platform,
        "device_token": device_token,
        "payload": payload or {},
    })


# ---------------------------------------------------------------------------
# Hosted-relay flag helper (manifest-driven, flow-aware)
# ---------------------------------------------------------------------------

def hosted_oauth_active(mcp_name: str, manifest, flow: str = "") -> bool:
    """True iff this MCP's OAuth should route through the relay for ``flow``.

    Replaces the deleted ``resolve_app_cred_slug`` — there is no local
    platform credential slug anymore; hosted OAuth is "route through the
    relay" or nothing.

    Flow-aware: per-flow ``app_credential_variants`` (e.g. Zoom S2S
    ``client_credentials``) are NEVER hosted — only the default user-OAuth
    auth-code flow is. Reads the admin's ``_hosted_service_mode`` override,
    else the manifest ``oauth_app.default_mode``. Gates on :func:`relay_offered`
    (NOT :func:`is_available`) so a connected install whose relay isn't live yet
    still routes here and surfaces the explicit "not available" error.
    """
    from storage import mcp_store

    oa = manifest.hosted.oauth_app if manifest.hosted else None
    if not (oa and oa.available):
        return False
    oauth_block = manifest.credentials.oauth or {}
    variants = oauth_block.get("app_credential_variants") or {}
    if flow and flow in variants:
        return False
    mode = (
        mcp_store.get_mcp_config_value(mcp_name, "_hosted_service_mode")
        or oa.default_mode
    )
    return mode == "hosted" and relay_offered()


# ---------------------------------------------------------------------------
# Hosted OAuth (relay holds OtoDock's client_secret).
# The relay is the OAuth redirect target and does the code→token exchange with
# OtoDock's client_secret; these post the install identity + per-call args and
# parse the relay's response (a TokenSet for exchange/refresh).
# ---------------------------------------------------------------------------

def _tokenset_from(data: dict):
    """Build the vendor-neutral TokenSet from the relay's JSON response."""
    from auth.oauth_providers.base import TokenSet

    return TokenSet(
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token", "") or "",
        # Absent expires_in = never expires; 0 is the never-expires sentinel.
        expires_in=int(data.get("expires_in") or 0),
        scope=data.get("scope", "") or "",
        token_type=data.get("token_type", "Bearer") or "Bearer",
        raw=data.get("raw") or {},
    )


async def oauth_authorize_url(
    *, provider_id: str, scopes: list[str], state: str, install_callback: str,
    user_sub: str = "",
) -> str:
    """Return the vendor consent URL for a hosted OAuth connect.

    The relay builds the vendor authorize URL with OtoDock's ``client_id`` and
    the *relay's* redirect URI, remembering ``state → install_callback``. After
    the user consents the relay does the code→token exchange server-side and
    bounces a one-time handle back to ``install_callback`` (redeemed by
    :func:`oauth_exchange`).

    ``user_sub`` (the connecting user) lets the relay enforce the license's seat
    cap: a new over-cap user is refused with
    ``RelayError("seat_exceeded")``.
    """
    _require_relay()
    data = await _relay_post("/v1/oauth/authorize-url", {
        **_relay_identity(),
        "provider_id": provider_id,
        "scopes": scopes,
        "state": state,
        "install_callback": install_callback,
        "user_sub": user_sub,
    })
    return data.get("url", "")


async def oauth_exchange(
    *, provider_id: str, code: str, redirect_uri: str, code_verifier: str | None,
):
    """Redeem the relay handle (``code``) for the user's tokens. Returns a
    ``TokenSet`` whose ``.raw`` carries ``{"via_relay": True}`` (the marker lands
    in the token file's ``extra`` so refreshes route back through the relay; no
    ``client_secret`` is ever persisted)."""
    _require_relay()
    data = await _relay_post("/v1/oauth/exchange", {
        **_relay_identity(), "provider_id": provider_id, "code": code,
    })
    return _tokenset_from(data)


async def oauth_refresh(*, provider_id: str, refresh_token: str):
    """Refresh a hosted token via the relay (relay holds the secret). Returns a
    ``TokenSet`` with ``.raw`` preserving ``{"via_relay": True}``."""
    _require_relay()
    data = await _relay_post("/v1/oauth/refresh", {
        **_relay_identity(), "provider_id": provider_id,
        "refresh_token": refresh_token,
    })
    return _tokenset_from(data)


async def oauth_revoke(*, provider_id: str, token: str) -> None:
    """Revoke a hosted token at the vendor via the relay (relay holds the
    secret). Best-effort — callers wrap this in try/except and proceed with local
    cleanup regardless."""
    _require_relay()
    await _relay_post("/v1/oauth/revoke", {
        **_relay_identity(), "provider_id": provider_id, "token": token,
    })


async def events_register(
    *, provider_id: str, events_url: str, enabled: bool = True,
    rotate_secret: bool = False,
) -> dict:
    """Register/enable (or disable) hosted event forwarding for this install.

    The relay forwards vendor events for the (workspace, user) bindings it
    observed at OAuth exchange to ``events_url``, re-signed with the install's
    forward secret. The response's ``forward_secret`` is non-null ONLY when
    freshly minted or rotated — callers send ``rotate_secret=True`` whenever
    no local secret exists (first registration / DB restore). Raises
    ``RelayError('events_provider_unsupported')`` when the relay has no event
    support for the provider (callers fall back to the manifest's own mode).
    """
    _require_relay()
    return await _relay_post("/v1/events/register", {
        **_relay_identity(),
        "provider_id": provider_id,
        "events_url": events_url,
        "enabled": enabled,
        "rotate_secret": rotate_secret,
    })


# ---------------------------------------------------------------------------
# Account connect (API/credit relay) — bind this install to an OtoDock account.
# A license-less install authenticates via the browser handshake; a paid install
# auto-links at activation (see activate_license). The resulting per-install
# account_token is what mint/credits present.
# ---------------------------------------------------------------------------

async def account_connect_authorize_url(*, state: str, install_callback: str) -> dict:
    """Start the connect handshake. Returns ``{"url", "pairing_code"}`` — the
    install opens ``url`` in a browser and DISPLAYS ``pairing_code`` (the admin
    types it on the consent page, proving the connect started from their own
    install). Carries no license/account_token — this is how an unconnected install
    authenticates."""
    _require_relay()
    return await _relay_post("/v1/connect/authorize-url", {
        "install_id": get_install_id(), "state": state,
        "install_callback": install_callback,
    })


async def account_connect_exchange(*, code: str) -> str:
    """Redeem the one-time connect handle for this install's ``account_token`` and
    persist it. Returns the token ('' on failure)."""
    _require_relay()
    data = await _relay_post("/v1/connect/exchange", {
        "install_id": get_install_id(), "code": code,
    })
    token = data.get("account_token", "")
    if token:
        store_account_token(token)
    return token


async def account_disconnect() -> None:
    """Disconnect this install from its OtoDock account: best-effort revoke the link
    at the relay (token-authed), then ALWAYS clear the local token (in ``finally``)
    so the install stops using the relay even if the relay is unreachable. Does NOT
    require the relay — a local disconnect must always succeed."""
    token = get_account_token()
    try:
        if token and is_available():
            await _relay_post("/v1/connect/disconnect", {
                "install_id": get_install_id(), "account_token": token,
            })
    except Exception:
        logger.warning("relay link revoke failed; clearing local token anyway", exc_info=True)
    finally:
        clear_account_token()


# ---------------------------------------------------------------------------
# Paid api_key_relay session token.
# ---------------------------------------------------------------------------

# A minted token is bound to (install_id, user_sub, account_token) and lives ~24h on
# the relay; cache it per (user_sub, account_token) and reuse for a window well under
# that so we don't mint on every (per-session) config build. Keyed on the
# account_token, so reconnecting / rotating it naturally invalidates the cache.
_SESSION_TOKEN_TTL_SECONDS = 12 * 3600
_session_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_session_token_lock = threading.Lock()


def mint_session_token(user_sub: str) -> str:
    """Mint a per-user relay token for paid ``api_key_relay`` MCP calls.

    Injected (with ``OTODOCK_RELAY_BASE``) into the MCP subprocess env so the MCP
    calls the relay instead of the vendor; the relay validates the token + license
    + the calling user's credit, appends the vendor key, meters, and proxies.
    Per-USER (the session user) so metering debits the right balance. **Sync**
    (called from the sync config builder), so it uses a sync HTTP client + an
    in-process TTL cache.

    Any failure (relay rejection, network, no token) raises
    :class:`RelayNotConfigured` — the only exception the config builder catches —
    so the MCP gets a clean ``OTODOCK_RELAY_ERROR`` (a mapped message for known
    relay codes) instead of a crashed session build.
    """
    _require_relay()
    if not is_connected():
        raise RelayNotConfigured(relay_error_message("not_connected"))
    identity = _link_identity()
    cache_key = (user_sub or "", identity["account_token"])
    now = time.monotonic()
    with _session_token_lock:
        hit = _session_token_cache.get(cache_key)
        if hit and hit[1] > now:
            return hit[0]

    try:
        import httpx

        base = config.OTODOCK_RELAY_BASE.rstrip("/")
        with httpx.Client(timeout=_RELAY_TIMEOUT_SECONDS) as client:
            resp = client.post(
                f"{base}/v1/relay/session/mint",
                json={**identity, "user_sub": user_sub or ""},
            )
        if 400 <= resp.status_code < 500:
            code = ""
            with contextlib.suppress(Exception):
                code = (resp.json() or {}).get("detail", "")
            raise RelayError(code or f"http_{resp.status_code}")
        resp.raise_for_status()
        token = (resp.json() or {}).get("token", "")
    except RelayError as e:
        raise RelayNotConfigured(relay_error_message(e.code))
    except Exception:
        raise RelayNotConfigured(
            "OtoDock hosted relay is temporarily unavailable — try again shortly."
        )
    if not token:
        raise RelayNotConfigured("OtoDock hosted relay returned no session token.")

    with _session_token_lock:
        _session_token_cache[cache_key] = (token, now + _SESSION_TOKEN_TTL_SECONDS)
    return token
