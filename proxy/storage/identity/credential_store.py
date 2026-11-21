"""Encrypted credential storage for MCP server credentials.

Six tables:
  - user_credentials:            per-user, per-account credential rows
                                 (one or more labeled accounts per
                                 (user_sub, mcp_name); each row carries
                                 ``account_label``).
  - user_credential_accounts:    account list — one row per labeled account a
                                 user has connected. ``is_default=TRUE`` picks
                                 the catch-all account used by agents with an
                                 empty set.
  - agent_account_bindings:      per agent, the set of a person's accounts the
                                 agent may use, one of them its default. A set
                                 takes precedence over the user's default
                                 account, and each row carries the person's
                                 own GitHub reach for that agent and account.
  - chat_account_choices:        a chat's own choice of account per MCP among
                                 the agent's set, for the person who made it.
  - infra_credentials:           shared infrastructure creds (Uptime Kuma,
                                 UniFi, HA, etc.). Single tier, no accounts.
  - service_agent_bindings:      per agent and tool, the set of accounts the
                                 agent itself uses, one of them its default:
                                 each a USER's own connected account (its
                                 ``account_owner_sub``, the lender). Agent-scope
                                 sessions read that user's tokens. There is no
                                 platform "service account" storage. The set's
                                 writers but the removal, and a chat's choice
                                 among it, are ``service_account_set``.

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
    """Delete one labeled account: credential rows, account row, and the
    account's row in every agent's set (each set left without its default
    promotes its oldest; a chat's choice of it goes with the row)."""
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
        agents = [r["agent_name"] for r in conn.execute(
            "SELECT DISTINCT agent_name FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s ORDER BY agent_name",
            (user_sub, mcp_name, account_label),
        ).fetchall()]
        for agent_name in agents:
            _lock_set(conn, user_sub, mcp_name, agent_name)
        conn.execute(
            "DELETE FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (user_sub, mcp_name, account_label),
        )
        for agent_name in agents:
            _promote_default(conn, user_sub, mcp_name, agent_name)
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


def mcps_naming_label(user_sub: str, account_label: str) -> list[str]:
    """Every MCP with an account row of this person under ``account_label``
    (the MCPs of one provider share that label's token file)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT mcp_name FROM user_credential_accounts "
            "WHERE user_sub=%s AND account_label=%s",
            (user_sub, account_label),
        ).fetchall()
        return sorted(r["mcp_name"] for r in rows)


def list_all_user_accounts(user_sub: str) -> list[dict]:
    """Every account a user has connected, across MCPs:
    ``[{mcp_name, account_label, meta}, ...]``."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, account_label, meta FROM user_credential_accounts "
            "WHERE user_sub=%s ORDER BY mcp_name, created_at ASC",
            (user_sub,),
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


def hosted_app_account_mcps(exclude_user_sub: str = "") -> set[str]:
    """The MCPs of every GitHub-App account signed in through OtoDock's
    hosted relay, whichever MCP manifests are loaded right now; a person
    being deleted (``exclude_user_sub``) left out."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_sub, mcp_name, account_label, display_email, meta "
            "FROM user_credential_accounts WHERE meta <> '' AND user_sub <> %s",
            (exclude_user_sub,),
        ).fetchall()
    return {
        a["mcp_name"] for a in (_account_row(r) for r in rows)
        if oauth_app_kinds.is_github_app_meta(a["meta"]) and a["meta"].get("hosted")
    }


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
    """One account of the set a person's agent may use, and their own GitHub
    reach for it: the repositories the agent may reach with that account
    (``()`` = every one it reaches, ``owner/name`` as stored), whether it
    may only read, and whether it is the set's default (the account a chat
    that chose none runs on)."""

    account_label: str
    repositories: tuple[str, ...] = ()
    read_only: bool = False
    is_default: bool = False


_SET_COLUMNS = "account_label, repositories, read_only, is_default"
_SET_WHERE = "user_sub=%s AND mcp_name=%s AND agent_name=%s"
# The set's four-column key (``storage/identity/schema.py``).
_SET_KEY = "agent_account_bindings_set_key"
# The key of an agent's own set (``storage/identity/schema.py``).
_SERVICE_SET_KEY = "service_agent_bindings_set_key"


def _set_row(row: Any) -> AccountAgentBinding:
    return AccountAgentBinding(
        row["account_label"], _stored_repositories(row["repositories"]),
        bool(row["read_only"]), bool(row["is_default"]),
    )


def _lock_set(conn: Any, user_sub: str, mcp_name: str, agent_name: str) -> None:
    """Serialise the writes to one agent's set until the transaction ends:
    two writers never both leave a default, nor none."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("\n".join(("agent_account_set", user_sub, mcp_name, agent_name)),),
    )


