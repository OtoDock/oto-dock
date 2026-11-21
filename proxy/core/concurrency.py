"""Resource-oriented concurrency admission for locally-executed sessions.

Admission is a **two-gate** test (NOT a static count ceiling). Every *local*
session process-tree (chat / phone / interactive-CLI / meeting participant / task)
reserves a coarse per-TYPE memory estimate, and a new local session is admitted
iff BOTH gates pass:

  GATE 1 (reservation budget):  reserved + est ≤ budget        (budget = total_RAM × BUDGET_FRACTION)
  GATE 2 (live-RAM veto):       live_available − est ≥ FLOOR

Gate 1 is instant + deterministic — its running ``reserved`` sum bounds a burst
(no grow-in window needed). Gate 2 reads the *real* free RAM (cgroup/proc,
reclaimable-aware) so small boxes pack to real capacity and the box can't OOM
under non-session pressure or estimate error. ``est`` is HEAVY for an engine
with its own process tree (CLI + STDIO MCP children); for Direct-LLM (no CLI
process) it counts the stdio MCPs the proxy starts for the session (LIGHT when
the config is unknown, never above HEAVY); Docker MCPs (sibling containers)
and remote MCPs (HTTP) add ~0.

**A personal or admin machine's sessions never count** —
``acquire(target=<machine_id>)`` returns immediately, untracked (the
satellite enforces its own count). **A compute machine's do**: a session it
runs for the platform reserves here on that machine's *box*
(``_session_box``), the box the session's registered placement or the
caller's target (``placement.admission_target``) names. The install's
compute machine shares the host with the platform, so the budget, the count
cap, the queue and the grow-in debit stay one; Gate 2 reads the box's own free
memory (the machine's report at its sign-in and in every heartbeat, nothing
known admits nothing), and a box whose report says it has a cap of its own
keeps its reservations under that cap too.

**Graceful eviction under pressure**: every denied interactive admit runs the
eviction loop, which frees BOTH gates (an evicted session returns budget to
Gate 1 and real RAM to Gate 2). Which gate binds is NOT the attribution signal
by itself: on an uncapped small host the Gate-1 budget is a fraction of HOST
RAM shared with sidecar containers and may never fill, so genuine session
pressure surfaces as a Gate-2 veto. Attribution instead asks "do tracked
sessions plausibly account for the missing RAM?" (``reserved ≥ shortfall``):
yes → evict idle sessions / report "busy"; no → evicting users can't close the
gap → stop and report "host_memory". (2026-07-06 audit: the previous
gate-identity attribution inverted into the OPPOSITE lie on small hosts.)
Only ``chat`` reservations idle past the floor are candidates, an unused
pre-warm first; a turn in progress and running background work are spared up
to the background-work ceiling.

**Last-resort admit**: the veto never locks an EMPTY platform out — with zero
tracked local sessions the one new session is admitted over both gates (loudly
logged). One swap-backed slow session beats a user locked out of their own box.

**Grow-in debit**: Gate 2 debits the un-materialized remainder of recently
admitted sessions (linear decay over ``_GROW_WINDOW_S``) — N near-simultaneous
warmups otherwise all read the same free RAM before any of them grows into it.

**Denials carry their reason**: ``acquire()`` returns an :class:`Admission`
(truthy = admitted) with ``reason`` ("busy" = sessions genuinely fill the box;
"host_memory" = something ELSE eats host RAM) + ``user_message`` — the text
must not blame "too many active sessions" when the admin page truthfully shows
zero, nor blame the host when idle sessions hold the RAM. Every denial surface
shows ``Admission.user_message`` instead of a hardcoded guess.

**Admission queue** (opt-in per call site, ``queue_wait_s``): a denied
interactive admit waits in arrival order for a release instead of losing it
to whoever retries first. Only the head of the line attempts; a host-memory
or cap denial never waits.

**Background wakes** (a delegate result or a continuation reaching a chat
whose session is gone) reserve before they spawn (``reserve_background``):
task headroom, parked while the box is full, and they give way the moment a
person starts using that chat.

**Per-person cap**: the ledger records who each session runs for
(``_session_owner``, filled by the first acquire that knows it) and a new
chat for a person who already holds ``MAX_SESSIONS_PER_USER`` of them first
closes one of that person's own idle sessions, else is refused with its own
sentence. Only a new chat is capped: re-attaching, phone calls, meetings,
tasks and background wakes are not.

State: ``_sessions`` (sid→kind) + ``_session_est`` (sid→MB) + ``_reserved_mb``
+ ``_session_owner``, guarded by one ``asyncio.Condition``. One entry per id ⇒
no double-count.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

import config
from core import concurrency_paired, placement
from core.background import spawn
from core.concurrency_scan import (
    _ADMITTED,
    _DENY_SPECULATIVE,
    _GROW_WINDOW_S,
    _STDIO_MCP_MB,
    Admission,
    _at_user_cap,
    _box_reserved_mb,
    _deny_busy,
    _deny_busy_background,
    _deny_user_cap,
    _estimate_mb,
    _fill_owner,
    _grow_debits,
    _growin_debit_mb,
    _is_head,
    _line,
    _live_local_sids,
    _oldest_evictable_local,
    _owned,
    _ring_join,
    _ring_leave,
    _satellite_stats,
    _session_added_at,
    _session_box,
    _session_est,
    _session_estimate,
    _session_grown,
    _session_host,
    _session_owner,
    _sessions,
    _swap_credit_mb,
    _task_ring,
    _task_ring_order,
    compute_reading,
    compute_sessions,
    host_of,
    placed_on_compute,
)
from core.sandbox import host_resources
from core.session import background_leash, session_kind

logger = logging.getLogger("otodock.concurrency")

# Surface kinds tracked in _sessions (phone = live phone calls).
_SURFACES = ("chat", "task", "meeting", "phone")

# Reconciler grace: a sid added more recently than this is spared (may be mid-spawn).
_RECONCILE_GRACE_S = 120
# Maintenance loop tick (parked-task wakeups + task-side eviction).
_MAINTENANCE_INTERVAL_S = 30
# Live-RAM read cache TTL — a burst of admits under _cond reads the cgroup once.
_LIVE_CACHE_TTL_S = 1.0

# Admission queue: the head re-attempts at least this often (a live-RAM change
# or a session ageing past the eviction floor notifies nobody).
_QUEUE_SLICE_S = 5.0
# A parked background wake re-checks whether a person took the chat over.
_WAKE_SLICE_S = 1.0

# An eviction source whose victim an engine's layer holds in the proxy.
_EVICT_CLOSE = {"direct": "direct-llm"}


_DENY_TOOL_MEMORY = Admission(
    False, "tool_memory",
    "There is not enough free memory to start this tool right now. Try again in a few minutes.",
)

# A session of the platform's place with no compute machine to take it
# (``placement.NO_COMPUTE``): its own reason word and the one sentence every
# door says, decided before any gate, so it never reads as "low on memory"
# and never reaches a paired computer's check or the platform's own box.
_DENY_NO_COMPUTE = Admission(False, "no_compute", placement.NO_COMPUTE_SENTENCE)
# How long a task, a delegated lane, a judge and a wrap-up wait for a compute
# machine before their run fails with that sentence.
NO_COMPUTE_WAIT_S = 300.0


def _grow_on_paired(session_id: str, judged: bool) -> Admission:
    """A tool's late start in a session on a personal or admin machine: with
    room in that machine's memory, else the tool's sentence."""
    if not judged:
        return _ADMITTED
    from core.session import session_manager
    found = session_manager.live_session(session_id)
    machine = found.machine_id if found is not None else ""
    if not machine or concurrency_paired.room(machine, _STDIO_MCP_MB) is not False:
        return _ADMITTED
    logger.info("Tool start refused (machine memory): %s on %s", session_id[:8], placement.log_id(machine))
    return _DENY_TOOL_MEMORY


def _deny_host_memory(est: int, box: str = "") -> Admission:
    # NOT session pressure: name the real cause, or the message contradicts the
    # admin page (which can truthfully show 0 active sessions).
    return Admission(
        False, "host_memory",
        f"This server is low on memory: {_box_live_mb(box)} MB free, about "
        f"{est + _box_floor_mb(box)} MB needed to start. Free some memory on the server, "
        "then try again.",
    )
_reserved_mb: int = 0                     # cached Σ _session_est of the platform's host
_parked_tasks: int = 0                    # count of tasks blocked in acquire

_cond: asyncio.Condition | None = None    # initialized in init()

_budget_mb: int = 0                       # GATE-1 budget (total × BUDGET_FRACTION), cached at init
_total_mb: int = 0                        # container memory limit (for the gauge)
_floor_mb: int = 0                        # GATE-2 floor, cached at init

_live_cache: tuple[float, int] | None = None   # (monotonic_ts, available_mb)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def _read_limits() -> None:
    """The platform's total, budget and floor: at start, and again at every
    maintenance tick, so a container cap changed while it runs counts (a
    compute machine's limit is read again at every report)."""
    global _budget_mb, _total_mb, _floor_mb
    _total_mb = max(1, host_resources.detect_memory_limit_bytes() // (1024 * 1024))
    _budget_mb = int(_total_mb * config.BUDGET_FRACTION)
    _floor_mb = _reserve_of(_total_mb)


def init() -> None:
    """Create the condition and read the budget/floor. Call once at startup."""
    global _cond

    _cond = asyncio.Condition()
    _read_limits()

    logger.info(
        "Concurrency: budget=%dMB (%.0f%% of %dMB) · gate-2 floor=%dMB · est heavy/light=%d/%d MB "
        "· evict_floor=%ds · hard_cap=%s · per_user_cap=%s",
        _budget_mb, config.BUDGET_FRACTION * 100, _total_mb, _floor_mb,
        config.SESSION_EST_HEAVY_MB, config.SESSION_EST_LIGHT_MB, config.SESSION_EVICT_FLOOR_S,
        config.OTODOCK_MAX_LOCAL_SESSIONS or "off", config.MAX_SESSIONS_PER_USER or "off",
    )


def _live_available_mb() -> int:
    """Live allocatable RAM (MB), cached ~1 s. Fail-closed: 0 on read failure."""
    global _live_cache
    now = time.monotonic()
    if _live_cache is not None and now - _live_cache[0] < _LIVE_CACHE_TTL_S:
        return _live_cache[1]
    try:
        mb = host_resources.live_available_bytes() // (1024 * 1024)
        # Swap credit (see config.SESSION_SWAP_CREDIT_MB): MemAvailable counts
        # zero swap; credit half the free swap, capped, so a small box with
        # swap packs one more session by paging cold heap instead of denying.
        # Never credits a fail-closed 0 read ("we know nothing" stays 0).
        if mb:
            mb += _swap_credit_mb()
    except (OSError, ValueError):
        mb = 0
    _live_cache = (now, mb)
    return mb


# A compute box's evicted estimates since its last report: the report is
# the only read of its free memory and an eviction does not change it, so
# each one counts as freed until a report newer than it (box -> [(when, MB)]).
_evicted_credit: dict[str, list[tuple[float, int]]] = {}


def _reported_at(box: str) -> float:
    """When a compute machine's last memory report was taken (monotonic)."""
    from core.remote.satellite_connection import get_connection_manager
    held = get_connection_manager().machine_memory_report(box, compute=True)
    return time.monotonic() - (held[1] if held else 0.0)


def _box_live_mb(box: str = "") -> int:
    """A box's free memory: the platform's own read, or a compute machine's
    report (0 when none is fresh) plus what it evicted since that report.
    A reading writes nothing: the balancer reads it from a worker thread
    (``box_room_mb``), and the stale credits go when one is written
    (``_note_evicted_credit``)."""
    if not box:
        return _live_available_mb()
    reading = compute_reading(box)
    if not reading.known:
        return 0
    reported_at = _reported_at(box)
    credit = [c for c in tuple(_evicted_credit.get(box, ())) if c[0] > reported_at]
    return reading.available_mb + sum(mb for _, mb in credit)


def _note_evicted_credit(box: str, mb: int) -> None:
    """What an eviction on a compute box gives back until its next report,
    with the credits that report already shows dropped. On the loop."""
    reported_at = _reported_at(box)
    kept = [c for c in _evicted_credit.get(box, []) if c[0] > reported_at]
    _evicted_credit[box] = [*kept, (time.monotonic(), mb)]


def _reserve_of(total_mb: int) -> int:
    """The reserve of the platform's host or a compute machine of this size:
    the larger of ``SESSION_RESERVE_FLOOR_MB`` and its share (``host_resources``,
    the numbers a machine's guard reads too)."""
    return host_resources.reserve_mb(total_mb, floor_mb=config.SESSION_RESERVE_FLOOR_MB,
                                     share=host_resources.COMPUTE_SHARE)


def _box_total_mb(box: str = "") -> int:
    return _total_mb if not box else compute_reading(box).total_mb


def _box_floor_mb(box: str = "") -> int:
    """Gate 2's floor for a box: its reserve (``_reserve_of``)."""
    if not box:
        return _floor_mb
    return _reserve_of(compute_reading(box).total_mb)


def _task_headroom_mb(box: str = "") -> int:
    """What a task or a background wake leaves free on a box, in both gates:
    one HEAVY, or a thirty-second of the box's room on a small machine, so
    the smallest server still starts a task beside a resting chat."""
    return host_resources.task_headroom_mb(_box_total_mb(box), _box_floor_mb(box),
                                           config.SESSION_EST_HEAVY_MB)


def _box_budget_mb(box: str) -> int | None:
    """A compute box's own budget, from its report: a compute machine on
    another host always (it is a host of its own), one beside the platform
    only when its report says it has a cap of its own; None otherwise."""
    if not box:
        return None
    reading = compute_reading(box)
    if not reading.capped and not host_of(box):
        return None
    return int(reading.total_mb * config.BUDGET_FRACTION)


def _box_cap_ok(box: str, est: int, headroom: int) -> bool:
    """A compute box with a budget of its own keeps its own reservations
    under it."""
    budget = _box_budget_mb(box)
    return budget is None or _box_reserved_mb(box) + est <= budget - headroom


def _host_count(host: str) -> int:
    """The sessions that hold a slot on one host. Over copies of the tables:
    the balancer reads it from a worker thread beside the loop."""
    hosts = dict(_session_host)
    return sum(1 for sid in list(_sessions) if hosts.get(sid, "") == host)


def _gate1(est: int, *, is_task: bool, box: str = "") -> bool:
    """Reservation budget (instant). Tasks keep a headroom (``_task_headroom_mb``)
    so background work never consumes the room a human interactive session needs, and, under
    a hard cap of 2 or more, one slot of the count (under a cap of 1 a task
    would never run). The budget and the count are the platform's host's, its
    own box and every compute machine beside it together; a capped compute box
    adds its own cap. A compute machine on another host is judged against its
    own report's budget alone, and the count does not cap it."""
    headroom = _task_headroom_mb(box) if is_task else 0
    if host_of(box):
        return _box_cap_ok(box, est, headroom)
    cap = config.OTODOCK_MAX_LOCAL_SESSIONS
    if cap and _host_count("") + (1 if is_task and cap >= 2 else 0) >= cap:
        return False
    return _reserved_mb + est <= _budget_mb - headroom and _box_cap_ok(box, est, headroom)


def _gate2(est: int, *, is_task: bool, box: str = "") -> bool:
    """Live-RAM OOM veto (grow-in-debited on the box's host), on the box's
    own free memory. Same task headroom as gate 1."""
    headroom = _task_headroom_mb(box) if is_task else 0
    return _box_live_mb(box) - _growin_debit_mb(host_of(box)) - est >= _box_floor_mb(box) + headroom


def box_room_mb(box: str = "") -> int:
    """How much a new session could take on a box now: the smaller of its
    host's Gate 1 room and its Gate 2 room, none on the platform's host at its
    count cap. The balancer ranks the compute machines by it
    (``remote_status.serving_compute_machine``)."""
    cap = config.OTODOCK_MAX_LOCAL_SESSIONS
    if not host_of(box) and cap and _host_count("") >= cap:
        return 0
    gate2 = _box_live_mb(box) - _growin_debit_mb(host_of(box)) - _box_floor_mb(box)
    own = _box_budget_mb(box)
    gate1 = own - _box_reserved_mb(box) if own is not None else _budget_mb - _reserved_mb
    if own is not None and not host_of(box):
        gate1 = min(gate1, _budget_mb - _reserved_mb)
    return max(0, min(gate1, gate2))


def _has_room(est: int, *, is_task: bool, box: str = "") -> bool:
    return _gate1(est, is_task=is_task, box=box) and _gate2(est, is_task=is_task, box=box)


def _speculative_room(est: int, owner: str, box: str = "") -> bool:
    """Room for this session AND one HEAVY more, so a speculative spawn never
    takes the last slot a real session needs: the count cap, the budget and
    the live read (grow-in debited), nobody parked or queued for a slot, and
    the person below their cap."""
    heavy = config.SESSION_EST_HEAVY_MB
    host = host_of(box)
    cap = config.OTODOCK_MAX_LOCAL_SESSIONS
    if not host and cap and _host_count("") + 2 > cap:
        return False
    if _parked_tasks > 0 or any(h == host for _t, h in _line):
        return False
    if owner and config.MAX_SESSIONS_PER_USER > 0 and _at_user_cap(owner):
        return False
    if (not host and _reserved_mb + est + heavy > _budget_mb) or not _box_cap_ok(box, est + heavy, 0):
        return False
    return _box_live_mb(box) - _growin_debit_mb(host) - est - heavy >= _box_floor_mb(box)


def prewarm_allowed(execution_path: str | None = None, *, user_sub: str | None = None,
                    shared_folder: bool = False) -> bool:
    """Whether a pre-warm may start now: a compute machine serves the engine's place, its box has room
    for the pre-warm and one more session (``fit_heavy >= 2``), nobody waits for a slot, and the person
    holds fewer sessions than their cap. Read-only; a pre-warm site asks before building the session's
    config, so with no compute machine nothing is built and nothing is said. ``shared_folder``: the
    site's ``remote_status.keeps_beside``, read off the loop (this runs on it and reads no store)."""
    if _cond is None:
        return False
    from core.session.session_manager import compute_placement_for
    built = compute_placement_for(execution_path or "", shared_folder=shared_folder)
    if built is None:
        return False
    return _speculative_room(_estimate_mb(execution_path), user_sub or "", str(built.machine_id))


def _add(session_id: str, kind: str, est: int, owner: str = "", box: str = "") -> None:
    """Reserve a slot. est is written ONLY here (never on idempotent re-acquire)."""
    global _reserved_mb
    assert session_id not in _session_est, f"double _add for {session_id}"
    _sessions[session_id] = kind
    _session_est[session_id] = est
    _session_added_at[session_id] = time.monotonic()
    if owner:
        _session_owner[session_id] = owner
    if box:
        _session_box[session_id] = box
    host = host_of(box)
    if host:
        _session_host[session_id] = host
    else:
        _reserved_mb += est


def _move_box(session_id: str, box: str) -> None:
    """A held reservation moved to another box, its host's sums with it."""
    global _reserved_mb
    est = _session_est.get(session_id, 0)
    if not _session_host.pop(session_id, ""):
        _reserved_mb = max(0, _reserved_mb - est)
    if box:
        _session_box[session_id] = box
    else:
        _session_box.pop(session_id, None)
    host = host_of(box)
    if host:
        _session_host[session_id] = host
    else:
        _reserved_mb += est


def _remove(session_id: str) -> str | None:
    """Single teardown: pops _sessions/_session_est/_session_added_at/_session_owner
    + frees the reservation. Returns the prior kind, or None if it wasn't tracked."""
    global _reserved_mb
    kind = _sessions.pop(session_id, None)
    est = _session_est.pop(session_id, None)
    _session_added_at.pop(session_id, None)
    _session_owner.pop(session_id, None)
    _session_box.pop(session_id, None)
    _session_grown.pop(session_id, None)
    if est and not _session_host.pop(session_id, ""):
        _reserved_mb = max(0, _reserved_mb - est)
    return kind


# ---------------------------------------------------------------------------
# Notify plumbing (sync release/maintenance → locked notify_all)
# ---------------------------------------------------------------------------

async def _notify_waiters() -> None:
    if _cond is None:
        return
    async with _cond:
        _cond.notify_all()


def _schedule_notify() -> None:
    """Hand a lock-held notify to the running loop from a synchronous caller.
    Done UNCONDITIONALLY whenever a slot frees (gating on 'are there waiters?'
    without the lock races a task mid-suspend in wait_for and loses the wakeup)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("concurrency: release outside a running loop; notify skipped")
        return
    spawn(_notify_waiters(), name="notify_waiters", owner="concurrency")


# ---------------------------------------------------------------------------
# Acquire / release
# ---------------------------------------------------------------------------

def _box_for(session_id: str, target: str) -> str | None:
    """The box a session reserves on: the compute machine its registered
    placement names (a resume or a re-attach passes the chat's ``local``
    pin) while that machine is connected or the remote layer still holds the
    session (its machine in its grace), else its caller's target, ``""`` for
    the platform's own and a connected compute machine's id for one
    (``placement.admission_target``); None for a personal or admin machine,
    which counts its own sessions. A placement kept across a restart whose
    machine is gone resumes on the platform: its box knows nothing."""
    from core.remote.satellite_connection import get_connection_manager
    from core.session import session_manager
    manager = get_connection_manager()
    machine = placed_on_compute(session_id)
    if machine and (manager.is_compute(machine) or session_manager.live_session(session_id) is not None):
        return machine
    if placement.is_local(target):
        return ""
    return target if manager.is_compute(target) else None


def _no_machine(session_id: str, target: str) -> bool:
    """Whether the session is the platform's with no compute machine to take
    it (``placement.NO_COMPUTE``, what ``placement.admission_target`` answers
    for it) and no machine holds it already."""
    return target == placement.NO_COMPUTE and not _box_for(session_id, placement.LOCAL)


def compute_signed_in(machine_id: str = "") -> None:
    """A compute machine signed in: what waits for one looks again (a task
    parked on a box that knew nothing, ``wait_for_compute``), and the time
    it went away is forgotten, so a later absence is counted from its own
    start. Called beside the sign-in's other steps
    (``compute_bootstrap.on_alive_report``)."""
    if machine_id:
        _box_away_since.pop(machine_id, None)
    _schedule_notify()


async def wait_for_compute(serves: Callable[[], bool], *, timeout_s: float = NO_COMPUTE_WAIT_S) -> bool:
    """Wait until ``serves()`` answers true (a compute machine that can take
    the caller's session is connected), at most ``timeout_s``: woken by a
    sign-in (``compute_signed_in``), by any slot that frees and by the
    maintenance tick. True when one serves. For a run that takes its slot
    before it builds: it waits here, then its build either lands on the
    machine or is refused with the sentence, which is the run's report."""
    if serves():
        return True
    if _cond is None:
        return False
    try:
        async with _cond:
            await asyncio.wait_for(_cond.wait_for(serves), timeout=max(0.0, timeout_s))
    except asyncio.TimeoutError:
        return False
    return True


async def acquire(session_id: str, kind: str, *, target: str = placement.LOCAL,
                  execution_path: str | None = None, blocking: bool = False,
                  user_sub: str | None = None, per_user_cap: bool = True,
                  speculative: bool = False, queue_wait_s: float | None = None,
                  mcp_config_path: str = "", ring_key: str = "") -> Admission:
    """Acquire a local-session slot.

    Returns an :class:`Admission` (truthy = admitted) — admitted immediately
    when the target is a machine (satellite-budgeted, not counted) or the id is
    already tracked (idempotent). ``blocking`` (tasks) wait until both gates
    allow; interactive admits check-and-reject, but on a Gate-1 (budget) failure
    first try to evict an idle local session to make room. A denial carries
    ``reason`` + ``user_message`` ("busy" = sessions fill the budget/cap;
    "host_memory" = the Gate-2 live-RAM veto, i.e. NON-session pressure;
    "user_cap" = the person already holds their share).

    ``user_sub`` records whom the session runs for. A new chat (not blocking,
    ``per_user_cap``) of a known person is held to ``MAX_SESSIONS_PER_USER``;
    call sites that re-attach to a live session pass ``per_user_cap=False``.
    ``speculative`` (pre-warms) admits only with room for two
    (:func:`prewarm_allowed`), never evicts, and is refused quietly
    (``reason="speculative"``, no message). ``queue_wait_s`` lets a denied
    interactive admit wait that long, in arrival order, for a slot.
    ``mcp_config_path`` (the session's generated MCP config) sizes the reserve
    by the stdio tools it starts with and those that wait for their first
    call (``_estimate_mb``); each later start grows it (``grow``).

    A session of the platform's place with no compute machine to take it
    (``target`` is ``placement.NO_COMPUTE``) is refused with the sentence
    and reserves nothing, a task too: the platform runs no session itself.
    """
    if _no_machine(session_id, target):
        if not speculative:
            logger.info("Slot refused (no compute machine): %s (%s)", session_id[:8], kind)
        return _DENY_SPECULATIVE if speculative else _DENY_NO_COMPUTE
    box = _box_for(session_id, target)
    if box is None:
        return await _admit_on_paired(session_id, target, execution_path, blocking=blocking,
                                      speculative=speculative, mcp_config_path=mcp_config_path)

    assert _cond is not None, "concurrency.init() not called"
    est = await _session_estimate(session_id, execution_path, mcp_config_path)
    owner = user_sub or ""

    if blocking:
        return await _acquire_task(session_id, kind, est, owner, ring_key=ring_key, box=box)
    if speculative:
        async with _cond:
            if session_id in _sessions:
                _fill_owner(session_id, owner)
                return _ADMITTED
            if not _speculative_room(est, owner, box):
                logger.debug("Speculative slot refused: %s (no room for two)", session_id[:8])
                return _DENY_SPECULATIVE
            _add(session_id, kind, est, owner, box)
            return _ADMITTED

    cap_owner = owner if (owner and per_user_cap and kind == "chat"
                          and config.MAX_SESSIONS_PER_USER > 0) else ""
    if cap_owner:
        adm = await _make_room_under_user_cap(session_id, cap_owner)
        if adm is not None:
            return adm
    if queue_wait_s and queue_wait_s > 0:
        return await _acquire_queued(session_id, kind, est, prefer_user=user_sub,
                                     owner=owner, cap_owner=cap_owner, wait_s=queue_wait_s, box=box)

    async with _cond:
        if session_id in _sessions:
            _fill_owner(session_id, owner)
            return _ADMITTED
        if _has_room(est, is_task=False, box=box):
            if cap_owner and _at_user_cap(cap_owner):
                return _deny_user_cap(_owned(cap_owner))
            _add(session_id, kind, est, owner, box)
            logger.debug("Slot acquired: %s (%s) reserved=%d/%dMB", session_id[:8], kind,
                         _reserved_mb, _budget_mb)
            return _ADMITTED

    # Denied at first look. The eviction loop owns what happens next — it
    # frees BOTH gates (budget + real RAM), decides whether tracked sessions
    # plausibly account for the pressure (which gate binds is NOT the signal
    # by itself — see the module docstring), applies the empty-platform
    # last-resort admit, and attributes the eventual denial honestly.
    return await _admit_with_eviction(session_id, kind, est, prefer_user=user_sub,
                                      owner=owner, cap_owner=cap_owner, box=box)


async def _admit_on_paired(session_id: str, machine: str, execution_path: str | None, *,
                           blocking: bool, speculative: bool, mcp_config_path: str) -> Admission:
    """A personal or admin machine reserves nothing here; a NEW start there
    is checked against the memory its satellite reports (``concurrency_paired``):
    an interactive one closes idle sessions there first and is then refused
    with one sentence, a task waits in that machine's line. A machine whose
    memory is not known, and a session already there, pass as before."""
    if (_cond is None or speculative or placement.is_local(machine)
            or concurrency_paired.live(session_id, machine)
            or concurrency_paired.room(machine, 0) is None):
        return _ADMITTED
    est = await _session_estimate(session_id, execution_path, mcp_config_path)
    if blocking:
        await concurrency_paired.wait_in_line(session_id, machine, est, _cond)
        return _ADMITTED
    if concurrency_paired.room(machine, est) is False:
        await concurrency_paired.make_room(machine, est)
        if concurrency_paired.room(machine, est) is False:
            logger.info("Slot denied (machine memory): %s on %s, %d MB short", session_id[:8],
                        placement.log_id(machine), concurrency_paired.shortfall_mb(machine, est))
            return concurrency_paired.denial()
    concurrency_paired.note_admitted(session_id, machine, est)
    return _ADMITTED


def machine_report(machine_id: str) -> None:
    """A paired machine's memory report arrived: a task waiting in its line
    reads it now."""
    if concurrency_paired.report_arrived(machine_id):
        _schedule_notify()


async def _acquire_queued(session_id: str, kind: str, est: int, *, prefer_user: str | None,
                          owner: str, cap_owner: str, wait_s: float, box: str = "") -> Admission:
    """Wait in arrival order, up to ``wait_s``, for a slot.

    The ticket is taken before the first attempt, so two newcomers never both
    see an empty line. Only the head attempts (the ordinary path: fast path,
    eviction, attribution): on arrival at the head, on every release or
    eviction, and every ``_QUEUE_SLICE_S``. A host-memory or cap denial
    returns at once; past the deadline the last denial does. Strict order:
    a HEAVY head keeps a LIGHT waiter that would fit behind it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    ticket: object = asyncio.current_task() or object()
    host = host_of(box)
    entry = (ticket, host)
    last: Admission | None = None
    async with cast("asyncio.Condition", _cond):
        _line.append(entry)
    try:
        while True:
            if _is_head(ticket, host):
                adm = await _admit_with_eviction(session_id, kind, est, prefer_user=prefer_user,
                                                 owner=owner, cap_owner=cap_owner, quiet=True, box=box)
                if adm or adm.reason in ("host_memory", "user_cap"):
                    return adm
                last = adm
            async with cast("asyncio.Condition", _cond):
                if session_id in _sessions:
                    _fill_owner(session_id, owner)
                    return _ADMITTED
                # Re-checked under the lock right before waiting: a release
                # during the head's lock-free eviction pass notified nobody.
                if _is_head(ticket, host) and _has_room(est, is_task=False, box=box):
                    if cap_owner and _at_user_cap(cap_owner):
                        return _deny_user_cap(_owned(cap_owner))
                    _add(session_id, kind, est, owner, box)
                    return _ADMITTED
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(cast("asyncio.Condition", _cond).wait(), min(_QUEUE_SLICE_S, remaining))
        adm = last or _deny_busy()
        logger.warning("Slot denied after %.0fs in the admission queue: %s (%s) reason=%s",
                       wait_s, session_id[:8], kind, adm.reason)
        return adm
    finally:
        # Synchronous: an awaited cleanup can be cancelled half-way and leave
        # a ticket behind that holds the line for good.
        with contextlib.suppress(ValueError):
            _line.remove(entry)
        _schedule_notify()


async def _make_room_under_user_cap(session_id: str, owner: str) -> Admission | None:
    """A person at their cap first gives up one of their own idle sessions.

    "Their own" is what the cap counts (``_cap_owner``), so every session
    closed here frees one counted seat.

    Returns the admission when the id turns out to be tracked already, the
    cap denial when every one of the person's sessions is in use, or None to
    continue with the ordinary admission (which re-checks the cap in the lock
    hold of its add: a second tab may take the seat meanwhile)."""
    floor_age: float | None = None
    while True:
        async with cast("asyncio.Condition", _cond):
            if session_id in _sessions:
                _fill_owner(session_id, owner)
                return _ADMITTED
            held = _owned(owner)
            if held < config.MAX_SESSIONS_PER_USER:
                return None
        if floor_age is None:
            floor_age = min(config.SESSION_EVICT_FLOOR_S, await _idle_timeout())
        ceiling = background_leash.background_work_ceiling()
        scan = await _oldest_evictable_local(
            floor_age, prefer_user=owner, only_user=owner,
            pending_leash_s=ceiling, turn_leash_s=ceiling, prewarm_any_age=True,
        )
        if scan.victim is None:
            logger.info("Slot denied (per-person cap): %s owner=%s… holds %d "
                        "(spared: %d with background work, %d mid-turn)",
                        session_id[:8], owner[:8], held,
                        scan.spared_background, scan.spared_live)
            return _deny_user_cap(held)
        await _evict_one(*scan.victim)


#: When each compute box a task waits on was first seen away.
_box_away_since: dict[str, float] = {}


def _box_left(box: str) -> bool:
    """Whether a waiting task's compute machine has been away for longer than
    ``NO_COMPUTE_WAIT_S``: the ticket then gives up and its run's build
    decides (another machine, or the sentence). While the machine is away
    for less, its box knows nothing and the ticket stays parked: a sign-in
    wakes it (``compute_signed_in``), so a short restart of the compute
    machine costs a waiting run nothing."""
    if not box:
        return False
    from core.remote.satellite_connection import get_connection_manager
    if get_connection_manager().is_compute(box):
        _box_away_since.pop(box, None)
        return False
    return time.monotonic() - _box_away_since.setdefault(box, time.monotonic()) > NO_COMPUTE_WAIT_S


def _ring_turn(key: str, ticket: tuple[str, int, str]) -> bool:
    """Whether ``ticket`` is the one the ring admits now: the head of the
    first creator in ring order whose head fits (a heavy head of one creator
    never blocks a lighter head of another)."""
    for k in _task_ring_order:
        line = _task_ring.get(k)
        if not line:
            continue
        head = line[0]
        if _has_room(head[1], is_task=True, box=head[2]):
            return k == key and head == ticket
    return False


def _ring_rotate(key: str) -> None:
    """The admitted creator goes to the back; the next head is woken."""
    with contextlib.suppress(ValueError):
        _task_ring_order.remove(key)
    if key in _task_ring:
        _task_ring_order.append(key)
    _schedule_notify()


async def _acquire_task(session_id: str, kind: str, est: int, owner: str = "",
                        ring_key: str = "", box: str = "") -> Admission:
    """Blocking task acquire — waits until both gates allow (with the task headroom),
    in the parked tasks' ring: the creators take turns. Task-side eviction is
    driven by the maintenance loop (keeps wait_for pure)."""
    global _parked_tasks
    async with cast("asyncio.Condition", _cond):
        if session_id in _sessions:
            _fill_owner(session_id, owner)
            return _ADMITTED
        if not _has_room(est, is_task=True, box=box) or _task_ring:
            key = ring_key or "-"
            ticket = (session_id, est, box)
            _ring_join(key, ticket)
            _parked_tasks += 1
            logger.info("Task %s parked: no slot (reserved=%d/%dMB, sessions=%d)",
                        session_id[:8], _reserved_mb, _budget_mb, len(_sessions))
            try:
                await cast("asyncio.Condition", _cond).wait_for(
                    lambda: session_id in _sessions or _box_left(box) or _ring_turn(key, ticket))
            finally:
                _parked_tasks -= 1  # decrement even on Cancelled/Timeout
                _ring_leave(key, ticket)
                _ring_rotate(key)
        if session_id in _sessions:
            _fill_owner(session_id, owner)
            return _ADMITTED
        if _box_left(box):
            logger.info("Task %s gave up its place in line: its compute machine %s stayed away",
                        session_id[:8], placement.log_id(box))
            return _DENY_NO_COMPUTE
        _add(session_id, kind, est, owner, box)
        return _ADMITTED


def _pressure_snapshot(est: int, box: str = "") -> tuple[bool, int, bool]:
    """(gate1_ok, live_mb, sessions_account) — call under ``_cond``.

    ``sessions_account`` answers "could evicting tracked sessions plausibly
    close the Gate-2 gap?": the reservation sum covers the live shortfall
    (estimate + floor + grow-in debit − live). A negative shortfall (Gate 2
    passing) trivially accounts."""
    live = _box_live_mb(box)
    host = host_of(box)
    shortfall = (est + _box_floor_mb(box) + _growin_debit_mb(host)) - live
    held = _box_reserved_mb(box) if host else _reserved_mb
    return _gate1(est, is_task=False, box=box), live, held >= shortfall


async def _idle_timeout() -> int:
    """The session idle timeout, read on the DB executor and cached (the
    reapers' helper): a denied admit and a maintenance tick never read the
    settings table on the loop."""
    from core.session import session_state
    return await session_state.cached_idle_timeout()


async def _admit_with_eviction(session_id: str, kind: str, est: int, *,
                               prefer_user: str | None, owner: str = "",
                               cap_owner: str = "", quiet: bool = False,
                               box: str = "") -> Admission:
    """Loop: evict the most-idle idle local session → re-check → admit.

    Runs for EVERY denied interactive admit. Eviction frees BOTH gates (budget
    AND real RAM), so it proceeds while tracked sessions plausibly account for
    the pressure; the loop stops — without sacrificing sessions pointlessly —
    the moment they can't (``host_memory``), admits last-resort on an empty
    platform, and otherwise runs until admitted or nothing idle remains
    (``busy``: sessions genuinely fill the box, none reclaimable). ``quiet``
    (a queued retry) logs denials at debug; the queue logs its final one."""
    log_denial = logger.debug if quiet else logger.warning
    floor_age = min(config.SESSION_EVICT_FLOOR_S, await _idle_timeout())
    skipped_pending = 0
    reading = compute_reading(box) if box else None
    # A compute machine whose own cap binds gives up its own sessions first.
    box_first = box if reading is not None and reading.capped else None
    while True:
        async with cast("asyncio.Condition", _cond):
            if session_id in _sessions:
                _fill_owner(session_id, owner)
                return _ADMITTED
            if cap_owner and _at_user_cap(cap_owner):
                return _deny_user_cap(_owned(cap_owner))
            if _has_room(est, is_task=False, box=box):
                _add(session_id, kind, est, owner, box)
                return _ADMITTED
            gate1_ok, live, sessions_account = _pressure_snapshot(est, box)
            if box and not compute_reading(box).known:
                # Nothing known of the compute machine's memory admits
                # nothing there: the platform's failed read does the same.
                log_denial("Slot denied (no memory report from compute machine %s): %s",
                           box[:8], session_id[:8])
                return _deny_host_memory(est, box)
            if not host_of(box) and _host_count("") == 0:
                # LAST-RESORT ADMIT: zero sessions on the platform's host, so the veto is
                # denying the platform its ONLY session — swap can back one
                # (a slow session beats a locked-out user). By construction
                # this bypasses both gates at most once. Never on another
                # host, whose memory is not the platform's.
                _add(session_id, kind, est, owner, box)
                logger.warning(
                    "Concurrency: last-resort admit %s (%s) on a low-memory host "
                    "(live=%dMB, est=%dMB, floor=%dMB) — only session, swap-backed",
                    session_id[:8], kind, live, est, _box_floor_mb(box),
                )
                return _ADMITTED
            if gate1_ok and not sessions_account:
                # Only Gate 2 binds AND tracked sessions can't plausibly hold
                # the missing RAM → true non-session pressure; evicting user
                # sessions would sacrifice them without closing the gap.
                log_denial(
                    "Slot denied (live-RAM veto, not session pressure): %s "
                    "reserved=%d/%dMB live=%dMB floor=%dMB",
                    session_id[:8], _reserved_mb, _budget_mb, live, _box_floor_mb(box),
                )
                return _deny_host_memory(est, box)
        # An interactive admit evicts a session with running background work
        # or a turn in progress only past the background-work ceiling; an
        # unused pre-warm yields at any age.
        ceiling = background_leash.background_work_ceiling()
        scan = await _oldest_evictable_local(
            floor_age, prefer_user=prefer_user,
            pending_leash_s=ceiling, turn_leash_s=ceiling, prewarm_any_age=True,
            box_first=box_first, host=host_of(box),
        )
        skipped_pending = scan.spared_background
        if scan.victim is None:
            break  # nothing idle to reclaim → attribute + deny below
        await _evict_one(*scan.victim)

    async with cast("asyncio.Condition", _cond):
        gate1_ok, live, sessions_account = _pressure_snapshot(est, box)
    if gate1_ok and not sessions_account:
        log_denial(
            "Slot denied (live-RAM veto after eviction): %s reserved=%d/%dMB live=%dMB floor=%dMB",
            session_id[:8], _reserved_mb, _budget_mb, live, _box_floor_mb(box),
        )
        return _deny_host_memory(est, box)
    if skipped_pending:
        # The idle sessions are idle only in the sense that nobody types:
        # each holds a running background command or subagent.
        log_denial(
            "Slot denied (idle sessions busy with background work): %s (%s) "
            "spared=%d reserved=%d/%dMB live=%dMB",
            session_id[:8], kind, skipped_pending, _reserved_mb, _budget_mb, live,
        )
        return _deny_busy_background(skipped_pending)
    # Sessions fill the budget/cap, or ACTIVE (non-idle) sessions hold the
    # RAM — either way the platform is genuinely busy with session load.
    log_denial("Slot denied (platform busy): %s (%s) reserved=%d/%dMB live=%dMB",
                   session_id[:8], kind, _reserved_mb, _budget_mb, live)
    return _deny_busy()


def _grow_room(delta: int, box: str) -> bool:
    """Room on a session's box for one more tool's program: its host's
    budget (a compute machine on another host has its own, ``_box_cap_ok``),
    the box's own cap and its live memory. Never the session count: the
    session is counted already."""
    host = host_of(box)
    live = _box_live_mb(box) - _growin_debit_mb(host) - delta >= _box_floor_mb(box)
    if host:
        return _box_cap_ok(box, delta, 0) and live
    return _reserved_mb + delta <= _budget_mb and _box_cap_ok(box, delta, 0) and live


async def grow(session_id: str, *, judged: bool, server: str = "") -> Admission:
    """A tool's program starting in a tracked session: its reserve grows by
    one program (``_STDIO_MCP_MB``), never past HEAVY in all, and the growth
    is debited like an admission while it materializes. ``judged`` (a start
    at the agent's first call) only with room on the session's box, else the
    sentence the call answers; not judged (a program that starts with its
    session) it is counted. What it added is kept per tool (``server``), so
    a failed start gives back its own (``shrink``). An untracked session (a
    personal or admin machine's, or one already gone) is admitted: its
    machine counts its own."""
    global _reserved_mb
    if _cond is None:
        return _ADMITTED
    async with _cond:
        est = _session_est.get(session_id)
        if est is None:
            return _grow_on_paired(session_id, judged)
        delta = min(_STDIO_MCP_MB, max(0, config.SESSION_EST_HEAVY_MB - est))
        if delta <= 0:
            return _ADMITTED
        box = _session_box.get(session_id, "")
        if judged and not _grow_room(delta, box):
            logger.info("Tool start refused: %s +%dMB reserved=%d/%dMB", session_id[:8], delta,
                        _reserved_mb, _budget_mb)
            return _DENY_TOOL_MEMORY
        host = _session_host.get(session_id, "")
        _session_est[session_id] = est + delta
        tools = _session_grown.setdefault(session_id, {})
        tools[server] = tools.get(server, 0) + delta
        if not host:
            _reserved_mb += delta
        now = time.monotonic()
        while _grow_debits and now - _grow_debits[0][0] >= _GROW_WINDOW_S:
            _grow_debits.popleft()
        _grow_debits.append((now, delta, host))
        return _ADMITTED


async def shrink(session_id: str, server: str = "") -> None:
    """A tool's start that ``grow`` counted and that did not happen: one
    program given back, never more than that tool's starts added (at the cap
    a start adds less, or nothing)."""
    global _reserved_mb
    if _cond is None:
        return
    async with _cond:
        tools = _session_grown.get(session_id, {})
        grown = tools.get(server, 0)
        delta = min(_STDIO_MCP_MB, grown)
        if delta <= 0 or session_id not in _session_est:
            return
        _session_est[session_id] -= delta
        tools[server] = grown - delta
        if not _session_host.get(session_id, ""):
            _reserved_mb = max(0, _reserved_mb - delta)
        _cond.notify_all()


def release(session_id: str) -> None:
    """Release a slot. Synchronous + idempotent — safe from reapers, finally
    blocks, the shutdown handler, and multiple cleanup paths for the same id."""
    concurrency_paired.forget(session_id)
    if _remove(session_id) is None:
        return  # never tracked (e.g. remote) or already released
    _schedule_notify()


async def reserve_background(session_id: str, *, target: str = placement.LOCAL,
                             execution_path: str | None = None, timeout_s: float,
                             superseded: Callable[[], bool]) -> str:
    """Reserve a slot for a background wake that is about to spawn a session.

    Returns ``"untracked"`` (a machine target: its satellite budgets it, and
    a second start of one id answers ``"superseded"``),
    ``"reserved"`` (this call added the slot, kind ``chat`` with no owner; the
    session's lifecycle releases it), ``"superseded"`` (the id is already
    tracked, or becomes tracked while waiting, or ``superseded()`` turns true:
    someone else is starting or using this session, so spawn nothing) or
    ``"timeout"``. Waits with task headroom (never the last slot a person
    needs), counted as parked so the maintenance pass serves it as it serves
    tasks, re-checking every ``_WAKE_SLICE_S``. The check and the add share one
    lock hold."""
    global _parked_tasks

    def taken() -> bool:
        if session_id in _sessions:
            return True
        try:
            return bool(superseded())
        except Exception:  # noqa: BLE001  # a caller's supersede predicate: a raise counts as superseded
            logger.warning("Wake %s: supersede check failed; not spawning",
                           session_id[:8], exc_info=True)
            return True

    if _no_machine(session_id, target):
        # No compute machine can take the wake: it is kept for later, as one
        # for a machine that is offline.
        return "superseded" if taken() else "timeout"
    box = _box_for(session_id, target)
    if box != "":
        # The remote layer cannot tell a second start from a re-warm of a
        # session it holds, so the second start is refused here.
        from core.session.session_state import is_starting, session_is_held
        if is_starting(session_id) or session_is_held(session_id) or taken():
            return "superseded"
        if box is None:
            return await _wake_on_paired(session_id, target, execution_path, timeout_s, taken)
    assert _cond is not None, "concurrency.init() not called"
    est = _estimate_mb(execution_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_s)

    async with _cond:
        if taken():
            return "superseded"
        if _has_room(est, is_task=True, box=box):
            _add(session_id, "chat", est, box=box)
            return "reserved"
        _parked_tasks += 1
        logger.info("Wake %s parked: no slot (reserved=%d/%dMB)",
                    session_id[:8], _reserved_mb, _budget_mb)
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "timeout"
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(_cond.wait(), min(_WAKE_SLICE_S, remaining))
                if taken():
                    return "superseded"
                if _has_room(est, is_task=True, box=box):
                    _add(session_id, "chat", est, box=box)
                    return "reserved"
        finally:
            _parked_tasks -= 1  # decrement even on Cancelled/Timeout


async def _wake_on_paired(session_id: str, machine: str, execution_path: str | None,
                          timeout_s: float, taken: Callable[[], bool]) -> str:
    """A wake on a personal or admin machine: ``"untracked"`` (it reserves
    nothing) once a new start fits that machine's memory, parked until then
    up to ``timeout_s``."""
    if _cond is None or concurrency_paired.live(session_id, machine):
        return "untracked"
    est = _estimate_mb(execution_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_s)
    while concurrency_paired.room(machine, est) is False:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "timeout"
        async with _cond:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_cond.wait(), min(_WAKE_SLICE_S, remaining))
        if taken():
            return "superseded"
    if concurrency_paired.room(machine, est) is not None:
        concurrency_paired.note_admitted(session_id, machine, est)
    return "untracked"


def release_unless_live(session_id: str) -> bool:
    """Release a reservation nobody's live session holds (a background wake
    giving up before or at its spawn). A live registry entry under the id
    (a person's session that adopted the reservation) keeps it. True when
    released."""
    if session_id not in _sessions or session_id in _live_local_sids():
        return False
    release(session_id)
    return True


# --- Back-compat shims (call sites keep their existing names) ---------------

async def acquire_chat_slot(session_id: str, *, kind: str = "chat",
                            target: str = placement.LOCAL, execution_path: str | None = None,
                            user_sub: str | None = None, per_user_cap: bool = True,
                            speculative: bool = False,
                            queue_wait_s: float | None = None,
                            mcp_config_path: str = "") -> Admission:
    """Interactive-surface acquire (chat / phone / interactive-CLI)."""
    return await acquire(session_id, kind, target=target, execution_path=execution_path,
                         blocking=False, user_sub=user_sub, per_user_cap=per_user_cap,
                         speculative=speculative, queue_wait_s=queue_wait_s,
                         mcp_config_path=mcp_config_path)


def release_chat_slot(session_id: str) -> None:
    release(session_id)


async def rehome_slot(session_id: str, *, kind: str = "chat", target: str = placement.LOCAL,
                      execution_path: str | None = None, user_sub: str | None = None,
                      mcp_config_path: str = "") -> Admission:
    """The reservation of a live session rebuilt in place, on the box it runs
    on now. One the reaper released is acquired again. One held on another
    box moves there in one step under the lock, and stays where it is when
    that box has no room: nothing is given up that cannot be replaced, so the
    session never ends up with neither. A denial ends nothing: the caller's
    session is already running."""
    assert _cond is not None, "concurrency.init() not called"
    if _no_machine(session_id, target):
        return _DENY_NO_COMPUTE
    box = _box_for(session_id, target)
    async with _cond:
        if box is not None and session_id in _sessions and _session_box.get(session_id, "") != box:
            est = _session_est[session_id]
            if not (_box_cap_ok(box, est, 0) and _gate2(est, is_task=False, box=box)):
                logger.warning("Slot not moved (no room on its new box): %s stays reserved where it was",
                               session_id[:8])
                return _deny_host_memory(est, box)
            _move_box(session_id, box)
            _session_added_at[session_id] = time.monotonic()  # its processes are new on that box
            _cond.notify_all()
            return _ADMITTED
    return await acquire_chat_slot(session_id, kind=kind, target=target, execution_path=execution_path,
                                   user_sub=user_sub, mcp_config_path=mcp_config_path)


async def reap_session(session_id: str, close: Callable[[str], Awaitable[object]]) -> None:
    """An idle reaper's close of one session: its chat slot and its
    subscription seat are released whatever the close does (both releases
    are pops, so a second one releases nothing), and a close that raises is
    logged, never ending the reaper's batch."""
    from services.engines.subscription_pool import release_subscription
    try:
        await close(session_id)
    except Exception:  # one session's close must not leak its seat nor end the batch
        logger.exception("Reaping session %s: the close failed", session_id[:8])
    finally:
        try:
            release_chat_slot(session_id)
        finally:
            release_subscription(session_id)


@asynccontextmanager
async def task_slot(session_id: str, *, target: str = placement.LOCAL,
                    execution_path: str | None = None, ring_key: str = ""):
    """Background-task slot — blocking acquire (both gates + the task headroom), then
    always releases on exit. A remote task consumes no local slot and never blocks.
    ``ring_key`` (the task's creator) is the parked tasks' round-robin key: never
    the slot's owner, which keeps counting chats only."""
    await acquire(session_id, "task", target=target, execution_path=execution_path,
                  blocking=True, ring_key=ring_key)
    try:
        yield
    finally:
        release(session_id)


async def acquire_meeting_slots(session_ids: list[str], *,
                                targets: dict[str, str] | None = None,
                                exec_paths: dict[str, str] | None = None,
                                mcp_paths: dict[str, str] | None = None) -> Admission:
    """Atomic-N reserve of the participants the platform counts, each on its
    own box (the platform's, or a compute machine's for a Direct LLM
    participant's tools), against both gates. ``exec_paths`` maps session_id →
    execution_path (for the per-type estimate; absent ⇒ HEAVY), ``mcp_paths``
    session_id → its generated MCP config (a Direct participant's stdio MCPs).
    Meetings deny-without-evicting (v1); the denial carries reason+message
    like ``acquire()``. Releasing all ids later is safe (per-id no-op for the
    untracked remote ones)."""
    assert _cond is not None, "concurrency.init() not called"
    targets = targets or {}
    exec_paths = exec_paths or {}
    mcp_paths = mcp_paths or {}
    if any(_no_machine(sid, targets.get(sid) or placement.LOCAL) for sid in session_ids):
        return _DENY_NO_COMPUTE
    boxes: dict[str, str] = {}
    for sid in session_ids:
        box = _box_for(sid, targets.get(sid) or placement.LOCAL)
        if box is not None and sid not in _sessions:
            boxes[sid] = box
    ests = {sid: await _session_estimate(sid, exec_paths.get(sid), mcp_paths.get(sid, ""))
            for sid in boxes}
    async with _cond:
        new = [sid for sid in boxes if sid not in _sessions]
        total_est = sum(ests[sid] for sid in new)
        cap = config.OTODOCK_MAX_LOCAL_SESSIONS
        # The count caps the platform's host alone: a meeting's participants
        # are there (a Direct LLM participant's tools on a shared machine).
        if cap and _host_count("") + len(new) > cap:
            logger.warning("Meeting denied (hard cap): %d participants", len(new))
            return _deny_busy()
        if _reserved_mb + total_est > _budget_mb:
            logger.warning("Meeting denied (budget): need %dMB, reserved=%d/%dMB",
                           total_est, _reserved_mb, _budget_mb)
            return _deny_busy()
        for box in {boxes[sid] for sid in new}:
            box_est = sum(ests[sid] for sid in new if boxes[sid] == box)
            if not _box_cap_ok(box, box_est, 0):
                logger.warning("Meeting denied (the box's own cap): need %dMB on %s", box_est, box or "the platform")
                return _deny_busy()
            if not _gate2(box_est, is_task=False, box=box):
                logger.warning("Meeting denied (live-RAM veto): need %dMB on %s, live=%dMB floor=%dMB",
                               box_est, box or "the platform", _box_live_mb(box), _box_floor_mb(box))
                return _deny_host_memory(box_est, box)
        for sid in new:
            _add(sid, "meeting", ests[sid], "", boxes[sid])
        return _ADMITTED


def release_meeting_slots(session_ids: list[str]) -> None:
    for sid in session_ids:
        release(sid)


async def _evict_one(sid: str, source: str, is_prewarm: bool = False) -> bool:
    """Free the reservation under _cond, then close the real session OUTSIDE _cond
    (close is slow). The later release() from close_session is a no-op (already
    removed). Existing dead-session resume covers the evicted-user-returns race."""
    if is_prewarm:
        # Atomically take the pre-warm out of the reapable set so the reuse path
        # in _spawn_tail can't adopt the session we're about to kill. If someone
        # else just claimed it (for reuse), don't evict — it's a real session now.
        try:
            from core.session.prewarm_session_registry import claim
            if not await claim(sid):
                return False
        except ImportError:
            pass
    async with cast("asyncio.Condition", _cond):
        if sid not in _sessions:
            return False
        est, box = _session_est.get(sid, 0), _session_box.get(sid, "")
        # Only the share the machine's report already shows in use comes back:
        # a session inside its grow-in window was debited the rest, and that
        # debit goes with it.
        now = time.monotonic()
        grown = min(1.0, (now - _session_added_at.get(sid, now - _GROW_WINDOW_S)) / _GROW_WINDOW_S)
        _remove(sid)
        if box:
            _note_evicted_credit(box, int(est * grown))
    logger.info("Concurrency: evicting idle %ssession %s (%s) to admit a new one",
                "pre-warm " if is_prewarm else "", sid[:8], source)
    try:
        from core.session import interactive_session as _is, session_manager
        if source == "interactive":
            await _is.close_session(sid, reason="evicted_for_capacity")
        elif source == "remote":
            if session_manager._remote_layer is not None:
                await session_manager._remote_layer.close_session(sid)
        elif (holder := session_manager.holding_engines().get(_EVICT_CLOSE[source])) is not None:
            await holder.close_session(sid)
        else:  # an engine that holds no session in the proxy has no such victim
            session_manager.release_unheld(sid)
    except Exception as e:  # noqa: BLE001  # closing an evicted session must not skip the cache reset below
        logger.warning("Concurrency: eviction close failed for %s (%s): %s", sid[:8], source, e)
    # The victim's RSS is freed now — drop the ≤1s live-RAM cache so the
    # eviction loop's immediate re-check reads reality instead of evicting a
    # second session against a stale pre-close number.
    global _live_cache
    _live_cache = None
    if _cond is not None:
        async with _cond:
            _cond.notify_all()
    return True


# ---------------------------------------------------------------------------
# Stats (admin API)
# ---------------------------------------------------------------------------

def get_stats() -> dict:
    """Live concurrency snapshot for the admin dashboard (memory-oriented)."""
    by_surface = {s: 0 for s in _SURFACES}
    for k in _sessions.values():
        by_surface[k] = by_surface.get(k, 0) + 1
    avail = _live_available_mb()
    budget_headroom = max(0, _budget_mb - _reserved_mb)
    live_headroom = max(0, avail - _floor_mb)
    heavy = max(1, config.SESSION_EST_HEAVY_MB)
    light = max(1, config.SESSION_EST_LIGHT_MB)
    return {
        "sessions": {
            "active": len(_sessions),
            "reserved_mb": _reserved_mb,
            "budget_mb": _budget_mb,
            "available_mb": avail,
            "total_mb": _total_mb,
            "fit_heavy": min(budget_headroom // heavy, live_headroom // heavy),
            "fit_light": min(budget_headroom // light, live_headroom // light),
        },
        "tasks": {"active": by_surface.get("task", 0)},
        "by_surface": by_surface,
        "satellites": _satellite_stats(),
        "compute": compute_stats(),
    }


def compute_stats() -> list[dict]:
    """Each connected compute machine's box as the gates read it: its total
    and free memory (MB, 0 free when no fresh report), whether it has a cap
    of its own, what is reserved on it and how many sessions hold a slot."""
    from core.remote.satellite_connection import get_connection_manager
    out = []
    for machine in sorted(get_connection_manager().compute_machine_ids()):
        reading = compute_reading(machine)
        out.append({
            "machine_id": machine,
            "own_host": bool(host_of(machine)),
            "total_mb": reading.total_mb,
            "available_mb": reading.available_mb,
            "capped": reading.capped,
            "reported": reading.known,
            "reserved_mb": _box_reserved_mb(machine),
            "sessions": sum(1 for sid in _sessions if _session_box.get(sid) == machine),
        })
    return out


# ---------------------------------------------------------------------------
# Maintenance loop — parked-task wakeups + task-side eviction
# ---------------------------------------------------------------------------

async def maintenance_loop() -> None:
    """Periodically wake parked tasks (the budget/live-RAM predicate can flip with
    no release — RAM freeing, a session ending elsewhere) and, when a task is
    parked because the budget is full, evict one idle local session to unblock it."""
    while True:
        await asyncio.sleep(_MAINTENANCE_INTERVAL_S)
        try:
            _read_limits()
            await _maintenance_pass()
        except Exception as e:  # noqa: BLE001  # the maintenance loop's tick: one failed pass must not end the loop
            logger.error("concurrency maintenance loop error: %s", e)


async def _maintenance_pass() -> None:
    """One maintenance tick: nothing to do unless a task is parked. A run
    that waits for a compute machine (``wait_for_compute``) is woken by the
    sign-in and by its own deadline."""
    if _parked_tasks <= 0 or _cond is None:
        return
    async with _cond:
        budget_full = not _gate1(config.SESSION_EST_HEAVY_MB, is_task=True)
    if budget_full:
        idle_timeout = await _idle_timeout()
        # A parked task run may evict a session with running background
        # work once it has been idle twice the idle timeout: a chat's
        # forgotten job must not hold the scheduler for hours.
        # The budget it waits on is the platform's host's: a session on
        # another host frees nothing there.
        scan = await _oldest_evictable_local(
            min(config.SESSION_EVICT_FLOOR_S, idle_timeout), prefer_user=None,
            pending_leash_s=2 * idle_timeout,
            turn_leash_s=background_leash.background_work_ceiling(),
            host="",
        )
        if scan.victim is not None:
            await _evict_one(*scan.victim)
    async with _cond:
        _cond.notify_all()


def _adopt_compute_sessions() -> int:
    """A session a compute machine runs that holds no slot (its acquire ran
    in a process before this one, and the machine kept it across the
    restart) takes one on its box: kind ``chat``, no owner, as a wake's,
    stamped old so the grow-in debit does not count memory it already holds.
    A task run's session is left to its run. Call under ``_cond``."""
    from core.session.session_state import get_session_client_type
    task = session_kind.TASK.name
    adopted = 0
    for sid, _s, _source, machine in compute_sessions():
        if sid in _sessions or get_session_client_type(sid) == task:
            continue
        _add(sid, "chat", config.SESSION_EST_HEAVY_MB, box=machine)
        _session_added_at[sid] = time.monotonic() - _GROW_WINDOW_S
        adopted += 1
    return adopted


async def reconcile_chat_slots() -> int:
    """Release orphaned slots not backed by any live registry, and give a
    slot to a session a compute machine kept across a restart.

    Tasks are EXCLUDED (their task_slot finally is authoritative); the live
    source is ``_live_local_sids`` (Direct LLM's pool, the local terminals and
    the sessions a compute machine runs for the platform); sids added within
    the last sweep are spared (mid-spawn window).
    """
    live_sids = _live_local_sids()

    if _cond is None:
        return 0
    now = time.monotonic()
    orphaned: list[str] = []
    async with _cond:
        for sid, kind in list(_sessions.items()):
            if kind == "task":
                continue  # lifecycle owned by task_slot's finally
            if sid in live_sids:
                continue
            if now - _session_added_at.get(sid, 0.0) < _RECONCILE_GRACE_S:
                continue  # mid-spawn — spare it
            orphaned.append(sid)
        for sid in orphaned:
            _remove(sid)
            logger.warning("Reconciliation: released orphaned slot %s (reserved=%d/%dMB)",
                           sid[:8], _reserved_mb, _budget_mb)
        adopted = _adopt_compute_sessions()
        if adopted:
            logger.info("Reconciliation: %d session(s) a compute machine kept took a slot "
                        "(reserved=%d/%dMB)", adopted, _reserved_mb, _budget_mb)
        if orphaned:
            _cond.notify_all()  # we already hold the lock
    return len(orphaned)


async def _reconciliation_loop() -> None:
    """Background task: periodically reconcile local slots."""
    while True:
        await asyncio.sleep(_RECONCILE_GRACE_S)
        try:
            released = await reconcile_chat_slots()
            if released:
                logger.info("Reconciliation released %d orphaned slot(s). reserved=%d/%dMB",
                            released, _reserved_mb, _budget_mb)
        except Exception as e:  # noqa: BLE001  # the reconciliation loop's tick: one failed pass must not end it
            logger.error("Reconciliation error: %s", e)
