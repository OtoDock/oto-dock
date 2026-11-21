"""The memory guard (``satellite/sessions/memory_guard.py``).

A planted process table and planted readings drive the guard's real rules: a
runaway is stopped before the machine's reserve is eaten, pressure the units
did not cause stops nothing, a session never becomes the victim of another's
growth, the engine and its wrappers are never stopped as a command, and the
sentence is written while the command is stopped, before it is killed. One test
runs a real child and stops it for real.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from satellite.sessions import memory_guard as mg
from satellite.sessions.memory_guard import Guard, Unit

SAT = 1          # the satellite's own pid in the planted tables
TOTAL = 20_480   # MB: a 20 GB compute machine, reserve 1,024, band 2,048, min_stop 512


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Planted(Guard):
    """The guard over a planted machine: ``procs`` maps pid to
    ``(ppid, exe, size_mb)``; every start time is the pid itself."""

    def __init__(self, units, *, compute=True, avail=10_000, swap=0, ages=None, folders=None):
        self.clock = Clock()
        self.ended: list[str] = []
        super().__init__(lambda: list(units), compute=compute, mark_ended=self.ended.append, clock=self.clock)
        # The platform's numbers for the machine's kind (a 2.0 platform sent them).
        self.set_policy({"floor_mb": 512, "share": 0.05} if compute else {"floor_mb": 1024, "share": 0.10})
        self.own_pid = SAT
        self.procs: dict[int, tuple[int, str, float]] = {}
        self.avail, self.swap, self.total = avail, swap, TOTAL
        self.ages = ages or {}
        self.folder_mb = folders or {}
        self.acts: list[tuple[str, int]] = []
        self.names: dict[int, str] = {}
        self.sent: list[dict] = []
        self.send = self.sent.append

    def _read(self):
        return self.avail, self.total, self.swap, True

    def _table(self):
        return {pid: (ppid, pid) for pid, (ppid, _exe, _mb) in self.procs.items()}

    def _size(self, pid):
        return self.procs[pid][2] if pid in self.procs else 0.0

    def _exe(self, pid):
        return self.procs[pid][1] if pid in self.procs else ""

    def _name(self, pid):
        return self.names.get(pid, "")

    def _folders(self, pid):
        return self.folder_mb.get(pid, 0.0), (pid,)

    def _children(self, pid):
        return [p for p, (ppid, _e, _m) in self.procs.items() if ppid == pid]

    def _start_of(self, pid):
        return pid if pid in self.procs else None

    def _signal(self, pid, start, sig):
        if self._start_of(pid) != start:
            return False
        self.acts.append(("stop" if sig == signal.SIGSTOP else "kill", pid))
        if sig == signal.SIGKILL:
            self.procs.pop(pid, None)
        return True

    def _sentence(self, pid, start, text, owner=()):
        self.acts.append(("sentence", pid))

    def killed(self) -> set[int]:
        return {pid for act, pid in self.acts if act == "kill"}

    def tick(self, seconds: float, *, avail=None) -> None:
        """Advance the clock and run one pass, with the walk due."""
        self.clock.t += seconds
        if avail is not None:
            self.avail = avail
        self._last_walk = float("-inf")
        self.run_pass()


def _engine_age_patch(monkeypatch, ages: dict[int, float]):
    """A unit's age by its root's pid (start time = pid in the planted tables)."""
    monkeypatch.setattr(mg, "_age_s", lambda start: ages.get(start, 3_600.0))


def _session(g: Planted, root: int, engine_exe: str = "claude.exe", wrappers: tuple[str, ...] = ()) -> int:
    """A session's tree: its wrappers from ``root`` down, then the engine.
    Returns the engine's pid."""
    parent, pid = SAT, root
    for exe in wrappers:
        g.procs[pid] = (parent, exe, 2.0)
        parent, pid = pid, pid + 1
    g.procs[pid] = (parent, engine_exe, 200.0)
    return pid


def _settle(g: Planted) -> None:
    """One settled walk: every part's baseline is its size now."""
    g.tick(0.5, avail=10_000)


# -- the numbers --------------------------------------------------------------

