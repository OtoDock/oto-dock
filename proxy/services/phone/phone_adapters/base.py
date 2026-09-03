"""Phone-server adapters — the control-plane abstraction over telephony providers.

An adapter is how the proxy *provisions* a phone provider: create/remove the
inbound and outbound routes, run the one-time bootstrap handshake, and report
health + drift. It is deliberately a **control-plane** surface only — carrying
the live call audio is the *media plane* (Asterisk AudioSocket handled by the
``phone/`` daemon today; Twilio Media-Streams later) and is NOT an adapter
concern.

Adapters run **proxy-side**: the proxy owns the DB, the encrypted credentials,
the admin API and the background workers, and it can reach a provider's
management interface (Asterisk/FreePBX AMI, Twilio REST) directly. This
also keeps cloud providers (Twilio/3CX) — which never touch the ``phone/`` media
daemon — in the same place as the self-hosted ones.

Contract for new adapters (see ``manual_asterisk.py`` for the reference impl):
  * Resolve secrets only through the injected ``credential_resolver`` — never
    read the DB/env directly, and never log them (``__repr__`` redacts).
  * ``media_endpoint`` (where the provider should send the call audio) is
    injected at construction; the adapter bakes it into its provisioning.
  * Raise ``PhoneAdapterError`` (with an HTTP ``status_code`` + optional vendor
    envelope) on any provider-side failure so the API turns it into a clean
    502/504/400 instead of a 500.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, ClassVar, Literal

# The one-time AudioSocket dialplan both Asterisk adapters install (one paste,
# both call directions). Three placeholders are substituted per server:
# ``__MEDIA_ENDPOINT__`` → the reachable AudioSocket ``host:port``;
# ``__REGISTER_ENDPOINT__`` → the phone daemon's ``host:port/v1/calls/register``;
# ``__REGISTER_SECRET__`` → this server's own register secret. Each context is
# installed ONCE and serves unlimited routes:
#   * inbound ``[oto-audiosocket-bridge]`` — ``_.`` matches any dialed number;
#     each number reaches it via a per-number Custom Destination so ``${EXTEN}``
#     is the number, looked up in the ``otodock/route_uuid/*`` AstDB tree. After
#     the route is resolved it synchronously POSTs caller metadata to
#     ``/v1/calls/register`` (Bearer the per-server secret) so ``${trigger.*}``
#     enrichment is cached before AudioSocket connects; ``-m 1`` caps the curl so
#     an unreachable daemon never stalls the call (it just proceeds unenriched).
#   * outbound ``[oto-audiosocket-outbound]`` — runs at ``s`` with the per-call
#     ``${OUTBOUND_UUID}`` the phone daemon passes at Originate (no AstDB, no
#     register POST — outbound metadata is attached in-process by the daemon).
AUDIOSOCKET_DIALPLAN_TEMPLATE = """[oto-audiosocket-bridge]
exten => _.,1,Answer()
 same => n,Set(AS_UUID=${DB(otodock/route_uuid/${EXTEN})})
 same => n,GotoIf($["${AS_UUID}" = ""]?dead)
 same => n,System(curl -s -m 1 -X POST -H 'Content-Type: application/json' -H 'Authorization: Bearer __REGISTER_SECRET__' --data-binary '{"audiosocket_uuid":"${AS_UUID}","phone":"${FILTER(+*#0-9A-Za-z,${CALLERID(num)})}","did":"${FILTER(+*#0-9A-Za-z,${EXTEN})}","source":"phone-freepbx","dial_event":{"channel":"${FILTER(/._!:A-Za-z0-9-,${CHANNEL})}","uniqueid":"${FILTER(.0-9,${UNIQUEID})}"}}' http://__REGISTER_ENDPOINT__)
 same => n,AudioSocket(${AS_UUID},__MEDIA_ENDPOINT__)
 same => n,Hangup()
 same => n(dead),Verbose(1,No OtoDock route for ${EXTEN})
 same => n,Hangup()

[oto-audiosocket-outbound]
exten => s,1,Answer()
 same => n,AudioSocket(${OUTBOUND_UUID},__MEDIA_ENDPOINT__)
 same => n,Hangup()
"""

# The dedicated AMI manager user the platform generates per server (username +
# secret are minted server-side and auto-wired into the server's credentials,
# so the admin only pastes — no typing back). IP-locked to the proxy's
# PBX-facing address (same host the dialplan dials for media/register).
# ``call,originate`` ride along by default so flipping a server to outbound
# later doesn't need a PBX revisit; inbound-only operators can trim them.
# ``dtmf`` read is REQUIRED for the daemon's DTMF event listener (PIN entry
# on RFC2833/SIP-INFO trunks): AMI delivers read-perms ∩ EventMask, so a
# user without the class silently yields zero digit events while login and
# ping look healthy. Servers provisioned before this class existed must
# re-paste the block (or add ``dtmf`` to read=) + ``manager reload``.
AMI_MANAGER_USER_TEMPLATE = """[__AMI_USER__]
secret = __AMI_SECRET__
deny = 0.0.0.0/0.0.0.0
permit = __PROXY_HOST__/255.255.255.255
read = system,call,originate,dtmf
write = system,call,originate
; remove call,originate if this server never places outbound calls
"""


@dataclass(frozen=True)
class HealthStatus:
    """Result of an adapter health probe. Persisted to ``phone_servers`` health
    columns and surfaced as the pill's health badge."""

    healthy: bool
    detail: str = ""
    server_version: str | None = None       # e.g. "Asterisk 20.5.0"
    bootstrap_intact: bool | None = None     # is the one-time bridge still in place?


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of get-snippet / verify / apply. ``status`` maps onto the
    ``phone_servers.bootstrap_status`` enum (minus ``pending``/``drift``, which
    the engine owns)."""

    status: Literal["verified", "snippet_provided", "failed"]
    snippet: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class RouteHandle:
    """What an adapter returns after provisioning a route.

    ``adapter_data`` is the opaque per-adapter blob persisted to
    ``phone_routes.adapter_data`` (provider-side ids, AstDB keys, …) and handed
    back on deprovision. ``audiosocket_uuid``/``did`` identify the route for
    drift reconciliation. ``instructions`` is human follow-up for
    no-automation adapters (e.g. the ``database put`` command for manual)."""

    adapter_data: dict = field(default_factory=dict)
    audiosocket_uuid: str | None = None
    did: str | None = None
    instructions: str = ""


class PhoneAdapterError(Exception):
    """A provider-side provisioning/bootstrap failure.

    Carries the HTTP status the API should surface (``502`` upstream vendor
    error by default, ``400`` for caller/config problems, ``504`` for a
    timeout) plus an optional vendor envelope echoed in the response body."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        vendor_status: int | None = None,
        vendor_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.vendor_status = vendor_status
        self.vendor_body = vendor_body


class PhoneServerAdapter(ABC):
    """Control-plane adapter for one phone provider.

    One instance wraps one ``phone_servers`` row. Construction is cheap (no
    network I/O); the async methods do the provider calls. Subclasses set the
    three ``ClassVar``s and implement the abstract methods."""

    adapter_type: ClassVar[str] = ""
    # Providers with a one-time manual/API bootstrap (Asterisk). Set False for
    # cloud providers that are usable the moment credentials are valid (Twilio).
    requires_bootstrap: ClassVar[bool] = True
    # Can ``apply_bootstrap`` push the snippet over SSH/SFTP? Default off; no
    # adapter ships SSH push today (FreePBX installs the bridge context by hand).
    supports_sftp_bootstrap: ClassVar[bool] = False
    # Where the generated AMI manager-user snippet belongs on the PBX, or None
    # for adapters that don't speak AMI (cloud stubs). FreePBX regenerates
    # ``manager.conf`` itself, so its custom users go in ``manager_custom.conf``;
    # plain Asterisk edits ``manager.conf`` directly.
    ami_snippet_file: ClassVar[str | None] = None

    def __init__(
        self,
        server_row: dict,
        *,
        credential_resolver: Callable[[str], dict[str, str]],
        media_endpoint: str,
        register_endpoint: str,
    ) -> None:
        self.server = server_row
        self.server_id = server_row["id"]
        self.config = server_row.get("config") or {}
        self.media_endpoint = media_endpoint
        # Where the inbound dialplan POSTs caller metadata before AudioSocket —
        # the phone daemon's ``host:port/v1/calls/register`` (no scheme).
        self.register_endpoint = register_endpoint
        # ``credential_resolver(suffix)`` → the decrypted infra-credential dict
        # stored under ``phone-server-{id}-{suffix}`` (e.g. "ami-secret").
        self._resolve_credentials = credential_resolver

    def __repr__(self) -> str:  # never leak resolved secrets
        return (
            f"<{type(self).__name__} server_id={self.server_id} "
            f"host={self.server.get('host', '')!r}>"
        )

    def render_ami_user_snippet(self, username: str, secret: str) -> str:
        """Fill the AMI manager-user template for this server. The permit host
        is the proxy's PBX-facing address — the same host the rendered dialplan
        dials for the register endpoint, which is also the source address the
        PBX sees on the AMI connection (containerised deployments masquerade
        behind the Docker host)."""
        proxy_host = self.register_endpoint.split("/", 1)[0].rsplit(":", 1)[0]
        return (
            AMI_MANAGER_USER_TEMPLATE
            .replace("__AMI_USER__", username)
            .replace("__AMI_SECRET__", secret)
            .replace("__PROXY_HOST__", proxy_host)
        )

    def _render_bootstrap_snippet(self) -> str:
        """Fill the shared AudioSocket dialplan template for this server: media
        endpoint, the inbound register endpoint, and this server's own register
        secret (via the injected resolver — empty until minted, which the
        bootstrap API does on the render path). Both Asterisk adapters use this
        so the one-paste snippet is byte-identical."""
        secret = self._resolve_credentials("register-secret").get(
            "REGISTER_SECRET", "",
        )
        return (
            AUDIOSOCKET_DIALPLAN_TEMPLATE
            .replace("__MEDIA_ENDPOINT__", self.media_endpoint)
            .replace("__REGISTER_ENDPOINT__", self.register_endpoint)
            .replace("__REGISTER_SECRET__", secret)
        )

    # -- Health -------------------------------------------------------------
    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Probe reachability + auth (and, where cheap, bootstrap integrity)."""

    # -- Bootstrap (one-time per server) -----------------------------------
    @abstractmethod
    async def get_bootstrap_snippet(self) -> str | None:
        """The dialplan/config the admin installs once, or None if not needed."""

    @abstractmethod
    async def verify_bootstrap(self) -> BootstrapResult:
        """Confirm the one-time bootstrap is in place (or trust the admin)."""

    async def apply_bootstrap(self, sftp_creds: dict) -> BootstrapResult:
        """Install the bootstrap over SSH/SFTP. Default: unsupported."""
        raise PhoneAdapterError(
            "This adapter does not support automatic SSH bootstrap; "
            "install the snippet manually and click Verify.",
            status_code=400,
        )

    # -- Route provisioning -------------------------------------------------
    @abstractmethod
    async def provision_route(self, route: dict) -> RouteHandle:
        """Make the provider deliver/accept ``route``'s calls to our media
        endpoint. Dispatches on ``route['direction']`` internally."""

    @abstractmethod
    async def deprovision_route(self, route: dict) -> None:
        """Undo ``provision_route`` for ``route`` (best-effort on delete)."""

    # -- Drift --------------------------------------------------------------
    async def list_provisioned_routes(self) -> list[RouteHandle] | None:
        """Routes the provider currently has, for drift reconciliation.
        Return None when the provider can't be enumerated (drift untracked)."""
        return None
