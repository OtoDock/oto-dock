"""Community-agent install orchestration.

End-to-end flow for installing a community-agents template (or a bundled
template like PA-lite):

1. Parse + validate template via :mod:`storage.community_agent_template_store`.
2. Pre-flight: every required MCP must exist either in the platform's local
   MCP registry OR in the community MCPs catalog. Hard-error otherwise.
3. Resolve slug collision (return 409 with ``suggested_slug`` for caller).
4. Create the agent row + folder structure (mirror of
   ``api/agents/agents.py::create_agent``, callable without going through HTTP).
5. Auto-assign the installer as manager of the new agent.
6. Cascade MCP enablement:
   - Auto-mode MCP already installed → enable for new agent.
   - Explicit-mode MCP installed with ``assigned_to_all`` instance → enable.
   - Otherwise → admin installer auto-installs inline; manager installer
     creates a batch of ``mcp_assignment_requests`` tagged with one shared
     ``batch_id``.
7. Seed tasks / triggers / notifications from the template manifest
   (idempotent via partial unique indexes).
8. Copy ``context/`` recursively into the new agent's ``config/context/``.
9. Copy ``setup.md`` to ``config/context/setup.md`` if present and notify the
   installer.
10. Fire one combined admin notification per batch (if a batch_id was
    generated).

Every install (PA-lite auto-install at first boot and admin-driven
Browse-Agents installs) goes through :func:`install_from_catalog`, which
fetches the tarball from ``OtoDock/community-agents`` and delegates to
:func:`install_from_extracted_template`. There is no on-disk bundled
template folder anymore — the catalog is the single source of truth.

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import uuid
from pathlib import Path

import config
from fastapi import HTTPException

from storage.community_agent_template_store import (
    CommunityAgentTemplate,
    TemplateValidationError,
    load_template_from_dict,
    load_template_from_dir,
    template_to_persistable_dict,
)

logger = logging.getLogger("claude-proxy.community-agent-installer")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def install_from_catalog(
    template_slug: str,
    target_slug: str,
    installer_user_sub: str | None,
    installer_role: str,
) -> dict:
    """Fetch + install a template from ``OtoDock/community-agents``.

    Mirrors :func:`community_installer.install_from_catalog` for MCPs:
    fetches the repo tarball, extracts the ``<slug>/`` subfolder into a
    temp dir, calls :func:`install_from_extracted_template`, then cleans
    up the temp dir.

    ``installer_user_sub`` is the installing user: the admin created by the
    first-boot setup wizard (``api/auth/setup.py``) for the bundled
    ``personal-assistant-lite`` install, or the logged-in user for
    dashboard-driven installs. The ``| None`` is defensive — live callers
    always pass a real sub.
    """
    from services.community import community_agents_catalog

    extracted = await community_agents_catalog.fetch_and_extract_template(
        template_slug,
    )
    try:
        template = await asyncio.to_thread(load_template_from_dir, extracted)
    except TemplateValidationError as exc:
        raise HTTPException(400, f"Invalid catalog template: {exc}")
    try:
        return await install_from_extracted_template(
            template=template,
            target_slug=target_slug,
            installer_user_sub=installer_user_sub,
            installer_role=installer_role,
            source_label=template_slug,
        )
    finally:
        # extracted dir is a tempdir owned by the catalog fetcher
        shutil.rmtree(extracted, ignore_errors=True)


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

async def install_from_extracted_template(
    *,
    template: CommunityAgentTemplate,
    target_slug: str,
    installer_user_sub: str | None,
    installer_role: str,
    source_label: str,
) -> dict:
    """Install a parsed template into an agent.

    Returns an install envelope::

        {
          "agent_slug": "personal-assistant-pro",
          "batch_id": "uuid…" | None,
          "created_requests": [{...}, ...],
          "ready_mcps": ["schedules-mcp", "notifications-mcp", ...],
          "seeded_tasks": 3,
          "seeded_triggers": 0,
          "seeded_notifications": 1,
          "copied_context": 5,
          "setup_md_copied": True,
        }

    Raises:
        ``HTTPException(400)`` — template validation error or unknown MCP.
        ``HTTPException(409)`` — slug collision; response body carries
            ``suggested_slug`` so the caller can retry.
    """
    from storage import agent_store

    # Pre-flight: every required MCP must be resolvable.
    await _preflight_check_mcps(template.mcps)

    # Slug collision check (don't try to "find a free slug" automatically —
    # the dashboard supplies the user-confirmed target_slug and the unified
    # AgentInstallModal handles auto-suffix retries on 409).
    # Sanitize the caller-supplied slug to a filesystem-safe token BEFORE it is
    # joined to AGENTS_DIR — otherwise '../../x' would create + rmtree OUTSIDE
    # the agents tree (same sanitizer the normal create-agent path uses).
    target_slug = agent_store.sanitize_slug(target_slug)
    if not target_slug:
        raise HTTPException(400, "Invalid agent slug")
    if await asyncio.to_thread(agent_store.agent_exists, target_slug):
        suggested = await asyncio.to_thread(_propose_free_slug, target_slug)
        raise HTTPException(
            409,
            detail={
                "error": "slug_taken",
                "suggested_slug": suggested,
                "message": f"Agent '{target_slug}' already exists",
            },
        )
    agent_dir = config.AGENTS_DIR / target_slug
    if agent_dir.exists():
        raise HTTPException(
            409,
            detail={
                "error": "slug_taken",
                "suggested_slug": await asyncio.to_thread(_propose_free_slug, target_slug),
                "message": f"Agent directory '{target_slug}' already exists on disk",
            },
        )

    # 1. Create folder structure.
    try:
        await asyncio.to_thread(_create_agent_folder, agent_dir, template)
    except Exception as exc:
        if agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        raise HTTPException(500, f"Failed to create agent directory: {exc}")

    # 2. Create DB row.
    #
    # Manifests are engine-agnostic — they don't pin an AI engine or model.
    # Auto-enable the first engine connected on BOTH the platform and the
    # installer's account (claude-code-cli, then codex-cli, never direct-llm),
    # falling back to claude-code-cli, so the installed agent runs zero-config.
    # ``default_model`` stays empty (Auto) and ``default_effort`` empty (High).
    from services.engines import subscription_pool
    execution_path = await asyncio.to_thread(
        subscription_pool.default_execution_layer_for_creator,
        installer_user_sub or "",
    )
    try:
        agent = await asyncio.to_thread(
            agent_store.create_agent,
            target_slug, template.display_name,
            admin_only=False,
            execution_path=execution_path,
            default_model="",
            default_effort="",
            created_by=installer_user_sub or "",
            color=template.color,
            description=template.description,
            community_template=template.slug,
            community_template_version=template.version,
            default_scope=template.default_scope,
            collaborative=template.collaborative,
        )
    except Exception as exc:
        shutil.rmtree(agent_dir, ignore_errors=True)
        raise HTTPException(500, f"Failed to create agent record: {exc}")

    # Persist the parsed template so on_user_added_to_agent can
    # re-seed per-user items for users attached after the installer.
    await asyncio.to_thread(
        agent_store.set_community_template_data,
        target_slug, template_to_persistable_dict(template),
    )
    # Copy the manifest's default_for_new_users.role into the agent's own
    # admin-editable column. Empty when the manifest doesn't declare the
    # block (or declares it disabled).
    if template.default_for_new_users.get("enabled"):
        await asyncio.to_thread(
            agent_store.set_default_for_new_users_role,
            target_slug, template.default_for_new_users["role"],
        )

    # 3. Assign installer as manager.
    if installer_user_sub:
        try:
            await _assign_installer_as_manager(installer_user_sub, target_slug)
        except Exception:
            logger.exception("Failed to assign installer %s as manager of %s",
                             installer_user_sub, target_slug)

    # 4. Cascade MCPs.
    batch_id = str(uuid.uuid4())
    cascade = await _cascade_required_mcps(
        template=template,
        target_slug=target_slug,
        installer_user_sub=installer_user_sub,
        installer_role=installer_role,
        batch_id=batch_id,
    )
    # If no requests landed, the batch_id is meaningless — null it out for
    # the response envelope.
    if not cascade["created_requests"]:
        batch_id = None

    # 5. Seed tasks / triggers / notifications.
    seeded_tasks = await asyncio.to_thread(
        _seed_tasks, target_slug, template, installer_user_sub,
    )
    seeded_triggers = await asyncio.to_thread(
        _seed_triggers, target_slug, template, installer_user_sub,
    )
    seeded_notifs = await asyncio.to_thread(
        _seed_notifications, target_slug, template, installer_user_sub,
    )

    # 6. Copy context/ + setup.md. Apply variable substitution to setup.md
    # so template authors can use {agent_slug} placeholders (handy for
    # links into the agent's own UI like /agents/{agent_slug}/workspace).
    copied_context, setup_copied = await asyncio.to_thread(
        _copy_template_context, agent_dir, template, target_slug,
    )

    # 7. Notifications.
    if batch_id and installer_user_sub:
        from services.community import community_installer
        try:
            await community_installer.notify_batch_created(
                batch_id=batch_id,
                requester_sub=installer_user_sub,
                target_agent_slug=target_slug,
                template_slug=source_label,
            )
        except Exception:
            logger.exception("notify_batch_created failed")

    if setup_copied and installer_user_sub:
        try:
            await _notify_setup_needed(installer_user_sub, target_slug, template.display_name)
        except Exception:
            logger.exception("notify_setup_needed failed")

    return {
        "agent_slug": target_slug,
        "batch_id": batch_id,
        "created_requests": cascade["created_requests"],
        "ready_mcps": cascade["ready_mcps"],
        "seeded_tasks": seeded_tasks,
        "seeded_triggers": seeded_triggers,
        "seeded_notifications": seeded_notifs,
        "copied_context": copied_context,
        "setup_md_copied": setup_copied,
        "agent": agent,
    }


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

async def _preflight_check_mcps(required: list) -> None:
    """Raise 400 if any required MCP isn't resolvable.

    Resolvable means: either the platform has it installed (custom or
    previously installed community), OR the community MCPs catalog has
    it. Otherwise the cascade has nothing to do for that MCP — hard fail
    at install time so the agent isn't half-functional.
    """
    from services.community import community_catalog
    from services.mcp import mcp_registry

    installed = await asyncio.to_thread(mcp_registry.get_all_manifests)
    installed_names = set(installed.keys())

    # Catalog state — failure here shouldn't be fatal (GitHub may be down).
    catalog_names: set[str] = set()
    try:
        registry = await community_catalog.fetch_registry()
        for entry in (registry or {}).get("mcps", []):
            if entry.get("name"):
                catalog_names.add(entry["name"])
    except Exception:
        logger.warning("preflight: community catalog fetch failed — relying on local-installed MCPs only")

    missing = [
        m.name for m in required
        if m.name not in installed_names and m.name not in catalog_names
    ]
    if missing:
        raise HTTPException(
            400,
            detail={
                "error": "missing_mcps",
                "missing": missing,
                "message": (
                    f"Template requires {len(missing)} MCP"
                    f"{'s' if len(missing) != 1 else ''} that are not in the "
                    f"platform OR the community catalog: {', '.join(missing)}"
                ),
            },
        )


# ---------------------------------------------------------------------------
# Folder + slug helpers
# ---------------------------------------------------------------------------

def _propose_free_slug(base: str) -> str:
    """Return the first ``base-N`` slug that doesn't collide."""
    from storage import agent_store
    n = 2
    while True:
        candidate = f"{base}-{n}"
        if not agent_store.agent_exists(candidate):
            return candidate
        n += 1
        if n > 999:  # absurd upper bound
            return f"{base}-{uuid.uuid4().hex[:6]}"


