"""Who pays for a session: a person's own AI accounts, or the subscriptions
shared with agents.

The rule, written once (``session_spender``): a delegated chat spends
exactly what its person's own chat on that agent would spend, and the shared
subscriptions pay only for the agent's own work. Every session builder asks
here, and ``spend_env`` makes the only call to the pool's
``resolve_subscription_env``.

Only who pays is decided here. Who a session runs as (its files, its mounts,
its role, its tools' accounts) stays the builders' (``TaskIdentity``): a
lane that works as the agent keeps the agent's scope while its person pays.

The person behind a lane is the platform's own record: the spawn writes it
(``dynamic_tasks.spender_sub``, copied onto the run), from the token's own
person or, for a session with none, from what that session's chat pays
(``asker_spender``). No prompt, request body or other agent sets it. A
person who can no longer use the agent is refused, never moved to the
shared subscriptions (``may_spend``).

Synchronous: every reader here touches the store, so the async callers run
them on the DB executor.
"""

from __future__ import annotations

import enum
import logging
from typing import Any, NamedTuple

from auth import roles
from core.session import session_kind
from core.session.visibility import SCOPE_AGENT, SCOPE_USER, is_shared_only
from services.engines import subscription_pool
from services.scheduler import task_kinds
from storage import database as task_store

logger = logging.getLogger(__name__)


class Kind(enum.Enum):
    """What a session is, for the rule."""

    CHAT = "chat"            # a person's own turn
    CHAT_LANE = "chat-lane"  # a chat-surface delegated worker
    TASK = "task"            # every other run: a task lane, a scheduled, one-time, trigger or app run
    JUDGE = "judge"          # a check's judge, paid like the turn it judges
    MEETING = "meeting"      # a meeting participant
    CALL = "call"            # a phone call


class Spender(NamedTuple):
    """Whose accounts pay: ``sub`` the person, ``None`` the subscriptions
    shared with agents. ``row`` names the rule's row that answered (the log
    and the tests). ``refused`` is the sentence when the person may no
    longer spend on the agent: the session is refused, never the pool."""

    sub: str | None
    row: str
    refused: str = ""


POOL = Spender(None, "pool")


class Spent(NamedTuple):
    """What ``spend_env`` acquired: the account, the engine's environment and
    the person the layer binds the seat to ("" when the shared
    subscriptions paid)."""

    subscription_id: str
    env: dict[str, str]
    bound_sub: str


def _runs_as_agent(agent: str, scope: str) -> bool:
    return scope == SCOPE_AGENT or is_shared_only(agent)


def session_spender(agent: str, *, person: str | None, kind: Kind, scope: str = "",
                    route: bool = False) -> Spender:
    """The rule. No person: the shared subscriptions. A person's own turn, a
    chat lane, a judge (``person``: the spender of the turn it judges) and a
    person's meeting: the person. Any other run: the person when it is
    their own user-scope run on an agent that is not Shared-only, else the
    shared subscriptions. A call: the person on a person's route (``route``),
    the shared subscriptions on the agent's own line, for an outside caller
    and for a wrap-up."""
    if not person:
        return Spender(None, "no-person")
    if kind is Kind.CHAT:
        return Spender(person, "chat")
    if kind is Kind.CHAT_LANE:
        return Spender(person, "chat-lane")
    if kind is Kind.JUDGE:
        return Spender(person, "judge")
    if kind is Kind.MEETING:
        return Spender(person, "meeting") if scope == SCOPE_USER else Spender(None, "agent-meeting")
    if kind is Kind.CALL:
        return Spender(person, "call-route") if route else Spender(None, "call")
    if scope == SCOPE_USER and not is_shared_only(agent):
        return Spender(person, "task")
    return Spender(None, "agent-task")


def may_spend(person: str, agent: str, *, as_agent: bool) -> bool:
    """Whether a person may still pay for work on the agent: they exist, they
    hold the agent, and they hold the editor tier when the session runs as
    the agent (the bar the spawn sets for agent-scope work)."""
    from auth.providers import effective_role_of
    if not person or not task_store.get_user(person):
        return False
    standing = effective_role_of(person, agent)
    if standing == roles.NO_ACCESS:
        return False
    return not as_agent or roles.can_edit(standing)


