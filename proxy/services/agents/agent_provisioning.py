"""The filesystem, git and quota steps an agent or a person's membership
takes after the store commits: an agent's config repo and shared quota
scope, the quota released when the agent goes, a person's folders on the
agents they hold and their memory scope removed on one they lost, and the
pre-1.4 persona file migration.

The stores call these through the hooks ``install`` registers
(``agent_store.on_agent_created`` / ``on_agent_deleted``,
``db_users.on_membership_change``), at the points where the steps always
ran; storage itself imports nothing above it. ``install`` runs first in the
app's start (``startup._boot_registrations``) and in the test process.
"""

import logging
from pathlib import Path

import psycopg

import config
from core import layout
from storage.agents import agent_store
from storage.identity import db_users

logger = logging.getLogger("otodock.provisioning")


def install() -> None:
    """Register the post-commit steps with the stores; idempotent."""
    agent_store.on_agent_created(provision_agent_tree)
    agent_store.on_agent_deleted(release_agent_quota)
    agent_store.on_agent_deleted(forget_agent_histories)
    db_users.on_membership_change(_remove_user_scope_memory, _ensure_user_agent_dirs)


def provision_agent_tree(slug: str) -> None:
    """An agent's config and knowledge histories and its shared quota scope,
    after ``create_agent`` wrote the row and the folders."""
    # agent.md, the context files and the shared memory have an audit trail
    # from day one. A new agent has no history: both create routes refuse a
    # name whose folder exists, so one found under the name is an earlier
    # agent's and is set aside, never shown as this one's.
    try:
        from services.infra import git_writer
        agent_dir = config.AGENTS_DIR / slug
        git_writer.set_aside_agent(slug)
        git_writer.init_if_missing(agent_dir / layout.CONFIG)
        git_writer.init_if_missing(agent_dir / layout.KNOWLEDGE)
    except OSError:
        pass
    # Bind the shared bucket (workspace + knowledge + config) to its XFS project
    # ID and apply the current limit now, so lazily-created children (.claude/ …)
    # inherit it. No-op unless the kernel quota tier is enabled. Every agent's
    # shared workspace is metered the same, regardless of visibility mode.
    try:
        from services.infra import storage_quota
        storage_quota.ensure_scope(slug, "shared")
    except psycopg.Error:
        pass  # best-effort; the quota monitor sweep re-asserts assignment


def release_agent_quota(slug: str) -> None:
    """The agent's quota released, after ``delete_agent`` removed a row."""
    # Release the agent's storage-quota enforcement + alert state (the project
    # rows are kept as tombstones so IDs are never reused). Best-effort; no-op
    # unless the kernel quota tier is enabled.
    try:
        from services.infra import storage_quota
        storage_quota.reclaim_agent(slug)
    except psycopg.Error:
        pass


def forget_agent_histories(slug: str) -> None:
    """The agent's histories removed with it, after ``delete_agent`` removed
    a row: a later agent of the same name starts with none."""
    from services.infra import git_writer
    git_writer.forget_agent(slug)


def _remove_user_scope_memory(sub: str, username: str | None, agent: str) -> None:
    """Delete ``agents/{agent}/users/{username}/context/memory/`` after the
    removal commits, so a later reassignment doesn't resurrect ghost
    memories (git history in the per-user context repo still allows
    recovery). Beneath the agent root with no symlink followed: the person's
    own sessions write under ``users/{username}``. Best-effort: a missing
    dir or username is silently ignored."""
    if not username:
        return
    try:
        import config as _cfg
        from services.infra import safe_fs
        # From AGENTS_DIR with the agent as the first component: the agent's
        # own folder is not a root (a sidecar can swap it for a link).
        rel = f"{agent}/{layout.USERS}/{username}/{layout.CONTEXT}/memory"
        safe_fs.rmtree_beneath(_cfg.AGENTS_DIR, rel)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("User-scope memory cleanup of %s on agent %s failed: %s", sub, agent, exc)


