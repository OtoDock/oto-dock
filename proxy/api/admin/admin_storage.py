"""Admin storage & retention endpoints (Setup → Storage & Retention card).

The settings themselves (session_retention_* and offboarded_retention_*)
ride the shared platform-settings GET/PUT in api/auth/platform.py; this
module hosts the action + readout endpoints backed by
services/infra/retention.py, and the list and purge of the archive of
removed people.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import get_current_user, mask_email, require_admin, UserContext, require_user
from services.infra import retention

# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])
logger = logging.getLogger("claude-proxy")


class RetentionRunRequest(BaseModel):
    dry_run: bool = False


@router.post("/v1/admin/retention/run-now")
async def admin_retention_run_now(
    req: RetentionRunRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Run the full retention sweep immediately (all passes; the aged-chats
    pass still honors the enabled toggle). dry_run reports what WOULD be
    deleted without touching anything."""
    require_admin(user)
    return await retention.run_sweep(dry_run=req.dry_run)


@router.get("/v1/admin/storage/usage")
async def admin_storage_usage(
    user: UserContext | None = Depends(get_current_user),
):
    """Disk-usage breakdown for the admin card (agents tree, session files,
    codex junk, recover-bin, proxy sessions dir, logs) + retention status."""
    require_admin(user)
    return await asyncio.to_thread(retention.compute_storage_usage)


@router.get("/v1/admin/offboarded")
async def admin_list_offboarded(
    user: UserContext | None = Depends(get_current_user),
):
    """The archive of removed people: each archive's agent folders, size,
    dates and when the retention sweep deletes it. Admin, in person."""
    require_admin(user)
    try:
        return await asyncio.to_thread(retention.list_offboarded_archives)
    except OSError:
        logger.warning("The archive of removed people could not be listed", exc_info=True)
        raise HTTPException(500, "The archive folder could not be read safely") from None


@router.delete("/v1/admin/offboarded/{username}")
async def admin_purge_offboarded(
    username: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete one removed person's archive now. Admin, in person: an agent
    session or API key never reaches it (``require_admin``)."""
    u = require_admin(user)
    try:
        result = await asyncio.to_thread(retention.purge_offboarded_archive, username)
    except ValueError:
        raise HTTPException(400, "Not a username") from None
    except FileNotFoundError:
        raise HTTPException(404, "No archive for this username") from None
    except retention.ArchiveBusy:
        raise HTTPException(409, "This archive is still being written; try again in a few minutes") from None
    except OSError:
        logger.warning("The archive of %s was not deleted", username, exc_info=True)
        raise HTTPException(500, "The archive could not be deleted; the server log names why") from None
    logger.info("Admin %s purged the archive of %s (%d bytes)",
                mask_email(u.email), username, result["bytes"])
    return result
