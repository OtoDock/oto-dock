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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
    except RuntimeError as e:
        logger.info("mcp-gateway: %s to machine %s not acknowledged: %s", msg.get("type"),
                    machine_id[:8], type(e).__name__)
        return False


async def _push_one(cm, entry: _Entry, session_id: str, key: str, res: mcp_gateway.Resolved) -> bool:
    lease: float = mcp_gateway.LEASE_S
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


def _exit_resolve(cred) -> mcp_gateway.Resolved | mcp_gateway.Refusal:
    """An exit entry resolved as the machine's exit will use it: the value
    with the tool's own prefix stripped, toward the first host the allowlist
    admits, in the gateway's ``Resolved`` shape so the push and the tick read
    one kind (the header is the declared one, the upstream names the host).
    Blocking."""
    from core.credentials import credential_exit
    host = credential_exit.probe_host(cred)
    if not host:
        return mcp_gateway.Refusal("host_not_allowed", "no declared host is approved")
    try:
        value, expires_in = credential_exit.exit_resolution(cred, host)
    except credential_exit.Judged as judged:
        return mcp_gateway.Refusal("exit_refused", judged.detail)
    return mcp_gateway.Resolved(header=cred.header, value=value, upstream=f"https://{host}", path="",
                                proxy_local=False, expires_in=expires_in)


def _exit_pairs(session_id: str) -> list[tuple[str, mcp_gateway.ExitCredential]]:
    """``(placeholder, entry)`` for every exit entry a session holds: the
    placeholder is the key the machine's table and this registry use."""
    return [(cred.placeholder, cred) for creds in mcp_gateway.exit_credentials_of(session_id).values()
            for cred in creds]


async def push_session(session_id: str, machine_id: str, token_hash: str, *, cm=None) -> int:
    """Resolve every gateway credential and every credential exit entry of
    ``session_id`` off the loop and push each to the machine; the count
    pushed. A credential the gateway refuses (a dead account, a removed row)
    is not pushed and the machine answers the MCP with the refusal it lacks
    a token for. A compute machine holds no gateway lease (its sessions'
    gateway requests ride the tunnel to the platform's own gateway) and
    takes the exit's values alone: its exit dials from the machine."""
    cm = cm or _manager()
    creds = {} if cm.is_compute(machine_id) else mcp_gateway.credentials_of(session_id)
    exits = _exit_pairs(session_id)
    if not creds and not exits:
        return 0
    register(session_id, machine_id, token_hash)
    entry = _registry[session_id]
    pushed = 0
    for key, cred in creds.items():
        if cred.forwarded_here:
            continue  # a sidecar's, or a narrowed one's, stays on the proxy (the tunnel's forward)
        res = await asyncio.to_thread(mcp_gateway.resolve_credential, cred)
        if isinstance(res, mcp_gateway.Refusal):
            logger.info("mcp-gateway: %s of session %s not pushed: %s", key, session_id[:8],
                        res.reason)
            continue
        if session_id not in _registry:
            break  # purged meanwhile: nothing lands after a wipe
        if await _push_one(cm, entry, session_id, key, res):
            pushed += 1
    for placeholder, exit_cred in exits:
        res = await asyncio.to_thread(_exit_resolve, exit_cred)
        if isinstance(res, mcp_gateway.Refusal):
            logger.info("mcp-gateway: the exit's %s of session %s not pushed: %s", exit_cred.key,
                        session_id[:8], res.reason)
            continue
        if session_id not in _registry:
            break
        if await _push_one(cm, entry, session_id, placeholder, res):
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
    """One pass for every machine that has registered sessions (for
    ``machine_id`` alone when given), each machine in a task of its own, and
    wait for them all: every credential of its sessions is re-resolved off
    the loop and pushed again when the value changed (the refresh worker
    rewrote the file), the machine reconnected, or the lease is within
    ``RENEW_BEFORE_S`` of its end; one the gateway now refuses (a
    disconnected account, an allowlist row removed) is revoked on the
    machine when it had been pushed. The registry is checked again on the
    loop before each send, so a push never lands after a purge. One pass
    runs at a time per machine: a machine whose pass already runs gets one
    more pass before this returns, so a revoke made meanwhile is told. A
    machine that does not answer holds up its own pass only."""
    machines = ({machine_id} if machine_id
                else {entry.machine_id for entry in _registry.values()})
    tasks = [task for task in (_pass(mid, cm, now) for mid in sorted(machines)) if task is not None]
    if not tasks:
        return 0
    counts = await asyncio.gather(*tasks, return_exceptions=True)
    return sum(n for n in counts if isinstance(n, int))


