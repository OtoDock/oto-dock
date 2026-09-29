"""Endpoint allowlist for EXTERNAL sessions (phone callers who are not
platform users) and the liveness rule for every phone-minted session token.

The counterpart of ``auth/service_endpoints.py`` for the other end of the
trust scale. A session on an external route carries a JWT like any agent
session — it sits in the CLI process env, and shell (where a role still has
one) can ``curl`` the proxy with it — so the tool-level rules alone would be
bypassable. The ``external_session_confinement`` middleware
(``middleware.py``) therefore:

  1. rejects (401) any session token carrying an ``ext`` claim whose session
     is no longer LIVE in a layer registry — a token lifted from a call is
     dead at hangup (this applies to user-tied phone sessions too);
  2. confines (403) tokens whose caller is not a platform user (``ext`` and
     no ``user_sub``) to the endpoints below — the memory op and the
     session-scoped callbacks the hook scripts and the attached MCPs use.
     Everything else — tasks, continuations, triggers, notifications,
     meetings, delegation, chats and transcripts, files, media, uploads,
     agent discovery, subscriptions, pins — is unreachable by construction.

Contributor contract — when do you add an entry here?
  - Only when a hook script or an MCP that is attached to EXTERNAL sessions
    (``mcp_registry.EXTERNAL_DENIED_MCPS`` and the ``"external"`` exclusion
    context decide that) calls a NEW proxy endpoint. Use an anchored
    ``^...$`` pattern scoped to the HTTP method(s) actually used, and ask
    whether an anonymous caller may drive that endpoint at all.
  - ``verify_session_match`` still binds the token's ``sid`` to the request
    body on the hook endpoints; this list only decides WHICH endpoints exist
    for an external caller.
"""

import re

import config
from auth.request_path import has_traversal

# (pattern, allowed-methods) — anchored, method-scoped.
_ALLOWLIST: list[tuple[re.Pattern, frozenset[str]]] = [
    # memory-mcp → the caller's own memory scope (the API enforces the matrix).
    (re.compile(r"^/v1/internal/memory/op$"), frozenset({"POST"})),
    # Hook scripts (permission_gate / tool_result_forwarder / stop_tracker /
    # subagent_tracker), the stdio interceptor's path resolvers, and the
    # session-scoped file / media callbacks of file-tools, image-gen,
    # music-gen and video-gen.
    (
        re.compile(
            r"^/v1/hooks/(permission|tool-result|stop|subagent"
            r"|resolve-path|resolve-tool-arg-paths"
            r"|file|file-written|document-preview"
            r"|images|image-generating|image-gen-failed|media)$"
        ),
        frozenset({"POST"}),
    ),
]

EXTERNAL_BLOCKED_DETAIL = "This endpoint is not available to external sessions"
SESSION_DEAD_DETAIL = "Session is no longer active"


def session_token_claims(request) -> dict | None:
    """The validated payload of a SESSION JWT presented as the bearer, else
    None (no bearer, the master key, a cookie-only request, or a token that
    does not validate — the endpoint's own auth answers those)."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() != "bearer ":
        return None
    token = auth[7:].strip()
    # Three dot-separated segments = JWT shape; the master key is not one.
    if not token or token.count(".") != 2 or config.is_master_key(token):
        return None
    from auth.session_token import validate_session_token
    return validate_session_token(token)


def is_external_endpoint_allowed(method: str, path: str) -> bool:
    """True if an external principal may call ``method path``."""
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return False
    for pattern, methods in _ALLOWLIST:
        if pattern.match(base) and method.upper() in methods:
            return True
    return False
