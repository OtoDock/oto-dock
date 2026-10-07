"""MCP-specification OAuth for a hosted MCP server that names its own
authorization server (``credentials.oauth.authorization_server``).

Discovery: the protected resource metadata of the MCP server (RFC 9728, the
path-suffixed well-known document first, then the root one, then the
``resource_metadata`` of a 401 challenge), then the authorization server's
metadata (RFC 8414 with the path-insert form, then the OpenID variants).
Registration: the install registers itself once per authorization server
and callback URL (RFC 7591) as a public client unless the manifest asks for
a confidential one. The code exchange, the refresh and the revocation carry
the resource indicator (RFC 8707) and PKCE S256.

Every URL fetched and every endpoint read is https, the metadata's issuer
must equal the issuer the URL was built from, every endpoint sits on the
issuer's origin, and nothing leaves the install before the start route has
checked the MCP host against the admin's bearer allowlist.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

import config
from auth.oauth_providers.base import OAuthTokenError, TokenSet, UserInfo

logger = logging.getLogger("claude-proxy.mcp-authorization")

# One vendor call (httpx's timeout is per read, so a slow drip needs a
# total deadline) and the most bytes a metadata document may carry.
CALL_DEADLINE_S = 20
DOC_BYTES_CAP = 64 * 1024
DISCOVERY_TTL_S = 3600
# A refused registration is remembered this long per issuer so repeated
# clicks do not hit the registration endpoint.
REFUSAL_TTL_S = 60
SOFTWARE_ID = "otodock"
CLIENT_URI = "https://otodock.io"

_WWW_AUTH_PARAM = re.compile(r'(\w+)="([^"]*)"')


class AuthorizationError(RuntimeError):
    """A refusal whose message is safe for the card; ``status`` is the HTTP
    status the route answers with."""

    status = 400

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status


class RegistrationUnavailable(AuthorizationError):
    """The authorization server registers no clients."""


class RegistrationRefused(AuthorizationError):
    """The authorization server refused the registration request."""

    def __init__(self, issuer: str, error: str, description: str):
        self.issuer = issuer
        self.error = error
        self.description = description
        detail = error + (f": {description}" if description else "")
        super().__init__(f"{_host(issuer)} refused the registration: {detail}")


class TryAgainLater(AuthorizationError):
    """A passing failure at the vendor (a 429 or a 5xx, no answer)."""

    status = 503


@dataclass
class AuthorizationServer:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    resource: str
    registration_endpoint: str = ""
    revocation_endpoint: str = ""
    scopes_supported: list[str] = field(default_factory=list)
    challenge_scopes: list[str] = field(default_factory=list)
    code_challenge_methods: list[str] = field(default_factory=list)
    token_endpoint_auth_methods: list[str] = field(default_factory=list)
    cimd_supported: bool = False
    iss_parameter_supported: bool = False
    fetched_at: float = 0.0


_discovery: dict[tuple[str, str], AuthorizationServer] = {}
_registration_locks: dict[str, asyncio.Lock] = {}
_refusals: dict[str, tuple[str, str, float]] = {}
_monotonic = time.monotonic


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def canonical_resource(url: str) -> str:
    """The RFC 8707 canonical form of the MCP server's URL: lowercase scheme
    and host, no fragment, the path as declared (a trailing slash is kept
    only when the server declares it)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _require_https(url: str, what: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise AuthorizationError(f"{what} is not an https URL ({_host(url) or url})")


def _well_known(issuer_or_resource: str, suffix: str, *, issuer: bool = False) -> list[str]:
    """The well-known URLs for an issuer or resource, path-insert form first
    (RFC 8414 §3.1 and RFC 9728 §3.1), then the root form. An issuer's
    trailing slash is removed before the insertion (RFC 8414); a resource's
    path is kept as declared (GitHub's server lives at ``/mcp/``)."""
    parts = urlsplit(issuer_or_resource)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/") if issuer else parts.path
    urls = []
    if path and path != "/":
        urls.append(f"{origin}/.well-known/{suffix}{path}")
    urls.append(f"{origin}/.well-known/{suffix}")
    return urls


def _is_private(address: str) -> bool:
    """Not a globally routable address (private, loopback, link-local,
    reserved, multicast, unspecified; an IPv4-mapped IPv6 judged by its
    IPv4 half)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_multicast or not ip.is_global


def _resolve_addresses(host: str) -> list[str]:
    """The addresses a host name resolves to (a literal resolves to itself).
    Runs in a thread; patched in tests."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    if host == "localhost":
        return ["127.0.0.1"]
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    return sorted({info[4][0] for info in infos})


async def _host_is_private(host: str) -> bool:
    addresses = await asyncio.to_thread(_resolve_addresses, host)
    return any(_is_private(a) for a in addresses)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def user_agent() -> str:
    return f"OtoDock/{getattr(config, 'PINNED_OTODOCK_VERSION', '') or 'dev'}"


def _client() -> httpx.AsyncClient:
    """One client per call: no redirects (a metadata URL that redirects is
    not where the document lives), the install's user agent (Notion's edge
    refuses plain python agents), a per-read timeout; the total deadline is
    ``_deadline``'s."""
    return httpx.AsyncClient(
        timeout=15, follow_redirects=False, headers={"User-Agent": user_agent()},
    )


async def _deadline(coro):
    try:
        return await asyncio.wait_for(coro, CALL_DEADLINE_S)
    except asyncio.TimeoutError:
        raise TryAgainLater("the vendor did not answer in time")


async def _read_capped(resp: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > DOC_BYTES_CAP:
            raise AuthorizationError("the vendor's answer is larger than allowed")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class _Answer:
    status: int
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        import json
        try:
            return json.loads(self.body)
        except ValueError:
            return None


async def _post_capped(url: str, **kw) -> _Answer:
    """POST and read at most ``DOC_BYTES_CAP`` bytes of the answer."""
    async with _client() as client:
        async with client.stream("POST", url, **kw) as resp:
            body = await _read_capped(resp)
            return _Answer(resp.status_code, resp.headers, body)


async def _get_json(url: str) -> dict | None:
    """A 200 JSON object at ``url`` (https only), else None: anything else
    (a redirect, a 404, HTML) counts as absent."""
    _require_https(url, "a metadata URL")

    async def _fetch():
        async with _client() as client:
            async with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
                if resp.status_code != 200:
                    return None
                body = await _read_capped(resp)
        try:
            import json
            doc = json.loads(body)
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    try:
        return await _deadline(_fetch())
    except httpx.HTTPError:
        return None


async def _challenge(resource_url: str) -> tuple[str, list[str]]:
    """POST an ``initialize`` without a token and read the 401 challenge's
    ``resource_metadata`` URL and ``scope`` (RFC 9728 §5.1)."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": SOFTWARE_ID, "version": user_agent()}},
    }

    try:
        resp = await _deadline(_post_capped(
            resource_url, json=body,
            headers={"Accept": "application/json, text/event-stream"},
        ))
    except (httpx.HTTPError, AuthorizationError):
        return "", []
    if resp.status != 401:
        return "", []
    header = resp.headers.get("www-authenticate", "")
    params = {k.lower(): v for k, v in _WWW_AUTH_PARAM.findall(header)}
    scopes = [s for s in params.get("scope", "").split() if s]
    return params.get("resource_metadata", ""), scopes


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _resource_matches(doc_resource: str, resource: str, found_at: str) -> bool:
    """RFC 9728 §3.3 by where the document came from (``found_at``): the
    path-suffixed document names the resource itself, the root document the
    origin (or a prefix of the resource), the challenge's document the URL
    requested."""
    got = canonical_resource(str(doc_resource or ""))
    if not got:
        return False
    if found_at == "root":
        return got == _origin(resource) or resource.startswith(got.rstrip("/") + "/") or got == resource
    # A trailing slash the document drops (or adds) is the same resource.
    return got.rstrip("/") == resource.rstrip("/")


async def _protected_resource(resource: str) -> tuple[dict, list[str]]:
    """The resource metadata document and the challenge's scopes."""
    urls = _well_known(resource, "oauth-protected-resource")
    places = ["path", "root"] if len(urls) == 2 else ["root"]
    for url, found_at in zip(urls, places):
        doc = await _get_json(url)
        if doc is None:
            continue
        if not _resource_matches(doc.get("resource", ""), resource, found_at):
            raise AuthorizationError(
                f"{_host(resource)}'s resource metadata names another resource"
            )
        return doc, []
    metadata_url, scopes = await _challenge(resource)
    # The challenge names where the resource's metadata lives (RFC 9728
    # §5.1): on the resource's own origin, or it is not followed.
    if metadata_url and _origin(metadata_url) == _origin(resource):
        doc = await _get_json(metadata_url)
        if doc is not None and _resource_matches(doc.get("resource", ""), resource, "challenge"):
            return doc, scopes
    raise AuthorizationError(
        f"{_host(resource)} names no authorization server (no resource metadata)"
    )


async def _server_metadata(issuer: str) -> dict:
    """The authorization server's metadata (RFC 8414, then OpenID
    discovery), whose ``issuer`` must equal ``issuer``."""
    _require_https(issuer, "the authorization server")
    parts = urlsplit(issuer)
    candidates = _well_known(issuer, "oauth-authorization-server", issuer=True)[:1]
    candidates += _well_known(issuer, "openid-configuration", issuer=True)[:1]
    if parts.path.rstrip("/"):
        candidates.append(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    else:
        candidates = [
            f"{_origin(issuer)}/.well-known/oauth-authorization-server",
            f"{_origin(issuer)}/.well-known/openid-configuration",
        ]
    for url in candidates:
        doc = await _get_json(url)
        if doc is None:
            continue
        if str(doc.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise AuthorizationError(
                f"{_host(issuer)}'s metadata names another issuer"
            )
        return doc
    raise AuthorizationError(f"{_host(issuer)} publishes no authorization server metadata")


def _endpoint(doc: dict, key: str, issuer: str, *, required: bool) -> str:
    value = str(doc.get(key) or "")
    if not value:
        if required:
            raise AuthorizationError(f"{_host(issuer)}'s metadata names no {key}")
        return ""
    _require_https(value, f"the {key}")
    if _origin(value) != _origin(issuer):
        raise AuthorizationError(
            f"{_host(issuer)}'s {key} sits on another origin ({_host(value)})"
        )
    return value


async def discover(resource_url: str, *, issuer_override: str = "") -> AuthorizationServer:
    """The authorization server of the MCP server at ``resource_url``.
    ``issuer_override`` (the manifest's) may only pick an entry the resource
    metadata lists. Cached per resource for ``DISCOVERY_TTL_S``."""
    resource = canonical_resource(resource_url)
    _require_https(resource, "the MCP server URL")
    key = (resource, issuer_override)
    cached = _discovery.get(key)
    if cached and _monotonic() - cached.fetched_at < DISCOVERY_TTL_S:
        return cached

    doc, challenge_scopes = await _protected_resource(resource)
    servers = [str(s).rstrip("/") for s in (doc.get("authorization_servers") or []) if s]
    if not servers:
        raise AuthorizationError(f"{_host(resource)} names no authorization server")
    issuer = servers[0]
    if issuer_override:
        wanted = issuer_override.rstrip("/")
        if wanted not in servers:
            raise AuthorizationError(
                f"the manifest's issuer is not one {_host(resource)} names"
            )
        issuer = wanted
    _require_https(issuer, "the authorization server")
    mcp_private = await _host_is_private(_host(resource))
    if not mcp_private and await _host_is_private(_host(issuer)):
        raise AuthorizationError(
            f"the authorization server {_host(issuer)} is on a private address"
        )

    meta = await _server_metadata(issuer)
    methods = [str(m) for m in (meta.get("code_challenge_methods_supported") or [])]
    if "S256" not in methods:
        raise AuthorizationError(f"{_host(issuer)} does not support PKCE S256")
    server = AuthorizationServer(
        issuer=issuer,
        authorization_endpoint=_endpoint(meta, "authorization_endpoint", issuer, required=True),
        token_endpoint=_endpoint(meta, "token_endpoint", issuer, required=True),
        registration_endpoint=_endpoint(meta, "registration_endpoint", issuer, required=False),
        revocation_endpoint=_endpoint(meta, "revocation_endpoint", issuer, required=False),
        resource=canonical_resource(str(doc.get("resource") or resource)),
        scopes_supported=[str(s) for s in (doc.get("scopes_supported") or [])],
        challenge_scopes=challenge_scopes,
        code_challenge_methods=methods,
        token_endpoint_auth_methods=[
            str(m) for m in (meta.get("token_endpoint_auth_methods_supported") or [])
        ],
        cimd_supported=bool(meta.get("client_id_metadata_document_supported")),
        iss_parameter_supported=bool(meta.get("authorization_response_iss_parameter_supported")),
        fetched_at=_monotonic(),
    )
    _discovery[key] = server
    return server


def resources_of_issuer(issuer: str) -> list[str]:
    """The resources whose cached discovery named ``issuer`` (the admin's
    registration list matches rows to MCPs with it)."""
    wanted = issuer.rstrip("/")
    return sorted({k[0] for k, v in _discovery.items() if v.issuer.rstrip("/") == wanted})


def forget(resource_url: str) -> None:
    """Drop the cached discovery of a resource (its metadata moved)."""
    resource = canonical_resource(resource_url)
    for key in [k for k in _discovery if k[0] == resource]:
        _discovery.pop(key, None)


def clear_caches() -> None:
    """Tests."""
    _discovery.clear()
    _refusals.clear()
    _registration_locks.clear()
    _mode_cache.clear()


# ---------------------------------------------------------------------------
# Registration (RFC 7591)
# ---------------------------------------------------------------------------

def _is_loopback_redirect(redirect_uri: str) -> bool:
    host = _host(redirect_uri)
    return host == "localhost" or _is_private(host) and ipaddress.ip_address(host).is_loopback


def default_client_name() -> str:
    host = _host(config.DASHBOARD_PUBLIC_URL or "") or "this install"
    return f"OtoDock ({host})"


def _auth_method(block: dict, server: AuthorizationServer) -> str:
    if not block.get("confidential", False):
        return "none"
    offered = server.token_endpoint_auth_methods or ["client_secret_basic"]
    for method in ("client_secret_post", "client_secret_basic"):
        if method in offered:
            return method
    raise AuthorizationError(
        f"{_host(server.issuer)} offers no client-secret method for a confidential client"
    )


def _secret_expired(row: dict) -> bool:
    """RFC 7591's ``client_secret_expires_at`` is epoch seconds (``0`` =
    never); an ISO timestamp is read too."""
    raw = str(row.get("client_secret_expires_at") or "")
    if not raw or raw == "0":
        return False
    try:
        return float(raw) <= time.time()
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp() <= time.time()
    except ValueError:
        return False


async def ensure_registration(
    server: AuthorizationServer, *, redirect_uri: str, block: dict, scope: str,
    for_resource: str = "",
) -> dict:
    """The live registration of this install at ``server`` for
    ``redirect_uri``, registering when there is none. One lock per issuer:
    a second connect waits and reuses the row.

    A row is judged against what the install REQUESTED (its stored
    ``requested_auth_method``): a vendor that hands a public request a secret is
    accepted once and its row reused, and only a manifest whose
    ``confidential`` flag changed since registers again. ``for_resource``
    (the MCP server's canonical URL) is recorded on the row, so the admin
    card can name the MCPs it serves after a restart."""
    from storage.identity import oauth_client_registrations as regs
    from storage.pg import run_db

    lock = _registration_locks.setdefault(server.issuer, asyncio.Lock())
    async with lock:
        row = await run_db(regs.get_live, server.issuer, redirect_uri)
        supersede: tuple[int, str] | None = None
        wants_secret = bool(block.get("confidential", False))
        if row is not None:
            reason = ""
            if _secret_expired(row):
                reason = "secret_expired"
            elif row.get("has_secret") and await run_db(regs.client_secret, row["id"]) is None:
                reason = "undecryptable"
            elif (str(row.get("requested_auth_method") or "none") == "none") == wants_secret:
                reason = "auth_method_changed"
            if not reason:
                if for_resource and for_resource not in str(row.get("resources") or "").split():
                    await run_db(regs.add_resource, row["id"], for_resource)
                    row = {**row, "resources": " ".join(
                        [*str(row.get("resources") or "").split(), for_resource])}
                return row
            supersede = (row["id"], reason)
        remembered = _refusals.get(server.issuer)
        if remembered and _monotonic() < remembered[2]:
            raise RegistrationRefused(server.issuer, remembered[0], remembered[1])
        if not server.registration_endpoint:
            raise RegistrationUnavailable(
                f"The authorization server at {_host(server.issuer)} does not register "
                "clients; this MCP cannot be connected from this install."
            )
        method = _auth_method(block, server)
        answer = await _register(server, redirect_uri=redirect_uri, block=block,
                                 method=method, scope=scope)
        row = await run_db(
            regs.insert,
            issuer=server.issuer, redirect_uri=redirect_uri,
            registration_endpoint=server.registration_endpoint,
            client_id=answer["client_id"],
            client_secret=answer.get("client_secret", ""),
            client_secret_expires_at=answer.get("client_secret_expires_at", ""),
            token_endpoint_auth_method=answer["token_endpoint_auth_method"],
            registration_client_uri=answer.get("registration_client_uri", ""),
            registration_access_token=answer.get("registration_access_token", ""),
            client_name=answer.get("client_name", ""), scope=scope,
            supersede_id=supersede[0] if supersede else None,
            supersede_reason=supersede[1] if supersede else "",
            requested_auth_method=method, resource=for_resource,
        )
        logger.info(
            "Registered this install at %s as client %s (%s)",
            server.issuer, row["client_id"], row["token_endpoint_auth_method"],
        )
        return row


async def _register(
    server: AuthorizationServer, *, redirect_uri: str, block: dict, method: str, scope: str,
) -> dict:
    """POST the registration and return the fields the store keeps. The
    answer must echo the redirect URI; a secret-bearing answer for a public
    request is kept with the method the server echoed; a confidential
    request whose answer carries no secret is refused."""
    request = {
        "client_name": block.get("client_name") or default_client_name(),
        "client_uri": CLIENT_URI,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
        "application_type": "native" if _is_loopback_redirect(redirect_uri) else "web",
        "software_id": SOFTWARE_ID,
        "software_version": getattr(config, "PINNED_OTODOCK_VERSION", "") or "dev",
    }
    if scope:
        request["scope"] = scope

    try:
        resp = await _deadline(_post_capped(
            server.registration_endpoint, json=request, headers={"Accept": "application/json"},
        ))
    except httpx.HTTPError:
        raise TryAgainLater(f"{_host(server.issuer)} could not be reached for the registration")
    payload: Any = resp.json()
    if resp.status in (429,) or resp.status >= 500 or 300 <= resp.status < 400:
        raise TryAgainLater(
            f"{_host(server.issuer)} answered {resp.status} to the registration; try again later"
        )
    if resp.status not in (200, 201):
        error = str((payload or {}).get("error") or f"http_{resp.status}") if isinstance(payload, dict) else f"http_{resp.status}"
        description = str((payload or {}).get("error_description") or "") if isinstance(payload, dict) else ""
        _refusals[server.issuer] = (error, description, _monotonic() + REFUSAL_TTL_S)
        raise RegistrationRefused(server.issuer, error, description)
    if not isinstance(payload, dict) or not str(payload.get("client_id") or ""):
        raise AuthorizationError(
            f"{_host(server.issuer)} answered the registration without a client id"
        )
    echoed = [str(u) for u in (payload.get("redirect_uris") or [])]
    if echoed and redirect_uri not in echoed:
        raise AuthorizationError(
            f"{_host(server.issuer)} registered another callback than the one sent"
        )
    if "redirect_uris" not in payload:
        logger.warning("%s echoed no redirect_uris in its registration answer", server.issuer)
    grants = [str(g) for g in (payload.get("grant_types") or [])]
    if grants and "refresh_token" not in grants:
        logger.warning("%s registered the client without the refresh_token grant", server.issuer)
    secret = str(payload.get("client_secret") or "")
    echoed_method = str(payload.get("token_endpoint_auth_method") or method)
    if method != "none" and not secret:
        raise AuthorizationError(
            f"{_host(server.issuer)} registered a confidential client without a secret"
        )
    if secret and echoed_method == "none":
        echoed_method = "client_secret_post"
    expires = payload.get("client_secret_expires_at")
    return {
        "client_id": str(payload["client_id"]),
        "client_secret": secret,
        "client_secret_expires_at": str(expires) if expires is not None else "",
        "token_endpoint_auth_method": echoed_method if secret else "none",
        "registration_client_uri": str(payload.get("registration_client_uri") or ""),
        "registration_access_token": str(payload.get("registration_access_token") or ""),
        "client_name": str(payload.get("client_name") or request["client_name"]),
    }


# ---------------------------------------------------------------------------
# The authorization URL, the exchange, the refresh, the revocation
# ---------------------------------------------------------------------------

def build_authorize_url(
    authorization_endpoint: str, *, client_id: str, redirect_uri: str, scope: str,
    state: str, code_challenge: str, resource: str,
) -> str:
    """The consent URL, the endpoint's own query kept."""
    parts = urlsplit(authorization_endpoint)
    params = parse_qsl(parts.query, keep_blank_values=True)
    params += [
        ("response_type", "code"), ("client_id", client_id),
        ("redirect_uri", redirect_uri), ("state", state),
        ("code_challenge", code_challenge), ("code_challenge_method", "S256"),
        ("resource", resource),
    ]
    if scope:
        params.append(("scope", scope))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))


def _client_auth(method: str, client_id: str, client_secret: str, data: dict) -> dict:
    """The request's client authentication: ``none`` and ``client_secret_post``
    in the body, ``client_secret_basic`` as basic auth."""
    data["client_id"] = client_id
    if method == "client_secret_post":
        data["client_secret"] = client_secret
        return {}
    if method == "client_secret_basic":
        return {"auth": (client_id, client_secret)}
    return {}


async def _token_request(
    provider_id: str, action: str, token_endpoint: str, data: dict, *,
    method: str, client_id: str, client_secret: str,
) -> dict:
    _require_https(token_endpoint, "the token endpoint")
    extra = _client_auth(method, client_id, client_secret, data)
    try:
        resp = await _deadline(_post_capped(
            token_endpoint, data=data, headers={"Accept": "application/json"}, **extra,
        ))
    except (httpx.HTTPError, TryAgainLater, AuthorizationError) as exc:
        raise OAuthTokenError(provider_id, action, "unreachable", type(exc).__name__, 0)
    payload = resp.json()
    if not isinstance(payload, dict):
        payload = {}
    if resp.status != 200 or "error" in payload:
        raise OAuthTokenError(
            provider_id, action,
            str(payload.get("error") or f"http_{resp.status}"),
            str(payload.get("error_description") or ""), resp.status,
        )
    return payload


def _token_set(payload: dict) -> TokenSet:
    """The token set with a raw copy that keeps the vendor's extra fields
    (Notion's ``workspace_id``) and nothing of the token material."""
    raw = {k: v for k, v in payload.items()
           if k not in ("access_token", "refresh_token", "id_token", "expires_in", "token_type", "scope")}
    return TokenSet(
        access_token=str(payload.get("access_token", "")),
        refresh_token=str(payload.get("refresh_token", "") or ""),
        expires_in=int(payload.get("expires_in", 0) or 0),
        scope=str(payload.get("scope", "") or ""),
        token_type=str(payload.get("token_type", "Bearer") or "Bearer"),
        raw=raw,
    )


async def exchange_code(
    provider_id: str, *, token_endpoint: str, code: str, redirect_uri: str,
    code_verifier: str, resource: str, method: str, client_id: str, client_secret: str,
) -> TokenSet:
    payload = await _token_request(
        provider_id, "exchange", token_endpoint,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
         "code_verifier": code_verifier, "resource": resource},
        method=method, client_id=client_id, client_secret=client_secret,
    )
    ts = _token_set(payload)
    if not ts.access_token:
        raise OAuthTokenError(provider_id, "exchange", "no_access_token", "", 200)
    return ts


async def refresh(
    provider_id: str, *, token_endpoint: str, refresh_token: str, resource: str,
    method: str, client_id: str, client_secret: str,
) -> TokenSet:
    payload = await _token_request(
        provider_id, "refresh", token_endpoint,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "resource": resource},
        method=method, client_id=client_id, client_secret=client_secret,
    )
    ts = _token_set(payload)
    if not ts.refresh_token:
        ts.refresh_token = refresh_token
    return ts


