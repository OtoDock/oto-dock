"""The system prompt's rendering (``build_agent_prompt`` and its sections)
and the fold that drops the MCPs a remote session could not install.

``config`` re-exports the public names and imports this module, so the
agents root and the agent-name helpers (``AGENTS_DIR``, ``get_agent_dir``,
``is_safe_agent_name``) are read through ``config`` at call time: the suite
rebinds ``config.AGENTS_DIR``.
"""

import contextlib
import dataclasses
import fnmatch
import logging
import os
from pathlib import Path

from auth import roles
from core import layout
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities

_prompt_log = logging.getLogger("otodock.prompts")

_DOC_EXTENSIONS = {".md", ".txt", ".markdown"}
_MAX_DOC_BYTES = 1 * 1024 * 1024       # 1 MB per file
_MAX_TOTAL_DOC_BYTES = 5 * 1024 * 1024  # 5 MB total per agent


def _read_persona(agent_fd: int, name: str) -> str:
    """``config/<name>`` beneath the agent's handle: a regular file reached
    with no link, at most ``_MAX_TOTAL_DOC_BYTES``, decoded as UTF-8."""
    from services.infra import safe_fs
    return safe_fs.read_bytes_beneath(
        agent_fd, f"{layout.CONFIG}/{name}", max_size=_MAX_TOTAL_DOC_BYTES).decode("utf-8")


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
    from core.agent_folder import agent_root
    from services.infra import safe_fs
    if not config.is_safe_agent_name(agent_name):
        return False
    try:
        with safe_fs.open_root(agent_root(agent_name)) as agent_fd:
            for name in ("agent.md", "prompt.md"):
                try:
                    text = _read_persona(agent_fd, name)
                except OSError:
                    continue
                # Drop a single leading H1 (the seeded title) and see what remains.
                body = text.strip()
                if body.startswith("#"):
                    body = body.split("\n", 1)[1] if "\n" in body else ""
                return not body.strip()
    except (OSError, ValueError):
        return False
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

    Every file is read beneath a handle on the agent's real folder
    (``core.agent_folder.agent_root``) with no link at any part below it: a
    manager's session writes ``config/``, so a link there
    is never followed into the prompt. A link in the context folder is listed
    as skipped; a persona that is a link, or past 5 MB, is refused and the
    agent has no prompt (logged), as with no persona at all.

    Returns list of (relative_path, content) tuples.
    """
    import config
    from core.agent_folder import agent_root
    from services.infra import safe_fs
    if not config.is_safe_agent_name(model):
        return []
    try:
        with safe_fs.open_root(agent_root(model)) as agent_fd:
            return _agent_files(agent_fd, model)
    except (FileNotFoundError, ValueError):
        return []
    except OSError as exc:
        _prompt_log.warning("prompt of %s: its folder was not read (%s)", model, type(exc).__name__)
        return []


def _agent_files(agent_fd: int, model: str) -> list[tuple[str, str]]:
    """``_read_agent_files`` beneath the agent's handle."""
    from services.infra import safe_fs
    persona_text: str | None = None
    # Two passes: a concurrent rename/restore can remove the probed file
    # between the listing and the read — re-probe both names before giving up.
    for _attempt in range(2):
        for name in ("agent.md", "prompt.md"):
            try:
                persona_text = _read_persona(agent_fd, name)
                break
            except FileNotFoundError:
                continue
            except (safe_fs.SafeFsError, UnicodeDecodeError) as exc:
                _prompt_log.warning("prompt of %s: config/%s was not read (%s)",
                                    model, name, type(exc).__name__)
                continue
            except OSError:
                continue
        if persona_text is not None:
            break
    if persona_text is None:
        return []

    files: list[tuple[str, str]] = [("agent.md", persona_text)]

    context_rel = f"{layout.CONFIG}/{layout.CONTEXT}"
    found: list[tuple[str, int]] = []
    links: list[str] = []

    def _unread(exc: OSError) -> None:
        _prompt_log.warning("prompt of %s: a folder of %s was not read (%s)",
                            model, context_rel, type(exc).__name__)

    try:
        for step in safe_fs.walk_beneath(agent_fd, context_rel, onerror=_unread):
            for name in step.files:
                if Path(name).suffix.lower() not in _DOC_EXTENSIONS:
                    continue
                try:
                    size = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False).st_size
                except OSError:
                    continue
                found.append((step.path(name), size))
            links.extend(step.path(name) for name in step.symlinks
                         if Path(name).suffix.lower() in _DOC_EXTENSIONS)
    except (FileNotFoundError, NotADirectoryError):
        return files
    except OSError as exc:
        _unread(exc)
        return files
    # The order of the sorted paths before: by path part.
    found.sort(key=lambda item: item[0].split("/"))

    total_bytes = 0
    skipped: list[str] = []
    for rel, size in found:
        if size > _MAX_DOC_BYTES:
            skipped.append(f"{rel} ({size} bytes — over 1 MB per-file limit)")
            continue
        if total_bytes + size > _MAX_TOTAL_DOC_BYTES:
            skipped.append(f"{rel} (5 MB total cap reached)")
            break
        try:
            data = safe_fs.read_bytes_beneath(agent_fd, rel, max_size=_MAX_DOC_BYTES)
        except safe_fs.FileTooLarge:
            skipped.append(f"{rel} (over 1 MB per-file limit)")
            continue
        except OSError:
            continue
        files.append((rel, data.decode("utf-8", errors="replace")))
        total_bytes += size
    skipped.extend(f"{rel} (a link, not loaded)" for rel in sorted(links, key=lambda r: r.split("/")))

    if skipped:
        files.append((
            "_auto_context_skipped.md",
            "## Auto-context files skipped\n\n"
            + "\n".join(f"- {s}" for s in skipped),
        ))

    return files