def test_the_numbers_follow_the_machine_and_the_platforms_policy():
    g = Planted([])
    g.policy = {}
    n = g.numbers(TOTAL)
    assert (n.reserve, n.guard, n.band, n.min_stop) == (1_024, 1_024, 2_048, 512)
    g.compute = False
    assert g.numbers(8_192).reserve == 1_024 and g.numbers(65_536).reserve == 4_096
    g.set_policy({"share": 0.10, "floor_mb": 1_024, "cap_mb": 0})   # a platform that sends no cap
    assert g.numbers(65_536).reserve == 6_553
    g.policy = {}
    g.compute = True
    g.set_policy({"share": 0.05, "floor_mb": 100_000_000, "min_stop_mb": 256, "credit_mb": "x", "share2": 1})
    # A floor set to close admission: the guard keeps at most half the machine.
    n = g.numbers(4_096)
    assert n.guard == 2_048 and n.min_stop == 256 and n.credit == 0
    assert n.ceiling == 2_048   # a session's room follows the guard's reserve, never the floor as sent
    g.set_policy({"share": 9})   # out of range: dropped, the default stands
    assert g.policy == {}


def test_the_local_variable_lowers_the_ceiling(monkeypatch):
    monkeypatch.setenv(mg.LOCAL_CEILING_ENV, "1536")
    g = Planted([])
    assert g.numbers(TOTAL).ceiling == 1_536


# -- rule 2: the runaway, and pressure nobody caused --------------------------

