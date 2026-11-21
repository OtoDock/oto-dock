"""Per-session MCP credential broker.

Stdio MCPs fetch their own secrets at spawn from this in-memory, per-session
store instead of having them baked into config FILES (the proxy's ``sessions/``
build output + the satellite disks) and bled as a flat union into every MCP's
env. Access is gated by a per-``(session_id, mcp)`` capability token: the
``POST /v1/hooks/mcp-credentials`` endpoint accepts ONLY that token — never the
session JWT and never the master ``PROXY_API_KEY`` — so the agent's own bash,
which holds the session JWT + ``PROXY_URL`` + curl, cannot harvest another MCP's
(or another user's) secrets. The ``mcp`` is derived from the token, so a token
for one MCP can't fetch another's.

An HTTP MCP's credential (a vendor bearer, a header-style API key, a
sidecar's token) is the bundle's ``gateway`` half: it is never fetched by a
process and never written anywhere; the gateway (``core/credentials/
mcp_gateway.py``) resolves it per request and adds it on the way out.

CRITICAL — this store holds SECRETS. It is in-memory ONLY and MUST NEVER be
persisted to disk. This is the exact OPPOSITE of the secret-free
``_session_security`` context, which IS persisted for crash recovery (see
``core/session/session_state.py``). A proxy restart empties this store on purpose: the
spawn-time wrapper then fail-closes and the session re-warms, repopulating it.
Persisting it would re-introduce the very secrets-in-files leak the broker exists
to remove.

The store is populated at session warmup by every execution layer (CLI / Codex /
Direct / remote) and read at spawn time via the stdio interceptor + the broker
fetch endpoint, so a sandboxed agent never sees the real secret on disk — its
config carries only a capability token that the wrapper exchanges for the live
value, in-memory, at MCP startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from auth import tokens
from core.credentials.mcp_gateway import (
    TOKEN_RENEWAL_ENV,
    TOKEN_RENEWAL_LIFE_S,
    CliCredential,
    ExitCredential,
    GatewayCredential,
    TokenCredential,
)

logger = logging.getLogger("otodock")

# The fetch token's life: a session's token's. A tool starts at the agent's
# first call to it, hours into a chat, and fetches its secrets then. The
# session's own processes can read the token (in the tool's config and its
# stand-in's environment) as they read a started tool's secrets in /proc, so
# a longer life reaches nothing more; the bundle's purge at the session's end
# still closes it (SECURITY-DECISIONS, decision 35).
MCP_FETCH_TOKEN_TTL_S = TOKEN_RENEWAL_LIFE_S

# session_id -> mcp_name -> SecretBundle.  IN-MEMORY ONLY (see module docstring).
_store: dict[str, dict[str, SecretBundle]] = {}


@dataclass
class SecretBundle:
    """The secret material one MCP needs: env vars a stdio MCP fetches at
    spawn, or the gateway credential an HTTP MCP's requests receive on the
    way out (never both: an HTTP MCP has no process to fetch), and the
    credential the session's command-line tools (``git``, ``gh``) fetch per
    command when the manifest names a host for them."""

    env: dict[str, str] = field(default_factory=dict)
    gateway: GatewayCredential | None = None
    cli: CliCredential | None = None
    # The secrets of ``env`` the session holds as placeholders, each swapped
    # for its value by the credential exit on the way out.
    exit: tuple[ExitCredential, ...] = ()
    # The account token ``env`` holds by value, by reference too: read fresh
    # at every fetch, and read again by the tool itself when it expires.
    token: TokenCredential | None = None
    # The account the build resolved this tool on (``"<owner's sub>|<label>"``,
    # "" for a tool with none): a session taken back after a restart gets its
    # secrets again only on this account (``mcp_gateway.accounts_of``).
    account: str = ""


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------


def provision(session_id: str, secrets: dict[str, SecretBundle]) -> None:
    """Install (replace) the secret bundles for a session.

    Called at MCP materialization and re-called on every (re)warmup, so the
    store always reflects the latest build. An empty/falsy mapping clears the
    session (nothing to broker). A bundle whose account token expires takes
    the tool's renewal token here, where the session's id is known."""
    if not session_id:
        return
    if secrets:
        for mcp, bundle in secrets.items():
            token = getattr(bundle, "token", None)
            if token is not None and token.renews:
                bundle.env[TOKEN_RENEWAL_ENV] = mint_renewal(session_id, mcp)
        _store[session_id] = dict(secrets)
    else:
        _store.pop(session_id, None)


def get(session_id: str, mcp: str) -> SecretBundle | None:
    """Direct in-process lookup of one MCP's bundle. The Direct-LLM in-proc spawn
    path uses this instead of the HTTP endpoint; the endpoint uses it too."""
    return _store.get(session_id, {}).get(mcp)


def bundles_of(session_id: str) -> dict[str, SecretBundle]:
    """Every bundle a session holds, by MCP key (a copy)."""
    return dict(_store.get(session_id, {}))


def exit_credentials(session_id: str) -> list[ExitCredential]:
    """Every placeholder a session holds, with its credential."""
    return [c for bundle in _store.get(session_id, {}).values() for c in bundle.exit]


def exit_credential(session_id: str, placeholder: str) -> ExitCredential | None:
    """The credential behind ``placeholder`` when the session holds it."""
    for cred in exit_credentials(session_id):
        if cred.placeholder == placeholder:
            return cred
    return None


def exit_placeholder_for_cli(session_id: str, cred: CliCredential) -> str:
    """The placeholder the session holds for a command-line credential, or
    ``""`` when it holds none (the hook then hands the token only to a
    credential built per command, ``CliCredential.per_command``)."""
    for c in exit_credentials(session_id):
        # By value, never identity: a session taken back after a restart
        # holds the descriptor's copies.
        if c.cli is not None and c.cli == cred:
            return c.placeholder
    return ""


def exit_takes_host(session_id: str, host: str) -> bool:
    """Whether the session holds a placeholder whose value may be sent to
    ``host``: the exit ends such a connection and reads inside."""
    return any(c.takes_host(host) for c in exit_credentials(session_id))


def cli_credential(session_id: str, host: str) -> CliCredential | None:
    """The command-line credential a session holds for ``host`` (the git
    helper's and the command wrappers' lookup), or None."""
    want = (host or "").strip().lower()
    if not want:
        return None
    for bundle in _store.get(session_id, {}).values():
        cred = getattr(bundle, "cli", None)
        if cred is not None and cred.host.lower() == want:
            return cred
    return None


def purge_session(session_id: str) -> None:
    """Drop a session's secrets — called on session close, so a capability token
    replayed afterwards (within its TTL) finds nothing. The gateway forgets
    the session's descriptor and tells the push registry."""
    _store.pop(session_id, None)
    from core.credentials import mcp_gateway
    mcp_gateway.on_purge(session_id)