def _lost_agent(kind: Kind, agent: str) -> str:
    """One sentence for a person who lost the agent, one now below the
    editor tier there, and one passed on who never held it."""
    who = ("the person whose chat asked for it" if kind is Kind.CHAT_LANE
           else "the person it runs for")
    what = "chat lane" if kind is Kind.CHAT_LANE else "work"
    return (f"This {what} runs on the AI account of {who}. That person cannot run work on {agent} now. "
            "It does not move to the subscriptions shared with agents.")


def _standing(spender: Spender, agent: str, kind: Kind, scope: str) -> Spender:
    """A judge is paid like the turn it judges, and that turn took its own
    rule (a person's chat takes no editor tier): the judge needs only the
    person to exist and hold the agent."""
    if not spender.sub or kind in (Kind.CHAT, Kind.CALL):
        return spender
    as_agent = _runs_as_agent(agent, scope) and kind is not Kind.JUDGE
    if may_spend(spender.sub, agent, as_agent=as_agent):
        return spender
    return spender._replace(refused=_lost_agent(kind, agent))


# ---------------------------------------------------------------------------
# What each source hands the rule
# ---------------------------------------------------------------------------

def run_kind(task: Any) -> Kind:
    """A judge's run, a chat-surface delegated worker, or any other run."""
    if getattr(task, "judge", None) or getattr(task, "task_type", "") == task_kinds.CHECK:
        return Kind.JUDGE
    chat_id = getattr(task, "target_chat_id", None) or ""
    if (getattr(task, "task_type", "") == task_kinds.DELEGATE and chat_id
            and not session_kind.is_task_chat_id(chat_id)):
        return Kind.CHAT_LANE
    return Kind.TASK


def _recorded(task: Any) -> str | None:
    """The person the spawn recorded (``spender_sub``): "" for the shared
    subscriptions. A row written before the column has none: its creator
    when that names a platform user and no agent (what the spawn gives a
    lane a person started), else the shared subscriptions. That is the only
    reading of the old shape."""
    recorded = getattr(task, "spender_sub", None)
    if recorded is not None:
        return recorded or None
    by = getattr(task, "created_by", None) or ""
    if not by:
        return None
    from storage.agents import agent_store
    if agent_store.agent_exists(by) or not task_store.get_user(by):
        return None
    return by


def run_spender(task: Any, *, agent: str = "") -> Spender:
    """Who pays for a run: a task definition, a run row, or the spawn's own
    view of its worker (an object with the same names). ``agent``: the
    builder's own agent, for a view that carries none."""
    agent = agent or task.agent
    kind = run_kind(task)
    person = _recorded(task) if kind in (Kind.CHAT_LANE, Kind.JUDGE) else (
        getattr(task, "created_by", None) or None)
    scope = getattr(task, "scope", "") or SCOPE_USER
    spender = session_spender(agent, person=person, kind=kind, scope=scope)
    return _standing(spender, agent, kind, scope)


class _RunRow:
    """A ``task_runs`` row read as ``run_spender`` reads a task: the run's
    chat is its target (a lane's chat, or the run's own ``task-`` chat)."""

    def __init__(self, row: dict) -> None:
        self.agent = row.get("agent") or ""
        self.scope = row.get("scope") or SCOPE_AGENT
        self.created_by = row.get("created_by")
        self.task_type = row.get("task_type") or ""
        self.spender_sub = row.get("spender_sub")
        self.target_chat_id = row.get("chat_id")
        self.judge = None