def _holds_account(conn: Any, user_sub: str, mcp_name: str, account_label: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM user_credential_accounts "
        "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
        (user_sub, mcp_name, account_label),
    ).fetchone() is not None


def _repositories_text(repositories: list[str] | None, kept: str) -> str:
    if repositories is None:
        return kept
    return json.dumps(list(dict.fromkeys(r.strip() for r in repositories if r.strip())))


def _promote_default(conn: Any, user_sub: str, mcp_name: str, agent_name: str) -> None:
    """A set that holds rows and no default: its oldest row (``set_at``,
    then ``id``) becomes the default. Every path that deletes set rows
    calls it, so a set that is not empty always has one."""
    key = (user_sub, mcp_name, agent_name)
    conn.execute(
        "UPDATE agent_account_bindings SET is_default=TRUE WHERE id = ("
        f"  SELECT id FROM agent_account_bindings WHERE {_SET_WHERE} ORDER BY set_at, id LIMIT 1"
        ") AND NOT EXISTS ("
        f"  SELECT 1 FROM agent_account_bindings WHERE {_SET_WHERE} AND is_default"
        ")",
        key + key,
    )


def get_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str, account_label: str | None = None,
) -> AccountAgentBinding | None:
    """One row of the agent's set: with ``account_label`` that account's
    (None when the set does not hold it), without it the set's default.

    None without a label means an empty set: fall back to the person's
    default account (with no reach: the default account reaches what it
    reaches).
    """
    sql = f"SELECT {_SET_COLUMNS} FROM agent_account_bindings WHERE {_SET_WHERE}"
    params: tuple = (user_sub, mcp_name, agent_name)
    if account_label is not None:
        sql += " AND account_label=%s"
        params += (account_label,)
    else:
        sql += " ORDER BY is_default DESC, set_at, id LIMIT 1"
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return _set_row(row) if row else None


