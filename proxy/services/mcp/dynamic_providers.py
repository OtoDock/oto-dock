"""The built-in Python providers of the dynamic prompt context: the
delegation roster and the Available Agents block, the display block's
template apps, the schedules block, the meeting rooms and the SSH hosts.

``services/mcp/dynamic_context.py`` is the engine that runs them (and the
manifest ``agent_context`` blocks); it registers each provider by MCP name.
The provider signature: ``def build_context(agent_name: str, **kwargs) -> str | None``.
"""

import logging
from typing import Any

import psycopg

from auth import roles
from core import host_os
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities
from services.mcp import team_line

logger = logging.getLogger("otodock")

# Cap on per-layer model ids rendered into the delegation roster — the full
# catalog belongs in the dashboard picker, not every system prompt.
_ROSTER_MODEL_CAP = 8


def build_delegation_roster(
    delegation_targets: list[str],
    user_sub: str = "",
) -> dict[str, list[dict]]:
    """Resolve each target agent's execution layers + models, for the
    session's person.

    Returns ``{slug: [{path, is_default, is_agent_default, person, platform,
    no_machines, default_model, models, more}, …]}`` — the data
    ``_delegation_mcp_context`` renders so a "delegate this on Opus / on
    codex" request can be validated and chosen instead of delegating blind.
    The engines are the agent's allowlist (``_get_execution_paths``, what the
    spawn validator ``spawn_authz.validate_spawn_overrides`` passes) narrowed
    to the engines someone can run there, each tagged with who: ``person``
    when the session's person holds an account the agent allows
    (``engine_resolution``: their chats, every chat lane, their own tasks;
    a resting account still counts, the listing follows what is connected),
    ``platform`` when the shared subscriptions serve it (the agent's own
    tasks, task lanes on a Shared-only agent; ``core/config/spender.py``). The
    person's resolved engine is marked default and the agent's own engine
    ``is_agent_default``; a session with no person (agent scope) lists the
    pool's engines alone; with nothing runnable, the whole allowlist (the
    spawn's refusal names the reason). Enabled ``list_models`` rows per layer (deduped —
    one query per distinct layer, not per agent), ``resolve_agent_model``
    for the default.

    Sync DB reads — call via ``asyncio.to_thread`` from the config builders
    and pass the result through the ``delegation_roster`` kwarg; NEVER call
    from a context provider (providers are no-I/O by contract).
    """
    import config as app_config
    from services.engines import engine_resolution, subscription_pool
    from storage.agents import agent_store
    from storage.agents.agent_store import _get_execution_paths
    from storage.billing import subscription_store

    models_by_layer: dict[str, list[dict]] = {}
    rows = {slug: agent_store.get_agent(slug) for slug in delegation_targets}
    resolved = engine_resolution.resolve_many([r for r in rows.values() if r], user_sub) if user_sub else {}
    pooled: dict[str, bool] = {}

    def _person_runs(slug: str, path: str) -> bool:
        r = resolved.get(slug)
        return bool(user_sub and r and path in r.allowed)

    def _platform_runs(path: str) -> bool:
        if path not in pooled:
            pooled[path] = subscription_pool.layer_platform_configured(path)
        return pooled[path]

    def _default(slug: str, data: dict) -> str:
        r = resolved.get(slug)
        return (r.execution_path if r and r.execution_path
                else data.get("execution_path") or "")

    def _layer_models(path: str) -> list[dict]:
        # Tier order (untiered last), then registry order: the line an
        # agent reads is the ranking it must not get backwards.
        if path not in models_by_layer:
            try:
                rows = [
                    m for m in subscription_store.list_models(path)
                    if m.get("enabled") and m.get("model_id")
                ]
                rows.sort(key=app_config.model_catalog_sort_key)
                models_by_layer[path] = [{
                    "model_id": m["model_id"],
                    "tier": m.get("tier"),
                    "tier_label": app_config.MODEL_TIER_LABELS.get(m.get("tier") or 0, ""),
                    "good_at": m.get("good_at") or "",
                } for m in rows]
            except psycopg.Error:
                models_by_layer[path] = []
        return models_by_layer[path]

    roster: dict[str, list[dict]] = {}
    for slug in delegation_targets:
        data = rows.get(slug)
        if not data:
            continue
        layers: list[dict] = []
        default_path = _default(slug, data)
        agent_default = data.get("execution_path") or ""
        # The person's order (their use) when the session has one.
        allowlist = list(resolved[slug].order_for_me) if slug in resolved else _get_execution_paths(data)
        # Nothing runnable for the person or the pool: the allowlist as it
        # is, so the model ids stay learnable and the refusal, with its
        # reason, comes from the spawn rather than from a blind pin.
        shown = [p for p in allowlist if _person_runs(slug, p) or _platform_runs(p)] or allowlist
        for path in shown:
            # Per path: the agent default is honoured only where that
            # layer serves it, else the layer's own first choice (the same
            # rule the task runner applies), so a non-default layer never
            # shows no default at all; the person's own model where one
            # resolved for them.
            person = resolved.get(slug)
            default_model = (person.models_for_me.get(path, "") if person else "")
            if not default_model:
                try:
                    default_model = app_config.resolve_agent_model(slug, layer=path)
                except (RuntimeError, psycopg.Error):
                    default_model = ""
            catalog = _layer_models(path)
            models = [m["model_id"] for m in catalog]
            # Default first so the cap can never hide it.
            if default_model in models:
                models = [default_model] + [
                    m for m in models if m != default_model
                ]
            layers.append({
                "path": path,
                "is_default": path == default_path,
                "is_agent_default": path == agent_default,
                "person": _person_runs(slug, path),
                "platform": _platform_runs(path),
                "no_machines": not _supports_remote(path),
                "default_model": default_model,
                "models": models[:_ROSTER_MODEL_CAP],
                "more": max(0, len(models) - _ROSTER_MODEL_CAP),
                "tiers": {m["model_id"]: m["tier"] for m in catalog},
                "catalog": catalog,
            })
        roster[slug] = layers
    return roster