def chat_spender(chat: dict | None, asker: str | None, *, woken: bool = False) -> Spender:
    """Who pays for a session of ``chat``. A task chat pays as its run. A
    lane's chat that nobody typed into (``woken``: a wake, a continuation it
    scheduled, a result of its own lanes; or no person asking) pays as its
    newest delegate round, after the standing check. Otherwise the person
    asking pays, as their own turn: the session decides, never the chat
    row's kind, so a person's text in a chat a call wrote is theirs."""
    chat = chat or {}
    chat_id = chat.get("id") or ""
    agent = chat.get("agent") or ""
    run_id = session_kind.run_id_of_chat(chat_id)
    if run_id:
        run = task_store.get_run(run_id)
        if run:
            return run_spender(_RunRow(run))
    if woken or not asker:
        lane = _lane_round(chat_id)
        if lane is not None:
            return run_spender(_RunRow(lane))
    return session_spender(agent, person=asker, kind=Kind.CHAT)


def _lane_round(chat_id: str) -> dict | None:
    """The newest delegate run of a lane's chat, None for any other chat. A
    lane's delegate row goes with its round; its run rows stay."""
    if not chat_id:
        return None
    lane = task_store.get_run_for_chat(chat_id)
    return lane if lane and (lane.get("task_type") or "") == task_kinds.DELEGATE else None


def round_spender(chat_id: str) -> Spender | None:
    """What a lane's chat's newest delegate round pays, None for a chat
    that is no lane's. Synchronous: the DB executor."""
    lane = _lane_round(chat_id)
    return run_spender(_RunRow(lane)) if lane is not None else None


def typist_of_lane(chat: dict, sub: str) -> bool:
    """Whether ``sub`` types in a lane's chat as someone other than the
    person its newest round is paid by: a resume of their turn there is
    theirs (``chat_rebuild.nobody_typed``). Synchronous: the DB executor."""
    paid = round_spender(chat.get("id") or "")
    return paid is not None and (paid.sub or "") != sub


def serves_lane(chat_id: str, session_id: str, *, built_for: str | None = None) -> bool:
    """Whether a turn nobody typed may run on ``session_id``, one session of
    ``chat_id``: always on a chat that is no lane's, and on a lane's chat
    only when that session serves the lane's spender (``serves``), so a
    viewer who warmed it never pays for the lane's wake. Only a live session
    is judged: one that died leaves its stored binding behind, which holds
    nothing (inside the boot window a survivor not yet taken back still
    counts, as for the pin). ``built_for``: the payer a terminal's config
    was built with, judged in place of a binding its seat may not hold yet
    at its start; a live terminal under the id gives its own when none is
    passed. Synchronous: the DB executor."""
    if not session_id:
        return True
    if built_for is None:
        from core.session import interactive_registry
        built_for = getattr(interactive_registry.get(session_id), "payer", None)
    if built_for is None and not subscription_pool.within_boot_grace() \
            and subscription_pool.session_registered_live(session_id) is False:
        return True
    paid = round_spender(chat_id)
    if paid is None:
        return True
    return serves_payer(built_for, paid) if built_for is not None else serves(session_id, paid)


def wake_rides(chat_id: str, session_id: str) -> bool:
    """``serves_lane`` for the chat's current session: the one its row names
    now, else the caller's. Synchronous: the DB executor."""
    chat = task_store.get_chat(chat_id) if chat_id else None
    return serves_lane(chat_id, (chat or {}).get("session_id") or session_id)


def build_spender(chat_id: str, agent: str, person: str | None, *, call: bool = False,
                  route: bool = False) -> Spender:
    """What a session the chat builder builds spends: a call by its own
    kind (``call``; ``route``: a person's route), any other session as
    ``chat_spender`` reads its chat for ``person``, who asked for it (their
    warm-up, or the platform's resume of their cut turn)."""
    if call:
        return session_spender(agent, person=person, kind=Kind.CALL, route=route)
    chat = (task_store.get_chat(chat_id) if chat_id else None) or {"id": chat_id, "agent": agent}
    return chat_spender(chat, person)


