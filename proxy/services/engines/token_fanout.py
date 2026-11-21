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
these files are deliberately excluded from the generic file sync). A push
that finds the machine away, or fails under it, is owed: the machine's next
sign-in pushes the account's current sign-in to those sessions
(``machine_signed_in``, called by the connection's registration). The
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
import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.execution_layer import CredentialFileSpec

logger = logging.getLogger("otodock.token-fanout")

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

# One lock per credential folder: a rotation of an account and a move that
# re-homes the folder's sessions to another account check the binding, write
# the file and swap the binding as one step, so the second of two never writes
# over the first with a binding that already changed. Local folders take a
# thread lock, a machine's folder an asyncio lock held across its push.
_dir_locks_guard = threading.Lock()
_dir_locks: dict[str, threading.Lock] = {}
_push_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

# The sessions a machine's push did not reach (the machine was away, or the
# push failed under it), per machine. Each gets its account's current sign-in
# when the machine signs in again (``machine_signed_in``): the vendor ended
# the token their files hold, and the five-minute worker comes later than a
# turn can. Guarded by ``_targets_lock``: a session's end takes it off.
_missed: dict[str, set[str]] = {}


def on_loop(callback: Callable[[], None]) -> bool:
    """Run ``callback`` on the platform's event loop: at once when the caller
    is on it, else handed to the loop the worker captured (from a pool
    thread). False when there is no loop (tests, scripts)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if _loop is None or _loop.is_closed():
            return False
        _loop.call_soon_threadsafe(callback)
        return True
    callback()
    return True


def _dir_lock(host_dir: str) -> threading.Lock:
    with _dir_locks_guard:
        return _dir_locks.setdefault(host_dir, threading.Lock())


@contextlib.contextmanager
def _folder_lock(host_dir: str):
    """Hold the folder's lock: the one the map holds once acquired, so a
    writer that took the lock before its last session's end pruned it never
    works under an orphan while the next writer takes a new one."""
    while True:
        lock = _dir_lock(host_dir)
        lock.acquire()
        if _dir_locks.get(host_dir) is lock:
            break
        lock.release()
    try:
        yield lock
    finally:
        lock.release()


def _newer_account_in(folder: tuple[str, str, str], session_ids: list[str], layer: str,
                      expected_sub_id: str | None) -> bool:
    """Whether another session of the folder was bound to an account other
    than ``expected_sub_id`` later than every session being written
    (``bound_since``), for an engine that does not take a sign-in in place:
    after a refusal one chat restarted on a new account while its neighbour
    still waits on the refused one. The folder carries the newer account, and
    a rotation of the old one must not write over it (the newcomer would be
    "signed in to another account" at its next refresh). The newcomer's own
    rotations are written."""
    if expected_sub_id is None:
        return False
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(layer)
    if caps is None or caps.auth.sign_in_in_place:
        return False
    with _targets_lock:
        neighbours = [sid for sid, t in _targets.items() if _folder(t) == folder and sid not in session_ids]
    from services.engines import subscription_pool as _pool
    writing = max((_pool.bound_since(sid) for sid in session_ids), default=0.0)
    return any(_pool.get_session_subscription(sid) not in (None, expected_sub_id)
               and _pool.bound_since(sid) > writing for sid in neighbours)


def _still_bound(session_ids: list[str], expected_sub_id: str | None) -> list[str]:
    if expected_sub_id is None:
        return session_ids
    from services.engines import subscription_pool as _pool
    return [sid for sid in session_ids if _pool.get_session_subscription(sid) == expected_sub_id]


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
    at spawn, only when it actually wrote one: a session whose key rides
    its environment never registers, a Codex session on a key does and is
    never written to, since a key does not rotate)."""
    with _targets_lock:
        _targets[session_id] = target


def _folder(t: CredentialFileTarget) -> tuple[str, str, str]:
    return (t.machine_id, t.agent_name, t.dir_relative) if t.machine_id else ("", "", t.host_dir)


def unregister_session_target(session_id: str) -> None:
    """Forget a session's file; the folder's lock goes with the last session
    that names the folder (a lock held right now stays)."""
    with _targets_lock:
        target = _targets.pop(session_id, None)
        if target is None:
            return
        _settle_missed_locked(target.machine_id, [session_id])
        folder = _folder(target)
        if any(_folder(t) == folder for t in _targets.values()):
            return
    if target.machine_id:
        push = _push_locks.get(folder)
        if push is not None and not push.locked():
            _push_locks.pop(folder, None)
        return
    with _dir_locks_guard:
        lock = _dir_locks.get(target.host_dir)
        if lock is not None and not lock.locked():
            _dir_locks.pop(target.host_dir, None)


def session_target(session_id: str) -> CredentialFileTarget | None:
    with _targets_lock:
        return _targets.get(session_id)


def _settle_missed_locked(machine_id: str, session_ids: list[str]) -> None:
    """These sessions are owed no push any more. Under ``_targets_lock``."""
    owed = _missed.get(machine_id)
    if owed is None:
        return
    owed.difference_update(session_ids)
    if not owed:
        _missed.pop(machine_id, None)


def _note_missed(machine_id: str, session_ids: list[str]) -> None:
    """A push did not reach these sessions of a machine: owed at its next
    sign-in, each for as long as it is registered."""
    with _targets_lock:
        live = [sid for sid in session_ids if sid in _targets]
        if live:
            _missed.setdefault(machine_id, set()).update(live)


def owed_while_signed_in() -> frozenset[str]:
    """The sessions a push did not reach whose machine is signed in now. No
    sign-in comes to pay those (``machine_signed_in``), so the worker's round
    names them to the pool, which sends each in the round's one list
    (``ensure_fresh_and_fan_out``). A machine that is away is asked nothing:
    its sign-in pays."""
    with _targets_lock:
        noted = {machine_id: frozenset(sids) for machine_id, sids in _missed.items()}
    if not noted:
        return frozenset()
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    return frozenset(sid for machine_id, sids in noted.items() if cm.is_connected(machine_id) for sid in sids)


async def machine_signed_in(machine_id: str) -> None:
    """A machine signed in: each of its sessions that missed a push gets its
    account's current sign-in now (``fan_out_current_token``, the pool's
    push of the stored token, through ``fan_out`` as every push). Nothing is
    asked of the store when the machine missed none, so a machine that
    reconnects every second costs a lookup. A session that ended since is
    owed nothing, one bound to no account now is asked of nobody, and a push
    that fails again is owed at the sign-in after."""
    with _targets_lock:
        owed = _missed.pop(machine_id, None) or set()
        live = sorted(sid for sid in owed
                      if (t := _targets.get(sid)) is not None and t.machine_id == machine_id)
    if not live:
        return
    from services.engines import subscription_pool as pool
    by_account: dict[str, list[str]] = {}
    for sid in live:
        sub_id = pool.bound_attribution(sid)[0]
        if sub_id:
            by_account.setdefault(sub_id, []).append(sid)
    logger.info("fan-out: %s signed in, %d session(s) get the sign-in they missed", machine_id[:8], len(live))
    for sub_id, sids in by_account.items():
        try:
            await asyncio.to_thread(pool.fan_out_current_token, sub_id, sids)
        except Exception:
            # One account's push must not keep the next, and it stays owed.
            logger.exception("fan-out: the sign-in %s missed was not pushed for %s", machine_id[:8], sub_id[:8])
            _note_missed(machine_id, sids)


# ---------------------------------------------------------------------------
# The credential file writer — one for every engine, named by its spec
# ---------------------------------------------------------------------------

def write_credential_file(config_dir: Path, filename: str, payload: dict) -> None:
    """Write a session's credential file — the full file content the engine's
    ``credential_file_payload`` produced (refresh token already neutralized
    there; the pool is the sole rotator) — 0600, into its scope config dir.
    Never through a link the agent planted at the name (the dir is
    agent-writable): the proxy would write the token over the link's target.
    A session's home is made by its start alone (``session_home.prepare``):
    a write after its close finds no folder and makes none. Another missing
    folder in an agent's folder is made beneath the agent's real folder
    (``core.agent_folder``), with no link followed."""
    from core.agent_folder import agent_holding, beneath_agent
    from core.sandbox import session_home
    from core.sandbox.session_config_dir import write_no_follow
    if not session_home.is_home(config_dir):
        agent = agent_holding(config_dir)
        if agent:
            from services.infra import safe_fs
            safe_fs.mkdirs_beneath(*beneath_agent(config_dir, agent))
        else:
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
    session_ids = _still_bound(session_ids, expected_sub_id)
    spec = credential_file_spec(layer)
    # Group sessions by their (deduped) credential directory.
    local: dict[str, tuple[CredentialFileTarget, list[str]]] = {}
    remote: dict[tuple[str, str, str], tuple[CredentialFileTarget, list[str]]] = {}
    for sid in session_ids:
        t = session_target(sid)
        if t is None:
            continue  # no credential file (a key in the environment / pre-restart session)
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

    from core.sandbox import session_home
    for host_dir, (_t, sids) in local.items():
        with _folder_lock(host_dir):
            sids = _still_bound(sids, expected_sub_id)
            if not sids:
                continue
            if session_home.is_home(host_dir) and not os.path.isdir(host_dir):
                logger.info("fan-out: the session's home %s is gone: not written", host_dir)
                continue
            if _newer_account_in(("", "", host_dir), sids, layer, expected_sub_id):
                logger.info("fan-out: %s carries a newer account, its sessions on %s are closing: not written",
                            host_dir, (expected_sub_id or "")[:8])
                continue
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
                         payload, sids, on_written, expected_sub_id, layer=layer),
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
    *,
    layer: str = "",
) -> None:
    """Push one credential file to a satellite and ack-gate the snapshot
    update. ``kind`` is the engine's wire kind — ``"claude"`` / ``"codex"``,
    the values the released satellites clamp on. Failure is logged and left
    to the backstops (Claude's 401-recovery poll window / codex's guarded
    reload after the next successful push)."""
    key = (machine_id, agent_name, dir_relative)
    while True:
        lock = _push_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if _push_locks.get(key) is not lock:
                continue   # pruned while this push waited: the folder's current lock
            await _push_remote_locked(machine_id, agent_name, dir_relative, kind, content,
                                      session_ids, on_written, expected_sub_id, layer)
            return


