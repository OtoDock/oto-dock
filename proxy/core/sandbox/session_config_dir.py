"""Per-session config-directory setup for agent sandboxes — the shared spine.

Where a session's config dir lives under its scope root and what every
engine installs into it (the hook scripts, the stdio interceptor), the one
funnel the session-config builders call (``ensure_persistent_agent_dir`` →
``ExecutionLayer.prepare_config_dir``), the sandbox-side MCP config copy
and the SSH key materialisation. The engine-specific bodies — Claude's
``settings.json`` and its built-in deny list, Codex's ``hooks.json`` — are
the engines' own (``core/layers/cli/config_dir.py``,
``core/layers/codex/config_dir.py``). ``sandbox.py`` re-exports the funnel
and the MCP config copy for its historical importers.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path

import config as app_config
from core import layout, placement
from core.agent_folder import agent_holding, agent_root, beneath_agent

logger = logging.getLogger("otodock.sandbox")


# Hooks source directory (proxy/hooks/)
_HOOKS_DIR = app_config.BASE_DIR / "hooks"

# The stdio interceptor lives at core/ (one level above this subpackage —
# it must stay there: the satellite vendors it by sha256 from that path). It
# is copied into each session's .claude/.codex dir (like the hooks) so it is
# reachable INSIDE the bwrap sandbox — stdlib-only, run via the sandbox's
# `python3`. Used by the credential broker (fetch-at-spawn) + tool-arg-path
# translation.
_INTERCEPTOR_SRC = Path(__file__).resolve().parent.parent / "stdio_path_interceptor.py"


def write_beneath_agent(agent_name: str, path: str | Path, data: bytes, *,
                        mode: int | None = None) -> None:
    """Write a file of a session's config folder whole (a temp beside it,
    then one rename) from the agent's real folder with no link at any part
    (``core.agent_folder``): a link at the name is replaced, never written
    through. For a file no sandbox binds by name: a rename would detach a
    mount made over the old file, so such a file keeps its inode
    (:func:`write_no_follow`). ``mode`` None keeps an existing file's bits,
    a new one gets the umask's."""
    from services.infra import safe_fs
    root, rel = beneath_agent(path, agent_name)
    safe_fs.atomic_write_beneath(root, rel, data, mode=mode, fsync=False)