def _tier_tag(tier) -> str:
    return f"t{int(tier)}" if tier else "t?"


def _fmt_model_tiers(roster: dict[str, list[dict]]) -> list[str]:
    """The tier legend rendered ONCE per prompt: every model any roster
    layer offers, grouped by tier, with its one-line "good at"."""
    import config as app_config
    seen: dict[str, dict] = {}
    for layers in roster.values():
        for layer in layers:
            for m in layer.get("catalog") or []:
                seen.setdefault(m["model_id"], m)
    if not seen:
        return []
    by_tier: dict[int | None, list[dict]] = {}
    for m in seen.values():
        by_tier.setdefault(m.get("tier") or None, []).append(m)
    lines = [
        "",
        "**Model tiers** (most capable first; complex, open-ended or "
        "judgement-heavy work belongs on tier 1, only mechanical, well-defined "
        "work should run lower; never assume a newer or bigger-sounding id "
        "is stronger):",
    ]
    for tier in (*sorted(t for t in by_tier if t), None):
        rows = by_tier.get(tier)
        if not rows:
            continue
        label = (f"t{tier} {app_config.MODEL_TIER_LABELS.get(tier, '')}" if tier
                 else "t? untiered (a local or custom model nobody rated)")
        # Models that share a line share it once — the ids first, then the
        # line — so two vendors' models of one tier read as the same thing.
        groups: dict[str, list[str]] = {}
        for m in rows:
            groups.setdefault(m.get("good_at") or "", []).append(f"`{m['model_id']}`")
        items = ", ".join(
            f"{', '.join(ids)} ({good_at})" if good_at else ", ".join(ids)
            for good_at, ids in groups.items()
        )
        lines.append(f"- {label}: {items}")
    return lines


def build_meetings_access(user_sub: str, user_role: str) -> list[dict]:
    """Resolve the agents the acting USER can access, with their per-agent
    role — the meetings participant set (the meetings API gates on user
    access, NOT the delegation roster; see api/meetings/meetings.py).

    Returns ``[{slug, display_name, description, role}, …]`` sorted by slug.
    Empty for no-user sessions (``user_sub == ""``) — their reach is the
    roster instead, and ``_meetings_mcp_context`` falls back to it. Admins
    have no explicit rows; they get every agent tagged "admin".

    Sync DB reads — call via ``asyncio.to_thread`` from the config builders
    and pass the result through the ``meetings_access`` kwarg; NEVER call
    from a context provider (providers are no-I/O by contract).
    """
    from storage import database as task_store
    from storage.agents import agent_store

    if not user_sub:
        return []
    if roles.is_admin(user_role):
        by_agent: dict[str, str] = {
            slug: roles.ADMIN for slug in agent_store.get_agent_slugs()}
    else:
        by_agent = task_store.get_user_agent_roles(user_sub) or {}
    rows: list[dict] = []
    for slug in sorted(by_agent):
        data = agent_store.get_agent(slug)
        if not data:
            continue
        rows.append({
            "slug": slug,
            "display_name": data.get("display_name", slug),
            "description": data.get("description", "") or "",
            "role": by_agent[slug],
        })
    return rows


