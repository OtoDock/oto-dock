"""Session retention + disk cleanup for LOCAL sandboxed agents.

A platform install's disk is dominated by on-disk CLI session state, not user
content: Claude session JSONLs (``.claude/projects``), Codex rollouts
(``.codex/sessions``), Codex internal telemetry (``logs_*.sqlite`` — observed
130 MB in one agent home) and plugin staging (``.codex/.tmp``), plus orphaned
session files that ``delete_chat`` never removes. This module bounds all of it
with a daily sweep (wired into app.py's registry sweep loop) and powers the
admin "Storage & Retention" card (run-now + usage readout).

Four passes:
  A. Aged chats — governed by the admin knob (``session_retention_enabled``,
     default ON; ``session_retention_days``, default 180): local chats
     untouched for N days lose their session files and are flagged
     ``pending_history_seed='retention'`` — the next turn transparently
     reseeds from DB history (core/session/history_seed.py). Remote/
     satellite chats and direct-LLM are never candidates.
  B. Orphans — fixed 7-day grace, always on: session files no DB row points
     at (deleted chats, CLI subagent sidechains, meeting agent sessions).
     Nothing can ever resume them.
  C. Codex junk — always on: ``logs_*.sqlite*`` + ``.codex/.tmp`` contents in
     idle homes. ``state_*.sqlite`` (thread state) and ``sessions/`` are kept.
  D. MCP tarball cache GC — ``services/mcp_tarball.gc()`` (500 MB quota +
     7-day stale) existed but was never scheduled; the sweep calls it.

Never touched: anything on a remote satellite, workspaces/user content,
plans, ``state_*.sqlite``, token/credential dirs.

Safety model: live sessions are excluded via a snapshot of the real runtime
registries (cli ``_persistent_sessions``, codex ``_codex_sessions`` incl.
their exact ``config_dir``/``thread_id``, ``_session_security`` contexts,
``_active_pumps``) — NOT ``session_state._sessions``, which is append-only.
Session ids shared across chat rows (continue_session delegation chains) are
protected by a "referenced by any fresh chat" set. Every file age-checked;
flagging chats never bumps ``updated_at`` (chat-list order is preserved).
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import config
from storage import database as task_store

logger = logging.getLogger("claude-proxy")

DEFAULT_DAYS = 180
MIN_DAYS = 7
# Orphan files (no DB reference at all) get a short fixed grace by mtime —
# independent of the admin knob; nothing can resume them.
ORPHAN_GRACE_S = 7 * 86400
# Never touch a file modified within the last hour (in-flight safety).
JUNK_MIN_AGE_S = 3600
# Orphaned ``*.partial`` reaper: a live write renames its .partial within
# seconds, so anything older is abandoned (e.g. a write killed by EDQUOT). The
# manifest skips .partial, so these are never re-synced or otherwise cleaned.
PARTIAL_ORPHAN_AGE_S = 3600
_SWEEP_INTERVAL_S = 86400

_last_run: float = 0.0
_sweep_lock = asyncio.Lock()

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings_enabled() -> bool:
    """Retention Pass A toggle. Unset means ON (the platform-settings
    unset-''-is-default convention); only an explicit '0' disables. On managed
    installs the operator pins this via OTODOCK_FORCED_SETTINGS (the overlay in
    storage.get_platform_setting makes it immutable + the admin UI hides it)."""
    return task_store.get_platform_setting("session_retention_enabled") != "0"


def settings_days() -> int:
    raw = task_store.get_platform_setting("session_retention_days")
    try:
        days = int(raw) if raw else DEFAULT_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return max(MIN_DAYS, days)


# ---------------------------------------------------------------------------
# Live snapshot — what is in use RIGHT NOW
# ---------------------------------------------------------------------------

@dataclass
class LiveSnapshot:
    session_ids: set = field(default_factory=set)
    codex_thread_ids: set = field(default_factory=set)
    pump_chat_ids: set = field(default_factory=set)
    codex_config_dirs: set = field(default_factory=set)   # resolved .codex paths
    busy_homes: set = field(default_factory=set)          # (agent, username)


def _build_live_snapshot() -> LiveSnapshot:
    """Snapshot every in-use signal. Must run on the event loop (the
    registries are loop-owned); the threaded sweep gets the frozen copy.

    Liveness truth = the warm-daemon registries (cli _persistent_sessions +
    codex _codex_sessions) and _active_pumps. Deliberately NOT
    core.session.session_state._sessions (append-only, persisted — would mark
    everything live) and NOT the raw _session_security map: contexts are
    cleared on clean close but survive a proxy restart for up to 24h, so
    blanket-trusting them marked every recently-used home busy and starved
    the junk pass. A context contributes the
    (agent, username) busy-home only when its session is in a live registry.
    """
    snap = LiveSnapshot()
    from core.layers.cli.session import _persistent_sessions
    from core.layers.codex.session import _codex_sessions
    from core.session.session_state import _session_security
    from core.events.stream_pump import _active_pumps

    snap.session_ids.update(_persistent_sessions.keys())
    for sid, sess in _codex_sessions.items():
        snap.session_ids.add(sid)
        tid = getattr(sess, "thread_id", None)
        if tid:
            snap.codex_thread_ids.add(tid)
        cfg_dir = getattr(sess, "config_dir", "") or ""
        if cfg_dir:
            try:
                snap.codex_config_dirs.add(str(Path(cfg_dir).resolve()))
            except OSError:
                snap.codex_config_dirs.add(str(cfg_dir))
    for sid in snap.session_ids:
        ctx = _session_security.get(sid)
        agent = getattr(ctx, "agent", "") or "" if ctx else ""
        if agent:
            snap.busy_homes.add((agent, getattr(ctx, "username", "") or ""))
    snap.pump_chat_ids.update(_active_pumps.keys())
    return snap


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def iter_local_homes() -> Iterator[tuple[str, str, Path]]:
    """Yield (agent, username, home) for every local agent home.

    Bounded two-level iteration over the known shapes
    ``AGENTS_DIR/<agent>/users/<username>`` and ``AGENTS_DIR/<agent>/workspace``
    (agent-scope sessions: tasks/phone without a user) — never a tree walk.
    """
    agents_dir = Path(config.AGENTS_DIR)
    if not agents_dir.is_dir():
        return
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        users_dir = agent_dir / "users"
        if users_dir.is_dir():
            for user_home in sorted(users_dir.iterdir()):
                if user_home.is_dir():
                    yield agent_dir.name, user_home.name, user_home
        ws = agent_dir / "workspace"
        if ws.is_dir():
            yield agent_dir.name, "", ws


def _home_for_chat(agent: str, user_sub: str) -> Path:
    """Mirror of the can_resume_session home recipe (cli/layer.py): user-scope
    chats live under users/<username>; sentinel subs (task::<agent>, phone)
    have no users row -> agent-scope workspace home."""
    username = task_store.get_username_by_sub(user_sub) or "" if user_sub else ""
    base = config.get_agent_dir(agent)
    return (base / "users" / username) if username else (base / "workspace")


def _unlink(path: Path, stats: dict, count_key: str, bytes_key: str,
            dry_run: bool) -> None:
    try:
        size = path.lstat().st_size
    except OSError:
        return
    if not dry_run:
        try:
            path.unlink()
        except OSError as e:
            stats["errors"] += 1
            logger.warning(f"retention: failed to delete {path}: {e}")
            return
    stats[count_key] += 1
    stats[bytes_key] += size


def _mtime_older_than(path: Path, age_s: float, now: float) -> bool:
    try:
        return (now - path.lstat().st_mtime) >= age_s
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------

def _pass_aged_chats(days: int, live: LiveSnapshot, stats: dict,
                     dry_run: bool) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    candidates = task_store.get_retention_candidate_chats(cutoff)
    if not candidates:
        return
    # Session files are shared across chat rows (continue_session delegation
    # reuses one session id on a fresh chat per round) — protect any id a
    # FRESH chat still references.
    prot_sids, prot_tids = task_store.get_protected_session_refs(cutoff)
    flag_ids: list[str] = []
    for chat in candidates:
        sid = chat.get("session_id") or ""
        tid = chat.get("codex_thread_id") or ""
        if sid and (sid in prot_sids or sid in live.session_ids):
            continue
        if tid and (tid in prot_tids or tid in live.codex_thread_ids):
            continue
        if chat["id"] in live.pump_chat_ids:
            continue
        home = _home_for_chat(chat["agent"], chat.get("user_sub") or "")
        if sid and _UUID_RE.match(sid):
            # All project dirs — the munged-cwd dir name varies (and
            # satellite-migrated copies exist inside local homes too).
            for f in (home / ".claude" / "projects").glob(f"*/{sid}.jsonl"):
                _unlink(f, stats, "session_files_deleted", "bytes_freed", dry_run)
            # Legacy pre-sandbox location under the proxy's own home.
            legacy = (Path.home() / ".claude" / "projects"
                      / str(config.AGENTS_DIR).replace("/", "-")
                      / f"{sid}.jsonl")
            if legacy.is_file():
                _unlink(legacy, stats, "session_files_deleted", "bytes_freed", dry_run)
        if tid and _UUID_RE.match(tid):
            for f in (home / ".codex" / "sessions").rglob(f"*{tid}.jsonl"):
                _unlink(f, stats, "session_files_deleted", "bytes_freed", dry_run)
        # Flag even when files were already missing — the chat can't resume
        # either way, and the digest is the right outcome on next open.
        flag_ids.append(chat["id"])
    if flag_ids and not dry_run:
        task_store.flag_chats_for_retention(flag_ids)
    stats["chats_flagged"] += len(flag_ids)


def _pass_orphans(live: LiveSnapshot, stats: dict, dry_run: bool) -> None:
    refs = task_store.get_all_session_refs()
    refs |= live.session_ids | live.codex_thread_ids
    now = time.time()
    for _agent, _username, home in iter_local_homes():
        for f in (home / ".claude" / "projects").glob("*/*.jsonl"):
            if not _UUID_RE.match(f.stem) or f.stem in refs:
                continue
            if _mtime_older_than(f, ORPHAN_GRACE_S, now):
                _unlink(f, stats, "orphans_deleted", "orphan_bytes", dry_run)
        for f in (home / ".codex" / "sessions").rglob("rollout-*.jsonl"):
            tid = f.stem[-36:]
            if not _UUID_RE.match(tid) or tid in refs:
                continue
            if _mtime_older_than(f, ORPHAN_GRACE_S, now):
                _unlink(f, stats, "orphans_deleted", "orphan_bytes", dry_run)


def _pass_codex_junk(live: LiveSnapshot, stats: dict, dry_run: bool) -> None:
    now = time.time()
    for agent, username, home in iter_local_homes():
        codex = home / ".codex"
        if not codex.is_dir():
            continue
        if (agent, username) in live.busy_homes:
            continue
        try:
            resolved = str(codex.resolve())
        except OSError:
            resolved = str(codex)
        if resolved in live.codex_config_dirs:
            continue
        # Telemetry/log DBs (incl. -wal/-shm sidecars). state_*.sqlite is
        # Codex's thread state — small and load-bearing, never touched.
        targets: list[Path] = list(codex.glob("logs_*.sqlite*"))
        tmp = codex / ".tmp"
        if tmp.is_dir():
            targets.extend(p for p in tmp.rglob("*")
                           if p.is_file() or p.is_symlink())
        for f in targets:
            if _mtime_older_than(f, JUNK_MIN_AGE_S, now):
                _unlink(f, stats, "codex_junk_files", "codex_junk_bytes", dry_run)
        if tmp.is_dir() and not dry_run:
            # Prune emptied staging subdirs, deepest first; keep .tmp itself.
            subdirs = sorted((p for p in tmp.rglob("*") if p.is_dir()),
                             key=lambda p: len(p.parts), reverse=True)
            for d in subdirs:
                with contextlib.suppress(OSError):
                    d.rmdir()


def _pass_tarball_gc(stats: dict, dry_run: bool) -> None:
    if dry_run:
        return
    from services.mcp import mcp_tarball
    stats["tarball_bytes"] += mcp_tarball.gc()


def _pass_orphan_partials(stats: dict, dry_run: bool) -> None:
    """Reap ``*.partial`` files left under the agent tree by a write that died
    mid-flight (e.g. EDQUOT under a full quota). See PARTIAL_ORPHAN_AGE_S."""
    root = config.AGENTS_DIR
    if not root.exists():
        return
    now = time.time()
    for p in root.rglob("*.partial"):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if _mtime_older_than(p, PARTIAL_ORPHAN_AGE_S, now):
            _unlink(p, stats, "partials_deleted", "partial_bytes", dry_run)


def _pass_mcp_autoupdate_log(stats: dict, dry_run: bool) -> None:
    """Trim the automatic MCP-update run log (keep ~90 days / newest 500 rows)."""
    if dry_run:
        return
    from storage import mcp_autoupdate_store
    stats["mcp_autoupdate_rows_deleted"] += mcp_autoupdate_store.prune()


def _pass_orphan_quota_projects(stats: dict, dry_run: bool) -> None:
    """Drift insurance for hard storage quotas: zero the limit on any project
    whose agent no longer exists (delete_agent already reclaims, so this only
    catches a failed reclaim or pre-feature rows). Project rows are kept as
    tombstones so their XFS IDs are never reused. No-op unless hard enforcement
    is active."""
    from services.infra import storage_quota
    if not storage_quota.hard_enabled():
        return
    try:
        from storage import agent_store
        live = set(agent_store.get_agent_slugs())
        for row in storage_quota.list_projects():
            if row.get("agent_slug") not in live:
                if not dry_run:
                    storage_quota.reclaim_project(row["scope_key"])
                stats["quota_projects_reclaimed"] += 1
    except Exception:
        stats["errors"] += 1
        logger.exception("retention: orphan quota-project reap failed")


# ---------------------------------------------------------------------------
# Sweep entry points
# ---------------------------------------------------------------------------

def _run_sweep_sync(days: int, enabled: bool, live: LiveSnapshot,
                    dry_run: bool) -> dict:
    started = time.monotonic()
    stats: dict = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "retention_days": days,
        "retention_pass_skipped": not enabled,
        "chats_flagged": 0,
        "session_files_deleted": 0,
        "bytes_freed": 0,
        "orphans_deleted": 0,
        "orphan_bytes": 0,
        "codex_junk_files": 0,
        "codex_junk_bytes": 0,
        "tarball_bytes": 0,
        "partials_deleted": 0,
        "partial_bytes": 0,
        "quota_projects_reclaimed": 0,
        "mcp_autoupdate_rows_deleted": 0,
        "errors": 0,
    }
    passes = []
    if enabled:
        passes.append(("aged-chats", lambda: _pass_aged_chats(days, live, stats, dry_run)))
    passes.extend([
        ("orphans", lambda: _pass_orphans(live, stats, dry_run)),
        ("codex-junk", lambda: _pass_codex_junk(live, stats, dry_run)),
        ("tarball-gc", lambda: _pass_tarball_gc(stats, dry_run)),
        ("orphan-partials", lambda: _pass_orphan_partials(stats, dry_run)),
        ("orphan-quota-projects", lambda: _pass_orphan_quota_projects(stats, dry_run)),
        ("mcp-autoupdate-log", lambda: _pass_mcp_autoupdate_log(stats, dry_run)),
    ])
    for name, fn in passes:
        try:
            fn()
        except Exception:
            stats["errors"] += 1
            logger.exception(f"retention: pass '{name}' failed")
    stats["duration_ms"] = int((time.monotonic() - started) * 1000)
    total = (stats["bytes_freed"] + stats["orphan_bytes"]
             + stats["codex_junk_bytes"] + stats["tarball_bytes"])
    logger.info(
        f"retention: sweep done in {stats['duration_ms']}ms "
        f"(dry_run={dry_run}, enabled={enabled}): "
        f"{stats['chats_flagged']} chats flagged, "
        f"{stats['session_files_deleted']} session files, "
        f"{stats['orphans_deleted']} orphans, "
        f"{stats['codex_junk_files']} codex-junk files, "
        f"{total} bytes total"
    )
    return stats


async def run_sweep(*, dry_run: bool = False) -> dict:
    """Run one full sweep (the run-now endpoint + the daily tick). The live
    snapshot is built on the event loop; the file/DB work runs in a thread.
    The lock serializes run-now against the daily tick."""
    global _last_run
    async with _sweep_lock:
        enabled = settings_enabled()
        days = settings_days()
        snapshot = _build_live_snapshot()
        stats = await asyncio.to_thread(
            _run_sweep_sync, days, enabled, snapshot, dry_run,
        )
        if not dry_run:
            _last_run = time.monotonic()
            try:
                task_store.set_platform_setting(
                    "session_retention_last_sweep", json.dumps(stats),
                )
            except Exception:
                logger.exception("retention: failed to persist sweep stats")
        return stats


async def maybe_run_daily() -> None:
    """Called every 60s from app.py's registry sweep loop; runs at most once
    per 24h. First run lands ~60s after boot (cheap when there's nothing
    to do)."""
    if time.monotonic() - _last_run < _SWEEP_INTERVAL_S and _last_run:
        return
    await run_sweep()


# ---------------------------------------------------------------------------
# Storage usage readout (admin card)
# ---------------------------------------------------------------------------

def _tree_bytes(path: Path) -> int:
    total = 0
    try:
        if not path.exists():
            return 0
        for p in path.rglob("*"):
            try:
                st = p.lstat()
            except OSError:
                continue
            if not (st.st_mode & 0o170000) == 0o040000:  # not a directory
                total += st.st_size
    except OSError:
        pass
    return total


def compute_storage_usage() -> dict:
    """Byte totals for the admin Storage & Retention card. Sync — call via
    asyncio.to_thread (walks the agents tree)."""
    session_files = 0
    codex_junk = 0
    for _agent, _username, home in iter_local_homes():
        for f in (home / ".claude" / "projects").glob("*/*.jsonl"):
            with contextlib.suppress(OSError):
                session_files += f.lstat().st_size
        for f in (home / ".codex" / "sessions").rglob("*.jsonl"):
            with contextlib.suppress(OSError):
                session_files += f.lstat().st_size
        codex = home / ".codex"
        for f in codex.glob("logs_*.sqlite*"):
            with contextlib.suppress(OSError):
                codex_junk += f.lstat().st_size
        codex_junk += _tree_bytes(codex / ".tmp")

    base = Path(config.BASE_DIR)
    logs = 0
    for f in base.glob("proxy.log*"):
        with contextlib.suppress(OSError):
            logs += f.lstat().st_size

    last_sweep = None
    raw = task_store.get_platform_setting("session_retention_last_sweep")
    if raw:
        try:
            last_sweep = json.loads(raw)
        except (ValueError, TypeError):
            last_sweep = None

    return {
        "agents_bytes": _tree_bytes(Path(config.AGENTS_DIR)),
        "session_files_bytes": session_files,
        "codex_junk_bytes": codex_junk,
        "recover_bin_bytes": _tree_bytes(Path(config.RECOVER_BIN_DIR)),
        "sessions_dir_bytes": _tree_bytes(Path(config.SESSIONS_DIR)),
        "logs_bytes": logs,
        "retention": {
            "enabled": settings_enabled(),
            "days": settings_days(),
            "last_sweep": last_sweep,
        },
    }