def _write_in_place(root: Path, rel: str, data: bytes, mode: int) -> None:
    """Write ``rel`` beneath ``root`` keeping its inode: a link at the name
    is removed, the file is reached with no link at any part and without
    waiting on a pipe, refused unless it is a regular file, then truncated,
    given ``mode`` and written. A file with a second name on the disk is
    never written into: the name gets a file of its own, held back while a
    session's home may hold a mask over it (:func:`_a_home_may_mask`)."""
    from services.infra import safe_fs
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISLNK(safe_fs.lstat_beneath(root, rel).st_mode):
            safe_fs.unlink_beneath(root, rel)
    fd = safe_fs.open_beneath(root, rel, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise safe_fs.NotRegularFile(errno.EINVAL, "not a regular file", rel)
        if st.st_nlink != 1:
            if _a_home_may_mask(root, rel):
                raise SignInHasAnotherName(errno.EMLINK, "file has other links", rel)
            os.close(fd)
            fd = -1
            safe_fs.unlink_beneath(root, rel, missing_ok=True)
            fd = safe_fs.open_beneath(root, rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.ftruncate(fd, 0)
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        if fd != -1:
            os.close(fd)


class SignInHasAnotherName(OSError):
    """A sign-in file with a second name on the disk while a session's home
    may hold a mask over it: it is not written. Its text is what the person
    reads on the failed start's card."""

    def __str__(self) -> str:
        return ("This chat's sign-in file has a second name on the server's disk, so it was not written. "
                "It is written once the agent's other chats have ended, or when an admin removes the second name.")


def _a_home_may_mask(root: Path, rel: str) -> bool:
    """Whether a running session may hold a mask over ``rel``: a session
    with a home masks the sign-in of every engine folder of the agent it
    sees (``SandboxBuilder._home_parts``), so a home anywhere in the agent's
    folder counts. A home's own file is masked by no one."""
    from core.sandbox import session_home
    from services.infra import safe_fs
    if session_home.is_home(rel.rpartition("/")[0]):
        return False

    def _names(folder: str) -> list[str]:
        try:
            fd = safe_fs.open_dir_beneath(root, folder)
        except OSError:
            return []
        try:
            with os.scandir(fd) as entries:
                return [e.name for e in entries]
        finally:
            os.close(fd)

    scopes = [layout.WORKSPACE, *(f"{layout.USERS}/{u}" for u in _names(layout.USERS))]
    return any(_names(f"{scope}/{engine}/{session_home.OWN}")
               for scope in scopes for engine in HOOK_FILE_OF_STATE_DIR)


def write_no_follow(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` in place, never through a link.

    The ``.claude``/``.codex`` trees sit inside agent-WRITABLE binds, so an
    agent can replace ``settings.json`` (or a hook script, or a folder on
    the way) with a link between sessions. In an agent's folder the file is
    reached from :func:`agent_root` with no link at any part
    (:func:`_write_in_place`), and keeps its inode: the store's sign-in is
    the mountpoint of every running home session's mask, which a rename
    would detach. A path outside every agent's folder keeps the leaf rule:
    an existing symlink is unlinked and the file recreated, ``O_NOFOLLOW``
    closes the race, the mode set via ``fchmod`` on the open fd.
    """
    agent = agent_holding(path)
    if agent:
        _write_in_place(*beneath_agent(path, agent), data, mode)
        return
    try:
        if path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                 0o644)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd != -1:
            os.close(fd)


#: The marker in the name of an in-flight temp file of ``write_if_changed``
#: (``.<name>.oto-tmp-<hex>`` beside its target). File sync keeps these
#: host-local on both sides (``core/remote/file_sync.py``, the satellite's
#: twin carries the same marker).
WRITE_TEMP_MARKER = ".oto-tmp-"
#: A temp older than this is a crashed start's and goes at the next write in
#: its directory; a younger one is a concurrent start's, still in flight.
STALE_TEMP_S = 600.0
_REPLACE_ATTEMPTS = 3
#: The writes of one folder, one at a time within the server (a folder's
#: name picks its lock): sibling starts of one scope write the same files at
#: once, and one writer's honest replace between another's rename and its
#: check would read to that writer as tampering.
_FOLDER_LOCKS = tuple(threading.Lock() for _ in range(64))


def _holds(dirfd: int, name: str, data: bytes, mode: int) -> bool:
    """Whether ``name`` in the folder ``dirfd`` is a regular file of one
    link, with ``mode`` and exactly ``data``, judged on the descriptor
    itself: a link at the name is not followed and a FIFO swapped in cannot
    block the read."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or stat.S_IMODE(st.st_mode) != mode or st.st_size != len(data)):
            return False
        with os.fdopen(fd, "rb") as f:
            fd = -1
            return f.read(len(data) + 1) == data
    finally:
        if fd != -1:
            os.close(fd)


def is_fresh_write_temp(entry: os.DirEntry, now: float | None = None) -> bool:
    """A temp of ``write_if_changed`` young enough to be another start's
    write in flight (a directory sweep leaves it alone)."""
    if not entry.name.startswith(".") or WRITE_TEMP_MARKER not in entry.name:
        return False
    try:
        mtime = entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - mtime < STALE_TEMP_S


def _sweep_stale_temps(dirfd: int) -> None:
    """Remove the temps a crashed start left in the folder ``dirfd`` (files
    and links only, never followed); a fresh one belongs to a concurrent
    start."""
    try:
        entries = os.scandir(dirfd)
    except OSError:
        return
    with entries:
        for entry in entries:
            if (not entry.name.startswith(".") or WRITE_TEMP_MARKER not in entry.name
                    or entry.is_dir(follow_symlinks=False) or is_fresh_write_temp(entry)):
                continue
            with contextlib.suppress(OSError):
                os.unlink(entry.name, dir_fd=dirfd)


def _folder_of(path: Path) -> int:
    """A listable handle on ``path``'s folder (:func:`_open_folder`)."""
    return _open_folder(path.parent)


def _open_folder(folder: Path) -> int:
    """A listable handle on ``folder``: beneath the agent's real folder
    (:func:`agent_root`) with no link at any part below it when the folder
    is in an agent's folder, so the folder a start checked is the folder
    written; a folder in no agent's folder (a test's) is opened as named,
    no link at its own name. The caller closes it."""
    from services.infra import safe_fs
    agent = agent_holding(folder)
    if agent:
        return safe_fs.open_dir_beneath(*beneath_agent(folder, agent))
    return os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def unlink_in(folder: Path, name: str) -> None:
    """Remove ``name`` in ``folder`` through a handle on the folder
    (:func:`_open_folder`): a link at the name goes, never its target, and a
    link at the folder or above it is refused. A name or a folder that is
    not there is fine."""
    try:
        fd = _open_folder(folder)
    except FileNotFoundError:
        return
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def restrict_dir(agent_name: str, folder: Path) -> None:
    """``chmod 0700`` on an engine folder through a handle opened beneath
    the agent's real folder (:func:`beneath_agent`) with no link at any part
    below it, never by its path."""
    from services.infra import safe_fs
    fd = safe_fs.open_dir_beneath(*beneath_agent(folder, agent_name))
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def write_if_changed(path: Path, data: bytes, mode: int = 0o644) -> bool:
    """Make ``path`` hold ``data`` with ``mode``; True when it was written.

    A file that already holds them is left alone: the hook files are
    rewritten at every session start, and a rewrite in place let a hook a
    sibling session launched read a half-written script. A changed file is
    written to a temp beside it and renamed onto the name, so a reader sees
    the old file or the new one, never a part: a link at the name is
    replaced, never followed, and a directory there is removed. The temp
    stays beside its target because each scope may be its own XFS quota
    project, which a rename across refuses. The tree is writable from inside
    a running sandbox, so after the rename the name must hold the temp's own
    inode with one link, or the same bytes another start wrote at the same
    moment: a sibling that swapped or linked the temp during the write is
    caught, its file removed and the write retried, and a write tampered
    with every time raises (the session start fails).

    A replace detaches the read-only file bind a running sandbox holds on
    the name (the kernel drops a mount whose mountpoint is renamed over from
    another namespace), and that session then sees the new file through its
    writable dir. So the content of every file the sandbox binds by name
    must depend only on the scope's paths and the proxy's build, and a
    changed file is replaced only at the first start after an upgrade. A
    sandbox does not die with the proxy: a session a compute machine keeps
    across the proxy's restart onto a new build sees that file through its
    writable dir, not through its bind, until it ends.

    The writes of one folder run one at a time within the server: two starts
    of one scope never interleave, so a sibling's good file is never taken
    for tampering and removed. The folder is opened once per write
    (:func:`_folder_of`) and every step works from that handle, and its lock
    is the folder's own identity, so two names of one folder share it."""
    dirfd = _folder_of(path)
    try:
        st = os.fstat(dirfd)
        with _FOLDER_LOCKS[hash((st.st_dev, st.st_ino)) % len(_FOLDER_LOCKS)]:
            return _write_if_changed(dirfd, path, data, mode)
    finally:
        os.close(dirfd)


def _write_if_changed(dirfd: int, path: Path, data: bytes, mode: int) -> bool:
    from services.infra import safe_fs
    name = path.name
    if _holds(dirfd, name, data, mode):
        return False
    _sweep_stale_temps(dirfd)
    for _ in range(_REPLACE_ATTEMPTS):
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISDIR(os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode):
                safe_fs.rmtree_beneath(dirfd, name)
        tmp = f".{name}{WRITE_TEMP_MARKER}{secrets.token_hex(6)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.replace(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
            mine = os.fstat(fd)
            try:
                there = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            except FileNotFoundError:
                there = None
            if (there is not None and (there.st_ino, there.st_dev) == (mine.st_ino, mine.st_dev)
                    and mine.st_nlink == 1):
                return True
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dirfd)
            raise
        finally:
            os.close(fd)
        if _holds(dirfd, name, data, mode):
            return True
        logger.warning("%s was replaced or linked during its write; writing it again", path)
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dirfd)
    raise RuntimeError(
        f"Refusing {path}: it was replaced during every write (possible tampering)"
    )


def _verified_session_dir(agent_name: str, *parts: str) -> Path:
    """Build ``agents/<agent>/parts...`` refusing symlinked components.

    Same invariant as ``sandbox._verified_literal_path``: the dir chain lives
    under agent-writable binds, so verify realpath == literal first (an early
    refusal), then create it with no link followed below the agent's
    resolved folder (``safe_fs.mkdirs_beneath``): a component swapped for a
    link after the check is refused, never created through. Raises on
    tampering — a session must fail loudly rather than write its config
    through a redirected path.
    """
    from services.infra import safe_fs
    root_real = agent_root(agent_name)
    expected = root_real.joinpath(*parts)
    if os.path.realpath(expected) != str(expected):
        raise RuntimeError(
            f"Refusing session config dir {expected}: a path component is a "
            f"symlink (possible tampering)"
        )
    root_real.mkdir(parents=True, exist_ok=True)
    try:
        safe_fs.mkdirs_beneath(root_real, "/".join(parts))
    except safe_fs.SafeFsError as exc:
        raise RuntimeError(
            f"Refusing session config dir {expected}: path changed "
            f"underneath the build (possible tampering)"
        ) from exc
    return expected


def _copy_hook_lf(src: Path, dst: Path) -> None:
    """Copy a hook/interceptor script into a sandbox, normalizing to LF and
    forcing the executable bit. A stray CR in the shebang
    (``#!/usr/bin/env python3\\r``) makes the kernel look for an interpreter
    literally named ``python3\\r`` → the hook fails to start
    (``/usr/bin/env: 'python3\\r': No such file or directory``) and silently
    bypasses enforcement. Normalize defensively so an editor/git CRLF can never
    break hook execution inside the sandbox (vs ``shutil.copy2``, which copies
    bytes + perms verbatim). Writes via ``write_if_changed`` — the dst lives
    in an agent-writable tree that running sessions read from."""
    write_if_changed(dst, src.read_bytes().replace(b"\r\n", b"\n"), mode=0o755)


# ---------------------------------------------------------------------------
# The shared spine: where a session's config dir lives, and what every
# engine installs into it. The engine-specific bodies — Claude's
# settings.json and its built-in deny list, Codex's hooks.json — live in the
# engine packages (core/layers/cli/config_dir.py, core/layers/codex/
# config_dir.py) behind ExecutionLayer.prepare_config_dir.
# ---------------------------------------------------------------------------

def scope_config_dir(
    agent_name: str, name: str, *, username: str = "", scope: str = "user",
    external_home=None,
) -> tuple[Path, str]:
    """``(host dir, sandbox-internal dir)`` of a session's config dir
    ``name`` (``.claude`` / ``.codex``) under the scope's root — created,
    verified against planted symlinks:

    - a user session: ``agents/<agent>/users/<username>/<name>`` ↔
      ``/users/<username>/<name>``
    - an agent-scoped session (a task, a phone call without a user):
      ``agents/<agent>/workspace/<name>`` ↔ ``/workspace/<name>``
    - an external caller with a private tree (``external_home``):
      ``<external_home>/<name>`` ↔ ``<SANDBOX_HOME>/<name>``
    """
    if external_home:
        from core.session.external_identity import SANDBOX_HOME
        return _verified_external_dir(agent_name, external_home, name), f"{SANDBOX_HOME}/{name}"
    if username and scope == "user":
        return (_verified_session_dir(agent_name, layout.USERS, username, name),
                f"{layout.virtual_user_root(username)}/{name}")
    return _verified_session_dir(agent_name, layout.WORKSPACE, name), f"{layout.V_WORKSPACE}/{name}"


def install_hook_scripts(config_dir: Path) -> None:
    """Copy the hook scripts, the credential helper and the stdio
    interceptor into a session config dir (LF-normalised, executable — a
    CRLF shebang silently breaks hook execution), and write the command
    wrappers under its ``bin/``. The interceptor is copied unconditionally:
    a missing source must raise, not silently disable the wrap spawn-time
    config still points at."""
    for script_name in HOOK_SCRIPTS:
        src = _HOOKS_DIR / script_name
        if src.exists():
            _copy_hook_lf(src, config_dir / script_name)
    _copy_hook_lf(_INTERCEPTOR_SRC, config_dir / _INTERCEPTOR_SRC.name)
    write_command_wrappers(config_dir)


def write_command_wrappers(config_dir: Path) -> None:
    """Write ``COMMAND_WRAPPERS`` under ``<config_dir>/bin``, which holds
    exactly them afterwards. The tree is agent-writable and the sandbox
    binds the directory read-only first on the session's PATH, where a file
    left there would run as the next session's command: a link at the
    directory's name is replaced, every entry that is not a wrapper file is
    removed (a planted file, a link, a subdirectory whole, a directory at a
    wrapper's name) but a concurrent start's write in flight, and each
    wrapper is written by ``write_if_changed``: the directory is bound as a
    whole, so a session running from it sees a changed wrapper at once,
    whole, and an unchanged one is not touched. In an agent's folder the
    directory is reached, listed and cleared beneath :func:`agent_root`, so
    a link above it is refused, never followed."""
    agent = agent_holding(config_dir)
    if agent:
        root, rel = beneath_agent(config_dir, agent)
        _clear_wrapper_dir(root, f"{rel}/{WRAPPER_DIR}")
    else:
        bin_dir = config_dir / WRAPPER_DIR
        if bin_dir.is_symlink() or (bin_dir.exists() and not bin_dir.is_dir()):
            bin_dir.unlink()
        bin_dir.mkdir(mode=0o755, exist_ok=True)
        with os.scandir(bin_dir) as entries:
            for entry in entries:
                if _keeps(entry):
                    continue
                # A concurrent start of the scope may remove the same entry.
                with contextlib.suppress(FileNotFoundError):
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path)
                    else:
                        os.unlink(entry.path)
    for relpath, content in COMMAND_WRAPPERS.items():
        write_if_changed(config_dir / relpath, content.encode(), mode=0o755)


def _keeps(entry: os.DirEntry) -> bool:
    """A wrapper file, or another start's write in flight: left in ``bin``."""
    keep = {Path(relpath).name for relpath in COMMAND_WRAPPERS}
    return (entry.name in keep and entry.is_file(follow_symlinks=False)) or is_fresh_write_temp(entry)


def _clear_wrapper_dir(root: Path, rel: str) -> None:
    """``bin`` beneath ``root`` made a plain folder holding only wrapper
    files: anything else at its name is removed, the folder made, and every
    other entry removed through the folder's own handle."""
    from services.infra import safe_fs
    with contextlib.suppress(FileNotFoundError, IsADirectoryError):
        safe_fs.unlink_beneath(root, rel)
    safe_fs.mkdirs_beneath(root, rel, mode=0o755)
    fd = safe_fs.open_dir_beneath(root, rel)
    try:
        with os.scandir(fd) as entries:
            doomed = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries if not _keeps(e)]
        for name, is_dir in doomed:
            # A concurrent start of the scope may remove the same entry.
            with contextlib.suppress(FileNotFoundError):
                if is_dir:
                    safe_fs.rmtree_beneath(fd, name)
                else:
                    safe_fs.unlink_beneath(fd, name)
    finally:
        os.close(fd)


#: Every hook script a session config dir carries, for both engines
#: (``proxy/hooks/``; the satellite receives the same set in its start
#: payload). ``credential_helper.py`` is not a hook: git and the command
#: wrappers invoke it (HOOKS.md "The credential helper").
HOOK_SCRIPTS: tuple[str, ...] = (
    "permission_gate.py", "tool_result_forwarder.py",
    "subagent_tracker.py", "stop_tracker.py", "credential_helper.py",
    "usage_report.py", "session_text.py", "start_context.py",
)

#: The file in each engine's state dir that names the hook scripts (the
#: sandbox binds it read-only beside them, ``SandboxBuilder._hook_file_binds``).
HOOK_FILE_OF_STATE_DIR: dict[str, str] = {".claude": "settings.json", ".codex": "hooks.json"}

#: The sandbox's PATH: the system dirs only (a user home is not mounted).
SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: The directory under a session config dir that holds the command wrappers,
#: first on the session's PATH; the sandbox binds it read-only
#: (``SandboxBuilder._hook_file_binds``).
WRAPPER_DIR = "bin"

#: The command wrappers every session config dir carries (both engines; the
#: satellite receives them in the start payload's ``command_wrappers``). Each
#: hands the command to the credential helper's ``run`` mode, which runs the
#: real command with the session's token for the host set on that one
#: process. ``gh`` is GitHub's CLI; the helper finds the real binary on the
#: PATH with this directory removed. The Windows wrapper stays ASCII (cmd
#: reads batch files in the OEM code page) and resolves the interpreter
#: through ``OTO_HOOK_PY`` like the Codex hook wrappers.
COMMAND_WRAPPERS: dict[str, str] = {
    f"{WRAPPER_DIR}/gh": (
        "#!/bin/sh\n"
        "# The platform's gh: the real gh runs with the session's GitHub token\n"
        "# in its own environment, fetched per command from the credential\n"
        "# broker; the session's environment never holds it.\n"
        'd=$(dirname "$0")\n'
        'exec "${OTO_HOOK_PY:-python3}" -I "$d/../credential_helper.py" run github.com gh -- "$@"\n'
    ),
    f"{WRAPPER_DIR}/gh.cmd": (
        '@if "%OTO_HOOK_PY%"=="" set "OTO_HOOK_PY=python"\r\n'
        '@"%OTO_HOOK_PY%" -I "%~dp0..\\credential_helper.py" run github.com gh -- %*\r\n'
        "@exit /b %errorlevel%\r\n"
    ),
}


def sandbox_python() -> str:
    """The interpreter the hook commands, the interceptor wrap and the git
    helper name inside a local sandbox: resolved on the sandbox's own PATH
    (the system dirs are bound read-only, so the host path is the sandbox
    path), never a bare name a directory earlier on PATH could shadow."""
    return shutil.which("python3", path=SANDBOX_PATH) or "python3"

#: The permission gate's and the Stop hook's transport ceiling: a dashboard
#: prompt and a turn-end verdict are long polls the proxy bounds itself. One
#: hour past the prompt's own wait (``session_state.PROMPT_WAIT_S``), so the
#: proxy's answer always comes first.
LONG_HOOK_TIMEOUT_S = 3 * 24 * 3600 + 3600
STOP_HOOK_TIMEOUT_S = LONG_HOOK_TIMEOUT_S


def _verified_external_dir(agent_name: str, external_home, sub: str) -> Path:
    """``<external_home>/<sub>`` for an external caller's tree, refusing
    symlinked components (the same invariant as ``_verified_session_dir``)
    and any home that is not a plain directory under this agent's tree."""
    root_real = agent_root(agent_name)
    home = Path(external_home)
    if os.path.realpath(home) != str(home) or not home.is_relative_to(root_real):
        raise RuntimeError(
            f"Refusing external session dir under {home}: not a plain "
            "directory under the agent tree (possible tampering)"
        )
    return _verified_session_dir(agent_name, *home.relative_to(root_real).parts, sub)

def ensure_persistent_agent_dir(
    agent_name: str,
    *,
    execution_path: str,
    username: str = "",
    scope: str = "user",
    external_home=None,
    no_shell: bool = False,
    read_only: bool = False,
    outside_picks=None,
    kind_rule=None,
) -> Path:
    """The persistent CLI config dir for a session — the ENGINE's
    (``ExecutionLayer.prepare_config_dir``): ``.codex/`` for Codex,
    ``.claude/`` for Claude CLI and for Direct LLM (which has no CLI config
    but keeps its plans dir and MCP config home in that tree).

    **The one entry point** for the session-config builders
    (``config_builder`` / ``task_config_builder`` / ``meeting_orchestrator`` /
    phone / the scheduler's one-shot resume) so they can't drift. The Codex
    layer reads ``config.sandbox_host_claude_dir`` AS its ``CODEX_HOME``; a
    Codex session whose config landed in ``.claude`` ran against a missing
    ``.codex`` config and hung / crashed at init (an interactive-task bug —
    a builder that forgot the codex branch, before this funnel existed).
    An unregistered ``execution_path`` raises ``UnknownExecutionPath``, as
    every builder already does one step earlier. The registry is reached
    function-locally: the layers import this package.
    """
    from core.session.session_manager import get_layer_by_path
    return get_layer_by_path(execution_path).prepare_config_dir(
        agent_name, username=username, scope=scope, external_home=external_home,
        no_shell=no_shell, read_only=read_only, outside_picks=outside_picks,
        kind_rule=kind_rule,
    )

def session_home_for(store, session_id: str, *, execution_path: str, ctx) -> str:
    """The home ``session_id`` runs from in its scope's engine folder
    ``store`` (``session_home``), as a host path; "" runs it from the folder
    itself, as every session of the scope did before.

    A home is made for an engine that declares one
    (``AuthProfile.session_home``), on a machine whose satellite reports
    that it makes homes in that engine's folder
    (``PlacementCapabilities.session_homes``), for a session that runs from
    its scope's folder, a judge included: on a machine that makes homes no
    session writes the store's own files (a Codex terminal reads a store's
    ``config.toml`` and ``hooks.json`` as its project's). Never on the
    platform's own sandbox (its start makes none) or for a session in a
    scratch home of its own (an outside caller, the agent's own line, a
    wrap-up: already one session per folder). A compute machine and a paired
    computer alike: the platform makes a compute session's home in its
    prepare step, a paired computer's satellite its own."""
    pl = getattr(ctx, "placement", None)
    if (not store or pl is None or not pl.session_homes
            or pl.kind not in (placement.KIND_ADMIN_REMOTE, placement.KIND_USER_REMOTE,
                               placement.KIND_COMPUTE)):
        return ""
    if getattr(ctx, "external_home", None) or getattr(ctx, "wrapup", "") or getattr(ctx, "open_line", False):
        return ""
    from core.sandbox import session_home
    from core.session.session_manager import capabilities_for_path
    auth = capabilities_for_path(execution_path).auth
    if (auth.session_home is None or auth.credential_file is None
            or auth.credential_file.dirname not in pl.session_homes):
        return ""
    if not session_home.valid_id(session_id):
        return ""
    return str(session_home.home_of(store, session_id))


def make_session_home(store, session_id: str, spec) -> Path:
    """The home ``session_id`` runs from in ``store``, made with the engine's
    links and seed (``SessionHomeSpec``) and stamped as a fresh start (the
    daily pass keeps a home changed in the last hour). The one builder of a
    home for every engine; what an engine writes into it is its own config
    writer's."""
    from core.sandbox import session_home
    home = session_home.prepare(Path(store), session_id, spec.links, spec.seed_map())
    # Through a handle on the home itself: a link at its name is never
    # followed to another folder's times.
    fd = _open_folder(home)
    try:
        os.utime(fd)
    finally:
        os.close(fd)
    return home


#: Claude Code's folder, whose ``plans`` holds a plan file (Codex has no plan
#: file: its plan is the turn's final message).
_PLAN_STORES = (".claude",)


def plan_file_name(path: str, session_id: str) -> str:
    """The file name of ``path`` when it is a plan file of ``session_id``, ""
    for any other path: a ``.md`` file straight in the ``plans`` folder of
    Claude Code's folder, the scope's (``<store>/plans/``) or this session's own
    home (``<store>/own/<session id>/plans/``, where the engine names it when
    it runs from its home: the home's ``plans`` is a link to the store's,
    ``SessionHomeSpec.links``). A machine's host path counts as the sandbox's
    does, backslashes are separators (Windows), the path is normalized first
    (one that climbs out with ``..`` is no plan), and another session's home
    holds no plan of this one."""
    import posixpath

    from core.sandbox import session_home
    parts = posixpath.normpath((path or "").replace("\\", "/")).split("/")
    name = parts[-1]
    if len(parts) < 3 or parts[-2] != "plans" or not name.endswith(".md") or name == ".md":
        return ""
    folder = parts[:-2]
    if folder[-1] in _PLAN_STORES:
        return name
    own = session_home.relative(session_id).split("/") if session_home.valid_id(session_id) else []
    if own and len(folder) > len(own) and folder[-len(own):] == own and folder[-len(own) - 1] in _PLAN_STORES:
        return name
    return ""


def _own_home(session_id: str, folder: str) -> bool:
    """Whether ``folder`` is ``session_id``'s own home in an agent's folder
    on the platform's disk (``agent_holding``: the agents folder as named
    or at its real place, so one reached through the operator's link
    counts)."""
    from core.sandbox import session_home
    return bool(folder and session_home.is_home(folder) and Path(folder).name == session_id
                and agent_holding(folder))


def drop_unstarted_home(session_id: str, home: str) -> bool:
    """A start that failed after it made the session's home: the home goes
    now, with the sign-in the start wrote in it. The session was bound to
    nothing, so no close finds it (``close_session_home`` reads the
    binding). True when a home was removed; a failure is logged and left to
    the daily pass."""
    from core.sandbox import session_home
    if not _own_home(session_id, home):
        return False
    try:
        return session_home.drop(session_home.store_of(home), session_id, ())
    except Exception:  # noqa: BLE001  # a home's removal must never replace the start's own failure
        logger.warning("session home of %s not removed after its failed start", session_id[:8], exc_info=True)
        return False


def close_session_home(session_id: str) -> bool:
    """At a session's close, before its binding is released: the home its
    binding names (``pool_bindings.bound_folder``), when it is on the
    platform's disk, loses its renewal target and then is removed. Every
    kind of close calls it (a headless session's, a terminal's, one taken
    back after a restart: adoption restores the binding first), so no record
    carries the home. A compute machine beside the platform shares this
    disk; a paired computer's home is the satellite's, never here, and its
    own close removes it. A start of the id in flight owns the home (a
    terminal replaced under its id is closed inside its successor's start),
    so it is left; otherwise the id is held while the home goes, so a start
    of it waits (``session_closing``). True when a home was removed; a
    failure is logged and left to the daily pass, never raised into the
    close."""
    from core.sandbox import session_home
    from core.session import session_closing, session_state
    from services.engines import pool_bindings, token_fanout
    folder = pool_bindings.bound_folder(session_id)
    if not _own_home(session_id, folder) or session_state.is_starting(session_id):
        return False
    with session_closing.closing(session_id):
        token_fanout.unregister_session_target(session_id)
        try:
            return session_home.drop(session_home.store_of(folder), session_id, ())
        except Exception:  # noqa: BLE001  # a home's removal must never stop the close that releases the seat
            logger.warning("session home of %s not removed at its close", session_id[:8], exc_info=True)
            return False


def prepare_mcp_config_for_sandbox(
    host_mcp_config_path: str | Path,
    host_config_dir: str | Path,
    sandbox_config_dir: str = "",
    *,
    session_id: str = "",
    secret_bundles: dict | None = None,
    agent_name: str,
) -> str:
    """Copy MCP config into the session config dir for sandboxed sessions.

    Copies the MCP config file into the config dir (.claude/ or .codex/)
    and returns the sandbox-internal path. The original file in
    proxy/sessions/ is NOT mounted in the sandbox.

    Also copies a tool's own instance config file (the one an argument such
    as --config-file names, ``<mcp>-<agent_name>.json`` in the sessions
    folder's ``user-mcp-configs``) into the config dir and rewrites that
    argument to its sandbox-internal path. No other file under the sessions
    folder is copied: an installed package's argument may name one.

    When ``session_id`` + ``secret_bundles`` are given (local CLI broker path),
    each stdio MCP that has a secret bundle gets a per-(session, mcp) capability
    token injected into THIS per-session copy, then its command is wrapped with
    the stdio interceptor so it fetches its secrets at spawn.

    Args:
        host_config_dir: Host path to .claude/ or .codex/ dir.
        sandbox_config_dir: Sandbox-internal config dir path
            (e.g. /users/alice/.claude or /workspace/.codex).
        session_id: Session id for broker token minting (empty → no broker).
        secret_bundles: ``{mcp_name: SecretBundle}`` for this session — only
            these MCPs get a fetch token + interceptor wrap.
    """
    if not host_mcp_config_path:
        return ""

    src = Path(host_mcp_config_path)
    if not src.exists():
        return str(src)

    dst = _session_copy_path(Path(host_config_dir), src, session_id, agent_name)
    # Read config, rewrite any referenced file paths, copy referenced files
    import json as _json
    try:
        config_data = _json.loads(src.read_text())
        sessions_dir = str(app_config.SESSIONS_DIR)
        mcp_servers = config_data.get("mcpServers", {})
        for key, srv in mcp_servers.items():
            args = srv.get("args", []) if isinstance(srv, dict) else []
            for i, arg in enumerate(args):
                if isinstance(arg, str) and arg.startswith(sessions_dir):
                    copied = _copy_instance_file(key, arg, agent_name, Path(host_config_dir))
                    if copied:
                        args[i] = f"{sandbox_config_dir}/{copied}"

        # Credential broker: inject the per-(session, mcp) capability
        # token into each stdio MCP that has a secret bundle, then wrap its
        # command with the stdio interceptor so it fetches its secrets at spawn.
        # The token lands in THIS per-session copy only — never the shared
        # sessions/ build file (reused across concurrent sessions). An HTTP
        # MCP's entry already names the credential gateway with the session
        # token sentinel; nothing is added to it here.
        if session_id and secret_bundles:
            from core.credentials import mcp_broker
            from core.sandbox.interceptor_wrap import wrap_servers_json
            bundle_keys = set(secret_bundles)
            for name, srv in mcp_servers.items():
                if name not in bundle_keys or not isinstance(srv, dict):
                    continue
                if "command" in srv:
                    env = srv.get("env") or {}
                    env["OTO_MCP_FETCH_TOKEN"] = mcp_broker.mint_token(session_id, name)
                    srv["env"] = env
            wrap_servers_json(
                config_data, interpreter=sandbox_python(), interpreter_args=("-I",),
                interceptor_path=f"{sandbox_config_dir}/{_INTERCEPTOR_SRC.name}",
            )

        # Write rewritten config beneath the agent's folder: this per-session
        # copy carries broker tokens and the destination dir is
        # agent-writable — a planted link must never redirect it.
        write_beneath_agent(agent_name, dst, _json.dumps(config_data, indent=2).encode(), mode=0o644)
    except (OSError, ValueError, ImportError):
        # Fallback: simple copy without rewriting (the same writer — a
        # refusal above is refused again here and ends the start).
        write_beneath_agent(agent_name, dst, src.read_bytes(), mode=0o644)

    # Return sandbox-internal path
    return f"{sandbox_config_dir}/{dst.name}"


# An instance config file is a few KiB of the instances' field values.
_INSTANCE_FILE_MAX = 1024 * 1024


def _instance_file_name(server_key: str, agent_name: str) -> str:
    """The instance file the platform writes for the tool behind a config's
    server entry (``mcp_server_resolve._generate_instance_config_file``), or
    "" when that tool delivers no instance file."""
    if not agent_name:
        return ""
    from services.mcp import mcp_registry
    for name, manifest in mcp_registry.get_all_manifests().items():
        if (manifest.server_name or name) == server_key:
            instances = manifest.instances
            return f"{name}-{agent_name}.json" if instances and instances.delivery == "config_file" else ""
    return ""


def _copy_instance_file(server_key: str, arg: str, agent_name: str, host_config_dir: Path) -> str:
    """Copy the instance file ``arg`` names into the session's config dir and
    answer its name, when it is exactly this tool's own file in
    ``user-mcp-configs``, a regular file reached with no link. Anything else
    is left as it is and logged: inside the sandbox the sessions folder is
    masked, so the argument reads nothing there. A refusal never costs the
    other tools their part of the build."""
    from services.infra import safe_fs
    folder = app_config.SESSIONS_DIR / "user-mcp-configs"
    expected = _instance_file_name(server_key, agent_name)
    try:
        name = safe_fs.rel_under(arg, folder)
    except OSError:
        name = ""
    if not expected or name != expected:
        logger.warning("tool %s: an argument names a file in the sessions folder that is not its own "
                       "instance file, not copied", server_key)
        return ""
    try:
        write_beneath_agent(agent_name, host_config_dir / name,
                            safe_fs.read_bytes_beneath(folder, name, max_size=_INSTANCE_FILE_MAX), mode=0o644)
    except OSError as exc:
        logger.warning("tool %s: its instance file was not copied: %s", server_key, type(exc).__name__)
        return ""
    return name


def _session_copy_path(host_config_dir: Path, src: Path, session_id: str, agent_name: str) -> Path:
    """Where a session's MCP config copy lands: named per session, so two
    concurrent sessions of one person on one agent never read each other's
    token. The shared-named copy earlier releases wrote (it carried inline
    vendor bearers) is removed, and per-session copies older than a token's
    life are swept, all beneath :func:`agent_root` (a link above the folder
    is refused, a link in it removed as itself)."""
    if not session_id:
        return host_config_dir / src.name
    from services.infra import safe_fs
    root, rel = beneath_agent(host_config_dir, agent_name)
    with contextlib.suppress(FileNotFoundError, IsADirectoryError):
        safe_fs.unlink_beneath(root, f"{rel}/{src.name}")
    cutoff = time.time() - 24 * 3600
    fd = safe_fs.open_dir_beneath(root, rel)
    try:
        with os.scandir(fd) as entries:
            stale = [e.name for e in entries
                     if e.name.startswith(f"{src.stem}-") and e.name.endswith(src.suffix)
                     and not e.is_dir(follow_symlinks=False) and _older(e, cutoff)]
        for name in stale:
            with contextlib.suppress(OSError):
                safe_fs.unlink_beneath(fd, name)
    finally:
        os.close(fd)
    return host_config_dir / f"{src.stem}-{session_id[:12]}{src.suffix}"


def _older(entry: os.DirEntry, cutoff: float) -> bool:
    try:
        return entry.stat(follow_symlinks=False).st_mtime < cutoff
    except OSError:
        return False


class AgentStateRefused(RuntimeError):
    """A session that would run from the agent's own CLI state (the agent
    scope's ``workspace/.claude`` / ``.codex``: the hooks, settings, MCP
    config and login every task, phone call and Shared-only chat of the
    agent runs with) for a person below the editor tier. Whoever runs from
    that dir can rewrite what the agent's other sessions execute, so only
    the tiers that already act as the agent may. The message is the
    person's to read."""


def refuse_agent_state_below_editor(scope: str, role: str, *, external: bool = False,
                                    shared_only: bool = True) -> None:
    """Raise :class:`AgentStateRefused` when a session of ``scope`` (its
    MOUNT scope) and ``role`` would run from the agent's own CLI state below
    the editor tier: a Shared-only chat of a viewer or a contributor, or
    their own task or meeting on a Shared-only agent. An external caller is
    not a person on the agent (a no-shell session the gate floors), and a
    personal session runs from its own tree.

    The sentence names what the session runs as: a Shared-only agent's
    chats (the default), or, with ``shared_only=False``, an agent-scope chat
    on an agent with personal chats (a delegate worker's chat)."""
    from auth import roles
    from core.session.visibility import SCOPE_AGENT
    if scope != SCOPE_AGENT or external or roles.can_edit(role):
        return
    ran_as = role or "no role"
    if not shared_only:
        raise AgentStateRefused(
            "This chat runs as the agent itself (an agent-scope chat), which "
            f"takes the editor role or above (this one would run as {ran_as}). "
            "Ask a manager of the agent for the editor role."
        )
    raise AgentStateRefused(
        "This agent is set to Shared only, so its chats and tasks run as the "
        "agent itself, which takes the editor role or above (this one would "
        f"run as {ran_as}). Ask a manager of the agent for the "
        "editor role, or to turn on personal chats."
    )


def refuse_session_on_agent_state(ctx) -> None:
    """:func:`refuse_agent_state_below_editor` for a built session, from its
    SecurityContext: the start-time floor every engine checks before it runs
    a CLI from its config dir (a builder the config-time check missed, a
    context rebuilt for a resume). The session runs from the agent's state
    when it mounts no person (``mount_username``, the sandbox's own rule);
    no context at all is refused as below the tier."""
    from core.session.external_identity import is_external_ctx
    from core.session.visibility import SCOPE_AGENT, SCOPE_USER
    if ctx is None:
        refuse_agent_state_below_editor(SCOPE_AGENT, "")
        return
    scope = SCOPE_USER if getattr(ctx, "mount_username", "") else SCOPE_AGENT
    role = getattr(ctx, "role", "") or ""
    external = is_external_ctx(ctx)
    try:
        refuse_agent_state_below_editor(scope, role, external=external)
    except AgentStateRefused:
        # Refused: word it by what the session runs as (an agent-scope chat
        # on an agent with personal chats, or a Shared-only agent's).
        from core.session.visibility import is_shared_only
        refuse_agent_state_below_editor(
            scope, role, external=external,
            shared_only=is_shared_only(getattr(ctx, "agent", "") or ""))


_SSH_HOSTS = "ssh-hosts"


def session_takes_ssh_keys(ctx) -> bool:
    """Whether a session is handed the agent's SSH keys, locally or on a
    machine: the editor tier of its SecurityContext (the keys are the
    agent's own credentials, so only the tiers that act as the agent),
    never an external caller's (ssh-hosts is not attached there, and key
    material must not land in a tree a caller's session reaches), never a
    check's judge (it reads; its MCPs are the check's list), and never a
    session with no context or no role. The one predicate the Claude and
    Codex layers, the satellite session-file broker and the command gate's
    attached-host opening share (``path_shell_hosts.takes_attached_hosts``);
    the role is read off the context alone, never inferred from a token or a
    name."""
    from auth.path_shell_hosts import takes_attached_hosts
    return takes_attached_hosts(ctx)


def clear_ssh_keys_for_sandbox(host_config_dir: str | Path, *, agent_name: str) -> None:
    """Remove ``<config_dir>/ssh``: the session does not take the agent's
    keys, so nothing an earlier, more entitled session of the same person
    left in that dir may survive into this one (the materializer rebuilds
    the dir only when the keys are due). Beneath :func:`agent_root`: a link
    at ``ssh`` goes as itself, a link above it is refused."""
    root, rel = beneath_agent(host_config_dir, agent_name)
    _clear_key_dir(root, f"{rel}/ssh")


def _clear_key_dir(root: Path, rel: str) -> None:
    """``rel`` removed beneath ``root``: a link or a file at its name as
    itself, a folder with everything in it. A link above it refuses (each
    remover reaches the name's folder with no link)."""
    from services.infra import safe_fs
    try:
        safe_fs.unlink_beneath(root, rel, missing_ok=True)
    except IsADirectoryError:
        safe_fs.rmtree_beneath(root, rel, missing_ok=True)


async def provision_ssh_keys_for_sandbox(
    ctx, agent_name: str, host_config_dir: str | Path, sandbox_config_dir: str,
) -> str:
    """The one SSH-key rule of the local layers, at spawn: a session that
    takes the keys (:func:`session_takes_ssh_keys`) gets them materialised
    and the ``OTO_SSH_KEY_DIR`` value the env carries (``""`` when the agent
    authorises none); a person below the editor tier gets the dir cleared,
    so nothing an earlier, entitled session of theirs left survives into
    this one; a judge and an external caller take no keys but leave the dir
    alone, because they run from a config dir another session may hold the
    keys in (the judged person's, the agent's own). The keys are read off the
    loop. The dir's wipe and rebuild stay on it, with no await between them: the
    starts of one scope share the dir, and in two threads one would remove what
    the other is writing and fail its start."""
    if session_takes_ssh_keys(ctx):
        # The proxy signs (``core/credentials/ssh_signer.py``): the dir holds
        # the public halves alone, which ``ssh -i <name>.pub`` picks the
        # agent's identity by; no private key enters a tree the session reads.
        from core.credentials import ssh_signer
        from storage.pg import run_db
        lines = await run_db(ssh_signer.public_lines, agent_name)
        if materialize_ssh_public_keys(lines, host_config_dir, agent_name=agent_name):
            return f"{sandbox_config_dir}/ssh"
        return ""
    from auth import roles
    from core.session.external_identity import is_external_ctx
    if ctx is None or is_external_ctx(ctx) or getattr(ctx, "read_only", False) is True:
        return ""
    if isinstance(getattr(ctx, "role", None), str) and not roles.can_edit(ctx.role):
        clear_ssh_keys_for_sandbox(host_config_dir, agent_name=agent_name)
    return ""


def materialize_ssh_public_keys(lines: dict[str, str], host_config_dir: str | Path, *,
                                agent_name: str) -> bool:
    """Provision the public halves (``ssh_signer.public_lines``) into
    ``<config_dir>/ssh/<key_name>.pub``, the dir removed and made again at
    every start, so a private file an earlier release wrote there is gone
    and a de-authorized key disappears. True when at least one landed.
    (Host keys are not pre-seeded: the prompt lines carry
    ``StrictHostKeyChecking=accept-new``.)"""
    return _materialize_key_dir(
        {f"{name}.pub": line.encode("utf-8") for name, line in lines.items()},
        host_config_dir, mode=0o644, agent_name=agent_name)


def _materialize_key_dir(files: dict[str, bytes], host_config_dir: str | Path, *, mode: int,
                         agent_name: str) -> bool:
    """``<config_dir>/ssh`` removed and made again holding ``files``, all
    beneath :func:`agent_root`: a link at ``ssh`` goes as itself, a link
    above it refuses the start. The make takes a folder that exists, so two
    starts of one scope at the same moment both pass. No sandbox binds a key
    by name, so each is written whole. Synchronous: no await falls between
    the removal and the last key."""
    from services.infra import safe_fs
    root, rel = beneath_agent(host_config_dir, agent_name)
    rel = f"{rel}/ssh"
    _clear_key_dir(root, rel)
    if not files:
        return False
    safe_fs.mkdirs_beneath(root, rel, mode=0o700)
    fd = safe_fs.open_dir_beneath(root, rel)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    for name, data in sorted(files.items()):
        write_beneath_agent(agent_name, Path(host_config_dir) / "ssh" / name, data, mode=mode)
    return True


def sweep_private_ssh_keys() -> int:
    """Remove every private key file an earlier release's start left in a
    scope's ``ssh/`` dir (``workspace/<engine dir>/ssh`` and
    ``users/<u>/<engine dir>/ssh`` of every agent): the proxy signs now and
    no key belongs in a tree a session reads. A ``.pub`` file stays. Each
    agent's folder is walked beneath its real place (``core.agent_folder``)
    with no link entered below it, and each entry is removed through its
    folder's own handle: a compute machine's session outlives the server's
    restart and may be at work in that folder. Runs once at boot, off the
    loop; returns the number removed."""
    from services.infra import safe_fs
    agents = app_config.AGENTS_DIR
    if not agents or not agents.is_dir():
        return 0

    def _folders(root: Path, rel: str) -> list[str]:
        try:
            fd = safe_fs.open_dir_beneath(root, rel)
        except OSError:
            return []
        try:
            with os.scandir(fd) as entries:
                return [e.name for e in entries if e.is_dir(follow_symlinks=False)]
        finally:
            os.close(fd)

    names: list[str] = []
    with contextlib.suppress(OSError), os.scandir(agents) as entries:
        names = [e.name for e in entries if app_config.is_safe_agent_name(e.name) and e.is_dir()]
    removed = 0
    for agent in names:
        root = agent_root(agent)
        scopes = [layout.WORKSPACE, *(f"{layout.USERS}/{u}" for u in _folders(root, layout.USERS))]
        for scope in scopes:
            for engine_dir in HOOK_FILE_OF_STATE_DIR:
                try:
                    fd = safe_fs.open_dir_beneath(root, f"{scope}/{engine_dir}/ssh")
                except OSError:
                    continue
                try:
                    with os.scandir(fd) as entries:
                        doomed = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries
                                  if not (e.name.endswith(".pub") and e.is_file(follow_symlinks=False))]
                    for name, is_dir in doomed:
                        with contextlib.suppress(OSError):
                            if is_dir:
                                safe_fs.rmtree_beneath(fd, name)
                            else:
                                safe_fs.unlink_beneath(fd, name)
                            removed += 1
                finally:
                    os.close(fd)
    if removed:
        logger.info("ssh keys: removed %d private key file(s) from the scopes' key dirs", removed)
    return removed


def collect_authorized_ssh_keys(agent_name: str) -> dict[str, Path]:
    """The SSH key files this agent's ssh-hosts authorization grants.

    Returns ``{key_name: absolute source Path}`` for the keys referenced by
    the agent's authorizing ssh-hosts instances — empty when the MCP isn't
    installed / isn't enabled for the agent (visible + manager-enabled +
    platform-enabled, the same gate every runtime surface uses) / no instance
    references a key. Key names are restricted to sanitized basenames so a
    poisoned instance row can never become a file-read primitive.

    Shared by the local sandbox materializer above and the remote
    session-file provisioning in ``core/remote/remote_session_start``.
    """
    manifest, instances = _authorizing_ssh_instances(agent_name)
    if manifest is None:
        return {}
    keys_dir = manifest.mcp_dir / "keys"
    out: dict[str, Path] = {}
    for inst in instances:
        key = ((inst.get("field_values") or {}).get("key_name") or "").strip()
        if not key or os.path.basename(key) != key or key in (".", ".."):
            continue
        src = keys_dir / key
        if src.is_file():
            out[key] = src
    return out


def _authorizing_ssh_instances(agent_name: str):
    """ssh-hosts' manifest and the instances authorizing ``agent_name``:
    ``(None, [])`` when the MCP isn't installed or isn't enabled for the agent
    (visible + manager-enabled + platform-enabled, the gate every runtime
    surface uses). The keys and the attached hosts read the same answer."""
    from services.mcp import mcp_registry
    from storage.mcp import mcp_store

    manifest = mcp_registry.get_manifest(_SSH_HOSTS)
    if manifest is None or not any(
            m.name == _SSH_HOSTS for m in mcp_registry.get_agent_mcps(agent_name)):
        return None, []
    return manifest, mcp_store.get_mcp_instances_for_agent(_SSH_HOSTS, agent_name)


def attached_ssh_hosts(agent_name: str) -> tuple[tuple[str, str], ...]:
    """The ``(host, user)`` pairs the agent's authorizing ssh-hosts instances
    name, normalised for the command gate's match: every instance with a host
    (a missing key still attaches the host: the login may fail, the gate is
    not where that is decided). Read live per shell call by the permission
    hook, so an instance an admin removes stops admitting at once."""
    from auth.path_shell_hosts import attached_host
    _, instances = _authorizing_ssh_instances(agent_name)
    pairs = []
    for inst in instances:
        fv = inst.get("field_values") or {}
        host = str(fv.get("host") or "").strip()
        if host:
            pairs.append(attached_host(host, str(fv.get("username") or "")))
    return tuple(dict.fromkeys(pairs))


def wants_attached_ssh_hosts(ctx, command: str) -> bool:
    """Whether one shell call reads the agent's attached SSH hosts: a local
    session whose role takes them below the platform admin (a manager or an
    editor; an admin needs none, a paired machine keeps its own rule), and a
    command that names ssh, scp or rsync. The name only decides whether to
    look, never what is allowed: a spelling that hides it gets no hosts and is
    refused."""
    from auth import roles
    from auth.path_shell_hosts import SSH_COMMANDS, takes_attached_hosts
    return (ctx is not None and ctx.placement.isolates_with_bwrap and not roles.is_admin(ctx.role)
            and takes_attached_hosts(ctx) and any(name in command for name in SSH_COMMANDS))


def agent_relative(agent_name: str, host_dir: str | Path) -> str:
    """``host_dir`` relative to the agent's folder (``users/<u>/.claude``),
    or ``""`` when it lies outside it (an app's or a run's scratch home):
    the path a compute machine's session names the same dir by through the
    shared folder."""
    try:
        return str(Path(host_dir).resolve().relative_to(
            app_config.get_agent_dir(agent_name).resolve()))
    except (ValueError, OSError):
        return ""