def _person_context_docs(agent_name: str, ctx_dir: Path,
                         cap: int) -> tuple[list[tuple[str, int, bytes | None]], list[str]]:
    """``(name, size, bytes)`` of each regular ``*.md`` file directly in a
    person's (or a caller's) context folder, a hidden name included, sorted
    by name, listed and read beneath the agent's real folder
    (``core.agent_folder``) with no link at any part below it; ``bytes`` is
    None for a file past ``cap`` (it is named as skipped). Beside them, the
    ``*.md`` names that are links: named as skipped, never loaded. A folder
    that is missing, outside the agent's folder or reached through a link
    lists nothing."""
    from core.agent_folder import beneath_agent
    from services.infra import safe_fs
    docs: list[tuple[str, int, bytes | None]] = []
    links: list[str] = []
    try:
        root, rel = beneath_agent(ctx_dir, agent_name)
        with safe_fs.open_root(root) as agent_fd:
            names: list[tuple[str, int]] = []
            for step in safe_fs.walk_beneath(agent_fd, rel):
                for name in step.files:
                    if not fnmatch.fnmatchcase(name, "*.md"):
                        continue
                    try:
                        names.append((name, os.stat(name, dir_fd=step.dirfd, follow_symlinks=False).st_size))
                    except OSError:
                        continue
                links.extend(sorted(name for name in step.symlinks if fnmatch.fnmatchcase(name, "*.md")))
                break
            for name, size in sorted(names):
                if size > cap:
                    docs.append((name, size, None))
                    continue
                try:
                    data: bytes | None = safe_fs.read_bytes_beneath(agent_fd, f"{rel}/{name}", max_size=cap)
                except safe_fs.FileTooLarge:
                    data = None
                except OSError:
                    continue
                docs.append((name, size, data))
    except FileNotFoundError:
        return docs, links
    except OSError as exc:
        _prompt_log.warning("prompt of %s: a person's context folder was not read (%s)",
                            agent_name, type(exc).__name__)
        return docs, links
    except ValueError:
        return docs, links
    return docs, links


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
                    external_home: str = "",
                    external: bool = False,
                    outside_knowledge: tuple[str, ...] = ()) -> str | None:
    """Scan an agent's directories and return a formatted tree.

    An outside session (``external``) sees its own home and the knowledge
    folders a manager picked (``outside_knowledge``), never the shared
    workspace or the knowledge tree whole: the tree shows what is mounted.

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

    Every folder is listed beneath a handle on the agent's real folder
    (``core.agent_folder.agent_root``) and entered from its parent's handle
    with no link followed.
    """
    from core.agent_folder import agent_root
    from services.infra import safe_fs
    try:
        with safe_fs.open_root(agent_root(agent_name)) as agent_fd:
            return _scan_tree(agent_fd, agent_dir, username=username, role=role,
                              mount_shared=mount_shared, external_home=external_home,
                              external=external, outside_knowledge=outside_knowledge)
    except (OSError, ValueError):
        return None


