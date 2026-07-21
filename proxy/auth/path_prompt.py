"""System-prompt context builders.

Renders the identity / execution-scope / execution-environment / folders /
building-agents sections of an agent session's system prompt. The public entry
point is :func:`build_permission_context`; the security-context type it reads
lives in ``auth.path_policy``.
"""

from auth.path_policy import SecurityContext


# ---------------------------------------------------------------------------
# Permission context for system prompt
# ---------------------------------------------------------------------------


def _build_identity_section(ctx: SecurityContext) -> str:
    """Build the session identity context.

    One-liner that states who the user is and their per-agent role. Folder
    permissions live in ``_build_folders_section`` so this block no longer
    repeats them. Agent-scope sessions (no ``ctx.username``) skip this
    block entirely — there's no human identity to introduce.
    """
    if not ctx.username:
        return ""
    name = ctx.display_name or ctx.username
    email_part = f" ({ctx.email})" if ctx.email else ""
    role_label = ctx.role if ctx.role in ("admin", "manager", "editor", "viewer") else "viewer"
    admin_suffix = " — this is an admin-only agent" if ctx.is_admin_agent else ""
    return (
        "\n\n---\n\n"
        "# Session Context\n\n"
        f"You are assisting **{name}**{email_part}; "
        f"their per-agent role is **{role_label}** on the `{ctx.agent}` agent{admin_suffix}.\n"
    )


# Mapping from scope-aware MCP slugs to the noun used in the Execution
# Scope sentence. Only MCPs that actually create scope-tagged things
# belong here — display-mcp / file-tools / etc. have nothing to scope.
_SCOPE_AWARE_MCP_NOUNS: dict[str, str] = {
    "schedules-mcp": "tasks",
    "delegation-mcp": "delegated sessions",
    "notifications-mcp": "notifications",
    "triggers-mcp": "triggers",
    "meetings-mcp": "meetings",
    "memory-mcp": "memories",
}


def _scope_aware_nouns(assigned_mcp_names: tuple[str, ...] | list[str]) -> list[str]:
    """Return the list of scope-aware nouns for the enabled MCPs.

    Preserves the canonical noun order (tasks → delegated sessions → notifications → triggers
    → meetings → memories) regardless of MCP scan order. Returns an empty
    list when none of the scope-aware MCPs are enabled — the caller then
    omits the MCP-defaults sentence entirely.
    """
    enabled = set(assigned_mcp_names or ())
    return [
        noun for slug, noun in _SCOPE_AWARE_MCP_NOUNS.items() if slug in enabled
    ]


def _join_nouns(nouns: list[str]) -> str:
    """Comma + ``and`` join — ``["a"] → "a"``; ``["a","b"] → "a and b"``;
    ``["a","b","c"] → "a, b, and c"`` (Oxford comma).
    """
    if not nouns:
        return ""
    if len(nouns) == 1:
        return nouns[0]
    if len(nouns) == 2:
        return f"{nouns[0]} and {nouns[1]}"
    return ", ".join(nouns[:-1]) + f", and {nouns[-1]}"


