"""A machine that cannot serve a session yet, and what the person is told.

* ``note_refused`` / ``refused_version``: a paired machine refused at its
  sign-in for its version (below ``MIN_SATELLITE_VERSION`` with automatic
  updates off or paused, or on a plaintext link no update reaches). The
  record is in memory: it clears at the machine's next accepted sign-in and
  at its removal, and a restart of the platform loses it until the machine's
  next attempt, a few seconds later. ``api/remote/machine_update`` reads it
  as the update state ``refused``.
* ``finishing_update``: a machine still in its update. One was sent and it
  has not signed in on it yet (``MachineLinks.update_running``), or it signed
  in and its credential exit still installs its packages (``exit_why``
  ``installing``). It counts as away for that time.
* ``away_sentence``: the one sentence every door that cannot start work on
  an absent machine gives for such a machine, and its own sentence for any
  other; ``sentence_for_chat`` gives it for the machine a chat's row names.
* ``settled_placement``: the placement a session start builds on. A machine
  in its update gets a bounded wait, and the start goes on as if nothing
  happened once it is done. Past the bound the start is refused with the
  update's sentence (``MachineAway``). A start is marked on the
  machine's link meanwhile, so a waiting update is not pushed under it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time

from core import placement
from core.placement import PlacementCapabilities
from core.remote.tunnel_state import EXIT_INSTALLING

logger = logging.getLogger("otodock.remote")

#: A refused machine: (the version it signed in with, plaintext link).
_refused: dict[str, tuple[str, bool]] = {}

#: What a person reads about a machine its version keeps out: the machine
#: an admin updates, and one on a plaintext link, which the installer does.
NEEDS_ADMIN = "{label} needs an update. Ask an admin to update it, then try again."
NEEDS_INSTALLER = "{label} needs an update. Run the OtoDock installer on it again, then try again."
#: What a person reads about a machine still in its update, past the wait.
FINISHING = "{label} is finishing its update. Try again in a minute."


class MachineAway(placement.PlacementRefused):
    """A start refused because its machine is away for a reason this module
    knows: its version keeps it out, or it is still in its update past the
    wait. The message is ``away_sentence``'s. Every door handles it as the
    refusal it is."""


def label_at_start(label: str) -> str:
    """A machine's name at the start of a sentence, "This computer" for one
    with no name."""
    return label or "This computer"


def label_inside(label: str) -> str:
    """A machine's name inside a sentence, "this computer" for one with no
    name."""
    return label or "this computer"


def note_refused(machine_id: str, version: str, *, plaintext: bool) -> None:
    """The machine was refused at its sign-in for its version."""
    if machine_id:
        _refused[machine_id] = (version, plaintext)


def clear_refused(machine_id: str) -> None:
    """Its sign-in was accepted, or it was removed."""
    _refused.pop(machine_id, None)


def refused_version(machine_id: str) -> tuple[str, bool] | None:
    """``(version, plaintext)`` of a machine refused for its version, else None."""
    return _refused.get(machine_id)


def finishing_update(machine_id: str) -> bool:
    """Whether the machine is still in its update: one was sent and it has
    not signed in on it yet, or it signed in and its credential exit still
    installs its packages. Reads memory alone."""
    if not machine_id:
        return False
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if cm.links.update_running(machine_id):
        return True
    return cm.is_connected(machine_id) and str(cm.exit_report(machine_id).get("why") or "") == EXIT_INSTALLING


def held_out(machine_id: str) -> bool:
    """Whether a door's away sentence for this machine is one of this
    module's: its version keeps it out, or it is still in its update."""
    return (machine_id or "") in _refused or finishing_update(machine_id)


def away_sentence(machine_id: str, label: str, default: str) -> str:
    """What a door tells the person when it cannot start work on this
    machine: the update sentence for a machine its version keeps out, the
    finishing sentence for one still in its update, ``default`` (the door's
    own) for any other."""
    held = _refused.get(machine_id or "")
    if held is not None:
        return (NEEDS_INSTALLER if held[1] else NEEDS_ADMIN).format(label=label_at_start(label))
    if finishing_update(machine_id):
        return FINISHING.format(label=label_at_start(label))
    return default


