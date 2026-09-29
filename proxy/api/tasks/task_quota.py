"""The caps on active automations (``config.TASK_MAX_ACTIVE_PER_USER``,
``TASK_MAX_ACTIVE_PER_AGENT``, ``CONTINUATION_MAX_ACTIVE_PER_CHAT``,
``CONTINUATION_MAX_ACTIVE_PER_AGENT``; 0 = no cap).

One person may hold at most the per-person number of active scheduled or
one-time tasks of their own (user scope, across agents); one agent at most
its number of active clocked tasks of both scopes; one chat at most its
pending self-continuations, one agent theirs. A row counts while it is
enabled and has a clock: a recurring row, or a one-time row not yet fired.
Delegate workers, check runs, app handler rows and continuations count as
tasks never (by kind), and a row a template seeded counts against nobody
(it is the installer's decision) until an edit gives it a clock: the edit
clears its ``community_template`` and it counts from then on (the timing
is the editor's decision). The rows that pass a cap and the rows
that count are decided by ONE predicate (``counted_task`` and its SQL twin
in ``storage/automation/db_tasks.py``), so a resume or a timing edit is
judged as "would this row newly count".

A platform admin's cookie and the master key are exempt from the
per-person cap; the per-agent cap holds for everyone but the master key;
the continuation caps hold for everyone (the route takes session callers
only, and ten pending wakes on one chat are a runaway whoever owns it).
The count and the insert run under one lock: concurrent creates cannot
all read cap minus one (the proxy is one process).
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException

import config
from auth.providers import UserContext
from core.session import visibility as _vis
from services.scheduler import task_kinds
from storage import database as task_store
from storage.pg import run_db

# The kinds a task cap counts; the continuation caps count their own kind.
COUNTED_KINDS = (task_kinds.SCHEDULED, task_kinds.ONE_TIME)

_create_lock = asyncio.Lock()


def counted_task(row: dict) -> bool:
    """Whether a row counts toward the task caps (the SQL twin is
    ``db_tasks.count_active_clocked_tasks``)."""
    return bool(
        row.get("enabled", True)
        and (row.get("task_type") or "") in COUNTED_KINDS
        and not row.get("fired")
        and row.get("app_id") is None
        and not row.get("community_template")
    )


def counted_continuation(row: dict) -> bool:
    return bool(row.get("enabled", True) and (row.get("task_type") or "") == task_kinds.CONTINUATION)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def enforce_task_caps(u: UserContext, agent: str, created_by: str, scope: str) -> None:
    """429 when one more counted row of ``scope`` on ``agent`` by
    ``created_by`` would pass a cap. Call under ``creating()``."""
    per_user = int(config.TASK_MAX_ACTIVE_PER_USER or 0)
    per_agent = int(config.TASK_MAX_ACTIVE_PER_AGENT or 0)
    person = (u.acting_sub is not None and not u.is_admin and not u.is_service
              and scope == _vis.SCOPE_USER)
    if u.is_service:
        per_agent = 0
    if not person:
        per_user = 0
    if per_user <= 0 and per_agent <= 0:
        return

    def _counts() -> tuple[int, int]:
        own = task_store.count_active_clocked_tasks(
            created_by=created_by, scope=_vis.SCOPE_USER) if per_user > 0 else 0
        held = task_store.count_active_clocked_tasks(agent=agent) if per_agent > 0 else 0
        return own, held

    own, held = await run_db(_counts)
    if per_user > 0 and own >= per_user:
        raise HTTPException(
            429,
            f"You already have {_plural(own, 'active scheduled task')} across your agents "
            f"(the limit is {per_user}). Delete or pause one first.",
        )
    if per_agent > 0 and held >= per_agent:
        raise HTTPException(
            429,
            f"This agent already has {_plural(held, 'active scheduled task')} "
            f"(the limit is {per_agent}). Delete or pause one first.",
        )


async def enforce_continuation_caps(chat_id: str, agent: str) -> None:
    """429 when one more pending self-continuation on ``chat_id`` would pass
    the chat's or the agent's cap. Call under ``creating()``."""
    per_chat = int(config.CONTINUATION_MAX_ACTIVE_PER_CHAT or 0)
    per_agent = int(config.CONTINUATION_MAX_ACTIVE_PER_AGENT or 0)
    if per_chat <= 0 and per_agent <= 0:
        return

    def _counts() -> tuple[int, int]:
        on_chat = task_store.count_active_continuations(
            target_chat_id=chat_id) if per_chat > 0 else 0
        on_agent = task_store.count_active_continuations(agent=agent) if per_agent > 0 else 0
        return on_chat, on_agent

    on_chat, on_agent = await run_db(_counts)
    if per_chat > 0 and on_chat >= per_chat:
        raise HTTPException(
            429,
            f"This chat already has {_plural(on_chat, 'pending self-continuation')} "
            f"(the limit is {per_chat}). Let one fire, or cancel one first.",
        )
    if per_agent > 0 and on_agent >= per_agent:
        raise HTTPException(
            429,
            f"This agent already has {_plural(on_agent, 'pending self-continuation')} "
            f"(the limit is {per_agent}). Let one fire, or cancel one first.",
        )


async def enforce_newly_counted(u: UserContext, before: dict, after: dict) -> None:
    """The resume and edit rule: a cap is checked when the row would count
    after the change and did not before, against the caps of its kind."""
    if counted_task(after) and not counted_task(before):
        await enforce_task_caps(u, after.get("agent") or "", after.get("created_by") or "",
                                after.get("scope") or _vis.SCOPE_USER)
    if counted_continuation(after) and not counted_continuation(before):
        await enforce_continuation_caps(after.get("target_chat_id") or "", after.get("agent") or "")


def creating():
    """The lock a count-then-insert holds."""
    return _create_lock
