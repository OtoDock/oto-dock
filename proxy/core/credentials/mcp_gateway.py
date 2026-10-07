"""The remote-MCP credential gateway: the credential a session's HTTP MCP
needs, resolved on the proxy at request time and added on the way out.

A session's configuration names the gateway route (``entry_url``) with the
session's own token as the bearer and never the vendor credential; the
credential lives here, in the session's broker bundle (``SecretBundle.gateway``),
as a reference the gateway resolves per request: a static value (an API key,
an instance field) or a token file the refresh worker keeps current. The
route (``api/mcp/gateway.py``) and the tunnel's in-process forward call
``resolve``; the satellite-local gateway receives the resolved value through
the push registry (``core/remote/mcp_gateway_push.py``).

A secret-free descriptor of every provisioned session is written under
``sessions/mcp-gateway/`` so a session re-adopted after a proxy restart is
re-provisioned with exactly the set it had; the descriptor never carries a
value, only where it comes from.

``resolve`` blocks (a stat, a file read, two store reads behind memos): the
callers run it in a thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import config

logger = logging.getLogger("claude-proxy.mcp-gateway")

GATEWAY_PATH_PREFIX = "/v1/mcp-gateway"

# The request body cap on both gateways. The gateway carries MCP JSON-RPC
# (a tool call, a result with an inline image) — kilobytes to a few MB, never
# a file upload, so the cap is far below the tunnel's own 128 MB: a session's
# own code cannot park a large buffer in the proxy or the satellite through it.
MAX_BODY_BYTES = 16 * 1024 * 1024
# The refusal shape reads the request's JSON-RPC id to answer in kind; it never
# parses more than this (a refused request is not forwarded, so a huge body is
# just dropped with a generic 202).
_REFUSAL_PARSE_CAP = 256 * 1024

# A token pushed to a satellite lives at most this long without a renewal
# from the proxy's tick, whatever the vendor token's own expiry.
LEASE_S = 15 * 60
# The tick re-pushes when the lease has less than this left.
RENEW_BEFORE_S = 10 * 60

_ALLOWLIST_MEMO_S = 30.0
_EGRESS_MEMO_S = 30.0

# The allowlist row a proxy-local sidecar's host is judged under: the seeded
# rows name ``localhost`` for the github and m365 containers, and in a
# containerised deployment the sidecar is dialled by its service name.
_SIDECAR_ALLOWLIST_HOST = "localhost"

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,160}")

_REFUSAL_NO_CREDENTIAL = "No credential is provisioned for this session and MCP."
_REFUSAL_NO_VALUE = (
    "The credential has no value; reconnect the account or re-enter the key in "
    "Settings > Integrations."
)


@dataclass(frozen=True)
class TokenRef:
    """Where a token-file credential is read from, per request."""

    token_dir: str
    account_label: str
    preferred_bearer: str = ""


@dataclass
class GatewayCredential:
    """One HTTP MCP's credential as the builder resolved it: the upstream
    origin and endpoint path the forward is confined to, the allowlist row
    it is judged under, the header to add, and the value or its reference."""

    upstream: str
    path: str
    allowlist_key: str
    header: str = "Authorization"
    prefix: str = "Bearer "
    value: str = ""
    token_ref: TokenRef | None = None
    proxy_local: bool = False
    manifest: str = ""
    # The resolved-env key a static value came from (a header-style key's
    # ``value_from``), so a re-adoption can resolve it again; "" for a
    # token-file credential.
    value_from: str = ""

    @property
    def host(self) -> str:
        return urlsplit(self.upstream).hostname or ""

    def descriptor(self) -> dict:
        """The secret-free shape persisted per session: a static value is
        replaced by the fact that one existed."""
        d = asdict(self)
        d["value"] = ""
        d["has_value"] = bool(self.value)
        return d

    @classmethod
    def from_descriptor(cls, d: dict) -> "GatewayCredential":
        ref = d.get("token_ref")
        return cls(
            upstream=str(d.get("upstream") or ""),
            path=str(d.get("path") or ""),
            allowlist_key=str(d.get("allowlist_key") or ""),
            header=str(d.get("header") or "Authorization"),
            prefix=str(d.get("prefix") or ""),
            value="",
            token_ref=TokenRef(**ref) if isinstance(ref, dict) else None,
            proxy_local=bool(d.get("proxy_local")),
            manifest=str(d.get("manifest") or ""),
            value_from=str(d.get("value_from") or ""),
        )


@dataclass
class Resolved:
    header: str
    value: str
    upstream: str
    path: str
    proxy_local: bool
    expires_in: float | None = None


@dataclass
class Refusal:
    reason: str
    detail: str


def entry_url(key: str, path: str) -> str:
    """The URL a session's configuration carries for the MCP ``key`` whose
    upstream endpoint path is ``path``: the proxy port, which the sandbox
    splice lands on the internal listener."""
    return f"http://127.0.0.1:{config.PORT}{GATEWAY_PATH_PREFIX}/{key}{path}"


def internal_entry_url(url: str) -> str:
    """A gateway URL re-addressed to the internal listener when the app
    bound one; any other URL, or no internal listener, unchanged."""
    port = int(getattr(config, "INTERNAL_LISTENER_PORT", 0) or 0)
    if not port or gateway_key_of(url) is None:
        return url
    parts = urlsplit(url)
    if parts.hostname != "127.0.0.1" or parts.port == port:
        return url
    return url.replace(f"//127.0.0.1:{parts.port}/", f"//127.0.0.1:{port}/", 1)


def gateway_key_of(url: str) -> str | None:
    """The MCP key a gateway URL names, or None for any other URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    rest = parts.path or ""
    if not rest.startswith(GATEWAY_PATH_PREFIX + "/"):
        return None
    key = rest[len(GATEWAY_PATH_PREFIX) + 1:].split("/", 1)[0]
    return key or None


