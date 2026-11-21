"""The server's own git never runs or obeys what a writer of a tracked folder
left there.

A tracked folder (an agent's config, its knowledge, a person's context) is
written by sessions, by people through the file routes and by paired
computers. Each test prepares a folder the way such a writer can, with a
command that would make an empty file, runs every entry point of
``git_writer`` as the platform's callers do, and asserts the file was never
made and the write is in the history all the same.

The module stands alone: it places the agents and the sessions folder
itself, so it also runs with no database, on another git.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import threading
import time
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

PROXY = Path(__file__).resolve().parents[2]


#: Every test runs twice: on an install whose earlier histories were all
#: taken (a ``.git`` in a tracked folder is then never read), and on one
#: where they still wait (the folder's first use takes it, history_move).
@pytest.fixture(params=["settled", "moving"])
def agents(request, tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 0)
    monkeypatch.setattr(git_writer, "_locks", {})
    if request.param == "settled":
        assert history_move.run_pass() == {"existing": 0, "taken": 0, "fresh": 0, "none": 0}
        assert not history_move.pending()
    return root


@pytest.fixture
def folder(agents):
    path = agents / "alpha" / "users" / "bob" / "context"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "ran"


def recorded(folder: Path) -> None:
    """The exercise's writes are in this folder's own history."""
    names = subjects(folder)
    assert "memory: create" in names and "memory: edit" in names, names


# ---------------------------------------------------------------------------
# Every way git can be made to run something
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", HOOKS)
def test_a_hook_in_the_folders_own_git_never_runs(folder, marker, name):
    old_layout(folder)
    script(folder / ".git" / "hooks" / name, marker)
    exercise(folder)
    assert not marker.exists()
    recorded(folder)


def test_a_hooks_folder_named_by_the_folders_config_never_runs(folder, marker):
    old_layout(folder)
    for name in HOOKS:
        script(folder / "elsewhere" / name, marker)
    plain_git(folder, "config", "core.hooksPath", str(folder / "elsewhere"))
    exercise(folder)
    assert not marker.exists()
    recorded(folder)


def test_the_file_system_monitor_of_the_folders_config_never_runs(folder, marker):
    old_layout(folder)
    plain_git(folder, "config", "core.fsmonitor", f"touch {marker}")
    exercise(folder)
    assert not marker.exists()
    recorded(folder)


def test_a_signing_program_of_the_folders_config_never_runs(folder, marker, tmp_path):
    old_layout(folder)
    plain_git(folder, "config", "commit.gpgSign", "true")
    plain_git(folder, "config", "log.showSignature", "true")
    plain_git(folder, "config", "gpg.program", str(script(tmp_path / "signer", marker)))
    exercise(folder)
    assert not marker.exists()
    recorded(folder)


def test_a_filter_named_by_an_attributes_file_never_runs(folder, marker):
    old_layout(folder)
    (folder / ".gitattributes").write_text("* filter=planted\n")
    plain_git(folder, "config", "filter.planted.clean", f"sh -c 'touch {marker}; cat'")
    plain_git(folder, "config", "filter.planted.smudge", f"sh -c 'touch {marker}; cat'")
    exercise(folder)
    assert not marker.exists()
    recorded(folder)


def test_a_text_conversion_named_by_an_attributes_file_never_runs(folder, marker, tmp_path):
    old_layout(folder)
    (folder / ".gitattributes").write_text("*.md diff=planted\n")
    plain_git(folder, "add", ".gitattributes")
    plain_git(folder, "commit", "-q", "-m", "attributes")
    plain_git(folder, "config", "diff.planted.textconv", str(script(tmp_path / "conv", marker)))
    plain_git(folder, "config", "diff.planted.command", str(script(tmp_path / "differ", marker)))
    plain_git(folder, "config", "diff.external", str(script(tmp_path / "external", marker)))
    exercise(folder)
    assert not marker.exists()
    recorded(folder)


def test_a_config_the_folders_config_includes_is_never_read(folder, marker):
    old_layout(folder)
    (folder / "extra.cfg").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
    plain_git(folder, "config", "include.path", str(folder / "extra.cfg"))
    exercise(folder)
    assert not marker.exists()
    recorded(folder)


def test_another_work_tree_named_by_the_folders_config_is_never_written(folder, tmp_path):
    old_layout(folder)
    other = tmp_path / "other"
    (other / "memory").mkdir(parents=True)
    plain_git(folder, "config", "core.worktree", str(other))
    first, _ = exercise(folder)
    assert first, "the write went unrecorded"
    assert list((other / "memory").iterdir()) == []
    assert (folder / "memory" / "topic.md").read_text() == "one\n"
    recorded(folder)


def _other_history(tmp_path: Path) -> Path:
    other = tmp_path / "carol-context"
    other.mkdir()
    old_layout(other)
    (other / "secret.md").write_text("carol's own\n")
    plain_git(other, "add", "secret.md")
    plain_git(other, "commit", "-q", "-m", "memory: carol's own note")
    return other


def test_objects_borrowed_from_another_history_are_never_shown(folder, tmp_path):
    other = _other_history(tmp_path)
    old_layout(folder)
    (folder / ".git" / "objects" / "info").mkdir(exist_ok=True)
    (folder / ".git" / "objects" / "info" / "alternates").write_text(f"{other}/.git/objects\n")
    (folder / ".git" / "refs" / "heads" / "main").write_text(plain_git(other, "rev-parse", "HEAD") + "\n")
    exercise(folder)
    assert "memory: carol's own note" not in subjects(folder)
    recorded(folder)


@pytest.mark.parametrize("kind", ["file", "link"])
def test_a_git_that_points_at_another_history_is_never_followed(folder, tmp_path, kind):
    other = _other_history(tmp_path)
    tip = plain_git(other, "rev-parse", "HEAD")
    if kind == "file":
        (folder / ".git").write_text(f"gitdir: {other}/.git\n")
    else:
        (folder / ".git").symlink_to(other / ".git")
    exercise(folder)
    assert plain_git(other, "rev-parse", "HEAD") == tip, "the write landed in another person's history"
    assert "memory: carol's own note" not in subjects(folder)
    recorded(folder)


def test_an_emptied_git_never_sends_git_up_to_a_folder_above(agents, folder, marker):
    above = agents / "alpha"
    plain_git(above, "init", "-q", "-b", "main")
    for name in HOOKS:
        script(above / ".git" / "hooks" / name, marker)
    (folder / ".git").mkdir()
    exercise(folder)
    assert not marker.exists()
    assert plain_git(above, "rev-list", "--all", "--count") == "0", "a commit landed in the folder above"
    recorded(folder)


def test_a_repository_nested_in_the_folder_is_read_as_files_and_never_opened(folder, marker):
    (folder / "memory").mkdir()
    plain_git(folder / "memory", "init", "-q", "-b", "main")
    plain_git(folder / "memory", "config", "core.fsmonitor", f"touch {marker}")
    for name in HOOKS:
        script(folder / "memory" / ".git" / "hooks" / name, marker)
    exercise(folder)
    assert not marker.exists()
    recorded(folder)
    (folder / "memory" / "other.md").write_text("x\n")
    sha = git_writer.commit_paths(folder, [folder / "memory"], "memory: the folder")
    shown = git_writer.diff(folder, sha)
    assert "memory/other.md" in shown and ".git" not in shown


def test_an_index_written_by_hand_is_never_read(folder):
    old_layout(folder)
    (folder / ".git" / "index").write_bytes(b"DIRC" + b"\xff" * 64)
    first, second = exercise(folder)
    assert first and second
    recorded(folder)


def test_an_ignore_file_in_the_folder_cannot_keep_a_write_out_of_the_history(folder):
    old_layout(folder)
    (folder / ".gitignore").write_text("*\n")
    first, _ = exercise(folder)
    assert first and "topic.md" in git_writer.diff(folder, first)
    recorded(folder)


def test_an_attributes_file_in_the_folder_cannot_fail_or_hide_a_write(folder):
    old_layout(folder)
    (folder / ".gitattributes").write_text("*.md working-tree-encoding=UTF-16 -diff\n")
    first, _ = exercise(folder)
    assert first and "+one" in git_writer.diff(folder, first)
    recorded(folder)
    sha = git_writer.commit_paths(folder, [folder / ".gitattributes"], "the attributes file itself")
    (folder / "memory" / "topic.md").write_text("three\n")
    later = git_writer.commit_paths(folder, [folder / "memory" / "topic.md"], "memory: after")
    assert sha and "+three" in git_writer.diff(folder, later)


def test_a_mailmap_in_the_history_never_renames_an_author(folder):
    git_writer.init_if_missing(folder)
    (folder / ".mailmap").write_text("Somebody Else <else@example.com> otodock-system <system@otodock>\n")
    sha = git_writer.commit_paths(folder, [folder / ".mailmap"], "a mailmap")
    assert sha
    shown = git_writer.diff(folder, sha)
    assert "Author: otodock-system <system@otodock>" in shown
    assert {row["author"] for row in git_writer.log(folder)} == {"otodock-system"}


def test_the_servers_own_environment_and_global_config_never_reach_git(folder, marker, tmp_path, monkeypatch):
    other = _other_history(tmp_path)
    tip = plain_git(other, "rev-parse", "HEAD")
    home = tmp_path / "home"
    for name in HOOKS:
        script(home / "hooks" / name, marker)
    (home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {home}/hooks\n\tfsmonitor = touch {marker}\n[commit]\n\tgpgSign = true\n"
        f"[gpg]\n\tprogram = {home}/hooks/pre-commit\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(home / "hooks" / "pre-commit"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(other / ".git" / "objects"))
    exercise(folder)
    assert not marker.exists()
    assert plain_git(other, "rev-parse", "HEAD") == tip
    recorded(folder)


def test_a_path_is_never_read_as_an_option_or_as_a_pattern(folder):
    exercise(folder)
    assert git_writer.log(folder, path=":(top)") == []
    assert git_writer.log(folder, path=":(exclude)memory/topic.md") == []
    assert git_writer.log(folder, path="memory/*.md") == []
    assert len(git_writer.log(folder, path="memory/topic.md")) >= 2
    assert git_writer.diff(folder, "--output=/tmp/x") == ""
    assert git_writer.revert_file_to(folder, "--force", folder / "memory" / "topic.md") is None
    assert git_writer.is_tracked(folder, "--name-only") is False


# ---------------------------------------------------------------------------
# What a writer's bytes and names can do inside a history
# ---------------------------------------------------------------------------


def test_no_character_of_a_subject_makes_a_row_of_the_log(folder):
    git_writer.init_if_missing(folder)
    (folder / "f.md").write_text("x")
    forged = "0" * 40 + "\t2020-01-01T00:00:00+00:00\tsomebody\tnever happened"
    for separator in ("\x1c", "\x1d", "\x1e", "\x85", " ", "\x0b", "\x0c"):
        git_writer.commit_paths(folder, [folder / "f.md"], f"real{separator}{forged}")
    rows = git_writer.log(folder)
    assert len(rows) == 8
    assert all(row["author"] == "otodock-system" and row["sha"] != "0" * 40 for row in rows)


@pytest.mark.parametrize("name", ['q"uote.md', "back\\slash.md", "new\nline.md", "tab\there.md",
                                  "ctrl\x01\x7f.md", "ünï.md", "M 100644 inline x.md", "-dash.md"])
def test_a_files_name_is_only_ever_a_name(folder, name):
    git_writer.init_if_missing(folder)
    (folder / name).write_text("content\n")
    sha = git_writer.commit_paths(folder, [folder / name], "an odd name")
    assert sha
    assert git_writer.is_tracked(folder, name)
    assert "+content" in git_writer.diff(folder, sha)
    assert _refs(folder) == ["refs/heads/main"]


def _refs(folder: Path) -> list[str]:
    store = git_writer.store_path(git_writer.entry_rel(folder))
    out = git_writer._git(store, "for-each-ref", "--format=%(refname)").stdout.decode()
    return out.split()


def test_a_files_bytes_are_only_ever_content(folder):
    git_writer.init_if_missing(folder)
    planted = (b"x\ndone\ncommit refs/heads/planted\ncommitter a <a@b> 1 +0000\ndata 1\nz\n"
               b"M 100644 inline y\ndata 1\nq\nreset refs/heads/main\nfeature export-marks=/tmp/m\n")
    (folder / "f.md").write_bytes(planted)
    sha = git_writer.commit_paths(folder, [folder / "f.md"], "bytes that look like commands")
    assert sha and _refs(folder) == ["refs/heads/main"]
    assert not git_writer.is_tracked(folder, "y")
    store = git_writer.store_path(git_writer.entry_rel(folder))
    assert git_writer._git(store, "cat-file", "blob", f"{sha}:f.md").stdout == planted


def test_a_message_is_counted_in_bytes(folder):
    git_writer.init_if_missing(folder)
    (folder / "f.md").write_text("x")
    message = "memory: Ελληνικά (user scope, by Δημήτρης)\n\nfrom refs/heads/other\nM 100644 inline z\ndata 1\nq"
    assert git_writer.commit_paths(folder, [folder / "f.md"], message)
    assert subjects(folder)[0] == "memory: Ελληνικά (user scope, by Δημήτρης)"
    assert not git_writer.is_tracked(folder, "z") and _refs(folder) == ["refs/heads/main"]


def test_the_parts_git_is_never_handed():
    for path in ("", ".", "..", "a/../b", "a//b", "/a", "a/", ".git", ".GIT/x", "a/.Git", "a/b/.git/hooks/h"):
        assert git_writer._unsafe_part(path), path
    for path in ("a", "a/b.md", ".gitignore", ".gitattributes", "git", "a.git", ".github/x", "..a", "a.."):
        assert not git_writer._unsafe_part(path), path


@pytest.mark.parametrize("path", [".git/config", "memory/.git/hooks/post-commit", ".GIT/config", "a/../b.md"])
def test_a_part_git_must_not_be_handed_is_left_out(folder, path):
    git_writer.init_if_missing(folder)
    target = folder / ".git" / "config"
    target.parent.mkdir()
    target.write_text("[core]\n")
    (folder / "b.md").write_text("b")
    sha = git_writer.commit_paths(folder, [path], "a path no history takes")
    assert sha, "the commit is made, without the path"
    store = git_writer.store_path(git_writer.entry_rel(folder))
    names = git_writer._git(store, "ls-tree", "-r", "--name-only", "-z", sha).stdout
    assert names == b""


def test_a_link_is_recorded_as_a_link_and_a_link_on_the_way_is_no_delete(folder, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("not this folder's\n")
    git_writer.init_if_missing(folder)
    (folder / "memory").mkdir()
    (folder / "memory" / "kept.md").write_text("kept\n")
    assert git_writer.commit_paths(folder, [folder / "memory" / "kept.md"], "kept")
    (folder / "link.md").symlink_to(outside / "secret.md")
    sha = git_writer.commit_paths(folder, [folder / "link.md"], "a link")
    shown = git_writer.diff(folder, sha)
    assert "new file mode 120000" in shown and "not this folder's" not in shown
    # The folder is swapped for a link: the file is not readable safely, and
    # that is not the same as gone.
    (folder / "memory" / "kept.md").unlink()
    (folder / "memory").rmdir()
    (folder / "memory").symlink_to(outside)
    (outside / "kept.md").write_text("another folder's bytes\n")
    sha = git_writer.commit_paths(folder, [folder / "memory" / "kept.md"], "through a link")
    assert sha and "kept.md" not in git_writer.diff(folder, sha)
    assert git_writer.is_tracked(folder, "memory/kept.md")


def test_a_directory_commit_takes_files_and_leaves_out_what_the_template_ignored(folder):
    git_writer.init_if_missing(folder)
    checks = folder / "checks" / "one"
    (checks / "__pycache__").mkdir(parents=True)
    (checks / "sub" / ".git").mkdir(parents=True)
    (checks / "check.json").write_text("{}")
    (checks / "run.sh").write_text("#!/bin/sh\n")
    (checks / "run.sh").chmod(0o755)
    (checks / "draft.tmp").write_text("t")
    (checks / "old.bak").write_text("b")
    (checks / ".~lock").write_text("l")
    (checks / "__pycache__" / "x.pyc").write_text("c")
    (checks / "sub" / ".git" / "config").write_text("[core]\n")
    (checks / "sub" / "deep.md").write_text("d")
    # A file named `.git`, in any case, at any depth, is never a name in a
    # history either.
    (checks / ".GIT").write_text("gitdir: /elsewhere\n")
    (checks / "other").mkdir()
    (checks / "other" / ".git").write_text("gitdir: /elsewhere\n")
    os.mkfifo(checks / "pipe")
    sha = git_writer.commit_paths(folder, [checks], "checks: add one")
    store = git_writer.store_path(git_writer.entry_rel(folder))
    listing = git_writer._git(store, "ls-tree", "-r", sha).stdout.decode().splitlines()
    assert sorted(line.split("\t")[1] for line in listing) == [
        "checks/one/check.json", "checks/one/run.sh", "checks/one/sub/deep.md"]
    assert next(line for line in listing if line.endswith("run.sh")).startswith("100755")
    # The folder removed: one commit records every file as gone.
    for child in sorted(checks.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    checks.rmdir()
    gone = git_writer.commit_paths(folder, [checks], "checks: remove one")
    assert git_writer._git(store, "ls-tree", "-r", "--name-only", gone).stdout == b""


def test_a_commit_is_bounded_in_entries_and_bytes(folder, monkeypatch):
    git_writer.init_if_missing(folder)
    many = folder / "many"
    many.mkdir()
    for i in range(6):
        (many / f"{i}.md").write_text("x" * 10)
    monkeypatch.setattr(git_writer, "MAX_COMMIT_ENTRIES", 4)
    sha = git_writer.commit_paths(folder, [many], "many files")
    store = git_writer.store_path(git_writer.entry_rel(folder))
    assert len(git_writer._git(store, "ls-tree", "-r", "--name-only", sha).stdout.split()) == 4
    monkeypatch.setattr(git_writer, "MAX_COMMIT_ENTRIES", 5000)
    # One commit's bytes: three files of ten fit in 35, the rest wait.
    monkeypatch.setattr(git_writer, "MAX_COMMIT_BYTES", 35)
    other = folder / "other"
    other.mkdir()
    for i in range(6):
        (other / f"{i}.md").write_text("y" * 10)
    sha = git_writer.commit_paths(folder, [other], "bytes over the cap")
    assert len(git_writer._git(store, "ls-tree", "-r", "--name-only", sha, "--", "other").stdout.split()) == 3
    monkeypatch.setattr(git_writer, "MAX_COMMIT_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(git_writer, "MAX_FILE_BYTES", 5)
    (folder / "big.md").write_text("more than five bytes")
    sha = git_writer.commit_paths(folder, [folder / "big.md"], "a file over the cap")
    assert sha and not git_writer.is_tracked(folder, "big.md")


def test_a_folder_committed_whole_never_records_as_gone_a_file_it_could_not_read(folder, monkeypatch, caplog):
    """A read that is refused is never a delete, for a folder as for a file:
    the folder's other lines are written, and what the history had stays."""
    git_writer.init_if_missing(folder)
    one = folder / "checks" / "one"
    one.mkdir(parents=True)
    (one / "a.md").write_text("a")
    (one / "big.md").write_text("small")
    assert git_writer.commit_paths(folder, [one], "first")
    (one / "a.md").write_text("a, changed")
    (one / "big.md").write_text("now over the cap")
    monkeypatch.setattr(git_writer, "MAX_FILE_BYTES", 12)
    sha = git_writer.commit_paths(folder, [one], "the folder")
    shown = git_writer.diff(folder, sha)
    assert git_writer.is_tracked(folder, "checks/one/big.md") and "deleted file" not in shown
    assert "+a, changed" in shown
    assert sum("left out" in r.getMessage() for r in caplog.records) >= 1
    # A file that is gone from the folder is still recorded as gone.
    monkeypatch.setattr(git_writer, "MAX_FILE_BYTES", 32 * 1024 * 1024)
    (one / "a.md").unlink()
    sha = git_writer.commit_paths(folder, [one], "a file gone")
    assert "deleted file" in git_writer.diff(folder, sha) and not git_writer.is_tracked(folder, "checks/one/a.md")


