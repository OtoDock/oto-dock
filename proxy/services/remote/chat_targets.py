"""Where a chat may run: the places a person is offered for it.

One list answers the chat's "Runs on" chip, the move's validation, the
agent's move tool and the machine a task or a delegation may name. A place
is offered when the person whose session would run there (the RUN person:
the chat's owner on a normal agent, each sender on a Shared-only agent, the
task's identity on a task-run chat) may use it (``placement.may_run_on``)
and the asker may pick it: the run person's default
(``resolve_execution_target``, offline included), their own paired machines
online and offline (none while personal computers are turned off on the
platform), and the admin machines for a platform admin asker when the run
person may run there. A machine is pickable when it is reachable,
runs the chat's engine and passes the guard; every other one carries the
sentence that says why not. Nothing here changes the defaults model.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from auth import roles
from core import placement

logger = logging.getLogger("otodock.remote")

#: How the platform's own place reads to a person: its name in a menu, a
#: tooltip or a list (``place_phrase`` gives it inside a sentence).
LOCAL_LABEL = "OtoDock platform"
#: The name the platform's place had before 1.9.0: a request naming it still
#: finds the platform.
_FORMER_LOCAL_LABEL = "OtoDock server"
#: What follows a machine's name when it reads as the platform's own.
_LOOKALIKE_SUFFIX = " (computer)"


def _names_the_platform(name: str) -> bool:
    low = (name or "").strip().lower()
    return low in (LOCAL_LABEL.lower(), _FORMER_LOCAL_LABEL.lower())


def place_phrase(target: str | None, label: str = "", *, start: bool = False) -> str:
    """A place as it reads inside a sentence: "the OtoDock platform" for the
    platform ("The …" opening a sentence), a machine's name as it is (a
    name takes no article and keeps its case). Decided by the target, never
    by the label's text, so a machine named like the platform stays a
    name."""
    if placement.is_local(target):
        return f"{'The' if start else 'the'} {LOCAL_LABEL}"
    return label or label_of(target)


@dataclass(frozen=True)
class RunPerson:
    """Whose session would run in a place, and at which role."""

    sub: str | None
    role: str


@dataclass
class Target:
    target: str
    label: str
    pairing: str          # "" (the server) or a placement.PAIRING_* word
    state: str            # remote_status's live state ("" for the server)
    reachable: bool
    is_default: bool
    pickable: bool
    why_not: str
    owner_sub: str = ""
    allowed: bool = True   # the guard passed (a pick may still wait for the machine)
    moves_files: bool = True   # its satellite moves a chat's session files (moves_files)

    def view(self) -> dict:
        d = asdict(self)
        d.pop("owner_sub", None)
        return d


def moves_files(place: str) -> bool:
    """A move from or to ``place`` can carry the chat's session files: the
    platform always, a machine whose connected satellite moves them
    (``session_move``). A move with a place that cannot continues from a
    summary of the conversation."""
    if placement.is_local(place):
        return True
    from core.remote.satellite_connection import get_connection_manager
    return get_connection_manager().satellite_supports_session_move(place)


def label_of(target: str | None, machine: dict | None = None) -> str:
    """A place's name as a person reads it; a machine whose name reads as the
    platform's says it is a computer."""
    if placement.is_local(target):
        return LOCAL_LABEL
    if machine is None:
        from storage import remote_store
        machine = remote_store.get_remote_machine(placement.machine_of(target)) or {}
    name = str(machine.get("name") or "") or placement.machine_of(target)[:8]
    return name + _LOOKALIKE_SUFFIX if _names_the_platform(name) else name


def owner_name(sub: str) -> str:
    """A person's display name (the lock's sentence)."""
    from storage import database as task_store
    u = task_store.get_user(sub) or {}
    return str(u.get("display_name") or u.get("username") or "someone")


def user_machines_allowed() -> bool:
    from storage import database as task_store
    return task_store.get_platform_setting("allow_user_paired_machines") != "0"


def run_person_of_chat(chat: dict, asker_sub: str, asker_role: str) -> RunPerson:
    """The run person of ``chat`` when ``asker_sub`` (at ``asker_role`` on the
    agent) drives it. Synchronous: call it on the DB executor."""
    from core.session import session_kind, visibility
    cid = chat.get("id") or ""
    agent = chat.get("agent") or ""
    if session_kind.is_task_chat_id(cid):
        from core.config.task_config_builder import rewarm_task_identity
        ident = rewarm_task_identity(cid, agent)
        if ident is not None:
            return RunPerson(ident.creds_user_sub, ident.role)
        return RunPerson(None, roles.MANAGER)
    owner = chat.get("user_sub") or ""
    if visibility.is_shared_chat_owner(owner) or owner == asker_sub:
        return RunPerson(asker_sub, asker_role)
    from auth.providers import acting_role_of
    return RunPerson(owner, acting_role_of(owner, agent))


def _engines_ok(machine_id: str, engine_path: str) -> bool:
    from core.remote.satellite_connection import get_connection_manager
    engines = get_connection_manager().satellite_engines(machine_id)
    return not engines or engine_path in engines


