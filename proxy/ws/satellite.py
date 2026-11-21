"""Satellite WebSocket endpoint — accepts connections from satellite daemons.

Protocol:
  1. Satellite connects to /v1/satellite
  2. Sends auth message within 5s: {type: "auth", machine_id, machine_secret, capabilities}
  3. Proxy validates and sends: {type: "auth_result", status: "ok"|"rejected"}
  4. Satellite enters message loop (heartbeats, events, acks)
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

import psycopg
from fastapi import WebSocket, WebSocketDisconnect

import config as app_config
from auth import roles
from core import placement
from core.remote import file_sync as file_sync_rules, machine_memory
from core.remote.satellite_connection import get_connection_manager
from storage import remote_store
from storage.pg import run_db
from ws import wire_events as wire
from ws.socket_gone import GONE_WHILE_OPENING

logger = logging.getLogger("otodock.satellite-ws")

# Every store call in the handshake below runs OFF the event loop (run_db on
# the dedicated DB executor, or a persister lane): a reconnect storm after a
# stall re-auths the whole fleet at once, and a synchronous commit here would
# freeze the loop exactly when it must serve pings (the 09-03 incident).

_AUTH_TIMEOUT_S = 5

# The first frame is sized before it is parsed. uvicorn assembles a frame up
# to its 16 MiB limit, and a parse of that size held the loop about half a
# second, before anything about the peer was known. The real hello (machine
# id, secret, capabilities, version) is about 1.5 KB; the cap is strictly
# above the 64 KB bound ``register`` puts on a persisted capabilities
# payload, so the two never cross. Counted in characters, the unit the parse
# cost follows.
_MAX_AUTH_FRAME_CHARS = 256 * 1024

# 2.0 raises the floor to its own satellite: the token every terminal's open
# carries (its ack and its exit name it), the live count in the sign-in's
# capabilities, the alive report that is complete (report_id, partial), the
# Codex turn kept for a replay, the engines named by the sign-in's answer.
# Below it a satellite with auto-update on is pushed the update at its first
# sign-in, before it serves a session; one with auto-update off, or on an
# opted-in plaintext link, is refused with the upgrade error.
MIN_SATELLITE_VERSION = "0.5.197"


# ----------------------------------------------------------------------
# SATELLITE_VERSION_LATEST is derived from satellite/config.py at module
# load so there's ONE source of truth for the version that ships. The
# two-constant arrangement that lived here before drifted into an
# infinite auto-update loop when only one side was bumped: proxy pushes
# tarball → tarball's config.py still has the old version → satellite
# reconnects reporting the old version → proxy pushes again, forever.
#
# We parse the version with a tight regex instead of importing the
# satellite package — satellite/config.py imports psutil + sets signal
# handlers + adjusts process titles, none of which belongs in the proxy
# process. Reading a 30-character version string is sufficient.
#
# Bumping the satellite version is now a one-place edit:
# ``satellite/config.py::SATELLITE_VERSION``. Proxy restart picks it up.
# ----------------------------------------------------------------------

_SATELLITE_CONFIG_PY = (
    Path(__file__).resolve().parent.parent.parent / "satellite" / "config.py"
)
_VERSION_LINE_RE = re.compile(
    r'^\s*SATELLITE_VERSION\s*=\s*[\'"]([^\'"]+)[\'"]\s*$', re.MULTILINE,
)


def _cli_pins() -> dict[str, str]:
    """``{pin_key: pinned version}`` for every engine with a CLI binary. The
    keys are the satellite's WIRE vocabulary (``claude_code`` / ``codex``,
    frozen — `satellite/host/cli_versions.py` drops unknown keys fail-open),
    declared on each engine's descriptor; the version is read at call time
    so a runtime change to ``config.PINNED_*`` reaches the next handshake."""
    from core.session.session_manager import get_all_layers
    return {
        layer.capabilities.runtime.pin_key: layer.pinned_cli_version()
        for layer in get_all_layers().values()
        if layer.capabilities.runtime.pin_key
    }


def _read_satellite_version_from_source() -> str | None:
    """Parse ``SATELLITE_VERSION`` from ``satellite/config.py``.

    Returns ``None`` when the satellite source tree is not part of this
    build — the platform runs without the remote-machines feature
    (pairing and auto-update are disabled; everything else is unaffected).

    Raises ``RuntimeError`` when the tree IS present but the constant
    can't be read — proxy startup fails loudly rather than silently
    serving a wrong "latest" value (which would break auto-update).
    """
    if not _SATELLITE_CONFIG_PY.is_file():
        return None
    try:
        text = _SATELLITE_CONFIG_PY.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(
            f"Cannot read satellite source version from {_SATELLITE_CONFIG_PY}: {e}"
        ) from e
    match = _VERSION_LINE_RE.search(text)
    if not match:
        raise RuntimeError(
            f"SATELLITE_VERSION = '...' line not found in {_SATELLITE_CONFIG_PY}"
        )
    return match.group(1)


# The version we offer to satellites that are running anything older.
# Distinct from MIN_SATELLITE_VERSION: this is "what we'd push as an
# update", not "what we hard-reject". Bumping LATEST without bumping MIN
# is the routine path for nice-to-have satellite improvements — old
# satellites can auto-update but aren't bricked. Source of truth lives
# in ``satellite/config.py``. ``None`` = satellite source not in this
# build (see satellite_source_available()).
SATELLITE_VERSION_LATEST = _read_satellite_version_from_source()


def satellite_source_available() -> bool:
    """True when the satellite source tree ships with this build.

    False means the remote-machines feature is unavailable: pairing,
    bootstrap-installer download and satellite auto-update are refused
    with a clear error, and the dashboard hides the feature's UI
    (``remote_machines_available`` on the identity payload).
    """
    return SATELLITE_VERSION_LATEST is not None

# machine_id → target_version we just pushed via update_required. Read on the
# next reconnect to detect a rolled-back update (satellite came back BELOW the
# target) so we (a) notify dashboards via _broadcast_satellite_update_failed
# and (b) do NOT re-push on THAT reconnect (which would crash-loop the
# satellite). In-memory: a proxy restart loses it and we fall back to
# re-pushing, which is acceptable. The persistent per-target rollback budget
# (below) is what stops a deterministically broken build from looping.
_pending_pushed_updates: dict[str, str] = {}

# Automatic re-pushes of the SAME target stop after this many rollbacks
# (`remote_machines.update_rollback_count`, counted per target version by
# `remote_store.record_update_rollback`). The reconnect-after-push check only
# skips ONE re-push; without a budget a broken build would push → crash →
# rollback on every reconnect forever. Once paused, only an explicit
# "Update now" (pending_update, or the online push) attempts that target
# again; a successful reconnect on the target — or a new target — resets it.
MAX_AUTO_UPDATE_ROLLBACKS = 2


def note_update_pushed(machine_id: str, target_version: str) -> None:
    """Remember a just-pushed target so the machine's NEXT reconnect can
    detect a rollback. The single writer of ``_pending_pushed_updates`` —
    used by the auth-time automatic push AND the admin/user "Update now"
    online push, so a manual push whose satellite rolls back is recorded,
    counted and announced exactly like an automatic one."""
    _pending_pushed_updates[machine_id] = target_version


#: Satellites from this version answer ``update_received`` when they read an
#: ``update_required`` (before they start applying it).
UPDATE_ACK_SATELLITE_VERSION = "0.5.140"
#: An update offered in the first seconds after this process began serving
#: waits for the rest of the window (a deploy's fresh proxy saw a satellite's
#: socket die right after its auth, and the lost push read as a rollback).
UPDATE_HOLD_S = 10.0
_UPDATE_ACK_WAIT_S = 20.0
_CLOSE_READ_S = 2.0
#: A machine the platform does not serve yet (a compute machine): close
#: codes 4001 to 4008 are taken, 4006 and 4007 by a satellite's own exits.
_CLOSE_NOT_SERVED = 4009
# When the first satellite reached this process (the lifespan startup runs
# before uvicorn serves, so module import is too early to count from).
_serving_since: float | None = None


def _note_serving() -> None:
    global _serving_since
    if _serving_since is None:
        _serving_since = time.monotonic()


def _update_hold_left() -> float:
    if _serving_since is None:
        return 0.0
    return max(0.0, UPDATE_HOLD_S - (time.monotonic() - _serving_since))


class _PushWatch:
    """Reads a satellite's socket while an update is offered on it, before
    the connection is registered: the frames it sends meanwhile are skipped,
    its disconnect is remembered.

    uvicorn's sans-I/O protocol writes the 4007 close and closes the
    transport without waiting for the peer's close, so a completed close is
    not observable here. What is: a satellite at
    ``UPDATE_ACK_SATELLITE_VERSION`` saying it read the push, and, for any
    satellite, whether the socket was still live when the close went out
    (the first disconnect event is then our own 4007)."""

    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.gone = False
        self.code: int | None = None

    async def _next(self, timeout: float) -> MutableMapping[str, Any] | None:
        if self.gone:
            return None
        try:
            msg = await asyncio.wait_for(self.ws.receive(), timeout)
        except asyncio.TimeoutError:
            return None
        except Exception:  # noqa: BLE001  # an ASGI receive raises by server implementation once the socket is gone
            self.gone = True
            return None
        if msg.get("type") == "websocket.disconnect":
            self.gone, self.code = True, msg.get("code")
            return None
        return msg

    async def lives_through(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.gone and (left := deadline - time.monotonic()) > 0:
            await self._next(left)
        return not self.gone

    async def acknowledged(self, target: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.gone and (left := deadline - time.monotonic()) > 0:
            msg = await self._next(left)
            try:
                frame = json.loads((msg or {}).get("text") or "null")
            except ValueError:
                continue
            if (isinstance(frame, dict) and frame.get("type") == "update_received"
                    and frame.get("target_version") == target):
                return True
        return False

    async def closed_live(self) -> bool:
        """After our 4007 close: whether it went out on a live socket."""
        if self.gone:
            return False
        deadline = time.monotonic() + _CLOSE_READ_S
        while not self.gone and (left := deadline - time.monotonic()) > 0:
            await self._next(left)
        return self.code == 4007


def auto_update_paused(machine: dict, target_version: str | None) -> bool:
    """True when automatic pushes of ``target_version`` are exhausted for this
    machine: the stored rollback budget refers to the SAME target and has
    reached ``MAX_AUTO_UPDATE_ROLLBACKS``. Pure — reads the machine row only."""
    if not target_version:
        return False
    if (machine.get("update_rollback_target") or "") != target_version:
        return False
    return int(machine.get("update_rollback_count") or 0) >= MAX_AUTO_UPDATE_ROLLBACKS


def _version_at_least(version: str, minimum: str) -> bool:
    """Compare dotted version strings (major.minor.patch). Missing = too old."""
    if not version:
        return False
    try:
        v_parts = [int(p) for p in version.split(".")[:3]]
        m_parts = [int(p) for p in minimum.split(".")[:3]]
    except (ValueError, AttributeError):
        return False
    while len(v_parts) < 3:
        v_parts.append(0)
    while len(m_parts) < 3:
        m_parts.append(0)
    return v_parts >= m_parts


def replays_turns_of() -> list[str]:
    """The engines whose in-flight turn a restarted platform re-adopts from
    the machine's retained buffer (``runtime.supports_reattach_after_restart``)."""
    from core.session.session_manager import get_all_layers
    return sorted(path for path, layer in get_all_layers().items()
                  if layer.capabilities.runtime.supports_reattach_after_restart)


