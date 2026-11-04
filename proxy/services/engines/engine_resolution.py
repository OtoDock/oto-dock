"""Engines resolve per person: the engine and model a person's new session
takes on an agent, from what the agent allows and what the person holds.

The agent's engine switches are an ALLOWLIST ("this agent may run on this
engine"; ``agent_store._get_execution_paths``, the primary first), with the
engines of ``agents.local_only_engines`` restricted to local models. The
person's holdings are the providers of their own active accounts and of the
platform's accounts they may borrow (``subscription_pool``'s rule:
``_USER_BORROWABLE_AUTH_TYPES``, never another person's OAuth login), both
per engine, because a spawn on an engine with several providers takes the
provider of its model (``subscription_pool.resolve_subscription_env``).

The order, written once (the engine docs say the same):

1. the engines the person may run on the agent, in the order of their own
   use (``person_order``): the chats they opened themselves on the agent in
   the last ``USE_WINDOW_DAYS`` days, then their chats on every agent, then
   the order they connected their accounts, then the agent's allowlist. A
   new first engine needs six chats in ten of the window (the agent's, else
   every agent's), else the first the picker showed last stays
   (``user_ui_prefs.data.engine_first``, written by the agent routes alone)
   while it has a chat in that window. A preferred engine stored before the
   order (``users.preferred_execution_path``, never written now) comes first
   while the person has no chat on an engine the agent lets them run, here
   or anywhere. When chats or that preference decide the first place, the
   pick is the first of the order;
2. else (no chat anywhere on an engine the agent lets them run) the agent's
   default engine, when the person holds it;
3. else the first engine in the agent's order the person holds with an
   account of their own;
4. else the first engine in the agent's order the person may borrow from
   the platform (with today's accounts: the hosted relay on Direct, an API
   key an admin pooled);
5. else nothing: the caller falls back to the agent's default and the pool
   refuses with the card that names what to connect.

"Holds an engine" means a model of a provider the person holds there is
offered on it: the agent's pinned model when the engine serves it, else the
engine's own order (the declared default, then down the tiers). A resting
account (``subscription_pool.account_resting``: a usage limit, a throttle)
is still held, so its engine stays in ``allowed``, the list a person picks
from; the pick skips it while another held engine is awake, and when every
held account rests the pick ignores the rest (the first message meets the
usage card).

Who calls it: the agent routes (the fields a new chat is seeded from), the
task runner (an unpinned user-scope run, stamped on its chat row), the
delegated worker's chat row, the task listing, the delegation roster (each
target's ``default`` mark and the engines the person may run there) and the
spawn validator (the engine a model pinned without its layer is looked up
on first).
Not a rebuild of an existing chat: it keeps the row's engine.
``engine_for_model`` is the pin's twin: the engine a model pinned without
its layer runs on (the spawn validator, the runner, the task listing).

Pure where it can be: ``resolve_for_person`` takes the rows it needs
(``person_holdings`` and ``engine_catalog`` read them once per request; the
person's use is read once a minute per person) and is the table the tests
cover. Synchronous (``asyncio.to_thread``). The
registry and the pool are imported inside the functions
(``tests/execution/test_import_direction.py``).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from core.execution_layer import PROVIDER_LOCAL
from storage.agents.agent_store import _get_execution_paths, _get_local_only_engines
from storage.agents.schema import DEFAULT_EXECUTION_PATH
from storage.chat import engine_use
from storage.prefs import user_ui_prefs_store

logger = logging.getLogger("otodock")

#: ``Resolution.reason``: which branch of the order answered.
REASON_USE = "use"
REASON_SEED = "seed"
REASON_AGENT_DEFAULT = "agent_default"
REASON_OWN = "own"
REASON_PLATFORM = "platform"
REASON_NONE = "none"

#: ``Candidate.source``: whose account the candidate rides.
SOURCE_OWN = "own"
SOURCE_PLATFORM = "platform"

#: The days a person's own chats count over, and the share of them a new
#: first engine needs (six chats in ten); below it the first place stays.
USE_WINDOW_DAYS = 30
_CLEAR_LEAD = (6, 10)
#: The use read is kept per person this long: the picker's fetch, the roster
#: and the runs of that minute share one read.
_USE_TTL = 60.0
_USE_CACHE_PRUNE_AT = 512
#: Sorts after every connection time: an engine with no account of the
#: person's own comes after the connected ones.
_NEVER_CONNECTED = "\uffff"


@dataclass(frozen=True)
class Holdings:
    """What one person can bring, per engine: the providers of their own
    active ``use_personal`` accounts (``own``), the providers of the
    platform's borrowable accounts when their Platform Auth is on
    (``pool``), the providers every held account of which rests right now
    (``resting``, own and pool alike). And what orders their engines: their
    own chats per agent and engine in the window (``use``), the engine
    their picker last showed first per agent (``first_shown``), when they
    connected each engine (``connected``: the oldest of their own active
    accounts there, ISO), and a preferred engine stored before the order
    (``seed``, '' = none). ``use_read`` is False when the use could not be
    read: the order then stands on the seed and the connection order alone,
    and nothing of it is remembered. Never mutated: the dicts may be
    shared."""
    own: dict[str, frozenset[str]] = field(default_factory=dict)
    pool: dict[str, frozenset[str]] = field(default_factory=dict)
    resting: dict[str, frozenset[str]] = field(default_factory=dict)
    use: dict[str, dict[str, int]] = field(default_factory=dict)
    first_shown: dict[str, str] = field(default_factory=dict)
    connected: dict[str, str] = field(default_factory=dict)
    seed: str = ""
    use_read: bool = True


@dataclass(frozen=True)
class ModelRow:
    """One row of an engine's model table, as the resolution reads it."""
    model_id: str
    provider: str
    enabled: bool


