"""Subscription pool caps: a ceiling on how much of a POOL of OAuth accounts
the platform may use, per engine.

One cap row per pool (``subscription_pool_caps``): the platform pool (every
contributed account, ``scope='platform'``) and each user's personal pool
(their own connected accounts, ``scope='user'``). A cap is four optional
numbers and a switch: the pool's weekly window percentage and the last 24
hours' share of it (the vendor's own reading, ``subscription_windows``,
averaged over the accounts with one equal share each), and the dollars
attributed to the pool's accounts over a rolling week and day
(``usage_records`` at API list prices). Any set field applies; the first
reading at or above its cap blocks; 80 % of a cap warns. ``on_reached``
decides what a hit does at spawn time: ``stop`` acquires nothing,
``continue`` drops the OAuth accounts and lets an API key take the spawn.

The same numbers are evaluated for every engine's pool separately (a full
ChatGPT pool never blocks Claude work), and only an engine that takes a
vendor login (``oauth`` among its declared auth types) carries OAuth
accounts. A pool with no OAuth account has nothing to cap: allowed.

Every reading is cached for ``_CACHE_TTL_S`` per (scope, target, layer) —
the chat send path asks on every message — and the cap endpoints
invalidate their pool on a write. All functions are synchronous (the
callers run them under ``asyncio.to_thread``).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from services.engines import subscription_windows as sw
from storage.billing import subscription_store

logger = logging.getLogger(__name__)

CAP_FIELDS = ("week_pct", "day_pct", "week_usd", "day_usd")
ON_REACHED = ("stop", "continue")
WARN_RATIO = 0.8

_CACHE_TTL_S = 30.0
_DAY = timedelta(hours=24)
_WEEK = timedelta(days=7)


def _oauth_layers() -> list[str]:
    """The engines whose pool can hold an OAuth account — the only pools
    with something to cap — in registry order. Read from the descriptors
    per call (registry-pull; the session manager is imported here, never at
    module level, so this module stays free of the layer packages)."""
    from core.session.session_manager import get_all_layers
    return [
        path for path, layer in get_all_layers().items()
        if "oauth" in layer.capabilities.auth.auth_types
    ]


_FIELD_LABELS = {
    "week_pct": ("the week", "pct"),
    "day_pct": ("today", "pct"),
    "week_usd": ("the week", "usd"),
    "day_usd": ("today", "usd"),
}


@dataclass
class CapStatus:
    scope: str                       # platform | user
    target: str                      # '' | user_sub
    layer: str
    configured: bool = False         # a cap row with at least one field set
    caps: dict = field(default_factory=lambda: {f: None for f in CAP_FIELDS})
    readings: dict = field(default_factory=lambda: {f: None for f in CAP_FIELDS})
    on_reached: str = "stop"
    accounts: int = 0                # OAuth accounts in the pool
    hits: list[str] = field(default_factory=list)
    warning: bool = False
    allowed: bool = True

    def to_public(self) -> dict:
        """The ``pool`` payload of the usage endpoints and the WS events."""
        return {
            "scope": self.scope,
            "layer": self.layer,
            "configured": self.configured,
            "caps": dict(self.caps),
            "readings": {k: (None if v is None else round(v, 2))
                         for k, v in self.readings.items()},
            "on_reached": self.on_reached,
            "accounts": self.accounts,
            "hits": list(self.hits),
            "warning": self.warning,
            "allowed": self.allowed,
        }

    @property
    def account_name(self) -> str:
        """What the engine's subscriptions are called ("Claude", "ChatGPT");
        the layer id for an engine that is no longer registered."""
        from core.session.session_manager import account_label_for
        return account_label_for(self.layer, self.layer)

    def _describe(self, key: str) -> str:
        period, unit = _FIELD_LABELS[key]
        cap, reading = self.caps.get(key), self.readings.get(key)
        if unit == "pct":
            return f"{period} is at {reading:.0f}% of the {cap:g}% cap"
        return f"{period} is at ${reading:.2f} of the ${cap:g} cap"

    def hit_text(self) -> str:
        """The first reading past its cap, e.g. "the week is at 52% of the 50% cap"."""
        return self._describe(self.hits[0]) if self.hits else ""

    def blocked_message(self, *, no_key: bool = False) -> str:
        """The user-facing reason a spawn was refused (``NoSubscriptionError``)."""
        if self.scope == "user":
            whose, where, keys = "Your", "User Settings → Usage", "User Settings → AI Engines"
        else:
            whose, where, keys = "The agent pool's", "Setup → Usage", "Setup → AI Engines"
        head = f"{whose} {self.account_name} subscription cap is reached: {self.hit_text()}."
        if no_key:
            return (f"{head} There is no API key to continue on. Connect one in "
                    f"{keys}, or change the cap in {where}.")
        return f"{head} It clears as the accounts' windows reset; change the cap in {where}."

    def short_reason(self) -> str:
        """One line for run records and meeting summaries."""
        return f"Subscription pool cap reached ({self.hit_text()})"


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

def _pool_accounts(scope: str, target: str, layer: str) -> list[dict]:
    if scope == "platform":
        rows = subscription_store.list_platform_pool(layer)
    else:
        rows = subscription_store.list_personal(layer, target)
    return [r for r in rows if r.get("auth_type") == "oauth"]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _quota_key(layer: str) -> str:
    """The key of the engine's declared quota window ("" when it declares
    none) — the window the ``week_pct`` / ``day_pct`` caps read. The cap
    columns are named for the week because both engines' quota windows are
    weekly; a non-weekly quota would still be read here, under those names."""
    spec = sw.quota_spec(sw.window_specs(layer))
    return spec.key if spec else ""


def _week_pct(accounts: list[dict], now: datetime) -> float | None:
    """Mean of the accounts' effective quota-window percentage; None when
    no account has a reading."""
    readings = sw.latest(accounts, now)
    values = []
    for r in readings.values():
        win = r.windows.get(r.quota_key)
        if win is not None:
            values.append(win.pct)
    return _mean(values)


def _day_pct_one(sub_id: str, since: str, key: str) -> float | None:
    """What one account consumed of its quota window in the last day: the
    positive steps of that window's percentage across its samples (a reset
    is a negative step, ignored, so a window that turned over mid-day counts
    what was used on both sides of it). None without any sample, 0 with one."""
    rows = subscription_store.window_samples_since(sub_id, since)
    pcts = [w.pct for row in rows if (w := sw.sample_window(row, key)) is not None]
    if not pcts:
        return None
    return sum(max(b - a, 0.0) for a, b in zip(pcts, pcts[1:]))


def _day_pct(accounts: list[dict], now: datetime, key: str) -> float | None:
    since = (now - _DAY).isoformat()
    per_account = [_day_pct_one(a["id"], since, key) for a in accounts]
    return _mean([v for v in per_account if v is not None])


def _dollars(sub_ids: list[str], since: datetime, scope: str, target: str) -> float:
    if scope == "platform":
        return subscription_store.get_pool_consumption(sub_ids, since.isoformat(), scope="agent")
    return subscription_store.get_pool_consumption(
        sub_ids, since.isoformat(), scope="user", user_sub=target)


def _read_cap(scope: str, target: str) -> dict | None:
    row = subscription_store.get_pool_cap(scope, target)
    if not row or all(row.get(f) is None for f in CAP_FIELDS):
        return None
    return row


def _evaluate(scope: str, target: str, layer: str, now: datetime) -> CapStatus:
    status = CapStatus(scope=scope, target=target, layer=layer)
    cap = _read_cap(scope, target)
    if cap is not None:
        status.configured = True
        status.caps = {f: cap.get(f) for f in CAP_FIELDS}
        status.on_reached = cap.get("on_reached") if cap.get("on_reached") in ON_REACHED else "stop"
    if layer not in _oauth_layers():
        return status
    accounts = _pool_accounts(scope, target, layer)
    status.accounts = len(accounts)
    if not accounts:
        return status
    ids = [a["id"] for a in accounts]
    if sw.is_enabled():
        status.readings["week_pct"] = _week_pct(accounts, now)
        quota = _quota_key(layer)
        status.readings["day_pct"] = _day_pct(accounts, now, quota) if quota else None
    status.readings["week_usd"] = _dollars(ids, now - _WEEK, scope, target)
    status.readings["day_usd"] = _dollars(ids, now - _DAY, scope, target)
    if not status.configured:
        return status
    for key in CAP_FIELDS:
        cap_value, reading = status.caps.get(key), status.readings.get(key)
        if cap_value is None or reading is None or cap_value <= 0:
            continue
        if reading >= cap_value:
            status.hits.append(key)
        elif reading >= cap_value * WARN_RATIO:
            status.warning = True
    if status.hits:
        status.warning = True
        status.allowed = False
    return status


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, CapStatus]] = {}


def evaluate(scope: str, target: str, layer: str, *, now: datetime | None = None) -> CapStatus:
    """The pool's readings against its cap for one engine. ``scope`` is
    ``platform`` (``target`` ignored) or ``user`` (``target`` the user's sub).
    Served from a 30 s cache; pass ``now`` only in tests (uncached)."""
    target = target if scope == "user" else ""
    key = (scope, target, layer)
    if now is None:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
                return hit[1]
        status = _evaluate(scope, target, layer, datetime.now(timezone.utc))
        with _cache_lock:
            _cache[key] = (time.monotonic(), status)
        return status
    return _evaluate(scope, target, layer, now)


def evaluate_engines(scope: str, target: str) -> dict[str, dict]:
    """The ``engines`` map of the usage endpoints: every login-taking engine
    whose pool holds an OAuth account, keyed by layer."""
    out: dict[str, dict] = {}
    for layer in _oauth_layers():
        status = evaluate(scope, target, layer)
        if status.accounts:
            out[layer] = status.to_public()
    return out


def invalidate(scope: str, target: str = "") -> None:
    """Drop the cached readings of one pool (every engine)."""
    target = target if scope == "user" else ""
    with _cache_lock:
        for key in [k for k in _cache if k[0] == scope and k[1] == target]:
            _cache.pop(key, None)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
