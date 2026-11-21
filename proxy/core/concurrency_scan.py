"""The admission gate's records and its reads of them: the session table, the
reserve per session, the per-person owner, the denials, and the scan that picks
what to evict. Every total a function rebinds stays in ``core.concurrency``.
"""

import asyncio
import collections
import contextlib
import json
import os
import time
from typing import NamedTuple

import config
from core.sandbox import host_resources
from core.session import background_leash

# Grow-in window: a freshly admitted session's RSS takes this long to
# materialize; until then Gate 2 debits the un-grown remainder of its estimate
# (linear decay). Without the debit, N near-simultaneous warmups all read the
# same pre-growth free RAM and overcommit a small box.
_GROW_WINDOW_S = 90.0

# A Direct-LLM session: the adapter in the proxy, plus each stdio MCP it starts
# in its own sandbox (about 50 MB for the server, 12 for pasta, 2 for bwrap).
_DIRECT_BASE_MB = 100
# One stdio tool's program in its sandbox, in any engine's session.
_STDIO_MCP_MB = 65
# A Claude Code or Codex session whose config is known: the engine with its
# sandbox and hooks (Claude Code measured about 200 MB at rest on the compute
# machine, Codex about 115), and each tool that waits for its first call as a
# stand-in (measured 8.7 MB with its saved answers).
_ENGINE_BASE_MB = 300
_STANDIN_MB = 10
# A generated MCP config is a few KB; anything this large is not one.
_MCP_CONFIG_MAX_BYTES = 1024 * 1024


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
        "Too many active sessions. The platform is busy. Try again shortly, or close an idle chat.",
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

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_sessions: dict[str, str] = {}            # session_id -> surface kind
_session_box: dict[str, str] = {}         # session_id -> its box: "" the platform's, else a compute machine's id
_session_host: dict[str, str] = {}        # session_id -> its box's host at the reserve: "" the platform's, else a compute machine on another host
_session_est: dict[str, int] = {}         # session_id -> reserved estimate (MB)
_session_added_at: dict[str, float] = {}  # session_id -> time.monotonic() at acquire
_session_owner: dict[str, str] = {}       # session_id -> the person it runs for, when known
_grow_debits: collections.deque = collections.deque()  # (time.monotonic(), MB, host) of each tool's late start
_session_grown: dict[str, dict[str, int]] = {}  # session_id -> {tool: MB its starts added (``grow``)}
_line: list[tuple[object, str]] = []      # queued admits in arrival order (their tasks), with their box's host


# ---------------------------------------------------------------------------
# Estimates + gates (call gate helpers under _cond)
# ---------------------------------------------------------------------------

def _no_os_process(execution_path: str | None) -> bool:
    """The engine runs in the proxy (Direct LLM), not as its own process tree."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(execution_path or "")
    return caps is not None and not caps.runtime.has_os_process


def _estimate_mb(execution_path: str | None, counts: tuple[int, int] | None = None) -> int:
    """Coarse per-TYPE memory reserve. ``counts`` is the session config's
    stdio tools, ``(late, with_chat)``: those whose program waits for the
    agent's first call and those that start with the session; None when the
    config is not known. A subprocess engine reserves HEAVY with no config,
    else its own base, a stand-in per late tool and a program per tool that
    starts with it, never more than HEAVY. An engine with no OS process (the
    in-process Direct LLM loop) reserves its adapter plus the programs that
    start with it, LIGHT at the least and never more than HEAVY. A program
    that starts later grows its session's reserve (``concurrency.grow``)."""
    heavy = config.SESSION_EST_HEAVY_MB
    late, with_chat = counts if counts is not None else (0, 0)
    if not _no_os_process(execution_path):
        if counts is None:
            return heavy
        return min(heavy, _ENGINE_BASE_MB + late * _STANDIN_MB + with_chat * _STDIO_MCP_MB)
    light = config.SESSION_EST_LIGHT_MB
    if with_chat <= 0:
        return light
    return min(heavy, max(light, _DIRECT_BASE_MB + with_chat * _STDIO_MCP_MB))


