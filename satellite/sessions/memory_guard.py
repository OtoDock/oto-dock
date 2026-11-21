"""The memory guard: a thread that keeps this machine's reserve free.

Nothing else limits what a session's own processes use: admission reserves an
estimate and never enforces it, and the kernel acts only once swap is full. This thread
reads the machine's free memory (``_vendored/host_resources``, the numbers the
platform's admission reads too) twice a second, ten times a second near the
reserve, and the memory of every process tree this satellite started for the
platform (``SessionManager.memory_units``). When free memory is about to fall
under the reserve it stops the command that took the memory since the machine
was last settled, in the unit that took the most; pressure the units did not
cause stops nothing, and admission refuses new starts until memory is free.

The rules, one stop at most per pass:

1. A session or terminal past its ceiling: its part that grew most.
2. The projected reading under the guard's reserve: the unit that grew most
   since its baseline, if that growth reaches ``min_stop``; then its part that
   grew most.
3. Inside the band, free swap fell ``SWAP_FALL_MB`` in ``SWAP_WINDOW_S`` while
   a unit's growth reaches ``min_stop``: that unit, as in rule 2, above the
   reserve.

A part is the unit's engine (found through the wrappers above it), a command
(the subtree under one of the engine's children, or under a wrapper's child
that is not on the engine's chain), or a compute session's sandbox memory
folders. A stopped command gets one sentence on its standard error; a stopped
engine or folders end the session, which the satellite then reports with the
reason ``memory``. Linux only: elsewhere the reading is not measured and the
guard does not start (``memory_guard: false``).
"""

from __future__ import annotations

import collections
import contextlib
import ctypes
import logging
import os
import signal
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import config
from .._vendored import host_resources

logger = logging.getLogger("satellite")

_MIB = 1024 * 1024

READ_S = 0.5            # the reading's period above the band
READ_BAND_S = 0.1       # inside the band
WALK_S = 2.0            # the walk's period while a unit lives
WALK_BAND_S = 0.5       # inside the band while the reading falls
WALK_MIN_S = 0.1        # at most this often when the projection is under the reserve
FALL_WINDOW_S = 1.0     # the fall rate's window
FALLING_MB_S = 64       # a fall this fast counts as falling
SETTLE_WINDOW_S = 60.0  # the window of "settled": a net fall under half of min_stop
#: The most a unit's growth is counted for while the machine sits quiet near its
#: reserve: a part must grow min_stop within it to be a cause by size (0.57 MB/s at
#: 512, 0.28 at 256), far above what an engine grows at rest. A slower leak is still
#: the cause at the reserve when it took half of what fell (``accounts``).
BASELINE_AGE_S = 900.0
SWAP_WINDOW_S = 5.0
SWAP_FALL_MB = 256
START_S = 90.0          # a unit's first seconds are its start (admission's grow-in window)
#: What a unit may grow in its start before it counts: admission's HEAVY, what
#: it reserved for a session whose config it did not know (``start_mb``).
START_ALLOWANCE_MB = 850
PENDING_S = 2.0         # after a stop, no new stop until its processes are gone or this long
PENDING_FALLING_S = 0.2  # ... unless the reading still falls fast after this long
STALL_S = 10.0          # a pass older than this reports the guard stalled
SENTENCE_WAIT_S = 0.1   # the most a stop waits for its sentence's write
SENTENCE_HELPERS = 4    # writes still hanging, at most: each holds a thread and one descriptor
BAND_MIN_MB = 1024      # the band is the reserve plus the larger of the reserve and this
ACCOUNTS_MB = 64        # a part this large that took half of what fell since its baseline is the cause
_LOG_EVERY_S = 60.0

COMMAND_SENTENCE = "That command used too much memory and was stopped."
SENTENCES = {
    "session": COMMAND_SENTENCE,
    "terminal": COMMAND_SENTENCE,
    "step": COMMAND_SENTENCE,
    "tool": "That tool used too much memory and was stopped.",
    "app": "The app server used too much memory and was stopped.",
    "own": "The build used too much memory and was stopped.",
}
ENGINE_KINDS = frozenset({"session", "terminal"})
#: The programs above an engine: the score's shell, the sandbox's network
#: helper and bubblewrap. Known by the program file's name.
WRAPPERS = frozenset({"sh", "dash", "bash", "pasta", "pasta.avx2", "passt", "passt.avx2", "bwrap"})
#: Where a sandbox mounts its memory filesystems (bubblewrap's ``--dev`` and
#: ``--tmpfs /tmp``): what a session writes there is memory no process holds.
MEMORY_FOLDERS = frozenset({"/tmp", "/dev", "/dev/shm"})
#: The sandbox's launcher, a script: known by its process name until it execs pasta.
WRAPPER_NAMES = frozenset({"oto-sandbox-net"})
#: A satellite's own lever, read at start: a lower ceiling per session in MB.
LOCAL_CEILING_ENV = "OTO_SAT_SESSION_MEMORY_MAX_MB"
# A floor of any size is kept (the rig closes admission with a huge one): the
# guard's reserve is clamped to half the machine anyway.
_POLICY_KEYS = {"share": (0.0, 0.5), "floor_mb": (0, 1 << 40), "cap_mb": (0, 1 << 26), "credit_mb": (0, 1 << 22),
                "min_stop_mb": (0, 1 << 22), "session_max_mb": (0, 1 << 26), "start_mb": (0, 1 << 22)}