def _create_agent_folder(agent_dir: Path, template: CommunityAgentTemplate) -> None:
    (agent_dir / "config" / "context").mkdir(parents=True, exist_ok=True)
    (agent_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (agent_dir / "users").mkdir(parents=True, exist_ok=True)
    prompt_file = agent_dir / "config" / "prompt.md"
    prompt_file.write_text(template.prompt_md, encoding="utf-8")


def _copy_template_context(
    agent_dir: Path, template: CommunityAgentTemplate, target_slug: str,
) -> tuple[int, bool]:
    """Copy template context/ into agent's config/context/. Returns (count, setup_md_copied).

    ``setup.md`` and every file under ``context/`` are run through
    :func:`_substitute_template_vars` so template authors can use
    ``{agent_slug}`` placeholders (for deep-links into the agent's own UI
    or for prompts that reference the slug). The substitution set is
    intentionally narrow — `{agent_slug}` only for v1 — to avoid
    accidentally mangling content that uses braces for other reasons.
    """
    context_target = agent_dir / "config" / "context"
    context_target.mkdir(parents=True, exist_ok=True)
    count = 0
    for rel, content in template.context_files.items():
        # rel is something like "context/methodology.md" — strip the leading "context/"
        rel_stripped = rel.split("/", 1)[1] if rel.startswith("context/") else rel
        dest = context_target / rel_stripped
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_substitute_template_vars(content, target_slug), encoding="utf-8")
        count += 1
    setup_copied = False
    if template.setup_md is not None:
        (context_target / "setup.md").write_text(
            _substitute_template_vars(template.setup_md, target_slug),
            encoding="utf-8",
        )
        setup_copied = True
    return count, setup_copied