def _live_sessions(capabilities: dict) -> int:
    """The satellite's live session count at its sign-in (from 0.5.171 in
    the capabilities; an older satellite answers 0 and is pushed at once)."""
    count = capabilities.get("active_sessions")
    return count if isinstance(count, int) and count > 0 else 0


async def _push_waiting_update(machine_id: str) -> None:
    """The idle hook: the update that waited for the machine's sessions to
    end, pushed over its socket now (``remote_machines.push_update_online``)."""
    from api.remote.remote_machines import UpdateRefused, push_update_online
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if machine is None:
        return
    try:
        pushed = await push_update_online(machine_id, machine)
    except UpdateRefused as e:
        logger.info("Satellite %s: idle, and the waiting update is not pushed: %s",
                    placement.log_id(machine_id), e)
        return
    if pushed:
        logger.info("Satellite %s: idle, so the waiting update to %s is pushed",
                    placement.log_id(machine_id), SATELLITE_VERSION_LATEST)


def register() -> None:
    """The link hooks of this module, once at startup."""
    from core.remote import satellite_link
    satellite_link.add_idle_hook(_push_waiting_update, name="update")
    get_connection_manager().set_link_callback(_on_machine_link)


async def _egress_proxy_for(machine: dict) -> dict | None:
    """The platform's outgoing proxy for the machine's credential exit: the
    install's own compute machine alone takes it; a compute machine on
    another server and a paired computer read their own environment."""
    if not placement.machine_is_compute(machine) or placement.compute_own_copy(machine):
        return None
    from services.mcp import mcp_egress
    return await asyncio.to_thread(mcp_egress.exit_upstream_block)


