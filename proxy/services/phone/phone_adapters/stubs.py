"""Placeholder adapters for providers whose automation ships later.

Selecting one of these in the dashboard is graceful — every method degrades
cleanly instead of crashing — so the UI can offer the full provider list now
while the real implementations land incrementally (3CX = future; Asterisk,
FreePBX and Twilio are real). A stub server can never reach ``verified``
(verify returns ``failed``), so the route cascade never actually calls
``provision_route`` on one; it raises defensively anyway.
"""

from __future__ import annotations

from typing import ClassVar

from .base import (
    BootstrapResult,
    HealthStatus,
    PhoneAdapterError,
    PhoneServerAdapter,
    RouteHandle,
)


class _StubAdapter(PhoneServerAdapter):
    label: ClassVar[str] = "This provider"

    def _not_yet(self) -> str:
        return f"{self.label} automation is available in a later release."

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=False, detail=self._not_yet())

    async def get_bootstrap_snippet(self) -> str | None:
        return None

    async def verify_bootstrap(self) -> BootstrapResult:
        return BootstrapResult(status="failed", detail=self._not_yet())

    async def provision_route(self, route: dict) -> RouteHandle:
        raise PhoneAdapterError(self._not_yet(), status_code=400)

    async def deprovision_route(self, route: dict) -> None:
        # Deleting a never-provisioned route must not explode.
        return None


class ThreeCxStubAdapter(_StubAdapter):
    adapter_type = "three_cx"
    label = "3CX"
