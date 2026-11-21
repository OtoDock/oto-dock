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

``resolve`` blocks (a stat, a file read, two store reads and the egress
guard's name lookup, behind memos): the callers run it in a thread.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import config
from core.credentials.exit_relay import MAX_BODY_BYTES

logger = logging.getLogger("otodock.mcp-gateway")

GATEWAY_PATH_PREFIX = "/v1/mcp-gateway"
#: The kind of a Docker tool's entry (``GatewayCredential.kind``).
CONTAINER = "container"

# The request body cap on both gateways. The gateway carries MCP JSON-RPC
# (a tool call, a result with an inline image) — kilobytes to a few MB, never
# a file upload, so the cap is far below the tunnel's own 128 MB: a session's
# own code cannot park a large buffer in the proxy or the satellite through it.
# The refusal shape reads the request's JSON-RPC id to answer in kind; it never
# parses more than this (a refused request is not forwarded, so a huge body is
# just dropped with a generic 202).
_REFUSAL_PARSE_CAP = 256 * 1024

# A token pushed to a satellite lives at most this long without a renewal
# from the proxy's tick, whatever the vendor token's own expiry.
LEASE_S = 15 * 60
# The tick re-pushes when the lease has less than this left.
RENEW_BEFORE_S = 10 * 60

# The names the platform sets beside a tool's account token (``token_env``,
# ``mcp_broker.provision``), part of the catalog's contract.
TOKEN_EXPIRES_ENV = "OTO_TOKEN_EXPIRES_AT"
TOKEN_SCOPES_ENV = "OTO_TOKEN_SCOPES"
TOKEN_ACCOUNT_ENV = "OTO_TOKEN_ACCOUNT"
TOKEN_RENEWAL_ENV = "OTO_TOKEN_RENEWAL"
TOKEN_ENV_NAMES = (TOKEN_EXPIRES_ENV, TOKEN_SCOPES_ENV, TOKEN_ACCOUNT_ENV, TOKEN_RENEWAL_ENV)
# A token that expires within this life is renewed by its tool, and a tool's
# renewal lasts this long (the life of the session's own token).
TOKEN_RENEWAL_LIFE_S = 24 * 3600
# Under this much life a renewal answers "again in a moment" (the token could
# not be refreshed yet), past the Google library's own 225-second margin.
TOKEN_MIN_LIFE_S = 240

_ALLOWLIST_MEMO_S = 30.0
_EGRESS_MEMO_S = 30.0

# The allowlist row a proxy-local sidecar's host is judged under: a container
# MCP's manifest names ``localhost`` (m365's), and in a containerised
# deployment the sidecar is dialled by its service name.
_SIDECAR_ALLOWLIST_HOST = "localhost"

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,160}")

# Added on every forward of a read-only GitHub reach: GitHub's hosted MCP
# server then offers read tools alone; its path suffix says the same.
READ_ONLY_HEADER = "X-MCP-Readonly"
READ_ONLY_PATH_SUFFIX = "readonly"
# The headers GitHub's server reads to widen or narrow what it offers: a
# narrowed credential forwards none the session sent.
_SERVER_OPTION_HEADER_PREFIX = "x-mcp-"
_IDENTITY_ENCODING = "identity"
_POST = "POST"
# Without a body: GET opens the server's stream, DELETE ends the session.
_BODILESS_METHODS = frozenset({"GET", "DELETE"})

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
    # A narrowed GitHub reach of the agent's service binding
    # (``auth/github_reach.Reach.to_dict``): every request is judged by it.
    reach: dict | None = None
    # ``CONTAINER`` for every Docker tool the platform runs: its requests are
    # its door's (``services/containers/door.py``), with this credential when
    # it has one (a token reference or a header key) and none when not. For
    # such a tool, whether it calls the proxy back (``server.proxy_callbacks``:
    # the door adds the session's callback token) and whether it wakes at the
    # session's handshake (a call's tool).
    kind: str = ""
    callbacks: bool = False
    wakes_early: bool = False

    @property
    def secretless(self) -> bool:
        """A credential that adds nothing: a Docker tool with no sign-in."""
        return self.token_ref is None and not self.value and not self.value_from

    @property
    def forwarded_here(self) -> bool:
        """The proxy forwards this credential's traffic itself, for a
        machine session too: a sidecar, or a credential a reach narrows
        (judged here, its token never pushed to a machine)."""
        return self.proxy_local or bool(self.reach)

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
    def from_descriptor(cls, d: dict) -> GatewayCredential:
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
            reach=d.get("reach") if isinstance(d.get("reach"), dict) else None,
            kind=str(d.get("kind") or ""), callbacks=bool(d.get("callbacks")),
            wakes_early=bool(d.get("wakes_early")),
        )


