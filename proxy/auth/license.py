"""License-key validation + deployment-aware enforcement.

Offline Ed25519 signature verification against OtoDock's PUBLIC key (safe to
bake into the platform — public keys aren't secret). The license server (key
signing + activation/dunning ledger), the relay, and the credit ledger are
commercial and out of scope.

Two deployment models (selected by `config.OTODOCK_CLOUD`):

* SELF-HOSTED — community (no license) = 5 users / unlimited agents; licensed
  Pro 15 / Team 50 / Business 100 / Enterprise ∞. On expiry, a **two-stage
  graceful downgrade**: 0-30 days past → block new users; 30+ days → also block
  new agents. Existing users + agents + automations always keep working.
* CLOUD — tier enforced by OtoDock's control plane (no customer-held key). Free
  = 1 user / 1 agent. A control-plane-provisioned license raises the cap.

The baked public key is the commercial keyserver's published key, with `kid`
support for rotation (see `_TRUSTED_PUBLIC_KEYS`); tests monkeypatch
`_LICENSE_PUBLIC_KEY_B64` with a test key and sign test licenses, so this module
is fully testable.

────────────────────────────────────────────────────────────────────────────
DESIGN OF RECORD — subscription licensing & air-gapped mode
────────────────────────────────────────────────────────────────────────────
This file implements the offline foundation (signature + entitlement +
expiry-grace) plus the two-mode subscription model on top:

* **Anti-share invariant (the spine).** Paid seats unlock by EITHER (a) a
  successful relay *binding* — `subscription` keys, self-serve; the relay binds
  key→`install_id` and enforces `activation_limit` — OR (b) the *signature alone*
  — `offline_term` keys, hand-issued to enterprises. A `subscription` key that
  never bound runs at the community cap, never its paid cap. NO config flag can
  convert (a)→(b): `license_mode` lives inside the Ed25519 signature, so it is
  unforgeable and an install cannot self-promote a bind-required key.
* **Signature = entitlement** (tier / seats / `license_mode` / expiry —
  tamper-proof, offline). **Phone-home = liveness** (still-paid / not-canceled /
  not-over-shared). Both kept; the signature is the SOLE enforcement for
  `offline_term` + grace-window operation.
* Two `license_mode`s (signed): `subscription` (bind once, then periodic
  liveness) and `offline_term` (perpetual/term, signed expiry, NEVER phones
  home). A `lifetime:true` key overrides the mode = activate-once (bind) + no
  liveness, perpetual. **Absent `license_mode` defaults to `offline_term`**
  (fail-safe — trust the signature, never lock out on a missing field).
* **The subscription gate is conditioned on `relay_client.is_available()`** (=
  configured for the relay, not a live ping). When the install has no relay
  configured OR is air-gapped, a subscription key CANNOT bind → community cap;
  only `offline_term`/lifetime (signature) grant seats.
* `OTODOCK_AIR_GAPPED` (default false =
  connected) is a **no-outbound switch** (relay AND license server). The FLAG
  governs outbound, the KEY governs licensing — they compose, with ONE policy
  rule: an `offline_term` key is **relay-excluded** (no hosted OAuth /
  api_key_relay) even on a connected box, and never phones home for the license,
  so an `offline_term` install makes zero outbound to OtoDock (see
  `services/billing/relay_client.relay_offered`). `OTODOCK_CLOUD=true` ⇒ air-gapped
  forced false.
* Two INDEPENDENT grace windows: **unreachable** (client, network — fail OPEN,
  keep last-known-good for `UNREACHABLE_GRACE_DAYS=21`, only AFTER a binding;
  deliberate air-gap gets none) and **payment** (server dunning — `active`
  ~7-14d then `canceled`).
* A confirmed lapse blocks only NEW user creation; existing users/agents/
  sessions/automations always keep working. `unactivated` is a SOFT community cap
  (still allows creation up to 5), distinct from the hard-blocking lapse states.
  FREE tiers never phone home. Paid `api_key_relay` is credit-gated on the user's
  OtoDock account, independent of the install license.
* `get_current_license()` is **purely local** (computes the effective status from
  the signed key + cached receipt/verdict/timestamps; NEVER calls the relay) and
  the status is **computed every call, never persisted** (only raw facts are
  stored). Only the liveness worker + the activate/recheck endpoints touch the
  network. Cloud seat-reclaim enforcement is deferred until the control plane
  ships.

Install-side seams for the subscription layer (`activate_license` / `license_check` /
`deactivate_license`) live in `services/billing/relay_client.py`, stubbed until the
license server ships — exactly like the hosted relay.
"""

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from storage import database as db