# ---------------------------------------------------------------------------
# Capability token  (binds one (session_id, mcp) pair)
# ---------------------------------------------------------------------------


def mint_token(session_id: str, mcp: str) -> str:
    """Mint a capability token binding ``(session_id, mcp)``. Injected into THAT
    MCP child's env block; the wrapper presents it to the endpoint and strips it
    before exec so the MCP process itself never carries it."""
    payload = {
        "type": "mcp_cred",
        "sid": session_id,
        "mcp": mcp,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=MCP_FETCH_TOKEN_TTL_S),
    }
    return tokens.mint(tokens.MCP_CREDENTIALS, payload)


def verify_token(token: str) -> tuple[str, str] | None:
    """Validate a capability token. Returns ``(session_id, mcp)`` or ``None``.

    Rejects anything that is not a ``mcp_cred`` token — in particular the session
    JWT (``type == "session"``) and the master key (not a JWT at all) both fail
    here, so the endpoint cannot be driven by a credential the agent already
    holds: they are other kinds (``auth/tokens.py``)."""
    payload = tokens.verify(token, tokens.MCP_CREDENTIALS)
    if payload is None:
        return None
    sid = payload.get("sid") or ""
    mcp = payload.get("mcp") or ""
    if not sid or not mcp:
        return None
    return sid, mcp


def live_token(token: str) -> tuple[str, str, SecretBundle] | int:
    """The one check of a fetch token, for every route that takes it (the
    secrets, a tool's saved answers, its start): ``(session_id, mcp, bundle)``
    while the token is valid and its session still holds that tool's bundle,
    else the status to answer: 401 for a token that is not one, 404 once the
    session is gone (its bundles are purged at its end, so a token copied out
    of a closed session reaches nothing)."""
    ident = verify_token(token)
    if ident is None:
        return 401
    bundle = get(*ident)
    if bundle is None:
        return 404
    return ident[0], ident[1], bundle


def mint_renewal(session_id: str, mcp: str) -> str:
    """Mint the renewal token of ``(session_id, mcp)``: what a stdio tool
    presents on the bundle's route when its account token runs out, for the
    token alone. It stays in the tool's environment, unlike the fetch token,
    so a process that reads that environment can ask for the tool's token
    while the session's bundle stands (SECURITY-DECISIONS); never a static
    secret, another tool's token, or the refresh token, which no session
    holds."""
    payload = {"sid": session_id, "mcp": mcp}
    return tokens.mint(tokens.MCP_TOKEN, payload, ttl_s=TOKEN_RENEWAL_LIFE_S)


def verify_renewal(token: str) -> tuple[str, str] | None:
    """Validate a renewal token. Returns ``(session_id, mcp)`` or ``None``."""
    payload = tokens.verify(token, tokens.MCP_TOKEN)
    if payload is None:
        return None
    sid = payload.get("sid") or ""
    mcp = payload.get("mcp") or ""
    if not sid or not mcp:
        return None
    return sid, mcp


def renewal_ended(token: str) -> bool:
    """Whether ``token`` is a genuine renewal token past its life: a tool
    that holds one is told to start a new chat, where any other refusal would
    read as a passing one and be tried for ever."""
    try:
        tokens.decode(token, tokens.MCP_TOKEN)
    except tokens.TokenExpired:
        return True
    except tokens.TokenInvalid:
        return False
    return False