def _auth_ok(machine: dict, *, cli_pins: dict[str, str] | None = None,
             report_id: str = "", egress_proxy: dict | None = None) -> dict:
    """The ``auth_result: ok`` frame. It carries the satellite-host policy so
    the satellite can re-validate file writes locally: defense in depth, a
    compromised proxy can't trick a home-only satellite into writing
    /etc/sudoers because the satellite checks against its own local copy of
    allow_full_fs before any satellite_host write. ``report_id`` names this
    sign-in: the satellite repeats it in its two alive frames, so a pair
    queued at an earlier sign-in is never read as this one's."""
    return {
        "type": "auth_result",
        "status": "ok",
        **({"report_id": report_id} if report_id else {}),
        # The platform's outgoing proxy, for the install's own compute
        # machine alone (``mcp_egress.exit_upstream_block``): its credential
        # exit reaches the world through it. Never logged.
        **({"egress_proxy": egress_proxy} if egress_proxy else {}),
        # The engines whose live sessions the connect report lists with
        # their turn (``sessions``): the satellite lists a Codex session
        # there only under a platform that says so.
        "replays_turns_of": replays_turns_of(),
        # A terminal's lines are acked once their rows are written
        # (``transcript_ack``): a satellite then sends again, from its start,
        # a terminal this platform acked nothing of yet (0.5.197).
        "transcript_acks": True,
        # The numbers of this machine's memory guard (0.5.177): its kind's
        # reserve, what it may stop, its ceiling per session.
        "memory_policy": machine_memory.policy(machine),
        # The admin's bound on session processes the install's compute
        # machine starts at once (``SESSION_SPAWN_CONCURRENCY``, the server's
        # own starts before 2.0). A personal or admin-paired computer, and a
        # compute machine with none set, keep their own.
        **({"session_starts": app_config.SESSION_SPAWN_CONCURRENCY}
           if app_config.SESSION_SPAWN_CONCURRENCY > 0 and placement.machine_is_compute(machine) else {}),
        "policy": {
            "allow_full_fs": bool(machine.get("allow_full_fs") or False),
            # Device-control consent set, so the
            # satellite can re-check capability grants at tool time.
            "device_grants": sorted(
                placement.parse_device_grants(machine.get("device_grants"))
            ),
            # Universal per-file sync cap (OTODOCK_MAX_FILE_MB): the proxy
            # config is the single source of truth and satellites apply it to
            # their manifests + inbound writes. Handshake-only delivery: the
            # cap changes only on proxy restart, which re-auths every WS.
            "sync_max_file_bytes": app_config.SYNC_MAX_FILE_BYTES,
            # Marker-confirmed generated-dir exclusions (1.5). Satellites
            # validate + apply the table to every sync walk, so the two sides
            # always walk with the same rules. Validated once at send so a
            # config typo degrades to legacy instead of shipping asymmetric
            # garbage.
            "sync_ignore_rules": file_sync_rules.validate_ignore_rules(
                app_config.SYNC_IGNORE_RULES),
        },
        # CLI version pins (VERSIONS.md) — the satellite reconciles its installed
        # claude/codex to these on auth, so the fleet runs the EXACT versions the
        # platform verified. Empty value → satellite skips that CLI (fail-open).
        "cli_pins": _cli_pins() if cli_pins is None else cli_pins,  # {pin_key: version}
    }


async def _refuse(websocket: WebSocket, reason: str, *, code: int, close_reason: str,
                  **extra: str) -> None:
    """Answer a refused handshake with its ``auth_result`` and close. A peer
    that already left fails the send (and may fail the close); the refusal
    ends here all the same."""
    with contextlib.suppress(Exception):
        await websocket.send_text(json.dumps({
            "type": "auth_result", "status": "rejected", "reason": reason, **extra,
        }))
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=close_reason)


# Sockets held in the pre-auth phase per client address: a peer that opens
# connections and sends nothing holds each for ``_AUTH_TIMEOUT_S``; past the
# cap a new one is refused at once. The address is the resolved client
# (``auth/lan_check``), the hop's when no hop is trusted, so the cap is
# generous: a healthy satellite's auth frame arrives at once.
_PREAUTH_PER_ADDRESS = 32
_preauth: dict[str, int] = {}


def _preauth_acquire(addr: str) -> bool:
    if _preauth.get(addr, 0) >= _PREAUTH_PER_ADDRESS:
        return False
    _preauth[addr] = _preauth.get(addr, 0) + 1
    return True


def _preauth_release(addr: str) -> None:
    n = _preauth.get(addr, 0) - 1
    if n <= 0:
        _preauth.pop(addr, None)
    else:
        _preauth[addr] = n


async def _note_version_change(cm, machine_id: str, machine: dict, sat_version: str) -> None:
    """A satellite that authenticated on another version than its row held
    came back from an update: clear the update bookkeeping and tell the
    dashboards (``record_update_result`` also clears ``pending_update``)."""
    prev_version = machine.get("satellite_version") or ""
    if not prev_version or prev_version == sat_version:
        return
    try:
        cm.persist_lane(
            machine_id, "update_result",
            {"target_version": sat_version, "error": None},
        )
        await _broadcast_satellite_updated(machine_id, machine, sat_version)
    except Exception:
        logger.exception("Failed to record/announce satellite_updated")


#: The words a compute machine claims for its files (``compute_files``): an
#: older compute satellite claims none, which reads as the shared folder.
_FILES_OWN, _FILES_SHARED = "own", "shared"


def compute_link_refusal(scope, capabilities: dict | None, machine: dict | None = None) -> str:
    """Why a compute machine's link is refused before its secret is read,
    ``""`` when it may go on: the install does not send sessions to compute
    machines (the key is off); the process does not claim to be a compute
    machine, or claims other files than its row was approved for (its own
    copy on another server, the platform's folder beside it); the socket came
    in on the internal listener (a sandbox's splice lands there: no session
    may link as the compute machine).

    A machine beside the platform links by the raw peer with no forwarding
    header (``deployment.compute_link_allowed``: the compose service's own
    address or loopback). One on another server links by the rule every
    machine link follows (``compute_pairing.link_address``); the address it
    came from is judged after its secret verified
    (``compute_pairing.address_refusal``: recorded, or refused by its lock)."""
    from auth import lan_check
    from core.config import deployment
    from services.remote import compute_pairing
    if not isinstance(capabilities, dict) or capabilities.get("compute") is not True:
        return "This machine does not claim to be a compute machine."
    own = placement.compute_own_copy(machine)
    if (capabilities.get("compute_files") or _FILES_SHARED) != (_FILES_OWN if own else _FILES_SHARED):
        return ("This machine's files are not set up the way the platform approved: "
                + ("it must keep its own copy." if own else "it must share the platform's folders."))
    if own:
        return compute_pairing.link_address(scope)[1]
    scope = scope or {}
    addr = lan_check.resolve(scope)
    if addr.internal:
        return "A compute machine does not link through the internal listener."
    if any(name in lan_check.EDGE_SIGNALS for name, _v in (scope.get("headers") or ())):
        return "A compute machine links from its own address, never through a forwarder."
    if not deployment.compute_link_allowed(addr.peer):
        return f"A compute machine does not link from {addr.peer or 'an unknown address'}."
    return ""


