"""Provider windows: an OAuth account's real 5-hour and weekly window state.

Claude and ChatGPT both cap a consumer subscription with a rolling session
window and a rolling weekly window, and both report the state of those
windows to their own CLI: Claude on a usage endpoint and in the headless
stream's ``rate_limit_event``, Codex on a usage endpoint, in the app-server's
``account/rateLimits/updated`` and next to the rollout's token counts. This
module turns every one of those shapes into one ``Windows`` reading, stores
it as a sample (``subscription_store.insert_window_sample``), and answers the
questions the pool asks of the latest sample: is this account exhausted for
this spawn, and when does its weekly window reset. The vendor's reading is
the account's whole truth, the owner's own chats and their CLI on a laptop
included, which is exactly why the pool prefers it to our own cost estimate.

Nothing here touches the network; ``token_fanout`` polls and the layers
record.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("claude-proxy.subscription-windows")

SETTING_KEY = "subscription_windows_enabled"
_SETTING_CACHE_S = 60

FIVE_HOUR_S = 5 * 3600
SEVEN_DAY_S = 7 * 86400

# An account is skipped for new work past these; the vendor's hard stop is
# 100, the margin keeps a live session's next few turns from hitting the wall.
SPILL_5H = 90.0
SPILL_7D = 95.0

# Vendors name their windows differently; both are identified by length.
_FIVE_HOUR_MAX_S = 6 * 3600

_FAMILIES = ("fable", "opus", "sonnet", "haiku")


@dataclass
class Window:
    pct: float                       # 0..100, above 100 when the vendor says so
    resets_at: datetime | None       # aware UTC; None when the vendor gave none


@dataclass
class Scoped:
    key: str                         # model family: fable | opus | sonnet | …
    label: str                       # the vendor's display name
    pct: float
    resets_at: datetime | None
    active: bool = False             # the vendor says this window is limiting now


@dataclass
class Windows:
    five_hour: Window | None = None
    seven_day: Window | None = None
    scoped: list[Scoped] = field(default_factory=list)
    reached: str = ""                # "" | five_hour | seven_day | scoped:<key>
    plan: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = ""

    def scoped_for(self, key: str) -> Scoped | None:
        for s in self.scoped:
            if s.key == key:
                return s
        return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _iso_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _epoch_dt(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _any_dt(value) -> datetime | None:
    return _epoch_dt(value) if isinstance(value, (int, float)) else _iso_dt(value)


def _pct(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _pick(d: dict, *names):
    """First present key among camelCase and snake_case spellings."""
    for n in names:
        if n in d:
            return d[n]
    return None


def model_family(model: str) -> str:
    """``claude-fable-5-1`` → ``fable``; a name with no known family → ``""``."""
    m = (model or "").lower()
    for fam in _FAMILIES:
        if fam in m:
            return fam
    return ""


def _scoped_key(label: str) -> str:
    fam = model_family(label)
    if fam:
        return fam
    return "".join(ch if ch.isalnum() else "-" for ch in (label or "").lower()).strip("-")


def _dt_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Normalizers — each returns None on a shape it does not recognise
# ---------------------------------------------------------------------------

def from_claude_usage(payload: dict) -> Windows | None:
    """``GET /api/oauth/usage``: percents and ISO reset instants; per-model
    weekly windows both as ``seven_day_<family>`` fields and as
    ``limits[]`` entries (which also carry ``is_active``)."""
    if not isinstance(payload, dict):
        return None
    if "five_hour" not in payload and "seven_day" not in payload:
        return None
    w = Windows(source="poll")

    def window(d) -> Window | None:
        if not isinstance(d, dict):
            return None
        pct = _pct(d.get("utilization"))
        if pct is None:
            return None
        return Window(pct=pct, resets_at=_iso_dt(d.get("resets_at")))

    w.five_hour = window(payload.get("five_hour"))
    w.seven_day = window(payload.get("seven_day"))
    scoped: dict[str, Scoped] = {}
    for fam in ("opus", "sonnet"):
        win = window(payload.get(f"seven_day_{fam}"))
        if win is not None:
            scoped[fam] = Scoped(key=fam, label=fam.capitalize(), pct=win.pct,
                                 resets_at=win.resets_at)
    # ``limits[]``: ``is_active`` is the vendor's "this limit is the one to
    # watch" (seen true on a session window at 85 %), NOT "refused" — the
    # vendor refuses at 100 %. ``reached`` therefore comes from the percent
    # alone; ``is_active`` survives as the scoped window's ``active`` flag,
    # which the pool treats as a spill mark (steer new work elsewhere).
    limits = payload.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            active = bool(entry.get("is_active"))
            pct = _pct(entry.get("percent"))
            full = pct is not None and pct >= 100
            if kind == "weekly_scoped":
                scope = entry.get("scope") or {}
                model = scope.get("model") if isinstance(scope, dict) else None
                label = (model or {}).get("display_name") if isinstance(model, dict) else None
                if not label:
                    continue
                key = _scoped_key(str(label))
                scoped[key] = Scoped(
                    key=key, label=str(label),
                    pct=pct if pct is not None else scoped.get(key, Scoped(key, label, 0.0, None)).pct,
                    resets_at=_iso_dt(entry.get("resets_at")), active=active,
                )
                if full and not w.reached:
                    w.reached = f"scoped:{key}"
            elif kind == "session" and full and not w.reached:
                w.reached = "five_hour"
            elif kind == "weekly_all" and full and not w.reached:
                w.reached = "seven_day"
    if not w.reached:
        if w.five_hour is not None and w.five_hour.pct >= 100:
            w.reached = "five_hour"
        elif w.seven_day is not None and w.seven_day.pct >= 100:
            w.reached = "seven_day"
    w.scoped = list(scoped.values())
    if w.five_hour is None and w.seven_day is None and not w.scoped:
        return None
    return w


_EVENT_TYPE_TO_KEY = {
    "five_hour": "five_hour",
    "seven_day": "seven_day",
    "seven_day_opus": "scoped:opus",
    "seven_day_sonnet": "scoped:sonnet",
}


def from_claude_event(info: dict) -> Windows | None:
    """The headless stream's ``rate_limit_event.rate_limit_info``: fractions
    (0..1, above 1 when usage ran past a cap) and epoch reset instants."""
    if not isinstance(info, dict):
        return None
    unified = info.get("unifiedWindows")
    if not isinstance(unified, dict):
        return None
    w = Windows(source="claude_event")

    def window(d) -> Window | None:
        if not isinstance(d, dict):
            return None
        frac = _pct(d.get("utilization"))
        if frac is None:
            return None
        return Window(pct=frac * 100.0, resets_at=_epoch_dt(d.get("resetsAt")))

    w.five_hour = window(unified.get("five_hour"))
    w.seven_day = window(unified.get("seven_day"))
    if w.five_hour is None and w.seven_day is None:
        return None
    if info.get("status") == "rejected":
        w.reached = _EVENT_TYPE_TO_KEY.get(str(info.get("rateLimitType") or ""), "")
    return w


def _codex_window_key(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return "five_hour" if seconds <= _FIVE_HOUR_MAX_S else "seven_day"


def from_codex_usage(payload: dict) -> Windows | None:
    """``GET /backend-api/wham/usage``: percents, window lengths in seconds,
    epoch reset instants; the windows are told apart by their length."""
    if not isinstance(payload, dict):
        return None
    rl = payload.get("rate_limit")
    if not isinstance(rl, dict):
        return None
    w = Windows(source="poll", plan=str(payload.get("plan_type") or ""))
    reached_type = str(rl.get("rate_limit_reached_type") or payload.get("rate_limit_reached_type") or "")
    slot_keys: dict[str, str] = {}
    for slot in ("primary_window", "secondary_window"):
        d = rl.get(slot)
        if not isinstance(d, dict):
            continue
        pct = _pct(d.get("used_percent"))
        if pct is None:
            continue
        key = _codex_window_key(_pct(d.get("limit_window_seconds")))
        if not key:
            continue
        resets = _epoch_dt(d.get("reset_at"))
        if resets is None:
            after = _pct(d.get("reset_after_seconds"))
            if after is not None:
                resets = w.observed_at + timedelta(seconds=after)
        setattr(w, key, Window(pct=pct, resets_at=resets))
        slot_keys[slot.split("_")[0]] = key
    if w.five_hour is None and w.seven_day is None:
        return None
    if rl.get("limit_reached"):
        w.reached = slot_keys.get(reached_type, "") or _fullest(w)
    return w


def from_codex_snapshot(rl: dict) -> Windows | None:
    """The app-server ``account/rateLimits/updated`` params (camelCase:
    ``{rateLimits: {primary, secondary: {usedPercent, windowDurationMins,
    resetsAt}, planType, rateLimitReachedType}}``, the ``account/rateLimits/
    read`` result carries the same ``rateLimits``) and the rollout
    ``token_count.rate_limits`` (snake_case): percents, window lengths in
    minutes, epoch reset instants."""
    if not isinstance(rl, dict):
        return None
    w = Windows(source="codex_event",
                plan=str(_pick(rl, "planType", "plan_type") or ""))
    reached_type = str(_pick(rl, "rateLimitReachedType", "rate_limit_reached_type") or "")
    slot_keys: dict[str, str] = {}
    for slot in ("primary", "secondary"):
        d = rl.get(slot)
        if not isinstance(d, dict):
            continue
        pct = _pct(_pick(d, "usedPercent", "used_percent"))
        if pct is None:
            continue
        # The app-server names the length ``windowDurationMins`` (verified on
        # 0.153.4), the rollout ``window_minutes``.
        minutes = _pct(_pick(d, "windowDurationMins", "windowMinutes", "window_minutes"))
        key = _codex_window_key(minutes * 60 if minutes is not None else None)
        if not key:
            continue
        setattr(w, key, Window(pct=pct, resets_at=_any_dt(_pick(d, "resetsAt", "resets_at"))))
        slot_keys[slot] = key
    if w.five_hour is None and w.seven_day is None:
        return None
    if reached_type:
        w.reached = slot_keys.get(reached_type, "") or _fullest(w)
    return w


def _fullest(w: Windows) -> str:
    best, best_pct = "", -1.0
    for key in ("five_hour", "seven_day"):
        win = getattr(w, key)
        if win is not None and win.pct > best_pct:
            best, best_pct = key, win.pct
    return best


# ---------------------------------------------------------------------------
# Store rows ⇄ readings
# ---------------------------------------------------------------------------

def to_row(w: Windows) -> dict:
    """The keyword arguments ``subscription_store.insert_window_sample`` takes."""
    return {
        "observed_at": w.observed_at.isoformat(),
        "source": w.source,
        "five_hour_pct": w.five_hour.pct if w.five_hour else None,
        "five_hour_resets_at": _dt_iso(w.five_hour.resets_at) if w.five_hour else None,
        "seven_day_pct": w.seven_day.pct if w.seven_day else None,
        "seven_day_resets_at": _dt_iso(w.seven_day.resets_at) if w.seven_day else None,
        "data": {
            "scoped": [
                {"key": s.key, "label": s.label, "pct": s.pct,
                 "resets_at": _dt_iso(s.resets_at), "active": s.active}
                for s in w.scoped
            ],
            "reached": w.reached,
            "plan": w.plan,
        },
    }


def from_row(row: dict) -> Windows:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    w = Windows(
        observed_at=_iso_dt(row.get("observed_at")) or datetime.now(timezone.utc),
        source=str(row.get("source") or ""),
        reached=str(data.get("reached") or ""),
        plan=str(data.get("plan") or ""),
    )
    if row.get("five_hour_pct") is not None:
        w.five_hour = Window(float(row["five_hour_pct"]), _iso_dt(row.get("five_hour_resets_at")))
    if row.get("seven_day_pct") is not None:
        w.seven_day = Window(float(row["seven_day_pct"]), _iso_dt(row.get("seven_day_resets_at")))
    for s in data.get("scoped") or []:
        if not isinstance(s, dict) or not s.get("key"):
            continue
        pct = _pct(s.get("pct"))
        if pct is None:
            continue
        w.scoped.append(Scoped(key=str(s["key"]), label=str(s.get("label") or s["key"]),
                               pct=pct, resets_at=_iso_dt(s.get("resets_at")),
                               active=bool(s.get("active"))))
    return w


def effective(w: Windows, now: datetime | None = None) -> Windows:
    """What the sample says about NOW. A window whose reset instant has
    passed is empty (and no longer ``reached``); a window with usage but no
    reset instant is unknown; a sample older than the window's own length is
    unknown for that window."""
    now = now or datetime.now(timezone.utc)
    age = (now - w.observed_at).total_seconds()
    out = Windows(observed_at=w.observed_at, source=w.source, plan=w.plan, reached=w.reached)

    def settle(win: Window | None, length: float) -> Window | None:
        if win is None or age > length:
            return None
        if win.resets_at is not None and win.resets_at <= now:
            return Window(pct=0.0, resets_at=None)
        if win.pct > 0 and win.resets_at is None:
            return None
        return win

    out.five_hour = settle(w.five_hour, FIVE_HOUR_S)
    out.seven_day = settle(w.seven_day, SEVEN_DAY_S)
    for key in ("five_hour", "seven_day"):
        src, dst = getattr(w, key), getattr(out, key)
        if out.reached == key and (dst is None or (src and src.resets_at and src.resets_at <= now)):
            out.reached = ""
    for s in w.scoped:
        settled = settle(Window(s.pct, s.resets_at), SEVEN_DAY_S)
        if settled is None:
            if out.reached == f"scoped:{s.key}":
                out.reached = ""
            continue
        reset_passed = s.resets_at is not None and s.resets_at <= now
        out.scoped.append(Scoped(key=s.key, label=s.label, pct=settled.pct,
                                 resets_at=settled.resets_at,
                                 active=s.active and not reset_passed))
        if reset_passed and out.reached == f"scoped:{s.key}":
            out.reached = ""
    return out


def to_public(w: Windows) -> dict:
    """The listing payload (an EFFECTIVE reading)."""
    def win(x: Window | None):
        return None if x is None else {"pct": round(x.pct, 1), "resets_at": _dt_iso(x.resets_at)}
    return {
        "five_hour": win(w.five_hour),
        "seven_day": win(w.seven_day),
        "scoped": [
            {"key": s.key, "label": s.label, "pct": round(s.pct, 1),
             "resets_at": _dt_iso(s.resets_at), "active": s.active}
            for s in w.scoped
        ],
        "reached": w.reached,
        "plan": w.plan,
        "observed_at": w.observed_at.isoformat(),
        "source": w.source,
    }


# ---------------------------------------------------------------------------
# The questions the pool asks
# ---------------------------------------------------------------------------

def exhausted(w: Windows, model: str = "") -> bool:
    """Skip this account for a spawn of ``model``: either overall window past
    its spill mark, the vendor's own reached flag on an overall window, or the
    per-model weekly window of the spawn's family past its spill mark or
    reached. A scoped window's ``active`` flag is NOT exhaustion: it is the
    vendor's "the limit to watch" and sits on a window at 61 % once the
    session window has reset (seen 2026-09-11, when it made the pool treat
    both accounts as out of Fable and pin a chat to the one that really was)."""
    if exhausted_overall(w):
        return True
    fam = model_family(model)
    if not fam:
        return False
    s = w.scoped_for(fam)
    if s is None:
        return False
    return s.pct >= SPILL_7D or w.reached == f"scoped:{fam}"


def exhausted_overall(w: Windows) -> bool:
    """Exhaustion that holds for every model (what moves a pinned scope)."""
    if w.five_hour is not None and w.five_hour.pct >= SPILL_5H:
        return True
    if w.seven_day is not None and w.seven_day.pct >= SPILL_7D:
        return True
    return w.reached in ("five_hour", "seven_day")


def frees_at(w: Windows, model: str = "") -> datetime | None:
    """The earliest reset instant among the windows that exhaust this account
    (how the all-exhausted fallback orders candidates)."""
    instants: list[datetime] = []
    if w.five_hour is not None and (w.five_hour.pct >= SPILL_5H or w.reached == "five_hour"):
        if w.five_hour.resets_at:
            instants.append(w.five_hour.resets_at)
    if w.seven_day is not None and (w.seven_day.pct >= SPILL_7D or w.reached == "seven_day"):
        if w.seven_day.resets_at:
            instants.append(w.seven_day.resets_at)
    fam = model_family(model)
    s = w.scoped_for(fam) if fam else None
    if s is not None and (s.pct >= SPILL_7D or w.reached == f"scoped:{fam}") and s.resets_at:
        instants.append(s.resets_at)
    return min(instants) if instants else None


def weekly_reset(w: Windows) -> datetime | None:
    return w.seven_day.resets_at if w.seven_day is not None else None


# ---------------------------------------------------------------------------
# Setting, recording
# ---------------------------------------------------------------------------

_setting_lock = threading.Lock()
_setting_cache: tuple[float, bool] | None = None


def is_enabled() -> bool:
    """The admin switch; unset means on. Cached for a minute — the pool asks
    on every spawn."""
    global _setting_cache
    now = time.monotonic()
    with _setting_lock:
        if _setting_cache and now - _setting_cache[0] < _SETTING_CACHE_S:
            return _setting_cache[1]
    try:
        from storage import database as task_store
        raw = str(task_store.get_platform_setting(SETTING_KEY) or "").strip().lower()
        enabled = raw not in ("0", "false", "no", "off")
    except Exception:
        enabled = True
    with _setting_lock:
        _setting_cache = (now, enabled)
    return enabled


def invalidate_setting_cache() -> None:
    global _setting_cache
    with _setting_lock:
        _setting_cache = None


def merged_reading(newest: dict, poll: dict | None, now: datetime | None = None) -> Windows:
    """The effective reading of an account from its newest sample, with the
    per-model windows carried over from its newest POLL when the newest
    sample is a stream event: the CLI's in-band events report the overall
    windows only, so on their own they hid a full Fable window for the
    minutes between two polls (live-observed 2026-09-11 — the pool sent
    Fable work to an account the poll had read at 100 %). The poll's scoped
    windows are settled by the poll's own age and reset instants."""
    e = effective(from_row(newest), now)
    if newest.get("source") != "poll" and poll:
        p = effective(from_row(poll), now)
        e.scoped = p.scoped
        if not e.reached and p.reached.startswith("scoped:"):
            e.reached = p.reached
    return e