def _build_execution_scope_section(
    ctx: SecurityContext,
    *,
    assigned_mcp_names: tuple[str, ...] | list[str] = (),
) -> str:
    """Per-session scope guidance for the system prompt.

    Renders one of SIX blocks based on
    ``(session_scope, role, agent.default_scope)``:

    - **Block A** — user-scope, manager/admin, ``default_scope == "user"``
      (personal-leaning agents). Defaults to user-scope; override to agent.
    - **Block B** — user-scope, manager/admin, ``default_scope == "agent"``
      (operational agents). Defaults to agent-scope; override to user.
    - **Block C** — user-scope, editor, ``default_scope == "user"``.
    - **Block D** — user-scope, editor, ``default_scope == "agent"``.
    - **Block E** — user-scope, viewer (regardless of default_scope).
      Read-only collaborator; can write own user-scope items only.
    - **Block F** — agent-scope (no user owner — phone / task / trigger).
      Everything defaults to agent-scope.

    Each block now emits 1–2 short sentences. Folder semantics moved to
    ``_build_folders_section``; knowledge-vs-workspace prose moved to
    ``_build_building_agents_section``. The MCP-defaults sentence is
    skipped entirely when no scope-aware MCPs are enabled.
    """
    # Look up agents.default_scope. Best-effort: a missing or unknown agent
    # row falls back to the safer "user" default.
    try:
        from storage import agent_store
        agent_row = agent_store.get_agent(ctx.agent) or {}
        default_scope = agent_row.get("default_scope") or "user"
        collaborative = bool(agent_row.get("collaborative", True))
    except Exception:
        default_scope = "user"
        collaborative = True
    if default_scope not in ("user", "agent"):
        default_scope = "user"

    nouns = _scope_aware_nouns(assigned_mcp_names)
    joined = _join_nouns(nouns)
    capitalised = joined[0].upper() + joined[1:] if joined else ""

    name = ctx.display_name or ctx.username or "this session"

    if not ctx.username:
        # Block F — service session, no human owner (phone / task / trigger /
        # meeting). Keyed on the ABSENCE of a human, regardless of mount scope.
        mcp_line = (
            f"{capitalised} you create default to **agent scope**, visible "
            f"to all users of this agent.\n"
        ) if joined else ""
        return (
            "# Execution Scope\n\n"
            "You are in **agent scope** — no user owner. This is a "
            "system-initiated task, phone call, or trigger. "
            f"{mcp_line}"
            "\n"
        )

    # Shared-only HUMAN chat (visibility-modes) — a person is here (keep their
    # identity + role), but everything lives in the agent's single SHARED space:
    # no personal space, and one chat history shared with every assigned user.
    if ctx.session_scope == "agent":
        if ctx.role == "viewer":
            return (
                "# Execution Scope\n\n"
                f"You are assisting {name} (viewer) in this agent's **shared "
                "space** — its workspace, knowledge, and memory are shared with "
                "every user of this agent. Your access is read-only; you cannot "
                "change shared state.\n\n"
            )
        role_tag = f" ({ctx.role})" if ctx.role == "editor" else ""
        mcp_line = (
            f"{capitalised} you create are **shared** with every user of this "
            "agent.\n"
        ) if joined else ""
        return (
            "# Execution Scope\n\n"
            f"You are assisting {name}{role_tag} in this agent's **shared "
            "space**. Everything here — workspace, knowledge, and memory — is "
            "shared with every user of this agent; there is no personal space, "
            "and the chat history is shared too. "
            f"{mcp_line}"
            "\n"
        )

    # Personal-only (visibility-modes) — fully private to this user. No shared
    # space exists: all work, context, and memory stay with this user alone.
    if not collaborative and default_scope == "user":
        role_tag = f" ({ctx.role})" if ctx.role in ("editor", "viewer") else ""
        mcp_line = (
            f"{capitalised} you create are **private to {name}** — this agent "
            "has no shared space.\n"
        ) if joined else ""
        return (
            "# Execution Scope\n\n"
            f"You are in a **personal space** for {name}{role_tag}. Everything "
            "here — your workspace, context, and memory — is private to this "
            "user; this agent has no shared space. "
            f"{mcp_line}"
            "\n"
        )

    if ctx.role == "viewer":
        # Block E — viewer (always user-scope; read-only collaborator).
        # Viewers cannot create agent-scope items at all, so no override note.
        mcp_line = (
            f"{capitalised} you create default to **user scope** — viewers "
            f"cannot create agent-scope items.\n"
        ) if joined else (
            "Viewers cannot create agent-scope items — only personal ones.\n"
        )
        return (
            "# Execution Scope\n\n"
            f"You are in **user scope** for {name} (viewer). "
            f"{mcp_line}"
            "\n"
        )

    role_tag = f" ({ctx.role})" if ctx.role == "editor" else ""

    if default_scope == "agent":
        # Blocks B (manager/admin) and D (editor) — operational agent.
        mcp_line = (
            f"This is an operational agent — most work is shared. "
            f"{capitalised} you create default to **agent scope**, visible "
            f"to all users of this agent. Override `scope=\"user\"` only "
            f"when the content is clearly user-specific.\n"
        ) if joined else (
            "This is an operational agent — most work is shared.\n"
        )
        return (
            "# Execution Scope\n\n"
            f"You are in **user scope** for {name}{role_tag}. "
            f"{mcp_line}"
            "\n"
        )

    # Blocks A (manager/admin) and C (editor) — personal-leaning agent.
    mcp_line = (
        f"{capitalised} you create default to **user scope** — visible "
        f"only to this user. Override `scope=\"agent\"` only when the user "
        f"explicitly asks for a shared / system-wide action.\n"
    ) if joined else ""
    return (
        "# Execution Scope\n\n"
        f"You are in **user scope** for {name}{role_tag}. "
        f"{mcp_line}"
        "\n"
    )


