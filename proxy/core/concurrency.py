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

**Remote sessions never count** — ``acquire(target=<machine_id>)`` returns
immediately, untracked (the satellite enforces its own budget).

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
import collections
import contextlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Callable, NamedTuple

import config
from core import placement
from core.session import background_leash
from core.sandbox import host_resources

logger = logging.getLogger("claude-proxy.concurrency")

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
# Grow-in window: a freshly admitted session's RSS takes this long to
# materialize; until then Gate 2 debits the un-grown remainder of its estimate
# (linear decay). Without the debit, N near-simultaneous warmups all read the
# same pre-growth free RAM and overcommit a small box.
_GROW_WINDOW_S = 90.0

# A Direct-LLM session: the adapter in the proxy, plus each stdio MCP it starts
# in its own sandbox (about 50 MB for the server, 12 for pasta, 2 for bwrap).
_DIRECT_BASE_MB = 100
_DIRECT_STDIO_MCP_MB = 65
# A generated MCP config is a few KB; anything this large is not one.
_MCP_CONFIG_MAX_BYTES = 1024 * 1024

# Map an eviction source to the execution_path that resolves its layer.
_EVICT_CLOSE = {"cli": "claude-code-cli", "direct": "direct-llm", "codex": "codex-cli"}


class Admission(NamedTuple):
    """Result of a local-slot admission attempt. Truthy iff admitted, so
    gate-only call sites keep reading naturally (``if not await acquire...``);
    denial surfaces show ``user_message`` (and log ``reason``) instead of
    guessing why."""
    ok: bool
    reason: str | None = None        # "busy" | "busy_background" | "host_memory" | "user_cap" | "speculative"
    user_message: str | None = None  # ready-to-display denial text (None when admitted)

    def __bool__(self) -> bool:  # a NamedTuple is otherwise always truthy
        return self.ok


_ADMITTED = Admission(True)
# A pre-warm that finds no room for two is skipped without a word: nothing
# was asked for yet.
_DENY_SPECULATIVE = Admission(False, "speculative", None)


class _Scan(NamedTuple):
    """One eviction scan: the victim ``(sid, source, is_prewarm)`` or None,
    and how many otherwise-evictable sessions were spared for running
    background work and for a turn in progress."""
    victim: tuple[str, str, bool] | None
    spared_background: int
    spared_live: int


def _deny_busy() -> Admission:
    return Admission(
        False, "busy",
        "Too many active sessions — platform busy. Try again shortly, or close an idle chat.",
    )


def _deny_busy_background(spared: int) -> Admission:
    return Admission(
        False, "busy_background",
        f"Too many active sessions, and the {spared} idle one(s) still have "
        f"background work running. Try again in a few minutes, or stop that work.",
    )


def _deny_user_cap(held: int) -> Admission:
    return Admission(
        False, "user_cap",
        f"You already have {held} sessions running on this platform. End one, or wait a "
        "few minutes for an idle one to end, then try again.",
    )


def _deny_host_memory(est: int) -> Admission:
    # NOT session pressure: name the real cause, or the message contradicts the
    # admin page (which can truthfully show 0 active sessions).
    return Admission(
        False, "host_memory",
        f"The platform host is low on memory ({_live_available_mb()} MB free; about "
        f"{est + _floor_mb} MB needed to start). This is host memory pressure, not "
        "session count — free memory on the host machine, then retry. "
        "Details: Admin Settings → Platform.",
    )

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_sessions: dict[str, str] = {}            # session_id -> surface kind
_session_est: dict[str, int] = {}         # session_id -> reserved estimate (MB)
_session_added_at: dict[str, float] = {}  # session_id -> time.monotonic() at acquire
_session_owner: dict[str, str] = {}       # session_id -> the person it runs for, when known
_reserved_mb: int = 0                     # cached Σ _session_est
_line: list[object] = []                  # queued admits in arrival order (their tasks)
_parked_tasks: int = 0                    # count of tasks blocked in acquire

_cond: asyncio.Condition | None = None    # initialized in init()

_budget_mb: int = 0                       # GATE-1 budget (total × BUDGET_FRACTION), cached at init
_total_mb: int = 0                        # container memory limit (for the gauge)
_floor_mb: int = 0                        # GATE-2 floor, cached at init

