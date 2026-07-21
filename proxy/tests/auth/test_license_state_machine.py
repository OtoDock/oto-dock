"""License state machine (modes, activation, liveness, air-gap gate).

Covers `get_current_license`'s decision procedure end-to-end with an in-memory
`platform_settings` store + a test Ed25519 signer for BOTH license keys and
relay activation receipts (same key + `<payload>.<sig>` envelope — the shared
verifier). The relay is never actually called: `is_available()` is monkeypatched
and the network seams are wired to explode if touched.

The complementary worker / API / dashboard tests live in the license API tests.
"""

import base64
import json
from datetime import datetime, timezone, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config
import auth.license as L
from services.billing import relay_client
from services.billing import license_check_worker


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.fixture
def store(monkeypatch):
    """In-memory platform_settings (patches storage.database globally, so both
    license.py and relay_client.get_install_id see it)."""
    data = {"install_id": "test-install"}
    monkeypatch.setattr(L.db, "get_all_platform_settings", lambda: dict(data))
    monkeypatch.setattr(L.db, "get_platform_setting", lambda k: data.get(k, ""))
    monkeypatch.setattr(L.db, "set_platform_setting", data.__setitem__)
    return data


@pytest.fixture
def sign(monkeypatch):
    """Return a signer over arbitrary payloads, with its public key baked in."""
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(L, "_LICENSE_PUBLIC_KEY_B64", _b64u(pub))

    def _sign(payload: dict) -> str:
        pb = json.dumps(payload).encode()
        return _b64u(pb) + "." + _b64u(sk.sign(pb))

    return _sign


def _key(sign, *, mode="subscription", tier="pro", expiry=100, lifetime=False):
    p = {"tier": tier, "company_name": "Acme"}
    if lifetime:
        p["lifetime"] = True
    else:
        p["license_mode"] = mode
        p["expiry_date"] = _iso(expiry)
    return sign(p)


def _receipt(sign, key, install_id="test-install"):
    return sign({"license_key": key, "install_id": install_id})


def _connected(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)


def _relay_unavailable(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: False)


# ---------------------------------------------------------------------------
# offline_term / default = signature-only
# ---------------------------------------------------------------------------

def test_no_key_is_community(store, monkeypatch):
    _connected(monkeypatch)
    lic = L.get_current_license()
    assert lic.tier == "community" and lic.status == "valid" and lic.max_users == 5


def test_offline_term_valid(store, sign, monkeypatch):
    _relay_unavailable(monkeypatch)  # offline_term ignores relay availability
    store["license_key"] = _key(sign, mode="offline_term", expiry=100)
    lic = L.get_current_license()
    assert lic.status == "valid" and lic.license_mode == "offline_term" and lic.max_users == 15


def test_offline_term_grace(store, sign, monkeypatch):
    _connected(monkeypatch)
    store["license_key"] = _key(sign, mode="offline_term", expiry=-10)
    assert L.get_current_license().status == "grace"


def test_offline_term_expired(store, sign, monkeypatch):
    _connected(monkeypatch)
    store["license_key"] = _key(sign, mode="offline_term", expiry=-40)
    assert L.get_current_license().status == "expired"


def test_absent_mode_defaults_offline_term(store, sign, monkeypatch):
    _relay_unavailable(monkeypatch)
    # No license_mode in the payload → fail-safe offline_term → full cap by signature.
    store["license_key"] = sign({"tier": "team", "expiry_date": _iso(100)})
    lic = L.get_current_license()
    assert lic.license_mode == "offline_term" and lic.status == "valid" and lic.max_users == 50


# ---------------------------------------------------------------------------
# subscription — the binding gate (the invariant)
# ---------------------------------------------------------------------------

def test_subscription_unactivated_when_relay_unavailable(store, sign, monkeypatch):
    # Relay unbuilt / air-gapped → can't bind → community cap, never paid seats.
    _relay_unavailable(monkeypatch)
    store["license_key"] = _key(sign, mode="subscription")
    assert L.get_current_license().status == "unactivated"


