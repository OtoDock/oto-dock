"""Live satellite connection status — merges in-memory WS state with DB.

The DB's `remote_machines.status` column is updated on lifecycle events
(connect, disconnect, heartbeat monitor tick) but can lag reality between
ticks. The authoritative source is `SatelliteConnectionManager._connections`
plus the last frame's timestamp. This module provides helpers that merge
both so API responses reflect the current state.

State categories:
- `online` — in-memory connection present, last frame < 60s ago
- `stale`  — in-memory connection present, last frame 60-90s ago (likely dying)
- `paused` — not connected AND the deliberate-pause flag is set (tray Pause).
  Offline by intent; a friendlier label than `disconnected`. Not reachable.
- `disconnected` — not in in-memory _connections and has last_seen in DB
- `never_connected` — no row or no last_seen (machine paired but never online)
"""

import logging
import time
from collections.abc import Iterable
from typing import Any, NamedTuple

from auth import roles
from core import placement
from core.engine_records import PROXY_MANAGED

logger = logging.getLogger("otodock.remote-status")

# The live-state vocabulary, named once (core-seams phase 8; the dashboard
# mirror is ``lib/status/machine.ts``). The persisted column keeps its own
# three words (``remote_store.STATUS_*``); every route overlays this state.
ONLINE = "online"
STALE = "stale"
PAUSED = "paused"
DISCONNECTED = "disconnected"
NEVER_CONNECTED = "never_connected"
STATES: frozenset[str] = frozenset({ONLINE, STALE, PAUSED, DISCONNECTED, NEVER_CONNECTED})
#: The machine can accept commands right now.
REACHABLE: frozenset[str] = frozenset({ONLINE, STALE})

# Heartbeat age thresholds. The satellite sends a heartbeat every 30s and the
# proxy heartbeat monitor drops a machine silent for 75 s, so within 90 s. We use
# tighter UI thresholds so the dashboard shows `stale` before the DB catches up.
_ONLINE_MAX_AGE_S = 60.0
_STALE_MAX_AGE_S = 90.0
#: The one band table: a connection's state by the age of its last contact.
#: The monitor drops a silent machine inside the last band, so "a socket
#: exists" (``is_connected``, the transport's question) and "the machine is
#: there" (``REACHABLE``) cannot part for longer than the monitor's tick.
_BANDS: tuple[tuple[float, str], ...] = ((_ONLINE_MAX_AGE_S, ONLINE), (_STALE_MAX_AGE_S, STALE))


def machine_state(conn) -> tuple[str, float]:
    """``(state, age_s)`` of a live connection: its band by the age of its
    last contact (any frame). The one place the bands are read; every
    answer to "is the machine there" comes from here
    (``get_live_machine_status``, ``is_reachable``)."""
    from core.remote.satellite_connection import last_contact
    age = time.monotonic() - last_contact(conn)
    return next((state for limit, state in _BANDS if age < limit), DISCONNECTED), age


def get_live_machine_status(
    machine_id: str, *, machine: dict | None = None,
) -> dict[str, Any]:
    """Return the authoritative live status for a machine.

    Reads from the in-memory `SatelliteConnectionManager._connections` map
    first (source of truth for active WS); falls back to DB `last_seen` for
    machines not currently connected.

    ``machine`` lets a caller that already holds the row (the 30 s admin
    alert evaluator walks every machine) skip the DB read. For a CONNECTED
    machine there is no DB read at all — ``last_seen_iso`` comes from the
    connection's in-memory last contact, which is at least as fresh as the
    coalesced DB copy. Loop-side callers must never trigger a read here for
    a connected machine (see storage/pg.py's event-loop rule).

    Returns a dict:
        {
          "state": "online" | "stale" | "paused" | "disconnected"
                   | "never_connected",
          "last_heartbeat_age_s": int | None,
          "last_seen_iso": str,
          "reachable": bool,  # True iff state in {online, stale}
          "link_ok": bool,     # the local link answers (``_link``)
          "link_down_s": int | None,
          "link_error": str,
        }
    """
    from core.remote.satellite_connection import get_connection_manager
    from storage import remote_store

    cm = get_connection_manager()
    conn = cm.get_connection(machine_id)

    if conn is not None:
        state, age = machine_state(conn)
        last_seen = getattr(conn, "last_seen_iso", "") or ""
        if not last_seen:
            if machine is None:
                machine = remote_store.get_remote_machine(machine_id)
            last_seen = (machine or {}).get("last_seen") or ""
        return {
            "state": state,
            "last_heartbeat_age_s": int(age),
            "last_seen_iso": last_seen,
            "reachable": state in REACHABLE,
            **_link(conn),
        }

    if machine is None:
        machine = remote_store.get_remote_machine(machine_id)
    last_seen = (machine or {}).get("last_seen") or ""

    # Not currently connected. A deliberately-paused machine reports
    # "paused" (offline by intent — friendlier than "disconnected", and the
    # admin offline evaluator already skips it). Still not reachable.
    if machine and machine.get("paused"):
        return {
            "state": PAUSED,
            "last_heartbeat_age_s": None,
            "last_seen_iso": last_seen,
            "reachable": False,
            **_link(None),
        }

    # Distinguish never-paired/never-online from formerly-connected via
    # DB last_seen.
    if not machine or not last_seen:
        return {
            "state": NEVER_CONNECTED,
            "last_heartbeat_age_s": None,
            "last_seen_iso": last_seen,
            "reachable": False,
            **_link(None),
        }

    return {
        "state": DISCONNECTED,
        "last_heartbeat_age_s": None,
        "last_seen_iso": last_seen,
        "reachable": False,
        **_link(None),
    }


