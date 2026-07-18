"""Encrypted credential storage for MCP server credentials.

Five tables:
  - user_credentials:            per-user, per-account credential rows
                                 (one or more labeled accounts per
                                 (user_sub, mcp_name); each row carries
                                 ``account_label``).
  - user_credential_accounts:    account list — one row per labeled account a
                                 user has connected. ``is_default=TRUE`` picks
                                 the catch-all account used by agents without
                                 an explicit binding.
  - agent_account_bindings:      per-agent override — pin a specific account
                                 for a specific agent. Takes precedence over
                                 the user's default account.
  - infra_credentials:           shared infrastructure creds (Uptime Kuma,
                                 UniFi, HA, etc.). Single tier, no accounts.
  - service_agent_bindings:      per-agent service identity — pins an agent to
                                 a USER's own connected account (the binding's
                                 ``account_owner_sub``). Agent-scope sessions
                                 read that user's tokens. There is no platform
                                 "service account" storage.

All values are Fernet-encrypted at rest. Encryption key is derived from
CREDENTIAL_ENCRYPTION_KEY env var (fallback: JWT_SECRET).

All functions are synchronous (called via asyncio.to_thread from async code).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import datetime, timezone

import config
from storage.pg import get_conn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise RuntimeError("cryptography package required – pip install cryptography")
    raw = os.environ.get("CREDENTIAL_ENCRYPTION_KEY") or config.JWT_SECRET
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    _fernet = Fernet(key)
    return _fernet


def _encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(enc: str) -> str:
    return _get_fernet().decrypt(enc.encode()).decode()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# User credentials (per-user, per-account)
# ---------------------------------------------------------------------------

def get_user_credentials(
    user_sub: str, mcp_name: str, account_label: str,
) -> dict[str, str]:
    """Return {credential_key: decrypted_value} for one account."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT credential_key, credential_value_enc FROM user_credentials "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["credential_key"]] = _decrypt(r["credential_value_enc"])
            except Exception:
                logger.warning(
                    "Failed to decrypt user credential %s/%s/%s/%s (key mismatch? see the CREDENTIAL KEY MISMATCH boot check)",
                    user_sub[:8], mcp_name, account_label, r["credential_key"],
                )
        return result


def set_user_credentials(
    user_sub: str, mcp_name: str, credentials: dict[str, str],
    account_label: str,
) -> None:
    """Insert or update credentials for one account.

    Also creates the matching ``user_credential_accounts`` row (idempotent)
    so the account is visible to the resolver / dashboard. The first
    account ever created for a (user_sub, mcp) is marked ``is_default=TRUE``
    automatically; subsequent accounts default to ``is_default=FALSE``
    and the user picks one via ``set_default_account``.
    """
    now = _now()
    with get_conn() as conn:
        # 1. Persist the credential rows.
        for key, value in credentials.items():
            enc = _encrypt(value)
            conn.execute(
                """INSERT INTO user_credentials
                   (user_sub, mcp_name, account_label, credential_key,
                    credential_value_enc, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT(user_sub, mcp_name, account_label, credential_key)
                   DO UPDATE SET credential_value_enc=EXCLUDED.credential_value_enc,
                                 updated_at=EXCLUDED.updated_at""",
                (user_sub, mcp_name, account_label, key, enc, now, now),
            )

        # 2. Auto-create the account row if it doesn't exist.
        existing_default = conn.execute(
            "SELECT 1 FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND is_default=TRUE",
            (user_sub, mcp_name),
        ).fetchone()
        is_default = not bool(existing_default)
        conn.execute(
            """INSERT INTO user_credential_accounts
               (user_sub, mcp_name, account_label, display_email,
                is_default, created_at)
               VALUES (%s, %s, %s, '', %s, %s)
               ON CONFLICT (user_sub, mcp_name, account_label) DO NOTHING""",
            (user_sub, mcp_name, account_label, is_default, now),
        )
        conn.commit()


