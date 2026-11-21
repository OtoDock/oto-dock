"""The prompt build reads an agent's files beneath its real folder with no
link at any part (``configuration/prompts.py``): the persona, the context
folder, the folder tree and a person's context. A manager's or a person's
session writes these folders, so a link there never carries a file from
elsewhere into a prompt, a pipe is never waited on, and the honest files
still load as before."""

from __future__ import annotations

import logging
import os
import threading

import pytest

import config
from configuration import prompts

AGENT = "beneath"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """An agent under the test's own agents root, and a folder beside it
    holding the stand-in files a link points at."""
    root = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    agent_dir = root / AGENT
    (agent_dir / "config" / "context").mkdir(parents=True)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "users" / "u" / "context").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "folder").mkdir(parents=True)
    (elsewhere / "stand-in.md").write_text("STAND-IN TEXT")
    (elsewhere / "folder" / "stand-in-name.md").write_text("STAND-IN TEXT")
    return agent_dir, elsewhere


def _within(seconds, fn):
    out: list = []
    th = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    th.start()
    th.join(seconds)
    assert out, "the read waited"
    return out[0]


def test_a_persona_that_is_a_link_is_not_read(agent):
    agent_dir, elsewhere = agent
    (agent_dir / "config" / "agent.md").symlink_to(elsewhere / "stand-in.md")
    assert prompts._read_agent_files(AGENT) == []
    assert prompts.persona_is_unconfigured(AGENT) is False
    assert prompts.build_agent_prompt(AGENT) is None


def test_the_context_folder_loads_its_own_files_and_names_a_link(agent):
    agent_dir, elsewhere = agent
    ctx = agent_dir / "config" / "context"
    (agent_dir / "config" / "agent.md").write_text("# Persona\n\nHelp.")
    (ctx / "a-b").mkdir()
    (ctx / "a-b" / "x.md").write_text("second")
    (ctx / "a").mkdir()
    (ctx / "a" / "y.md").write_text("first")
    (ctx / "linked.md").symlink_to(elsewhere / "stand-in.md")
    (ctx / "linked-folder").symlink_to(elsewhere / "folder")
    os.mkfifo(ctx / "pipe.md")
    files = _within(10, lambda: prompts._read_agent_files(AGENT))
    assert files[0] == ("agent.md", "# Persona\n\nHelp.")
    assert files[1:3] == [("config/context/a/y.md", "first"), ("config/context/a-b/x.md", "second")]
    assert "STAND-IN TEXT" not in repr(files)
    assert files[-1][0] == "_auto_context_skipped.md"
    assert "config/context/linked.md (a link, not loaded)" in files[-1][1]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder with no permission bits")
def test_a_context_folder_it_cannot_read_keeps_the_others_loading(agent):
    agent_dir, _ = agent
    ctx = agent_dir / "config" / "context"
    (agent_dir / "config" / "agent.md").write_text("# Persona\n\nHelp.")
    (ctx / "a.md").write_text("readable")
    locked = ctx / "locked"
    locked.mkdir()
    (locked / "b.md").write_text("behind")
    locked.chmod(0)
    try:
        files = prompts._read_agent_files(AGENT)
    finally:
        locked.chmod(0o755)
    assert ("config/context/a.md", "readable") in files


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file with no permission bits")
def test_a_context_document_it_cannot_read_keeps_the_others_loading(agent):
    agent_dir, _ = agent
    ctx = agent_dir / "config" / "context"
    (agent_dir / "config" / "agent.md").write_text("# Persona\n\nHelp.")
    (ctx / "a.md").write_text("first")
    (ctx / "b.md").write_text("locked")
    (ctx / "c.md").write_text("third")
    (ctx / "b.md").chmod(0)
    try:
        files = prompts._read_agent_files(AGENT)
    finally:
        (ctx / "b.md").chmod(0o644)
    assert ("config/context/a.md", "first") in files
    assert ("config/context/c.md", "third") in files
    assert not any(rel == "config/context/b.md" for rel, _text in files)


def test_the_tree_names_a_link_and_never_enters_it(agent):
    agent_dir, elsewhere = agent
    (agent_dir / "workspace" / "notes.txt").write_text("x")
    (agent_dir / "workspace" / "real").mkdir()
    (agent_dir / "workspace" / "real" / "inside.md").write_text("x")
    (agent_dir / "workspace" / "linked-folder").symlink_to(elsewhere / "folder")
    tree = prompts._scan_workspace(agent_dir, AGENT, username=None) or ""
    assert "notes.txt" in tree and "real/" in tree and "inside.md" in tree
    assert "linked-folder@" in tree
    assert "stand-in-name.md" not in tree and "linked-folder/" not in tree


