"""FreePBX / Asterisk adapter — AMI-only control plane.

Provisions inbound call routes by writing the ``otodock/route_uuid/<number>``
AstDB tree over AMI (``DBPut``/``DBGet``/``DBDel``) with a least-privilege
``system``-class AMI user. No SSH, no GraphQL, no CLI: AstDB is dynamic, so a
route is a single ``DBPut`` with no ``fwconsole reload``. The one-time wiring —
install the ``[oto-audiosocket-bridge]`` dialplan, point each number at it via a
Custom Destination, create the AMI user — is admin setup surfaced in the
bootstrap UI, not a runtime op.

Why a Custom Destination per number: FreePBX rewrites ``${EXTEN}`` to ``s`` on
both the internal Misc-App and the external Inbound-Route paths (the dialed
number survives only in ``${FROM_DID}``, and only for external calls). Routing
each number through a Custom Destination whose target is
``oto-audiosocket-bridge,<number>,1`` makes ``${EXTEN}=<number>`` uniformly, so
the bridge's ``DB(otodock/route_uuid/${EXTEN})`` lookup is correct on both paths.

AMI coordinates come from the server row: ``config.ami_host`` (falls back to the
row ``host``), ``config.ami_port`` (default 5038), ``config.ami_username``, and
the secret from ``infra_credentials`` (``phone-server-{id}-ami-secret`` → inner
key ``AMI_SECRET``). Outbound media (Originate) is the phone daemon's concern and
is NOT provisioned here.
"""

from __future__ import annotations

import logging
import uuid

from adapters.phone import is_outbound

from .ami import AMIClient
from .base import (
    BootstrapResult,
    HealthStatus,
    PhoneAdapterError,
    PhoneServerAdapter,
    RouteHandle,
)

logger = logging.getLogger("claude-proxy")

# The AstDB tree OtoDock owns. Asterisk's DB() splits on the FIRST '/', so
# DB(otodock/route_uuid/<n>) ⇒ Family="otodock", Key="route_uuid/<n>" — i.e. the
# slash lives *inside* the AMI Key field.
_ASTDB_FAMILY = "otodock"
_ROUTE_KEY_PREFIX = "route_uuid"
_VERIFY_KEY_PREFIX = "_verify"
_DEFAULT_AMI_PORT = 5038