async def _admitted_machine(websocket: WebSocket, machine_id: str,
                            machine_secret: str, capabilities: dict | None = None) -> dict | None:
    """The machine's row when its credentials admit it; None after the
    refusal is sent (a deleted machine, user-paired machines off, a wrong
    secret). The store reads may raise: the caller answers that. A compute
    row is admitted only under ``compute_link_refusal``'s rule, and a paired
    row never from a process that claims to be a compute machine."""
    # Machine DB row gone (admin/user deleted it from dashboard).
    # Tell the satellite to self-uninstall via close code 4006 so it
    # cleans up local files instead of looping forever in the reconnect
    # backoff trying with stale credentials.
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if not machine and isinstance(capabilities, dict) and capabilities.get("compute"):
        # A compute machine is told it was removed only when an admin removed
        # it and its secret verifies against the record kept then: an id this
        # database does not know (a restore, another install at the same
        # address) is refused as a wrong secret is, and waits.
        from storage import compute_store
        if not await run_db(compute_store.removed_verifies, machine_id, machine_secret):
            logger.info("Satellite %s rejected: no compute machine of that id", placement.log_id(machine_id))
            await _refuse(websocket, "Invalid credentials", code=4001, close_reason="Auth rejected")
            return None
    if not machine:
        logger.info(
            "Satellite %s rejected: machine_id not found — instructing "
            "self-uninstall",
            placement.log_id(machine_id),
        )
        await _refuse(websocket, "machine_deleted", code=4006, close_reason="machine_deleted",
                      action="uninstall")
        return None

    # A compute machine links only under the compute rule, with its own close
    # code, never 4006 (a satellite uninstalls itself on it) nor 4007 (it
    # waits for an update), and with no action: a refusal reveals no more
    # than 4006 does. The rule is judged before the secret.
    compute = placement.machine_is_compute(machine)
    if compute:
        why = compute_link_refusal(getattr(websocket, "scope", None), capabilities, machine)
        if why:
            logger.info("Satellite %s rejected: %s", placement.log_id(machine_id), why)
            await _refuse(websocket, why, code=_CLOSE_NOT_SERVED, close_reason="not_served")
            return None
    elif isinstance(capabilities, dict) and capabilities.get("compute"):
        logger.warning("Satellite %s rejected: a paired machine claims to be a compute machine",
                       placement.log_id(machine_id))
        await _refuse(websocket, "A paired machine cannot claim to be a compute machine.",
                      code=4001, close_reason="Auth rejected")
        return None

    # Reject user-paired satellites trying to reconnect when the
    # admin has disabled the feature. Looking up the machine before
    # verifying the secret is fine — the pairing_scope read is read-only.
    if not compute and not placement.machine_is_admin_paired(machine):
        from storage import database as _db
        if await run_db(_db.get_platform_setting, "allow_user_paired_machines") == "0":
            logger.info(
                "Satellite %s rejected: user-paired machines disabled",
                placement.log_id(machine_id),
            )
            await _refuse(websocket, "User-paired machines are disabled by admin policy.",
                          code=4005, close_reason="feature_disabled_by_admin")
            return None

    # Verify machine secret
    if not await run_db(remote_store.verify_machine_secret, machine_id, machine_secret):
        logger.warning("Satellite auth failed for machine %s", placement.log_id(machine_id))
        await _refuse(websocket, "Invalid credentials", code=4001, close_reason="Auth rejected")
        return None
    if compute and placement.compute_own_copy(machine):
        # After the secret: the address is written, and a lock's refusal
        # tells every admin.
        from services.remote import compute_pairing
        seen, _why, shared = compute_pairing.link_address(getattr(websocket, "scope", None))
        why = await compute_pairing.address_refusal(machine, seen, shared)
        if why:
            logger.info("Satellite %s rejected: %s", placement.log_id(machine_id), why)
            await _refuse(websocket, why, code=_CLOSE_NOT_SERVED, close_reason="not_served")
            return None
        machine = {**machine, "compute_address": seen}
    return cast("dict", machine)


async def ws_satellite_handler(websocket: WebSocket):
    """WebSocket handler for satellite daemon connections."""
    from auth import lan_check
    # Keyed on the bucket: the internal listener's sockets (sandboxes and
    # tunnels) never share a key with a machine that links from loopback.
    addr = lan_check.resolve(getattr(websocket, "scope", None) or {}).bucket_key or "?"
    if not _preauth_acquire(addr):
        logger.warning("Satellite socket from %s refused: %d in the auth phase already",
                       addr, _PREAUTH_PER_ADDRESS)
        with contextlib.suppress(Exception):
            await websocket.close(code=1013, reason="try again later")
        return
    # One-shot: the slot frees when the auth PHASE ends (authenticated or
    # refused), never held for the life of a connected socket, so a reconnect
    # storm of unauthenticated sockets is the only thing the cap bounds.
    freed = {"done": False}

    def _free() -> None:
        if not freed["done"]:
            freed["done"] = True
            _preauth_release(addr)

    try:
        await _ws_satellite_handler(websocket, _free)
    finally:
        _free()


async def _refuse_first_frame(websocket: WebSocket, e: BaseException) -> None:
    """A first frame that never came, was binary or does not parse."""
    logger.warning("Satellite auth timeout or invalid first frame: %r", e)
    await _refuse(websocket, "Auth timeout or invalid message", code=4001,
                  close_reason="Auth failed")


