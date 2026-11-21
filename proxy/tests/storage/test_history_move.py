"""An install's earlier histories are taken whole, and nothing of an old
``.git`` is ever run or obeyed on the way.

Until 2.0 each history's ``.git`` sat inside the folder it tracked. The
tests build such folders with plain git, plant what a writer of the folder
could plant, let the platform take the history, and check: no planted
command ran, the history is complete, the old folder left the tracked one,
and what an honest folder never holds was named and kept aside.

The module stands alone, like ``test_git_writer``.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

import config
from services.infra import git_writer, history_move
from tests.storage._git_folders import (
    HOOKS,
    exercise,
    old_layout,
    plain_git,
    script,
    subjects,
)


@pytest.fixture
def agents(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 0)
    monkeypatch.setattr(git_writer, "_locks", {})
    return root


@pytest.fixture
def folder(agents):
    path = agents / "alpha" / "users" / "bob" / "context"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "ran"


def written(folder: Path, count: int = 6, *, pack_at: int = 3) -> list[str]:
    """An old-layout folder with ``count`` memory writes, some of them
    packed as ``gc`` leaves them. Returns the subjects, newest first."""
    old_layout(folder)
    (folder / "memory").mkdir(exist_ok=True)
    for i in range(1, count + 1):
        (folder / "memory" / "topic.md").write_text(f"version {i}\n")
        plain_git(folder, "add", "memory/topic.md")
        plain_git(folder, "commit", "-q", "-m", f"memory: write {i} (user scope, by bob)")
        if i == pack_at:
            plain_git(folder, "gc", "-q")
    return [f"memory: write {i} (user scope, by bob)" for i in range(count, 0, -1)] + ["init"]


def store_of(folder: Path) -> Path:
    return git_writer.store_path(git_writer.entry_rel(folder))


def aside(slug: str = "alpha") -> list[Path]:
    root = git_writer.history_root() / "set-aside" / slug
    return sorted(root.iterdir()) if root.is_dir() else []


# ---------------------------------------------------------------------------
# An honest history
# ---------------------------------------------------------------------------


def test_a_history_is_taken_whole_and_goes_on(folder, caplog):
    expected = written(folder)
    tip = plain_git(folder, "rev-parse", "HEAD")
    with caplog.at_level(logging.INFO):
        assert subjects(folder) == expected
    assert git_writer._head_sha(store_of(folder)) == tip
    assert not (folder / ".git").exists(), "the old git folder left the tracked folder"
    assert (folder / "memory" / "topic.md").read_text() == "version 6\n"
    assert aside() == [] and "took the history of alpha/users/bob/context" in caplog.text
    assert "never writes" not in caplog.text and "could not be taken" not in caplog.text
    assert not (store_of(folder) / history_move.PENDING_FILE).exists()
    # The next write is the old tip's child, and every old version is there.
    (folder / "memory" / "topic.md").write_text("version 7\n")
    new = git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "memory: write 7")
    parent = git_writer._git(store_of(folder), "rev-parse", f"{new}^").stdout.decode().strip()
    assert parent == tip and subjects(folder) == ["memory: write 7", *expected]
    first = git_writer.log(folder)[-2]["sha"]
    assert git_writer.revert_file_to(folder, first, folder / "memory" / "topic.md")
    assert (folder / "memory" / "topic.md").read_text() == "version 1\n"


def test_an_honest_folder_shows_no_sign(folder):
    written(folder, 8, pack_at=4)
    plain_git(folder, "gc", "-q")
    (folder / "memory" / "topic.md").write_text("after the gc\n")
    plain_git(folder, "add", "memory/topic.md")
    plain_git(folder, "commit", "-q", "-m", "after the gc")
    assert history_move.signs("alpha/users/bob/context/.git") == []


def test_what_the_tip_does_not_reach_stays_behind(folder):
    written(folder)
    (folder / "stray.txt").write_text("never committed\n")
    stray = plain_git(folder, "hash-object", "-w", "stray.txt")
    subjects(folder)
    store = store_of(folder)
    assert git_writer._git(store, "cat-file", "-e", stray).returncode != 0
    assert git_writer._git(store, "count-objects", "-v").stdout.decode().count("count: 0") == 1


def test_the_stores_own_files_are_the_servers_alone(folder, marker):
    written(folder)
    plain_git(folder, "config", "core.fsmonitor", f"touch {marker}")
    plain_git(folder, "config", "user.name", "somebody")
    subjects(folder)
    store = store_of(folder)
    assert sorted(p.name for p in store.iterdir()) == ["HEAD", "config", "objects", "refs"]
    text = (store / "config").read_text()
    assert "fsmonitor" not in text and "somebody" not in text and text.count("[") == 1
    assert (store / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert not marker.exists()


# ---------------------------------------------------------------------------
# What a writer could have planted in the old folder
# ---------------------------------------------------------------------------


def _hook(folder, marker, tmp_path):
    for name in HOOKS:
        script(folder / ".git" / "hooks" / name, marker)
    return "hooks/post-commit"


def _hooks_path(folder, marker, tmp_path):
    for name in HOOKS:
        script(folder / "elsewhere" / name, marker)
    plain_git(folder, "config", "core.hooksPath", str(folder / "elsewhere"))
    return "config key core.hookspath"


def _monitor(folder, marker, tmp_path):
    plain_git(folder, "config", "core.fsmonitor", f"touch {marker}")
    return "config key core.fsmonitor"


def _signing(folder, marker, tmp_path):
    plain_git(folder, "config", "gpg.program", str(script(tmp_path / "signer", marker)))
    plain_git(folder, "config", "log.showSignature", "true")
    return "config section [gpg]"


def _filter(folder, marker, tmp_path):
    (folder / ".git" / "info" / "attributes").write_text("* filter=planted diff=planted\n")
    plain_git(folder, "config", "filter.planted.clean", f"sh -c 'touch {marker}; cat'")
    plain_git(folder, "config", "diff.planted.textconv", str(script(tmp_path / "conv", marker)))
    return "info/attributes"


def _include(folder, marker, tmp_path):
    (folder / "extra.cfg").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
    plain_git(folder, "config", "include.path", str(folder / "extra.cfg"))
    return "config section [include]"


def _work_tree(folder, marker, tmp_path):
    (tmp_path / "other").mkdir()
    plain_git(folder, "config", "core.worktree", str(tmp_path / "other"))
    return "config key core.worktree"


def _partial_clone(folder, marker, tmp_path):
    plain_git(folder, "config", "extensions.partialClone", "origin")
    plain_git(folder, "config", "remote.origin.promisor", "true")
    plain_git(folder, "config", "remote.origin.url", f"ext::touch {marker}")
    plain_git(folder, "config", "protocol.ext.allow", "always")
    return "config section [extensions]"


def _replace_ref(folder, marker, tmp_path):
    first = plain_git(folder, "rev-list", "--max-parents=0", "HEAD")
    plain_git(folder, "replace", plain_git(folder, "rev-parse", "HEAD"), first)
    (folder / ".git" / "info" / "grafts").write_text("")
    return "info/grafts"


def _alternates(folder, marker, tmp_path):
    (folder / ".git" / "objects" / "info").mkdir(exist_ok=True)
    (folder / ".git" / "objects" / "info" / "alternates").write_text("/nonexistent/objects\n")
    return "objects/info/alternates"


def _extra_files(folder, marker, tmp_path):
    (folder / ".git" / "commondir").write_text("../elsewhere\n")
    (folder / ".git" / "worktrees").mkdir()
    return "commondir"


def _links(folder, marker, tmp_path):
    (folder / ".git" / "hooks" / "post-commit").symlink_to(script(tmp_path / "linked", marker))
    (folder / ".git" / "description").unlink()
    (folder / ".git" / "description").symlink_to("/etc/hostname")
    # One that stays inside the folder: the copy kept aside holds no link either.
    (folder / ".git" / "info" / "near").symlink_to("../config")
    return "a link hooks/post-commit to"


PLANTED = [_hook, _hooks_path, _monitor, _signing, _filter, _include, _work_tree, _partial_clone,
           _replace_ref, _alternates, _extra_files, _links]


@pytest.mark.parametrize("plant", PLANTED, ids=lambda f: f.__name__.strip("_"))
def test_nothing_planted_in_an_old_folder_runs_and_the_history_is_whole(folder, marker, tmp_path, caplog, plant):
    expected = written(folder)
    tip = plain_git(folder, "rev-parse", "HEAD")
    sign = plant(folder, marker, tmp_path)
    assert subjects(folder) == expected
    first, second = exercise(folder)
    assert first and second and not marker.exists()
    store = store_of(folder)
    assert git_writer._git(store, "merge-base", "--is-ancestor", tip, git_writer.BRANCH).returncode == 0
    assert not (folder / ".git").exists()
    # What an honest folder never holds is named, and the folder is kept.
    assert "held what the platform never writes" in caplog.text and sign in caplog.text
    kept = aside()
    assert len(kept) == 1 and kept[0].name.startswith("users-bob-context.old-git-")
    assert (kept[0] / "HEAD").is_file() and not any(p.is_symlink() for p in kept[0].rglob("*"))
    assert str(marker) not in caplog.text, "a sign names what was found, never its value"


@pytest.mark.parametrize("kind", ["file", "link"])
def test_a_git_that_is_no_folder_is_removed_and_said(folder, tmp_path, caplog, kind):
    other = tmp_path / "other"
    other.mkdir()
    written(other)
    tip = plain_git(other, "rev-parse", "HEAD")
    if kind == "file":
        (folder / ".git").write_text(f"gitdir: {other}/.git\n")
    else:
        (folder / ".git").symlink_to(other / ".git")
    assert subjects(folder) == []
    assert not os.path.lexists(folder / ".git")
    assert "was not a git folder" in caplog.text and str(other) in caplog.text
    assert (other / ".git" / "HEAD").is_file() and plain_git(other, "rev-parse", "HEAD") == tip
    assert git_writer.init_if_missing(folder) is True and subjects(folder) == ["init"]


# ---------------------------------------------------------------------------
# A history that cannot be taken, for what its folder holds
# ---------------------------------------------------------------------------


def _no_head(folder):
    (folder / ".git" / "HEAD").unlink()


def _head_is_text(folder):
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/../../config\n")


def _missing_object(folder):
    tip = plain_git(folder, "rev-parse", "HEAD")
    (folder / ".git" / "objects" / tip[:2] / tip[2:]).unlink()


def _wrong_object(folder):
    tip = plain_git(folder, "rev-parse", "HEAD")
    parent = plain_git(folder, "rev-parse", "HEAD^")
    target = folder / ".git" / "objects" / tip[:2] / tip[2:]
    source = folder / ".git" / "objects" / parent[:2] / parent[2:]
    target.chmod(0o644)
    target.write_bytes(source.read_bytes())


def _broken_pack(folder):
    pack = next((folder / ".git" / "objects" / "pack").glob("*.pack"))
    pack.chmod(0o644)
    data = bytearray(pack.read_bytes())
    data[len(data) // 2] ^= 0xFF
    pack.write_bytes(bytes(data))


def _tip_is_a_tree(folder):
    tree = plain_git(folder, "rev-parse", "HEAD^{tree}")
    (folder / ".git" / "refs" / "heads" / "main").write_text(tree + "\n")


def _objects_is_a_link(folder):
    real = folder / "elsewhere-objects"
    (folder / ".git" / "objects").rename(real)
    (folder / ".git" / "objects").symlink_to(real)


def _an_object_has_two_names(folder):
    tip = plain_git(folder, "rev-parse", "HEAD")
    os.link(folder / ".git" / "objects" / tip[:2] / tip[2:], folder / "another-name")


def _an_object_is_a_pipe(folder):
    tip = plain_git(folder, "rev-parse", "HEAD")
    path = folder / ".git" / "objects" / tip[:2] / tip[2:]
    path.unlink()
    os.mkfifo(path)


def _head_names_no_branch(folder):
    tip = plain_git(folder, "rev-parse", "HEAD")
    (folder / ".git" / "ORIG_HEAD").write_text(tip + "\n")
    (folder / ".git" / "HEAD").write_text("ref: ORIG_HEAD\n")


def _loose(folder: Path, name: str) -> Path:
    return folder / ".git" / "objects" / name[:2] / name[2:]


def _a_commit_below_the_tip_holds_another_ones_bytes(folder):
    target = plain_git(folder, "rev-parse", "HEAD~1")
    source = plain_git(folder, "rev-parse", "HEAD~2")
    _loose(folder, target).chmod(0o644)
    _loose(folder, target).write_bytes(_loose(folder, source).read_bytes())


def _a_blob_below_the_tip_holds_bytes_the_history_never_had(folder):
    (folder / "stray.txt").write_text("never committed\n")
    stray = plain_git(folder, "hash-object", "-w", "stray.txt")
    (folder / "stray.txt").unlink()
    target = plain_git(folder, "rev-parse", "HEAD~1:memory/topic.md")
    _loose(folder, target).chmod(0o644)
    _loose(folder, target).write_bytes(_loose(folder, stray).read_bytes())


BROKEN = [_no_head, _head_is_text, _head_names_no_branch, _missing_object, _wrong_object, _broken_pack,
          _tip_is_a_tree, _objects_is_a_link, _an_object_has_two_names, _an_object_is_a_pipe,
          _a_commit_below_the_tip_holds_another_ones_bytes, _a_blob_below_the_tip_holds_bytes_the_history_never_had]


@pytest.mark.parametrize("damage", BROKEN, ids=lambda f: f.__name__.strip("_"))
def test_a_history_that_cannot_be_taken_starts_fresh_and_is_set_aside(folder, caplog, damage):
    written(folder)
    damage(folder)
    assert subjects(folder) == ["init"], "the first use decides: a new history starts"
    (folder / "memory" / "topic.md").write_text("after\n")
    assert git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "memory: after")
    assert subjects(folder) == ["memory: after", "init"]
    assert "could not be taken" in caplog.text and "A new one starts" in caplog.text
    assert not os.path.lexists(folder / ".git")
    kept = aside()
    assert len(kept) == 1 and (kept[0] / "config").is_file()
    assert not list((git_writer.history_root() / "incoming").iterdir())


def test_a_history_in_another_object_format_is_set_aside(folder, caplog):
    try:
        plain_git(folder, "init", "-q", "-b", "main", "--object-format=sha256")
    except Exception:  # noqa: BLE001  # a git with no SHA-256 support has nothing to test here
        pytest.skip("this git makes no SHA-256 repository")
    (folder / "f.md").write_text("x")
    plain_git(folder, "add", "f.md")
    plain_git(folder, "commit", "-q", "-m", "in sha256")
    assert git_writer.commit_paths(folder, [folder / "f.md"], "after")
    assert subjects(folder) == ["after", "init"] and len(aside()) == 1


def test_a_history_over_the_caps_is_not_copied(folder, monkeypatch, caplog):
    written(folder)
    monkeypatch.setattr(history_move, "MAX_HISTORY_FILES", 3)
    assert git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "after")
    assert "over the size a take allows" in caplog.text and subjects(folder) == ["after", "init"]


def test_a_step_that_runs_too_long_ends_the_take(folder, monkeypatch, caplog):
    written(folder)
    monkeypatch.setattr(history_move, "STEP_TIMEOUT", 0)
    assert git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "after")
    assert "took over 0 s" in caplog.text and subjects(folder) == ["after", "init"]


def test_every_step_of_a_take_is_bounded(agents):
    prefix = history_move._bounded()
    assert prefix[0] == "/bin/sh" and "oom_score_adj" in prefix[2]
    assert f"ulimit -v {2 * 1024 * 1024}" in prefix[-2] and 'exec "$@"' in prefix[-2]
    seen: list[list[str]] = []
    real = git_writer.subprocess.run

    def spy(cmd, **kwargs):
        seen.append(list(cmd))
        return real(cmd, **kwargs)

    folder = agents / "alpha" / "config"
    folder.mkdir(parents=True)
    written(folder)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(git_writer.subprocess, "run", spy)
        assert history_move.take("alpha/config") is True
    steps = [cmd for cmd in seen if any(w in ("index-pack", "pack-objects", "update-ref") for w in cmd)]
    assert len(steps) >= 4
    for cmd in steps:
        assert cmd[:len(prefix)] == prefix and "pack.threads=1" in cmd
    packs = [cmd for cmd in steps if "index-pack" in cmd]
    assert all(any(w.startswith("--max-input-size=") for w in cmd) for cmd in packs)
    # Each old pack is checked object by object. The one repacked from the
    # tip is checked with every link too, in the new store.
    raw = [cmd for cmd in packs if "--fsck-objects" in cmd]
    final = [cmd for cmd in packs if "--strict" in cmd]
    assert raw and final and len(raw) + len(final) == len(packs)
    assert all("/incoming/" in cmd[-1] for cmd in packs)
    assert all(cmd[-1].endswith(".pack") for cmd in final) and len(final) == 1


def test_a_set_aside_copy_that_does_not_fit_leaves_the_old_folder(folder, monkeypatch, caplog):
    written(folder)
    plain_git(folder, "config", "core.fsmonitor", "true")
    monkeypatch.setattr(history_move, "MAX_SET_ASIDE_ENTRIES", 2)
    assert len(subjects(folder)) == 7
    assert (folder / ".git" / "HEAD").is_file() and aside() == []
    assert "could not be set aside" in caplog.text and "It stays where it is" in caplog.text
    assert not (store_of(folder) / history_move.PENDING_FILE).exists()
    # It is never read again: the history goes on from the store.
    script(folder / ".git" / "hooks" / "post-commit", folder / "ran")
    exercise(folder)
    assert not (folder / "ran").exists() and len(subjects(folder)) == 10


# ---------------------------------------------------------------------------
# The store's own trouble decides nothing
# ---------------------------------------------------------------------------


def test_with_no_room_nothing_is_decided_and_the_next_start_takes_it(folder, monkeypatch, caplog):
    expected = written(folder)
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10 ** 9)
    assert git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "no room") is None
    assert subjects(folder) == [] and git_writer.is_tracked(folder, "memory/topic.md") is False
    assert "not enough free space" in caplog.text
    assert (folder / ".git" / "HEAD").is_file() and not store_of(folder).exists()
    assert history_move.run_pass()["taken"] == 0 and history_move.pending()
    assert "The next start goes on from there" in caplog.text
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 0)
    assert history_move.run_pass() == {"existing": 0, "taken": 1, "fresh": 0, "none": 0}
    assert not history_move.pending() and subjects(folder) == expected


def test_with_no_git_program_nothing_is_decided(folder, monkeypatch):
    written(folder)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "no git") is None
    assert git_writer.log(folder) == [] and git_writer.init_if_missing(folder) is False
    assert (folder / ".git" / "HEAD").is_file() and history_move.pending()


# ---------------------------------------------------------------------------
# The pass and its marker
# ---------------------------------------------------------------------------


def test_the_pass_gives_every_folder_its_history_and_then_no_git_is_ever_taken(agents, marker, caplog):
    bob = agents / "alpha" / "users" / "bob" / "context"
    bob.mkdir(parents=True)
    expected = written(bob)
    (agents / "alpha" / "config").mkdir()
    written(agents / "alpha" / "config", 2, pack_at=0)
    (agents / "alpha" / "knowledge").mkdir()
    (agents / "alpha" / "users" / "carol" / "context").mkdir(parents=True)
    (agents / "alpha" / "users" / "carol" / "workspace").mkdir()
    (agents / "alpha" / "users" / ".hidden" / "context").mkdir(parents=True)
    (agents / "alpha" / "workspace" / ".git").mkdir(parents=True)
    (agents / "beta" / "config").mkdir(parents=True)
    (agents / ".offboarded" / "dave" / "alpha" / "context" / ".git").mkdir(parents=True)
    (agents / "stray-file").write_text("x")
    with caplog.at_level(logging.INFO):
        assert history_move.run_pass() == {"existing": 0, "taken": 2, "fresh": 3, "none": 0}
    assert "history: 2 taken from the folders, 3 started, 0 already in place" in caplog.text
    assert not history_move.pending()
    entries = git_writer.history_root() / "agents"
    assert sorted(p.relative_to(entries).as_posix() for p in entries.rglob("*.git")) == [
        "alpha/config.git", "alpha/knowledge.git", "alpha/users/bob/context.git",
        "alpha/users/carol/context.git", "beta/config.git"]
    assert subjects(bob) == expected and not (bob / ".git").exists()
    assert (agents / "alpha" / "workspace" / ".git").is_dir(), "a folder with no history is not touched"
    assert (agents / ".offboarded" / "dave" / "alpha" / "context" / ".git").is_dir()
    assert history_move.run_pass() == {"existing": 0, "taken": 0, "fresh": 0, "none": 0}
    # After the marker, a history a writer makes up for a folder that has
    # none is never taken, and nothing in it runs.
    erin = agents / "alpha" / "users" / "erin" / "context"
    erin.mkdir(parents=True)
    made_up = written(erin)
    for name in HOOKS:
        script(erin / ".git" / "hooks" / name, marker)
    assert subjects(erin) == []
    exercise(erin)
    assert "init" in subjects(erin) and not set(made_up[:-1]) & set(subjects(erin))
    assert (erin / ".git" / "HEAD").is_file() and not marker.exists()


def test_a_crash_between_the_store_and_the_old_folder_is_finished_at_the_next_start(folder, monkeypatch):
    expected = written(folder)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(history_move, "finish", lambda rel: None)
        assert subjects(folder) == expected
    store = store_of(folder)
    assert (store / history_move.PENDING_FILE).read_text() == "remove\n" and (folder / ".git").is_dir()
    assert history_move.run_pass() == {"existing": 1, "taken": 0, "fresh": 0, "none": 0}
    assert not (folder / ".git").exists() and not (store / history_move.PENDING_FILE).exists()
    assert subjects(folder) == expected


def test_a_store_half_built_by_a_process_that_is_gone_is_removed(folder):
    incoming = git_writer.history_root() / "incoming"
    (incoming / "0000dead-leftover" / "objects").mkdir(parents=True)
    mine = incoming / f"{git_writer.PROCESS_TOKEN}-in-flight"
    mine.mkdir()
    history_move.run_pass()
    assert sorted(p.name for p in incoming.iterdir()) == [mine.name]


def test_two_writes_during_the_move_are_both_on_top_of_the_whole_history(folder):
    expected = written(folder)
    results: list[str | None] = []

    def write(i: int) -> None:
        path = folder / f"{i}.md"
        path.write_text(str(i))
        results.append(git_writer.commit_paths(folder, [path], f"write {i}"))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    pass_thread = threading.Thread(target=history_move.run_pass)
    for t in (*threads, pass_thread):
        t.start()
    for t in (*threads, pass_thread):
        t.join()
    assert all(results)
    names = subjects(folder)
    assert names[4:] == expected and sorted(names[:4]) == [f"write {i}" for i in range(4)]
    assert not (folder / ".git").exists() and not history_move.pending()


def test_the_marker_is_the_stores_own_file(agents):
    history_move.run_pass()
    marker_file = git_writer.history_root() / history_move.MARKER
    assert marker_file.is_file() and marker_file.read_text().startswith("20")
    # An agent may carry any of the store's names: its histories live apart.
    for name in ("legacy-taken", "set-aside", "incoming", "offboarded", "home", "agents"):
        path = agents / name / "config"
        assert git_writer.init_if_missing(path) is True
        assert git_writer.store_path(f"{name}/config") == git_writer.history_root() / "agents" / name / "config.git"
    assert marker_file.is_file() and not history_move.pending()


def test_the_boot_starts_the_pass_in_the_background():
    """One pass per start, off the event loop, never awaited before the
    server listens."""
    import ast
    source = (Path(__file__).resolve().parents[2] / "startup.py").read_text(encoding="utf-8")
    boot = next(node for node in ast.parse(source).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "_boot_registrations")
    starts = [ast.unparse(node) for node in ast.walk(boot)
              if isinstance(node, ast.Call) and "history_move.run_pass" in ast.unparse(node)]
    assert "_bg_tasks.append(asyncio.create_task(asyncio.to_thread(history_move.run_pass)))" in starts
    assert not any(isinstance(node, ast.Await) and "history_move" in ast.unparse(node) for node in ast.walk(boot))


def test_only_object_files_are_listed_for_the_copy(folder):
    """The copy takes files named and placed as objects and packs, nowhere
    else in the old folder."""
    written(folder)
    objects = folder / ".git" / "objects"
    name = "ab" * 19
    for odd in ("zz", "info", "pack/sub"):
        (objects / odd).mkdir(parents=True, exist_ok=True)
        (objects / odd / name).write_text("not an object\n")
    (objects / "pack" / "pack-not-hex.pack").write_text("x")
    (objects / "ab" / "short").parent.mkdir(exist_ok=True)
    (objects / "ab" / "short").write_text("x")
    listed = [inner for inner, _size in history_move._object_files("alpha/users/bob/context/.git")]
    assert listed and all(
        (len(inner) == 41 and inner[2] == "/" and inner[:2] != "zz") or
        (inner.startswith("pack/pack-") and inner.endswith(".pack") and len(inner) == 55)
        for inner in listed), listed
    assert not any(inner.startswith(("zz/", "info/", "pack/sub")) for inner in listed)


def test_a_repack_that_fails_fails_the_take_even_with_a_name(folder, monkeypatch, caplog):
    """git's word is its exit status: a repack that ends in failure never
    gives a history, whatever it printed."""
    import subprocess
    written(folder)
    real = history_move._step

    def failing_repack(store, *args, data=None):
        if "pack-objects" in args:
            done = real(store, *args, data=data)
            return subprocess.CompletedProcess(done.args, 1, done.stdout, b"")
        return real(store, *args, data=data)

    monkeypatch.setattr(history_move, "_step", failing_repack)
    assert git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "after")
    assert subjects(folder) == ["after", "init"] and "not complete from its tip" in caplog.text


def several_packs(folder: Path, packs: int, *, loose_after: bool) -> list[str]:
    """An old history as git's own clean-up leaves it after several runs: one
    pack per run, each newer one naming objects of the ones before, and with
    ``loose_after`` some loose objects written since the last run."""
    old_layout(folder)
    (folder / "memory").mkdir()
    writes = 0
    # A newer git packs loose objects by itself after a commit, in the
    # background: the runs below are the only clean-up this history gets.
    commit = ("-c", "maintenance.auto=false", "-c", "gc.auto=0", "commit", "-q", "-m")
    for _run in range(packs):
        for _ in range(3):
            writes += 1
            (folder / "memory" / "topic.md").write_text(f"version {writes}\n")
            plain_git(folder, "add", "memory/topic.md")
            plain_git(folder, *commit, f"write {writes}")
        plain_git(folder, "repack", "-d", "-q")
    if loose_after:
        for _ in range(2):
            writes += 1
            (folder / "memory" / "topic.md").write_text(f"version {writes}\n")
            plain_git(folder, "add", "memory/topic.md")
            plain_git(folder, *commit, f"write {writes}")
    assert len(list((folder / ".git" / "objects" / "pack").glob("*.pack"))) == packs
    return [f"write {i}" for i in range(writes, 0, -1)] + ["init"]


@pytest.mark.parametrize("packs, loose_after", [(2, False), (3, False), (2, True)],
                         ids=["two-packs", "three-packs", "packs-and-loose"])
@pytest.mark.parametrize("order", ["older-first", "newer-first"])
def test_a_history_in_several_packs_is_taken_whole_in_any_order(folder, monkeypatch, packs, loose_after, order):
    """The packs are read in whatever order the folder lists them: a newer one
    first must not fail the take, so each is checked on its own and the links
    are checked once, in the store repacked from the tip."""
    expected = several_packs(folder, packs, loose_after=loose_after)
    real = history_move._object_files

    def listed(old):
        files = real(old)
        found = [f for f in files if f[0].startswith("pack/")]
        rest = [f for f in files if not f[0].startswith("pack/")]
        age = {f: os.stat(config.AGENTS_DIR / old / "objects" / f[0]).st_mtime_ns for f in found}
        found.sort(key=age.__getitem__, reverse=(order == "newer-first"))
        return found + rest

    monkeypatch.setattr(history_move, "_object_files", listed)
    assert subjects(folder) == expected
    assert aside() == [] and not (folder / ".git").exists()
