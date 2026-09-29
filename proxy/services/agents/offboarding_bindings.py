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

import logging

from auth import roles
from services.agents import offboarding
from services.webhooks import subscription_manager
from storage import database as task_store
from storage.automation import webhook_subscription_store
from storage.identity import credential_store
from storage.pg import run_db

logger = logging.getLogger("claude-proxy")

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
    """Unregister the service-scope subscriptions the binding lent through
    the lender's own token, then drop the binding row; the admin delete
    route calls it while that token still exists."""
    mcp_name, agent, label = row["mcp_name"], row["agent_name"], row["account_label"]
    try:
        cleaned = await subscription_manager.cleanup_account_subscriptions(
            scope="service", owner=owner_sub, mcp_name=mcp_name, account_label=label,
            agent=agent, token_owner_sub=owner_sub,
        )
    except Exception:
        logger.exception("offboarding: the subscriptions of %s on %s were not cleaned",
                         mcp_name, agent)
        cleaned = 0
    await run_db(credential_store.remove_service_agent_binding, mcp_name, agent)
    logger.info("offboarding: cleared the %s binding of %s on %s (%d subscription(s))",
                mcp_name, owner_sub[:8], agent, cleaned)


async def _sweep_orphans() -> int:
    """Every service-scope subscription whose binding no longer stands, or
    now names another account, goes; the vendor delete is best-effort."""
    rows = await run_db(webhook_subscription_store.list_subscriptions, scope="service")
    gone = 0
    for row in rows:
        agent = row.get("agent") or ""
        binding = await run_db(credential_store.get_service_agent_binding,
                               row["mcp_name"], agent)
        if binding and binding[0] == row["account_label"]:
            continue
        try:
            gone += await subscription_manager.cleanup_account_subscriptions(
                scope="service", owner="", mcp_name=row["mcp_name"],
                account_label=row["account_label"], agent=agent,
            )
        except Exception:
            logger.exception("offboarding: the orphaned subscription %s was not cleaned",
                             row.get("id"))
    return gone


async def on_offboard(event: offboarding.OffboardEvent) -> None:
    if not _lost_manager_tier(event):
        return
    user, agent_roles = await run_db(_standing, event.sub)
    rows = await run_db(credential_store.list_service_agent_bindings_for_owner, event.sub)
    for row in rows:
        if _manages(user, agent_roles, row["agent_name"]):
            continue
        await clear_binding(row, event.sub)
    swept = await _sweep_orphans()
    if swept:
        logger.info("offboarding: %d orphaned service subscription(s) swept after %s (%s)",
                    swept, event.sub[:8], event.reason)


def register() -> None:
    offboarding.subscribe(SUBSCRIBER, on_offboard, priority=30)
    logger.info("offboarding: %s subscribed (a binding follows its lender's standing)",
                SUBSCRIBER)
