"""The proxy's half of the satellite-local credential gateway: the tokens a
machine session's HTTP MCPs need are pushed to the machine before the
session spawns, renewed by a tick, and wiped at close.

A machine holds each token in memory under a lease (``mcp_gateway.LEASE_S``)
keyed by the hash of the session's own token and the MCP; the proxy's
refresh worker is the only refresher (it rewrites the token file, the tick
reads the new value and pushes it). The registry here is memory only; a
session re-adopted after a proxy restart is re-provisioned from its
descriptor (``mcp_gateway.read_descriptor``) and pushed again.

The frames are built here, under ``core/remote``, with the rest of the
satellite protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

from core.background import spawn
from core.credentials import mcp_gateway

logger = logging.getLogger("otodock.mcp-gateway-push")

TICK_S = 60.0
_ACK_TIMEOUT_S = 10.0


@dataclass
class _Pushed:
    value_hash: str
    pushed_at: float
    lease_s: float
    generation: float


@dataclass
class _Entry:
    machine_id: str
    token_hash: str
    pushed: dict[str, _Pushed] = field(default_factory=dict)


_registry: dict[str, _Entry] = {}


def token_hash_of(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _manager():
    from core.remote.satellite_connection import get_connection_manager
    return get_connection_manager()


def _generation(cm, machine_id: str) -> float:
    conn = cm.get_connection(machine_id)
    return float(getattr(conn, "connected_at", 0.0) or 0.0) if conn else 0.0


def register(session_id: str, machine_id: str, token_hash: str) -> None:
    _registry[session_id] = _Entry(machine_id=machine_id, token_hash=token_hash)


def registered(session_id: str) -> bool:
    return session_id in _registry


def forget(session_id: str) -> _Entry | None:
    return _registry.pop(session_id, None)


async def _send(cm, machine_id: str, msg: dict) -> bool:
    try:
        await cm.send_command(machine_id, msg, timeout=_ACK_TIMEOUT_S)
        return True
    except Exception as e:
        logger.info("mcp-gateway: %s to machine %s not acknowledged: %s", msg.get("type"),
                    machine_id[:8], type(e).__name__)
        return False


async def _push_one(cm, entry: _Entry, session_id: str, key: str, res: mcp_gateway.Resolved) -> bool:
    lease = mcp_gateway.LEASE_S
    if res.expires_in is not None:
        lease = max(0.0, min(lease, res.expires_in))
    # The connection the frame goes out on: one that drops and comes back
    # before the ack must not be recorded as holding the value.
    generation = _generation(cm, entry.machine_id)
    ok = await _send(cm, entry.machine_id, {
        "type": "mcp_gateway_token",
        "session_id": session_id,
        "token_hash": entry.token_hash,
        "mcp": key,
        "header": res.header,
        "value": res.value,
        "expires_in": lease,
        "upstream": res.upstream + res.path,
    })
    if ok:
        entry.pushed[key] = _Pushed(
            value_hash=hashlib.sha256(res.value.encode()).hexdigest(),
            pushed_at=time.monotonic(), lease_s=lease, generation=generation,
        )
    return ok


async def _revoke_one(cm, entry: _Entry, session_id: str, key: str,
                      cred: mcp_gateway.GatewayCredential,
                      refusal: mcp_gateway.Refusal | None = None) -> bool:
    """A credential the proxy now refuses leaves the machine at once: a
    token frame with a lease of zero (a satellite at 0.5.132 to 0.5.138
    stores it as already over and never serves it, 0.5.139 removes the
    entry, 0.5.140 also keeps the refusal's ``kind`` and ``reason`` to
    answer the session's next request with), built from the credential
    itself since nothing resolved. The key leaves the record only when the
    machine acknowledged, so a machine offline at this moment is told at
    its reconnect (``tick_machine_soon``)."""
    ok = await _send(cm, entry.machine_id, {
        "type": "mcp_gateway_token",
        "session_id": session_id,
        "token_hash": entry.token_hash,
        "mcp": key,
        "header": cred.header,
        "value": "-",
        "expires_in": 0,
        "upstream": cred.upstream + cred.path,
        "kind": refusal.reason if refusal else "",
        "reason": refusal.detail if refusal else "",
    })
    if ok:
        entry.pushed.pop(key, None)
    return ok


_tick_lock: asyncio.Lock | None = None
_tick_lock_loop = None


def _lock() -> asyncio.Lock:
    """The tick's lock, made for the running loop (a test runs several)."""
    global _tick_lock, _tick_lock_loop
    loop = asyncio.get_running_loop()
    if _tick_lock is None or _tick_lock_loop is not loop:
        _tick_lock, _tick_lock_loop = asyncio.Lock(), loop
    return _tick_lock


def tick_soon() -> None:
    """Run a tick now, as a task on the running loop: the actions that make
    the proxy refuse a credential (a disconnect, an allowlist row removed)
    call this so a machine stops using it within seconds instead of at the
    60 s loop. Nothing happens off the loop; never raises."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    spawn(_tick_logged(), name="tick_logged", owner="mcp_gateway_push")


async def _tick_logged() -> None:
    try:
        await tick()
    except Exception:
        logger.exception("mcp-gateway: immediate push tick failed")


async def push_session(session_id: str, machine_id: str, token_hash: str, *, cm=None) -> int:
    """Resolve every gateway credential of ``session_id`` off the loop and
    push each to the machine; the count pushed. A credential the gateway
    refuses (a dead account, a removed row) is not pushed and the machine
    answers the MCP with the refusal it lacks a token for."""
    cm = cm or _manager()
    creds = mcp_gateway.credentials_of(session_id)
    if not creds:
        return 0
    register(session_id, machine_id, token_hash)
    entry = _registry[session_id]
    pushed = 0
    for key, cred in creds.items():
        if cred.proxy_local:
            continue  # a sidecar's token stays on the proxy (the tunnel's forward)
        res = await asyncio.to_thread(mcp_gateway.resolve_credential, cred)
        if isinstance(res, mcp_gateway.Refusal):
            logger.info("mcp-gateway: %s of session %s not pushed: %s", key, session_id[:8],
                        res.reason)
            continue
        if session_id not in _registry:
            break  # purged meanwhile: nothing lands after a wipe
        if await _push_one(cm, entry, session_id, key, res):
            pushed += 1
    return pushed


async def wipe_session(session_id: str, entry: _Entry | None = None, *, cm=None) -> None:
    entry = entry or _registry.get(session_id)
    if entry is None:
        return
    cm = cm or _manager()
    await _send(cm, entry.machine_id, {
        "type": "mcp_gateway_wipe", "session_id": session_id, "token_hash": entry.token_hash,
    })


async def tick(*, cm=None, now: float | None = None, machine_id: str = "") -> int:
    """One pass: every registered session's credentials (of ``machine_id``
    alone when given) are re-resolved off the loop and pushed again when
    the value changed (the refresh worker rewrote the file), the machine
    reconnected, or the lease is within ``RENEW_BEFORE_S`` of its end; one
    the gateway now refuses (a disconnected account, an allowlist row
    removed) is revoked on the machine when it had been pushed. The
    registry is checked again on the loop before each send, so a push never
    lands after a purge; one tick runs at a time (the loop's and an
    immediate one never interleave)."""
    async with _lock():
        return await _tick(cm or _manager(), time.monotonic() if now is None else now,
                           machine_id)


# machine id -> its reconnect tick in flight; a reconnect while one runs
# asks for one more pass instead of a second task.
_machine_ticks: dict[str, asyncio.Task] = {}
_machine_again: set[str] = set()


def tick_machine_soon(machine_id: str) -> None:
    """A machine reconnected: tick its sessions now, so a credential revoked
    while its link was down stops there within seconds instead of at the
    next 60 s tick (the gateway on the machine serves from memory with no
    link). Coalesced per machine: one tick runs, at most one more follows,
    however often the machine reconnects meanwhile. Never raises."""
    if not machine_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    running = _machine_ticks.get(machine_id)
    if running is not None and not running.done():
        _machine_again.add(machine_id)
        return
    _machine_ticks[machine_id] = loop.create_task(_machine_tick(machine_id))


async def _machine_tick(machine_id: str) -> None:
    # No await between the last check of the flag and the task's removal,
    # so a reconnect lands either in this loop or in a task of its own.
    try:
        while True:
            _machine_again.discard(machine_id)
            await tick(machine_id=machine_id)
            if machine_id not in _machine_again:
                break
    except Exception:
        logger.exception("mcp-gateway: reconnect push tick for machine %s failed", machine_id[:8])
    finally:
        if _machine_ticks.get(machine_id) is asyncio.current_task():
            _machine_ticks.pop(machine_id, None)


async def _tick(cm, now: float, machine_id: str = "") -> int:
    pushed = 0
    for session_id, entry in list(_registry.items()):
        if machine_id and entry.machine_id != machine_id:
            continue
        creds = mcp_gateway.credentials_of(session_id)
        gen = _generation(cm, entry.machine_id)
        for key, cred in creds.items():
            if cred.proxy_local:
                continue
            res = await asyncio.to_thread(mcp_gateway.resolve_credential, cred)
            if _registry.get(session_id) is not entry:
                break
            if isinstance(res, mcp_gateway.Refusal):
                if key in entry.pushed:
                    logger.info("mcp-gateway: %s of session %s revoked on machine %s: %s",
                                key, session_id[:8], entry.machine_id[:8], res.reason)
                    await _revoke_one(cm, entry, session_id, key, cred, res)
                continue
            prior = entry.pushed.get(key)
            due = (
                prior is None
                or prior.value_hash != hashlib.sha256(res.value.encode()).hexdigest()
                or prior.generation != gen
                or now >= prior.pushed_at + prior.lease_s - mcp_gateway.RENEW_BEFORE_S
            )
            if due and await _push_one(cm, entry, session_id, key, res):
                pushed += 1
    return pushed


async def run_push_loop() -> None:
    while True:
        await asyncio.sleep(TICK_S)
        try:
            await tick()
        except Exception:
            logger.exception("mcp-gateway: push tick failed")


def on_purge(session_id: str) -> None:
    """The broker purged the session: forget it and wipe its tokens on the
    machine, scheduled on the running loop; never raises."""
    entry = forget(session_id)
    if entry is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "mcp-gateway: purge of %s ran off the loop; its tokens on machine %s "
            "wait for the lease to lapse (no wipe sent)", session_id[:8], entry.machine_id[:8],
        )
        return
    spawn(wipe_session(session_id, entry), name="wipe_session", owner="mcp_gateway_push")


async def reprovision_adopted(session_id: str, machine_id: str, *, cm=None) -> int:
    """A session re-adopted after a proxy restart: rebuild exactly the
    gateway and command-line credentials its descriptor names (by
    reference, the values re-resolved), provision them into the broker and
    push the gateway ones again. The count pushed; nothing when the session
    left no descriptor. A machine that does not run the gateway
    (``satellite_supports_mcp_gateway``, the start path's gate) is pushed
    nothing and the session is not registered: its config keeps the inline
    shape, and an old satellite acks no frame of this kind, so every tick
    would wait out the ack timeout."""
    from core.credentials import mcp_broker
    doc = await asyncio.to_thread(mcp_gateway.read_descriptor, session_id)
    if not doc or (not doc.get("credentials") and not doc.get("cli")):
        return 0
    creds: dict[str, mcp_gateway.GatewayCredential] = dict(doc["credentials"])
    cli: dict[str, mcp_gateway.CliCredential] = dict(doc.get("cli") or {})
    values = await asyncio.to_thread(
        mcp_gateway.static_values_for, doc.get("agent", ""), doc.get("user_sub", ""),
        doc.get("task_scope", ""), creds,
    ) if creds else {}
    for key, cred in creds.items():
        if cred.token_ref is None:
            cred.value = values.get(key, "")
    bundles = dict(mcp_broker.bundles_of(session_id))
    for key, cred in creds.items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.gateway = cred
        bundles[key] = bundle
    for key, cli_cred in cli.items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.cli = cli_cred
        bundles[key] = bundle
    mcp_broker.provision(session_id, bundles)
    token_hash = str(doc.get("token_hash") or "")
    if not token_hash or not creds:
        return 0
    machine = machine_id or doc.get("machine_id", "")
    cm = cm or _manager()
    if cm.satellite_supports_mcp_gateway(machine) is not True:
        return 0
    return await push_session(session_id, machine, token_hash, cm=cm)
