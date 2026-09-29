"""A task's schedule in words, for the ``tasks`` feed and the task REST view.

The dashboard's ``describeSchedule`` (``dashboard/src/lib/format.ts``) is
the same vocabulary; ``dashboard/src/tests/fixtures/scheduleText.json``
pins both sides case by case. An app page prints ``schedule_text`` instead
of humanizing a cron itself, so the two must never drift.

Stdlib only, no ``core.*`` imports (the standalone scheduler imports from
this package). Every field is read with ``.get``: the REST view also
renders duck-typed rows in tests.
"""

from __future__ import annotations

import contextlib
import re
import zoneinfo
from collections.abc import Mapping
from datetime import datetime

from services.scheduler import task_kinds

_DOW =["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_DOW_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # datetime.weekday order
_MON_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MINUTE, _HOUR, _DAY = 60, 3600, 86400


def _ord(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fixed_time(cron: str) -> str | None:
    """``HH:MM`` when both minute and hour are plain numbers, else None."""
    parts = cron.strip().split()
    if len(parts) != 5:
        return None
    minute, hour = parts[0], parts[1]
    if minute.isdigit() and hour.isdigit():
        return f"{int(hour):02d}:{int(minute):02d}"
    return None


def cron_words(cron: str) -> str:
    """Standard 5-field cron in words; the raw string for a shape not
    recognized. Day-of-week is standard cron (0 or 7 = Sunday)."""
    if not cron:
        return ""
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, _, dow = parts
    every = "*"
    time = _fixed_time(cron)

    if cron.strip() == "* * * * *":
        return "Every minute"
    m = re.fullmatch(r"\*/(\d+)", minute)
    if m and hour == every and dom == every and dow == every:
        return f"Every {m.group(1)} minutes"
    h = re.fullmatch(r"\*/(\d+)", hour)
    if minute.isdigit() and h and dom == every and dow == every:
        return (f"Every {h.group(1)} hours" if int(minute) == 0
                else f"Every {h.group(1)} hours at :{int(minute):02d}")
    if time and dom == every and dow == every:
        return f"Daily at {time}"
    if time and dom == every and dow.isdigit():
        day = _DOW[int(dow) % 7] if int(dow) <= 7 else dow
        return f"Weekly on {day} at {time}"
    if time and dom.isdigit() and dow == every:
        return f"Monthly on the {_ord(int(dom))} at {time}"
    if time and "," in dom and dow == every and all(d.isdigit() for d in dom.split(",")):
        days = " & ".join(_ord(int(d)) for d in dom.split(","))
        return f"On the {days} of each month at {time}"
    if time and dom == every and dow == "1-5":
        return f"Weekdays at {time}"
    if time and dom == every and dow in ("0,6", "6,0"):
        return f"Weekends at {time}"
    return cron


def duration_words(seconds: int) -> str:
    """``5 minutes``, ``1 hour``, ``2 days``, ``1d 1h 1m 1s``."""
    if seconds == _MINUTE:
        return "1 minute"
    if seconds == _HOUR:
        return "1 hour"
    if seconds == _DAY:
        return "1 day"
    if seconds % _DAY == 0:
        return f"{seconds // _DAY} days"
    if seconds % _HOUR == 0:
        return f"{seconds // _HOUR} hours"
    if seconds % _MINUTE == 0:
        return f"{seconds // _MINUTE} minutes"
    rest = seconds
    parts: list[str] = []
    for unit, label in ((_DAY, "d"), (_HOUR, "h"), (_MINUTE, "m")):
        n, rest = divmod(rest, unit)
        if n:
            parts.append(f"{n}{label}")
    if rest:
        parts.append(f"{rest}s")
    return " ".join(parts)


def interval_words(seconds: int) -> str:
    if seconds == _MINUTE:
        return "Every minute"
    if seconds == _HOUR:
        return "Every hour"
    if seconds == _DAY:
        return "Every day"
    return f"Every {duration_words(seconds)}"


def _run_at_words(run_at: str, zone: str) -> str:
    """``Once on Sat 20 Sep, 05:00``: a naive ISO is a wall clock in the
    task's zone and prints literally; an aware one is converted into it."""
    try:
        d = datetime.fromisoformat(run_at)
    except ValueError:
        return f"Once on {run_at}"
    if d.tzinfo is not None and zone:
        with contextlib.suppress(zoneinfo.ZoneInfoNotFoundError, ValueError):
            d = d.astimezone(zoneinfo.ZoneInfo(zone))
    return f"Once on {_DOW_SHORT[d.weekday()]} {d.day} {_MON_SHORT[d.month - 1]}, {d:%H:%M}"


def zone_of(task: Mapping) -> str:
    return str(task.get("effective_tz") or task.get("user_tz") or "")


def has_clock(task: Mapping) -> bool:
    """Whether the words carry a wall-clock time, i.e. depend on the zone:
    a run_at, or a recognized cron with a fixed hour and minute."""
    if task.get("task_type") == task_kinds.TRIGGER:
        return False
    if task.get("run_at"):
        return True
    cron = str(task.get("schedule") or "")
    return bool(cron) and _fixed_time(cron) is not None and cron_words(cron) != cron


def words(task: Mapping) -> str:
    """The schedule in words, without the zone."""
    if task.get("task_type") == task_kinds.TRIGGER:
        return "On trigger"
    interval = task.get("interval_seconds")
    if interval:
        return interval_words(int(interval))
    cron = str(task.get("schedule") or "")
    if cron:
        return cron_words(cron)
    run_at = task.get("run_at")
    if run_at:
        return _run_at_words(str(run_at), zone_of(task))
    delay = task.get("delay_seconds")
    if delay is not None:
        return f"Once, {duration_words(int(delay))} after creation"
    return "—"


def describe(task: Mapping) -> str:
    """The words, with the zone named when they carry a clock time."""
    text = words(task)
    zone = zone_of(task)
    if zone and has_clock(task):
        return f"{text} ({zone})"
    return text