@dataclass
class CliCredential:
    """The account token a session's command-line tools fetch per command
    through ``/v1/hooks/cli-credential`` (``proxy/hooks/credential_helper.py``
    for ``git``, and through it the ``gh`` wrapper): the host the helper
    serves, the username git presents, the names the wrapper sets on the
    real command's own process, the manifest it was built for, and the
    token file by reference, read at the moment of each call."""

    host: str
    token_ref: TokenRef
    username: str = "x-access-token"
    env_names: tuple[str, ...] = ()
    manifest: str = ""
    # A narrowed GitHub reach (``auth/github_reach``): each command's request
    # names its repository and is judged by it.
    reach: dict | None = None
    # The helper hands the token itself per command: on a paired macOS or
    # Windows computer, where git and gh verify with the system store and
    # cannot trust the machine's exit (``mcp_session_config.cli_per_command``).
    # Everywhere else the session holds a placeholder and nothing else.
    per_command: bool = False

    def descriptor(self) -> dict:
        d = asdict(self)
        d["env_names"] = list(self.env_names)
        return d

    @classmethod
    def from_descriptor(cls, d: dict) -> CliCredential | None:
        ref = d.get("token_ref")
        if not isinstance(ref, dict) or not d.get("host"):
            return None
        return cls(
            host=str(d["host"]),
            token_ref=TokenRef(**ref),
            username=str(d.get("username") or "x-access-token"),
            env_names=tuple(str(n) for n in (d.get("env_names") or [])),
            manifest=str(d.get("manifest") or ""),
            reach=d.get("reach") if isinstance(d.get("reach"), dict) else None,
            per_command=d.get("per_command") is True,
        )


@dataclass
class TokenCredential:
    """An account's access token a stdio tool holds in its own environment
    (the manifest's ``mcp_env_injection``): the names it lands under and the
    token file by reference, read at every fetch. ``renews``: the token
    expires within a session's life, so the tool gets a renewal token and
    reads the bundle again when the hour ends (``mcp_broker.mint_renewal``)."""

    env_names: tuple[str, ...]
    token_ref: TokenRef
    manifest: str = ""
    renews: bool = False

    def descriptor(self) -> dict:
        d = asdict(self)
        d["env_names"] = list(self.env_names)
        return d

    @classmethod
    def from_descriptor(cls, d: dict) -> TokenCredential | None:
        ref = d.get("token_ref")
        if not isinstance(ref, dict) or not d.get("env_names"):
            return None
        return cls(
            env_names=tuple(str(n) for n in d["env_names"]),
            token_ref=TokenRef(**ref),
            manifest=str(d.get("manifest") or ""),
            renews=bool(d.get("renews")),
        )


@dataclass(frozen=True)
class ExitCredential:
    """One secret the session holds as a placeholder, which the credential
    exit (``core/credentials/credential_exit.py``) swaps for the value in
    ``header`` alone, toward ``hosts`` alone (literal names or ``*.domain``).
    The value is a static one as the build resolved it (``value``), or the
    account's token file by reference (``token_ref``, read fresh per
    request), or the command-line credential's (``cli``: git and gh, whose
    host is the authority the manifest named). ``env_name`` is where the
    placeholder sits in the tool's environment ("" for the command-line
    credential, which the helper hands git per command).
    ``streams_declared``: the manifest's entry asked for the larger path
    (``streams``)."""

    placeholder: str
    header: str
    hosts: tuple[str, ...]
    allowlist_key: str
    manifest: str
    env_name: str = ""
    prefix: str = ""
    value: str = ""
    token_ref: TokenRef | None = None
    cli: CliCredential | None = None
    streams_declared: bool = False

    @property
    def key(self) -> str:
        """The name the log calls this credential by: its env name, else its
        manifest, else its allowlist key."""
        return self.env_name or self.manifest or self.allowlist_key

    @property
    def streams(self) -> bool:
        """Whether a large body, or one of unknown length, passes the exit as
        it arrives (the relay's ``Entry.streams``): the command-line
        credential's does, since a push's pack has no bound, and a tool's
        whose manifest entry asked for it."""
        return self.cli is not None or self.streams_declared

    @property
    def judged(self) -> bool:
        """Whether the exit asks about each request that carries this
        placeholder (the relay's ``Entry.judged``): a command-line credential
        whose reach narrows (``auth/github_reach.judge_request``)."""
        if self.cli is None:
            return False
        from auth import github_reach
        reach = github_reach.Reach.from_dict(self.cli.reach)
        return reach is not None and reach.narrows

    def takes_host(self, host: str) -> bool:
        """Whether ``host`` is one the value may be sent to (the relay's rule)."""
        from core.credentials.exit_relay import host_matches
        return host_matches(self.hosts, host)

    def gateway_for(self, host: str) -> GatewayCredential:
        """The gateway's shape of this credential toward ``host``, for
        ``resolve_credential`` (the allowlist, the egress guard, the value)."""
        return GatewayCredential(
            upstream=f"https://{host}", path="/", allowlist_key=self.allowlist_key,
            header=self.header, prefix=self.prefix, value=self.value, token_ref=self.token_ref,
            manifest=self.manifest, value_from=self.env_name,
        )

    def descriptor(self) -> dict:
        """The secret-free shape: the value replaced by the fact that one
        existed. The placeholder stays (a random string nothing swaps
        without the exit's admission, and a machine session taken back
        after a restart holds it in its tools' environments); the
        command-line credential is the bundle's, so its entry is the fact
        that there is one."""
        d = asdict(self)
        d["value"] = ""
        d["has_value"] = bool(self.value)
        d["cli"] = self.cli is not None
        return d

    @classmethod
    def from_descriptor(cls, d: dict, *, cli: CliCredential | None = None) -> ExitCredential | None:
        """The entry back from its descriptor, the value to be resolved again
        (``has_value``) and the bundle's own command-line credential set."""
        if not isinstance(d, dict) or not d.get("placeholder") or not d.get("header"):
            return None
        ref = d.get("token_ref")
        return cls(
            placeholder=str(d["placeholder"]), header=str(d["header"]),
            hosts=tuple(str(h) for h in (d.get("hosts") or [])),
            allowlist_key=str(d.get("allowlist_key") or ""), manifest=str(d.get("manifest") or ""),
            env_name=str(d.get("env_name") or ""), prefix=str(d.get("prefix") or ""), value="",
            token_ref=TokenRef(**ref) if isinstance(ref, dict) else None,
            cli=cli if d.get("cli") else None,
            streams_declared=d.get("streams_declared") is True,
        )


