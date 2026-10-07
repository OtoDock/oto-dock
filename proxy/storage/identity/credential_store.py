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

All values are Fernet-encrypted at rest under a MultiFernet: the keys of
``CREDENTIAL_ENCRYPTION_KEY`` (config.env or the environment; the first one
encrypts) and then the key derived from JWT_SECRET, so a row written before
a key was configured still opens.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import config
from storage.pg import get_conn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

_fernet = None


def _configured_keys() -> list[str]:
    return [k.strip() for k in config.credential_key_setting().split(",") if k.strip()]


def _derived_key(raw: str) -> bytes:
    """A value's Fernet key: ``sha256(value)``, the derivation 1.7.0 used for
    ``CREDENTIAL_ENCRYPTION_KEY`` and ``JWT_SECRET`` alike, so any string
    works and a key set in the environment before keeps its rows."""
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:
        raise RuntimeError("cryptography package required – pip install cryptography")
    keys = _configured_keys()
    # 1.7.0 took the whole value as one key: one with a comma or surrounding
    # whitespace in it still opens the rows it wrote (decrypt only).
    whole = config.credential_key_setting()
    raws = keys + ([whole] if whole and whole not in keys else []) + [config.JWT_SECRET]
    _fernet = MultiFernet([Fernet(_derived_key(raw)) for raw in raws])
    return _fernet


def _encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(enc: str) -> str:
    return _get_fernet().decrypt(enc.encode()).decode()


def encrypt_secret(value: str) -> str:
    """Encrypt a secret another table stores (e.g. a machine's browser
    extension token) with the credential-store key, so it shares the same
    at-rest guarantees. The boot key canary does not sample such columns:
    a key mismatch surfaces where the value is read (the reader degrades)."""
    return _encrypt(value)


def decrypt_secret(enc: str) -> str:
    return _decrypt(enc)


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


#: The platform setting that remembers the fingerprint of every value the
#: secret floor accepted, per secret (never listed by any route).
FLOOR_SETTING = "secret_floor_fingerprints"
_FLOOR_LABEL = b"otodock secret floor"
_FLOOR_KEEP = 16


def _fingerprint(value: str) -> str:
    return hmac.new(value.encode(), _FLOOR_LABEL, hashlib.sha256).hexdigest()[:32]


def _install_has_users() -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT EXISTS (SELECT 1 FROM users) AS e").fetchone()["e"]


def judge_secret_floor() -> None:
    """The length floor on ``JWT_SECRET`` and on the first configured
    credential key (``config.SECRET_MIN_LENGTH``), judged at boot after the
    schema. A short value an install already ran on only warns: one seen at
    an earlier boot, or, at the first boot that judges (the upgrade), the
    value an install with users is running on. A short value never seen
    before (a fresh install, or a value newly set) refuses the boot. Every
    accepted fingerprint is remembered, so restoring an earlier value (the
    key canary's own advice) is never refused. A database error skips the
    judge: the boot must not fail on it."""
    checks = [("JWT_SECRET", config.JWT_SECRET)]
    keys = _configured_keys()
    if keys:
        checks.append(("CREDENTIAL_ENCRYPTION_KEY", keys[0]))
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM platform_settings WHERE key=%s",
                               (FLOOR_SETTING,)).fetchone()
        record = json.loads(row["value"]) if row and row["value"] else None
        has_users = _install_has_users() if record is None else True
    except Exception as e:
        logger.debug("secret floor skipped: %s", e)
        return
    seen: dict[str, list[str]] = record if isinstance(record, dict) else {}
    floor = config.SECRET_MIN_LENGTH
    refused: list[str] = []
    for name, value in checks:
        fp = _fingerprint(value)
        known = list(seen.get(name) or [])
        if len(value) >= floor or fp in known or (record is None and has_users):
            if fp not in known:
                seen[name] = (known + [fp])[-_FLOOR_KEEP:]
            if len(value) < floor:
                logger.warning(_floor_advice(name, len(value)))
        else:
            refused.append(name)
    if refused:
        msg = (f"{' and '.join(refused)} must be at least {floor} characters: a new value "
               f"shorter than that is refused. Set a random value (openssl rand -base64 48) "
               f"in config.env and restart.")
        logger.critical(msg)
        raise RuntimeError(msg)
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO platform_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                (FLOOR_SETTING, json.dumps(seen, sort_keys=True)))
            conn.commit()
    except Exception as e:
        logger.debug("secret floor record not written: %s", e)