def test_a_runaway_command_is_stopped_before_the_reserve_and_nothing_else(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude"), Unit(200, "s-b", "session", "claude")])
    a = _session(g, 100)
    b = _session(g, 200)
    g.procs[110] = (a, "bash", 50.0)      # a resting command in A
    g.procs[210] = (b, "bash", 900.0)     # B's large but steady build
    _settle(g)
    g.procs[111] = (a, "python3", 2_000.0)  # A's runaway, born after the settled walk
    g.tick(0.5, avail=900)
    assert g.killed() == {111}
    assert g.acts[:3] == [("stop", 111), ("sentence", 111), ("kill", 111)]
    assert not g.ended
    assert g.sent and g.sent[-1]["what"] == "command" and g.sent[-1]["session_id"] == "s-a"


def test_the_machine_low_and_no_unit_grown_stops_nothing_on_compute_and_on_a_computer(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    for compute in (True, False):
        g = Planted([Unit(100, "s-a", "session", "claude"), Unit(200, "s-b", "session", "claude")],
                    compute=compute)
        a, b = _session(g, 100), _session(g, 200)
        g.procs[110] = (a, "node", 600.0)
        g.procs[210] = (b, "node", 3_000.0)
        _settle(g)
        # A filler outside the satellite takes the machine under its reserve.
        g.tick(0.5, avail=300)
        g.tick(0.5, avail=200)
        assert g.acts == [] and not g.ended, compute


def test_a_session_split_into_many_small_commands_is_the_one_stopped(monkeypatch):
    """Mutation: min_stop per command would leave A and take B's one build."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude"), Unit(200, "s-b", "session", "claude")])
    a, b = _session(g, 100), _session(g, 200)
    _settle(g)
    for i in range(10):
        g.procs[120 + i] = (a, "python3", 100.0 + i)   # ten workers of about 100 MB
    g.procs[210] = (b, "bash", 600.0)                  # B's one 600 MB command
    g.tick(0.5, avail=800)
    assert g.killed() == {129} and 210 in g.procs


def test_a_slow_leak_of_30_mb_a_second_is_stopped(monkeypatch):
    """Mutation: growth over a 5 s window (150 MB) would never reach min_stop."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    g.procs[110] = (a, "python3", 10.0)
    _settle(g)
    avail = 1_900.0   # inside the band, falling toward the reserve
    for _ in range(60):
        g.procs[110] = (a, "python3", g.procs[110][2] + 30.0)
        avail -= 30.0
        g.tick(1.0, avail=avail)
        if g.killed():
            break
    assert g.killed() == {110}


def test_a_session_admitted_on_a_low_host_keeps_its_engine(monkeypatch):
    """Mutation: counting a unit's start would end the session."""
    ages = {100: 10.0}
    _engine_age_patch(monkeypatch, ages)
    g = Planted([Unit(100, "s-a", "session", "claude")])
    engine = _session(g, 100, wrappers=("sh",))
    g.tick(0.5, avail=1_500)                       # the host is low before it starts
    g.procs[engine] = (100, "claude.exe", 1_100.0)  # its start, while the host keeps falling
    g.tick(0.5, avail=800)
    g.tick(0.5, avail=700)
    ages[100] = 120.0                              # its start is over: its size is its baseline
    g.tick(0.5, avail=650)
    g.tick(0.5, avail=600)
    assert g.acts == [] and not g.ended


def test_a_stop_does_not_cascade_while_its_memory_is_given_back(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    _settle(g)
    g.procs[110] = (a, "python3", 2_000.0)
    g.procs[111] = (a, "python3", 1_500.0)
    g.tick(0.1, avail=500)
    assert g.killed() == {110}
    # The reading still shows the stopped command's memory for a moment.
    g.procs[110] = (a, "python3", 2_000.0)
    g.tick(0.1, avail=500)
    assert g.killed() == {110}


def test_swap_filling_with_a_runaway_is_stopped_above_the_reserve(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")], swap=2_000)
    a = _session(g, 100)
    _settle(g)
    g.procs[110] = (a, "python3", 1_200.0)
    g.swap = 1_600
    g.tick(1.0, avail=1_900)   # inside the band (2,048), above the reserve
    assert g.killed() == {110}


def test_swap_falling_while_the_machine_has_room_stops_nothing(monkeypatch):
    """The review's probe inverted: 9 GB free, a command grew 600 MB, swap fell
    300 MB in a second (a container at its own cap, a slice with MemoryHigh):
    nothing is stopped, so a test suite's start is left alone (mutation: the
    swap rule at any reading)."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")], swap=2_000)
    a = _session(g, 100)
    _settle(g)
    g.procs[110] = (a, "python3", 600.0)
    g.swap = 1_700
    g.tick(1.0, avail=9_000)
    assert g.acts == []


# -- rule 1: the ceiling -------------------------------------------------------

def test_a_ceiling_stop_never_touches_another_session(monkeypatch):
    """Mutation: attributing by user instead of by tree would take B's."""
    monkeypatch.setenv(mg.LOCAL_CEILING_ENV, "1500")
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude"), Unit(200, "s-b", "session", "claude")])
    a, b = _session(g, 100), _session(g, 200)
    _settle(g)
    g.procs[110] = (a, "python3", 1_400.0)
    g.procs[210] = (b, "node", 1_200.0)   # larger than any single process of A's but under the ceiling
    g.tick(0.5, avail=9_000)
    assert g.killed() == {110}


def test_a_floor_set_to_close_admission_stops_no_running_session(monkeypatch):
    """Mutation: a ceiling worked out from the reserve as sent is 0 under such a
    floor, and every session is past it the moment it starts."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    g.set_policy({"share": 0.05, "floor_mb": 100_000_000})
    _session(g, 100)
    g.tick(0.5, avail=15_000)
    assert g.acts == [] and g.ended == []


def test_a_session_whose_engine_is_its_largest_part_is_ended(monkeypatch):
    monkeypatch.setenv(mg.LOCAL_CEILING_ENV, "150")
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    _session(g, 100, wrappers=("sh",))
    g.tick(0.5, avail=9_000)
    assert g.ended == ["s-a"] and g.killed() == {100, 101}
    assert ("sentence", 101) not in g.acts


# -- the parts: wrappers, the engine, the commands, the folders ---------------

def test_a_compute_session_through_its_wrappers_stops_only_the_command(monkeypatch):
    """Mutation: dropping the wrapper rule would take bubblewrap or the engine."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    engine = _session(g, 100, wrappers=("pasta", "bwrap", "bwrap"))
    inner_bwrap = engine - 1
    g.procs[300] = (inner_bwrap, "python3", 1.0)   # an orphan bubblewrap adopted (younger than the engine)
    _settle(g)
    g.procs[310] = (engine, "bash", 1.0)
    g.procs[311] = (310, "python3", 1_800.0)       # the command's own child grows
    g.tick(0.5, avail=700)
    assert g.killed() == {310, 311}
    assert not ({100, 101, 102, engine} & g.killed()) and SAT not in g.killed()
    assert ("sentence", 310) in g.acts


def test_the_orphan_under_bubblewrap_is_a_command_of_its_session(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    engine = _session(g, 100, wrappers=("bwrap", "bwrap"))
    _settle(g)
    g.procs[300] = (engine - 1, "python3", 2_000.0)  # a command that left the engine's tree
    g.tick(0.5, avail=700)
    assert g.killed() == {300} and engine in g.procs


def test_codexs_launcher_and_a_command_named_like_the_engine(monkeypatch):
    """The engine is the native binary under node; a command under the native
    engine named like it stays a command (mutation: find the engine by its name)."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "codex")])
    g.procs[100] = (SAT, "node", 60.0)                 # codex.js
    g.procs[101] = (100, "codex", 150.0)                # the native engine
    _settle(g)
    g.procs[102] = (101, "codex", 1_900.0)              # a command renamed like the engine
    g.tick(0.5, avail=600)
    assert g.killed() == {102} and not g.ended


def test_a_sandbox_holding_its_growth_in_memory_folders_ends_its_session(monkeypatch):
    """Mutation: sizes from processes alone would stop nothing."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    engine = _session(g, 100, wrappers=("bwrap",))
    _settle(g)
    g.folder_mb[engine] = 2_500.0
    g.tick(0.5, avail=700)
    assert g.ended == ["s-a"] and {100, engine} <= g.killed()


def test_the_satellites_own_process_is_never_a_unit():
    g = Planted([])
    g.procs[SAT] = (0, "python3", 9_000.0)
    assert g.walk() == []


def test_the_satellites_own_work_is_stopped_with_the_builds_sentence(monkeypatch, caplog):
    _engine_age_patch(monkeypatch, {400: 5.0})   # a new build: in its start, past its allowance
    g = Planted([])
    g.procs[SAT] = (0, "python3", 100.0)
    _settle(g)
    g.procs[400] = (SAT, "uv", 1_800.0)   # a tool's build the satellite runs itself
    g.tick(0.5, avail=700)
    assert g.killed() == {400} and g.sent[-1]["what"] == "own"
    assert "memory: stopped its own work, a build or a sync (1800 MB" in caplog.text


def test_a_reused_pid_is_never_signalled(monkeypatch):
    """The real ``Guard._signal`` over the planted start: a pid now naming
    another start is left alone (mutation: dropping the start-time check
    would signal the new process)."""
    g = Planted([])
    g.procs[500] = (SAT, "bash", 10.0)
    sent = []
    monkeypatch.setattr(mg.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert not Guard._signal(g, 500, 499, signal.SIGKILL)
    assert sent == []
    assert Guard._signal(g, 500, 500, signal.SIGKILL) and sent == [(500, signal.SIGKILL)]


def test_with_no_platform_policy_nothing_is_sent(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    g.policy = {}
    a = _session(g, 100)
    _settle(g)
    g.procs[110] = (a, "python3", 2_000.0)
    g.tick(0.5, avail=700)
    assert g.killed() == {110} and g.sent == []


# -- the start check, the state, the frames --------------------------------------

def test_the_last_start_check_refuses_under_the_reserve_and_names_the_sentence():
    """A computer's person can close something on it, a compute machine's
    cannot: the server's sentence says to wait."""
    g = Planted([], avail=900, compute=False)   # a 20 GB computer: reserve 2,048
    assert g.start_refusal() == mg.START_REFUSAL
    g.avail = 2_100
    assert g.start_refusal() is None
    g = Planted([], avail=900)
    assert g.start_refusal() == mg.COMPUTE_REFUSAL == "The server is low on memory. Try again in a few minutes."


def test_a_stalled_guard_says_so():
    g = Planted([])
    g.last_pass = g.clock.t - mg.STALL_S - 1
    assert g.state() == "stalled"
    g.last_pass = g.clock.t
    assert g.state() is True


def test_the_cadence_is_faster_inside_the_band():
    g = Planted([], avail=10_000)
    g.run_pass()
    assert g.period == mg.READ_S
    g.avail = 1_500
    g.clock.t += 0.5
    g.run_pass()
    assert g.period == mg.READ_BAND_S


def test_a_heartbeat_carries_memory_only_with_the_guard_or_on_compute(monkeypatch):
    monkeypatch.setattr(mg, "_thread", None)
    assert mg.frame_fields(False) == {}
    assert "memory" in mg.frame_fields(True)


# -- a real child ------------------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_a_real_command_is_stopped_with_its_sentence_in_its_output(monkeypatch, tmp_path):
    """A real child holding 64 MB, under a 16 MB ceiling: it is stopped before
    it is killed, and its standard error (a file, as Claude Code gives a
    command) holds the sentence (mutation: kill first)."""
    monkeypatch.setenv(mg.LOCAL_CEILING_ENV, "16")
    out, rc = tmp_path / "command.output", tmp_path / "rc"
    alloc = ("import resource, time; resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20)); "
             "b = bytearray(64 << 20); [b.__setitem__(i, 1) for i in range(0, len(b), 4096)]; "
             "print('held', flush=True); time.sleep(60)")
    # A small "engine" whose one command is the allocator, its output in a file.
    engine = (f"import subprocess, sys; fh = open({str(out)!r}, 'w'); "
              f"p = subprocess.Popen([sys.executable, '-c', {alloc!r}], stdout=fh, stderr=fh); "
              f"p.wait(); open({str(rc)!r}, 'w').write(str(p.returncode))")
    child = subprocess.Popen([sys.executable, "-c", engine])
    try:
        deadline = time.monotonic() + 20
        while not (out.exists() and "held" in out.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        guard = Guard(lambda: [Unit(child.pid, "s-real", "session")], compute=False)
        guard.run_pass()
        assert child.wait(timeout=10) == 0
        assert rc.read_text() == str(-signal.SIGKILL)
        assert mg.COMMAND_SENTENCE in out.read_text()
        assert guard.stops == 1
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait()


def test_a_slow_leak_of_3_mb_a_second_is_stopped_at_the_reserve(monkeypatch):
    """Under ``reserve + 2 × min_stop`` the baselines never move (mutation: a
    small net fall settles anywhere, and the leak resets its own baseline)."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    g.procs[110] = (a, "python3", 10.0)
    avail = 2_500.0
    stopped_at = None
    for _ in range(700):
        g.procs[110] = (a, "python3", g.procs[110][2] + 3.0)
        avail -= 3.0
        g.tick(1.0, avail=avail)
        if g.killed():
            stopped_at = avail
            break
    assert g.killed() == {110} and 900 <= stopped_at <= 1_024


def test_a_leak_slower_than_the_window_is_still_stopped_at_the_reserve(monkeypatch):
    """Half a MB a second, from 2,500 MB free: its baselines age out three
    times on the way down, and at the reserve it is still the cause, since it
    took what fell since its last baseline (``accounts``)."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    g.procs[110] = (a, "python3", 10.0)
    avail = 2_500.0
    stopped_at = None
    for _ in range(4_000):
        g.procs[110] = (a, "python3", g.procs[110][2] + 0.5)
        avail -= 0.5
        g.tick(1.0, avail=avail)
        if g.killed():
            stopped_at = avail
            break
    assert g.killed() == {110} and 1_024 - 2 * mg.ACCOUNTS_MB <= stopped_at <= 1_024


def test_a_resting_chat_is_not_ended_for_what_its_engine_grew_over_an_hour(monkeypatch):
    """The review's probe: 1,500 MB free (under ``guard + 2 x min_stop``) for an
    hour while a resting chat's engine grows 10 MB a minute, then a dip the
    sessions did not cause. Growth older than the window is excused, so nothing
    is stopped (mutation: baselines that never age)."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    _settle(g)
    for _ in range(60):
        g.procs[a] = (SAT, "claude.exe", g.procs[a][2] + 10.0)
        g.tick(60.0, avail=1_500)
    g.tick(1.0, avail=900)
    assert g.acts == [] and g.ended == []


def test_a_slow_filler_outside_beside_a_resting_chat_stops_nothing(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    g.procs[110] = (a, "node", 300.0)
    avail = 2_500.0
    for _ in range(700):
        avail -= 3.0
        g.tick(1.0, avail=avail)
    assert g.acts == []


def test_a_command_started_just_above_the_reserve_is_stopped_at_the_reserve(monkeypatch):
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    g.tick(1.0, avail=1_124)              # resting just above the reserve, under the line
    g.tick(1.0, avail=1_124)
    g.procs[110] = (a, "python3", 1.0)
    g.tick(0.1, avail=1_123)
    g.procs[110] = (a, "python3", 120.0)  # it grows fast across the reserve
    g.tick(0.1, avail=1_004)
    assert g.killed() == {110}


def test_a_unit_running_away_in_its_start_is_stopped_past_its_allowance(monkeypatch):
    ages = {100: 20.0}
    _engine_age_patch(monkeypatch, ages)
    g = Planted([Unit(100, "s-a", "session", "claude")])
    engine = _session(g, 100)
    _settle(g)
    g.procs[110] = (engine, "python3", 600.0)    # a tool starting: within the allowance
    g.tick(0.1, avail=900)
    assert g.acts == []
    g.procs[110] = (engine, "python3", 1_500.0)  # past what admission reserved for it
    g.tick(0.1, avail=600)
    assert g.killed() == {110}


def test_a_fresh_sessions_tools_beside_a_filler_are_not_stopped(monkeypatch):
    ages = {100: 20.0}
    _engine_age_patch(monkeypatch, ages)
    g = Planted([Unit(100, "s-a", "session", "claude")])
    engine = _session(g, 100)
    g.tick(0.5, avail=1_500)
    g.procs[110] = (engine, "node", 130.0)   # two tools that start with it
    g.tick(0.5, avail=800)                    # a filler outside takes the rest
    g.tick(0.5, avail=700)
    assert g.acts == []


def test_a_walk_inside_the_launchers_exec_stops_nothing(monkeypatch):
    """The launcher is a script: its program is python until it execs pasta."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    g.procs[100] = (SAT, "python3.13", 9.0)
    g.names[100] = "oto-sandbox-net"
    _settle(g)
    g.tick(0.1, avail=600)
    assert g.acts == []
    walked = g.walk()
    assert [p.kind for w in walked for p in w.parts] == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_only_memory_filesystems_count_as_a_sandboxs_folders():
    """This test's own process: its ``/tmp`` is a disk folder here, never counted
    (mutation: statvfs of ``/tmp`` whatever its filesystem)."""
    g = Guard(lambda: [], compute=True)
    used, ids = g._folders(os.getpid())
    with open(f"/proc/{os.getpid()}/mountinfo") as fh:
        tmpfs = [ln.split()[4] for ln in fh if " - tmpfs " in ln and ln.split()[4] in mg.MEMORY_FOLDERS]
    assert len(ids) == len(tmpfs)
    if not tmpfs:
        assert used == 0.0


def test_with_no_child_of_the_satellite_there_is_no_walk(monkeypatch):
    g = Planted([])
    walks = []
    monkeypatch.setattr(g, "walk", lambda: walks.append(1) or [])
    g.tick(2.5, avail=500)
    assert walks == []
    g.procs[400] = (SAT, "bash", 1.0)
    g.tick(2.5, avail=500)
    assert walks == [1]


def test_a_runaway_that_starts_on_a_machine_with_room_counts_from_its_start(monkeypatch):
    """A fresh session (its start allowance) whose command runs away while the
    machine still has room is stopped at the reserve. Mutation: settling above
    the band while the reading falls fast takes the first 550 MB into its
    baseline, and the stop comes about 350 MB under the reserve."""
    _engine_age_patch(monkeypatch, {100: 20.0})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    g.tick(0.5, avail=2_600)
    g.procs[110] = (a, "python3", 1.0)
    avail, size = 2_600.0, 1.0
    for _ in range(200):                     # 160 MB a second
        size += 16.0
        avail -= 16.0
        g.procs[110] = (a, "python3", size)
        g.tick(0.1, avail=avail)
        if g.killed():
            break
    assert g.killed() == {110} and avail >= 950


def test_a_leak_that_began_in_the_sessions_start_is_stopped_at_the_reserve(monkeypatch):
    """The walk's slow leak: a fresh session whose command leaks 30 MB a second
    from its first seconds is stopped at the reserve after its start ends.
    Mutation: rebaselining every part at the end of the start erases the leak's
    first 90 s, and the stop comes about 300 MB under the reserve."""
    ages = {100: 5.0}
    _engine_age_patch(monkeypatch, ages)
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    g.tick(0.5, avail=4_500)
    g.procs[110] = (a, "python3", 1.0)
    avail, size = 4_500.0, 1.0
    for _ in range(1_600):                   # 3 MB every 0.1 s
        size += 3.0
        avail -= 3.0
        ages[100] += 0.1
        g.procs[110] = (a, "python3", size)
        g.tick(0.1, avail=avail)
        if g.killed():
            break
    assert g.killed() == {110} and ages[100] > 90 and avail >= 950


def test_pasta_with_its_program_hidden_is_still_a_wrapper(monkeypatch):
    """The compute unit's real tree: the kernel shows pasta's name but not its
    program file. Mutation: wrappers by their program file alone make pasta
    the engine and the whole session one command."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    engine = _session(g, 100, wrappers=("", "bwrap", "bwrap"))
    g.names[100] = "pasta"
    _settle(g)
    g.procs[310] = (engine, "bash", 1.0)
    g.procs[311] = (310, "python3", 1_800.0)
    g.tick(0.5, avail=700)
    assert g.killed() == {310, 311} and engine in g.procs and not g.ended


def _stderr_ready(child: subprocess.Popen, name: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with __import__("contextlib").suppress(OSError):
            if name in os.readlink(f"/proc/{child.pid}/fd/2"):
                return
        time.sleep(0.02)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_the_sentence_never_goes_where_the_commands_own_descriptor_cannot_write(tmp_path):
    target = tmp_path / "read-only.txt"
    target.write_text("original\n")
    child = subprocess.Popen(["/bin/sh", "-c", f"exec 2<{target}; sleep 30"])
    try:
        _stderr_ready(child, "read-only.txt")
        Guard(lambda: [], compute=False)._sentence(child.pid, _start(child), mg.COMMAND_SENTENCE)
        assert target.read_text() == "original\n"
    finally:
        child.kill()
        child.wait()


def _start(child: subprocess.Popen) -> int:
    return mg._stat(child.pid)[1]


def _written_by_the_guard(script: str, name: str, *, start_off: int = 0, **popen) -> None:
    """A real child running ``script`` whose standard error is ``name``,
    stopped as the guard stops it, then given the sentence."""
    child = subprocess.Popen(["/bin/sh", "-c", script], **popen)
    try:
        _stderr_ready(child, name)
        os.kill(child.pid, signal.SIGSTOP)
        guard = Guard(lambda: [], compute=False)
        guard._sentence(child.pid, _start(child) + start_off, mg.COMMAND_SENTENCE)
        guard._helper.join(5)
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_the_sentence_reaches_what_the_commands_own_descriptor_writes_to(tmp_path):
    """Through the command's own descriptor: a file it writes, and a socket,
    which a reopen of ``/proc/<pid>/fd/2`` cannot reach."""
    out = tmp_path / "out.txt"
    _written_by_the_guard(f"exec 2>>{out}; sleep 30", "out.txt")
    assert mg.COMMAND_SENTENCE in out.read_text()
    import socket
    mine, its = socket.socketpair()
    with mine, its:
        _written_by_the_guard("sleep 30", "socket", stderr=its.fileno())
        mine.settimeout(2)
        assert mg.COMMAND_SENTENCE.encode() in mine.recv(200)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_the_sentence_never_goes_to_another_process_of_a_reused_pid(tmp_path):
    """The pid named with another start: nothing is written (mutation: no
    start check once the pidfd holds the process)."""
    out = tmp_path / "out.txt"
    _written_by_the_guard(f"exec 2>>{out}; sleep 30", "out.txt", start_off=1)
    assert not out.exists() or out.read_text() == ""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_a_refused_copy_writes_nothing_and_a_folder_takes_nothing(tmp_path, monkeypatch):
    """When the kernel refuses the copy (no ptrace right, an old kernel) the
    guard has no other way in; a descriptor on a folder takes no write."""
    out = tmp_path / "out.txt"
    def refused(pidfd, fd):
        raise PermissionError(1, "refused")
    monkeypatch.setattr(mg, "_pidfd_getfd", refused)
    _written_by_the_guard(f"exec 2>>{out}; sleep 30", "out.txt")
    assert not out.exists() or out.read_text() == ""
    monkeypatch.undo()
    _written_by_the_guard(f"exec 2<{tmp_path}; sleep 30", str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]


# -- the second review's tests: the stop's wait, the copy, ageing, one unit's hung write --


def _full_pipe():
    """A pipe whose buffer is full: a write to it blocks until someone reads."""
    import fcntl
    r, w = os.pipe()
    fcntl.fcntl(w, fcntl.F_SETPIPE_SZ, 4096)
    filler = os.open(f"/proc/self/fd/{w}", os.O_WRONLY | os.O_NONBLOCK)
    try:
        while True:
            os.write(filler, b"x" * 4096)
    except BlockingIOError:
        pass
    os.close(filler)
    return r, w


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_a_write_nobody_reads_never_holds_the_stop():
    """A command whose standard error is a full pipe: the stop goes on after
    ``SENTENCE_WAIT_S`` (mutation: a join with no limit holds the guard's thread)."""
    r, w = _full_pipe()
    child = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], stderr=w)
    os.close(w)
    guard = Guard(lambda: [], compute=False)
    done = threading.Event()
    try:
        os.kill(child.pid, signal.SIGSTOP)
        t = threading.Thread(target=lambda: (guard._sentence(child.pid, _start(child), mg.COMMAND_SENTENCE),
                                             done.set()), daemon=True)
        t.start()
        assert done.wait(2.0)
    finally:
        child.kill()
        child.wait()
        os.read(r, 8192)
        os.close(r)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_the_copy_is_closed_after_the_write(tmp_path):
    """The satellite keeps no descriptor of a stopped command (mutation: the copy never closed)."""
    out = tmp_path / "out.txt"
    child = subprocess.Popen(["/bin/sh", "-c", f"exec 2>>{out}; sleep 30"])
    try:
        _stderr_ready(child, "out.txt")
        os.kill(child.pid, signal.SIGSTOP)
        before = set(os.listdir("/proc/self/fd"))
        guard = Guard(lambda: [], compute=False)
        guard._sentence(child.pid, _start(child), mg.COMMAND_SENTENCE)
        guard._helper.join(5)
        assert mg.COMMAND_SENTENCE in out.read_text()
        assert set(os.listdir("/proc/self/fd")) == before
    finally:
        child.kill()
        child.wait()


def test_under_the_reserve_growth_never_ages(monkeypatch):
    """A compute session that writes 250 MB into its sandbox's /tmp every 16
    quiet minutes on a 4 GB machine (min_stop 256): under the reserve its
    growth counts with no age limit, so it is ended min_stop under it
    (mutation: baselines that age under the reserve too never end it)."""
    _engine_age_patch(monkeypatch, {})
    folders = {100: 0.0}
    g = Planted([Unit(100, "s-a", "session", "claude")], folders=folders)
    g.total = 4_096
    _session(g, 100)
    g.tick(0.5, avail=3_000)
    avail = 1_500.0
    for _step in range(6):
        for _ in range(10):
            folders[100] += 25.0
            avail -= 25.0
            g.tick(1.0, avail=avail)
        for _ in range(16):
            g.tick(60.0, avail=avail)
        if g.ended:
            break
    assert g.ended == ["s-a"] and avail >= 250


def test_an_old_units_engine_leak_is_ended_at_the_reserve(monkeypatch):
    """A unit older than the window whose engine leaks 2 MB a second inside the
    band: its baselines age at most once per window, so its growth reaches
    min_stop by the reserve (mutations: a renewal at every quiet walk, a
    renewal whatever the net fall)."""
    _engine_age_patch(monkeypatch, {})
    g = Planted([Unit(100, "s-a", "session", "claude")])
    a = _session(g, 100)
    _settle(g)
    for _ in range(20):                      # an hour at rest, well inside the band
        g.tick(180.0, avail=1_900)
    avail = 1_900.0
    for _ in range(600):
        g.procs[a] = (SAT, "claude.exe", g.procs[a][2] + 2.0)
        avail -= 2.0
        g.tick(1.0, avail=avail)
        if g.ended:
            break
    assert g.ended == ["s-a"] and avail >= 1_024 - 64


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_a_hung_write_of_one_unit_never_takes_anothers_sentence(tmp_path):
    """Unit A's command writes to a full pipe another process holds and never
    reads; unit B's command, stopped next, still gets its sentence (mutation:
    one helper for the whole machine)."""
    r, w = _full_pipe()
    a = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], stderr=w)
    os.close(w)
    out = tmp_path / "other.txt"
    b = subprocess.Popen(["/bin/sh", "-c", f"exec 2>>{out}; sleep 30"])
    guard = Guard(lambda: [], compute=True)
    try:
        os.kill(a.pid, signal.SIGSTOP)
        guard._sentence(a.pid, _start(a), mg.COMMAND_SENTENCE, owner=(a.pid, _start(a)))
        hung = guard._helper
        assert hung.is_alive()
        _stderr_ready(b, "other.txt")
        os.kill(b.pid, signal.SIGSTOP)
        guard._sentence(b.pid, _start(b), mg.COMMAND_SENTENCE, owner=(b.pid, _start(b)))
        guard._helper.join(5)
        assert mg.COMMAND_SENTENCE in out.read_text() and hung.is_alive()
    finally:
        for child in (a, b):
            child.kill()
            child.wait()
        os.read(r, 8192)
        os.close(r)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the guard runs on Linux")
def test_only_a_file_a_pipe_a_socket_or_a_terminal_takes_a_sentence(tmp_path):
    """The writer is the satellite: a file of the kernel's own (procfs, sysfs,
    cgroup), another device and an eventfd take nothing, whatever the
    descriptor's access (mutation: procfs alone, or the type alone)."""
    import pty
    import socket
    checks = []
    plain = tmp_path / "plain.txt"
    plain.write_text("")
    for path, flags, want in ((str(plain), os.O_WRONLY, True), ("/proc/self/comm", os.O_WRONLY, False),
                              ("/sys/kernel/mm/transparent_hugepage/enabled", os.O_RDONLY, False),
                              ("/sys/fs/cgroup/cgroup.controllers", os.O_RDONLY, False),
                              ("/dev/null", os.O_WRONLY, False)):
        if not os.path.exists(path):
            continue
        fd = os.open(path, flags)
        try:
            checks.append((path, mg._takes_a_sentence(fd), want))
        finally:
            os.close(fd)
    r, w = os.pipe()
    mine, its = socket.socketpair()
    leader, follower = pty.openpty()
    event = os.eventfd(0)
    try:
        checks += [("pipe", mg._takes_a_sentence(w), True), ("socket", mg._takes_a_sentence(its.fileno()), True),
                   ("terminal", mg._takes_a_sentence(follower), True), ("eventfd", mg._takes_a_sentence(event), False)]
    finally:
        for fd in (r, w, leader, follower, event):
            os.close(fd)
        mine.close()
        its.close()
    assert [c for c in checks if c[1] != c[2]] == [], checks
    assert {c[0] for c in checks} >= {str(plain), "/proc/self/comm", "pipe", "socket", "terminal", "eventfd"}
