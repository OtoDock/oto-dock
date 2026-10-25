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
                                 the user's default account, and carries the
                                 person's own GitHub reach for that agent.
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
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, NamedTuple

import psycopg
from cryptography.fernet import InvalidToken

import config
from storage.identity import oauth_app_kinds
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
    except ImportError as exc:
        raise RuntimeError("cryptography package required – pip install cryptography") from exc
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
            except (InvalidToken, UnicodeDecodeError):
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
            except (InvalidToken, UnicodeDecodeError):
                logger.warning(
                    "Failed to decrypt %s/%s/%s/%s (key mismatch? see the CREDENTIAL KEY MISMATCH boot check)",
                    user_sub[:8], mcp, account_label, r["credential_key"],
                )
        return result


# ---------------------------------------------------------------------------
# Account list management
# ---------------------------------------------------------------------------

def _parse_meta(value: Any) -> dict:
    """The ``meta`` column as a dict (``{}`` when empty or unreadable)."""
    if not value or not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _account_row(row: Any) -> dict:
    d = dict(row)
    d["meta"] = _parse_meta(d.get("meta"))
    return d


def list_user_accounts(user_sub: str, mcp_name: str) -> list[dict]:
    """Return [{account_label, display_email, is_default, created_at, meta},
    ...] for every account a user has connected for this MCP; ``meta`` is
    the parsed vendor meta (``{}`` on an account that keeps none).

    Sorted: ``is_default`` first, then ``created_at`` ascending.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT account_label, display_email, is_default, created_at, meta "
            "FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s "
            "ORDER BY is_default DESC, created_at ASC",
            (user_sub, mcp_name),
        ).fetchall()
        return [_account_row(r) for r in rows]


def get_account_meta(user_sub: str, mcp_name: str, account_label: str) -> dict:
    """The vendor meta of one account (``{}`` when none or no such row)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT meta FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        ).fetchone()
        return _parse_meta(row["meta"]) if row else {}


def set_account_meta(user_sub: str, mcp_name: str, account_label: str, meta: dict | None) -> bool:
    """Replace an account's vendor meta (``None`` or ``{}`` clears it).
    Returns whether the row exists."""
    with get_conn() as conn:
        updated = conn.execute(
            "UPDATE user_credential_accounts SET meta=%s "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (json.dumps(meta) if meta else "", user_sub, mcp_name, account_label),
        ).rowcount
        conn.commit()
        return bool(updated)


def update_account_meta(
    user_sub: str, mcp_name: str, account_label: str,
    change: Callable[[dict], dict | None],
) -> dict | None:
    """Rewrite an account's meta through ``change`` under the row lock (the
    app webhook and a Refresh may race): ``change`` gets the current meta
    and answers the new one (``None`` = leave it). Returns the meta as
    written, or ``None`` when the row does not exist."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT meta FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s FOR UPDATE",
            (user_sub, mcp_name, account_label),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        current = _parse_meta(row["meta"])
        new = change(dict(current))
        if new is None:
            conn.rollback()
            return current
        conn.execute(
            "UPDATE user_credential_accounts SET meta=%s "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (json.dumps(new) if new else "", user_sub, mcp_name, account_label),
        )
        conn.commit()
        return new


def _accounts_with_meta(mcp_names: list[str]) -> list[dict]:
    if not mcp_names:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_sub, mcp_name, account_label, display_email, meta "
            "FROM user_credential_accounts "
            "WHERE mcp_name = ANY(%s) AND meta <> ''",
            (list(mcp_names),),
        ).fetchall()
        return [_account_row(r) for r in rows]


def accounts_holding_installation(mcp_names: list[str], installation_id: str | int) -> list[dict]:
    """Every account (any person) whose meta lists ``installation_id``:
    ``{user_sub, mcp_name, account_label, display_email, meta}``."""
    return [
        a for a in _accounts_with_meta(mcp_names)
        if oauth_app_kinds.installation_in(a["meta"], installation_id) is not None
    ]


def accounts_by_github_user(mcp_names: list[str], user_id: int) -> list[dict]:
    """Every GitHub-App account whose meta says the person is GitHub user
    ``user_id`` (an id, never a login: logins are renamed and reclaimed)."""
    return [
        a for a in _accounts_with_meta(mcp_names)
        if oauth_app_kinds.is_github_app_meta(a["meta"]) and a["meta"].get("user_id") == user_id
    ]


def any_hosted_app_account(mcp_names: list[str]) -> bool:
    """Whether any account of these MCPs is a GitHub-App account signed in
    through OtoDock's hosted relay (its meta says ``hosted``): the relay's
    forwarding of the app's events must stay on for it."""
    return any(
        oauth_app_kinds.is_github_app_meta(a["meta"]) and a["meta"].get("hosted")
        for a in _accounts_with_meta(mcp_names)
    )


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

