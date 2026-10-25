"""The role guards the api modules share beyond ``auth.providers``'s own
(``require_auth``, ``require_admin``, ``require_human``, ``require_creator``).

One copy of each check; the sentence a route answers with stays at its
call site (``detail``). Both answer 401 when there is no principal (fail
closed, as ``require_auth`` does) and 403 when the principal lacks the role.
"""

from __future__ import annotations

from fastapi import HTTPException

from auth import roles
from auth.providers import UserContext, require_auth


def require_manager(user: UserContext | None, agent: str, *, detail: str) -> UserContext:
    """The caller manages ``agent``: a platform admin or the agent's own
    manager (``UserContext.can_manage_agent``)."""
    u = require_auth(user)
    if not u.can_manage_agent(agent):
        raise HTTPException(status_code=403, detail=detail)
    return u


def require_creator_or_admin(user: UserContext | None, *, detail: str) -> UserContext:
    """The caller's platform role is creator or above. A session token
    resolves to its person's role: the routes that must refuse
    every bearer use ``require_creator``."""
    u = require_auth(user)
    if not roles.is_creator_or_above(u.role):
        raise HTTPException(status_code=403, detail=detail)
    return u