@dataclass(frozen=True)
class EngineCatalog:
    """Per engine: its model rows in the engine's own order (the declared
    default first, then down the tiers; disabled rows after) and the ids of
    its local providers."""
    models: dict[str, tuple[ModelRow, ...]] = field(default_factory=dict)
    local_providers: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    execution_path: str
    model: str
    source: str


@dataclass(frozen=True)
class Resolution:
    """The engine and model a new session takes (``""`` when nothing
    resolves), the branch that answered, the agent's allowlist in its
    order, the engines of it the person may run in their order, and each
    one's model. ``first_shown`` is the engine the person's order put first
    by their use or a stored preference ('' otherwise), ``first_is_new``
    whether it is not the one remembered for the agent."""
    execution_path: str
    model: str
    reason: str
    order: tuple[str, ...]
    allowed: tuple[str, ...]
    models_for_me: dict[str, str]
    first_shown: str = ""
    first_is_new: bool = False

    @property
    def engines_for_me(self) -> dict[str, bool]:
        """Every engine of the agent's allowlist: whether the person may run it."""
        return {path: path in self.allowed for path in self.order_for_me}

    @property
    def order_for_me(self) -> tuple[str, ...]:
        """The agent's allowlist for this person: the engines they may run in
        their order, then the rest in the allowlist's."""
        return self.allowed + tuple(path for path in self.order if path not in self.allowed)


# --- the registry -----------------------------------------------------------

def engine_order() -> list[str]:
    """Every registered engine, in the AI Engines page order
    (``identity.sort_order``, then the id)."""
    from core.session.session_manager import get_all_layers
    rows = [(layer.capabilities.identity.sort_order, path)
            for path, layer in get_all_layers().items()]
    return [path for _order, path in sorted(rows)]


def every_engine(primary: str) -> list[str]:
    """The extras a new agent is created with: every registered engine but
    its primary (the switches are an allowlist, all on by default)."""
    return [path for path in engine_order() if path != primary]