def path_matches(declared: str, requested: str) -> bool:
    """The forward is confined to the declared endpoint: the request path
    must equal it, a trailing slash tolerated either way."""
    if not declared or not requested:
        return False
    return declared.rstrip("/") == requested.rstrip("/")


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def credential(session_id: str, mcp: str) -> GatewayCredential | None:
    from core.credentials import mcp_broker
    bundle = mcp_broker.get(session_id, mcp)
    return getattr(bundle, "gateway", None) if bundle else None


def has_credential(session_id: str, mcp: str) -> bool:
    return credential(session_id, mcp) is not None


def credentials_of(session_id: str) -> dict[str, GatewayCredential]:
    """Every gateway credential a session holds, by MCP key."""
    from core.credentials import mcp_broker
    out: dict[str, GatewayCredential] = {}
    for key, bundle in (mcp_broker.bundles_of(session_id) or {}).items():
        cred = getattr(bundle, "gateway", None)
        if cred is not None:
            out[key] = cred
    return out


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# (allowlist key, host) -> (allowed, deadline, allowlist generation)
_allowlist_memo: dict[tuple[str, str], tuple[bool, float, int]] = {}
# upstream -> (refusal or None, deadline)
_egress_memo: dict[str, tuple[str | None, float]] = {}
# (token dir, label) -> (mtime_ns, size, parsed file)
_token_memo: dict[tuple[str, str], tuple[int, int, dict | None]] = {}


def _host_allowed(key: str, host: str) -> bool:
    from storage.identity import bearer_allowlist
    gen = bearer_allowlist.generation()
    now = time.monotonic()
    with _lock:
        hit = _allowlist_memo.get((key, host))
    if hit is not None and hit[1] > now and hit[2] == gen:
        return hit[0]
    allowed = bearer_allowlist.is_host_allowed(key, host)
    with _lock:
        _allowlist_memo[(key, host)] = (allowed, now + _ALLOWLIST_MEMO_S, gen)
    return allowed


