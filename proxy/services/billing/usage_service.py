"""Usage tracking and limit enforcement service.

All public functions are synchronous (called via asyncio.to_thread from async code).
Uses task_store (storage/database.py) for DB access.
"""

from datetime import datetime, timezone, timedelta
from calendar import monthrange

from storage import database as task_store
from storage import db_apps


# ---------------------------------------------------------------------------
# Period calculation helpers
# ---------------------------------------------------------------------------

def _monthly_range() -> tuple[str, str]:
    """Return (start_iso, end_iso) for current calendar month in UTC."""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _, days_in_month = monthrange(now.year, now.month)
    end = start + timedelta(days=days_in_month)
    return start.isoformat(), end.isoformat()


def _weekly_range() -> tuple[str, str]:
    """Return (start_iso, end_iso) for current ISO week (Mon-Sun) in UTC."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record_turn_usage(rows: list[dict]) -> list[int]:
    """Insert N usage rows in one transaction. Returns row IDs in order.

    Each row dict matches insert_usage_record's kwargs. A row is persisted
    when cost_usd > 0 OR message_count > 0 — so the LLM row (which always
    has message_count=1) lands even on a $0 cache-only turn, but MCP rows
    that round to zero are dropped instead of polluting the table.
    """
    filtered = [
        r for r in rows
        if (r.get("cost_usd") or 0) > 0 or (r.get("message_count") or 0) > 0
    ]
    return task_store.insert_usage_records_batch(filtered)


def record_usage(
    user_sub: str | None,
    agent: str,
    scope: str,
    source_type: str,
    source_id: str | None,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    message_count: int = 1,
    provider: str = "anthropic",
    model: str = "",
) -> int:
    """Insert a single usage row. Thin wrapper over `record_turn_usage` for
    callers that have just one row (meeting orchestrator, tests). Same
    filter rule: persisted when cost_usd > 0 OR message_count > 0.
    Returns the row ID, or 0 if filtered.
    """
    ids = record_turn_usage([{
        "user_sub": user_sub, "agent": agent, "scope": scope,
        "source_type": source_type, "source_id": source_id,
        "cost_usd": cost_usd, "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cache_read": cache_read,
        "cache_write": cache_write, "message_count": message_count,
        "provider": provider, "model": model,
    }])
    return ids[0] if ids else 0


# ---------------------------------------------------------------------------
# Limit resolution
# ---------------------------------------------------------------------------

def _resolve_limit(limit_type_primary: str, target_primary: str,
                   limit_type_fallback: str | None, target_fallback: str | None,
                   period: str) -> float | None:
    """Resolve effective limit: primary target > fallback > None (unlimited).

    Returns the cost_limit_usd value. None means no limit.
    A row with cost_limit_usd=NULL is an explicit "no limit" override.
    """
    row = task_store.get_usage_limit(limit_type_primary, target_primary, period)
    if row is not None:
        return row["cost_limit_usd"]  # could be None = explicit unlimited
    if limit_type_fallback and target_fallback:
        row = task_store.get_usage_limit(limit_type_fallback, target_fallback, period)
        if row is not None:
            return row["cost_limit_usd"]
    return None  # no limit configured


def _check_period(used: float, limit: float | None, start: str, end: str) -> dict | None:
    """Build period status dict. Returns None if no limit for this period."""
    if limit is None:
        return None
    if limit <= 0:
        # Zero limit = always blocked
        return {"limit": 0, "used": round(used, 4), "percent": 100.0, "start": start, "end": end}
    pct = round((used / limit) * 100, 1)
    return {"limit": round(limit, 2), "used": round(used, 4), "percent": pct, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Limit checking
# ---------------------------------------------------------------------------

def check_user_limit(user_sub: str, user_role: str) -> dict:
    """Check current usage against limits for a user.

    Resolution: per-user override > role default > no limit.
    Checks both weekly and monthly periods.

    Returns {
        allowed: bool,
        warning: bool,
        periods: {monthly: {...}|None, weekly: {...}|None,
                  self: {monthly: {...}|None, weekly: {...}|None}}
    }

    ``periods.self`` is the user's OWN dollar cap (``limit_type='user_self'``,
    set by the user, no role fallback) over the spend of their own API keys
    (``basis='self_api_key'``): their own money, gated at their own request.
    Same thresholds; any period of either budget blocks.

    Future per-provider sub-limit hook: `usage_limits.limit_type` is TEXT
    so a future `'provider'` row needs no migration. Wiring would add a
    parallel loop here that resolves limits per provider, fetches
    `get_usage_aggregated(..., provider=<p>)`, and ANDs the result into
    `allowed`/`warning`. No surface area is exposed yet.
    """
    result = {"allowed": True, "warning": False,
              "periods": {"monthly": None, "weekly": None,
                          "self": {"monthly": None, "weekly": None}}}

    def _apply(period_info: dict | None) -> None:
        if period_info and period_info["percent"] >= 100:
            result["allowed"] = False
        if period_info and period_info["percent"] >= 80:
            result["warning"] = True

    for period, range_fn in [("monthly", _monthly_range), ("weekly", _weekly_range)]:
        start, end = range_fn()
        limit = _resolve_limit("user_override", user_sub, "role_default", user_role, period)
        if limit is not None:
            # User/role limits are a PLATFORM-AUTH budget: gate only usage paid by
            # borrowed platform credentials. A user on their own subscription has
            # platform=0 here, so they're never blocked (their provider enforces
            # their own limits). See storage/database.py basis= classification.
            agg = task_store.get_usage_aggregated(
                user_sub=user_sub, scope="user", start=start, end=end, basis="platform",
            )
            period_info = _check_period(agg["total_cost"], limit, start, end)
            result["periods"][period] = period_info
            _apply(period_info)
        own_limit = _resolve_limit("user_self", user_sub, None, None, period)
        if own_limit is not None:
            agg = task_store.get_usage_aggregated(
                user_sub=user_sub, scope="user", start=start, end=end, basis="self_api_key",
            )
            period_info = _check_period(agg["total_cost"], own_limit, start, end)
            result["periods"]["self"][period] = period_info
            _apply(period_info)

    return result


def check_agent_limit(agent: str) -> dict:
    """Check current usage against limits for agent-scoped tasks."""
    result = {"allowed": True, "warning": False, "periods": {"monthly": None, "weekly": None}}

    for period, range_fn in [("monthly", _monthly_range), ("weekly", _weekly_range)]:
        limit = _resolve_limit("agent", agent, None, None, period)
        if limit is None:
            continue
        start, end = range_fn()
        agg = task_store.get_usage_aggregated(agent=agent, scope="agent", start=start, end=end)
        used = agg["total_cost"]
        period_info = _check_period(used, limit, start, end)
        result["periods"][period] = period_info
        if period_info and period_info["percent"] >= 100:
            result["allowed"] = False
        if period_info and period_info["percent"] >= 80:
            result["warning"] = True

    return result


# ---------------------------------------------------------------------------
# User summary (for Settings page)
# ---------------------------------------------------------------------------

def get_user_summary(user_sub: str, user_role: str, days: int = 30) -> dict:
    """Full usage summary for user settings page.

    Each period reports the PLATFORM-paid spend as ``used`` (what the limit gates),
    plus ``self_used`` (the user's own-subscription estimate, reference only) and
    ``total_used`` (grand total). The limit bar tracks ``used`` / platform.
    ``self_limits`` is the user's own API-key budget per period (``used`` =
    own-key spend, ``limit`` = their ``user_self`` cap or None).
    """
    def _period(period: str, start: str, end: str) -> dict:
        platform = task_store.get_usage_aggregated(
            user_sub=user_sub, scope="user", start=start, end=end, basis="platform",
        )["total_cost"]
        own = task_store.get_usage_aggregated(
            user_sub=user_sub, scope="user", start=start, end=end, basis="self",
        )["total_cost"]
        total = task_store.get_usage_aggregated(
            user_sub=user_sub, scope="user", start=start, end=end,
        )["total_cost"]
        limit = _resolve_limit("user_override", user_sub, "role_default", user_role, period)
        if limit is not None:
            info = _check_period(platform, limit, start, end)
        else:
            info = {"limit": None, "used": round(platform, 4), "percent": 0, "start": start, "end": end}
        info["self_used"] = round(own, 4)
        info["total_used"] = round(total, 4)
        return info

    def _self_period(period: str, start: str, end: str) -> dict:
        used = task_store.get_usage_aggregated(
            user_sub=user_sub, scope="user", start=start, end=end, basis="self_api_key",
        )["total_cost"]
        limit = _resolve_limit("user_self", user_sub, None, None, period)
        if limit is not None:
            return _check_period(used, limit, start, end)
        return {"limit": None, "used": round(used, 4), "percent": 0, "start": start, "end": end}

    m_start, m_end = _monthly_range()
    w_start, w_end = _weekly_range()
    return {
        "monthly": _period("monthly", m_start, m_end),
        "weekly": _period("weekly", w_start, w_end),
        "self_limits": {
            "monthly": _self_period("monthly", m_start, m_end),
            "weekly": _self_period("weekly", w_start, w_end),
        },
        "daily_chart": task_store.get_usage_daily(user_sub=user_sub, scope="user", days=days),
        "agent_breakdown": task_store.get_usage_by_agent(user_sub, m_start, m_end),
    }


# ---------------------------------------------------------------------------
# Admin overview
# ---------------------------------------------------------------------------

def get_admin_overview(days: int = 30) -> dict:
    """Platform-wide overview for admin page."""
    m_start, m_end = _monthly_range()

    # Platform totals this month
    totals_agg = task_store.get_usage_aggregated(start=m_start, end=m_end)

    # Count distinct active users this month
    # (use per-user breakdown for this)
    per_user = task_store.get_all_users_usage(m_start, m_end)
    active_users = sum(1 for u in per_user if u["total_cost"] > 0)

    # Per-(provider, model) breakdown rolled up once and bucketed below.
    user_breakdown_rows = task_store.get_usage_provider_breakdown("user", m_start, m_end)
    user_breakdown_by_sub: dict[str, list[dict]] = {}
    for row in user_breakdown_rows:
        sub = row.get("user_sub")
        if not sub:
            continue
        user_breakdown_by_sub.setdefault(sub, []).append({
            "provider": row.get("provider", ""),
            "model": row.get("model", ""),
            "cost": round(float(row.get("cost") or 0), 4),
        })

    agent_breakdown_rows = task_store.get_usage_provider_breakdown("agent", m_start, m_end)
    agent_breakdown_by_name: dict[str, list[dict]] = {}
    for row in agent_breakdown_rows:
        ag = row.get("agent")
        if not ag:
            continue
        agent_breakdown_by_name.setdefault(ag, []).append({
            "provider": row.get("provider", ""),
            "model": row.get("model", ""),
            "cost": round(float(row.get("cost") or 0), 4),
        })

    # Resolve limits + attach breakdown for each user. The limit gates the
    # PLATFORM-paid portion, so the percentage tracks `platform_cost` — but
    # `total_cost` stays for display (else own-sub-heavy users wrongly read 0%).
    # `platform_cost` / `self_cost` come through from get_all_users_usage via **u.
    users_with_limits = []
    for u in per_user:
        m_limit = _resolve_limit("user_override", u["sub"], "role_default", u["role"], "monthly")
        platform = u.get("platform_cost", 0) or 0
        m_pct = round((platform / m_limit) * 100, 1) if m_limit and m_limit > 0 else 0
        users_with_limits.append({
            **u,
            "monthly_limit": round(m_limit, 2) if m_limit is not None else None,
            "monthly_percent": m_pct,
            "breakdown": user_breakdown_by_sub.get(u["sub"], []),
        })

    # Agent-scoped usage
    per_agent = task_store.get_all_agents_usage(m_start, m_end)
    agents_with_limits = []
    for a in per_agent:
        a_limit = _resolve_limit("agent", a["agent"], None, None, "monthly")
        a_pct = round((a["total_cost"] / a_limit) * 100, 1) if a_limit and a_limit > 0 else 0
        agents_with_limits.append({
            **a,
            "monthly_limit": round(a_limit, 2) if a_limit is not None else None,
            "monthly_percent": a_pct,
            "breakdown": agent_breakdown_by_name.get(a["agent"], []),
        })

    # Unattended spend per app (APPS.md "Handlers"): runs a handler fired,
    # named by the app's title while the row exists.
    apps_usage = []
    for a in task_store.get_usage_by_app(m_start, m_end):
        app_row = task_store.get_app(a.get("app_id") or "")
        apps_usage.append({
            **a,
            "total_cost": round(float(a.get("total_cost") or 0), 4),
            "title": (app_row or {}).get("title") or (app_row or {}).get("slug") or a.get("app_id"),
            "slug": (app_row or {}).get("slug") or "",
            "agent": (app_row or {}).get("agent") or "",
            "scope": db_apps.app_scope(app_row.get("username")) if app_row else "gone",
        })

    # Judge spend per check (CHECKS.md "Spend"): the judge runs a check
    # started, grouped by agent and check.
    checks_usage = [
        {**c, "total_cost": round(float(c.get("total_cost") or 0), 4)}
        for c in task_store.get_usage_by_check(m_start, m_end)
    ]

    daily_chart = task_store.get_usage_daily(days=days)

    # Platform-wide rollups for the prominent "Costs by Provider" /
    # "Costs by Model" sections at the top of the admin Usage page.
    # Combines user-scope and agent-scope so the totals match the headline.
    provider_totals = [
        {"provider": r["provider"], "cost": round(float(r["cost"]), 4),
         "message_count": int(r["message_count"])}
        for r in task_store.get_usage_totals_by_provider(m_start, m_end)
    ]
    model_totals = [
        {"provider": r["provider"], "model": r["model"],
         "cost": round(float(r["cost"]), 4),
         "message_count": int(r["message_count"])}
        for r in task_store.get_usage_totals_by_model(m_start, m_end)
    ]

    return {
        "totals": {
            "cost": round(totals_agg["total_cost"], 4),
            "messages": totals_agg["total_messages"],
            "active_users": active_users,
        },
        "daily_chart": daily_chart,
        "provider_totals": provider_totals,
        "model_totals": model_totals,
        "users": users_with_limits,
        "agents": agents_with_limits,
        "apps": apps_usage,
        "checks": checks_usage,
    }