def local_providers_of(execution_path: str) -> frozenset[str]:
    """The ids of the engine's local providers (the descriptor's ``local``
    kind); empty for an engine that declares none (Claude Code, the hosted
    build's Codex and Direct)."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(execution_path)
    if caps is None:
        return frozenset()
    return frozenset(
        str(p.get("id") or "") for p in (caps.providers or ())
        if p.get("kind") == PROVIDER_LOCAL and p.get("id")
    )


def engine_catalog(engines: list[str] | None = None) -> EngineCatalog:
    """The model tables of ``engines`` (every registered one by default), in
    each engine's order, with the local providers. One store read per
    engine."""
    import config as app_config
    from storage.billing import subscription_store
    models: dict[str, tuple[ModelRow, ...]] = {}
    local: dict[str, frozenset[str]] = {}
    for path in (engines if engines is not None else engine_order()):
        rows = subscription_store.list_models(layer=path)
        enabled = app_config.ordered_layer_models(path, [m for m in rows if m.get("enabled")])
        disabled = [m for m in rows if not m.get("enabled")]
        models[path] = tuple(
            ModelRow(str(m.get("model_id") or ""), str(m.get("provider") or ""), bool(m.get("enabled")))
            for m in [*enabled, *disabled] if m.get("model_id")
        )
        local[path] = local_providers_of(path)
    return EngineCatalog(models=models, local_providers=local)


# --- the person ---------------------------------------------------------------

def person_holdings(user_sub: str, engines: list[str] | None = None) -> Holdings:
    """What ``user_sub`` can bring on each of ``engines`` (every registered
    one by default): the store reads of the pool's own rule, nothing
    acquired. A falsy sub holds nothing."""
    from services.engines import subscription_pool
    from storage import database as task_store
    from storage.billing import subscription_store
    if not user_sub:
        return Holdings()
    own: dict[str, frozenset[str]] = {}
    pool: dict[str, frozenset[str]] = {}
    resting: dict[str, frozenset[str]] = {}
    connected: dict[str, str] = {}
    borrow = subscription_store.get_user_allow_platform_auth(user_sub)
    for path in (engines if engines is not None else engine_order()):
        held = [
            s for s in subscription_store.list_personal(path, user_sub) if s.get("provider")]
        borrowed = [
            s for s in subscription_store.list_platform_pool(path)
            if borrow and s.get("provider")
            and s.get("auth_type") in subscription_pool._USER_BORROWABLE_AUTH_TYPES
        ]
        own[path] = frozenset(str(s["provider"]) for s in held)
        pool[path] = frozenset(str(s["provider"]) for s in borrowed)
        since = [str(s.get("created_at") or "") for s in held if s.get("created_at")]
        if since:
            connected[path] = min(since)
        awake = {str(s["provider"]) for s in [*held, *borrowed]
                 if not subscription_pool.account_resting(str(s.get("id") or ""))}
        resting[path] = frozenset((own[path] | pool[path]) - awake)
    row = task_store.get_user(user_sub) or {}
    use, first_shown, read = _person_use(user_sub)
    return Holdings(own=own, pool=pool, resting=resting, use=use, first_shown=first_shown,
                    connected=connected, seed=str(row.get("preferred_execution_path") or ""),
                    use_read=read)


# --- the person's use -------------------------------------------------------

_use_lock = threading.Lock()
_use_cache: dict[str, tuple[float, dict[str, dict[str, int]], dict[str, str]]] = {}


def clear_use_cache() -> None:
    """Forget every person's use read (the tests' clean slate)."""
    with _use_lock:
        _use_cache.clear()


def _person_use(user_sub: str) -> tuple[dict[str, dict[str, int]], dict[str, str], bool]:
    """The person's own chats per agent and engine in the window, the first
    engine their picker last showed per agent and whether they were read,
    read at most once a minute per person. A store error reads as no use
    (the ladder decides), is not kept and answers False."""
    now = time.monotonic()
    with _use_lock:
        hit = _use_cache.get(user_sub)
        if hit and hit[0] > now:
            return hit[1], hit[2], True
    since = (datetime.now(timezone.utc) - timedelta(days=USE_WINDOW_DAYS)).isoformat()
    try:
        use = engine_use.own_chat_counts(user_sub, since)
        first_shown = user_ui_prefs_store.engine_firsts(user_sub)
    except psycopg.Error:
        logger.warning("the engine use of %s could not be read; the order goes without it",
                       user_sub[:8], exc_info=True)
        return {}, {}, False
    with _use_lock:
        if len(_use_cache) >= _USE_CACHE_PRUNE_AT:
            for key in [k for k, v in _use_cache.items() if v[0] <= now]:
                del _use_cache[key]
        _use_cache[user_sub] = (now + _USE_TTL, use, first_shown)
    return use, first_shown, True


def remember_firsts(user_sub: str, firsts: dict[str, str]) -> None:
    """Keep the first engine each agent's order showed the person (only the
    changed ones are passed), and the kept read with it, so the next fetch
    of the minute does not write again. A failed write is logged: the next
    fetch tries again."""
    if not user_sub or not firsts:
        return
    try:
        user_ui_prefs_store.remember_engine_firsts(user_sub, firsts)
    except psycopg.Error:
        logger.warning("the engine order of %s could not be remembered", user_sub[:8], exc_info=True)
        return
    with _use_lock:
        hit = _use_cache.get(user_sub)
        if hit:
            _use_cache[user_sub] = (hit[0], hit[1], {**hit[2], **firsts})


def _everywhere(use: dict[str, dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for counts in use.values():
        for path, n in counts.items():
            total[path] = total.get(path, 0) + n
    return total


def person_order(agent_slug: str, listed: list[str], holdings: Holdings) -> tuple[tuple[str, ...], str]:
    """``listed`` (the engines the person may run on the agent, in the
    allowlist's order) in the person's order, and what decided its first
    place: ``REASON_USE`` (their chats), ``REASON_SEED`` (a stored
    preferred engine, at the cold start alone: the person can no longer see
    or change it, so it never outranks a chat) or ``""`` (no chat anywhere
    on a listed engine: the connection order, and the ladder picks). Pure."""
    if not listed:
        return (), ""
    here = holdings.use.get(agent_slug, {})
    everywhere = _everywhere(holdings.use)
    index = {path: i for i, path in enumerate(listed)}
    ranked = sorted(listed, key=lambda p: (
        -here.get(p, 0), -everywhere.get(p, 0),
        holdings.connected.get(p) or _NEVER_CONNECTED, index[p]))

    def _first(path: str) -> tuple[str, ...]:
        return (path, *(p for p in ranked if p != path))

    def _total(counts: dict[str, int]) -> int:
        return sum(counts.get(p, 0) for p in listed)

    if not _total(everywhere) and holdings.seed in index:
        return _first(holdings.seed), REASON_SEED
    counts = here if _total(here) else everywhere
    total = _total(counts)
    if not total:
        return tuple(ranked), ""
    top = ranked[0]
    need, out_of = _CLEAR_LEAD
    incumbent = holdings.first_shown.get(agent_slug, "")
    if counts.get(top, 0) * out_of >= total * need or incumbent not in index or not counts.get(incumbent, 0):
        return tuple(ranked), REASON_USE
    return _first(incumbent), REASON_USE


# --- the table ----------------------------------------------------------------

def _candidate(agent_row: dict, path: str, holdings: Holdings, catalog: EngineCatalog,
               *, awake: bool = False) -> Candidate | None:
    """The model a session of this person takes on ``path``, or None when the
    person holds no provider that serves a model there (on a local-only
    engine: a local provider's model; with ``awake``, a provider whose every
    account rests does not serve)."""
    rows = catalog.models.get(path, ())
    local = catalog.local_providers.get(path, frozenset())
    local_only = path in _get_local_only_engines(agent_row)
    own = holdings.own.get(path, frozenset())
    pooled = holdings.pool.get(path, frozenset())
    resting = holdings.resting.get(path, frozenset()) if awake else frozenset()

    def _ok(row: ModelRow) -> str:
        if local_only and row.provider not in local:
            return ""
        if row.provider in resting:
            return ""
        if row.provider in own:
            return SOURCE_OWN
        if row.provider in pooled:
            return SOURCE_PLATFORM
        return ""

    import config as app_config
    pinned = app_config.live_model_id(str(agent_row.get("default_model") or ""))
    if pinned:
        for row in rows:
            if row.model_id == pinned:
                source = _ok(row)
                if source:
                    return Candidate(path, row.model_id, source)
                break
    best: Candidate | None = None
    for row in rows:
        if not row.enabled:
            break
        source = _ok(row)
        if source == SOURCE_OWN:
            return Candidate(path, row.model_id, source)
        if source == SOURCE_PLATFORM and best is None:
            best = Candidate(path, row.model_id, source)
    return best


def resolve_for_person(agent_row: dict, holdings: Holdings, catalog: EngineCatalog) -> Resolution:
    """The order above, over the agent's allowlist, pure. ``allowed`` and
    ``models_for_me`` count every held engine, a resting one included, and
    ``allowed`` is in the person's order; the pick runs over the awake ones,
    and over all of them when none is awake."""
    order = _get_execution_paths(agent_row)
    held: dict[str, Candidate] = {}
    awake: dict[str, Candidate] = {}
    for path in order:
        found = _candidate(agent_row, path, holdings, catalog)
        if found is not None:
            held[path] = found
            ready = _candidate(agent_row, path, holdings, catalog, awake=True)
            if ready is not None:
                awake[path] = ready
    slug = str(agent_row.get("slug") or "")
    allowed, why = person_order(slug, [path for path in order if path in held], holdings)
    models = {path: (awake.get(path) or held[path]).model for path in allowed}
    candidates = awake or held
    first = allowed[0] if why else ""
    is_new = bool(first) and first != holdings.first_shown.get(slug, "")

    def _answer(path: str, reason: str) -> Resolution:
        return Resolution(path, candidates[path].model, reason, tuple(order), allowed, models,
                          first, is_new)

    if why:
        for path in allowed:
            if path in candidates:
                return _answer(path, why)
    default = str(agent_row.get("execution_path") or DEFAULT_EXECUTION_PATH)
    if default in candidates:
        return _answer(default, REASON_AGENT_DEFAULT)
    for path in order:
        if path in candidates and candidates[path].source == SOURCE_OWN:
            return _answer(path, REASON_OWN)
    for path in order:
        if path in candidates:
            return _answer(path, REASON_PLATFORM)
    return Resolution("", "", REASON_NONE, tuple(order), allowed, models)


def engine_for_model(agent_row: dict, model: str, *, first: str = "") -> str:
    """The engine a model pinned without its layer runs on: ``first`` (the
    engine the run takes unpinned: a lane's is the one the roster marks
    default, a task's its own, the agent's default for an agent-scope task)
    when it offers ``model`` enabled, else the first engine of the agent's
    allowlist that does; "" when none does, and the pin is refused. A
    retired id is the caller's to map to its successor first. One store
    read per engine tried."""
    from storage.billing import subscription_store
    order = _get_execution_paths(agent_row)
    tried = ([first] if first in order else []) + [p for p in order if p != first]
    for path in tried:
        if any((m.get("model_id") or "") == model and m.get("enabled")
               for m in subscription_store.list_models(path)):
            return path
    return ""


def model_allowed_on(agent_row: dict, execution_path: str, model: str, catalog: EngineCatalog) -> bool:
    """Whether ``model`` may run on ``execution_path`` for this agent under
    the local-only rule (any model elsewhere; an unknown model is left to
    the engine's own validation)."""
    if execution_path not in _get_local_only_engines(agent_row):
        return True
    local = catalog.local_providers.get(execution_path, frozenset())
    for row in catalog.models.get(execution_path, ()):
        if row.model_id == model:
            return row.provider in local
    return True


# --- the reads ---------------------------------------------------------------

def resolve_session_engine(agent_name: str, user_sub: str) -> Resolution:
    """``resolve_for_person`` over the store: the agent's row, the person's
    holdings and the catalog of the agent's engines. A person who is nobody
    (``""``) or an unknown agent resolves to nothing."""
    from storage.agents import agent_store
    agent_row = agent_store.get_agent(agent_name) or {}
    if not agent_row or not user_sub:
        return Resolution("", "", REASON_NONE, tuple(_get_execution_paths(agent_row)), (), {})
    engines = _get_execution_paths(agent_row)
    return resolve_for_person(
        agent_row, person_holdings(user_sub, engines), engine_catalog(engines),
    )


def resolve_many(agent_rows: list[dict], user_sub: str, *, remember: bool = False) -> dict[str, Resolution]:
    """One resolution per agent row for one person, the holdings and the
    catalog read once (the agents listing, the roster). ``remember``: the
    picker's fetch (the agent routes) keeps each changed first place, which
    holds the order steady until a new engine leads clearly."""
    if not user_sub:
        return {}
    engines = engine_order()
    holdings = person_holdings(user_sub, engines)
    catalog = engine_catalog(engines)
    resolved = {
        str(row.get("slug") or ""): resolve_for_person(row, holdings, catalog)
        for row in agent_rows if row.get("slug")
    }
    # A failed use read orders by the seed and the connections alone: never
    # remembered over the real firsts.
    if remember and holdings.use_read:
        remember_firsts(user_sub, {slug: r.first_shown for slug, r in resolved.items() if r.first_is_new})
    return resolved
