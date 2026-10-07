"""Provider windows: an OAuth account's real session and quota window state.

Claude and ChatGPT both cap a consumer subscription with a rolling session
window and a rolling weekly window, and both report the state of those
windows to their own CLI. Each ENGINE turns its vendor's shapes into one
``Windows`` reading (``ExecutionLayer.parse_usage`` for the poll,
``record_usage_event`` for the in-band stream — ``core/layers/cli/usage.py``,
``core/layers/codex/usage.py``); this module stores a reading as a sample
(``subscription_store.insert_window_sample``) and answers the questions the
pool asks of the latest one: is this account exhausted for this spawn, and
when does its quota window reset. The vendor's reading is the account's
whole truth, the owner's own chats and their CLI on a laptop included, which
is exactly why the pool prefers it to our own cost estimate.

WHICH windows an engine's vendor reports is the engine's declaration
(``LayerCapabilities.usage.windows`` — a ``WindowSpec`` per window: its key,
length, role and label; ``core/execution_layer.py``). Every reading carries
the specs it was read against, so nothing here names a window by attribute:
the routing, the caps and the alerts ask for the window whose ROLE is
``quota``, and settle each window by ITS declared length. The two engines
today declare the same two windows (``five_hour`` session, ``seven_day``
quota), which are also the two the sample table has columns for; a window
under any other key rides in the row's JSONB ``data`` (``to_row`` /
``from_row``), so a fourth engine with a different quota window needs no
migration. What is NOT the engine's to declare is the routing margin: the
spill marks below are the platform's policy, keyed by role. A per-model
window is matched to a spawn by the engine's ``usage_scope_key(model)``.

The poller here asks each engine for its vendor request and hands the
answer back to it to parse; ``token_fanout`` drives the poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from core.execution_layer import Scoped, Window, WindowSpec, Windows

logger = logging.getLogger("claude-proxy.subscription-windows")

SETTING_KEY = "subscription_windows_enabled"
_SETTING_CACHE_S = 60

# An account is skipped for new work at/after these; the vendor's hard stop
# is 100, the margin keeps a live session's next few turns from hitting the
# wall. Platform ROUTING policy, keyed by the window's role — an engine
# declares its windows, never the margin the pool routes by. A per-model
# window is a quota window.
SPILL_PCT: dict[str, float] = {"session": 90.0, "quota": 95.0}

# The two window keys the sample table has columns for (``five_hour_pct`` …);
# a storage fact, frozen with the schema. Any other declared key is stored
# under ``data["windows"]``.
_COLUMN_KEYS: tuple[str, ...] = ("five_hour", "seven_day")


def window_specs(layer: str) -> dict[str, WindowSpec]:
    """The windows ``layer``'s vendor reports, keyed by window key — the
    engine's declaration, read through the registry per call (registry-pull;
    the session manager is never imported at module level here). An engine
    that is not registered declares nothing: its readings have no windows
    and never exhaust."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(layer)
    if caps is None:
        return {}
    return {spec.key: spec for spec in caps.usage.windows}


def spill_pct(spec: WindowSpec) -> float:
    """The percentage at which the pool stops sending new work to an account
    for this window (100 when the role is unknown — only the vendor's own
    stop applies)."""
    return SPILL_PCT.get(spec.role, 100.0)


def quota_spec(specs: dict[str, WindowSpec]) -> WindowSpec | None:
    """The declared quota window — the one the drain-first sort, the day cap
    and the alerts key off — or None for an engine that declares none."""
    for spec in specs.values():
        if spec.role == "quota":
            return spec
    return None


def spec_for_length(specs: dict[str, WindowSpec], seconds: float | None) -> WindowSpec | None:
    """The declared window a vendor-reported length belongs to: the shortest
    whose length covers the report with a fifth of slack (5 h covers a report
    of up to 6 h — the boundary the Codex parser always used), else the
    longest. None when nothing is declared or no length was reported."""
    if seconds is None or not specs:
        return None
    by_length = sorted(specs.values(), key=lambda s: s.length_s)
    for spec in by_length:
        if seconds <= spec.length_s * 1.2:
            return spec
    return by_length[-1]


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


