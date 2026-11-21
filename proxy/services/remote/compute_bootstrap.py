"""The install's own compute machine: its row, its credential, the marker
it checks the agents folder by, and the boot's wait for it.

A compose install runs the compute service beside the proxy, and a
bare-metal install the compute unit; neither is paired by a person. At every
boot with a credential folder named (``OTODOCK_COMPUTE_LINK_DIR``, set by
the compose file or the bare-metal setup), ``ensure`` makes the row once (its id kept in
``platform_settings``) and writes ``<folder>/credential`` (JSON:
``machine_id``, ``machine_secret``, ``marker``) when the file is missing, its
secret does not verify against the row's hash (a restore onto a new host, a
lost volume) or its marker is not the agents folder's: the secret is minted
again, ``remote_store.mint_compute_secret`` the one writer of its hash. The
compute machine's start (``compute_start.py``) reads the file and refuses an
agents folder whose marker differs, so a mount the proxy and the compute
machine do not share is caught at its start. The proxy reads nothing the
compute machine can write: the folder is read-only to it.

The marker is ``AGENTS_DIR/.otodock-platform``: a paired satellite refuses
to start on a folder that carries it, and its content is a token written
once and kept. The compute machine can write that folder, so the marker is
read without following a link, only from a regular file, at most 64 bytes,
and kept only when it is a 32-hex token; anything else is replaced. No other
value read from the agents folder reaches the credential.

A proxy restart leaves the compute machine's sessions running. The start's
hold (``drain.hold_for_start``) waits for the install's own compute machine
to sign in and send its first complete alive report, at most ``OWN_WAIT_S``:
nothing runs before it. When the reloaded session index names a session
placed on a compute machine, the hold waits for that machine too (the
re-adoption runs on its report), at most ``BOOT_WAIT_S``: a turn that came
first would start a second process on the same transcript. Past the bound
the hold ends, and a session of the platform's place is refused until a
compute machine signs in (``placement.NoComputeMachine``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import stat
from pathlib import Path

import config

logger = logging.getLogger("otodock")

#: The platform setting that names the install's own compute machine's row.
OWN_MACHINE_KEY = "compute_own_machine_id"
#: The row's name on the Remote Machines page.
OWN_MACHINE_NAME = "Compute"
#: The credential file in ``OTODOCK_COMPUTE_LINK_DIR``.
CREDENTIAL_NAME = "credential"
#: How long the start's hold waits for a compute machine named by the
#: reloaded session index: its link comes back after the proxy's, and its
#: sign-in and report take a while on a loaded host.
BOOT_WAIT_S = 300.0
# The start's wait for the install's own compute machine when the reloaded
# index names no session on it: it signs in within seconds of the proxy.
OWN_WAIT_S = 60.0

_expected: set[str] = set()
_back: asyncio.Event | None = None
_bound_s = BOOT_WAIT_S


def marker_path() -> Path:
    from core.sandbox.sandbox_render import PLATFORM_MARKER
    return config.AGENTS_DIR / PLATFORM_MARKER


_TOKEN = re.compile(r"[0-9a-f]{32}")
_MARKER_MAX_BYTES = 64


def _held_token(path: Path) -> str:
    """The marker's token, ``""`` when it is not a 32-hex token in a regular
    file: a link is not followed and a FIFO does not block the read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        raw = os.read(fd, _MARKER_MAX_BYTES + 1)
    finally:
        os.close(fd)
    token = raw.decode("ascii", errors="replace").strip()
    return token if len(raw) <= _MARKER_MAX_BYTES and _TOKEN.fullmatch(token) else ""


def _replace_marker(path: Path) -> str:
    """A fresh token in a new regular file, whatever stood at the path."""
    token = secrets.token_hex(16)
    for _ in range(3):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except OSError:
            continue  # something was put back in between: clear it again
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        return token
    # The folder keeps refusing a file of the platform's: the credential names
    # a token the folder does not hold, and the compute machine refuses it.
    logger.warning("Compute: the agents folder's marker could not be written (%s)", path)
    return token


def ensure_marker() -> str:
    """Mark the platform's agents folder and return its token: a released
    install's empty marker, or anything that is not a token, is replaced
    once; a token is kept."""
    path = marker_path()
    config.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    return _held_token(path) or _replace_marker(path)


def runs_own() -> bool:
    """Whether this install runs a compute machine of its own."""
    return bool(config.OTODOCK_COMPUTE_LINK_DIR)


def own_machine_id() -> str:
    """The install's own compute machine's row id, ``""`` before the first
    boot that made it (or on an install that runs none)."""
    from storage import database
    return database.get_platform_setting(OWN_MACHINE_KEY) or ""


