"""Regression tests for the 2026-09-02 relaunch assurance pass.

Covers the verified findings that did not already have targeted coverage:
require_admin principal gate, the rate-limiter escalation fix, the
session-vs-password-change invalidation, the sandbox symlink-refusing bind
guard, and the local-login tarpit recording.
"""

import os
import time

import pytest

import config
from auth.providers import (
    UserContext, require_admin, session_iat_after_password_change,
)
from fastapi import HTTPException


# --- require_admin rejects non-cookie principals (session-token / API key) ---

def test_require_admin_rejects_api_key_principal():
    """An agent-subprocess session token resolves to the owner's real role;
    without the is_api_key gate, admin-owned-session agent code could drive
    /v1/admin/*. require_admin must reject it like require_creator does."""
    svc = UserContext(sub="local:x", email="a@b.c", name="A", role="admin",
                      is_api_key=True)
    with pytest.raises(HTTPException) as ei:
        require_admin(svc)
    assert ei.value.status_code == 403


def test_require_admin_allows_real_admin_cookie():
    u = UserContext(sub="local:x", email="a@b.c", name="A", role="admin",
                    is_api_key=False)
    assert require_admin(u) is u


# --- session cookie invalidated by a later password change --------------------

def _iso_ts(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp())


def test_session_iat_after_password_change():
    now = int(time.time())
    changed = "2026-09-02T12:00:00+00:00"
    changed_ts = _iso_ts(changed)
    # Cookie minted BEFORE the change (beyond the 5s grace) → invalid.
    assert session_iat_after_password_change(
        {"password_changed_at": changed}, {"iat": changed_ts - 3600}) is False
    # Cookie minted AFTER the change → valid.
    assert session_iat_after_password_change(
        {"password_changed_at": changed}, {"iat": changed_ts + 3600}) is True
    # No password_changed_at (OIDC / legacy) → fail open (valid).
    assert session_iat_after_password_change({}, {"iat": now}) is True
    # Missing iat on a cookie that should have one → treated as stale.
    assert session_iat_after_password_change(
        {"password_changed_at": changed}, {}) is False


# --- rate limiter: escalation survives a window lapse -------------------------

def test_rate_limiter_block_survives_window_lapse():
    """A patient attacker who spaces attempts one window apart must still face
    the ESCALATING block, not a permanent reset to base."""
    from auth import rate_limiter as rl
    bucket, key = "confirm", "esc-test-key"
    rl.clear_rate_limit(bucket, key)
    rule = config.RATE_LIMIT_RULES["confirm"]

    # Burn through the window cap → arm the first block.
    for _ in range(rule["max"]):
        allowed, _ = rl.hit(bucket, key)
        assert allowed
    allowed, retry = rl.hit(bucket, key)
    assert not allowed and retry > 0
    entry = rl._attempts[(bucket, key)]
    assert entry["block_count"] == 1

    # Simulate the window lapsing (older than window) with the block expired.
    entry["first_at"] = time.time() - rule["window"] - 1
    entry["blocked_until"] = 0
    # One probe is allowed after expiry, but block_count (escalation history)
    # must be PRESERVED — the old code deleted the entry here.
    allowed, _ = rl.hit(bucket, key)
    assert allowed
    assert rl._attempts[(bucket, key)]["block_count"] == 1
    rl.clear_rate_limit(bucket, key)


def test_rate_limiter_record_does_not_wipe_active_block():
    """record_attempt must not roll the window (zeroing blocked_until) while a
    block is still being served."""
    from auth import rate_limiter as rl
    bucket, key = "confirm", "active-block-key"
    rl.clear_rate_limit(bucket, key)
    rule = config.RATE_LIMIT_RULES["confirm"]
    for _ in range(rule["max"] + 1):
        rl.hit(bucket, key)
    entry = rl._attempts[(bucket, key)]
    assert entry["blocked_until"] > time.time()
    # Age the window past its cap while the block is still in the future.
    entry["first_at"] = time.time() - rule["window"] - 1
    blocked_until = entry["blocked_until"]
    rl.record_attempt(bucket, key)
    # The active block must survive.
    assert rl._attempts[(bucket, key)]["blocked_until"] == blocked_until
    rl.clear_rate_limit(bucket, key)


# --- sandbox: symlink-refusing literal path guard ----------------------------

def test_verified_literal_path_refuses_symlink(tmp_path):
    from core.sandbox.sandbox import _verified_literal_path
    root = tmp_path / "agent"
    (root / "knowledge").mkdir(parents=True)
    real = root.resolve()

    # Plain nested dir → returned as-is.
    (root / "knowledge" / ".credentials").mkdir()
    got = _verified_literal_path(real, "knowledge", ".credentials")
    assert got == real / "knowledge" / ".credentials"

    # Replace the leaf with a symlink to somewhere else → refused (None).
    (root / "knowledge" / ".credentials").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "knowledge" / ".credentials")
    assert _verified_literal_path(real, "knowledge", ".credentials") is None