def _supports_remote(execution_path: str) -> bool:
    """Whether the engine can run on a satellite at all (the roster's
    ``no_machines`` flag). An unregistered id reads as local."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(execution_path)
    return bool(caps and caps.runtime.supports_remote_execution)


#: The words of a `layers:` line, explained once under Available Agents.
ROSTER_LEGEND = (
    "On a `layers:` line, `person` marks an engine the person in this chat "
    "can run (their chats, every chat lane, their own tasks), `platform` one "
    "the shared subscriptions serve (the agent's own tasks, task lanes on a "
    "Shared-only agent); `default` is the engine an "
    "unpinned run of this chat takes and `agent default` the agent's own, "
    "which its agent-scope tasks take; `no paired machine` means the engine "
    "runs on the platform host alone. A model copied from a line runs on "
    "the `default` engine when that engine offers it, else on the first "
    "engine of the agent's own order that does, which is not always the "
    "line's order: name its `layer` for an id on two lines."
)


def _fmt_roster_layers(layers: list[dict]) -> str:
    """One compact `layers:` line for an agent's roster entry."""
    rendered = []
    for layer in layers:
        parts = []
        if layer["is_default"]:
            parts.append("default")
        elif layer.get("is_agent_default"):
            parts.append("agent default")
        who = [w for w, runs in (("person", layer.get("person")), ("platform", layer.get("platform"))) if runs]
        if who:
            parts.append(", ".join(who))
        if layer["no_machines"]:
            parts.append("no paired machine")
        if layer["models"]:
            tiers = layer.get("tiers") or {}
            shown = ", ".join(
                f"{m} [default, {_tier_tag(tiers.get(m))}]"
                if m == layer["default_model"]
                else f"{m} [{_tier_tag(tiers.get(m))}]"
                for m in layer["models"]
            )
            if layer["more"]:
                shown += f", +{layer['more']} more"
            parts.append(f"models: {shown}")
        rendered.append(
            f"{layer['path']} ({' | '.join(parts)})" if parts
            else layer["path"]
        )
    return " · ".join(rendered)


def _department_data(agent_name: str) -> dict | None:
    """The agent's department row, for ``_department_line``, and a warm
    agent cache (sync store reads: call off the loop). None when the agent
    is in no department."""
    from storage.agents import agent_store, db_departments
    self_data = agent_store.get_agent(agent_name) or {}
    dept_id = self_data.get("department_id") or ""
    if not dept_id or not self_data.get("department_level_id"):
        return None
    agent_store.get_all_agents()
    return db_departments.get_department(dept_id)


_DEPARTMENT_UNRESOLVED = object()


