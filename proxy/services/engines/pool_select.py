"""The selection's order: vendor readings, out-of-window checks, drain-first and least-consumed sort.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime

import psycopg

from core.execution_layer import SubscriptionHandle
from services.billing import pool_caps as _pool_caps
from services.engines import subscription_windows as _windows
from services.engines.pool_rests import (
    _is_throttled,
    _rest_end,
    _scope_resting,
)
from services.engines.pool_state import _session_models
from services.engines.pool_tokens import (
    _build_handle,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)


# Headroom routing: consumption within these rolling windows approximates each
# account's remaining headroom, so a new chat lands on the least-consumed one.
# Two tiers, matched to how the providers actually reset (Claude consumer subs:
# a ~5h rolling window + weekly caps): the SHORT window is the primary key —
# right after everyone's reset all accounts tie at ~0 and the pool spreads by
# real recent burn — and the 7-day window breaks ties so weekly caps are still
# respected. A single long window remembers last week and keeps routing away
# from an account whose real headroom already reset (live-observed).
_CONSUMPTION_WINDOW_HOURS = 5


_CONSUMPTION_WINDOW_DAYS = 7


def _consumption_window_starts() -> tuple[str, str]:
    """ISO start timestamps for the (short, long) consumption windows."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return (
        (now - timedelta(hours=_CONSUMPTION_WINDOW_HOURS)).isoformat(),
        (now - timedelta(days=_CONSUMPTION_WINDOW_DAYS)).isoformat(),
    )


def _window_readings(candidates: list[dict], now=None) -> dict:
    """The effective provider-window reading per OAuth candidate that has a
    sample (``services.engines.subscription_windows``); empty when the
    platform setting is off or nothing has been read yet. ``candidates`` are
    subscription ROWS — the row's ``layer`` names the windows its engine's
    vendor reports, which the reading is settled against."""
    if not _windows.is_enabled():
        return {}
    oauth = [c for c in candidates if c.get("auth_type") == "oauth" and c.get("id")]
    if not oauth:
        return {}
    try:
        readings = _windows.latest_readings(subscription_store, oauth, now)
    except (psycopg.Error, ValueError):
        logger.debug("Pool: window sample read failed", exc_info=True)
        return {}
    return readings if isinstance(readings, dict) else {}


def _scope_key(layer: str | None, model: str) -> str:
    """The per-model window a spawn of ``model`` counts against on ``layer``
    — the engine's ``usage_scope_key`` (Claude: the model family); "" for no
    model, no per-model windows, or an engine that is not registered."""
    if not model or not layer:
        return ""
    from core.session.session_manager import get_all_layers
    engine = get_all_layers().get(layer)
    return engine.usage_scope_key(model) if engine is not None else ""


def _window_exhausted_for(row: dict, model: str) -> bool:
    """Is the account out of window for a spawn of ``model`` — overall, or
    its per-model quota window (``""`` = overall only)? ``row`` is the
    subscription row (``id``, ``layer``, ``auth_type``); a non-OAuth row has
    no windows and is never exhausted."""
    reading = _window_readings([row]).get(row.get("id"))
    return reading is not None and _windows.exhausted(
        reading, _scope_key(row.get("layer"), model))


def _out_for(row: dict, model: str) -> bool:
    """Out of service for a spawn of ``model``: its windows exhausted for it,
    or resting for the model's scope after a usage limit on that family's
    own window (``rest_after_limit``)."""
    return (_window_exhausted_for(row, model)
            or _scope_resting(str(row.get("id") or ""), _scope_key(row.get("layer"), model)))


@dataclasses.dataclass(frozen=True)
class _ScopeFacts:
    """What the pool judges a group of sessions by. ``models``: every model
    they run ("" for one the pool cannot name), read per session.
    ``chats``: session id to chat id, for the sessions that have a chat.
    ``last_active``: the newest activity of those chats (epoch seconds), None
    when no chat says or a session of the group has no chat (a call, a
    meeting: unknown is never idle). ``turn_open``: a turn is open on one of
    the chats (``chat_move.open_turn``), which the chat's activity alone does
    not show (a card waiting on a person, a long tool)."""
    models: frozenset[str]
    chats: dict[str, str]
    last_active: float | None
    turn_open: bool = False