def test_a_folder_past_the_entry_cap_says_so_once(folder, monkeypatch, caplog):
    git_writer.init_if_missing(folder)
    many = folder / "checks" / "many"
    many.mkdir(parents=True)
    for i in range(60):
        (many / f"{i:03}.md").write_text("x")
    monkeypatch.setattr(git_writer, "MAX_COMMIT_ENTRIES", 10)
    assert git_writer.commit_paths(folder, [many], "many")
    said = [r.getMessage() for r in caplog.records if "entries" in r.getMessage()]
    assert len(said) == 1 and "50" in said[0], said


# ---------------------------------------------------------------------------
# The reads, the revert
# ---------------------------------------------------------------------------


def test_init_and_commit_roundtrip(folder):
    assert git_writer.init_if_missing(folder) is True
    # Idempotent — second call is a no-op.
    assert git_writer.init_if_missing(folder) is False
    (folder / "topic.md").write_text("v1")
    sha1 = git_writer.commit_file(folder, folder / "topic.md", "first commit")
    assert sha1 and len(sha1) == 40
    (folder / "topic.md").write_text("v2")
    sha2 = git_writer.commit_file(folder, folder / "topic.md", "second commit")
    assert sha2 and sha2 != sha1
    rows = git_writer.log(folder, limit=10)
    assert [row["subject"] for row in rows] == ["second commit", "first commit", "init"]
    assert rows[0]["short_sha"] == sha2[:8] and rows[0]["date"].startswith("20")
    assert len(git_writer.log(folder, limit=1)) == 1


