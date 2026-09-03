"""Departments store — company org structure for the agents map.

Departments + named levels are installation-wide metadata that the edge
compiler (services/departments/edge_compiler.py) turns into
agent_delegation_targets rows tagged source='department'. Agent membership
lives ON the agents row (agents.department_id / department_level_id, '' =
unassigned) and is written through agent_store.update_agent so the agents
cache invalidates.

All functions are synchronous (called via asyncio.to_thread from async code).
Ownership: ``created_by_sub`` stores the creating user's acting_sub; the API
layer compares it for creator-scoped edit/delete.
"""

import logging
import uuid
from datetime import datetime, timezone

from storage.pg import get_conn

logger = logging.getLogger(__name__)

MAX_LEVELS = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dept_row(row: dict) -> dict:
    d = dict(row)
    d["auto_delegation"] = bool(d["auto_delegation"])
    return d


def _levels_for(conn, department_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, rank, name FROM department_levels "
        "WHERE department_id = %s ORDER BY rank",
        (department_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_departments() -> list[dict]:
    """All departments with their ordered levels (visibility scoping is the
    API layer's job — this returns the installation-wide truth)."""
    with get_conn() as conn:
        depts = [
            _dept_row(r) for r in conn.execute(
                "SELECT * FROM departments ORDER BY name, id"
            ).fetchall()
        ]
        level_rows = conn.execute(
            "SELECT id, department_id, rank, name FROM department_levels "
            "ORDER BY department_id, rank"
        ).fetchall()
    by_dept: dict[str, list[dict]] = {}
    for r in level_rows:
        by_dept.setdefault(r["department_id"], []).append(
            {"id": r["id"], "rank": r["rank"], "name": r["name"]}
        )
    for d in depts:
        d["levels"] = by_dept.get(d["id"], [])
    return depts


def get_department(department_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM departments WHERE id = %s", (department_id,)
        ).fetchone()
        if not row:
            return None
        d = _dept_row(row)
        d["levels"] = _levels_for(conn, department_id)
        return d


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_department(
    name: str,
    created_by_sub: str,
    *,
    auto_delegation: bool = True,
    reach: str = "adjacent",
    level_names: list[str] | None = None,
) -> dict:
    """Create a department with its initial levels (rank = list order)."""
    names = [n.strip() for n in (level_names or ["Head", "Team"]) if n.strip()]
    if not names:
        raise ValueError("At least one level is required")
    if len(names) > MAX_LEVELS:
        raise ValueError(f"At most {MAX_LEVELS} levels allowed")
    if reach not in ("adjacent", "subtree"):
        raise ValueError(f"reach must be 'adjacent' or 'subtree', got {reach!r}")
    dept_id = f"dept-{uuid.uuid4().hex[:12]}"
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO departments "
            "(id, name, created_by_sub, auto_delegation, reach, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (dept_id, name, created_by_sub, auto_delegation, reach, now, now),
        )
        for rank, level_name in enumerate(names):
            conn.execute(
                "INSERT INTO department_levels (id, department_id, rank, name) "
                "VALUES (%s, %s, %s, %s)",
                (f"lvl-{uuid.uuid4().hex[:12]}", dept_id, rank, level_name),
            )
        conn.commit()
    return get_department(dept_id)  # type: ignore[return-value]


def update_department(department_id: str, **fields) -> dict | None:
    """Partial update of name / auto_delegation / reach / position_hint."""
    allowed = {"name", "auto_delegation", "reach", "position_hint"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "reach" in updates and updates["reach"] not in ("adjacent", "subtree"):
        raise ValueError(
            f"reach must be 'adjacent' or 'subtree', got {updates['reach']!r}"
        )
    if not updates:
        return get_department(department_id)
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE departments SET {set_clause} WHERE id = %s",
            list(updates.values()) + [department_id],
        )
        conn.commit()
    return get_department(department_id)


def set_department_levels(
    department_id: str, levels: list[dict]
) -> tuple[list[dict], list[str]]:
    """Replace a department's levels; rank = list order.

    ``levels`` items are {"id": existing-or-empty, "name": str} — keeping an
    id preserves member assignments on that level across renames/reorders.
    Agents on a REMOVED level drop out of the department entirely (both
    fields cleared — "in a department without a level" has no delegation
    meaning); their slugs are returned so the UI can surface it.

    Returns (new levels, unassigned agent slugs).
    """
    cleaned: list[dict] = []
    for item in levels:
        name = (item.get("name") or "").strip()
        if not name:
            raise ValueError("Level names cannot be empty")
        cleaned.append({"id": (item.get("id") or "").strip(), "name": name})
    if not cleaned:
        raise ValueError("At least one level is required")
    if len(cleaned) > MAX_LEVELS:
        raise ValueError(f"At most {MAX_LEVELS} levels allowed")

    unassigned: list[str] = []
    with get_conn() as conn:
        existing = {lv["id"] for lv in _levels_for(conn, department_id)}
        kept = {c["id"] for c in cleaned if c["id"]}
        unknown = kept - existing
        if unknown:
            raise ValueError(f"Unknown level id(s): {sorted(unknown)}")
        removed = existing - kept
        if removed:
            rows = conn.execute(
                "SELECT slug FROM agents WHERE department_id = %s "
                "AND department_level_id = ANY(%s) ORDER BY slug",
                (department_id, list(removed)),
            ).fetchall()
            unassigned = [r["slug"] for r in rows]
            conn.execute(
                "UPDATE agents SET department_id = '', department_level_id = '' "
                "WHERE department_id = %s AND department_level_id = ANY(%s)",
                (department_id, list(removed)),
            )
            conn.execute(
                "DELETE FROM department_levels WHERE department_id = %s "
                "AND id = ANY(%s)",
                (department_id, list(removed)),
            )
        for rank, c in enumerate(cleaned):
            if c["id"]:
                conn.execute(
                    "UPDATE department_levels SET rank = %s, name = %s "
                    "WHERE id = %s AND department_id = %s",
                    (rank, c["name"], c["id"], department_id),
                )
            else:
                conn.execute(
                    "INSERT INTO department_levels (id, department_id, rank, name) "
                    "VALUES (%s, %s, %s, %s)",
                    (f"lvl-{uuid.uuid4().hex[:12]}", department_id, rank, c["name"]),
                )
        conn.execute(
            "UPDATE departments SET updated_at = %s WHERE id = %s",
            (_now(), department_id),
        )
        conn.commit()
        new_levels = _levels_for(conn, department_id)
    if unassigned:
        # agents rows changed behind agent_store's cache
        from storage import agent_store
        agent_store._invalidate_cache()
    return new_levels, unassigned


def delete_department(department_id: str) -> list[str]:
    """Delete a department; members drop to unassigned (levels CASCADE).

    Returns the slugs of agents that were members (compiled edges are the
    caller's job — recompile after)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT slug FROM agents WHERE department_id = %s ORDER BY slug",
            (department_id,),
        ).fetchall()
        members = [r["slug"] for r in rows]
        conn.execute(
            "UPDATE agents SET department_id = '', department_level_id = '' "
            "WHERE department_id = %s",
            (department_id,),
        )
        conn.execute("DELETE FROM departments WHERE id = %s", (department_id,))
        conn.commit()
    if members:
        from storage import agent_store
        agent_store._invalidate_cache()
    return members
