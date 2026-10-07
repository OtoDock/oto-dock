"""Admin API — LLM chat-title generation settings.

GET returns the enable flag, the pinned model (''=Auto), whether the feature is
currently active (a Direct-LLM provider resolves), the effective provider/model,
and the dropdown options. PUT sets the enable toggle and/or the pinned model.
Provider + credentials come from the Direct-LLM execution layer (no separate API
key) — see services/title_generator.py.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_admin, require_user
from services import title_generator
from storage import database as task_store

logger = logging.getLogger("claude-proxy")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])


@router.get("/v1/admin/title-generation")
async def get_title_generation(user: UserContext | None = Depends(get_current_user)):
    """Title-generation status for the admin Setup card (read-only — never mints)."""
    require_admin(user)
    return await asyncio.to_thread(title_generator.title_generation_status)


class TitleGenerationUpdate(BaseModel):
    enabled: bool | None = None
    model: str | None = None   # a Direct-LLM model id, or "" for Auto


@router.put("/v1/admin/title-generation")
async def put_title_generation(
    req: TitleGenerationUpdate,
    user: UserContext | None = Depends(get_current_user),
):
    """Set the enable toggle and/or the pinned title model, then return the
    refreshed status."""
    require_admin(user)
    if req.enabled is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "title_generation_enabled", "1" if req.enabled else "0",
        )
    if req.model is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "title_generation_model", req.model.strip(),
        )
    return await asyncio.to_thread(title_generator.title_generation_status)
