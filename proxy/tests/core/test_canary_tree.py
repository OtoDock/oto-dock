"""The canary tree: the server's file paths, driven over folders that hold links and pipes.

The behaviour twin of ``test_agent_tree_surface.py``. That test reads names;
this one runs the code. One fixture builds a stand-in agents folder in the
test's own temporary folder, with a canary folder beside it, outside the
agents root, holding a file and folders with known bytes. Each row plants, in
a folder a session or a person writes, a link to the canary or a pipe at a name
the server opens, then drives one server path, the way a session's start, a
hook, a sweep or a copy would. The row passes when nothing in the canary was
opened, read, written, created, moved or removed, and when the call never
waited on a pipe. A refusal by the server (an error it raises on purpose) is a
pass: it is what the safe file helpers do.

How a touch is seen: an inotify watch on every folder of the canary (an open,
a read, a write, an attribute change, a create, a remove or a move inside it
is an event; a ``stat`` is none), and a snapshot of its names, bytes and modes
before and after. How a wait is seen: the call runs on a thread of its own;
when that thread sits in a pipe's open or read, or has not returned by the
deadline, every pipe of the row is opened from its other side, so the call
goes on, and the row fails.

It catches the class by behaviour on the paths it drives and misses every
path it does not drive: the surface test is the net for those. Every link,
pipe and canary sits inside the test's own temporary folder.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import ctypes
import inspect
import io
import json
import os
import shutil
import sqlite3
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import config

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify and pipes: Linux only")

AGENT = "a"
SID = "canary-session-1"
_SECRET = b"canary bytes: never read by the server\n"
#: How long a call may sit on a pipe it should have refused, when the
#: thread's state cannot be read; and how long any call may take at all.
_PIPE_DEADLINE = 3.0
_HANG_DEADLINE = 30.0
#: What ``/proc/<task>/wchan`` reads while a thread waits in a pipe's open or
#: read, by the kernel's names (older and newer).
_PIPE_WAITS = {"wait_for_partner", "pipe_read", "pipe_write", "pipe_wait", "pipe_wait_readable",
               "pipe_wait_writable", "fifo_open", "anon_pipe_read", "anon_pipe_write", "fifo_pipe_read",
               "fifo_pipe_write"}


class CanaryTouched(AssertionError):
    """The server opened, read, wrote, made or removed something in the canary."""


class WaitedOnAPipe(CanaryTouched):
    """The server's call sat on a pipe it should have refused at once."""


class DriverBroken(Exception):
    """The row's own call hung for another reason, or never reached the name
    it plants: the row must be fixed, it proves nothing as it stands."""


class _Inotify:
    """Every event on a set of folders and files, read through ``ctypes``
    (the standard library has no inotify)."""

    _ALL = 0x00000FFF  # IN_ALL_EVENTS

    def __init__(self, paths: list[Path]) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self._names: dict[int, Path] = {}
        for path in paths:
            wd = self._libc.inotify_add_watch(self._fd, os.fsencode(path), self._ALL | 0x02000000)  # IN_DONT_FOLLOW
            if wd < 0:
                os.close(self._fd)
                raise OSError(ctypes.get_errno(), f"inotify_add_watch {path}")
            self._names[wd] = path

    def events(self) -> list[Path]:
        """The path of each event: the watched path, or a name in a watched
        folder."""
        seen = []
        with contextlib.suppress(BlockingIOError):
            while True:
                data = os.read(self._fd, 65536)
                offset = 0
                while offset < len(data):
                    wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                    name = data[offset + 16:offset + 16 + length].rstrip(b"\0").decode(errors="replace")
                    if not mask & 0x8000 and wd in self._names:  # IN_IGNORED: the watch's own end
                        seen.append(self._names[wd] / name if name else self._names[wd])
                    offset += 16 + length
        return seen

    def close(self) -> None:
        os.close(self._fd)


def _everything_below(root: Path) -> list[Path]:
    """``root`` and every folder and file below it, no link followed."""
    if not root.exists() or root.is_symlink():
        return []
    return [root, *(p for p in root.rglob("*") if not p.is_symlink())]