def _substitute_template_vars(content: str, agent_slug: str) -> str:
    """Replace ``{agent_slug}`` literals in template content with the actual
    slug. Intentionally narrow — v1 only supports this one variable so we
    don't accidentally chew up other braces in markdown/code fences.
    """
    return content.replace("{agent_slug}", agent_slug)


# ---------------------------------------------------------------------------
# Manager assignment
# ---------------------------------------------------------------------------

async def _assign_installer_as_manager(user_sub: str, agent_slug: str) -> None:
    """Add ``user_sub`` as a manager of ``agent_slug``, preserving existing
    assignments.
    """
    from storage import database as task_store

    current_roles = await asyncio.to_thread(task_store.get_user_agent_roles, user_sub)
    if agent_slug in current_roles:
        return
    current_roles[agent_slug] = "manager"
    await asyncio.to_thread(
        task_store.set_user_agents,
        user_sub, list(current_roles.keys()), user_sub,
        agent_roles=current_roles,
    )


# ---------------------------------------------------------------------------
# MCP cascade
# ---------------------------------------------------------------------------

async def _cascade_required_mcps(
    *,
    template: CommunityAgentTemplate,
    target_slug: str,
    installer_user_sub: str | None,
    installer_role: str,
    batch_id: str,
) -> dict[str, list]:
    """Enable / request each required MCP for the new agent.

    Per the MCP-cascade matrix:

    - Auto-mode installed → enable for agent (no request).
    - Explicit-mode installed + ``assigned_to_all`` instance → enable + attach
      (no request).
    - Explicit-mode installed + no covering instance → admin installer
      auto-attaches to lowest-id instance (same precedence as runtime env
      delivery) OR fails if zero instances. Manager installer creates an
      ``access`` request tagged with batch_id.
    - Not installed → admin installer auto-installs + enables. Manager
      installer creates an ``install`` request tagged with batch_id.

    Returns ``{"created_requests": [...], "ready_mcps": [...]}``.
    """
    from services.mcp import mcp_registry
    from storage import mcp_store, mcp_request_store

    created_requests: list[dict] = []
    ready_mcps: list[str] = []

    for req in template.mcps:
        mcp_name = req.name
        manifest = await asyncio.to_thread(mcp_registry.get_manifest, mcp_name)

        if manifest is None:
            # Not installed — either admin auto-installs or manager queues.
            if installer_role == "admin":
                # Admin self-installs inline. We model this as creating a
                # request row + immediately approving it — same orchestration
                # as the auto-approve branch in api/mcp/community.py.
                row = await _admin_inline_install(
                    mcp_name=mcp_name,
                    target_slug=target_slug,
                    installer_user_sub=installer_user_sub or "",
                    reason=(
                        f"Required by community agent template "
                        f"'{template.slug}' v{template.version}"
                    ),
                    batch_id=batch_id,
                )
                created_requests.append(row)
                if row["status"] == "installed":
                    ready_mcps.append(mcp_name)
            elif installer_user_sub:
                row = await asyncio.to_thread(
                    mcp_request_store.create_request,
                    mcp_name, target_slug, installer_user_sub,
                    f"Required by community agent template '{template.slug}'",
                    batch_id,
                )
                created_requests.append(row)
            continue

        # Installed → check assignment_mode.
        assignment_mode = getattr(manifest, "assignment_mode", "auto")
        if assignment_mode != "explicit":
            # Auto-mode: just enable for the agent (idempotent).
            await asyncio.to_thread(mcp_store.add_agent_mcp, target_slug, mcp_name)
            await _seed_skills_for_mcp(target_slug, mcp_name, req.skills)
            ready_mcps.append(mcp_name)
            continue

        # Explicit-mode: need instance authorization.
        instances = await asyncio.to_thread(mcp_store.get_mcp_instances, mcp_name)
        if any(i.get("assigned_to_all") for i in instances):
            await asyncio.to_thread(mcp_store.add_agent_mcp, target_slug, mcp_name)
            await _seed_skills_for_mcp(target_slug, mcp_name, req.skills)
            ready_mcps.append(mcp_name)
            continue

        if any(target_slug in (i.get("agents") or []) for i in instances):
            await asyncio.to_thread(mcp_store.add_agent_mcp, target_slug, mcp_name)
            await _seed_skills_for_mcp(target_slug, mcp_name, req.skills)
            ready_mcps.append(mcp_name)
            continue

        # No covering instance → admin attaches to lowest-id, or manager queues.
        if installer_role == "admin" and instances:
            target = sorted(instances, key=lambda i: i["id"])[0]
            await asyncio.to_thread(
                mcp_store.add_agent_to_instance, target["id"], target_slug,
            )
            await asyncio.to_thread(mcp_store.add_agent_mcp, target_slug, mcp_name)
            await _seed_skills_for_mcp(target_slug, mcp_name, req.skills)
            ready_mcps.append(mcp_name)
            continue
        if installer_user_sub:
            row = await asyncio.to_thread(
                mcp_request_store.create_request,
                mcp_name, target_slug, installer_user_sub,
                f"Required by community agent template '{template.slug}'",
                batch_id,
            )
            created_requests.append(row)

    return {"created_requests": created_requests, "ready_mcps": ready_mcps}


