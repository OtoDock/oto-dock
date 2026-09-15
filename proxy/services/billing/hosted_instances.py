"""'Hosted by OtoDock' system MCP instance lifecycle.

Creates/removes the platform-managed ("Hosted by OtoDock") instance for every
``api_key_relay`` MCP to match ``relay_client.system_relay_active()`` (relay usable
AND the master toggle on AND connected). Runs at startup AND on connect/disconnect
— the toggle is a runtime change, so a startup-only pass would strand stale
instances.
"""

from __future__ import annotations

import logging

from services.billing import relay_client
from services.mcp import mcp_registry
from storage.mcp import mcp_store

logger = logging.getLogger("claude-proxy.hosted-instances")


def _relay_mcp_names() -> set[str]:
    """MCPs that currently offer the api_key_relay hosted block."""
    return {
        name
        for name, m in mcp_registry.get_all_manifests().items()
        if m.hosted and m.instances and m.hosted.api_key_relay
        and m.hosted.api_key_relay.available
    }


def reconcile_otodock_system_instances() -> None:
    """Bring the 'Hosted by OtoDock' system instances in line with
    ``relay_client.system_relay_active()``. Idempotent; callers wrap it in
    try/except so it never aborts boot or a request handler.

    * **active** (relay usable + toggle on + connected) → create the system
      instance for every relay MCP (respecting an admin rename via
      ``managed_by='system'`` and a ``_managed_instance_deleted`` tombstone), and
      drop any whose MCP no longer offers relay (manifest-stale).
    * **inactive** (toggle off / not connected / air-gapped / offline) → drop ALL
      system instances (plain delete, no tombstone, so a later connect recreates
      them).
    """
    relay_mcps = _relay_mcp_names()
    active = relay_client.system_relay_active()
    if active:
        for name in relay_mcps:
            if mcp_store.get_mcp_config_value(name, "_managed_instance_deleted") == "true":
                continue  # admin explicitly deleted it
            if mcp_store.get_system_instance(name):
                continue  # already exists (respects an admin rename)
            mcp_store.upsert_mcp_instance(name, {
                "instance_name": "Hosted by OtoDock",
                "field_values": {},
                "agents": [],
                "assigned_to_all": True,
                "hosted_mode": "hosted",
                "managed_by": "system",
            })
            logger.info("Created platform-managed hosted instance for %s", name)
    # Removal pass: active → keep current relay MCPs (drop manifest-stale);
    # inactive → keep nothing (drop ALL system instances).
    keep = relay_mcps if active else set()
    for removed in mcp_store.reconcile_system_instances(keep):
        logger.info("Removed platform-managed hosted instance for %s", removed)
