"""The lifecycle of a folder app (APPS.md "Lifecycle"): stopping the server
wherever a row leaves the viewer surfaces, and the purge that removes the
app with everything it owns.

Purge order: stop the server → delete the row (the state document, the
per-user hides and the shares cascade) → forget the live audience → remove
the releases → remove the database → remove the folder from the workspace
with a tombstone and a delete push per file, so no satellite resurrects
it. The folder is captured in the recover bin like any files-API delete;
the database is not (the dialog says so). A partial failure leaves at
most a folder without a row, which a later pin registers over.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import config
from services.apps import app_supervisor, releases
from storage import database as task_store
from storage.pg import run_db

if TYPE_CHECKING:
    from services.agents.offboarding import OffboardEvent

logger = logging.getLogger("claude-proxy.apps")


async def stop_rows(rows: list[dict]) -> int:
    """Stop the live and preview servers of every row given (a delete site
    calls this BEFORE the rows or their directories go)."""
    count = 0
    for row in rows:
        try:
            count += await app_supervisor.stop(row["id"])
        except Exception:
            logger.exception("App %s: stop failed", row.get("slug"))
    return count


async def forget_rows(rows: list[dict]) -> None:
    """A delete site: the handler schedules and the app triggers of every
    row given go with it (APPS.md "Handlers")."""
    from services.apps import app_handlers
    for row in rows:
        try:
            await app_handlers.remove_rows(row["id"])
        except Exception:
            logger.exception("App %s: handler rows not removed", row.get("slug"))


async def stop_agent_apps(agent: str) -> int:
    rows = await run_db(task_store.list_apps, agent, "", True)
    rows += await run_db(_personal_rows_of_agent, agent)
    await forget_rows(rows)
    return await stop_rows(rows)


def _personal_rows_of_agent(agent: str) -> list[dict]:
    from storage.pg import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pinned_apps WHERE agent=%s AND username<>''", (agent,),
        ).fetchall()
        return [dict(r) for r in rows]


def _personal_rows_of_user(sub: str) -> list[dict]:
    from storage.pg import get_conn
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pinned_apps WHERE owner_sub=%s", (sub,)).fetchall()
        return [dict(r) for r in rows]


async def stop_user_apps(sub: str) -> int:
    rows = await run_db(_personal_rows_of_user, sub)
    await forget_rows(rows)
    return await stop_rows(rows)


# ── the owner's standing (the offboarding event) ──────────────────────────

SUBSCRIBER = "apps-owner-standing"


def _dormant_rows_of(sub: str, agents: list[str]) -> list[dict]:
    """The person's personal rows on ``agents`` whose owner no longer stands
    there, the standing read now (a re-add since the event keeps the app)."""
    from storage.pg import get_conn
    if not agents:
        return []
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pinned_apps WHERE owner_sub=%s AND username<>'' AND agent = ANY(%s)",
            (sub, list(agents)),
        ).fetchall()]
    return [r for r in rows if task_store.personal_row_dormant(r)]


async def on_offboard(event: OffboardEvent) -> int:
    """A person lost standing on some agents: their personal apps there stop
    (APPS.md "Lifecycle"). Nothing is deleted, hidden or detached: the row,
    its approval, its data, its schedules and its triggers stay, so a
    re-attach brings the app back on the next open; the handler drain
    already refuses a dormant app's wakes."""
    rows = await run_db(_dormant_rows_of, event.sub, [loss.agent for loss in event.agents])
    stopped = await stop_rows(rows)
    if rows:
        logger.info("offboarding: %d personal app(s) of %s dormant (%d server(s) stopped)",
                    len(rows), event.sub[:8], stopped)
    return stopped


def register() -> None:
    from services.agents import offboarding
    offboarding.subscribe(SUBSCRIBER, on_offboard, priority=40)


async def purge(row: dict) -> dict:
    """Remove the app with its data (the caller judged the authority and
    the typed confirmation). A row a template seeded stays behind hidden
    as the owner's opt-out (COMMUNITY-AGENTS-REGISTRY.md "Per-user template
    apps"): no seed, reseed or update recreates it, and the assistant's
    ``pin_app(slug)`` brings it back from the template's copy."""
    await app_supervisor.stop(row["id"])
    await forget_rows([row])
    opted_out = bool(row.get("template_ref"))
    if opted_out:
        await run_db(task_store.set_template_state, row["id"], "opted_out", hidden=True)
    else:
        await run_db(task_store.delete_app, row["id"])
    from services.apps import audience
    audience.forget(row["id"])
    await asyncio.to_thread(releases.remove_release_dir, row)
    await asyncio.to_thread(releases.remove_data_dir, row)
    removed_files = 0
    agent_dir = config.get_agent_dir(row["agent"])
    folder = agent_dir / (row.get("rel_path") or "")
    # The folder only where it lexically is: a folder that is, or sits
    # behind, a symlink a session planted would aim the per-file delete at
    # another tree of the agent (the shared workspace, another user's files).
    def _is_the_registered_folder() -> tuple[bool, bool]:
        return folder.is_dir(), folder.resolve() == agent_dir.resolve() / row["rel_path"]

    present, same = (await asyncio.to_thread(_is_the_registered_folder)) if row.get("rel_path") else (False, False)
    if present:
        if same:
            removed_files = await _remove_workspace_folder(row["agent"], agent_dir, folder)
        else:
            logger.warning("App purge: %s resolves elsewhere; the folder was left in place",
                           row["rel_path"])
    logger.info("App purged: app=%s id=%s files=%d opted_out=%s", row.get("slug"), row["id"],
                removed_files, opted_out)
    return {"status": "ok", "app_id": row["id"], "slug": row["slug"], "files_removed": removed_files,
            "opted_out": opted_out}


async def remove_user_app_dirs(sub: str) -> int:
    """A user delete: the release copies and the databases of every
    personal app they own go with the rows (the rows cascade with the users
    row; the directories would not)."""
    rows = await run_db(_personal_rows_of_user, sub)
    for row in rows:
        await asyncio.to_thread(releases.remove_release_dir, row)
        await asyncio.to_thread(releases.remove_data_dir, row)
    return len(rows)


async def _remove_workspace_folder(agent: str, agent_dir: Path, folder: Path) -> int:
    """The files-API delete sequence per file (recover-bin capture,
    tombstone, delete push), then the empty directories."""
    from services.infra import file_bookkeeping
    count = 0
    if await asyncio.to_thread(folder.is_symlink):
        # The walk below would follow it into another folder's files.
        logger.warning("App purge: %s is a link and was left in place", folder.name)
        return 0
    files = await asyncio.to_thread(
        lambda: sorted(p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()))
    for f in files:
        try:
            await file_bookkeeping.delete_platform_file(agent, agent_dir, f.resolve())
            count += 1
        except Exception:
            logger.exception("App purge: could not delete %s", f)
    # The folder itself goes beneath the agents tree: a link in its place or
    # above it (swapped in after the check) is refused, never followed, and
    # a folder left in place is not announced as gone to the satellites.
    from services.infra import safe_fs

    def _rmtree() -> bool:
        try:
            root, rel = releases.agents_rel(folder)
            safe_fs.rmtree_beneath(root, rel, missing_ok=True)
            return True
        except OSError as e:
            logger.warning("App purge: %s was left in place (%s)", folder.name, e.strerror or e)
            return False

    if await asyncio.to_thread(_rmtree):
        try:
            rel = folder.relative_to(agent_dir).as_posix()
            await file_bookkeeping.push_file_delete(agent, rel)
        except Exception:
            logger.exception("App purge: delete push failed for %s", folder)
    return count
