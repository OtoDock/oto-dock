"""The OAuth bearer allowlist's admin routes: the (provider, host) pairs a
connected account's token may be sent to, the hosts installed MCPs declare
that wait for an admin's Allow, and the removal of the admin rows. The rows live in
``storage/identity/bearer_allowlist.py``, which the gateway reads. The
released restore-defaults path answers a sentence and changes nothing: no
address is predefined, and a released dashboard's button there has no confirm.
Auth: an admin (``require_admin``).
"""

import asyncio
import functools

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_admin, require_user
from services.mcp import mcp_registry

router = APIRouter(dependencies=[Depends(require_user)])


class AddBearerAllowlistRequest(BaseModel):
    provider_id: str
    host_pattern: str


class AllowMcpHostsRequest(BaseModel):
    mcp_name: str
    provider: str
    hosts: list[str]
    digest: str


@router.get("/v1/admin/oauth-bearer-allowlist")
async def list_bearer_allowlist(
    user: UserContext = Depends(get_current_user),
):
    """List approved (provider, host) pairs for OAuth bearer-token injection,
    and the hosts installed MCPs declare that wait for an admin's Allow."""
    require_admin(user)
    from services.community import manifest_hosts
    from storage.identity import bearer_allowlist
    rows = await asyncio.to_thread(bearer_allowlist.list_allowed)
    return {"entries": rows, "pending": await asyncio.to_thread(manifest_hosts.all_pending)}


@router.post("/v1/admin/oauth-bearer-allowlist/allow")
async def allow_mcp_hosts(body: AllowMcpHostsRequest, user: UserContext = Depends(get_current_user)):
    """The admin allows an MCP's hosts as shown (409 if changed): its own rows, taking over an admin row."""
    require_admin(user)
    from core.credentials import catalog_install_registry
    from services.community import manifest_hosts
    async with catalog_install_registry.lock_for(body.mcp_name):
        try:
            await asyncio.to_thread(functools.partial(
                manifest_hosts.confirm, body.mcp_name, provider=body.provider, hosts=body.hosts,
                shown=body.digest, by="api-key" if user.is_api_key else user.sub))
        except LookupError as exc:
            raise HTTPException(404, f"MCP '{body.mcp_name}' not found") from exc
        except manifest_hosts.Refused as exc:
            raise HTTPException(409, str(exc)) from exc
    return {"status": "ok"}


@router.post("/v1/admin/oauth-bearer-allowlist")
async def add_bearer_allowlist(
    body: AddBearerAllowlistRequest,
    user: UserContext = Depends(get_current_user),
):
    """Approve a (provider, host_pattern) pair. Idempotent on conflict."""
    require_admin(user)
    from storage.identity import bearer_allowlist
    if not body.provider_id.strip() or not body.host_pattern.strip():
        raise HTTPException(400, "provider_id and host_pattern required")
    added_by = "api-key" if user.is_api_key else user.sub
    row_id = await asyncio.to_thread(
        bearer_allowlist.add_allowed,
        body.provider_id.strip(), body.host_pattern.strip(), added_by,
    )
    return {"status": "ok", "id": row_id}


@router.delete("/v1/admin/oauth-bearer-allowlist/{row_id}")
async def remove_bearer_allowlist(
    row_id: int,
    user: UserContext = Depends(get_current_user),
):
    """Revoke a (provider, host) pair. The gateway refuses the host at the
    next request (the table's generation moved), and a paired machine
    holding a pushed token for it is told now, not at the lease's end."""
    require_admin(user)
    from core.remote import mcp_gateway_push
    from storage.identity import bearer_allowlist
    row = await asyncio.to_thread(bearer_allowlist.get_allowed, row_id)
    if row is None:
        raise HTTPException(404, "Allowlist entry not found")
    if row["source"] != bearer_allowlist.ADMIN_SOURCE:
        # An installed MCP's row follows its manifest; a gone MCP's goes with its approvals.
        if mcp_registry.get_manifest(row["source"]) is not None:
            raise HTTPException(409, f"This address belongs to the MCP {row['source']}. "
                                     "Turn the MCP off or remove it to take it away.")
        await asyncio.to_thread(bearer_allowlist.forget_mcp, row["source"])
    elif not await asyncio.to_thread(bearer_allowlist.delete_allowed, row_id):
        raise HTTPException(404, "Allowlist entry not found")
    mcp_gateway_push.tick_soon()
    return {"status": "ok"}


@router.post("/v1/admin/oauth-bearer-allowlist/admin-rows/remove")
async def remove_admin_bearer_rows(user: UserContext = Depends(get_current_user)):
    """Remove the addresses an admin added: every admin row goes, the
    MCPs' rows stay, and paired machines are told now, as at a revoke."""
    require_admin(user)
    from core.remote import mcp_gateway_push
    from storage.identity import bearer_allowlist
    rows = await asyncio.to_thread(bearer_allowlist.remove_admin_rows)
    mcp_gateway_push.tick_soon()
    return {"entries": rows}


@router.post("/v1/admin/oauth-bearer-allowlist/restore-defaults")
async def restore_bearer_allowlist_defaults(user: UserContext = Depends(get_current_user)):
    """The released dashboard's one-click button: no address is
    predefined any more, so it changes nothing and says so."""
    require_admin(user)
    raise HTTPException(409, "No address is predefined any more. Reload the page to see the current list.")