def _link(conn) -> dict[str, Any]:
    """The machine's local link: the loopback tunnel its sessions reach the
    platform through, as satellite 0.5.168 reports it. Up for an older
    satellite and a machine not connected. ``reachable`` does not read it: a
    machine whose link is down still takes commands, and its new sessions are
    refused with the reason (``core/remote/tunnel_state.py``)."""
    if conn is None or getattr(conn, "tunnel_ok", True):
        return {"link_ok": True, "link_down_s": None, "link_error": ""}
    since = getattr(conn, "tunnel_down_since", None)
    return {
        "link_ok": False,
        "link_down_s": None if since is None else int(time.monotonic() - since),
        "link_error": getattr(conn, "tunnel_error", "") or "",
    }


def is_reachable(machine_id: str) -> bool:
    """Shortcut: True iff the machine can accept commands right now."""
    return get_live_machine_status(machine_id)["reachable"]


def _is_compute(machine_id: str) -> bool:
    from storage import remote_store
    return placement.machine_is_compute(remote_store.get_remote_machine(machine_id))


def keeps_beside(agent: str, machines: Iterable[str] | None = None) -> bool:
    """Whether a session of ``agent`` must run on a compute machine beside
    the platform although one on another server could take it: the agent
    has a tool whose local address its sandbox opens
    (``mcp_egress.reaches_local_address``), and one of ``machines`` (else
    of the connected compute machines) keeps its own copy. The platform
    works that address out on its own host, and another server's network
    is not its own. The one gate of the rule: the picker asks it of its
    candidates, the runner's slot and the pre-warm checks before they wait
    or ask room, the resume of the machine its target names. With no agent
    or no such machine it reads nothing; else it reads the store: call it
    off the loop."""
    if not agent:
        return False
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if not any(cm.owns_copy(mid) for mid in (cm.compute_machine_ids() if machines is None else machines)):
        return False
    from services.mcp import mcp_egress
    return mcp_egress.reaches_local_address(agent)


def serving_compute_machine(execution_path: str = "", *, prefer: str = "",
                            shared_folder: bool = False, agent: str = "") -> dict | None:
    """The compute machine that serves the platform's place now, else None
    (none can take the session: the platform runs none itself, see
    ``compute_row_for``): an engine that runs on a machine or whose tool
    processes do (Direct LLM's loop stays in the proxy, its stdio tools go
    there); a connected compute machine whose local tunnel answers, the row's id, name,
    satellite version and the address it signed in from, as the connection holds them (the
    sign-in admitted it by its row, so no store is read: a chat's layer asks
    on the loop; the build reads the version for the tools a satellite must
    be new enough for). Read once per session build, by the builders, and
    carried in the config's placement: nothing else decides where a session
    runs.

    The balancer: ``prefer`` (the machine a chat's engine record lives on)
    takes the session while it is a candidate, so a chat stays where it
    started; otherwise the machine with the most room for a session
    (``concurrency.box_room_mb``), ties by id. A machine on another host
    (its own copy of the folders) is never a candidate for a session that
    needs the platform's own folder: ``shared_folder`` (an outside person's
    session, a call, a meeting's participant, a wrap-up, an app server or
    step, a check's script, a judge) and an engine whose tools the proxy
    runs (Direct LLM: its loop and file tools read the platform's copy).
    Nor for a session of ``agent`` when a tool of it reaches a local
    address (``keeps_beside``, asked of these candidates: no machine that
    signs in between a check and the pick can take the session); it wins
    over ``prefer``. Naming ``agent`` reads the store while a machine on
    another host is a candidate: such a caller runs off the loop."""
    if execution_path:
        from core.session.session_manager import get_layer_capabilities
        caps = get_layer_capabilities(execution_path)
        if caps is not None and not (caps.runtime.supports_remote_execution
                                     or caps.mcp_delivery == PROXY_MANAGED):
            return None
        if caps is not None and caps.mcp_delivery == PROXY_MANAGED:
            shared_folder = True
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    candidates = sorted(mid for mid in cm.compute_machine_ids()
                        if cm.tunnel_ok(mid) is not False and (not shared_folder or cm.shares_folders(mid)))
    if not shared_folder and keeps_beside(agent, candidates):
        candidates = [mid for mid in candidates if cm.shares_folders(mid)]
    if not candidates:
        return None
    if prefer in candidates:
        pick = prefer
    elif len(candidates) == 1:
        pick = candidates[0]
    else:
        from core import concurrency
        pick = sorted(candidates, key=lambda mid: (-concurrency.box_room_mb(mid), mid))[0]
    return {"id": pick, "name": cm.satellite_name(pick),
            "satellite_version": cm.satellite_version(pick),
            "pairing_scope": placement.PAIRING_COMPUTE,
            "compute_address": cm.compute_address(pick),
            "capabilities": {"session_home_dirs": sorted(cm.satellite_session_home_dirs(pick))}}