@dataclass(frozen=True)
class Unit:
    """One process tree the satellite started: its root, the session it serves
    (``""`` for none), its kind and, for a session or terminal, its engine's
    binary name (``claude``, ``codex``)."""
    pid: int
    session_id: str
    kind: str
    engine: str = ""


@dataclass
class Part:
    key: tuple
    kind: str                         # "engine" | "command" | "folders"
    pids: list[tuple[int, int]]       # (pid, start)
    size_mb: float


@dataclass
class Walked:
    unit: Unit
    key: tuple
    pids: list[tuple[int, int]]
    parts: list[Part] = field(default_factory=list)
    other_mb: float = 0.0             # the wrappers' and the root's own memory
    age_s: float = 0.0                # the unit's age (its root's own clock)

    @property
    def total_mb(self) -> float:
        return self.other_mb + sum(p.size_mb for p in self.parts)


@dataclass(frozen=True)
class Numbers:
    total: int
    reserve: int
    guard: int
    band: int
    min_stop: int
    ceiling: int
    credit: int
    start: int


class Guard:
    """The guard's state and its passes. The readers (``_read``, ``_table``,
    ``_size``, ``_exe``, ``_folders``, ``_children``) and the act
    (``_signal``, ``_sentence``) are methods a test replaces."""

    def __init__(self, units: Callable[[], list[Unit]], *, compute: bool,
                 mark_ended: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._units = units
        self.compute = compute
        self._mark_ended = mark_ended or (lambda _sid: None)
        self._clock = clock
        self.policy: dict = {}
        self.send: Callable[[dict], None] | None = None
        self.local_ceiling_mb = _env_mb(LOCAL_CEILING_ENV)
        self._readings: collections.deque[tuple[float, float]] = collections.deque()
        self._swaps: collections.deque[tuple[float, float]] = collections.deque()
        self._baseline: dict[tuple, tuple[float, float]] = {}   # part -> (size, the reading then)
        self._credit: dict[tuple, float] = {}   # unit -> what its start may keep (fixed at its end)
        self._young: set[tuple] = set()          # units the guard saw in their start
        self._settled_at: dict[tuple, float] = {}  # unit -> when its baselines were last taken
        self._last_walk = float("-inf")
        self._walk_s = 0.0
        self._pending: tuple[float, frozenset[tuple[int, int]]] | None = None
        self._logged: dict[str, float] = {}
        self.period = READ_S
        self.last_pass = clock()
        self.stops = 0
        self._helper: threading.Thread | None = None   # the last helper started
        self._helpers: dict[tuple, threading.Thread] = {}   # unit -> its sentence's helper while it writes
        self.own_pid = os.getpid()

    # -- the numbers ------------------------------------------------------

    def numbers(self, total_mb: int) -> Numbers:
        """This machine's reserve, band, min_stop and ceiling: the platform's
        numbers for its kind (``memory_policy``), else the defaults the
        platform would send."""
        p = self.policy
        if self.compute:
            share, floor, cap = host_resources.COMPUTE_SHARE, host_resources.COMPUTE_FLOOR_MB, 0
        else:
            share, floor, cap = (host_resources.COMPUTER_SHARE, host_resources.COMPUTER_FLOOR_MB,
                                 host_resources.COMPUTER_CAP_MB)
        reserve = host_resources.reserve_mb(total_mb, floor_mb=int(p.get("floor_mb", floor)),
                                            share=float(p.get("share", share)),
                                            cap_mb=int(p.get("cap_mb", cap)))
        guard = host_resources.guard_reserve_mb(total_mb, reserve)
        # The room above the guard's own reserve: a floor set to close admission
        # leaves the running sessions theirs.
        ceiling = host_resources.ceiling_mb(total_mb, guard, session_max_mb=int(p.get("session_max_mb", 0)))
        if self.local_ceiling_mb:
            ceiling = min(ceiling, self.local_ceiling_mb)
        return Numbers(total=total_mb, reserve=reserve, guard=guard, band=guard + max(guard, BAND_MIN_MB),
                       min_stop=host_resources.min_stop_mb(
                           guard, int(p.get("min_stop_mb", host_resources.MIN_STOP_CAP_MB))),
                       ceiling=ceiling, credit=int(p.get("credit_mb", 0)),
                       start=int(p.get("start_mb", START_ALLOWANCE_MB)))

    def set_policy(self, policy: object) -> None:
        """Keep the platform's numbers, each checked; an unknown or odd one
        is dropped and its default stands."""
        kept = {}
        if isinstance(policy, dict):
            for key, (low, high) in _POLICY_KEYS.items():
                val = policy.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool) and low <= val <= high:
                    kept[key] = val
        self.policy = kept

    # -- the readers (replaced in tests) -----------------------------------

    def _read(self) -> tuple[int, int, int, bool]:
        """``(available_mb, total_mb, swap_free_mb, known)``."""
        r = host_resources.report()
        return (int(r["available_bytes"]) // _MIB, int(r["total_bytes"]) // _MIB,
                int(r["swap_free_bytes"]) // _MIB, bool(r.get("known")))

    def _table(self) -> dict[int, tuple[int, int]]:
        """Every process: ``pid -> (ppid, start)`` from ``/proc/<pid>/stat``."""
        out: dict[int, tuple[int, int]] = {}
        with os.scandir("/proc") as it:
            for entry in it:
                if not entry.name.isdigit():
                    continue
                stat = _stat(int(entry.name))
                if stat is not None:
                    out[int(entry.name)] = stat
        return out

    def _size(self, pid: int) -> float:
        """A process's own memory in MB: anonymous, shared and swapped pages."""
        try:
            with open(f"/proc/{pid}/status") as fh:
                kb = 0
                for line in fh:
                    if line.startswith(("RssAnon:", "RssShmem:", "VmSwap:")):
                        kb += int(line.split()[1])
                return kb / 1024
        except (OSError, ValueError, IndexError):
            return 0.0

    def _exe(self, pid: int) -> str:
        try:
            return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            return ""

    def _name(self, pid: int) -> str:
        try:
            with open(f"/proc/{pid}/comm") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _is_wrapper(self, pid: int) -> bool:
        """Read at every walk, never kept: an exec changes a process's program
        and keeps its pid and start. By its program file, or by its name when
        the kernel does not show the file (pasta hides it). Only a process
        above the engine is ever asked: a command named like a wrapper below
        it stays a command."""
        exe = self._exe(pid)
        name = self._name(pid)
        return exe in WRAPPERS or name in WRAPPER_NAMES or (not exe and name in WRAPPERS)

    def _folders(self, pid: int) -> tuple[float, tuple]:
        """A compute session's sandbox memory folders seen from its engine:
        the memory filesystems (tmpfs) mounted at its ``/tmp``, ``/dev`` or
        ``/dev/shm``, from its own mount table, and their use in MB (one mount
        counted once). A folder on a disk is not memory and is not counted."""
        try:
            with open(f"/proc/{pid}/mountinfo") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return 0.0, ()
        used, ids = 0.0, []
        for line in lines:
            left, _, right = line.partition(" - ")
            fields, kind = left.split(), right.split()[:1]
            if len(fields) < 5 or kind != ["tmpfs"] or fields[4] not in MEMORY_FOLDERS:
                continue
            try:
                st = os.statvfs(f"/proc/{pid}/root{fields[4]}")
            except OSError:
                continue
            ids.append(fields[0])
            used += (st.f_blocks - st.f_bfree) * st.f_frsize / _MIB
        return used, tuple(ids)

    def _children(self, pid: int) -> list[int]:
        """A process's children now, from its threads' ``children`` lists."""
        kids: list[int] = []
        try:
            for tid in os.listdir(f"/proc/{pid}/task"):
                with contextlib.suppress(OSError), open(f"/proc/{pid}/task/{tid}/children") as fh:
                    kids.extend(int(k) for k in fh.read().split())
        except OSError:
            pass
        return kids

    def _start_of(self, pid: int) -> int | None:
        stat = _stat(pid)
        return None if stat is None else stat[1]

    def _signal(self, pid: int, start: int, sig: int) -> bool:
        """Signal ``pid`` only while it is still the process of that start."""
        if self._start_of(pid) != start:
            return False
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False

    def _descriptor(self, pid: int, start: int, fd: int) -> int:
        """A copy of the process's own descriptor ``fd`` (``pidfd_getfd``):
        the same open file, with the access the process itself has. Raises
        OSError when the pid no longer names the process of that start (checked
        once the pidfd holds it) or the kernel refuses the copy."""
        pidfd = os.pidfd_open(pid)
        try:
            if self._start_of(pid) != start:
                raise ProcessLookupError(pid)
            return _pidfd_getfd(pidfd, fd)
        finally:
            os.close(pidfd)

    def _sentence(self, pid: int, start: int, text: str, owner: tuple = ()) -> None:
        """The stop's sentence on the stopped command's standard error,
        written through the command's own descriptor and nothing else: never
        a file reopened with the satellite's rights: the descriptor's own
        access decides whether anything is written (one held read-only or on a
        folder takes nothing). The writer is the satellite, so the sentence
        goes only to a file, a pipe, a socket or a terminal, never to one of
        the kernel's own files. Written by a helper the stop waits on for at
        most ``SENTENCE_WAIT_S``, one helper per unit (``owner``) and
        ``SENTENCE_HELPERS`` at most: a full pipe or a hung network mount costs
        that unit its later sentences, never the stop and never another unit's
        sentence."""
        self._helpers = {k: t for k, t in self._helpers.items() if t.is_alive()}
        if owner in self._helpers or len(self._helpers) >= SENTENCE_HELPERS:
            return
        line = ("\n" + text + "\n").encode()

        def write() -> None:
            try:
                fd = self._descriptor(pid, start, 2)
            except (OSError, AttributeError) as e:   # AttributeError: a Python with no os.pidfd_open
                self._log_once("sentence", "memory: no sentence for a stopped command: %s", e)
                return
            try:
                if _takes_a_sentence(fd):
                    os.write(fd, line)
            except OSError:
                pass
            finally:
                os.close(fd)

        self._helper = self._helpers[owner] = threading.Thread(
            target=write, name="memory-guard-sentence", daemon=True)
        self._helper.start()
        self._helper.join(SENTENCE_WAIT_S)

    # -- one pass -----------------------------------------------------------

    def run_pass(self) -> None:
        """One reading, maybe one walk, maybe one stop."""
        now = self._clock()
        self.last_pass = now
        avail, total, swap_free, known = self._read()
        if not known or total <= 0:
            self.period = READ_S
            return
        nums = self.numbers(total)
        avail += host_resources.swap_credit_mb(swap_free, nums.credit)
        _keep(self._readings, now, avail, SETTLE_WINDOW_S)
        _keep(self._swaps, now, swap_free, SWAP_WINDOW_S)
        fall = _fall_rate(self._readings, now)
        in_band = avail < nums.band
        self.period = READ_BAND_S if in_band else READ_S
        projected = avail - max(0.0, fall) * (self.period + self._walk_s)
        urgent = projected < nums.guard
        # Only near the reserve: swap also falls for what no session caused (a
        # container at its own cap, a slice with MemoryHigh), and a machine
        # with room leaves a busy session alone.
        swap_fell = in_band and self._swaps[0][1] - swap_free >= SWAP_FALL_MB
        every = WALK_BAND_S if in_band and fall >= FALLING_MB_S else WALK_S
        if urgent or swap_fell:
            every = WALK_MIN_S
        if now - self._last_walk < every:
            return
        if not self._children(self.own_pid):
            return  # no unit can exist without a child of the satellite: no walk
        began = self._clock()
        walked = self.walk()
        self._walk_s = max(0.0, self._clock() - began)
        self._last_walk = now
        # Settled: not falling fast, and above the band or with a small net fall
        # well above the reserve. Under ``guard + 2 × min_stop`` the baselines
        # never move, so a slow leak still counts by the time the reading
        # reaches the reserve; while the reading falls fast they never move
        # either, so a runaway that starts on a machine with room counts from
        # its start.
        # Quiet: not falling, not under the reserve. A unit whose baselines are
        # older than ``BASELINE_AGE_S`` is settled then too, so growth spread
        # over hours never makes a resting chat the cause of someone else's dip.
        quiet = not swap_fell and fall < FALLING_MB_S and not urgent and self._net_fall() < nums.min_stop / 2
        settled = not swap_fell and fall < FALLING_MB_S and (avail >= nums.band or (
            avail >= nums.guard + 2 * nums.min_stop and self._net_fall() < nums.min_stop / 2))
        self._note_baselines(walked, settled, avail, nums.start, aged=quiet, now=now)
        self.decide(walked, nums, avail=avail, urgent=urgent, swap_fell=swap_fell, fall=fall, now=now)

    def _net_fall(self) -> float:
        return self._readings[0][1] - self._readings[-1][1] if self._readings else 0.0

    # -- the walk -----------------------------------------------------------

    def walk(self) -> list[Walked]:
        """Every unit's parts and their sizes, from one process table."""
        try:
            units = list(self._units())
        except RuntimeError:  # a table changed while it was copied: the next pass reads it
            return []
        table = self._table()
        kids: dict[int, list[int]] = collections.defaultdict(list)
        for pid, (ppid, _start) in table.items():
            kids[ppid].append(pid)
        own = self.own_pid
        roots = {u.pid for u in units}
        units += [Unit(pid, "", "own") for pid in kids.get(own, ()) if pid not in roots]
        skip = self._pending[1] if self._pending else frozenset()
        out = []
        for unit in units:
            if unit.pid not in table or unit.pid == own:
                continue
            walked = self._split(unit, table, kids, skip)
            if walked is not None:
                out.append(walked)
        return out

    def _split(self, unit: Unit, table: dict, kids: dict, skip: frozenset) -> Walked | None:
        start = table[unit.pid][1]
        key = (unit.pid, start)

        def subtree(pid: int) -> list[tuple[int, int]]:
            got, stack = [], [pid]
            while stack:
                p = stack.pop()
                if p not in table:
                    continue
                got.append((p, table[p][1]))
                stack.extend(kids.get(p, ()))
            return [x for x in got if x not in skip]

        everything = subtree(unit.pid)
        walked = Walked(unit, key, everything, age_s=_age_s(start))
        if unit.kind not in ENGINE_KINDS:
            walked.parts.append(Part(("command", key), "command", everything,
                                     sum(self._size(p) for p, _ in everything)))
            return walked
        chain, side, engine = [], [], None
        pid = unit.pid
        while pid in table:
            if not self._is_wrapper(pid):
                engine = pid
                break
            chain.append(pid)
            children = sorted(kids.get(pid, ()), key=lambda k: table[k][1] if k in table else 0)
            if not children:
                break
            side.extend(children[1:])
            pid = children[0]
        # A launcher (Codex's node script) that runs the engine's own binary as
        # its one child: the child is the engine. Never past a process that is
        # the engine's binary itself, so a command named like it stays a command.
        while engine is not None and unit.engine and not self._exe(engine).startswith(unit.engine):
            children = kids.get(engine, ())
            if len(children) == 1 and self._exe(children[0]).startswith(unit.engine):
                chain.append(engine)
                engine = children[0]
                continue
            break
        walked.other_mb = sum(self._size(p) for p in chain)
        if engine is not None:
            estart = table[engine][1]
            walked.parts.append(Part(("engine", engine, estart), "engine", [(engine, estart)],
                                     self._size(engine)))
            side = list(kids.get(engine, ())) + side
            if self.compute:
                used, devs = self._folders(engine)
                walked.parts.append(Part(("folders", key, devs), "folders", [], used))
        for child in side:
            pids = subtree(child)
            if pids:
                walked.parts.append(Part(("command", child, table[child][1]), "command", pids,
                                         sum(self._size(p) for p, _ in pids)))
        return walked

    # -- baselines and growth ------------------------------------------------

    def _note_baselines(self, walked: list[Walked], settled: bool, avail: float, start_mb: int, *,
                        aged: bool = False, now: float = 0.0) -> None:
        """A part's baseline: its size at the last settled walk; zero for a
        part born since. At the end of a unit's start its credit is fixed:
        what it grew in its start, at most ``start_mb``, is excused for good
        and everything past it counts, a runaway that began in the start too.
        With ``aged`` (the machine quiet), a unit whose baselines are older
        than ``BASELINE_AGE_S`` takes new ones as at a settled walk."""
        live = set()
        for w in walked:
            taken = self._settled_at.setdefault(w.key, now)
            renew = settled or (aged and now - taken >= BASELINE_AGE_S)
            if renew:
                self._settled_at[w.key] = now
            # A unit first seen past its start (the guard started after it):
            # its history is unknown, so its parts start at their sizes now.
            known = w.key in self._credit or w.age_s < START_S
            if w.age_s >= START_S and w.key not in self._credit:
                self._credit[w.key] = (min(float(start_mb), sum(self.growth(p) for p in w.parts))
                                       if w.key in self._young else 0.0)
            if w.age_s < START_S:
                self._young.add(w.key)
            for part in w.parts:
                live.add(part.key)
                if renew or (part.key not in self._baseline and not known):
                    self._baseline[part.key] = (part.size_mb, avail)
                elif part.key not in self._baseline:
                    self._baseline[part.key] = (0.0, avail)
            if renew and w.key in self._credit:
                self._credit[w.key] = 0.0
        for key in [k for k in self._baseline if k not in live]:
            del self._baseline[key]
        units = {w.key for w in walked}
        for key in [k for k in self._credit if k not in units]:
            del self._credit[key]
        for key in [k for k in self._settled_at if k not in units]:
            del self._settled_at[key]
        self._young &= units

    def growth(self, part: Part) -> float:
        return max(0.0, part.size_mb - self._baseline.get(part.key, (0.0, 0.0))[0])

    def accounts(self, part: Part, avail: float) -> bool:
        """Whether a part took at least half of what the reading fell since its
        baseline (and ``ACCOUNTS_MB``): a runaway that starts just above the
        reserve is the cause there, before it reaches min_stop."""
        grown = self.growth(part)
        fell = self._baseline.get(part.key, (0.0, avail))[1] - avail
        return grown >= ACCOUNTS_MB and fell > 0 and grown * 2 >= fell

    def unit_growth(self, w: Walked, start_mb: int = START_ALLOWANCE_MB) -> float:
        """A unit's growth since its baselines, less what its start may keep:
        all of ``start_mb`` while it starts, its fixed credit after."""
        grown = sum(self.growth(p) for p in w.parts)
        credit = float(start_mb) if w.age_s < START_S else self._credit.get(w.key, 0.0)
        return max(0.0, grown - credit)

    # -- the rules ------------------------------------------------------------

    def decide(self, walked: list[Walked], nums: Numbers, *, avail: float, urgent: bool,
               swap_fell: bool, fall: float, now: float) -> None:
        if self._pending is not None:
            at, pids = self._pending
            alive = any(self._start_of(p) == s for p, s in pids)
            if alive and now - at < PENDING_S and not (now - at >= PENDING_FALLING_S and fall >= FALLING_MB_S):
                return
            self._pending = None
        for w in walked:
            if w.unit.kind in ENGINE_KINDS and w.total_mb > nums.ceiling and w.parts:
                # The largest command when stopping it brings the session under
                # its ceiling; otherwise the session itself.
                commands = [p for p in w.parts if p.kind == "command"]
                largest = max(commands, key=lambda p: p.size_mb, default=None)
                if largest is not None and w.total_mb - largest.size_mb <= nums.ceiling:
                    part = largest
                else:
                    part = next((p for p in w.parts if p.kind == "engine"), max(w.parts, key=lambda p: p.size_mb))
                self.stop(w, part, nums, avail, why="ceiling", now=now)
                return
        if not (urgent or swap_fell) or not walked:
            if urgent:
                self._log_once("nocause", "memory: %d MB free, under the reserve of %d MB, and no session grew; "
                               "nothing is stopped", int(avail), nums.guard)
            return

        def cause(w: Walked) -> bool:
            grown = self.unit_growth(w, nums.start)
            return grown >= nums.min_stop or (urgent and grown >= ACCOUNTS_MB and any(
                self.accounts(p, avail) for p in w.parts if p.kind == "command"))

        causes = [w for w in walked if cause(w)]
        victim = max(causes, key=lambda w: self.unit_growth(w, nums.start), default=None)
        if victim is None:
            self._log_once("nocause", "memory: %d MB free, under the reserve of %d MB, and no session grew "
                           "%d MB; nothing is stopped", int(avail), nums.guard, nums.min_stop)
            return
        part = max(victim.parts, key=self.growth)
        self.stop(victim, part, nums, avail, why="swap" if swap_fell and not urgent else "reserve", now=now)

    # -- the stop ---------------------------------------------------------------

    def stop(self, w: Walked, part: Part, nums: Numbers, avail: float, *, why: str, now: float) -> None:
        """Stop the part where it stands, write its sentence, then end it;
        an engine or the sandbox's folders end the whole session."""
        ends_session = part.kind != "command"
        pids = w.pids if ends_session else part.pids
        if ends_session and w.unit.session_id:
            self._mark_ended(w.unit.session_id)
        frozen: list[tuple[int, int]] = []
        try:
            frozen = self._freeze(pids)
            if not ends_session and frozen:
                self._sentence(*part.pids[0], SENTENCES.get(w.unit.kind, COMMAND_SENTENCE), owner=w.key)
        finally:
            # Every process of the last look, stopped or not: a child born
            # after the last round must not escape its parent's end.
            last = list(dict.fromkeys([*frozen, *pids, *self._born_under(frozen)]))
            for pid, start in last:
                self._signal(pid, start, signal.SIGKILL)
            frozen = last
        self._pending = (now, frozenset(frozen))
        self.stops += 1
        grown = self.growth(part) if why != "ceiling" else part.size_mb
        what = ("its own work, a build or a sync" if w.unit.kind == "own" else
                f"{'the session' if ends_session else 'a command'} of {w.unit.kind} {(w.unit.session_id or '-')[:8]}")
        logger.warning("memory: stopped %s (%d MB, %d MB grown, %d processes) with %d MB free, reserve %d MB (%s)",
                       what, int(part.size_mb), int(grown), len(frozen), int(avail), nums.guard, why)
        if self.send is not None and self.policy:
            with contextlib.suppress(Exception):
                self.send({"type": "memory_stop", "session_id": w.unit.session_id, "kind": w.unit.kind,
                           "what": "session" if ends_session else ("own" if w.unit.kind == "own" else "command"),
                           "mb": int(part.size_mb), "reading_mb": int(avail), "reserve_mb": nums.guard,
                           "why": why})

    def _freeze(self, pids: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """``SIGSTOP`` each process, then the children they forked meanwhile,
        at most three rounds; the processes actually stopped."""
        frozen: list[tuple[int, int]] = []
        seen: set[int] = set()
        todo = list(pids)
        for _round in range(3):
            more = []
            for pid, start in todo:
                if pid in seen:
                    continue
                seen.add(pid)
                if self._signal(pid, start, signal.SIGSTOP):
                    frozen.append((pid, start))
                    for kid in self._children(pid):
                        kid_start = self._start_of(kid)
                        if kid_start is not None and kid not in seen:
                            more.append((kid, kid_start))
            if not more:
                break
            todo = more
        return frozen

    def _born_under(self, pids: list[tuple[int, int]]) -> list[tuple[int, int]]:
        born = []
        for pid, _start in pids:
            for kid in self._children(pid):
                kid_start = self._start_of(kid)
                if kid_start is not None:
                    born.append((kid, kid_start))
        return born

    def _log_once(self, key: str, msg: str, *args) -> None:
        now = self._clock()
        if now - self._logged.get(key, float("-inf")) >= _LOG_EVERY_S:
            self._logged[key] = now
            logger.warning(msg, *args)

    # -- what the satellite asks ------------------------------------------------

    def start_refusal(self) -> str | None:
        """The last check of a new start: the sentence while this machine's
        own reading is under its guard reserve, else None."""
        avail, total, swap_free, known = self._read()
        if not known or total <= 0:
            return None
        nums = self.numbers(total)
        if avail + host_resources.swap_credit_mb(swap_free, nums.credit) < nums.guard:
            return COMPUTE_REFUSAL if self.compute else START_REFUSAL
        return None

    def state(self) -> object:
        """``True`` while it runs, ``"stalled"`` when its last pass is old."""
        return True if self._clock() - self.last_pass < STALL_S else "stalled"


START_REFUSAL = "This computer is low on memory. Close something on it or try again."
#: A compute machine's: the person cannot close anything on the server.
COMPUTE_REFUSAL = "The server is low on memory. Try again in a few minutes."


class MachineMemoryRefused(RuntimeError):
    """A start refused by this machine's own last check; its acknowledgement
    carries the reason, the platform shows the sentence."""
    reason = "machine_memory"


def _env_mb(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name, "0") or 0))
    except ValueError:
        return 0


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _stat(pid: int) -> tuple[int, int] | None:
    """``(ppid, start)`` of a process, start in clock ticks since boot."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        rest = raw[raw.rindex(b")") + 2:].split()
        return int(rest[1]), int(rest[19])
    except (ValueError, IndexError):
        return None


#: ``pidfd_getfd``'s number: one on every Linux architecture (new calls since 5.1 share theirs).
_SYS_PIDFD_GETFD = 438
#: The C library, loaded once where the guard runs: a stop's first sentence
#: must not pay the load out of its 0.1 s.
_LIBC = ctypes.CDLL(None, use_errno=True) if config.HOST.memory_guard else None
#: The kernel's own filesystems (``statfs.f_type``): their files act on who
#: writes, and the writer of a sentence is the satellite. procfs, sysfs,
#: cgroup v1 and v2, debugfs, tracefs, securityfs, bpffs, configfs, pstore,
#: efivarfs, selinuxfs and the anonymous inodes.
_KERNEL_FS = frozenset({0x9FA0, 0x62656572, 0x27E0EB, 0x63677270, 0x64626720, 0x74726163, 0x73636673,
                        0xCAFE4A11, 0x62656570, 0x6165676C, 0xDE5E81E4, 0xF97CFF8C, 0x09041934})


def _libc_errno() -> OSError:
    err = ctypes.get_errno()
    return OSError(err, os.strerror(err))


def _pidfd_getfd(pidfd: int, fd: int) -> int:
    """``pidfd_getfd(2)``: a copy of another process's descriptor, allowed to
    a process that may ptrace it (its ancestor under Yama's default). Python
    names no wrapper, so the call goes through libc's ``syscall``."""
    if _LIBC is None:
        raise OSError(38, "pidfd_getfd needs Linux")   # ENOSYS
    got = _LIBC.syscall(_SYS_PIDFD_GETFD, pidfd, fd, 0)
    if got < 0:
        raise _libc_errno()
    return int(got)


def _takes_a_sentence(fd: int) -> bool:
    """A file, a pipe, a socket or a terminal: what a command's output is, and
    what ends with its holders. Never a file of the kernel's own (procfs asks
    who writes, and the writer here is the satellite), another device, nor an
    object a blocked write keeps alive by itself (an eventfd)."""
    mode = os.fstat(fd).st_mode
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return True
    if stat.S_ISCHR(mode):
        return os.isatty(fd)
    if not stat.S_ISREG(mode) or _LIBC is None:
        return False
    buf = (ctypes.c_long * 32)()   # struct statfs, f_type first
    if _LIBC.fstatfs(fd, buf) != 0:
        raise _libc_errno()
    return (buf[0] & 0xFFFFFFFF) not in _KERNEL_FS


def _age_s(start_ticks: int) -> float:
    try:
        boot = time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return float("inf")
    return boot - start_ticks / _CLK_TCK


def _keep(series: collections.deque, now: float, value: float, window: float) -> None:
    series.append((now, value))
    while series and now - series[0][0] > window:
        series.popleft()


def _fall_rate(series: collections.deque, now: float) -> float:
    """How fast the reading falls, MB a second, over the last second."""
    recent = [(t, v) for t, v in series if now - t <= FALL_WINDOW_S]
    if len(recent) < 2 or recent[-1][0] <= recent[0][0]:
        return 0.0
    return (recent[0][1] - recent[-1][1]) / (recent[-1][0] - recent[0][0])


# -- the one guard of this satellite ----------------------------------------------

_guard: Guard | None = None
_thread: threading.Thread | None = None
_stop = threading.Event()


def supported() -> bool:
    """Linux with a readable memory: elsewhere the reading is not measured."""
    return config.HOST.memory_guard and bool(host_resources.report().get("known"))


def start(units: Callable[[], list[Unit]], *, compute: bool,
          mark_ended: Callable[[str], None] | None = None) -> bool:
    """Start the guard's thread once; False where it does not run."""
    global _guard, _thread
    if _thread is not None:
        return True
    if not supported():
        logger.info("memory: this machine's memory is not watched (only Linux is)")
        return False
    _guard = Guard(units, compute=compute, mark_ended=mark_ended)
    _stop.clear()

    def loop() -> None:
        guard = _guard
        while guard is not None and not _stop.is_set():
            try:
                guard.run_pass()
            except Exception:  # noqa: BLE001  # one bad pass must never end the guard
                guard._log_once("error", "memory: a pass of the guard failed")
                logger.debug("memory: the pass's error", exc_info=True)
            _stop.wait(guard.period)

    _thread = threading.Thread(target=loop, name="memory-guard", daemon=True)
    _thread.start()
    logger.info("memory: the guard watches this machine (reserve %d MB)",
                _guard.numbers(int(host_resources.report()["total_bytes"]) // _MIB).guard)
    return True


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(2.0)
    _thread = None


def running() -> bool:
    return _thread is not None and _guard is not None


def set_policy(policy: object) -> None:
    """The platform's numbers from its sign-in answer (``memory_policy``)."""
    if _guard is not None:
        _guard.set_policy(policy)


def set_sender(send: Callable[[dict], None] | None) -> None:
    if _guard is not None:
        _guard.send = send


def start_refusal() -> str | None:
    return _guard.start_refusal() if running() and _guard is not None else None


def report() -> dict:
    """This machine's memory as the platform's admission reads it (the
    vendored ``host_resources``), read again every time; empty when it cannot
    be read."""
    try:
        return host_resources.report()
    except Exception:  # noqa: BLE001  # a failed read must not stop the heartbeat or the sign-in
        logger.warning("memory: the report could not be read", exc_info=True)
        return {}


def frame_fields(compute: bool) -> dict:
    """What a sign-in or a heartbeat carries: the memory report of a compute
    machine or of a machine whose guard runs, and the guard's state."""
    if not (compute or running()):
        return {}
    fields: dict = {"memory": report()}
    if _guard is not None:
        fields["memory_guard"] = _guard.state()
    return fields