async def _push_remote_locked(
    machine_id: str,
    agent_name: str,
    dir_relative: str,
    kind: str,
    content: dict,
    session_ids: list[str],
    on_written: Callable[[str], None],
    expected_sub_id: str | None,
    layer: str,
) -> None:
    # Re-check under the folder's lock: the push was scheduled from a pool
    # thread, and a rebind or a rotation may have changed the binding since.
    session_ids = _still_bound(session_ids, expected_sub_id)
    if not session_ids:
        return
    if _newer_account_in((machine_id, agent_name, dir_relative), session_ids, layer, expected_sub_id):
        logger.info("fan-out: %s on %s carries a newer account: not pushed", dir_relative, machine_id[:8])
        return
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if not cm.is_connected(machine_id):
        logger.warning(
            "fan-out: satellite %s offline — credential push skipped for %s",
            machine_id[:8], dir_relative,
        )
        _note_missed(machine_id, session_ids)
        return
    try:
        ack = await cm.send_command(machine_id, {
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
        _note_missed(machine_id, session_ids)
        return
    with _targets_lock:
        _settle_missed_locked(machine_id, session_ids)
    if isinstance(ack, dict) and ack.get("note"):
        # "session gone, not written": the machine wrote nothing.
        logger.info("fan-out: %s on %s: %s", dir_relative, machine_id[:8], ack["note"])
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
            # With the sessions a failed push still owes on a machine that
            # stayed signed in: read anew for each account, so one a sign-in
            # paid a moment ago is not named.
            await asyncio.to_thread(
                pool.ensure_fresh_and_fan_out, sub_id,
                pool.TURN_MIN_TOKEN_RUNWAY_MS, owed_while_signed_in(),
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
        from storage.billing import subscription_status, subscription_store as _store
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
