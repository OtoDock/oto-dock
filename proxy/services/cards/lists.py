"""The list cards (services/cards/cards.py draws them): the kinds of live
list a card may show, each the settings page's own list, the filters the
agent may ask for, the title the card wears and the lowest role that sees
the kind. The card carries no row of the list: the dashboard fetches it
with the viewer's own sign-in through the page's route, which filters by
the viewer's role, and the row actions are the page's own calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from auth import roles
from services.cards import status as st

# The scope a list may be narrowed to: the person's own rows, the agent's,
# or both (the page's own filter).
SCOPE_MINE = "mine"
SCOPE_AGENT = "agent"
SCOPE_ALL = "all"
SCOPES = (SCOPE_MINE, SCOPE_AGENT, SCOPE_ALL)
# A row's state the list may be narrowed to.
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUSES = (STATUS_ACTIVE, STATUS_PAUSED)

MANAGERS_SEE_MCPS = "Only managers and admins see the agent's MCP servers."


@dataclass(frozen=True)
class Kind:
    name: str
    # The words of the title: for everything, and for the person's own rows.
    title: str
    title_mine: str
    # The filter keys the agent may set, each to one of its values.
    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Managers and admins alone for the agent's MCP servers (the page's rule).
    managers_only: bool = False


KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind(st.LIST_TASKS, "Scheduled tasks", "Your scheduled tasks",
         {"scope": SCOPES, "status": STATUSES}),
    Kind(st.LIST_TRIGGERS, "Triggers", "Your triggers", {"scope": SCOPES, "status": STATUSES}),
    Kind(st.LIST_NOTIFICATIONS, "Notifications", "Your notifications",
         {"scope": SCOPES, "status": STATUSES}),
    Kind(st.LIST_MACHINES, "Remote machines", "Your remote machines"),
    Kind(st.LIST_MCPS, "MCP servers", "MCP servers", managers_only=True),
)}


def validate(kind_name: str, filters: Any) -> tuple[Kind, dict[str, str], list[str]]:
    """The kind and its filters as the card stores them, with the problems
    as sentences (an unknown kind, a filter the kind lacks, a value it
    does not take)."""
    kind = KINDS.get(str(kind_name or ""))
    if kind is None:
        return KINDS[st.LIST_TASKS], {}, [st.NO_SUCH_LIST]
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        return kind, {}, ["The filters must be an object."]
    clean: dict[str, str] = {}
    problems: list[str] = []
    for key, value in filters.items():
        allowed = kind.filters.get(str(key))
        if allowed is None:
            problems.append(f"The {kind.name} list has no filter named {key}.")
        elif str(value) not in allowed:
            problems.append(f"The filter {key} takes one of {', '.join(allowed)}.")
        else:
            clean[str(key)] = str(value)
    return kind, clean, problems


def title_of(kind: Kind, filters: dict[str, str]) -> str:
    return kind.title_mine if filters.get("scope") == SCOPE_MINE else kind.title


def check(kind: Kind, agent: str, u) -> str | None:
    """Why the person could never see the list, or None: the agent's MCP
    servers are a manager's page."""
    if kind.managers_only and not (u.is_admin or roles.can_manage(u.effective_role(agent))):
        return MANAGERS_SEE_MCPS
    return None