def list_agent_account_set(
    user_sub: str, mcp_name: str, agent_name: str,
) -> list[AccountAgentBinding]:
    """Every account of the agent's set, the default first, then by
    ``set_at`` and ``id``."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_SET_COLUMNS} FROM agent_account_bindings WHERE {_SET_WHERE} "
            "ORDER BY is_default DESC, set_at, id",
            (user_sub, mcp_name, agent_name),
        ).fetchall()
    return [_set_row(r) for r in rows]


def add_account_to_agent(
    user_sub: str, mcp_name: str, agent_name: str, account_label: str, *,
    repositories: list[str] | None = None, read_only: bool | None = None,
    make_default: bool = False,
) -> bool:
    """Add one of the person's accounts to the agent's set, or update its
    reach when the set holds it already.

    A new row takes the reach the caller names, else the whole account
    (``[]``, writes allowed); an existing row keeps its own for a field the
    caller leaves ``None``. The first row of a set is its default;
    ``make_default`` moves the default to this account.

    Returns False if ``account_label`` doesn't exist (no-op), True on success.
    """
    key = (user_sub, mcp_name, agent_name)
    with get_conn() as conn:
        if not _holds_account(conn, user_sub, mcp_name, account_label):
            return False
        _lock_set(conn, *key)
        prior = conn.execute(
            f"SELECT repositories, read_only FROM agent_account_bindings "
            f"WHERE {_SET_WHERE} AND account_label=%s",
            key + (account_label,),
        ).fetchone()
        stored = _repositories_text(repositories, prior["repositories"] if prior else "[]")
        if read_only is None:
            read_only = bool(prior["read_only"]) if prior else False
        has_default = conn.execute(
            f"SELECT 1 FROM agent_account_bindings WHERE {_SET_WHERE} AND is_default", key,
        ).fetchone() is not None
        if make_default:
            conn.execute(
                f"UPDATE agent_account_bindings SET is_default=FALSE "
                f"WHERE {_SET_WHERE} AND is_default AND account_label<>%s",
                key + (account_label,),
            )
        conn.execute(
            f"""INSERT INTO agent_account_bindings
               (user_sub, mcp_name, agent_name, account_label, set_at, repositories,
                read_only, is_default)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT ON CONSTRAINT {_SET_KEY}
               DO UPDATE SET repositories=EXCLUDED.repositories,
                             read_only=EXCLUDED.read_only,
                             is_default=agent_account_bindings.is_default OR EXCLUDED.is_default""",
            key + (account_label, _now(), stored, bool(read_only), make_default or not has_default),
        )
        conn.commit()
        return True


def set_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str, account_label: str, *,
    repositories: list[str] | None = None, read_only: bool | None = None,
) -> bool:
    """Make the agent's set exactly this one account, its default (the
    1.9.1 meaning of an agent's choice of account).

    ``repositories`` and ``read_only``, the person's own GitHub reach for the
    agent: ``None`` keeps what the agent's prior choice holds (the same
    account's row when the set holds it, else the set's default), whatever
    else changes (a client that knows nothing of the reach never widens it;
    a list kept across another account only narrows more).

    Returns False if ``account_label`` doesn't exist (no-op), True on success.
    """
    key = (user_sub, mcp_name, agent_name)
    with get_conn() as conn:
        if not _holds_account(conn, user_sub, mcp_name, account_label):
            return False
        _lock_set(conn, *key)
        prior = conn.execute(
            f"SELECT repositories, read_only FROM agent_account_bindings WHERE {_SET_WHERE} "
            "ORDER BY account_label=%s DESC, is_default DESC, set_at, id LIMIT 1",
            key + (account_label,),
        ).fetchone()
        stored = _repositories_text(repositories, prior["repositories"] if prior else "[]")
        if read_only is None:
            read_only = bool(prior["read_only"]) if prior else False
        conn.execute(
            f"DELETE FROM agent_account_bindings WHERE {_SET_WHERE} AND account_label<>%s",
            key + (account_label,),
        )
        conn.execute(
            f"""INSERT INTO agent_account_bindings
               (user_sub, mcp_name, agent_name, account_label, set_at, repositories,
                read_only, is_default)
               VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
               ON CONFLICT ON CONSTRAINT {_SET_KEY}
               DO UPDATE SET set_at=EXCLUDED.set_at,
                             repositories=EXCLUDED.repositories,
                             read_only=EXCLUDED.read_only,
                             is_default=TRUE""",
            key + (account_label, _now(), stored, bool(read_only)),
        )
        conn.commit()
        return True


def remove_account_agent_binding(
    user_sub: str, mcp_name: str, agent_name: str, account_label: str | None = None,
) -> bool:
    """With ``account_label``, take that account out of the agent's set (the
    oldest left becomes the default when it was); without, empty the set
    (the agent reverts to the user's default account). The reach a row
    carried goes with it, and a chat's choice of it too. Returns whether a
    row went."""
    key = (user_sub, mcp_name, agent_name)
    sql = f"DELETE FROM agent_account_bindings WHERE {_SET_WHERE}"
    params: tuple = key
    if account_label is not None:
        sql += " AND account_label=%s"
        params += (account_label,)
    with get_conn() as conn:
        _lock_set(conn, *key)
        gone = conn.execute(sql, params).rowcount
        _promote_default(conn, *key)
        conn.commit()
    return bool(gone)


def set_agent_default_account(
    user_sub: str, mcp_name: str, agent_name: str, account_label: str,
) -> bool:
    """Make one account of the agent's set its default. Returns False when
    the set does not hold it (no-op)."""
    key = (user_sub, mcp_name, agent_name)
    with get_conn() as conn:
        _lock_set(conn, *key)
        held = conn.execute(
            f"SELECT 1 FROM agent_account_bindings WHERE {_SET_WHERE} AND account_label=%s",
            key + (account_label,),
        ).fetchone()
        if not held:
            conn.rollback()
            return False
        conn.execute(
            f"UPDATE agent_account_bindings SET is_default=FALSE "
            f"WHERE {_SET_WHERE} AND is_default AND account_label<>%s",
            key + (account_label,),
        )
        conn.execute(
            f"UPDATE agent_account_bindings SET is_default=TRUE WHERE {_SET_WHERE} AND account_label=%s",
            key + (account_label,),
        )
        conn.commit()
        return True


def list_agent_account_bindings(user_sub: str, mcp_name: str) -> list[dict]:
    """Return every set row for (user_sub, mcp_name) — used in UI:
    ``{agent_name, account_label, set_at, repositories, read_only,
    is_default}`` (``repositories`` the stored list, parsed), by agent,
    each set's default first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, account_label, set_at, repositories, read_only, is_default "
            "FROM agent_account_bindings "
            "WHERE user_sub=%s AND mcp_name=%s "
            "ORDER BY agent_name ASC, is_default DESC, set_at, id",
            (user_sub, mcp_name),
        ).fetchall()
    return [{**dict(r), "repositories": list(_stored_repositories(r["repositories"])),
             "read_only": bool(r["read_only"]), "is_default": bool(r["is_default"])} for r in rows]