def latest_readings(store, sub_ids: list[str], now: datetime | None = None) -> dict[str, Windows]:
    """``merged_reading`` per account that has a sample, through ``store``
    (``storage.billing.subscription_store`` or a caller's own import of it —
    the pool patches its module in tests)."""
    ids = list(sub_ids)
    rows = store.latest_window_samples(ids)
    need = [sid for sid, row in rows.items() if row.get("source") != "poll"]
    polls = store.latest_window_samples(need, source="poll") if need else {}
    return {sid: merged_reading(row, polls.get(sid), now) for sid, row in rows.items()}


def latest(sub_ids: list[str], now: datetime | None = None) -> dict[str, Windows]:
    """Effective readings for the accounts that have a sample."""
    from storage.billing import subscription_store
    return latest_readings(subscription_store, sub_ids, now)


def exhausted_any(w: Windows) -> bool:
    """Exhausted for SOME model: the overall windows, or any per-model
    weekly window past its spill mark or reached. What makes a new sample
    worth a rebalance pass (the pass then judges each scope by its model)."""
    if exhausted_overall(w):
        return True
    return any(s.pct >= SPILL_7D or w.reached == f"scoped:{s.key}" for s in w.scoped)


def record(sub_id: str, w: Windows) -> bool:
    """Store one reading. When the account just crossed into exhaustion —
    overall, or for some model — ask the pool to move the scopes pinned to
    it. Returns whether a row was written (a coalesced repeat is not)."""
    if not is_enabled():
        return False
    from storage.billing import subscription_store
    before = subscription_store.latest_window_samples([sub_id]).get(sub_id)
    was_exhausted = bool(before) and exhausted_any(effective(from_row(before), w.observed_at))
    written = subscription_store.insert_window_sample(sub_id, **to_row(w))
    if written and not was_exhausted and exhausted_any(effective(w, w.observed_at)):
        try:
            from services.engines import subscription_pool
            subscription_pool.schedule_rebalance("window exhausted")
        except Exception:
            logger.debug("windows: rebalance scheduling failed", exc_info=True)
    return written