def _build_execution_environment_section(
    ctx: SecurityContext, *, has_file_tools: bool = False,
) -> str:
    """Build the ``# Execution Environment`` section — describes where the
    agent is actually running and what filesystem access it has.

    Variants (by ``target_kind`` × ``allow_full_fs``):

    - **Local sandbox** — bwrap mount namespace on the platform host.
      Tier 1 + Tier 2 dev toolchain. Writes kernel-restricted to mounts.
    - **user_remote, allow_full_fs=False** (user-pairing default) —
      agent tree + OS user's home dir. System paths denied.
    - **user_remote, allow_full_fs=True** — user opted in to full FS.
    - **admin_remote, allow_full_fs=True** (admin-pairing default) —
      full FS, the satellite-user's OS permissions are the only
      boundary.
    - **admin_remote, allow_full_fs=False** (rare opt-out) — home-only
      even on an admin-paired machine.
    """
    if ctx.target_kind == "admin_remote":
        return _admin_remote_env_section(ctx)
    if ctx.target_kind == "user_remote":
        return _user_remote_env_section(ctx)
    return _local_env_section(has_file_tools=has_file_tools)


def _format_user_dirs(user_dirs: dict) -> str:
    """Render the well-known user folders as a tight bullet list. Returns
    an empty string when no dirs are known (e.g. probe wasn't run yet).
    """
    if not user_dirs:
        return ""
    order = ("desktop", "downloads", "documents", "pictures", "music", "videos")
    lines = []
    for key in order:
        value = user_dirs.get(key)
        if value:
            label = key.capitalize()
            lines.append(f"  - {label}: `{value}`")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _remote_mcp_paths_section() -> str:
    """The framework auto-translates MCP tool-arg paths declared
    via ``tool_arg_paths`` in each MCP's manifest, so the LLM can use
    either sandbox-virtual or satellite-host paths in MCP calls. Native
    file READ-side tools plus Write get the same treatment via the
    permission hook: a sandbox-virtual or ``~`` path arg is rewritten to
    the satellite-host form before the tool runs (PreToolUse
    ``updatedInput``). Edit/NotebookEdit are the documented exception —
    the CLI validates their target (file-exists / read-state) BEFORE the
    hook pipeline runs, so no rewrite can reach them and they need the
    OS-native path. Shell command STRINGS are the other exception —
    nothing rewrites inside a Bash command.
    """
    return (
        "**Native file tools** (`Read`, `Write`, `Edit`, `Glob`, `Grep`): "
        "prefer the OS-native paths shown above (under `# Folders` and the "
        "home directory section). `Read`, `Write`, `Glob` and `Grep` also "
        "accept the sandbox-virtual forms (`/workspace/...`, "
        "`/users/{u}/...`, `/knowledge/...`, `/config/...`) and `~/...` — "
        "the platform rewrites the path to its satellite location before "
        "the tool runs. `Edit` and `NotebookEdit` do NOT: they check their "
        "target before the rewrite can happen, so give them the OS-native "
        "path (a sandbox-virtual path there fails with "
        "file-not-found).\n\n"
        "**Shell commands** (`Bash`): OS-native paths ONLY. Nothing "
        "rewrites inside a command string, so a sandbox-virtual path there "
        "fails with file-not-found (or hits the wrong location on "
        "Windows).\n\n"
        "**MCP tools** (anything starting with `mcp__`): pass paths in "
        "either form — sandbox-virtual (`/workspace/foo`, "
        "`/users/{u}/foo`) or satellite-host (`/home/user/Desktop/foo`, "
        "`~/Desktop/foo`). The framework translates them "
        "automatically.\n\n"
    )