def _pct(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dt_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Store rows ⇄ readings
# ---------------------------------------------------------------------------

def to_row(w: Windows) -> dict:
    """The keyword arguments ``subscription_store.insert_window_sample`` takes.
    A column-backed window (``_COLUMN_KEYS``) goes to its columns; any other
    declared window rides under ``data["windows"]``."""
    row: dict = {
        "observed_at": w.observed_at.isoformat(),
        "source": w.source,
        "five_hour_pct": None, "five_hour_resets_at": None,
        "seven_day_pct": None, "seven_day_resets_at": None,
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
    extra: dict[str, dict] = {}
    for key, win in w.windows.items():
        if key in _COLUMN_KEYS:
            row[f"{key}_pct"] = win.pct
            row[f"{key}_resets_at"] = _dt_iso(win.resets_at)
        else:
            extra[key] = {"pct": win.pct, "resets_at": _dt_iso(win.resets_at)}
    if extra:
        row["data"]["windows"] = extra
    return row


def sample_window(row: dict, key: str) -> Window | None:
    """One window out of a stored sample row — from its columns when the key
    is column-backed, else from ``data["windows"]``. None when the sample
    did not carry it."""
    if key in _COLUMN_KEYS:
        pct = row.get(f"{key}_pct")
        if pct is None:
            return None
        return Window(float(pct), _iso_dt(row.get(f"{key}_resets_at")))
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    extra = data.get("windows") if isinstance(data.get("windows"), dict) else {}
    entry = extra.get(key)
    if not isinstance(entry, dict):
        return None
    pct = _pct(entry.get("pct"))
    if pct is None:
        return None
    return Window(pct, _iso_dt(entry.get("resets_at")))


def from_row(row: dict, specs: dict[str, WindowSpec]) -> Windows:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    w = Windows(
        specs=specs,
        observed_at=_iso_dt(row.get("observed_at")) or datetime.now(timezone.utc),
        source=str(row.get("source") or ""),
        reached=str(data.get("reached") or ""),
        plan=str(data.get("plan") or ""),
    )
    for key in specs:
        win = sample_window(row, key)
        if win is not None:
            w.windows[key] = win
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
    reset instant is unknown; a sample older than the window's own declared
    length is unknown for that window. The per-model windows settle by the
    quota window's length (they are per-model quota windows)."""
    now = now or datetime.now(timezone.utc)
    age = (now - w.observed_at).total_seconds()
    out = Windows(specs=w.specs, observed_at=w.observed_at, source=w.source,
                  plan=w.plan, reached=w.reached)

    def settle(win: Window | None, length: float) -> Window | None:
        if win is None or age > length:
            return None
        if win.resets_at is not None and win.resets_at <= now:
            return Window(pct=0.0, resets_at=None)
        if win.pct > 0 and win.resets_at is None:
            return None
        return win

    for key, spec in w.specs.items():
        src = w.windows.get(key)
        dst = settle(src, spec.length_s)
        if dst is not None:
            out.windows[key] = dst
        if out.reached == key and (dst is None or (src and src.resets_at and src.resets_at <= now)):
            out.reached = ""
    quota = quota_spec(w.specs)
    scoped_len = quota.length_s if quota else max(
        (s.length_s for s in w.specs.values()), default=0)
    for s in w.scoped:
        settled = settle(Window(s.pct, s.resets_at), scoped_len)
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
    """The listing payload (an EFFECTIVE reading): one entry per declared
    window under its key (``five_hour`` / ``seven_day`` for both engines
    today — the dashboard's ``SubscriptionWindows`` shape), null when the
    reading does not carry it."""
    def win(x: Window | None):
        return None if x is None else {"pct": round(x.pct, 1), "resets_at": _dt_iso(x.resets_at)}
    out: dict = {key: win(w.windows.get(key)) for key in w.specs}
    out.update({
        "scoped": [
            {"key": s.key, "label": s.label, "pct": round(s.pct, 1),
             "resets_at": _dt_iso(s.resets_at), "active": s.active}
            for s in w.scoped
        ],
        "reached": w.reached,
        "plan": w.plan,
        "observed_at": w.observed_at.isoformat(),
        "source": w.source,
    })
    return out


# ---------------------------------------------------------------------------
# The questions the pool asks
# ---------------------------------------------------------------------------

def _scoped_spill() -> float:
    return SPILL_PCT["quota"]


def exhausted(w: Windows, scope_key: str = "") -> bool:
    """Skip this account for a spawn: either overall window past its spill
    mark, the vendor's own reached flag on an overall window, or — when the
    spawn's model falls under a per-model window (``scope_key``, the engine's
    ``usage_scope_key(model)``; "" = none) — that window past its spill mark
    or reached. A scoped window's ``active`` flag is NOT exhaustion: it is
    the vendor's "the limit to watch" and sits on a window at 61 % once the
    session window has reset (seen 2026-09-11, when it made the pool treat
    both accounts as out of Fable and pin a chat to the one that really was)."""
    if exhausted_overall(w):
        return True
    if not scope_key:
        return False
    s = w.scoped_for(scope_key)
    if s is None:
        return False
    return s.pct >= _scoped_spill() or w.reached == f"scoped:{scope_key}"


def exhausted_overall(w: Windows) -> bool:
    """Exhaustion that holds for every model (what moves a pinned scope):
    any declared window at or past the spill mark of its role, or the
    vendor's reached flag on one."""
    for key, win in w.windows.items():
        spec = w.specs.get(key)
        if spec is not None and win.pct >= spill_pct(spec):
            return True
    return w.reached in w.specs


def has_headroom(w: Windows, key: str) -> bool:
    """Whether the reading shows the window ``key`` (a declared window key,
    or ``scoped:<scope key>`` for a model family's window) below its spill
    mark and not reached: what ends a usage-limit rest early. False when the
    reading does not carry the window."""
    if key.startswith("scoped:"):
        s = w.scoped_for(key[len("scoped:"):])
        return s is not None and s.pct < _scoped_spill() and w.reached != key
    win = w.windows.get(key)
    spec = w.specs.get(key)
    if win is None or spec is None:
        return False
    return win.pct < spill_pct(spec) and w.reached != key


def frees_at(w: Windows, scope_key: str = "") -> datetime | None:
    """The earliest reset instant among the windows that exhaust this account
    (how the all-exhausted fallback orders candidates)."""
    instants: list[datetime] = []
    for key, win in w.windows.items():
        spec = w.specs.get(key)
        if spec is None:
            continue
        if (win.pct >= spill_pct(spec) or w.reached == key) and win.resets_at:
            instants.append(win.resets_at)
    s = w.scoped_for(scope_key) if scope_key else None
    if s is not None and (s.pct >= _scoped_spill() or w.reached == f"scoped:{scope_key}") and s.resets_at:
        instants.append(s.resets_at)
    return min(instants) if instants else None


def quota_reset(w: Windows) -> datetime | None:
    """When the account's quota window resets (the drain-first sort key);
    None without a quota window or a reading of it."""
    win = w.windows.get(w.quota_key)
    return win.resets_at if win is not None else None


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


def merged_reading(specs: dict[str, WindowSpec], newest: dict, poll: dict | None,
                   now: datetime | None = None) -> Windows:
    """The effective reading of an account from its newest sample, with the
    per-model windows carried over from its newest POLL when the newest
    sample is a stream event: the CLI's in-band events report the overall
    windows only, so on their own they hid a full Fable window for the
    minutes between two polls (live-observed 2026-09-11 — the pool sent
    Fable work to an account the poll had read at 100 %). The poll's scoped
    windows are settled by the poll's own age and reset instants."""
    e = effective(from_row(newest, specs), now)
    if newest.get("source") != "poll" and poll:
        p = effective(from_row(poll, specs), now)
        e.scoped = p.scoped
        if not e.reached and p.reached.startswith("scoped:"):
            e.reached = p.reached
    return e


def latest_readings(store, subs: list[dict], now: datetime | None = None) -> dict[str, Windows]:
    """``merged_reading`` per account that has a sample, through ``store``
    (``storage.billing.subscription_store`` or a caller's own import of it —
    the pool patches its module in tests). ``subs`` are subscription ROWS
    (``id`` + ``layer``): the layer names the windows the sample is read
    against."""
    by_id = {str(s.get("id")): s for s in subs if s.get("id")}
    rows = store.latest_window_samples(list(by_id))
    need = [sid for sid, row in rows.items() if row.get("source") != "poll"]
    polls = store.latest_window_samples(need, source="poll") if need else {}
    return {
        sid: merged_reading(window_specs(by_id[sid].get("layer") or ""), row,
                            polls.get(sid), now)
        for sid, row in rows.items()
    }


def latest(subs: list[dict], now: datetime | None = None) -> dict[str, Windows]:
    """Effective readings for the accounts (rows with ``id`` + ``layer``)
    that have a sample."""
    from storage.billing import subscription_store
    return latest_readings(subscription_store, subs, now)


def exhausted_any(w: Windows) -> bool:
    """Exhausted for SOME model: the overall windows, or any per-model
    quota window past its spill mark or reached. What makes a new sample
    worth a rebalance pass (the pass then judges each scope by its model)."""
    if exhausted_overall(w):
        return True
    return any(s.pct >= _scoped_spill() or w.reached == f"scoped:{s.key}" for s in w.scoped)


def record(sub_id: str, w: Windows) -> bool:
    """Store one reading. When the account just crossed into exhaustion —
    overall, or for some model — ask the pool to move the scopes pinned to
    it; a rested window the reading shows with headroom again ends the
    pool's rest early (``subscription_pool.clear_rests_with_headroom``, a
    coalesced repeat included). Returns whether a row was written (a
    coalesced repeat is not)."""
    if not is_enabled():
        return False
    from storage.billing import subscription_store
    before = subscription_store.latest_window_samples([sub_id]).get(sub_id)
    was_exhausted = bool(before) and exhausted_any(effective(from_row(before, w.specs), w.observed_at))
    written = subscription_store.insert_window_sample(sub_id, **to_row(w))
    now_reading = effective(w, w.observed_at)
    if written and not was_exhausted and exhausted_any(now_reading):
        try:
            from services.engines import subscription_pool
            subscription_pool.schedule_rebalance("window exhausted")
        except Exception:
            logger.debug("windows: rebalance scheduling failed", exc_info=True)
    try:
        from services.engines import subscription_pool
        subscription_pool.clear_rests_with_headroom(sub_id, now_reading)
    except Exception:
        logger.debug("windows: rest check failed", exc_info=True)
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


# ---------------------------------------------------------------------------
# Polling — idle accounts and interactive sessions have no in-band signal
# ---------------------------------------------------------------------------

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
    """The URL and headers for one account's usage read — the ENGINE's
    request (``ExecutionLayer.usage_request``) with the platform's
    ``User-Agent`` (honest: these are the endpoints the official CLIs use,
    not published APIs) — or None when the engine is not registered, declares
    no windows, or the stored credential lacks what the vendor needs."""
    from core.session.session_manager import get_all_layers
    layer = get_all_layers().get(str(sub.get("layer") or ""))
    if layer is None or not layer.capabilities.usage.windows:
        return None
    req = layer.usage_request(cred)
    if req is None:
        return None
    url, headers = req
    return url, {**headers, "User-Agent": _user_agent()}


def normalize_poll(sub: dict, payload) -> Windows | None:
    """The engine's reading of its vendor's usage payload, or None."""
    from core.session.session_manager import get_all_layers
    layer = get_all_layers().get(str(sub.get("layer") or ""))
    return layer.parse_usage(payload) if layer is not None else None


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
    interval, on every engine whose vendor reports windows. The freshness
    tick's first step; returns how many were asked."""
    if not is_enabled() or _air_gapped():
        return 0
    from storage.billing import subscription_status, subscription_store
    try:
        rows = await asyncio.to_thread(subscription_store.list_subscriptions)
    except Exception:
        logger.exception("windows: subscription enumeration failed")
        return 0
    candidates = [
        r for r in rows
        if r.get("auth_type") == "oauth" and r.get("status") == subscription_status.ACTIVE
        and window_specs(str(r.get("layer") or ""))
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