def test_several_paths_are_one_commit_and_a_delete_is_recorded(folder):
    a, b = folder / "memory" / "a.md", folder / "memory" / "MEMORY.md"
    a.parent.mkdir()
    a.write_text("topic")
    b.write_text("index")
    sha = git_writer.commit_paths(folder, [a, b], "memory: create (user scope, by alice)")
    assert sha and subjects(folder)[0] == "memory: create (user scope, by alice)"
    shown = git_writer.diff(folder, sha)
    assert "memory/a.md" in shown and "memory/MEMORY.md" in shown
    assert git_writer.is_tracked(folder, "memory/a.md") and git_writer.is_tracked(folder, "memory")
    a.unlink()
    gone = git_writer.commit_paths(folder, [a], "memory: delete")
    assert "deleted file" in git_writer.diff(folder, gone)
    assert not git_writer.is_tracked(folder, "memory/a.md")
    assert "MEMORY.md" in git_writer.diff(folder, sha, path="memory/MEMORY.md")
    assert "a.md" not in git_writer.diff(folder, sha, path="memory/MEMORY.md")
    # A path that never was is no failure.
    assert git_writer.commit_paths(folder, [folder / "memory" / "never.md"], "memory: delete")


def test_a_path_outside_the_folder_is_refused(folder, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x")
    assert git_writer.commit_paths(folder, [outside], "nope") is None
    assert git_writer.commit_paths(folder, [], "nothing") is None


def test_a_folder_that_is_not_tracked_has_no_history(agents, tmp_path):
    for path in (tmp_path / "anywhere", agents / "alpha" / "workspace", agents / "alpha",
                 agents / ".offboarded" / "bob" / "alpha" / "context", agents / "alpha" / "users" / "bob",
                 agents / "alpha" / "users" / ".hidden" / "context", agents / "alpha" / "config" / "context"):
        path.mkdir(parents=True, exist_ok=True)
        (path / "f.md").write_text("x")
        assert git_writer.init_if_missing(path) is False
        assert git_writer.commit_file(path, path / "f.md", "no") is None
        assert git_writer.log(path) == [] and git_writer.diff(path, "a" * 40) == ""
        assert git_writer.is_tracked(path, "f.md") is False
        assert git_writer.revert_file_to(path, "a" * 40, path / "f.md") is None
    assert not (git_writer.history_root() / git_writer.ENTRIES).exists()
    # Nothing was even looked up for them: no history's lock was taken.
    assert git_writer._locks == {}


def test_the_three_tracked_folders_each_have_a_store_of_their_own(agents):
    made = {}
    for rel in ("alpha/config", "alpha/knowledge", "alpha/users/bob/context", "alpha/users/carol/context"):
        path = agents / rel
        assert git_writer.init_if_missing(path) is True
        (path / "f.md").write_text(rel)
        made[rel] = git_writer.commit_file(path, path / "f.md", rel)
    for rel in made:
        assert subjects(agents / rel) == [rel, "init"]
    root = git_writer.history_root()
    assert (root / "agents" / "alpha" / "users" / "bob" / "context.git" / "HEAD").is_file()
    assert sorted(p.name for p in (root / "agents" / "alpha").iterdir()) == ["config.git", "knowledge.git", "users"]


def test_the_server_leaves_nothing_of_git_in_the_folder_and_no_hooks_in_the_store(folder):
    exercise(folder)
    assert sorted(p.name for p in folder.iterdir()) == ["memory"]
    store = git_writer.store_path(git_writer.entry_rel(folder))
    assert not (store / "hooks").exists() and not (store / "info").exists()
    assert not list(store.glob("fast_import_crash_*"))
    assert stat.S_IMODE(git_writer.history_root().stat().st_mode) == 0o700
    text = (store / "config").read_text()
    assert "bare = true" in text and "[core]" in text and text.count("[") == 1


def test_revert_puts_a_plain_file_back_and_commits(folder):
    first, _ = exercise(folder)
    topic = folder / "memory" / "topic.md"
    assert topic.read_text() == "one\n"
    assert subjects(folder)[0] == "revert"
    topic.unlink()
    (folder / "memory").rmdir()
    assert git_writer.revert_file_to(folder, first[:10], topic)
    assert topic.read_text() == "one\n" and subjects(folder)[0] == f"revert memory/topic.md to {first[:8]}"


def test_revert_takes_the_exec_bit_from_the_history(folder):
    git_writer.init_if_missing(folder)
    tool = folder / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o750)
    with_bit = git_writer.commit_file(folder, tool, "a script")
    tool.chmod(0o640)
    tool.write_text("changed\n")
    without = git_writer.commit_file(folder, tool, "no longer one")
    assert git_writer.revert_file_to(folder, with_bit, tool)
    assert stat.S_IMODE(tool.stat().st_mode) == 0o751 and tool.read_text() == "#!/bin/sh\n"
    assert git_writer.revert_file_to(folder, without, tool)
    assert stat.S_IMODE(tool.stat().st_mode) == 0o640


