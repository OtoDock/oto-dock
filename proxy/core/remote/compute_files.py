"""The files a session on a compute machine on another host needs from the
platform's preparation.

The engine's layer prepares a compute session on the platform
(``ExecutionLayer.prepare_spawn``): the settings, the hooks and their
scripts, the wrappers, the tool config copy, the prompt file, the login file,
the terminal's seed, Codex's ``config.toml`` and the session's own profile
and text (``core/layers/codex/session_text.py``), all in the session's
config folder. A compute machine beside the platform reads them
through the shared folder. One on another host has its own copy, which the
sync never fills with them (they are host-local, ``file_sync``), so they ride
the session's start frame as a paired machine's prompt, login file and tool
config do (``host_files``, keyed by the path below the agent's folder): the
satellite writes them under its own agent folder before the render and
removes them at the close (``satellite/sessions/session_files.py``).

What goes: the files the preparation writes in the config folder, named
where they are written (the per-host config files ``file_sync`` never
syncs, ``session_config_dir``'s hook scripts, interceptor, exit bundle and
wrapper folder, the terminal's ``.claude.json`` seed, this session's tool
config copy), never the engine's own state there (its databases, caches,
history, transcripts: the machine's engine keeps its own) nor the person's
folders (the sync carries them), plus this session's prompt file, plus every
file the sandbox's description
binds below the agent's folder (another engine's gate script, a hook file);
a folder it binds goes as a name to make (``folders``), since the shim
refuses a bind whose source is missing and the sync never carries a hidden
folder. Bounded: a set over the bounds refuses the start rather than send
part of it.
"""

from __future__ import annotations

import base64
import fnmatch
import os
import stat
from pathlib import Path

from services.infra import safe_fs

#: Bounds on what one session's start frame carries.
MAX_FILES = 400
MAX_BYTES = 4 * 1024 * 1024


class TooMuchToSend(RuntimeError):
    """The session's prepared files exceed the bounds."""


def _foreign(name: str, session_id: str, own_prompt: str) -> bool:
    """Another session's own file in a shared config folder: its prompt
    file, its tool config copy (named with the session's first twelve
    characters), its Codex profile or text, or a thread's record (the
    machine keeps its own)."""
    from core.layers.cli.spawn import _PROMPT_GLOB
    if fnmatch.fnmatch(name, _PROMPT_GLOB):
        return name != own_prompt
    from core.layers.codex import session_text
    if session_text.is_session_file(name):
        return not session_text.is_own_file(name, session_id)
    from core.remote.file_sync import is_session_copy_name
    return is_session_copy_name(name) and not name.endswith(f"-{session_id[:12]}.json")


#: ``cli_settings_manager``'s seed past the terminal's first run.
_TERMINAL_SEED = ".claude.json"


def _prepared(name: str) -> bool:
    """A file the preparation writes directly in a session's config folder."""
    from core.layers.codex import session_text
    from core.remote import file_sync
    from core.sandbox import session_config_dir as scd
    return (file_sync.is_host_local_config_name(name) or file_sync.is_session_copy_name(name)
            or session_text.is_session_file(name)
            or name in scd.HOOK_SCRIPTS or name == scd._INTERCEPTOR_SRC.name
            or name == _TERMINAL_SEED)


def folders(agent_dir: Path, sandbox: dict) -> list[str]:
    """The folders the description binds below the agent's folder, by the
    path below it: the machine makes the ones its copy lacks. A folder is
    judged where it is named, with no link followed below the agent's
    folder (itself taken by its realpath, as the render takes it)."""
    out: list[str] = []
    for rel in _agent_binds(sandbox):
        try:
            if stat.S_ISDIR(safe_fs.lstat_beneath(agent_dir, rel).st_mode):
                out.append(rel)
        except OSError:
            continue
    return sorted(set(out))


def _agent_binds(sandbox: dict) -> list[str]:
    """The paths below the agent's folder that the description binds (its
    role table and its extra binds, root ``agents``; the folder itself is
    not one), as written: every open of one refuses a ``..``, an absolute
    path or a link on the way."""
    from core.sandbox import sandbox_render as render
    return [str(bind["rel"])
            for bind in (*(sandbox.get("binds") or ()), *(sandbox.get("extra_binds") or ()))
            if isinstance(bind, dict) and bind.get("root") == render.ROOT_AGENT and bind.get("rel")]


