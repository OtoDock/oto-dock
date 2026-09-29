"""An agent's setup state and its completion (COMMUNITY-AGENTS-REGISTRY.md
"Setup guides"), shared by the route (``POST /v1/agents/{name}/
complete-setup``, the ``complete_setup`` tool behind it) and the platform
methods an app declares (``setup.status`` / ``setup.complete``, APPS.md
"Platform catalog"): one service, three callers.

Agent-wide: ``config/context/setup.md`` present = pending; the stamp
``agents.setup_completed_at`` = done; neither = the agent never had a
guide. Per user: the canonical ``config/user-setup.md`` says the agent
onboards its members; a member's own copy under ``users/<u>/context/``
present = pending, gone = done. File presence is the state, so a restore
is honest. Every delete carries the sync and audit bookkeeping a
platform-side delete needs (a bare unlink is resurrected by the next
satellite merge), and a completed per-user setup is announced on the
member's ``file_changes`` slice so a page that welcomed them flips.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import config
from storage import database as task_store
from storage.agents import agent_store
from core import layout

logger = logging.getLogger("claude-proxy.agents")

AGENT_SETUP = "setup.md"
USER_SETUP = "user-setup.md"


def agent_setup_path(agent: str) -> Path:
    return config.get_agent_dir(agent) / layout.CONFIG / layout.CONTEXT / AGENT_SETUP


def user_setup_path(agent: str, username: str) -> Path | None:
    if not username:
        return None
    return layout.context_dir(config.get_agent_dir(agent), username) / USER_SETUP


def status(agent: str, username: str, agent_row: dict | None) -> dict:
    """``{"user": pending|done|none, "agent": pending|done|none}``."""
    agent_dir = config.get_agent_dir(agent)
    user_state = "none"
    if username and (agent_dir / "config" / USER_SETUP).is_file():
        own = user_setup_path(agent, username)
        user_state = "pending" if own is not None and own.is_file() else "done"
    if agent_setup_path(agent).is_file():
        agent_state = "pending"
    elif (agent_row or {}).get("setup_completed_at"):
        agent_state = "done"
    else:
        agent_state = "none"
    return {"user": user_state, "agent": agent_state}


async def remove_synced_setup_file(agent: str, abs_path: Path | None, rel_path: str,
                                   repo_dir: Path) -> bool:
    """Delete a setup file WITH the bookkeeping every platform-side delete
    needs: tombstone (an idle satellite APPLIES the delete at its next
    merge instead of resurrecting its copy — user context and owner-tier
    config write back), author clear, live-delete fan-out, and a commit in
    the owning git repo when the file is tracked (so the dashboard revert
    button cannot silently resurrect a completed setup)."""
    if abs_path is None or not abs_path.is_file():
        return False
    try:
        await asyncio.to_thread(abs_path.unlink)
    except Exception:
        logger.exception("complete-setup: failed to remove %s for %s", rel_path, agent)
        return False
    import time as _time
    from storage.files import file_author_store, file_tombstones_store
    await asyncio.to_thread(file_tombstones_store.record, agent, rel_path, _time.time(),
                            origin="complete-setup")
    await asyncio.to_thread(file_author_store.clear, agent, rel_path)
    try:
        from services.remote import workspace_fanout
        await workspace_fanout.fan_out_delete(agent, rel_path, include_idle=True)
    except Exception:
        logger.exception("complete-setup: delete fan-out failed for %s", rel_path)
    try:
        from services.infra import git_writer
        rel_in_repo = str(abs_path.relative_to(repo_dir))
        if git_writer.is_tracked(repo_dir, rel_in_repo):
            await asyncio.to_thread(git_writer.commit_paths, repo_dir, [abs_path],
                                    f"Complete setup: remove {abs_path.name}")
    except Exception:
        logger.exception("complete-setup: git commit failed for %s", rel_path)
    return True


async def complete_user_setup(agent: str, username: str, user_sub: str) -> dict:
    """The member's own guide goes; their ``file_changes`` slice hears it."""
    agent_dir = config.get_agent_dir(agent)
    rel = f"{layout.user_rel(username)}/{layout.CONTEXT}/{USER_SETUP}"
    removed = await remove_synced_setup_file(
        agent, user_setup_path(agent, username), rel, layout.context_dir(agent_dir, username))
    if removed:
        try:
            from api.apps import catalog
            catalog.file_changed([user_sub], agent, rel, source="setup")
        except Exception:
            logger.exception("complete-setup: file_changes delta failed for %s", rel)
    return {"status": "user_setup_complete", "user_setup_removed": removed}


async def complete_agent_setup(agent: str, agent_row: dict, *, summary: str = "") -> dict:
    """The agent-wide guide goes, the stamp is set once, the installer hears
    of the first transition (every admin when the installer is gone)."""
    agent_dir = config.get_agent_dir(agent)
    setup_md_removed = await remove_synced_setup_file(
        agent, agent_setup_path(agent), f"config/context/{AGENT_SETUP}", agent_dir / "config")
    if agent_row.get("setup_completed_at"):
        return {"status": "already_complete", "setup_completed_at": agent_row["setup_completed_at"],
                "setup_md_removed": setup_md_removed}
    updated = await asyncio.to_thread(agent_store.mark_setup_completed, agent)
    try:
        from services.notifications import notification_manager
        display = agent_row.get("display_name") or agent
        body_text = f"`{agent}` has confirmed post-install setup is complete."
        if (summary or "").strip():
            body_text += f"\n\nSummary: {summary.strip()}"
        installer_sub = agent_row.get("created_by") or ""
        targets: list[str] = []
        if installer_sub and await asyncio.to_thread(task_store.get_user, installer_sub):
            targets = [installer_sub]
        if not targets:
            from storage.pg import get_conn

            def _admin_subs() -> list[str]:
                with get_conn() as conn:
                    rows = conn.execute("SELECT sub FROM users WHERE role='admin'").fetchall()
                    return [r["sub"] for r in rows]

            targets = await asyncio.to_thread(_admin_subs)
        for target in targets:
            await notification_manager.fire_notification(
                title=f"Setup complete for {display}", body=body_text, severity="info",
                scope="user", target=target, source="community_agent", source_id=agent)
    except Exception:
        logger.exception("complete-setup notification failed for %s", agent)
    return {"status": "completed", "agent": updated, "setup_md_removed": setup_md_removed}