@dataclass
class Resolved:
    header: str
    value: str
    upstream: str
    path: str
    proxy_local: bool
    expires_in: float | None = None
    # A token-file credential's: the digest of the token sent
    # (``token_digest``) and the methods whose refusal marked the MCP
    # (``credential_resolver.marked_methods``; ``()`` when none).
    digest: str = ""
    marked: tuple[str, ...] = ()


@dataclass
class ResolvedCli:
    username: str
    secret: str
    env_names: tuple[str, ...]


@dataclass
class ResolvedToken:
    """A tool's account token as the file holds it now."""

    secret: str
    expires_at: str
    expires_in: float | None
    scopes: tuple[str, ...]
    account: str


@dataclass
class Refusal:
    reason: str
    detail: str


@dataclass
class Answered:
    """What a Docker tool's door answers itself (``services/containers/
    door.py``): shaped as the upstream response a carrier streams back, its
    body whole or a stream. The door's use count is the stream's own: its
    end, however it ends, gives it back."""

    status_code: int
    headers: dict[str, str]
    body: bytes = b""
    stream: Any = None

    async def aiter_raw(self):
        if self.stream is None:
            if self.body:
                yield self.body
            return
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        if self.stream is not None:
            await self.stream.aclose()


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


def is_own_entry(url: str) -> bool:
    """A gateway URL on this proxy's own loopback listener (the main port or
    the internal one): the platform's own hop, never a manifest's host."""
    if gateway_key_of(url) is None:
        return False
    parts = urlsplit(url)
    ports = {config.PORT, int(getattr(config, "INTERNAL_LISTENER_PORT", 0) or 0)} - {0}
    return parts.hostname == "127.0.0.1" and parts.port in ports


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


def token_credentials_of(session_id: str) -> dict[str, TokenCredential]:
    """Every tool's account token a session holds, by MCP key."""
    from core.credentials import mcp_broker
    out: dict[str, TokenCredential] = {}
    for key, bundle in (mcp_broker.bundles_of(session_id) or {}).items():
        cred = getattr(bundle, "token", None)
        if cred is not None:
            out[key] = cred
    return out


def account_text(ref) -> str:
    """An account as a session's records name it: ``"<owner's sub>|<label>"``
    of a resolver's ``AccountRef``, "" for none. Never a value."""
    return f"{ref.owner_sub}|{ref.label}" if ref is not None else ""


def env_names_of(session_id: str) -> dict[str, list[str]]:
    """Per tool key, the names a stdio tool's bundle sets in its environment
    (names alone: the values never leave memory), less the account token's
    names and the two beside them, which every fetch reads fresh and the
    provision mints again. A re-adoption resolves them again
    (``env_values_for``)."""
    from core.credentials import mcp_broker
    out: dict[str, list[str]] = {}
    for key, bundle in (mcp_broker.bundles_of(session_id) or {}).items():
        token = getattr(bundle, "token", None)
        skip = {TOKEN_EXPIRES_ENV, TOKEN_RENEWAL_ENV, *(token.env_names if token is not None else ())}
        names = sorted(n for n in (getattr(bundle, "env", None) or {}) if n not in skip)
        if names:
            out[key] = names
    return out


def accounts_of(session_id: str) -> dict[str, str]:
    """Per tool key, the account its bundle was built on (``account_text``:
    the owner's sub and the label, never a value), for the bundles built on
    one. A re-adoption takes a tool's secrets again only on it."""
    from core.credentials import mcp_broker
    return {key: bundle.account for key, bundle in (mcp_broker.bundles_of(session_id) or {}).items()
            if getattr(bundle, "account", "")}


def exit_credentials_of(session_id: str) -> dict[str, tuple[ExitCredential, ...]]:
    """Every exit half a session holds, by MCP key (the bundles with one)."""
    from core.credentials import mcp_broker
    out: dict[str, tuple[ExitCredential, ...]] = {}
    for key, bundle in (mcp_broker.bundles_of(session_id) or {}).items():
        creds = tuple(getattr(bundle, "exit", ()) or ())
        if creds:
            out[key] = creds
    return out