def _snapshot(root: Path) -> dict[str, tuple]:
    """Names, kinds, modes and bytes of everything below ``root``, read
    without following a link."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            st = path.lstat()
            body = path.read_bytes() if stat.S_ISREG(st.st_mode) else b""
            out[str(path.relative_to(root))] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), body)
    return out


@dataclass
class Tree:
    """The stand-in tree of one row: the agents root, the canary outside it,
    and what the row planted. An honest tree plants, at each name, what an
    honest session leaves there: a file or a folder with the target's
    content where a link would be, nothing where a link would dangle, a
    file where a pipe would be."""

    root: Path
    agents: Path
    canary: Path
    honest: bool = False
    pipes: list[Path] = field(default_factory=list)
    planted: list[Path] = field(default_factory=list)

    def at(self, rel: str) -> Path:
        return self.agents / rel

    def link(self, rel: str, target: Path) -> Path:
        path = self.at(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.planted.append(path)
        if not self.honest:
            path.symlink_to(target)
        elif target.is_dir():
            shutil.copytree(target, path)
        elif target.exists():
            shutil.copyfile(target, path)
        return path

    def pipe(self, rel: str) -> Path:
        path = self.at(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.planted.append(path)
        if self.honest:
            path.write_bytes(b"honest\n")
        else:
            os.mkfifo(path)
            self.pipes.append(path)
        return path

    def write(self, rel: str, data: bytes | str = b"honest\n") -> Path:
        path = self.at(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path


def _a_tiny_database(path: Path) -> None:
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute("create table t (x)")
        db.execute("insert into t values ('canary')")
        db.commit()


def _build(tmp_path: Path, monkeypatch, *, honest: bool) -> Tree:
    agents, canary = tmp_path / "agents", tmp_path / "canary"
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    from auth import path_policy
    monkeypatch.setattr(path_policy, "_AGENTS_DIR", agents.resolve())
    t = Tree(tmp_path, agents, canary, honest=honest)
    t.write("a/config/agent.md", "# Persona\nAn honest persona.\n")
    t.write("a/config/context/notes.md", "Honest context.\n")
    t.write("a/config/user-setup.md", "Welcome.\n")
    t.write("a/workspace/readme.md")
    t.write("a/knowledge/readme.md")
    for person in ("u", "v"):
        t.write(f"a/users/{person}/workspace/readme.md")
        t.write(f"a/users/{person}/context/me.md", "Honest me.\n")
        t.at(f"a/users/{person}/.claude").mkdir(parents=True)
        t.at(f"a/users/{person}/.codex").mkdir(parents=True)
    folder = canary / "folder"
    for rel in ("brand.md", "p/memory/keep", "ssh/keep", "coding/check.json", "w/keep"):
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(_SECRET)
    (folder / "empty").mkdir()
    (folder / "2026").mkdir()
    (folder / "2026" / "rollout-2026-01-01T00-00-00-tid-9.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")
    _a_tiny_database(folder / "app.db")
    (canary / "secret.md").write_bytes(_SECRET)
    (canary / "settings.json").write_text('{"canary": true}\n')
    return t


@pytest.fixture
def tree(tmp_path, monkeypatch) -> Tree:
    """An agent ``a`` with persons ``u`` and ``v``, the folders a session
    writes with honest files in them, and the canary outside the agents
    root: ``secret.md``, ``settings.json`` and ``folder/`` (``brand.md``, an
    empty ``empty/``, ``p/memory/keep``, ``ssh/keep``, ``w/keep``,
    ``coding/check.json``, a rollout and a database)."""
    return _build(tmp_path, monkeypatch, honest=False)


@pytest.fixture
def honest_tree(tmp_path, monkeypatch) -> Tree:
    """The same tree, where each row plants what an honest session leaves."""
    return _build(tmp_path, monkeypatch, honest=True)


def _wchan(native_id: int) -> str:
    try:
        return Path(f"/proc/self/task/{native_id}/wchan").read_text().strip()
    except OSError:
        return ""


def _tasks() -> set[int]:
    """The threads of this process, by their native ids."""
    try:
        return {int(name) for name in os.listdir("/proc/self/task")}
    except OSError:
        return set()


def _release(pipes: list[Path]) -> None:
    """Open each pipe from both sides at once, so a call that waits on one
    goes on."""
    held = []
    for pipe in pipes:
        for flags in (os.O_RDONLY | os.O_NONBLOCK, os.O_WRONLY | os.O_NONBLOCK):
            with contextlib.suppress(OSError):
                held.append(os.open(pipe, flags))
    for fd in held:
        os.close(fd)


def _drive(thunk: Callable[[], object], pipes: list[Path]) -> tuple[bool, BaseException | None]:
    """Run ``thunk`` on a thread of its own: (whether it waited on a pipe,
    what it raised). A call that sits in a pipe (the state of a thread it
    started says so, the driving one or one it handed work to, or it is still
    there after the pipe deadline with no state to read, and opening the
    pipes lets it go) waited on a pipe; one that hangs anyway is a broken row.
    Its pipes are opened before it is given up, so no thread is left on one
    to hold the worker at its exit."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            result = thunk()
            if inspect.isawaitable(result):
                asyncio.run(_awaited(result))
        except BaseException as exc:  # noqa: BLE001  # the row reads what the server raised
            outcome["error"] = exc

    before = _tasks()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    waited, start = False, time.monotonic()
    while worker.is_alive():
        worker.join(0.02)
        states = [_wchan(task) for task in _tasks() - before]
        in_a_pipe = bool(pipes) and any(state in _PIPE_WAITS for state in states)
        late = (bool(pipes) and all(state in ("", "0") for state in states)
                and time.monotonic() - start > _PIPE_DEADLINE)
        if worker.is_alive() and (in_a_pipe or late):
            waited = _let_go(worker, pipes)
            break
        if time.monotonic() - start > _HANG_DEADLINE:
            break
    if worker.is_alive():
        _let_go(worker, pipes)
        raise DriverBroken("the row's call hung and did not end when its pipes were opened")
    error = outcome.get("error")
    return waited, error if isinstance(error, BaseException) else None