def _rel_below(agent_dir: Path, path: str) -> str:
    """``path`` as written, as the path below the agent's folder (``""``
    outside it or for the folder itself); never resolved."""
    try:
        return safe_fs.rel_under(path, agent_dir)
    except (OSError, ValueError):
        return ""


def _listing(root: Path, rel: str) -> tuple[list[str], list[str]]:
    """The regular files and the folders directly in ``rel`` beneath
    ``root``, reached with no link followed; nothing when it cannot be
    listed."""
    walk = safe_fs.walk_beneath(root, rel)
    try:
        step = next(walk)
        return list(step.files), list(step.dirs)
    except (StopIteration, OSError):
        return [], []
    finally:
        walk.close()


def collect(agent_dir: Path, config_dir: str, prompt_file: str, session_id: str,
            sandbox: dict | None = None) -> dict[str, dict]:
    """The start frame's files for one session, keyed by the path below the
    agent's folder (``{"content_b64", "mode"}`` each). ``config_dir``
    and ``prompt_file`` are the plan's platform paths, ``sandbox`` its
    description; anything outside the agent's folder is not sent (the
    description cannot name it either). Every file is named and read where
    the plan wrote it, beneath the agent's folder (taken by its realpath, as
    the render takes it) with no link followed below it (a session writes
    these folders), and read only up to what the frame has left."""
    from core.remote import file_sync
    out: dict[str, dict] = {}
    total = 0
    own_prompt = Path(prompt_file).name if prompt_file else ""

    def add(rel: str) -> None:
        nonlocal total
        try:
            fd, st = safe_fs.open_regular_for_read(agent_dir, rel)
        except OSError:
            return
        try:
            data = b"".join(safe_fs.iter_fd(fd, length=max(MAX_BYTES - total, 0) + 1))
        finally:
            os.close(fd)
        total += len(data)
        if len(out) >= MAX_FILES or total > MAX_BYTES:
            raise TooMuchToSend(f"more than {MAX_FILES} files or {MAX_BYTES // (1024 * 1024)} MB")
        out[rel] = {"content_b64": base64.b64encode(data).decode("ascii"),
                    "mode": st.st_mode & 0o777}

    cfg = _rel_below(agent_dir, config_dir) if config_dir else ""
    if cfg:
        from core.sandbox.session_config_dir import WRAPPER_DIR
        files, dirs = _listing(agent_dir, cfg)
        if WRAPPER_DIR in dirs:
            for name in _listing(agent_dir, f"{cfg}/{WRAPPER_DIR}")[0]:
                add(f"{cfg}/{WRAPPER_DIR}/{name}")
        # The session's own home (``session_home``): its files, never another
        # session's home, and never through a link (the listing enters none;
        # the machine makes the home's links itself).
        from core.sandbox import session_home
        from core.sandbox.sandbox_render import STORE_SIGN_IN_FILES
        home = f"{cfg}/{session_home.relative(session_id)}" if session_home.valid_id(session_id) else ""
        home_files = _listing(agent_dir, home)[0] if home else []
        # A session in a home reads its home's sign-in: the store's (an
        # earlier version's) never rides its frame, so a machine with its own
        # copy never writes it under another session's mask.
        skip = STORE_SIGN_IN_FILES.get(cfg.rsplit("/", 1)[-1], "") if home_files else ""
        for name in files:
            if name != skip and _prepared(name) and not _foreign(name, session_id, own_prompt):
                add(f"{cfg}/{name}")
        for name in home_files:
            add(f"{home}/{name}")
    prompt = _rel_below(agent_dir, prompt_file) if prompt_file else ""
    if prompt:
        add(prompt)
    for rel in _agent_binds(sandbox or {}):
        parts = rel.split("/")
        runtime = any(file_sync.is_runtime_child(a, b) for a, b in zip(parts, parts[1:], strict=False))
        if not runtime and not _foreign(parts[-1], session_id, own_prompt):
            add(rel)
    return out