def test_revert_never_writes_a_link_a_folder_or_through_a_link(folder, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_text("untouched\n")
    git_writer.init_if_missing(folder)
    (folder / "memory").mkdir()
    (folder / "memory" / "a.md").write_text("a\n")
    (folder / "link.md").symlink_to(outside / "victim.md")
    sha = git_writer.commit_paths(folder, [folder / "memory", folder / "link.md"], "a folder and a link")
    (folder / "link.md").unlink()
    assert git_writer.revert_file_to(folder, sha, folder / "link.md") is None
    assert not (folder / "link.md").exists()
    assert git_writer.revert_file_to(folder, sha, folder / "memory") is None
    assert git_writer.revert_file_to(folder, sha, folder / "never.md") is None
    # A link planted at the file's place is replaced, its target untouched.
    (folder / "memory" / "a.md").unlink()
    (folder / "memory" / "a.md").symlink_to(outside / "victim.md")
    assert git_writer.revert_file_to(folder, sha, folder / "memory" / "a.md")
    assert not (folder / "memory" / "a.md").is_symlink()
    assert (outside / "victim.md").read_text() == "untouched\n"
    # A link planted at the folder above it refuses the write.
    (folder / "memory" / "a.md").unlink()
    (folder / "memory").rmdir()
    (folder / "memory").symlink_to(outside)
    assert git_writer.revert_file_to(folder, sha, folder / "memory" / "a.md") is None
    assert not (outside / "a.md").exists()


def test_revert_never_writes_a_file_over_the_cap(folder, monkeypatch):
    git_writer.init_if_missing(folder)
    (folder / "big.md").write_text("ten bytes!")
    sha = git_writer.commit_file(folder, folder / "big.md", "ten bytes")
    (folder / "big.md").write_text("now")
    monkeypatch.setattr(git_writer, "MAX_FILE_BYTES", 5)
    assert git_writer.revert_file_to(folder, sha, folder / "big.md") is None
    assert (folder / "big.md").read_text() == "now"


def test_revert_takes_only_a_commit_of_this_history(folder):
    first, _ = exercise(folder)
    store = git_writer.store_path(git_writer.entry_rel(folder))
    tree = git_writer._git(store, "rev-parse", f"{first}^{{tree}}").stdout.decode().strip()
    stray = git_writer._git(store, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree,
                            "-m", "not on the branch").stdout.decode().strip()
    assert len(stray) == 40
    topic = folder / "memory" / "topic.md"
    topic.write_text("now\n")
    assert git_writer.revert_file_to(folder, stray, topic) is None
    assert git_writer.revert_file_to(folder, tree, topic) is None
    assert git_writer.revert_file_to(folder, "f" * 40, topic) is None
    assert topic.read_text() == "now\n"


def test_two_writes_at_once_are_both_recorded(folder):
    git_writer.init_if_missing(folder)
    results: list[str | None] = []

    def write(i: int) -> None:
        path = folder / f"{i}.md"
        path.write_text(str(i))
        results.append(git_writer.commit_paths(folder, [path], f"write {i}"))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results) and len(set(results)) == 8
    assert len(git_writer.log(folder)) == 9