def delete_user_credentials(
    user_sub: str, mcp_name: str, account_label: str,
) -> None:
    """Delete one labeled account: credential rows, account row, and
    any per-agent bindings pinned to that label."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM user_credentials "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        )
        conn.execute(
            "DELETE FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        )
        conn.execute(
            "DELETE FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        )
        conn.commit()


def get_all_user_credentials(
    user_sub: str, account_label: str,
) -> dict[str, dict[str, str]]:
    """Return {mcp_name: {key: value}} for one user, one account label.

    Per-MCP scoped — useful for diagnostics. For multi-account browsing,
    use ``list_user_accounts(user_sub, mcp_name)``.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, credential_key, credential_value_enc "
            "FROM user_credentials WHERE user_sub=%s AND account_label=%s",
            (user_sub, account_label),
        ).fetchall()
        result: dict[str, dict[str, str]] = {}
        for r in rows:
            mcp = r["mcp_name"]
            if mcp not in result:
                result[mcp] = {}
            try:
                result[mcp][r["credential_key"]] = _decrypt(r["credential_value_enc"])
            except Exception:
                logger.warning(
                    "Failed to decrypt %s/%s/%s/%s (key mismatch? see the CREDENTIAL KEY MISMATCH boot check)",
                    user_sub[:8], mcp, account_label, r["credential_key"],
                )
        return result


# ---------------------------------------------------------------------------
# Account list management
# ---------------------------------------------------------------------------

def list_user_accounts(user_sub: str, mcp_name: str) -> list[dict]:
    """Return [{account_label, display_email, is_default, created_at}, ...]
    for every account a user has connected for this MCP.

    Sorted: ``is_default`` first, then ``created_at`` ascending.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT account_label, display_email, is_default, created_at "
            "FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s "
            "ORDER BY is_default DESC, created_at ASC",
            (user_sub, mcp_name),
        ).fetchall()
        return [dict(r) for r in rows]


def get_default_account(user_sub: str, mcp_name: str) -> str | None:
    """Return the ⭐ default ``account_label`` for (user_sub, mcp_name), or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_label FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND is_default=TRUE",
            (user_sub, mcp_name),
        ).fetchone()
        return row["account_label"] if row else None


def set_default_account(
    user_sub: str, mcp_name: str, account_label: str,
) -> bool:
    """Mark one account as the default for (user_sub, mcp_name).

    Atomically unsets ``is_default`` from any other account so the partial
    unique index never fires.

    Returns False if ``account_label`` doesn't exist for this user+mcp
    (no-op), True on success.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        ).fetchone()
        if not row:
            return False
        # Clear current default first to keep the partial unique index happy.
        conn.execute(
            "UPDATE user_credential_accounts SET is_default=FALSE "
            "WHERE user_sub=%s AND mcp_name=%s AND is_default=TRUE",
            (user_sub, mcp_name),
        )
        conn.execute(
            "UPDATE user_credential_accounts SET is_default=TRUE "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        )
        conn.commit()
        return True


def set_account_display_email(
    user_sub: str, mcp_name: str, account_label: str, display_email: str,
) -> None:
    """Update an account's display email (e.g. after OAuth userinfo)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_credential_accounts SET display_email=%s "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (display_email, user_sub, mcp_name, account_label),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Per-agent account bindings
# ---------------------------------------------------------------------------

def get_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str,
) -> str | None:
    """Return the bound ``account_label`` for (user, mcp, agent), or None.

    None means "no explicit binding — fall back to default account".
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_label FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND agent_name=%s",
            (user_sub, mcp_name, agent_name),
        ).fetchone()
        return row["account_label"] if row else None


def set_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str, account_label: str,
) -> bool:
    """Pin an agent to a specific account. Upsert via UNIQUE constraint.

    Returns False if ``account_label`` doesn't exist (no-op), True on success.
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        ).fetchone()
        if not existing:
            return False
        now = _now()
        conn.execute(
            """INSERT INTO agent_account_bindings
               (user_sub, mcp_name, agent_name, account_label, set_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT(user_sub, mcp_name, agent_name)
               DO UPDATE SET account_label=EXCLUDED.account_label,
                             set_at=EXCLUDED.set_at""",
            (user_sub, mcp_name, agent_name, account_label, now),
        )
        conn.commit()
        return True


