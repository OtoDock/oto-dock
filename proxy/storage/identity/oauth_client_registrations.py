"""The OAuth clients this install registered at vendors' authorization
servers (RFC 7591; ``services/oauth/mcp_authorization.py``): one live row
per issuer and redirect URI. A client secret and a registration access
token are Fernet-encrypted with the credential-store key. A row is revoked,
never deleted: token files point at it by id.

Synchronous; call through ``run_db`` or ``asyncio.to_thread``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from storage.identity import credential_store
from storage.pg import get_conn

# The columns a reader gets; the encrypted ones are read by their own
# accessors and never listed.
_COLUMNS = (
    "id, issuer, redirect_uri, registration_endpoint, client_id, "
    "client_secret_expires_at, token_endpoint_auth_method, "
    "registration_client_uri, client_name, scope, created_at, last_used_at, "
    "revoked_at, revoked_reason, requested_auth_method, resources"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(row) -> dict:
    d = dict(row)
    d["has_secret"] = bool(d.pop("_has_secret", False))
    return d


def get_live(issuer: str, redirect_uri: str) -> dict | None:
    """The unrevoked registration for this issuer and callback, or None."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS}, client_secret_enc <> '' AS _has_secret "
            "FROM oauth_client_registrations "
            "WHERE issuer = %s AND redirect_uri = %s AND revoked_at = ''",
            (issuer, redirect_uri),
        ).fetchone()
    return _public(row) if row else None


def get(reg_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS}, client_secret_enc <> '' AS _has_secret "
            "FROM oauth_client_registrations WHERE id = %s",
            (reg_id,),
        ).fetchone()
    return _public(row) if row else None


def insert(
    *,
    issuer: str,
    redirect_uri: str,
    registration_endpoint: str,
    client_id: str,
    client_secret: str = "",
    client_secret_expires_at: str = "",
    token_endpoint_auth_method: str = "none",
    registration_client_uri: str = "",
    registration_access_token: str = "",
    client_name: str = "",
    scope: str = "",
    supersede_id: int | None = None,
    supersede_reason: str = "",
    requested_auth_method: str = "",
    resource: str = "",
) -> dict:
    """Record a registration. The partial unique index keeps one live row per
    issuer and callback: a concurrent insert loses and reads the winner.
    ``supersede_id`` revokes an older live row in the same transaction (its
    secret expired or no longer decrypts). ``requested_auth_method`` is the
    method the install asked for (the row is judged against it, not the
    vendor's echo); ``resource`` the MCP server URL the registration serves."""
    now = _now()
    with get_conn() as conn:
        if supersede_id is not None:
            conn.execute(
                "UPDATE oauth_client_registrations SET revoked_at = %s, "
                "revoked_reason = %s WHERE id = %s AND revoked_at = ''",
                (now, supersede_reason, supersede_id),
            )
        conn.execute(
            "INSERT INTO oauth_client_registrations (issuer, redirect_uri, "
            "registration_endpoint, client_id, client_secret_enc, "
            "client_secret_expires_at, token_endpoint_auth_method, "
            "registration_client_uri, registration_access_token_enc, client_name, "
            "scope, created_at, last_used_at, requested_auth_method, resources) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (issuer, redirect_uri) WHERE revoked_at = '' DO NOTHING",
            (
                issuer, redirect_uri, registration_endpoint, client_id,
                credential_store.encrypt_secret(client_secret) if client_secret else "",
                client_secret_expires_at or "", token_endpoint_auth_method or "none",
                registration_client_uri or "",
                credential_store.encrypt_secret(registration_access_token)
                if registration_access_token else "",
                client_name or "", scope or "", now, now,
                requested_auth_method or "", resource or "",
            ),
        )
        conn.commit()
    row = get_live(issuer, redirect_uri)
    if row is None:
        raise RuntimeError("the registration row vanished between insert and read")
    return row


def client_secret(reg_id: int) -> str | None:
    """The decrypted client secret: ``""`` for a public client, ``None`` when
    the stored value no longer decrypts (the credential key changed)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT client_secret_enc FROM oauth_client_registrations WHERE id = %s",
            (reg_id,),
        ).fetchone()
    if row is None:
        return None
    enc = row["client_secret_enc"] or ""
    if not enc:
        return ""
    try:
        return credential_store.decrypt_secret(enc)
    except Exception:
        return None


def add_resource(reg_id: int, resource: str) -> None:
    """Record that ``resource`` (an MCP server URL, canonical) signs in
    through this registration; a no-op when it is already listed."""
    if not resource:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE oauth_client_registrations "
            "SET resources = btrim(resources || ' ' || %s) "
            "WHERE id = %s AND position(' ' || %s || ' ' in ' ' || resources || ' ') = 0",
            (resource, reg_id, resource),
        )
        conn.commit()


def touch(reg_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE oauth_client_registrations SET last_used_at = %s WHERE id = %s",
            (_now(), reg_id),
        )
        conn.commit()


def revoke(reg_id: int, reason: str) -> bool:
    """Mark a live row revoked. Returns False when it was not live."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE oauth_client_registrations SET revoked_at = %s, revoked_reason = %s "
            "WHERE id = %s AND revoked_at = ''",
            (_now(), reason, reg_id),
        )
        changed = cur.rowcount
        conn.commit()
    return bool(changed)


def list_all() -> list[dict]:
    """Every row, live first then newest first, without the encrypted columns."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS}, client_secret_enc <> '' AS _has_secret "
            "FROM oauth_client_registrations "
            "ORDER BY (revoked_at <> ''), created_at DESC, id DESC",
        ).fetchall()
    return [_public(r) for r in rows]