# machine id -> (loop, its pass in flight); a request while one runs asks for
# one more pass instead of a second task. The loop is kept because a test
# runs several: a task of a loop that is gone counts for nothing.
_machine_ticks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task]] = {}
_machine_again: set[str] = set()


def _pass(machine_id: str, cm=None, now: float | None = None) -> asyncio.Task | None:
    """The machine's pass: the one that runs (asked for one more), or a new
    one. None off the loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    running = _machine_ticks.get(machine_id)
    if running is not None and running[0] is loop and not running[1].done():
        _machine_again.add(machine_id)
        return running[1]
    task = loop.create_task(_machine_tick(machine_id, cm, now))
    _machine_ticks[machine_id] = (loop, task)
    return task


def tick_machine_soon(machine_id: str) -> None:
    """A machine reconnected: tick its sessions now, so a credential revoked
    while its link was down stops there within seconds instead of at the
    next 60 s tick (the gateway on the machine serves from memory with no
    link). Coalesced per machine: one pass runs, at most one more follows,
    however often the machine reconnects meanwhile. Never raises."""
    if machine_id:
        _pass(machine_id)


async def _machine_tick(machine_id: str, cm=None, now: float | None = None) -> int:
    # No await between the last check of the flag and the task's removal,
    # so a request lands either in this loop or in a task of its own.
    pushed = 0
    try:
        while True:
            _machine_again.discard(machine_id)
            pushed += await _tick(cm or _manager(), time.monotonic() if now is None else now,
                                  machine_id)
            now = None      # a pass asked for meanwhile reads the clock itself
            if machine_id not in _machine_again:
                break
    except Exception:
        logger.exception("mcp-gateway: push tick for machine %s failed", machine_id[:8])
    finally:
        held = _machine_ticks.get(machine_id)
        if held is not None and held[1] is asyncio.current_task():
            _machine_ticks.pop(machine_id, None)
    return pushed


async def _tick(cm, now: float, machine_id: str = "") -> int:
    pushed = 0
    for session_id, entry in list(_registry.items()):
        if machine_id and entry.machine_id != machine_id:
            continue
        creds = {} if cm.is_compute(entry.machine_id) else mcp_gateway.credentials_of(session_id)
        gen = _generation(cm, entry.machine_id)
        # The gateway credentials by MCP key, then the exit's entries by
        # placeholder: one rule for both (a changed value, a reconnect, a
        # lease near its end push again; a refusal revokes what was pushed).
        rows: list[tuple[str, Any, mcp_gateway.GatewayCredential]] = [
            (key, cred, cred) for key, cred in creds.items() if not cred.forwarded_here]
        from core.credentials import credential_exit
        rows.extend((placeholder, exit_cred, exit_cred.gateway_for(credential_exit.probe_host(exit_cred) or "none"))
                    for placeholder, exit_cred in _exit_pairs(session_id))
        for key, cred, shape in rows:
            resolve: Callable[[Any], mcp_gateway.Resolved | mcp_gateway.Refusal] = (
                mcp_gateway.resolve_credential if cred is shape else _exit_resolve)
            res = await asyncio.to_thread(resolve, cred)
            if _registry.get(session_id) is not entry:
                break
            if isinstance(res, mcp_gateway.Refusal):
                if key in entry.pushed:
                    logger.info("mcp-gateway: %s of session %s revoked on machine %s: %s",
                                key, session_id[:8], entry.machine_id[:8], res.reason)
                    await _revoke_one(cm, entry, session_id, key, shape, res)
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
    """Every ``TICK_S``: a pass for each machine with sessions, started and
    not waited for, so a machine that does not answer delays no other
    machine's renewals (a machine whose pass still runs is asked for one
    more)."""
    while True:
        await asyncio.sleep(TICK_S)
        try:
            for machine_id in sorted({entry.machine_id for entry in _registry.values()}):
                _pass(machine_id)
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
    gateway, command-line and tool token credentials its descriptor names (by
    reference, the values re-resolved for the identity and the chat's
    choice of account it records, and only on the account each tool was
    built on), provision them into the broker and push the gateway ones
    again. The count pushed; nothing when the session left no descriptor."""
    from core.credentials import mcp_broker
    from services.oauth.credential_resolver import choice_kw
    doc = await asyncio.to_thread(mcp_gateway.read_descriptor, session_id)
    if not doc or (not doc.get("credentials") and not doc.get("cli") and not doc.get("tokens")
                   and not doc.get("exits") and not doc.get("envs")):
        return 0
    creds: dict[str, mcp_gateway.GatewayCredential] = dict(doc["credentials"])
    cli: dict[str, mcp_gateway.CliCredential] = dict(doc.get("cli") or {})
    exits = _exits_from_descriptor(doc, cli)
    # The values resolve for the account the chat chose, as at the start:
    # the gateway credentials by MCP key, the exit entries each under its
    # own key (an MCP may declare two), through the same resolution.
    wanted: dict[str, mcp_gateway.GatewayCredential] = dict(creds)
    for key, entries in exits.items():
        for exit_cred in entries:
            if exit_cred.token_ref is None and exit_cred.cli is None and exit_cred.env_name and exit_cred.hosts:
                wanted[f"{key}\0{exit_cred.env_name}"] = exit_cred.gateway_for(exit_cred.hosts[0].lstrip("*."))
    built = await asyncio.to_thread(_built_accounts, doc)
    values = await asyncio.to_thread(
        mcp_gateway.static_values_for, doc.get("agent", ""), doc.get("user_sub", ""),
        doc.get("task_scope", ""), wanted, **choice_kw(doc.get("account_choices")), built=built,
    ) if wanted else {}
    for key, cred in creds.items():
        if cred.token_ref is None:
            cred.value = values.get(key, "")
    valued: dict[str, tuple[mcp_gateway.ExitCredential, ...]] = {}
    for key, entries in exits.items():
        kept = tuple(c for c in (_valued_exit(key, e, values, doc) for e in entries) if c is not None)
        if kept:
            valued[key] = kept
    bundles = dict(mcp_broker.bundles_of(session_id))
    for key, cred in creds.items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.gateway = cred
        bundles[key] = bundle
    for key, cli_cred in cli.items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.cli = cli_cred
        bundles[key] = bundle
    # A running tool renews its account token with the renewal it holds.
    for key, token_cred in (doc.get("tokens") or {}).items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.token = token_cred
        bundles[key] = bundle
    for key, kept in valued.items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.exit = kept
        bundles[key] = bundle
    # The paired computer's record at its exit, and the variables a waiting
    # tool's bundle carried to reach it (none for a compute machine, or one
    # the machine rule refuses now).
    exit_env: dict[str, str] = {}
    if valued:
        exit_env = await asyncio.to_thread(
            _readopt_machine_exit, session_id, machine_id or doc.get("machine_id", ""), doc)
    # A tool not yet started fetches its environment at its first call: its
    # names resolved again (exit placeholders as recorded, the exit's
    # variables made again, the relay's token minted again), restored only
    # when every name has its value.
    restored = await asyncio.to_thread(_env_halves, doc, exits, built, exit_env)
    for key, env in restored.items():
        bundle = bundles.get(key) or mcp_broker.SecretBundle()
        bundle.env = {**env, **bundle.env}
        bundles[key] = bundle
    mcp_broker.provision(session_id, bundles)
    token_hash = str(doc.get("token_hash") or "")
    if not token_hash or not (creds or any(bundle.exit for bundle in bundles.values())):
        return 0
    return await push_session(session_id, machine_id or doc.get("machine_id", ""), token_hash, cm=cm)