def _let_go(worker: threading.Thread, pipes: list[Path]) -> bool:
    """Open the pipes from their other sides until the call ends: True when
    that let it go."""
    for _round in range(50):
        _release(pipes)
        worker.join(0.1)
        if not worker.is_alive():
            return True
    return False


async def _awaited(awaitable) -> None:
    await awaitable


@dataclass(frozen=True)
class Row:
    """One driven path: what it plants and calls (``case``), and the
    refusals the code raises on purpose. ``honest`` is False only where the
    call reads no such name at all, so an honest tree shows no effect either."""

    case: Callable[[Tree, pytest.MonkeyPatch], Callable[[], object]]
    refusals: tuple[type[BaseException], ...] = ()
    honest: bool = True


def _check(tree: Tree, row: Row, monkeypatch) -> None:
    thunk = row.case(tree, monkeypatch)
    before = _snapshot(tree.canary)
    watch = _Inotify(_everything_below(tree.canary))
    try:
        waited, error = _drive(thunk, tree.pipes)
        events = watch.events()
    finally:
        watch.close()
    if waited:
        raise WaitedOnAPipe(f"the call waited on a pipe it should have refused: {tree.pipes}")
    if events or _snapshot(tree.canary) != before:
        shown = [str(path) for path in events] or ["the canary's content changed"]
        raise CanaryTouched("the server reached the canary through a link:\n  " + "\n  ".join(shown))
    if error is not None and not isinstance(error, row.refusals):
        raise error


def _reached(tree: Tree, events: list[Path]) -> bool:
    """Whether an event names a planted path, something below one, or a
    planted name made in its folder."""
    for path in events:
        for planted in tree.planted:
            if path == planted or planted in path.parents:
                return True
    return False


def _check_honest(tree: Tree, row: Row, monkeypatch) -> None:
    """The same call over the honest tree reaches the names the row plants:
    the row drives the path it claims to."""
    thunk = row.case(tree, monkeypatch)
    watched = [p for planted in tree.planted for p in (planted.parent, *_everything_below(planted))]
    watch = _Inotify(list(dict.fromkeys(watched)))
    try:
        _waited, error = _drive(thunk, [])
        events = watch.events()
    finally:
        watch.close()
    if error is not None:
        raise error
    if not _reached(tree, events):
        raise DriverBroken(f"the call never opened, wrote or removed the honest names {tree.planted}")


# ── the rows ────────────────────────────────────────────────────────────────


def _persona_link(tree, monkeypatch):
    from configuration import prompts
    tree.at("a/config/agent.md").unlink()
    tree.link("a/config/agent.md", tree.canary / "secret.md")
    return lambda: prompts._read_agent_files(AGENT)


def _context_link(tree, monkeypatch):
    from configuration import prompts
    tree.link("a/config/context/linked.md", tree.canary / "secret.md")
    return lambda: prompts._read_agent_files(AGENT)


def _folder_tree_link(tree, monkeypatch):
    from configuration import prompts
    tree.link("a/workspace/linked", tree.canary / "folder")
    return lambda: prompts._scan_workspace(tree.at(AGENT), AGENT, username="u")


def _persons_context_link(tree, monkeypatch):
    from configuration import prompts
    tree.link("a/users/u/context/linked.md", tree.canary / "secret.md")
    return lambda: prompts.build_agent_prompt(AGENT, username="u")