async def _admin_inline_install(
    *,
    mcp_name: str,
    target_slug: str,
    installer_user_sub: str,
    reason: str,
    batch_id: str,
) -> dict:
    """Create a request row + immediately approve it (admin auto-approve).
    Returns the terminal row.
    """
    from services.community import community_installer
    from storage import mcp_request_store

    row = await asyncio.to_thread(
        mcp_request_store.create_request,
        mcp_name, target_slug, installer_user_sub, reason, batch_id,
    )
    try:
        terminal = await community_installer.approve_request(
            row["id"], installer_user_sub,
            admin_note="Auto-approved (admin community-agent install).",
        )
        return terminal
    except Exception:
        logger.exception("Admin inline install failed for %s", mcp_name)
        # Re-fetch the row in whatever state it landed.
        latest = await asyncio.to_thread(mcp_request_store.get_request, row["id"])
        return latest or row


async def _seed_skills_for_mcp(agent_slug: str, mcp_name: str, skills: list[str]) -> None:
    """Seed per-skill rows for an MCP. Empty ``skills`` list = all default-on."""
    from services.mcp import mcp_registry
    from storage import mcp_store

    manifest = await asyncio.to_thread(mcp_registry.get_manifest, mcp_name)
    if not manifest:
        return
    chosen = set(skills) if skills else None
    for skill in manifest.skills:
        if chosen is not None and skill.id not in chosen:
            continue
        await asyncio.to_thread(
            mcp_store.ensure_agent_skill, agent_slug, skill.id,
            default_enabled=True,
            default_exclude_from=skill.default_exclude_from,
        )