def cli_credentials_of(session_id: str) -> dict[str, CliCredential]:
    """Every command-line credential a session holds, by MCP key."""
    from core.credentials import mcp_broker
    out: dict[str, CliCredential] = {}
    for key, bundle in (mcp_broker.bundles_of(session_id) or {}).items():
        cred = getattr(bundle, "cli", None)
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
# upstream -> whether the environment proxies it (static while the proxy runs)
_proxied_memo: dict[str, bool] = {}
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
    The rule is ``pinned_http.control_plane``, the same one the vendor
    pool applies when it connects (``client``), so a record that answers
    differently between this lookup and the dial is refused there; this
    pre-check keeps the message the MCP client reads."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    from services.infra import outbound_url, pinned_http
    now = time.monotonic()
    with _lock:
        hit = _egress_memo.get(upstream)
    if hit is not None and hit[1] > now:
        return hit[0]
    parts = urlsplit(upstream)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    verdict: str | None = None
    # Behind the environment's egress proxy the proxy resolves the name, and
    # this machine's resolver may know nothing of it: the local lookup is
    # best effort there (no answer passes), while an answer in the
    # platform's own space, and a name that only ever means this machine,
    # are still refused (a proxy on this machine would dial its loopback).
    proxied = _proxied(upstream)
    if proxied and outbound_url.names_this_machine(host):
        verdict = f"the MCP server host {host} names this machine"
        with _lock:
            _egress_memo[upstream] = (verdict, now + _EGRESS_MEMO_S)
        return verdict
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        infos = []
        if not proxied:
            verdict = f"the MCP server host {host} does not resolve"
    if infos:
        for info in infos:
            ip = cast("str", info[4][0])  # an IP family's sockaddr starts with the address
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                verdict = f"the MCP server host {host} resolves to an unreadable address"
                break
            if pinned_http.control_plane(ip):
                where = ("a private address, which a hosted install never reaches"
                         if config.OTODOCK_CLOUD else "the platform's own address space")
                verdict = f"the MCP server host {host} resolves to {where} ({ip})"
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
    """The header to add for ``cred`` right now, or why not. A Docker
    tool with no sign-in adds none."""
    if cred.kind == CONTAINER and cred.secretless:
        return Resolved(header="", value="", upstream=cred.upstream, path=cred.path, proxy_local=True)
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
    digest = ""
    marked: tuple[str, ...] = ()
    if cred.token_ref is not None:
        got = _token_secret(cred.token_ref, cred.manifest)
        if isinstance(got, Refusal):
            return got
        secret, expires_in, marked = got
        digest = _digest(secret)
    else:
        secret = cred.value
    if not secret:
        return Refusal("no_value", _REFUSAL_NO_VALUE)
    return Resolved(
        header=cred.header, value=f"{cred.prefix}{secret}", upstream=cred.upstream,
        path=cred.path, proxy_local=cred.proxy_local, expires_in=expires_in,
        digest=digest, marked=marked,
    )


def _secret_of(raw: dict, ref: TokenRef) -> str:
    """The secret a token file gives a reference: the preferred bearer of
    ``extra`` when the reference or the file names one, else the access
    token."""
    from services.oauth import oauth_account_store
    extra = raw.get("extra") or {}
    preferred = ref.preferred_bearer or (
        extra.get("preferred_bearer", "") if isinstance(extra, dict) else "")
    if preferred and isinstance(extra, dict) and extra.get(preferred):
        return str(extra.get(preferred) or "")
    return oauth_account_store.get_canonical_access_token(raw)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def token_digest(raw: dict, ref: TokenRef) -> str:
    """A short digest of the secret ``raw`` gives ``ref`` now (``""`` for
    none): what a later rewrite is compared with, never the token."""
    secret = _secret_of(raw, ref)
    return _digest(secret) if secret else ""


def _token_secret(ref: TokenRef, manifest_name: str) -> tuple[str, float | None, tuple[str, ...]] | Refusal:
    """The token file's current secret, its remaining life and the methods
    whose refusal marked the MCP, or why it cannot be used: no file, an
    account that needs a reconnect, no value. A mark of the MCP's own
    server does not refuse here: a session that holds the MCP keeps
    calling, and the call its server answers clears the mark."""
    raw = _read_token(ref)
    if not raw:
        return Refusal("no_value", _REFUSAL_NO_VALUE)
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver, oauth_account_store
    manifest = mcp_registry.get_manifest(manifest_name) if manifest_name else None
    dead = (credential_resolver.account_unusable_reason(raw, manifest, refused=False) if manifest
            else oauth_account_store.token_dead_reason(raw))
    if dead:
        return Refusal("needs_reconnect", credential_resolver.reconnect_wording(dead))
    secret = _secret_of(raw, ref)
    if not secret:
        return Refusal("no_value", _REFUSAL_NO_VALUE)
    return secret, _expires_in(raw), credential_resolver.marked_methods(raw, manifest_name)


def resolve_cli_credential(cred: CliCredential) -> ResolvedCli | Refusal:
    """The token a command-line tool gets for ``cred`` right now, or why not
    (the same reader and the same reconnect rule as the gateway's)."""
    got = _token_secret(cred.token_ref, cred.manifest)
    if isinstance(got, Refusal):
        return got
    return ResolvedCli(username=cred.username, secret=got[0], env_names=cred.env_names)


def resolved_token_of(raw: dict, ref: TokenRef) -> ResolvedToken:
    """What a token file gives a tool: the secret, its expiry, the scopes the
    sign-in granted, the account's label (its address, for Google)."""
    scopes = raw.get("scopes")
    return ResolvedToken(
        secret=_secret_of(raw, ref), expires_at=str(raw.get("expires_at") or ""),
        expires_in=_expires_in(raw),
        scopes=tuple(str(s) for s in scopes) if isinstance(scopes, list) else (),
        account=ref.account_label,
    )


def resolve_token_credential(cred: TokenCredential) -> ResolvedToken | Refusal:
    """The token a tool gets for ``cred`` right now, or why not (the same
    reader and the same reconnect rule as the gateway's)."""
    got = _token_secret(cred.token_ref, cred.manifest)
    if isinstance(got, Refusal):
        return got
    raw = _read_token(cred.token_ref)
    if not raw:
        return Refusal("no_value", _REFUSAL_NO_VALUE)
    return resolved_token_of(raw, cred.token_ref)