def asker_spender(session_id: str, agent: str) -> Spender:
    """What a session with no person passes on to work it starts: what its
    chat pays (``chat_spender`` with nobody asking), else the person its live
    binding pays with (a Shared-only participant of a person's meeting),
    else the shared subscriptions."""
    if not session_id:
        return POOL
    chat = task_store.get_chat_by_session(session_id)
    if chat:
        spender = chat_spender(chat, None)
        if spender.sub or spender.refused:
            return spender
    payer = subscription_pool.get_session_payer_sub(session_id)
    return Spender(payer, "asker-binding") if payer else POOL


def meeting_spender(meeting: dict, agent: str) -> Spender:
    """A meeting a person starts stays theirs, every participant included;
    an agent-scope meeting is the agent's own work."""
    scope = meeting.get("scope") or SCOPE_AGENT
    spender = session_spender(agent, person=meeting.get("created_by"), kind=Kind.MEETING, scope=scope)
    return _standing(spender, agent, Kind.MEETING, scope)


def judged_spender(session_id: str, chat: dict | None, person: str | None) -> Spender:
    """What a check's judge records: what the turn it judges spent, the
    payer its session is bound to, else what its chat pays for
    ``person``."""
    if session_id and subscription_pool.get_session_subscription(session_id):
        payer = subscription_pool.get_session_payer_sub(session_id)
        return Spender(payer or None, "judged-binding")
    return chat_spender(chat, person)


def spawn_spender(acting_sub: str | None, session_id: str, source_agent: str) -> Spender:
    """The person a new delegate row records: the token's own person, else
    what the delegating session passes on (``asker_spender``)."""
    if acting_sub:
        return Spender(acting_sub, "spawn-person")
    return asker_spender(session_id, source_agent)


def lane_spender(acting_sub: str | None, session_id: str, source_agent: str, *, agent: str,
                 scope: str, chat_lane: bool) -> Spender:
    """What a new delegate round on ``agent`` spends, before its row exists
    (the spawn's view): a chat lane its asker's person (``spawn_spender``),
    a task lane by the task rule for the token's person. The token's own
    person stands as the spawn's authorization judged them; a person a
    session with none passes on takes the standing check on ``agent``, so
    a lane's own delegation to an agent its person cannot use is refused."""
    if chat_lane:
        asked = spawn_spender(acting_sub, session_id, source_agent)
        if asked.refused:
            return asked
        kind, person = Kind.CHAT_LANE, asked.sub
    else:
        kind, person = Kind.TASK, acting_sub
    spender = session_spender(agent, person=person, kind=kind, scope=scope)
    return spender if acting_sub else _standing(spender, agent, kind, scope)


# ---------------------------------------------------------------------------
# The pool, through this one door
# ---------------------------------------------------------------------------

def _account_word(layer: str) -> str:
    from core.session.session_manager import capabilities_for_path
    label = capabilities_for_path(layer).identity.account_label
    return f"{label} account" if label else "AI account"


def lane_block(layer: str, person: str, agent: str) -> str:
    """The sentence a lane's person reads when their accounts cannot run the
    lane's engine, as their own chat on ``agent`` would not."""
    head = ("This chat lane runs on the AI account of the person whose chat asked for it, "
            f"as their own chat on {agent} would.")
    reason = subscription_pool.user_scope_block_reason(layer, person)
    if reason == "own_sub_expired":
        return f"{head} Their {_account_word(layer)} sign-in has expired. They can reconnect it under Settings, AI Engines."
    if reason in ("throttled", "pool_cap"):
        return str(subscription_pool.NoSubscriptionError(reason))
    return (f"{head} They have no {_account_word(layer)} they can use there. "
            "They can connect one under Settings, AI Engines.")


def refusal(spender: Spender, layer: str, *, agent: str = "", lane: bool = False) -> str:
    """"" when ``spender`` can run ``layer``, else the sentence: the lost
    agent's, a lane's own, or a person's block reason."""
    if spender.refused:
        return spender.refused
    if not spender.sub or subscription_pool.user_can_run(layer, spender.sub):
        return ""
    if lane:
        return lane_block(layer, spender.sub, agent)
    return str(subscription_pool.NoSubscriptionError(
        subscription_pool.user_scope_block_reason(layer, spender.sub)))