def _stop_hook_transcript(tree, monkeypatch):
    from api.hooks import lifecycle
    from core.session import interactive_session, transcript_tailer

    async def _matched(*_a, **_k):
        return None

    monkeypatch.setattr(lifecycle, "verify_session_match_async", _matched)
    monkeypatch.setattr(interactive_session, "get", lambda _sid: SimpleNamespace(
        transcript_kind="claude", chat_id="chat-1", _tailer=lambda: transcript_tailer))
    monkeypatch.setattr(transcript_tailer, "_process_lines", lambda *_a, **_k: {})
    path = tree.link(f"a/users/u/.claude/projects/p/{SID}.jsonl", tree.canary / "secret.md")
    request = lifecycle.HookStopRequest(session_id=SID, transcript_path=str(path))
    return lambda: lifecycle.hook_stop(request, None)


def _transcript_tail(tree, monkeypatch):
    from core.session import transcript_tailer
    monkeypatch.setattr(transcript_tailer, "_process_lines", lambda *_a, **_k: {})
    path = tree.link(f"a/users/u/.claude/projects/p/{SID}.jsonl", tree.canary / "secret.md")
    return lambda: transcript_tailer.tail_transcript(SID, "chat-1", str(path))


def _transcript_search(tree, monkeypatch):
    from core.session import session_state, transcript_tailer
    monkeypatch.setitem(session_state._session_claude_dirs, SID, str(tree.at("a/users/u/.claude")))
    tree.link("a/users/u/.claude/projects", tree.canary / "folder")
    return lambda: transcript_tailer.resolve_transcript_path(SID)


def _memory_wipe(tree, monkeypatch):
    from core.layers.cli import config_dir
    from core.sandbox import skills_materializer
    monkeypatch.setattr(skills_materializer, "materialize_skills_for_sandbox", lambda *_a, **_k: None)
    tree.link("a/users/u/.claude/projects", tree.canary / "folder")
    return lambda: config_dir.ensure_persistent_claude_dir(AGENT, username="u", scope="user")


def _prompt_file(kind):
    def case(tree, monkeypatch):
        from core.layers.cli import spawn
        rel = f"a/users/u/.claude/{spawn._prompt_filename(SID)}"
        if kind == "link":
            tree.link(rel, tree.canary / "secret.md")
        else:
            tree.pipe(rel)
        return lambda: spawn._write_prompt_file(str(tree.at("a/users/u/.claude")), "the prompt", SID)
    return case


def _claude_json_seed(tree, monkeypatch):
    from services.engines import cli_settings_manager
    tree.link("a/users/u/.claude/.claude.json", tree.canary / "settings.json")
    return lambda: cli_settings_manager.seed_interactive_cli_config(str(tree.at("a/users/u/.claude")), "/workspace")


def _codex_rollout(kind):
    def case(tree, monkeypatch):
        from core.layers.codex import spawn
        if kind == "link":
            tree.link("a/users/u/.codex/sessions", tree.canary / "folder")
        else:
            tree.pipe("a/users/u/.codex/sessions/2026/rollout-2026-01-01T00-00-00-tid-9.jsonl")
        return lambda: spawn._resume_provider_changed(str(tree.at("a/users/u/.codex")), "tid-9", True)
    return case


def _codex_junk(tree, monkeypatch):
    from services.infra import retention
    tree.link("a/users/u/.codex/.tmp", tree.canary / "folder")
    return lambda: retention._pass_codex_junk(retention.LiveSnapshot(), collections.defaultdict(int), False)


def _ssh_keys(tree, monkeypatch):
    from auth.path_policy import SecurityContext
    from core.credentials import ssh_signer
    from core.sandbox import session_config_dir as scd
    monkeypatch.setattr(ssh_signer, "public_lines", lambda _agent: {"prod": "ssh-ed25519 AAAAhonest prod\n"})
    tree.at("a/users/v/.claude").rmdir()
    config_dir = tree.link("a/users/v/.claude", tree.canary / "folder")
    ctx = SecurityContext(role="editor", username="v", agent=AGENT, is_admin_agent=False)
    return lambda: scd.provision_ssh_keys_for_sandbox(ctx, AGENT, str(config_dir), "/users/v/.claude")


def _display_page(tree, monkeypatch):
    from api.hooks import artifacts
    from auth.path_policy import SecurityContext
    from core.session import session_state

    async def _matched(*_a, **_k):
        return None

    monkeypatch.setattr(artifacts, "verify_session_match_async", _matched)
    monkeypatch.setitem(session_state._session_security, SID,
                        SecurityContext(role="manager", username="u", agent=AGENT, is_admin_agent=False))
    tree.link("a/users/u/workspace/generated-ui", tree.canary / "folder")
    request = artifacts.HookUiRequest(session_id=SID, html="<p>an honest page</p>", display=False)
    return lambda: artifacts.hook_ui(request, None)


