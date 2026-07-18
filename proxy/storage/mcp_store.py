"""MCP Framework — DB operations for MCP state, agent assignments, skills, config.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

import json
from datetime import datetime, timezone

from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# MCP State (platform-level enable/disable)
# ---------------------------------------------------------------------------

def get_mcp_state(name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name, enabled, updated_at, tool_filter_regex "
            "FROM mcp_state WHERE name = %s",
            (name,),
        ).fetchone()
        return dict(row) if row else None


def get_tool_filter_regex(name: str) -> str:
    """Read the per-MCP runtime tool filter regex.

    Returns empty string when no row exists or when admin hasn't set one.
    Empty string means "no filter — expose the full tool surface."
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tool_filter_regex FROM mcp_state WHERE name = %s",
            (name,),
        ).fetchone()
        return (row["tool_filter_regex"] if row else "") or ""


def set_tool_filter_regex(name: str, regex: str) -> None:
    """Update the per-MCP tool filter regex.

    Does NOT create the mcp_state row — assumes the MCP was already
    registered via ``ensure_mcp_state``. Empty string clears the filter.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE mcp_state SET tool_filter_regex = %s, updated_at = %s "
            "WHERE name = %s",
            (regex or "", _now(), name),
        )
        conn.commit()


def get_all_mcp_states() -> dict[str, bool]:
    """Return {mcp_name: enabled} for all MCPs with state."""
    with get_conn() as conn:
        rows = conn.execute("SELECT name, enabled FROM mcp_state").fetchall()
        return {r["name"]: bool(r["enabled"]) for r in rows}


def set_mcp_enabled(name: str, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mcp_state (name, enabled, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT(name) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = EXCLUDED.updated_at""",
            (name, enabled, _now()),
        )
        conn.commit()


def ensure_mcp_state(name: str, default_enabled: bool) -> None:
    """Insert mcp_state row if not exists (idempotent)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mcp_state (name, enabled, updated_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (name, default_enabled, _now()),
        )
        conn.commit()


def is_mcp_state_empty() -> bool:
    """Check if mcp_state table has any rows (used for seed migration)."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM mcp_state").fetchone()
        return row["cnt"] == 0


def delete_mcp_all_data(mcp_name: str, skill_ids: list[str] | None = None) -> None:
    """Remove all DB data for an MCP: state, agent assignments, skills, config, instances.

    ``skill_ids`` are the manifest's declared skill ids — skill ids are BARE
    identifiers (``voiceover``, ``frontend-design``), not namespaced by the
    provider, so the caller must pass them for the ``agent_skills`` rows to
    be cleaned up (the provider name never appears in ``skill_id``).
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM mcp_state WHERE name = %s", (mcp_name,))
        conn.execute("DELETE FROM agent_mcps WHERE mcp_name = %s", (mcp_name,))
        for sid in skill_ids or []:
            conn.execute("DELETE FROM agent_skills WHERE skill_id = %s", (sid,))
        conn.execute("DELETE FROM mcp_config_values WHERE mcp_name = %s", (mcp_name,))
        conn.execute("DELETE FROM mcp_instances WHERE mcp_name = %s", (mcp_name,))
        conn.commit()


# ---------------------------------------------------------------------------
# Manager-enabled MCPs (the agent_mcps table)
#
# A row in agent_mcps means the manager has TOGGLED this MCP ON for this
# agent. It does NOT mean the MCP is visible/authorized — visibility is
# computed separately via the mcp_instances table (for explicit-mode MCPs)
# or from the manifest's assignment_mode (for auto-mode MCPs).
#
# Runtime loads an MCP only when (visible AND manager-enabled AND platform-
# enabled). See mcp_registry.get_agent_mcps() for the choke-point function.
# ---------------------------------------------------------------------------

def get_manager_enabled_mcps(agent_name: str) -> list[str]:
    """Return MCP names the manager has toggled on for this agent.

    Visibility is NOT checked here. The runtime path filters this list against
    the visibility set; UI consumers should use mcp_registry.get_visible_mcps_for_agent
    plus this list to compute per-row enabled state.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name FROM agent_mcps WHERE agent_name = %s ORDER BY mcp_name",
            (agent_name,),
        ).fetchall()
        return [r["mcp_name"] for r in rows]