async def _ws_satellite_handler(websocket: WebSocket, _free) -> None:
    try:
        await websocket.accept()
    except GONE_WHILE_OPENING as e:
        logger.info("Satellite socket closed during the handshake (%s)", type(e).__name__)
        return

    # --- Authentication (5s timeout) ---
    # Only the read is the peer's to lose: the parse below is the frame's,
    # and a RecursionError (a RuntimeError) there is a bad frame, not a
    # closed socket.
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=_AUTH_TIMEOUT_S
        )
    except GONE_WHILE_OPENING as e:
        logger.info("Satellite socket closed before its auth frame (%s)", type(e).__name__)
        return
    except (asyncio.TimeoutError, KeyError) as e:
        # KeyError: a binary first frame (the socket's text read finds none).
        await _refuse_first_frame(websocket, e)
        return
    if len(raw) > _MAX_AUTH_FRAME_CHARS:
        logger.warning(
            "Satellite auth frame of %d chars from %s refused before parse "
            "(cap %d)", len(raw), getattr(websocket, "client", None),
            _MAX_AUTH_FRAME_CHARS,
        )
        await _refuse(websocket, "Auth frame too large", code=4001,
                      close_reason="Auth frame too large")
        return
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError) as e:
        await _refuse_first_frame(websocket, e)
        return

    if not isinstance(msg, dict) or msg.get("type") != "auth":
        await _refuse(websocket, "First message must be auth", code=4001,
                      close_reason="Expected auth message")
        return

    machine_id = msg.get("machine_id", "")
    machine_secret = msg.get("machine_secret", "")
    capabilities = msg.get("capabilities", {})
    sat_version = msg.get("satellite_version", "")

    # The id, the secret and the version are strings and the capabilities an
    # object: anything else is refused before it reaches the store or the
    # connection registry.
    if (not all(isinstance(v, str) for v in (machine_id, machine_secret, sat_version))
            or not isinstance(capabilities, dict)):
        await _refuse(websocket, "Malformed auth message", code=4001,
                      close_reason="Malformed auth")
        return

    if not machine_id or not machine_secret:
        await _refuse(websocket, "Missing machine_id or machine_secret", code=4001,
                      close_reason="Missing credentials")
        return

    try:
        machine = await _admitted_machine(websocket, machine_id, machine_secret, capabilities)
    except (psycopg.Error, OSError) as e:
        # The store could not answer: the machine is not at fault, so it is
        # told to come back (it reconnects on its own loop), never refused
        # for good, and the handler ends here instead of raising past it.
        logger.warning("Satellite %s auth: the store could not be read (%s)", placement.log_id(machine_id), e)
        await _refuse(websocket, "The platform could not check this machine now; try again",
                      code=1013, close_reason="try again later")
        return
    if machine is None:
        return
    # What the row keeps: register persists these capabilities, and every
    # later write of them starts from the same dict.
    from services.remote import compute_pairing
    capabilities = compute_pairing.kept_claims(capabilities)
    # Authenticated: the auth phase is over, so the pre-auth slot frees now
    # (the socket then lives for hours; it must not count against the cap).
    _free()
    _note_serving()

    # --- version policy + auto-update push ---
    # After authentication so we never send the tarball to an unknown peer.
    # An older satellite with valid credentials either gets the new tarball
    # pushed (when auto_update_enabled OR pending_update is set) or gets
    # rejected with a "manual update required" reason. The
    # SATELLITE_VERSION_LATEST → SATELLITE_VERSION_LATEST flow handles
    # forward-compat as future versions bump.
    # No satellite source in this build → nothing to offer as an update.
    needs_update = SATELLITE_VERSION_LATEST is not None and not _version_at_least(
        sat_version, SATELLITE_VERSION_LATEST,
    )
    must_reject = not _version_at_least(sat_version, MIN_SATELLITE_VERSION)
    if placement.compute_own_copy(machine):
        # A machine on another server that its host service started follows
        # the platform's image tag: the refusal names the tag (its host takes
        # the tag alone), or says why it cannot.
        from services.remote import compute_bootstrap
        refused = compute_pairing.image_refusal(capabilities, sat_version, SATELLITE_VERSION_LATEST or "")
        if refused:
            reason = refused.pop("reason")
            logger.warning("Compute machine %s refused: %s", placement.log_id(machine_id), reason)
            compute_bootstrap.give_up(machine_id)
            await _refuse(websocket, reason, code=4001, close_reason="version mismatch", **refused)
            return
    if placement.machine_is_compute(machine):
        # A compute machine's version is its image's: nothing is ever pushed
        # to it, and it serves only the build it was made from. During an
        # update the old compute container may sign in to the new server for a
        # moment: a session started there would carry a description its render
        # does not know, so any other version is refused.
        if must_reject or (SATELLITE_VERSION_LATEST is not None and sat_version != SATELLITE_VERSION_LATEST):
            logger.warning("Compute machine %s rejected: version %r is not the platform's %s",
                           placement.log_id(machine_id), sat_version, SATELLITE_VERSION_LATEST)
            await _refuse(
                websocket,
                f"This compute machine runs satellite {sat_version or 'unknown'}, not the "
                f"platform's own {SATELLITE_VERSION_LATEST or MIN_SATELLITE_VERSION}. "
                "Start it from the platform's own version.",
                code=4001, close_reason="version mismatch",
            )
            return
        needs_update = False

    # Rollback detection: if we pushed an update last cycle and the satellite
    # reconnected STILL below that target, its boot guard crash-rolled-back.
    # Notify dashboards (red banner) and skip re-pushing — re-pushing would
    # just crash-loop it. It connects on the old (working) version instead.
    _pushed_target = _pending_pushed_updates.pop(machine_id, "")
    if _pushed_target and not _version_at_least(sat_version, _pushed_target):
        try:
            attempts = await run_db(
                remote_store.record_update_rollback,
                machine_id, target_version=_pushed_target,
                error=f"update to {_pushed_target} rolled back to {sat_version}",
            )
        except Exception:
            logger.exception("Failed to record update rollback")
            attempts = int(machine.get("update_rollback_count") or 0) + 1
        # The row as the record left it: the push decision below reads it.
        machine = {**machine, "update_rollback_target": _pushed_target,
                   "update_rollback_count": attempts, "pending_update": False}
        paused = attempts >= MAX_AUTO_UPDATE_ROLLBACKS
        logger.warning(
            "Satellite %s: update → %s rolled back (reconnected on %s), "
            "attempt %d/%d; not re-pushing%s",
            placement.log_id(machine_id), _pushed_target, sat_version or "unknown",
            attempts, MAX_AUTO_UPDATE_ROLLBACKS,
            " — automatic updates PAUSED until 'Update now'" if paused else "",
        )
        try:
            await _broadcast_satellite_update_failed(
                machine_id, machine,
                error=f"Update to {_pushed_target} failed; rolled back.",
                rolled_back_to=sat_version,
                attempts=attempts, paused=paused,
            )
        except Exception:
            logger.exception("Failed to announce update rollback")
        needs_update = False  # connect on the old version; do not re-push

    # A satellite on a plaintext link it opted into (``insecure_transport``
    # in its capabilities) refuses code it cannot authenticate, so no update
    # is pushed to it: the operator re-runs the installer on that machine.
    insecure_link = bool(capabilities.get("insecure_transport"))
    if (needs_update or must_reject) and insecure_link:
        logger.warning(
            "Satellite %s runs %s on a plaintext link: the update to %s is not pushed; "
            "re-run the installer on that machine",
            placement.log_id(machine_id), sat_version or "unknown", SATELLITE_VERSION_LATEST,
        )
        if must_reject:
            from services.remote import machine_away
            machine_away.note_refused(machine_id, sat_version, plaintext=True)
            await _refuse(
                websocket,
                f"Satellite version {sat_version or 'unknown'} is older than the "
                f"proxy's minimum {MIN_SATELLITE_VERSION}, and this machine's link is "
                "plaintext, so no update is pushed to it: re-run the installer there.",
                code=4001, close_reason="version too old",
            )
            return
        needs_update = False
    # Every sign-in clears the mark of an update that waited for an idle
    # moment, and ends one that was sent (it landed, rolled back or was lost:
    # the version decides again); the branch below sets them again.
    links = get_connection_manager().links
    was_waiting = links.update_waiting(machine_id)
    links.update_waits(machine_id, "")
    links.update_sent(machine_id, "")
    if needs_update or must_reject:
        auto_update = bool(machine.get("auto_update_enabled", True))
        pending = bool(machine.get("pending_update", False))
        # Rollback budget for THIS target exhausted → no automatic push; an
        # explicit "Update now" (pending) still goes through.
        paused = auto_update and auto_update_paused(machine, SATELLITE_VERSION_LATEST)
        if paused and not pending:
            logger.warning(
                "Satellite %s: automatic update to %s paused after %d "
                "rollback(s); waiting for 'Update now'",
                placement.log_id(machine_id), SATELLITE_VERSION_LATEST,
                int(machine.get("update_rollback_count") or 0),
            )
        # A machine with a live session, or a session start on its way to it
        # (``MachineLinks.starting``), takes the update once it is idle
        # (``_push_waiting_update``, at the heartbeat that reports none): the
        # push ends every session there. Below the floor, on "Update now",
        # or from a satellite that sends no count (below 0.5.171) it is
        # pushed at the sign-in, before the machine serves a session.
        live = _live_sessions(capabilities)
        update_waits = (needs_update and not must_reject and not pending
                        and (live > 0 or links.starting(machine_id))
                        and auto_update and not paused and satellite_source_available())
        # A push needs the satellite source tree (the tarball is built from
        # it) — without it, a below-floor satellite falls through to the
        # manual-reject branch instead of a doomed tarball build.
        if update_waits:
            links.update_waits(machine_id, cast("str", SATELLITE_VERSION_LATEST))
            if was_waiting != SATELLITE_VERSION_LATEST:
                logger.info(
                    "Satellite %s: runs %s with %d live session(s)%s; the update to %s waits "
                    "until it is idle", placement.log_id(machine_id), sat_version, live,
                    " and a start on its way" if links.starting(machine_id) else "", SATELLITE_VERSION_LATEST,
                )
        elif (pending or (auto_update and not paused)) and satellite_source_available():
            # Push the new tarball over WS, then close. The satellite
            # extracts, restarts via systemd, and reconnects on the new
            # version. Dashboards are notified by _broadcast_satellite_updating.
            #
            # ``auth_result: ok`` goes first: the satellite waits only 5 s
            # for its answer, and the tarball (about 0.32 MB on the wire)
            # needs about 0.6 Mbit/s of free uplink per satellite inside
            # them, a share that shrinks when every satellite reconnects at
            # once, and a miss reads as a rollback. Past the auth every satellite applies
            # ``update_required`` from its message loop, with no deadline.
            # No CLI pins: no reconcile starts on code about to be replaced.
            # The connection is never registered.
            # A rollback is counted at the next reconnect only for a push that
            # reached the satellite (its acknowledgement, or our close going
            # out on a live socket): a push lost with a dying socket is a
            # delivery failure, offered again without spending the budget or
            # raising the banner.
            watch = _PushWatch(websocket)
            sent = acked = False
            try:
                await websocket.send_text(json.dumps(_auth_ok(machine, cli_pins={})))
                hold = _update_hold_left()
                if hold > 0 and not await watch.lives_through(hold):
                    logger.info(
                        "Satellite %s: its socket ended while the update waited out the "
                        "proxy's start; offered again at its next connect", placement.log_id(machine_id),
                    )
                    return
                import base64 as _b64

                from api.remote.remote_machines import get_satellite_tarball_with_hash
                # Cached after the first build, but that first build tars
                # the satellite tree — off the loop.
                tarball_bytes, expected_sha256 = await asyncio.to_thread(
                    get_satellite_tarball_with_hash,
                )
                payload = {
                    "type": "update_required",
                    "target_version": SATELLITE_VERSION_LATEST,
                    "tarball_b64": _b64.b64encode(tarball_bytes).decode(),
                    "expected_sha256": expected_sha256,
                    "previous_version": sat_version,
                }
                logger.info(
                    "Satellite %s: pushing update %s → %s (tarball %d bytes)",
                    placement.log_id(machine_id), sat_version, SATELLITE_VERSION_LATEST,
                    len(tarball_bytes),
                )
                # Before the send: a press while the machine restarts finds
                # the update running.
                links.update_sent(machine_id, cast("str", SATELLITE_VERSION_LATEST))
                await websocket.send_text(json.dumps(payload))
                sent = True
                if _version_at_least(sat_version, UPDATE_ACK_SATELLITE_VERSION):
                    acked = await watch.acknowledged(cast("str", SATELLITE_VERSION_LATEST), _UPDATE_ACK_WAIT_S)
            except Exception as e:
                logger.exception(
                    "Satellite %s: failed to push update: %s",
                    placement.log_id(machine_id), e,
                )
                if not sent:
                    links.update_sent(machine_id, "")
                get_connection_manager().persist_lane(
                    machine_id, "update_result",
                    {"target_version": "", "error": f"push failed: {e}"},
                )
            # Close so the satellite applies the update and reconnects.
            with contextlib.suppress(Exception):
                await websocket.close(code=4007, reason="updating")
            if sent and (acked or await watch.closed_live()):
                # Remember the target so the next reconnect can detect a rollback.
                note_update_pushed(machine_id, cast("str", SATELLITE_VERSION_LATEST))
                with contextlib.suppress(Exception):
                    await _broadcast_satellite_updating(
                        machine_id, machine, sat_version, cast("str", SATELLITE_VERSION_LATEST),
                    )
            elif sent:
                logger.warning(
                    "Satellite %s: the update push did not reach it (its socket was gone); "
                    "offered again at its next connect, no rollback counted", placement.log_id(machine_id),
                )
            return
        elif must_reject:
            # Auto-update disabled (or paused after repeated rollbacks) and
            # version is below the hard floor — this satellite cannot connect
            # until an admin clicks "Update now" (sets pending_update=True)
            # or re-pairs.
            why = ("automatic updates are paused after repeated rollbacks"
                   if paused else "auto-update is disabled")
            logger.warning(
                "Satellite %s rejected: version %r < required %s and %s",
                placement.log_id(machine_id), sat_version, MIN_SATELLITE_VERSION, why,
            )
            from services.remote import machine_away
            machine_away.note_refused(machine_id, sat_version, plaintext=False)
            await _refuse(
                websocket,
                f"Satellite version {sat_version or 'unknown'} is older than the "
                f"proxy's minimum {MIN_SATELLITE_VERSION}, and {why}. Ask your "
                f"admin to click 'Update now' in the dashboard.",
                code=4001, close_reason="version too old",
            )
            return
        # needs_update but not must_reject and not auto_update: connect
        # normally. Admin will see "Update available" in the dashboard.

    # Auth succeeded — record the version we just observed, when it is not
    # the one the row already holds (a machine that reconnects often writes
    # nothing).
    if (machine.get("satellite_version") or "") != sat_version:
        try:
            await run_db(remote_store.set_satellite_version, machine_id, sat_version)
        except Exception:
            logger.exception("Failed to record satellite_version")
    # A queued update is pushed at the sign-in, which returned above: a
    # sign-in that gets here leaves nothing queued. It runs the platform's
    # version or newer, or it cannot take an update (a plaintext link, a
    # build with no satellite source, a rollback).
    if machine.get("pending_update"):
        get_connection_manager().persist_lane(machine_id, "pending_update", False)
    from core.remote import satellite_link
    report_id = satellite_link.new_report_id()
    egress = await _egress_proxy_for(machine)
    answer = json.dumps(_auth_ok(machine, report_id=report_id, egress_proxy=egress))
    cm = get_connection_manager()
    try:
        await websocket.send_text(answer)
    except (WebSocketDisconnect, RuntimeError):
        # The machine dropped while the version write ran: a gone peer
        # raises WebSocketDisconnect, or under uvloop a RuntimeError for the
        # closed transport (starlette converts only an OSError). The write
        # above already holds the new version, so the next connect could not
        # tell that an update landed: that is recorded now.
        logger.info("Satellite %s: its socket closed before the answer", placement.log_id(machine_id))
        await _note_version_change(cm, machine_id, machine, sat_version)
        return

    # Clear a deliberate-pause flag on reconnect: a fresh auth means the
    # machine is back (tray Resume, or a reboot), so the sustained-outage
    # evaluator should resume normal monitoring of it. Fire-and-forget
    # through the persister: the satellite is authed but has no reader until
    # register() below, so nothing here may wait on the database.
    if machine.get("paused"):
        cm.persist_lane(machine_id, "paused", False)

    await _note_version_change(cm, machine_id, machine, sat_version)

    # --- Register connection ---
    conn = await cm.register(
        machine_id, websocket, capabilities, satellite_version=sat_version,
        report_id=report_id, compute=placement.machine_is_compute(machine),
        name=str(machine.get("name") or ""),
        compute_address=str(machine.get("compute_address") or "") if placement.compute_own_copy(machine) else "",
    )
    # Its sessions built before its first heartbeat already compose their
    # variables for the upstream the platform just sent it.
    conn.exit_upstream_sent = bool(egress)
    from services.remote import machine_away
    machine_away.clear_refused(machine_id)
    # The machine's credential gateway served from memory while its link was
    # down: tell it now what was revoked (or renewed) meanwhile.
    from core.remote import mcp_gateway_push
    mcp_gateway_push.tick_machine_soon(machine_id)

    # --- Message loop ---
    try:
        async for raw in websocket.iter_text():
            await _dispatch_frame(cm, machine_id, conn, raw)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001  # one satellite connection's loop: an error ends this connection only
        logger.error("Satellite %s WS error: %s", placement.log_id(machine_id), e)
    finally:
        # Identity-guarded: only tear down if WE are still the registered
        # connection — a duplicate reconnect may have replaced us while this
        # socket was draining, and popping blindly would unregister the LIVE
        # connection (proxy shows the machine down, satellite thinks it's up).
        await cm.deregister(machine_id, expected=conn)