def _department_line(
    agent_name: str, self_data: dict, delegation_targets: list[str],
    department=_DEPARTMENT_UNRESOLVED,
) -> str:
    """One compact department-membership line for the roster, or ''.

    "You are in <Dept> at level <Head>. Same-level: a; level above: b;
    level below: c." Buckets follow the department's mode × reach (the
    wired targets are the input, so they already do): under 'both' adjacent
    = one level either way and subtree = the whole department; under the
    downward modes no level above appears (the buckets are capped so a big
    mesh can't bloat the prompt) and one clause says so, since a bottom-level
    agent would otherwise see no list at all and guess. Best-effort — a
    lookup failure must never break a prompt build."""
    dept_id = self_data.get("department_id") or ""
    level_id = self_data.get("department_level_id") or ""
    if not dept_id or not level_id:
        return ""
    try:
        from storage.agents import agent_store, db_departments
        # ``department`` is the row ``get_dynamic_contexts`` pre-resolved
        # off the loop; a direct caller without it reads here.
        dept = (db_departments.get_department(dept_id)
                if department is _DEPARTMENT_UNRESOLVED else department)
        if not dept:
            return ""
        levels = dept["levels"]
        level = next((lv for lv in levels if lv["id"] == level_id), None)
        if not level:
            return ""
        targets = set(delegation_targets)
        same_level: list[str] = []
        above: list[str] = []
        below: list[str] = []
        above_ids = {lv["id"] for lv in levels if lv["rank"] < level["rank"]}
        below_ids = {lv["id"] for lv in levels if lv["rank"] > level["rank"]}
        for a in (agent_store.get_all_agents() if targets else ()):
            slug = a["slug"]
            if slug == agent_name or slug not in targets:
                continue
            if (a.get("department_id") or "") != dept_id:
                continue
            member_level = a.get("department_level_id") or ""
            if member_level == level_id:
                same_level.append(slug)
            elif member_level in above_ids:
                above.append(slug)
            elif member_level in below_ids:
                below.append(slug)
        line = (
            f"\nYou are part of the **{dept['name']}** department "
            f"at level **{level['name']}**."
        )
        wiring = db_departments.MODE_WIRING[dept["mode"]]
        if wiring.down and not wiring.up:
            line += (
                " Delegation in this department runs downward and across "
                "your own level, never upward." if wiring.peers
                else " Delegation in this department runs downward only."
            )

        def _bucket(slugs: list[str], cap: int = 15) -> str:
            # A subtree (full-mesh) department can be large — cap the listing
            # so the prompt stays compact; the wired-targets roster above
            # carries the complete set anyway.
            shown = ", ".join(f"`{s}`" for s in sorted(slugs)[:cap])
            extra = len(slugs) - cap
            return shown + (f" (+{extra} more)" if extra > 0 else "")

        parts = []
        if same_level:
            parts.append("same-level: " + _bucket(same_level))
        if above:
            parts.append("level(s) above: " + _bucket(above))
        if below:
            parts.append("level(s) below: " + _bucket(below))
        if parts:
            line += " Department delegation — " + "; ".join(parts) + "."
        return line
    except Exception:
        logger.exception("department roster line failed for %s", agent_name)
        return ""


