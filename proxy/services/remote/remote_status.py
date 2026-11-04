"""Live satellite connection status — merges in-memory WS state with DB.

The DB's `remote_machines.status` column is updated on lifecycle events
(connect, disconnect, heartbeat monitor tick) but can lag reality between
ticks. The authoritative source is `SatelliteConnectionManager._connections`
plus `last_heartbeat` timestamp. This module provides helpers that merge
both so API responses reflect the current state.

State categories:
- `online` — in-memory connection present, heartbeat < 60s ago
- `stale`  — in-memory connection present, heartbeat 60-90s ago (likely dying)
- `paused` — not connected AND the deliberate-pause flag is set (tray Pause).
  Offline by intent; a friendlier label than `disconnected`. Not reachable.
- `disconnected` — not in in-memory _connections and has last_seen in DB
- `never_connected` — no row or no last_seen (machine paired but never online)
"""

import logging
import time
from typing import Any

from auth import roles
from core import placement

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
# proxy heartbeat monitor flips DB status to `disconnected` after 90s. We use
# tighter UI thresholds so the dashboard shows `stale` before the DB catches up.
_ONLINE_MAX_AGE_S = 60.0
_STALE_MAX_AGE_S = 90.0


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
        age = time.monotonic() - conn.last_heartbeat
        if age < _ONLINE_MAX_AGE_S:
            state = ONLINE
        elif age < _STALE_MAX_AGE_S:
            state = STALE
        else:
            state = DISCONNECTED
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
          contributor, editor) kept on the platform host: a machine runs the
          engines with no sandbox. The slug is historical. Their own paired
          machine (step 1) is still honored: they own the hardware
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
    # satellite (session_manager.get_layer routes it local unconditionally
    # and remote_session_start refuses it), so a direct-llm agent's target is
    # local whatever its pin or the user's override says. Deciding it HERE
    # covers every builder (chat, task, meeting, phone), the dashboard's
    # pin/mismatch logic and the scheduler in one place — before the fix each
    # of them believed the pin and shaped a remote placement prompt + device
    # MCP set for a session that then ran on the server (2026-09-07).
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

    if placement.is_local(agent_target):
        if _user_override_fallback:
            return (placement.LOCAL, "user-override-offline")
        return (placement.LOCAL, None)

    # 3. Agent-set remote target. A satellite runs the engines with no
    #    sandbox, on every OS: the path gate applies a role's limits to the
    #    paths it sees, and a command it allows reads whatever the machine's
    #    user reads. So below the owner tier a session stays on the platform
    #    host unless the machine's admin let every role of its agents run
    #    there (``placement.admits_every_role``), and only a PERSON's session
    #    follows that switch: one with no person below the owner tier is an
    #    outside caller (a phone call's external identity), which stays on the
    #    platform host in every case. A machine the person paired themselves
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