class AccountAgentBinding(NamedTuple):
    """A person's own choice of account for one agent, and their own GitHub
    reach for it: the repositories the agent may reach with that account
    (``()`` = every one it reaches, ``owner/name`` as stored) and whether it
    may only read."""

    account_label: str
    repositories: tuple[str, ...] = ()
    read_only: bool = False


def get_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str,
) -> AccountAgentBinding | None:
    """The person's binding of (user, mcp, agent), or None.

    None means "no explicit binding — fall back to default account" (with no
    reach: the default account reaches what it reaches).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_label, repositories, read_only FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND agent_name=%s",
            (user_sub, mcp_name, agent_name),
        ).fetchone()
    if not row:
        return None
    return AccountAgentBinding(
        row["account_label"], _stored_repositories(row["repositories"]), bool(row["read_only"]))


def set_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str, account_label: str, *,
    repositories: list[str] | None = None, read_only: bool | None = None,
) -> bool:
    """Pin an agent to a specific account. Upsert via UNIQUE constraint.

    ``repositories`` and ``read_only``, the person's own GitHub reach for the
    agent: ``None`` keeps what the row holds, whatever else changes (a client
    that knows nothing of the reach never widens it; a list kept across
    another account only narrows more).

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
        prior = conn.execute(
            "SELECT repositories, read_only FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND agent_name=%s",
            (user_sub, mcp_name, agent_name),
        ).fetchone()
        if repositories is None:
            stored = prior["repositories"] if prior else "[]"
        else:
            stored = json.dumps(list(dict.fromkeys(r.strip() for r in repositories if r.strip())))
        if read_only is None:
            read_only = bool(prior["read_only"]) if prior else False
        now = _now()
        conn.execute(
            """INSERT INTO agent_account_bindings
               (user_sub, mcp_name, agent_name, account_label, set_at, repositories, read_only)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT(user_sub, mcp_name, agent_name)
               DO UPDATE SET account_label=EXCLUDED.account_label,
                             set_at=EXCLUDED.set_at,
                             repositories=EXCLUDED.repositories,
                             read_only=EXCLUDED.read_only""",
            (user_sub, mcp_name, agent_name, account_label, now, stored, bool(read_only)),
        )
        conn.commit()
        return True


def remove_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str,
) -> None:
    """Drop the per-agent override (agent reverts to user's default account,
    and the reach the row carried goes with it)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND agent_name=%s",
            (user_sub, mcp_name, agent_name),
        )
        conn.commit()


def list_agent_account_bindings(user_sub: str, mcp_name: str) -> list[dict]:
    """Return all per-agent bindings for (user_sub, mcp_name) — used in UI:
    ``{agent_name, account_label, set_at, repositories, read_only}``
    (``repositories`` the stored list, parsed)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, account_label, set_at, repositories, read_only "
            "FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s "
            "ORDER BY agent_name ASC",
            (user_sub, mcp_name),
        ).fetchall()
    return [{**dict(r), "repositories": list(_stored_repositories(r["repositories"])),
             "read_only": bool(r["read_only"])} for r in rows]


#: The platform setting that remembers the fingerprint of every value the
#: secret floor accepted, per secret (never listed by any route).
FLOOR_SETTING = "secret_floor_fingerprints"
#: The platform setting that names every key this install can decrypt with,
#: by fingerprint, written at every boot: a backup's manifest copies the
#: source's, and restore.sh compares it with the target's before it restores
#: (features/BACKUP-RESTORE.md "Restore", item 3, "Keys this install cannot
#: read").
KEY_FINGERPRINTS_SETTING = "credential_key_fingerprints"
_FLOOR_LABEL = b"otodock secret floor"
_FLOOR_KEEP = 16


def _fingerprint(value: str) -> str:
    return hmac.new(value.encode(), _FLOOR_LABEL, hashlib.sha256).hexdigest()[:32]


def key_fingerprints() -> dict[str, str]:
    """``{fingerprint: label}`` of the decrypting set ``_get_fernet`` builds:
    the comma entries of CREDENTIAL_ENCRYPTION_KEY in order, the whole value
    when it differs, then JWT_SECRET. Labels name a line of config.env, never
    a value."""
    keys = _configured_keys()
    out: dict[str, str] = {}
    for n, raw in enumerate(keys, 1):
        out.setdefault(_fingerprint(raw), f"CREDENTIAL_ENCRYPTION_KEY entry {n}")
    whole = config.credential_key_setting()
    if whole and whole not in keys:
        out.setdefault(_fingerprint(whole), "CREDENTIAL_ENCRYPTION_KEY as one value")
    out.setdefault(_fingerprint(config.JWT_SECRET), "JWT_SECRET")
    return out