def _scan_tree(agent_fd: int, agent_dir: Path, *, username: str | None, role: str,
               mount_shared: bool, external_home: str, external: bool,
               outside_knowledge: tuple[str, ...]) -> str | None:
    """``_scan_workspace`` beneath the agent's handle."""
    from services.infra import safe_fs
    lines: list[str] = []
    rendered_bytes = 0
    truncated_count = 0  # entries past the cap we couldn't fit

    def _trim_name(name: str) -> str:
        if len(name) <= _TREE_MAX_NAME_LEN:
            return name
        return name[:_TREE_MAX_NAME_LEN - 1] + "…"

    def _walk(dir_fd: int, depth: int, indent: int) -> None:
        nonlocal rendered_bytes, truncated_count
        if depth > _TREE_MAX_DEPTH:
            return
        try:
            with os.scandir(dir_fd) as it:
                entries = list(it)
        except OSError:
            return
        # Filter: skip hidden + denylisted dirs; keep everything else. A
        # folder is entered from its parent's handle with no link followed;
        # a link is listed by its name with ``@`` and never entered, and an
        # outside session lists no link at all.
        dirs: list[str] = []
        files: list[tuple[str, str]] = []
        for e in entries:
            if e.name.startswith(".") or e.name in _TREE_SKIP_DIRS:
                continue
            try:
                if e.is_symlink():
                    if not external:
                        files.append((e.name, f"{_trim_name(e.name)}@"))
                elif e.is_dir(follow_symlinks=False):
                    dirs.append(e.name)
                elif e.is_file(follow_symlinks=False):
                    files.append((e.name, _trim_name(e.name)))
            except OSError:
                continue
        # Sort: directories first, then files; both alphabetical.
        for d in sorted(dirs):
            line = f"{'  ' * indent}{_trim_name(d)}/"
            if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
                truncated_count += 1
                continue
            lines.append(line)
            rendered_bytes += len(line) + 1
            try:
                child = safe_fs.open_dir_beneath(dir_fd, d)
            except OSError:
                continue
            try:
                _walk(child, depth + 1, indent + 1)
            finally:
                os.close(child)
        for _name, shown in sorted(files):
            line = f"{'  ' * indent}{shown}"
            if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
                truncated_count += 1
                continue
            lines.append(line)
            rendered_bytes += len(line) + 1

    def _emit_root(rel_label: str, rel: str) -> None:
        nonlocal rendered_bytes
        try:
            fd = safe_fs.open_dir_beneath(agent_fd, rel)
        except OSError:
            return
        try:
            line = rel_label
            if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
                return
            lines.append(line)
            rendered_bytes += len(line) + 1
            _walk(fd, 1, 1)
        finally:
            os.close(fd)

    # An external caller's own tree (mounted at /caller) comes first.
    if external_home:
        try:
            home_rel = safe_fs.rel_under(external_home, agent_dir)
        except safe_fs.SafeFsError:
            home_rel = ""
        if home_rel:
            for sub in layout.USER_SUBDIRS:
                _emit_root(f"/caller/{sub}/", f"{home_rel}/{sub}")
    if external:
        from services.mcp.outside_picks import knowledge_parts
        for pick in outside_knowledge:
            parts_ = knowledge_parts(pick)
            if parts_ is None:
                continue
            # A picked folder reached through a link shows nothing, as the
            # mounts bind nothing for it.
            _emit_root(f"{layout.V_KNOWLEDGE}/{pick}/", "/".join((layout.KNOWLEDGE, *parts_)))
    # Agent-scope (no username): /workspace/ + /knowledge/
    elif not username:
        _emit_root(f"{layout.V_WORKSPACE}/", layout.WORKSPACE)
        _emit_root(f"{layout.V_KNOWLEDGE}/", layout.KNOWLEDGE)
    else:
        # User-scope: own user dir + shared spaces + (manager+) config
        user_rel = safe_fs.rel_under(layout.user_dir(agent_dir, username), agent_dir)
        for sub in layout.USER_SUBDIRS:
            _emit_root(f"{layout.virtual_user_root(username)}/{sub}/", f"{user_rel}/{sub}")
        # Shared workspace + knowledge only when the agent's mode has them
        # (Personal-only omits both — they aren't mounted in that mode).
        if mount_shared:
            _emit_root(f"{layout.V_WORKSPACE}/", layout.WORKSPACE)
            _emit_root(f"{layout.V_KNOWLEDGE}/", layout.KNOWLEDGE)
        if roles.can_manage(role):
            _emit_root(f"{layout.V_CONFIG}/", layout.CONFIG)

    if truncated_count:
        lines.append(
            f"… ({truncated_count} more entries not shown — tree truncated "
            f"at ~{_TREE_MAX_BYTES // 1000}KB)"
        )

    return "\n".join(lines) if lines else None


# Memory injection: the graded fill. The scope's topics, newest date first,
# inject in FULL while the running total stays within the admin
# `inline_budget_bytes` (memory_settings, default 8 KB): a topic that fits
# the room left is inlined, one that does not is listed with its index entry
# (size, dates, sections) for the `memory` tool's `view`. A first fit, so the
# freshest small topics load whole until the budget is spent; a topic over
# the per-file cap (a hand edit) is always listed.
_memory_log = logging.getLogger("otodock.prompts")


