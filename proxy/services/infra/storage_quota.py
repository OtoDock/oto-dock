"""Per-agent storage quotas over the local agent tree (``config.AGENTS_DIR``).

Two tiers share one scope model:

  * **Soft tier** (always on once a limit is set): ``services/infra/quota_monitor.py``
    measures each scope's usage and fires the 90/95/100 % WARNING notifications.
    Works on any filesystem; needs no privilege. This is the baseline everywhere.
  * **Hard tier** (XFS project quotas): adds a hard cap so an over-limit write
    fails with ``EDQUOT``. **Auto-enabled** when the agents dir is on an XFS
    mount with project quota active (``prjquota``); otherwise the soft tier runs
    and a single line is logged — ``quotas_preflight`` never bricks boot. This
    module owns the project-ID registry, the privileged ``oto-quota`` helper
    shelling (project assign / limit / report — no image, no mounting), and the
    per-scope assignment. Put the data dir on a project-quota XFS volume to get
    hard enforcement; we never create an image or reconfigure a host filesystem.

Two scopes (XFS project IDs) per agent:

  * ``shared`` → ``workspace`` + ``knowledge`` + ``config``  (one project ID
    stamped on all three; ``quota_shared_folder_mb``)
  * ``user``   → ``users/{username}/``                       (``quota_user_folder_mb``)
"""

import logging
import os
import stat as _stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import config
from storage.pg import get_conn

logger = logging.getLogger(__name__)

# XFS project IDs are uint32. Allocate from a high reserved base so we never
# collide with project IDs that may already exist on an operator-provided shared
# quota volume, and stay below the uint32 ceiling.
_PROJECT_ID_BASE = 0x40000000          # 1,073,741,824
_PROJECT_ID_MAX = 0xFFFFFFFF - 1       # 4,294,967,294
# Postgres advisory-lock key serializing concurrent project-ID allocation
# (arbitrary fixed bigint, namespaced to this feature).
_ALLOC_LOCK_KEY = 5102024_0616

# Process-local memo: scope_keys whose XFS project ID has already been stamped
# on disk this process. Lets the belt-and-braces ensure_scope() call (before
# every sandbox build) skip the subprocess shell-out after the first time.
_assigned_this_process: set[str] = set()

# Resolved once at startup by quotas_preflight(): True iff the agents dir is on a
# project-quota-capable XFS mount AND enforcement isn't force-disabled AND the
# helper is reachable. All kernel ops gate on this — never on a static flag — so
# we never try to enforce on a filesystem that can't, and never brick boot.
_hard_enabled: bool = False


def hard_enabled() -> bool:
    """True if hard (XFS project-quota) enforcement is active for this process."""
    return _hard_enabled


# ---------------------------------------------------------------------------
# Scope identity + on-disk layout
# ---------------------------------------------------------------------------

def shared_scope_key(agent_slug: str) -> str:
    return f"{agent_slug}:shared"


def user_scope_key(agent_slug: str, username: str) -> str:
    return f"user:{agent_slug}:{username}"


def shared_scope_dirs(agent_slug: str) -> list[Path]:
    """The three directories the shared bucket spans (one project ID covers all)."""
    base = config.get_agent_dir(agent_slug)
    return [base / "workspace", base / "knowledge", base / "config"]


def user_scope_dir(agent_slug: str, username: str) -> Path:
    return config.get_agent_dir(agent_slug) / "users" / username


def external_scope_key(agent_slug: str) -> str:
    return f"external:{agent_slug}"


def external_scope_dir(agent_slug: str) -> Path:
    """The external callers' trees (``externals/``) — one bucket per agent,
    separate from the shared one so a caller cannot fill it."""
    return config.get_agent_dir(agent_slug) / "externals"


@dataclass(frozen=True)
class QuotaScope:
    """One quota bucket: its identity, the dirs it governs, and notify routing."""
    scope_key: str
    scope_type: str            # 'shared' | 'user'
    agent_slug: str
    username: str | None       # filesystem username slug, for 'user' scopes
    dirs: tuple[Path, ...]
    owner_sub: str | None = None  # the user's sub, for routing 'user'-scope alerts