def test_a_failed_commit_leaves_no_report_in_the_store_and_the_history_goes_on(folder):
    """git's commit reader writes a report naming the commit's paths into
    the store when it stops: the store is the server's, so the report goes."""
    git_writer.init_if_missing(folder)
    store = git_writer.store_path(git_writer.entry_rel(folder))
    assert git_writer._write_commit(store, "a stream git refuses", [b"not a command\n"]) is None
    assert not list(store.glob("fast_import_crash_*"))
    (folder / "f.md").write_text("x")
    assert git_writer.commit_paths(folder, [folder / "f.md"], "after the refusal")
    assert subjects(folder) == ["after the refusal", "init"]


def test_a_history_that_cannot_be_read_is_set_aside_and_a_new_one_starts(folder, caplog):
    exercise(folder)
    store = git_writer.store_path(git_writer.entry_rel(folder))
    (store / "refs" / "heads" / "main").write_text("0123456789" * 4 + "\n")
    (folder / "f.md").write_text("x")
    sha = git_writer.commit_paths(folder, [folder / "f.md"], "after the damage")
    assert sha and subjects(folder) == ["after the damage", "init"]
    aside = list((git_writer.history_root() / "set-aside" / "alpha").iterdir())
    assert len(aside) == 1 and aside[0].name.startswith("users-bob-context.unreadable-")
    assert "cannot be read and is set aside" in caplog.text