def get_all_manager_enabled_mcps() -> dict[str, list[str]]:
    """Return {agent_name: [mcp_names]} for all agents (manager-enabled set)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, mcp_name FROM agent_mcps ORDER BY agent_name, mcp_name"
        ).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r["agent_name"], []).append(r["mcp_name"])
        return result


def get_mcp_agents(mcp_name: str) -> list[str]:
    """Return agent names that have manager-enabled this MCP."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name FROM agent_mcps WHERE mcp_name = %s ORDER BY agent_name",
            (mcp_name,),
        ).fetchall()
        return [r["agent_name"] for r in rows]


def set_manager_enabled_mcps(agent_name: str, mcp_names: list[str]) -> None:
    """Replace the manager-enabled set for an agent.

    Visibility is enforced by the API layer before calling this. agent_skills
    rows are NOT deleted when an MCP is removed — manager intent is preserved
    so re-enabling restores the prior skill state.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_mcps WHERE agent_name = %s", (agent_name,))
        for name in mcp_names:
            conn.execute(
                "INSERT INTO agent_mcps (agent_name, mcp_name) VALUES (%s, %s)",
                (agent_name, name),
            )
        conn.commit()


def add_agent_mcp(agent_name: str, mcp_name: str) -> None:
    """Idempotently mark an MCP as manager-enabled for this agent."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_mcps (agent_name, mcp_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (agent_name, mcp_name),
        )
        conn.commit()


def remove_agent_mcp(agent_name: str, mcp_name: str) -> None:
    """Mark an MCP as manager-disabled for this agent."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM agent_mcps WHERE agent_name = %s AND mcp_name = %s",
            (agent_name, mcp_name),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Agent-Skill Assignments
# ---------------------------------------------------------------------------

def get_agent_skills(agent_name: str) -> list[dict]:
    """Return [{skill_id, enabled, exclude_from}] for an agent."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT skill_id, enabled, exclude_from FROM agent_skills WHERE agent_name = %s",
            (agent_name,),
        ).fetchall()
        result = []
        for r in rows:
            try:
                excl = json.loads(r["exclude_from"])
            except (json.JSONDecodeError, TypeError):
                excl = []
            result.append({
                "skill_id": r["skill_id"],
                "enabled": bool(r["enabled"]),
                "exclude_from": excl,
            })
        return result


def set_agent_skill(
    agent_name: str, skill_id: str, enabled: bool, exclude_from: list[str]
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_skills (agent_name, skill_id, enabled, exclude_from)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(agent_name, skill_id) DO UPDATE
               SET enabled = EXCLUDED.enabled, exclude_from = EXCLUDED.exclude_from""",
            (agent_name, skill_id, enabled, json.dumps(exclude_from)),
        )
        conn.commit()


def ensure_agent_skill(
    agent_name: str, skill_id: str, default_enabled: bool, default_exclude_from: list[str]
) -> None:
    """Insert agent_skill row if not exists (idempotent)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_skills (agent_name, skill_id, enabled, exclude_from) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (agent_name, skill_id, default_enabled, json.dumps(default_exclude_from)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# MCP Config Values
# ---------------------------------------------------------------------------

def get_mcp_config_values(mcp_name: str) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT config_key, config_value FROM mcp_config_values WHERE mcp_name = %s",
            (mcp_name,),
        ).fetchall()
        return {r["config_key"]: r["config_value"] for r in rows}


def get_mcp_config_value(mcp_name: str, key: str) -> str | None:
    """Read a single config value, or None if unset.

    Used for reserved control keys (``_hosted_service_mode``,
    ``_managed_instance_deleted``) where a single lookup is cheaper than
    pulling the whole map.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT config_value FROM mcp_config_values "
            "WHERE mcp_name = %s AND config_key = %s",
            (mcp_name, key),
        ).fetchone()
        return row["config_value"] if row else None


def set_mcp_config_value(mcp_name: str, key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mcp_config_values (mcp_name, config_key, config_value)
               VALUES (%s, %s, %s)
               ON CONFLICT(mcp_name, config_key) DO UPDATE SET config_value = EXCLUDED.config_value""",
            (mcp_name, key, value),
        )
        conn.commit()


def set_mcp_config_values(mcp_name: str, values: dict[str, str]) -> None:
    """Set multiple config values at once."""
    with get_conn() as conn:
        for key, value in values.items():
            conn.execute(
                """INSERT INTO mcp_config_values (mcp_name, config_key, config_value)
                   VALUES (%s, %s, %s)
                   ON CONFLICT(mcp_name, config_key) DO UPDATE SET config_value = EXCLUDED.config_value""",
                (mcp_name, key, value),
            )
        conn.commit()