def iter_scopes() -> list[QuotaScope]:
    """Enumerate every quota scope across all agents (one shared + one per user).

    Every agent's SHARED scope is metered (all modes the same). Per-user scopes
    only exist for agents that mount user scope — a Shared-only agent has no
    per-user dirs, so it contributes only the shared scope. Derived from the live
    agent/user tables, so it works in both tiers (the soft tier needs no project
    rows).
    """
    from storage.agents import agent_store
    from storage import database
    from core.session.visibility import is_shared_only

    scopes: list[QuotaScope] = []
    for agent in agent_store.get_all_agents():
        slug = agent.get("slug")
        if not slug:
            continue
        scopes.append(QuotaScope(
            scope_key=shared_scope_key(slug),
            scope_type="shared",
            agent_slug=slug,
            username=None,
            dirs=tuple(shared_scope_dirs(slug)),
        ))
        if external_scope_dir(slug).is_dir():
            scopes.append(QuotaScope(
                scope_key=external_scope_key(slug),
                scope_type="external",
                agent_slug=slug,
                username=None,
                dirs=(external_scope_dir(slug),),
            ))
        if is_shared_only(slug):
            continue  # no per-user dirs for a Shared-only agent
        for u in database.get_agent_users_with_profile(slug):
            uname = (u.get("username") or "").strip()
            if not uname:
                continue  # no filesystem slug yet → no user folder to bound
            scopes.append(QuotaScope(
                scope_key=user_scope_key(slug, uname),
                scope_type="user",
                agent_slug=slug,
                username=uname,
                dirs=(user_scope_dir(slug, uname),),
                owner_sub=u.get("sub") or None,
            ))
    return scopes


# ---------------------------------------------------------------------------
# Limits (single source of truth = the quota_* platform settings)
# ---------------------------------------------------------------------------

def _setting_mb(key: str, default_mb: int) -> int:
    """A quota platform setting in MB. Unset → default; ``0`` → unlimited."""
    from storage import database
    raw = database.get_platform_setting(key)
    if raw == "":
        return default_mb
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default_mb


def _setting_inodes(key: str, default_inodes: int) -> int:
    from storage import database
    raw = database.get_platform_setting(key)
    if raw == "":
        return default_inodes
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default_inodes


def limits_for(scope_type: str) -> tuple[int, int]:
    """``(limit_bytes, inode_limit)`` for a scope type. ``0`` means unlimited."""
    if scope_type == "shared":
        mb = _setting_mb("quota_shared_folder_mb", config.QUOTA_SHARED_FOLDER_MB_DEFAULT)
        inodes = _setting_inodes("quota_shared_folder_inodes", config.QUOTA_SHARED_FOLDER_INODES_DEFAULT)
    elif scope_type == "external":
        mb = _setting_mb("quota_external_folder_mb", config.QUOTA_EXTERNAL_FOLDER_MB_DEFAULT)
        inodes = _setting_inodes("quota_user_folder_inodes", config.QUOTA_USER_FOLDER_INODES_DEFAULT)
    else:
        mb = _setting_mb("quota_user_folder_mb", config.QUOTA_USER_FOLDER_MB_DEFAULT)
        inodes = _setting_inodes("quota_user_folder_inodes", config.QUOTA_USER_FOLDER_INODES_DEFAULT)
    return (mb * 1024 * 1024, inodes)


# ---------------------------------------------------------------------------
# Project-ID registry (kernel tier)
# ---------------------------------------------------------------------------

def get_project_id(scope_key: str) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM storage_quota_projects WHERE scope_key=%s",
            (scope_key,),
        ).fetchone()
        return int(row["project_id"]) if row else None