def _stdio_counts(path: str) -> tuple[int, int] | None:
    """``(late, with_chat)`` stdio servers in a generated MCP config (a Codex
    build's ``.servers.json`` beside its TOML): late when the build gave it a
    stand-in (``OTO_TOOL_STANDIN``), both for a call's warm stand-in (a
    stand-in whose program starts with the session, counted here once and
    never grown). None when the file is unreadable, not JSON or implausibly
    large."""
    from core.stdio_path_interceptor import STANDIN_ENV
    from services.mcp.mcp_manifest_types import is_stdio_transport
    from services.mcp.tool_listing import is_warm
    if path.endswith(".toml"):
        path = path[:-len(".toml")] + ".servers.json"
    try:
        if os.path.getsize(path) > _MCP_CONFIG_MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        servers = loaded.get("mcpServers", loaded) if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return None
    late = with_chat = 0
    for server in (servers or {}).values():
        if not isinstance(server, dict) or not is_stdio_transport(server.get("type")):
            continue
        marker = (server.get("env") or {}).get(STANDIN_ENV)
        if marker is None:
            with_chat += 1
            continue
        late += 1
        if is_warm(marker):
            with_chat += 1
    return late, with_chat


async def _session_estimate(session_id: str, execution_path: str | None,
                            mcp_config_path: str) -> int:
    """The reserve for a new session; the config is read off the loop, and
    only for a session not yet tracked."""
    counts = None
    if mcp_config_path and session_id not in _sessions:
        counts = await asyncio.to_thread(_stdio_counts, mcp_config_path)
    return _estimate_mb(execution_path, counts)


def _swap_credit_mb(swap_free_bytes: int | None = None) -> int:
    """min(SwapFree / 2, SESSION_SWAP_CREDIT_MB) — 0 when disabled (the
    default) or swapless (``host_resources.swap_credit_mb``, the formula a
    machine's guard reads too). ``swap_free_bytes`` is a machine's reported
    free swap; None reads this machine's own."""
    if config.SESSION_SWAP_CREDIT_MB <= 0:
        return 0
    if swap_free_bytes is None:
        swap_free_bytes = host_resources.swap_free_bytes()
    return host_resources.swap_credit_mb(max(0, swap_free_bytes) // (1024 * 1024),
                                         config.SESSION_SWAP_CREDIT_MB)


# A compute machine's memory report counts only this fresh: it comes with each
# heartbeat (20 s), so three missed ones read as nothing known, and nothing
# known admits nothing (the platform's own failed read does the same).
REPORT_FRESH_S = 60.0


class Reading(NamedTuple):
    """One box's memory as the gates read it (MB): its total (its own cap, or
    the host's), what is free now (the swap credit included; 0 when nothing
    is known), whether the total is a cap of its own, and whether a reading
    was there at all."""
    total_mb: int
    available_mb: int
    capped: bool
    known: bool


def compute_reading(machine_id: str) -> Reading:
    """A compute machine's memory, from its last report (``conn.memory``,
    0.5.173): free memory 0 when the report is missing or older than
    ``REPORT_FRESH_S``, so the gates admit nothing there."""
    from core.remote.satellite_connection import get_connection_manager
    held = get_connection_manager().machine_memory_report(machine_id, compute=True)
    if held is None:
        return Reading(0, 0, False, False)
    memory, age = held
    total = int(memory.get("total_bytes", 0)) // (1024 * 1024)
    if age > REPORT_FRESH_S:
        return Reading(total, 0, bool(memory.get("capped")), False)
    available = int(memory.get("available_bytes", 0)) // (1024 * 1024)
    if available:
        available += _swap_credit_mb(int(memory.get("swap_free_bytes", 0)))
    return Reading(total, available, bool(memory.get("capped")), True)


def host_of(box: str) -> str:
    """The host a box's memory belongs to: ``""`` for the platform and for
    every compute machine beside it (one budget, one count, one debit), the
    machine's id for a compute machine on another host, which is a host of
    its own."""
    if not box:
        return ""
    from core.remote.satellite_connection import get_connection_manager
    return box if get_connection_manager().owns_copy(box) else ""


def _box_reserved_mb(box: str) -> int:
    """The reservations of one box (a compute machine's cap is checked
    against its own). Over copies of the tables: the balancer reads it from a
    worker thread beside the loop."""
    boxes = dict(_session_box)
    return sum(est for sid, est in tuple(_session_est.items()) if boxes.get(sid, "") == box)


