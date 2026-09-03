"""Department → delegation-edge compiler.

Departments are metadata; delegation reach is real only as
agent_delegation_targets rows. This module owns EVERY row tagged
source='department': ``recompile()`` derives the full desired edge set from
all departments + member assignments and swaps it in atomically
(agent_store.replace_department_edges). Global recompile by design — O(agents
+ edges) at installation scale, zero per-department bookkeeping, and it
self-heals dangling references (a member pointing at a deleted department or
level simply compiles to no edges).

Call ``recompile()`` after ANY edge-affecting write: department create /
update / delete, level replace, agent assignment change — and after a manual
delegation-targets PUT (un-checking a manual edge that a department also
wants must let the compiler re-assert it as source='department').

Semantics (since 2026-09-02 — SYMMETRIC; operator decision):
- same level: mutual edges between all members
- reach='adjacent' (the default): each level ↔ the level DIRECTLY above and
  DIRECTLY below (both directions, NOT transitive)
- reach='subtree': full department mesh — every member ↔ every member
- per-department auto_delegation toggle compiles to nothing when off
Note: an edge is also a read grant for no-user sessions
(core/session/visibility.py nouser_read_targets) — symmetry means a lower
level's autonomous runs can read the level above. Documented in
DEPARTMENTS.md; guarded upstream by the spawn-authz chain/depth checks.

Staleness note: running sessions bake their roster at spawn and the
delegation MCP parses its target list at process start — edges ADDED here
become usable at the next session spawn; REMOVED edges 403 instantly at the
spawn-authz layer. Same behavior manual edges have always had.
"""

import logging

from storage import agent_store, db_departments

logger = logging.getLogger(__name__)


def compute_desired_edges() -> set[tuple[str, str]]:
    """Derive the full (agent → target) edge set every department implies."""
    departments = db_departments.list_departments()
    agents = agent_store.get_all_agents()

    members_by_dept: dict[str, dict[str, list[str]]] = {}
    for a in agents:
        dept_id = a.get("department_id") or ""
        level_id = a.get("department_level_id") or ""
        if dept_id and level_id:
            members_by_dept.setdefault(dept_id, {}).setdefault(
                level_id, []
            ).append(a["slug"])

    desired: set[tuple[str, str]] = set()
    for dept in departments:
        if not dept["auto_delegation"]:
            continue
        levels = dept["levels"]  # already rank-ordered
        dept_members = members_by_dept.get(dept["id"], {})
        tiers = [dept_members.get(lv["id"], []) for lv in levels]
        subtree = dept["reach"] == "subtree"
        for i, tier in enumerate(tiers):
            for slug in tier:
                for peer in tier:
                    if peer != slug:
                        desired.add((slug, peer))
                if subtree:
                    # Full department mesh: every other tier, both directions
                    # (each i adds its outgoing edges; the reverse pairs are
                    # added when the other tier iterates).
                    reachable = tiers[:i] + tiers[i + 1:]
                else:
                    # Adjacent: one level up + one level down, symmetric.
                    reachable = tiers[max(i - 1, 0):i] + tiers[i + 1:i + 2]
                for other_tier in reachable:
                    for target in other_tier:
                        desired.add((slug, target))
    return desired


def recompile() -> int:
    """Swap the compiled edge set; returns the number of desired edges."""
    desired = compute_desired_edges()
    agent_store.replace_department_edges(desired)
    logger.info("department edge compiler: %d edge(s) compiled", len(desired))
    return len(desired)
