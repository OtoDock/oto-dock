"""What an agent's prompt says about its MCPs: the available-MCPs section and the
skills it reads inline, lists in its catalog or materializes on demand.
"""

import logging
from pathlib import Path

from auth import roles
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities
from core.session import session_kind
from services.mcp import mcp_manifest_parse as _mmp
from services.mcp.mcp_manifest_store import (
    get_agent_mcps,
    get_agent_mcps_all_placements,
)
from services.mcp.mcp_session_filter import (
    EXTERNAL_CONTEXT,
    SHARED_CHAT_CONTEXT,
    _prompt_contexts,
    audience_admits,
    session_exclusion_reason,
    skill_audience_admits,
)
from storage.mcp import mcp_store

logger = logging.getLogger(__name__)


def _first_sentence(text: str) -> str:
    """Return the first sentence of a description (period-delimited).

    Falls back to the whole text if there's no period. Used to trim
    manifest descriptions to a one-line catalog entry — keeps operational
    notes ("Permission-aware: viewers see zero tools" and similar) out
    of the agent's view while preserving the meaningful lede.
    """
    text = (text or "").strip()
    if not text:
        return ""
    idx = text.find(". ")
    if idx == -1:
        # No multi-sentence — strip a single trailing period if present
        return text.rstrip(".")
    return text[:idx]


def is_skill_package(manifest) -> bool:
    """A standalone skill package: a context-only carrier of skills with
    no server and no tool (``category: skill``)."""
    return manifest.category == "skill"


def build_available_mcps_section(
    agent_name: str,
    *,
    context: str = "",
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    external: bool = False,
    outside_picks=None,
    user_role: str | None = None,
    shared_chat: bool = False,
    kind_rule=None,
) -> str:
    """Build the ``# Available Tools (MCPs)`` prompt section.

    Lists each enabled MCP as one bullet — ``**{label}** (`{slug}`) — {desc}``.
    Sourced from ``get_agent_mcps`` (visible + manager-enabled + platform-
    enabled), filtered to skip:
      - Manifests whose ``exclude_from`` matches the current session
        context (e.g. ``display-mcp`` is phone-excluded).

    Returns an empty string when no MCPs would be listed (the caller
    omits the section heading entirely in that case).

    Args:
        agent_name: agent slug.
        context: session context string — ``"dashboard"`` / ``"phone"`` /
            ``"task"`` / ``"terminal"``. Empty string skips context filtering
            (defense-only — upstream usually filters).
        placement: the session's resolved placement, forwarded to
            ``get_agent_mcps`` so the prompt catalog only lists device-local
            MCPs the session can actually use. Fail-closed defaults.
        user_role: the session's person's role on the agent; None lists
            every audience (no person known).
    """
    manifests = get_agent_mcps(agent_name, placement=placement) or []
    # ``user_role`` (the session's person) drops the manifests whose
    # ``audience`` the person is below, as the session config does.
    manifests = [m for m in manifests if audience_admits(getattr(m, 'audience', ''), user_role)]
    # Sort alphabetically by label for deterministic output — registry scan
    # order is otherwise filesystem-dependent and can drift across reinstalls.
    manifests_sorted = sorted(
        manifests, key=lambda m: (m.label or m.name).lower()
    )
    rows: list[str] = []
    for m in manifests_sorted:
        # Standalone skill packages are context-only carriers of skills —
        # advertising them as "MCP servers ... tools you can call" would put
        # a tool-less server in front of the agent. Their skills surface via
        # the skills pipeline (inline or the CLI's own index) instead.
        if is_skill_package(m):
            continue
        if session_exclusion_reason(
            m, contexts=_prompt_contexts(context, shared_chat), external=external,
            picks=outside_picks, kind_rule=kind_rule,
        ):
            continue
        label = (m.label or m.name).strip()
        desc = _first_sentence(m.description)
        if desc:
            rows.append(f"- **{label}** (`{m.name}`) — {desc}")
        else:
            rows.append(f"- **{label}** (`{m.name}`)")
    if not rows:
        return ""
    header = (
        "# Available Tools (MCPs)\n\n"
        "You have these MCP servers enabled in this session — each "
        "provides a set of tools you can call:\n\n"
    )
    footer = (
        "\n\nDetailed usage instructions for each MCP follow in "
        "`# MCP Tool Skills` below."
    )
    return header + "\n".join(rows) + footer