def _growin_debit_mb(host: str = "") -> int:
    """Σ un-materialized estimate of sessions admitted < ``_GROW_WINDOW_S`` ago
    on one host (the platform's, or a compute machine on another host).

    Linear decay: a session admitted N seconds ago is assumed to have grown
    into ``est × N/window`` of its reservation already (visible in live RAM),
    so only the remainder is debited — conservative during the window, zero
    after it (no steady-state double-count). Over copies of the tables: the
    balancer reads it from a worker thread beside the loop."""
    now = time.monotonic()
    hosts, ests = dict(_session_host), dict(_session_est)
    debit = 0
    for sid, added in tuple(_session_added_at.items()):
        if hosts.get(sid, "") != host:
            continue
        elapsed = now - added
        if elapsed < _GROW_WINDOW_S:
            debit += int(ests.get(sid, 0) * (1.0 - elapsed / _GROW_WINDOW_S))
    # A tool's program started later grows in over the same window, on its
    # session's host. Over a copy: the balancer reads it from a worker thread.
    for at, mb, grown_on in tuple(_grow_debits):
        if grown_on == host and now - at < _GROW_WINDOW_S:
            debit += int(mb * (1.0 - (now - at) / _GROW_WINDOW_S))
    return debit


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


def _is_head(ticket: object, host: str = "") -> bool:
    """Whether ``ticket`` leads its host's admission line (a finished waiter's
    ticket is dropped: its own cleanup already ran or never will). Each host
    has a line of its own: a full host never holds a waiter for another."""
    for entry in list(_line):
        held, entry_host = entry
        if entry_host != host:
            continue
        if held is ticket:
            return True
        if isinstance(held, asyncio.Task) and held.done():
            with contextlib.suppress(ValueError):
                _line.remove(entry)
            continue
        return False
    return False


# The parked tasks' ring (`_acquire_task`): one FIFO per creator and the
# round-robin order of the creators, so one person's burst takes one slot
# per round and a single run waits behind at most one run of each other
# creator. A waiter is its (session id, estimate, box) ticket.
_task_ring: dict[str, collections.deque[tuple[str, int, str]]] = {}
_task_ring_order: collections.deque[str] = collections.deque()


def _ring_join(key: str, ticket: tuple[str, int, str]) -> None:
    line = _task_ring.get(key)
    if line is None:
        line = _task_ring[key] = collections.deque()
        _task_ring_order.append(key)
    line.append(ticket)


def _ring_leave(key: str, ticket: tuple[str, int, str]) -> None:
    line = _task_ring.get(key)
    if line is None:
        return
    with contextlib.suppress(ValueError):
        line.remove(ticket)
    if not line:
        del _task_ring[key]
        with contextlib.suppress(ValueError):
            _task_ring_order.remove(key)


# ---------------------------------------------------------------------------
# Graceful LRU eviction
# ---------------------------------------------------------------------------