# The frames that describe the connection they came on: from a socket a
# newer one replaced they say nothing about the machine as it is now.
_OWN_CONNECTION_FRAMES = frozenset({"heartbeat", "pty_alive", "sessions_alive"})


async def _dispatch_frame(cm, machine_id: str, conn, raw: str) -> None:
    """One frame of the message loop, read on the socket ``conn`` holds."""
    conn.last_frame_at = time.monotonic()
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    # A socket a duplicate reconnect replaced may still drain a frame it
    # received before the swap: its heartbeat must not overwrite the live
    # connection's link state and port, and its alive report is the old
    # connection's. Its other frames (an ack) still count.
    if (isinstance(msg, dict) and msg.get("type") in _OWN_CONNECTION_FRAMES
            and cm.get_connection(machine_id) is not conn):
        return
    await cm.handle_message(machine_id, msg)


# ---------------------------------------------------------------------------
# Broadcast helpers — surface in-flight satellite updates on the
# dashboard WS so users see a banner while their satellite is restarting.
# ---------------------------------------------------------------------------


def _dashboard_recipients_for_machine(machine: dict) -> list[str]:
    """Return user_subs that should see updates for this machine: the
    machine's owner + every admin currently logged in. Agent-assigned
    users are intentionally NOT included for v1 to keep this cheap; they
    can still see the badge on next chat warmup if there's an active
    update on their target machine.
    """
    from storage import database as _db
    subs = set()
    owner = machine.get("registered_by", "")
    if owner:
        subs.add(owner)
    # Admins — best-effort; if listing fails we still notify the owner.
    try:
        for u in _db.list_users() or []:
            if roles.is_admin(u.get("role")):
                subs.add(u["sub"])
    except Exception:
        logger.exception("listing admin users failed")
    return sorted(subs)