def _own_row() -> str:
    """The install's own compute row, made once."""
    from storage import database, remote_store
    machine_id = own_machine_id()
    if machine_id and remote_store.get_remote_machine(machine_id):
        return machine_id
    machine_id = f"compute-{secrets.token_hex(6)}"
    remote_store.create_remote_machine(machine_id, OWN_MACHINE_NAME, None, pairing_scope="compute")
    database.set_platform_setting(OWN_MACHINE_KEY, machine_id)
    logger.info("Compute: registered the install's own compute machine %s", machine_id)
    return machine_id


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    """The file in one rename, readable by its owner alone (the compute
    machine runs as the same user id)."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def ensure() -> str:
    """The row and the credential file, minted again when the file does not
    hold a secret the row verifies or the agents folder's marker. Returns the
    row's id, ``""`` when the install runs no compute machine of its own.
    Synchronous: the boot runs it on a worker thread."""
    if not runs_own():
        return ""
    from storage import remote_store
    marker = ensure_marker()
    folder = Path(config.OTODOCK_COMPUTE_LINK_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / CREDENTIAL_NAME
    machine_id = _own_row()
    held = _read(path)
    if (held.get("machine_id") == machine_id and held.get("marker") == marker
            and isinstance(held.get("machine_secret"), str)
            and remote_store.verify_machine_secret(machine_id, held["machine_secret"])):
        return machine_id
    secret = remote_store.mint_compute_secret(machine_id)
    _write(path, {"machine_id": machine_id, "machine_secret": secret, "marker": marker})
    logger.info("Compute: the compute machine's credential was written (%s)", path)
    return machine_id


def load_addresses() -> None:
    """The addresses the compute machines on other servers last signed in
    from, handed to ``deployment`` (the sign-in's local-network rule reads
    them on the loop): at boot, after a pairing, a new address or a removal."""
    from core.config import deployment
    from storage import remote_store
    deployment.set_compute_addresses(remote_store.compute_addresses())


async def unlink(machine_id: str) -> None:
    """Before a compute machine's row is removed: its headless sessions close
    through the remote layer (a turn in flight ends there) and its terminals
    through the interactive close (rather than waiting out the reconnect
    window as "reconnecting"), then its link closes with 4006 and is
    deregistered, so no new session is sent to it. A compute machine ends on
    4006 with exit 75 and never uninstalls itself; the removed row refuses it
    at its next sign-in."""
    import contextlib

    from core.remote import remote_pty
    from core.remote.satellite_connection import get_connection_manager
    from core.session import interactive_session
    from core.session.session_manager import _get_remote_layer
    cm = get_connection_manager()
    remote = _get_remote_layer()
    for sid in list(remote.session_ids_on(machine_id)):
        try:
            await remote.close_session(sid)
        except Exception:
            logger.exception("Compute: closing session %s on the removed machine failed", sid[:8])
    for sid in sorted(remote_pty.machine_pty_session_ids(machine_id)):
        try:
            await interactive_session.close_session(sid, reason="machine_removed")
        except Exception:
            logger.exception("Compute: closing terminal %s on the removed machine failed", sid[:8])
    conn = cm.get_connection(machine_id)
    if conn is None:
        return
    with contextlib.suppress(Exception):
        await conn.ws.close(code=4006, reason="machine_deleted")
    await cm.deregister(machine_id)
    logger.info("Compute: the removed machine %s was unlinked", machine_id)


def is_own(machine_id: str) -> bool:
    """Whether ``machine_id`` is the install's own compute machine (its
    sign-in is kept by the install: the human route refuses it)."""
    return bool(machine_id) and machine_id == own_machine_id()


# ---------------------------------------------------------------------------
# A short restart of the install's own compute machine
# ---------------------------------------------------------------------------

def back_soon() -> bool:
    """Whether the install's own compute machine is away and may still be
    coming back: it dropped within its reconnect window (a restart of its
    service, an update). Past the window it reads as away."""
    own = own_machine_id() if runs_own() else ""
    if not own:
        return False
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    return not cm.is_connected(own) and cm.machine_dropped_recently(own)


def chat_waits(chat: dict | None) -> bool:
    """Whether a message in ``chat`` waits for the install's own compute
    machine instead of being refused: the chat is the platform's, its
    session ended, nothing can start it again now and that machine dropped
    a moment ago. Its message waits in the chat's queue and goes out at
    the sign-in (``_arm_waiting``). Synchronous: callers run it on the
    store's executor."""
    from core.session import session_manager
    return bool(chat and chat.get("id") and back_soon() and session_manager.no_compute_for(chat))