def get_all_mcp_config_values() -> dict[str, dict[str, str]]:
    """Return {mcp_name: {key: value}} for all MCPs."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, config_key, config_value FROM mcp_config_values"
        ).fetchall()
        result: dict[str, dict[str, str]] = {}
        for r in rows:
            result.setdefault(r["mcp_name"], {})[r["config_key"]] = r["config_value"]
        return result


# ---------------------------------------------------------------------------
# MCP Instances (generalized per-instance, per-agent configuration)
#
# An "instance" is a saved bundle of credential/config field values for an
# explicit-mode MCP. Two ways an instance authorizes an agent:
#   1. agent name appears in the instance's `agents` JSON array
#   2. instance has assigned_to_all=True (catch-all for all current+future agents)
# ---------------------------------------------------------------------------


def _normalize_instance_row(d: dict) -> dict:
    """Decode field_values_enc + parse agents JSON + ensure assigned_to_all is bool.

    Mutates in place and also returns the dict.
    """
    from storage.credential_store import _decrypt
    try:
        d["field_values"] = json.loads(_decrypt(d.pop("field_values_enc")))
    except Exception:
        d["field_values"] = {}
        d.pop("field_values_enc", None)
    try:
        d["agents"] = json.loads(d["agents"]) if d["agents"] else []
    except (json.JSONDecodeError, TypeError):
        d["agents"] = []
    d["assigned_to_all"] = bool(d.get("assigned_to_all", False))
    # Hosted-relay columns — set defaults OUTSIDE the decrypt
    # try-block above so a decrypt failure can't drop them.
    d.setdefault("hosted_mode", "self_managed")
    d.setdefault("managed_by", "admin")
    return d


def get_mcp_instances(mcp_name: str) -> list[dict]:
    """Return all instances for an MCP with decrypted field values.

    Order: alphabetical by instance_name (admin UI display order).
    Each dict includes: id, mcp_name, instance_name, field_values, agents,
    assigned_to_all, created_at, updated_at.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_instances WHERE mcp_name = %s ORDER BY instance_name",
            (mcp_name,),
        ).fetchall()
        return [_normalize_instance_row(dict(r)) for r in rows]


def get_mcp_instances_for_agent(mcp_name: str, agent_name: str) -> list[dict]:
    """Return instances of mcp_name authorizing agent_name.

    An instance authorizes the agent if EITHER:
      - agent_name is in the instance's agents list, OR
      - the instance has assigned_to_all = True

    Order: ascending id (deterministic; do NOT rely on instance_name alphabetical
    order for runtime decisions). Used by config_file delivery in mcp_registry
    and by visibility checks.
    """
    all_instances = get_mcp_instances(mcp_name)
    matching = [
        i for i in all_instances
        if agent_name in i["agents"] or i["assigned_to_all"]
    ]
    return sorted(matching, key=lambda i: i["id"])


def get_instance_for_agent_env_delivery(
    mcp_name: str, agent_name: str
) -> dict | None:
    """Pick a single instance for an env-delivery MCP and agent.

    Precedence (deterministic):
      1. instance with agent_name in agents list, ordered by id ASC
      2. instance with assigned_to_all = True, ordered by id ASC
      3. None — no instance authorizes the agent

    Rationale: explicit per-agent assignment beats catch-all because the admin
    made a deliberate decision to wire up that specific instance for that agent.
    """
    instances = get_mcp_instances(mcp_name)

    explicit = sorted(
        [i for i in instances if agent_name in i["agents"]],
        key=lambda i: i["id"],
    )
    if explicit:
        return explicit[0]

    catchall = sorted(
        [i for i in instances if i["assigned_to_all"]],
        key=lambda i: i["id"],
    )
    if catchall:
        return catchall[0]

    return None


def is_agent_authorized_for_mcp(mcp_name: str, agent_name: str) -> bool:
    """True if any instance of mcp_name authorizes agent_name.

    Used by API-layer validation when a manager submits a PUT to enable an MCP.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agents, assigned_to_all FROM mcp_instances WHERE mcp_name = %s",
            (mcp_name,),
        ).fetchall()
    for r in rows:
        if r["assigned_to_all"]:
            return True
        try:
            agents = json.loads(r["agents"]) if r["agents"] else []
        except (json.JSONDecodeError, TypeError):
            agents = []
        if agent_name in agents:
            return True
    return False


def get_visible_explicit_mcps(agent_name: str) -> set[str]:
    """Return MCP names where this agent has visibility via at least one instance.

    Used as the bulk visibility helper for both the UI list endpoint and the
    runtime registry. One SQL call instead of N+1.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, agents, assigned_to_all FROM mcp_instances"
        ).fetchall()
    result: set[str] = set()
    for r in rows:
        if r["assigned_to_all"]:
            result.add(r["mcp_name"])
            continue
        try:
            agents = json.loads(r["agents"]) if r["agents"] else []
        except (json.JSONDecodeError, TypeError):
            agents = []
        if agent_name in agents:
            result.add(r["mcp_name"])
    return result