def _user_remote_env_section(ctx: SecurityContext) -> str:
    label = ctx.target_label or "the user's machine"
    home_dir = ctx.target_home_dir
    allow_full_fs = ctx.target_allow_full_fs
    folders_listing = _format_user_dirs(ctx.target_user_dirs)
    home_block = ""
    if home_dir:
        home_block = (
            f"\n**OS user's home directory** is `{home_dir}`"
            + (
                f" (OS user: `{ctx.target_os_user}`)" if ctx.target_os_user else ""
            )
            + ". Common shortcuts you can use directly:\n"
            + folders_listing
            + "Tilde paths (`~/Desktop/foo.png`) also expand to this home dir.\n\n"
        )
    if allow_full_fs:
        access_block = (
            "**Filesystem access**: this machine has been opted into "
            "full filesystem access by the user. You can read and write "
            "any path the satellite user's OS account can reach — home "
            "dirs, system configs, third-party app dirs, etc. Be "
            "careful — these are real changes to the user's machine.\n\n"
        )
    else:
        access_block = (
            "**Filesystem access**: limited to the agent's synced tree "
            "and the OS user's home directory above. System paths "
            "(`/etc`, `/var`, `/private`, `C:/Windows`, etc.) and other "
            "OS users' home directories are denied. If broader access "
            "is needed, ask the user to enable **Full filesystem "
            "access** for this machine in their Settings → Remote "
            "machines page.\n\n"
        )
    return (
        "# Execution Environment\n\n"
        f"You are running on **`{label}`**, the user's own machine "
        "(paired by them via User Settings; only applies to their own "
        f"user-scope sessions). The `agents/{ctx.agent}/` folder tree "
        "(see `# Folders` below) is kept in bidirectional sync between "
        "the platform and this machine.\n"
        + home_block
        + access_block
        + _remote_mcp_paths_section()
    )


def _admin_remote_env_section(ctx: SecurityContext) -> str:
    label = ctx.target_label or "an admin-paired machine"
    home_dir = ctx.target_home_dir
    allow_full_fs = ctx.target_allow_full_fs
    home_block = ""
    if home_dir:
        home_block = (
            f"\n**OS user's home directory** is `{home_dir}`"
            + (
                f" (OS user: `{ctx.target_os_user}`)" if ctx.target_os_user else ""
            )
            + ".\n\n"
        )
    if allow_full_fs:
        access_block = (
            "**Filesystem access**: full. The admin paired this machine "
            "with full filesystem access — manage system services, "
            "edit configs, deploy code, etc. The satellite-user's OS "
            "permissions are the only boundary. The bash admin tier "
            "(`docker`, `systemctl`, `journalctl`, `apt`, etc.) is "
            "available because the admin-pairing decision delegates "
            "that trust.\n\n"
        )
    else:
        access_block = (
            "**Filesystem access**: limited to the agent's synced tree "
            "and the OS user's home directory. The admin explicitly "
            "opted this machine out of full filesystem access. The "
            "bash admin tier remains available (admin-pairing trust), "
            "but path-touching operations on system paths are denied. "
            "Ask the admin to flip the **Allow full filesystem access** "
            "toggle if you need broader reach.\n\n"
        )
    return (
        "# Execution Environment\n\n"
        f"You are running on **`{label}`**, a remote machine paired by "
        "the platform admin as this agent's execution target — this "
        "is an operational deployment, often used for managing "
        "services, deployments, or infrastructure. The "
        f"`agents/{ctx.agent}/` folder tree (see `# Folders` below) "
        "is kept in bidirectional sync between the platform and this "
        "machine.\n"
        + home_block
        + access_block
        + _remote_mcp_paths_section()
    )


def _local_env_section(*, has_file_tools: bool = False) -> str:
    # Mention File Tools only when it's actually assigned to this session —
    # pointing an agent at a tool it doesn't have wastes a failed call.
    file_tools_line = (
        "For Office documents, PDFs, charts, and image editing use the "
        "**File Tools MCP** (`read_document`, "
        "`write_docx`/`xlsx`/`pptx`/`pdf`, `convert_document`, "
        "`edit_image`, `create_chart`, …). "
    ) if has_file_tools else ""
    return (
        "# Execution Environment\n\n"
        "You are running inside a **local bwrap kernel sandbox** on the "
        "platform host. The folders listed in `# Folders` below are the "
        "only paths you can read or write — everything else literally "
        "doesn't exist in this mount namespace. The sandbox ships a full "
        "dev toolchain (`python3`, `node`, `pip`, `npm`, `gcc`, `make`, "
        "`pdftotext`, `sqlite3`, `git`, `gh`, etc.). "
        + file_tools_line
        + "Command access depends on your role — see `# File Permissions` "
        "below.\n\n"
    )


def _get_default_scope(agent: str) -> str:
    """Look up ``agents.default_scope`` with a safe fallback to ``"user"``."""
    try:
        from storage import agent_store
        row = agent_store.get_agent(agent) or {}
        ds = row.get("default_scope") or "user"
        return ds if ds in ("user", "agent") else "user"
    except Exception:
        return "user"


