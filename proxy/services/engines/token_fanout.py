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

WHERE a session's file lives is the target the layer registered at spawn;
WHAT the file is called and how it is named on the satellite wire is the
engine's declaration (``LayerCapabilities.auth.credential_file`` — the
``CredentialFileSpec`` with the wire kind, the scope dir name, the filename
and the start-payload key). The satellite clamps exactly that triple by the
wire kind (``satellite/sessions/session_manager.py``), so the two values the
released fleet knows — ``claude`` / ``codex`` — are frozen vocabulary the
descriptor declares and this module never derives.

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

from core.execution_layer import CredentialFileSpec

logger = logging.getLogger("claude-proxy.token-fanout")

_FRESHNESS_INTERVAL_S = 300


@dataclass(frozen=True)
class CredentialFileTarget:
    """Where one session's credential file lives.

    ``layer`` is the session's engine (its execution path); the file's name,
    wire kind and scope-dir name are the engine's declaration
    (``credential_file_spec``). Local sessions carry the absolute
    ``host_dir``; remote sessions carry ``machine_id`` + ``agent_name`` +
    ``dir_relative`` (agent-dir-rooted, the satellite resolves its own tree).
    """
    layer: str                 # execution path: "claude-code-cli" | "codex-cli" | …
    machine_id: str = ""       # "" = local proxy host
    host_dir: str = ""         # local: absolute scope config dir
    agent_name: str = ""       # remote: agent slug
    dir_relative: str = ""     # remote: e.g. "users/alice/.claude"


_targets: dict[str, CredentialFileTarget] = {}  # session_id → target
_targets_lock = threading.Lock()

# Event loop for scheduling satellite pushes from pool worker threads —
# captured by start_worker() at startup. None (tests / pre-startup) skips
# remote pushes with a log line.
_loop: asyncio.AbstractEventLoop | None = None


def credential_file_spec(layer: str) -> CredentialFileSpec:
    """The engine's credential-file declaration. Fails closed: a target for
    an engine that is not registered, or for one that delivers credentials
    by env, is a bug at the registration site, not a runtime condition."""
    from core.session.session_manager import capabilities_for_path
    spec = capabilities_for_path(layer).auth.credential_file
    if spec is None:
        raise ValueError(f"{layer} delivers credentials by env — it has no credential file")
    return spec


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
# The credential file writer — one for every engine, named by its spec
# ---------------------------------------------------------------------------

def write_credential_file(config_dir: Path, filename: str, payload: dict) -> None:
    """Write a session's credential file — the full file content the engine's
    ``credential_file_payload`` produced (refresh token already neutralized
    there; the pool is the sole rotator) — 0600, into its scope config dir.
    Never through a link the agent planted at the name (the dir is
    agent-writable): the proxy would write the token over the link's target."""
    from core.sandbox.session_config_dir import write_no_follow
    config_dir.mkdir(parents=True, exist_ok=True)
    write_no_follow(config_dir / filename, json.dumps(payload).encode(), mode=0o600)


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def fan_out(
    session_ids: list[str],
    *,
    layer: str,
    payload: dict,
    on_written: Callable[[str], None],
    expected_sub_id: str | None = None,
) -> None:
    """Deliver one credential-file payload to every listed session's file.

    The sessions of one subscription run one engine, so one ``payload`` (the
    full file content, from ``layer``'s ``credential_file_payload``) serves
    them all; a target registered for another engine is skipped with a
    warning — impossible by construction (a session bound to a Claude row is
    a Claude session), and a silent wrong-format write is the worse failure.
    Deduped per credential DIRECTORY (scope dirs are shared across a scope's
    sessions). Local writes are synchronous; satellite writes are scheduled
    on the captured loop and ack-gated. ``on_written(session_id)`` fires per
    session once its file landed. Sync — safe from pool threads.

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
    spec = credential_file_spec(layer)
    # Group sessions by their (deduped) credential directory.
    local: dict[str, tuple[CredentialFileTarget, list[str]]] = {}
    remote: dict[tuple[str, str, str], tuple[CredentialFileTarget, list[str]]] = {}
    for sid in session_ids:
        t = session_target(sid)
        if t is None:
            continue  # no credential file (API key / pre-restart session)
        if t.layer != layer:
            logger.warning(
                "fan-out: session %s is registered for %s, not %s — skipped",
                sid[:8], t.layer, layer,
            )
            continue
        if t.machine_id:
            key = (t.machine_id, t.agent_name, t.dir_relative)
            remote.setdefault(key, (t, []))[1].append(sid)
        else:
            local.setdefault(t.host_dir, (t, []))[1].append(sid)

    for host_dir, (_t, sids) in local.items():
        try:
            write_credential_file(Path(host_dir), spec.filename, payload)
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
    for (machine_id, agent_name, dir_relative), (_t, sids) in remote.items():
        # The push carries the FULL file payload (the satellite writes it
        # verbatim — the same content the start_session payload carried).
        asyncio.run_coroutine_threadsafe(
            _push_remote(machine_id, agent_name, dir_relative, spec.wire_kind,
                         payload, sids, on_written, expected_sub_id),
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
    update. ``kind`` is the engine's wire kind — ``"claude"`` / ``"codex"``,
    the values the released satellites clamp on. Failure is logged and left
    to the backstops (Claude's 401-recovery poll window / codex's guarded
    reload after the next successful push)."""
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
        # Provider windows FIRST: a fresh reading of each account's session /
        # quota state lets the rebind and rebalance passes below converge in
        # this tick instead of the next. Runs before the boot-grace return
        # too, so a restart never starves the idle accounts' samples.
        from services.engines import subscription_windows as _windows
        await _windows.poll_due()
    except Exception:
        logger.exception("window poll pass failed")
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
    bound = set(pool.bound_oauth_subscription_ids())
    for sub_id in bound:
        try:
            await asyncio.to_thread(
                pool.ensure_fresh_and_fan_out, sub_id,
                pool.TURN_MIN_TOKEN_RUNWAY_MS,
            )
        except Exception:
            logger.exception("freshness tick failed for sub %s", sub_id[:8])
    # Idle rows too: an OAuth account nobody is running would otherwise only
    # discover a dead login grant at the owner's next chat, and its stored
    # grant-expiry field would go stale. Guards: never during the post-restart
    # boot grace (a surviving satellite session's row LOOKS unbound until it
    # re-announces), and rows with a persisted session binding count as bound
    # (their fan-out target may be live but unannounced). Refresh only when
    # the access token is inside the same runway; ensure_fresh_and_fan_out
    # no-ops otherwise and its status short-circuit skips expired rows.
    if pool.within_boot_grace():
        return
    try:
        from storage.billing import subscription_status
        from storage.billing import subscription_store as _store
        persisted = await asyncio.to_thread(_store.list_persisted_binding_sub_ids)
        rows = await asyncio.to_thread(_store.list_subscriptions)
    except Exception:
        logger.exception("unbound freshness enumeration failed")
        return
    for sub in rows:
        sub_id = sub.get("id") or ""
        if (sub.get("auth_type") != "oauth" or sub.get("status") != subscription_status.ACTIVE
                or sub_id in bound or sub_id in persisted):
            continue
        try:
            await asyncio.to_thread(
                pool.ensure_fresh_and_fan_out, sub_id,
                pool.TURN_MIN_TOKEN_RUNWAY_MS,
            )
        except Exception:
            logger.exception("unbound freshness tick failed for sub %s", sub_id[:8])


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