def _ensure_user_agent_dirs(sub: str, agents: list[str]) -> None:
    """Create users/{username}/workspace and users/{username}/context dirs
    for each agent the user is assigned to. Safe to call repeatedly.
    Skips Shared-only agents — they have no per-user directories.
    Also initializes a git repo in users/{username}/context/ so
    per-user docs + memory/ are audit-tracked from day one.
    """
    try:
        username = db_users.get_username_by_sub(sub)
        if not username:
            return
        import config as _cfg
        from core.session.visibility import is_shared_only
        for agent_name in agents:
            if is_shared_only(agent_name):
                continue  # Shared-only agents are agent-scope; no per-user dirs
            agent_dir = _cfg.get_agent_dir(agent_name)
            if not agent_dir.exists():
                continue
            user_dir = layout.user_dir(agent_dir, username)
            # Made beneath the agent's real folder with no link followed.
            from core import agent_folder
            from services.infra import safe_fs
            root = agent_folder.agent_root(agent_name)
            try:
                for folder in (layout.WORKSPACE, "context"):
                    safe_fs.mkdirs_beneath(root, f"{layout.user_rel(username)}/{folder}")
            except OSError as exc:
                logger.warning("The folders of %s in %s were not made: %s", username, agent_name, exc)
                continue
            try:
                from services.infra import git_writer
                git_writer.init_if_missing(user_dir / "context")
            except OSError as exc:
                # Non-critical
                logger.debug("Git init of user context dir for %s failed: %s", agent_name, exc)
            # Bind this user's folder to its XFS project ID + limit before any
            # write (no-op unless the kernel quota tier is enabled).
            try:
                from services.infra import storage_quota
                storage_quota.ensure_scope(agent_name, "user", username)
            except psycopg.Error as exc:
                # Non-critical — the quota sweep re-asserts assignment
                logger.debug("Quota scope binding for %s failed: %s", agent_name, exc)
    except (psycopg.Error, OSError, ValueError) as exc:
        # Non-critical — dirs will be created on first access anyway
        logger.debug("Ensuring user-agent dirs failed: %s", exc)


# Persona filename migration (prompt.md → agent.md, pre-1.4 installs).
#: Two persona files are compared whole up to this size; a larger one is
#: left as it is (the readers prefer agent.md anyway).
_PERSONA_COMPARE_MAX_BYTES = 16 * 1024 * 1024


def migrate_persona_filenames() -> int:
    """One-shot startup sweep: converge every agent on ``config/agent.md``.

    ``prompt.md`` is the pre-1.4 persona filename. Per agent: only
    ``prompt.md`` → git-committed rename to ``agent.md``; both present and
    byte-identical → drop the duplicate ``prompt.md``; both and divergent →
    keep both (readers prefer ``agent.md``) and warn. Every removal records
    a sync tombstone so an idle satellite APPLIES the delete at next merge
    instead of resurrecting the old file, and the change is committed to the
    config audit repo so revert-from-history keeps a coherent trail.

    Runs in the lifespan before the app serves traffic (single process, no
    readers racing). Best-effort per agent — a failure leaves that agent on
    ``prompt.md``, which every reader still accepts. Returns the number of
    agents changed.
    """
    import time as _time

    from core import agent_folder
    from services.infra import git_writer, safe_fs
    from storage.files import file_author_store, file_tombstones_store

    migrated = 0
    for slug in agent_store.get_agent_slugs():
        config_dir = config.AGENTS_DIR / slug / "config"
        old = config_dir / "prompt.md"
        new = config_dir / "agent.md"
        # Looked at, compared, removed and renamed beneath the agent's real
        # folder with no link followed (a manager's session writes /config).
        root = agent_folder.agent_root(slug)
        old_rel, new_rel = "config/prompt.md", "config/agent.md"
        try:
            if not safe_fs.kind_beneath(root, old_rel).is_file:
                continue
            if safe_fs.kind_beneath(root, new_rel).is_file:
                cap = _PERSONA_COMPARE_MAX_BYTES
                if (safe_fs.read_bytes_beneath(root, new_rel, max_size=cap)
                        != safe_fs.read_bytes_beneath(root, old_rel, max_size=cap)):
                    logger.warning(
                        "Agent %s has both agent.md and prompt.md with "
                        "different content — keeping both; agent.md wins at "
                        "read time", slug,
                    )
                    continue
                safe_fs.unlink_beneath(root, old_rel)
                action = "removed duplicate config/prompt.md"
            else:
                safe_fs.rename_beneath(root, old_rel, new_rel)
                action = "renamed config/prompt.md to agent.md"
            file_tombstones_store.record(
                slug, "config/prompt.md", _time.time(),
                origin="persona-migration",
            )
            file_author_store.clear(slug, "config/prompt.md")
            file_tombstones_store.drop(slug, "config/agent.md")
            commit_targets: list[Path | str] = [new]
            if git_writer.is_tracked(config_dir, "prompt.md"):
                commit_targets.append(old)
            git_writer.commit_paths(
                config_dir, commit_targets, "Rename persona file to agent.md",
            )
            logger.info("Agent %s: %s", slug, action)
            migrated += 1
        except OSError as exc:
            logger.warning("Persona migration skipped for %s: %s", slug, exc)
    return migrated