# ---------------------------------------------------------------------------
# A chat's choice of account
# ---------------------------------------------------------------------------

def get_chat_account_choices(chat_id: str, user_sub: str) -> dict[str, str]:
    """``{mcp_name: account_label}``: the accounts this person chose for
    the chat, each among the agent's set (a row the set lost went with it)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, account_label FROM chat_account_choices "
            "WHERE chat_id=%s AND user_sub=%s",
            (chat_id, user_sub),
        ).fetchall()
    return {r["mcp_name"]: r["account_label"] for r in rows}


def set_chat_account_choice(
    chat_id: str, user_sub: str, agent_name: str, mcp_name: str, account_label: str,
) -> bool:
    """Record the person's choice of account for one MCP in the chat.
    Returns False when the agent's set does not hold the account (or the
    chat is gone): the foreign keys refuse the row."""
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO chat_account_choices
                   (chat_id, user_sub, agent_name, mcp_name, account_label, set_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (chat_id, user_sub, mcp_name)
                   DO UPDATE SET agent_name=EXCLUDED.agent_name,
                                 account_label=EXCLUDED.account_label,
                                 set_at=EXCLUDED.set_at""",
                (chat_id, user_sub, agent_name, mcp_name, account_label, _now()),
            )
            conn.commit()
    except psycopg.errors.ForeignKeyViolation:
        return False
    return True


def clear_chat_account_choice(chat_id: str, user_sub: str, mcp_name: str) -> None:
    """The chat runs on the set's default for this MCP again."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM chat_account_choices WHERE chat_id=%s AND user_sub=%s AND mcp_name=%s",
            (chat_id, user_sub, mcp_name),
        )
        conn.commit()


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
    sets = sorted({(r["mcp_name"], r["agent_name"]) for r in snapshot})
    standing = {key: _standing_lenders(*key) for key in sets}
    with get_conn() as conn:
        for key in sets:
            _lock_service_set(conn, *key)
        conn.execute(
            "DELETE FROM service_agent_bindings WHERE account_owner_sub=%s",
            (owner_sub,),
        )
        for key in sets:
            _promote_service_default(conn, *key, standing[key])
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
    """One of an agent's own accounts of a tool: a person's own account,
    and on a GitHub-App account optionally one of its installations (``""``
    = the account itself); the agent's GitHub reach: the repositories it may
    reach (``()`` = every one the identity reaches, ``owner/name`` as
    stored) and whether it may only read; whether it is the set's default
    (the account a session that chose none runs on)."""

    account_label: str
    account_owner_sub: str
    installation_id: str = ""
    repositories: tuple[str, ...] = ()
    read_only: bool = False
    is_default: bool = False


_SERVICE_COLUMNS = ("account_label, account_owner_sub, installation_id, repositories, read_only, "
                    "is_default")


def _lock_service_set(conn: Any, mcp_name: str, agent_name: str) -> None:
    """Serialise the writes to one agent's own set of a tool until the
    transaction ends: two writers never both leave a default, nor none."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("\n".join(("service_account_set", mcp_name, agent_name)),),
    )