COMMUNITY_MAX_USERS = 5
# Cloud free tier caps.
CLOUD_FREE_USERS = 1
CLOUD_FREE_AGENTS = 1
# Sentinel: agent count is unlimited.
UNLIMITED = -1
# Grace window (days past expiry) before the second downgrade stage (offline_term).
GRACE_DAYS = 30
# Subscription unreachable-grace: keep last-known-good entitlement this many days
# after the last SUCCESSFUL relay check before lapsing (a network/relay outage
# must never downgrade a paying customer; only applies AFTER a binding).
UNREACHABLE_GRACE_DAYS = 21


@dataclass
class LicenseInfo:
    tier: str  # community | pro | team | business | enterprise
    max_users: int
    valid_until: str = ""          # ISO expiry date, or "" for perpetual/community
    # Effective enforcement status (computed by get_current_license):
    #   valid | grace | expired | lifetime | community          (signature-derived)
    #   unactivated | grace_unreachable | lapsed                 (subscription)
    status: str = "valid"
    days_since_expiry: int = 0
    company_name: str = ""
    lifetime: bool = False
    # Subscription mode + cached activation/liveness facts (filled by get_current_license).
    license_mode: str = "offline_term"   # subscription | offline_term (default = fail-safe)
    activation_state: str = "none"        # none | activated
    check_status: str = ""                # last raw relay verdict: active|past_due|canceled
    last_ok_at: str = ""                  # ISO ts of last successful check (grace math)


# Tier → seat cap. Agents are unlimited at every self-hosted tier.
_TIERS: dict[str, int] = {
    "community": COMMUNITY_MAX_USERS,
    "pro": 15,
    "team": 50,
    "business": 100,
    "enterprise": 999999,
}

# Baked Ed25519 public key (base64url, no padding) — the DEFAULT license-verifying
# key. It verifies any token whose payload carries no `kid` (activation receipts +
# liveness responses don't) and is the fallback when a license's `kid` isn't in
# `_TRUSTED_PUBLIC_KEYS`. Public keys aren't secret — safe to bake in. Tests
# monkeypatch this with a test public key.
#
# Published by the commercial license server (`scripts/gen_keys.py` prints it; the
# service exposes it at `app.keyserver.public_key_b64()`). This is OtoDock's CURRENT
# license-signing public key (kid `k1`). Rotation: move the retiring key into
# `_TRUSTED_PUBLIC_KEYS` under its kid and bake the new current key here. Public
# keys aren't secret — safe to bake into OSS.
_LICENSE_PUBLIC_KEY_B64 = "bqeVwnhwhXmVDiZ24GP8YEALcxyGQhZEyvqf7j9i0jU"

# Trusted public keys by `kid`, for key ROTATION. A license carries the `kid` it
# was signed under, so rotating the signing key doesn't invalidate keys issued
# under the previous one. On rotation: move the retiring key here under its kid
# and bake the new current key as `_LICENSE_PUBLIC_KEY_B64` (a keyed match wins
# over the default). Empty until the first rotation, e.g. {"k1": "<old pubkey>"}.
_TRUSTED_PUBLIC_KEYS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Offline signature verification
# ---------------------------------------------------------------------------

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key_obj(b64: str):
    """Build an Ed25519 public key object from a base64url(no-pad) string, or
    None if the string is empty/invalid."""
    if not b64:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        return Ed25519PublicKey.from_public_bytes(_b64url_decode(b64))
    except Exception:
        return None


def _public_key_for_kid(kid: str | None):
    """Resolve the trusted verifying key for a payload's `kid`.

    A keyed match in `_TRUSTED_PUBLIC_KEYS` wins (rotation); otherwise the default
    baked key (`_LICENSE_PUBLIC_KEY_B64`), which also covers tokens with no `kid`
    (activation receipts + liveness responses). None if neither is configured.
    """
    return _key_obj(_TRUSTED_PUBLIC_KEYS.get(kid or "") or _LICENSE_PUBLIC_KEY_B64)