def _floor_advice(name: str, length: int) -> str:
    if name == "JWT_SECRET":
        return (f"JWT_SECRET is {length} characters, shorter than the "
                f"{config.SECRET_MIN_LENGTH} the platform asks for. Replacing it alone "
                "makes every stored secret unreadable; rotate it this way: in config.env "
                "set CREDENTIAL_ENCRYPTION_KEY=<a new random value>,<the current JWT_SECRET>, "
                "then set JWT_SECRET to a new random value (openssl rand -base64 48) and "
                "restart. Everyone signs in again.")
    return (f"CREDENTIAL_ENCRYPTION_KEY's first key is {length} characters, shorter than "
            f"the {config.SECRET_MIN_LENGTH} the platform asks for: put a new random value "
            "first and keep this one after it (it then only decrypts), and restart.")


def startup_key_canary() -> None:
    """Boot-time probe: can the current key decrypt what's already stored?

    The Fernet key derives from JWT_SECRET (or CREDENTIAL_ENCRYPTION_KEY), so
    a recreated config.env silently orphans every encrypted row: 2FA 500s at
    login, provider subscriptions report "no subscription", MCP/phone creds
    read back empty. Sampling a few rows per store at boot turns that
    multi-hour mystery into one log line. The probe itself is diagnosis
    only and never raises; the secret floor it runs first refuses a new
    short secret (``judge_secret_floor``).
    """
    judge_secret_floor()
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
        ("app secrets",
         "SELECT value_enc AS v FROM app_secrets WHERE value_enc <> '' LIMIT 5"),
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
            "current key. The encryption key derives from JWT_SECRET (and "
            "CREDENTIAL_ENCRYPTION_KEY when set) in config.env — if config.env was recreated (e.g. the install moved "
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


def list_service_agent_bindings_for_owner(owner_sub: str) -> list[dict]:
    """Every ``service_agent_bindings`` row lending this person's accounts,
    ``{mcp_name, agent_name, account_label}``, standing or not: the
    offboarding subscriber judges each against the person's standing on
    that agent and clears the ones that no longer hold."""
    if not owner_sub:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, agent_name, account_label "
            "FROM service_agent_bindings WHERE account_owner_sub=%s "
            "ORDER BY agent_name, mcp_name",
            (owner_sub,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Per-agent service-account bindings
# ---------------------------------------------------------------------------

def _owner_manages(owner_sub: str, agent_name: str) -> bool:
    """Whether the bound account's owner still manages ``agent_name``: a
    platform admin, or a per-agent manager. The binding lends the manager's
    OWN account, so it stands only while they hold that standing — the same
    re-check a share link's creator gets at every click (SHARING.md)."""
    from auth import roles
    from storage.identity import db_users
    user = db_users.get_user(owner_sub)
    if not user:
        return False
    return roles.can_manage(roles.effective_role(
        user.get("role"), db_users.get_user_agent_roles(owner_sub), agent_name))


def get_service_agent_binding(
    mcp_name: str, agent_name: str,
) -> tuple[str, str] | None:
    """Return ``(account_label, account_owner_sub)`` for (mcp, agent), or None.

    ``account_owner_sub`` is always a real ``<user_sub>`` — the binding points
    at that user's ``user_credential_accounts(<sub>, mcp, account_label)`` row
    (a manager/admin designated their connected account as the agent's service
    identity for agent-scope sessions). ``None`` → no binding, or a binding
    whose owner no longer manages the agent (demoted, unassigned, deleted):
    the row stays for the next bind or clear, but the agent gets no
    credential for this MCP in agent scope (no platform default).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_label, account_owner_sub FROM service_agent_bindings "
            "WHERE mcp_name=%s AND agent_name=%s",
            (mcp_name, agent_name),
        ).fetchone()
        if not row:
            return None
    if not _owner_manages(row["account_owner_sub"], agent_name):
        logger.debug("service binding %s/%s: owner %s no longer manages the agent",
                     mcp_name, agent_name, row["account_owner_sub"][:8])
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
    mcp_name: str, agent_name: str, *,
    owner_sub: str | None = None, account_label: str | None = None,
) -> bool:
    """Drop the per-agent service binding (the agent is left with no service
    identity for this MCP until a new binding is set). With ``owner_sub`` and
    ``account_label`` only while the binding still names that lender's
    account: a clear that ran for seconds never removes the binding a
    manager set meanwhile. Returns whether a row went."""
    sql = "DELETE FROM service_agent_bindings WHERE mcp_name=%s AND agent_name=%s"
    params: tuple = (mcp_name, agent_name)
    if owner_sub is not None:
        sql += " AND account_owner_sub=%s"
        params += (owner_sub,)
    if account_label is not None:
        sql += " AND account_label=%s"
        params += (account_label,)
    with get_conn() as conn:
        gone = conn.execute(sql, params).rowcount
        conn.commit()
    return bool(gone)


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