async def revoke(
    revocation_endpoint: str, *, issuer: str, token: str, method: str,
    client_id: str, client_secret: str,
) -> bool:
    """Best effort (RFC 7009), only at an https endpoint on the issuer's
    origin."""
    if not revocation_endpoint or _origin(revocation_endpoint) != _origin(issuer):
        return False
    if urlsplit(revocation_endpoint).scheme != "https":
        return False
    data = {"token": token, "token_type_hint": "refresh_token"}
    extra = _client_auth(method, client_id, client_secret, data)
    try:
        resp = await _deadline(_post_capped(revocation_endpoint, data=data, **extra))
    except (httpx.HTTPError, TryAgainLater, AuthorizationError) as exc:
        logger.warning("Revocation at %s failed: %s", issuer, type(exc).__name__)
        return False
    if 200 <= resp.status < 300:
        return True
    logger.warning("Revocation at %s answered %d", issuer, resp.status)
    return False


# ---------------------------------------------------------------------------
# Identity and scopes
# ---------------------------------------------------------------------------

def _dotted(data: dict, key: str) -> str:
    if not key:
        return ""
    v: Any = data.get(key)
    if v is None and "." in key:
        cur: Any = data
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        v = cur
    if isinstance(v, (dict, list)) or v is None:
        return ""
    return str(v)


