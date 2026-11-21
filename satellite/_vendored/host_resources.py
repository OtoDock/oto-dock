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

Capping policy (the ONE story — keep docker-compose.yml and the admission docs
in agreement): the T2 proxy ships memory-UNCAPPED by default, DELIBERATELY — Gate 2
reads host ``MemAvailable`` there, so admission is bounded by the real machine
(sidecars included) and sessions may lean on swap instead of hard-failing.
Setting ``OTODOCK_PROXY_MEM_LIMIT`` is the opt-in HARD-containment mode (the
kernel OOM-kills the proxy tree before it can starve the host); since
2026-07-06 the capped Gate-2 read is ``min(cgroup headroom, host MemAvailable)``
so capping no longer trades away the sidecar-leak veto. The one-time
uncapped-in-Docker log is informational (know which mode you run), not an error.
"""

from __future__ import annotations

import hashlib
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

_CGROUP_ROOT = "/sys/fs/cgroup"
_PROC_SELF_CGROUP = "/proc/self/cgroup"


def _own_cgroup_dirs() -> list[str]:
    """This process's cgroup v2 folder and each of its ancestors, innermost
    first, ending at the mount's root. Inside a container with its own cgroup
    namespace that is the root alone; a folder that does not exist (a path
    named from outside the namespace) is skipped by the readers."""
    rel = ""
    try:
        with open(_PROC_SELF_CGROUP) as f:
            for line in f:
                if line.startswith("0::"):
                    rel = line[3:].strip()
                    break
    except OSError:
        pass
    parts = [p for p in rel.split("/") if p]
    return [os.path.join(_CGROUP_ROOT, *parts[:i]) for i in range(len(parts), -1, -1)]


def _v2_value(path: str) -> int | None:
    """A cgroup v2 limit file (``memory.max``, ``memory.high``): bytes, or
    None for ``max`` or an unreadable file."""
    try:
        with open(path) as f:
            raw = f.read().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def _v2_limits(host_total: int | None) -> list[tuple[int, str]]:
    """Each limit on this process's cgroup v2 path as ``(bytes, folder)``:
    ``memory.max`` and ``memory.high`` of its own group and every ancestor (a
    bare-metal unit's ``MemoryMax`` sits on its own group, a container's on the
    namespace's root). ``memory.high`` counts: past it the group's processes
    stall in reclaim. A value ``<= 0`` or at least the host's memory is no
    limit."""
    out = []
    for folder in _own_cgroup_dirs():
        for name in ("memory.max", "memory.high"):
            val = _v2_value(os.path.join(folder, name))
            if val is None or val <= 0 or (host_total is not None and val >= host_total):
                continue
            out.append((val, folder))
    return out


def _limited_groups() -> list[tuple[int, str, str]]:
    """Every memory cap on this process as ``(bytes, "v2"|"v1", folder)``:
    each v2 limit on its path (``_v2_limits``), else the v1 hierarchy's one.

    A cgroup value is treated as UNCAPPED when it is the literal ``"max"``,
    unreadable, ``<= 0``, or ``>= host MemTotal`` — the v1 "unlimited"
    sentinel is a huge page-aligned number that varies by page size, so we compare
    against ``MemTotal`` rather than matching a literal.
    """
    host_total = _proc_mem_total_bytes()
    groups = [(val, "v2", folder) for val, folder in _v2_limits(host_total)]
    if groups:
        return groups
    val = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if val is not None and val > 0 and (host_total is None or val < host_total):
        return [(val, "v1", "/sys/fs/cgroup/memory")]
    return []


def _capped_limit() -> tuple[int, str, str] | None:
    """The tightest memory cap on this process (``_limited_groups``), or
    ``None`` if uncapped."""
    groups = _limited_groups()
    return min(groups) if groups else None


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
    """Live allocatable RAM right now, container-aware + reclaimable-aware;
    0 when nothing is readable (``_live_available``).

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
    avail = _live_available()
    if avail is not None:
        return avail
    logger.error(
        "host_resources: no readable memory source (cgroup + /proc/meminfo both failed); "
        "admission fails closed (0 available)"
    )
    return 0


def _cgroup_headroom(limit: int, ver: str, folder: str) -> int | None:
    """One capped group's free room: its limit less its use, plus what the
    kernel can reclaim there without swapping; None when its use is unreadable."""
    current = _read_int(os.path.join(folder, "memory.current" if ver == "v2" else "memory.usage_in_bytes"))
    if current is None:
        return None
    recl = reclaimable_bytes(_read_memory_stat(os.path.join(folder, "memory.stat")), v2=ver == "v2")
    return max(0, (limit - current) + recl)


def _live_available() -> int | None:
    """``live_available_bytes`` without its fail-closed 0: None when nothing
    is readable, so a report can say it knows nothing. Every capped group on
    the path counts (an inner group short of its own cap is short whatever an
    outer one holds), then the host's ``MemAvailable``."""
    room: list[int] = []
    for limit, ver, folder in _limited_groups():
        head = _cgroup_headroom(limit, ver, folder)
        if head is not None:
            room.append(head)
    host_avail = _proc_meminfo_available_bytes()
    if host_avail is not None:
        room.append(host_avail)
    return min(room) if room else None


def report() -> dict:
    """This machine's memory as the admission gates read it, in one frame: a
    satellite sends it to the platform at its sign-in and in every heartbeat
    (the module is vendored into the satellite byte for byte), so the platform
    admits the sessions it runs against its own limit, read again each time.
    ``known`` is false when nothing could be read: the platform then checks
    nothing by memory there, never "nothing free"."""
    avail = _live_available()
    return {
        "total_bytes": detect_memory_limit_bytes(),
        "available_bytes": avail or 0,
        "swap_free_bytes": swap_free_bytes(),
        "capped": _capped_limit() is not None,
        "known": avail is not None and _proc_mem_total_bytes() is not None,
    }


# ---------------------------------------------------------------------------
# The numbers both sides read: the platform's admission and a machine's guard
# ---------------------------------------------------------------------------

#: The reserve of a compute machine and the platform's own host: the larger
#: of the floor (``SESSION_RESERVE_FLOOR_MB``, this by default) and this share
#: of the memory.
COMPUTE_FLOOR_MB = 512
COMPUTE_SHARE = 0.05
#: The reserve of a personal or admin computer: its owner's own programs
#: need the room.
COMPUTER_SHARE = 0.10
COMPUTER_FLOOR_MB = 1024
#: ... at most this much of it by its share: 10 % of a 64 GB workstation would
#: refuse new chats with 6.5 GB free.
COMPUTER_CAP_MB = 4096
#: An admin's ceiling per session never goes under this: a full test suite
#: (4.4 GB measured) with its engine and tools.
WORK_FLOOR_MB = 6144
#: The machine rule stops nothing that grew less than the smaller of this
#: and half the reserve.
MIN_STOP_CAP_MB = 512


def reserve_mb(total_mb: int, *, floor_mb: int, share: float, cap_mb: int = 0) -> int:
    """What a machine keeps free: the larger of ``floor_mb`` and ``share`` of
    its memory, the share at most ``cap_mb`` when one is given."""
    by_share = int(total_mb * share)
    if cap_mb > 0:
        by_share = min(by_share, int(cap_mb))
    return max(int(floor_mb), by_share)


def computer_reserve_mb(total_mb: int) -> int:
    """A personal or admin computer's reserve: 10 % of it, at least 1 GB and,
    by the share, at most 4 GB."""
    return reserve_mb(total_mb, floor_mb=COMPUTER_FLOOR_MB, share=COMPUTER_SHARE, cap_mb=COMPUTER_CAP_MB)


def room_mb(total_mb: int, reserve: int) -> int:
    """What the sessions together may use."""
    return max(0, total_mb - reserve)


def guard_reserve_mb(total_mb: int, reserve: int) -> int:
    """The reserve a machine's guard acts on: at most half the machine, so a
    floor set to close admission (a huge ``SESSION_RESERVE_FLOOR_MB``) never
    makes it stop everything."""
    return min(reserve, total_mb // 2)


def min_stop_mb(reserve: int, cap_mb: int = MIN_STOP_CAP_MB) -> int:
    """The least growth the machine rule stops."""
    return min(int(cap_mb), reserve // 2)


def task_headroom_mb(total_mb: int, reserve: int, heavy_mb: int) -> int:
    """What a task or a background wake leaves free for a person: one heavy
    session's estimate, or a thirty-second of the room on a small machine, so
    the smallest server still starts a task beside a resting chat. A machine of
    unknown size keeps the heavy estimate."""
    if total_mb <= 0:
        return int(heavy_mb)
    return min(int(heavy_mb), room_mb(total_mb, reserve) // 32)


def ceiling_mb(total_mb: int, reserve: int, *, session_max_mb: int = 0) -> int:
    """A session's ceiling: the room, or a lower ceiling the platform names
    (``session_max_mb``, never under ``WORK_FLOOR_MB`` there)."""
    room = room_mb(total_mb, reserve)
    return min(room, int(session_max_mb)) if session_max_mb > 0 else room


def swap_credit_mb(swap_free_mb: int, cap_mb: int) -> int:
    """What a reading adds for free swap: half of it, at most ``cap_mb``; 0
    when the cap is 0 (the default: swap is not free memory)."""
    if cap_mb <= 0 or swap_free_mb <= 0:
        return 0
    return min(swap_free_mb // 2, int(cap_mb))


def self_hash() -> str:
    """SHA256 of this module's source — used by the satellite drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""
