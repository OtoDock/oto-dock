"""Auto-attach newly-created users to default community agents.

A community-agent template can declare in its ``agent.json``::

    {
      "default_for_new_users": { "enabled": true, "role": "viewer" }
    }

The platform mirrors that into the agent row's ``default_for_new_users_role``
column at install time (admin can override later via the agent's Setup tab).
Every fresh user-creation path then calls :func:`assign_default_agents` once,
which walks the set of agents with a non-empty role and attaches the user
with that role — idempotent via ``user_agents``' composite PK and a
``users.default_agents_assigned`` boolean that prevents OIDC re-logins from
re-firing the attach.

This module is the SINGLE wiring point for the default-attach feature; the
caller need only know about :func:`assign_default_agents`.
"""

from __future__ import annotations

import logging

from storage.agents import agent_store
from storage import database as user_store
from services.community import community_agent_installer

logger = logging.getLogger("claude-proxy.default-agent-assigner")


def assign_default_agents(user_sub: str) -> dict[str, str]:
    """Attach ``user_sub`` to every agent whose admin enabled the default-attach.

    Marks the user as default-assigned at the end so subsequent OIDC
    re-logins skip the pass. Returns ``{agent_slug: status}`` for callers
    that want to log the outcome — ``"attached"`` if a new ``user_agents``
    row was created, ``"already-attached"`` for the PK-conflict path,
    ``"skipped-already-assigned"`` when the bool guard short-circuits the
    whole loop, ``"error: <message>"`` for per-agent failures (other agents
    in the loop still get processed).
    """
    if user_store.is_default_agents_assigned(user_sub):
        return {"_all_": "skipped-already-assigned"}

    defaults = agent_store.list_default_for_new_users_agents()
    result: dict[str, str] = {}
    for agent in defaults:
        slug = agent["slug"]
        role = agent.get("default_for_new_users_role", "")
        if not role:
            # Defense in depth — list_default_for_new_users_agents already
            # filters this, but if a future caller swaps in a different
            # source, we don't want to attach with an empty role.
            continue
        try:
            inserted = user_store.add_user_agent(
                user_sub, slug, role, assigned_by="system",
            )
            if inserted:
                from services.notifications.notification_manager import invalidate_audience
                invalidate_audience(slug)
                # Seed per-user template items for the freshly-attached pair.
                # Failures here don't undo the attach — the user can still
                # use the agent; admin can re-run reseed-template-items.
                try:
                    community_agent_installer.on_user_added_to_agent(
                        slug, user_sub, role,
                    )
                except Exception:
                    logger.exception(
                        "on_user_added_to_agent failed for (%s, %s) — attach kept",
                        slug, user_sub,
                    )
                result[slug] = "attached"
            else:
                result[slug] = "already-attached"
        except Exception as exc:
            # Most likely: FK violation if user_sub doesn't exist yet
            # (caller should have created the user first). Logged as an
            # error because every legitimate code path creates the user
            # before calling us.
            logger.exception(
                "Failed to attach default agent %s to %s", slug, user_sub,
            )
            result[slug] = f"error: {exc}"

    user_store.mark_default_agents_assigned(user_sub)
    return result