def _build_folders_section(
    ctx: SecurityContext,
    *,
    default_scope: str,
) -> str:
    """Build the ``# Folders`` section — per-role folder map.

    Lists ONLY the folders this session can see, each with one-line purpose
    + access mode (RW / RO). Closes with the default-workspace pointer
    derived from ``(session_scope, default_scope)``:

    - user-scope session + agent default_scope='agent' → default `/workspace/`
      (operational agents prefer shared work).
    - user-scope session + default_scope='user' → default `/users/{u}/workspace/`.
    - agent-scope session → always `/workspace/`.

    User-scope sessions get explicit upload paths
    (``users/{u}/workspace/uploads/photos/`` for chat photos,
    ``users/{u}/workspace/uploads/files/`` for chat files) so the agent
    knows where to look when the user asks about an attachment.
    """
    if not ctx.username:
        # Block F — service session: agent-scope shared dirs only, no user dirs.
        return (
            "# Folders\n\n"
            "You have access to the following folders in this session:\n\n"
            "- `/workspace/` (RW) — The agent's shared workspace. Operational "
            "output goes here.\n"
            "- `/knowledge/` (RO) — The agent's manager-curated reference "
            "library. Readable but not editable in this scope.\n\n"
            "Default workspace for this session is `/workspace/`. Read from "
            "`/knowledge/` when reference material is needed.\n\n"
        )

    # Shared-only HUMAN chat (visibility-modes) — agent-scope mount, role-aware,
    # NO per-user dirs. The shared workspace IS the personal workspace here.
    if ctx.session_scope == "agent":
        srows: list[str] = [
            "# Folders\n\n",
            "You have access to the following folders in this session:\n\n",
        ]
        if ctx.role == "viewer":
            srows.append(
                "- `/workspace/` (RO) — The agent's shared workspace. You can "
                "read but not edit.\n"
            )
        else:
            srows.append(
                "- `/workspace/` (RW) — The agent's **shared workspace**. "
                "Everything here is shared with every user of this agent.\n"
            )
        if ctx.effective_config_visible:
            srows.append(
                "- `/knowledge/` (RW) — The agent's **reference library**. "
                "Manager-curated docs / templates; read on demand.\n"
            )
            srows.append(
                "- `/config/` (RW) — The agent's configuration. Holds "
                "`prompt.md` (the persona) and `context/` (auto-loaded every "
                "session). Only managers see this folder.\n"
            )
        else:
            srows.append(
                "- `/knowledge/` (RO) — The agent's manager-curated reference "
                "library. Read on demand when relevant.\n"
            )
        srows.append(
            "\nDefault writes for this session go to `/workspace/` — this agent "
            "has **no personal space**; everything is shared with all its "
            "users.\n\n"
        )
        return "".join(srows)

    u = ctx.username
    rows: list[str] = [
        "# Folders\n\n",
        "You have access to the following folders in this session:\n\n",
        f"- `/users/{u}/workspace/` (RW) — Your personal workspace. Day-to-day "
        "work, files saved here are yours alone.\n",
        f"- `/users/{u}/context/` (RW) — Your personal context docs. Markdown "
        "files here auto-load into your sessions on this agent only.\n",
    ]

    # Shared workspace + knowledge — only when the agent's mode offers them
    # (Personal-only omits both: it is fully private, no shared collaboration).
    if ctx.mount_shared:
        if ctx.role == "viewer":
            rows.append(
                "- `/workspace/` (RO) — The agent's shared workspace. You can "
                "read but not edit; personal output goes to "
                f"`/users/{u}/workspace/`.\n"
            )
        else:
            rows.append(
                "- `/workspace/` (RW) — The agent's **shared workspace**. "
                "Collaborative output visible to every user of this agent.\n"
            )
        if ctx.role in ("manager", "admin"):
            rows.append(
                "- `/knowledge/` (RW) — The agent's **reference library**. "
                "Manager-curated docs / templates. Not auto-loaded — read on "
                "demand when relevant.\n"
            )
        else:
            rows.append(
                "- `/knowledge/` (RO) — The agent's manager-curated reference "
                "library. Read on demand when relevant.\n"
            )

    # Agent config — only manager/admin see it at all (incl. Personal-only:
    # managers still curate the persona even with no shared space).
    if ctx.role in ("manager", "admin"):
        rows.append(
            "- `/config/` (RW) — The agent's configuration. Holds "
            "`prompt.md` (the agent persona) and `context/` (files that "
            "auto-load into every session of this agent). Only managers "
            "see this folder.\n"
        )

    # Upload paths and default-workspace pointer.
    rows.append(
        f"\nFiles uploaded to chat are saved under "
        f"`/users/{u}/workspace/uploads/photos/` (images) and "
        f"`/users/{u}/workspace/uploads/files/` (other files).\n"
    )

    if not ctx.mount_shared:
        # Personal-only — there is no shared workspace to point at.
        rows.append(
            f"\nDefault writes for this session go to `/users/{u}/workspace/`. "
            "This agent is **personal only** — it has no shared space; "
            "everything you do stays private to you.\n\n"
        )
    elif default_scope == "agent":
        rows.append(
            "\nDefault writes for this session go to `/workspace/` (this is "
            "an operational agent — outputs there are visible to every "
            f"user of this agent). Use `/users/{u}/workspace/` for personal "
            "drafts, private notes, or anything the user wants to keep to "
            "themselves.\n\n"
        )
    else:
        rows.append(
            f"\nDefault writes for this session go to "
            f"`/users/{u}/workspace/`. The agent's `/workspace/` is its "
            "shared workspace — write there when the work is collaborative "
            "or the user wants to share it with other users of this "
            "agent.\n\n"
        )

    return "".join(rows)


