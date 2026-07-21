"""Tests for the agent_api_keys / user_api_keys system.

Covers:
- otok_ key generation + format
- bcrypt hash round-trip
- verify_bearer_for_agent / _for_user accept valid keys
- verify_bearer_for_agent rejects user keys (scope mismatch)
- verify_bearer rejects revoked keys
- verify_bearer rejects keys without required permission
- verify_bearer rejects master PROXY_API_KEY (security boundary)
- verify_bearer rejects malformed/missing tokens
- last_used_at updates on successful verify
- permission validation (unknown / disabled in v1)
- Soft revocation preserves audit trail

Run: cd proxy && python -m pytest tests/auth/test_api_keys.py -v
"""

import sys
from datetime import datetime, timezone

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _seed_user_with_username(sub: str, username: str):
    """Insert a user with a username so resolve_username_to_sub works."""
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login, username) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (sub) DO UPDATE SET username=%s",
            (sub, f"{username}@test.com", username, "creator", now, now, username, username),
        )
        conn.commit()


# ───────────────────────────────────────────────────────────────────────────
# Generation + format
# ───────────────────────────────────────────────────────────────────────────


def test_generate_key_format(temp_db):
    from services.infra import api_key_manager as akm
    raw, prefix = akm._generate_raw_key()
    assert raw.startswith("otok_")
    assert len(prefix) == akm.KEY_INDEX_PREFIX_LEN
    assert prefix == raw[len("otok_"):][:akm.KEY_INDEX_PREFIX_LEN]


def test_generated_keys_are_unique(temp_db):
    """Two consecutive calls generate distinct keys."""
    from services.infra import api_key_manager as akm
    raw1, _ = akm._generate_raw_key()
    raw2, _ = akm._generate_raw_key()
    assert raw1 != raw2


def test_bcrypt_roundtrip(temp_db):
    from services.infra import api_key_manager as akm
    raw = "otok_test_value_xyz"
    hashed = akm._hash_key(raw)
    assert akm._check_key(raw, hashed)
    assert not akm._check_key("otok_other", hashed)


def test_check_key_handles_bad_hash(temp_db):
    from services.infra import api_key_manager as akm
    assert not akm._check_key("otok_x", "not-a-bcrypt-hash")


# ───────────────────────────────────────────────────────────────────────────
# Agent key creation + verification
# ───────────────────────────────────────────────────────────────────────────


def test_create_agent_key_returns_raw(temp_db):
    from services.infra import api_key_manager as akm
    row, raw = akm.create_agent_key(
        agent="agent-x", name="GitHub", permissions=["triggers"], created_by="user-admin",
    )
    assert raw.startswith("otok_")
    assert row["agent"] == "agent-x"
    assert row["name"] == "GitHub"
    assert row["prefix"] in raw


def test_verify_bearer_for_agent_accepts_valid(temp_db):
    from services.infra import api_key_manager as akm
    row, raw = akm.create_agent_key(
        agent="agent-x", name="GH", permissions=["triggers"], created_by="user-admin",
    )
    matched = akm.verify_bearer_for_agent(f"Bearer {raw}", agent="agent-x")
    assert matched["id"] == row["id"]


def test_verify_bearer_for_agent_rejects_wrong_agent(temp_db):
    from services.infra import api_key_manager as akm
    _, raw = akm.create_agent_key(
        agent="agent-x", name="GH", permissions=["triggers"], created_by="user-admin",
    )
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent(f"Bearer {raw}", agent="agent-other")
    assert e.value.code == "scope"


def test_verify_bearer_for_agent_rejects_master_key(temp_db):
    """The master PROXY_API_KEY must never authorize a webhook fire."""
    import config
    from services.infra import api_key_manager as akm
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent(f"Bearer {config.API_KEY}", agent="agent-x")
    assert e.value.code == "master"


def test_verify_bearer_for_agent_rejects_bad_format(temp_db):
    from services.infra import api_key_manager as akm
    # Missing Bearer prefix
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent("otok_abc", agent="agent-x")
    assert e.value.code == "format"
    # Missing otok_ prefix
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent("Bearer xyz", agent="agent-x")
    assert e.value.code == "format"
    # Empty
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent(None, agent="agent-x")
    assert e.value.code == "format"
    # Too short
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent("Bearer otok_a", agent="agent-x")
    assert e.value.code == "format"