def compute_row_for(target: str | None, execution_path: str = "", *, prefer: str = "",
                    shared_folder: bool = False, agent: str = "") -> dict | None:
    """The compute machine a session resolved to ``target`` is built on:
    None for a session on a paired computer (or one that is offline, whose
    sentinel the doors refuse), else the machine that serves the platform's
    place (``serving_compute_machine``, ``agent`` the session's). The one
    reader for every builder, so the refusal is raised in one place:
    ``placement.NoComputeMachine`` when the session is the platform's and no
    compute machine can take it."""
    if not placement.is_local(target):
        return None
    row = serving_compute_machine(execution_path, prefer=prefer, shared_folder=shared_folder,
                                  **({"agent": agent} if agent else {}))
    if row is None:
        raise placement.NoComputeMachine()
    return row


def any_own_copy() -> bool:
    """Whether a compute machine on another host is connected: only then
    does a chat's own machine (``prefer``) say anything, so an install with
    none reads no chat for it."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    return any(cm.owns_copy(mid) for mid in cm.compute_machine_ids())


class ChatPlace(NamedTuple):
    """Where a chat's next session goes among the compute machines, from its
    row: ``machine`` the one on another host its engine record lives on
    (``chats.compute_machine``), ``shared`` a machine that shares the
    platform's folders, since its record lives there (it has one and no
    machine on another host is recorded). A chat with no record goes where
    the room is. ``stays``: its record lives on ``machine`` and the chat
    continues delegated or task work, whose next round resumes by id and
    never reads a digest, so it stays there even for an agent whose tool
    reaches a local address (as a move keeps such a lane,
    ``chat_move._lane_without_memory``)."""
    machine: str
    shared: bool
    stays: bool = False

    def agent_for(self, agent: str) -> str:
        """The agent a door names to ``serving_compute_machine``: ``""``
        for a chat that stays on its record's machine."""
        return "" if self.stays else agent


def chat_place(chat: dict | None) -> ChatPlace:
    """The chat's place for ``serving_compute_machine`` (``prefer``,
    ``shared_folder`` and its ``agent``), from a row the caller already
    holds."""
    from core.session import session_kind
    row = chat or {}
    machine = str(row.get("compute_machine") or "")
    record = bool(row.get("session_id") or row.get("codex_thread_id"))
    work = bool(row.get("parent_chat_id")) or session_kind.of_chat(row) is session_kind.TASK
    return ChatPlace(machine, record and not machine, bool(machine) and work)