def get_or_alloc_project(scope_key: str, agent_slug: str, scope_type: str,
                         username: str | None) -> int:
    """Return the scope's project ID, allocating a fresh monotonic one if new.

    Rows are never deleted (reclaim only zeroes the limit), so ``MAX(project_id)``
    only ever climbs — a freed ID is never handed to a different scope, which
    matters because XFS caches per-ID usage. A Postgres advisory lock serializes
    concurrent allocations so two different scopes can't grab the same ID.
    """
    with get_conn() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ALLOC_LOCK_KEY,))
        row = conn.execute(
            "SELECT project_id FROM storage_quota_projects WHERE scope_key=%s",
            (scope_key,),
        ).fetchone()
        if row:
            return int(row["project_id"])
        nxt = conn.execute(
            "SELECT COALESCE(MAX(project_id), %s) + 1 AS nid FROM storage_quota_projects",
            (_PROJECT_ID_BASE - 1,),
        ).fetchone()["nid"]
        if int(nxt) > _PROJECT_ID_MAX:
            raise RuntimeError("storage_quota: XFS project-ID space exhausted")
        conn.execute(
            "INSERT INTO storage_quota_projects "
            "(scope_key, agent_slug, scope_type, username, project_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (scope_key, agent_slug, scope_type, username, int(nxt)),
        )
        conn.commit()
        return int(nxt)


def list_projects() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT scope_key, agent_slug, scope_type, username, project_id "
            "FROM storage_quota_projects ORDER BY project_id"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Privileged helper shelling (oto-quota)
# ---------------------------------------------------------------------------

def _is_root() -> bool:
    return getattr(os, "geteuid", lambda: 1000)() == 0


def _helper_argv(args: list[str]) -> list[str]:
    """Build the argv for the oto-quota helper.

    Called directly when the proxy runs as root (e.g. a T3 cloud privileged
    quota sidecar); otherwise via ``sudo -n`` against the allowlisted helper
    (bare-metal T1). The non-root T2 proxy has no sudo → hard tier is unavailable
    and quotas degrade to soft (handled in ``quotas_preflight``).
    """
    helper = config.OTODOCK_QUOTA_HELPER
    if _is_root():
        return [helper, *args]
    return ["sudo", "-n", helper, *args]


def _run_helper(*args, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    argv = _helper_argv([str(a) for a in args])
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"oto-quota {args[0] if args else ''} failed (rc={cp.returncode}): "
            f"{(cp.stderr or cp.stdout or '').strip()}"
        )
    return cp


# ---------------------------------------------------------------------------
# Assignment (kernel tier) — stamp the project ID + inherit flag before writes
# ---------------------------------------------------------------------------

def ensure_scope(agent_slug: str, scope_type: str, username: str | None = None) -> int | None:
    """Idempotently bind a scope to its XFS project ID and apply its limit.

    Creates the scope's directories (so the inherit flag is set BEFORE any write
    — lazily-created children like ``.claude/`` then inherit the ID), allocates
    or looks up the project ID, stamps every dir, and applies the current limit.

    No-op (returns ``None``) unless the kernel tier is enabled. Safe to call on
    every sandbox build: after the first stamp this process, the on-disk
    ``assign`` is skipped via an in-memory memo (limits are re-applied by the
    monitor sweep, not here).
    """
    if not hard_enabled():
        return None
    if scope_type == "shared":
        scope_key = shared_scope_key(agent_slug)
        dirs = shared_scope_dirs(agent_slug)
    elif scope_type == "user":
        if not username:
            return None
        scope_key = user_scope_key(agent_slug, username)
        dirs = [user_scope_dir(agent_slug, username)]
    elif scope_type == "external":
        scope_key = external_scope_key(agent_slug)
        dirs = [external_scope_dir(agent_slug)]
    else:
        raise ValueError(f"unknown scope_type {scope_type!r}")

    if scope_key in _assigned_this_process:
        return None  # already stamped this process — keep the hot path O(1)

    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("storage_quota: could not create scope dir %s", d)
            return None

    pid = get_or_alloc_project(scope_key, agent_slug, scope_type, username)
    try:
        for d in dirs:
            _run_helper("assign", str(d), pid)
        limit_bytes, inode_limit = limits_for(scope_type)
        _run_helper("setlimit", str(config.AGENTS_DIR), pid, limit_bytes, inode_limit)
    except Exception:
        # Don't wedge agent creation / session start on a transient helper error;
        # the monitor sweep re-asserts assignment + limits. Leave the scope out of
        # the memo so the next call retries.
        logger.exception("storage_quota: ensure_scope(%s) helper call failed", scope_key)
        return pid
    _assigned_this_process.add(scope_key)
    return pid