def identity_from_token_response(
    raw: dict, block: dict, *, provider_id: str, label_hint: str,
) -> tuple[str, UserInfo]:
    """The account label and identity of a connect without a userinfo probe:
    the typed label, else the block's ``identity`` fields read from the
    token response, else the provider id. The display name is the display
    field, else the label."""
    identity = block.get("identity") or {}
    label_value = _dotted(raw, str(identity.get("label_field") or ""))
    display = _dotted(raw, str(identity.get("display_field") or ""))
    account_id = _dotted(raw, str(identity.get("id_field") or ""))
    label = (label_hint or "").strip() or label_value or provider_id
    return label, UserInfo(email=display or label, name="", account_id=account_id or label, raw=dict(raw))


def scopes_for(
    block: dict, *, service_scopes: list[str], server: AuthorizationServer,
) -> str:
    """The scope string of a connect: the services the person picked, else
    the block's, else the 401 challenge's, else the resource's."""
    for candidate in (service_scopes, block.get("scopes") or [], server.challenge_scopes,
                      server.scopes_supported):
        seen: list[str] = []
        for s in candidate:
            if s and s not in seen:
                seen.append(str(s))
        if seen:
            return " ".join(seen)
    return ""


def registration_scope(block: dict, oauth: dict) -> str:
    """The scopes the registration asks for: the union of every service's
    scopes and the block's."""
    seen: list[str] = []
    for svc in oauth.get("services") or []:
        for s in svc.get("scopes") or []:
            if s and s not in seen:
                seen.append(str(s))
    for s in block.get("scopes") or []:
        if s and s not in seen:
            seen.append(str(s))
    return " ".join(seen)


