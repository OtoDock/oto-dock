"""Phone route store — DB operations for telephony routing.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

import json
import uuid
from datetime import datetime, timezone

from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: dict) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


# ---------------------------------------------------------------------------
# Inbound PIN — encrypted sidecar credential (phone-server secret precedent:
# no route column, so the raw ``SELECT *`` rows that feed API responses and
# the daemon config push can never leak it by accident).
# ---------------------------------------------------------------------------

#: Inner credential key for a route's PIN.
ROUTE_PIN_KEY = "PIN"


def pin_cred_name(route_id: str) -> str:
    """infra_credentials mcp_name for a route's inbound PIN."""
    return f"phone-route-{route_id}-pin"


def get_route_pin(route_id: str) -> str:
    """Decrypted PIN for a route ("" when none is configured)."""
    from storage.credential_store import get_infra_credentials
    return get_infra_credentials(pin_cred_name(route_id)).get(ROUTE_PIN_KEY, "")


def set_route_pin(route_id: str, pin: str) -> None:
    from storage.credential_store import set_infra_credentials
    set_infra_credentials(pin_cred_name(route_id), {ROUTE_PIN_KEY: pin})


def delete_route_pin(route_id: str) -> None:
    from storage.credential_store import delete_infra_credentials
    delete_infra_credentials(pin_cred_name(route_id))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_all_routes() -> list[dict]:
    """Get all phone routes, sorted by direction then name."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM phone_routes ORDER BY direction, name"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_route(route_id: str) -> dict | None:
    """Get a single route by ID."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM phone_routes WHERE id = %s", (route_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_route_by_uuid(audiosocket_uuid: str) -> dict | None:
    """Get a route by its AudioSocket UUID (inbound lookup)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM phone_routes WHERE audiosocket_uuid = %s",
            (audiosocket_uuid,),
        ).fetchone()
        return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_route(data: dict) -> dict:
    """Insert a new phone route and return its record.

    ``phone_server_id`` is required by the schema (FK, NOT NULL): a route is
    provisioned against a phone server. Optional provider /
    mode columns fall back to their DB defaults — NULL provider = call
    default; filler modes default 'on'.
    """
    route_id = data.get("id") or str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO phone_routes
               (id, direction, name, agent, language, llm_mode,
                phone_server_id, stt_provider_id, tts_provider_id,
                greeting, phone_context_override,
                backchannel_mode, thinking_filler_mode, background_sound,
                enabled,
                audiosocket_uuid, did, ami_caller_id, ami_outbound_context,
                dial_prefix, adapter_data, trigger_slug, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                route_id,
                data.get("direction", "inbound"),
                data.get("name", ""),
                data.get("agent", "personal-assistant"),
                data.get("language", "en"),
                data.get("llm_mode", "proxy"),
                data.get("phone_server_id"),
                data.get("stt_provider_id"),
                data.get("tts_provider_id"),
                data.get("greeting", ""),
                data.get("phone_context_override", ""),
                data.get("backchannel_mode", "on"),
                data.get("thinking_filler_mode", "on"),
                data.get("background_sound", "off"),
                data.get("enabled", True),
                data.get("audiosocket_uuid") or None,
                data.get("did") or None,
                data.get("ami_caller_id", ""),
                data.get("ami_outbound_context", ""),
                data.get("dial_prefix", ""),
                json.dumps(data.get("adapter_data") or {}),
                data.get("trigger_slug") or None,
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM phone_routes WHERE id = %s", (route_id,)
        ).fetchone()
        return _row_to_dict(row)


def update_route(route_id: str, data: dict) -> dict | None:
    """Partial update of a phone route. Returns updated record or None."""
    allowed = {
        "direction", "name", "agent", "language", "llm_mode",
        "phone_server_id", "stt_provider_id", "tts_provider_id",
        "greeting", "phone_context_override",
        "backchannel_mode", "thinking_filler_mode", "background_sound",
        "enabled",
        "audiosocket_uuid", "did", "ami_caller_id",
        "ami_outbound_context", "dial_prefix", "trigger_slug",
    }
    # ``trigger_slug`` may be cleared by passing empty string — keep it in the
    # update dict so the explicit clear lands in DB. Other fields are dropped
    # when None (Pydantic ``exclude_unset`` already handles "not supplied").
    updates = {
        k: v for k, v in data.items()
        if k in allowed and (v is not None or k == "trigger_slug")
    }
    if not updates:
        return get_route(route_id)

    # Allow clearing audiosocket_uuid by passing empty string → NULL
    if "audiosocket_uuid" in updates and updates["audiosocket_uuid"] == "":
        updates["audiosocket_uuid"] = None
    # Same for trigger_slug — empty string means "unbind trigger".
    if "trigger_slug" in updates and updates["trigger_slug"] == "":
        updates["trigger_slug"] = None

    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [route_id]

    with get_conn() as conn:
        conn.execute(
            f"UPDATE phone_routes SET {set_clause} WHERE id = %s", values
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM phone_routes WHERE id = %s", (route_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def set_adapter_data(
    route_id: str, *, adapter_data: dict, audiosocket_uuid: str | None = None,
) -> dict | None:
    """Persist adapter-provisioned state after a successful provision.

    Called by the route-create / re-provision cascade once the adapter returns a
    ``RouteHandle``. ``adapter_data`` is the opaque per-adapter blob (provider-side
    ids, AstDB keys, …). ``audiosocket_uuid`` is written only when provided
    (inbound provisioning allocates or confirms it); pass None to leave it as-is.
    """
    now = _now()
    sets = ["adapter_data = %s"]
    values: list = [json.dumps(adapter_data or {})]
    if audiosocket_uuid is not None:
        sets.append("audiosocket_uuid = %s")
        values.append(audiosocket_uuid or None)
    sets.append("updated_at = %s")
    values.append(now)
    values.append(route_id)
    with get_conn() as conn:
        row = conn.execute(
            f"UPDATE phone_routes SET {', '.join(sets)} WHERE id = %s RETURNING *",
            values,
        ).fetchone()
        conn.commit()
        return _row_to_dict(row) if row else None


def delete_route(route_id: str) -> bool:
    """Delete a phone route. Returns True if deleted."""
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM phone_routes WHERE id = %s", (route_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
