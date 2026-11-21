"""The first 2.0 start of an install with shared links: every visitor of an
app's link is an external identity from now on (the role ``external`` in the
claim the app's code reads and at its buttons), and a link to an app that
reads a folder outsiders may not see stops serving. The people who can act
are told once: the admins, each agent's managers for its shared apps, each
owner for a personal app. Every app with a live link is named, since a
declared ``viewer`` floor is stored as no floor and the two cannot be told
apart. An install with no live link is told nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from storage import database as task_store
from storage.agents import agent_store

logger = logging.getLogger("otodock")

FLAG = "shared_links_first_boot"
NOTICE_TITLE = "Visitors of shared links are now external identities"
# Names past this many are counted, not listed.
LIST_MAX = 20


def _named(titles: list[str]) -> str:
    shown = titles[:LIST_MAX]
    more = len(titles) - len(shown)
    return ", ".join(shown) + (f", and {more} more" if more else "")


def _apps_with_links() -> list[dict]:
    """One entry per app with a live link: its agent, title, owner, and the
    folder that now stops its link ("" when none). Synchronous."""
    from services.mcp import outside_picks
    from storage.sharing import share_store
    apps: dict[str, dict] = {}
    for link in share_store.live_app_links():
        if link["app_id"] in apps:
            continue
        row = task_store.get_app(link["app_id"]) or {}
        apps[link["app_id"]] = {
            "agent": link["agent"], "title": link["title"] or link["slug"],
            "agent_label": (agent_store.get_agent(link["agent"]) or {}).get("display_name") or link["agent"],
            "owner_sub": link["owner_sub"] if link["username"] else "",
            "stopped": outside_picks.link_refusal(row) if row else "",
        }
    return list(apps.values())


def _body(intro: str, apps: list[dict], *, with_agent: bool = False) -> str:
    """The notice's words; ``with_agent`` names each app with its agent (the
    admins' list spans agents, where two may hold an app of one title)."""
    name = (lambda a: f"{a['title']} on {a['agent_label']}") if with_agent else (lambda a: a["title"])
    lines = [f"{intro} {_named([name(a) for a in apps])}.",
             "Their visitors now count as external identities.",
             "A button on such a link works only if the app opens it to them."]
    stopped = [name(a) for a in apps if a["stopped"]]
    if stopped:
        lines.append(f"The links of these apps no longer open, because each reads a folder its visitors may not "
                     f"see: {_named(stopped)}. A manager can add a knowledge folder in Agent Settings, "
                     "Configuration, External identities.")
    lines.append("Ask the agent to update the app if something stops working.")
    return " ".join(lines)


async def first_boot() -> str | None:
    """Once, at the first start of 2.0: the notices, then the flag (a notice
    that fails is retried at the next start). Silent on an install with no
    live link."""
    if await asyncio.to_thread(task_store.get_platform_setting, FLAG):
        return None
    apps = await asyncio.to_thread(_apps_with_links)
    if apps:
        await _tell(apps)
    await asyncio.to_thread(task_store.set_platform_setting, FLAG, "1")
    return f"{len(apps)} apps with a shared link told" if apps else None


async def _tell(apps: list[dict]) -> None:
    from auth import roles
    from services.notifications import notification_manager
    await notification_manager.fire_notification(
        title=NOTICE_TITLE, body=_body("These apps have a shared link:", apps, with_agent=True),
        severity="warning", scope="admin", source="platform", source_id="shared-links/first-boot",
    )
    shared: dict[str, list[dict]] = defaultdict(list)
    personal: dict[str, list[dict]] = defaultdict(list)
    for app in apps:
        (personal[app["owner_sub"]] if app["owner_sub"] else shared[app["agent"]]).append(app)
    for agent, items in shared.items():
        await notification_manager.fire_notification(
            title=NOTICE_TITLE, body=_body("This agent's apps with a shared link:", items),
            severity="warning", scope="agent", target=agent, agent_slug=agent,
            source="platform", source_id=f"shared-links/first-boot/{agent}", audience=roles.MANAGER,
        )
    for owner, items in personal.items():
        await notification_manager.fire_notification(
            title=NOTICE_TITLE, body=_body("Your apps with a shared link:", items),
            severity="warning", scope="user", target=owner, agent_slug=items[0]["agent"],
            source="platform", source_id="shared-links/first-boot/personal",
        )