def test_subscription_unactivated_when_no_receipt(store, sign, monkeypatch):
    _connected(monkeypatch)
    store["license_key"] = _key(sign, mode="subscription")
    assert L.get_current_license().status == "unactivated"


def test_subscription_valid_when_activated(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "active"
    store["license_last_ok_at"] = _iso(0)
    lic = L.get_current_license()
    assert lic.status == "valid" and lic.activation_state == "activated" and lic.max_users == 15


def test_subscription_freshly_activated_no_check_yet_is_valid(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_last_ok_at"] = _iso(0)  # set at activation; no check verdict yet
    assert L.get_current_license().status == "valid"


def test_subscription_canceled_is_lapsed(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "canceled"
    store["license_last_ok_at"] = _iso(-1)
    assert L.get_current_license().status == "lapsed"


def test_subscription_unreachable_beyond_grace_is_lapsed(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "active"
    store["license_last_ok_at"] = _iso(-25)   # 25d since last success > 21d
    assert L.get_current_license().status == "lapsed"


def test_subscription_unreachable_within_grace_is_grace_unreachable(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "active"
    store["license_last_ok_at"] = _iso(-10)    # last success 10d ago (< 21d)
    store["license_last_check_at"] = _iso(-1)  # a later attempt → currently failing
    assert L.get_current_license().status == "grace_unreachable"


def test_subscription_unknown_status_fails_open(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "something_weird"  # unrecognized → fail-open
    store["license_last_ok_at"] = _iso(-1)
    assert L.get_current_license().status == "valid"


# ---------------------------------------------------------------------------
# lifetime = activate-once, no liveness
# ---------------------------------------------------------------------------

def test_lifetime_activated_is_lifetime(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, tier="pro", lifetime=True)
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    # No check status / last_ok needed — lifetime never runs liveness.
    assert L.get_current_license().status == "lifetime"


def test_lifetime_bound_survives_air_gap(store, sign, monkeypatch):
    _relay_unavailable(monkeypatch)  # later went air-gapped
    key = _key(sign, tier="pro", lifetime=True)
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)  # bound on a prior connected run
    assert L.get_current_license().status == "lifetime"


def test_lifetime_never_bound_air_gapped_is_unactivated(store, sign, monkeypatch):
    _relay_unavailable(monkeypatch)
    store["license_key"] = _key(sign, tier="pro", lifetime=True)  # no receipt
    assert L.get_current_license().status == "unactivated"


# ---------------------------------------------------------------------------
# receipt binding — tamper / wrong key / wrong install
# ---------------------------------------------------------------------------

def test_receipt_for_wrong_key_rejected(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, "some-other-key")
    assert L.get_current_license().status == "unactivated"


def test_receipt_wrong_install_id_rejected(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key, install_id="other-install")
    assert L.get_current_license().status == "unactivated"


def test_receipt_tampered_rejected(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)[:-3] + "AAA"  # break sig
    assert L.get_current_license().status == "unactivated"


# ---------------------------------------------------------------------------
# enforcement gates consume the effective status
# ---------------------------------------------------------------------------

def test_seat_unactivated_soft_community_cap(store, sign, monkeypatch):
    _connected(monkeypatch)
    store["license_key"] = _key(sign, mode="subscription")  # no receipt → unactivated
    monkeypatch.setattr(L.db, "count_users", lambda: 3)
    allowed, cur, cap = L.check_seat_limit()
    assert allowed is True and cap == 5      # community cap, NOT the pro 15
    monkeypatch.setattr(L.db, "count_users", lambda: 5)
    assert L.check_seat_limit()[0] is False  # at community cap


def test_seat_subscription_lapsed_hard_blocks(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "canceled"  # lapsed
    monkeypatch.setattr(L.db, "count_users", lambda: 2)  # well under cap
    assert L.check_seat_limit()[0] is False


def test_agents_subscription_lapsed_not_blocked(store, sign, monkeypatch):
    # A subscription lapse blocks seats, NOT agents.
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "canceled"
    monkeypatch.setattr(L, "_count_user_agents", lambda: 10)
    allowed, cur, cap = L.check_agent_count_limit()
    assert allowed is True and cap == L.UNLIMITED


# ---------------------------------------------------------------------------
# the enforcement read path never phones home
# ---------------------------------------------------------------------------

def test_get_current_license_makes_no_relay_call(store, sign, monkeypatch):
    _connected(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("get_current_license must not call the relay")

    monkeypatch.setattr(relay_client, "license_check", _boom)
    monkeypatch.setattr(relay_client, "activate_license", _boom)
    monkeypatch.setattr(relay_client, "oauth_exchange", _boom)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_check_status"] = "active"
    store["license_last_ok_at"] = _iso(0)
    assert L.get_current_license().status == "valid"  # must not raise


# ---------------------------------------------------------------------------
# license_check_worker — activate-then-check, cadence, fail-open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_activates_when_no_receipt(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    rec = _receipt(sign, key)

    async def _activate(k):
        assert k == key
        return rec

    monkeypatch.setattr(relay_client, "activate_license", _activate)
    await license_check_worker._do_check()
    assert store["license_activation_receipt"] == rec
    assert store["license_check_status"] == "active"
    assert store.get("license_last_ok_at")


@pytest.mark.asyncio
async def test_worker_checks_when_due(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_last_check_at"] = _iso(-10)  # >7d ago → due

    async def _check(k):
        return {"status": "active"}

    monkeypatch.setattr(relay_client, "license_check", _check)
    await license_check_worker._do_check()
    assert store["license_check_status"] == "active"
    assert store.get("license_last_ok_at")


@pytest.mark.asyncio
async def test_worker_skips_when_not_due(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_last_check_at"] = _iso(0)  # just checked → not due

    async def _boom(k):
        raise AssertionError("must not check when not due")

    monkeypatch.setattr(relay_client, "license_check", _boom)
    await license_check_worker._do_check()  # must not raise


@pytest.mark.asyncio
async def test_worker_check_fails_open(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)
    store["license_last_check_at"] = _iso(-10)
    ok = _iso(-10)
    store["license_last_ok_at"] = ok

    async def _boom(k):
        raise relay_client.RelayNotConfigured("relay down")

    monkeypatch.setattr(relay_client, "license_check", _boom)
    await license_check_worker._do_check()  # must not raise
    # last_ok untouched → the unreachable-grace window keeps running.
    assert store["license_last_ok_at"] == ok


@pytest.mark.asyncio
async def test_worker_lifetime_never_checks(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, tier="pro", lifetime=True)
    store["license_key"] = key
    store["license_activation_receipt"] = _receipt(sign, key)  # bound

    async def _boom(k):
        raise AssertionError("a bound lifetime key must never run liveness")

    monkeypatch.setattr(relay_client, "license_check", _boom)
    await license_check_worker._do_check(force=True)  # even forced → no check


@pytest.mark.asyncio
async def test_worker_dormant_on_cloud(store, sign, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    store["license_key"] = _key(sign, mode="subscription")

    async def _boom(k):
        raise AssertionError("worker must be dormant on cloud")

    monkeypatch.setattr(relay_client, "activate_license", _boom)
    await license_check_worker._do_check(force=True)  # no-op


@pytest.mark.asyncio
async def test_worker_activation_limit_stays_unactivated(store, sign, monkeypatch):
    _connected(monkeypatch)
    key = _key(sign, mode="subscription")
    store["license_key"] = key

    async def _activate(k):
        raise relay_client.RelayError("activation_limit_reached")

    monkeypatch.setattr(relay_client, "activate_license", _activate)
    await license_check_worker._do_check()  # must not raise
    assert "license_activation_receipt" not in store  # no binding cached