def record_for_session(session_id: str, w: Windows) -> bool:
    """Attribute a reading to the account the session is bound to."""
    if not session_id or not is_enabled():
        return False
    from services.engines import subscription_pool
    sub_id = subscription_pool.get_session_subscription(session_id)
    if not sub_id or sub_id == "default":
        return False
    try:
        return record(sub_id, w)
    except Exception:
        logger.warning(f"windows: recording for session {session_id[:8]} failed", exc_info=True)
        return False


def record_for_session_async(session_id: str, w: Windows) -> None:
    """``record_for_session`` off the event loop, fire-and-forget; called from
    the stream consumers, which must never wait on the store."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        record_for_session(session_id, w)
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(record_for_session, session_id, w)
        except Exception:
            logger.debug("windows: async record failed", exc_info=True)

    loop.create_task(_run())


# The stream consumers' entry points: a raw vendor shape in, a sample out.

def record_claude_event_async(session_id: str, info: dict) -> None:
    """A headless Claude session's ``rate_limit_event.rate_limit_info``."""
    w = from_claude_event(info)
    if w is not None:
        record_for_session_async(session_id, w)


def record_codex_snapshot_async(session_id: str, snapshot: dict) -> None:
    """The app-server's ``account/rateLimits/updated`` params."""
    w = from_codex_snapshot(snapshot)
    if w is not None:
        record_for_session_async(session_id, w)


