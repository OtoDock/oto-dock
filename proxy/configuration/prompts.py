"""The system prompt's rendering (``build_agent_prompt`` and its sections)
and the fold that drops the MCPs a remote session could not install.

``config`` re-exports the public names and imports this module, so the
agents root and the agent-name helpers (``AGENTS_DIR``, ``get_agent_dir``,
``is_safe_agent_name``) are read through ``config`` at call time: the suite
rebinds ``config.AGENTS_DIR``.
"""

import contextlib
from pathlib import Path

from auth import roles
from core import layout
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities

_DOC_EXTENSIONS = {".md", ".txt", ".markdown"}
_MAX_DOC_BYTES = 1 * 1024 * 1024       # 1 MB per file
_MAX_TOTAL_DOC_BYTES = 5 * 1024 * 1024  # 5 MB total per agent


def persona_is_unconfigured(agent_name: str) -> bool:
    """True when the agent's persona file still holds nothing but its title.

    A dashboard-created agent is seeded with ``# {display_name}\\n\\n`` and
    nothing else, and that empty state is otherwise INVISIBLE — the prompt
    builder only bails on a MISSING persona, so a title-only file renders as
    a perfectly valid (and empty) section 1. The manager-facing
    ``# Building Agents`` section uses this to say so, once, until someone
    writes the persona.

    Fail-safe: an unreadable/missing file returns False. Never nag when we
    cannot tell (a template-installed agent always has real content).
    """
    import config
    if not config.is_safe_agent_name(agent_name):
        return False
    try:
        config_dir = config.get_agent_dir(agent_name) / layout.CONFIG
    except ValueError:
        return False
    for candidate in (config_dir / "agent.md", config_dir / "prompt.md"):
        try:
            text = candidate.read_text()
        except OSError:
            continue
        # Drop a single leading H1 (the seeded title) and see what remains.
        body = text.strip()
        if body.startswith("#"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        return not body.strip()
    return False


def _read_agent_files(model: str) -> list[tuple[str, str]]:
    """Read an agent's persona (config/agent.md) and every doc under config/context/.

    ``agent.md`` is the persona file; ``prompt.md`` is its pre-1.4 name and
    stays readable so a restored old backup keeps working until the startup
    sweep (``agent_provisioning.migrate_persona_filenames``) converges it.
    When both exist (e.g. a stale satellite pushed the old file back),
    ``agent.md`` wins.

    All files under config/context/ matching `.md`/`.txt`/`.markdown` auto-load
    as context. Files over 1 MB and the tail past a 5 MB total cap are skipped;
    skipped files are listed in a synthesized `_auto_context_skipped.md` so the
    agent knows what didn't make it in.

    Returns list of (relative_path, content) tuples.
    """
    import config
    if not config.is_safe_agent_name(model):
        return []
    try:
        agent_dir = config.get_agent_dir(model)
    except ValueError:
        return []
    config_dir = agent_dir / layout.CONFIG
    persona_text: str | None = None
    # Two passes: a concurrent rename/restore can remove the probed file
    # between exists() and read_text() — re-probe both names before giving up.
    for _attempt in range(2):
        for candidate in (config_dir / "agent.md", config_dir / "prompt.md"):
            try:
                persona_text = candidate.read_text()
                break
            except OSError:
                continue
        if persona_text is not None:
            break
    if persona_text is None:
        return []

    files: list[tuple[str, str]] = [("agent.md", persona_text)]

    context_dir = agent_dir / layout.CONFIG / layout.CONTEXT
    if not context_dir.is_dir():
        return files

    candidates = sorted(
        p for p in context_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _DOC_EXTENSIONS
    )

    total_bytes = 0
    skipped: list[str] = []
    for p in candidates:
        size = p.stat().st_size
        rel = str(p.relative_to(agent_dir))
        if size > _MAX_DOC_BYTES:
            skipped.append(f"{rel} ({size} bytes — over 1 MB per-file limit)")
            continue
        if total_bytes + size > _MAX_TOTAL_DOC_BYTES:
            skipped.append(f"{rel} (5 MB total cap reached)")
            break
        files.append((rel, p.read_text(encoding="utf-8", errors="replace")))
        total_bytes += size

    if skipped:
        files.append((
            "_auto_context_skipped.md",
            "## Auto-context files skipped\n\n"
            + "\n".join(f"- {s}" for s in skipped),
        ))

    return files


# Directories to skip when scanning the workspace tree. The session-state
# / runtime-cache dirs (.claude, .codex) would only add noise; .git is a
# huge tree of plumbing the agent never needs to read directly; venv /
# node_modules / __pycache__ are dependency artefacts. ``.`` prefix dirs
# are also skipped (existing behavior).
_TREE_SKIP_DIRS: set[str] = {
    "venv", "node_modules", "__pycache__",
}

_TREE_MAX_DEPTH = 5
_TREE_MAX_BYTES = 50_000          # ~12K tokens at typical text density
_TREE_MAX_NAME_LEN = 80           # truncate excessively long filenames


def _scan_workspace(agent_dir: Path, agent_name: str, *,
                    username: str | None = None,
                    role: str = "manager",
                    sandboxed: bool = True,
                    mount_shared: bool = True,
                    external_home: str = "") -> str | None:
    """Scan an agent's directories and return a formatted tree.

    Returns ``None`` if no folder is visible to this session. Otherwise
    returns a multi-line string with directories AND files, sorted
    dirs-first within each level, max depth ``_TREE_MAX_DEPTH``,
    truncated when the total rendered text exceeds ``_TREE_MAX_BYTES``.

    Role-based visibility mirrors ``# Folders`` in the permission context:

    - viewer (user-scope): users/{own}/ + workspace/ (RO) + knowledge/ (RO)
    - editor (user-scope): users/{own}/ + workspace/ (RW) + knowledge/ (RO)
    - manager/admin (user-scope): users/{own}/ + workspace/ + knowledge/
      + config/
    - agent-scoped (no username): workspace/ + knowledge/

    When ``sandboxed=True``, root paths use sandbox-relative prefixes
    (``/workspace/`` etc.) — what the CLI sees inside bwrap.
    """
    lines: list[str] = []
    rendered_bytes = 0
    truncated_count = 0  # entries past the cap we couldn't fit

    def _trim_name(name: str) -> str:
        if len(name) <= _TREE_MAX_NAME_LEN:
            return name
        return name[:_TREE_MAX_NAME_LEN - 1] + "…"

    def _walk(current: Path, depth: int, indent: int) -> None:
        nonlocal rendered_bytes, truncated_count
        if depth > _TREE_MAX_DEPTH:
            return
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            return
        # Filter: skip hidden + denylisted dirs; keep everything else.
        visible = [
            e for e in entries
            if not e.name.startswith(".") and e.name not in _TREE_SKIP_DIRS
        ]
        # Sort: directories first, then files; both alphabetical.
        dirs = sorted([e for e in visible if e.is_dir()], key=lambda p: p.name)
        files = sorted([e for e in visible if e.is_file()], key=lambda p: p.name)
        for d in dirs:
            line = f"{'  ' * indent}{_trim_name(d.name)}/"
            if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
                truncated_count += 1
                continue
            lines.append(line)
            rendered_bytes += len(line) + 1
            _walk(d, depth + 1, indent + 1)
        for f in files:
            line = f"{'  ' * indent}{_trim_name(f.name)}"
            if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
                truncated_count += 1
                continue
            lines.append(line)
            rendered_bytes += len(line) + 1

    def _emit_root(rel_label: str, root: Path) -> None:
        nonlocal rendered_bytes
        if not root.is_dir():
            return
        line = rel_label
        if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
            return
        lines.append(line)
        rendered_bytes += len(line) + 1
        _walk(root, 1, 1)

    # An external caller's own tree (mounted at /caller) comes first.
    if external_home:
        for sub in layout.USER_SUBDIRS:
            _emit_root(f"/caller/{sub}/", Path(external_home) / sub)
    # Agent-scope (no username): /workspace/ + /knowledge/
    if not username:
        _emit_root(f"{layout.V_WORKSPACE}/", agent_dir / layout.WORKSPACE)
        _emit_root(f"{layout.V_KNOWLEDGE}/", agent_dir / layout.KNOWLEDGE)
    else:
        # User-scope: own user dir + shared spaces + (manager+) config
        user_dir = layout.user_dir(agent_dir, username)
        if user_dir.is_dir():
            for sub in layout.USER_SUBDIRS:
                _emit_root(f"{layout.virtual_user_root(username)}/{sub}/", user_dir / sub)
        # Shared workspace + knowledge only when the agent's mode has them
        # (Personal-only omits both — they aren't mounted in that mode).
        if mount_shared:
            _emit_root(f"{layout.V_WORKSPACE}/", agent_dir / layout.WORKSPACE)
            _emit_root(f"{layout.V_KNOWLEDGE}/", agent_dir / layout.KNOWLEDGE)
        if roles.can_manage(role):
            _emit_root(f"{layout.V_CONFIG}/", agent_dir / layout.CONFIG)

    if truncated_count:
        lines.append(
            f"… ({truncated_count} more entries not shown — tree truncated "
            f"at ~{_TREE_MAX_BYTES // 1000}KB)"
        )

    return "\n".join(lines) if lines else None


# Memory injection: while a scope's topic files total ≤ the admin
# `inline_budget_bytes` (memory_settings, default 8 KB) they inject in FULL;
# past it the section degrades to the generated MEMORY.md index + the
# `memory` tool's `view`. A hand-edited single file above the per-file cap
# is skipped with a note (tool writes can't produce one).
_MEMORY_INLINE_FILE_HARD_CAP = 64 * 1024


def _render_memory_scope(root, *, virtual: str, budget: int,
                         writable: bool = True) -> str:
    """One scope's body: inline topics, index-only, or empty-state priming."""
    from services.memory import memory_file

    topics = memory_file.iter_topic_files(root)
    if not topics:
        if writable:
            return (
                f"_No memories saved yet. Create the first topic with the "
                f"`memory` tool: `create {virtual}/<topic>.md`._"
            )
        return "_No memories saved yet._"
    total = sum(p.stat().st_size for p in topics)
    out: list[str] = []
    if total <= budget:
        for p in topics:
            rel = p.relative_to(root).as_posix()
            if p.stat().st_size > _MEMORY_INLINE_FILE_HARD_CAP:
                out.append(f"### `{rel}`\n\n_(skipped — file over 64 KB; "
                           f"read it with the `memory` tool)_")
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace").rstrip()
            except OSError:
                continue
            out.append(f"### `{rel}`\n\n{content}")
    else:
        # Over budget → index only. Heal first so hand-edits are reflected.
        memory_file.heal_index_if_stale(root)
        index_path = root / memory_file.INDEX_FILENAME
        try:
            index = index_path.read_text(encoding="utf-8", errors="replace").rstrip()
        except OSError:
            index = memory_file.build_index_content(root).rstrip()
        out.append(index)
        out.append(
            f"_(memory over the inline budget — index only; read a topic "
            f'with `memory(command="view", path="{virtual}/<topic>")`)_'
        )
    return "\n\n".join(out)


def _render_memory_sections(model: str, agent_dir: Path, *,
                            username: str | None, role: str,
                            external: bool = False,
                            external_home: str = "") -> str | None:
    """The `# Memory` prompt section: capture directive + per-scope content.

    Scope loading matrix: user-scoped sessions get agent + user memory;
    agent-scoped sessions (no username) get agent memory only. A viewer's
    agent scope is read-only. An EXTERNAL session (a phone caller who is not
    a platform user) never gets the agent memory; with a caller tree
    (``external_home``) the caller's own memory is its user scope — same
    toggles and mode clamp as a user's. Returns None when memory is fully
    disabled.
    """
    from services.memory import memory_file
    from storage.agents import agent_store, memory_store

    settings = memory_store.get_settings()
    toggles = memory_store.get_agent_toggles(model)
    agent_row = agent_store.get_agent(model) or {}
    # Visibility-modes: Personal-only has no agent memory; Shared-only has no
    # user memory. Clamp availability to the agent's mode, on top of the
    # master + per-agent toggles. (Shared-only also has username="" here, which
    # already zeroes user memory; the mode check is belt-and-braces.)
    _collaborative = bool(agent_row.get("collaborative", True))
    _row_scope = agent_row.get("default_scope") or "user"
    _mode_agent_ok = _collaborative or _row_scope == "agent"   # agent scope exists
    _mode_user_ok = _collaborative or _row_scope == "user"     # user scope exists
    agent_enabled = bool(
        settings.get("agent_memory_enabled")
        and toggles.get("agent_memory_enabled")
        and _mode_agent_ok
        and not external
    )
    user_enabled = bool(
        (external_home if external else username)
        and settings.get("user_memory_enabled")
        and toggles.get("user_memory_enabled")
        and _mode_user_ok
    )
    if not (agent_enabled or user_enabled):
        return None

    budget = int(settings.get("inline_budget_bytes") or 8192)

    parts: list[str] = ["\n\n---\n\n# Memory\n"]

    default_scope = agent_row.get("default_scope") or (
        "user" if user_enabled else "agent"
    )
    if default_scope == "user" and not user_enabled:
        default_scope = "agent"
    if default_scope == "agent" and not agent_enabled:
        default_scope = "user"

    scope_lines = []
    if agent_enabled:
        scope_lines.append(
            "- `/memories/agent/` — shared with every user of this "
            "agent: operational facts, conventions, workflows, shared "
            "project state."
        )
    if user_enabled and external:
        scope_lines.append(
            "- `/memories/user/` — private to this caller: their "
            "preferences, context, facts about them. Nothing else on this "
            "line is remembered."
        )
    elif user_enabled:
        scope_lines.append(
            f"- `/memories/user/` — private to {username}: their "
            "preferences, context, facts about them."
        )
    directive = (
        "\nYou have persistent memory that survives across sessions — "
        "markdown topic files you manage with the `memory` tool:\n"
        + "\n".join(scope_lines)
        + f"\n\nDefault scope for new memories: `/memories/{default_scope}/`."
        " **Save the moment you learn something durable** — don't wait "
        "for the end of the session: decisions, infrastructure facts, "
        "conventions, preferences, corrections you receive, anything a "
        "future session will need. When the user says \"remember "
        "this\", save it.\n\n"
        "**Maintain, don't accumulate**: the content below is your "
        "current memory — update existing topics (`str_replace`) "
        "instead of adding duplicates, merge related facts into one "
        "topic, delete entries that turn out wrong, date entries "
        "(YYYY-MM-DD), and supersede outdated facts (\"was X until "
        "DATE; now Y\"). Prefer targeted edits — never rewrite a whole "
        "topic from scratch when a small edit will do. Start each "
        "topic file with a one-line `# heading` (it becomes its index "
        "entry; `MEMORY.md` is auto-generated — never edit it). Never "
        "store secrets, credentials, or tokens.\n"
    )
    # The shared memory is the editor tier's to write (the memory API's
    # rule); a viewer or a contributor reads it and saves user-scope only.
    agent_writable = roles.can_edit(role)
    if not agent_writable and agent_enabled:
        directive += (
            "\nAgent memory is read-only for your role — save "
            "user-scope memories only.\n"
        )
    parts.append(directive)

    if agent_enabled:
        root = memory_file.scope_root(agent_dir, "agent")
        parts.append(
            "\n## Agent memory (shared)\n\n"
            + _render_memory_scope(
                root, virtual="/memories/agent", budget=budget,
                writable=agent_writable,
            )
            + "\n"
        )
    if user_enabled and external:
        root = Path(external_home) / layout.CONTEXT / "memory"
        parts.append(
            "\n## Caller memory (private to this caller)\n\n"
            + _render_memory_scope(root, virtual="/memories/user", budget=budget)
            + "\n"
        )
    elif user_enabled:
        root = memory_file.scope_root(agent_dir, "user", username)
        parts.append(
            f"\n## User memory ({username})\n\n"
            + _render_memory_scope(root, virtual="/memories/user", budget=budget)
            + "\n"
        )
    return "".join(parts)


# Per-bulletin injection cap. Bulletins are prompt-level context — a short
# team briefing, not a document dump.
_BULLETIN_MAX_BYTES = 4 * 1024
# Read-side soft warning. Bulletin edits are plain file writes (no tool
# result to warn through, unlike memory's write-time soft limit), so the
# injected section itself tells every attached agent when pruning is due —
# any RW-attached agent can act on it.
_BULLETIN_SOFT_WARN_BYTES = 3 * 1024


def _read_bulletin(source_agent: str, sub_rel: str) -> str:
    """The SOURCE-resident bulletin's text, capped at 4 KB and truncated on
    a UTF-8 character boundary (decode drops a split trailing sequence).
    '' when the file is missing/unreadable — injection then skips it."""
    import config
    path = config.get_agent_dir(source_agent) / layout.KNOWLEDGE / sub_rel
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        data = path.read_bytes()
    except OSError:
        return ""
    truncated = len(data) > _BULLETIN_MAX_BYTES
    text = data[:_BULLETIN_MAX_BYTES].decode("utf-8", errors="ignore").strip()
    if truncated and text:
        text += ("\n\n_(bulletin truncated at 4 KB — anything below the cap "
                 "never reaches agents; writers should prune older entries)_")
    elif len(data) > _BULLETIN_SOFT_WARN_BYTES and text:
        text += (f"\n\n_(this bulletin is {len(data) / 1024:.1f} KB of the "
                 "4 KB injected cap — writers should prune older entries)_")
    return text


def _render_library_bulletins(agent_name: str) -> str | None:
    """The ``# Library Bulletins`` prompt section — one subsection per
    shared library this agent is ATTACHED to or is the SOURCE of, reading
    the source-resident ``bulletin/<library name>.md`` only (writable
    attachments may author the bulletin since 1.5 — those edits ARRIVE
    here via the projector's normal mirror→source adoption, so the source
    copy stays the single read point). Default-on convention: no flag,
    missing file = no subsection."""
    from storage.knowledge import db_knowledge_libraries
    # Each entry names the library's source, its subdir, its name, whether
    # it is the agent's own and whether it is writable.
    entries: list[tuple[str, str, str, bool, bool]] = []
    for a in db_knowledge_libraries.attachments_for_consumer(agent_name):
        entries.append((a["source_agent"], a["subdir"] or "",
                        a["name"] or "", False, bool(a.get("writable"))))
    for lib in db_knowledge_libraries.libraries_of(agent_name):
        entries.append((agent_name, lib["subdir"] or "",
                        lib["name"] or "", True, False))
    sections: list[str] = []
    for source, subdir, name, own, writable in entries:
        rel = db_knowledge_libraries.bulletin_rel(subdir, name)
        if not rel:
            continue
        text = _read_bulletin(source, rel)
        if not text:
            continue
        if own:
            origin = "this agent's own library"
            hint = f"\n_File: `{layout.V_KNOWLEDGE}/{rel}`._"
        elif writable:
            mount = (f"{layout.V_KNOWLEDGE}/shared/{source}/{subdir}/" if subdir
                     else f"{layout.V_KNOWLEDGE}/shared/{source}/")
            origin = f"from the **{source}** agent"
            hint = (f"\n_Writable attachment — publish updates at "
                    f"`{mount}bulletin/{name}.md`._")
        else:
            origin = f"from the **{source}** agent"
            hint = ""
        sections.append(f"## {name} ({origin}){hint}\n\n{text}")
    if not sections:
        return None
    return (
        "\n\n---\n\n# Library Bulletins\n\n"
        "Each shared knowledge library carries a bulletin — a small runtime "
        "broadcast file auto-loaded into the context of every agent attached "
        "to that library, at every session start. Teams use it to pass "
        "daily/weekly progress and \"what changed\" notes between agents "
        "automatically; it is NOT a memory — keep durable facts in memory or "
        "knowledge files, and keep the bulletin short (only its first 4 KB "
        "is injected): prune superseded entries whenever you update it.\n\n"
        + "\n\n".join(sections)
    )


def build_agent_prompt(model: str, *,
                       username: str | None = None,
                       role: str = "manager",
                       excluded_mcps: dict[str, str] | None = None,
                       dynamic_contexts: list[tuple[str, str]] | None = None,
                       sandboxed: bool = True,
                       client_type: str = "",
                       placement: PlacementCapabilities = LOCAL_PLACEMENT,
                       mount_shared: bool = True,
                       execution_path: str = "",
                       skip_http_mcps: bool = False,
                       external: bool = False,
                       external_home: str = "",
                       user_role: str | None = None) -> str | None:
    """Build the full system prompt for an agent.

    ``user_role`` is the person's effective role on the agent when the
    caller knows it: the inline skills and the skill catalog drop a skill
    whose ``audience`` that role fails (``mcp_registry.skill_audience_admits``),
    and the MCP catalog an MCP whose ``audience`` it fails (``audience_admits``).
    ``None`` filters nothing (a task, a phone call, a caller with no person).

    ``external`` = an EXTERNAL session (a phone caller who is not a platform
    user): the MCP catalog, the inline skills and the skill catalog drop the
    MCPs such a session never attaches (``mcp_registry.session_exclusion_reason``),
    the "Unavailable Tools" list omits the external rules themselves, and the
    shared agent memory is never injected. ``external_home`` (the caller's
    own tree, mounted at /caller) makes the caller's memory the user scope
    and auto-loads the caller's context docs — the per-user shape, re-rooted.

    Section order:

      1. Agent's own ``config/agent.md`` (the persona)
      2. Platform company context (DB) + universal language rule
      3. ``# Auto-Loaded Documentation`` — ``config/context/*`` knowledge files
      4. ``# Available Tools (MCPs)`` — catalog of enabled MCPs (one-liners)
      5. ``# MCP Tool Skills`` — per-MCP deep-dive docs from manifests
      5b. ``# Skills`` — on-demand skill catalog (Direct-LLM sessions only)
      6. ``# MCP Dynamic Context`` — runtime-generated context per MCP
      7. ``# User Context (Personal)`` — ``users/{u}/context/*`` personal docs
      8. ``# Workspace`` — live directory tree (role-filtered)
      9. ``# Excluded MCPs`` — only when tools are unavailable
      10. ``# Repository Instructions`` — the instruction files of the
          repositories in the person's own workspace, for an engine that
          declares ``behaviour.repository_instruction_files``

    Identity / scope / folder / permission sections are appended later by
    ``auth.path_policy.build_permission_context`` from the per-session
    config builders (config_builder / task_config_builder / phone_config_builder).

    Args:
        model: Agent name.
        username: Filesystem-safe username slug. When set, creates per-user dirs
                  and loads user-context files.
        role: User role (viewer/editor/manager/admin). Controls which
              directories appear in the workspace tree listing.
        excluded_mcps: {mcp_name: reason} dict of unavailable MCPs.
        dynamic_contexts: list of (mcp_name, markdown) tuples from
            ``services.mcp.dynamic_context.get_dynamic_contexts``.
        sandboxed: when True, paths in the prompt use sandbox-relative
            prefixes (``/workspace/``, ``/users/{u}/``).
        client_type: a session kind's name (``core/session/session_kind.py``:
            the manifests' ``exclude_from`` word) or ``""``. Drives MCP catalog filtering against
            each manifest's ``exclude_from``. Empty string skips the filter
            (defense-only — upstream usually pre-filters).
        placement: the session's resolved placement (``core.placement``),
            forwarded to the MCP catalog + skill loaders so a device-local
            MCP (computer / browser / app control) only appears in the prompt
            when the session can actually run it — i.e. on a satellite that
            has granted the capability. The local default keeps
            ``satellite_only`` / device-capability
            MCPs out of local-session prompts.
        execution_path: ``"claude-code-cli"`` / ``"codex-cli"`` /
            ``"direct-llm"`` / ``""``. Only ``"direct-llm"`` changes the
            output: it appends the ``# Skills`` catalog (the CLI engines index
            their materialized skills dir themselves).
        skip_http_mcps: leave sidecar (HTTP-transport) MCPs out of the MCP
            catalog, the inline skills and the skill catalog — a phone
            Direct-LLM session never connects them, so the prompt must not
            describe tools it cannot reach.
    """
    import config
    if not config.is_safe_agent_name(model):
        return None  # unsafe agent name → treat as unknown agent
    own_files = _read_agent_files(model)
    if not own_files:
        return None

    parts = []

    # Agent's own persona (agent.md — always first)
    parts.append(own_files[0][1])

    # Platform company context (from DB — admin-configured) + universal
    # language rule. The language rule is functional (the agent needs to
    # mirror the user's language) and emitted regardless of branding.
    # When ``company_name`` is empty, we deliberately skip the branding
    # block — unbranded deployments don't get an unsolicited marketing
    # line. The DB read can fail during startup (schema not yet migrated)
    # or in tests with no DB; in either case the language rule still ships.
    _LANGUAGE_RULE = (
        "Respond in the same language the user uses unless they ask you to switch."
    )
    company_name = ""
    instructions = ""
    with contextlib.suppress(Exception):
        from storage import database as _db
        company_name = _db.get_platform_setting("company_name") or ""
        instructions = _db.get_platform_setting("platform_instructions") or ""
    if company_name:
        parts.append(f"\n\n---\n\n# {company_name}\n")
        if instructions:
            parts.append(instructions)
        parts.append(f"\n\n{_LANGUAGE_RULE}")
    else:
        parts.append(f"\n\n---\n\n{_LANGUAGE_RULE}")

    # Agent's own extra docs
    if len(own_files) > 1:
        parts.append(
            "\n\n---\n\n"
            "# Auto-Loaded Documentation\n\n"
            "These files are pre-loaded. Do NOT read them from disk.\n"
        )
        for rel_path, content in own_files[1:]:
            parts.append(f"\n## `{rel_path}`\n\n{content}")

    agent_dir = config.get_agent_dir(model)

    # Auto-create per-user directories
    if username:
        with contextlib.suppress(Exception):
            user_dir = layout.user_dir(agent_dir, username)
            for sub in layout.USER_SUBDIRS:
                (user_dir / sub).mkdir(parents=True, exist_ok=True)

    # Available Tools (MCPs) catalog — one-line summary per enabled MCP.
    # Sits BEFORE MCP Tool Skills so the agent gets a top-down map of its
    # capabilities before diving into per-MCP usage docs. Filtered by
    # client_type against each manifest's exclude_from (defense in depth —
    # build_session_mcp_config already excludes the right ones, but this
    # keeps the prompt's catalog in sync with the actual loaded set).
    # mcp_registry not yet initialized (startup race) → no catalog.
    with contextlib.suppress(Exception):
        from services.mcp import mcp_registry
        catalog = mcp_registry.build_available_mcps_section(
            model, context=client_type or "",
            placement=placement,
            skip_http_mcps=skip_http_mcps,
            external=external,
            # The person's role: an MCP whose audience is above it is not
            # listed, as the session config leaves it out.
            user_role=user_role,
        )
        if catalog:
            parts.append("\n\n---\n\n" + catalog)

    # Load MCP skills for this agent (from manifest-driven skill system).
    # The real client_type threads through so skill-level exclude_from
    # (e.g. voiceover's ["phone"]) is honored — it was dead at this call
    # site until 2026-07 (hardcoded "system_prompt" matched nothing).
    # mcp_registry not yet initialized (startup race) → no skills section.
    with contextlib.suppress(Exception):
        from services.mcp import mcp_registry
        skills = mcp_registry.get_skills_for_agent(
            model, context=client_type or "",
            placement=placement,
            skip_http_mcps=skip_http_mcps,
            external=external,
            user_role=user_role,
        )
        # Only "always" skills inline; "on_demand" skills reach CLI sessions
        # as materialized skill folders (core/sandbox/skills_materializer)
        # where the CLI's own progressive disclosure indexes them, and are
        # deliberately absent on direct-llm (no Read tool to follow a body).
        inline = [(sid, body) for sid, body, loading in skills
                  if loading == "always"]
        if inline:
            parts.append(
                "\n\n---\n\n"
                "# MCP Tool Skills\n\n"
                "The following tool instructions are auto-loaded from active MCP servers.\n"
            )
            for _skill_id, content in inline:
                parts.append(f"\n{content}")

    # On-demand skill catalog — only for an engine whose skills are delivered
    # as a prompt catalog (Direct LLM's client-side `Skill` builtin loads what
    # the prompt lists, one line each). The CLI engines index the
    # materialized skills dir themselves with their own Skill tool.
    if _skills_delivery(execution_path) == "prompt_catalog":
        with contextlib.suppress(Exception):
            from services.mcp import mcp_registry
            skill_catalog = mcp_registry.get_skill_catalog_for_agent(
                model, context=client_type or "",
                placement=placement,
                skip_http_mcps=skip_http_mcps,
                external=external,
                user_role=user_role,
            )
            if skill_catalog:
                parts.append(
                    "\n\n---\n\n"
                    "# Skills\n\n"
                    "On-demand skills for this session. Load one with the "
                    "`Skill` tool (its full instructions come back as the "
                    "result) before doing that kind of work:\n\n"
                )
                parts.append("\n".join(
                    f"- `{sid}` — {desc}" if desc else f"- `{sid}`"
                    for sid, desc in skill_catalog
                ))

    # Dynamic MCP context (runtime-generated, only for assigned MCPs)
    if dynamic_contexts:
        parts.append("\n\n---\n\n# MCP Dynamic Context\n")
        for _mcp_name, content in dynamic_contexts:
            parts.append(f"\n{content}")

    # Load user-context files (per-user, per-agent personal docs).
    # Size budget enforced here so a runaway pasted markdown can't blow
    # the prompt context. Mirrors the
    # _MAX_DOC_BYTES / _MAX_TOTAL_DOC_BYTES pattern in _read_agent_files.
    _USER_CTX_MAX_FILE_BYTES = 256 * 1024     # 256 KB per file
    _USER_CTX_MAX_TOTAL_BYTES = 1024 * 1024   # 1 MB across all user-ctx files
    # An external caller's context docs (their tree's context/) load the same
    # way — the per-user shape, re-rooted.
    user_ctx_dir = None
    if external_home:
        user_ctx_dir = Path(external_home) / layout.CONTEXT
    elif username:
        user_ctx_dir = layout.context_dir(agent_dir, username)
    if user_ctx_dir is not None:
        if user_ctx_dir.is_dir():
            ctx_files = sorted(user_ctx_dir.glob("*.md"))
            if ctx_files:
                parts.append(
                    "\n\n---\n\n"
                    + ("# Caller Context\n\n"
                       "These notes about the current caller are loaded for "
                       "this caller only.\n"
                       if external_home else
                       "# User Context (Personal)\n\n"
                       "These personal documents are loaded for the current user only.\n")
                )
                running_bytes = 0
                skipped: list[tuple[str, int, str]] = []  # (name, size, reason)
                for md_file in ctx_files:
                    try:
                        size = md_file.stat().st_size
                    except OSError:
                        continue
                    if size > _USER_CTX_MAX_FILE_BYTES:
                        skipped.append((md_file.name, size, "exceeds per-file cap"))
                        continue
                    if running_bytes + size > _USER_CTX_MAX_TOTAL_BYTES:
                        skipped.append((md_file.name, size, "exceeds total cap"))
                        continue
                    with contextlib.suppress(Exception):
                        parts.append(
                            f"\n## `{md_file.name}`\n\n{md_file.read_text()}"
                        )
                        running_bytes += size
                if skipped:
                    skipped_md = "\n".join(
                        f"- `{n}` ({sz:,} bytes): {reason}"
                        for n, sz, reason in skipped
                    )
                    parts.append(
                        "\n## `_user_context_skipped.md`\n\n"
                        "The following user-context files were skipped due to "
                        "size limits.\n\n"
                        + skipped_md
                    )

    # Memory — agent-maintained topic files (knowledge/memory/ shared,
    # users/{u}/context/memory/ per-user) with the capture directive. The
    # generic context loaders above never see these dirs (user-context glob
    # is flat; knowledge/ isn't auto-loaded) — this is their ONLY injection
    # point. Best-effort: a memory failure never breaks prompt build.
    with contextlib.suppress(Exception):
        memory_section = _render_memory_sections(
            model, agent_dir, username=username, role=role,
            external=external, external_home=external_home,
        )
        if memory_section:
            parts.append(memory_section)

    # Shared-library bulletins — the SOURCE-resident
    # ``<subtree>/bulletin/<library name>.md`` of every library this agent
    # is attached to or shares, read server-side so personal-only consumers
    # and agent-scope sessions get it too. Best-effort like memory.
    with contextlib.suppress(Exception):
        bulletin_section = _render_library_bulletins(model)
        if bulletin_section:
            parts.append(bulletin_section)

    # Workspace and user directory listing
    ws_listing = _scan_workspace(agent_dir, model, username=username, role=role,
                                 sandboxed=sandboxed, mount_shared=mount_shared,
                                 external_home=external_home)
    if ws_listing:
        parts.append(
            "\n\n---\n\n"
            "# Workspace\n\n"
            "Available directories:\n\n"
            f"```\n{ws_listing}\n```\n"
        )
        # Path guidance uses sandbox-relative paths
        if external_home:
            parts.append("\nFiles for this caller go to `/caller/workspace/`.")
            if roles.can_write_workspace(role):
                parts.append(" Shared output goes to `/workspace/`.")
        elif external and not roles.can_write_workspace(role):
            parts.append("\nThis session has no writable folder.")
        elif username:
            parts.append(f"\nUser files go to `{layout.virtual_workspace(username)}/`.")
            if roles.can_write_workspace(role):
                parts.append(" Agent-scoped output goes to `/workspace/`.")
        else:
            parts.append("\nAgent-scoped output goes to `/workspace/`.")

    # Excluded MCPs section (tools unavailable in this session). On an
    # external session the entries that ARE the external rules are left
    # out: a caller-facing prompt must not enumerate the platform's
    # management surface (there is nothing the model could "configure").
    listed_exclusions = dict(excluded_mcps or {})
    if external and listed_exclusions:
        with contextlib.suppress(Exception):
            from services.mcp.mcp_registry import (
                EXTERNAL_CONTEXT_REASON,
                EXTERNAL_DENIED_REASON,
            )
            listed_exclusions = {
                k: v for k, v in listed_exclusions.items()
                if v not in (EXTERNAL_DENIED_REASON, EXTERNAL_CONTEXT_REASON)
            }
    if listed_exclusions:
        parts.append(_UNAVAILABLE_HEAD)
        for mcp_name, reason in sorted(listed_exclusions.items()):
            parts.append(_unavailable_row(mcp_name, reason))
        parts.append(_UNAVAILABLE_TAIL)

    # The instruction files of the repositories in the person's own
    # workspace, for an engine that does not load a nested repository's
    # files itself (services/agents/repository_instructions: never the
    # shared workspace, never an agent-scope or external session). LAST, so
    # the platform's own headings above are found first, and under the
    # ceiling an engine that cuts a long prompt from its end needs.
    instruction_files = _repository_instruction_files(execution_path)
    if instruction_files and username and not external and not external_home:
        with contextlib.suppress(Exception):
            from services.agents import repository_instructions as repo_instructions
            budget = min(repo_instructions.BUDGET_BYTES,
                         repo_instructions.PROMPT_CEILING_BYTES - len("\n".join(parts).encode()))
            section = repo_instructions.render(
                model, layout.scope_workspace(username), layout.virtual_workspace(username),
                instruction_files, budget=budget)
            if section:
                parts.append("\n\n---\n\n" + section)

    return "\n".join(parts)


_UNAVAILABLE_HEADING = "# Unavailable Tools"
_UNAVAILABLE_HEAD = (
    "\n\n---\n\n"
    f"{_UNAVAILABLE_HEADING}\n\n"
    "The following MCP servers are NOT available in this session:\n"
)
_UNAVAILABLE_TAIL = (
    "\nDo not attempt to use tools from these servers. "
    "If the user asks about them, explain they need to be configured first."
)
_AVAILABLE_HEADING = "# Available Tools (MCPs)"
_SKILLS_HEADING = "# MCP Tool Skills"


def _unavailable_row(mcp_name: str, reason: str) -> str:
    return f"- **{mcp_name}**: {reason}"


def _always_skill_bodies(mcp_name: str) -> list[str]:
    """The inline ("always") skill bodies an MCP contributes to a prompt, as
    ``build_agent_prompt`` renders them; empty when the registry has no such
    manifest."""
    try:
        from services.mcp import mcp_manifest_parse, mcp_registry
        from services.mcp.skill_format import strip_frontmatter
        manifest = mcp_registry.get_manifest(mcp_name)
        if manifest is None:
            return []
        out = []
        for skill_def in manifest.skills:
            if skill_def.loading != "always":
                continue
            path = mcp_manifest_parse.resolve_skill_file(manifest.mcp_dir, skill_def.file)
            if path is not None:
                out.append(strip_frontmatter(path.read_text()))
        return out
    except (ImportError, OSError, ValueError):
        return []


def fold_unavailable_mcps(prompt: str, unavailable: dict[str, str]) -> str:
    """A prompt built before a remote session's MCP sync, made true after it.

    The prompt is rendered before the satellite installs the session's MCPs;
    a server it could not install is stripped from the CLI's config, so the
    prompt must stop offering it: its "Available Tools (MCPs)" row and its
    inline skill bodies go, and it is listed with its reason under
    "Unavailable Tools" (the block is added when the prompt has none)."""
    if not unavailable or not prompt:
        return prompt
    lines = prompt.split("\n")
    out: list[str] = []
    in_catalog = False
    for line in lines:
        if line == _AVAILABLE_HEADING:
            in_catalog = True
        elif line == "---":
            in_catalog = False
        if in_catalog and line.startswith("- **") and any(
                f"(`{name}`)" in line for name in unavailable):
            continue
        out.append(line)
    text = "\n".join(out)
    skills_at = text.find(_SKILLS_HEADING)
    if skills_at >= 0:
        head, tail = text[:skills_at], text[skills_at:]
        for name in unavailable:
            for body in _always_skill_bodies(name):
                tail = tail.replace("\n" + body, "", 1)
        text = head + tail
    rows = [_unavailable_row(name, reason) for name, reason in sorted(unavailable.items())]
    head_at = text.find(_UNAVAILABLE_HEADING)
    if head_at < 0:
        return text + "\n" + "\n".join([_UNAVAILABLE_HEAD, *rows, _UNAVAILABLE_TAIL])
    tail_at = text.find(_UNAVAILABLE_TAIL, head_at)
    if tail_at < 0:
        return text + "\n" + "\n".join(rows)
    return text[:tail_at] + "\n".join(rows) + "\n" + text[tail_at:]


def _skills_delivery(layer: str) -> str:
    """The engine's ``behaviour.skills_delivery`` ("materialized_dir" for an
    unregistered or empty id — nothing is appended to the prompt)."""
    try:
        from core.session.session_manager import get_layer_capabilities
        caps = get_layer_capabilities(layer or "")
    except ImportError:
        return "materialized_dir"
    return caps.behaviour.skills_delivery if caps else "materialized_dir"


def _repository_instruction_files(layer: str) -> tuple[str, ...]:
    """The engine's ``behaviour.repository_instruction_files`` (``()`` for
    an unregistered or empty id — nothing is lifted into the prompt)."""
    try:
        from core.session.session_manager import get_layer_capabilities
        caps = get_layer_capabilities(layer or "")
    except ImportError:
        return ()
    return tuple(caps.behaviour.repository_instruction_files) if caps else ()
