"""A satellite's local link: the loopback tunnel its sessions reach the
platform through (``PROXY_URL``, every rewritten MCP address).

Satellite 0.5.168 checks that link every heartbeat and reports four fields in
the heartbeat and the auth capabilities: ``tunnel_ok``, ``local_tunnel_port``,
``tunnel_rebinds`` and ``last_tunnel_error``. An older satellite reports none
and is never judged. The connection keeps the state; a link reported down
refuses new sessions on the machine (``TunnelDown``) until a heartbeat says it
answers again, and a session started on a port the machine no longer serves
is dead to the platform (its environment and MCP addresses hold that port):
its next message resumes it on the live one.
"""

import logging
import time
from urllib.parse import urlsplit

logger = logging.getLogger("otodock.remote.tunnel")

_ERROR_MAX = 200


class TunnelDown(RuntimeError):
    """A start refused on a machine whose local link is down."""


def refusal(machine_name: str) -> str:
    return (
        f"{machine_name} cannot start a session right now. Its sessions cannot "
        "reach OtoDock while its satellite restores the link. Try again in a "
        "minute, or restart the satellite on that machine."
    )


def valid_port(value: object) -> int:
    """``value`` when it is a TCP port, else 0 (a bool is never one)."""
    return value if type(value) is int and 0 < value < 65536 else 0


def read(src: dict) -> dict:
    """The fields of a heartbeat or the auth capabilities, each kept only
    when well formed (an authenticated satellite still writes nothing
    unbounded into the capabilities column or a log line)."""
    out: dict = {}
    ok = src.get("tunnel_ok")
    if isinstance(ok, bool):
        out["tunnel_ok"] = ok
    port = valid_port(src.get("local_tunnel_port"))
    if port:
        out["local_tunnel_port"] = port
    rebinds = src.get("tunnel_rebinds")
    if type(rebinds) is int and rebinds >= 0:
        out["tunnel_rebinds"] = rebinds
    error = src.get("last_tunnel_error")
    if isinstance(error, str):
        out["last_tunnel_error"] = "".join(
            ch if ch.isprintable() else " " for ch in error)[:_ERROR_MAX]
    return out


def seed(conn, capabilities: dict) -> None:
    """A fresh connection's state from its auth capabilities."""
    fields = read(capabilities)
    conn.tunnel_ok = fields.get("tunnel_ok", True)
    conn.tunnel_rebinds = fields.get("tunnel_rebinds", 0)
    conn.tunnel_error = fields.get("last_tunnel_error", "")
    conn.tunnel_down_since = None if conn.tunnel_ok else time.monotonic()
    if not conn.tunnel_ok:
        logger.warning(
            "Satellite %s: connected with its local link down (%s); new sessions "
            "there are refused until it answers", conn.machine_id[:8],
            conn.tunnel_error or "no reason given",
        )


def apply(conn, msg: dict) -> bool:
    """Take a heartbeat's report into the connection; True when the
    capabilities changed (the caller persists a snapshot). Each change of
    state is logged once."""
    fields = read(msg)
    if not fields:
        return False
    mid = conn.machine_id[:8]
    error = fields.get("last_tunnel_error", conn.tunnel_error)
    changed = False

    port = fields.get("local_tunnel_port", 0)
    old_port = valid_port(conn.capabilities.get("local_tunnel_port"))
    if port and port != old_port:
        conn.capabilities["local_tunnel_port"] = port
        changed = True
        logger.warning(
            "Satellite %s: its local link moved from port %s to %s; sessions "
            "started on the old port resume at their next message", mid, old_port, port,
        )

    rebinds = fields.get("tunnel_rebinds")
    if rebinds is not None:
        if rebinds > conn.tunnel_rebinds:
            logger.warning(
                "Satellite %s: rebound its local link (%d since it started): %s",
                mid, rebinds, error or "no reason given",
            )
            changed = True
        conn.tunnel_rebinds = rebinds

    ok = fields.get("tunnel_ok")
    if ok is False and conn.tunnel_ok:
        conn.tunnel_down_since = time.monotonic()
        changed = True
        logger.warning(
            "Satellite %s: its local link is down (%s); new sessions there are "
            "refused until it answers", mid, error or "no reason given",
        )
    elif ok is True and not conn.tunnel_ok:
        since = conn.tunnel_down_since
        changed = True
        logger.info(
            "Satellite %s: its local link answers again after %.0f s", mid,
            0.0 if since is None else time.monotonic() - since,
        )
        conn.tunnel_down_since = None
    if ok is not None:
        conn.tunnel_ok = ok
    conn.tunnel_error = error

    if changed:
        for key in ("tunnel_ok", "tunnel_rebinds", "last_tunnel_error"):
            if key in fields:
                conn.capabilities[key] = fields[key]
    return changed


def payload_port(payload: dict) -> int:
    """The tunnel port a start payload's ``PROXY_URL`` names (0 when none)."""
    url = ((payload or {}).get("env") or {}).get("PROXY_URL") or ""
    try:
        return valid_port(urlsplit(url).port)
    except ValueError:
        return 0


def session_port_moved(info, cm) -> bool:
    """True when a session was started on a tunnel port its machine no
    longer serves. A session with no recorded port is never judged."""
    started = getattr(info, "tunnel_port", 0)
    if not started:
        return False
    live = valid_port(cm.tunnel_port(info.machine_id))
    return bool(live) and live != started