def record_codex_snapshot(session_id: str, snapshot: dict, observed_at: str | None = None) -> bool:
    """A rollout line's ``rate_limits``, dated by the line's own timestamp so a
    replayed line never reads as a fresh observation. Synchronous: the
    tailer already runs in a thread."""
    w = from_codex_snapshot(snapshot)
    if w is None:
        return False
    stamped = _iso_dt(observed_at)
    if stamped is not None:
        w.observed_at = stamped
    return record_for_session(session_id, w)


# ---------------------------------------------------------------------------
# Polling — idle accounts and interactive sessions have no in-band signal
# ---------------------------------------------------------------------------

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# The beta the CLI itself sends on every OAuth request.
_CLAUDE_OAUTH_BETA = "oauth-2025-04-20"

# An account with a younger sample is not asked. Below the freshness tick
# (300 s) so every tick polls every account without an in-band sample: an
# interactive terminal has no stream events, and a reading that lags a tick
# behind the CLI is what the AI Engines cards then show.
_POLL_INTERVAL_S = 240
_POLL_TIMEOUT_S = 10.0
_BACKOFF_MIN_S = 60.0
_BACKOFF_MAX_S = 1800.0
_SHAPE_WARN_INTERVAL_S = 3600.0

_poll_lock = threading.Lock()
_backoff: dict[str, tuple[float, float]] = {}     # sub_id → (retry_at, current_wait)
_shape_warned: dict[str, float] = {}