async def _arm_waiting() -> None:
    """A compute machine signed in: every chat that holds waiting messages
    is armed, as at a boot (``continue_pass.arm_queues``), so a message that
    waited through the machine's absence goes out, whatever made it wait:
    a session the machine held in its reconnect window, or one that had
    ended (``chat_waits``). The reconnect pass arms the chats pinned to a
    machine, and a compute machine is never a pin."""
    from services.scheduler import continue_pass
    try:
        armed = await continue_pass.arm_queues()
    except Exception:
        logger.exception("Compute: the chats with waiting messages were not armed at the sign-in")
        return
    if armed:
        logger.info("Compute: %d chat(s) with waiting messages armed at the compute machine's sign-in", armed)


# ---------------------------------------------------------------------------
# The boot's wait
# ---------------------------------------------------------------------------

def _may_come_back(session_id: str) -> bool:
    """Whether a session placed on a compute machine can be there after a
    restart: not one whose engine runs no process (Direct LLM: its tools
    closed with the link and the session is rebuilt), and not one with no
    chat row (a Direct participant, a wrap-up, a button's or a pre-warm's
    session): the hold has nothing to adopt it into."""
    from services.scheduler import run_recovery
    from storage import database as task_store
    chat = task_store.get_chat_by_session(session_id)
    return chat is not None and not run_recovery.chat_runs_no_process(chat)


def expect_back() -> bool:
    """At boot, after the session index is reloaded: the compute machines
    its sessions are placed on that can come back (a row that still exists;
    the install's own only while it runs one). True when the start's hold
    must wait."""
    global _back, _bound_s
    from core.session import session_state
    from storage import remote_store
    _expected.clear()
    own = own_machine_id()
    _expected.update(m for m in session_state.compute_machines_placed(_may_come_back)
                     if remote_store.get_remote_machine(m) and (m != own or runs_own()))
    _bound_s = BOOT_WAIT_S if _expected else OWN_WAIT_S
    if runs_own() and own:
        # Nothing runs until the install's own compute machine signed in:
        # the start holds new work for it, sessions to adopt or not.
        _expected.add(own)
    _back = asyncio.Event() if _expected else None
    if _expected:
        logger.info("Startup: waiting for the compute machine%s %s before new work starts",
                    "s" if len(_expected) > 1 else "", ", ".join(sorted(_expected)))
    return bool(_expected)


async def on_alive_report(report) -> None:
    """The alive-report hook: a compute machine's complete report wakes
    what waits for one (``concurrency.compute_signed_in``: a task parked on
    its box, a run that waits for a machine, an install that waits for its
    build; ``_arm_waiting``: a chat's message that waited through a short
    restart; ``tool_listing.retry_owed``: a tool that could not be listed
    without one), and an expected machine's (its sessions adopted) ends the
    start's wait for it."""
    machine_id = getattr(report, "machine_id", "")
    if not getattr(report, "complete", False):
        return
    from core.remote.satellite_connection import get_connection_manager
    if get_connection_manager().is_compute(machine_id):
        from core import concurrency
        concurrency.compute_signed_in(machine_id)
        await _arm_waiting()
        from services.mcp import tool_listing
        tool_listing.retry_owed()
    if machine_id not in _expected:
        return
    _expected.discard(machine_id)
    logger.info("Startup: the compute machine %s is back%s", machine_id,
                "; new work starts" if not _expected else "")
    if not _expected and _back is not None:
        _back.set()


def waiting() -> bool:
    """Whether a compute machine the reloaded session index names has not
    sent its first complete report yet: until then the sessions it runs are
    in no registry of this process (the retention pass keeps their files)."""
    return bool(_expected)


def give_up(machine_id: str) -> None:
    """A compute machine the platform refused at its sign-in (its lock, its
    image, its removal) sends no report this boot: the wait stops waiting
    for it."""
    if machine_id not in _expected:
        return
    _expected.discard(machine_id)
    logger.info("Startup: the compute machine %s was refused at its sign-in; no wait for it", machine_id)
    if not _expected and _back is not None:
        _back.set()


async def await_back(bound_s: float | None = None) -> None:
    """Wait for every expected compute machine's report, at most ``bound_s``
    (``BOOT_WAIT_S`` when the reloaded index names sessions on one,
    ``OWN_WAIT_S`` for the install's own with none)."""
    if _back is None or not _expected:
        return
    bound = _bound_s if bound_s is None else bound_s
    try:
        await asyncio.wait_for(_back.wait(), timeout=bound)
    except asyncio.TimeoutError:
        logger.warning("Startup: the compute machine %s did not sign in within %.0f s: new work "
                       "is refused until one does", ", ".join(sorted(_expected)), bound)
        _expected.clear()


def register() -> None:
    """Boot: the alive-report hook the wait ends on."""
    from core.remote import satellite_link
    satellite_link.add_alive_report_hook(on_alive_report, name="compute_back")


def reset_for_tests() -> None:
    global _back
    _expected.clear()
    _back = None