def _standing_lenders(mcp_name: str, agent_name: str) -> set[str]:
    """The lenders of the agent's own set of a tool who still manage it. Read
    before the set's lock: each read takes a connection of its own, and under
    the lock that second checkout could wait on the pool it holds from."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT account_owner_sub FROM service_agent_bindings "
            "WHERE mcp_name=%s AND agent_name=%s",
            (mcp_name, agent_name),
        ).fetchall()
    return {r["account_owner_sub"] for r in rows if _owner_manages(r["account_owner_sub"], agent_name)}


def _promote_service_default(conn: Any, mcp_name: str, agent_name: str, standing: set[str]) -> None:
    """A set that holds rows and no default: its oldest row whose lender
    stands (``_standing_lenders``) becomes the default, else its oldest row
    (a set that is not empty always has one). Every path that deletes set
    rows calls it, under the set's lock."""
    key = (mcp_name, agent_name)
    if conn.execute(
        "SELECT 1 FROM service_agent_bindings WHERE mcp_name=%s AND agent_name=%s AND is_default",
        key,
    ).fetchone():
        return
    rows = conn.execute(
        "SELECT id, account_owner_sub FROM service_agent_bindings "
        "WHERE mcp_name=%s AND agent_name=%s ORDER BY set_at, id",
        key,
    ).fetchall()
    if not rows:
        return
    chosen = next((r for r in rows if r["account_owner_sub"] in standing), rows[0])
    conn.execute("UPDATE service_agent_bindings SET is_default=TRUE WHERE id=%s", (chosen["id"],))


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
        bool(row["is_default"]),
    )


def service_binding_stands(binding: ServiceBinding, agent_name: str) -> bool:
    """Whether a row of the agent's set still lends its account: its lender
    still manages the agent (``_owner_manages``)."""
    return _owner_manages(binding.account_owner_sub, agent_name)


def get_service_agent_binding(
    mcp_name: str, agent_name: str, account_label: str | None = None,
) -> ServiceBinding | None:
    """One row of the agent's own set of a tool, or None: with
    ``account_label`` that account's row, without it the set's default.

    ``account_owner_sub`` is always a real ``<user_sub>`` — the row points
    at that user's ``user_credential_accounts(<sub>, mcp, account_label)`` row
    (a manager/admin lent their connected account to the agent for
    agent-scope sessions). ``None`` → no such row, or a row whose owner no
    longer manages the agent (demoted, unassigned, deleted): the row stays
    for the next bind or clear, but the agent gets no credential from it in
    agent scope (no platform default, no other row in its place).
    """
    sql = (f"SELECT {_SERVICE_COLUMNS} FROM service_agent_bindings "
           "WHERE mcp_name=%s AND agent_name=%s")
    params: tuple = (mcp_name, agent_name)
    if account_label is not None:
        sql += " AND account_label=%s"
        params += (account_label,)
    else:
        sql += " ORDER BY is_default DESC, set_at, id LIMIT 1"
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
    if not _owner_manages(row["account_owner_sub"], agent_name):
        logger.debug("service binding %s/%s: owner %s no longer manages the agent",
                     mcp_name, agent_name, row["account_owner_sub"][:8])
        return None
    return _binding_of(row)


def remove_service_agent_binding(
    mcp_name: str, agent_name: str, *,
    owner_sub: str | None = None, account_label: str | None = None,
    installation_id: str | None = None,
) -> bool:
    """Drop rows of the agent's own set of a tool: without a filter the whole
    set (the agent is left with no account for this MCP until one is set);
    with ``owner_sub``, ``account_label`` and ``installation_id`` only the
    rows that still name that lender's account (and that installation): a
    clear that ran for seconds never removes a row a manager set meanwhile.
    The oldest standing row left becomes the default when the default went,
    and the chats' choices of a removed row go with it. Returns whether a
    row went. A caller that may hold a session on the row goes through
    ``services/remote/chat_reload.remove_service_account``, which closes
    the sessions built on it."""
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
    standing = _standing_lenders(mcp_name, agent_name)
    with get_conn() as conn:
        _lock_service_set(conn, mcp_name, agent_name)
        gone = conn.execute(sql, params).rowcount
        _promote_service_default(conn, mcp_name, agent_name, standing)
        conn.commit()
    return bool(gone)


def list_service_agent_bindings(mcp_name: str) -> list[dict]:
    """Return every row of every agent's own set for this MCP — used in UI.

    Each row has ``{agent_name, account_label, account_owner_sub, set_by,
    set_at, installation_id, repositories, read_only, is_default}``
    (``repositories`` the stored JSON list), by agent, each set's default
    first.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, account_label, account_owner_sub, set_by, set_at, "
            "installation_id, repositories, read_only, is_default FROM service_agent_bindings "
            "WHERE mcp_name=%s "
            "ORDER BY agent_name ASC, is_default DESC, set_at, id",
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


