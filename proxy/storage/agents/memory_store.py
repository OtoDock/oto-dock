"""PostgreSQL-backed memory settings store.

Memory content lives in markdown files under each agent's tree
(``knowledge/memory/`` shared, ``users/{u}/context/memory/`` per-user —
see ``services/memory_file``). This module manages only the platform
toggles, per-agent overrides, the prompt-injection inline budget, and the
turn-counter nudge knob.

All functions are synchronous (called via ``asyncio.to_thread`` from async
contexts).
"""

from __future__ import annotations

from typing import Any

from storage.pg import get_conn


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings() -> dict[str, Any]:
    """Return the singleton platform memory_settings row. Lazily inserts a
    default row if one doesn't exist yet (e.g. after a test TRUNCATE wiped
    the seed from init_schema). Idempotent via ON CONFLICT."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM memory_settings WHERE id = 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO memory_settings (id) VALUES (1) "
                "ON CONFLICT (id) DO NOTHING"
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM memory_settings WHERE id = 1"
            ).fetchone()
        return dict(row) if row else {}


def update_settings(**kwargs) -> dict[str, Any]:
    """Patch the singleton memory_settings row. Returns the new state.

    Ensures the singleton row exists (lazy upsert) before applying the
    UPDATE — so updates after a TRUNCATE or fresh DB still land.
    """
    allowed = {
        "user_memory_enabled", "agent_memory_enabled",
        "inline_budget_bytes", "nudge_turns",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return get_settings()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values())
    with get_conn() as conn:
        # Ensure singleton row exists before UPDATE — TRUNCATE in tests
        # (or first-startup if get_settings hasn't been called) leaves
        # an empty table and the UPDATE would silently no-op.
        conn.execute(
            "INSERT INTO memory_settings (id) VALUES (1) "
            "ON CONFLICT (id) DO NOTHING"
        )
        conn.execute(
            f"UPDATE memory_settings SET {set_clause} WHERE id = 1",
            values,
        )
        conn.commit()
    return get_settings()


def get_agent_toggles(agent: str) -> dict[str, Any]:
    """Return ``agent_memory_settings`` row for ``agent``, or defaults if
    missing (agents without an explicit row inherit ON)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_memory_settings WHERE agent = %s",
            (agent,),
        ).fetchone()
        if row:
            return dict(row)
    return {
        "agent": agent,
        "user_memory_enabled": True,
        "agent_memory_enabled": True,
    }


def set_agent_toggle(agent: str, key: str, value: Any) -> dict[str, Any]:
    """Upsert a single field in ``agent_memory_settings``. Returns full row."""
    allowed = {"user_memory_enabled", "agent_memory_enabled"}
    if key not in allowed:
        raise ValueError(f"unknown agent_memory_settings field: {key}")
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO agent_memory_settings (agent, {key})
                VALUES (%s, %s)
                ON CONFLICT (agent) DO UPDATE SET {key} = EXCLUDED.{key}""",
            (agent, value),
        )
        conn.commit()
    return get_agent_toggles(agent)