def upsert_mcp_instance(mcp_name: str, data: dict) -> int:
    """Create or update an MCP instance. Returns the instance ID.

    data: {
        "instance_name": str,
        "field_values": dict,
        "agents": list[str],
        "assigned_to_all": bool (default False),
        "hosted_mode": str (default 'self_managed'),
        "managed_by": str (default 'admin'; 'system' for the startup pass),
    }

    Persists `agents` and `assigned_to_all` independently — the backend never
    second-guesses the UI. Runtime ignores `agents` when `assigned_to_all` is
    True per the precedence rules.
    """
    from storage.credential_store import _encrypt

    instance_name = data["instance_name"]
    field_values_enc = _encrypt(json.dumps(data.get("field_values", {})))
    agents_json = json.dumps(data.get("agents", []))
    assigned_to_all = bool(data.get("assigned_to_all", False))
    hosted_mode = data.get("hosted_mode", "self_managed")
    managed_by = data.get("managed_by", "admin")
    now = _now()

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mcp_instances
               (mcp_name, instance_name, field_values_enc, agents, assigned_to_all, hosted_mode, managed_by, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (mcp_name, instance_name) DO UPDATE SET
                   field_values_enc = EXCLUDED.field_values_enc,
                   agents = EXCLUDED.agents,
                   assigned_to_all = EXCLUDED.assigned_to_all,
                   hosted_mode = EXCLUDED.hosted_mode,
                   managed_by = EXCLUDED.managed_by,
                   updated_at = EXCLUDED.updated_at""",
            (mcp_name, instance_name, field_values_enc, agents_json, assigned_to_all, hosted_mode, managed_by, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM mcp_instances WHERE mcp_name = %s AND instance_name = %s",
            (mcp_name, instance_name),
        ).fetchone()
        return row["id"] if row else 0


def update_mcp_instance_by_id(
    instance_id: int, mcp_name: str, data: dict,
) -> bool:
    """Full UPDATE of an instance by primary key (PUT semantics).

    Distinct from :func:`upsert_mcp_instance` which keys on
    ``(mcp_name, instance_name)`` and is right for POST/create. Here the
    URL ``{instance_id}`` is authoritative so the admin can rename an
    existing instance without leaving an orphan row behind — the
    previous PUT path (also upsert) used to INSERT a fresh row on
    rename because the new (mcp_name, instance_name) tuple didn't
    collide with anything, leaving the old row dangling.

    Pre-checks for an `instance_name` collision against any OTHER row
    of the same MCP. Raises ``ValueError`` with a clear message so the
    API layer can translate to 409. Returns ``True`` if the row was
    updated, ``False`` if ``instance_id`` wasn't found (API → 404).
    Credentials are re-encrypted on every call (same as upsert) so the
    new ``field_values`` always replaces the old ones atomically.
    """
    from storage.credential_store import _encrypt

    instance_name = data["instance_name"]
    field_values_enc = _encrypt(json.dumps(data.get("field_values", {})))
    agents_json = json.dumps(data.get("agents", []))
    assigned_to_all = bool(data.get("assigned_to_all", False))
    hosted_mode = data.get("hosted_mode", "self_managed")
    now = _now()

    with get_conn() as conn:
        collision = conn.execute(
            "SELECT id FROM mcp_instances "
            "WHERE mcp_name = %s AND instance_name = %s AND id != %s",
            (mcp_name, instance_name, instance_id),
        ).fetchone()
        if collision:
            raise ValueError(
                f"Another instance of '{mcp_name}' is already named "
                f"'{instance_name}'",
            )
        # `managed_by` is deliberately NOT updated here — it's set once at
        # creation ('admin') or by the startup pass ('system') and stays
        # immutable through the admin edit path. The API endpoint guards
        # against flipping a system instance to self_managed.
        cursor = conn.execute(
            """UPDATE mcp_instances
               SET instance_name = %s,
                   field_values_enc = %s,
                   agents = %s,
                   assigned_to_all = %s,
                   hosted_mode = %s,
                   updated_at = %s
               WHERE id = %s AND mcp_name = %s""",
            (instance_name, field_values_enc, agents_json, assigned_to_all,
             hosted_mode, now, instance_id, mcp_name),
        )
        conn.commit()
        return cursor.rowcount > 0


def add_agent_to_instance(instance_id: int, agent_name: str) -> bool:
    """Idempotently add ``agent_name`` to an instance's ``agents`` list.

    Surgical update that does NOT touch ``field_values_enc`` — avoids the
    decrypt-encrypt round trip ``upsert_mcp_instance`` would require to
    re-persist unchanged credentials. Returns True if the agent was added,
    False if the agent was already present (or the instance doesn't exist).

    Used by the request-approval flow for explicit-mode MCPs.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT agents FROM mcp_instances WHERE id = %s",
            (instance_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            current = json.loads(row["agents"]) if row["agents"] else []
        except (json.JSONDecodeError, TypeError):
            current = []
        if agent_name in current:
            return False
        current.append(agent_name)
        conn.execute(
            "UPDATE mcp_instances SET agents = %s, updated_at = %s WHERE id = %s",
            (json.dumps(current), _now(), instance_id),
        )
        conn.commit()
        return True


def delete_mcp_instance_with_tombstone(instance_id: int) -> None:
    """Delete an MCP instance by ID; tombstone it if it was platform-managed.

    When the deleted row is ``managed_by='system'`` (created by the
    startup auto-instance pass), write a ``_managed_instance_deleted='true'``
    marker into ``mcp_config_values`` so the next startup pass does NOT
    recreate an instance the admin deliberately removed.

    All of it — SELECT, DELETE, and the tombstone write — runs in ONE
    ``with get_conn()`` block / ONE commit. ``get_conn()`` hands out a fresh
    pooled connection per call, so splitting this across two store functions
    would be two transactions; a crash in between could resurrect the row.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mcp_name, managed_by FROM mcp_instances WHERE id = %s",
            (instance_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute("DELETE FROM mcp_instances WHERE id = %s", (instance_id,))
        if row["managed_by"] == "system":
            conn.execute(
                """INSERT INTO mcp_config_values (mcp_name, config_key, config_value)
                   VALUES (%s, %s, %s)
                   ON CONFLICT(mcp_name, config_key) DO UPDATE SET config_value = EXCLUDED.config_value""",
                (row["mcp_name"], "_managed_instance_deleted", "true"),
            )
        conn.commit()


def reconcile_system_instances(relay_mcp_names: set[str]) -> list[str]:
    """Drop platform-managed ('system') instances whose MCP no longer offers
    api_key_relay.

    ``relay_mcp_names`` is the set of MCPs whose manifest still declares an
    available ``hosted.api_key_relay`` block. Any ``managed_by='system'``
    instance for an MCP NOT in that set is stale — the manifest dropped its
    hosted block (e.g. a vendor whose ToS forbids relaying) — and is removed.

    Plain delete, NO ``_managed_instance_deleted`` tombstone: the tombstone
    exists to suppress recreation after an ADMIN delete, whereas a re-enabled
    api_key_relay SHOULD let the startup pass recreate the instance.

    Returns the mcp_names whose stale instance was removed (for logging).
    """
    removed: list[str] = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, mcp_name FROM mcp_instances WHERE managed_by = 'system'"
        ).fetchall()
        for row in rows:
            if row["mcp_name"] not in relay_mcp_names:
                conn.execute("DELETE FROM mcp_instances WHERE id = %s", (row["id"],))
                removed.append(row["mcp_name"])
        conn.commit()
    return removed


def get_system_instance(mcp_name: str) -> dict | None:
    """Return the platform-managed ('system') instance for an MCP, or None.

    Keyed on ``managed_by='system'`` (NOT instance_name) so the
    startup pass finds it even after an admin renamed it — which is what
    prevents the pass from spawning a duplicate every restart.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_instances WHERE mcp_name = %s AND managed_by = 'system' "
            "ORDER BY id LIMIT 1",
            (mcp_name,),
        ).fetchone()
        return _normalize_instance_row(dict(row)) if row else None


def get_all_mcp_instances() -> dict[str, list[dict]]:
    """Return all instances grouped by mcp_name. For admin listing."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_instances ORDER BY mcp_name, instance_name"
        ).fetchall()
        result: dict[str, list[dict]] = {}
        for r in rows:
            d = _normalize_instance_row(dict(r))
            result.setdefault(d["mcp_name"], []).append(d)
        return result