def apply_limit(project_id: int, scope_type: str) -> None:
    """Re-apply the current limit for a project (used by the monitor on setting
    changes). Idempotent; no-op when the kernel tier is off."""
    if not hard_enabled():
        return
    limit_bytes, inode_limit = limits_for(scope_type)
    _run_helper("setlimit", str(config.AGENTS_DIR), project_id, limit_bytes, inode_limit)


def reapply_all_limits() -> None:
    """Push the current quota settings to every live XFS project — called after
    an admin changes a limit so it takes effect immediately. No-op (and cheap)
    when the kernel tier is off; the soft-tier monitor re-reads settings anyway."""
    if not hard_enabled():
        return
    for row in list_projects():
        try:
            apply_limit(int(row["project_id"]), row["scope_type"])
        except Exception:
            logger.exception("storage_quota: reapply limit for %s failed", row.get("scope_key"))


def reclaim_project(scope_key: str) -> None:
    """Release a scope's enforcement on agent/user delete.

    Zeroes the limit (accounting may continue on any persisted folder, but no
    EDQUOT) and drops it from the process memo. The registry row is KEPT as a
    tombstone so its project ID is never reallocated to a different scope; the
    retention orphan-project reaper handles long-term drift.
    """
    _assigned_this_process.discard(scope_key)
    # Clear the scope's threshold-alert dedup state regardless of tier, so a
    # later re-creation of the same scope_key starts from a clean slate.
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM storage_quota_alerts WHERE scope_key=%s", (scope_key,))
            conn.commit()
    except Exception:
        logger.exception("storage_quota: clearing alerts for %s failed", scope_key)
    if not hard_enabled():
        return
    pid = get_project_id(scope_key)
    if pid is None:
        return
    try:
        _run_helper("setlimit", str(config.AGENTS_DIR), pid, 0, 0)
    except Exception:
        logger.exception("storage_quota: reclaim_project(%s) failed", scope_key)


def reclaim_agent(agent_slug: str) -> None:
    """Release quota enforcement + alert state for every scope of a deleted
    agent. Project rows are KEPT as tombstones so their IDs are never reused."""
    try:
        scope_keys = [r["scope_key"] for r in list_projects() if r.get("agent_slug") == agent_slug]
    except Exception:
        logger.exception("storage_quota: reclaim_agent(%s) list failed", agent_slug)
        return
    # Always also reclaim the canonical shared scope_key (covers the soft-tier
    # case where no project row was ever created but alerts may exist).
    for sk in set(scope_keys) | {shared_scope_key(agent_slug)}:
        reclaim_project(sk)


# ---------------------------------------------------------------------------
# Usage measurement
# ---------------------------------------------------------------------------

def dir_usage(path: Path) -> tuple[int, int]:
    """``(bytes, file_count)`` under ``path``, best-effort.

    Uses ``lstat`` (never follows symlinks → no escape / double-count), skips
    unreadable entries, and never raises. A permission error mid-walk yields an
    undercount, so callers treat the result as a FLOOR.
    """
    total = 0
    files = 0
    try:
        if not path.exists():
            return (0, 0)
        for p in path.rglob("*"):
            try:
                st = p.lstat()
            except OSError:
                continue
            if _stat.S_ISDIR(st.st_mode):
                continue
            total += st.st_size
            files += 1
    except OSError:
        pass
    return (total, files)


def report_usage(project_id: int) -> tuple[int, int] | None:
    """Kernel-reported ``(used_bytes, used_inodes)`` for a project, or ``None``.

    O(1) via ``xfs_quota report`` — far cheaper than a tree walk — but only
    available in the kernel tier. ``None`` falls the monitor back to ``dir_usage``.
    """
    if not hard_enabled():
        return None
    try:
        cp = _run_helper("report", str(config.AGENTS_DIR), project_id, check=True, timeout=30)
    except Exception:
        return None
    parts = cp.stdout.split()
    if len(parts) < 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Boot preflight (mirrors core/sandbox/sandbox.py::netns_preflight)
