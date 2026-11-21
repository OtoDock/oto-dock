"""A Docker tool's container on the admin's MCP page: the words its row
reads beside Docker's (asleep until its next use, or its last wake failed)
and the admin's "Keep running".
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user, require_admin
from services.containers import door, idle, lifecycle, state
from services.mcp import docker_manager, mcp_manifest_types as _mt, mcp_registry
from storage.mcp import mcp_store

router = APIRouter()


def status_word(manifest, status: str) -> str:
    """The row's word for an enabled tool: a container the platform drives
    that does not run sleeps until its next use, or did not start when its
    last wake failed; one that wakes now is starting."""
    if not lifecycle.managed(manifest) or status not in (docker_manager.STOPPED, docker_manager.NOT_FOUND):
        return status
    st = state.of(manifest.name)
    if st.waking:
        return docker_manager.STARTING
    return docker_manager.DID_NOT_START if st.failed else docker_manager.SLEEPING


def row_facts(manifest, keep_running: set[str]) -> dict:
    """What the row shows beside the word: whether the tool always runs (one
    of the platform's own that runs with it), the admin's switch (never on a
    hosted install; ``keep_running`` is the stored set, read once for the
    page), and one plain cause of its last failed start."""
    held = state.of(manifest.name).held_since
    left = (idle.hold_bound_s(manifest) - (time.monotonic() - held)) if held else 0
    return {
        "docker_always_on": lifecycle.runs_with_platform(manifest),
        "docker_keep_running": lifecycle.kept_running(manifest.name, keep_running),
        "docker_wake_error": state.plain_cause(manifest.name),
        # It says it is working, so the sweep keeps it awake this much longer.
        "docker_working_minutes": int(left // 60) + 1 if held and left > 0 else 0,
    }


class KeepRunning(BaseModel):
    keep: bool


@router.put("/v1/admin/mcps/{name}/docker/keep-running")
async def keep_running(name: str, req: KeepRunning, user: UserContext = Depends(get_current_user)):
    """The admin's "Keep running": the tool's container never sleeps, and a
    sleeping one starts now."""
    require_admin(user)
    if config.OTODOCK_CLOUD:
        raise HTTPException(400, "Keep running is unavailable on hosted OtoDock.")
    manifest = mcp_registry.get_manifest(name)
    if not manifest or not _mt.is_container(manifest.server):
        raise HTTPException(400, "Not a Docker MCP")
    if not lifecycle.managed(manifest):
        raise HTTPException(409, "This tool runs with the platform: it is always running.")
    await asyncio.to_thread(mcp_store.set_keep_running, name, req.keep)
    if req.keep and state.of(name).asleep:
        state_row = await asyncio.to_thread(mcp_store.get_mcp_state, name) or {}
        if state_row.get("enabled"):
            door.wake_in_background(manifest)
    return {"name": name, "keep_running": req.keep}