def _scope_facts(session_ids: list[str]) -> _ScopeFacts:
    """Each session's model, from its chat row (a model picked mid-life by
    any door lands there), else the model it was bound with
    (``bind_session``): never one model for a whole group, since each chat
    of one folder picks its own."""
    from services.remote import chat_move
    from storage import database as task_store
    models: set[str] = set()
    chats: dict[str, str] = {}
    newest: float | None = None
    chatless = turn_open = False
    for sid in session_ids:
        try:
            row = task_store.get_chat_by_session(sid)
        except psycopg.Error:
            row = None
        models.add(str((row or {}).get("model") or "") or _session_models.get(sid, ""))
        if not (row and row.get("id")):
            chatless = True
            continue
        chats[sid] = str(row["id"])
        try:
            turn_open = turn_open or chat_move.open_turn(row)
        except Exception:  # noqa: BLE001  # a turn the pass cannot read is treated as open: the return waits
            logger.debug("Pool: the open turn of chat %s could not be read", str(row["id"])[:8], exc_info=True)
            turn_open = True
        try:
            at = datetime.fromisoformat(str(row.get("updated_at") or "")).timestamp()
        except ValueError:
            continue
        newest = at if newest is None else max(newest, at)
    return _ScopeFacts(frozenset(models or {""}), chats, None if chatless else newest, turn_open)


_FAR_FUTURE = float("inf")


def _epoch_or_inf(dt) -> float:
    return dt.timestamp() if dt is not None else _FAR_FUTURE


def _consumption_per_row(ids: list[str], since_short: str, since_long: str) -> dict[str, tuple[float, float]]:
    """The spawn's read of the two consumption windows: two sums per account."""
    return {i: (subscription_store.get_subscription_consumption(i, since_short),
                subscription_store.get_subscription_consumption(i, since_long)) for i in ids}


def _ranked(
    candidates: list[dict],
    *,
    allowed_auth: frozenset[str] | None,
    model: str,
    consumption: Callable[[list[str], str, str], dict[str, tuple[float, float]]],
    rank_waiting: bool = False,
) -> tuple[list[dict], list[dict], set[str]]:
    """The selection's own order, read by the spawn (``_select``) and by the
    lists of accounts (``display_order``): ``(ready, waiting, exhausted)``.

    ``ready``: the candidates the spawn may pick now, on the selection's key
    (below). ``waiting``: the rest, resting after a provider limit or out of
    window for ``model``, by when each frees. ``exhausted``: the ids out of
    window. ``consumption(ids, since_short, since_long)`` answers the two
    consumption sums per id; it is read only when two or more rows compete.
    ``waiting`` is ordered only when nothing is ready or ``rank_waiting``
    asks (a list of accounts): the spawn reads no sum it does not use.

    The key, drain first: a real BYO credential always beats the hosted
    relay (credit cost + latency); within that, the account whose WEEKLY
    window resets soonest comes first (quota left in a window when it closes
    is lost, so the account that closes first is used up first; the vendor's
    own reading, see ``subscription_windows``), accounts without a reading
    after every account with one (a subscription drains before a
    pay-per-token key), and equal reset instants fall back to the
    LEAST-CONSUMED key — recent burn (~5h) first, the 7-day total as tiebreak —
    so accounts that reset together still spread. The store's least-active
    order breaks remaining ties (stable sort)."""
    auth_ok = [
        c for c in candidates
        if allowed_auth is None or c.get("auth_type") in allowed_auth
    ]
    # Read the samples for ONE candidate too: the sticky path selects from a
    # single-row list and the exhaustion check must still see it.
    readings = _window_readings(auth_ok)
    exhausted = {
        c["id"] for c in auth_ok
        if c["id"] in readings
        and _windows.exhausted(readings[c["id"]], _scope_key(c.get("layer"), model))
    }
    resting = {
        c["id"] for c in auth_ok
        if _is_throttled(c["id"])
        or _scope_resting(c["id"], _scope_key(c.get("layer"), model))
    }
    ready = [c for c in auth_ok if c["id"] not in resting and c["id"] not in exhausted]
    waiting = [c for c in auth_ok if c["id"] in resting or c["id"] in exhausted]
    since_short, since_long = _consumption_window_starts()

    def _sorted(group: list[dict], *, fallback: bool) -> list[dict]:
        if len(group) <= 1:
            return sorted(group, key=lambda s: s.get("auth_type") == "relay")
        used = consumption([c["id"] for c in group], since_short, since_long)

        def _instant(s: dict) -> float:
            reading = readings.get(s["id"])
            if fallback:
                # When the account frees: the end of its rest and the reset
                # of the windows that exhaust it, whichever comes later.
                scope = _scope_key(s.get("layer"), model)
                known = [end for end in (_rest_end(s["id"], scope),) if end is not None]
                if reading is not None:
                    frees = _windows.frees_at(reading, scope)
                    if frees is not None:
                        known.append(frees.timestamp())
                return max(known) if known else _FAR_FUTURE
            if reading is None:
                return _FAR_FUTURE
            return _epoch_or_inf(_windows.quota_reset(reading))

        return sorted(group, key=lambda s: (s.get("auth_type") == "relay", _instant(s),
                                            *used.get(s["id"], (0.0, 0.0))))

    if not ready or rank_waiting:
        waiting = _sorted(waiting, fallback=True)
    return _sorted(ready, fallback=False), waiting, exhausted


