"""Container-aware host memory detection for concurrency admission.

Provides the two memory signals the admission gates in ``core.concurrency`` need:

- ``detect_memory_limit_bytes()`` — the memory budget available to this proxy
  *container* (cgroup v2 ``memory.max`` → v1 ``memory.limit_in_bytes`` →
  ``/proc/meminfo MemTotal``). Feeds GATE 1's reservation budget + the admin
  gauge's "total".
- ``live_available_bytes()`` — the *live* free RAM right now, reclaimable-aware,
  read per-admit. Feeds GATE 2's OOM veto.

Why not ``psutil`` / ``os.cpu_count()``? Inside a container both report the HOST's
totals, not the cgroup quota. We read the cgroup files directly and fall back to
``/proc`` only when genuinely uncapped.

Capping policy (the ONE story — keep docker-compose.yml + CONCURRENCY.md in
agreement): the T2 proxy ships memory-UNCAPPED by default, DELIBERATELY — Gate 2
reads host ``MemAvailable`` there, so admission is bounded by the real machine
(sidecars included) and sessions may lean on swap instead of hard-failing.
Setting ``OTODOCK_PROXY_MEM_LIMIT`` is the opt-in HARD-containment mode (the
kernel OOM-kills the proxy tree before it can starve the host); since
2026-07-06 the capped Gate-2 read is ``min(cgroup headroom, host MemAvailable)``
so capping no longer trades away the sidecar-leak veto. The one-time
uncapped-in-Docker log is informational (know which mode you run), not an error.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("otodock.host_resources")

_MIB = 1024 * 1024
_warned_uncapped = False  # one-time uncapped-in-Docker warning latch


# ---------------------------------------------------------------------------
# Low-level readers
# ---------------------------------------------------------------------------

def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _proc_mem_total_bytes() -> int | None:
    """Host RAM from ``/proc/meminfo`` (``MemTotal``, kB → bytes). ``None`` if unreadable."""
    return _proc_meminfo_field("MemTotal:")


def _proc_meminfo_available_bytes() -> int | None:
    """Host available RAM from ``/proc/meminfo`` (``MemAvailable``, kB → bytes).

    ``MemAvailable`` is the kernel's own estimate of allocatable memory without
    swapping — it already accounts for reclaimable page cache, so on bare-metal /
    uncapped hosts it is exactly the GATE-2 signal we want.
    """
    return _proc_meminfo_field("MemAvailable:")


def swap_free_bytes() -> int:
    """Host free swap from ``/proc/meminfo`` (``SwapFree``, bytes; 0 if none/unreadable).

    Consumed by the admission gate's SWAP CREDIT — MemAvailable counts zero
    swap, so without this a small box with healthy swap denies sessions it
    could comfortably run by letting the kernel page out cold heap."""
    return _proc_meminfo_field("SwapFree:") or 0


def _proc_meminfo_field(prefix: str) -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(prefix):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_memory_stat(path: str) -> dict[str, int]:
    """Parse a cgroup ``memory.stat`` file into ``{key: int}``. ``{}`` on failure."""
    out: dict[str, int] = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    try:
                        out[parts[0]] = int(parts[1])
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _running_in_docker() -> bool:
    if os.environ.get("RUNNING_IN_DOCKER", "").strip().lower() in ("1", "true", "yes"):
        return True
    return os.path.exists("/.dockerenv")


# ---------------------------------------------------------------------------
# Memory limit (cgroup-aware) — feeds the GATE-1 budget + the gauge total
# ---------------------------------------------------------------------------

def _capped_limit() -> tuple[int, str] | None:
    """The container's memory cap as ``(bytes, "v2"|"v1")``, or ``None`` if uncapped.

    A cgroup value is treated as UNCAPPED (→ ``None``) when it is the literal
    ``"max"``, unreadable, ``<= 0``, or ``>= host MemTotal`` — the v1 "unlimited"
    sentinel is a huge page-aligned number that varies by page size, so we compare
    against ``MemTotal`` rather than matching a literal.
    """
    host_total = _proc_mem_total_bytes()

    def cap(val: int | None) -> int | None:
        if val is None or val <= 0:
            return None
        if host_total is not None and val >= host_total:
            return None  # >= host RAM ⇒ effectively uncapped
        return val

    # cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            raw = f.read().strip()
        if raw != "max":
            c = cap(int(raw))
            if c is not None:
                return c, "v2"
    except (OSError, ValueError):
        pass

    # cgroup v1
    c = cap(_read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    if c is not None:
        return c, "v1"

    return None


def detect_memory_limit_bytes() -> int:
    """Memory budget available to this container, in bytes.

    The container's cgroup cap when capped, else host ``MemTotal`` (a conservative
    2 GiB if even that is unreadable). When uncapped *inside Docker*, logs a
    one-time warning — admission then sizes against the whole host.
    """
    global _warned_uncapped
    capped = _capped_limit()
    if capped is not None:
        return capped[0]

    host_total = _proc_mem_total_bytes()
    if host_total is not None:
        if _running_in_docker() and not _warned_uncapped:
            _warned_uncapped = True
            logger.info(
                "host_resources: proxy container is memory-UNCAPPED (the default) — "
                "admission sizes against the whole host (%d MiB) and Gate 2 vetoes on "
                "host MemAvailable. Set OTODOCK_PROXY_MEM_LIMIT for opt-in hard OOM "
                "containment of the proxy tree.",
                int(host_total / _MIB),
            )
        return host_total

    logger.warning("host_resources: no cgroup limit and /proc/meminfo unreadable; assuming 2 GiB")
    return 2 * 1024 * _MIB


# ---------------------------------------------------------------------------
# Live available RAM — feeds the GATE-2 OOM veto
# ---------------------------------------------------------------------------

def reclaimable_bytes(stat: dict[str, int], *, v2: bool) -> int:
    """Bytes the kernel can reclaim under pressure WITHOUT swapping, from a parsed
    ``memory.stat``. Conservative (never over-counts available) and clamped ≥ 0.

    v2: ``inactive_file + slab_reclaimable − file_dirty − file_writeback``.
    v1: ``total_inactive_file − total_dirty − total_writeback`` — cgroup v1 has NO
    slab-reclaimable key, so v1 cannot credit slab.

    Dirty + writeback pages live inside ``inactive_file`` but are not instantly
    reclaimable (need flushing), so they are subtracted to stay safe under write
    bursts. ``unevictable``/mlocked memory is not part of these keys, so no
    subtraction is needed. Missing keys count as 0.
    """
    if v2:
        recl = (stat.get("inactive_file", 0)
                + stat.get("slab_reclaimable", 0)
                - stat.get("file_dirty", 0)
                - stat.get("file_writeback", 0))
    else:
        recl = (stat.get("total_inactive_file", 0)
                - stat.get("total_dirty", 0)
                - stat.get("total_writeback", 0))
    return max(0, recl)


def live_available_bytes() -> int:
    """Live allocatable RAM right now, container-aware + reclaimable-aware.

    Capped container → ``min((limit − current) + reclaimable(memory.stat),
    host MemAvailable)``. The cgroup term is the CONTAINMENT signal (how much
    of our own cap remains); the host term is the OOM-REALITY signal — a
    sibling container (a leaky Docker-MCP sidecar, the 2026-07-06 camoufox
    incident) eats HOST RAM without touching this cgroup, and a capped proxy
    that reads only its own headroom would keep admitting while the host OOM
    killer (which does not respect our optimism, and may pick postgres) closes
    in. min() keeps both protections: capping bounds US, the host read vetoes
    for EVERYONE.

    Uncapped / bare-metal → ``/proc/meminfo MemAvailable`` (kernel-computed,
    already reclaimable-aware; inside a container /proc/meminfo shows HOST
    values, which here is exactly what we want). FAIL-CLOSED: returns 0
    (⇒ GATE 2 admits nothing) when nothing is readable, logging loudly —
    better to deny than to OOM blind.
    """
    capped = _capped_limit()
    if capped is not None:
        limit, ver = capped
        if ver == "v2":
            current = _read_int("/sys/fs/cgroup/memory.current")
            stat_path = "/sys/fs/cgroup/memory.stat"
            v2 = True
        else:
            current = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
            stat_path = "/sys/fs/cgroup/memory/memory.stat"
            v2 = False
        if current is not None:
            recl = reclaimable_bytes(_read_memory_stat(stat_path), v2=v2)
            cgroup_avail = max(0, (limit - current) + recl)
            host_avail = _proc_meminfo_available_bytes()
            if host_avail is not None:
                return min(cgroup_avail, host_avail)
            return cgroup_avail

    # Uncapped (or cgroup current unreadable) → host MemAvailable.
    avail = _proc_meminfo_available_bytes()
    if avail is not None:
        return avail

    logger.error(
        "host_resources: no readable memory source (cgroup + /proc/meminfo both failed); "
        "admission fails closed (0 available)"
    )
    return 0
