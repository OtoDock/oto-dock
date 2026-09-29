"""Judge spend (CHECKS.md "Spend"): the per-agent daily cap — the agent's
own setting, else the platform's ``checks_daily_cap_usd``, else none — and
today's total from the judge runs (``task_runs.task_type='check'`` joined
to the usage ledger through the run's chat, as the unattended grouping in
USAGE.md). A cap that is reached is a visible ``skipped`` verdict, never a
silent pass."""

from __future__ import annotations

from datetime import datetime, timezone

from storage import database as task_store
from storage.checks import db_checks

PLATFORM_SETTING = "checks_daily_cap_usd"


def cap_for(agent: str) -> float | None:
    row = db_checks.get_settings(agent)
    cap = row.get("daily_cap_usd")
    if cap is not None:
        return float(cap)
    raw = (task_store.get_platform_setting(PLATFORM_SETTING) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def day_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def spent_today(agent: str) -> float:
    return db_checks.judge_spend_today(agent, day_start_iso())


def over_cap(agent: str) -> tuple[bool, float, float | None]:
    """``(over, spent, cap)``; never over without a cap."""
    cap = cap_for(agent)
    if cap is None:
        return False, 0.0, None
    spent = spent_today(agent)
    return spent >= cap, spent, cap