def _delegation_mcp_context(
    agent_name: str,
    delegation_targets: list[str] | None = None,
    **kwargs: Any,
) -> str | None:
    """Inject available agents section into the prompt.

    Lists self + delegation targets with descriptions, plus each agent's
    enabled execution layers/models when the caller pre-resolved them
    (``delegation_roster`` kwarg — built off-loop by the config builders via
    ``build_delegation_roster``; this provider itself stays no-I/O beyond
    the pre-existing agent-row reads). This block is the WIRED reach
    (delegation edges) only — meetings advertise the user-access set in
    their own block (``_meetings_mcp_context``). The parallel sessions are
    not here: they change at most starts, so they ride on the start's first
    turn (``core/session/start_context.py``) and the text stays the same.

    Deliberately NO session-start notice for received files (removed
    2026-08-14 after the first live test): a stamped once-only notice is
    fragile against warmup/parallel config builds racing to consume it,
    while the prompt's workspace listing already shows
    ``workspace/inbox/<sender>/…`` on every session — presence + sender
    for free. Senders put context IN the files (README) when it matters.
    """
    from storage.agents import agent_store

    if not delegation_targets:
        # No roster to list (a bottom level under a downward mode, or a
        # person's narrowed view): the department line still tells the agent
        # its place.
        dept_line = _department_line(agent_name, agent_store.get_agent(agent_name) or {}, [],
                                     department=kwargs.get("department", _DEPARTMENT_UNRESOLVED))
        people = team_line.render(kwargs.get("team"), kwargs.get("user_sub", "") or "",
                                  kwargs.get("user_role", "") or "")
        return "\n".join(p for p in (dept_line.strip(), people) if p) or None

    roster: dict[str, list[dict]] = kwargs.get("delegation_roster") or {}

    # No-user sessions read EVERY wired target (a delegation edge is the
    # read grant — merged observe semantics); the config builders pass
    # False for user-backed sessions, which read via their user's access.
    nouser_reads = bool(kwargs.get("nouser_reads"))

    def _agent_lines(slug: str, data: dict, *, is_self: bool) -> list[str]:
        name = data.get("display_name", slug)
        desc = data.get("description", "")
        tag = " *(this is you)*" if is_self else ""
        out = [f"- **{name}** (`{slug}`){tag}{f' — {desc}' if desc else ''}"]
        layers = roster.get(slug) or []
        if layers:
            out.append(f"  layers: {_fmt_roster_layers(layers)}")
        return out

    # Build agent roster: self first, then targets. This is the WIRED set
    # (delegation edges) — the meetings block advertises the user-access set
    # separately, so the two reaches never blur.
    lines = [
        "## Available Agents\n",
        "The following agents are wired as your delegation targets"
        " (`delegate()` / `send_files()`):\n",
    ]

    # Self
    self_data = agent_store.get_agent(agent_name)
    if self_data:
        lines.extend(_agent_lines(agent_name, self_data, is_self=True))

    # Department framing (agents map): one compact line when assigned. Peer
    # lists are drawn ONLY from delegation_targets (already access-filtered
    # and edge-backed), so the prompt never claims reach that spawn authz
    # would refuse — with the department's mode off, dept-mates simply
    # don't appear. Buckets are capped inside _department_line (a subtree
    # mesh can be the whole department under mode 'both').
    dept_line = _department_line(agent_name, self_data or {}, delegation_targets,
                                 department=kwargs.get("department", _DEPARTMENT_UNRESOLVED))
    if dept_line:
        lines.append(dept_line)
    # The people the agent serves, beside the department line (team_line.py).
    people = team_line.render(kwargs.get("team"), kwargs.get("user_sub", "") or "",
                              kwargs.get("user_role", "") or "")
    if people:
        lines.append(("" if dept_line else "\n") + people)

    # Delegation targets (excluding self if present)
    for slug in delegation_targets:
        if slug == agent_name:
            continue
        data = agent_store.get_agent(slug)
        if data:
            lines.extend(_agent_lines(slug, data, is_self=False))

    lines.extend(_fmt_model_tiers(roster))
    lines.append("")
    lines.append(
        "**Delegation**: Use `delegate(agent=\"...\", surface=...)` to delegate work to another agent. "
        "Use `continue_id` with a prior task/chat id for multi-turn conversations."
    )
    if roster:
        lines.append(
            "Model/layer overrides on `delegate()` must come from the "
            "target's `layers:` line above (the `[t1]`..`[t4]` tags are the "
            "capability tiers listed under Model tiers); omit both to "
            "inherit the worker's defaults (recommended). Ignored with "
            "`continue_id`. " + ROSTER_LEGEND
        )
    lines.append(
        "`send_files(target_agent, paths, …)` copies workspace files into a "
        "target's `workspace/inbox/` — hand-off without making it act; "
        "follow with `delegate()` when it should."
    )
    if nouser_reads:
        lines.append(
            "This session runs without a user: every delegation target "
            "listed above is READABLE — pass its slug as the `agent` "
            "argument of the list tools (schedules / triggers / "
            "notifications / delegation `list_sessions`+`peek_session`) to "
            "read its scheduled tasks, run history, sessions, triggers, "
            "and notifications — agent-scope only, strictly read-only "
            "(fire/mutations stay on your own agent; `delegate()` when "
            "they should act)."
        )
    return "\n".join(lines)




# At most this many apps are named; a team with more reads list_apps.
_TEMPLATE_APPS_CAP = 12
# An app's title is the bundle's own word: one line, no backtick, at most
# this long before it reaches a prompt.
_TEMPLATE_TITLE_CAP = 80


def _prompt_word(text: str, cap: int = _TEMPLATE_TITLE_CAP) -> str:
    """``text`` as a prompt may carry it: whitespace and control
    characters collapsed to one space, backticks dropped, cut at ``cap``."""
    cleaned = "".join(" " if ch.isspace() or not ch.isprintable() else ch for ch in text)
    return " ".join(cleaned.replace("`", "").split())[:cap]


def _template_apps(agent_name: str, user_sub: str = "") -> list[dict]:
    """The agent's shared apps and the person's own that a template or a
    bundle gave: ``{title, slug, from_template, from_version}`` (sync store
    reads: call off the loop)."""
    from services.apps import app_blueprints
    from storage import database as task_store
    username = task_store.get_username_by_sub(user_sub) if user_sub else ""
    rows = task_store.list_apps(agent_name, username or "")
    out = []
    for r in rows:
        # A copy seeded before the origin columns reads its template from
        # its seed reference (origin_of).
        from_template, from_version = app_blueprints.origin_of(r)
        if from_template:
            out.append({"title": _prompt_word(str(r.get("title") or r.get("slug") or "")), "slug": r.get("slug") or "",
                        "from_template": from_template, "from_version": from_version})
    return out


