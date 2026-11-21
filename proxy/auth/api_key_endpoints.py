"""The routes a person's ``otok_`` key reaches, and the scope each needs.

A key acts as its person on this list and nowhere else: the gate
(``auth/api_keys.gate``, from the middleware) answers 403 for a key bearer on
any other route, before the route runs, and the principal resolver turns an ``otok_``
bearer into nobody everywhere the gate did not leave a principal
(WebSocket scopes included). Never by key: the keys themselves, the
password and the two-step sign-in, the admin pages, a permission card's
answer. ``tests/auth/test_api_key_route_list.py`` pins the list against the
app's routes.

A new public route joins this list, ``api/public/``, its description
(``scripts/generate-openapi.py``) and the API keys page in the same commit.
"""

import re
from dataclasses import dataclass

from auth.request_path import has_traversal

#: Any scope of the key is enough (the key's own name, the agents it reaches).
ANY = ""


@dataclass(frozen=True)
class KeyRoute:
    pattern: re.Pattern
    methods: frozenset[str]
    scope: str
    #: The middleware verifies the key here. The webhook fire routes verify
    #: it themselves, against the owner their address names.
    verified: bool = True


_SEG = r"[^/]+"

_ROUTES: tuple[KeyRoute, ...] = (
    KeyRoute(re.compile(r"^/v1/api/me$"), frozenset({"GET"}), ANY),
    KeyRoute(re.compile(r"^/v1/api/agents$"), frozenset({"GET"}), ANY),
    KeyRoute(re.compile(r"^/v1/api/openapi\.json$"), frozenset({"GET"}), ANY),
    KeyRoute(re.compile(r"^/v1/api/otodock\.py$"), frozenset({"GET"}), ANY),
    KeyRoute(re.compile(r"^/v1/api/chats$"), frozenset({"GET", "POST"}), "chat"),
    KeyRoute(re.compile(rf"^/v1/api/chats/{_SEG}$"), frozenset({"GET"}), "chat"),
    KeyRoute(re.compile(rf"^/v1/api/chats/{_SEG}/messages$"), frozenset({"POST"}), "chat"),
    KeyRoute(re.compile(rf"^/v1/api/chats/{_SEG}/messages/{_SEG}$"), frozenset({"GET"}), "chat"),
    KeyRoute(re.compile(r"^/v1/api/tasks$"), frozenset({"GET", "POST"}), "tasks"),
    KeyRoute(re.compile(rf"^/v1/api/tasks/{_SEG}/run$"), frozenset({"POST"}), "tasks"),
    KeyRoute(re.compile(rf"^/v1/api/runs/{_SEG}$"), frozenset({"GET"}), "tasks"),
    KeyRoute(re.compile(rf"^/v1/api/runs/{_SEG}/stream$"), frozenset({"GET"}), "tasks"),
    KeyRoute(re.compile(rf"^/v1/api/agents/{_SEG}/files$"), frozenset({"GET"}), "files"),
    KeyRoute(re.compile(rf"^/v1/api/agents/{_SEG}/files/.+$"), frozenset({"GET", "PUT"}), "files"),
    KeyRoute(re.compile(r"^/v1/api/notifications$"), frozenset({"GET", "POST"}), "notifications"),
    # A sender's address may end in a slash: the router redirects it, as always.
    KeyRoute(re.compile(rf"^/v1/webhooks/agent/{_SEG}/{_SEG}/?$"), frozenset({"POST"}), "triggers",
             verified=False),
    KeyRoute(re.compile(rf"^/v1/webhooks/user/{_SEG}/{_SEG}/?$"), frozenset({"POST"}), "triggers",
             verified=False),
)

_BEARER = "bearer "

#: The public routes' prefix: a cookie reaches only the description and the
#: command there, a caller with no credential nothing.
PUBLIC_PREFIX = "/v1/api/"
COOKIE_PATHS = frozenset({"/v1/api/openapi.json", "/v1/api/otodock.py"})


def is_key_bearer(authorization: str) -> bool:
    """Whether the ``Authorization`` header carries a person's key."""
    if authorization[:len(_BEARER)].lower() != _BEARER:
        return False
    return authorization[7:].strip().startswith("otok_")


def key_route(method: str, path: str) -> KeyRoute | None:
    """The listed route ``method path`` matches, or None (refused)."""
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return None
    for route in _ROUTES:
        if route.pattern.match(base) and method.upper() in route.methods:
            return route
    return None


def listed() -> tuple[KeyRoute, ...]:
    return _ROUTES