def test_verify_bearer_for_agent_rejects_unknown_key(temp_db):
    """A made-up key with valid format → 'unknown' (no row matches)."""
    from services.infra import api_key_manager as akm
    fake = "otok_" + "a" * 40
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent(f"Bearer {fake}", agent="agent-x")
    assert e.value.code == "unknown"


def test_verify_bearer_for_agent_rejects_revoked(temp_db):
    from services.infra import api_key_manager as akm
    row, raw = akm.create_agent_key(
        agent="agent-x", name="GH", permissions=["triggers"], created_by="user-admin",
    )
    akm.revoke_agent_key(row["id"])
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent(f"Bearer {raw}", agent="agent-x")
    # Revoked rows are filtered at the prefix lookup → looks like 'unknown'.
    assert e.value.code == "unknown"


def test_verify_bearer_for_agent_rejects_missing_permission(temp_db):
    """A key without 'triggers' permission can't fire trigger webhooks."""
    from services.infra import api_key_manager as akm
    # Bypass the validator (which would reject empty perms) by constructing
    # the row directly with no permissions.
    from storage import api_key_store
    raw, prefix = akm._generate_raw_key()
    api_key_store.create_agent_api_key(
        agent="agent-x", name="No-perm", key_hash=akm._hash_key(raw),
        prefix=prefix, permissions=[], created_by="user-admin",
    )
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent(f"Bearer {raw}", agent="agent-x", required_permission="triggers")
    assert e.value.code == "permission"


def test_verify_bearer_updates_last_used(temp_db):
    from services.infra import api_key_manager as akm
    from storage import api_key_store
    row, raw = akm.create_agent_key(
        agent="agent-x", name="GH", permissions=["triggers"], created_by="user-admin",
    )
    assert row.get("last_used_at") is None
    akm.verify_bearer_for_agent(f"Bearer {raw}", agent="agent-x")
    refreshed = api_key_store.get_agent_api_key(row["id"])
    assert refreshed["last_used_at"] is not None


# ───────────────────────────────────────────────────────────────────────────
# User key creation + verification
# ───────────────────────────────────────────────────────────────────────────


def test_create_user_key_returns_raw(temp_db):
    from services.infra import api_key_manager as akm
    row, raw = akm.create_user_key(
        user_sub="user-test", name="Personal", permissions=["triggers"],
    )
    assert raw.startswith("otok_")
    assert row["user_sub"] == "user-test"


def test_verify_bearer_for_user_accepts_valid(temp_db):
    from services.infra import api_key_manager as akm
    _seed_user_with_username("user-alice", "alice")
    _, raw = akm.create_user_key(
        user_sub="user-alice", name="Personal", permissions=["triggers"],
    )
    matched = akm.verify_bearer_for_user(f"Bearer {raw}", username="alice")
    assert matched["user_sub"] == "user-alice"


def test_verify_bearer_for_user_rejects_wrong_username(temp_db):
    from services.infra import api_key_manager as akm
    _seed_user_with_username("user-alice", "alice")
    _seed_user_with_username("user-bob", "bob")
    _, raw = akm.create_user_key(
        user_sub="user-alice", name="Personal", permissions=["triggers"],
    )
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_user(f"Bearer {raw}", username="bob")
    assert e.value.code == "scope"


def test_verify_bearer_for_user_rejects_master(temp_db):
    import config
    from services.infra import api_key_manager as akm
    _seed_user_with_username("user-alice", "alice")
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_user(f"Bearer {config.API_KEY}", username="alice")
    assert e.value.code == "master"


def test_verify_bearer_for_user_rejects_unknown_username(temp_db):
    """User key for nonexistent username → scope mismatch (don't leak user-existence)."""
    from services.infra import api_key_manager as akm
    _, raw = akm.create_user_key(
        user_sub="user-alice", name="P", permissions=["triggers"],
    )
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_user(f"Bearer {raw}", username="nonexistent")
    assert e.value.code == "scope"


