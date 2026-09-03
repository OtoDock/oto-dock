"""Phone server store — telephony adapters (Asterisk/FreePBX, Twilio, 3CX).

One row per configured phone server. ``config`` holds non-secret adapter
settings (AMI host/port/username, …); the secret (the AMI password) lives in
``infra_credentials`` keyed ``phone-server-{id}-ami-secret`` and is managed by
the API layer, never stored here in plaintext. ``bootstrap_status`` tracks the
one-time dialplan handshake that the adapter drives.

Deleting a server that a phone route still references is blocked by the FK
(``ON DELETE RESTRICT``); callers should pre-check ``routes_using_server`` for
a friendly 409.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from storage.pg import get_conn

# AMI secret storage: one ``infra_credentials`` row per server, keyed by id.
AMI_SECRET_KEY = "AMI_SECRET"

# Register secret storage: one ``infra_credentials`` row per server. This is the
# per-server token the server's dialplan presents on ``POST /v1/calls/register``
# (minted on demand, not admin-typed). Deleting the server revokes it.
REGISTER_SECRET_KEY = "REGISTER_SECRET"


def ami_cred_name(server_id: int) -> str:
    """infra_credentials mcp_name holding a server's AMI secret."""
    return f"phone-server-{server_id}-ami-secret"


def register_cred_name(server_id: int) -> str:
    """infra_credentials mcp_name holding a server's register secret."""
    return f"phone-server-{server_id}-register-secret"


# Twilio auth-token storage: one ``infra_credentials`` row per Twilio server
# (admin-typed, like the AMI secret). The account SID is non-secret config.
TWILIO_AUTH_TOKEN_KEY = "AUTH_TOKEN"


def twilio_cred_name(server_id: int) -> str:
    """infra_credentials mcp_name holding a server's Twilio auth token."""
    return f"phone-server-{server_id}-twilio-auth-token"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_field(value) -> dict:
    if isinstance(value, str):
        try:
            return json.loads(value) or {}
        except (ValueError, TypeError):
            return {}
    return value or {}


def _row_to_dict(row: dict) -> dict:
    d = dict(row)
    d["is_default"] = bool(d["is_default"])
    d["credentials"] = _json_field(d.get("credentials"))
    d["config"] = _json_field(d.get("config"))
    return d


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_all_servers() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM phone_servers ORDER BY is_default DESC, name"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_server(server_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM phone_servers WHERE id = %s", (server_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_default_server() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM phone_servers WHERE is_default = TRUE"
        ).fetchone()
        return _row_to_dict(row) if row else None


def routes_using_server(server_id: int) -> list[str]:
    """Names of phone routes provisioned against this server."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, id FROM phone_routes WHERE phone_server_id = %s "
            "ORDER BY name",
            (server_id,),
        ).fetchall()
        return [r["name"] or r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_server(data: dict) -> dict:
    """Insert a phone server. First server created is marked default."""
    now = _now()
    with get_conn() as conn:
        has_default = conn.execute(
            "SELECT 1 FROM phone_servers WHERE is_default = TRUE"
        ).fetchone()
        # First server is always the default; later ones only if explicitly asked.
        is_default = bool(data.get("is_default")) or not bool(has_default)
        # Keep the partial unique index happy if this one claims default.
        if is_default and has_default:
            conn.execute("UPDATE phone_servers SET is_default = FALSE WHERE is_default = TRUE")
        row = conn.execute(
            """INSERT INTO phone_servers
               (name, adapter_type, host, credentials, config,
                is_default, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING *""",
            (
                data["name"],
                data.get("adapter_type", "asterisk_freepbx"),
                data.get("host", ""),
                json.dumps(data.get("credentials") or {}),
                json.dumps(data.get("config") or {}),
                is_default,
                now,
                now,
            ),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)


def update_server(server_id: int, data: dict) -> dict | None:
    """Partial update. ``is_default`` is set via ``set_default`` only."""
    allowed = {
        "name", "adapter_type", "host", "credentials", "config",
        "bootstrap_status", "bootstrap_log",
        "last_health_check", "last_health_status", "last_health_detail",
    }
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if not updates:
        return get_server(server_id)
    for jcol in ("credentials", "config"):
        if jcol in updates:
            updates[jcol] = json.dumps(updates[jcol] or {})
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [server_id]
    with get_conn() as conn:
        row = conn.execute(
            f"UPDATE phone_servers SET {set_clause} WHERE id = %s RETURNING *",
            values,
        ).fetchone()
        conn.commit()
        return _row_to_dict(row) if row else None


def set_default(server_id: int) -> dict:
    """Make ``server_id`` the default server, clearing any prior default."""
    now = _now()
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM phone_servers WHERE id = %s", (server_id,)
        ).fetchone()
        if not exists:
            raise ValueError(f"Phone server {server_id} not found")
        conn.execute(
            "UPDATE phone_servers SET is_default = FALSE, updated_at = %s "
            "WHERE is_default = TRUE",
            (now,),
        )
        row = conn.execute(
            "UPDATE phone_servers SET is_default = TRUE, updated_at = %s "
            "WHERE id = %s RETURNING *",
            (now, server_id),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)


def delete_server(server_id: int) -> bool:
    """Delete a server. Returns True if removed. The phone_routes FK is
    ``ON DELETE RESTRICT`` — pre-check ``routes_using_server`` for a 409."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM phone_servers WHERE id = %s", (server_id,)
        )
        conn.commit()
        return cur.rowcount > 0