def remove_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str,
) -> None:
    """Drop the per-agent override (agent reverts to user's default account)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND agent_name=%s",
            (user_sub, mcp_name, agent_name),
        )
        conn.commit()


def list_agent_account_bindings(user_sub: str, mcp_name: str) -> list[dict]:
    """Return all per-agent bindings for (user_sub, mcp_name) — used in UI."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, account_label, set_at "
            "FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s "
            "ORDER BY agent_name ASC",
            (user_sub, mcp_name),
        ).fetchall()
        return [dict(r) for r in rows]


def startup_key_canary() -> None:
    """Boot-time probe: can the current key decrypt what's already stored?

    The Fernet key derives from JWT_SECRET (or CREDENTIAL_ENCRYPTION_KEY), so
    a recreated config.env silently orphans every encrypted row: 2FA 500s at
    login, provider subscriptions report "no subscription", MCP/phone creds
    read back empty. Sampling a few rows per store at boot turns that
    multi-hour mystery into one log line. Diagnosis only — never raises,
    never blocks boot.
    """
    probes = [
        ("user MCP credentials",
         "SELECT credential_value_enc AS v FROM user_credentials "
         "WHERE credential_value_enc <> '' LIMIT 5"),
        ("infrastructure credentials",
         "SELECT credential_value_enc AS v FROM infra_credentials "
         "WHERE credential_value_enc <> '' LIMIT 5"),
        ("provider subscriptions",
         "SELECT credential_data_enc AS v FROM execution_layer_subscriptions "
         "WHERE credential_data_enc <> '' LIMIT 5"),
        ("2FA/TOTP enrollments",
         "SELECT totp_secret_enc AS v FROM users "
         "WHERE totp_secret_enc IS NOT NULL AND totp_secret_enc <> '' LIMIT 5"),
    ]
    bad: list[str] = []
    try:
        with get_conn() as conn:
            for store, sql in probes:
                try:
                    rows = conn.execute(sql).fetchall()
                except Exception:
                    continue  # table absent (partial install) — not this probe's job
                for r in rows:
                    try:
                        _decrypt(r["v"])
                    except Exception:
                        bad.append(store)
                        break
    except Exception as e:  # canary must never take the boot down with it
        logger.debug("credential key canary skipped: %s", e)
        return
    if bad:
        logger.error(
            "CREDENTIAL KEY MISMATCH: stored %s cannot be decrypted with the "
            "current key. The encryption key derives from JWT_SECRET in "
            "config.env — if config.env was recreated (e.g. the install moved "
            "to a new folder), every previously saved secret is unreadable: "
            "2FA login will fail with 500, provider subscriptions will report "
            "'no subscription', MCP/phone credentials will read back empty. "
            "Fix: restore the original JWT_SECRET in config.env and restart; "
            "or re-enroll 2FA and re-connect the affected credentials under "
            "the new key.",
            " + ".join(bad),
        )


# ---------------------------------------------------------------------------
# Infrastructure credentials (shared, admin-only)
# ---------------------------------------------------------------------------