def _build_building_agents_section(
    ctx: SecurityContext,
    *,
    default_scope: str,
    assigned_mcp_names: tuple[str, ...] | list[str] = (),
) -> str:
    """Build the manager-only ``# Building Agents`` section.

    Explains how agents are structured on this platform (config/knowledge/
    workspace/users layout, default_scope semantics) and points at the
    chat-level tools managers can use to change agent settings
    (``agent-config-mcp``) or browse new MCPs (``mcps-mcp``).

    Renders only when the session can act on this guidance:
    - User has a username (user-scope session).
    - Role is manager or admin (editor / viewer cannot change agent structure).

    Returns an empty string otherwise.
    """
    if not ctx.username:
        return ""
    if ctx.role not in ("manager", "admin"):
        return ""

    enabled = set(assigned_mcp_names or ())
    has_config = "agent-config-mcp" in enabled
    has_mcps = "mcps-mcp" in enabled

    # Build the "tools you can use from chat" lines — only mention tools
    # that are actually enabled. When neither is, fall back to the dashboard.
    tool_lines: list[str] = []
    if has_config:
        tool_lines.append(
            "- `agent-config-mcp` — edit identity (display name, "
            "description, color), default model, default execution layer, "
            "default scope, and per-agent memory toggles."
        )
    if has_mcps:
        tool_lines.append(
            "- `mcps-mcp` — browse the community catalog and request "
            "additional MCPs (tools)."
        )
    if not tool_lines:
        tool_lines.append(
            "- Agent settings and MCP installation are managed from the "
            "dashboard's agent settings page."
        )
    tool_block = "\n".join(tool_lines)

    # Default-scope closer — actionable: tell the manager how to change it
    # via the right channel given which tools are available. # Execution
    # Scope above already states the consequence; this sentence focuses on
    # the change path.
    if has_config:
        scope_close = (
            f"This agent's `default_scope` is **{default_scope}** — change "
            "it via `agent-config-mcp.update_default_scope(...)` if a "
            "different default fits the agent's purpose better.\n"
        )
    else:
        scope_close = (
            f"This agent's `default_scope` is **{default_scope}** — change "
            "it from the agent settings page in the dashboard if a different "
            "default fits the agent's purpose better.\n"
        )

    # Folder bullets adapt to the agent's mode: Personal-only has no shared
    # /knowledge or /workspace; Shared-only has no per-user dirs.
    folder_bullets = [
        "- Edit the persona: `/config/prompt.md` (loaded first every session).\n",
        "- Add always-loaded knowledge: drop markdown files in "
        + "`/config/context/*` (operational rules, business context, vocabulary; "
        + "1MB per file, 5MB total cap, loaded EVERY session).\n",
    ]
    if ctx.mount_shared:
        folder_bullets.append(
            "- Curate the on-demand reference library: `/knowledge/` — files "
            "here are NOT auto-loaded; the agent reads them when relevant.\n"
        )
        folder_bullets.append(
            "- Shared collaborative output lives in `/workspace/` (writable by "
            "every user of this agent except viewers).\n"
        )
    if "user" in ctx.available_scopes:
        folder_bullets.append(
            "- Per-user files at `/users/{u}/` are private to each assigned "
            "user.\n"
        )

    return (
        "# Building Agents\n\n"
        f"On this platform, agents are folders. As manager you shape the "
        f"`{ctx.agent}` agent by editing its files and config:\n\n"
        + "".join(folder_bullets)
        + "\nTools you can use from chat:\n"
        f"{tool_block}\n\n"
        f"{scope_close}\n"
    )