def _user_agent() -> str:
    try:
        import config as app_config
        version = getattr(app_config, "PINNED_OTODOCK_VERSION", "") or "0.0.0"
    except Exception:
        version = "0.0.0"
    return f"OtoDock/{version}"


def _air_gapped() -> bool:
    try:
        import config as app_config
        return bool(getattr(app_config, "OTODOCK_AIR_GAPPED", False))
    except Exception:
        return False


def _backed_off(sub_id: str, now: float) -> bool:
    with _poll_lock:
        entry = _backoff.get(sub_id)
    return bool(entry) and now < entry[0]


def _note_failure(sub_id: str, now: float) -> None:
    with _poll_lock:
        _, wait = _backoff.get(sub_id, (0.0, 0.0))
        wait = min(_BACKOFF_MAX_S, max(_BACKOFF_MIN_S, wait * 2))
        _backoff[sub_id] = (now + wait, wait)


def _note_success(sub_id: str) -> None:
    with _poll_lock:
        _backoff.pop(sub_id, None)


def reset_poll_state() -> None:
    with _poll_lock:
        _backoff.clear()
        _shape_warned.clear()


def poll_request(sub: dict, cred: dict) -> tuple[str, dict] | None:
    """The URL and headers for one account's usage read, or None when the
    stored credential lacks what the vendor needs."""
    oauth = cred.get("oauth_token") if isinstance(cred.get("oauth_token"), dict) else {}
    token = str(oauth.get("accessToken") or "")
    if not token:
        return None
    ua = _user_agent()
    layer = sub.get("layer")
    if layer == "claude-code-cli":
        return CLAUDE_USAGE_URL, {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _CLAUDE_OAUTH_BETA,
            "Content-Type": "application/json",
            "User-Agent": ua,
        }
    if layer == "codex-cli":
        blob = cred.get("codex_auth_blob") if isinstance(cred.get("codex_auth_blob"), dict) else {}
        tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else {}
        account_id = str(tokens.get("account_id") or "")
        headers = {"Authorization": f"Bearer {token}", "User-Agent": ua}
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        return CODEX_USAGE_URL, headers
    return None


