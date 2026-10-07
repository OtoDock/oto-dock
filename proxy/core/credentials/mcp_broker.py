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

import jwt

import config
from core.credentials.mcp_gateway import GatewayCredential

logger = logging.getLogger("claude-proxy")

# Capability-token lifetime — SHORT by design (NOT the 24h session-JWT TTL). The
# wrapper fetches once, at MCP-child startup, immediately after the token is
# minted, so this only has to span spawn/install latency. A token replayed after
# the session closes finds a purged store anyway; the short TTL is belt-and-
# suspenders against a captured-but-unused token.
MCP_CRED_TOKEN_TTL_S = 30 * 60

# session_id -> mcp_name -> SecretBundle.  IN-MEMORY ONLY (see module docstring).
_store: dict[str, dict[str, "SecretBundle"]] = {}

# session_id -> relpath -> SessionFile. Per-SESSION secret FILES (not per-MCP
# env) — SSH private keys today, credentials_dir token files next. A remote
# session's satellite fetches these once at start via a session-files
# capability token, materializes them 0600 under its per-session secrets dir,
# and wipes them at session close. Same in-memory-only rule as ``_store``.
_file_store: dict[str, dict[str, "SessionFile"]] = {}


@dataclass
class SecretBundle:
    """The secret material one MCP needs: env vars a stdio MCP fetches at
    spawn, or the gateway credential an HTTP MCP's requests receive on the
    way out (never both: an HTTP MCP has no process to fetch)."""

    env: dict[str, str] = field(default_factory=dict)
    gateway: GatewayCredential | None = None


@dataclass
class SessionFile:
    """One secret file delivered to a remote session's satellite at start."""

    content_b64: str
    mode: int = 0o600


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------


def provision(session_id: str, secrets: dict[str, "SecretBundle"]) -> None:
    """Install (replace) the secret bundles for a session.

    Called at MCP materialization and re-called on every (re)warmup, so the
    store always reflects the latest build. An empty/falsy mapping clears the
    session (nothing to broker)."""
    if not session_id:
        return
    if secrets:
        _store[session_id] = dict(secrets)
    else:
        _store.pop(session_id, None)


def get(session_id: str, mcp: str) -> "SecretBundle | None":
    """Direct in-process lookup of one MCP's bundle. The Direct-LLM in-proc spawn
    path uses this instead of the HTTP endpoint; the endpoint uses it too."""
    return _store.get(session_id, {}).get(mcp)


def bundles_of(session_id: str) -> dict[str, "SecretBundle"]:
    """Every bundle a session holds, by MCP key (a copy)."""
    return dict(_store.get(session_id, {}))


def purge_session(session_id: str) -> None:
    """Drop a session's secrets — called on session close, so a capability token
    replayed afterwards (within its TTL) finds nothing. The gateway forgets
    the session's descriptor and tells the push registry."""
    _store.pop(session_id, None)
    _file_store.pop(session_id, None)
    from core.credentials import mcp_gateway
    mcp_gateway.on_purge(session_id)


def provision_session_files(
    session_id: str, files: dict[str, "SessionFile"],
) -> None:
    """Install (replace) the per-session secret FILES for a session.

    Empty/falsy mapping clears the session's files."""
    if not session_id:
        return
    if files:
        _file_store[session_id] = dict(files)
    else:
        _file_store.pop(session_id, None)


def get_session_files(session_id: str) -> dict[str, "SessionFile"] | None:
    """The session's secret files, or None when none were provisioned."""
    return _file_store.get(session_id)


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
        "exp": datetime.now(timezone.utc) + timedelta(seconds=MCP_CRED_TOKEN_TTL_S),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def verify_token(token: str) -> tuple[str, str] | None:
    """Validate a capability token. Returns ``(session_id, mcp)`` or ``None``.

    Rejects anything that is not a ``mcp_cred`` token — in particular the session
    JWT (``type == "session"``) and the master key (not a JWT at all) both fail
    here, so the endpoint cannot be driven by a credential the agent already
    holds. Signature + expiry are enforced by ``jwt.decode``."""
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except (jwt.InvalidTokenError, jwt.ExpiredSignatureError):
        return None
    if payload.get("type") != "mcp_cred":
        return None
    sid = payload.get("sid") or ""
    mcp = payload.get("mcp") or ""
    if not sid or not mcp:
        return None
    return sid, mcp


def mint_files_token(session_id: str) -> str:
    """Mint the session-files capability token. Rides the start payload; the
    SATELLITE presents it once (before the CLI spawns) to fetch this session's
    secret files over the tunnel. It never enters the spawned CLI/agent env,
    so the agent's bash cannot replay it — and like the per-MCP token it is
    the ONLY accepted credential on the endpoint."""
    payload = {
        "type": "session_files",
        "sid": session_id,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=MCP_CRED_TOKEN_TTL_S),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def verify_files_token(token: str) -> str | None:
    """Validate a session-files capability token → session_id (or None)."""
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except (jwt.InvalidTokenError, jwt.ExpiredSignatureError):
        return None
    if payload.get("type") != "session_files":
        return None
    return payload.get("sid") or None