def record_key_fingerprints() -> None:
    """Write ``KEY_FINGERPRINTS_SETTING`` for this boot's decrypting set. A
    database error skips it: the boot must not fail on it."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO platform_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                (KEY_FINGERPRINTS_SETTING, json.dumps(key_fingerprints(), sort_keys=True)))
            conn.commit()
    except psycopg.Error as e:
        logger.debug("credential key fingerprints not written: %s", e)


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
    except (psycopg.Error, ValueError) as e:
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
    except psycopg.Error as e:
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
    short secret (``judge_secret_floor``), and the decrypting set is then
    recorded by fingerprint for restore.sh (``record_key_fingerprints``).
    """
    judge_secret_floor()
    record_key_fingerprints()
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
                except psycopg.Error:
                    continue  # table absent (partial install) — not this probe's job
                for r in rows:
                    try:
                        _decrypt(r["v"])
                    except (InvalidToken, UnicodeDecodeError):
                        bad.append(store)
                        break
    except Exception as e:  # noqa: BLE001  # the boot's key canary must never take the boot down with it
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
            except (InvalidToken, UnicodeDecodeError):
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
            except (InvalidToken, UnicodeDecodeError):
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
            except (InvalidToken, UnicodeDecodeError):
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
            "SELECT mcp_name, agent_name, account_label, installation_id "
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
    ``{mcp_name, agent_name, account_label, installation_id}``, standing or
    not: the
    offboarding subscriber judges each against the person's standing on
    that agent and clears the ones that no longer hold."""
    if not owner_sub:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, agent_name, account_label, installation_id "
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


class ServiceBinding(NamedTuple):
    """An agent's service identity: a person's own account, and on a
    GitHub-App account optionally one of its installations (``""`` = the
    account itself); and the agent's GitHub reach: the repositories it may
    reach (``()`` = every one the identity reaches, ``owner/name`` as
    stored) and whether it may only read."""

    account_label: str
    account_owner_sub: str
    installation_id: str = ""
    repositories: tuple[str, ...] = ()
    read_only: bool = False


def _stored_repositories(text: Any) -> tuple[str, ...]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return ()
    return tuple(str(v) for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _binding_of(row: Any) -> ServiceBinding:
    return ServiceBinding(
        row["account_label"], row["account_owner_sub"], row["installation_id"] or "",
        _stored_repositories(row["repositories"]), bool(row["read_only"]),
    )


def get_service_agent_binding(
    mcp_name: str, agent_name: str,
) -> ServiceBinding | None:
    """The binding of (mcp, agent), or None.

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
            "SELECT account_label, account_owner_sub, installation_id, repositories, read_only "
            "FROM service_agent_bindings WHERE mcp_name=%s AND agent_name=%s",
            (mcp_name, agent_name),
        ).fetchone()
        if not row:
            return None
    if not _owner_manages(row["account_owner_sub"], agent_name):
        logger.debug("service binding %s/%s: owner %s no longer manages the agent",
                     mcp_name, agent_name, row["account_owner_sub"][:8])
        return None
    return _binding_of(row)