def _display_mcp_context(agent_name: str, template_apps: list[dict] | None = None, **kwargs: Any) -> str | None:
    """Tell the agent which of its apps came from a template, and which
    one, so it knows the job each app was built for (app_blueprints.py)."""
    apps = template_apps or []
    if not apps:
        return None
    lines = ["## Apps from templates", "",
             "These apps came from a template. Each line names it and its version."]
    for a in apps[:_TEMPLATE_APPS_CAP]:
        version = f" {a['from_version']}" if a.get("from_version") else ""
        lines.append(f"- {_prompt_word(str(a['title']))} (`{a['slug']}`): from the `{a['from_template']}` template{version}.")
    if len(apps) > _TEMPLATE_APPS_CAP:
        lines.append(f"- and {len(apps) - _TEMPLATE_APPS_CAP} more: `list_apps` names them.")
    return "\n".join(lines)




def _schedules_mcp_context(
    agent_name: str,
    **kwargs: Any,
) -> str | None:
    """Tell the agent which models/layers its own scheduled tasks may pin.

    ``create_scheduled_task`` / ``create_one_time_task`` / ``edit_task`` accept
    a per-task ``model``/``layer`` that overrides the agent's default for that
    task alone. The valid ids are this agent's OWN enabled layers — the same
    set ``spawn_authz.validate_spawn_overrides`` enforces — which until now
    were rendered only by the delegation block. An agent with schedules-mcp
    and no delegation-mcp could not see them at all.

    Renders the agent's own ``layers:`` line from the pre-resolved
    ``delegation_roster`` (self is always in it: both config builders prepend
    the agent's own slug). When ``delegation-mcp`` is ALSO assigned, that
    block already prints the same line under *(this is you)* — emit a pointer
    instead of a second copy.
    """
    roster: dict[str, list[dict]] = kwargs.get("delegation_roster") or {}
    layers = roster.get(agent_name) or []
    if not layers:
        return None

    lines = ["## Scheduled-task Execution\n"]
    if "delegation-mcp" in (kwargs.get("assigned_mcps") or []):
        lines.append(
            "Your own `layers:` line in **Available Agents** above lists the "
            "execution layers and model ids your scheduled tasks may pin, "
            "and the Model tiers list there ranks them."
        )
    else:
        lines.append(f"Your layers: {_fmt_roster_layers(layers)}")
        lines.extend(_fmt_model_tiers({agent_name: layers}))
        lines.append("\n" + ROSTER_LEGEND)

    lines.append(
        "\nAn unpinned task runs on the engine marked `default` when a person "
        "created it (their own account, a user-scope task), and on the "
        "agent's default engine from the platform pool when it is the "
        "agent's own (an agent-scope task needs an engine tagged `platform`). "
        "`model` / `layer` on `create_scheduled_task`, `create_one_time_task` "
        "and `edit_task` pin ONE task's runs to a different model or layer, "
        "leaving the defaults untouched everywhere else — the lever for "
        "keeping a lighter default while one demanding task runs on a "
        "stronger model (or the reverse); a model alone runs on the engine "
        "the task takes unpinned when that engine offers it, else on the "
        "first engine of the agent's own order that does, which is not always "
        "the line's order (name its `layer` for an id on two lines). Pin by "
        "tier, never by how an id sounds: complex, open-ended or "
        "judgement-heavy work belongs on a tier 1 model. Values outside the "
        "list above are rejected. Ask the user before pinning unless they "
        "asked for it; `edit_task(model=\"\")` clears a pin. `get_task` reads "
        "a task back with its prompt."
    )
    return "\n".join(lines)




# Cap on agents rendered into the meetings participant list — a big install's
# full catalog belongs in the dashboard, not every system prompt.
_MEETINGS_ACCESS_CAP = 24