def resolve_execution_target(
    agent_slug: str,
    user_sub: str | None = None,
    role: str = roles.MANAGER,
) -> tuple[str, str | None]:
    """Resolve effective execution target: user override > agent default > local.

    Returns (target, fallback_reason) where:
    - target is a machine_id, ``placement.LOCAL``, or the offline sentinel
      (``placement.offline_sentinel``) when the intended remote target is
      unreachable and fallback is disabled.
    - fallback_reason is a short slug describing *why* the target differs from
      the user-configured/admin-configured intent. Values:
        * None — target matches intent (user override or agent default)
        * "user-override-offline" — user override exists but machine is offline
          and fallback-to-agent-default is enabled
        * "agent-default-offline" — agent-default machine offline and fallback
          to local is enabled
        * "viewer-on-admin-remote" — a role below the owner tier (viewer,
          contributor, editor) kept in the platform's place, the sandbox on
          a compute machine: a paired machine runs the engines with no
          sandbox. The slug is historical. Their own paired machine (step 1)
          is still honored: they own the hardware
        * "all-roles-on-admin-remote" — such a person's session runs on the
          agent's default machine because its admin let every role of its
          agents run there (``placement.admits_every_role``)

    The 'role' parameter is the caller's effective per-agent role for this
    agent (admin/manager/editor/contributor/viewer). Below the owner tier a
    session runs on the agent's admin machine only by that machine's switch,
    and a session with no person (an outside caller) never does.
    """
    from storage import database as _db, remote_store
    from storage.agents import agent_store

    # The Direct LLM engine runs in-process on the server, never on a
    # satellite (``session_manager.get_execution_layer`` returns it before
    # any target and remote_session_start refuses it), so a direct-llm
    # agent's target is the platform's place whatever its pin or the user's
    # override says. Deciding it HERE covers every builder (chat, task,
    # meeting, phone), the dashboard's pin/mismatch logic and the scheduler
    # in one place, so none of them shapes a paired machine's prompt or
    # device tools for a session that runs on the server.
    agent = agent_store.get_agent(agent_slug)
    from core.session.session_manager import get_layer_capabilities
    _ec = get_layer_capabilities((agent or {}).get("execution_path") or "")
    if _ec is not None and not _ec.runtime.supports_remote_execution:
        if not placement.is_local((agent or {}).get("execution_target")):
            logger.info(
                "resolve_execution_target: %s runs on the Direct LLM engine — "
                "its remote target is ignored (always local)", agent_slug,
            )
        return (placement.LOCAL, None)

    fallback_user = _db.get_platform_setting("remote_fallback_user_override")
    fallback_agent = _db.get_platform_setting("remote_fallback_agent_default")
    allow_fallback_user = fallback_user != "0"    # default true
    allow_fallback_agent = fallback_agent == "1"  # default false

    # 1. Check user-level override. Works for all roles (viewer included) —
    #    user paired their own hardware.
    if user_sub:
        user_target = remote_store.get_user_remote_target(user_sub, agent_slug)
        if user_target and _is_compute(user_target["machine_id"]):
            # Never written by the store; a row edited by hand is no place.
            user_target = None
        if user_target:
            machine_id = user_target["machine_id"]
            if is_reachable(machine_id):
                return (machine_id, None)
            # User's machine is offline. Fall through to agent-default if
            # allowed, else hard-fail with sentinel.
            logger.warning(
                "User %s remote target %s is offline, fallback_user=%s",
                user_sub[:16], machine_id[:8], allow_fallback_user,
            )
            if not allow_fallback_user:
                return (placement.offline_sentinel(machine_id), "user-override-offline-hard-fail")
            # Soft fall-through to agent default — continues below with reason.
            _user_override_fallback = True
        else:
            _user_override_fallback = False
    else:
        _user_override_fallback = False

    # 2. Agent-level default (the row was loaded above)
    agent_target = (agent or {}).get("execution_target") or placement.LOCAL
    if not placement.is_local(agent_target) and _is_compute(agent_target):
        # A compute machine is never a resolved target: the platform's place
        # is ``local``, and a session reaches the compute machine that
        # serves it through its own record.
        agent_target = placement.LOCAL

    if placement.is_local(agent_target):
        if _user_override_fallback:
            return (placement.LOCAL, "user-override-offline")
        return (placement.LOCAL, None)

    # 3. Agent-set remote target. A satellite runs the engines with no
    #    sandbox, on every OS: the path gate applies a role's limits to the
    #    paths it sees, and a command it allows reads whatever the machine's
    #    user reads. So below the owner tier a session stays in the
    #    platform's place unless the machine's admin let every role of its
    #    agents run there (``placement.admits_every_role``), and only a
    #    PERSON's session follows that switch: one with no person below the
    #    owner tier is an outside caller (a phone call's external identity),
    #    which stays in the platform's place in every case. A machine the person paired themselves
    #    is step 1's (their own hardware).
    on_switch = False
    if not roles.can_manage(role):
        on_switch = bool(user_sub) and placement.admits_every_role(
            remote_store.get_remote_machine(agent_target), agent_target)
        if not on_switch:
            return (placement.LOCAL, "viewer-on-admin-remote")  # the slug is historical
        logger.info(
            "resolve_execution_target: %s as %s runs on %s by the machine's "
            "every-role switch", agent_slug, role, agent_target[:8],
        )

    # 4. An owner (manager/admin), or a person the switch admits — honor if reachable.
    if is_reachable(agent_target):
        if _user_override_fallback:
            return (agent_target, "user-override-offline")
        return (agent_target, "all-roles-on-admin-remote" if on_switch else None)

    # Agent target offline.
    if allow_fallback_agent:
        return (placement.LOCAL, "agent-default-offline")
    return (placement.offline_sentinel(agent_target), "agent-default-offline-hard-fail")
