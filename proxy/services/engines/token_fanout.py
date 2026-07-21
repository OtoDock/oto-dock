"""Rotation fan-out — delivers a refreshed OAuth token to live sessions' files.

The subscription pool is the platform's SOLE token rotator (session credential
files carry a neutralized refresh token, so a CLI physically cannot rotate).
Providers revoke older outstanding access tokens on rotation, so every rotation
MUST reach every live session's on-disk credential file immediately:

- Claude reads ``.credentials.json`` in ``CLAUDE_CONFIG_DIR`` — an mtime-watch
  picks a rewrite up proactively, and its 401-recovery re-reads the file (and
  patches its own process env) as the backstop.
- Codex reads ``auth.json`` in ``CODEX_HOME`` — its guarded reload re-reads the
  file before refreshing and skips its own refresh when the token changed.

Credential files live in the SCOPE config dir (``users/<u>/.claude`` or
``workspace/.claude`` — shared by every session of that scope), so writes are
deduped per directory. Local dirs are written synchronously; satellite dirs get
a ``credentials_update`` push over the machine's WS (fire-and-forget with ack —
these files are deliberately excluded from the generic file sync). The
``on_written`` callback lets the pool advance its per-session expiry snapshots
only for sessions whose file actually landed.

Also hosts the token-freshness worker: a 5-minute tick that keeps every bound
OAuth subscription's runway above the turn-guard threshold, so live sessions —
including otodock-attached terminals, which can never be respawned — simply
never reach their token's death. It replaced the interactive re-warm worker's
token-driven respawns. Each tick starts with a selection-change rebind pass
(``subscription_pool.rebind_delisted_sessions``) — the convergence loop that
re-homes sessions off deselected/deleted accounts when the immediate API-hook
pass couldn't land (satellite offline, replacement connected later).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import contextlib

logger = logging.getLogger("claude-proxy.token-fanout")

_FRESHNESS_INTERVAL_S = 300

# Claude's 401 recovery polls the credentials file for a rotated token for up
# to this long before giving up (CLAUDE_CODE_OAUTH_401_WAIT_MS). Set on REMOTE
# sessions only: a satellite's file lands after a WS round-trip, so a request
# racing the push needs the poll window; local writes are synchronous.
REMOTE_CLAUDE_401_WAIT_MS = 20_000


@dataclass(frozen=True)
class CredentialFileTarget:
    """Where one session's credential file lives.

    ``kind`` picks the file format: ``"claude"`` → ``.credentials.json``
    (claudeAiOauth schema), ``"codex"`` → ``auth.json``. Local sessions carry
    the absolute ``host_dir``; remote sessions carry ``machine_id`` +
    ``agent_name`` + ``dir_relative`` (agent-dir-rooted, the satellite resolves
    its own tree).
    """
    kind: str                  # "claude" | "codex"
    machine_id: str = ""       # "" = local proxy host
    host_dir: str = ""         # local: absolute .claude/.codex dir
    agent_name: str = ""       # remote: agent slug
    dir_relative: str = ""     # remote: e.g. "users/alice/.claude"


_targets: dict[str, CredentialFileTarget] = {}  # session_id → target
_targets_lock = threading.Lock()

# Event loop for scheduling satellite pushes from pool worker threads —
# captured by start_worker() at startup. None (tests / pre-startup) skips
# remote pushes with a log line.
_loop: asyncio.AbstractEventLoop | None = None


def register_session_target(session_id: str, target: CredentialFileTarget) -> None:
    """Track where ``session_id``'s credential file lives (called by the layer
    at spawn, only when it actually wrote one — API-key sessions never
    register)."""
    with _targets_lock:
        _targets[session_id] = target


def unregister_session_target(session_id: str) -> None:
    with _targets_lock:
        _targets.pop(session_id, None)


def session_target(session_id: str) -> CredentialFileTarget | None:
    with _targets_lock:
        return _targets.get(session_id)


# ---------------------------------------------------------------------------
# Credential file writers — the single source for each file's on-disk shape
# ---------------------------------------------------------------------------

def write_claude_credentials_file(config_dir: Path, claude_blob: dict) -> None:
    """Write ``.credentials.json`` (the CLI's ``claudeAiOauth`` schema) into a
    session's ``CLAUDE_CONFIG_DIR``. ``claude_blob`` comes from the pool with a
    neutralized (blank) refreshToken — the pool is the sole rotator."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": claude_blob}))
    path.chmod(0o600)