def test_a_linked_workspace_lists_nothing(agent):
    agent_dir, elsewhere = agent
    (agent_dir / "workspace").rmdir()
    (agent_dir / "workspace").symlink_to(elsewhere / "folder")
    tree = prompts._scan_workspace(agent_dir, AGENT, username=None) or ""
    assert "stand-in-name.md" not in tree and "/workspace/" not in tree


def test_a_persons_context_loads_its_own_files_only(agent):
    agent_dir, elsewhere = agent
    (agent_dir / "config" / "agent.md").write_text("# Persona\n\nHelp.")
    ctx = agent_dir / "users" / "u" / "context"
    (ctx / "mine.md").write_text("MY OWN NOTES")
    (ctx / "linked.md").symlink_to(elsewhere / "stand-in.md")
    os.mkfifo(ctx / "pipe.md")
    (ctx / ".hidden.md").write_text("MY HIDDEN NOTES")
    text = _within(10, lambda: prompts.build_agent_prompt(AGENT, username="u")) or ""
    assert "MY OWN NOTES" in text and "MY HIDDEN NOTES" in text
    assert "STAND-IN TEXT" not in text
    skipped = text.split("_user_context_skipped.md", 1)[1]
    assert "- `linked.md`: a link, not loaded" in skipped


def test_a_persons_context_of_links_alone_names_them(agent):
    agent_dir, elsewhere = agent
    (agent_dir / "config" / "agent.md").write_text("# Persona\n\nHelp.")
    (agent_dir / "users" / "u" / "context" / "linked.md").symlink_to(elsewhere / "stand-in.md")
    text = prompts.build_agent_prompt(AGENT, username="u") or ""
    assert "- `linked.md`: a link, not loaded" in text and "STAND-IN TEXT" not in text


def test_an_agent_whose_folder_is_a_link_loads_and_still_refuses_a_link_below(tmp_path, monkeypatch):
    """The operator may move an agent's folder and leave a link in its place:
    the build follows that one link and no link below it."""
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    real = tmp_path / "disk2" / AGENT
    (real / "config" / "context").mkdir(parents=True)
    (real / "workspace").mkdir()
    (real / "users" / "u" / "context").mkdir(parents=True)
    (root / AGENT).symlink_to(real)
    (real / "config" / "agent.md").write_text("# Persona\n\n")
    assert prompts.persona_is_unconfigured(AGENT) is True
    (real / "config" / "agent.md").write_text("# Persona\n\nTHE PERSONA.")
    (real / "config" / "context" / "a.md").write_text("THE CONTEXT")
    (real / "users" / "u" / "context" / "mine.md").write_text("MY OWN NOTES")
    (real / "workspace" / "notes.txt").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "stand-in.md").write_text("STAND-IN TEXT")
    (real / "config" / "context" / "linked.md").symlink_to(elsewhere / "stand-in.md")
    (real / "users" / "u" / "context" / "linked.md").symlink_to(elsewhere / "stand-in.md")
    assert prompts.persona_is_unconfigured(AGENT) is False
    text = prompts.build_agent_prompt(AGENT, username="u") or ""
    assert "THE PERSONA." in text and "THE CONTEXT" in text and "MY OWN NOTES" in text
    assert "notes.txt" in (prompts._scan_workspace(root / AGENT, AGENT, username="u") or "")
    assert "STAND-IN TEXT" not in text
    assert "config/context/linked.md (a link, not loaded)" in text
    assert "- `linked.md`: a link, not loaded" in text


def test_a_persons_context_folder_that_is_a_link_is_logged(agent, caplog):
    agent_dir, elsewhere = agent
    (agent_dir / "config" / "agent.md").write_text("# Persona\n\nHelp.")
    ctx = agent_dir / "users" / "u" / "context"
    ctx.rmdir()
    ctx.symlink_to(elsewhere / "folder")
    with caplog.at_level(logging.WARNING, logger="otodock.prompts"):
        text = prompts.build_agent_prompt(AGENT, username="u") or ""
    assert "STAND-IN TEXT" not in text
    assert "a person's context folder was not read (SymlinkRefused)" in caplog.text


def test_a_persona_that_is_not_utf8_is_logged_and_gives_no_prompt(agent, caplog):
    agent_dir, _ = agent
    (agent_dir / "config" / "agent.md").write_bytes(b"# Persona\n\n\xff\xfe not UTF-8")
    with caplog.at_level(logging.WARNING, logger="otodock.prompts"):
        assert prompts._read_agent_files(AGENT) == []
    assert "config/agent.md was not read (UnicodeDecodeError)" in caplog.text
