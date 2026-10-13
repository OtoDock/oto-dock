"""License validation + deployment-aware enforcement.

Covers offline Ed25519 verification (valid / expired / tampered / lifetime),
the two-stage graceful downgrade windows, deployment-aware seat + agent caps
(self-hosted vs cloud), and confirms the license-side gates don't collide with
the cost-based `usage_service` ones.
"""

import base64
import json
from datetime import datetime, timezone, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config
import auth.license as L


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _tamper(token: str) -> str:
    """``token`` with one bit of its signature flipped. Replacing its last
    base64url characters leaves it valid about once in a thousand: they
    carry the signature's top bits, which an Ed25519 signature leaves zero."""
    payload, sig = token.rsplit(".", 1)
    raw = bytearray(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
    raw[0] ^= 0x01
    return payload + "." + _b64u(bytes(raw))


@pytest.fixture
def signer(monkeypatch):
    """Install a test public key on the license module + return a signer."""
    sk = Ed25519PrivateKey.generate()
    pub_raw = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(L, "_LICENSE_PUBLIC_KEY_B64", _b64u(pub_raw))

    def sign(tier="pro", expiry=None, lifetime=False, user_limit=None, company="Acme"):
        payload = {"company_name": company, "tier": tier, "lifetime": lifetime}
        if user_limit is not None:
            payload["user_limit"] = user_limit
        if expiry is not None:
            payload["expiry_date"] = expiry
        pb = json.dumps(payload).encode()
        return _b64u(pb) + "." + _b64u(sk.sign(pb))

    return sign


def _days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Signature verification + tiers
# ---------------------------------------------------------------------------

def test_valid_pro_team_business(signer):
    assert L.validate_license_key(signer("pro", _days(100))).max_users == 15
    assert L.validate_license_key(signer("team", _days(100))).max_users == 50
    assert L.validate_license_key(signer("business", _days(100))).max_users == 100


def test_custom_seat_count(signer):
    li = L.validate_license_key(signer("pro", _days(100), user_limit=3))
    assert li.max_users == 3 and li.tier == "pro"


def test_tampered_key_rejected(signer):
    tok = signer("business", _days(100))
    assert L.validate_license_key(_tamper(tok)) is None


def test_unknown_tier_rejected(signer):
    assert L.validate_license_key(signer("ultra", _days(100))) is None


def test_empty_and_garbage_rejected(signer):
    assert L.validate_license_key("") is None
    assert L.validate_license_key("not-a-token") is None


def test_no_public_key_rejects_real_key(signer, monkeypatch):
    monkeypatch.setattr(L, "_LICENSE_PUBLIC_KEY_B64", "")
    assert L.validate_license_key(signer("pro", _days(100))) is None


# ---------------------------------------------------------------------------
# Status / two-stage grace windows
# ---------------------------------------------------------------------------

def test_status_valid(signer):
    assert L.validate_license_key(signer("team", _days(10))).status == "valid"


def test_status_grace_window(signer):
    li = L.validate_license_key(signer("team", _days(-15)))
    assert li.status == "grace" and li.days_since_expiry == 15


def test_status_expired_window(signer):
    li = L.validate_license_key(signer("team", _days(-40)))
    assert li.status == "expired" and li.days_since_expiry == 40


def test_status_grace_boundary_30d(signer):
    li = L.validate_license_key(signer("team", _days(-30)))
    assert li.status == "grace"   # day 30 is still grace; 31+ is expired


def test_lifetime_perpetual_caps_held(signer):
    li = L.validate_license_key(signer("pro", expiry=None, lifetime=True))
    assert li.status == "lifetime"
    assert li.valid_until == ""
    assert li.max_users == 15      # Lifetime Pro still enforces the Pro cap


# ---------------------------------------------------------------------------
# get_current_license fallback
# ---------------------------------------------------------------------------

def test_get_current_license_no_key_is_community(monkeypatch):
    monkeypatch.setattr(L.db, "get_platform_setting", lambda k: "")
    li = L.get_current_license()
    assert li.tier == "community" and li.max_users == 5 and li.status == "valid"


def test_get_current_license_invalid_key_is_community(signer, monkeypatch):
    monkeypatch.setattr(L, "_LICENSE_PUBLIC_KEY_B64", "")  # can't verify
    monkeypatch.setattr(L.db, "get_platform_setting", lambda k: "some-key")
    assert L.get_current_license().tier == "community"


# ---------------------------------------------------------------------------
# Seat enforcement — deployment aware + two-stage downgrade
# ---------------------------------------------------------------------------

def _patch_license(monkeypatch, tier, max_users, status="valid"):
    monkeypatch.setattr(
        L, "get_current_license",
        lambda: L.LicenseInfo(tier=tier, max_users=max_users, status=status),
    )


def test_seat_self_host_community_under_cap(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    _patch_license(monkeypatch, "community", 5)
    monkeypatch.setattr(L.db, "count_users", lambda: 4)
    allowed, cur, cap = L.check_seat_limit()
    assert allowed is True and cur == 4 and cap == 5


def test_seat_self_host_community_at_cap(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    _patch_license(monkeypatch, "community", 5)
    monkeypatch.setattr(L.db, "count_users", lambda: 5)
    assert L.check_seat_limit()[0] is False


def test_seat_self_host_grace_blocks_new_users(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    _patch_license(monkeypatch, "team", 50, status="grace")
    monkeypatch.setattr(L.db, "count_users", lambda: 6)  # well under cap
    # Lapsed (grace) → new users blocked even though count < cap.
    assert L.check_seat_limit()[0] is False


def test_seat_self_host_expired_blocks_new_users(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    _patch_license(monkeypatch, "team", 50, status="expired")
    monkeypatch.setattr(L.db, "count_users", lambda: 6)
    assert L.check_seat_limit()[0] is False


def test_seat_cloud_free_one_user(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    _patch_license(monkeypatch, "community", 5)  # no license on cloud
    monkeypatch.setattr(L.db, "count_users", lambda: 1)
    allowed, cur, cap = L.check_seat_limit()
    assert allowed is False and cap == 1   # cloud free = 1 user


# ---------------------------------------------------------------------------
# Agent-count enforcement — both gating reasons
# ---------------------------------------------------------------------------

def test_agents_self_host_unlimited_when_valid(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    _patch_license(monkeypatch, "team", 50, status="valid")
    monkeypatch.setattr(L, "_count_user_agents", lambda: 999)
    allowed, cur, cap = L.check_agent_count_limit()
    assert allowed is True and cap == L.UNLIMITED


def test_agents_self_host_grace_still_unlimited(monkeypatch):
    # Stage 1 (grace) blocks new USERS but NOT new agents.
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    _patch_license(monkeypatch, "team", 50, status="grace")
    monkeypatch.setattr(L, "_count_user_agents", lambda: 10)
    assert L.check_agent_count_limit()[0] is True


def test_agents_self_host_expired_blocks(monkeypatch):
    # Stage 2 (30+ days) ALSO blocks new agents.
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    _patch_license(monkeypatch, "team", 50, status="expired")
    monkeypatch.setattr(L, "_count_user_agents", lambda: 10)
    assert L.check_agent_count_limit()[0] is False


def test_agents_cloud_free_one_agent(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    _patch_license(monkeypatch, "community", 5)
    monkeypatch.setattr(L, "_count_user_agents", lambda: 1)
    allowed, cur, cap = L.check_agent_count_limit()
    assert allowed is False and cap == 1


# ---------------------------------------------------------------------------
# Non-collision with the cost-based usage_service caps
# ---------------------------------------------------------------------------

def test_license_gates_distinct_from_usage_service():
    from services.billing import usage_service
    # Both pairs exist and are different callables.
    assert callable(L.check_seat_limit) and callable(L.check_agent_count_limit)
    assert callable(usage_service.check_user_limit) and callable(usage_service.check_agent_limit)
    assert L.check_seat_limit is not usage_service.check_user_limit
    assert L.check_agent_count_limit is not usage_service.check_agent_limit