def _readopt_machine_exit(session_id: str, machine_id: str, doc: dict) -> dict[str, str]:
    """A paired computer's record at its machine's exit, built again as the
    start built it (the door's secret is derived, the variables come from
    the machine as it reports now): the credential hook then hands gh what
    reaches the door the machine kept, and a tool that has not started yet
    gets the variables back in its bundle. Their names and values, empty
    for a compute machine or one the machine rule refuses now (``machine_env``
    answers empty there)."""
    from core.credentials import credential_exit
    from storage import remote_store
    where = remote_store.placement_of(machine_id, str(doc.get("person_sub") or "") or None, str(doc.get("agent") or ""))
    return credential_exit.machine_env(session_id, where)


def _exits_from_descriptor(doc: dict, cli: dict) -> dict[str, list[mcp_gateway.ExitCredential]]:
    """The exit entries a descriptor names, each rebuilt with its bundle's
    own command-line credential (the helper matches the two by value)."""
    out: dict[str, list[mcp_gateway.ExitCredential]] = {}
    for key, items in (doc.get("exits") or {}).items():
        entries = [c for c in (mcp_gateway.ExitCredential.from_descriptor(d, cli=cli.get(key)) for d in items)
                   if c is not None]
        if entries:
            out[key] = entries
    return out