# ---------------------------------------------------------------------------
# Template-item seeding
# ---------------------------------------------------------------------------

def _seed_tasks(
    agent_slug: str, template: CommunityAgentTemplate, installer_user_sub: str | None,
) -> int:
    """Seed agent-scope tasks + the installer's user-scope tasks at install time.

    Later joiners pick up their user-scope items via
    :func:`on_user_added_to_agent` reading the persisted
    ``agents.community_template_data`` column.
    """
    count = 0
    for item in template.tasks:
        if item.scope != "agent":
            continue
        created_by = installer_user_sub or ""
        if _create_task_idempotent(item, agent_slug, created_by, template.slug):
            count += 1
    if installer_user_sub:
        # Installer is auto-assigned the manager role for their new agent
        # (see _assign_installer_as_manager), so the role filter runs
        # against "manager" here.
        count += _seed_tasks_for_user(
            agent_slug, template, installer_user_sub, "manager",
        )
    return count


def _seed_tasks_for_user(
    agent_slug: str,
    template: CommunityAgentTemplate,
    user_sub: str,
    role: str,
) -> int:
    """Seed every user-scope template task that targets ``role`` for ``user_sub``.

    Idempotent via ``idx_dyn_tasks_tpl_user`` — re-runs of the same (agent,
    template_item_slug, user_sub) tuple no-op. Skips items where
    ``auto_create_for_new_users`` is false or where ``roles`` restricts to
    a different per-agent role.
    """
    count = 0
    for item in template.tasks:
        if item.scope != "user":
            continue
        if not item.auto_create_for_new_users:
            continue
        if item.roles and role not in item.roles:
            continue
        if _create_task_idempotent(item, agent_slug, user_sub, template.slug):
            count += 1
    return count


