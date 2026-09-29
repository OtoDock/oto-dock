"""ChatGPT's usage-window shapes for the Codex engine — the vendor half of
``services/engines/subscription_windows``.

Codex reports a consumer subscription's windows on a usage endpoint (the
poll), in the app-server's ``account/rateLimits/updated`` notification and
next to the rollout's token counts (in band). The vendor tells its two
windows apart by LENGTH, not by name; each is filed under the declared
window whose length covers it (``subscription_windows.spec_for_length``).
Each normalizer returns None on a shape it does not recognise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.execution_layer import Window, WindowSpec, Windows

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def usage_request(stored: dict) -> tuple[str, dict] | None:
    """The URL and headers for one account's usage read (the poller adds the
    platform's ``User-Agent``), or None when the stored credential has no
    access token. ``ChatGPT-Account-Id`` comes from the login blob."""
    oauth = stored.get("oauth_token") if isinstance(stored.get("oauth_token"), dict) else {}
    token = str(oauth.get("accessToken") or "")
    if not token:
        return None
    blob = stored.get("codex_auth_blob") if isinstance(stored.get("codex_auth_blob"), dict) else {}
    tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else {}
    headers = {"Authorization": f"Bearer {token}"}
    account_id = str(tokens.get("account_id") or "")
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return USAGE_URL, headers


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


def _fullest(w: Windows) -> str:
    best, best_pct = "", -1.0
    for key, win in w.windows.items():
        if win.pct > best_pct:
            best, best_pct = key, win.pct
    return best


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def from_usage(payload: dict, specs: dict[str, WindowSpec]) -> Windows | None:
    """``GET /backend-api/wham/usage``: percents, window lengths in seconds,
    epoch reset instants; the windows are told apart by their length."""
    from services.engines.subscription_windows import spec_for_length
    if not isinstance(payload, dict):
        return None
    rl = payload.get("rate_limit")
    if not isinstance(rl, dict):
        return None
    w = Windows(specs=specs, source="poll", plan=str(payload.get("plan_type") or ""))
    reached_type = str(rl.get("rate_limit_reached_type") or payload.get("rate_limit_reached_type") or "")
    slot_keys: dict[str, str] = {}
    for slot in ("primary_window", "secondary_window"):
        d = rl.get(slot)
        if not isinstance(d, dict):
            continue
        pct = _pct(d.get("used_percent"))
        if pct is None:
            continue
        spec = spec_for_length(specs, _pct(d.get("limit_window_seconds")))
        if spec is None:
            continue
        resets = _epoch_dt(d.get("reset_at"))
        if resets is None:
            after = _pct(d.get("reset_after_seconds"))
            if after is not None:
                resets = w.observed_at + timedelta(seconds=after)
        w.windows[spec.key] = Window(pct=pct, resets_at=resets)
        slot_keys[slot.split("_")[0]] = spec.key
    if not w.windows:
        return None
    if rl.get("limit_reached"):
        w.reached = slot_keys.get(reached_type, "") or _fullest(w)
    return w


def from_snapshot(rl: dict, specs: dict[str, WindowSpec]) -> Windows | None:
    """The app-server ``account/rateLimits/updated`` params (camelCase:
    ``{rateLimits: {primary, secondary: {usedPercent, windowDurationMins,
    resetsAt}, planType, rateLimitReachedType}}``, the ``account/rateLimits/
    read`` result carries the same ``rateLimits``) and the rollout
    ``token_count.rate_limits`` (snake_case): percents, window lengths in
    minutes, epoch reset instants."""
    from services.engines.subscription_windows import spec_for_length
    if not isinstance(rl, dict):
        return None
    w = Windows(specs=specs, source="codex_event",
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
        spec = spec_for_length(specs, minutes * 60 if minutes is not None else None)
        if spec is None:
            continue
        w.windows[spec.key] = Window(pct=pct, resets_at=_any_dt(_pick(d, "resetsAt", "resets_at")))
        slot_keys[slot] = spec.key
    if not w.windows:
        return None
    if reached_type:
        w.reached = slot_keys.get(reached_type, "") or _fullest(w)
    return w


# ---------------------------------------------------------------------------
# The engine's own stream consumers (the translator, the rollout tailer)
# ---------------------------------------------------------------------------

def _specs() -> dict[str, WindowSpec]:
    from core.layers.codex.layer import _CODEX_CAPABILITIES
    return {s.key: s for s in _CODEX_CAPABILITIES.usage.windows}


def record_snapshot_async(session_id: str, snapshot: dict) -> None:
    """The app-server's ``account/rateLimits/updated`` params → a window
    sample on the session's account, off the event loop."""
    w = from_snapshot(snapshot, _specs())
    if w is not None:
        from services.engines import subscription_windows
        subscription_windows.record_for_session_async(session_id, w)


def record_snapshot(session_id: str, snapshot: dict, observed_at: str | None = None) -> bool:
    """A rollout line's ``rate_limits``, dated by the line's own timestamp so a
    replayed line never reads as a fresh observation. Synchronous: the
    tailer already runs in a thread."""
    w = from_snapshot(snapshot, _specs())
    if w is None:
        return False
    stamped = _iso_dt(observed_at)
    if stamped is not None:
        w.observed_at = stamped
    from services.engines import subscription_windows
    return subscription_windows.record_for_session(session_id, w)