def _render_memory_scope(root, *, agent: str, virtual: str, budget: int,
                         writable: bool = True, label: str = "") -> str:
    """One scope's body: the inlined topics, the listed ones, or empty-state
    priming. Each inlined topic is read beneath ``agent``'s real folder
    (``core.agent_folder``) with no link at any part below it, under the
    topic's hard cap. ``label`` names the agent and the scope in the one
    warning a failed index rewrite logs."""
    from core.agent_folder import beneath_agent
    from services.infra import safe_fs
    from services.memory import memory_file, memory_index

    facts = memory_index.scope_facts(root, today=memory_index.today_utc())
    if not facts:
        if writable:
            return (
                f"_No memories saved yet. Create the first topic with the "
                f"`memory` tool: `create {virtual}/<topic>.md`._"
            )
        return "_No memories saved yet._"
    # The on-disk index is healed at every prompt build (a hand edit, an
    # index from an older builder), so `view MEMORY.md` and the Files UI
    # carry what the prompt knows. A folder the proxy cannot write (a
    # restore as another user, a full disk) keeps its old index, and the
    # section still renders from the files: the rules never drop.
    try:
        memory_file.heal_index_if_stale(root)
    except (OSError, memory_file.MemoryOpError):
        _memory_log.warning(
            "memory: the index of %s was not rewritten; the prompt renders from the topic files",
            label or root, exc_info=True,
        )
    order = sorted(
        facts,
        key=lambda f: (f.newest is None, -f.newest.toordinal() if f.newest else 0, f.rel),
    )
    out: list[str] = []
    listed: list[memory_index.TopicFacts] = []
    used = 0
    for f in order:
        if f.size > memory_file.TOPIC_HARD_CAP_BYTES or used + f.size > budget:
            listed.append(f)
            continue
        try:
            data = safe_fs.read_bytes_beneath(*beneath_agent(root / f.rel, agent),
                                             max_size=memory_file.TOPIC_HARD_CAP_BYTES)
        except (OSError, ValueError):
            # Unreadable now (a permission, a file swapped mid-build, a link
            # in its place): keep it visible with its index entry, so `view`
            # can still be tried.
            listed.append(f)
            continue
        content = data.decode("utf-8", errors="replace").rstrip()
        out.append(f"### `{f.rel}`\n\n{content}")
        used += f.size
    if listed:
        out.append(
            "### Topics listed, not loaded\n\n"
            + memory_index.render_capped(listed).rstrip()
            + f'\n\n_(read one with `memory(command="view", path="{virtual}/<topic>")`, '
            "or a section with `view_range`)_"
        )
    return "\n\n".join(out)


def _has_folders(*, external: bool, external_home: str, outside_knowledge: tuple[str, ...]) -> bool:
    """Whether the session can see any folder: an outside person with no
    tree of their own and no folder picked sees none, whatever the files."""
    return not external or bool(external_home or outside_knowledge)


def _kb(n: int) -> str:
    return f"{n // 1024} KB" if n % 1024 == 0 else f"{n:,} bytes"


def _where_files_go(*, username: str | None, role: str, external: bool, wrapup: bool,
                    home: str, files_write: bool) -> str:
    """The workspace listing's last line: where this session writes, in
    sandbox paths."""
    if wrapup:
        return ("\nFiles for this person go to `/caller/workspace/`. Nothing else is written."
                if home else "\nNothing is written after this conversation.")
    if external:
        return ("\nFiles for this person go to `/caller/workspace/`."
                if home and files_write else "\nNothing is written during this conversation.")
    if username:
        line = f"\nUser files go to `{layout.virtual_workspace(username)}/`."
        if roles.can_write_workspace(role):
            line += " Agent-scoped output goes to `/workspace/`."
        return line
    return "\nAgent-scoped output goes to `/workspace/`."


def _notes_apply(model: str) -> bool:
    """Whether the agent's sessions read the notes from conversations: a
    manager may turn them off, and the kept lines then stay for the switch
    to bring back, read by no prompt."""
    from storage.agents import outside_store
    return bool(outside_store.get_wrapup(model)["shared_notes"])


def _render_outside_notes(model: str) -> str:
    """The `# Notes from conversations` section's instructions: what the
    notes are. The notes ride (``_start_notes``)."""
    from services.infra.external_data import NOTE
    if not _notes_apply(model):
        return ""
    return ("\n\n---\n\n# Notes from conversations\n\n"
            "Notes the agent kept after conversations with people outside the "
            f"team, oldest first, come with the context at this start. {NOTE}\n")


def _start_notes(model: str) -> str:
    """The notes from conversations, each fenced as data."""
    from services.infra.external_data import fence
    from storage.chat import conversation_store
    notes = conversation_store.notes_for(model)
    if not notes:
        return "_No notes yet._"
    return "\n".join(f"- {n['day']}: {fence(n['text'])}" for n in notes)