def sentence_for_chat(row: dict | None, default: str, *, label: str = "") -> str:
    """``away_sentence`` for the machine a chat's row names
    (``execution_target``), with the machine's name read when ``label`` is
    not given. Blocking only for a machine its version keeps out or that is
    still in its update, whose name is read from its row."""
    machine = placement.machine_of((row or {}).get("execution_target") or "")
    if not machine or not held_out(machine):
        return default
    if not label:
        from services.remote import chat_targets
        label = chat_targets.label_of(machine)
    return away_sentence(machine, label, default)


#: How long a session start waits for a machine still in its update, how
#: often it reads the machine's state meanwhile, and how long a build holds
#: the machine for its start (``MachineLinks.hold_for_start``).
UPDATE_WAIT_S = 120.0
_POLL_S = 1.0
START_HOLD_S = 120.0


async def _waited_out(machine: str, *, bound: float, step: float) -> bool:
    """Wait while ``machine`` is in its update or away for its restart, up
    to ``bound`` seconds, holding it for the start meanwhile: True once it
    is back and done, False past the bound."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    logger.info("Satellite %s is still in its update; the session start waits up to %.0f s",
                placement.log_id(machine), bound)
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        await asyncio.sleep(step)
        cm.links.hold_for_start(machine, START_HOLD_S)
        if not finishing_update(machine) and cm.is_connected(machine):
            return True
    logger.info("Satellite %s is still in its update after %.0f s; the start is refused",
                placement.log_id(machine), bound)
    return False


async def update_done(machine_id: str, *, bound: float = UPDATE_WAIT_S, step: float = _POLL_S) -> None:
    """A start toward ``machine_id`` (a chat's pinned machine, before its
    reachability is read) waits while the machine is in its update, its
    restart included, and is refused with ``FINISHING`` past ``bound``. A
    machine that is not in its update passes at once, reachable or not."""
    if not machine_id or not finishing_update(machine_id):
        return
    from core.remote.satellite_connection import get_connection_manager
    get_connection_manager().links.hold_for_start(machine_id, START_HOLD_S)
    if await _waited_out(machine_id, bound=bound, step=step):
        return
    from services.remote import chat_targets
    label = await asyncio.to_thread(chat_targets.label_of, machine_id)
    raise MachineAway(away_sentence(machine_id, label, FINISHING.format(label=label_at_start(label))))


async def settled_placement(target: str, user_sub: str | None, agent: str, *, compute: dict | None = None,
                            bound: float = UPDATE_WAIT_S, step: float = _POLL_S) -> PlacementCapabilities:
    """The placement of one session start: ``remote_store.placement_of`` for
    ``target``, read again once a machine still in its update is done
    (``finishing_update``, read every ``step`` seconds). The start is marked
    on the machine's link from here, so its waiting update is not pushed
    under it. Past ``bound`` the start is refused with ``FINISHING``
    (``MachineAway``): no session is built there with tools left out.
    Every session build reads its placement here."""
    from storage import remote_store
    read = functools.partial(remote_store.placement_of, target, user_sub, agent,
                             **({"compute": compute} if compute else {}))
    where = await asyncio.to_thread(read)
    machine = where.machine_id if where.is_remote else ""
    if not machine:
        return where
    from core.remote.satellite_connection import get_connection_manager
    get_connection_manager().links.hold_for_start(machine, START_HOLD_S)
    if where.exit_why != EXIT_INSTALLING and not finishing_update(machine):
        return where
    if await _waited_out(machine, bound=bound, step=step):
        where = await asyncio.to_thread(read)
        if where.exit_why != EXIT_INSTALLING:
            return where
    raise MachineAway(away_sentence(machine, where.label, FINISHING.format(label=label_at_start(where.label))))
