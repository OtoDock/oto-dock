"""Bootstrap configuration for the Phone Server.

Only contains settings needed to connect to the proxy management
WebSocket. All other configuration is received from the proxy DB
via the management WebSocket and managed through ConfigManager.
"""

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# Load shared platform config first (secrets), then phone-specific overrides
load_dotenv(Path(__file__).parent.parent / "config.env")
load_dotenv(Path(__file__).parent / ".env")  # phone-specific overrides (optional)

# Paths
BASE_DIR = Path(__file__).parent

# Proxy API (needed to establish management WebSocket + per-call phone WS)
PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8400")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
# Telephony-scoped secret shared with the proxy via config.env. Required (as a
# Bearer token) on the phone server's HTTP API. FAIL-CLOSED: when unset, the
# guarded endpoints reject every request (only /health stays open).
PHONE_API_SECRET = os.environ.get("PHONE_API_SECRET", "")

# Parsed host[:port] for WebSocket URI construction (used by proxy/client.py).
# Keep the port EXACTLY as PROXY_URL states it: an implicit-port URL (e.g.
# https://otodock.example.com behind a tunnel serving on 443) must stay
# implicit — appending the 8400 default would aim the per-call WS at a port
# the front-end never serves and silently degrade every call to the HTTP
# fallback. Same derivation as the management WS (proxy/management_ws.py).
PROXY_WS_HOST_PORT = PROXY_URL.rstrip("/").replace("http://", "").replace("https://", "")

# ws vs wss: secure WebSocket when the proxy is reachable over HTTPS, else
# plaintext ws (the single-host / trusted-LAN default). Keeps the API key and
# call audio off the wire in clear when the proxy is remote + TLS-fronted.
PROXY_WS_SCHEME = "wss" if PROXY_URL.lower().startswith("https://") else "ws"


def _routable(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_global
    except ValueError:
        return False


def plaintext_proxy_refusal(url: str, resolve=socket.getaddrinfo) -> str | None:
    """Why the daemon must not start on ``url`` (None when it may): the
    proxy key and every call's audio cross this link, so plain http goes
    only to an address that is not globally routable (this machine, a
    private network, the shared 100.64/10 range a tailnet uses). A
    single-label name (the Docker service, ``otodock-proxy``) counts as
    private; another name is refused only when none of its addresses is
    private (a LAN host with a global IPv6 address beside its private one
    stays on the LAN); a name that does not resolve is left to the
    connection, which fails on its own.
    The proxy applies the same rule to its ``PHONE_SERVER_URL``."""
    parts = urlsplit(url or "")
    if (parts.scheme or "").lower() != "http":
        return None
    host = parts.hostname or ""
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        if "." not in host.rstrip("."):
            return None
        try:
            addresses = sorted({info[4][0] for info in resolve(host, None)})
        except OSError:
            return None
    public = [a for a in addresses if _routable(a)]
    if not public or len(public) < len(addresses):
        return None
    return (f"PROXY_URL is plain http to {host} ({public[0]}), an address reachable "
            "from the internet, and the proxy key and call audio would cross it "
            "unencrypted: use https, or the proxy's address on this machine or a "
            "private network")

# Audio constants (Asterisk AudioSocket = 8kHz 16-bit signed LE mono)
# These are protocol constants that never change.
SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1
FRAME_SIZE = 320  # bytes per AudioSocket audio frame (20ms at 8kHz 16-bit)