def normalize_poll(sub: dict, payload) -> Windows | None:
    if sub.get("layer") == "claude-code-cli":
        return from_claude_usage(payload)
    if sub.get("layer") == "codex-cli":
        return from_codex_usage(payload)
    return None


async def poll_one(sub: dict, client, *, now: float | None = None) -> bool:
    """Ask the vendor for one account's windows and record them. Returns
    whether a sample was recorded. Never raises: a failure backs the
    account off (60 s doubling to 30 min); a 401 too — the freshness worker
    owns token refreshes and nothing here ever expires a row."""
    sub_id = str(sub.get("id") or "")
    now = time.monotonic() if now is None else now
    if not sub_id or _backed_off(sub_id, now):
        return False
    from storage.billing import subscription_store
    try:
        cred = await asyncio.to_thread(subscription_store.get_credential_data, sub_id)
    except Exception:
        logger.debug("windows: credential read failed for %s", sub_id[:8], exc_info=True)
        _note_failure(sub_id, now)
        return False
    req = poll_request(sub, cred)
    if req is None:
        _note_failure(sub_id, now)
        return False
    url, headers = req
    try:
        resp = await client.get(url, headers=headers, timeout=_POLL_TIMEOUT_S)
        status = int(getattr(resp, "status_code", 0) or 0)
        payload = resp.json() if status == 200 else None
    except Exception as exc:
        logger.info("windows: poll of %s failed: %s", sub_id[:8], type(exc).__name__)
        _note_failure(sub_id, now)
        return False
    if status != 200:
        logger.info("windows: %s answered %s for %s", sub.get("layer"), status, sub_id[:8])
        _note_failure(sub_id, now)
        return False
    windows = normalize_poll(sub, payload)
    if windows is None:
        with _poll_lock:
            warn = now - _shape_warned.get(sub_id, -_SHAPE_WARN_INTERVAL_S) >= _SHAPE_WARN_INTERVAL_S
            if warn:
                _shape_warned[sub_id] = now
        if warn:
            logger.warning("windows: unrecognised %s usage shape for %s",
                           sub.get("layer"), sub_id[:8])
        _note_failure(sub_id, now)
        return False
    _note_success(sub_id)
    try:
        return await asyncio.to_thread(record, sub_id, windows)
    except Exception:
        logger.warning("windows: recording the poll for %s failed", sub_id[:8], exc_info=True)
        return False