def get_skills_for_agent(
    agent_name: str,
    context: str = "dashboard",
    *,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    external: bool = False,
    user_role: str | None = None,
    shared_chat: bool = False,
    outside_picks=None,
    kind_rule=None,
) -> list[tuple[str, str, str]]:
    """Return (skill_id, prompt_content, loading) for an agent, context-filtered.

    Reads agent_skills DB table, checks exclude_from, loads skill files from
    disk with any SKILL.md frontmatter stripped (frontmatter is index
    metadata for the CLI skills dir, never prompt text). Context is the
    session kind's name (``core/session/session_kind.py``) or the ``terminal``
    placement — matched against the skill's ``exclude_from``; ``""`` skips
    the exclusion filter.

    ``loading`` is the manifest stamp (``"always"`` | ``"on_demand"``) — the
    caller decides what to inline vs. hand to the CLI's own progressive
    disclosure.

    ``placement`` forwards to ``get_agent_mcps`` so a device-local MCP's
    skill text is dropped on sessions that can't run it (fail-closed: the
    local placement by default). ``user_role`` (the session's role) drops the skills whose ``audience``
    the role fails (``skill_audience_admits``); ``None`` filters nothing.
    """
    from services.mcp.skill_format import strip_frontmatter

    result: list[tuple[str, str, str]] = []
    for manifest, skill_def in _iter_agent_skills(
        agent_name, context, placement=placement,
        external=external, user_role=user_role, shared_chat=shared_chat,
        outside_picks=outside_picks, kind_rule=kind_rule,
    ):
        # The file must be a regular file inside this MCP's own folder, read
        # as it was authorized; a value that escapes it is never read.
        content = _mmp.read_skill_file(manifest.mcp_dir, skill_def.file)
        if content is None:
            logger.warning(
                "Skill %s of %s names %r, which is not a readable file inside its "
                "folder; skipped", skill_def.id, manifest.name, skill_def.file,
            )
            continue
        result.append((skill_def.id, strip_frontmatter(content), skill_def.loading))

    return result


def _iter_agent_skills(
    agent_name: str,
    context: str,
    *,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    external: bool = False,
    outside_picks=None,
    user_role: str | None = None,
    shared_chat: bool = False,
    kind_rule=None,
):
    """``(manifest, skill_def)`` for every ENABLED skill of the agent's
    assigned MCPs that is not excluded from ``context`` — the one filter
    behind the inline skills and the Direct-LLM skill catalog. DB rows
    override the manifest defaults (enabled gate + exclude_from copy);
    ``""`` as context excludes nothing; ``external`` drops
    the MCPs an external session never attaches (their skills with them) and
    honours a skill-level ``"external"`` exclusion; ``user_role`` drops the
    skills whose audience the role fails."""
    db_skills = mcp_store.get_agent_skills(agent_name)
    skill_map: dict[str, dict] = {s["skill_id"]: s for s in db_skills}

    assigned_mcps = get_agent_mcps(agent_name, placement=placement)
    for manifest in assigned_mcps:
        if session_exclusion_reason(
            manifest, contexts=_prompt_contexts(context, shared_chat), external=external,
            picks=outside_picks, kind_rule=kind_rule,
        ):
            continue
        for skill_def in manifest.skills:
            if not skill_audience_admits(skill_def, user_role):
                continue
            db_entry = skill_map.get(skill_def.id)
            if db_entry:
                if not db_entry["enabled"]:
                    continue
                exclude_from = db_entry["exclude_from"]
            else:
                exclude_from = skill_def.default_exclude_from
            if context in exclude_from:
                continue
            if external and EXTERNAL_CONTEXT in exclude_from:
                continue
            if shared_chat and SHARED_CHAT_CONTEXT in exclude_from:
                continue
            yield manifest, skill_def


def get_skill_catalog_for_agent(
    agent_name: str,
    context: str = "dashboard",
    *,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    external: bool = False,
    user_role: str | None = None,
    shared_chat: bool = False,
    outside_picks=None,
    kind_rule=None,
) -> list[tuple[str, str]]:
    """``(skill_id, description)`` of the agent's enabled ON-DEMAND skills in
    this context, sorted by id — the ``# Skills`` catalog of a Direct-LLM
    prompt. The CLI engines index their materialized skills dir themselves;
    the direct layer has a client-side ``Skill`` builtin instead and needs
    the names in the prompt to know what it can load. Same filters as the
    inline path (``_iter_agent_skills``), the audience included."""
    rows = [
        (skill_def.id, skill_def.description or "")
        for _m, skill_def in _iter_agent_skills(
            agent_name, context, placement=placement,
            external=external, user_role=user_role, shared_chat=shared_chat,
            outside_picks=outside_picks, kind_rule=kind_rule,
        )
        if skill_def.loading == "on_demand"
    ]
    return sorted(rows)