# ---------------------------------------------------------------------------
# A history follows its folder
# ---------------------------------------------------------------------------


def test_an_agents_histories_go_with_the_agent(agents):
    for rel in ("alpha/config", "alpha/users/bob/context", "beta/config"):
        git_writer.init_if_missing(agents / rel)
    root = git_writer.history_root()
    (root / "set-aside" / "alpha").mkdir(parents=True)
    git_writer.forget_agent("alpha")
    assert not (root / "agents" / "alpha").exists() and not (root / "set-aside" / "alpha").exists()
    assert (root / "agents" / "beta" / "config.git").is_dir()
    for name in ("", ".", "..", "a/b", ".hidden"):
        git_writer.forget_agent(name)
    assert (root / "agents" / "beta" / "config.git").is_dir()


def test_a_new_agent_never_meets_histories_left_under_its_name(agents):
    git_writer.init_if_missing(agents / "alpha" / "config")
    (agents / "alpha" / "config" / "agent.md").write_text("the earlier agent")
    git_writer.commit_file(agents / "alpha" / "config", agents / "alpha" / "config" / "agent.md", "earlier")
    git_writer.set_aside_agent("alpha")
    assert git_writer.init_if_missing(agents / "alpha" / "config") is True
    assert subjects(agents / "alpha" / "config") == ["init"]
    kept = list((git_writer.history_root() / "set-aside" / "alpha").iterdir())
    assert len(kept) == 1 and kept[0].name.startswith("earlier-agent-")


