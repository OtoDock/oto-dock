"""A Shared-only agent holds no viewer or contributor assignment.

Every session of a Shared-only agent runs as the agent, which takes the
editor tier (``core/sandbox/session_config_dir.refuse_agent_state_below_editor``),
so a lower row is an assignment that opens no chat. This module judges the
rule for assignments: the routes that write a row refuse a new or changed
one below the tier there (``refused_assignments``), and switching an agent
to Shared only removes the rows a person confirmed (``remove_members``).

The rule keeps the panels and the rows consistent; the boundary stays the
session start, which refuses below the tier whatever the rows say. So a
row that exists anyway (an install upgraded from 1.7.0, a save that raced
the switch) is inert: the panels mark it, the full-set save keeps it while
unchanged, and an admin raises or removes it.

The agent's mode is read uncached here: the agent cache can lag an agent
write by a beat, and these reads decide what a route writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping

from auth import roles
from core.session import visibility as _vis
from storage import database as task_store
from storage.agents import agent_store
from storage.pg import run_db

logger = logging.getLogger("claude-proxy")


def is_shared_only_row(agent: Mapping) -> bool:
    """The mode of an ``agents`` row as read (no cache)."""
    return _vis.mode_for(bool(agent.get("collaborative", True)),
                         agent.get("default_scope") or _vis.SCOPE_USER) == _vis.MODE_SHARED_ONLY


def agent_row(slug: str) -> dict | None:
    """The agent's row read now, not from the cache."""
    return next((a for a in agent_store.get_all_agents() if a["slug"] == slug), None)


def shared_only_slugs(slugs: Iterable[str] | None = None) -> set[str]:
    """The Shared-only agents among ``slugs`` (every agent when None)."""
    wanted = None if slugs is None else set(slugs)
    return {a["slug"] for a in agent_store.get_all_agents()
            if (wanted is None or a["slug"] in wanted) and is_shared_only_row(a)}


def refused_assignments(requested: Mapping[str, str], stored: Mapping[str, str]) -> list[str]:
    """The agents of ``requested`` (``{agent: role}`` as it will be written)
    whose row would be new or changed below the editor tier on a Shared-only
    agent. An unchanged row passes: an older install may hold one."""
    low = {a for a, r in requested.items()
           if not roles.allowed_on_shared_only(r) and stored.get(a) != r}
    if not low:
        return []
    return sorted(low & shared_only_slugs(low))


def refusal_message(agents: Iterable[str]) -> str:
    return (f"{', '.join(agents)}: a Shared-only agent takes the editor or manager "
            "role. Its chats run as the agent itself, which takes the editor role "
            "or above.")


def below_editor_members(agent: str) -> list[dict]:
    """The people holding a viewer or contributor row on ``agent``, in the
    shape ``GET /v1/agents/{name}/users`` answers with. A platform admin's
    row is left out: an admin acts as admin whatever the row says."""
    return [
        {"sub": r["sub"],
         "name": r["display_name"] or r["name"] or r["username"] or r["email"] or r["sub"],
         "email": r["email"], "role": r["agent_role"], "platform_role": r["platform_role"]}
        for r in task_store.get_agent_users_with_profile(agent)
        if not roles.allowed_on_shared_only(r["agent_role"])
        and not roles.is_admin(r["platform_role"])
    ]


def legacy_rows_by_agent() -> dict[str, int]:
    """How many viewer or contributor rows each Shared-only agent holds."""
    counts: dict[str, int] = {}
    for slug in sorted(shared_only_slugs()):
        n = len(below_editor_members(slug))
        if n:
            counts[slug] = n
    return counts


def log_legacy_rows() -> None:
    """One WARNING per Shared-only agent that holds rows below the editor
    tier (an install upgraded from 1.7.0): they open no chat there."""
    for slug, n in legacy_rows_by_agent().items():
        logger.warning(
            "Shared-only agent %s holds %d viewer or contributor assignment(s), which "
            "open no chat there: an admin raises them to editor or removes them "
            "(Admin, Users)", slug, n)


def _remove_row(sub: str, agent: str, actor_sub: str):
    """Remove ``sub``'s row on ``agent`` if it is still below the editor
    tier: the person's rows read and written back without it, in this one
    worker job. The store replaces a person's whole set, so an edit of the
    same person landing between the read and the write here is lost with
    its cascade; the window is this job. None when there is nothing to
    remove (the row is gone or was raised meanwhile)."""
    current = task_store.get_user_agent_roles(sub)
    role = current.get(agent)
    if role is None or roles.allowed_on_shared_only(role):
        return None
    person = task_store.get_user(sub) or {}
    if roles.is_admin(person.get("role")):
        return None
    keep = {a: r for a, r in current.items() if a != agent}
    change = task_store.set_user_agents(sub, list(keep), actor_sub, agent_roles=keep)
    return change, person


async def remove_members(agent: str, subs: Iterable[str], actor_sub: str) -> tuple[list[str], list[str]]:
    """Remove the confirmed people's rows on a Shared-only ``agent`` and run
    the offboarding chain for each (concurrently: each dispatch waits up to
    its route budget). Returns ``(removed, failed)``."""
    from services.agents import offboarding
    removed: list[str] = []
    failed: list[str] = []
    dispatches = []
    for sub in subs:
        try:
            done = await run_db(_remove_row, sub, agent, actor_sub)
        except Exception:
            logger.exception("Shared-only switch of %s: the row of %s was not removed", agent, sub)
            failed.append(sub)
            continue
        if done is None:
            continue
        change, person = done
        removed.append(sub)
        dispatches.append(offboarding.dispatch_losses(
            sub, offboarding.losses(change.platform_role, change.before,
                                    change.platform_role, change.after),
            actor_sub, person=person, platform_before=change.platform_role,
            platform_after=change.platform_role))
    if dispatches:
        await asyncio.gather(*dispatches)
    from services.community import template_app_seeder
    for sub in removed:
        try:
            await template_app_seeder.on_user_removed(agent, sub)
        except Exception:
            logger.exception("template apps of (%s, %s) not parked on removal", agent, sub)
    if removed:
        from services.notifications.notification_manager import invalidate_audience
        invalidate_audience(agent)
        logger.info("Shared-only switch of %s removed %d assignment(s) below the editor tier",
                    agent, len(removed))
    return removed, failed