def offered(agent: str, person: RunPerson, *, engine_path: str,
            admin_asker: bool) -> tuple[str, list[Target]]:
    """``(default_target, targets)`` for ``person`` on ``agent``: the server,
    the run person's default, their own machines and, for a platform admin
    asker, the admin machines the run person may use. Synchronous."""
    from core.session.session_manager import get_layer_capabilities
    from services.remote import remote_status
    from storage import remote_store

    resolved, _reason = remote_status.resolve_execution_target(agent, person.sub, person.role)
    default = placement.machine_of(resolved) or placement.LOCAL
    out = [Target(placement.LOCAL, LOCAL_LABEL, "", "", True, placement.is_local(default),
                  True, "")]
    caps = get_layer_capabilities(engine_path) if engine_path else None
    if caps is not None and not caps.runtime.supports_remote_execution:
        return placement.LOCAL, out

    allowed_user = user_machines_allowed()
    can_manage = roles.can_manage(person.role)
    rows: dict[str, dict] = {}
    if person.sub:
        for m in remote_store.get_visible_machines_for_user(person.sub):
            rows[m["id"]] = m
    if admin_asker and (not person.sub or can_manage):
        for m in remote_store.get_all_remote_machines():
            if placement.machine_is_admin_paired(m):
                rows.setdefault(m["id"], m)
    if not placement.is_local(default) and default not in rows:
        default_row = remote_store.get_remote_machine(default)
        if default_row:
            rows[default] = default_row
    for mid, m in sorted(rows.items(), key=lambda kv: (kv[1].get("pairing_scope") != placement.PAIRING_USER,
                                                       str(kv[1].get("name") or ""))):
        if not allowed_user and not placement.machine_is_admin_paired(m):
            continue  # personal computers are turned off on this platform
        live = remote_status.get_live_machine_status(mid, machine=m)
        guard = placement.may_run_on(m, run_sub=person.sub, run_can_manage=can_manage,
                                     user_paired_allowed=allowed_user)
        name = label_of(mid, m)
        why = ""
        if not guard:
            why = f"You can't run this chat on {name}."
        elif not live["reachable"]:
            why = f"{name} is offline."
        elif not _engines_ok(mid, engine_path):
            why = f"{name} can't run this chat's engine."
        pairing = (placement.PAIRING_ADMIN if placement.machine_is_admin_paired(m)
                   else placement.PAIRING_USER)
        out.append(Target(mid, name, pairing, live["state"], bool(live["reachable"]),
                          mid == default, not why, why,
                          owner_sub=str(m.get("registered_by") or ""), allowed=guard,
                          moves_files=moves_files(mid)))
    return default, out


def chat_engine(chat: dict) -> str:
    """The engine a chat runs (its row's, else its agent's)."""
    from core.session.session_manager import resolve_execution_path
    return resolve_execution_path(chat.get("agent") or "", chat.get("execution_path") or "")


def current_target(chat: dict) -> str:
    """Where a chat runs now: its pin, or local for an unpinned row."""
    return placement.machine_of(chat.get("execution_target")) or placement.LOCAL


def find(targets: list[Target], wanted: str) -> Target | None:
    """The offered entry a request names: a target id, ``default``, the
    platform by its name (its former name too), or a machine name
    (case-insensitive, when only one machine has it)."""
    wanted = (wanted or "").strip()
    if not wanted:
        return None
    if wanted == "default":
        return next((t for t in targets if t.is_default), None)
    for t in targets:
        if t.target == wanted:
            return t
    if _names_the_platform(wanted):
        return next((t for t in targets if placement.is_local(t.target)), None)
    low = wanted.lower()
    named = [t for t in targets if t.label.lower() == low]
    return named[0] if len(named) == 1 else None


def agent_view(agent: str, sub: str | None, role: str, *, admin_asker: bool,
               engine_path: str = "") -> dict:
    """Where NEW work of ``agent`` may run for this person (the new chat's
    chip, the task dialog, a task's or a delegation's machine). Synchronous."""
    from core.session.session_manager import resolve_execution_path
    engine = engine_path or resolve_execution_path(agent, "")
    default, targets = offered(agent, RunPerson(sub, role), engine_path=engine,
                               admin_asker=admin_asker)
    return {"default_target": default, "targets": [t.view() for t in targets]}


def resolve_work_machine(agent: str, wanted: str, *, person: RunPerson, admin_asker: bool,
                         engine_path: str = "") -> tuple[str, str]:
    """The machine a task or a delegation names for its runs: ``(machine id,
    "")`` with ``""`` for the agent's default, or ``("", refusal)``. The run
    person is the task's (the creator for a user-scope task; no person for
    an agent-scope one, which never runs on a personal machine). A machine
    that is offline now is accepted (the run falls back to the default and
    says so); one the guard refuses, or that does not run the engine, is
    not. The refusal lists the names that would do. Synchronous."""
    wanted = (wanted or "").strip()
    if not wanted or wanted == "default":
        return "", ""
    from core.session.session_manager import resolve_execution_path
    engine = engine_path or resolve_execution_path(agent, "")
    _default, targets = offered(agent, person, engine_path=engine, admin_asker=admin_asker)
    hit = find(targets, wanted)
    usable = [t for t in targets if t.allowed and not placement.is_local(t.target)
              and (t.pickable or not t.reachable)]
    if hit is not None and placement.is_local(hit.target):
        return placement.LOCAL, ""
    if hit is None or hit not in usable:
        choices = ", ".join([LOCAL_LABEL, *(t.label for t in usable)])
        return "", (f"{wanted} is not a machine this work can run on. "
                    f"The choices are: {choices}, or default.")
    return hit.target, ""