def get_infra_credentials(mcp_name: str) -> dict[str, str]:
    """Return {key: decrypted_value} for an infrastructure MCP."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT credential_key, credential_value_enc FROM infra_credentials "
            "WHERE mcp_name=%s",
            (mcp_name,),
        ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["credential_key"]] = _decrypt(r["credential_value_enc"])
            except Exception:
                logger.warning("Failed to decrypt infra credential %s/%s (key mismatch? see the CREDENTIAL KEY MISMATCH boot check)",
                               mcp_name, r["credential_key"])
        return result


def set_infra_credentials(mcp_name: str, credentials: dict[str, str]) -> None:
    """Insert or update infrastructure credentials."""
    now = _now()
    with get_conn() as conn:
        for key, value in credentials.items():
            enc = _encrypt(value)
            conn.execute(
                """INSERT INTO infra_credentials
                   (mcp_name, credential_key, credential_value_enc,
                    created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT(mcp_name, credential_key)
                   DO UPDATE SET credential_value_enc=EXCLUDED.credential_value_enc,
                                 updated_at=EXCLUDED.updated_at""",
                (mcp_name, key, enc, now, now),
            )
        conn.commit()


def set_infra_credentials_if_absent(
    mcp_name: str, credentials: dict[str, str],
) -> dict[str, str]:
    """Insert infra credentials only if absent — first-writer-wins.

    Unlike ``set_infra_credentials`` (which overwrites), this uses
    ``ON CONFLICT DO NOTHING`` so a value minted concurrently is never
    clobbered, then reads the rows back on the same connection and returns the
    **effective** decrypted values (whoever won the race). Lets two callers that
    both generate a fresh secret converge on the single stored value — used to
    mint a per-server register secret idempotently from either the config-push
    or the snippet-render path.
    """
    now = _now()
    with get_conn() as conn:
        for key, value in credentials.items():
            enc = _encrypt(value)
            conn.execute(
                """INSERT INTO infra_credentials
                   (mcp_name, credential_key, credential_value_enc,
                    created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT(mcp_name, credential_key) DO NOTHING""",
                (mcp_name, key, enc, now, now),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT credential_key, credential_value_enc FROM infra_credentials "
            "WHERE mcp_name=%s",
            (mcp_name,),
        ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["credential_key"]] = _decrypt(r["credential_value_enc"])
            except Exception:
                logger.warning("Failed to decrypt infra credential %s/%s (key mismatch? see the CREDENTIAL KEY MISMATCH boot check)",
                               mcp_name, r["credential_key"])
        return result


def delete_infra_credentials(mcp_name: str) -> None:
    """Remove all infrastructure credentials for an MCP."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM infra_credentials WHERE mcp_name=%s", (mcp_name,)
        )
        conn.commit()


def delete_infra_credential_key(mcp_name: str, credential_key: str) -> None:
    """Remove a SINGLE infra credential key, leaving other keys in the bundle.

    Used to clear the OtoDock ``account_token`` on disconnect without dropping the
    shared ``otodock-relay`` bundle's event-forward secret.
    """
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM infra_credentials WHERE mcp_name=%s AND credential_key=%s",
            (mcp_name, credential_key),
        )
        conn.commit()


def get_all_infra_credentials() -> dict[str, dict[str, str]]:
    """Return {mcp_name: {key: decrypted_value}} for all infrastructure MCPs."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, credential_key, credential_value_enc "
            "FROM infra_credentials"
        ).fetchall()
        result: dict[str, dict[str, str]] = {}
        for r in rows:
            mcp = r["mcp_name"]
            if mcp not in result:
                result[mcp] = {}
            try:
                result[mcp][r["credential_key"]] = _decrypt(r["credential_value_enc"])
            except Exception:
                logger.warning("Failed to decrypt infra %s/%s (key mismatch? see the CREDENTIAL KEY MISMATCH boot check)",
                               mcp, r["credential_key"])
        return result


# ---------------------------------------------------------------------------
# Agent-scope credentials: per-agent bindings to a user's own account
# ---------------------------------------------------------------------------
# Agent-scope (service) sessions resolve credentials through a per-agent
# binding in `service_agent_bindings`, which ALWAYS points at a user's own
# `user_credential_accounts` row (a manager/admin designates one of their
# connected accounts as the agent's service identity). There is no platform
# "service account" storage — user accounts are reused directly.


def delete_all_mcp_credentials(mcp_name: str) -> None:
    """Remove all credentials for an MCP — infra, per-agent service bindings,
    and every user's accounts + bindings + credential rows for that MCP."""
    with get_conn() as conn:
        conn.execute("DELETE FROM infra_credentials WHERE mcp_name=%s", (mcp_name,))
        conn.execute(
            "DELETE FROM service_agent_bindings WHERE mcp_name=%s", (mcp_name,)
        )
        conn.execute("DELETE FROM user_credentials WHERE mcp_name=%s", (mcp_name,))
        conn.execute(
            "DELETE FROM user_credential_accounts WHERE mcp_name=%s", (mcp_name,)
        )
        conn.execute(
            "DELETE FROM agent_account_bindings WHERE mcp_name=%s", (mcp_name,)
        )
        conn.commit()


