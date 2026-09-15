"""Audio provider store — STT / TTS providers shared by chat audio + telephony.

Each row is one provider (Deepgram STT, Cartesia TTS, …). Providers carry
enabled/default flags split by context (``calls`` vs ``chat``), a per-language
``voices`` map, an ``advanced`` JSONB (endpointing, etc.) and a
``credential_key`` pointing at ``infra_credentials.mcp_name``.

Invariants enforced here (the schema can't):
  - At most one default STT and one default TTS per context (partial unique
    indexes guarantee it; ``set_default`` clears the prior default first).
  - A provider can only be default for a context it is enabled for.
    ``set_default`` rejects a disabled context; disabling a context in
    ``update_provider`` auto-demotes that context's default.

Deleting a provider that a phone route still references is blocked by the FK
(``ON DELETE RESTRICT``); callers should pre-check ``routes_using_provider``
to return a friendly 409.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from storage.pg import get_conn

# Provider contexts (a provider can be enabled / default for either or both).
CONTEXTS = ("calls", "chat")

# All audio-provider API keys live under this single inner key in
# ``infra_credentials[provider.credential_key]`` so any provider resolves
# uniformly (the credential row's mcp_name is the provider's ``credential_key``).
CREDENTIAL_INNER_KEY = "API_KEY"

_BOOL_COLS = (
    "enabled_for_calls", "enabled_for_chat", "is_default_calls", "is_default_chat",
)


class ProviderDefaultDisabledError(ValueError):
    """Raised when trying to default a provider for a context it isn't enabled
    for. The store keeps "default ⇒ enabled" true at all times."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_field(value) -> dict:
    """JSONB columns come back as dicts under psycopg3; tolerate a raw string
    just in case the driver hands one back."""
    if isinstance(value, str):
        try:
            return json.loads(value) or {}
        except (ValueError, TypeError):
            return {}
    return value or {}


def _row_to_dict(row: dict) -> dict:
    d = dict(row)
    for col in _BOOL_COLS:
        d[col] = bool(d[col])
    d["voices"] = _json_field(d.get("voices"))
    d["advanced"] = _json_field(d.get("advanced"))
    return d


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_all_providers() -> list[dict]:
    """All providers, STT before TTS, then by id (creation order)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audio_providers ORDER BY provider_type, id"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_providers_by_type(provider_type: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audio_providers WHERE provider_type = %s ORDER BY id",
            (provider_type,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_provider(provider_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM audio_providers WHERE id = %s", (provider_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_default_provider(provider_type: str, context: str) -> dict | None:
    """The default provider of ``provider_type`` for ``context`` (calls|chat)."""
    col = f"is_default_{context}"
    if context not in CONTEXTS:
        raise ValueError(f"Unknown context: {context!r}")
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM audio_providers "
            f"WHERE provider_type = %s AND {col} = TRUE",
            (provider_type,),
        ).fetchone()
        return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Route-usage helpers (FK RESTRICT pre-check → 409)
# ---------------------------------------------------------------------------

def routes_using_provider(provider_id: int) -> list[str]:
    """Names of phone routes referencing this provider as STT or TTS."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, id FROM phone_routes "
            "WHERE stt_provider_id = %s OR tts_provider_id = %s "
            "ORDER BY name",
            (provider_id, provider_id),
        ).fetchall()
        return [r["name"] or r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_provider(data: dict) -> dict:
    """Insert a provider. ``provider_type`` + ``provider_name`` form the unique
    identity; the rest fall back to sensible defaults."""
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO audio_providers
               (provider_type, provider_name, label, credential_key,
                enabled_for_calls, enabled_for_chat, voices, advanced,
                created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING *""",
            (
                data["provider_type"],
                data["provider_name"],
                data.get("label") or data["provider_name"].title(),
                data.get("credential_key") or None,
                data.get("enabled_for_calls", True),
                data.get("enabled_for_chat", True),
                json.dumps(data.get("voices") or {}),
                json.dumps(data.get("advanced") or {}),
                now,
                now,
            ),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)


def update_provider(provider_id: int, data: dict) -> dict | None:
    """Partial update. Identity (``provider_type``/``provider_name``) is
    immutable. Disabling a context auto-demotes that context's default so the
    "default ⇒ enabled" invariant always holds.
    """
    allowed = {
        "label", "credential_key",
        "enabled_for_calls", "enabled_for_chat",
        "voices", "advanced",
    }
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if not updates:
        return get_provider(provider_id)

    # Auto-demote default when its context is being disabled.
    if updates.get("enabled_for_calls") is False:
        updates["is_default_calls"] = False
    if updates.get("enabled_for_chat") is False:
        updates["is_default_chat"] = False

    # JSONB fields serialise to text for the cast.
    for jcol in ("voices", "advanced"):
        if jcol in updates:
            updates[jcol] = json.dumps(updates[jcol] or {})

    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [provider_id]
    with get_conn() as conn:
        row = conn.execute(
            f"UPDATE audio_providers SET {set_clause} WHERE id = %s RETURNING *",
            values,
        ).fetchone()
        conn.commit()
        return _row_to_dict(row) if row else None


def set_default(provider_id: int, context: str) -> dict:
    """Make ``provider_id`` the default for ``context`` (calls|chat).

    Raises ``ProviderDefaultDisabledError`` if the provider isn't enabled for
    that context. Clears the previous default of the same ``provider_type`` +
    context in the same transaction (keeps the partial unique index happy).
    """
    if context not in CONTEXTS:
        raise ValueError(f"Unknown context: {context!r}")
    enabled_col = f"enabled_for_{context}"
    default_col = f"is_default_{context}"
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT provider_type, "
            f"{enabled_col} AS enabled FROM audio_providers WHERE id = %s",
            (provider_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Provider {provider_id} not found")
        if not bool(row["enabled"]):
            raise ProviderDefaultDisabledError(
                f"Provider {provider_id} is not enabled for {context}; "
                "enable it before making it the default."
            )
        provider_type = row["provider_type"]
        conn.execute(
            f"UPDATE audio_providers SET {default_col} = FALSE, updated_at = %s "
            f"WHERE provider_type = %s AND {default_col} = TRUE",
            (now, provider_type),
        )
        out = conn.execute(
            f"UPDATE audio_providers SET {default_col} = TRUE, updated_at = %s "
            "WHERE id = %s RETURNING *",
            (now, provider_id),
        ).fetchone()
        conn.commit()
        return _row_to_dict(out)


def delete_provider(provider_id: int) -> bool:
    """Delete a provider. Returns True if a row was removed. The phone_routes
    FK is ``ON DELETE RESTRICT`` — callers should pre-check
    ``routes_using_provider`` and surface a 409; this raises the driver's
    ForeignKeyViolation if a route still references it."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM audio_providers WHERE id = %s", (provider_id,)
        )
        conn.commit()
        return cur.rowcount > 0