# ---------------------------------------------------------------------------

def _mount_info(path: Path) -> tuple[str, str] | None:
    """``(fstype, mount_options)`` of the mount backing ``path``, via /proc/mounts.

    Returns the longest-prefix mount, so a dedicated XFS volume mounted AT the
    agents dir wins over the rootfs. ``None`` if /proc/mounts is unreadable.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    best: tuple[str, str, str] | None = None  # (mountpoint, fstype, opts)
    try:
        with open("/proc/mounts") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 4:
                    continue
                mp = fields[1].encode().decode("unicode_escape")  # \040 etc.
                fstype, opts = fields[2], fields[3]
                norm = mp.rstrip("/") or "/"
                if real == norm or real.startswith(norm.rstrip("/") + "/") or norm == "/":
                    if best is None or len(norm) > len(best[0]):
                        best = (norm, fstype, opts)
    except OSError:
        return None
    if best is None:
        return None
    return (best[1], best[2])


def quotas_preflight() -> None:
    """Resolve hard-vs-soft enforcement at startup and remember it (``_hard_enabled``).

    Hard XFS enforcement auto-activates when the agents dir is on an XFS mount
    with project quota active (``prjquota``) AND the privileged helper is
    reachable. Anything else degrades to the soft tier (measure + warn) with a
    single log line. This NEVER raises / bricks boot — the soft tier is always a
    valid mode, so a misconfigured or unsupported filesystem just means "no hard
    cap", not "platform down".
    """
    global _hard_enabled
    _hard_enabled = False

    if config.STORAGE_QUOTAS_FORCE_SOFT:
        logger.info("storage_quota: hard enforcement disabled by config "
                    "(OTODOCK_STORAGE_QUOTAS=off) — soft tier (measure + warn) only")
        return

    agents_dir = config.AGENTS_DIR
    if not agents_dir.is_dir():
        logger.info("storage_quota: agents dir %s does not exist yet — soft tier only", agents_dir)
        return

    info = _mount_info(agents_dir)
    if info is None:
        logger.info("storage_quota: could not resolve the agents dir filesystem — soft tier only")
        return
    fstype, opts = info
    if fstype != "xfs" or not ("prjquota" in opts or "pquota" in opts):
        logger.info(
            "storage_quota: agents dir is on %r (mount opts: %s) — soft tier (measure + "
            "warn) only. For hard EDQUOT enforcement, put the data dir on an XFS mount "
            "with the 'prjquota' option.",
            fstype, opts,
        )
        return

    # Capable filesystem. Hard enforcement also needs to actually drive
    # xfs_quota: directly when root (e.g. a T3 cloud privileged quota sidecar),
    # else the allowlisted helper must resolve via `sudo -n` (bare-metal T1). The
    # non-root T2/cloud proxy (uid 1000) has no sudo in the container, so it lands
    # in the helper-unreachable branch below → soft tier. That is the intended
    # one T1↔T2 behavioural difference.
    if not _is_root():
        helper = Path(config.OTODOCK_QUOTA_HELPER)
        if not helper.exists():
            logger.warning("storage_quota: XFS project quota is active on %s but the "
                           "oto-quota helper is missing at %s — soft tier only",
                           agents_dir, helper)
            return
        try:
            _run_helper("check", str(agents_dir), check=True, timeout=15)
        # Do NOT narrow this `except` — it is the safety net that degrades the
        # non-root proxy to the soft tier. `_run_helper` shells out to
        # `sudo -n oto-quota`, which on a sudo-less container raises
        # FileNotFoundError (not CalledProcessError); narrowing this would let
        # that propagate and brick boot on an XFS-prjquota volume. Covered by the
        # capable-fs degradation test in tests/.
        except Exception as e:  # noqa: BLE001
            logger.warning("storage_quota: XFS project quota is active but the oto-quota "
                           "helper isn't reachable via 'sudo -n' (%s) — soft tier only "
                           "(install /etc/sudoers.d/otodock-quota, or run the proxy as root)", e)
            return

    _hard_enabled = True
    logger.info("storage_quota: hard enforcement ACTIVE — XFS project quotas on %s", agents_dir)
