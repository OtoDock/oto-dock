"""What every public route shares: the key's principal, the person it acts
as, the agents it reaches, and the per-key cap on open waits.

A public route takes ``key_user`` (the middleware verified the key and left
its principal; ``auth/api_keys.py``) and hands an existing handler
``person_of(u)``: the same person with ``is_api_key`` off, so the handler
takes its person branch, while ``api_key_id`` stays set and every "person
at the keyboard" gate still refuses it (``PrincipalKind.API_KEY``).
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator, Callable

from fastapi import Depends, HTTPException

from auth import roles
from auth.providers import UserContext, capped_principal, get_current_user

#: Open waits and run streams one key may hold at once.
OPEN_WAITS_PER_KEY = 8
_open: dict[str, int] = {}


async def key_user(user: UserContext | None = Depends(get_current_user)) -> UserContext:
    if user is None or not user.api_key_id:
        raise HTTPException(401, "This route takes a personal API key.")
    return user


def person_of(u: UserContext) -> UserContext:
    """The key's person for an existing handler (module docstring)."""
    return dataclasses.replace(u, is_api_key=False)


def reaches(u: UserContext, agent: str) -> bool:
    """The key reaches ``agent``: its person holds it and the key names it
    (a key that names none reaches every agent its person holds)."""
    return bool(agent) and u.can_access_agent(agent) and (
        not u.api_key_agents or agent in u.api_key_agents)


def require_agent(u: UserContext, agent: str) -> None:
    if not reaches(u, agent):
        raise HTTPException(403, f"This key does not reach the agent {agent!r}.")


def files_person(u: UserContext, agent: str) -> UserContext:
    """The person for the file routes: at most the editor tier on ``agent``
    and never a platform admin, so a key reads and writes the person's own
    folder, the workspace (from the contributor tier) and reads the
    knowledge, and never ``config/`` or another person's folder."""
    return dataclasses.replace(capped_principal(u, agent, roles.EDITOR), is_api_key=False)


def take_wait(u: UserContext) -> Callable[[], None]:
    """One of the key's open waits (429 past ``OPEN_WAITS_PER_KEY``); the
    answer gives it back."""
    key = u.api_key_id
    if _open.get(key, 0) >= OPEN_WAITS_PER_KEY:
        raise HTTPException(429, "This key has too many requests waiting. Wait for one to end.",
                            headers={"Retry-After": "10"})
    _open[key] = _open.get(key, 0) + 1
    done = False

    def give_back() -> None:
        nonlocal done
        if done:
            return
        done = True
        left = _open.get(key, 1) - 1
        if left > 0:
            _open[key] = left
        else:
            _open.pop(key, None)
    return give_back


@contextlib.asynccontextmanager
async def open_wait(u: UserContext) -> AsyncIterator[None]:
    """``take_wait`` for the length of a block."""
    give_back = take_wait(u)
    try:
        yield
    finally:
        give_back()


def record(u: UserContext, kind: str, ref: str, *, label: str = "", agent: str = "") -> None:
    """Note what the key made (the revoke's list). Synchronous."""
    from storage.identity import api_key_store
    api_key_store.record_key_created(u.api_key_id, kind, ref, label=label, agent=agent)