def _role_of_username(username: str, agent_name: str) -> str | None:
    """The person's effective role on the agent, read from the users table;
    ``None`` when the username names nobody (an unknown person gets the
    unfiltered set, as every caller that passes no username does)."""
    from auth import providers
    from storage.identity import db_users
    sub = db_users.get_user_sub_by_username(username)
    if not sub:
        return None
    return providers.effective_role_of(sub, agent_name)


def get_on_demand_skills_for_materialization(
    agent_name: str,
    *,
    username: str | None = None,
    outside_picks=None,
    kind_rule=None,
) -> list[tuple[str, Path, str, str, str]]:
    """The on-demand skills to materialize into a session CLI skills dir.

    Returns ``(skill_id, source_path, package_name, package_version,
    description)`` for every ENABLED ``loading: on_demand`` skill of the
    agent's base manifest set.

    Deliberately **context-free and placement-free** (all-placements set, no
    ``exclude_from`` filtering): the materialized ``skills/`` dir is shared
    by every session context that uses the same config dir (chat + task +
    phone on the workspace scope), and concurrent config-dir ensures MUST
    compute the identical set or reconciliation ping-pongs — one session
    start deleting the folders another just wrote (plan §2.1). Per-context
    precision lives on the INLINE path, where the token cost is; an
    on-demand skill's residual footprint is a one-line CLI index entry.

    ``username`` names the person whose own config dir is being
    materialized: a skill with an ``audience`` their role on the agent
    fails is left out. One person has one role, so every session that
    shares that dir computes the same set; the agent-level dir (no
    username) keeps the role-free set.

    ``outside_picks`` names an outside session's config dir (a caller's own
    tree, shared with no other context): the set is what the session's own
    filter admits for a caller (``_iter_agent_skills`` with the picks, the
    role ``external`` and the caller's contexts: the tools the one rule
    admits, and of each the skills whose own exclusion and audience let
    them in), so the dir and the catalog never disagree. The home's calls
    read the same picks, so the identical-set invariant holds there too.
    """
    if outside_picks is not None:
        pairs = _iter_agent_skills(
            agent_name, session_kind.PHONE.name, external=True, shared_chat=True,
            outside_picks=outside_picks, user_role=roles.EXTERNAL,
        )
        return _materialized(pairs)
    if kind_rule is not None:
        # A scratch dir of a session kind, shared with no other context (a
        # wrap-up's, the agent's own line's): what its rule admits.
        context = session_kind.PHONE.name if getattr(kind_rule, "open_line", False) else session_kind.TASK.name
        pairs = _iter_agent_skills(
            agent_name, context, external=False, shared_chat=True,
            kind_rule=kind_rule, user_role=roles.MANAGER,
        )
        return _materialized(pairs)
    user_role = _role_of_username(username, agent_name) if username else None
    db_skills = {s["skill_id"]: s for s in mcp_store.get_agent_skills(agent_name)}
    pairs = (
        (manifest, skill_def)
        for manifest in get_agent_mcps_all_placements(agent_name)
        for skill_def in manifest.skills
        if skill_audience_admits(skill_def, user_role)
        and not ((row := db_skills.get(skill_def.id)) and not row["enabled"])
    )
    return _materialized(pairs)


def _materialized(pairs) -> list[tuple[str, Path, str, str, str]]:
    """The on-demand ones of ``(manifest, skill_def)`` pairs, each with its
    file resolved inside the MCP's folder."""
    out: list[tuple[str, Path, str, str, str]] = []
    for manifest, skill_def in pairs:
        if skill_def.loading != "on_demand":
            continue
        source = _mmp.resolve_skill_file(manifest.mcp_dir, skill_def.file)
        if source is None:
            logger.warning(
                "Skill %s of %s names %r, which is not a file inside its "
                "folder; not materialized", skill_def.id, manifest.name,
                skill_def.file,
            )
            continue
        out.append((skill_def.id, source, manifest.name, manifest.version, skill_def.description))
    return out