def _memory_sections(model: str, agent_dir: Path, *,
                     username: str | None, role: str,
                     external: bool = False,
                     external_home: str = "",
                     wrapup: bool = False,
                     content: bool = True) -> tuple[str, str] | None:
    """The `# Memory` prompt section's two halves: the capture directive
    (the stable text) and the per-scope content (the riding part, which
    ``_start_memory`` renders; ``core/session/start_context.py``).

    Scope loading matrix: user-scoped sessions get agent + user memory;
    agent-scoped sessions (no username) get agent memory only. A viewer's
    agent scope is read-only. An EXTERNAL session (an outside person who is
    not a platform user) never gets the agent memory; with a home
    (``external_home``) the person's own memory is read to the agent and
    never written during the conversation (the memory tool is not in the
    session: the wrap-up after it records what matters). The WRAP-UP after
    a conversation (``wrapup``) writes that one person's memory
    (``external_home`` their tree) and reads no other. Returns None when
    memory is fully disabled.

    Only the content half reads the topic files (and heals their index),
    and only with ``content``: a directive rendered twice with the same
    settings is the same bytes.
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
        and not wrapup
    )
    user_enabled = bool(
        (external_home if external or wrapup else username)
        and settings.get("user_memory_enabled")
        and toggles.get("user_memory_enabled")
        and _mode_user_ok
    )
    if not (agent_enabled or user_enabled):
        return None

    budget = int(settings.get("inline_budget_bytes") or 8192)
    caps = memory_file.Caps.from_settings(settings)

    parts: list[str] = ["\n\n---\n\n# Memory\n"]
    data: list[str] = []

    default_scope = agent_row.get("default_scope") or (
        "user" if user_enabled else "agent"
    )
    if default_scope == "user" and not user_enabled:
        default_scope = "agent"
    if default_scope == "agent" and not agent_enabled:
        default_scope = "user"
    # The shared memory is the editor tier's to write (the memory API's
    # rule): below it, the person's own scope is the default, never a
    # scope the role cannot write.
    agent_writable = roles.can_edit(role)
    if not agent_writable and user_enabled:
        default_scope = "user"

    scope_lines = []
    if agent_enabled:
        scope_lines.append(
            "- `/memories/agent/` is shared with every user of this agent."
        )
    if user_enabled:
        scope_lines.append(f"- `/memories/user/` is private to {username}.")
    # The one scope rule, for a session that can write both scopes; a
    # viewer or a contributor reads the shared memory and saves user-scope
    # only.
    if agent_enabled and user_enabled and agent_writable:
        scope_rule = (
            " About a person, their preferences, their situation: user "
            "scope. About the work, the systems, the decisions, the traps: "
            "agent scope. When a fact is both, the fact goes shared and the "
            "preference personal. A process the team follows is knowledge, "
            "not memory."
        )
    elif agent_enabled and not agent_writable:
        scope_rule = (
            " Agent memory is read-only for your role: save user-scope "
            "memories only." if user_enabled
            else " Agent memory is read-only for your role."
        )
    else:
        scope_rule = ""
    if wrapup:
        parts.append(
            "\nYou keep this person's memory with the `memory` tool at "
            "`/memories/user/`: what they told you and what concerns them, a "
            "fact or a decision per entry, dated (YYYY-MM-DD), one to three "
            "lines. Update the existing entry instead of adding one. Never "
            "anything about another person, and never what you read in your "
            "own files or tools about anyone. Nothing to keep is a valid "
            "answer. This person's memory comes with the context at this start.\n"
        )
        root = Path(external_home) / layout.CONTEXT / "memory"
        if content:
            data.append(
                "## This person's memory\n\n"
                + _render_memory_scope(
                    root, agent=model, virtual="/memories/user", budget=budget,
                    writable=True, label=f"{model} (caller scope)",
                )
            )
        return "".join(parts), "\n\n".join(data)
    if external:
        directive = (
            "\nWhat you remember of this person comes with the context at "
            "this start. Nothing you learn "
            "in this conversation is saved while it runs: what matters is "
            "recorded for this person after it ends, so do not promise to "
            "remember anything beyond that.\n"
        )
        parts.append(directive)
        root = Path(external_home) / layout.CONTEXT / "memory"
        if content:
            data.append(
                "## This person's memory\n\n"
                + _render_memory_scope(
                    root, agent=model, virtual="/memories/user", budget=budget,
                    writable=False, label=f"{model} (caller scope)",
                )
            )
        return "".join(parts), "\n\n".join(data)
    directive = (
        "\nYou have persistent memory: markdown topic files you manage with "
        "the `memory` tool.\n"
        + "\n".join(scope_lines)
        + f"\nDefault for new memories: `/memories/{default_scope}/`.{scope_rule}\n\n"
        "A memory is a fact, a rule, a decision or a pointer, dated "
        "(YYYY-MM-DD), one to three lines. Not a log, a status or a "
        "transcript, not anything your prompt already says. Save it the "
        "moment you learn it, or when asked to remember something. Update "
        "the existing entry instead of adding one. A changed fact is "
        "replaced, with one line on what it was until when. A topic is one "
        "subject: a `# heading`, a "
        "one-sentence summary line, `##` sections. Keep it under "
        f"{_kb(memory_file.TOPIC_SOFT_WARN_BYTES)}: the tool refuses growth "
        f"past {_kb(caps.grow_cap)} and an edit that adds over "
        f"{_kb(caps.entry_cap)}. Delete what no longer governs. `MEMORY.md` "
        "is generated, never edit it. Never store secrets. This is the only "
        "memory in play: the engine's own memory is off. The topics come "
        "with the context at this start. If no such context reached you, list "
        "them with the `memory` tool before you answer from memory.\n"
    )
    parts.append(directive)

    if agent_enabled and content:
        root = memory_file.scope_root(agent_dir, "agent")
        data.append(
            "## Agent memory (shared)\n\n"
            + _render_memory_scope(
                root, agent=model, virtual="/memories/agent", budget=budget,
                writable=agent_writable, label=f"{model} (agent scope)",
            )
        )
    if user_enabled and content:
        root = memory_file.scope_root(agent_dir, "user", username)
        data.append(
            f"## User memory ({username})\n\n"
            + _render_memory_scope(
                root, agent=model, virtual="/memories/user", budget=budget,
                label=f"{model} (user scope, {username})",
            )
        )
    return "".join(parts), "\n\n".join(data)


def _has_delegation_tools(model: str, placement: PlacementCapabilities) -> bool:
    """Whether the agent's sessions get the delegation MCP, where the
    Notebook's two tools live (assigned to the agent, on for the platform)."""
    with contextlib.suppress(Exception):
        from services.mcp import mcp_registry
        manifests = mcp_registry.get_agent_mcps(model, placement=placement) or []
        return any(m.name == "delegation-mcp" for m in manifests)
    return False


