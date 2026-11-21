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
from datetime import datetime, timedelta, timezone
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
    agents: list[str] | None = None,
    expires_at: str | None = None,
    run_without_asking: bool = False,
) -> dict:
    kid = key_id or str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_api_keys
               (id, user_sub, name, key_hash, prefix, permissions, created_at,
                agents, expires_at, run_without_asking)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (kid, user_sub, name, key_hash, prefix,
             json.dumps(permissions), now, json.dumps(agents or []), expires_at,
             run_without_asking),
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
    """Delete all keys for a user, with what their requests and creations
    recorded. Used on user deletion."""
    with get_conn() as conn:
        for table in ("user_api_key_requests", "user_api_key_created"):
            conn.execute(f"DELETE FROM {table} WHERE key_id IN "
                         "(SELECT id FROM user_api_keys WHERE user_sub=%s)", (user_sub,))
        cur = conn.execute("DELETE FROM user_api_keys WHERE user_sub=%s", (user_sub,))
        conn.commit()
        return cur.rowcount


def list_all_api_keys(*, include_revoked: bool = False) -> list[dict]:
    """Every person's and every agent's key, newest first, with the owner's
    name: the admin's list. The hashes are left out."""
    where = "" if include_revoked else "WHERE k.revoked_at IS NULL"
    with get_conn() as conn:
        users = conn.execute(
            f"""SELECT k.id, k.name, k.prefix, k.permissions, k.agents, k.expires_at,
                       k.run_without_asking, k.created_at, k.last_used_at, k.revoked_at,
                       k.user_sub, u.email AS owner_email, u.name AS owner_name
                FROM user_api_keys k LEFT JOIN users u ON u.sub = k.user_sub
                {where} ORDER BY k.created_at DESC""").fetchall()
        agents = conn.execute(
            f"""SELECT k.id, k.name, k.prefix, k.permissions, k.agent, k.created_by,
                       k.created_at, k.last_used_at, k.revoked_at
                FROM agent_api_keys k {where} ORDER BY k.created_at DESC""").fetchall()
    return ([{**dict(r), "kind": "user"} for r in users]
            + [{**dict(r), "kind": "agent"} for r in agents])


# =====================================================================
# A key's requests and what it created
# =====================================================================


def claim_key_request(key_id: str, request_id: str, chat_id: str) -> dict | None:
    """Record the first send of ``request_id`` by this key: None when this
    call claimed it, else the earlier claim's row. Claims older than seven
    days are swept first, so a request id is remembered for a week."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM user_api_key_requests WHERE key_id=%s AND created_at < %s",
                     (key_id, cutoff))
        row = conn.execute(
            """INSERT INTO user_api_key_requests (key_id, request_id, chat_id, created_at)
               VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING key_id""",
            (key_id, request_id, chat_id, _now()),
        ).fetchone()
        if row is not None:
            return None
        earlier = conn.execute(
            "SELECT * FROM user_api_key_requests WHERE key_id=%s AND request_id=%s",
            (key_id, request_id),
        ).fetchone()
        return dict(earlier) if earlier else {}


def retake_stale_key_request(key_id: str, request_id: str, chat_id: str, older_than_s: int) -> bool:
    """Take over a claim that never got its message and is older than
    ``older_than_s`` (the process that made it is gone): True when this
    call holds it now."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_s)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE user_api_key_requests SET chat_id=%s, created_at=%s
               WHERE key_id=%s AND request_id=%s AND message_id IS NULL AND created_at < %s
               RETURNING key_id""",
            (chat_id, _now(), key_id, request_id, cutoff),
        ).fetchone()
        return row is not None


def set_key_request_message(key_id: str, request_id: str, message_id: int | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_api_key_requests SET message_id=%s WHERE key_id=%s AND request_id=%s",
            (message_id, key_id, request_id),
        )


def drop_key_request(key_id: str, request_id: str) -> None:
    """A send that did not go out: the request id may be sent again."""
    with get_conn() as conn:
        conn.execute("DELETE FROM user_api_key_requests WHERE key_id=%s AND request_id=%s",
                     (key_id, request_id))


def get_key_request_by_message(key_id: str, message_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_api_key_requests WHERE key_id=%s AND message_id=%s",
            (key_id, message_id),
        ).fetchone()
        return dict(row) if row else None


def record_key_created(key_id: str, kind: str, ref: str, *, label: str = "",
                       agent: str = "") -> None:
    """Note something the key made (a chat, a task, a notification, a
    file), for the list its revocation shows."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_api_key_created (key_id, kind, ref, label, agent, created_at)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (key_id, kind, ref, label[:200], agent, _now()),
        )


def key_created(key_id: str, *, limit: int = 200) -> tuple[list[dict], int]:
    """What the key made, newest first (at most ``limit``), and how many."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT kind, ref, label, agent, created_at FROM user_api_key_created
               WHERE key_id=%s ORDER BY created_at DESC LIMIT %s""",
            (key_id, limit),
        ).fetchall()
        total = conn.execute(
            "SELECT count(*) AS n FROM user_api_key_created WHERE key_id=%s", (key_id,),
        ).fetchone()["n"]
        return [dict(r) for r in rows], int(total)


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


def parse_agents(raw) -> list[str]:
    """The key's named agents (JSONB list), [] for every agent."""
    return parse_permissions(raw)


def has_permission(key_row: dict, permission: str) -> bool:
    """Check if a key row grants the given permission."""
    perms = parse_permissions(key_row.get("permissions"))
    return permission in perms


# =====================================================================
# A key's message in its chat
# =====================================================================


def take_back_key_message(chat_id: str, message_id: int) -> bool:
    """Remove a key's message that no turn took (its row only: a person's
    row carries the key's mark in ``event_data``), while it is still the
    chat's last row. Rows after it mean a turn ran on it: the question
    stays with its answer. True when the row went."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE chat_id=%s AND id=%s AND role='user' "
            "AND event_data LIKE %s "
            "AND id = (SELECT max(id) FROM chat_messages WHERE chat_id=%s)",
            (chat_id, message_id, '%"api_key"%', chat_id),
        )
        conn.commit()
        gone = (cur.rowcount or 0) > 0
    if gone:
        from storage.chat.db_chats import rebuild_chat_search
        rebuild_chat_search(chat_id)
    return gone


def key_turn_rows(chat_id: str, after: int, upto: int | None = None) -> list[dict]:
    """The chat's rows after the key's message (at most ``upto``), oldest
    first, at most 500."""
    with get_conn() as conn:
        if upto is None:
            rows = conn.execute(
                "SELECT id, role, content, author_sub FROM chat_messages "
                "WHERE chat_id=%s AND id > %s ORDER BY id LIMIT 500", (chat_id, after),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, role, content, author_sub FROM chat_messages "
                "WHERE chat_id=%s AND id > %s AND id <= %s ORDER BY id LIMIT 500",
                (chat_id, after, upto),
            ).fetchall()
        return [dict(r) for r in rows]


def last_chat_row_id(chat_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT max(id) AS m FROM chat_messages WHERE chat_id=%s",
                           (chat_id,)).fetchone()
        return int(row["m"] or 0)