def _new_client():
    import httpx
    return httpx.AsyncClient(timeout=_POLL_TIMEOUT_S)


async def poll_due(*, client=None, now: datetime | None = None) -> int:
    """Poll every active OAuth account whose latest sample is older than the
    interval. The freshness tick's first step; returns how many were asked."""
    if not is_enabled() or _air_gapped():
        return 0
    from storage.billing import subscription_store
    try:
        rows = await asyncio.to_thread(subscription_store.list_subscriptions)
    except Exception:
        logger.exception("windows: subscription enumeration failed")
        return 0
    candidates = [
        r for r in rows
        if r.get("auth_type") == "oauth" and r.get("status") == "active"
        and r.get("layer") in ("claude-code-cli", "codex-cli")
    ]
    if not candidates:
        return 0
    now = now or datetime.now(timezone.utc)
    try:
        latest_rows = await asyncio.to_thread(
            subscription_store.latest_window_samples, [r["id"] for r in candidates])
    except Exception:
        logger.exception("windows: latest-sample read failed")
        return 0
    due = []
    for r in candidates:
        row = latest_rows.get(r["id"])
        observed = _iso_dt(row.get("observed_at")) if row else None
        if observed is None or (now - observed).total_seconds() >= _POLL_INTERVAL_S:
            due.append(r)
    if not due:
        return 0
    own_client = client is None
    if own_client:
        client = _new_client()
    try:
        for r in due:
            await poll_one(r, client)
    finally:
        if own_client:
            with contextlib.suppress(Exception):
                await client.aclose()
    try:
        cutoff = (now - timedelta(days=8)).isoformat()
        await asyncio.to_thread(subscription_store.prune_window_samples, cutoff)
    except Exception:
        logger.debug("windows: prune failed", exc_info=True)
    return len(due)


def schedule_poll(sub_id: str) -> None:
    """One immediate poll for a freshly connected account, from the event
    loop, so its bars show right after the connect. Fire-and-forget."""
    if not sub_id or not is_enabled() or _air_gapped():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        from storage.billing import subscription_store
        try:
            sub = await asyncio.to_thread(subscription_store.get_subscription, sub_id)
            if not sub or sub.get("auth_type") != "oauth":
                return
            client = _new_client()
            try:
                await poll_one(sub, client)
            finally:
                await client.aclose()
        except Exception:
            logger.debug("windows: connect-time poll failed", exc_info=True)

    loop.create_task(_run())