def inflight_pushed_updates_for_user(user_sub: str) -> list[dict]:
    """Snapshot of satellite updates the proxy has pushed and is still awaiting a
    reconnect for, scoped to the machines ``user_sub`` may see.

    Used to RECONCILE a (re)connecting dashboard's update banners: the
    ``satellite_updating``/``satellite_updated`` events are transient per-connection
    pushes, so if the dashboard is briefly disconnected during the satellite's
    restart (e.g. a proxy restart drops both, the satellite reconnects on the new
    version while the dashboard is still reconnecting) the ``satellite_updated`` is
    dropped and a stale "updating" banner sticks until a page refresh. On connect
    the dashboard reconciles against this authoritative set — dismissing any
    ``updating`` banner whose machine is no longer mid-update, and surfacing any it
    missed. ``_pending_pushed_updates`` is popped the moment a satellite reconnects
    (success or rollback), so a machine drops out of here exactly when its update
    resolves."""
    out: list[dict] = []
    for mid, target in list(_pending_pushed_updates.items()):
        m = remote_store.get_remote_machine(mid)
        if not m or user_sub not in _dashboard_recipients_for_machine(m):
            continue
        out.append({
            "machine_id": mid,
            "machine_name": m.get("name", ""),
            "from_version": m.get("satellite_version", "") or "",
            "to_version": target,
        })
    return out