def _create_task_idempotent(
    item, agent_slug: str, created_by: str, template_slug: str,
) -> bool:
    """Returns True if a new row was created, False if it already existed
    (idempotent guard catches the unique-index violation)."""
    from storage import database as db
    import psycopg

    task_id = f"task-{template_slug}-{item.slug}-{agent_slug}-{created_by or 'agent'}"[:120]
    enabled = item.default_state == "active"
    schedule = item.cron if item.schedule_kind == "cron" else None
    interval = item.interval_seconds if item.schedule_kind == "interval" else None
    run_at = item.run_at if item.schedule_kind == "run_at" else None
    # Platform task_type vocabulary is scheduled/one_time/trigger (see
    # scheduler.TaskDefinition) — cron and interval are both "scheduled";
    # the schedule/interval columns carry the how.
    task_type = (
        "scheduled" if item.schedule_kind in ("cron", "interval") else "one_time"
    )
    try:
        db.create_dynamic_task(
            task_id=task_id, agent=agent_slug, name=item.description or item.slug,
            prompt=item.prompt, llm_mode="cli", task_type=task_type,
            schedule=schedule, run_at=run_at,
            delay_seconds=None, interval_seconds=interval,
            timeout_seconds=600, created_by=created_by, scope=item.scope,
            on_complete_agent=None, on_complete_prompt=None,
            on_complete_session_id=None, on_complete_chat_id=None,
            continue_session=None, use_persistent=False,
            notification_mode="manual", notify_severity="info",
            user_tz="UTC",
            community_template=template_slug,
            community_template_item_slug=item.slug,
        )
        # Set enabled flag if needed (default in CREATE TABLE is TRUE; pause
        # via an UPDATE when default_state='paused').
        if not enabled:
            from storage.pg import get_conn
            with get_conn() as conn:
                conn.execute(
                    "UPDATE dynamic_tasks SET enabled = FALSE WHERE id = %s",
                    (task_id,),
                )
                conn.commit()
        return True
    except psycopg.errors.UniqueViolation:
        return False
    except Exception:
        logger.exception("Failed to seed task %s for agent %s", item.slug, agent_slug)
        return False


def _seed_triggers(
    agent_slug: str, template: CommunityAgentTemplate, installer_user_sub: str | None,
) -> int:
    """Seed agent-scope triggers + the installer's user-scope triggers at install time.

    Template ``trigger.prompt`` is realized as a paired
    ``dynamic_tasks`` row with ``task_type='trigger'`` plus a trigger row
    linked via ``task_id``. The legacy ``prompt_template`` column was
    dropped along with the inline-prompt fire path.

    Later joiners pick up their user-scope triggers via
    :func:`on_user_added_to_agent`.
    """
    count = 0
    for item in template.triggers:
        if item.scope != "agent":
            continue
        created_by = installer_user_sub or ""
        if _seed_trigger_with_paired_task(item, agent_slug, created_by, template.slug):
            count += 1
    if installer_user_sub:
        count += _seed_triggers_for_user(
            agent_slug, template, installer_user_sub, "manager",
        )
    return count


