"""Bootstrap configuration for the Phone Server.

Only contains settings needed to connect to the proxy management
WebSocket. All other configuration is received from the proxy DB
via the management WebSocket and managed through ConfigManager.
"""

import os
from pathlib import Path

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

# Audio constants (Asterisk AudioSocket = 8kHz 16-bit signed LE mono)
# These are protocol constants that never change.
SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1
FRAME_SIZE = 320  # bytes per AudioSocket audio frame (20ms at 8kHz 16-bit)