def build_permission_context(
    ctx: SecurityContext,
    *,
    assigned_mcp_names: tuple[str, ...] | list[str] = (),
    execution_path: str = "",
) -> str:
    """Generate the identity / scope / folders / permissions sections.

    Injected into the agent prompt so the LLM knows its constraints upfront,
    avoiding wasted tool calls that would be denied by the hook.

    Section order:
      1. ``# Session Context`` — one-liner identity + per-agent role
         (skipped for agent-scope sessions).
      2. ``# Execution Scope`` — current scope + MCP defaults sentence
         (only mentions scope-aware MCPs that are actually enabled).
      3. ``# Execution Environment`` — local sandbox vs admin-paired vs
         user-paired remote satellite.
      4. ``# Folders`` — per-role folder map with uploads paths +
         default-workspace pointer.
      5. ``# Building Agents`` — manager/admin on user-scope only;
         explains agent structure + points at chat-level config tools.
      6. ``# File Permissions`` — Bash tiers (CLI/Codex only) +
         WebFetch + Glob/Grep restrictions. Per-folder summaries live
         in `# Folders`.

    Args:
        ctx: SecurityContext for the session.
        assigned_mcp_names: tuple/list of MCP slugs assigned to this
            session. Drives the dynamic MCP-defaults sentence in
            ``# Execution Scope`` and the tools pointer in
            ``# Building Agents``. Empty tuple = omit those sentences.
        execution_path: ``"claude-code-cli"`` / ``"codex-cli"`` /
            ``"direct-llm"`` / ``""``. Drives Bash + plans-dir gating:
            direct-llm sessions get no Bash block and no plans-dir
            mention (those are CLI / Codex features).
    """
    default_scope = _get_default_scope(ctx.agent)
    # Used in multiple branches below — hoist so it's defined for any
    # combination of execution_path / bash-layer.
    is_remote = ctx.target_kind in ("admin_remote", "user_remote")
    # Layer capability gates. Direct LLM sessions never have Bash (it's a
    # built-in tool on Claude Code CLI / Codex CLI). Plans dir is a Claude
    # Code feature — Codex uses update_plan natively, Direct LLM has no
    # plan tooling.
    layer_supports_bash = execution_path in ("claude-code-cli", "codex-cli")
    layer_supports_plans = execution_path == "claude-code-cli"

    sections: list[str] = []

    # 1. Session Context — one-liner identity (skipped for agent-scope).
    identity = _build_identity_section(ctx)
    if identity:
        sections.append(identity)

    # 2. Execution Scope — slim, dynamic MCP-list sentence.
    sections.append(
        "\n\n---\n\n"
        + _build_execution_scope_section(
            ctx, assigned_mcp_names=assigned_mcp_names,
        )
    )

    # 3. Execution Environment — where the agent runs (local sandbox vs
    # admin-paired vs user-paired remote satellite). Always emitted —
    # even the local case is worth one short paragraph so the agent
    # knows the toolchain and the mount-namespace boundary.
    has_file_tools = "file-tools" in set(assigned_mcp_names or ())
    sections.append(
        "---\n\n"
        + _build_execution_environment_section(ctx, has_file_tools=has_file_tools)
    )

    # 4. Folders — per-role folder map with uploads + default-workspace pointer.
    sections.append(
        "---\n\n"
        + _build_folders_section(ctx, default_scope=default_scope)
    )

    # 5. Building Agents — manager+ on user-scope only (empty otherwise).
    building = _build_building_agents_section(
        ctx,
        default_scope=default_scope,
        assigned_mcp_names=assigned_mcp_names,
    )
    if building:
        sections.append("---\n\n" + building)

    # 6. File Permissions — Bash tiers (CLI/Codex only) + WebFetch + Glob/Grep.
    perm_lines: list[str] = ["---\n\n# File Permissions\n\n"]

    if layer_supports_bash:
        # Top-level Bash blurb — what's available + where the filesystem
        # boundary is (varies by execution target — see `# Execution
        # Environment` above for full picture).
        file_tools_note = (
            "Office documents, PDFs, charts, and image editing go "
            "through the File Tools MCP (Pillow / LibreOffice backends). "
        ) if has_file_tools else ""
        perm_lines.append(
            "**Bash access** — a full dev toolchain ships in every "
            "session: `python3`, `node`, `pip`, `npm`, `gcc`, `make`, "
            "`pdftotext`, `sqlite3`, `git`, `gh`, etc. "
            + file_tools_note
            + "Filesystem boundary "
            "is bwrap on local sandboxes / OS permissions on remote "
            "satellites (see `# Execution Environment` above).\n\n"
        )

        # Admin-tier (host-touching: docker, systemctl, ssh, apt) — gate
        # depends on (role, target_kind):
        #   - admin role: always available
        #   - manager/editor on remote satellite: available (admin/user
        #     pairing = trust delegation)
        #   - manager/editor on local sandbox: not available
        #   - viewer: never available
        if ctx.role == "admin":
            perm_lines.append(
                "- Host-touching commands (`docker`, `docker compose`, "
                "`systemctl`, `journalctl`, `ssh`, `scp`, `apt`): "
                "available to you as platform admin.\n"
            )
        elif is_remote and ctx.role in ("manager", "editor"):
            perm_lines.append(
                "- Host-touching commands (`docker`, `docker compose`, "
                "`systemctl`, `journalctl`, `ssh`, `scp`, `apt`): "
                "**available here** because this is a remote satellite "
                "and you're an ops-capable role. Their effect depends on "
                "the satellite user's OS permissions on the host.\n"
            )
        elif ctx.role == "viewer":
            perm_lines.append(
                "- Host-touching commands (`docker`, `systemctl`, `ssh`, "
                "`apt`): not available to viewers in any environment.\n"
            )
        else:
            perm_lines.append(
                "- Host-touching commands (`docker`, `systemctl`, `ssh`, "
                "`apt`): not available on local sandbox sessions for your "
                "role. On admin-paired remote satellites, manager / "
                "editor roles get access.\n"
            )

        perm_lines.append(
            "- Always blocked (every role, every environment): only "
            "catastrophic, irreversible commands — `rm -rf /` or `~`, fork "
            "bombs, writes to raw devices / `/proc` / `/sys`, kernel-module "
            "ops, `/dev/tcp`, reading `/etc/shadow` (and the PowerShell "
            "equivalents: `Format-Volume`, `Clear-Disk`, recursive-force "
            "delete of a drive root). Nothing else is hard-blocked.\n"
        )
        perm_lines.append(
            "- Other commands run by tier: common read-only commands run "
            "freely; an UNKNOWN command (incl. `bash -c`/`eval`/`$()`, which "
            "are unwrapped and re-checked) just prompts for approval — it is "
            "NOT refused. Destructive commands (`rm`, `dd`, …) prompt before "
            "running. So use the tool you need; you'll be asked if it's "
            "unusual, rather than blocked.\n\n"
        )
        perm_lines.append(
            "- Backgrounding a multi-statement shell loop (a `while`/`for` "
            "one-liner via `run_in_background`) can be rejected by the CLI's OWN "
            "parser before it ever reaches the sandbox. If that happens, put the "
            "loop in a `.sh` file and background that, or pass a single "
            "`bash -lc '…'`. You CAN read a finished background command's output "
            "from the `.output` file path the CLI prints.\n\n"
        )

    perm_lines.append("**Other restrictions:**\n")
    perm_lines.append(
        "- **WebFetch**: cannot access private/internal network addresses\n"
    )
    if is_remote:
        # Remote satellite: host filesystem reach depends on the machine's
        # allow_full_fs pairing flag — mirror the Execution Environment
        # section above so the prompt never promises more than the path
        # gate actually admits.
        if ctx.target_allow_full_fs:
            perm_lines.append(
                "- **Glob / Grep / Read / Write**: free across the entire "
                "satellite host filesystem (subject to OS permissions of "
                "the user account the satellite runs as). The synced "
                "`# Folders` above are the platform-shared tree; files "
                "outside it are on the satellite host only.\n"
            )
        else:
            perm_lines.append(
                "- **Glob / Grep / Read / Write**: allowed in the synced "
                "`# Folders` tree and the OS user's home directory; "
                "system paths and other OS users' home directories are "
                "denied (see the Execution Environment section above).\n"
            )
    else:
        perm_lines.append(
            "- **Glob / Grep**: scope-restricted to your allowed read paths\n"
        )
        perm_lines.append(
            "- Writes outside the folders listed in `# Folders` above are denied\n"
        )
    if layer_supports_plans and ctx.username:
        perm_lines.append(
            f"- Plan files live under `/users/{ctx.username}/.claude/plans/` "
            "and are managed by the plan mode tool — you don't need to "
            "edit them directly\n"
        )
    perm_lines.append("\n")

    sections.append("".join(perm_lines))

    return "".join(sections)