def display_order(rows: list[dict], *, sessions: dict[str, int],
                  kept_out: Callable[[dict], bool]) -> list[dict]:
    """A list of accounts as the pool uses them (both AI Engines lists): the
    ones with sessions on them first, then what the selection would pick now,
    then what waits for a rest or a window, all in the selection's own order
    (``_ranked``), and last the ones the list's owner keeps out (``kept_out``:
    not active, or not used for this list's work), in the store's order. Kept
    out wins over in use: an API key unticked keeps its sessions. Reads the
    two consumption sums in one query."""
    out = [r for r in rows if kept_out(r)]
    ready, waiting, _ = _ranked([r for r in rows if not kept_out(r)], allowed_auth=None, model="",
                                consumption=subscription_store.consumption_of, rank_waiting=True)
    ranked = ready + waiting
    in_use = [r for r in ranked if sessions.get(r["id"], 0) > 0]
    return in_use + [r for r in ranked if sessions.get(r["id"], 0) <= 0] + out


def _select(
    candidates: list[dict],
    *,
    allowed_auth: frozenset[str] | None,
    model: str = "",
) -> SubscriptionHandle | None:
    """Try candidates in priority order, claiming the first that yields usable
    credentials. ``candidates`` is already ordered (least-active first) by the
    store; here we additionally push hosted-relay subs last (a real
    BYO credential always wins over the relay's credit cost + latency hop).

    ``allowed_auth=None`` → no auth-type restriction (agent-scope / the owner's own
    accounts). When a restriction is given (a user borrowing the admin pool) it is
    enforced HERE, on the same list the usable-credential check reads, so an
    excluded auth type (notably ``oauth``) can never slip through a later branch.

    Ordering: the selection's own key (``_ranked``). Subscriptions currently
    resting (a recent provider limit, account-wide or for this spawn's model
    scope: ``rest_after_limit``) or EXHAUSTED for this spawn's ``model`` (a
    window past its spill mark, the vendor's reached flag, or the model
    family's own weekly window) are skipped so the spawn fails over to an
    account with headroom.
    """
    ready, waiting, exhausted = _ranked(candidates, allowed_auth=allowed_auth, model=model,
                                        consumption=_consumption_per_row)
    pool = ready or waiting
    if not ready and exhausted:
        logger.warning(
            "Pool: every candidate is out of window for %s "
            "(%s) — using the one "
            "that frees first; the provider may refuse until it resets",
            model or 'this spawn', ', '.join(sorted(s[:8] for s in exhausted))
        )
    for chosen in pool:
        handle = _build_handle(chosen)
        # A relay sub carries no stored credential — its token is minted per
        # session/user at resolve time (resolve_subscription_env / change_model).
        if handle.api_key or handle.oauth_access_token or handle.endpoint_url or handle.auth_type == "relay":
            subscription_store.increment_active_sessions(chosen["id"])
            logger.info(
                "Pool: acquired subscription %s for layer=%s "
                "(provider=%s, auth=%s, "
                "sessions=%s)",
                chosen['id'][:8], chosen['layer'], chosen['provider'], handle.auth_type,
                chosen['active_sessions'] + 1
            )
            return handle
        logger.warning(
            "Pool: subscription %s has no usable credentials, trying next",
            chosen['id'][:8]
        )
    return None


def _cap_status(layer: str, user_sub: str | None):
    """The pool cap this spawn is subject to (``services.billing.pool_caps``):
    the user's own pool for user scope, the platform pool for agent scope.
    None when the evaluation itself fails — a cap read must never take a
    spawn down with it."""
    try:
        if user_sub:
            return _pool_caps.evaluate("user", user_sub, layer)
        return _pool_caps.evaluate("platform", "", layer)
    except Exception:  # noqa: BLE001  # a failed cap evaluation lets the spawn proceed uncapped
        logger.warning("Pool: cap evaluation failed, spawn proceeds uncapped", exc_info=True)
        return None


def _rows_without_login(candidates: list[dict]) -> list[dict]:
    return [c for c in candidates if c.get("auth_type") != "oauth"]
