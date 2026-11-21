"""The first boot of 2.0 on an install that already had outside callers
(``startup.py``, once, a ``platform_settings`` flag): the callers already on
disk get their row in ``external_identities``, and the people who decide
what a caller gets from now on are told, because from this boot a caller
gets only what a manager picked and nothing is picked yet. A fresh install
sets the flag with nothing to say.
"""

from __future__ import annotations

import asyncio
import logging

from auth import roles
from services.mcp import outside_picks
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import external_identity_store
from storage.phone import phone_route_store

logger = logging.getLogger("otodock.phone")

FLAG = "outside_people_first_boot"
NOTICE_TITLE = "Callers get only what a manager picks"
# The phone modes whose caller is an outside person (a user route's caller
# is that user).
_OUTSIDE_MODES = ("caller", "shared")


def backfill_from_disk(homes) -> int:
    """One row per durable tree that has none (``(agent, channel, slug,
    home)`` from the caller-data iterator): the call log gives the id and
    the dates where it has the caller, the folder's time otherwise. Returns
    the rows made."""
    from datetime import datetime, timezone

    from core.session import external_identity
    seen: dict[tuple[str, str], tuple[str, str, str, int]] = {}
    for agent, ident, first, last, n in external_identity_store.callers_in_call_log():
        key = (agent, external_identity.slug_for(ident))
        prev = seen.get(key)
        seen[key] = ((ident, first, last, n) if prev is None
                     else (ident, min(prev[1], first), max(prev[2], last), prev[3] + n))
    made = 0
    for agent, channel, slug, home in homes:
        ident, first, last, count = seen.get((agent, slug), ("", "", "", 0))
        if not ident:
            try:
                stamp = datetime.fromtimestamp(home.stat().st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                stamp = datetime.now(timezone.utc).isoformat()
            ident, first, last = slug, stamp, stamp
        if external_identity_store.add_if_absent(agent, channel, ident, slug, first_seen=first,
                                                 last_seen=last, conversations=count):
            made += 1
    return made


def _backfill() -> int:
    from services.infra import external_retention
    return backfill_from_disk(list(external_retention.iter_caller_homes()))


def _agents_with_outside_lines() -> list[str]:
    """The agents whose enabled phone routes take outside callers."""
    return sorted({
        r["agent"] for r in phone_route_store.get_all_routes()
        if r.get("enabled", True) and (r.get("identity_mode") or "caller") in _OUTSIDE_MODES
    })


def _label(agent: str) -> str:
    return (agent_store.get_agent(agent) or {}).get("display_name") or agent


async def _tell(agents: list[str]) -> None:
    from services.notifications import notification_manager
    names = ", ".join(_label(a) for a in agents)
    await notification_manager.fire_notification(
        title=NOTICE_TITLE,
        body=(f"After this update a caller on an agent's phone line gets only the tools, "
              f"folders and knowledge a manager picked, and nothing is picked yet. "
              f"Agents with a phone line for outside callers: {names}. Each agent's "
              "managers were told where to pick."),
        severity="warning", scope="admin", source="platform", source_id="outside-people/first-boot",
    )
    for agent in agents:
        await notification_manager.fire_notification(
            title=NOTICE_TITLE,
            body=(f"Callers on the phone line of {_label(agent)} now get only what a manager "
                  "picks. Nothing is picked yet: a caller keeps the call tools and their own "
                  f"notes, read only, and no other tool. Pick what callers may use in "
                  f"{outside_picks.CARD_PLACE}."),
            severity="warning", scope="agent", target=agent, agent_slug=agent,
            source="platform", source_id=f"outside-people/first-boot/{agent}",
            audience=roles.MANAGER, href=outside_picks.card_href(agent),
        )


async def first_boot() -> str | None:
    """Once per install: the callers' rows and the notices. Returns what
    it did, None when nothing was there to do or it already ran."""
    if await asyncio.to_thread(task_store.get_platform_setting, FLAG):
        return None
    made = await asyncio.to_thread(_backfill)
    agents = await asyncio.to_thread(_agents_with_outside_lines)
    if agents:
        await _tell(agents)
    await asyncio.to_thread(task_store.set_platform_setting, FLAG, "1")
    if not made and not agents:
        return None
    return f"{made} caller rows recorded, {len(agents)} agents with an outside line told"


#: Once per install: the calls made before a conversation named its person.
OLD_CALLS_FLAG = "old_calls_named"


def _session_owners() -> dict[tuple[str, str], tuple[str, str]]:
    """``(agent, session or thread id) -> (channel, slug)``: whose tree holds
    each engine's files."""
    from core.session.session_manager import get_all_layers
    from services.infra import external_retention
    layers = list(get_all_layers().values())
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    for agent, channel, slug, home in external_retention.iter_caller_homes():
        for layer in layers:
            for _path, sid in layer.iter_session_files(home):
                owners[(agent, sid)] = (channel, slug)
    return owners


def _name_old_calls() -> int:
    """Every call from before the record named its person gets them, so the
    people view shows it and forgetting them removes it. The call log's label
    names them, else the tree that holds the call's session. A caller the log
    names whose tree is gone gets their row. A call neither names (a route-less
    or a personal call, a log row and a tree both aged out) stays nobody's.
    Returns the calls named."""
    from core.session import external_identity
    from core.session.visibility import PHONE_CHAT_OWNER
    from storage.chat import conversation_store
    calls = conversation_store.calls_with_no_person(PHONE_CHAT_OWNER)
    if not calls:
        return 0
    owners = _session_owners()
    named = 0
    for call in calls:
        agent, stamp = call["agent"], call["created_at"]
        caller = external_identity_store.caller_of_label(call["label"])
        if caller:
            channel, slug = external_identity.PHONE, external_identity.slug_for(caller)
            external_identity_store.add_if_absent(agent, channel, caller, slug, first_seen=stamp,
                                                  last_seen=stamp, conversations=1)
        else:
            found = (owners.get((agent, call["session_id"] or ""))
                     or owners.get((agent, call["codex_thread_id"] or "")))
            if found is None:
                continue
            channel, slug = found
        row = external_identity_store.get(agent, channel, slug)
        if row is not None:
            conversation_store.name_person(call["id"], row["id"])
            named += 1
    return named


async def name_old_calls() -> str | None:
    """Once per install, at a boot, after :func:`first_boot` made the callers'
    rows. Returns what it did, None when nothing was to do or it already ran."""
    if await asyncio.to_thread(task_store.get_platform_setting, OLD_CALLS_FLAG):
        return None
    named = await asyncio.to_thread(_name_old_calls)
    await asyncio.to_thread(task_store.set_platform_setting, OLD_CALLS_FLAG, "1")
    return f"{named} earlier calls named their caller" if named else None


#: Once per install: the conversations tool on the agents with a phone line.
CONVERSATIONS_FLAG = "conversations_tool_added"


def _add_conversations_tool() -> int:
    """The conversations tool on every agent with a phone route that lacks
    it: a manager asks it about the conversations from their own chats. A
    new agent gets it with the core tools."""
    return sum(give_conversations_tool(agent)
               for agent in sorted({r.get("agent") or "" for r in phone_route_store.get_all_routes()} - {""}))


def give_conversations_tool(agent: str) -> bool:
    """The conversations tool on an agent that gets a phone line. True when it
    was added."""
    from services.mcp.mcp_session_filter import CONVERSATIONS_MCP
    from storage.mcp import mcp_store
    if CONVERSATIONS_MCP in mcp_store.get_manager_enabled_mcps(agent):
        return False
    mcp_store.add_agent_mcp(agent, CONVERSATIONS_MCP)
    return True


async def add_conversations_tool() -> str | None:
    """Once per install, at a boot: the conversations tool on the agents
    with a phone line. Returns what it did, None when nothing was to do or
    it already ran."""
    if await asyncio.to_thread(task_store.get_platform_setting, CONVERSATIONS_FLAG):
        return None
    added = await asyncio.to_thread(_add_conversations_tool)
    await asyncio.to_thread(task_store.set_platform_setting, CONVERSATIONS_FLAG, "1")
    return f"the conversations tool added to {added} agents with a phone line" if added else None