def cleanup_service_agent_bindings_for_owner(owner_sub: str) -> list[dict]:
    """Drop every ``service_agent_bindings`` row pointing at a deleted user's
    account. Affected agents lose that MCP at next agent-scope resolve (no
    platform default to fall back on).

    Returns the rows BEFORE delete so callers can audit/log. Called from the
    user-delete cascade — the user's ``user_credential_accounts`` rows are
    cleaned up by FK cascade; this removes the bindings that referenced them.
    """
    if not owner_sub:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, agent_name, account_label "
            "FROM service_agent_bindings WHERE account_owner_sub=%s",
            (owner_sub,),
        ).fetchall()
        snapshot = [dict(r) for r in rows]
        conn.execute(
            "DELETE FROM service_agent_bindings WHERE account_owner_sub=%s",
            (owner_sub,),
        )
        conn.commit()
        return snapshot


# ---------------------------------------------------------------------------
# Per-agent service-account bindings
# ---------------------------------------------------------------------------

def get_service_agent_binding(
    mcp_name: str, agent_name: str,
) -> tuple[str, str] | None:
    """Return ``(account_label, account_owner_sub)`` for (mcp, agent), or None.

    ``account_owner_sub`` is always a real ``<user_sub>`` — the binding points
    at that user's ``user_credential_accounts(<sub>, mcp, account_label)`` row
    (a manager/admin designated their connected account as the agent's service
    identity for agent-scope sessions). ``None`` → no binding; the agent gets
    no credential for this MCP in agent scope (no platform default).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_label, account_owner_sub FROM service_agent_bindings "
            "WHERE mcp_name=%s AND agent_name=%s",
            (mcp_name, agent_name),
        ).fetchone()
        if not row:
            return None
        return (row["account_label"], row["account_owner_sub"])


def set_service_agent_binding(
    mcp_name: str, agent_name: str, *,
    account_label: str, owner_sub: str, set_by: str = "",
) -> bool:
    """Pin an agent to a user's own connected account as its service identity.

    The bound account is identified by ``(owner_sub, mcp_name, account_label)``
    in ``user_credential_accounts``. ``owner_sub`` MUST be a real user_sub —
    there is no platform-tier account. Validates the target row exists first.

    Returns False if ``owner_sub`` is empty or the target row doesn't exist
    (no-op), True on success.
    """
    if not owner_sub:
        return False
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (owner_sub, mcp_name, account_label),
        ).fetchone()
        if not existing:
            return False
        now = _now()
        conn.execute(
            """INSERT INTO service_agent_bindings
               (mcp_name, agent_name, account_label, account_owner_sub, set_by, set_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT(mcp_name, agent_name)
               DO UPDATE SET account_label=EXCLUDED.account_label,
                             account_owner_sub=EXCLUDED.account_owner_sub,
                             set_by=EXCLUDED.set_by,
                             set_at=EXCLUDED.set_at""",
            (mcp_name, agent_name, account_label, owner_sub, set_by, now),
        )
        conn.commit()
        return True


def remove_service_agent_binding(
    mcp_name: str, agent_name: str,
) -> None:
    """Drop the per-agent service binding (the agent is left with no service
    identity for this MCP until a new binding is set)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM service_agent_bindings "
            "WHERE mcp_name=%s AND agent_name=%s",
            (mcp_name, agent_name),
        )
        conn.commit()


def list_service_agent_bindings(mcp_name: str) -> list[dict]:
    """Return all per-agent service-account bindings for this MCP — used in UI.

    Each row has ``{agent_name, account_label, account_owner_sub, set_by, set_at}``.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, account_label, account_owner_sub, set_by, set_at "
            "FROM service_agent_bindings "
            "WHERE mcp_name=%s "
            "ORDER BY agent_name ASC",
            (mcp_name,),
        ).fetchall()
        return [dict(r) for r in rows]