def _meetings_mcp_context(
    agent_name: str,
    delegation_targets: list[str] | None = None,
    **kwargs: Any,
) -> str | None:
    """Inject the meeting capability note when meetings-mcp is assigned.

    Participants follow the acting USER's access (the ``meetings_access``
    kwarg, pre-resolved off-loop by the config builders via
    ``build_meetings_access``) — the meetings API never gated on the
    delegation roster. No-user sessions have no user to derive access from;
    their reach IS the roster (the create endpoint clamps to it), so they
    fall back to the roster-derived list.
    """
    from storage.agents import agent_store as _agent_store

    access: list[dict] = kwargs.get("meetings_access") or []
    if access:
        peers = [a for a in access if a["slug"] != agent_name]
        if not peers:
            return None
        shown = peers[:_MEETINGS_ACCESS_CAP]
        lines = [
            "## Meeting Rooms\n",
            "You can start multi-agent meetings with `start_meeting(topic, agents)`.",
            "Participants follow YOUR USER'S access (their role travels with "
            "you) — not your delegation roster. You can invite:",
        ]
        for a in shown:
            desc = a["description"].strip()
            if len(desc) > 140:
                desc = desc[:140].rstrip() + "…"
            lines.append(
                f"- **{a['display_name']}** (`{a['slug']}`) ({a['role']})"
                f"{f' — {desc}' if desc else ''}"
            )
        if len(peers) > len(shown):
            lines.append(f"- …and {len(peers) - len(shown)} more (see the dashboard's agents page).")
        lines.extend([
            "",
            "Meetings are deliberate, observable communication — every turn "
            "lands in a visible transcript. To hand WORK to another agent, "
            "use `delegate()` (wired targets only), not a meeting.",
        ])
        return "\n".join(lines)

    # No-user fallback: roster-derived (the create endpoint clamps a no-user
    # session's participants to roster ∪ self).
    if not delegation_targets:
        return None
    peer_agents = [t for t in delegation_targets if t != agent_name]
    if not peer_agents:
        return None
    peer_names = []
    for slug in peer_agents:
        data = _agent_store.get_agent(slug)
        name = (data or {}).get("display_name", slug)
        peer_names.append(f"{name} (`{slug}`)")
    lines = [
        "## Meeting Rooms\n",
        "You can start multi-agent meetings with `start_meeting(topic, agents)`.",
        "This session has no user, so participants are limited to your wired "
        f"delegation targets: {', '.join(peer_names)}",
        "",
        "Meetings are deliberate, observable communication — every turn "
        "lands in a visible transcript. To hand WORK to another agent, "
        "use `delegate()`, not a meeting.",
    ]
    return "\n".join(lines)




def _ssh_hosts_context(
    agent_name: str,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    **kwargs: Any,
) -> str | None:
    """Inject the authorized SSH host list for the ssh-hosts MCP.

    Agents use plain ``ssh``/``scp``/``rsync`` from bash against
    admin-configured hosts (the MCP's one tool, ``list_ssh_hosts``, only
    re-reads this list mid-session). Each authorizing instance
    renders as a ready-to-run command line. The proxy signs through the
    session's agent socket and the key dir holds the public halves, so the
    block renders on every machine kind. A session below the editor tier
    holds no keys
    (``session_config_dir.session_takes_ssh_keys``), so the block that names
    them is not rendered.
    """
    from auth import roles
    if not roles.can_edit(kwargs.get("user_role") or ""):
        return None

    from storage.mcp import mcp_store

    instances = mcp_store.get_mcp_instances_for_agent("ssh-hosts", agent_name)
    if not instances:
        return None

    # Connection multiplexing: agent sessions burst short ssh commands, and
    # each fresh TCP connect to :22 looks like a scan to IDS gateways
    # (Suricata ET SCAN 2001219 killed a legit flow mid-command, 2026-07-06).
    # ControlMaster reuses one authenticated connection; ControlPersist keeps
    # the master ≤60s past last use so nothing authenticated outlives session
    # teardown by much. Windows OpenSSH has no unix-socket mux — omit there,
    # and omit when the machine reported no ``os`` ("" = unknown,
    # conservative — the RAW capability, not the path-shape family).
    #
    # Socket path: NOT under $OTO_SSH_KEY_DIR — on satellites that dir nests
    # in the session-secrets tree and `cm-%C` (40-hex) overflowed the 108-byte
    # sun_path limit (ssh exits 255 before connecting; hit live 2026-07-11).
    # The shell expands the chain to the OS's short PRIVATE runtime dir:
    # XDG_RUNTIME_DIR (/run/user/<uid>, 0700, systemd Linux) → TMPDIR
    # (per-user 0700 /var/folders/… on macOS) → /tmp (inside the local
    # sandbox /tmp is mount-namespaced private; the chain only lands on a
    # shared /tmp for exotic non-systemd admin-paired hosts).
    _row = host_os.of(placement.os)
    mux_capable = placement.isolates_with_bwrap or bool(_row is not None and _row.posix)
    # A machine that is no POSIX one serves the agent on a named pipe, which
    # only Windows' own ssh reads (the satellite names it as OTO_SSH).
    pipe_agent = bool(placement.is_remote and _row is not None and not _row.posix)

    keys_sentence = (
        "The platform holds the keys and signs for you through this session's "
        "SSH agent socket (`$SSH_AUTH_SOCK`). Each command names its key by the "
        "public file in `$OTO_SSH_KEY_DIR`, so use the lines as given with "
        "standard `ssh`, `scp` or `rsync`."
        + (" " + PIPE_AGENT_NOTE if pipe_agent else ""))
    lines = [
        "## SSH Hosts\n",
        "You have direct SSH access to the following admin-configured hosts "
        "from your shell. " + keys_sentence
        + " The `StrictHostKeyChecking=accept-new` option makes "
        "the first connect record the host key automatically (later connects "
        "verify against it) — reuse it on `scp`/`rsync` too."
        + (" The ControlMaster options multiplex repeated commands over one "
           "authenticated connection — reuse them too so command bursts don't "
           "open a new TCP connection each time." if mux_capable else "")
        + "\n",
    ]
    for inst in instances:
        fv = inst.get("field_values", {}) or {}
        command = format_ssh_host_command(fv, mux=mux_capable, pipe_agent=pipe_agent)
        if command is None:
            continue
        name = (fv.get("name") or "").strip() or (fv.get("host") or "").strip()
        lines.append(f"- **{name}** — `{command}`")
    if len(lines) == 2:
        return None  # every instance was missing its host
    # The block above is static prompt text rendered once at session build —
    # long sessions lose it to attention decay, so point at the queryable twin.
    lines.append(
        "\nIf you are no longer sure which hosts or keys are available, call "
        "the `list_ssh_hosts` tool — it returns this same list on demand."
    )
    return "\n".join(lines)