def _seed_triggers_for_user(
    agent_slug: str,
    template: CommunityAgentTemplate,
    user_sub: str,
    role: str,
) -> int:
    """Seed every user-scope template trigger targeting ``role`` for ``user_sub``."""
    count = 0
    for item in template.triggers:
        if item.scope != "user":
            continue
        if not item.auto_create_for_new_users:
            continue
        if item.roles and role not in item.roles:
            continue
        if _seed_trigger_with_paired_task(
            item, agent_slug, user_sub, template.slug,
        ):
            count += 1
    return count


def _seed_trigger_with_paired_task(
    item, agent_slug: str, created_by: str, template_slug: str,
) -> bool:
    """Create a ``dynamic_tasks(task_type='trigger')`` + ``triggers`` pair.

    Returns True iff at least the trigger row was inserted (the task is
    created idempotently as well). Both rows carry matching
    ``community_template`` + ``community_template_item_slug`` so the
    template's cleanup hook can wipe them together.
    """
    from storage import database as db
    from storage import trigger_store
    import psycopg

    # Stable IDs derived from template + agent (+ user for user-scope) so
    # re-installs are idempotent.
    suffix = created_by or "agent"
    task_id = f"task-{template_slug}-{item.slug}-{agent_slug}-{suffix}"[:120]
    trig_id = f"trig-{template_slug}-{item.slug}-{suffix}"[:120]
    enabled = item.default_state == "active"

    # Ensure the paired trigger task exists. An existing task (UniqueViolation)
    # is fine — same idempotency contract as task seeder.
    try:
        with contextlib.suppress(psycopg.errors.UniqueViolation):
            db.create_dynamic_task(
                task_id=task_id, agent=agent_slug,
                name=f"[trigger] {item.description or item.slug}",
                prompt=item.prompt, llm_mode="cli", task_type="trigger",
                schedule=None, run_at=None,
                delay_seconds=None, interval_seconds=None,
                timeout_seconds=600, created_by=created_by, scope=item.scope,
                on_complete_agent=None, on_complete_prompt=None,
                on_complete_session_id=None, on_complete_chat_id=None,
                continue_session=None, use_persistent=False,
                notification_mode="manual", notify_severity="info",
                user_tz="UTC",
                community_template=template_slug,
                community_template_item_slug=f"{item.slug}__task",
            )
    except Exception:
        logger.exception("Failed to seed trigger-task %s for agent %s",
                         item.slug, agent_slug)
        return False

    # Create the trigger row linked to the task.
    try:
        trigger_store.create_trigger(
            trigger_id=trig_id,
            slug=item.slug, name=item.description or item.slug,
            scope=item.scope, agent=agent_slug, created_by=created_by,
            task_id=task_id,
            enabled=enabled,
            community_template=template_slug,
            community_template_item_slug=item.slug,
        )
        return True
    except psycopg.errors.UniqueViolation:
        return False
    except Exception:
        logger.exception("Failed to seed trigger %s for agent %s",
                         item.slug, agent_slug)
        return False


def _seed_notifications(
    agent_slug: str, template: CommunityAgentTemplate, installer_user_sub: str | None,
) -> int:
    """Seed agent-scope notifications + the installer's user-scope notifications.

    Later joiners pick up their user-scope notifications via
    :func:`on_user_added_to_agent`.
    """
    from storage import notification_store
    import psycopg

    count = 0
    for item in template.notifications:
        if item.scope != "agent":
            continue
        notif_id = f"notif-{template.slug}-{item.slug}-{agent_slug}"
        schedule = item.cron if item.schedule_kind == "cron" else None
        interval = item.interval_seconds if item.schedule_kind == "interval" else None
        run_at = item.run_at if item.schedule_kind == "run_at" else None
        notif_type = "recurring" if item.schedule_kind in ("cron", "interval") else "one_time"
        try:
            notification_store.create_notification(
                notification_id=notif_id, title=item.title, body=item.body,
                severity="info", scope=item.scope, target=None,
                source="template", source_id=f"{template.slug}/{item.slug}",
                notification_type=notif_type,
                schedule=schedule, run_at=run_at, interval_seconds=interval,
                created_by=installer_user_sub, agent_slug=agent_slug,
                community_template=template.slug,
                community_template_item_slug=item.slug,
            )
            count += 1
        except psycopg.errors.UniqueViolation:
            continue
        except Exception:
            logger.exception("Failed to seed notification %s", item.slug)
    if installer_user_sub:
        count += _seed_notifs_for_user(
            agent_slug, template, installer_user_sub, "manager",
        )
    return count


