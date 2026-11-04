"""Who may post to the Notebook: the one floor the posting route and the
prompt's posting paragraph both read, so a session is never told to post
when the route would refuse it.

A session posts when its recorded role is editor or above (a person's role
on the agent, a user-scope task's creator's, a manager's for a run of the
agent itself) and the agent's mode offers the agent scope: an agent that
keeps each person's chats private has nothing its sessions may publish to
the agents around it. A check's judge only reads, an app's session and an
external caller never post. The route adds what only it can know: the
session is registered, belongs to the agent, and a person driving it still
holds the role now.
"""

from __future__ import annotations

from auth import roles
from core.session import session_kind

PRIVATE_MODE = (
    "This agent keeps each person's chats private, so it cannot post to the "
    "Notebook."
)
BELOW_EDITOR = (
    "Only an editor or a manager of this agent can post to its Notebook. "
    "This session runs with a lower role."
)
JUDGE = "A check's judge only reads, so it cannot post to the Notebook."
APP_SESSION = "An app's session cannot post to the Notebook."
EXTERNAL = "This session cannot post to the Notebook."
UNREADABLE = "The Notebook cannot check this agent's settings now. Try again in a moment."


def _offers_agent_scope(agent: str) -> bool:
    """Whether the agent's mode includes the shared scope, read from the
    agent row (never from a session's own copy of the scopes)."""
    from core.session.visibility import resolve_visibility
    return resolve_visibility(agent).mount_shared


def posting_refusal(agent: str, *, role: str, read_only: bool = False,
                    client_type: str = "", external: bool = False) -> str | None:
    """The sentence that refuses this session's post, or None when it may
    post. Fails closed when the agent's mode cannot be read."""
    if external:
        return EXTERNAL
    if read_only:
        return JUDGE
    if session_kind.of(client_type) is session_kind.APP:
        return APP_SESSION
    try:
        shared = _offers_agent_scope(agent)
    except Exception:  # noqa: BLE001 — an unreadable agent row never lets a post through
        return UNREADABLE
    if not shared:
        return PRIVATE_MODE
    if not roles.can_edit(role):
        return BELOW_EDITOR
    return None
