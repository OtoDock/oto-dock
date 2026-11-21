"""The parts the health answer names, read from memory.

``GET /health`` is polled by Docker every 10 s and by a page returning to the
foreground, and anyone may call it, so it never waits on the database or the
network: a loop started at boot samples what needs a read (``sample_loop``),
and every answer is built from what the loop and the process already hold.

Five parts, one word each: the database (``ok``, ``down``), the tools'
environments (``ok``, ``failed`` while a bundled tool's is not built), the
scheduler (``ok``, ``down``), the compute machine (``ok`` while one that
shares the platform's folders is signed in, else ``down``: the platform's
chats, tasks and calls cannot run without one) and the phone service (``ok``,
``down``, ``off`` with no phone server configured).
``state`` sums them up for a reader: ``starting`` while new work is held for
the start, ``degraded`` when the database, the scheduler, a tool or the
compute machine is not working, else ``ready``. A phone service that is down
shows in its part only.
"""

from __future__ import annotations

import asyncio
import logging
import time

import config
from core import drain
from services.mcp import tool_envs

logger = logging.getLogger("otodock")

OK = "ok"
DOWN = "down"
OFF = "off"
FAILED = "failed"

READY = "ready"
STARTING = "starting"
DEGRADED = "degraded"

PARTS = ("database", "tools", "scheduler", "compute", "phone")

#: How often the loop reads, and how long one database read may take.
SAMPLE_S = 10.0
DB_BOUND_S = 2.0

# The boot migrated the database before the server listens: it answered.
_db_ok = True
_db_answered_at: float | None = None
_phone_configured = False
# One read at a time: a read that hangs on a frozen database keeps its lane
# thread, and the next rounds wait for it rather than take another.
_inflight: asyncio.Future | None = None


def _ping() -> None:
    from storage import pg
    with pg.get_conn(timeout=DB_BOUND_S) as conn:
        conn.execute("SELECT 1")


def _has_phone_server() -> bool:
    from storage.phone import phone_server_store
    return bool(phone_server_store.get_all_servers())


async def sample_once() -> None:
    """One round of the reads the answer cannot make itself."""
    global _db_ok, _db_answered_at, _phone_configured, _inflight
    from storage.pg import run_db_fast
    if _inflight is not None and not _inflight.done():
        _db_ok = False
        return
    _inflight = asyncio.ensure_future(run_db_fast(_ping))
    try:
        await asyncio.wait_for(asyncio.shield(_inflight), DB_BOUND_S + 1)
        _db_ok, _db_answered_at = True, time.monotonic()
    except Exception:  # noqa: BLE001  # any failure of the read is the part's answer
        if _db_ok:
            logger.warning("health: the database did not answer within %.0f s", DB_BOUND_S)
        _db_ok = False
        return
    try:
        _phone_configured = await run_db_fast(_has_phone_server)
    except Exception:  # noqa: BLE001  # the phone part keeps its last answer
        logger.debug("health: the phone servers were not read", exc_info=True)


async def sample_loop() -> None:
    while True:
        await sample_once()
        await asyncio.sleep(SAMPLE_S)


def _database() -> str:
    from storage import pg
    loop_pool = pg.pool_stats().get("loop") or {}
    if loop_pool.get("state") == "open":
        return DOWN
    return OK if _db_ok else DOWN


def _tools() -> str:
    """Whether every bundled tool's environment is built: the boot's scan
    found none stale (``tool_envs``)."""
    return FAILED if tool_envs.stale_names() else OK


def _scheduler() -> str:
    if config.SCHEDULER_MODE == "standalone":
        # Its own process runs it; the proxy only serves the fire routes.
        return OK
    from services.scheduler import scheduler
    return OK if scheduler.get_scheduler().running else DOWN


def _compute() -> str:
    """Whether a session of the platform's place has a machine to run on:
    the one decision's own answer for one that needs the platform's folders
    (``app_compute.place``). Read from the connection table, no store."""
    from services.apps import app_compute
    return OK if app_compute.place() is not None else DOWN


def _phone() -> str:
    if not _phone_configured:
        return OFF
    from services.phone import phone_config
    return OK if phone_config._management_clients else DOWN


def parts() -> dict[str, str]:
    return {"database": _database(), "tools": _tools(),
            "scheduler": _scheduler(), "compute": _compute(), "phone": _phone()}


def state(of: dict[str, str] | None = None) -> str:
    of = of if of is not None else parts()
    if drain.starting():
        return STARTING
    if of["database"] != OK or of["scheduler"] != OK or of["tools"] != OK or of["compute"] != OK:
        return DEGRADED
    return READY


def ready(of: dict[str, str] | None = None) -> bool:
    """The server takes new work: the database answers, a compute machine
    is signed in to run it and nothing holds."""
    of = of if of is not None else parts()
    return of["database"] == OK and of["compute"] == OK and not drain.holding()


# Read by the admin's detail and /metrics only: whether an install has
# telephony set up is nothing a stranger needs, and it degrades nothing.
_PRIVATE_PARTS = frozenset({"phone"})


def answer(build: str) -> dict:
    """The public body: no version, no count, no name, and no part that
    tells a stranger what the install has set up (the phone)."""
    of = parts()
    public = {k: v for k, v in of.items() if k not in _PRIVATE_PARTS}
    return {"status": "ok", "service": "otodock", "build": build,
            "state": state(of), "ready": ready(of), "parts": public}


def detail() -> dict:
    """The admin's: the same words and what stands behind them."""
    of = parts()
    answered = None if _db_answered_at is None else round(time.monotonic() - _db_answered_at, 1)
    return {**of, "state": state(of), "ready": ready(of),
            "tools_failed": sorted(tool_envs.stale_names()),
            "scheduler_mode": config.SCHEDULER_MODE,
            "database_answered_s_ago": answered}


def reset_for_tests() -> None:
    global _db_ok, _db_answered_at, _phone_configured, _inflight
    _db_ok, _db_answered_at, _phone_configured, _inflight = True, None, False, None
