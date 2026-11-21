"""A service binding and its webhook subscriptions follow the manager standing
that created them.

A manager lends one of their own connected accounts to an agent as its
service identity (``service_agent_bindings``); a service-scope webhook
subscription registers that account's events at the vendor for the agent.
The binding store already answers nothing for a lender who no longer
manages the agent, so agent-scope sessions lose the credential at once;
the rows themselves and the vendor registration are what this subscriber
clears. It runs on the offboarding chain, after the
sessions closer and the transfer: on a loss of the manager tier on any
agent, or a deletion, it re-reads the person's standing, clears every
binding of theirs on an agent they no longer manage (the subscriptions
first, the vendor delete resolving the token through the lender directly,
then the row), and sweeps every service-scope subscription whose binding
no longer stands (the delete route drops a deleted person's bindings before
the event, so the sweep is what reaches those).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Iterable

from auth import roles
from services.agents import offboarding
from services.webhooks import subscription_manager
from storage import database as task_store
from storage.automation import webhook_subscription_store
from storage.identity import credential_store, oauth_app_kinds
from storage.pg import run_db

logger = logging.getLogger("otodock")

SUBSCRIBER = "service-bindings"


def _lost_manager_tier(event: offboarding.OffboardEvent) -> bool:
    if event.reason == offboarding.DELETED:
        return True
    return any(roles.can_manage(loss.old_role) and not roles.can_manage(loss.new_role)
               for loss in event.agents)


def _standing(sub: str) -> tuple[dict | None, dict[str, str]]:
    user = task_store.get_user(sub)
    return user, (task_store.get_user_agent_roles(sub) if user else {})


def _manages(user: dict | None, agent_roles: dict[str, str], agent: str) -> bool:
    if not user:
        return False
    return roles.can_manage(roles.effective_role(user.get("role"), agent_roles, agent))


async def clear_binding(row: dict, owner_sub: str) -> None:
    """Take the lender's row out of the agent's set through the one remover,
    which unregisters its service-scope subscriptions through the lender's
    own token first; the admin delete route calls it while that token still
    exists. A chat that chose it is closed and told."""
    mcp_name, agent, label = row["mcp_name"], row["agent_name"], row["account_label"]
    from services.remote import chat_reload
    removed = await chat_reload.remove_service_account(
        mcp_name, agent, owner_sub=owner_sub, account_label=label)
    if not removed:
        logger.info("offboarding: the %s binding on %s names another account now; kept",
                    mcp_name, agent)
        return
    logger.info("offboarding: cleared the %s binding of %s on %s", mcp_name, owner_sub[:8], agent)


async def clear_personal_only(agent_name: str | None = None) -> int:
    """Take every account out of the own sets of a Personal-only agent (of
    every such agent with no ``agent_name``), through the one remover: its
    subscriptions, token files and sessions follow. A Personal-only agent
    has no accounts of its own (``account_context.holds_own_accounts``).
    Idempotent. Returns how many sets were emptied."""
    from services.mcp import account_context
    from services.remote import chat_reload
    from storage.identity import service_account_set
    emptied = 0
    for mcp_name, agent in await run_db(service_account_set.sets_of, agent_name):
        if await run_db(account_context.holds_own_accounts, agent):
            continue
        try:
            if await chat_reload.remove_service_account(mcp_name, agent):
                emptied += 1
                logger.warning("service accounts: %s is Personal only, its own %s accounts were "
                               "taken out", agent, mcp_name)
        except Exception:
            logger.exception("service accounts: the %s accounts of Personal-only %s were not "
                             "taken out", mcp_name, agent)
    return emptied


async def _clear_personal_only_after_boot() -> None:
    try:
        await asyncio.sleep(BOOT_PASS_AFTER_S)
        await clear_personal_only()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("service accounts: the boot pass over Personal-only agents failed")


async def _sweep_orphans() -> int:
    """Every service-scope subscription whose own account of the agent's set
    no longer stands, or no longer covers the installation it targets, goes,
    through that account's lender's token while the row is still there. A
    subscription on another account than the default stays: each account of
    the set keeps its own. The vendor delete is best-effort."""
    from storage.identity import service_account_set
    rows = await run_db(webhook_subscription_store.list_subscriptions, scope="service")
    gone = 0
    for row in rows:
        agent, mcp_name, label = row.get("agent") or "", row["mcp_name"], row["account_label"]
        if row.get("target_kind") == oauth_app_kinds.INSTALLATION_TARGET_KIND:
            stands = await run_db(subscription_manager.service_row_covers, mcp_name, agent, label,
                                  str(row.get("vendor_target") or ""))
        else:
            stands = await run_db(subscription_manager.service_account, mcp_name, agent, label) is not None
        if stands:
            continue
        held = next((r for r in await run_db(functools.partial(
            service_account_set.list_service_agent_set, mcp_name, agent, standing_only=False))
            if r.account_label == label), None)
        try:
            deleted, _ = await subscription_manager.delete_subscription(
                subscription_id=row["id"], token_owner_sub=held.account_owner_sub if held else "")
            gone += int(deleted)
        except Exception:
            logger.exception("offboarding: the orphaned subscription %s was not cleaned",
                             row.get("id"))
    return gone


async def clear_unmanaged_bindings(sub: str) -> int:
    """Clear every binding ``sub`` lends to an agent they no longer manage,
    their standing read now. Returns how many went."""
    user, agent_roles = await run_db(_standing, sub)
    rows = await run_db(credential_store.list_service_agent_bindings_for_owner, sub)
    cleared = 0
    for row in rows:
        if _manages(user, agent_roles, row["agent_name"]):
            continue
        await clear_binding(row, sub)
        cleared += 1
    return cleared


#: How long a demotion route waits for the inline clear (vendor calls); the
#: rest finishes in the background and the subscriber is the safety net.
DEMOTION_CLEAR_WAIT_S = 15.0
# The inline clears still running, by person: the subscriber waits for one
# instead of clearing the same rows beside it.
_inflight: dict[str, asyncio.Task] = {}


async def clear_at_demotion(sub: str, agent_losses: Iterable[offboarding.AgentLoss]) -> None:
    """The demotion site's own clear: when a change took the manager tier
    away on some agent, the person's bindings there go before the route
    answers, not only when the offboarding chain reaches its subscriber."""
    if not any(roles.can_manage(loss.old_role) and not roles.can_manage(loss.new_role)
               for loss in agent_losses):
        return
    task = _inflight.get(sub)
    if task is None or task.done():
        task = asyncio.ensure_future(clear_unmanaged_bindings(sub))
        _inflight[sub] = task
        offboarding._keep(task)
        task.add_done_callback(lambda t: _inflight.pop(sub, None) if _inflight.get(sub) is t else None)
    try:
        await asyncio.wait_for(asyncio.shield(task), DEMOTION_CLEAR_WAIT_S)
    except TimeoutError:
        logger.info("offboarding: the bindings of %s are still clearing in the background", sub[:8])
    except Exception:
        logger.exception("offboarding: the bindings of %s were not cleared at the demotion", sub[:8])


async def on_offboard(event: offboarding.OffboardEvent) -> None:
    if not _lost_manager_tier(event):
        return
    running = _inflight.get(event.sub)
    if running is not None:
        with contextlib.suppress(Exception):
            await asyncio.shield(running)
    await clear_unmanaged_bindings(event.sub)
    swept = await _sweep_orphans()
    if swept:
        logger.info("offboarding: %d orphaned service subscription(s) swept after %s (%s)",
                    swept, event.sub[:8], event.reason)


#: A short while after the start, so the remover's closes find the sessions
#: re-adopted from machines.
BOOT_PASS_AFTER_S = 30.0


def register() -> None:
    """Subscribe the clear and schedule the boot pass over Personal-only
    agents. Called once from the lifespan (a running loop)."""
    from core import background
    offboarding.subscribe(SUBSCRIBER, on_offboard, priority=30)
    background.spawn(_clear_personal_only_after_boot(), name="personal-only-accounts",
                     owner="offboarding")
    logger.info("offboarding: %s subscribed (a binding follows its lender's standing)",
                SUBSCRIBER)
