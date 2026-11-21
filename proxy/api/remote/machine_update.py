"""Where a paired machine stands on the satellite version the platform sends.

One reading, ``machine_update_state``, for both update-now routes, the push over
the socket (``remote_machines.push_update_online``) and every machine row, so
the card and a press never disagree; ``update_refusal`` is a press's answer.
"""

from core import placement

#: The refusal of an update to a machine on a plaintext link, in the words of
#: the auth path's (``ws/satellite.py``): the satellite takes no code over it.
_PLAINTEXT_UPDATE_REFUSAL = (
    "This machine's link is plaintext, so no update is pushed to it: "
    "re-run the installer there."
)


def _plaintext_link(conn, machine: dict) -> bool:
    """Whether the machine runs on a plaintext link it opted into
    (``insecure_transport``): the live connection's capabilities, else the
    ones it last reported (the row's JSON column)."""
    if conn is not None:
        caps = getattr(conn, "capabilities", None) or {}
    else:
        caps = machine.get("capabilities") or {}
        if isinstance(caps, str):
            import json
            try:
                caps = json.loads(caps)
            except ValueError:
                caps = {}
    return isinstance(caps, dict) and bool(caps.get("insecure_transport"))


class UpdateRefused(Exception):
    """An update the machine's state refuses. The message is the sentence a
    press is answered with."""


def machine_update_state(machine_id: str, machine: dict, conn) -> str:
    """Where the machine stands on the version the platform sends, the one
    reading of it the update-now routes, the push over the socket and the
    machine cards share (``conn``: its live connection, None when away):

    ``""`` no update is offered (no satellite source, a compute machine);
    ``current`` it runs that version or newer (a live connection's version
    decides over the row's); ``running`` an update sent to it has not ended
    (``MachineLinks.update_running``); ``manual`` its link is plaintext, so
    only the installer updates it; ``refused`` away because its last sign-in
    was refused for a version below the platform's minimum
    (``machine_away.note_refused``), no session runs there until it updates;
    ``queued`` away with an update waiting for its next connect; ``waiting``
    the update waits for its sessions to end; ``available`` anything else,
    an unknown version included."""
    from core.remote.satellite_connection import get_connection_manager
    from ws.satellite import SATELLITE_VERSION_LATEST, _version_at_least
    latest = SATELLITE_VERSION_LATEST
    if latest is None or placement.machine_is_compute(machine):
        return ""
    if conn is not None and _version_at_least(str(getattr(conn, "satellite_version", "") or ""), latest):
        return "current"
    links = get_connection_manager().links
    if links.update_running(machine_id):
        return "running"
    if conn is None and _version_at_least(str(machine.get("satellite_version") or ""), latest):
        return "current"
    if _plaintext_link(conn, machine):
        return "manual"
    from services.remote import machine_away
    refused = machine_away.refused_version(machine_id) if conn is None else None
    if refused is not None and refused[1]:
        return "manual"
    if refused is not None and not machine.get("pending_update"):
        return "refused"
    if conn is None and machine.get("pending_update"):
        return "queued"
    if conn is not None and links.update_waiting(machine_id):
        return "waiting"
    return "available"


def update_refusal(state: str) -> str:
    """The sentence a press is answered with in ``state``, ``""`` when the
    update may go."""
    if state == "current":
        from ws.satellite import SATELLITE_VERSION_LATEST
        return f"This machine already runs the newest version, {SATELLITE_VERSION_LATEST}."
    return {
        "running": "This machine is already updating.",
        "manual": _PLAINTEXT_UPDATE_REFUSAL,
        "queued": "An update is already queued for this machine. It runs when the machine connects.",
    }.get(state, "")