def verify_license_token(token: str, public_key=None) -> dict | None:
    """Verify a `<payload_b64url>.<sig_b64url>` Ed25519 license token.

    The signature is over the raw payload bytes. When `public_key` is not given,
    the verifying key is selected by the payload's `kid` (`_public_key_for_kid`).
    Selecting the key by an as-yet-unverified `kid` is safe: it only decides WHICH
    trusted key to check the signature against — an attacker still can't forge a
    valid signature for a key they don't hold. Returns the decoded payload dict on
    success, else None (malformed / bad signature / no key configured).
    """
    if not token or "." not in token:
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        payload_b64, sig_b64 = token.strip().split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict):
            return None
        pk = public_key if public_key is not None else _public_key_for_kid(payload.get("kid"))
        if pk is None:
            return None
        pk.verify(_b64url_decode(sig_b64), payload_bytes)  # raises on bad sig
        return payload
    except InvalidSignature:
        return None
    except Exception:
        return None


def _compute_status(expiry_date: str, lifetime: bool, now: datetime | None = None) -> tuple[str, int]:
    """Return (status, days_since_expiry) for a license (offline/signature view).

    ``now`` defaults to the real wall clock (keeps ``validate_license_key`` pure
    + testable); ``get_current_license`` passes the rollback-clamped effective
    now so a backdated clock can't dodge an ``offline_term`` expiry.
    """
    if lifetime:
        return "lifetime", 0
    if not expiry_date:
        return "valid", 0
    try:
        exp = datetime.fromisoformat(expiry_date.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except ValueError:
        return "valid", 0
    if now is None:
        now = datetime.now(timezone.utc)
    if now <= exp:
        return "valid", 0
    days = (now - exp).days
    return ("grace", days) if days <= GRACE_DAYS else ("expired", days)


def validate_license_key(key: str) -> LicenseInfo | None:
    """Validate a signed license key → LicenseInfo (entitlement) or None.

    PURE: verifies the signature + maps the payload; NO I/O, NO activation/
    liveness (``get_current_license`` layers those on). ``status`` here is the
    signature/expiry view; for a ``subscription`` key the effective status is
    overridden by the activation state machine.

    Payload shape: ``{company_name, tier, user_limit, license_mode, issue_date,
    expiry_date, lifetime}``. ``license_mode`` ∈ {subscription, offline_term};
    absent → ``offline_term`` (fail-safe). No ``deployment`` field — the install
    knows its deployment via ``OTODOCK_CLOUD``.
    """
    if not key or not key.strip():
        return None
    payload = verify_license_token(key.strip())
    if not payload:
        return None
    tier = payload.get("tier", "")
    if tier not in _TIERS:
        return None
    lifetime = bool(payload.get("lifetime"))
    expiry = "" if lifetime else (payload.get("expiry_date") or "")
    status, days = _compute_status(expiry, lifetime)
    # License mode is signed; absent → offline_term (fail-safe: trust the
    # signature rather than demand an activation that can't happen yet).
    license_mode = payload.get("license_mode") or "offline_term"
    if license_mode not in ("subscription", "offline_term"):
        license_mode = "offline_term"
    # `user_limit` from the key overrides the tier default (e.g. a 3-seat Pro
    # key). Falls back to the tier's standard cap.
    try:
        user_limit = int(payload.get("user_limit") or _TIERS[tier])
    except (TypeError, ValueError):
        user_limit = _TIERS[tier]
    return LicenseInfo(
        tier=tier,
        max_users=user_limit,
        valid_until=expiry,
        status=status,
        days_since_expiry=days,
        company_name=payload.get("company_name", ""),
        lifetime=lifetime,
        license_mode=license_mode,
    )


def _community() -> LicenseInfo:
    return LicenseInfo(
        tier="community", max_users=COMMUNITY_MAX_USERS, valid_until="",
        status="valid",
    )


# ---------------------------------------------------------------------------
# License-key storage — encrypted at rest in the credential store. The signed
# entitlement token is kept out of plain-text platform_settings AND out of the
# admin API response. Falls back to the legacy platform_settings location when
# the credential store is unavailable (e.g. unit tests with no DB), and migrates
# a legacy plain key into the encrypted store on first read.
# ---------------------------------------------------------------------------

_LICENSE_CRED_SLUG = "otodock-license"
_LICENSE_CRED_KEY = "license_key"


def get_license_key() -> str:
    """The install's license key (decrypted). Reads the encrypted credential
    store first; falls back to — and migrates from — the legacy plain
    ``platform_settings.license_key``."""
    try:
        from storage import credential_store
        enc = (credential_store.get_infra_credentials(_LICENSE_CRED_SLUG) or {}).get(
            _LICENSE_CRED_KEY, "")
        if enc:
            return enc
    except Exception:
        pass
    legacy = (db.get_platform_setting("license_key") or "").strip()
    if legacy:
        try:  # one-time migration: move the plain key into the encrypted store
            from storage import credential_store
            credential_store.set_infra_credentials(
                _LICENSE_CRED_SLUG, {_LICENSE_CRED_KEY: legacy})
            db.set_platform_setting("license_key", "")
        except Exception:
            pass
    return legacy


def set_license_key(key: str) -> None:
    """Store the license key ENCRYPTED (credential store); never leave a plain
    copy in platform_settings. An empty key clears it."""
    key = (key or "").strip()
    try:
        from storage import credential_store
        if key:
            credential_store.set_infra_credentials(
                _LICENSE_CRED_SLUG, {_LICENSE_CRED_KEY: key})
        else:
            credential_store.delete_infra_credential_key(
                _LICENSE_CRED_SLUG, _LICENSE_CRED_KEY)
        db.set_platform_setting("license_key", "")
    except Exception:
        # Credential store unavailable (e.g. unit tests, no DB) → keep it in
        # platform_settings so the license still works; migrates on next read.
        db.set_platform_setting("license_key", key)


# ---------------------------------------------------------------------------
# Effective license (subscription state machine) — composes the signed entitlement with
# the cached activation + liveness facts. PURELY LOCAL: reads platform_settings,
# NEVER calls the relay (only the worker + activate/recheck endpoints do).
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def _effective_now() -> datetime:
    """Real now, clamped up to the persisted monotonic floor so a backdated
    system clock can't extend an expiry/grace window (best-effort). The floor
    is advanced by the enforcement gates + the liveness worker, never by this
    read-only function."""
    now = datetime.now(timezone.utc)
    floor = _parse_iso(db.get_platform_setting("license_last_seen_clock"))
    return max(now, floor) if floor else now


def _advance_seen_clock() -> None:
    """Advance the anti-rollback floor to max(stored, real_now). Called by the
    enforcement gates + the worker (infrequent — never a hot path)."""
    now = datetime.now(timezone.utc)
    floor = _parse_iso(db.get_platform_setting("license_last_seen_clock"))
    if floor is None or now > floor:
        db.set_platform_setting("license_last_seen_clock", now.isoformat())


def _receipt_valid(receipt_token: str, key: str) -> bool:
    """True iff ``receipt_token`` is a relay-signed activation receipt that binds
    to THIS license key + this install_id. Verified with the same Ed25519 key +
    ``<payload>.<sig>`` envelope as a license key (``verify_license_token``), so
    tamper / wrong-key / wrong-install → False → treated as unactivated."""
    if not receipt_token:
        return False
    payload = verify_license_token(receipt_token)
    if not payload:
        return False
    from services.billing import relay_client
    return (payload.get("license_key") == key
            and payload.get("install_id") == relay_client.get_install_id())


def _subscription_status(check_status: str, last_ok_at: str, last_check_at: str,
                         now: datetime) -> str:
    """Effective status for an ACTIVATED subscription key from cached liveness.

    canceled → lapsed; >UNREACHABLE_GRACE_DAYS since the last SUCCESSFUL check →
    lapsed; within grace but a check is currently failing → grace_unreachable;
    otherwise → valid. Unknown verdicts fail OPEN (never lapse on a surprise).
    """
    if check_status == "canceled":
        return "lapsed"
    ok_dt = _parse_iso(last_ok_at)
    if ok_dt is not None and (now - ok_dt).days >= UNREACHABLE_GRACE_DAYS:
        return "lapsed"
    # Within the unreachable-grace window (or freshly activated). A check
    # attempted AFTER the last success means it's currently failing — surface
    # the amber banner; otherwise healthy. Both fail-open to the full cap.
    chk_dt = _parse_iso(last_check_at)
    if ok_dt is not None and chk_dt is not None and chk_dt > ok_dt:
        return "grace_unreachable"
    return "valid"


def get_current_license() -> LicenseInfo:
    """Effective license: signed entitlement composed with cached activation +
    liveness facts. PURELY LOCAL (one settings read; never calls the relay); the
    effective status is computed fresh every call, never persisted. See the
    module docstring's state machine.
    """
    from services.billing import relay_client
    settings = db.get_all_platform_settings()
    key = get_license_key()
    if not key:
        return _community()
    lic = validate_license_key(key)
    if lic is None:
        return _community()

    # Surface the cached liveness facts on the returned object (for the admin UI).
    lic.check_status = settings.get("license_check_status", "")
    lic.last_ok_at = settings.get("license_last_ok_at", "")
    activated = _receipt_valid(settings.get("license_activation_receipt", ""), key)
    lic.activation_state = "activated" if activated else "none"

    now = _effective_now()

    # offline_term (incl. an absent/default mode) = signature-only, no subscription binding.
    # Recompute the expiry status against the rollback-clamped clock. No
    # activation, no liveness.
    if not lic.lifetime and lic.license_mode == "offline_term":
        lic.status, lic.days_since_expiry = _compute_status(lic.valid_until, False, now)
        return lic

    # subscription / lifetime — a binding governs paid seats (the invariant).
    if not relay_client.is_available():
        # Relay not configured (unbuilt today) or air-gapped → can't bind. A
        # lifetime key already bound (cached receipt) stays perpetual; a
        # subscription key (or never-bound lifetime) gets no paid seats here.
        lic.status = "lifetime" if (lic.lifetime and activated) else "unactivated"
        return lic

    # Relay available → activation governs.
    if not activated:
        lic.status = "unactivated"
    elif lic.lifetime:
        lic.status = "lifetime"   # bound lifetime → perpetual, no liveness check
    else:
        lic.status = _subscription_status(
            lic.check_status, lic.last_ok_at,
            settings.get("license_last_check_at", ""), now,
        )
    return lic


# ---------------------------------------------------------------------------
# Enforcement — deployment-aware. Named to avoid colliding with the cost-based
# `usage_service.check_user_limit` / `check_agent_limit`.
# ---------------------------------------------------------------------------

def check_seat_limit() -> tuple[bool, int, int]:
    """Can another user be created? Returns (allowed, current_count, max_users).

    * Cloud: free tier = 1 user; a provisioned license raises the cap (control
      plane manages the plan — no grace concept here).
    * Self-hosted: community 5 / licensed cap. Effective status (subscription):
      `unactivated` → SOFT community cap (still create up to 5 until the key
      binds); `grace`/`expired`/`lapsed` → hard-block NEW users (existing keep
      working); `valid`/`lifetime`/`grace_unreachable` → up to the signed cap.
    """
    _advance_seen_clock()   # anti-rollback floor (infrequent call site)
    lic = get_current_license()
    current = db.count_users()
    if config.OTODOCK_CLOUD:
        cap = lic.max_users if lic.tier != "community" else CLOUD_FREE_USERS
        return current < cap, current, cap
    if lic.status == "unactivated":
        # Paid seats require a relay binding; until then, community cap.
        return current < COMMUNITY_MAX_USERS, current, COMMUNITY_MAX_USERS
    if lic.status in ("grace", "expired", "lapsed"):
        return False, current, lic.max_users
    return current < lic.max_users, current, lic.max_users


def _count_user_agents() -> int:
    """Count agents toward the licensed agent cap — every mode counts the same."""
    from storage import agent_store
    return len(agent_store.get_all_agents())


def check_agent_count_limit() -> tuple[bool, int, int]:
    """Can another agent be created? Returns (allowed, current, max_agents).

    ``max_agents == UNLIMITED`` (-1) means no cap. Two gating reasons:
    * Cloud free tier → 1 agent.
    * Self-hosted with a license expired > GRACE_DAYS (stage-2 downgrade) →
      block new agents. Otherwise self-hosted agents are unlimited at every tier.
    """
    _advance_seen_clock()   # anti-rollback floor (infrequent call site)
    lic = get_current_license()
    current = _count_user_agents()
    if config.OTODOCK_CLOUD:
        cap = 999999 if lic.tier != "community" else CLOUD_FREE_AGENTS
        return current < cap, current, cap
    if lic.status == "expired":
        return False, current, current
    return True, current, UNLIMITED