def _preview_digest(kind):
    def case(tree, monkeypatch):
        from api.hooks import preview
        if kind == "link":
            path = tree.link("a/users/u/workspace/doc.pdf", tree.canary / "secret.md")
        else:
            path = tree.pipe("a/users/u/workspace/doc.pdf")
        return lambda: preview.file_digest(path)
    return case


def _a_tiny_picture() -> str:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _pasted_photo(tree, monkeypatch):
    from ws import dashboard
    photos = tree.link("a/users/u/workspace/uploads/photos", tree.canary / "folder")
    picture = _a_tiny_picture()
    return lambda: dashboard._save_base64_image(f"data:image/png;base64,{picture}", photos,
                                                scope_dir=tree.at("a/users/u/workspace"))


def _check_files(kind):
    def case(tree, monkeypatch):
        from services.checks import documents
        from storage.checks import db_checks
        monkeypatch.setattr(db_checks, "upsert_index", lambda *_a, **_k: {"updated_by": "x", "updated_at": "t"})
        monkeypatch.setattr(db_checks, "delete_index", lambda *_a, **_k: False)
        monkeypatch.setattr(documents, "_commit", lambda *_a, **_k: None)
        if kind == "write":
            tree.at("a/config/checks/coding").mkdir(parents=True)
            tree.link("a/config/checks/coding/check.json", tree.canary / "secret.md")
            doc = {"name": "coding", "description": "d", "judge": {"rubric": "r"}}
            return lambda: documents.write_check(AGENT, "", doc, None, updated_by="x")
        tree.link("a/config/checks", tree.canary / "folder")
        return lambda: documents.delete_check(AGENT, "", "coding")
    return case


def _setup_copy(tree, monkeypatch):
    from services.community import community_agent_installer as installer
    from storage import database
    monkeypatch.setattr(database, "get_username_by_sub", lambda _sub: "u")
    tree.link("a/users/u/context/user-setup.md", tree.canary / "new.md")
    return lambda: installer.seed_user_setup_file(AGENT, "sub-u")


def _library_delete(tree, monkeypatch):
    from services.knowledge import library_projector
    from storage.knowledge import db_knowledge_libraries, db_library_mirror_state

    async def _quiet(*_a, **_k):
        return None

    monkeypatch.setattr(db_knowledge_libraries, "attachment_covering", lambda *_a, **_k: {"writable": True})
    monkeypatch.setattr(db_knowledge_libraries, "library_covering", lambda *_a, **_k: None)
    monkeypatch.setattr(library_projector, "_fan_out", _quiet)
    monkeypatch.setattr(db_library_mirror_state, "delete_consumer_subtree", lambda *_a, **_k: None)
    tree.write("src/workspace/readme.md")
    tree.link("src/knowledge/docs", tree.canary / "folder")
    return lambda: library_projector.propagate_mirror_delete(AGENT, "src", "docs/brand.md")


def _updater_dashboard(tree, monkeypatch):
    from services.community import community_agent_updater as upd
    from storage import database
    monkeypatch.setattr(database, "get_username_by_sub", lambda _sub: "u")
    monkeypatch.setattr(database, "upsert_app", lambda *_a, **_k: None)
    tree.at("a/config/community").mkdir(parents=True)
    tree.link("a/workspace/apps/board.html", tree.canary / "new.html")
    new = SimpleNamespace(dashboards=[SimpleNamespace(slug="board", html="<h1>B</h1>", visibility="agent",
                                                      title="B")], version="2.0.0")
    run = upd._Run(agent=AGENT, agent_dir=tree.at(AGENT), new=new, old=None, old_data={}, baseline={}, apply=True,
                   by_sub="s", by_role="admin")
    return lambda: upd._layer_dashboards(run)


def _app_database(tree, monkeypatch):
    from services.apps import app_render, releases
    row = {"agent": AGENT, "username": "", "id": "app-1", "slug": "board"}
    tree.at("a/app-data/shared").mkdir(parents=True)
    tree.link("a/app-data/shared/board", tree.canary / "folder")
    scratch = releases.app_release_dir(row) / "scratch-data-x"
    scratch.mkdir(parents=True)
    return lambda: app_render._seed(row, scratch)


def _sync_manifest_pipe(tree, monkeypatch):
    from core.remote import file_sync
    tree.pipe("a/workspace/notes.txt")
    return lambda: file_sync.compute_manifest(tree.at(AGENT))


