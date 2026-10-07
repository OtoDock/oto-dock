"""Who a server-originated prompt wakes a chat as: the person a delegate
result runs as, their role, the standing gate, and the visibility of the
session it respawns. One decision (``resolve_delivery_person``), shared by
the delivery ladder (``delivery.py``) and the result-files attach
(``services/delegation/result_files.py``), whose destination tree follows
the same person: the two can never disagree. The continuation fire
(``firing.py``) reuses the pieces (``_shared_chat_person``,
``_standing_of``, ``_wake_refused``) with its own person rule.

One piece of the task scheduler. Every ``core.*`` import that is not a leaf
stays function-local so the standalone scheduler can import this package
without the platform.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from storage import database as task_store
from core.session.visibility import is_synthetic_owner
from auth import roles

if TYPE_CHECKING:
    from core.session.visibility import VisibilityResolution


def _standing_of(sub: str, agent: str) -> tuple[str, str]:
    """A person's current standing on the agent (``roles.NO_ACCESS`` when they
    hold none; ADMIN for a platform admin) and their agent row (the role a
    delegate delivery runs at). Synchronous: call it on the DB executor."""
    from auth.providers import effective_role_of
    return (effective_role_of(sub, agent),
            roles.row_role(task_store.get_user_agent_roles(sub), agent))


def _wake_refused(standing: str, *, shared: bool) -> bool:
    """A person gets no wake when they no longer hold the agent, and on a
    Shared-only chat (it runs from the agent's own state) when they are below
    the editor tier: offboarding treats that as gone from the agent too."""
    return standing == roles.NO_ACCESS or (shared and not roles.can_edit(standing))


def _shared_chat_person(chat: dict | None, created_by: str | None) -> str:
    """The person a delivery into a Shared-only chat runs as: the one its task
    row recorded (a continuation's creator, the person whose session delegated
    the work), when the chat is the agent's shared history and the agent is
    Shared-only now. "" when no person is on record (a session without one
    records the agent's own slug) or the chat is of another kind (a chat
    left from before a mode change keeps the agent's identity: its
    conversation lives where it was written). Synchronous: call it on the DB
    executor."""
    from core.session.visibility import is_shared_only, shared_chat_owner
    agent = (chat or {}).get("agent") or ""
    if (not agent or not created_by or created_by == agent
            or (chat or {}).get("user_sub") != shared_chat_owner(agent)
            or not is_shared_only(agent)):
        return ""
    return created_by


def _wake_visibility(agent: str, username: str, role: str,
                     user_sub: str | None) -> "VisibilityResolution":
    """The visibility of a respawned session, from the same inputs as its
    security context (``build_delivery_security_context``): the person's own
    tree for a personal chat, the agent scope with the person present on a
    Shared-only agent, the agent scope with nobody otherwise. Synchronous:
    call it on the DB executor."""
    from core.session.visibility import SCOPE_AGENT, SCOPE_USER, resolve_visibility
    return resolve_visibility(
        agent, username=username, user_role=role or "", user_sub=user_sub or "",
        scope_override=SCOPE_USER if user_sub else SCOPE_AGENT,
    )


@dataclass(frozen=True)
class DeliveryPerson:
    """Who a delegate result wakes the delegating chat as."""

    person: str          # "" when the delivery runs as nobody
    role: str            # the role the wake runs at
    shared: bool         # the delegating person of a Shared-only shared chat
    refused: bool        # the standing gate refused the person: nothing is warmed
    chat: dict | None    # the chat the delivery targets (by id, else by session)
    standing: str = ""   # the person's effective role on the agent (NO_ACCESS when none)


async def resolve_delivery_person(chat_id: str | None, session_id: str,
                                  created_by: str | None, scope: str,
                                  agent: str) -> DeliveryPerson:
    """The chat decides who wakes, not the worker's scope: a Shared-only
    agent's shared chat wakes as the person whose session delegated the work
    (the row's ``created_by``) at their effective role; a chat a person owns
    wakes as its owner at their row role (viewer without one); any other
    chat (a task or phone chat, none at all) keeps the worker's scope rule:
    its creator for a user-scope worker, else nobody at the manager role.
    Then the standing gate: a person who no longer holds the agent, or sits
    below the editor tier on a Shared-only chat, is ``refused``."""
    from storage.pg import run_db
    target_chat = (await run_db(task_store.get_chat, chat_id) if chat_id
                   else await run_db(task_store.get_chat_by_session, session_id))
    owner = (target_chat or {}).get("user_sub") or ""
    shared_person = await run_db(_shared_chat_person, target_chat, created_by)
    if shared_person:
        person = shared_person
    elif owner and not is_synthetic_owner(owner):
        person = owner
    else:
        person = (created_by or "") if scope == "user" else ""
    if not person:
        return DeliveryPerson("", roles.MANAGER, False, False, target_chat)
    standing, row = await run_db(_standing_of, person, agent)
    shared = bool(shared_person)
    if _wake_refused(standing, shared=shared):
        return DeliveryPerson(person, "", shared, True, target_chat, standing)
    # On a Shared-only chat the wake runs from the agent's own state, at the
    # role the person's own turn there runs at (their effective role).
    # Elsewhere the creator's row alone (a viewer without one): the echo rung
    # of an admin's task resolves its target as a viewer would, while the
    # run's own session read the admin; aligning the two is left to the
    # operator.
    role = standing if shared else (row or roles.VIEWER)
    return DeliveryPerson(person, role, shared, False, target_chat, standing)
