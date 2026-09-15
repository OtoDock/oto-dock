"""API key CRUD against PostgreSQL.

Two tables: agent_api_keys (manager-managed, per-agent) and user_api_keys
(per-user). Both use bcrypt-hashed keys with a plaintext prefix for UI
display. The plaintext key is returned ONCE on creation; subsequent reads
only see the prefix.

Service-layer key generation + verification lives in
services/infra/api_key_manager.py.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# Agent API keys
# =====================================================================


def create_agent_api_key(
    *,
    agent: str,
    name: str,
    key_hash: str,
    prefix: str,
    permissions: list[str],
    created_by: str,
    key_id: str | None = None,
) -> dict:
    kid = key_id or str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_api_keys
               (id, agent, name, key_hash, prefix, permissions, created_by, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (kid, agent, name, key_hash, prefix,
             json.dumps(permissions), created_by, now),
        )
        row = conn.execute(
            "SELECT * FROM agent_api_keys WHERE id=%s", (kid,),
        ).fetchone()
        return dict(row)


def list_agent_api_keys(
    *, agent: str | None = None, include_revoked: bool = False,
) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        conditions.append("agent=%s")
        params.append(agent)
    if not include_revoked:
        conditions.append("revoked_at IS NULL")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM agent_api_keys {where} ORDER BY created_at DESC", params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_agent_api_key(key_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_api_keys WHERE id=%s", (key_id,),
        ).fetchone()
        return dict(row) if row else None


def get_agent_keys_by_prefix(prefix: str) -> list[dict]:
    """Return all non-revoked agent keys with the given prefix.

    Caller does the bcrypt hash check across the small list. Prefix is
    indexed; even with a few collisions per prefix, this is fast.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_api_keys WHERE prefix=%s AND revoked_at IS NULL",
            (prefix,),
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_agent_api_key(key_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_api_keys SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL",
            (_now(), key_id),
        )
        return cur.rowcount > 0


def update_agent_key_last_used(key_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE agent_api_keys SET last_used_at=%s WHERE id=%s",
            (_now(), key_id),
        )


def cleanup_agent_api_keys(agent: str) -> int:
    """Delete all keys for an agent. Used on agent deletion."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM agent_api_keys WHERE agent=%s", (agent,))
        return cur.rowcount


# =====================================================================
# User API keys
# =====================================================================


def create_user_api_key(
    *,
    user_sub: str,
    name: str,
    key_hash: str,
    prefix: str,
    permissions: list[str],
    key_id: str | None = None,
) -> dict:
    kid = key_id or str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_api_keys
               (id, user_sub, name, key_hash, prefix, permissions, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (kid, user_sub, name, key_hash, prefix,
             json.dumps(permissions), now),
        )
        row = conn.execute(
            "SELECT * FROM user_api_keys WHERE id=%s", (kid,),
        ).fetchone()
        return dict(row)


def list_user_api_keys(
    *, user_sub: str, include_revoked: bool = False,
) -> list[dict]:
    conditions: list[str] = ["user_sub=%s"]
    params: list[Any] = [user_sub]
    if not include_revoked:
        conditions.append("revoked_at IS NULL")
    where = f"WHERE {' AND '.join(conditions)}"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM user_api_keys {where} ORDER BY created_at DESC", params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_api_key(key_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_api_keys WHERE id=%s", (key_id,),
        ).fetchone()
        return dict(row) if row else None


def get_user_keys_by_prefix(prefix: str) -> list[dict]:
    """Non-revoked user keys with the given prefix."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM user_api_keys WHERE prefix=%s AND revoked_at IS NULL",
            (prefix,),
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_user_api_key(key_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE user_api_keys SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL",
            (_now(), key_id),
        )
        return cur.rowcount > 0


def update_user_key_last_used(key_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_api_keys SET last_used_at=%s WHERE id=%s",
            (_now(), key_id),
        )


def cleanup_user_api_keys(user_sub: str) -> int:
    """Delete all keys for a user. Used on user deletion."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM user_api_keys WHERE user_sub=%s", (user_sub,))
        conn.commit()
        return cur.rowcount


# =====================================================================
# Permission helpers
# =====================================================================


def parse_permissions(raw) -> list[str]:
    """Normalize the JSONB permissions field to a Python list.

    psycopg returns JSONB as either a Python list/dict (newer) or a string
    (older). Handle both.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(p) for p in raw]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return [str(p) for p in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []


def has_permission(key_row: dict, permission: str) -> bool:
    """Check if a key row grants the given permission."""
    perms = parse_permissions(key_row.get("permissions"))
    return permission in perms
