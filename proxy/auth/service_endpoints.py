"""Service-to-service endpoint allowlist for the master ``PROXY_API_KEY``.

The master key is held only by trusted in-process services that talk to the
proxy over the network: the **phone server** and the **standalone scheduler**.
It is NOT a general web-admin credential. This module confines it to the exact
set of endpoints those services call, so a leaked master key cannot drive
arbitrary user/admin REST endpoints (it would be rejected with 403 everywhere
off this list). Enforced by the ``service_key_confinement`` middleware in
``middleware.py``.

Contributor contract — when do you add an entry here?
  - The standard MCP → proxy callback (task / notifications / meetings /
    triggers / memory / mcps / agent-config, plus file-tools and the hook
    scripts) authenticates with a **per-session JWT**, never the master key.
    A new core or community MCP that follows that contract needs **NO** change
    here.
  - Only a genuine service-to-service daemon — its own process, holding the
    master key in ``Authorization: Bearer`` (or a ``?key=`` query) — must add
    its endpoint(s) below. Use an anchored ``^...$`` pattern and scope it to the
    HTTP method(s) actually used.

The ``/ws/phone`` and ``/ws/phone-management`` WebSocket endpoints also carry
the master key (as a ``?key=`` query) but are a separate ASGI scope that HTTP
middleware does not wrap; they are gated inline by their own key check.
"""

import re

import config

# (pattern, allowed-methods) — anchored, method-scoped.
_ALLOWLIST: list[tuple[re.Pattern, frozenset[str]]] = [
    (re.compile(r"^/v1/internal/fire-task$"), frozenset({"POST"})),
    (re.compile(r"^/v1/internal/fire-notification$"), frozenset({"POST"})),
    (re.compile(r"^/v1/sessions/warmup$"), frozenset({"POST"})),
    (re.compile(r"^/v1/sessions/[^/]+$"), frozenset({"DELETE"})),  # close session
    (re.compile(r"^/v1/phone/usage/turn-classifier$"), frozenset({"POST"})),
]


def _has_traversal(path: str) -> bool:
    low = path.lower()
    if "%2e" in low or "%2f" in low or "%5c" in low or "\\" in path:
        return True
    return any(seg in (".", "..") for seg in path.split("/"))


def extract_master_key(request) -> str | None:
    """Return the presented credential IFF it is the master key, else None.

    Accepts ``Authorization: Bearer <key>`` (the S2S REST path) or a ``?key=``
    query param (defensive — covers any legacy query-keyed surface). A session
    JWT in either position is NOT the master key, so this returns None and the
    caller is left to the normal principal resolution.
    """
    if not config.API_KEY:
        return None
    auth = request.headers.get("authorization", "")
    presented = ""
    if auth[:7].lower() == "bearer ":
        presented = auth[7:]
    if not presented:
        presented = request.query_params.get("key", "")
    if presented and config.is_master_key(presented):
        return presented
    return None


def is_service_endpoint_allowed(method: str, path: str) -> bool:
    """True if the master key may call ``method path``."""
    base = path.split("?", 1)[0]
    if _has_traversal(base):
        return False
    for pattern, methods in _ALLOWLIST:
        if pattern.match(base) and method.upper() in methods:
            return True
    return False
