"""Wall-clock bounds that hold on every host.

A test that asserts an operation ends within some seconds guards an
algorithm, not a machine: the bound was written on the dev box, and a shared
CI runner that packs eight workers onto four cores takes two to three times
as long for the same work. ``bound(seconds)`` scales the written bound by how
much slower this process runs right now, measured by a fixed reference run at
the moment of the comparison (the run never touches the measured interval:
the bound is read after it). The factor is never below one, so a fast host
still meets the written bound, and never above ten, so a quadratic blow-up
(minutes at the sizes these tests use) still fails::

    from tests._timing import bound

    started = time.monotonic()
    lint_tree(root, walked, declared)
    assert time.monotonic() - started < bound(5)

``cpu_bound(seconds)`` is the same bound on the CPU time of the calling
thread, read with ``thread_cpu()``, for work that runs on one thread and
holds the interpreter lock all through (a regex pass, a parse): what it
guards is how long that work freezes the process, and a runner that packs
eight workers onto two cores stretches its wall time many times over while
its CPU time stays the work's own::

    from tests._timing import cpu_bound, thread_cpu

    started = thread_cpu()
    lint_tree(root, walked, declared)
    assert thread_cpu() - started < cpu_bound(5)

Work that crosses threads, waits on I/O or sleeps keeps ``bound()`` and the
wall clock: its CPU time says nothing about how long it took.

``cpu_share(call)`` runs a call and says how much of its wall time this
process spent on the CPU, for a test whose premise is that the process ran
all through the call (a loaded host may not grant that; the test skips).
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable

# json.dumps(list(range(300_000))): 0.031 to 0.035 s on the dev box.
_REFERENCE_S = 0.032
_REFERENCE_N = 300_000
_REFERENCE_RUNS = 3
_MAX_FACTOR = 10.0


def _reference_run() -> float:
    started = time.perf_counter()
    json.dumps(list(range(_REFERENCE_N)))
    return time.perf_counter() - started


def host_factor() -> float:
    """How many times slower than the dev box this process runs right now:
    the median of three reference runs over the dev box's time, between one
    and ten."""
    measured = statistics.median(_reference_run() for _ in range(_REFERENCE_RUNS))
    return min(max(1.0, measured / _REFERENCE_S), _MAX_FACTOR)


def bound(seconds: float) -> float:
    """``seconds`` as written on the dev box, scaled to this host right now."""
    return seconds * host_factor()


def thread_cpu() -> float:
    """The CPU seconds the calling thread has used so far."""
    return time.thread_time()


def _reference_cpu_run() -> float:
    started = time.thread_time()
    json.dumps(list(range(_REFERENCE_N)))
    return time.thread_time() - started


def cpu_factor() -> float:
    """``host_factor`` on the thread's CPU clock: how many times more CPU the
    reference run costs here than on the dev box (a slower core, a sibling
    hyperthread at work), between one and ten. Waiting for a core is not in
    it: that is the point."""
    measured = statistics.median(_reference_cpu_run() for _ in range(_REFERENCE_RUNS))
    return min(max(1.0, measured / _REFERENCE_S), _MAX_FACTOR)


def cpu_bound(seconds: float) -> float:
    """``seconds`` of one thread's CPU as written on the dev box, scaled to
    this host's cores right now. Compare with a ``thread_cpu()`` interval."""
    return seconds * cpu_factor()


def cpu_share(call: Callable[[], object]) -> tuple[float, float]:
    """Run ``call``; return its wall seconds and the share of them this
    process spent on the CPU (1.0 when nothing else ran on its cores)."""
    wall0, cpu0 = time.perf_counter(), time.process_time()
    call()
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    return wall, (cpu / wall if wall > 0 else 1.0)
