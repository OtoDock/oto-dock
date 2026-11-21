"""Tracked folders for the history tests: a folder as an earlier release
left it, a script that would leave a mark, and one pass through every entry
point of ``git_writer``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from services.infra import git_writer

#: Every hook a call of git could run in a repository it is given.
HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-index-change",
         "reference-transaction", "post-checkout", "pre-auto-gc")

_PLAIN_ENV = {
    "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C", "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "otodock-system", "GIT_AUTHOR_EMAIL": "system@otodock",
    "GIT_COMMITTER_NAME": "otodock-system", "GIT_COMMITTER_EMAIL": "system@otodock",
}


def plain_git(cwd: Path, *args: str) -> str:
    """git as a writer of the folder would run it: in the folder, with the
    folder's own repository."""
    return subprocess.run(["git", *args], cwd=cwd, env=_PLAIN_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def old_layout(folder: Path) -> None:
    """The folder as a release before the move left it: its history in a
    ``.git`` of its own, with the first commit that release made."""
    plain_git(folder, "init", "-q", "-b", "main")
    (folder / ".gitignore").write_text("*.tmp\n*.bak\n.~*\n__pycache__/\n")
    plain_git(folder, "add", ".gitignore")
    plain_git(folder, "commit", "-q", "-m", "init")


def script(path: Path, marker: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {marker}\ncat \"$@\" 2>/dev/null\nexit 0\n")
    path.chmod(0o755)
    return path


def exercise(folder: Path) -> tuple[str | None, str | None]:
    """Every entry point, in the order the platform's callers use them."""
    git_writer.init_if_missing(folder)
    (folder / "memory").mkdir(exist_ok=True)
    topic = folder / "memory" / "topic.md"
    topic.write_text("one\n")
    first = git_writer.commit_paths(folder, [topic], "memory: create")
    topic.write_text("two\n")
    second = git_writer.commit_file(folder, topic, "memory: edit")
    git_writer.is_tracked(folder, "memory/topic.md")
    for row in git_writer.log(folder)[:4]:
        git_writer.diff(folder, row["sha"])
    if first:
        git_writer.revert_file_to(folder, first, topic, message="revert")
    return first, second


def subjects(folder: Path) -> list[str]:
    return [row["subject"] for row in git_writer.log(folder)]
