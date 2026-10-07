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

from core.credentials import mcp_gateway

logger = logging.getLogger("claude-proxy.mcp-gateway-push")

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
            pushed_at=time.monotonic(), lease_s=lease,
            generation=_generation(cm, entry.machine_id),
        )
    return ok


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


async def tick(*, cm=None, now: float | None = None) -> int:
    """One pass: every registered session's credentials are re-resolved
    off the loop and pushed again when the value changed (the refresh
    worker rewrote the file), the machine reconnected, or the lease is
    within ``RENEW_BEFORE_S`` of its end. The registry is checked again
    on the loop before each send, so a push never lands after a purge."""
    cm = cm or _manager()
    now = time.monotonic() if now is None else now
    pushed = 0
    for session_id, entry in list(_registry.items()):
        creds = mcp_gateway.credentials_of(session_id)
        gen = _generation(cm, entry.machine_id)
        for key, cred in creds.items():
            if cred.proxy_local:
                continue
            res = await asyncio.to_thread(mcp_gateway.resolve_credential, cred)
            if _registry.get(session_id) is not entry:
                break
            if isinstance(res, mcp_gateway.Refusal):
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
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "mcp-gateway: purge of %s ran off the loop; its tokens on machine %s "
            "wait for the lease to lapse (no wipe sent)", session_id[:8], entry.machine_id[:8],
        )
        return
    loop.create_task(wipe_session(session_id, entry))


async def reprovision_adopted(session_id: str, machine_id: str, *, cm=None) -> int:
    """A session re-adopted after a proxy restart: rebuild exactly the
    gateway credentials its descriptor names (by reference, the values
    re-resolved), provision them into the broker and push them again. The
    count pushed; nothing when the session left no descriptor. A machine
    that does not run the gateway (``satellite_supports_mcp_gateway``, the
    start path's gate) is pushed nothing and the session is not registered:
    its config keeps the inline shape, and an old satellite acks no frame
    of this kind, so every tick would wait out the ack timeout."""
    from core.credentials import mcp_broker
    doc = await asyncio.to_thread(mcp_gateway.read_descriptor, session_id)
    if not doc or not doc.get("credentials"):
        return 0
    creds: dict[str, mcp_gateway.GatewayCredential] = dict(doc["credentials"])
    values = await asyncio.to_thread(
        mcp_gateway.static_values_for, doc.get("agent", ""), doc.get("user_sub", ""),
        doc.get("task_scope", ""), creds,
    )
    for key, cred in creds.items():
        if cred.token_ref is None:
            cred.value = values.get(key, "")
    bundles = dict(mcp_broker.bundles_of(session_id))
    for key, cred in creds.items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.gateway = cred
        bundles[key] = bundle
    mcp_broker.provision(session_id, bundles)
    token_hash = str(doc.get("token_hash") or "")
    if not token_hash:
        return 0
    machine = machine_id or doc.get("machine_id", "")
    cm = cm or _manager()
    if cm.satellite_supports_mcp_gateway(machine) is not True:
        return 0
    return await push_session(session_id, machine, token_hash, cm=cm)
