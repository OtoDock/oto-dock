"""Per-user roaming UI preferences — a small free-form JSON bag the dashboard
persists server-side so sticky choices follow the user across devices (first
tenant: ``last_execution_mode``, the per-agent interactive/headless pick).

One row per user (PK = ``user_sub``, FK → users ON DELETE CASCADE). No row =
empty bag; ``upsert_prefs`` shallow-merges the provided top-level keys onto
whatever is stored. Backed by GET/PUT /v1/users/me/ui-prefs (the audio-prefs
pattern); localStorage on the client remains the offline fallback.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_field(value) -> dict:
    if isinstance(value, str):
        try:
            return json.loads(value) or {}
        except (ValueError, TypeError):
            return {}
    return value or {}


def get_prefs(user_sub: str) -> dict:
    """Return the user's prefs bag, or ``{}`` if unset."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM user_ui_prefs WHERE user_sub = %s", (user_sub,)
        ).fetchone()
        if row:
            return _json_field(row["data"])
    return {}


# Ceiling on the MERGED bag, not just one PUT: the API caps each payload at
# 8 KB, but the shallow merge accumulates distinct top-level keys — without
# this, many small PUTs grow the row (and every GET) without bound.
_MERGED_PREFS_MAX_BYTES = 64 * 1024


def mark_release_seen(user_sub: str, version: str) -> None:
    """``last_seen_version`` set in place (``data || {...}`` in one
    statement): atomic, so a second tab's read-then-write merge cannot drop
    it, and outside the merged-bag cap (one short key)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_ui_prefs (user_sub, data, updated_at)
               VALUES (%s, %s::jsonb, %s)
               ON CONFLICT (user_sub) DO UPDATE SET
                   data = user_ui_prefs.data || EXCLUDED.data,
                   updated_at = EXCLUDED.updated_at""",
            (user_sub, json.dumps({"last_seen_version": version}), _now()),
        )
        conn.commit()


def engine_firsts(user_sub: str) -> dict[str, str]:
    """``engine_first``: per agent, the engine the person's model picker
    last showed first (``services/engines/engine_resolution.py``, the
    engine order by use); ``{}`` when none was shown."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data -> 'engine_first' AS firsts FROM user_ui_prefs WHERE user_sub = %s",
            (user_sub,),
        ).fetchone()
    firsts = _json_field(row["firsts"]) if row else {}
    if not isinstance(firsts, dict):
        return {}
    return {str(k): v for k, v in firsts.items() if isinstance(v, str)}


def remember_engine_firsts(user_sub: str, firsts: dict[str, str]) -> None:
    """Merge ``firsts`` into ``engine_first`` key by key in one statement
    (two tabs remembering different agents never drop each other's), outside
    the merged-bag cap like ``last_seen_version``: one short key per agent
    the person opens."""
    if not firsts:
        return
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_ui_prefs (user_sub, data, updated_at)
               VALUES (%s, jsonb_build_object('engine_first', %s::jsonb), %s)
               ON CONFLICT (user_sub) DO UPDATE SET
                   data = user_ui_prefs.data || jsonb_build_object(
                       'engine_first',
                       CASE WHEN jsonb_typeof(user_ui_prefs.data -> 'engine_first') = 'object'
                            THEN user_ui_prefs.data -> 'engine_first' ELSE '{}'::jsonb END
                       || %s::jsonb),
                   updated_at = EXCLUDED.updated_at""",
            (user_sub, json.dumps(firsts), _now(), json.dumps(firsts)),
        )
        conn.commit()


def upsert_prefs(user_sub: str, data: dict) -> dict:
    """Insert or update the user's prefs bag. Shallow merge: the provided
    top-level keys overwrite the stored ones; every other stored key is kept.
    The merge happens in the statement (``data || EXCLUDED.data``), so two
    tabs writing different keys never drop each other's; the cap is judged on
    the bag as read, a race across it is a few bytes."""
    merged = {**get_prefs(user_sub), **data}
    if len(json.dumps(merged)) > _MERGED_PREFS_MAX_BYTES:
        raise ValueError(
            f"ui-prefs bag would exceed {_MERGED_PREFS_MAX_BYTES} bytes — "
            f"remove stale keys before adding more"
        )
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO user_ui_prefs (user_sub, data, updated_at)
               VALUES (%s, %s::jsonb, %s)
               ON CONFLICT (user_sub) DO UPDATE SET
                   data = user_ui_prefs.data || EXCLUDED.data,
                   updated_at = EXCLUDED.updated_at
               RETURNING data""",
            (user_sub, json.dumps(data), now),
        ).fetchone()
        conn.commit()
        return _json_field(row["data"])
