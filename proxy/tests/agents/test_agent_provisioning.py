"""The stores' post-commit steps run through the hooks
``services/agents/agent_provisioning.py`` installs: storage imports nothing
above it, and a store call still gets its folders, repos and quota as the
direct calls gave them (the test process installs them in conftest)."""

import ast
import logging

import config
from services.agents import agent_provisioning
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
    assert (config.get_agent_dir("prov-a") / "config" / ".git").is_dir()


def test_a_membership_makes_the_folders_and_a_removal_drops_the_memory():
    agent_store.create_agent("prov-b", "Prov B")
    task_store.upsert_user(PERSON, "pp@t.com", "Person", "member")
    uname = task_store.get_username_by_sub(PERSON)
    task_store.set_user_agents(PERSON, ["prov-b"], ADMIN)
    user_dir = config.get_agent_dir("prov-b") / "users" / uname
    assert (user_dir / "workspace").is_dir()
    assert (user_dir / "context" / ".git").is_dir()
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
    assert not (config.get_agent_dir("prov-d") / "config" / ".git").exists()


def test_install_is_idempotent():
    agent_provisioning.install()
    agent_provisioning.install()
    assert agent_store._created_hooks.count(agent_provisioning.provision_agent_tree) == 1
    assert db_users._agent_folder_hooks.count(agent_provisioning._ensure_user_agent_dirs) == 1