def service_agent_binding_row_exists(mcp_name: str, agent_name: str) -> bool:
    """Whether (mcp, agent) has a service binding row at all, whoever owns
    it (a row whose owner stopped managing the agent still holds its own
    reach, which a rebind keeps)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM service_agent_bindings WHERE mcp_name=%s AND agent_name=%s",
            (mcp_name, agent_name),
        ).fetchone() is not None


def set_service_agent_binding(
    mcp_name: str, agent_name: str, *,
    account_label: str, owner_sub: str, set_by: str = "",
    installation_id: str | None = None,
    repositories: list[str] | None = None, read_only: bool | None = None,
) -> bool:
    """Pin an agent to a user's own connected account as its service identity.

    The bound account is identified by ``(owner_sub, mcp_name, account_label)``
    in ``user_credential_accounts``. ``owner_sub`` MUST be a real user_sub —
    there is no platform-tier account. Validates the target row exists first.

    ``installation_id``: an installation of that account as the identity
    (it must stand in the account's meta: a GitHub-App account, the person
    owning the installation's target, not suspended); ``""`` the account
    itself; ``None`` keeps what the row holds (a client that knows nothing
    of installations never widens one to the whole account).

    ``repositories`` and ``read_only``, the agent's GitHub reach: ``None``
    keeps what the row holds, whatever else changes (a client that knows
    nothing of the reach never widens it; a list kept across another
    account only narrows more).

    Returns False if ``owner_sub`` is empty, the target row doesn't exist or
    the installation does not stand (no-op), True on success.
    """
    if not owner_sub:
        return False
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT meta FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (owner_sub, mcp_name, account_label),
        ).fetchone()
        if not existing:
            return False
        prior = conn.execute(
            "SELECT installation_id, account_owner_sub, account_label, repositories, read_only "
            "FROM service_agent_bindings WHERE mcp_name=%s AND agent_name=%s",
            (mcp_name, agent_name),
        ).fetchone()
        same_account = bool(prior) and (prior["account_owner_sub"], prior["account_label"]) == (owner_sub, account_label)
        if installation_id is None:
            installation_id = (prior["installation_id"] or "") if same_account else ""
        if installation_id and oauth_app_kinds.installation_standing(_parse_meta(existing["meta"]), installation_id):
            return False
        if repositories is None:
            stored = prior["repositories"] if prior else "[]"
        else:
            stored = json.dumps(list(dict.fromkeys(r.strip() for r in repositories if r.strip())))
        if read_only is None:
            read_only = bool(prior["read_only"]) if prior else False
        now = _now()
        conn.execute(
            """INSERT INTO service_agent_bindings
               (mcp_name, agent_name, account_label, account_owner_sub, set_by, set_at,
                installation_id, repositories, read_only)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT(mcp_name, agent_name)
               DO UPDATE SET account_label=EXCLUDED.account_label,
                             account_owner_sub=EXCLUDED.account_owner_sub,
                             set_by=EXCLUDED.set_by,
                             set_at=EXCLUDED.set_at,
                             installation_id=EXCLUDED.installation_id,
                             repositories=EXCLUDED.repositories,
                             read_only=EXCLUDED.read_only""",
            (mcp_name, agent_name, account_label, owner_sub, set_by, now, installation_id,
             stored, bool(read_only)),
        )
        conn.commit()
        return True


def remove_service_agent_binding(
    mcp_name: str, agent_name: str, *,
    owner_sub: str | None = None, account_label: str | None = None,
    installation_id: str | None = None,
) -> bool:
    """Drop the per-agent service binding (the agent is left with no service
    identity for this MCP until a new binding is set). With ``owner_sub``,
    ``account_label`` and ``installation_id`` only while the binding still
    names that lender's account (and that installation): a clear that ran
    for seconds never removes the binding a manager set meanwhile. Returns
    whether a row went."""
    sql = "DELETE FROM service_agent_bindings WHERE mcp_name=%s AND agent_name=%s"
    params: tuple = (mcp_name, agent_name)
    if owner_sub is not None:
        sql += " AND account_owner_sub=%s"
        params += (owner_sub,)
    if account_label is not None:
        sql += " AND account_label=%s"
        params += (account_label,)
    if installation_id is not None:
        sql += " AND installation_id=%s"
        params += (installation_id,)
    with get_conn() as conn:
        gone = conn.execute(sql, params).rowcount
        conn.commit()
    return bool(gone)


def list_service_agent_bindings(mcp_name: str) -> list[dict]:
    """Return all per-agent service-account bindings for this MCP — used in UI.

    Each row has ``{agent_name, account_label, account_owner_sub, set_by,
    set_at, installation_id, repositories, read_only}`` (``repositories`` the
    stored JSON list).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, account_label, account_owner_sub, set_by, set_at, "
            "installation_id, repositories, read_only FROM service_agent_bindings "
            "WHERE mcp_name=%s "
            "ORDER BY agent_name ASC",
            (mcp_name,),
        ).fetchall()
        return [dict(r) for r in rows]


def bindings_naming_installation(mcp_names: list[str], installation_id: str | int) -> list[dict]:
    """Every binding (any agent, any lender) whose identity is
    ``installation_id``: ``{mcp_name, agent_name, account_label,
    account_owner_sub, installation_id, repositories, read_only}``."""
    if not mcp_names:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, agent_name, account_label, account_owner_sub, installation_id, "
            "repositories, read_only "
            "FROM service_agent_bindings "
            "WHERE mcp_name = ANY(%s) AND installation_id=%s "
            "ORDER BY agent_name ASC",
            (list(mcp_names), str(installation_id)),
        ).fetchall()
        return [dict(r) for r in rows]