def _egress_refusal(upstream: str) -> str | None:
    """The self-SSRF guard on a vendor origin: the admin's allowlist is the
    authority on WHICH host the gateway may reach (an approved LAN or
    homelab host is allowed, as a homelab MCP's own private target is), so
    this refuses only an upstream that resolves to the platform's OWN
    control plane — loopback, the link-local/metadata range — so an
    allowlisted name that resolves back onto the proxy (a rebind, a
    misconfigured row) cannot turn the gateway on the platform itself.
    The forward resolves the name again when it connects, so a record that
    answers differently between the two lookups is not caught here (the
    stated limit of every such guard)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    now = time.monotonic()
    with _lock:
        hit = _egress_memo.get(upstream)
    if hit is not None and hit[1] > now:
        return hit[0]
    parts = urlsplit(upstream)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    verdict: str | None = None
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        verdict = f"the MCP server host {host} does not resolve"
    else:
        for info in infos:
            ip = info[4][0]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                verdict = f"the MCP server host {host} resolves to an unreadable address"
                break
            mapped = getattr(addr, "ipv4_mapped", None)
            if mapped is not None:
                addr = mapped
            if (addr.is_loopback or addr.is_link_local or addr.is_unspecified
                    or addr.is_multicast or addr.is_reserved):
                verdict = (f"the MCP server host {host} resolves to the platform's own "
                           f"address space ({ip})")
                break
    with _lock:
        _egress_memo[upstream] = (verdict, now + _EGRESS_MEMO_S)
    return verdict


def _read_token(ref: TokenRef) -> dict | None:
    """The token file, re-read when its mtime or size changed."""
    from services.oauth import oauth_account_store
    try:
        path = oauth_account_store.account_token_path(Path(ref.token_dir), ref.account_label)
    except ValueError:
        return None
    try:
        st = os.stat(path)
    except OSError:
        with _lock:
            _token_memo.pop((ref.token_dir, ref.account_label), None)
        return None
    key = (ref.token_dir, ref.account_label)
    with _lock:
        hit = _token_memo.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    raw = oauth_account_store.read_account_token(Path(ref.token_dir), ref.account_label)
    with _lock:
        _token_memo[key] = (st.st_mtime_ns, st.st_size, raw)
    return raw


def _expires_in(raw: dict) -> float | None:
    expiry = str(raw.get("expires_at") or "")
    if not expiry:
        return None
    try:
        dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def resolve_credential(cred: GatewayCredential) -> Resolved | Refusal:
    """The header to add for ``cred`` right now, or why not."""
    host = cred.host
    allow_host = _SIDECAR_ALLOWLIST_HOST if cred.proxy_local else host
    if not _host_allowed(cred.allowlist_key, allow_host):
        return Refusal(
            "host_not_allowed",
            f"The host {host} is not approved for {cred.allowlist_key}; an admin "
            "can approve it in Admin > Security (the bearer allowlist).",
        )
    if not cred.proxy_local:
        why = _egress_refusal(cred.upstream)
        if why:
            return Refusal(
                "egress_refused",
                f"The MCP server cannot be reached from the platform: {why}.",
            )
    expires_in: float | None = None
    if cred.token_ref is not None:
        raw = _read_token(cred.token_ref)
        if not raw:
            return Refusal("no_value", _REFUSAL_NO_VALUE)
        from services.mcp import mcp_registry
        from services.oauth import credential_resolver, oauth_account_store
        manifest = mcp_registry.get_manifest(cred.manifest) if cred.manifest else None
        dead = (credential_resolver.account_unusable_reason(raw, manifest) if manifest
                else oauth_account_store.token_dead_reason(raw))
        if dead:
            return Refusal("needs_reconnect", credential_resolver.reconnect_wording(dead))
        extra = raw.get("extra") or {}
        preferred = cred.token_ref.preferred_bearer or (
            extra.get("preferred_bearer", "") if isinstance(extra, dict) else "")
        if preferred and isinstance(extra, dict) and extra.get(preferred):
            secret = str(extra.get(preferred) or "")
        else:
            secret = oauth_account_store.get_canonical_access_token(raw)
        expires_in = _expires_in(raw)
    else:
        secret = cred.value
    if not secret:
        return Refusal("no_value", _REFUSAL_NO_VALUE)
    return Resolved(
        header=cred.header, value=f"{cred.prefix}{secret}", upstream=cred.upstream,
        path=cred.path, proxy_local=cred.proxy_local, expires_in=expires_in,
    )


def resolve(session_id: str, mcp: str) -> Resolved | Refusal:
    """The header for the MCP ``mcp`` of ``session_id``, or why not."""
    cred = credential(session_id, mcp)
    if cred is None:
        return Refusal("no_credential", _REFUSAL_NO_CREDENTIAL)
    return resolve_credential(cred)


def forget_memos() -> None:
    with _lock:
        _allowlist_memo.clear()
        _egress_memo.clear()
        _token_memo.clear()


# ---------------------------------------------------------------------------
# The per-session descriptor (secret-free)
# ---------------------------------------------------------------------------


def descriptor_dir() -> Path:
    d = config.SESSIONS_DIR / "mcp-gateway"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _descriptor_path(session_id: str) -> Path | None:
    if not session_id or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return descriptor_dir() / f"{session_id}.json"


def write_descriptor(
    session_id: str, credentials: dict[str, GatewayCredential], *,
    machine_id: str = "", token_hash: str = "", agent: str = "", user_sub: str = "",
    task_scope: str = "",
) -> None:
    """Record what a session was provisioned with, by reference, plus the
    identity its static values resolve under (the agent, the person, the
    scope). Nothing to record removes an earlier descriptor."""
    path = _descriptor_path(session_id)
    if path is None:
        return
    if not credentials:
        remove_descriptor(session_id)
        return
    doc = {
        "machine_id": machine_id,
        "token_hash": token_hash,
        "agent": agent,
        "user_sub": user_sub,
        "task_scope": task_scope,
        "credentials": {k: c.descriptor() for k, c in credentials.items()},
    }
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def read_descriptor(session_id: str) -> dict | None:
    path = _descriptor_path(session_id)
    if path is None or not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    creds = doc.get("credentials")
    out: dict[str, GatewayCredential] = {}
    for key, d in (creds or {}).items():
        if isinstance(d, dict):
            out[str(key)] = GatewayCredential.from_descriptor(d)
    return {
        "machine_id": str(doc.get("machine_id") or ""),
        "token_hash": str(doc.get("token_hash") or ""),
        "agent": str(doc.get("agent") or ""),
        "user_sub": str(doc.get("user_sub") or ""),
        "task_scope": str(doc.get("task_scope") or ""),
        "credentials": out,
    }


def static_values_for(
    agent: str, user_sub: str, task_scope: str, creds: dict[str, GatewayCredential],
) -> dict[str, str]:
    """The static values (header-style keys) of ``creds`` resolved again for
    the identity a descriptor names, by MCP key; a token-file credential
    needs none. As the session build does: the MCP's resolved secrets, then
    the field values of the env-delivered instance the agent takes
    (``mcp_registry.env_instance_values``), the instance's winning. Blocking
    (the credential resolver's and the instance store's reads)."""
    wanted = {k: c for k, c in creds.items() if c.token_ref is None and c.value_from}
    if not wanted:
        return {}
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    result = credential_resolver.resolve_credentials(
        agent, user_sub or None, task_scope=task_scope or "user",
    )
    out: dict[str, str] = {}
    for key, cred in wanted.items():
        name = cred.manifest or key
        env = dict(result.env_by_mcp.get(name, {}))
        manifest = mcp_registry.get_manifest(name)
        if manifest is not None:
            env.update(mcp_registry.env_instance_values(manifest, agent))
        value = env.get(cred.value_from, "")
        if value:
            out[key] = value
    return out


def remove_descriptor(session_id: str) -> None:
    path = _descriptor_path(session_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("mcp-gateway: descriptor of %s not removed: %s", session_id[:8], e)


# ---------------------------------------------------------------------------
# The forward: what both front doors share (the route, the tunnel)
# ---------------------------------------------------------------------------

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade",
})
# Request headers never forwarded: the hop-by-hop set, the client's own
# host and length, every credential of the session (the bearer is the
# session token, a cookie is a person's) and anything naming a client
# address.
_REQUEST_DROP = _HOP_BY_HOP | frozenset({
    "host", "content-length", "authorization", "cookie", "forwarded", "x-real-ip",
})
# Response headers never passed back: a vendor's challenge would start the
# CLIs' OAuth discovery against the proxy, a redirect is never followed, a
# cookie is the vendor's business with no client of ours.
_RESPONSE_DROP = _HOP_BY_HOP | frozenset({
    "set-cookie", "www-authenticate", "location", "content-length",
})

_POOL_MAX = 2304  # ≥ the tunnel's MCP fleet cap (2048) + a machine's headroom

_REFUSAL_PATH = "The gateway forwards only to the MCP's declared endpoint."
_REFUSAL_REDIRECT = "The MCP server answered with a redirect to another address; the gateway follows none."
_REFUSAL_VENDOR_401 = (
    "The MCP server refused the credential; reconnect the account or re-enter the key "
    "in Settings > Integrations."
)


def forward_request_headers(items) -> dict[str, str]:
    """The request headers that reach the upstream: everything the client
    sent minus the dropped set, with ``Accept-Encoding: identity`` when the
    client named none (the response bytes pass through untouched)."""
    out: dict[str, str] = {}
    for k, v in items:
        lk = k.lower()
        if lk in _REQUEST_DROP or lk.startswith("x-forwarded-"):
            continue
        out[k] = v
    if not any(k.lower() == "accept-encoding" for k in out):
        out["Accept-Encoding"] = "identity"
    return out


def forward_response_headers(items) -> list[tuple[str, str]]:
    return [(k, v) for k, v in items if k.lower() not in _RESPONSE_DROP]


def refusal_shape(method: str, body: bytes, message: str, *, code: int = -32001) -> tuple[int, dict, bytes]:
    """The answer a refused request gets, in the shape an MCP client reads
    without giving up: a JSON-RPC error with the request's id for a POST
    that carries one (never a 401, which starts the client's OAuth
    discovery, and never a 4xx without a JSON-RPC body, which ends Codex's
    transport worker), 202 for a notification, 405 for a GET (no stream on
    offer), 200 for a DELETE."""
    if method == "GET":
        return 405, {"Allow": "POST, DELETE"}, b""
    if method == "DELETE":
        return 200, {}, b""
    rid = None
    if body and len(body) <= _REFUSAL_PARSE_CAP:
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and doc.get("id") is not None:
            rid = doc["id"]
    if rid is None:
        return 202, {}, b""
    payload = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()


@dataclass
class Prepared:
    url: object
    headers: dict[str, str]
    proxy_local: bool
    client: object


_clients: dict[tuple[int, bool], object] = {}


def client(proxy_local: bool):
    """The gateway's own HTTP client for the running loop: one pool for
    vendors, one for sidecars (the tunnel's two-class rule, so a vendor's
    held streams never starve a sidecar), no redirects, no read timeout
    (a standing stream), a 10 s connect."""
    import asyncio
    import httpx
    loop = asyncio.get_running_loop()
    key = (id(loop), proxy_local)
    c = _clients.get(key)
    if c is None:
        for stale in [k for k in _clients if k[0] != id(loop)]:
            _clients.pop(stale, None)
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=_POOL_MAX, max_keepalive_connections=_POOL_MAX),
            follow_redirects=False,
        )
        _clients[key] = c
    return c


def upstream_url(res: Resolved, query: bytes, session_id: str):
    """The target, built from parts: the credential's origin and declared
    path, never a byte of the request. A sidecar keeps the client's query
    with ``session_id`` overwritten by the token's; a vendor gets none."""
    import httpx
    from urllib.parse import parse_qsl, urlencode
    parts = urlsplit(res.upstream)
    q = b""
    if res.proxy_local:
        params = [(k, v) for k, v in parse_qsl(query.decode("latin-1"), keep_blank_values=True)
                  if k != "session_id"]
        params.append(("session_id", session_id))
        q = urlencode(params).encode()
    kwargs = {"query": q} if q else {}
    return httpx.URL(scheme=parts.scheme, host=parts.hostname or "", port=parts.port,
                     path=res.path, **kwargs)