def test_a_person_who_leaves_takes_their_history_out_of_the_names_reach(agents):
    context = agents / "alpha" / "users" / "bob" / "context"
    git_writer.init_if_missing(context)
    (context / "f.md").write_text("bob's")
    git_writer.commit_file(context, context / "f.md", "bob's own")
    git_writer.archive_person("alpha", "bob")
    root = git_writer.history_root()
    assert (root / "offboarded" / "bob" / "alpha" / "context.git").is_dir()
    # The name given out again starts with nothing.
    assert git_writer.init_if_missing(context) is True
    assert subjects(context) == ["init"]
    git_writer.archive_person("alpha", "nobody")
    git_writer.forget_archived("bob")
    assert not (root / "offboarded" / "bob").exists()


def test_a_store_set_aside_keeps_its_days_from_the_day_it_was_set_aside(agents):
    """A rename keeps a folder's own date: a store last changed long ago
    would be removed by the next daily pass at once."""
    (agents / "alpha").mkdir()
    store = git_writer.history_root() / "agents" / "alpha" / "config.git"
    store.mkdir(parents=True)
    long_ago = time.time() - (git_writer.SET_ASIDE_KEEP_DAYS + 20) * 86400
    os.utime(store, (long_ago, long_ago))
    kept = git_writer.set_aside(store, "alpha", "config.unreadable")
    assert kept is not None and kept.is_dir()
    assert git_writer.remove_orphans({"alpha"}) == 0
    assert kept.is_dir()


def test_only_what_has_no_row_and_no_folder_is_removed(agents, tmp_path, monkeypatch):
    root = git_writer.history_root()
    for name in ("row-and-folder", "row-only", "folder-only", "neither"):
        (root / "agents" / name / "config.git").mkdir(parents=True)
        (root / "set-aside" / name / "config.old-git-1").mkdir(parents=True)
    (agents / "row-and-folder").mkdir()
    (agents / "folder-only").symlink_to(tmp_path)
    old = root / "set-aside" / "row-only" / "config.old-git-0"
    old.mkdir()
    long_ago = time.time() - (git_writer.SET_ASIDE_KEEP_DAYS + 1) * 86400
    os.utime(old, (long_ago, long_ago))
    rows = {"row-and-folder", "row-only"}
    assert git_writer.remove_orphans(rows, dry_run=True) == 3
    assert (root / "agents" / "neither").is_dir() and old.is_dir()
    # An agents folder that is away removes nothing.
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "not-mounted")
    assert git_writer.remove_orphans(set()) == 0
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    assert git_writer.remove_orphans(rows) == 3
    assert sorted(p.name for p in (root / "agents").iterdir()) == ["folder-only", "row-and-folder", "row-only"]
    assert sorted(p.name for p in (root / "set-aside").iterdir()) == ["folder-only", "row-and-folder", "row-only"]
    assert not old.exists() and (root / "set-aside" / "row-only" / "config.old-git-1").is_dir()
    assert git_writer.remove_orphans(rows) == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a folder of mode 000")
def test_an_agents_folder_that_cannot_be_listed_removes_nothing(agents):
    """A folder that exists and cannot be listed answers every name as
    absent: the backstop must not read that as every agent gone."""
    root = git_writer.history_root()
    (agents / "kept").mkdir()
    (root / "agents" / "kept" / "config.git").mkdir(parents=True)
    agents.chmod(0)
    try:
        assert git_writer.remove_orphans(set()) == 0
    finally:
        agents.chmod(0o755)
    assert (root / "agents" / "kept" / "config.git").is_dir()