def _notebook_functions(model: str, placement: PlacementCapabilities,
                        tool_limits=None) -> tuple[bool, bool]:
    """Whether the session holds ``notebook_read`` and ``notebook_post``:
    the delegation MCP assigned (``_has_delegation_tools``), each function
    within the session's tool limits (``tool_limits.allows``)."""
    if not _has_delegation_tools(model, placement):
        return False, False
    with contextlib.suppress(Exception):
        from services.mcp import mcp_registry, tool_limits as _tool_limits
        from services.mcp.outside_picks import server_key
        manifest = mcp_registry.get_all_manifests().get("delegation-mcp")
        key = server_key(manifest) if manifest is not None else "delegation-mcp"
        return (_tool_limits.allows(tool_limits, key, "notebook_read"),
                _tool_limits.allows(tool_limits, key, "notebook_post"))
    return False, False


def _render_notebook(model: str, *, role: str, external: bool, read_only: bool,
                     client_type: str, placement: PlacementCapabilities,
                     tool_limits=None) -> str | None:
    """The ``# Notebook`` section (``services/notebook/notebook_prompt.py``):
    the latest entries of the agent's read set and when to post. Never for
    an external caller. Whether the session may post is
    ``posting.posting_refusal``, the floor the posting route applies."""
    if external:
        return None
    from services.notebook import notebook_prompt, posting
    refusal = posting.posting_refusal(
        model, role=role, read_only=read_only, client_type=client_type,
    )
    reads, posts = _notebook_functions(model, placement, tool_limits)
    return notebook_prompt.render(model, refusal=refusal, reads=reads, posts=posts)