#: What a Windows machine's lines need said: the prompt
#: block and ``GET /v1/agents/{name}/ssh-hosts`` (the ``list_ssh_hosts``
#: answer) both carry it.
PIPE_AGENT_NOTE = (
    "On this Windows computer the lines run Windows' own ssh, `$OTO_SSH`, "
    "because the `ssh` and `scp` of Git Bash cannot reach the agent: copy "
    "files with `scp -S \"$OTO_SSH\"`."
)

_SSH_MUX_OPTS = (
    " -o ControlMaster=auto"
    ' -o ControlPath="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/oto-cm-%C"'
    " -o ControlPersist=60s"
)


def format_ssh_host_command(field_values: dict, *, mux: bool, pipe_agent: bool = False) -> str | None:
    """One authorized ssh-hosts instance → its ready-to-run ssh command.

    Single source for the SSH Hosts prompt block above AND the
    ``GET /v1/agents/{name}/ssh-hosts`` endpoint behind the ``list_ssh_hosts``
    tool — the two surfaces must never drift. Returns ``None`` when the
    instance has no host. ``mux`` gates the ControlMaster options (see the
    provider comment: no unix-socket mux on Windows / unknown targets).
    ``pipe_agent``: a Windows machine, whose session agent is a named pipe
    that only Windows' own ssh reads; the line runs it
    (``$OTO_SSH``, the satellite's), since Git Bash's ``ssh`` is Git's own.
    """
    fv = field_values or {}
    host = (fv.get("host") or "").strip()
    if not host:
        return None
    username = (fv.get("username") or "").strip()
    port = str(fv.get("port") or "22").strip() or "22"
    key_name = (fv.get("key_name") or "").strip()
    target = f"{username}@{host}" if username else host
    # The proxy signs: the dir holds the public halves, and ``-i <name>.pub``
    # with IdentitiesOnly picks that identity from the session's agent socket.
    key_part = f' -i "$OTO_SSH_KEY_DIR/{key_name}.pub" -o IdentitiesOnly=yes' if key_name else ""
    mux_part = _SSH_MUX_OPTS if mux else ""
    program = '"${OTO_SSH:-ssh}"' if pipe_agent else "ssh"
    # accept-new: without it the first connect dies on ssh's TOFU check in
    # a non-interactive shell ("Host key verification failed"). Hosts are
    # admin-configured and often reachable only from the machine the
    # session runs on, so a platform-side pre-scan can't replace this
    # (deliberate: reachability from the proxy is NOT assumed).
    return (
        f"{program}{key_part} -o StrictHostKeyChecking=accept-new"
        f"{mux_part} -p {port} {target}"
    )

# memory-mcp has no dynamic_context provider: memory reaches a session
# through the # Memory directive and the start's riding part
# (``configuration.prompts._memory_sections``, ``start_sections``), so a
# restart picks up new entries at its start.