def can_run(layer: str, person: str) -> bool:
    """Whether a person's own accounts (or what they may borrow) can run
    ``layer``: the setup banner's and the engine pages' question."""
    return subscription_pool.user_can_run(layer, person)


def spend_env(spender: Spender, layer: str, *, model: str = "", agent_info: dict | None = None,
              sticky_scope: str = "", agent: str = "", lane: bool = False,
              call_fallback: bool = False) -> Spent:
    """Acquire the account ``spender`` pays with and the engine's
    environment. A person who resolves nothing is refused with the reason
    (a lane's own sentence when ``lane``); the shared subscriptions never
    stand in, except for a call to a person's route (``call_fallback``),
    which then binds to nobody. Every engine with no single vendor gets the
    person its provider switch draws for (``_USER_SUB``)."""
    from core.session.session_manager import capabilities_for_path
    if spender.refused:
        raise subscription_pool.NoSubscriptionError("lost_agent", spender.refused)
    person = spender.sub
    subscription_id, env = "", dict[str, str]()
    try:
        subscription_id, env = subscription_pool.resolve_subscription_env(
            layer, person, model=model, agent_info=agent_info, sticky_scope=sticky_scope)
    except subscription_pool.NoSubscriptionError:
        if not (person and call_fallback):
            raise
    except Exception as e:  # the pool spans db, relay and engine: a miss becomes a clean block
        logger.warning("Subscription pool error on %s for %s: %s", layer, agent or "-", e)
        if capabilities_for_path(layer).auth.credential_file is not None:
            # A start with no account in a sign-in folder would read
            # whatever file sits there, another person's among them.
            raise subscription_pool.NoSubscriptionError("none") from e
    if person and not subscription_id and call_fallback:
        subscription_id, env = subscription_pool.resolve_subscription_env(
            layer, None, model=model, agent_info=agent_info, sticky_scope=sticky_scope)
        logger.info("A call on %s's route: no account of the person, the shared subscriptions pay", agent or "-")
        person = None
    if person and not subscription_id:
        if lane:
            raise subscription_pool.NoSubscriptionError("none", lane_block(layer, person, agent))
        raise subscription_pool.NoSubscriptionError(subscription_pool.user_scope_block_reason(layer, person))
    env = dict(env)
    if not capabilities_for_path(layer).identity.vendor_id:
        env["_USER_SUB"] = person or ""
    return Spent(subscription_id, env, person or "")


def bucket(spender: Spender, scope: str) -> str:
    """The scope a run's pump writes: the agent's when the shared
    subscriptions pay, else the run's own (an agent-scope row the pump's
    writer moves to the session's payer, ``stream_pump._record_usage``)."""
    return scope if spender.sub else SCOPE_AGENT


def run_bucket(run_id: str) -> str:
    """``bucket`` for a run row (a re-adopted run's pump)."""
    run = task_store.get_run(run_id) if run_id else None
    if not run:
        return SCOPE_AGENT
    row = _RunRow(run)
    return bucket(run_spender(row), row.scope)


def session_payer(session_id: str) -> str | None:
    """The payer a live session's binding names ("" the shared
    subscriptions), None for a session the pool never bound. Synchronous:
    the store when the in-memory maps do not know it."""
    if not session_id:
        return None
    _sub, payer = subscription_pool.bound_attribution(session_id)
    if payer is not None:
        return payer
    if not subscription_pool.get_session_subscription(session_id):
        return None
    return subscription_pool.get_session_payer_sub(session_id)


def serves_payer(payer: str | None, spender: Spender) -> bool:
    """The one answer to whether a live session serves ``spender``, from the
    payer its binding names (``session_payer``): a session the pool never
    bound serves anyone, since it holds no account of another person."""
    return payer is None or payer == (spender.sub or "")


def serves(session_id: str, spender: Spender) -> bool:
    """Whether a live session serves this spender (``serves_payer``)."""
    return serves_payer(session_payer(session_id), spender)