def write_codex_auth_file(config_dir: Path, auth: dict) -> None:
    """Write ``auth.json`` into a session's ``CODEX_HOME`` (already the full
    file payload — built by ``codex.helpers.build_auth_json``, refresh token
    neutralized there)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "auth.json"
    path.write_text(json.dumps(auth))
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def fan_out(
    session_ids: list[str],
    *,
    claude_blob: dict | None,
    codex_auth: dict | None,
    on_written: Callable[[str], None],
    expected_sub_id: str | None = None,
) -> None:
    """Deliver a freshly rotated token to every listed session's credential
    file. Deduped per credential DIRECTORY (scope dirs are shared across a
    scope's sessions). Local writes are synchronous; satellite writes are
    scheduled on the captured loop and ack-gated. ``on_written(session_id)``
    fires per session once its file landed. Sync — safe from pool threads.

    ``expected_sub_id`` guards against cross-account clobber: a session whose
    pool binding is no longer that subscription — it was re-homed by a
    selection change or released between the caller's snapshot and this write
    — is dropped, so a stale rotation can't overwrite a just-rebound
    credential file. Best-effort (re-checked again at satellite dispatch): a
    push already in flight can still land after a re-home; the next rotation
    of the session's current subscription repairs the file."""
    if expected_sub_id is not None:
        from services.engines import subscription_pool as _pool
        session_ids = [
            sid for sid in session_ids
            if _pool.get_session_subscription(sid) == expected_sub_id
        ]
    # Group sessions by their (deduped) credential directory.
    local: dict[str, tuple[CredentialFileTarget, list[str]]] = {}
    remote: dict[tuple[str, str, str], tuple[CredentialFileTarget, list[str]]] = {}
    for sid in session_ids:
        t = session_target(sid)
        if t is None:
            continue  # no credential file (API key / pre-restart session)
        if t.machine_id:
            key = (t.machine_id, t.agent_name, t.dir_relative)
            remote.setdefault(key, (t, []))[1].append(sid)
        else:
            local.setdefault(t.host_dir, (t, []))[1].append(sid)

    for host_dir, (t, sids) in local.items():
        try:
            if t.kind == "codex":
                if codex_auth is None:
                    continue
                write_codex_auth_file(Path(host_dir), codex_auth)
            else:
                if claude_blob is None:
                    continue
                write_claude_credentials_file(Path(host_dir), claude_blob)
        except OSError:
            logger.exception("fan-out: local write failed for %s", host_dir)
            continue
        for sid in sids:
            on_written(sid)
    if local:
        logger.info("fan-out: rewrote %d local credential dir(s)", len(local))

    if not remote:
        return
    if _loop is None or _loop.is_closed():
        logger.warning(
            "fan-out: no event loop captured — %d remote credential dir(s) "
            "skipped (sessions repair via 401-recovery on their next turn)",
            len(remote),
        )
        return
    for (machine_id, agent_name, dir_relative), (t, sids) in remote.items():
        # The push carries the FULL file payload (the satellite writes it
        # verbatim — same shape as the start_session credentials_json /
        # auth_json payloads).
        if t.kind == "codex":
            content = codex_auth
        else:
            content = {"claudeAiOauth": claude_blob} if claude_blob else None
        if content is None:
            continue
        asyncio.run_coroutine_threadsafe(
            _push_remote(machine_id, agent_name, dir_relative, t.kind,
                         content, sids, on_written, expected_sub_id),
            _loop,
        )


async def _push_remote(
    machine_id: str,
    agent_name: str,
    dir_relative: str,
    kind: str,
    content: dict,
    session_ids: list[str],
    on_written: Callable[[str], None],
    expected_sub_id: str | None = None,
) -> None:
    """Push one credential file to a satellite and ack-gate the snapshot
    update. Failure is logged and left to the backstops (Claude's 401-recovery
    poll window / codex's guarded reload after the next successful push)."""
    if expected_sub_id is not None:
        # Re-check at dispatch: the push was scheduled from a pool thread and a
        # selection-change rebind may have re-homed these sessions meanwhile.
        from services.engines import subscription_pool as _pool
        session_ids = [
            sid for sid in session_ids
            if _pool.get_session_subscription(sid) == expected_sub_id
        ]
        if not session_ids:
            return
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if not cm.is_connected(machine_id):
        logger.warning(
            "fan-out: satellite %s offline — credential push skipped for %s",
            machine_id[:8], dir_relative,
        )
        return
    try:
        await cm.send_command(machine_id, {
            "type": "credentials_update",
            "agent_slug": agent_name,
            "dir_relative": dir_relative,
            "kind": kind,
            "content": content,
        }, timeout=15.0)
    except Exception:
        logger.exception(
            "fan-out: credentials_update failed for %s on %s",
            dir_relative, machine_id[:8],
        )
        return
    for sid in session_ids:
        on_written(sid)
    logger.info(
        "fan-out: pushed %s credentials to %s:%s (%d session(s))",
        kind, machine_id[:8], dir_relative, len(session_ids),
    )


# ---------------------------------------------------------------------------
# Token-freshness worker
# ---------------------------------------------------------------------------

async def _tick() -> None:
    from services.engines import subscription_pool as pool
    try:
        # Selection-change convergence: re-home sessions bound to delisted
        # subscriptions BEFORE freshening, so the pass below keeps the account
        # each session will actually keep using — and so a rebind whose write
        # couldn't land (satellite offline, no replacement yet) retries every
        # tick until it converges.
        await asyncio.to_thread(pool.rebind_delisted_sessions)
    except Exception:
        logger.exception("selection rebind pass failed")
    try:
        # Scope rebalance AFTER the rebind pass (it reads the bindings the
        # rebind just settled): the drift check every tick, and the retry
        # loop for reactive moves whose fan-out couldn't land.
        await asyncio.to_thread(pool.rebalance_scopes)
    except Exception:
        logger.exception("scope rebalance pass failed")
    for sub_id in pool.bound_oauth_subscription_ids():
        try:
            await asyncio.to_thread(
                pool.ensure_fresh_and_fan_out, sub_id,
                pool.TURN_MIN_TOKEN_RUNWAY_MS,
            )
        except Exception:
            logger.exception("freshness tick failed for sub %s", sub_id[:8])


async def _worker_loop() -> None:
    while True:
        await asyncio.sleep(_FRESHNESS_INTERVAL_S)
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("token-freshness tick crashed")


_worker_task: asyncio.Task | None = None


def start_worker() -> None:
    """Start the freshness loop + capture the loop for remote pushes (idempotent)."""
    global _worker_task, _loop
    _loop = asyncio.get_event_loop()
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = _loop.create_task(_worker_loop(), name="token-freshness-worker")
    logger.info(
        "token-freshness worker started (interval=%ss)", _FRESHNESS_INTERVAL_S,
    )


async def stop_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _worker_task
    _worker_task = None
