"""Offboarding: one event when a person loses standing on an agent.

Three admin actions take standing away: removing a person from an agent
(or lowering their role there), lowering their platform role (an admin is
ADMIN on every agent), and deleting them. Several subsystems must follow:
the person's agent-scope automations change hands (the actor becomes their
owner), their live sessions on the agent close, their personal app servers
stop. Each registers here once with ``subscribe``; the async routes that
change membership call ``dispatch_losses`` after the change has committed
(never the store function itself: it runs in a worker thread).

A subscriber is ``async def fn(event)`` or a plain function (run in a
worker thread). The chain runs in the background, subscribers in
``priority`` order (then by name), each under its own error guard and time
budget: one that fails is logged and the next runs, one that overruns keeps
running while the chain moves on. The route waits for the chain only
briefly, so nothing a subscriber does can block or undo the change.

An event is sent once and a restart can drop it: a subscriber whose work
must happen (moving automations) also reconciles at boot, and re-reads the
current state before acting (the person may have been re-added since).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from auth import roles

logger = logging.getLogger("claude-proxy.offboarding")

REMOVED = "removed"
DEMOTED = "demoted"
DELETED = "deleted"
REASONS = (REMOVED, DEMOTED, DELETED)

# How long the chain waits on one subscriber before moving on (it keeps
# running: a transfer is never cancelled halfway).
SUBSCRIBER_BUDGET_S = 30.0
# How long the route waits for the whole chain before answering.
ROUTE_WAIT_S = 10.0
_LOGGED_AGENTS = 10


@dataclass(frozen=True)
class AgentLoss:
    """One agent's standing before and after. ``old_role``/``new_role`` are
    effective roles (ADMIN for a platform admin, else the row);
    ``old_row``/``new_row`` are the membership rows alone, which some
    surfaces judge by (apps, a user-paired machine, task delivery). An empty
    role is NO_ACCESS."""
    agent: str
    old_role: str
    new_role: str
    old_row: str = roles.NO_ACCESS
    new_row: str = roles.NO_ACCESS

    @property
    def lost_access(self) -> bool:
        return self.new_role == roles.NO_ACCESS

    @property
    def lost_editor(self) -> bool:
        """Can no longer act as the agent (own agent-scope automations,
        shared apps, Shared-only sessions)."""
        return roles.can_edit(self.old_role) and not roles.can_edit(self.new_role)

    @property
    def lost_workspace(self) -> bool:
        """Can no longer write the shared workspace (a live session's
        mount still can)."""
        return roles.can_write_workspace(self.old_role) and not roles.can_write_workspace(self.new_role)

    @property
    def lost_row(self) -> bool:
        """The membership row is gone (a platform admin keeps access)."""
        return bool(self.old_row) and not self.new_row


@dataclass(frozen=True)
class OffboardEvent:
    """``sub`` lost standing on ``agents`` because of ``actor_sub`` (the
    admin who made the change; empty when no person did, e.g. an identity
    provider's group change: fall back to the install owner). The person's
    names are read before the change, so they survive a deletion."""
    sub: str
    reason: str
    agents: tuple[AgentLoss, ...]
    actor_sub: str
    username: str = ""
    email: str = ""
    display_name: str = ""
    platform_before: str | None = None
    platform_after: str | None = None


Subscriber = Callable[[OffboardEvent], Awaitable[None] | None]
_subscribers: dict[str, tuple[int, Subscriber]] = {}
_background: set[asyncio.Task] = set()


def subscribe(name: str, fn: Subscriber, *, priority: int = 100) -> None:
    """Register ``fn`` under ``name`` (a name registered again is replaced);
    lower ``priority`` runs first."""
    _subscribers[name] = (priority, fn)


def unsubscribe(name: str) -> None:
    _subscribers.pop(name, None)


def losses(platform_before: str | None, rows_before: Mapping[str, str],
           platform_after: str | None, rows_after: Mapping[str, str],
           all_agents: Iterable[str] = ()) -> list[AgentLoss]:
    """Every agent where the effective role or the row vanished or ranked
    lower. ``all_agents`` matters when the person was a platform admin (then
    they stood on every agent, rows or not)."""
    agents = set(rows_before) | set(rows_after)
    if roles.is_admin(platform_before):
        agents |= set(all_agents)

    def dropped(old: str, new: str) -> bool:
        # NO_ACCESS and viewer share rank 0, so a vanished role is its own test.
        return bool(old) and (not new or roles.rank(new) < roles.rank(old))

    out = []
    for agent in sorted(agents):
        old = roles.effective_role(platform_before, rows_before, agent)
        new = roles.effective_role(platform_after, rows_after, agent)
        old_row, new_row = rows_before.get(agent, ""), rows_after.get(agent, "")
        if dropped(old, new) or dropped(old_row, new_row):
            out.append(AgentLoss(agent, old, new, old_row, new_row))
    return out


async def _run(name: str, fn: Subscriber, event: OffboardEvent) -> None:
    try:
        if inspect.iscoroutinefunction(fn):
            await fn(event)
        else:
            result = await asyncio.to_thread(fn, event)
            if inspect.isawaitable(result):
                await result
    except Exception:
        logger.exception("offboarding subscriber %s failed for %s (%s)", name, event.sub, event.reason)


def _keep(task: asyncio.Task) -> None:
    _background.add(task)
    task.add_done_callback(_background.discard)


async def on_user_offboarded(event: OffboardEvent) -> None:
    """Run every subscriber for ``event``, in order. Awaiting this waits for
    the chain up to each subscriber's budget; ``dispatch_losses`` runs it in
    the background instead."""
    if event.reason not in REASONS:
        raise ValueError(f"unknown offboarding reason {event.reason!r}")
    shown = ", ".join(f"{a.agent} ({a.old_role or '-'}->{a.new_role or '-'})"
                      for a in event.agents[:_LOGGED_AGENTS])
    more = len(event.agents) - _LOGGED_AGENTS
    logger.info("offboarding: %s %s on %d agent(s) [%s%s] by %s; %d subscriber(s)",
                event.sub, event.reason, len(event.agents), shown or "none",
                f", +{more} more" if more > 0 else "", event.actor_sub or "no person",
                len(_subscribers))
    for name, (_priority, fn) in sorted(_subscribers.items(), key=lambda kv: (kv[1][0], kv[0])):
        task = asyncio.ensure_future(_run(name, fn, event))
        try:
            await asyncio.wait_for(asyncio.shield(task), SUBSCRIBER_BUDGET_S)
        except TimeoutError:
            logger.warning("offboarding subscriber %s still running after %.0f s for %s; "
                           "continuing without it", name, SUBSCRIBER_BUDGET_S, event.sub)
            _keep(task)


async def dispatch_losses(sub: str, agent_losses: Iterable[AgentLoss], actor_sub: str, *,
                          deleted: bool = False, person: Mapping | None = None,
                          platform_before: str | None = None,
                          platform_after: str | None = None) -> None:
    """Send the events for one committed change: ``removed`` for the agents
    where the person lost access or their row, ``demoted`` for the rest, or
    one ``deleted`` event (sent even with no agent, so a subscriber can sweep
    what the person left on agents they had lost before). ``person`` is the
    user row read before the change. The chain runs in the background; the
    caller waits for it at most ``ROUTE_WAIT_S``."""
    agent_losses = list(agent_losses)
    person = person or {}
    common = dict(actor_sub=actor_sub, username=person.get("username") or "",
                  email=person.get("email") or "",
                  display_name=person.get("display_name") or person.get("name") or "",
                  platform_before=platform_before, platform_after=platform_after)
    if deleted:
        events = [OffboardEvent(sub, DELETED, tuple(agent_losses), **common)]
    else:
        gone = tuple(a for a in agent_losses if a.lost_access or a.lost_row)
        lower = tuple(a for a in agent_losses if not (a.lost_access or a.lost_row))
        events = [OffboardEvent(sub, reason, group, **common)
                  for reason, group in ((REMOVED, gone), (DEMOTED, lower)) if group]
    if not events:
        return

    async def chain() -> None:
        for event in events:
            try:
                await on_user_offboarded(event)
            except Exception:
                logger.exception("offboarding of %s (%s) failed", sub, event.reason)

    task = asyncio.ensure_future(chain())
    _keep(task)
    try:
        await asyncio.wait_for(asyncio.shield(task), ROUTE_WAIT_S)
    except TimeoutError:
        logger.info("offboarding of %s continues in the background", sub)