def _built_accounts(doc: dict) -> dict[str, str]:
    """The account each MCP of a descriptor was built on (its ``accounts``,
    per tool key, by the MCP's name). Blocking (the manifest lookup)."""
    from services.mcp import mcp_permissions
    out: dict[str, str] = {}
    for key, account in (doc.get("accounts") or {}).items():
        manifest = mcp_permissions.manifest_for_server(key)
        out[manifest.name if manifest is not None else key] = account
    return out


def _env_halves(doc: dict, exits: dict[str, list[mcp_gateway.ExitCredential]],
                built: dict[str, str], exit_env: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """The environment halves a descriptor names (``envs``), resolved again
    for its identity on the account each tool was built on (``built``): a
    name the exit covers takes its recorded placeholder, a tool that holds
    exit entries takes the paired computer's exit variables made again
    (``exit_env``, ``_readopt_machine_exit``), the hosted relay's token is
    minted again for the person, the rest resolve as at the build
    (``mcp_gateway.env_values_for``). A tool whose every name cannot be had
    now, or whose account moved since, gets none (it then asks for a
    reload). Blocking."""
    from services.mcp import mcp_permissions
    from services.mcp.mcp_session_config import RELAY_TOKEN_ENV
    envs = {str(k): [str(n) for n in v] for k, v in (doc.get("envs") or {}).items() if isinstance(v, list)}
    if not envs:
        return {}
    placeholders = {key: {e.env_name: e.placeholder for e in entries if e.env_name}
                    for key, entries in exits.items()}
    # The start gave the exit's variables only to a tool whose bundle held
    # an exit entry (``_exit_placeholders``): only such a tool takes them.
    made = {key: dict(exit_env or {}) for key in exits}
    wanted: dict[str, tuple[str, tuple[str, ...]]] = {}
    for key, names in envs.items():
        manifest = mcp_permissions.manifest_for_server(key)
        rest = tuple(n for n in names if n not in placeholders.get(key, {}) and n != RELAY_TOKEN_ENV
                     and n not in made.get(key, {}))
        if manifest is not None and rest:
            wanted[key] = (manifest.name, rest)
    values = mcp_gateway.env_values_for(
        doc.get("agent", ""), doc.get("user_sub", ""), doc.get("task_scope", ""), wanted,
        doc.get("account_choices"), built) if wanted else {}
    relay = ""
    out: dict[str, dict[str, str]] = {}
    for key, names in envs.items():
        env = dict(values.get(key, {}))
        env.update({n: p for n, p in placeholders.get(key, {}).items() if n in names})
        env.update({n: v for n, v in made.get(key, {}).items() if n in names})
        if RELAY_TOKEN_ENV in names and RELAY_TOKEN_ENV not in env:
            if not relay:
                relay = _relay_token(doc)
            if relay:
                env[RELAY_TOKEN_ENV] = relay
        missing = [n for n in names if n not in env]
        if missing:
            logger.info("mcp-gateway: %s of a re-adopted session keeps no environment (%d names not had)",
                        key, len(missing))
            continue
        out[key] = env
    return out


def _relay_token(doc: dict) -> str:
    from services.billing import relay_client
    try:
        return relay_client.mint_session_token(str(doc.get("person_sub") or ""))
    except Exception as e:  # noqa: BLE001  # the relay's own refusal leaves the tool without it, never the re-adoption
        logger.info("mcp-gateway: the relay token was not minted again: %s", type(e).__name__)
        return ""


def _valued_exit(key: str, cred: mcp_gateway.ExitCredential, values: dict[str, str],
                 doc: dict) -> mcp_gateway.ExitCredential | None:
    """The entry with its static value back: the resolved one, or the hosted
    relay's token minted again for the descriptor's person; None for a
    static entry whose value cannot be had now (the tool's call is then
    refused at the exit until a new chat)."""
    from dataclasses import replace

    from services.mcp.mcp_session_config import RELAY_TOKEN_ENV
    if cred.token_ref is not None or cred.cli is not None:
        return cred
    if cred.env_name == RELAY_TOKEN_ENV:
        from services.billing import relay_client
        try:
            return replace(cred, value=relay_client.mint_session_token(str(doc.get("person_sub") or "")))
        except Exception as e:  # noqa: BLE001  # the relay's own refusal ends this entry, never the re-adoption
            logger.info("mcp-gateway: the relay token of %s was not minted again: %s", key, type(e).__name__)
            return None
    value = values.get(f"{key}\0{cred.env_name}", "")
    return replace(cred, value=value) if value else None
