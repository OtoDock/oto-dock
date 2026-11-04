"""Promotion: a person's memory topic moved into the agent's shared scope.

One function, ``promote``, behind the person's role: the memory tool's
``rename`` from ``/memories/user/<x>.md`` to ``/memories/agent/<x>.md``,
the dashboard route ``POST /v1/internal/memory/promote`` and the chat's
"promote a memory" card (``api/cards/actions_team``) all call it. The
other direction never exists: shared knowledge does not disappear into
one person's scope.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import config
from auth import roles
from services.memory import memory_file, memory_publish
from services.memory.memory_file import Caps, MemoryOpError, OpResult
from storage.agents import agent_store, memory_store

ROLE_REFUSAL = (
    "agent memory is read-only for your role (editors and managers write "
    "it): the topic stays in /memories/user/"
)


class MemoryRoleError(Exception):
    """The promotion refused by the person's role; the message is the text
    the tool relays and the route answers 403 with. Deliberately not
    ``PermissionError``: that one is an ``OSError`` a disk refusal raises."""


def _enabled(scope: str, settings: Mapping[str, Any], toggles: Mapping[str, Any]) -> bool:
    key = f"{scope}_memory_enabled"
    return bool(settings.get(key) and toggles.get(key))


async def promote(
    agent: str, username: str, rel: str, *, role: str, user_sub: str = "",
    dst_rel: str | None = None, caps: Caps | None = None,
) -> OpResult:
    """Move ``rel`` from ``username``'s scope into the shared scope as
    ``dst_rel`` (``rel`` by default; a rename and a promotion in one step).

    Refuses below the editor tier (``MemoryRoleError``); with a
    ``MemoryOpError`` an agent that is gone, an agent whose mode or toggles
    lack one of the two scopes, a missing topic, a folder, a taken name and
    a cap the shared scope would break (the create caps, the growth cap
    included). Commits the person's repo (``memory: delete``) and the
    agent's (``memory: promote``), both attributed, and publishes both roots.
    """
    if not roles.can_edit(role):
        raise MemoryRoleError(ROLE_REFUSAL)
    # The function is safe whoever calls it: the paths are validated here
    # (no traversal, no hidden segment, never the index, a text extension),
    # not only at the tool and the route.
    if not username:
        raise MemoryOpError("Error: a promotion needs the person's own memory scope.")
    rel = memory_file.validate_rel(rel, mutating=True, require_ext=True)
    target = memory_file.validate_rel(dst_rel or rel, mutating=True, require_ext=True)
    if not rel or not target:
        raise MemoryOpError("Error: a promotion names one topic file. Please provide a valid path.")
    row = await asyncio.to_thread(agent_store.get_agent, agent)
    if not row:
        raise MemoryOpError("Error: this agent no longer exists.")
    from core.session.visibility import SCOPE_AGENT, SCOPE_USER, available_scopes_for
    modes = available_scopes_for(
        bool(row.get("collaborative", True)), row.get("default_scope") or SCOPE_USER,
    )
    settings = await asyncio.to_thread(memory_store.get_settings)
    toggles = await asyncio.to_thread(memory_store.get_agent_toggles, agent)
    for scope in (SCOPE_USER, SCOPE_AGENT):
        if scope not in modes or not _enabled(scope, settings, toggles):
            raise MemoryOpError(
                f"Error: the {scope} memory scope is not available on this "
                "agent, so nothing can be promoted."
            )
    agent_dir = config.get_agent_dir(agent)
    src_root = memory_file.scope_root(agent_dir, SCOPE_USER, username)
    dst_root = memory_file.scope_root(agent_dir, SCOPE_AGENT)
    try:
        src_result, dst_result = await asyncio.to_thread(
            memory_file.op_promote, src_root, dst_root, rel, target, caps=caps,
        )
    except OSError as exc:
        raise MemoryOpError(
            f"Error: {rel} could not be moved. Try again."
        ) from exc
    await asyncio.to_thread(
        memory_publish.commit, agent_dir, SCOPE_USER, username, src_result,
        "delete", username,
    )
    await asyncio.to_thread(
        memory_publish.commit, agent_dir, SCOPE_AGENT, None, dst_result,
        "promote", username,
    )
    await memory_publish.publish_changes(
        agent, agent_dir, src_root, src_result,
        writer_slug=username, exclude_user_sub=user_sub,
    )
    await memory_publish.publish_changes(
        agent, agent_dir, dst_root, dst_result,
        writer_slug=username, exclude_user_sub=user_sub,
    )
    return dst_result