def token_env(cred: TokenCredential, token: ResolvedToken) -> dict[str, str]:
    """The env a tool's process gets for its account token: the value under
    the manifest's names, and beside it what the tool needs to know when to
    ask again and what it was granted."""
    env = {name: token.secret for name in cred.env_names}
    if token.expires_at:
        env[TOKEN_EXPIRES_ENV] = token.expires_at
    if token.scopes:
        env[TOKEN_SCOPES_ENV] = " ".join(token.scopes)
    if token.account:
        env[TOKEN_ACCOUNT_ENV] = token.account
    return env


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
        _proxied_memo.clear()
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
    cli: dict[str, CliCredential] | None = None,
    tokens: dict[str, TokenCredential] | None = None,
    exits: dict[str, tuple[ExitCredential, ...]] | None = None,
    machine_id: str = "", token_hash: str = "", agent: str = "", user_sub: str = "",
    task_scope: str = "", account_choices: Mapping[str, str] | None = None, person_sub: str = "",
    envs: Mapping[str, list[str]] | None = None, accounts: Mapping[str, str] | None = None,
) -> None:
    """Record what a session was provisioned with, by reference (the gateway
    credentials, the command-line ones, the tools' account tokens and the
    credential exit's entries), plus the identity its static values resolve
    under (the agent, the person, the scope, the chat's choice of account
    per MCP), and ``person_sub``, the person in the chat when that is not
    whose accounts it used (the hosted relay's token is minted for the
    person). A descriptor without a choice carries no ``account_choices``
    key. ``envs`` names, per tool key, what each stdio tool's bundle sets in
    its environment (``env_names_of``: names, never values), so a tool that
    first starts after a restart gets them again; ``accounts`` the account
    each tool key was built on (``accounts_of``), the only one a re-adoption
    takes its values on again. Nothing to record removes an earlier
    descriptor."""
    path = _descriptor_path(session_id)
    if path is None:
        return
    if not credentials and not cli and not tokens and not exits and not envs:
        remove_descriptor(session_id)
        return
    doc = {
        "machine_id": machine_id,
        "token_hash": token_hash,
        "agent": agent,
        "user_sub": user_sub,
        "task_scope": task_scope,
        "credentials": {k: c.descriptor() for k, c in credentials.items()},
        "cli": {k: c.descriptor() for k, c in (cli or {}).items()},
        "tokens": {k: c.descriptor() for k, c in (tokens or {}).items()},
        "exits": {k: [c.descriptor() for c in v] for k, v in (exits or {}).items() if v},
    }
    if account_choices:
        doc["account_choices"] = {str(k): str(v) for k, v in account_choices.items()}
    if envs:
        doc["envs"] = {str(k): [str(n) for n in v] for k, v in envs.items() if v}
    if accounts:
        doc["accounts"] = {str(k): str(v) for k, v in accounts.items() if v}
    if person_sub:
        doc["person_sub"] = person_sub
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
    cli: dict[str, CliCredential] = {}
    for key, d in (doc.get("cli") or {}).items():
        c = CliCredential.from_descriptor(d) if isinstance(d, dict) else None
        if c is not None:
            cli[str(key)] = c
    token_creds: dict[str, TokenCredential] = {}
    for key, d in (doc.get("tokens") or {}).items():
        tc = TokenCredential.from_descriptor(d) if isinstance(d, dict) else None
        if tc is not None:
            token_creds[str(key)] = tc
    choices = doc.get("account_choices")
    exits_raw = doc.get("exits")
    exits = ({str(k): [d for d in v if isinstance(d, dict)] for k, v in exits_raw.items() if isinstance(v, list)}
             if isinstance(exits_raw, dict) else {})
    envs_raw = doc.get("envs")
    envs = ({str(k): [str(n) for n in v if isinstance(n, str) and n] for k, v in envs_raw.items()
             if isinstance(v, list)} if isinstance(envs_raw, dict) else {})
    accounts_raw = doc.get("accounts")
    accounts = ({str(k): v for k, v in accounts_raw.items() if isinstance(v, str) and v}
                if isinstance(accounts_raw, dict) else {})
    return {
        "machine_id": str(doc.get("machine_id") or ""),
        "token_hash": str(doc.get("token_hash") or ""),
        "agent": str(doc.get("agent") or ""),
        "user_sub": str(doc.get("user_sub") or ""),
        "person_sub": str(doc.get("person_sub") or doc.get("user_sub") or ""),
        "task_scope": str(doc.get("task_scope") or ""),
        "account_choices": ({str(k): str(v) for k, v in choices.items() if k and v}
                            if isinstance(choices, dict) else {}),
        "credentials": out,
        "cli": cli,
        "tokens": token_creds,
        "envs": {k: v for k, v in envs.items() if v},
        "accounts": accounts,
        # The exit entries as written: the re-adoption rebuilds them with the
        # bundle's command-line credential (``ExitCredential.from_descriptor``).
        "exits": exits,
    }