def _seed_notifs_for_user(
    agent_slug: str,
    template: CommunityAgentTemplate,
    user_sub: str,
    role: str,
) -> int:
    """Seed every user-scope template notification targeting ``role`` for ``user_sub``.

    ``notification_store.create_notification`` is an UPSERT (``ON CONFLICT (id)
    DO UPDATE``) — admin-edit paths rely on that behavior. The seeder needs a
    different "is this new?" signal, so we ``get_notification`` first and
    skip if the row already exists. (Tasks + triggers raise on PK conflict,
    so they don't need the pre-check.)
    """
    from storage import notification_store

    count = 0
    for item in template.notifications:
        if item.scope != "user":
            continue
        if not item.auto_create_for_new_users:
            continue
        if item.roles and role not in item.roles:
            continue
        notif_id = f"notif-{template.slug}-{item.slug}-{agent_slug}-{user_sub}"
        if notification_store.get_notification(notif_id) is not None:
            continue
        schedule = item.cron if item.schedule_kind == "cron" else None
        interval = item.interval_seconds if item.schedule_kind == "interval" else None
        run_at = item.run_at if item.schedule_kind == "run_at" else None
        notif_type = "recurring" if item.schedule_kind in ("cron", "interval") else "one_time"
        try:
            notification_store.create_notification(
                notification_id=notif_id, title=item.title, body=item.body,
                severity="info", scope=item.scope, target=user_sub,
                source="template", source_id=f"{template.slug}/{item.slug}",
                notification_type=notif_type,
                schedule=schedule, run_at=run_at, interval_seconds=interval,
                created_by=user_sub, agent_slug=agent_slug,
                community_template=template.slug,
                community_template_item_slug=item.slug,
            )
            count += 1
        except Exception:
            logger.exception("Failed to seed user-scope notification %s", item.slug)
    return count


# ---------------------------------------------------------------------------
# User-join hook
# ---------------------------------------------------------------------------

def on_user_added_to_agent(
    agent_slug: str, user_sub: str, role: str,
) -> dict[str, int]:
    """Seed per-user template items when a user joins a community-agent template.

    Called from:
    - ``database.set_user_agents`` whenever a new (sub, agent) row lands.
    - ``services.community.default_agent_assigner.assign_default_agents`` after the
      auto-attach insert.
    - ``POST /v1/admin/agents/{slug}/reseed-template-items`` for recovery.

    Reads from ``agents.community_template_data`` (persisted at install
    time). No-ops when the agent isn't a community-template install or
    when the template column is empty (e.g. an agent created directly via
    the API/admin rather than from a community template).
    """
    from storage import agent_store

    empty = {"tasks": 0, "triggers": 0, "notifications": 0}
    agent = agent_store.get_agent(agent_slug)
    if not agent or not agent.get("community_template"):
        return empty
    data = agent_store.get_community_template_data(agent_slug)
    if not data:
        return empty
    try:
        template = load_template_from_dict(data)
    except TemplateValidationError:
        logger.exception(
            "Stored template data for %s failed validation; skipping seed",
            agent_slug,
        )
        return empty
    return {
        "tasks": _seed_tasks_for_user(agent_slug, template, user_sub, role),
        "triggers": _seed_triggers_for_user(agent_slug, template, user_sub, role),
        "notifications": _seed_notifs_for_user(agent_slug, template, user_sub, role),
    }


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

async def _notify_setup_needed(
    user_sub: str, agent_slug: str, display_name: str,
) -> None:
    from services.notifications import notification_manager
    await notification_manager.fire_notification(
        title=f"Setup required for {display_name}",
        body=(
            f"Your new agent **{agent_slug}** needs configuration. "
            f"Chat with it to walk through `setup.md` — the agent will "
            f"mark setup complete via its `complete_setup` tool."
        ),
        severity="info",
        scope="user",
        target=user_sub,
        source="community_agent",
        source_id=agent_slug,
    )
