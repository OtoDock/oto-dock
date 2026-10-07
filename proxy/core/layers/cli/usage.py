"""Anthropic's usage-window shapes for the Claude Code engine — the vendor
half of ``services/engines/subscription_windows``.

Claude reports a consumer subscription's windows on a usage endpoint (the
poll) and in the headless stream's ``rate_limit_event`` (in band); both name
the windows ``five_hour`` / ``seven_day`` — the keys the engine declares them
under (``LayerCapabilities.usage.windows``) — and the weekly per-model
windows by the model FAMILY (Fable, Opus, Sonnet, …), which is also how a
spawn's model is matched to them (``usage_scope_key``). Each normalizer
returns None on a shape it does not recognise.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.execution_layer import Scoped, Window, WindowSpec, Windows

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# The model families Claude scopes weekly windows by; the join between the
# vendor's window label ("Fable") and a spawn's model id (claude-fable-5-1).
FAMILIES = ("fable", "opus", "sonnet", "haiku")

# The vendor's ``rateLimitType`` on a rejected stream event for an overall
# window → our reached key; a model family's weekly window
# (``seven_day_<family>``) is ``scoped:<family>`` (``event_reached_key``).
_EVENT_TYPE_TO_KEY = {
    "five_hour": "five_hour",
    "seven_day": "seven_day",
}


def model_family(model: str) -> str:
    """``claude-fable-5-1`` → ``fable``; a name with no known family → ``""``."""
    m = (model or "").lower()
    for fam in FAMILIES:
        if fam in m:
            return fam
    return ""


def event_reached_key(rate_limit_type: str) -> str:
    """The reached key of a rejected stream event's ``rateLimitType``: an
    overall window's own key, ``scoped:<family>`` for a model family's
    weekly window; "" for a type that names neither."""
    kind = str(rate_limit_type or "")
    if kind in _EVENT_TYPE_TO_KEY:
        return _EVENT_TYPE_TO_KEY[kind]
    if kind.startswith("seven_day_"):
        family = model_family(kind[len("seven_day_"):])
        if family:
            return f"scoped:{family}"
    return ""


def scoped_key(label: str) -> str:
    """The key a vendor window label is filed under: its family when it has
    one, else a slug of the label."""
    fam = model_family(label)
    if fam:
        return fam
    return "".join(ch if ch.isalnum() else "-" for ch in (label or "").lower()).strip("-")


def usage_request(stored: dict) -> tuple[str, dict] | None:
    """The URL and headers for one account's usage read (the poller adds the
    platform's ``User-Agent``), or None when the stored credential has no
    access token."""
    from core.layers.cli.oauth import OAUTH_BETA
    oauth = stored.get("oauth_token") if isinstance(stored.get("oauth_token"), dict) else {}
    token = str(oauth.get("accessToken") or "")
    if not token:
        return None
    return USAGE_URL, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Content-Type": "application/json",
    }


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


def _pct(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def from_usage(payload: dict, specs: dict[str, WindowSpec]) -> Windows | None:
    """``GET /api/oauth/usage``: percents and ISO reset instants; per-model
    weekly windows both as ``seven_day_<family>`` fields and as
    ``limits[]`` entries (which also carry ``is_active``)."""
    if not isinstance(payload, dict):
        return None
    if "five_hour" not in payload and "seven_day" not in payload:
        return None
    w = Windows(specs=specs, source="poll")

    def window(d) -> Window | None:
        if not isinstance(d, dict):
            return None
        pct = _pct(d.get("utilization"))
        if pct is None:
            return None
        return Window(pct=pct, resets_at=_iso_dt(d.get("resets_at")))

    for key in ("five_hour", "seven_day"):
        win = window(payload.get(key)) if key in specs else None
        if win is not None:
            w.windows[key] = win
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
                key = scoped_key(str(label))
                scoped[key] = Scoped(
                    key=key, label=str(label),
                    pct=pct if pct is not None else scoped.get(key, Scoped(key, label, 0.0, None)).pct,
                    resets_at=_iso_dt(entry.get("resets_at")), active=active,
                )
                if full and not w.reached:
                    w.reached = f"scoped:{key}"
            elif kind == "session" and full and not w.reached and "five_hour" in specs:
                w.reached = "five_hour"
            elif kind == "weekly_all" and full and not w.reached and "seven_day" in specs:
                w.reached = "seven_day"
    if not w.reached:
        for key, win in w.windows.items():
            if win.pct >= 100:
                w.reached = key
                break
    w.scoped = list(scoped.values())
    if not w.windows and not w.scoped:
        return None
    return w


def from_event(info: dict, specs: dict[str, WindowSpec]) -> Windows | None:
    """The headless stream's ``rate_limit_event.rate_limit_info``: fractions
    (0..1, above 1 when usage ran past a cap) and epoch reset instants."""
    if not isinstance(info, dict):
        return None
    unified = info.get("unifiedWindows")
    if not isinstance(unified, dict):
        return None
    w = Windows(specs=specs, source="claude_event")

    def window(d) -> Window | None:
        if not isinstance(d, dict):
            return None
        frac = _pct(d.get("utilization"))
        if frac is None:
            return None
        return Window(pct=frac * 100.0, resets_at=_epoch_dt(d.get("resetsAt")))

    for key in ("five_hour", "seven_day"):
        win = window(unified.get(key)) if key in specs else None
        if win is not None:
            w.windows[key] = win
    if not w.windows:
        return None
    if info.get("status") == "rejected":
        reached = event_reached_key(info.get("rateLimitType"))
        if reached.startswith("scoped:") or reached in specs:
            w.reached = reached
    return w


def record_event_async(session_id: str, info: dict) -> None:
    """A headless Claude session's ``rate_limit_event.rate_limit_info`` → a
    window sample on the session's account, off the event loop. The engine's
    own session and translator call this; shared code goes through
    ``ExecutionLayer.record_usage_event``."""
    from core.layers.cli.layer import _CLI_CAPABILITIES
    w = from_event(info, {s.key: s for s in _CLI_CAPABILITIES.usage.windows})
    if w is not None:
        from services.engines import subscription_windows
        subscription_windows.record_for_session_async(session_id, w)