class AsteriskFreePBXAdapter(PhoneServerAdapter):
    """FreePBX/Asterisk over AMI: ``DBPut`` a DID→UUID map that the bridge
    dialplan reads at call time. Bootstrap is admin-installed (the bridge +
    per-number routing); ``verify`` is a real AMI DB round-trip."""

    adapter_type = "asterisk_freepbx"
    requires_bootstrap = True
    supports_sftp_bootstrap = False  # AMI-only: no SSH/SFTP push of the snippet
    # FreePBX owns manager.conf (regenerates it on Apply Config) — custom AMI
    # users must live in manager_custom.conf to survive.
    ami_snippet_file = "manager_custom.conf"

    # -- AMI wiring ---------------------------------------------------------
    def _ami_params(self) -> tuple[str, int, str, str]:
        """(host, port, user, secret) from the server row + credentials.
        Raises 400 if the AMI coordinates aren't configured yet."""
        host = (self.config.get("ami_host") or self.server.get("host") or "").strip()
        port = int(self.config.get("ami_port") or _DEFAULT_AMI_PORT)
        user = (self.config.get("ami_username") or "").strip()
        secret = self._resolve_credentials("ami-secret").get("AMI_SECRET", "")
        if not host or not user or not secret:
            raise PhoneAdapterError(
                "Configure the AMI host, username and secret for this phone "
                "server before provisioning.",
                status_code=400,
            )
        return host, port, user, secret

    def _client(self) -> AMIClient:
        host, port, user, secret = self._ami_params()
        return AMIClient(host=host, port=port, username=user, secret=secret)

    # -- Health -------------------------------------------------------------
    async def health_check(self) -> HealthStatus:
        try:
            async with self._client():
                pass  # connect + login IS the probe
        except PhoneAdapterError as e:
            return HealthStatus(healthy=False, detail=e.message)
        return HealthStatus(healthy=True, detail="AMI reachable.")

    # -- Bootstrap ----------------------------------------------------------
    async def get_bootstrap_snippet(self) -> str:
        return self._render_bootstrap_snippet()

    async def verify_bootstrap(self) -> BootstrapResult:
        """Round-trip a temp key (``DBPut`` → ``DBGet`` → ``DBDel``) to prove AMI
        reachability + auth + AstDB read/write — the load-bearing automated check.

        It deliberately does NOT introspect the dialplan: confirming the bridge
        context + per-number routing would need the ``command`` privilege we don't
        request. Those are admin-asserted and confirmed by a live test call.
        """
        probe_key = f"{_VERIFY_KEY_PREFIX}/{uuid.uuid4().hex}"
        token = uuid.uuid4().hex
        try:
            async with self._client() as ami:
                await ami.db_put(_ASTDB_FAMILY, probe_key, token)
                got = await ami.db_get(_ASTDB_FAMILY, probe_key)
                await ami.db_del(_ASTDB_FAMILY, probe_key)
        except PhoneAdapterError as e:
            return BootstrapResult(status="failed", detail=e.message)
        if got != token:
            return BootstrapResult(
                status="failed",
                detail=f"AMI DB round-trip mismatch (wrote {token!r}, read {got!r}).",
            )
        return BootstrapResult(
            status="verified",
            detail="AMI reachable and AstDB writable. Make sure the bridge "
                   "dialplan is installed and each number is routed to it.",
        )

    # -- Route provisioning -------------------------------------------------
    async def provision_route(self, route: dict) -> RouteHandle:
        if is_outbound(route.get("direction")):
            return self._outbound_handle()
        return await self._provision_inbound(route)

    async def deprovision_route(self, route: dict) -> None:
        if is_outbound(route.get("direction")):
            return
        number = self._route_number(route)
        if not number:
            return  # nothing was ever mapped
        async with self._client() as ami:
            await ami.db_del(_ASTDB_FAMILY, f"{_ROUTE_KEY_PREFIX}/{number}")

    async def _provision_inbound(self, route: dict) -> RouteHandle:
        number = self._route_number(route)
        if not number:
            raise PhoneAdapterError(
                "An inbound FreePBX route needs a DID/number — it is the AstDB "
                "key the bridge looks up.",
                status_code=400,
            )
        audiosocket_uuid = route.get("audiosocket_uuid") or ""
        if not audiosocket_uuid:
            raise PhoneAdapterError(
                "Inbound route is missing its AudioSocket UUID.", status_code=400,
            )
        key = f"{_ROUTE_KEY_PREFIX}/{number}"
        async with self._client() as ami:
            await ami.db_put(_ASTDB_FAMILY, key, audiosocket_uuid)
        return RouteHandle(
            adapter_data={"mode": "ami", "astdb_key": f"{_ASTDB_FAMILY}/{key}"},
            audiosocket_uuid=audiosocket_uuid,
            did=number,
            instructions=(
                f"{number} is mapped to this AI agent. One-time routing on "
                f"FreePBX so calls reach the bridge: create a Custom Destination "
                f"targeting 'oto-audiosocket-bridge,{number},1', then send {number} "
                f"to it — via its Inbound Route (an external phone number / DID) "
                f"or a Misc Application (an internal extension) → Apply Config."
            ),
        )

    def _outbound_handle(self) -> RouteHandle:
        return RouteHandle(
            adapter_data={"mode": "ami"},
            instructions=(
                "Outbound calls are placed by the phone server via AMI Originate "
                "using this server's AMI credentials and the route's dialplan "
                "context — no inbound provisioning needed."
            ),
        )

    @staticmethod
    def _route_number(route: dict) -> str:
        number = (route.get("did") or "").strip()
        # AMI is line-oriented — never let a stray CR/LF slip into a Key.
        if any(c in number for c in "\r\n"):
            raise PhoneAdapterError("DID contains invalid characters.", status_code=400)
        return number

    # list_provisioned_routes() inherits the base ``None`` — AMI can't enumerate
    # the AstDB tree without the ``command`` privilege we deliberately don't
    # request, so drift stays untracked (honest) for this least-privilege user.
