"""The agent and the app a binding reaches.

A binding names an agent by slug. A department set's apps name the other
members by their templates' slugs, and a second install of the set, or a
member the install adopted or replaced, carries another slug. So when the
calling app's agent is a member of a department, and that department
recorded a member under the binding's name (``department_set_members``),
the binding reaches that recorded agent, if it is still a member of the
department and was installed from that template; otherwise nothing. Every
other binding reaches the agent it names. The record is the only input
beside the signed manifest: the set install writes it
(``db_departments.place_members``), and a placement that passed the
placement rule fills an empty place (``db_departments.fill_place``).

The one reader of the agent a binding reaches: the broker, the snapshot,
the event index and the card's words ask here. The caller still runs the
gate (``api/apps/app_bindings.gate``) on the row this answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.apps import manifest as _mf
from storage import database as task_store
from storage.agents import agent_store, db_departments


@dataclass
class Snapshot:
    """The agents and the records read once, for a pass over many apps."""

    agents: dict[str, dict] = field(default_factory=dict)
    records: dict[tuple[str, str], str] = field(default_factory=dict)


def snapshot() -> Snapshot:
    """Blocking: a worker-thread call."""
    return Snapshot(agents={a["slug"]: a for a in agent_store.get_all_agents()},
                    records=db_departments.set_members_all())


def _agent(slug: str, snap: Snapshot | None) -> dict | None:
    if not slug:
        return None
    return snap.agents.get(slug) if snap is not None else agent_store.get_agent(slug)


def _record(department_id: str, template: str, snap: Snapshot | None) -> str | None:
    if snap is not None:
        return snap.records.get((department_id, template))
    return db_departments.set_member(department_id, template)


def follows_department(row: dict, binding: dict, snap: Snapshot | None = None) -> bool:
    """True when the binding is read inside the calling app's department:
    its agent's department recorded a set member under the binding's name."""
    name = str(binding.get("agent") or "")
    department = db_departments.department_of(_agent(str(row.get("agent") or ""), snap))
    return bool(department and name) and _record(department, name, snap) is not None


def agent_of(row: dict, binding: dict, snap: Snapshot | None = None) -> str:
    """The slug of the agent the binding of ``row`` reaches, '' for none."""
    name = str(binding.get("agent") or "")
    department = db_departments.department_of(_agent(str(row.get("agent") or ""), snap))
    if department and name:
        recorded = _record(department, name, snap)
        if recorded is not None:
            return recorded if db_departments.member_from(_agent(recorded, snap), department, name) else ""
    return name


def shared_app_row(agent: str, slug: str) -> dict | None:
    """The shared folder row of ``agent`` named ``slug``, the only kind a
    binding reaches (a personal app or a Dock pin never is)."""
    if not agent or not slug:
        return None
    row = task_store.get_app_by_slug(agent, "", slug)
    if not row or row.get("hidden") or not task_store.app_kind_of(row).may_serve:
        return None
    if row.get("scope_chat_id") or row.get("scope_project_id"):
        return None
    return row


def target_of(row: dict, binding: dict) -> dict | None:
    """The row the binding of ``row`` reaches, or None."""
    return shared_app_row(agent_of(row, binding), str(binding.get("app") or ""))


def following(row: dict) -> list[str]:
    """The names of the row's bindings that are read inside its department
    (the card says so)."""
    bindings = _mf.parse_bindings(row)
    if not bindings:
        return []
    return [str(b.get("name") or "") for b in bindings if follows_department(row, b)]
