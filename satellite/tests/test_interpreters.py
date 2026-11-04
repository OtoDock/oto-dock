"""Where a Windows machine keeps a script's interpreters (``host/interpreters``):
Git for Windows' bash from the git on PATH or its install folders, never
WSL's bash; the install's Python, never the Store's alias."""

from __future__ import annotations

import sys

import pytest

from satellite.host import interpreters


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    interpreters.windows_interpreters.cache_clear()
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "no-python"))
    yield
    interpreters.windows_interpreters.cache_clear()


def _git(root):
    for rel in ("bin/bash.exe", "bin/sh.exe", "usr/bin/env.exe", "cmd/git.exe"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("")
    return root


def test_git_bash_from_the_git_on_path(tmp_path, monkeypatch):
    root = _git(tmp_path / "Git")
    monkeypatch.setattr(interpreters.shutil, "which",
                        lambda name: str(root / "cmd" / "git.exe") if name == "git" else None)
    tools = interpreters.windows_interpreters()
    assert tools["bash"] == str(root / "bin" / "bash.exe")
    assert tools["sh"] == str(root / "bin" / "sh.exe") and tools["env"].endswith("env.exe")


@pytest.mark.parametrize("var,rel", [("ProgramFiles", "Git"), ("LOCALAPPDATA", "Programs/Git")])
def test_git_bash_from_its_install_folders(tmp_path, monkeypatch, var, rel):
    """The service's PATH may lack Git's folder: the known roots still find it."""
    root = _git(tmp_path / rel)
    monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.setattr(interpreters.shutil, "which", lambda name: None)
    assert interpreters.windows_interpreters()["bash"] == str(root / "bin" / "bash.exe")


def test_the_install_folder_wins_over_a_git_on_path(tmp_path, monkeypatch):
    """A git.exe a program put early on PATH, with a bash.exe beside it,
    never runs the script checks while Git sits in its install folder."""
    planted = _git(tmp_path / "planted")
    installed = _git(tmp_path / "pf" / "Git")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setattr(interpreters.shutil, "which",
                        lambda name: str(planted / "cmd" / "git.exe") if name == "git" else None)
    tools = interpreters.windows_interpreters()
    assert tools["bash"] == str(installed / "bin" / "bash.exe")
    assert tools["env"] == str(installed / "usr" / "bin" / "env.exe")


def test_wsl_bash_on_path_is_never_taken(tmp_path, monkeypatch):
    """``System32\\bash.exe`` (WSL) answers a ``bash`` lookup: it is never asked."""
    monkeypatch.setattr(interpreters.shutil, "which",
                        lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None)
    assert interpreters.windows_interpreters()["bash"] == ""


def test_the_install_python_and_never_the_store_alias(tmp_path, monkeypatch):
    store = r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\python.exe"
    monkeypatch.setattr(interpreters.shutil, "which", lambda name: store if name.startswith("python") else None)
    assert interpreters.windows_interpreters()["python"] == ""
    interpreters.windows_interpreters.cache_clear()
    base = tmp_path / "Python313"
    base.mkdir()
    (base / "python.exe").write_text("")
    monkeypatch.setattr(sys, "base_prefix", str(base))
    assert interpreters.windows_interpreters()["python"] == str(base / "python.exe")