def _notebook_applies(model: str, *, role: str, external: bool, read_only: bool,
                      client_type: str, placement: PlacementCapabilities,
                      tool_limits=None) -> bool:
    """Whether the session's text has the ``# Notebook`` section, so its
    entries ride."""
    if external:
        return False
    with contextlib.suppress(Exception):
        from services.notebook import notebook_prompt, posting
        refusal = posting.posting_refusal(
            model, role=role, read_only=read_only, client_type=client_type,
        )
        return notebook_prompt.applies(
            model, refusal=refusal, posts=_notebook_functions(model, placement, tool_limits)[1])
    return False


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
                       external: bool = False,
                       external_home: str = "",
                       user_role: str | None = None,
                       shared_chat: bool = False,
                       read_only: bool = False,
                       outside_picks=None,
                       kind_rule=None,
                       wrapup: bool = False,
                       wrapup_home: str = "",
                       tool_limits=None,
                       session_id: str = "") -> str | None:
    """Build the full system prompt for an agent.

    The text is built only from inputs that change rarely, so two builds
    with the same inputs are the same bytes and a session that starts again
    inside the engine's cache lifetime reads it from the cache. The data
    that changes (memory topics, notes from conversations, Notebook
    entries, the folder tree, the parallel sessions) rides on the start's
    first turn instead (``core/session/start_context.py``), rendered by
    ``start_sections`` from the arguments recorded here for ``session_id``.

    ``tool_limits`` (``services/mcp/tool_limits``, the session's pairs of
    server key and names): the section "Tools you may use in part", so the
    agent knows what it may use of a limited tool and what to say.

    ``outside_picks`` (an external session's, ``services/mcp/outside_picks``)
    reaches the three readers of the one tool rule, so the catalog and the
    skills agree with the config about what an outside person gets.

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

    ``read_only`` = a check's judge: its Notebook section says it
    cannot post, as the posting route refuses it.

    ``kind_rule`` (``mcp_session_filter.KindRule``): a session kind's own
    hard set, which the catalog and the skills read with the config.
    ``wrapup`` is the wrap-up after a conversation: the person's memory
    (``wrapup_home`` their tree, "" for none) is the one memory it writes,
    and it reads neither the shared memory, the notes from conversations
    nor the Notebook.

    Section order:

      1. Agent's own ``config/agent.md`` (the persona)
      2. Platform company context (DB) + universal language rule
      3. ``# Auto-Loaded Documentation`` — ``config/context/*`` knowledge files
      4. ``# Available Tools (MCPs)`` — catalog of enabled MCPs (one-liners)
      5. ``# MCP Tool Skills`` — per-MCP deep-dive docs from manifests
      5b. ``# Skills`` — on-demand skill catalog (Direct-LLM sessions only)
      6. ``# MCP Dynamic Context`` — runtime-generated context per MCP
      7. ``# User Context (Personal)`` — ``users/{u}/context/*`` personal docs
      7b. ``# Notebook`` — the read set's latest entries (``services/notebook/``)
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
            shared_chat=shared_chat,
            placement=placement,
            external=external,
            outside_picks=outside_picks,
            kind_rule=kind_rule,
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
            shared_chat=shared_chat,
            placement=placement,
            external=external,
            user_role=user_role,
            outside_picks=outside_picks,
            kind_rule=kind_rule,
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
                shared_chat=shared_chat,
                placement=placement,
                external=external,
                user_role=user_role,
                outside_picks=outside_picks,
                kind_rule=kind_rule,
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
        ctx_docs, ctx_links = _person_context_docs(model, user_ctx_dir, _USER_CTX_MAX_FILE_BYTES)
        if ctx_docs or ctx_links:
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
            skipped: list[tuple[str, int | None, str]] = []  # (name, size, reason)
            for name, size, data in ctx_docs:
                if size > _USER_CTX_MAX_FILE_BYTES or data is None:
                    skipped.append((name, size, "exceeds per-file cap"))
                    continue
                if running_bytes + size > _USER_CTX_MAX_TOTAL_BYTES:
                    skipped.append((name, size, "exceeds total cap"))
                    continue
                with contextlib.suppress(Exception):
                    parts.append(
                        f"\n## `{name}`\n\n{data.decode('utf-8')}"
                    )
                    running_bytes += size
            skipped.extend((name, None, "a link, not loaded") for name in ctx_links)
            if skipped:
                skipped_md = "\n".join(
                    f"- `{n}`: {reason}" if sz is None else f"- `{n}` ({sz:,} bytes): {reason}"
                    for n, sz, reason in skipped
                )
                parts.append(
                    "\n## `_user_context_skipped.md`\n\n"
                    "The following user-context files were skipped.\n\n"
                    + skipped_md
                )

    # Memory — agent-maintained topic files (knowledge/memory/ shared,
    # users/{u}/context/memory/ per-user) with the capture directive. The
    # generic context loaders above never see these dirs (user-context glob
    # is flat; knowledge/ isn't auto-loaded) — this is their ONLY injection
    # point. Best-effort: a memory failure never breaks prompt build.
    # The agent's own line: anyone who dials hears what the prompt carries,
    # so it carries none of the agent's records. The memory is shut to the
    # line in its mounts, its path policy and the memory API too.
    open_line = bool(getattr(kind_rule, "open_line", False))
    with contextlib.suppress(Exception):
        memory_section = None if open_line else _memory_sections(
            model, agent_dir, username=username, role=role,
            external=external, external_home=wrapup_home if wrapup else external_home,
            wrapup=wrapup, content=False,
        )
        if memory_section:
            parts.append(memory_section[0])

    # The notes from conversations (a wrap-up's one line for the team, when
    # the agent's switch is on): data from outside, in members' and the
    # agent's own sessions, never in a call of any line nor in a wrap-up.
    from core.session.session_kind import PHONE as PHONE_KIND
    if not (external or wrapup or client_type == PHONE_KIND.name):
        with contextlib.suppress(Exception):
            notes_section = _render_outside_notes(model)
            if notes_section:
                parts.append(notes_section)

    # The Notebook — the latest entries of the agents one delegation edge
    # away (and the agent's own), read server-side so every kind of session
    # gets them. Best-effort like memory.
    with contextlib.suppress(Exception):
        notebook_section = _render_notebook(
            model, role=role, external=external or wrapup or open_line, read_only=read_only,
            client_type=client_type, placement=placement, tool_limits=tool_limits,
        )
        if notebook_section:
            parts.append(notebook_section)

    # Workspace: where files go. The folder tree rides (``start_sections``).
    outside_knowledge = tuple(getattr(outside_picks, "knowledge", ()) or ())
    if _has_folders(external=external, external_home=external_home,
                    outside_knowledge=outside_knowledge):
        parts.append(
            "\n\n---\n\n"
            "# Workspace\n\n"
            "The tree of the folders this session reaches comes with the context "
            "at this start.\n"
        )
        parts.append(_where_files_go(
            username=username, role=role, external=external, wrapup=wrapup,
            home=wrapup_home if wrapup else external_home,
            files_write=bool(getattr(outside_picks, "files_write", False))))

    # Excluded MCPs section (tools unavailable in this session). On an
    # external session the entries that ARE the external rules are left
    # out: a caller-facing prompt must not enumerate the platform's
    # management surface (there is nothing the model could "configure").
    listed_exclusions = dict(excluded_mcps or {})
    if external and listed_exclusions:
        with contextlib.suppress(Exception):
            from services.mcp.mcp_session_filter import EXTERNAL_REASONS
            listed_exclusions = {
                k: v for k, v in listed_exclusions.items() if v not in EXTERNAL_REASONS
            }
    if listed_exclusions:
        parts.append(_UNAVAILABLE_HEAD)
        for mcp_name, reason in sorted(listed_exclusions.items()):
            parts.append(_unavailable_row(mcp_name, reason))
        parts.append(_UNAVAILABLE_TAIL)
    if tool_limits:
        with contextlib.suppress(Exception):
            from services.mcp import tool_limits as _tool_limits
            section = _tool_limits.prompt_section(tool_limits)
            if section:
                parts.append("\n\n---\n\n" + section)

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

    if session_id:
        from core.session import start_context
        start_context.record(session_id, StartArgs(
            agent=model, username=username or "", role=role, sandboxed=sandboxed,
            client_type=client_type, mount_shared=mount_shared, external=external,
            external_home=external_home,
            outside_knowledge=outside_knowledge,
            wrapup=wrapup, wrapup_home=wrapup_home, open_line=open_line,
            notes=not (external or wrapup or client_type == PHONE_KIND.name),
            notebook=_notebook_applies(model, role=role, external=external or wrapup or open_line,
                                       read_only=read_only, client_type=client_type,
                                       placement=placement, tool_limits=tool_limits),
            notebook_tools=_notebook_functions(model, placement, tool_limits)[0],
        ))
    return "\n".join(parts)


@dataclasses.dataclass(frozen=True)
class StartArgs:
    """The builder's own arguments a session's riding part is rendered from
    (``build_agent_prompt`` records them for its session), never derived
    from the session's security context: an agent-scope run mounts no
    person's folders, whoever created it."""
    agent: str
    username: str = ""
    role: str = "manager"
    sandboxed: bool = True
    client_type: str = ""
    mount_shared: bool = True
    external: bool = False
    external_home: str = ""
    outside_knowledge: tuple[str, ...] = ()
    wrapup: bool = False
    wrapup_home: str = ""
    open_line: bool = False
    notes: bool = True
    notebook: bool = False
    notebook_tools: bool = False

    def to_dict(self) -> dict:
        return {**dataclasses.asdict(self), "outside_knowledge": list(self.outside_knowledge)}

    @classmethod
    def from_dict(cls, raw: object) -> "StartArgs | None":
        """The arguments a persisted record holds; None for one that does
        not name an agent (an index from another build)."""
        if not isinstance(raw, dict) or not isinstance(raw.get("agent"), str):
            return None
        names = {f.name for f in dataclasses.fields(cls)}
        kept = {k: v for k, v in raw.items() if k in names}
        kept["outside_knowledge"] = tuple(str(x) for x in kept.get("outside_knowledge") or ())
        try:
            return cls(**kept)
        except TypeError:
            return None