_live_cache: tuple[float, int] | None = None   # (monotonic_ts, available_mb)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init() -> None:
    """Create the condition and cache the budget/floor. Call once at startup."""
    global _cond, _budget_mb, _total_mb, _floor_mb

    _cond = asyncio.Condition()
    _total_mb = max(1, host_resources.detect_memory_limit_bytes() // (1024 * 1024))
    _budget_mb = int(_total_mb * config.BUDGET_FRACTION)
    _floor_mb = max(config.SESSION_RESERVE_FLOOR_MB, int(_total_mb * 0.03))

    logger.info(
        "Concurrency: budget=%dMB (%.0f%% of %dMB) · gate-2 floor=%dMB · est heavy/light=%d/%d MB "
        "· evict_floor=%ds · hard_cap=%s · per_user_cap=%s",
        _budget_mb, config.BUDGET_FRACTION * 100, _total_mb, _floor_mb,
        config.SESSION_EST_HEAVY_MB, config.SESSION_EST_LIGHT_MB, config.SESSION_EVICT_FLOOR_S,
        config.OTODOCK_MAX_LOCAL_SESSIONS or "off", config.MAX_SESSIONS_PER_USER or "off",
    )


# ---------------------------------------------------------------------------
# Estimates + gates (call gate helpers under _cond)
# ---------------------------------------------------------------------------

def _no_os_process(execution_path: str | None) -> bool:
    """The engine runs in the proxy (Direct LLM), not as its own process tree."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(execution_path or "")
    return caps is not None and not caps.runtime.has_os_process


def _estimate_mb(execution_path: str | None, stdio_mcps: int = 0) -> int:
    """Coarse per-TYPE memory reserve: every subprocess engine is HEAVY; an
    engine with no OS process (the in-process Direct LLM loop) reserves its
    adapter plus the stdio MCPs it starts, LIGHT at the least and never more
    than a CLI session carrying the same servers."""
    heavy = config.SESSION_EST_HEAVY_MB
    if not _no_os_process(execution_path):
        return heavy
    light = config.SESSION_EST_LIGHT_MB
    if stdio_mcps <= 0:
        return light
    return min(heavy, max(light, _DIRECT_BASE_MB + stdio_mcps * _DIRECT_STDIO_MCP_MB))


def _stdio_mcp_count(path: str) -> int:
    """Stdio servers in a generated MCP config; 0 when the file is unreadable,
    not JSON or implausibly large."""
    from services.mcp.mcp_manifest_types import is_stdio_transport
    try:
        if os.path.getsize(path) > _MCP_CONFIG_MAX_BYTES:
            return 0
        with open(path, encoding="utf-8") as f:
            servers = json.load(f).get("mcpServers") or {}
        return sum(1 for server in servers.values()
                   if isinstance(server, dict) and is_stdio_transport(server.get("type")))
    except Exception:
        return 0


async def _session_estimate(session_id: str, execution_path: str | None,
                            mcp_config_path: str) -> int:
    """The reserve for a new session; the config is read off the loop, and
    only for an in-process engine not yet tracked."""
    stdio_mcps = 0
    if mcp_config_path and session_id not in _sessions and _no_os_process(execution_path):
        stdio_mcps = await asyncio.to_thread(_stdio_mcp_count, mcp_config_path)
    return _estimate_mb(execution_path, stdio_mcps)


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
    except Exception:
        mb = 0
    _live_cache = (now, mb)
    return mb


def _swap_credit_mb() -> int:
    """min(SwapFree / 2, SESSION_SWAP_CREDIT_MB) — 0 when disabled/swapless."""
    if config.SESSION_SWAP_CREDIT_MB <= 0:
        return 0
    swap_mb = host_resources.swap_free_bytes() // (1024 * 1024)
    return min(swap_mb // 2, config.SESSION_SWAP_CREDIT_MB)


def _gate1(est: int, *, is_task: bool) -> bool:
    """Reservation budget (instant). Tasks keep one HEAVY of headroom so background
    work never consumes the room a human interactive session needs, and, under
    a hard cap of 2 or more, one slot of the count (under a cap of 1 a task
    would never run)."""
    cap = config.OTODOCK_MAX_LOCAL_SESSIONS
    if cap and len(_sessions) + (1 if is_task and cap >= 2 else 0) >= cap:
        return False
    headroom = config.SESSION_EST_HEAVY_MB if is_task else 0
    return _reserved_mb + est <= _budget_mb - headroom


def _growin_debit_mb() -> int:
    """Σ un-materialized estimate of sessions admitted < ``_GROW_WINDOW_S`` ago.

    Linear decay: a session admitted N seconds ago is assumed to have grown
    into ``est × N/window`` of its reservation already (visible in live RAM),
    so only the remainder is debited — conservative during the window, zero
    after it (no steady-state double-count)."""
    now = time.monotonic()
    debit = 0
    for sid, added in _session_added_at.items():
        elapsed = now - added
        if elapsed < _GROW_WINDOW_S:
            debit += int(_session_est.get(sid, 0) * (1.0 - elapsed / _GROW_WINDOW_S))
    return debit


def _gate2(est: int, *, is_task: bool) -> bool:
    """Live-RAM OOM veto (grow-in-debited). Same task headroom as gate 1."""
    headroom = config.SESSION_EST_HEAVY_MB if is_task else 0
    return _live_available_mb() - _growin_debit_mb() - est >= _floor_mb + headroom


def _has_room(est: int, *, is_task: bool) -> bool:
    return _gate1(est, is_task=is_task) and _gate2(est, is_task=is_task)


def _speculative_room(est: int, owner: str) -> bool:
    """Room for this session AND one HEAVY more, so a speculative spawn never
    takes the last slot a real session needs: the count cap, the budget and
    the live read (grow-in debited), nobody parked or queued for a slot, and
    the person below their cap."""
    heavy = config.SESSION_EST_HEAVY_MB
    cap = config.OTODOCK_MAX_LOCAL_SESSIONS
    if cap and len(_sessions) + 2 > cap:
        return False
    if _parked_tasks > 0 or _line:
        return False
    if owner and config.MAX_SESSIONS_PER_USER > 0 and _at_user_cap(owner):
        return False
    if _reserved_mb + est + heavy > _budget_mb:
        return False
    return _live_available_mb() - _growin_debit_mb() - est - heavy >= _floor_mb


def prewarm_allowed(execution_path: str | None = None, *, user_sub: str | None = None) -> bool:
    """Whether a pre-warm may start now: the box has room for it and one more
    session (``fit_heavy >= 2``), nobody waits for a slot, and the person
    holds fewer sessions than their cap. Read-only; a pre-warm site asks
    before building the session's config."""
    if _cond is None:
        return False
    return _speculative_room(_estimate_mb(execution_path), user_sub or "")


def _add(session_id: str, kind: str, est: int, owner: str = "") -> None:
    """Reserve a slot. est is written ONLY here (never on idempotent re-acquire)."""
    global _reserved_mb
    assert session_id not in _session_est, "double _add for %s" % session_id
    _sessions[session_id] = kind
    _session_est[session_id] = est
    _session_added_at[session_id] = time.monotonic()
    if owner:
        _session_owner[session_id] = owner
    _reserved_mb += est


def _fill_owner(session_id: str, owner: str | None) -> None:
    """An idempotent re-acquire that knows the owner records it when none is
    recorded yet; a recorded owner is never overwritten."""
    if owner and session_id in _sessions and not _session_owner.get(session_id):
        _session_owner[session_id] = owner


def _cap_owner(session_id: str) -> str:
    """Whom a session counts against under the per-person cap: the ledger's
    owner only. A session with none counts against nobody, so it is also no
    one's to give up at the cap (``_make_room_under_user_cap``)."""
    return _session_owner.get(session_id, "")


def _owned(owner: str) -> int:
    """Chat sessions the ledger holds for ``owner`` (``_cap_owner``)."""
    return sum(1 for sid, kind in _sessions.items()
               if kind == "chat" and _cap_owner(sid) == owner)


def _at_user_cap(owner: str) -> bool:
    return _owned(owner) >= config.MAX_SESSIONS_PER_USER


def _remove(session_id: str) -> str | None:
    """Single teardown: pops _sessions/_session_est/_session_added_at/_session_owner
    + frees the reservation. Returns the prior kind, or None if it wasn't tracked."""
    global _reserved_mb
    kind = _sessions.pop(session_id, None)
    est = _session_est.pop(session_id, None)
    _session_added_at.pop(session_id, None)
    _session_owner.pop(session_id, None)
    if est:
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
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("concurrency: release outside a running loop; notify skipped")
        return
    loop.create_task(_notify_waiters())


# ---------------------------------------------------------------------------
# Acquire / release
# ---------------------------------------------------------------------------

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
    ``mcp_config_path`` (the session's generated MCP config) sizes a Direct-LLM
    reserve by the stdio MCPs it starts.
    """
    if not placement.is_local(target):
        return _ADMITTED

    assert _cond is not None, "concurrency.init() not called"
    est = await _session_estimate(session_id, execution_path, mcp_config_path)
    owner = user_sub or ""

    if blocking:
        return await _acquire_task(session_id, kind, est, owner, ring_key=ring_key)
    if speculative:
        async with _cond:
            if session_id in _sessions:
                _fill_owner(session_id, owner)
                return _ADMITTED
            if not _speculative_room(est, owner):
                logger.debug("Speculative slot refused: %s (no room for two)", session_id[:8])
                return _DENY_SPECULATIVE
            _add(session_id, kind, est, owner)
            return _ADMITTED

    cap_owner = owner if (owner and per_user_cap and kind == "chat"
                          and config.MAX_SESSIONS_PER_USER > 0) else ""
    if cap_owner:
        adm = await _make_room_under_user_cap(session_id, cap_owner)
        if adm is not None:
            return adm
    if queue_wait_s and queue_wait_s > 0:
        return await _acquire_queued(session_id, kind, est, prefer_user=user_sub,
                                     owner=owner, cap_owner=cap_owner, wait_s=queue_wait_s)

    async with _cond:
        if session_id in _sessions:
            _fill_owner(session_id, owner)
            return _ADMITTED
        if _has_room(est, is_task=False):
            if cap_owner and _at_user_cap(cap_owner):
                return _deny_user_cap(_owned(cap_owner))
            _add(session_id, kind, est, owner)
            logger.debug("Slot acquired: %s (%s) reserved=%d/%dMB", session_id[:8], kind,
                         _reserved_mb, _budget_mb)
            return _ADMITTED

    # Denied at first look. The eviction loop owns what happens next — it
    # frees BOTH gates (budget + real RAM), decides whether tracked sessions
    # plausibly account for the pressure (which gate binds is NOT the signal
    # by itself — see the module docstring), applies the empty-platform
    # last-resort admit, and attributes the eventual denial honestly.
    return await _admit_with_eviction(session_id, kind, est, prefer_user=user_sub,
                                      owner=owner, cap_owner=cap_owner)


def _is_head(ticket: object) -> bool:
    """Whether ``ticket`` leads the admission line (a finished waiter's ticket
    ahead of it is dropped: its own cleanup already ran or never will)."""
    while (_line and _line[0] is not ticket and isinstance(_line[0], asyncio.Task)
           and _line[0].done()):
        _line.pop(0)
    return bool(_line) and _line[0] is ticket


async def _acquire_queued(session_id: str, kind: str, est: int, *, prefer_user: str | None,
                          owner: str, cap_owner: str, wait_s: float) -> Admission:
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
    last: Admission | None = None
    async with _cond:
        _line.append(ticket)
    try:
        while True:
            if _is_head(ticket):
                adm = await _admit_with_eviction(session_id, kind, est, prefer_user=prefer_user,
                                                 owner=owner, cap_owner=cap_owner, quiet=True)
                if adm or adm.reason in ("host_memory", "user_cap"):
                    return adm
                last = adm
            async with _cond:
                if session_id in _sessions:
                    _fill_owner(session_id, owner)
                    return _ADMITTED
                # Re-checked under the lock right before waiting: a release
                # during the head's lock-free eviction pass notified nobody.
                if _is_head(ticket) and _has_room(est, is_task=False):
                    if cap_owner and _at_user_cap(cap_owner):
                        return _deny_user_cap(_owned(cap_owner))
                    _add(session_id, kind, est, owner)
                    return _ADMITTED
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(_cond.wait(), min(_QUEUE_SLICE_S, remaining))
        adm = last or _deny_busy()
        logger.warning("Slot denied after %.0fs in the admission queue: %s (%s) reason=%s",
                       wait_s, session_id[:8], kind, adm.reason)
        return adm
    finally:
        # Synchronous: an awaited cleanup can be cancelled half-way and leave
        # a ticket behind that holds the line for good.
        with contextlib.suppress(ValueError):
            _line.remove(ticket)
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
        async with _cond:
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


# The parked tasks' ring (`_acquire_task`): one FIFO per creator and the
# round-robin order of the creators, so one person's burst takes one slot
# per round and a single run waits behind at most one run of each other
# creator. A waiter is its (session id, estimate) ticket.
_task_ring: dict[str, collections.deque[tuple[str, int]]] = {}
_task_ring_order: collections.deque[str] = collections.deque()


def _ring_join(key: str, ticket: tuple[str, int]) -> None:
    line = _task_ring.get(key)
    if line is None:
        line = _task_ring[key] = collections.deque()
        _task_ring_order.append(key)
    line.append(ticket)


def _ring_leave(key: str, ticket: tuple[str, int]) -> None:
    line = _task_ring.get(key)
    if line is None:
        return
    with contextlib.suppress(ValueError):
        line.remove(ticket)
    if not line:
        del _task_ring[key]
        with contextlib.suppress(ValueError):
            _task_ring_order.remove(key)


def _ring_turn(key: str, ticket: tuple[str, int]) -> bool:
    """Whether ``ticket`` is the one the ring admits now: the head of the
    first creator in ring order whose head fits (a heavy head of one creator
    never blocks a lighter head of another)."""
    for k in _task_ring_order:
        line = _task_ring.get(k)
        if not line:
            continue
        head = line[0]
        if _has_room(head[1], is_task=True):
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
                        ring_key: str = "") -> Admission:
    """Blocking task acquire — waits until both gates allow (with HEAVY headroom),
    in the parked tasks' ring: the creators take turns. Task-side eviction is
    driven by the maintenance loop (keeps wait_for pure)."""
    global _parked_tasks
    async with _cond:
        if session_id in _sessions:
            _fill_owner(session_id, owner)
            return _ADMITTED
        if not _has_room(est, is_task=True) or _task_ring:
            key = ring_key or "-"
            ticket = (session_id, est)
            _ring_join(key, ticket)
            _parked_tasks += 1
            logger.info("Task %s parked: no slot (reserved=%d/%dMB, sessions=%d)",
                        session_id[:8], _reserved_mb, _budget_mb, len(_sessions))
            try:
                await _cond.wait_for(
                    lambda: session_id in _sessions or _ring_turn(key, ticket))
            finally:
                _parked_tasks -= 1  # decrement even on Cancelled/Timeout
                _ring_leave(key, ticket)
                _ring_rotate(key)
        if session_id in _sessions:
            _fill_owner(session_id, owner)
            return _ADMITTED
        _add(session_id, kind, est, owner)
        return _ADMITTED


def _pressure_snapshot(est: int) -> tuple[bool, int, bool]:
    """(gate1_ok, live_mb, sessions_account) — call under ``_cond``.

    ``sessions_account`` answers "could evicting tracked sessions plausibly
    close the Gate-2 gap?": the reservation sum covers the live shortfall
    (estimate + floor + grow-in debit − live). A negative shortfall (Gate 2
    passing) trivially accounts."""
    live = _live_available_mb()
    shortfall = (est + _floor_mb + _growin_debit_mb()) - live
    return _gate1(est, is_task=False), live, _reserved_mb >= shortfall


async def _idle_timeout() -> int:
    """The session idle timeout, read on the DB executor and cached (the
    reapers' helper): a denied admit and a maintenance tick never read the
    settings table on the loop."""
    from core.session import session_state
    return await session_state.cached_idle_timeout()


async def _admit_with_eviction(session_id: str, kind: str, est: int, *,
                               prefer_user: str | None, owner: str = "",
                               cap_owner: str = "", quiet: bool = False) -> Admission:
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
    while True:
        async with _cond:
            if session_id in _sessions:
                _fill_owner(session_id, owner)
                return _ADMITTED
            if cap_owner and _at_user_cap(cap_owner):
                return _deny_user_cap(_owned(cap_owner))
            if _has_room(est, is_task=False):
                _add(session_id, kind, est, owner)
                return _ADMITTED
            gate1_ok, live, sessions_account = _pressure_snapshot(est)
            if not _sessions:
                # LAST-RESORT ADMIT: zero tracked sessions, so the veto is
                # denying the platform its ONLY session — swap can back one
                # (a slow session beats a locked-out user). By construction
                # this bypasses both gates at most once.
                _add(session_id, kind, est, owner)
                logger.warning(
                    "Concurrency: last-resort admit %s (%s) on a low-memory host "
                    "(live=%dMB, est=%dMB, floor=%dMB) — only session, swap-backed",
                    session_id[:8], kind, live, est, _floor_mb,
                )
                return _ADMITTED
            if gate1_ok and not sessions_account:
                # Only Gate 2 binds AND tracked sessions can't plausibly hold
                # the missing RAM → true non-session pressure; evicting user
                # sessions would sacrifice them without closing the gap.
                log_denial(
                    "Slot denied (live-RAM veto, not session pressure): %s "
                    "reserved=%d/%dMB live=%dMB floor=%dMB",
                    session_id[:8], _reserved_mb, _budget_mb, live, _floor_mb,
                )
                return _deny_host_memory(est)
        # An interactive admit evicts a session with running background work
        # or a turn in progress only past the background-work ceiling; an
        # unused pre-warm yields at any age.
        ceiling = background_leash.background_work_ceiling()
        scan = await _oldest_evictable_local(
            floor_age, prefer_user=prefer_user,
            pending_leash_s=ceiling, turn_leash_s=ceiling, prewarm_any_age=True,
        )
        skipped_pending = scan.spared_background
        if scan.victim is None:
            break  # nothing idle to reclaim → attribute + deny below
        await _evict_one(*scan.victim)

    async with _cond:
        gate1_ok, live, sessions_account = _pressure_snapshot(est)
    if gate1_ok and not sessions_account:
        log_denial(
            "Slot denied (live-RAM veto after eviction): %s reserved=%d/%dMB live=%dMB floor=%dMB",
            session_id[:8], _reserved_mb, _budget_mb, live, _floor_mb,
        )
        return _deny_host_memory(est)
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


def release(session_id: str) -> None:
    """Release a slot. Synchronous + idempotent — safe from reapers, finally
    blocks, the shutdown handler, and multiple cleanup paths for the same id."""
    if _remove(session_id) is None:
        return  # never tracked (e.g. remote) or already released
    _schedule_notify()


async def reserve_background(session_id: str, *, target: str = placement.LOCAL,
                             execution_path: str | None = None, timeout_s: float,
                             superseded: Callable[[], bool]) -> str:
    """Reserve a slot for a background wake that is about to spawn a session.

    Returns ``"untracked"`` (a machine target: its satellite budgets it),
    ``"reserved"`` (this call added the slot, kind ``chat`` with no owner; the
    session's lifecycle releases it), ``"superseded"`` (the id is already
    tracked, or becomes tracked while waiting, or ``superseded()`` turns true:
    someone else is starting or using this session, so spawn nothing) or
    ``"timeout"``. Waits with task headroom (never the last slot a person
    needs), counted as parked so the maintenance pass serves it as it serves
    tasks, re-checking every ``_WAKE_SLICE_S``. The check and the add share one
    lock hold."""
    global _parked_tasks
    if not placement.is_local(target):
        return "untracked"
    assert _cond is not None, "concurrency.init() not called"
    est = _estimate_mb(execution_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_s)

    def taken() -> bool:
        if session_id in _sessions:
            return True
        try:
            return bool(superseded())
        except Exception:
            logger.warning("Wake %s: supersede check failed; not spawning",
                           session_id[:8], exc_info=True)
            return True

    async with _cond:
        if taken():
            return "superseded"
        if _has_room(est, is_task=True):
            _add(session_id, "chat", est)
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
                if _has_room(est, is_task=True):
                    _add(session_id, "chat", est)
                    return "reserved"
        finally:
            _parked_tasks -= 1  # decrement even on Cancelled/Timeout


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


@asynccontextmanager
async def task_slot(session_id: str, *, target: str = placement.LOCAL,
                    execution_path: str | None = None, ring_key: str = ""):
    """Background-task slot — blocking acquire (both gates + HEAVY headroom), then
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
    """Atomic-N reserve of the LOCAL participants against both gates. ``exec_paths``
    maps session_id → execution_path (for the per-type estimate; absent ⇒ HEAVY),
    ``mcp_paths`` session_id → its generated MCP config (a Direct participant's
    stdio MCPs). Meetings deny-without-evicting (v1); the denial carries
    reason+message like ``acquire()``. Releasing all ids later is safe (per-id
    no-op for the untracked remote ones)."""
    assert _cond is not None, "concurrency.init() not called"
    targets = targets or {}
    exec_paths = exec_paths or {}
    mcp_paths = mcp_paths or {}
    local = [sid for sid in session_ids
             if placement.is_local(targets.get(sid)) and sid not in _sessions]
    ests = {sid: await _session_estimate(sid, exec_paths.get(sid), mcp_paths.get(sid, ""))
            for sid in local}
    async with _cond:
        local_new = [sid for sid in local if sid not in _sessions]
        total_est = sum(ests[sid] for sid in local_new)
        cap = config.OTODOCK_MAX_LOCAL_SESSIONS
        if cap and len(_sessions) + len(local_new) > cap:
            logger.warning("Meeting denied (hard cap): %d local participants", len(local_new))
            return _deny_busy()
        if _reserved_mb + total_est > _budget_mb:
            logger.warning("Meeting denied (budget): need %dMB, reserved=%d/%dMB",
                           total_est, _reserved_mb, _budget_mb)
            return _deny_busy()
        if not _gate2(total_est, is_task=False):
            logger.warning("Meeting denied (live-RAM veto): need %dMB, live=%dMB floor=%dMB",
                           total_est, _live_available_mb(), _floor_mb)
            return _deny_host_memory(total_est)
        for sid in local_new:
            _add(sid, "meeting", ests[sid])
        return _ADMITTED


def release_meeting_slots(session_ids: list[str]) -> None:
    for sid in session_ids:
        release(sid)


# ---------------------------------------------------------------------------
# Graceful LRU eviction
# ---------------------------------------------------------------------------

def _turn_live(sid: str, s: object) -> bool:
    """True while the session is inside a turn: streaming, running a
    foreground tool, or waiting on a person's answer, read from what each
    session object exposes (a headless CLI turn, an app-server turn, a
    terminal's open turn or parked question), any engine's question waiting
    on a person, and the in-process engine's stream map."""
    try:
        if (getattr(s, "_turn_active", False) or getattr(s, "_current_turn_id", None)
                or getattr(s, "turn_open", False) or getattr(s, "question_parked", False)):
            return True
        from core.session import session_state
        if session_state.has_pending_prompt(sid):
            return True
        # The in-process engine touches last_activity only at the start and
        # end of a turn; its layer's stream map is the truthful signal.
        from core.layers.direct.layer import DirectLLMExecutionLayer
        task = DirectLLMExecutionLayer._active_streams.get(sid)
        return task is not None and not task.done()
    except Exception:
        return False


async def _oldest_evictable_local(min_idle_s: float, *,
                                  prefer_user: str | None = None,
                                  only_user: str | None = None,
                                  pending_leash_s: float | None = None,
                                  turn_leash_s: float | None = None,
                                  prewarm_any_age: bool = False,
                                  ) -> _Scan:
    """Best eviction victim, plus how many otherwise-evictable sessions were
    spared: with ``pending_leash_s`` a session whose registries show a
    running command or subagent, with ``turn_leash_s`` a session inside a
    turn, each skipped until it has been idle that long (None = no leash).

    Scans the 4 real session pools (cli/direct/codex layer pools + interactive
    locals). A candidate must hold a ``chat`` reservation (task, meeting and
    phone reservations are released by their owners) and be idle
    ≥ ``min_idle_s``, idle age counting from the newer of ``last_activity``
    and the last permission hook. ``only_user`` limits the scan to the
    sessions the per-person cap counts for that person (``_cap_owner``, the
    owner ``_owned`` counts by). Ordering: unclaimed **pre-warms first**
    (speculative, unused; at any age with ``prewarm_any_age``) → the
    **requesting user's own** idle sessions → everyone else; within a group,
    most-idle first. The order's owner is the ledger's, else the session's
    own ``user_sub``: it orders and never counts.
    """
    now = time.monotonic()
    try:
        from core.session.prewarm_session_registry import _entries as _pw_entries
    except Exception:
        _pw_entries = {}
    try:
        from core.session.session_state import get_hook_activity
    except Exception:
        def get_hook_activity(_sid: str) -> float:
            return 0.0
    cands: list[tuple[int, float, str, str, bool]] = []

    spared_background = 0
    spared_live = 0

    def consider(sid: str, s: object, source: str) -> None:
        nonlocal spared_background, spared_live
        if _sessions.get(sid) != "chat":
            return  # must hold a reclaimable reservation to be worth evicting
        if only_user is not None and _cap_owner(sid) != only_user:
            return
        owner = _cap_owner(sid) or getattr(s, "user_sub", None) or ""
        age = now - max(getattr(s, "last_activity", now), get_hook_activity(sid))
        is_pw = sid in _pw_entries
        if not (is_pw and prewarm_any_age):
            if age < min_idle_s:
                return
            if (turn_leash_s is not None and age < turn_leash_s
                    and _turn_live(sid, s)):
                spared_live += 1
                return
            if (pending_leash_s is not None and age < pending_leash_s
                    and background_leash.background_pending_count(sid)):
                spared_background += 1
                return
        group = 0 if is_pw else (1 if (prefer_user and owner == prefer_user) else 2)
        cands.append((group, -age, sid, source, is_pw))

    try:
        from core.layers.cli.session import _persistent_sessions, _persistent_sessions_lock
        async with _persistent_sessions_lock:
            for sid, s in list(_persistent_sessions.items()):
                consider(sid, s, "cli")
    except Exception:
        pass
    try:
        from core.layers.direct.session import _direct_sessions, _direct_sessions_lock
        async with _direct_sessions_lock:
            for sid, s in list(_direct_sessions.items()):
                consider(sid, s, "direct")
    except Exception:
        pass
    try:
        from core.layers.codex.session import _codex_sessions, _codex_sessions_lock
        async with _codex_sessions_lock:
            for sid, s in list(_codex_sessions.items()):
                consider(sid, s, "codex")
    except Exception:
        pass
    try:
        from core.session import interactive_session as _is
        for sid in _is.live_session_ids(local_only=True):
            s = _is.get(sid)
            if s is not None:
                consider(sid, s, "interactive")
    except Exception:
        pass

    if not cands:
        return _Scan(None, spared_background, spared_live)
    cands.sort()  # (group asc, -age asc ⇒ most-idle first within a group)
    _, _, sid, source, is_pw = cands[0]
    return _Scan((sid, source, is_pw), spared_background, spared_live)


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
        except Exception:
            pass
    async with _cond:
        if sid not in _sessions:
            return False
        _remove(sid)
    logger.info("Concurrency: evicting idle %ssession %s (%s) to admit a new one",
                "pre-warm " if is_prewarm else "", sid[:8], source)
    try:
        if source == "interactive":
            from core.session import interactive_session as _is
            await _is.close_session(sid, reason="evicted_for_capacity")
        else:
            from core.session.session_manager import get_layer_by_path
            await get_layer_by_path(_EVICT_CLOSE[source]).close_session(sid)
    except Exception as e:
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
    }


def _satellite_stats() -> list[dict]:
    """Per-satellite live counts + load (filled by the satellite layer). Empty
    until the satellite connection manager is up."""
    try:
        from core.remote.satellite_connection import get_connection_manager
        return get_connection_manager().concurrency_stats()
    except Exception:
        return []


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
            await _maintenance_pass()
        except Exception as e:
            logger.error("concurrency maintenance loop error: %s", e)


async def _maintenance_pass() -> None:
    """One maintenance tick: nothing to do unless a task is parked."""
    if _parked_tasks <= 0 or _cond is None:
        return
    async with _cond:
        budget_full = not _gate1(config.SESSION_EST_HEAVY_MB, is_task=True)
    if budget_full:
        idle_timeout = await _idle_timeout()
        # A parked task run may evict a session with running background
        # work once it has been idle twice the idle timeout: a chat's
        # forgotten job must not hold the scheduler for hours.
        scan = await _oldest_evictable_local(
            min(config.SESSION_EVICT_FLOOR_S, idle_timeout), prefer_user=None,
            pending_leash_s=2 * idle_timeout,
            turn_leash_s=background_leash.background_work_ceiling(),
        )
        if scan.victim is not None:
            await _evict_one(*scan.victim)
    async with _cond:
        _cond.notify_all()


# ---------------------------------------------------------------------------
# Reconciliation — safety net for orphaned slots
# ---------------------------------------------------------------------------

def _live_local_sids() -> set[str]:
    """Every session id a LOCAL registry holds alive: each engine's pool, the
    live pumps, the local interactive PTYs."""
    from core.events.stream_pump import _active_pumps
    from core.session.session_manager import get_all_layers

    # Every LOCAL engine's live sessions, straight from the registry. The
    # three private pool dicts (and their locks) used to be imported here by
    # name, so a fourth engine's sessions would read as orphaned and have
    # their concurrency slots released out from under them.
    live_sids: set[str] = set()
    for layer in get_all_layers().values():
        live_sids.update(layer.local_session_ids())
    live_sids.update(p.session_id for p in _active_pumps.values() if not p.is_done)

    try:
        from core.session.interactive_session import live_session_ids
        live_sids.update(live_session_ids(local_only=True))
    except Exception:
        pass
    return live_sids


async def reconcile_chat_slots() -> int:
    """Release orphaned LOCAL slots not backed by any live registry.

    Tasks are EXCLUDED (their task_slot finally is authoritative); remote layer
    sessions are not a live source (they never hold a slot); interactive counted
    LOCAL-only; sids added within the last sweep are spared (mid-spawn window).
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
        except Exception as e:
            logger.error("Reconciliation error: %s", e)