def _turn_live(sid: str, s: object) -> bool:
    """True while the session is inside a turn: streaming, running a
    foreground tool, or waiting on a person's answer, read from what each
    session object exposes (a machine session's running turn, a terminal's
    open turn or parked question), any engine's question waiting on a
    person, and the in-process engine's stream map."""
    try:
        if (getattr(s, "turn_active", False)
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
    except ImportError:
        return False


def placed_on_compute(session_id: str) -> str:
    """The compute machine a live session's registered placement names, ""
    for any other (its security context; a fresh start not registered yet
    answers through the target its caller passed)."""
    from core import placement
    from core.session import session_state
    ctx = session_state.get_session_security(session_id)
    pl = getattr(ctx, "placement", None) if ctx is not None else None
    if pl is not None and pl.kind == placement.KIND_COMPUTE and pl.machine_id:
        return str(pl.machine_id)
    return ""


def compute_sessions() -> list[tuple[str, object, str, str]]:
    """The live sessions a compute machine runs for the platform, each with
    its eviction source and its machine: its headless records
    (``compute``; one in its machine's reconnect grace counts live,
    its slot kept for the machine's return; eviction spares it) and its
    terminals, by their placement. The remote layer is read, never made."""
    out: list[tuple[str, object, str, str]] = []
    from core.session import session_manager
    remote = session_manager._remote_layer
    if remote is not None:
        for sid, info in list(remote._sessions.items()):
            if not info.alive or not info.compute:
                continue
            if session_manager.live_session(sid) is not None:
                out.append((sid, info, "remote", info.machine_id))
    try:
        from core.session import interactive_session as _is
        for sid in _is.live_session_ids() - _is.live_session_ids(local_only=True):
            s = _is.get(sid)
            machine = placed_on_compute(sid) if s is not None else ""
            if machine:
                out.append((sid, s, "interactive", machine))
    except ImportError:
        pass
    return out


async def _oldest_evictable_local(min_idle_s: float, *,
                                  prefer_user: str | None = None,
                                  only_user: str | None = None,
                                  pending_leash_s: float | None = None,
                                  turn_leash_s: float | None = None,
                                  prewarm_any_age: bool = False,
                                  box_first: str | None = None,
                                  host: str | None = None,
                                  ) -> _Scan:
    """Best eviction victim, plus how many otherwise-evictable sessions were
    spared: with ``pending_leash_s`` a session whose registries show a
    running command or subagent, with ``turn_leash_s`` a session inside a
    turn, each skipped until it has been idle that long (None = no leash).

    Scans Direct LLM's pool, the terminals and the sessions a compute machine
    runs for the platform (``compute_sessions``). ``box_first`` (a compute machine whose own cap
    binds) puts that box's sessions ahead of every other; ``host`` limits the
    scan to one host's sessions (evicting another host's frees nothing on
    this one). A candidate must
    hold a ``chat`` reservation (task, meeting and
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
    except ImportError:
        _pw_entries = {}
    try:
        from core.session.session_state import get_hook_activity
    except ImportError:
        def get_hook_activity(_sid: str) -> float:  # type: ignore[misc]  # an ImportError fallback: mypy wants conditional variants to share parameter names (checker limitation)
            return 0.0
    cands: list[tuple[int, int, float, str, str, bool]] = []

    spared_background = 0
    spared_live = 0

    def consider(sid: str, s: object, source: str) -> None:
        nonlocal spared_background, spared_live
        if _sessions.get(sid) != "chat":
            return  # must hold a reclaimable reservation to be worth evicting
        if host is not None and _session_host.get(sid, "") != host:
            return
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
        elsewhere = int(box_first is not None and _session_box.get(sid, "") != box_first)
        cands.append((elsewhere, group, -age, sid, source, is_pw))

    s: object
    try:
        from core.layers.direct.session import _direct_sessions, _direct_sessions_lock
        async with _direct_sessions_lock:
            for sid, s in list(_direct_sessions.items()):
                consider(sid, s, "direct")
    except ImportError:
        pass
    try:
        from core.session import interactive_session as _is
        for sid in _is.live_session_ids(local_only=True):
            s = _is.get(sid)
            if s is not None:
                consider(sid, s, "interactive")
    except ImportError:
        pass
    from core.session import session_manager
    for sid, s, source, _machine in compute_sessions():
        live = session_manager.live_session(sid) if source == "remote" else None
        if live is None or not live.grace:  # a record in its machine's grace is never a victim
            consider(sid, s, source)

    if not cands:
        return _Scan(None, spared_background, spared_live)
    cands.sort()  # (box, group asc, -age asc ⇒ most-idle first within a group)
    _, _, _, sid, source, is_pw = cands[0]
    return _Scan((sid, source, is_pw), spared_background, spared_live)


def _satellite_stats() -> list[dict]:
    """Per-satellite live counts + load (filled by the satellite layer). Empty
    until the satellite connection manager is up."""
    try:
        from core.remote.satellite_connection import get_connection_manager
        return get_connection_manager().concurrency_stats()
    except (ImportError, ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Reconciliation — safety net for orphaned slots
# ---------------------------------------------------------------------------

def _live_local_sids() -> set[str]:
    """Every session id a registry holds alive that holds a slot here:
    Direct LLM's pool, the live pumps, the local interactive PTYs and the
    sessions a compute machine runs for the platform."""
    from core.events.stream_pump import _active_pumps
    from core.session.session_manager import holding_engines

    # The live sessions of every engine that holds its own in the proxy,
    # straight from the registry: a pool read by name here would leave another
    # holder's sessions reading as orphaned, and their slots released.
    live_sids: set[str] = set()
    for layer in holding_engines().values():
        live_sids.update(layer.local_session_ids())
    live_sids.update(p.session_id for p in _active_pumps.values() if not p.is_done)

    try:
        from core.session.interactive_session import live_session_ids
        live_sids.update(live_session_ids(local_only=True))
    except ImportError:
        pass
    live_sids.update(sid for sid, _s, _source, _machine in compute_sessions())
    return live_sids