def start_sections(args: StartArgs) -> list[tuple[str, str, str]]:
    """The riding sections of a session's start, as ``(key, title, body)``:
    its memory, the notes from conversations, the Notebook's entries and its
    folder tree, rendered by the helpers the text's sections use, for the
    same kind of session (``build_agent_prompt`` says which apply). A
    section that applies is always rendered, empty or not, so a newer one
    replaces an older one whole. A section that fails to render is left
    out; the others still ride."""
    import config
    from core.session.start_context import TITLES
    if not config.is_safe_agent_name(args.agent):
        return []
    agent_dir = config.get_agent_dir(args.agent)
    out: list[tuple[str, str, str]] = []
    if not args.open_line:
        with contextlib.suppress(Exception):
            memory = _memory_sections(
                args.agent, agent_dir, username=args.username or None, role=args.role,
                external=args.external,
                external_home=args.wrapup_home if args.wrapup else args.external_home,
                wrapup=args.wrapup,
            )
            if memory:
                out.append(("memory", TITLES["memory"], memory[1] or "_No memory topics._"))
    if args.notes:
        with contextlib.suppress(Exception):
            if _notes_apply(args.agent):
                out.append(("notes", TITLES["notes"], _start_notes(args.agent)))
    if args.notebook:
        with contextlib.suppress(Exception):
            from services.notebook import notebook_prompt
            entries = notebook_prompt.render_entries(args.agent, reads=args.notebook_tools)
            if entries:
                out.append(("notebook", TITLES["notebook"], entries))
    if not _has_folders(external=args.external, external_home=args.external_home,
                        outside_knowledge=args.outside_knowledge):
        return out
    with contextlib.suppress(Exception):
        tree = _scan_workspace(
            agent_dir, args.agent, username=args.username or None, role=args.role,
            sandboxed=args.sandboxed, mount_shared=args.mount_shared,
            external_home=args.external_home, external=args.external,
            outside_knowledge=args.outside_knowledge)
        out.append(("workspace", TITLES["workspace"],
                    f"```\n{tree}\n```" if tree else "_No folder is visible to this session._"))
    return out


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
            text = mcp_manifest_parse.read_skill_file(manifest.mcp_dir, skill_def.file)
            if text is not None:
                out.append(strip_frontmatter(text))
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
