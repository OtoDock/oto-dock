"""``GET /v1/mcps/{name}/icon.png``: an MCP's icon for the dashboard.

The installed folder's ``icon.png`` first (any category), then, for the
Browse dialog's audience (creators and admins), the community catalog's
icon through :mod:`services.community.community_icons`. Cookie sessions
only: the ``.png`` route is what an ``<img>`` in the dashboard loads.
"""

from __future__ import annotations

import asyncio
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from auth.providers import UserContext, get_current_user, require_auth
from services.community import community_icons
from auth import roles

router = APIRouter()

# An icon costs one request per hour per browser. A miss is cached for five
# minutes only: the URL never changes, and the MCP may get installed (or its
# folder converged, icon included) in the meantime.
_CACHE_HEADERS = {"Cache-Control": "private, max-age=3600"}
_MISS_HEADERS = {"Cache-Control": "private, max-age=300"}


@router.get("/v1/mcps/{name}/icon.png")
async def mcp_icon(
    name: str,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> Response:
    user = require_auth(user)
    if user.is_api_key:
        raise HTTPException(403, "Dashboard sessions only")
    # Every surface that shows an MCP row asks for a creator, an admin or an
    # agent's manager (api/mcp/mcps.py); anyone else would only learn which
    # MCPs are installed from the answer.
    if not roles.is_creator_or_above(user.role) and roles.MANAGER not in user.agent_roles.values():
        raise HTTPException(403, "MCP managers only")
    if not community_icons.valid_name(name):
        raise HTTPException(404, "No such MCP", headers=_MISS_HEADERS)

    path = await asyncio.to_thread(community_icons.installed_icon_path, name)
    if path is not None:
        return FileResponse(path, media_type="image/png", headers=_CACHE_HEADERS)

    body = None
    if roles.is_creator_or_above(user.role):
        body = await community_icons.catalog_icon(name)
    if body is None:
        raise HTTPException(404, "No icon", headers=_MISS_HEADERS)

    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={**_CACHE_HEADERS, "ETag": etag})
    return Response(content=body, media_type="image/png", headers={**_CACHE_HEADERS, "ETag": etag})
