"""An agent's folder as the root of the server's own file work in it
(``core/agent_folder.py``): the one link an operator may make at the agent's
folder is followed once, and a path is answered with that root and its rel,
or refused."""

from __future__ import annotations

import pytest

import config as app_config
from core import agent_folder
from services.infra import safe_fs


@pytest.fixture
def agents(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    (root / "pa" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(app_config, "AGENTS_DIR", root)
    return root


def test_the_root_is_the_agents_real_folder(agents, tmp_path):
    real = tmp_path / "disk2" / "linked"
    (real / "workspace").mkdir(parents=True)
    (agents / "linked").symlink_to(real)
    assert agent_folder.agent_root("pa") == (agents / "pa").resolve()
    assert agent_folder.agent_root("linked") == real.resolve()


def test_the_agent_holding_a_path(agents, tmp_path):
    real = tmp_path / "disk2" / "linked"
    (real / "users").mkdir(parents=True)
    (agents / "linked").symlink_to(real)
    assert agent_folder.agent_holding(agents / "pa" / "workspace" / "a.txt") == "pa"
    assert agent_folder.agent_holding(agents / "linked" / "users" / "b.txt") == "linked"
    assert agent_folder.agent_holding(real / "users" / "b.txt") == "linked"
    assert agent_folder.agent_holding(tmp_path / "elsewhere" / "c.txt") == ""
    assert agent_folder.agent_holding(agents / ".." / "x" / "c.txt") == ""


def test_the_root_and_the_rel_of_a_path_in_an_agents_folder(agents, tmp_path):
    real = tmp_path / "disk2" / "linked"
    (real / "users").mkdir(parents=True)
    (agents / "linked").symlink_to(real)
    root, rel = agent_folder.beneath_agent(agents / "pa" / "workspace" / "a.txt")
    assert (root, rel) == ((agents / "pa").resolve(), "workspace/a.txt")
    assert agent_folder.beneath_agent(real / "users" / "b.txt") == (real.resolve(), "users/b.txt")
    assert agent_folder.beneath_agent(agents / "pa" / "workspace" / "a.txt", "pa")[1] == "workspace/a.txt"


@pytest.mark.parametrize("where", ["elsewhere", "another-agent", "climbing"])
def test_a_path_in_no_agents_folder_or_another_ones_is_refused(agents, tmp_path, where):
    path = {"elsewhere": tmp_path / "elsewhere" / "c.txt",
            "another-agent": agents / "other" / "workspace" / "c.txt",
            "climbing": agents / "pa" / ".." / "other" / "c.txt"}[where]
    with pytest.raises(safe_fs.SafeFsError):
        agent_folder.beneath_agent(path, "pa")
    if where == "elsewhere":
        with pytest.raises(safe_fs.EscapeRefused):
            agent_folder.beneath_agent(path)


def test_a_listing_of_the_agents_folder_that_fails_is_raised_not_answered_none(agents, tmp_path, monkeypatch):
    """A path at a linked agent's real place needs the agents folder's
    listing; a listing that fails is an error, never "no agent holds it"."""
    real = tmp_path / "disk2" / "linked"
    (real / "users").mkdir(parents=True)
    (agents / "linked").symlink_to(real)
    import os

    def failing_scandir(path):
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(agent_folder.os, "scandir", failing_scandir, raising=False)
    monkeypatch.setattr(os, "scandir", failing_scandir)
    with pytest.raises(OSError) as raised:
        agent_folder.agent_holding(real / "users" / "b.txt")
    assert raised.value.errno == 24
    with pytest.raises(OSError) as raised:
        agent_folder.beneath_agent(real / "users" / "b.txt")
    assert not isinstance(raised.value, safe_fs.EscapeRefused)


def test_an_agents_folder_that_is_not_there_holds_no_path(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "no-agents")
    assert agent_folder.agent_holding(tmp_path / "elsewhere" / "c.txt") == ""


def test_an_entry_that_cannot_be_read_is_passed_over(agents, tmp_path, monkeypatch):
    real = tmp_path / "disk2" / "linked"
    (real / "users").mkdir(parents=True)
    (agents / "linked").symlink_to(real)
    (agents / "broken").symlink_to(tmp_path / "gone")
    import os
    real_path = os.path.realpath

    def flaky_realpath(path, *a, **k):
        if str(path).endswith("/broken"):
            raise OSError(5, "I/O error")
        return real_path(path, *a, **k)
    monkeypatch.setattr(os.path, "realpath", flaky_realpath)
    assert agent_folder.agent_holding(real / "users" / "b.txt") == "linked"


def test_a_path_joined_at_the_agents_folders_real_place_is_held_and_rooted(tmp_path, monkeypatch):
    """The agents folder itself a link and the agent's folder a link too: a
    path joined at the agents folder's real place answers the agent's real
    folder and its rel."""
    vol = tmp_path / "vol" / "agents"
    vol.mkdir(parents=True)
    (tmp_path / "agents-link").symlink_to(vol)
    disk2 = tmp_path / "disk2" / "linked"
    (disk2 / "users").mkdir(parents=True)
    (vol / "linked").symlink_to(disk2)
    (vol / "other" / "users").mkdir(parents=True)
    monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "agents-link")
    path = vol / "linked" / "users" / "b.txt"
    assert agent_folder.agent_holding(path) == "linked"
    assert agent_folder.beneath_agent(path) == (disk2.resolve(), "users/b.txt")
    with pytest.raises(safe_fs.EscapeRefused):
        agent_folder.beneath_agent(vol / "other" / "users" / "b.txt", "linked")