def test_agent_key_does_not_authorize_user_webhook(temp_db):
    """Cross-key-type attack: agent key on user URL must reject."""
    from services.infra import api_key_manager as akm
    _seed_user_with_username("user-alice", "alice")
    _, agent_raw = akm.create_agent_key(
        agent="agent-x", name="A", permissions=["triggers"], created_by="user-admin",
    )
    # Agent key would never match user_api_keys prefix index → "unknown"
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_user(f"Bearer {agent_raw}", username="alice")
    assert e.value.code == "unknown"


def test_user_key_does_not_authorize_agent_webhook(temp_db):
    """Reverse: user key on agent URL must reject."""
    from services.infra import api_key_manager as akm
    _, user_raw = akm.create_user_key(
        user_sub="user-alice", name="P", permissions=["triggers"],
    )
    with pytest.raises(akm.KeyMismatch) as e:
        akm.verify_bearer_for_agent(f"Bearer {user_raw}", agent="agent-x")
    assert e.value.code == "unknown"


# ───────────────────────────────────────────────────────────────────────────
# Permission validation
# ───────────────────────────────────────────────────────────────────────────


def test_validate_user_permissions_rejects_unknown(temp_db):
    from services.infra import api_key_manager as akm
    with pytest.raises(ValueError):
        akm.validate_user_permissions(["triggers", "made-up"])


def test_validate_user_permissions_rejects_disabled_in_v1(temp_db):
    from services.infra import api_key_manager as akm
    # 'chat' is in ALL_USER_PERMISSIONS but not ENABLED_USER_PERMISSIONS_V1
    with pytest.raises(ValueError) as e:
        akm.validate_user_permissions(["chat"])
    assert "v1" in str(e.value).lower() or "supported" in str(e.value).lower()


def test_validate_user_permissions_canonicalises(temp_db):
    """Unknown values get dropped if validator allowed them; here we test
    that valid duplicates are deduped."""
    from services.infra import api_key_manager as akm
    out = akm.validate_user_permissions(["triggers", "triggers"])
    assert out == ["triggers"]


def test_validate_agent_permissions_rejects_disabled(temp_db):
    from services.infra import api_key_manager as akm
    with pytest.raises(ValueError):
        akm.validate_agent_permissions(["chat"])


# ───────────────────────────────────────────────────────────────────────────
# Soft revocation
# ───────────────────────────────────────────────────────────────────────────


def test_revocation_is_soft(temp_db):
    """Revoke flips revoked_at but doesn't delete the row (audit trail)."""
    from services.infra import api_key_manager as akm
    from storage import api_key_store
    row, _ = akm.create_agent_key(
        agent="agent-x", name="GH", permissions=["triggers"], created_by="user-admin",
    )
    assert akm.revoke_agent_key(row["id"])
    # Soft-deleted row still readable
    after = api_key_store.get_agent_api_key(row["id"])
    assert after is not None
    assert after["revoked_at"] is not None


def test_double_revocation_is_idempotent(temp_db):
    from services.infra import api_key_manager as akm
    row, _ = akm.create_agent_key(
        agent="agent-x", name="GH", permissions=["triggers"], created_by="user-admin",
    )
    assert akm.revoke_agent_key(row["id"])
    # Second revoke returns False (already revoked) but doesn't error.
    assert not akm.revoke_agent_key(row["id"])


def test_listing_excludes_revoked_by_default(temp_db):
    from services.infra import api_key_manager as akm
    from storage import api_key_store
    row1, _ = akm.create_agent_key(
        agent="agent-x", name="A1", permissions=["triggers"], created_by="user-admin",
    )
    row2, _ = akm.create_agent_key(
        agent="agent-x", name="A2", permissions=["triggers"], created_by="user-admin",
    )
    akm.revoke_agent_key(row1["id"])
    active = api_key_store.list_agent_api_keys(agent="agent-x")
    ids = [r["id"] for r in active]
    assert row2["id"] in ids
    assert row1["id"] not in ids
    # With include_revoked=True, both appear.
    all_keys = api_key_store.list_agent_api_keys(agent="agent-x", include_revoked=True)
    all_ids = [r["id"] for r in all_keys]
    assert row1["id"] in all_ids
    assert row2["id"] in all_ids