async def prepare_forward(
    session_id: str, mcp: str, method: str, rest: str, query: bytes, inbound_headers,
) -> Prepared | Refusal:
    """Everything a front door needs to forward one request of ``session_id``
    for ``mcp``: the credential resolved in a thread, the path confined to
    the declared endpoint, the headers filtered and the credential header
    added, the client picked. A refusal carries the sentence the client
    reads."""
    import asyncio
    cred = credential(session_id, mcp)
    if cred is None:
        return Refusal("no_credential", _REFUSAL_NO_CREDENTIAL)
    if not path_matches(cred.path, rest):
        return Refusal("path_refused", _REFUSAL_PATH)
    res = await asyncio.to_thread(resolve_credential, cred)
    if isinstance(res, Refusal):
        return res
    headers = forward_request_headers(inbound_headers)
    headers[res.header] = res.value
    return Prepared(
        url=upstream_url(res, query, session_id), headers=headers,
        proxy_local=res.proxy_local, client=client(res.proxy_local),
    )


@dataclass
class _PurgeHooks:
    hooks: list = field(default_factory=list)


_purge = _PurgeHooks()


def add_purge_hook(fn) -> None:
    """A callable run with the session id at every purge (the push registry
    registers its wipe here); it must never raise."""
    _purge.hooks.append(fn)


def on_purge(session_id: str) -> None:
    """The broker purged a session: forget its descriptor and tell the
    hooks. Never raises into the session cleanup."""
    try:
        remove_descriptor(session_id)
    except Exception:
        logger.exception("mcp-gateway: purge of %s failed", session_id[:8])
    for fn in list(_purge.hooks):
        try:
            fn(session_id)
        except Exception:
            logger.exception("mcp-gateway: purge hook failed for %s", session_id[:8])