def _deliver(event: dict, recipients: list[str]) -> None:
    """Put ``event`` into every dashboard notify queue of ``recipients``; the
    dashboard handler (``ws/dashboard_server_events._handle_server_notification``)
    forwards a catalogued frame straight to the page."""
    from services.notifications import notification_manager
    for sub in recipients:
        if not sub:
            continue
        for conn in notification_manager.get_all_connections(sub):
            try:
                conn.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "notify queue full for %s — dropping %s",
                    sub[:8], event.get("type"),
                )
            except Exception:
                logger.exception("queue put failed")


async def _push_machine_event(machine: dict, event: dict) -> None:
    """Push an event payload to the people who see this machine's updates."""
    _deliver(event, await run_db(_dashboard_recipients_for_machine, machine))


async def push_install_event(
    machine_id: str, event: dict, recipients: list[str],
) -> None:
    """Per-user delivery hook for ``core/install_registry``.

    Registered at startup via ``install_registry.set_broadcaster``. Fans an
    MCP-install lifecycle event (install_started / install_mcp_plan /
    install_progress / install_heartbeat / install_verifying / install_done /
    mcp_install_failed / install_failed) out to ``recipients`` — the install's
    participants (the user_subs that warmed this (machine, agent)) — through
    the same per-user notify channel satellite-update events use.

    Scoping to participants is what makes every combination correct:
      * personal (user-paired) machine → only the owning user sees it;
      * admin-shared machine as an agent's default target → the viewer /
        editor / member actually using that agent sees it (NOT every admin);
      * multiple users on one shared install → each participant sees it.

    This replaced the old per-(machine, agent) WS-listener attach, which
    raced and leaked on reconnect: a backgrounded ``pre_warmup`` task kept a
    dead ``_send`` attached for the whole install while the live tab never
    attached, so the install bar stayed invisible until a manual refresh.
    """
    _deliver(event, recipients)


async def push_transfer_event(event: dict, recipients: list[str]) -> None:
    """Per-user delivery hook for ``core/remote/transfer_registry``
    (workspace upload/sync progress — Feature E, 1.4.0).

    Registered at startup via ``transfer_registry.set_broadcaster``. Fans a
    transfer lifecycle event (transfer_started / transfer_machine_state /
    transfer_progress / transfer_done / transfer_state) out to ``recipients``
    — resolved ONCE at item creation with the same per-user isolation
    predicate the workspace fan-out itself applies (``should_sync_to_target``)
    — through the same per-user notify channel install events use."""
    _deliver(event, recipients)


# A machine's connect or leave reaches the pages once per window: a machine
# that signs in and drops again and again inside it costs one frame, sent at
# the window's end. The page then reads every place it shows again.
_LINK_FRAME_WINDOW_S = 1.0
_link_pushes: dict[str, asyncio.Task] = {}


def _machine_link_recipients(machine_id: str, machine: dict) -> list[str]:
    """Who hears that a machine came or went: who sees its update banners,
    and the members of every agent whose new chats run on it by default
    (their place badge and banner read it)."""
    from storage.sharing import share_store
    subs = set(_dashboard_recipients_for_machine(machine))
    subs |= share_store.members_of_agents(remote_store.get_agents_for_machine(machine_id))
    return sorted(subs)


def _on_machine_link(machine_id: str) -> None:
    """The connection manager's callback at a connect and a leave: on the
    loop with no await, so it only schedules the push."""
    pending = _link_pushes.get(machine_id)
    if pending is not None and not pending.done():
        return
    from core.background import spawn
    _link_pushes[machine_id] = spawn(_push_machine_link(machine_id), name="machine_link",
                                     owner="ws.satellite")


async def _push_machine_link(machine_id: str) -> None:
    await asyncio.sleep(_LINK_FRAME_WINDOW_S)
    # Forgotten before the reads: a change while they run schedules the next frame.
    _link_pushes.pop(machine_id, None)

    def _job() -> list[str]:
        machine = remote_store.get_remote_machine(machine_id)
        return _machine_link_recipients(machine_id, machine) if machine else []
    recipients = await run_db(_job)
    if recipients:
        frame: wire.MachineLinkFrame = {"machine_id": machine_id}
        _deliver({"type": wire.MACHINE_LINK, **frame}, recipients)


async def _broadcast_satellite_updating(
    machine_id: str, machine: dict, from_version: str, to_version: str,
) -> None:
    """Fired when the proxy is about to push a tarball to a satellite
    whose version is below LATEST. Dashboards render a transient banner
    "Updating <name> to X — reconnects in ~30s" via machineUpdateStore."""
    from datetime import datetime, timezone
    await _push_machine_event(machine, {
        "type": wire.SATELLITE_UPDATING,
        "machine_id": machine_id,
        "machine_name": machine.get("name", ""),
        "from_version": from_version or "unknown",
        "to_version": to_version,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })


async def _broadcast_satellite_updated(
    machine_id: str, machine: dict, version: str,
) -> None:
    """Fired after a previously-updating satellite reconnects on the new
    version. Dashboards flash a green confirmation, then dismiss."""
    await _push_machine_event(machine, {
        "type": wire.SATELLITE_UPDATED,
        "machine_id": machine_id,
        "machine_name": machine.get("name", ""),
        "version": version,
    })


async def _broadcast_satellite_update_failed(
    machine_id: str, machine: dict, error: str, rolled_back_to: str,
    *, attempts: int = 1, paused: bool = False,
) -> None:
    """Fired when an update was attempted but the satellite came back on
    the OLD version (rollback path). Dashboards render a sticky red
    banner until dismissed. ``attempts`` = consecutive rollbacks of this
    target; ``paused`` = automatic pushes of it are now suspended (the
    banner tells the admin to use "Update now")."""
    await _push_machine_event(machine, {
        "type": wire.SATELLITE_UPDATE_FAILED,
        "machine_id": machine_id,
        "machine_name": machine.get("name", ""),
        "error": error,
        "rolled_back_to": rolled_back_to,
        "attempts": attempts,
        "auto_update_paused": paused,
    })
