"""WebAuthn passkey credential storage.

One row per registered passkey. ``credential_id`` / ``public_key`` are
base64url strings (the wire format); ``sign_count`` backs the library's
cloned-authenticator detection. Rows cascade-delete with the user. All
functions are synchronous (call via ``asyncio.to_thread`` from async code).
"""

import json
from datetime import datetime, timezone

from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CredentialAlreadyRegistered(ValueError):
    """The credential_id is already registered (to this or another user)."""


def add_credential(credential_id: str, user_sub: str, public_key: str,
                   sign_count: int, name: str, transports: list[str]) -> None:
    from psycopg import errors as _pg_errors
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO webauthn_credentials
                   (credential_id, user_sub, public_key, sign_count, name,
                    transports, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (credential_id, user_sub, public_key, sign_count, name,
                 json.dumps(transports or []), _now()),
            )
            conn.commit()
        except _pg_errors.UniqueViolation as e:
            # credential_id is the PRIMARY KEY. With attestation "none" the
            # client controls it, so a crafted response can carry an existing
            # id — the PK correctly blocks the row (no cross-user takeover),
            # but the raw IntegrityError would surface as a 500. Translate to a
            # typed error the endpoint turns into a clean 400.
            conn.rollback()
            raise CredentialAlreadyRegistered(
                "This passkey is already registered") from e


def get_credential(credential_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM webauthn_credentials WHERE credential_id=%s",
            (credential_id,),
        ).fetchone()
        return dict(row) if row else None


def list_credentials(user_sub: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT credential_id, name, created_at, last_used "
            "FROM webauthn_credentials WHERE user_sub=%s ORDER BY created_at",
            (user_sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_credentials(user_sub: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS cnt FROM webauthn_credentials WHERE user_sub=%s",
            (user_sub,),
        ).fetchone()["cnt"]


def record_use(credential_id: str, new_sign_count: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE webauthn_credentials SET sign_count=%s, last_used=%s "
            "WHERE credential_id=%s",
            (new_sign_count, _now(), credential_id),
        )
        conn.commit()


def rename_credential(user_sub: str, credential_id: str, name: str) -> bool:
    """Rename a credential the user owns. Returns False when not theirs."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE webauthn_credentials SET name=%s "
            "WHERE credential_id=%s AND user_sub=%s",
            (name, credential_id, user_sub),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_credential(user_sub: str, credential_id: str) -> bool:
    """Delete a credential the user owns. Returns False when not theirs."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM webauthn_credentials "
            "WHERE credential_id=%s AND user_sub=%s",
            (credential_id, user_sub),
        )
        conn.commit()
        return cur.rowcount > 0
