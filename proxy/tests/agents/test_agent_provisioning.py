"""The stores' post-commit steps run through the hooks
``services/agents/agent_provisioning.py`` installs: storage imports nothing
above it, and a store call still gets its folders, repos and quota as the
direct calls gave them (the test process installs them in conftest)."""

import ast
import logging

import config
from services.agents import agent_provisioning
from services.infra import git_writer
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import db_users
from tests._paths import PROXY_DIR

ADMIN = "local:prov-admin"
PERSON = "local:prov-person"


def test_the_app_installs_the_hooks_before_anything_else_at_start():
    tree = ast.parse((PROXY_DIR / "startup.py").read_text(encoding="utf-8"))
    boot = next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_boot_registrations")
    calls = [n for stmt in boot.body for n in ast.walk(stmt) if isinstance(n, ast.Call)]
    first = calls[0].func
    assert isinstance(first, ast.Attribute) and first.attr == "install"
    assert isinstance(first.value, ast.Name) and first.value.id == "agent_provisioning"


def test_an_agent_gets_its_config_repo():
    agent_store.create_agent("prov-a", "Prov A")
    assert git_writer.store_path("prov-a/config").is_dir()
    assert git_writer.store_path("prov-a/knowledge").is_dir()
    assert not (config.get_agent_dir("prov-a") / "config" / ".git").exists()


def test_an_agents_histories_go_with_it_and_a_new_agent_of_the_name_has_none():
    agent_store.create_agent("prov-life", "Prov Life")
    folder = config.get_agent_dir("prov-life") / "config"
    (folder / "agent.md").write_text("the first agent")
    assert git_writer.commit_file(folder, folder / "agent.md", "the first agent's persona")
    assert agent_store.delete_agent("prov-life")
    assert not (git_writer.history_root() / "agents" / "prov-life").exists()
    # A removal that failed leaves histories under the name: the next agent
    # of that name never shows them.
    (git_writer.history_root() / "agents" / "prov-life" / "config.git").mkdir(parents=True)
    agent_store.create_agent("prov-life", "Prov Life Again")
    assert [row["subject"] for row in git_writer.log(folder)] == ["init"]
    assert len(list((git_writer.history_root() / "set-aside" / "prov-life").iterdir())) == 1


def test_a_membership_makes_the_folders_and_a_removal_drops_the_memory():
    agent_store.create_agent("prov-b", "Prov B")
    task_store.upsert_user(PERSON, "pp@t.com", "Person", "member")
    uname = task_store.get_username_by_sub(PERSON)
    task_store.set_user_agents(PERSON, ["prov-b"], ADMIN)
    user_dir = config.get_agent_dir("prov-b") / "users" / uname
    assert (user_dir / "workspace").is_dir()
    assert git_writer.store_path(f"prov-b/users/{uname}/context").is_dir()
    assert not (user_dir / "context" / ".git").exists()
    (user_dir / "context" / "memory").mkdir()
    (user_dir / "context" / "memory" / "topic.md").write_text("x")
    task_store.set_user_agents(PERSON, [], ADMIN)
    assert not (user_dir / "context" / "memory").exists()


def test_an_additive_membership_makes_the_folders():
    agent_store.create_agent("prov-c", "Prov C")
    task_store.upsert_user(PERSON, "pp@t.com", "Person", "member")
    uname = task_store.get_username_by_sub(PERSON)
    assert db_users.add_user_agent(PERSON, "prov-c", "viewer", ADMIN)
    assert (config.get_agent_dir("prov-c") / "users" / uname / "workspace").is_dir()


def test_a_store_with_no_hook_installed_says_so(monkeypatch, caplog):
    monkeypatch.setattr(agent_store, "_created_hooks", [])
    monkeypatch.setattr(db_users, "_agent_folder_hooks", [])
    with caplog.at_level(logging.ERROR):
        agent_store.create_agent("prov-d", "Prov D")
        task_store.upsert_user(PERSON, "pp@t.com", "Person", "member")
        task_store.set_user_agents(PERSON, ["prov-d"], ADMIN)
    assert "prov-d created with no provisioning hook installed" in caplog.text
    assert "No provisioning hook installed: no folders for" in caplog.text
    assert not git_writer.store_path("prov-d/config").exists()


def test_install_is_idempotent():
    agent_provisioning.install()
    agent_provisioning.install()
    assert agent_store._created_hooks.count(agent_provisioning.provision_agent_tree) == 1
    assert db_users._agent_folder_hooks.count(agent_provisioning._ensure_user_agent_dirs) == 1


def test_a_membership_makes_no_folder_through_a_link(tmp_path):
    """A person's folders are made beneath the agent's real folder with no
    link followed: a users folder replaced by a link gets nothing made
    where it points."""
    agent_store.create_agent("prov-link", "Prov Link")
    task_store.upsert_user(PERSON, "pp@t.com", "Person", "member")
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    users = config.get_agent_dir("prov-link") / "users"
    if users.exists():
        import shutil
        shutil.rmtree(users)
    users.symlink_to(stand_in)
    task_store.set_user_agents(PERSON, ["prov-link"], ADMIN)
    assert list(stand_in.iterdir()) == []