def _platform_digest(tree, monkeypatch):
    from core.remote import remote_file_cache
    from storage.files import sync_state_store
    monkeypatch.setattr(remote_file_cache, "_merged_agents", {})
    remote_file_cache.note_signed_in("machine-1")
    monkeypatch.setattr(sync_state_store, "get_one", lambda *_a, **_k: None)
    path = tree.link("a/workspace/doc.txt", tree.canary / "secret.md")
    return lambda: remote_file_cache.platform_copy_unsettled(("machine-1", AGENT, "workspace/doc.txt"), path)


def _tool_outputs(tree, monkeypatch):
    from services.mcp import mcp_output_relocation
    shots = tree.link("a/users/u/workspace/.screenshots", tree.canary / "folder")
    return lambda: mcp_output_relocation._remove_folders([shots / "p"])


def _in_place_writer(tree, monkeypatch):
    from core.sandbox import session_config_dir as scd
    tree.link("a/users/u/.claude/hooks", tree.canary / "folder")
    return lambda: scd.write_no_follow(tree.at("a/users/u/.claude/hooks/settings.json"), b"{}")


def _quota_folders(tree, monkeypatch):
    from services.infra import storage_quota as sq
    monkeypatch.setattr(sq, "_hard_enabled", True)
    monkeypatch.setattr(sq, "_assigned_this_process", set())
    # The module remembers a refused scope per process.
    monkeypatch.setattr(sq, "_refused_this_process", set())
    monkeypatch.setattr(sq, "get_or_alloc_project", lambda *_a, **_k: 4242)
    monkeypatch.setattr(sq, "_run_helper", lambda *_a, **_k: None)
    tree.link("a/app-data/users", tree.canary / "folder")
    return lambda: sq.ensure_scope(AGENT, "user", "x")


def _credential_file(tree, monkeypatch):
    from services.engines import token_fanout
    tree.link("a/users/w", tree.canary / "folder" / "w")
    return lambda: token_fanout.write_credential_file(tree.at("a/users/w/.claude"), ".credentials.json", {"x": 1})


def _caller_homes(tree, monkeypatch):
    from services.infra import external_retention
    tree.link("a/externals/phone", tree.canary / "folder")
    return lambda: list(external_retention.iter_caller_homes())


def _caller_age(tree, monkeypatch):
    from services.infra import external_retention
    home = tree.link("a/externals/phone/p", tree.canary / "folder")
    return lambda: (external_retention.newest_mtime(home), external_retention._tree_bytes(home))


def _caller_removal(tree, monkeypatch):
    from services.infra import external_retention
    from storage.files import file_author_store, file_tombstones_store
    monkeypatch.setattr(file_author_store, "purge_prefix", lambda *_a: None)
    monkeypatch.setattr(file_tombstones_store, "purge_prefix", lambda *_a: None)
    monkeypatch.setattr(external_retention, "_forget_record", lambda *_a: None)
    tree.link("a/externals/phone", tree.canary / "folder")
    home = tree.at("a/externals/phone/p")
    return lambda: external_retention.remove_home(AGENT, home, {"errors": 0, "caller_bytes_freed": 0}, False)


def _ephemeral(kind):
    def case(tree, monkeypatch):
        from core.session import external_identity
        tree.link("a/externals/phone/_ephemeral", tree.canary / "folder")
        if kind == "prune":
            return lambda: external_identity.prune_ephemeral(tree.at("a/externals/phone/_ephemeral/p"))
        if kind == "session":
            return lambda: external_identity.prune_session_scratch(tree.at(AGENT), "p")
        return lambda: external_identity.scratch_home(tree.at(AGENT), "phone", "p")
    return case


def _caller_touch(tree, monkeypatch):
    from core.session import external_identity
    home = tree.link("a/externals/phone/p", tree.canary / "folder" / "p")
    return lambda: external_identity.touch_home(str(home))


_SHARE = {"snapshot_ref": "a/shares/users/u/p", "id": "p"}


def _share_copy(kind):
    def case(tree, monkeypatch):
        from services.sharing import chat_snapshot
        if kind == "removal":
            tree.link("a/shares/users/u", tree.canary / "folder")
            return lambda: chat_snapshot.remove(_SHARE)
        if kind == "sweep":
            tree.link("a/shares/users/u", tree.canary / "folder")
            return lambda: chat_snapshot.sweep_orphans(set())
        if kind == "read":
            tree.link("a/shares/users/u/p/chat.json", tree.canary / "settings.json")
            return lambda: chat_snapshot.load(_SHARE)
        tree.write("a/shares/users/u/p/chat.json", json.dumps({"files": {"t": "media/x.md"}}))
        tree.link("a/shares/users/u/p/media/x.md", tree.canary / "secret.md")

        def serve():
            opened = chat_snapshot.open_file(_SHARE, "t")
            if opened is not None:
                os.close(opened[0])
        return serve
    return case