# ---------------------------------------------------------------------------
# One builder, one owner
# ---------------------------------------------------------------------------

#: The settings and the environment, by value. Several guard a way the calls
#: made here cannot reach (no index, no hooks folder, no terminal, no
#: transport), so no behaviour shows their loss: this list does.
SETTINGS = (
    "core.hooksPath=/dev/null", "core.fsmonitor=false", "core.attributesFile=/dev/null",
    "core.excludesFile=/dev/null", "attr.tree=refs/otodock/none", "commit.gpgSign=false",
    "tag.gpgSign=false", "log.showSignature=false", "log.mailmap=false", "mailmap.file=",
    "mailmap.blob=", "core.pager=cat", "core.editor=true", "core.sshCommand=false", "core.askPass=",
    "credential.helper=", "protocol.allow=never", "gc.autoDetach=false", "init.defaultRefFormat=files",
)


def test_every_call_names_its_store_and_carries_every_setting(agents):
    store = git_writer.store_path("alpha/config")
    cmd = git_writer.command(store, "log", "-z")
    assert cmd[:2] == ["git", f"--git-dir={store}"] and cmd[-2:] == ["log", "-z"]
    middle = cmd[2:-2]
    assert middle[0::2] == ["-c"] * len(SETTINGS) and tuple(middle[1::2]) == SETTINGS


def test_git_gets_a_fixed_environment(agents, monkeypatch):
    for name in ("GIT_DIR", "GIT_SSH_COMMAND", "GIT_EXEC_PATH", "GIT_TRACE", "EDITOR", "PAGER", "LD_PRELOAD"):
        monkeypatch.setenv(name, "/planted")
    monkeypatch.setenv("TZ", "Europe/Athens")
    env = git_writer._environment()
    home = str(git_writer.history_root() / "home")
    assert env == {
        "PATH": os.environ["PATH"], "TZ": "Europe/Athens", "LC_ALL": "C", "HOME": home, "XDG_CONFIG_HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }
    assert list(Path(home).iterdir()) == []


def test_git_runs_in_the_store_and_never_in_a_tracked_folder(folder, monkeypatch):
    seen: list[tuple[str, list[str]]] = []
    real = subprocess.run

    def spy(cmd, **kwargs):
        seen.append((str(kwargs.get("cwd")), list(cmd)))
        return real(cmd, **kwargs)

    monkeypatch.setattr(git_writer.subprocess, "run", spy)
    exercise(folder)
    assert seen
    tree = str(config.AGENTS_DIR)
    for cwd, cmd in seen:
        assert cwd == str(git_writer.history_root())
        assert cmd[0] == "git" and cmd[1].startswith(f"--git-dir={git_writer.history_root()}/")
        assert not any(tree in word for word in cmd), cmd
    # The words of each call, by value: no behaviour shows a flag that guards
    # a way the store's own config already closes.
    calls = {next(w for w in cmd if w in ("init", "show", "log", "fast-import", "ls-tree")): cmd
             for _cwd, cmd in seen if any(w in ("init", "show", "log", "fast-import", "ls-tree") for w in cmd)}
    assert calls["init"][-8:-1] == ["init", "-q", "--bare", "--template=", "--object-format=sha1", "-b", "main"]
    show = calls["show"]
    assert show[show.index("show"):][:7] == ["show", "--no-color", "--no-ext-diff", "--no-textconv",
                                             "--no-mailmap", "--end-of-options", show[show.index("show") + 6]]
    log = calls["log"]
    assert log[log.index("log"):][:3] == ["log", "-z", "--no-mailmap"] and log[-1] == "refs/heads/main"
    assert calls["fast-import"][-4:] == ["fast-import", "--quiet", "--done", "--date-format=raw"]
    assert calls["ls-tree"][calls["ls-tree"].index("ls-tree") + 1] == "-z"


#: Files under proxy/ that may name the git program in a command: the one
#: owner, and two that run inside a build or a session, never in the server.
GIT_NAMERS = {
    "services/infra/git_writer.py": "the server's own git, every call",
    "services/mcp/mcp_installer.py": "a tool's build, on a machine or in the image build",
    "hooks/credential_helper.py": "runs inside a session",
}


def _names_git(tree: ast.AST) -> bool:
    """A command list that opens with the git program (a tuple too, where
    it is handed to a call), or a lookup of the program by name."""
    def opens_with_git(node: ast.AST) -> bool:
        return (isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts)
                and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "git")

    for node in ast.walk(tree):
        if isinstance(node, ast.List) and opens_with_git(node):
            return True
        if isinstance(node, ast.Call):
            if any(opens_with_git(a) for a in node.args):
                return True
            if getattr(node.func, "attr", "") == "which" and any(
                    isinstance(a, ast.Constant) and a.value == "git" for a in node.args):
                return True
    return False


def test_only_the_owner_names_the_git_program():
    found = set()
    for path in PROXY.rglob("*.py"):
        rel = path.relative_to(PROXY).as_posix()
        if rel.split("/")[0] in ("tests", "docs", "venv", ".venv", "node_modules"):
            continue
        if _names_git(ast.parse(path.read_text(encoding="utf-8"))):
            found.add(rel)
    assert found == set(GIT_NAMERS), (
        "a file names the git program in a command: the server's git has one owner, "
        "services/infra/git_writer.py")
