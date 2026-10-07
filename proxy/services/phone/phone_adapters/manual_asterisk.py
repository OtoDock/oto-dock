"""Manual Asterisk adapter — the no-automation reference adapter.

For a self-hosted Asterisk (or any PBX) where OtoDock does NOT drive a config
API: the admin installs the one-time dialplan bridge by hand, confirms it in the
dashboard, and OtoDock hands them the exact ``database put`` command for each
inbound route's DID→UUID mapping. No GraphQL, no AMI, no SFTP — zero live-PBX
calls — so it works against any Asterisk and needs no extra credentials.

It doubles as the engine's reference adapter: it exercises the full
provision / deprovision / bootstrap lifecycle without any provider dependency.
"""

from __future__ import annotations

from adapters.phone import is_outbound

from .base import (
    BootstrapResult,
    HealthStatus,
    PhoneServerAdapter,
    RouteHandle,
)


class ManualAsteriskAdapter(PhoneServerAdapter):
    """Snippet + admin-confirmed verify + per-route AstDB instructions."""

    adapter_type = "asterisk_manual"
    requires_bootstrap = True
    supports_sftp_bootstrap = False
    ami_snippet_file = "manager.conf"  # plain Asterisk: edit manager.conf directly

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            healthy=True,
            detail="Manual adapter — connection is not actively monitored.",
        )

    async def get_bootstrap_snippet(self) -> str:
        return self._render_bootstrap_snippet()

    async def verify_bootstrap(self) -> BootstrapResult:
        # Manual adapter can't introspect the PBX — "verify" is the admin's
        # explicit confirmation that they installed the bridge context.
        return BootstrapResult(
            status="verified",
            detail="Marked verified (manual adapter — bootstrap not introspected).",
        )

    async def provision_route(self, route: dict) -> RouteHandle:
        if is_outbound(route.get("direction")):
            return self._provision_outbound(route)
        return self._provision_inbound(route)

    async def deprovision_route(self, route: dict) -> None:
        # Nothing to undo on our side. A leftover AstDB entry is harmless; the
        # admin may remove it with ``database del otodock/route_uuid/<did>``.
        return None

    # -- helpers (sync; no I/O) --------------------------------------------
    def _provision_inbound(self, route: dict) -> RouteHandle:
        did = (route.get("did") or "").strip()
        uuid = route.get("audiosocket_uuid") or ""
        astdb_key = f"otodock/route_uuid/{did}" if did else ""
        if did:
            instructions = (
                "On the Asterisk server, map this DID to the call:\n"
                f'    asterisk -rx "database put {astdb_key} {uuid}"\n'
                f"Then route {did} into [oto-audiosocket-bridge] with EXTEN={did} "
                f"(e.g. a Custom Destination / Goto oto-audiosocket-bridge,{did},1)."
            )
        else:
            instructions = (
                "Set a DID on this inbound route, then map it on the PBX with "
                "`database put otodock/route_uuid/<did> <uuid>`."
            )
        return RouteHandle(
            adapter_data={"mode": "manual", "astdb_key": astdb_key},
            audiosocket_uuid=uuid or None,
            did=did or None,
            instructions=instructions,
        )

    def _provision_outbound(self, route: dict) -> RouteHandle:
        return RouteHandle(
            adapter_data={"mode": "manual"},
            instructions=(
                "Outbound calls are placed via AMI Originate using this server's "
                "AMI credentials and the route's dialplan context — no inbound "
                "provisioning is needed. Ensure the outbound context/trunk exists "
                "on the PBX."
            ),
        )