def _cache_copy(tree, monkeypatch):
    from services.media import media_pipeline
    tree.link(".remote-host-cache/sess-1", tree.canary / "folder")
    row = {"abs_path": str(tree.at(".remote-host-cache/sess-1/brand.md")), "session_id": "sess-1",
           "agent": AGENT, "cache_owned": 1}
    return lambda: media_pipeline.remove_cache_copies([row])


_APP = {"agent": AGENT, "username": "", "id": "app-1", "slug": "board"}


def _app_folders(kind):
    def case(tree, monkeypatch):
        from services.apps import app_follow, app_supervisor
        log = "a/app-releases/shared/app-1/app.log"
        if kind == "log-link":
            tree.link(log, tree.canary / "secret.md")
            return lambda: app_supervisor.append_log(_APP, "a line\n")
        if kind == "log-pipe":
            tree.pipe(log)
            return lambda: app_supervisor.append_log(_APP, "a line\n")
        if kind == "log-tail":
            tree.link(log, tree.canary / "secret.md")
            return lambda: app_supervisor.read_log_tail(_APP)
        if kind == "data-folder":
            tree.link("a/app-data/shared", tree.canary / "folder")
            return lambda: app_supervisor._make_folder(tree.at("a/app-data/shared/board"))
        tree.link(f"a/app-releases/shared/app-1/{app_follow.SENT_BACK_NAME}", tree.canary / "settings.json")
        return lambda: (app_follow._note(_APP), app_follow._drop_note(_APP, 0))
    return case


def _codex_mask_point(tree, monkeypatch):
    from core.sandbox.sandbox import SandboxBuilder
    (tree.canary / "folder" / ".codex").mkdir()
    tree.link("a/users/w", tree.canary / "folder")
    root = Path(os.path.realpath(tree.at(AGENT)))
    return lambda: SandboxBuilder._shared_text_masks(root, root / "users" / "w" / ".codex", "/users/w/.codex")


#: The rows, by id.
ROWS: dict[str, Row] = {
    "prompt-persona-link": Row(_persona_link),
    "prompt-context-link": Row(_context_link),
    "prompt-folder-tree-link": Row(_folder_tree_link),
    "prompt-persons-context-link": Row(_persons_context_link),
    # The hook reads no posted path at all: no honest effect to see.
    "stop-hook-transcript": Row(_stop_hook_transcript, honest=False),
    "transcript-tail-link": Row(_transcript_tail),
    "transcript-search": Row(_transcript_search),
    "claude-memory-wipe": Row(_memory_wipe),
    "prompt-file-link": Row(_prompt_file("link")),
    "prompt-file-pipe": Row(_prompt_file("pipe")),
    "claude-json-seed": Row(_claude_json_seed),
    "codex-rollout-link": Row(_codex_rollout("link")),
    "codex-rollout-pipe": Row(_codex_rollout("pipe")),
    "codex-junk-pass": Row(_codex_junk),
    "ssh-key-folder": Row(_ssh_keys, refusals=(OSError,)),
    "display-page": Row(_display_page, refusals=(HTTPException,)),
    "preview-digest-link": Row(_preview_digest("link")),
    "preview-digest-pipe": Row(_preview_digest("pipe")),
    "pasted-photo": Row(_pasted_photo),
    "check-write": Row(_check_files("write")),
    "check-delete": Row(_check_files("delete"), refusals=(ValueError,)),
    "setup-copy": Row(_setup_copy),
    "library-delete": Row(_library_delete),
    "updater-dashboard": Row(_updater_dashboard),
    "app-database-copy": Row(_app_database),
    "sync-manifest-pipe": Row(_sync_manifest_pipe),
    "platform-digest-link": Row(_platform_digest),
    "tool-output-folders": Row(_tool_outputs),
    "in-place-writer": Row(_in_place_writer, refusals=(OSError,)),
    "quota-folders": Row(_quota_folders),
    "credential-file": Row(_credential_file, refusals=(OSError,)),
    "caller-homes-listing": Row(_caller_homes),
    "caller-tree-age": Row(_caller_age),
    "caller-tree-removal": Row(_caller_removal),
    "ephemeral-prune": Row(_ephemeral("prune")),
    "session-scratch-prune": Row(_ephemeral("session")),
    "scratch-home-make": Row(_ephemeral("make"), refusals=(OSError,)),
    "caller-touch": Row(_caller_touch),
    "share-copy-removal": Row(_share_copy("removal")),
    "share-orphan-sweep": Row(_share_copy("sweep")),
    "share-copy-read": Row(_share_copy("read")),
    "share-copy-serve": Row(_share_copy("serve")),
    "media-cache-copy": Row(_cache_copy),
    "app-log-link": Row(_app_folders("log-link")),
    "app-log-pipe": Row(_app_folders("log-pipe")),
    "app-log-tail": Row(_app_folders("log-tail")),
    "app-data-folder": Row(_app_folders("data-folder"), refusals=(OSError,)),
    "app-sent-back-note": Row(_app_folders("note")),
    "codex-mask-point": Row(_codex_mask_point),
}