def static_values_for(
    agent: str, user_sub: str, task_scope: str, creds: dict[str, GatewayCredential],
    account_choices: Mapping[str, str] | None = None, built: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The static values (header-style keys) of ``creds`` resolved again for
    the identity a descriptor names, by MCP key; a token-file credential
    needs none. As the session build does: the MCP's resolved secrets, then
    the field values of the env-delivered instance the agent takes
    (``mcp_registry.env_instance_values``), the instance's winning. The
    secrets are the account the chat chose (``account_choices``, the
    descriptor's) when the agent's set holds it, and only the account each
    MCP was built on (``built``, ``_resolved_envs``). Blocking (the
    credential resolver's and the instance store's reads)."""
    wanted = {k: c for k, c in creds.items() if c.token_ref is None and c.value_from}
    if not wanted:
        return {}
    envs = _resolved_envs(agent, user_sub, task_scope, {c.manifest or k for k, c in wanted.items()},
                          account_choices, built)
    out: dict[str, str] = {}
    for key, cred in wanted.items():
        value = envs.get(cred.manifest or key, {}).get(cred.value_from, "")
        if value:
            out[key] = value
    return out


def env_values_for(
    agent: str, user_sub: str, task_scope: str, wanted: Mapping[str, tuple[str, tuple[str, ...]]],
    account_choices: Mapping[str, str] | None = None, built: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """A stdio tool's environment values resolved again for the identity a
    descriptor names, as ``static_values_for`` resolves a gateway's, on the
    account each was built on: per tool key, ``wanted`` maps it to its
    manifest's name and the names to resolve; the answer holds the names
    that resolved, host-self addresses rewritten as the build rewrites
    them. Blocking."""
    if not wanted:
        return {}
    from services.mcp import mcp_registry
    from services.mcp.mcp_egress import _rewrite_host_self_targets_to_loopback
    envs = _resolved_envs(agent, user_sub, task_scope, {m for m, _names in wanted.values()},
                          account_choices, built)
    out: dict[str, dict[str, str]] = {}
    for key, (manifest_name, names) in wanted.items():
        env = {n: v for n, v in envs.get(manifest_name, {}).items() if n in names and v}
        manifest = mcp_registry.get_manifest(manifest_name)
        if manifest is not None and env:
            _rewrite_host_self_targets_to_loopback(manifest, env, agent, user_sub=user_sub or None,
                                                   task_scope=task_scope or "user")
        out[key] = env
    return out


def _resolved_envs(agent: str, user_sub: str, task_scope: str, manifests: set[str],
                   account_choices: Mapping[str, str] | None,
                   built: Mapping[str, str] | None) -> dict[str, dict[str, str]]:
    """The secrets the session build resolves per MCP for this identity and
    choice of account, then the field values of the env-delivered instance
    the agent takes (``mcp_registry.env_instance_values``), the instance's
    winning. Only on the account each MCP was built on (``built``, per MCP,
    ``account_text``; one not named was built on none): an MCP this
    resolution takes another account for, or none, gets nothing, so a
    session taken back never moves to another account by itself (another
    lender's of the agent's set, whose secret the build's owner filter kept
    off a person's own computer, or another of the person's own). The
    account compared is the one this same resolution read the values from."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    from services.oauth.credential_resolver import choice_kw
    result = credential_resolver.resolve_credentials(
        agent, user_sub or None, task_scope=task_scope or "user", **choice_kw(account_choices),
    )
    out: dict[str, dict[str, str]] = {}
    for name in manifests:
        if account_text(result.accounts.get(name)) != (built or {}).get(name, ""):
            logger.info("mcp-gateway: %s resolves on another account than its session was built on: "
                        "not taken back", name)
            continue
        env = dict(result.env_by_mcp.get(name, {}))
        manifest = mcp_registry.get_manifest(name)
        if manifest is not None:
            env.update(mcp_registry.env_instance_values(manifest, agent))
        out[name] = env
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


def request_methods(body: bytes) -> tuple[str, ...]:
    """The JSON-RPC methods a request body names (a batch's, in order), or
    ``()`` for a body that is not JSON-RPC or is larger than the parse cap."""
    if not body or len(body) > _REFUSAL_PARSE_CAP:
        return ()
    try:
        doc = json.loads(body)
    except ValueError:
        return ()
    messages = doc if isinstance(doc, list) else [doc]
    return tuple(m["method"] for m in messages
                 if isinstance(m, dict) and isinstance(m.get("method"), str) and m["method"])


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
    doc = None
    if body and len(body) <= _REFUSAL_PARSE_CAP:
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
    if isinstance(doc, list):
        # A batch: one error per message that awaits an answer.
        errors = [{"jsonrpc": "2.0", "id": m["id"], "error": {"code": code, "message": message}}
                  for m in doc if isinstance(m, dict) and m.get("id") is not None]
        if not errors:
            return 202, {}, b""
        return 200, {"Content-Type": "application/json"}, json.dumps(errors).encode()
    rid = doc.get("id") if isinstance(doc, dict) else None
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
    # The body to send when a reach rewrote it (a search given the reach's
    # ``repo:`` qualifiers); ``None`` = the request's own.
    content: bytes | None = None
    # A token-file credential's reference, its manifest, the digest of the
    # token sent and the methods whose refusal marked the MCP: what the
    # route needs to note the server's answer (``services/oauth/token_refusals``).
    token_ref: TokenRef | None = None
    manifest: str = ""
    digest: str = ""
    marked: tuple[str, ...] = ()


def _writes(mcp: str, manifest: str):
    """Whether a tool writes, by the annotations its server last sent
    (``None`` while unknown)."""
    from services.mcp import mcp_permissions

    def writes(tool: str) -> bool | None:
        hints = mcp_permissions.tool_hints(mcp, tool) or (
            mcp_permissions.tool_hints(manifest, tool) if manifest else None)
        if not hints:
            return None
        return hints.get("readOnlyHint") is not True
    return writes


def judge_reach(
    cred: GatewayCredential, mcp: str, method: str, body: bytes, inbound_headers=(),
) -> tuple[str, bytes | None]:
    """A narrowed credential's request judged by its reach
    (``auth/github_reach.judge_mcp``): the refusal sentence, else the body
    to forward (``None`` = as it came: a request with no body). A body
    must be plain JSON the judge reads as the server will. Blocking (the
    JSON of a body): the callers run it in a thread."""
    from auth import github_reach
    reach = github_reach.Reach.from_dict(cred.reach)
    if reach is None or not reach.narrows:
        return "", None
    encodings = [v.strip().lower() for k, v in inbound_headers if k.lower() == "content-encoding"]
    if any(e and e != _IDENTITY_ENCODING for e in encodings):
        return github_reach.UNREADABLE, None
    if len(body) > MAX_BODY_BYTES:
        return github_reach.UNREADABLE, None
    # The method as the upstream client will send it (httpx uppercases a
    # tunnel frame's): only a POST carries a call to judge.
    verb = (method or "").upper()
    if verb in _BODILESS_METHODS and not body:
        return "", None
    if verb != _POST:
        return github_reach.METHOD_REFUSED, None
    verdict = github_reach.judge_mcp(reach, body, writes=_writes(mcp, cred.manifest))
    return verdict.refusal, verdict.body


OUTSIDE_METHOD_REFUSED = "Outside people may not use this request of the tool."
LIMIT_METHOD_REFUSED = "This tool is limited for this agent, and it may not use this request."
OUTSIDE_UNREADABLE = "This request to the tool could not be read, so it was refused."


def judge_outside(grants, mcp: str, method: str, body: bytes, inbound_headers=(),
                  *, refused: str = "") -> str:
    """An outside session's request to an attached server, judged by the
    functions its Outside people pick granted (``outside_picks``, by server
    key): the refusal sentence, or "" to forward the body as it came. Every
    function granted (a Read and write pick, a channel tool) passes
    whole. Otherwise a bodiless GET or DELETE passes (the stream, the
    session's end), a POST body is read with the reach judge's strict parser
    (one message or a batch): the plain methods pass, a ``tools/call`` whose
    name is granted passes, everything else is refused, an unreadable body
    with it. Blocking (the JSON): the caller runs it in a thread. ``refused``
    words a refused function for a tool limit (the outside sentence
    otherwise)."""
    from auth import github_reach
    from services.mcp import outside_picks
    names = outside_picks.granted(grants, mcp)
    if names is outside_picks.EVERY_FUNCTION:
        return ""
    verb = (method or "").upper()
    if verb in _BODILESS_METHODS and not body:
        return ""
    if verb != _POST:
        return OUTSIDE_METHOD_REFUSED
    encodings = [v.strip().lower() for k, v in inbound_headers if k.lower() == "content-encoding"]
    if any(e and e != _IDENTITY_ENCODING for e in encodings) or len(body) > MAX_BODY_BYTES:
        return OUTSIDE_UNREADABLE
    try:
        doc = json.loads(body.decode("utf-8"), object_pairs_hook=github_reach._strict_object,
                         parse_constant=github_reach._no_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return OUTSIDE_UNREADABLE
    if not isinstance(doc, (dict, list)) or (isinstance(doc, list) and not doc):
        return OUTSIDE_UNREADABLE
    for msg in doc if isinstance(doc, list) else [doc]:
        if not isinstance(msg, dict):
            return OUTSIDE_UNREADABLE
        name = msg.get("method")
        if name is None:
            continue  # the client's answer to the server
        if not isinstance(name, str):
            return OUTSIDE_UNREADABLE
        if name in github_reach._PLAIN_METHODS or name.startswith(github_reach._PLAIN_PREFIXES):
            continue
        if name == github_reach._METHOD_CALL:
            params = msg.get("params")
            function = str(params.get("name") or "") if isinstance(params, dict) else ""
            if function and function in names:
                continue
            return refused or "This function of the tool is not open to outside people on this line."
        return refused or OUTSIDE_METHOD_REFUSED
    return ""


def _reach_headers(cred: GatewayCredential, headers: dict[str, str]) -> None:
    """A narrowed credential forwards none of the server's option headers
    the session sent (two copies of one name read as the first); under a
    read-only reach, GitHub's own read-only mode."""
    if not cred.reach:
        return
    for k in [k for k in headers if k.lower().startswith(_SERVER_OPTION_HEADER_PREFIX)]:
        del headers[k]
    if cred.reach.get("read_only"):
        headers[READ_ONLY_HEADER] = "true"


def _reach_path(cred: GatewayCredential, path: str) -> str:
    """Under a read-only reach the forward goes to the declared path's
    read-only twin (GitHub's server: ``/mcp/readonly``)."""
    if cred.reach and cred.reach.get("read_only"):
        return path.rstrip("/") + "/" + READ_ONLY_PATH_SUFFIX
    return path


# (loop id, pool kind) -> client; the kinds are "sidecar", "vendor", "proxied"
_clients: dict[tuple[int, str], object] = {}


def _proxied(upstream: str) -> bool:
    """Whether the environment's proxy carries ``upstream`` (memoised: the
    forward runs per MCP request, and the environment does not change while
    the proxy runs)."""
    if not upstream:
        return False
    with _lock:
        hit = _proxied_memo.get(upstream)
    if hit is None:
        from services.infra import pinned_http
        hit = pinned_http.proxied(upstream)
        with _lock:
            _proxied_memo[upstream] = hit
    return hit


def client(proxy_local: bool, upstream: str = ""):
    """The gateway's own HTTP client for the running loop: one pool for
    sidecars, one for vendors (the tunnel's two-class rule, so a vendor's
    held streams never starve a sidecar) and one plain pool for a vendor
    the environment's proxy carries; no redirects, no read timeout (a
    standing stream), a 10 s connect. The vendor pool connects only to
    addresses that pass ``_egress_refusal``'s rule, resolved at the dial
    (``pinned_http.control_plane``), and on a hosted install the sidecar
    pool only to the install's own tool networks (``docker_tool``); the
    pool limits sit on the transport, the only place a custom transport
    reads them."""
    import asyncio

    import httpx

    from services.infra import pinned_http
    loop = asyncio.get_running_loop()
    kind = "sidecar" if proxy_local else ("proxied" if _proxied(upstream) else "vendor")
    key = (id(loop), kind)
    c = _clients.get(key)
    if c is None:
        for stale in [k for k in _clients if k[0] != id(loop)]:
            _clients.pop(stale, None)
        limits = httpx.Limits(max_connections=_POOL_MAX, max_keepalive_connections=_POOL_MAX)
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        if kind == "vendor":
            c = httpx.AsyncClient(
                transport=pinned_http.CheckedTransport(pinned_http.control_plane, limits=limits),
                timeout=timeout, follow_redirects=False,
            )
        elif kind == "sidecar":
            c = httpx.AsyncClient(**pinned_http.tool_transport(
                timeout=timeout, limits=limits, follow_redirects=False))
        else:
            c = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)
        _clients[key] = c
    return c


def upstream_url(res: Resolved, query: bytes, session_id: str):
    """The target, built from parts: the credential's origin and declared
    path, never a byte of the request. A sidecar keeps the client's query
    with ``session_id`` overwritten by the token's; a vendor gets none."""
    from urllib.parse import parse_qsl, urlencode

    import httpx
    parts = urlsplit(res.upstream)
    q = b""
    if res.proxy_local:
        params = [(k, v) for k, v in parse_qsl(query.decode("latin-1"), keep_blank_values=True)
                  if k != "session_id"]
        params.append(("session_id", session_id))
        q = urlencode(params).encode()
    kwargs: dict[str, Any] = {"query": q} if q else {}
    return httpx.URL(scheme=parts.scheme, host=parts.hostname or "", port=parts.port,
                     path=res.path, **kwargs)


async def prepare_forward(
    session_id: str, mcp: str, method: str, rest: str, query: bytes, inbound_headers,
    body: bytes = b"",
) -> Prepared | Refusal | Answered:
    """Everything a front door needs to forward one request of ``session_id``
    for ``mcp``: the credential resolved in a thread, the path confined to
    the declared endpoint, a narrowed reach's judgment of ``body``, the
    headers filtered and the credential header added, the client picked. A
    refusal carries the sentence the client reads. A Docker tool's request
    is the door's (``Answered``), once the session's grants and limits
    passed."""
    import asyncio
    cred = credential(session_id, mcp)
    if cred is None:
        return Refusal("no_credential", _REFUSAL_NO_CREDENTIAL)
    if not path_matches(cred.path, rest):
        return Refusal("path_refused", _REFUSAL_PATH)
    content: bytes | None = None
    inbound = list(inbound_headers)
    # An outside session: each call judged by the function its pick granted,
    # before the reach (the context is the session's, registered at its build).
    from core.session.external_identity import is_external_ctx
    from core.session.session_state import get_session_security
    ctx = get_session_security(session_id)
    if is_external_ctx(ctx):
        refusal = await asyncio.to_thread(
            judge_outside, getattr(ctx, "outside_grants", ()), mcp, method, body, inbound)
        if refusal:
            return Refusal("outside_refused", refusal)
    # The agent's tool limits (services/mcp/tool_limits): a limited server's
    # call judged by its function name the same way, every session kind.
    from services.mcp import tool_limits
    limited = tool_limits.limited(getattr(ctx, "tool_limits", ()) or (), mcp)
    if limited is not None:
        refusal = await asyncio.to_thread(
            judge_outside, ((mcp, limited),), mcp, method, body, inbound,
            refused=LIMIT_METHOD_REFUSED)
        if refusal:
            return Refusal("limit_refused", refusal)
    if cred.reach:
        refusal, content = await asyncio.to_thread(judge_reach, cred, mcp, method, body, inbound)
        if refusal:
            return Refusal("reach_refused", refusal)
    res = await asyncio.to_thread(resolve_credential, cred)
    if isinstance(res, Refusal):
        return res
    if cred.kind == CONTAINER:
        # Every Docker tool: its door, after the grants, the limits and the
        # credential (a person whose sign-in cannot be used wakes nothing),
        # with the credential to add, none for a tool with no sign-in.
        from services.containers import door
        signed = ((res.header, res.value),) if res.header else ()
        return await door.step(session_id, mcp, cred, method, rest, query, inbound, body,
                               credential=signed)
    res.path = _reach_path(cred, res.path)
    headers = forward_request_headers(inbound)
    _reach_headers(cred, headers)
    headers[res.header] = res.value
    return Prepared(
        url=upstream_url(res, query, session_id), headers=headers,
        proxy_local=res.proxy_local, client=client(res.proxy_local, res.upstream),
        content=content, token_ref=cred.token_ref, manifest=cred.manifest,
        digest=res.digest, marked=res.marked,
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
        # Off the event loop; the next start of the id waits for it.
        from core import file_commit
        file_commit.behind(remove_descriptor, session_id, name="descriptor_removal", key=session_id)
    except Exception:
        logger.exception("mcp-gateway: purge of %s failed", session_id[:8])
    for fn in list(_purge.hooks):
        try:
            fn(session_id)
        except Exception:
            logger.exception("mcp-gateway: purge hook failed for %s", session_id[:8])