# ---------------------------------------------------------------------------
# The token-origin invariant (synchronous; the resolver's threads call it)
# ---------------------------------------------------------------------------

_MODE_TTL_S = 5.0
_mode_cache: dict[str, tuple[bool, float]] = {}


def registered_client_mode(manifest) -> bool:
    """True when a connect of this manifest goes through the registered
    client. With the block declared that is the rule, since the MCP server
    takes its own authorization server's tokens only; a block with
    ``accepts_app_tokens`` makes it the fallback instead, taken when hosted
    mode is not active and no app credentials are configured. The fallback
    reads the store (call it off the loop); the answer is kept a few
    seconds per manifest, since one session build asks for every account of
    every bearer MCP."""
    oauth = (manifest.credentials.oauth or {}) if manifest and manifest.credentials else {}
    block = oauth.get("authorization_server")
    if not block:
        return False
    if not (isinstance(block, dict) and block.get("accepts_app_tokens") is True):
        return True
    cached = _mode_cache.get(manifest.name)
    now = _monotonic()
    if cached and now - cached[1] < _MODE_TTL_S:
        return cached[0]
    from services.billing import relay_client
    from services.oauth import oauth_engine
    from storage.identity import credential_store
    if relay_client.hosted_oauth_active(manifest.name, manifest):
        answer = False
    else:
        app_cred = oauth.get("app_credential", "")
        creds = credential_store.get_infra_credentials(app_cred) if app_cred else {}
        client_id, client_secret = oauth_engine._resolve_app_credentials(oauth, creds)
        answer = not (client_id and client_secret)
    _mode_cache[manifest.name] = (answer, now)
    return answer


def token_origin_problem(raw: dict, manifest) -> str:
    """Why a token file does not belong to the way this manifest issues
    tokens, or ``""``: in registered-client mode a file must carry the
    ``mcp_authorization`` flow and the manifest's resource (an older relay
    or app token under the same provider id would otherwise be sent to the
    MCP server, which refuses it)."""
    if not registered_client_mode(manifest):
        return ""
    extra = raw.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    if str(extra.get("flow") or "") != "mcp_authorization":
        return "mechanism_changed"
    resource = canonical_resource(manifest.server.url_template or "")
    got = canonical_resource(str(extra.get("resource") or ""))
    # The file holds the resource the server's metadata named, which may be
    # the origin, a prefix, or the URL without its trailing slash (the same
    # equivalence discovery accepts).
    if not got or not (
        got.rstrip("/") == resource.rstrip("/") or got == _origin(resource)
        or resource.startswith(got.rstrip("/") + "/")
    ):
        return "mechanism_changed"
    return ""