@pytest.mark.parametrize("row", [pytest.param(row, id=name) for name, row in ROWS.items()])
def test_the_server_never_reaches_the_canary(tree, monkeypatch, row):
    _check(tree, row, monkeypatch)


@pytest.mark.parametrize("row", [pytest.param(row, id=name) for name, row in ROWS.items() if row.honest])
def test_each_row_drives_the_path_it_names(honest_tree, monkeypatch, row):
    """Over honest files the same call opens, writes or removes the names the
    row plants: a row that passes the canary test because its call died early
    is caught here."""
    _check_honest(honest_tree, row, monkeypatch)


def test_the_watch_sees_a_read_through_a_link(tree, monkeypatch):
    """The witness itself: a plain read through a planted link is a touch."""
    def case(t, _mp):
        link = t.link("a/workspace/linked.md", t.canary / "secret.md")
        return link.read_bytes
    with pytest.raises(CanaryTouched):
        _check(tree, Row(case), monkeypatch)


def test_the_watch_sees_a_listing_through_a_linked_folder(tree, monkeypatch):
    def case(t, _mp):
        link = t.link("a/workspace/linked", t.canary / "folder")
        return lambda: os.listdir(link)
    with pytest.raises(CanaryTouched):
        _check(tree, Row(case), monkeypatch)


def test_the_watch_sees_a_read_through_a_hard_link(tree, monkeypatch):
    def case(t, _mp):
        path = t.at("a/workspace/hard.md")
        os.link(t.canary / "secret.md", path)
        return path.read_bytes
    with pytest.raises(CanaryTouched):
        _check(tree, Row(case), monkeypatch)


def test_a_call_that_waits_on_a_pipe_fails_and_is_let_go(tree, monkeypatch):
    def case(t, _mp):
        pipe = t.pipe("a/workspace/notes.txt")
        return pipe.read_bytes
    with pytest.raises(WaitedOnAPipe):
        _check(tree, Row(case), monkeypatch)


def test_a_call_that_waits_on_a_pipe_in_a_worker_thread_fails_and_is_let_go(tree, monkeypatch):
    """The wait may sit in a thread the call started (``asyncio.to_thread``),
    not in the driving thread: it is seen there too, at once."""
    def case(t, _mp):
        pipe = t.pipe("a/workspace/notes.txt")

        async def read():
            return await asyncio.to_thread(pipe.read_bytes)
        return read
    started = time.monotonic()
    with pytest.raises(WaitedOnAPipe):
        _check(tree, Row(case), monkeypatch)
    assert time.monotonic() - started < _HANG_DEADLINE


def test_a_row_whose_call_dies_early_is_caught(honest_tree, monkeypatch):
    def case(t, _mp):
        t.link("a/workspace/linked.md", t.canary / "secret.md")
        return lambda: None
    with pytest.raises(DriverBroken):
        _check_honest(honest_tree, Row(case), monkeypatch)


def test_an_honest_safe_read_touches_nothing(tree, monkeypatch):
    """A read through the safe helpers refuses the link and the pipe at once
    and the row passes: the witness has no false alarm."""
    from services.infra import safe_fs

    def case(t, _mp):
        t.link("a/workspace/linked.md", t.canary / "secret.md")
        t.pipe("a/workspace/notes.txt")

        def run():
            for rel in ("a/workspace/linked.md", "a/workspace/notes.txt", "a/workspace/readme.md"):
                with contextlib.suppress(OSError):
                    safe_fs.read_bytes_beneath(config.AGENTS_DIR, rel, max_size=1024)
        return run
    _check(tree, Row(case), monkeypatch)
