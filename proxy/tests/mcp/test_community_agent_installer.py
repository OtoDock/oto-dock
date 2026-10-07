"""Tests for the community-agents installer.

Covers:
- Template parsing + validation (load_template_from_dir)
- Pre-flight (MCP-not-in-any-catalog hard error)
- Slug collision suggestion
- Admin cascade: all MCPs resolved inline, no requests
- Manager cascade: batch_id generated, mcp_assignment_requests rows created
- Task/trigger/notification seeding (idempotent)
- Cascade cleanup invariants
- Notification batching (one per admin per batch)
- Batch completion follow-up notification

Run: cd proxy && venv/bin/pytest tests/mcp/test_community_agent_installer.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ADMIN_SUB = "user-admin"
MANAGER_SUB = "user-manager"


def _write_template(
    tmp_path: Path,
    *,
    slug: str = "demo-template",
    mcps: list[dict] | None = None,
    tasks: list[dict] | None = None,
    triggers: list[dict] | None = None,
    notifications: list[dict] | None = None,
    setup_md: str | None = None,
    user_setup_md: str | None = None,
    skills: list[dict] | None = None,
    context_files: dict[str, str] | None = None,
    dashboards: list[dict] | None = None,
    dashboard_files: dict[str, str] | None = None,
    apps: dict[str, dict] | None = None,
    user_apps: dict[str, dict] | None = None,
    checks: dict[str, dict] | None = None,
    agent_json_extra: dict | None = None,
) -> Path:
    """Write a minimal valid template directory under tmp_path/<slug>/.

    ``apps`` / ``user_apps`` map an app slug to its ``app.json`` (an
    optional ``_blueprint`` key becomes ``blueprint.json``, ``_files`` extra
    files); ``checks`` map a check name to its document (an optional
    ``_script`` key becomes the script the document names)."""
    template_dir = tmp_path / slug
    template_dir.mkdir(parents=True, exist_ok=True)

    agent_json = {
        "schema_version": "1",
        "slug": slug,
        "display_name": slug.replace("-", " ").title(),
        "description": "Test template",
        "color": "#10B981",
        "version": "1.0.0",
        **(agent_json_extra or {}),
    }
    (template_dir / "agent.json").write_text(json.dumps(agent_json))
    for folder, items in (("apps", apps), ("user-apps", user_apps)):
        for app_slug, manifest in (items or {}).items():
            manifest = dict(manifest)
            blueprint = manifest.pop("_blueprint", None)
            extra = manifest.pop("_files", {})
            root = template_dir / folder / app_slug
            (root / "client").mkdir(parents=True, exist_ok=True)
            (root / "app.json").write_text(json.dumps(manifest))
            (root / "client" / "index.html").write_text("<p>app</p>")
            if blueprint is not None:
                (root / "blueprint.json").write_text(json.dumps(blueprint))
            for rel, content in extra.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(content)
    for name, doc in (checks or {}).items():
        doc = dict(doc)
        script = doc.pop("_script", None)
        root = template_dir / "checks" / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "check.json").write_text(json.dumps(doc))
        if script is not None:
            (root / doc["script"]["run"]).write_text(script)
    (template_dir / "agent.md").write_text("# Test Prompt\n")
    (template_dir / "mcps.json").write_text(
        json.dumps({"required": mcps or []})
    )
    (template_dir / "README.md").write_text("# README\n")
    if tasks:
        (template_dir / "tasks.json").write_text(json.dumps({"tasks": tasks}))
    if triggers:
        (template_dir / "triggers.json").write_text(json.dumps({"triggers": triggers}))
    if notifications:
        (template_dir / "notifications.json").write_text(
            json.dumps({"notifications": notifications})
        )
    if setup_md is not None:
        (template_dir / "setup.md").write_text(setup_md)
    if user_setup_md is not None:
        (template_dir / "user-setup.md").write_text(user_setup_md)
    if skills is not None:
        (template_dir / "skills.json").write_text(json.dumps({"required": skills}))
    if context_files:
        context_dir = template_dir / "context"
        context_dir.mkdir()
        for rel, content in context_files.items():
            (context_dir / rel).write_text(content)
    if dashboards is not None:
        (template_dir / "dashboards.json").write_text(
            json.dumps({"dashboards": dashboards})
        )
        dash_dir = template_dir / "dashboards"
        dash_dir.mkdir(exist_ok=True)
        for name, content in (dashboard_files or {}).items():
            (dash_dir / name).write_text(content)
    return template_dir


def _install_admin(template_dir: Path, target_slug: str = "demo-agent",
                   installer_sub: str | None = ADMIN_SUB,
                   installer_role: str = "admin"):
    # Tests stage templates as plain on-disk dirs and install directly via
    # the shared orchestrator. Production code only uses
    # ``install_from_catalog`` (which fetches the tarball first); this helper
    # skips the fetch since the test owns the dir.
    from services.community.community_agent_installer import install_from_extracted_template
    from storage.agents.community_agent_template_store import load_template_from_dir
    template = load_template_from_dir(template_dir)
    return asyncio.run(install_from_extracted_template(
        template=template, target_slug=target_slug,
        installer_user_sub=installer_sub, installer_role=installer_role,
        source_label="test",
    ))


# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------

class TestTemplateLoading:
    def test_minimal_valid_template(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path)
        template = load_template_from_dir(tdir)
        assert template.slug == "demo-template"
        assert template.display_name == "Demo Template"
        assert template.mcps == []
        assert template.tasks == []
        assert template.context_files == {}

    def test_missing_agent_json_raises(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = tmp_path / "bad"
        tdir.mkdir()
        (tdir / "prompt.md").write_text("x")
        (tdir / "mcps.json").write_text("{}")
        (tdir / "README.md").write_text("x")
        with pytest.raises(TemplateValidationError, match="agent.json"):
            load_template_from_dir(tdir)

    def test_invalid_slug_in_agent_json(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = tmp_path / "demo"
        tdir.mkdir()
        (tdir / "agent.json").write_text(json.dumps({
            "slug": "BadSlug", "display_name": "x", "version": "1.0.0",
        }))
        (tdir / "prompt.md").write_text("x")
        (tdir / "mcps.json").write_text("{}")
        (tdir / "README.md").write_text("x")
        with pytest.raises(TemplateValidationError, match="invalid slug"):
            load_template_from_dir(tdir)

    def test_invalid_cron_in_tasks(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = _write_template(tmp_path, tasks=[{
            "slug": "bad-task", "description": "x", "scope": "agent",
            "prompt": "echo",
            "schedule": {"type": "cron", "cron": "not a cron"},
        }])
        with pytest.raises(TemplateValidationError, match="invalid cron"):
            load_template_from_dir(tdir)

    def test_valid_task_with_cron_schedule(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path, tasks=[{
            "slug": "good-task", "description": "Test",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
        }])
        template = load_template_from_dir(tdir)
        assert len(template.tasks) == 1
        assert template.tasks[0].cron == "0 9 * * *"

    def test_setup_md_parsed_when_present(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path, setup_md="## Setup steps\n1. Do X\n")
        template = load_template_from_dir(tdir)
        assert template.setup_md is not None
        assert "Setup steps" in template.setup_md

    def test_context_collected(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path, context_files={
            "methodology.md": "## Methodology", "glossary.txt": "terms",
        })
        template = load_template_from_dir(tdir)
        assert set(template.context_files.keys()) == {
            "context/methodology.md", "context/glossary.txt",
        }


class TestTemplateAppsAndChecks:
    """Folder apps in ``apps/`` (shared) and ``user-apps/`` (per member),
    checks in ``checks/``: what the loader admits, what it hashes, what it
    persists (COMMUNITY-AGENTS-REGISTRY.md "Per-template layout")."""

    HOME = {"title": "Home", "egress": ["api.open-meteo.com"],
            "actions": [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]}

    def test_both_folders_discovered_with_their_hashes(self, tmp_path, temp_db):
        from storage.agents import template_sig
        from storage.agents.community_agent_template_store import load_template_from_dir
        tdir = _write_template(
            tmp_path, agent_json_extra={"collaborative": True},
            apps={"board": {"title": "Board", "requires": {"mcps": ["github-mcp"]},
                            "_blueprint": {"format": 1, "tasks": [{"slug": "r", "description": "R", "prompt": "go"}]}}},
            user_apps={"home": {**self.HOME, "_blueprint": {"roles": ["editor"], "auto_create_for_new_users": False}}},
        )
        template = load_template_from_dir(tdir)
        by_slug = {a.slug: a for a in template.apps}
        assert set(by_slug) == {"board", "home"}
        board, home = by_slug["board"], by_slug["home"]
        assert board.visibility == "agent" and home.visibility == "user"
        assert board.title == "Board" and board.requires_mcps == ["github-mcp"]
        assert [m.name for m in template.mcps] == ["github-mcp"]
        assert board.blueprint["tasks"][0]["slug"] == "r"
        assert home.roles == ["editor"] and home.auto_create_for_new_users is False
        assert board.auto_create_for_new_users is True and board.roles is None
        # The hashes: the tree without app.json (blueprint.json stays, it
        # travels verbatim), the manifest, the consent sig.
        assert home.tree_sha == template_sig.tree_sha(
            tdir / "user-apps/home", ["blueprint.json", "client/index.html"])
        assert home.app_json_sha == template_sig.sha256_text(template_sig.canonical(self.HOME))
        assert home.sig == template_sig.template_app_sig(self.HOME, home.blueprint, home.tree_sha)
        assert home.owner_approval is False
        assert board.sig != home.sig

    def test_signature_pins_and_ignores_formatting(self, tmp_path, temp_db):
        from storage.agents import template_sig
        doc = {"title": "X", "egress": ["api.open-meteo.com"]}
        assert template_sig.template_app_sig(doc, None, "abc") == \
            "b08d34904992bac762cdc2366e81dcbea1911fb932be03e8146ad17c6169c1a9"
        assert template_sig.template_app_sig({"egress": ["api.open-meteo.com"], "title": "X"}, None, "abc") == \
            template_sig.template_app_sig(doc, None, "abc")
        assert template_sig.check_sig({"name": "lint", "judge": {"rubric": "r"}}, "s") == \
            template_sig.sha256_text('{"doc":{"judge":{"rubric":"r"},"name":"lint"},"script_sha256":"s"}')
        # app.json never enters the tree hash (the importer rewrites it).
        root = tmp_path / "t"
        (root / "client").mkdir(parents=True)
        (root / "client" / "index.html").write_text("<p>x</p>")
        (root / "app.json").write_text('{"title": "a"}')
        rels = ["app.json", "client/index.html"]
        first = template_sig.tree_sha(root, rels)
        (root / "app.json").write_text('{"title": "b"}')
        assert template_sig.tree_sha(root, rels) == first
        (root / "client" / "index.html").write_text("<p>y</p>")
        assert template_sig.tree_sha(root, rels) != first

    def test_the_tree_hash_keeps_its_format_and_never_reads_through_a_swap(self, tmp_path, temp_db,
                                                                           monkeypatch):
        # The registry's generator hashes the same bytes the same way, so the
        # hex of a fixed tree is pinned. A file swapped for a link (even one
        # naming the same bytes) or a FIFO after the walk is refused, never
        # read through.
        import os
        from services.apps import releases
        from storage.agents import template_sig
        from storage.agents.community_agent_template_store import (
            TemplateValidationError, load_template_from_dir)
        root = tmp_path / "pinned"
        (root / "client").mkdir(parents=True)
        (root / "server").mkdir()
        (root / "app.json").write_text('{"title": "Pinned"}')
        (root / "blueprint.json").write_text('{"format": 1}')
        (root / "client" / "index.html").write_text("<p>pinned</p>")
        (root / "server" / "index.ts").write_text("export {}\n")
        rels = [rel for rel, _path in releases.walk_tree(root)]
        assert template_sig.tree_sha(root, rels) == \
            "ff0a1d86591ab4399df0479962fba577e57f38ee6f3f2807206d4148948fbcc7"
        twin = tmp_path / "twin.html"
        twin.write_text("<p>pinned</p>")
        page = root / "client" / "index.html"
        page.unlink()
        page.symlink_to(twin)
        with pytest.raises(OSError):
            template_sig.tree_sha(root, rels)
        page.unlink()
        os.mkfifo(page)
        with pytest.raises(OSError):
            template_sig.tree_sha(root, rels)
        # The loader: the walk judged the tree, then the page became a link.
        tdir = _write_template(tmp_path, user_apps={"home": self.HOME},
                               agent_json_extra={"collaborative": True})
        twin.write_text("<p>app</p>")
        real_walk = releases.walk_tree

        def swapping(source):
            files = real_walk(source)
            target = Path(source) / "client" / "index.html"
            if not target.is_symlink():
                target.unlink()
                target.symlink_to(twin)
            return files

        monkeypatch.setattr(releases, "walk_tree", swapping)
        with pytest.raises(TemplateValidationError):
            load_template_from_dir(tdir)

    def test_owner_approval_predicate(self, temp_db):
        from storage.agents.template_sig import needs_owner
        base = {"title": "H", "egress": ["a.example"], "exports": {"methods": {}},
                "bindings": [{"name": "b", "agent": "x", "app": "y"}], "external": {"links": ["z"]},
                "files": {"read": ["workspace/x/"]},
                "actions": [{"id": "s", "type": "platform", "method": "setup.status", "label": "S"},
                            {"id": "f", "type": "data_feed", "feed": "tasks", "label": "F"},
                            {"id": "p", "type": "send_prompt", "prompt": "hi", "label": "P"}]}
        assert needs_owner(base) is False
        for change in (
            {"actions": [{"id": "t", "type": "mcp_tool", "mcp": "m", "tool": "t", "label": "T"}]},
            {"actions": [{"id": "t", "type": "fire_task", "task": "r", "label": "T"}]},
            {"actions": [{"id": "w", "type": "platform", "method": "files.write", "label": "W"}]},
            {"handlers": {"on_schedule": {"x": {"cron": "0 7 * * *"}}}},
            {"steps": {"x": {"run": "x.sh"}}},
            {"inbound": {"x": {"verify": "stripe", "secret": "S", "handler": "h"}}},
            {"secrets": [{"name": "S"}]},
            {"files": {"write": ["workspace/x/"]}},
        ):
            assert needs_owner({**base, **change}) is True, change

    def test_user_app_rules_and_mode(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import (
            TemplateValidationError, load_template_from_dir)
        for slug, manifest, message in (
            ("inb", {**self.HOME, "inbound": {"s": {"verify": "stripe", "secret": "S", "handler": "h"}}},
             "inbound hooks"),
            ("kn", {**self.HOME, "files": {"read": ["knowledge/docs/"]}}, "never knowledge/"),
        ):
            tdir = _write_template(tmp_path, slug=f"tpl-{slug}", user_apps={"home": manifest})
            with pytest.raises(TemplateValidationError, match=message):
                load_template_from_dir(tdir)
        # A per-user app may wake on a trigger since 1.7: its blueprint names
        # the triggers each copy gets, aimed at the manifest's handlers.
        waking = {**self.HOME, "handlers": {"on_trigger": ["ping"]}}
        tdir = _write_template(tmp_path, slug="tpl-wake", user_apps={"home": dict(
            waking, _blueprint={"triggers": [{"slug": "ping", "handler": "ping", "description": "Ping"}]})})
        home = load_template_from_dir(tdir).apps[0]
        assert home.blueprint["triggers"][0]["handler"] == "ping" and home.owner_approval is True
        for slug, blueprint, message in (
            ("nolist", {"triggers": {"slug": "ping"}}, "must be a list"),
            ("badslug", {"triggers": [{"slug": "Ping!", "handler": "ping"}]}, "lowercase letters"),
            ("long", {"triggers": [{"slug": "p" * 62, "handler": "ping"}]}, "at most 64 characters"),
            ("twice", {"triggers": [{"slug": "ping", "handler": "ping"}, {"slug": "ping", "handler": "ping"}]},
             "listed twice"),
            ("nohandler", {"triggers": [{"slug": "ping", "handler": "pong"}]}, "not one of the manifest's"),
            ("many", {"triggers": [{"slug": f"t{i}", "handler": "ping"} for i in range(9)]}, "at most 8"),
        ):
            tdir = _write_template(tmp_path, slug=f"tpl-{slug}",
                                   user_apps={"home": dict(waking, _blueprint=blueprint)})
            with pytest.raises(TemplateValidationError, match=message):
                load_template_from_dir(tdir)
        # A per-user app on a Shared-only template, a shared app on a
        # Personal-only one: the mode rule of the dashboards.
        tdir = _write_template(tmp_path, slug="shared-only", user_apps={"home": self.HOME},
                               agent_json_extra={"collaborative": False, "default_scope": "agent"})
        with pytest.raises(TemplateValidationError, match="not offered"):
            load_template_from_dir(tdir)
        tdir = _write_template(tmp_path, slug="personal-only", apps={"board": {"title": "B"}},
                               agent_json_extra={"collaborative": False, "default_scope": "user"})
        with pytest.raises(TemplateValidationError, match="not offered"):
            load_template_from_dir(tdir)
        # The Personal Assistant's shape: Personal-only with a per-user app.
        tdir = _write_template(tmp_path, slug="pa-shape", user_apps={"home": self.HOME},
                               agent_json_extra={"collaborative": False, "default_scope": "user"})
        assert [a.visibility for a in load_template_from_dir(tdir).apps] == ["user"]

    def test_slug_clash_caps_and_missing_page(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import (
            TemplateValidationError, load_template_from_dir)
        tdir = _write_template(tmp_path, slug="clash", apps={"home": {"title": "A"}},
                               user_apps={"home": self.HOME}, agent_json_extra={"collaborative": True})
        with pytest.raises(TemplateValidationError, match="used by another app"):
            load_template_from_dir(tdir)
        tdir = _write_template(tmp_path, slug="many", agent_json_extra={"collaborative": True},
                               apps={f"a{i}": {"title": "A"} for i in range(3)},
                               user_apps={f"u{i}": self.HOME for i in range(2)})
        with pytest.raises(TemplateValidationError, match="at most 4 apps"):
            load_template_from_dir(tdir)
        tdir = _write_template(tmp_path, slug="nopage", user_apps={"home": self.HOME})
        (tdir / "user-apps/home/client/index.html").unlink()
        with pytest.raises(TemplateValidationError, match="client/index.html"):
            load_template_from_dir(tdir)
        tdir = _write_template(tmp_path, slug="badbp", user_apps={"home": {**self.HOME, "_blueprint": [1]}})
        with pytest.raises(TemplateValidationError, match="blueprint.json must be an object"):
            load_template_from_dir(tdir)

    CHECK = {"name": "lint", "description": "Lint the change", "mandatory": True,
             "applies": ["chats"], "script": {"run": "lint.sh", "timeout": 60},
             "judge": {"rubric": "Is it tidy?"}, "_script": "#!/bin/sh\nexit 0\n"}

    def test_checks_discovered_validated_and_hashed(self, tmp_path, temp_db):
        from services.checks import documents
        from storage.agents.community_agent_template_store import (
            TemplateValidationError, load_template_from_dir)
        tdir = _write_template(tmp_path, checks={"lint": self.CHECK,
                                                 "review": {"name": "review", "judge": {"rubric": "r"}}})
        template = load_template_from_dir(tdir)
        by_name = {c.name: c for c in template.checks}
        assert set(by_name) == {"lint", "review"}
        lint = by_name["lint"]
        assert lint.mandatory is True and lint.script_name == "lint.sh"
        assert lint.script == "#!/bin/sh\nexit 0\n"
        assert lint.doc["applies"] == ["chats"] and lint.doc["rounds"] == documents.DEFAULT_ROUNDS
        assert lint.doc_sha256 == documents.sha256_text(documents.canonical_json(lint.doc))
        assert lint.script_sha256 == documents.sha256_text(b"#!/bin/sh\nexit 0\n")
        assert by_name["review"].mandatory is False and by_name["review"].script_sha256 == ""
        # A bad document fails the template with the validator's words; a
        # name that is not the folder's, a missing script and the cap too.
        for name, doc, message in (
            ("bad", {"name": "bad", "rounds": 9, "judge": {"rubric": "r"}}, "rounds"),
            ("other", {"name": "not-other", "judge": {"rubric": "r"}}, "folder's"),
            ("noscript", {"name": "noscript", "script": {"run": "x.sh"}}, "not in the check's folder"),
        ):
            tdir = _write_template(tmp_path, slug=f"chk-{name}", checks={name: doc})
            with pytest.raises(TemplateValidationError, match=message):
                load_template_from_dir(tdir)
        tdir = _write_template(tmp_path, slug="chk-many",
                               checks={f"c{i}": {"name": f"c{i}", "judge": {"rubric": "r"}} for i in range(9)})
        with pytest.raises(TemplateValidationError, match="at most 8 checks"):
            load_template_from_dir(tdir)

    def test_persisted_record_carries_apps_checks_and_the_baseline(self, tmp_path, temp_db):
        import hashlib
        from storage.agents import template_sig
        from storage.agents.community_agent_template_store import (
            BASELINE_FORMAT, item_projection, load_template_from_dict, load_template_from_dir,
            template_to_persistable_dict)
        tdir = _write_template(
            tmp_path, agent_json_extra={"collaborative": True},
            apps={"board": {"title": "Board"}}, user_apps={"home": self.HOME},
            checks={"lint": self.CHECK},
            context_files={"notes.md": "See /agents/{agent_slug}/config"},
            user_setup_md="Welcome to {agent_slug}",
            dashboards=[{"slug": "brief", "file": "b.html", "visibility": "user"}],
            dashboard_files={"b.html": "<h1>{agent_slug}</h1>"},
            tasks=[{"slug": "daily", "description": "D", "scope": "user", "prompt": "do",
                    "schedule": {"type": "cron", "cron": "0 9 * * *"}, "default_state": "active"}],
        )
        template = load_template_from_dir(tdir)
        data = template_to_persistable_dict(template, agent_slug="my-agent")
        assert data["baseline_format"] == BASELINE_FORMAT
        assert [a["slug"] for a in data["apps"]] == ["board"]
        assert data["user_apps"][0]["slug"] == "home" and data["user_apps"][0]["owner_approval"] is False
        assert data["user_apps"][0]["sig"] == template.apps[1].sig if template.apps[1].slug == "home" \
            else data["user_apps"][0]["sig"] == template.apps[0].sig
        assert "owner_approval" not in data["apps"][0]
        assert data["checks"] == [{"name": "lint", "doc": template.checks[0].doc,
                                   "doc_sha256": template.checks[0].doc_sha256,
                                   "script_sha256": template.checks[0].script_sha256, "mandatory": True,
                                   "sig": template.checks[0].sig}]
        assert template.checks[0].sig == template_sig.check_sig(
            {k: v for k, v in self.CHECK.items() if k != "_script"}, template.checks[0].script_sha256)
        assert data["consent"] == {"apps": {}, "checks": {}}
        sha = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
        base = data["baseline"]
        assert base["persona"] == sha("# Test Prompt\n")
        assert base["context"] == {"context/notes.md": sha("See /agents/my-agent/config")}
        assert base["setup"] is None and base["user_setup"] == sha("Welcome to my-agent")
        assert base["dashboards"] == {"brief": sha("<h1>my-agent</h1>")}
        assert base["items"] == {"task:daily": sha(template_sig.canonical(
            item_projection("task", {"prompt": "do", "schedule_kind": "cron", "cron": "0 9 * * *",
                                     "interval_seconds": None, "run_at": None, "description": "D"})))}
        # Without a slug there is no baseline (nothing was substituted yet).
        assert "baseline" not in template_to_persistable_dict(template)
        # The record round-trips into items the late-joiner path can use.
        back = load_template_from_dict(json.loads(json.dumps(data)))
        apps = {a.slug: a for a in back.apps}
        assert apps["home"].visibility == "user" and apps["home"].dir is None
        assert apps["home"].sig == data["user_apps"][0]["sig"]
        assert apps["board"].visibility == "agent"
        assert back.checks[0].name == "lint" and back.checks[0].mandatory is True
        assert back.checks[0].doc_sha256 == template.checks[0].doc_sha256
        # A record written before this format has none of it and still loads.
        old = load_template_from_dict({"slug": "x", "version": "1", "tasks": [], "triggers": [],
                                       "notifications": [], "dashboards": []})
        assert old.apps == [] and old.checks == []


class TestTemplateAppSeeding:
    """Template apps and checks at install and on attach (COMMUNITY-AGENTS-
    REGISTRY.md "Consent", "Per-user template apps"): the cold deploy, the
    installer's consent and what it may not cover, late joiners, the
    re-check, the member's own app, the opt-out, removal and re-attach."""

    HOME = {"title": "Home", "egress": ["api.open-meteo.com"],
            "actions": [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]}
    BOARD = {"title": "Board",
             "actions": [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]}
    LINT = {"name": "lint", "mandatory": True, "judge": {"rubric": "Is it tidy?"}}

    @pytest.fixture(autouse=True)
    def _clean(self):
        from services.apps import app_supervisor
        from services.community import template_app_seeder
        yield
        template_app_seeder._locks.clear()
        template_app_seeder._healed.clear()
        template_app_seeder._failure_told.clear()
        app_supervisor._instances.clear()
        from api.apps import apps as apps_api
        apps_api._deploy_locks.clear()

    def _install(self, tmp_path, *, apps=None, user_apps=None, checks=None, consent=True,
                 slug="apptpl", target="app-agent", collaborative=True):
        from storage.agents.community_agent_template_store import load_template_from_dir
        from services.community.community_agent_installer import install_from_extracted_template
        _make_user(ADMIN_SUB, "admin@test.com", "admin")
        tdir = _write_template(tmp_path, slug=slug, apps=apps, user_apps=user_apps, checks=checks,
                               agent_json_extra={"collaborative": collaborative})
        template = load_template_from_dir(tdir)
        app_consent = {a.slug: a.sig for a in template.apps} if consent else None
        check_consent = {c.name: c.sig for c in template.checks} if consent else None
        with patch("services.community.community_agents_catalog.fetch_registry",
                   new=AsyncMock(return_value={"mcps": []})), \
                patch("services.mcp.mcp_registry.get_all_manifests", return_value={}), \
                patch("services.notifications.notification_manager.fire_notification",
                      new=AsyncMock()):
            return asyncio.run(install_from_extracted_template(
                template=template, target_slug=target, installer_user_sub=ADMIN_SUB,
                installer_role="admin", source_label="test",
                app_consent=app_consent, check_consent=check_consent,
                consent_by=ADMIN_SUB if consent else ""))

    @staticmethod
    def _seed(agent, sub, slug):
        from services.community import template_app_seeder
        with patch("services.notifications.notification_manager.fire_notification",
                   new=AsyncMock()):
            return asyncio.run(template_app_seeder.seed_user_copy(agent, sub, slug))

    def test_consented_install_seeds_cold_and_approved(self, tmp_path, temp_db):
        import config as app_config
        from services.apps import app_supervisor
        from services.checks import documents
        from storage import database as db
        result = self._install(tmp_path, apps={"board": self.BOARD},
                               user_apps={"home": self.HOME}, checks={"lint": self.LINT})
        assert result["seeded_apps"]["seeded"] == ["board"]
        assert result["seeded_apps"]["user"] == ["home"]
        assert result["seeded_apps"]["pending"] == [] and result["seeded_apps"]["failed"] == []
        assert result["seeded_checks"] == {"consented": ["lint"], "offered": [], "failed": []}
        assert result["consent_ignored"] == []
        shared = db.get_app_by_slug("app-agent", "", "board")
        admin = db.get_username_by_sub(ADMIN_SUB)
        own = db.get_app_by_slug("app-agent", admin, "home")
        for row, ref in ((shared, "apptpl:board"), (own, "apptpl:home")):
            assert row["kind"] == "folder" and row["deploy_state"] == "idle"
            assert row["release_path"] and row["pending_release"] == 0
            assert db.app_actions_approved(row) and row["approved_by"] == ADMIN_SUB
            assert row["template_ref"] == ref and not row["hidden"]
        # Cold: no server was started; the seed source is on disk for later
        # members; the check landed mandatory with its provenance.
        assert app_supervisor._instances == {}
        agent_dir = app_config.get_agent_dir("app-agent")
        assert (agent_dir / "config/community/user-apps/home/client/index.html").is_file()
        assert (agent_dir / f"users/{admin}/workspace/apps/home/app.json").is_file()
        checks = {c.name: c for c in documents.load_checks("app-agent")}
        assert checks["lint"].mandatory is True
        assert checks["lint"].updated_by == "template:apptpl@1.0.0"
        assert (agent_dir / "config/checks/lint/check.json").is_file()

    def test_without_consent_everything_waits(self, tmp_path, temp_db):
        from services.checks import documents
        from storage import database as db
        result = self._install(tmp_path, apps={"board": self.BOARD},
                               user_apps={"home": self.HOME}, checks={"lint": self.LINT},
                               consent=False)
        assert result["seeded_apps"]["seeded"] == [] and result["seeded_apps"]["user"] == []
        assert [p["slug"] for p in result["seeded_apps"]["pending"]] == ["board", "home"]
        assert all("no consent" in p["reason"] for p in result["seeded_apps"]["pending"])
        assert result["seeded_checks"]["offered"] == ["lint"]
        admin = db.get_username_by_sub(ADMIN_SUB)
        for row in (db.get_app_by_slug("app-agent", "", "board"),
                    db.get_app_by_slug("app-agent", admin, "home")):
            assert row["deploy_state"] == "pending" and row["pending_release"] == 1
            assert not db.app_actions_approved(row) and row["template_ref"]
        assert documents.load_checks("app-agent")[0].mandatory is False

    def test_a_wrong_signature_is_ignored_not_approved(self, tmp_path, temp_db):
        from storage import database as db
        from storage.agents.community_agent_template_store import load_template_from_dir
        from services.community.community_agent_installer import install_from_extracted_template
        _make_user(ADMIN_SUB, "admin@test.com", "admin")
        tdir = _write_template(tmp_path, slug="sigtpl", user_apps={"home": self.HOME},
                               agent_json_extra={"collaborative": False, "default_scope": "user"})
        template = load_template_from_dir(tdir)
        with patch("services.community.community_agents_catalog.fetch_registry",
                   new=AsyncMock(return_value={"mcps": []})), \
                patch("services.mcp.mcp_registry.get_all_manifests", return_value={}), \
                patch("services.notifications.notification_manager.fire_notification",
                      new=AsyncMock()):
            result = asyncio.run(install_from_extracted_template(
                template=template, target_slug="sig-agent", installer_user_sub=ADMIN_SUB,
                installer_role="admin", source_label="test",
                app_consent={"home": "stale"}, consent_by=ADMIN_SUB))
        assert result["consent_ignored"] == ["app:home"]
        row = db.get_app_by_slug("sig-agent", db.get_username_by_sub(ADMIN_SUB), "home")
        assert row["deploy_state"] == "pending" and not db.app_actions_approved(row)

    def test_an_edited_stored_copy_is_not_approved_by_the_consent(self, tmp_path, temp_db):
        # The per-user seed source lives under /config, which a manager's
        # session writes: an edit after the install (here an owner-only file
        # write and a new egress host) must wait on the late joiner's card,
        # never ride the installer's consent.
        import json as _json
        import config as app_config
        from services.community import template_app_seeder
        from storage import database as db
        self._install(tmp_path, user_apps={"home": self.HOME})
        src = app_config.get_agent_dir("app-agent") / "config/community/user-apps/home/app.json"
        doc = _json.loads(src.read_text())
        doc["egress"] = ["api.open-meteo.com", "evil.example.com"]
        doc["files"] = {"read": ["workspace/"], "write": ["workspace/"]}
        src.write_text(_json.dumps(doc))
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "app-agent", "viewer", "test")
        status, detail = self._seed("app-agent", "bob-sub", "home")
        row = db.get_app_by_slug("app-agent", db.get_username_by_sub("bob-sub"), "home")
        assert status == "pending" and detail == template_app_seeder.COPY_CHANGED
        assert not db.app_actions_approved(row) and not row.get("approved_by")

    def test_a_copy_that_keeps_failing_is_told_once(self, tmp_path, temp_db):
        # The heal retries a missing copy every minute: its failure reaches
        # the member and the managers once, not on every retry.
        from services.community import template_app_seeder
        from storage import database as db
        self._install(tmp_path, user_apps={"home": self.HOME})
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "app-agent", "viewer", "test")
        notes = AsyncMock()
        with patch("services.apps.app_blueprints.import_folder",
                   new=AsyncMock(side_effect=RuntimeError("MCP x not available"))), \
                patch("services.notifications.notification_manager.fire_notification", new=notes):
            for _ in range(3):
                status, _detail = asyncio.run(template_app_seeder.seed_user_copy("app-agent", "bob-sub", "home"))
                assert status == "failed"
        told = {c.kwargs["target"] for c in notes.call_args_list}
        assert "bob-sub" in told and notes.call_count == len(told)

    def test_owner_only_copies_wait_for_their_owner(self, tmp_path, temp_db):
        from storage import database as db
        manifest = {"title": "Tasks", "actions": [
            {"id": "go", "label": "Go", "type": "fire_task", "task": "report"}],
            "_blueprint": {"format": 1, "tasks": [{"slug": "report", "description": "R", "prompt": "do"}]}}
        result = self._install(tmp_path, user_apps={"home": manifest})
        # The installer IS the owner of their own copy: approved.
        assert result["seeded_apps"]["user"] == ["home"]
        admin = db.get_username_by_sub(ADMIN_SUB)
        assert db.app_actions_approved(db.get_app_by_slug("app-agent", admin, "home"))
        # A later member's copy waits on that member's card.
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "app-agent", "viewer", "test")
        assert self._seed("app-agent", "bob-sub", "home") == ("pending", "the owner approves this app on their own card")
        row = db.get_app_by_slug("app-agent", db.get_username_by_sub("bob-sub"), "home")
        assert row["deploy_state"] == "pending" and row["template_ref"] == "apptpl:home"
        dyn = db.find_template_task("app-agent", "home__report", "bob-sub")
        assert dyn and dyn["scope"] == "user" and dyn["created_by"] == "bob-sub"

    def test_late_joiner_seeded_from_the_persisted_consent(self, tmp_path, temp_db):
        from services.community.community_agent_installer import on_user_added_to_agent
        from storage import database as db
        self._install(tmp_path, user_apps={"home": self.HOME})
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "app-agent", "viewer", "test")
        # The hook only queues (no worker runs in a test); the seed itself:
        counts = on_user_added_to_agent("app-agent", "bob-sub", "viewer")
        assert counts["apps"] == 0
        status, detail = self._seed("app-agent", "bob-sub", "home")
        assert status == "seeded", detail
        bob = db.get_username_by_sub("bob-sub")
        row = db.get_app_by_slug("app-agent", bob, "home")
        assert row["owner_sub"] == "bob-sub" and db.app_actions_approved(row)
        assert row["approved_by"] == ADMIN_SUB and row["release_path"]
        assert self._seed("app-agent", "bob-sub", "home") == ("exists", "already seeded")
        # The consent is re-checked: a demoted installer approves no new copy.
        _make_user("carol-sub", "carol@test.com")
        db.add_user_agent("carol-sub", "app-agent", "viewer", "test")
        db.upsert_user(ADMIN_SUB, "admin@test.com", "admin", "member")
        status, detail = self._seed("app-agent", "carol-sub", "home")
        assert status == "pending" and "may no longer approve" in detail
        # Bob's copy is untouched by that.
        assert db.app_actions_approved(db.get_app_by_slug("app-agent", bob, "home"))

    def test_own_app_opt_out_removal_and_reattach(self, tmp_path, temp_db):
        import config as app_config
        from services.apps import app_lifecycle
        from services.community import template_app_seeder
        from storage import database as db
        self._install(tmp_path, user_apps={"home": self.HOME})
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "app-agent", "viewer", "test")
        bob = db.get_username_by_sub("bob-sub")
        agent_dir = app_config.get_agent_dir("app-agent")
        # A folder of the member's own under that name is never touched.
        own = agent_dir / f"users/{bob}/workspace/apps/home"
        own.mkdir(parents=True)
        assert self._seed("app-agent", "bob-sub", "home")[0] == "kept"
        own.rmdir()
        assert self._seed("app-agent", "bob-sub", "home")[0] == "seeded"
        row = db.get_app_by_slug("app-agent", bob, "home")
        # A purge keeps the row hidden as the opt-out; no seed recreates it.
        out = asyncio.run(app_lifecycle.purge(row))
        assert out["opted_out"] is True
        parked = db.get_app_by_slug("app-agent", bob, "home")
        assert parked["hidden"] and parked["template_state"] == "opted_out"
        assert parked["release_path"] == "" and not own.exists()
        assert self._seed("app-agent", "bob-sub", "home") == ("exists", "opted out")
        assert template_app_seeder.heal_missing("app-agent", "bob-sub", bob) == 0
        # The restore brings it back from the template's copy, approved.
        with patch("services.notifications.notification_manager.fire_notification",
                   new=AsyncMock()):
            res = asyncio.run(template_app_seeder.restore(parked))
        assert res["status"] == "ok" and res["restored"]
        back = db.get_app_by_slug("app-agent", bob, "home")
        assert not back["hidden"] and back["template_state"] == "" and db.app_actions_approved(back)
        # A membership removal parks it; a re-attach restores it.
        assert asyncio.run(template_app_seeder.on_user_removed("app-agent", "bob-sub")) == 1
        gone = db.get_app_by_slug("app-agent", bob, "home")
        assert gone["hidden"] and gone["template_state"] == "removed"
        assert self._seed("app-agent", "bob-sub", "home") == ("restored", "back after a membership removal")
        assert not db.get_app_by_slug("app-agent", bob, "home")["hidden"]

    def test_blueprint_triggers_seed_one_per_copy(self, tmp_path, temp_db):
        """A template app's blueprint triggers (APPS.md "Blueprints and
        templates"): one trigger per copy owned by its member (the agent's
        for a shared app), aimed at the copy, waiting with a pending copy;
        a slug the member already uses skips the trigger and tells them; a
        membership removal pauses it and the re-attach resumes it, a pause
        of the member's own stays; a purge detaches it and the restore
        re-attaches it."""
        from api.events.triggers import trigger_webhook_path
        from services.apps import app_handlers, app_lifecycle
        from services.community import template_app_seeder
        from services.scheduler import trigger_manager
        from storage import database as db
        from storage.automation import trigger_store
        wake = {"handlers": {"on_trigger": ["ping"]},
                "_blueprint": {"triggers": [{"slug": "ping", "handler": "ping", "description": "Ping"}]}}
        self._install(tmp_path, apps={"board": {**self.BOARD, **wake}}, user_apps={"home": {**self.HOME, **wake}})
        admin = db.get_username_by_sub(ADMIN_SUB)
        own = db.get_app_by_slug("app-agent", admin, "home")
        shared = db.get_app_by_slug("app-agent", "", "board")
        # The installer's own copy is theirs to approve: live, its trigger
        # aimed at it; the shared app's trigger is the agent's.
        assert db.app_actions_approved(own)
        mine = trigger_store.find_template_trigger("app-agent", "home__ping", ADMIN_SUB)
        assert mine and mine["scope"] == "user" and mine["created_by"] == ADMIN_SUB
        assert mine["app_id"] == own["id"] and mine["handler"] == "ping" and mine["enabled"]
        assert mine["slug"] == "home-ping" and mine["name"] == "Ping"
        assert mine["community_template"] == "apptpl" and mine["community_template_item_slug"] == "home__ping"
        assert trigger_webhook_path(mine) == f"/v1/webhooks/user/{admin}/home-ping"
        theirs = trigger_store.find_template_trigger("app-agent", "board__ping")
        assert theirs and theirs["scope"] == "agent" and theirs["app_id"] == shared["id"]
        assert trigger_webhook_path(theirs) == "/v1/webhooks/agent/app-agent/board-ping"
        # A member's copy carries a handler, so it waits for their approval;
        # its trigger exists from the seed and a fire waits with it.
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "app-agent", "viewer", "test")
        bob = db.get_username_by_sub("bob-sub")
        assert self._seed("app-agent", "bob-sub", "home")[0] == "pending"
        row = db.get_app_by_slug("app-agent", bob, "home")
        bobs = trigger_store.find_template_trigger("app-agent", "home__ping", "bob-sub")
        assert bobs and bobs["created_by"] == "bob-sub" and bobs["app_id"] == row["id"]
        assert app_handlers._precheck(row, {"handler": "ping", "trigger_id": bobs["id"]}) == "unapproved"
        assert self._seed("app-agent", "bob-sub", "home")[0] == "exists"
        assert trigger_store.find_template_trigger("app-agent", "home__ping", "bob-sub")["id"] == bobs["id"]
        # A slug the member already uses: the copy lands, the trigger is
        # skipped and the member is told which.
        _make_user("carol-sub", "carol@test.com")
        db.add_user_agent("carol-sub", "app-agent", "viewer", "test")
        trigger_manager.register_trigger(name="Mine", scope="user", agent="app-agent", created_by="carol-sub",
                                         slug="home-ping", notify_enabled=True, notify_title="t", notify_body="b")
        fired = AsyncMock()
        with patch("services.notifications.notification_manager.fire_notification", new=fired):
            assert asyncio.run(template_app_seeder.seed_user_copy("app-agent", "carol-sub", "home"))[0] == "pending"
        assert trigger_store.find_template_trigger("app-agent", "home__ping", "carol-sub") is None
        titles = [c.args[0] for c in fired.call_args_list]
        assert any("a trigger was not created" in t for t in titles), titles
        # Removal pauses the seeded trigger with the mark; a pause of the
        # member's own is not touched; the re-attach resumes the marked one.
        assert asyncio.run(template_app_seeder.on_user_removed("app-agent", "bob-sub")) == 1
        paused = trigger_store.get_trigger(bobs["id"])
        assert not paused["enabled"] and paused["last_error"] == template_app_seeder.REMOVED_MARK
        assert self._seed("app-agent", "bob-sub", "home")[0] == "restored"
        back = trigger_store.get_trigger(bobs["id"])
        assert back["enabled"] and back["last_error"] == ""
        trigger_store.set_trigger_enabled(bobs["id"], False)
        asyncio.run(template_app_seeder.on_user_removed("app-agent", "bob-sub"))
        assert self._seed("app-agent", "bob-sub", "home")[0] == "restored"
        assert not trigger_store.get_trigger(bobs["id"])["enabled"]
        # A purge detaches the trigger; the restore re-attaches and enables it.
        out = asyncio.run(app_lifecycle.purge(db.get_app_by_slug("app-agent", bob, "home")))
        assert out["opted_out"] is True
        gone = trigger_store.get_trigger(bobs["id"])
        assert gone["app_id"] is None and not gone["enabled"]
        with patch("services.notifications.notification_manager.fire_notification", new=AsyncMock()):
            res = asyncio.run(template_app_seeder.restore(db.get_app_by_slug("app-agent", bob, "home")))
        assert res["triggers"] == {"ping": "reattached"}, res
        again = trigger_store.get_trigger(bobs["id"])
        assert again["app_id"] == db.get_app_by_slug("app-agent", bob, "home")["id"] and again["enabled"]
        assert again["last_error"] == ""

    def test_a_mode_without_personal_apps_seeds_nothing(self, tmp_path, temp_db):
        from storage import database as db
        from storage.pg import get_conn
        self._install(tmp_path, user_apps={"home": self.HOME})
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "app-agent", "viewer", "test")
        from storage.agents import agent_store
        with get_conn() as conn:
            conn.execute("UPDATE agents SET collaborative=FALSE, default_scope='agent' WHERE slug=%s",
                         ("app-agent",))
            conn.commit()
        agent_store._invalidate_cache()
        assert self._seed("app-agent", "bob-sub", "home") == ("skipped", "the agent's mode offers no personal apps")

    def test_the_wizard_consents_to_the_default_template(self, tmp_path, temp_db):
        from services.community.community_agent_installer import install_from_catalog
        from storage import database as db
        _make_user(ADMIN_SUB, "admin@test.com", "admin")
        tdir = _write_template(tmp_path, slug="personal-assistant", user_apps={"home": self.HOME},
                               agent_json_extra={"collaborative": False, "default_scope": "user"})
        with patch("services.community.community_agents_catalog.fetch_registry",
                   new=AsyncMock(return_value={"mcps": [], "agents": []})), \
                patch("services.community.community_agents_catalog.fetch_and_extract_template",
                      new=AsyncMock(return_value=tdir)), \
                patch("services.mcp.mcp_registry.get_all_manifests", return_value={}), \
                patch("services.notifications.notification_manager.fire_notification",
                      new=AsyncMock()), \
                patch("shutil.rmtree"):
            result = asyncio.run(install_from_catalog(
                template_slug="personal-assistant", target_slug="pa", installer_user_sub=ADMIN_SUB,
                installer_role="admin", consent_all=True))
        assert result["seeded_apps"]["user"] == ["home"]
        row = db.get_app_by_slug("pa", db.get_username_by_sub(ADMIN_SUB), "home")
        assert db.app_actions_approved(row) and row["approved_by"] == ADMIN_SUB


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_unknown_mcp_blocks_install(self, tmp_path, temp_db):
        from fastapi import HTTPException
        tdir = _write_template(tmp_path, mcps=[{"name": "nonexistent-mcp"}])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ):
            with pytest.raises(HTTPException) as exc:
                _install_admin(tdir)
        assert exc.value.status_code == 400
        assert "missing_mcps" in str(exc.value.detail)

    def test_mcp_in_catalog_passes_preflight(self, tmp_path, temp_db):
        from services.community.community_agent_installer import _preflight_check_mcps
        from storage.agents.community_agent_template_store import McpRequirement

        with patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [{"name": "future-mcp"}]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ):
            # Should not raise.
            asyncio.run(_preflight_check_mcps([McpRequirement(name="future-mcp")]))


# ---------------------------------------------------------------------------
# Slug collision
# ---------------------------------------------------------------------------

class TestSlugCollision:
    def test_propose_free_slug_appends_2(self, temp_db):
        from services.community.community_agent_installer import _propose_free_slug
        from storage.agents import agent_store
        agent_store.create_agent("foo", "Foo")
        assert _propose_free_slug("foo") == "foo-2"

    def test_propose_free_slug_skips_existing_suffix(self, temp_db):
        from services.community.community_agent_installer import _propose_free_slug
        from storage.agents import agent_store
        agent_store.create_agent("foo", "Foo")
        agent_store.create_agent("foo-2", "Foo Two")
        assert _propose_free_slug("foo") == "foo-3"

    def test_install_collision_returns_409_with_suggestion(self, tmp_path, temp_db):
        from fastapi import HTTPException
        from storage.agents import agent_store
        agent_store.create_agent("demo-agent", "Existing")
        tdir = _write_template(tmp_path)
        with pytest.raises(HTTPException) as exc:
            _install_admin(tdir, target_slug="demo-agent")
        assert exc.value.status_code == 409
        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "slug_taken"
        assert detail["suggested_slug"] == "demo-agent-2"


# ---------------------------------------------------------------------------
# Admin cascade — all MCPs resolved inline
# ---------------------------------------------------------------------------


class _StubAutoManifest:
    """Stand-in for a parsed MCP manifest with auto assignment_mode."""

    def __init__(self, name: str, skills: list | None = None):
        self.name = name
        self.assignment_mode = "auto"
        self.category = "community"
        self.skills = skills or []
        self.exclude_from: list[str] = []


class TestAdminCascade:
    def test_admin_install_with_only_auto_installed_mcps(self, tmp_path, temp_db):
        """Auto-mode MCP already installed → just enable, no requests created."""
        from storage.mcp import mcp_store

        tdir = _write_template(tmp_path, mcps=[{"name": "auto-mcp"}])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={"auto-mcp": _StubAutoManifest("auto-mcp")},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            side_effect=lambda n: _StubAutoManifest("auto-mcp") if n == "auto-mcp" else None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(tdir)

        assert result["agent_slug"] == "demo-agent"
        assert result["batch_id"] is None
        assert result["created_requests"] == []
        assert result["ready_mcps"] == ["auto-mcp"]
        assert "auto-mcp" in mcp_store.get_manager_enabled_mcps("demo-agent")


# ---------------------------------------------------------------------------
# Manager cascade — batch_id generated, requests pending admin
# ---------------------------------------------------------------------------


class TestManagerCascade:
    def test_manager_install_with_missing_mcp_creates_request(self, tmp_path, temp_db):
        from storage.mcp import mcp_request_store

        tdir = _write_template(tmp_path, mcps=[{"name": "missing-mcp"}])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [{"name": "missing-mcp"}]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(
                tdir, installer_sub=MANAGER_SUB, installer_role="manager",
            )

        assert result["batch_id"] is not None
        assert len(result["created_requests"]) == 1
        req = result["created_requests"][0]
        assert req["mcp_name"] == "missing-mcp"
        assert req["status"] == "pending"
        assert req["batch_id"] == result["batch_id"]
        # Confirm it's persisted.
        rows = mcp_request_store.list_requests_by_batch(result["batch_id"])
        assert len(rows) == 1

    def test_manager_install_two_missing_mcps_share_one_batch(self, tmp_path, temp_db):
        from storage.mcp import mcp_request_store

        tdir = _write_template(tmp_path, mcps=[
            {"name": "first-mcp"}, {"name": "second-mcp"},
        ])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [
                {"name": "first-mcp"}, {"name": "second-mcp"},
            ]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(
                tdir, installer_sub=MANAGER_SUB, installer_role="manager",
            )

        assert result["batch_id"] is not None
        rows = mcp_request_store.list_requests_by_batch(result["batch_id"])
        assert {r["mcp_name"] for r in rows} == {"first-mcp", "second-mcp"}


# ---------------------------------------------------------------------------
# Template-item seeding
# ---------------------------------------------------------------------------

class TestSeeding:
    def test_seeds_agent_scope_task_from_template(self, tmp_path, temp_db):
        from storage import database as db

        tdir = _write_template(tmp_path, tasks=[{
            "slug": "daily-check", "description": "Daily check",
            "scope": "agent", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 8 * * *"},
            "default_state": "paused",
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir)

        tasks = db.list_dynamic_tasks(agent="demo-agent")
        assert len(tasks) == 1
        assert tasks[0]["community_template"] == "demo-template"
        assert tasks[0]["community_template_item_slug"] == "daily-check"
        assert tasks[0]["scope"] == "agent"
        # default_state=paused → enabled=False
        assert tasks[0]["enabled"] is False
        # An agent-scope task follows the platform clock, never the
        # installer's zone (a literal would stop following the setting).
        assert tasks[0]["user_tz"] is None

    def test_seeds_user_scope_task_for_installer(self, tmp_path, temp_db):
        from storage import database as db

        tdir = _write_template(tmp_path, tasks=[{
            "slug": "user-task", "description": "Per-user",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 10 * * *"},
            "default_state": "active",
            "auto_create_for_new_users": True,
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir)

        tasks = db.list_dynamic_tasks(agent="demo-agent")
        assert len(tasks) == 1
        assert tasks[0]["scope"] == "user"
        assert tasks[0]["created_by"] == ADMIN_SUB
        assert tasks[0]["enabled"] is True
        # No dashboard has reported the installer's zone: platform clock.
        assert tasks[0]["user_tz"] is None

    def test_seeds_user_scope_task_in_the_installers_zone(self, tmp_path, temp_db):
        """A user-scope task is the installer's own: it takes the zone their
        dashboard reported, like a task they would create themselves."""
        from core.session import session_state
        from storage import database as db

        tdir = _write_template(tmp_path, tasks=[{
            "slug": "user-task", "description": "Per-user",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 10 * * *"},
            "default_state": "active",
            "auto_create_for_new_users": True,
        }])
        session_state.set_user_tz(ADMIN_SUB, "Europe/Athens")
        try:
            with patch(
                "services.community.community_agents_catalog.fetch_registry",
                new=AsyncMock(return_value={"mcps": []}),
            ), patch(
                "services.mcp.mcp_registry.get_all_manifests",
                return_value={},
            ), patch(
                "services.notifications.notification_manager.fire_notification",
                new=AsyncMock(),
            ):
                _install_admin(tdir)
        finally:
            session_state._user_tz.pop(ADMIN_SUB, None)

        tasks = db.list_dynamic_tasks(agent="demo-agent")
        assert len(tasks) == 1 and tasks[0]["scope"] == "user"
        assert tasks[0]["user_tz"] == "Europe/Athens"

    def test_seeds_agent_scope_trigger_task_on_the_platform_clock(self, tmp_path, temp_db):
        """The paired task of an agent-scope trigger is seeded with the
        installer's sub as created_by — the zone rule keys on the item's
        scope, so it still lands NULL."""
        from core.session import session_state
        from storage import database as db

        tdir = _write_template(tmp_path, triggers=[{
            "slug": "on-push", "description": "On push",
            "scope": "agent", "prompt": "echo trig",
            "default_state": "active",
        }])
        session_state.set_user_tz(ADMIN_SUB, "Europe/Athens")
        try:
            with patch(
                "services.community.community_agents_catalog.fetch_registry",
                new=AsyncMock(return_value={"mcps": []}),
            ), patch(
                "services.mcp.mcp_registry.get_all_manifests",
                return_value={},
            ), patch(
                "services.notifications.notification_manager.fire_notification",
                new=AsyncMock(),
            ):
                _install_admin(tdir)
        finally:
            session_state._user_tz.pop(ADMIN_SUB, None)

        task = db.find_template_task("demo-agent", "on-push__task")
        assert task and task["task_type"] == "trigger"
        assert task["created_by"] == ADMIN_SUB and task["scope"] == "agent"
        assert task["user_tz"] is None


# ---------------------------------------------------------------------------
# Notification batching
# ---------------------------------------------------------------------------

class TestNotificationBatching:
    def test_batch_create_fires_one_notification_per_admin(self, tmp_path, temp_db):
        """Two missing MCPs in one cascade → one notification per admin, not
        two (per-request notifications are suppressed for batched rows)."""

        fire_mock = AsyncMock()
        tdir = _write_template(tmp_path, mcps=[
            {"name": "missing-a"}, {"name": "missing-b"},
        ])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [
                {"name": "missing-a"}, {"name": "missing-b"},
            ]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=fire_mock,
        ):
            _install_admin(
                tdir, installer_sub=MANAGER_SUB, installer_role="manager",
            )

        # Only one admin in the seed → exactly one notification, with both
        # MCPs in the body. Setup notification fires if setup.md present;
        # template has no setup.md, so this is the only call.
        admin_notifs = [
            call for call in fire_mock.call_args_list
            if call.kwargs.get("target") == ADMIN_SUB
        ]
        assert len(admin_notifs) == 1
        body = admin_notifs[0].kwargs["body"]
        assert "missing-a" in body
        assert "missing-b" in body


# ---------------------------------------------------------------------------
# Cascade cleanup invariants under template-seeded items
# ---------------------------------------------------------------------------

class TestSeededCleanupInvariants:
    def test_delete_agent_removes_seeded_items(self, tmp_path, temp_db):
        from storage.agents import agent_store
        from storage import database as db

        tdir = _write_template(tmp_path, tasks=[{
            "slug": "smoke-task", "description": "x", "scope": "agent",
            "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 8 * * *"},
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir, target_slug="doomed-agent")

        assert len(db.list_dynamic_tasks(agent="doomed-agent")) == 1
        agent_store.delete_agent("doomed-agent")
        assert db.list_dynamic_tasks(agent="doomed-agent") == []


# ---------------------------------------------------------------------------
# On_user_added_to_agent hook (catalog-aware seeding)
# ---------------------------------------------------------------------------

def _install_with_user_items(tmp_path, *, default_for_new_users=None, agent_json_extra=None):
    """Helper: install a template that declares ALL three user-scope items.

    Returns the installed agent_slug. ADMIN_SUB is the installer/manager.
    """
    agent_json_extras = dict(agent_json_extra or {})
    if default_for_new_users is not None:
        agent_json_extras["default_for_new_users"] = default_for_new_users

    tdir = _write_template(
        tmp_path,
        slug="hooktpl",
        tasks=[{
            "slug": "user-task", "description": "Per-user task",
            "scope": "user", "prompt": "echo task",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
            "auto_create_for_new_users": True,
        }],
        triggers=[{
            "slug": "user-trig", "description": "Per-user trigger",
            "scope": "user", "prompt": "echo trig",
            "default_state": "paused",
            "auto_create_for_new_users": True,
        }],
        notifications=[{
            "slug": "user-notif", "title": "Per-user notification",
            "body": "body", "scope": "user",
            "schedule": {"type": "cron", "cron": "0 12 * * *"},
            "default_state": "active",
            "auto_create_for_new_users": True,
        }],
    )
    # Patch the agent.json with optional default_for_new_users block.
    if agent_json_extras:
        agent_json_path = tdir / "agent.json"
        existing = json.loads(agent_json_path.read_text())
        existing.update(agent_json_extras)
        agent_json_path.write_text(json.dumps(existing))

    with patch(
        "services.community.community_agents_catalog.fetch_registry",
        new=AsyncMock(return_value={"mcps": []}),
    ), patch(
        "services.mcp.mcp_registry.get_all_manifests",
        return_value={},
    ), patch(
        "services.notifications.notification_manager.fire_notification",
        new=AsyncMock(),
    ):
        _install_admin(tdir, target_slug="hook-agent")
    return "hook-agent"


def _make_user(sub: str, email: str, role: str = "member") -> None:
    """Insert a minimal users row directly so add_user_agent's FK clears."""
    from storage import database as db
    # Use upsert_user — covers both fresh creation and re-runs.
    db.upsert_user(sub, email, email.split("@")[0], role)


class TestUserJoinHook:
    """``on_user_added_to_agent`` re-seeds per-user template items
    when a user is attached to a community-template agent after install."""

    def test_persists_template_data_at_install_time(self, tmp_path, temp_db):
        from storage.agents import agent_store
        agent_slug = _install_with_user_items(tmp_path)
        data = agent_store.get_community_template_data(agent_slug)
        assert data is not None
        assert data["slug"] == "hooktpl"
        assert len(data["tasks"]) == 1
        assert len(data["triggers"]) == 1
        assert len(data["notifications"]) == 1

    def test_hook_seeds_items_for_late_joiner(self, tmp_path, temp_db):
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        agent_slug = _install_with_user_items(tmp_path)
        _make_user("user-bob", "bob@example.com")
        db.add_user_agent("user-bob", agent_slug, "viewer", "system")

        counts = on_user_added_to_agent(agent_slug, "user-bob", "viewer")
        assert counts == {"tasks": 1, "triggers": 1, "notifications": 1, "dashboards": 0, "apps": 0, "user_setup": 0}

        # bob owns: the user-task itself + the trigger's paired task
        # (trigger model spawns a dynamic_tasks row with
        # task_type='trigger' alongside the trigger row). Filter to the
        # standalone user-task by its template slug.
        bob_user_tasks = [
            t for t in db.list_dynamic_tasks(agent=agent_slug)
            if t["created_by"] == "user-bob"
            and t["scope"] == "user"
            and t["community_template_item_slug"] == "user-task"
        ]
        assert len(bob_user_tasks) == 1

    def test_hook_is_idempotent(self, tmp_path, temp_db):
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        agent_slug = _install_with_user_items(tmp_path)
        _make_user("user-bob", "bob@example.com")
        db.add_user_agent("user-bob", agent_slug, "viewer", "system")

        first = on_user_added_to_agent(agent_slug, "user-bob", "viewer")
        second = on_user_added_to_agent(agent_slug, "user-bob", "viewer")
        # First call seeds, second call sees the unique-index conflict and
        # returns 0 across the board.
        assert first == {"tasks": 1, "triggers": 1, "notifications": 1, "dashboards": 0, "apps": 0, "user_setup": 0}
        assert second == {"tasks": 0, "triggers": 0, "notifications": 0, "dashboards": 0, "apps": 0, "user_setup": 0}

    def test_hook_respects_role_filter(self, tmp_path, temp_db):
        """Item with ``roles: ["manager"]`` is NOT seeded for a viewer."""
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        tdir = _write_template(tmp_path, slug="rolefilter", tasks=[{
            "slug": "mgr-only", "description": "Manager-only",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
            "auto_create_for_new_users": True,
            "roles": ["manager"],
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir, target_slug="role-agent")

        _make_user("user-viewer", "viewer@example.com")
        db.add_user_agent("user-viewer", "role-agent", "viewer", "system")
        counts = on_user_added_to_agent("role-agent", "user-viewer", "viewer")
        assert counts == {"tasks": 0, "triggers": 0, "notifications": 0, "dashboards": 0, "apps": 0, "user_setup": 0}

        _make_user("user-mgr", "mgr@example.com")
        db.add_user_agent("user-mgr", "role-agent", "manager", "system")
        mgr_counts = on_user_added_to_agent("role-agent", "user-mgr", "manager")
        assert mgr_counts["tasks"] == 1

    def test_hook_noop_for_non_community_agent(self, tmp_path, temp_db):
        """Agents not installed from a template have no template_data; hook
        returns empty counts without raising."""
        from storage.agents import agent_store
        from services.community.community_agent_installer import on_user_added_to_agent

        agent_store.create_agent("native-agent", "Native Agent")
        counts = on_user_added_to_agent("native-agent", "user-anon", "viewer")
        assert counts == {"tasks": 0, "triggers": 0, "notifications": 0, "dashboards": 0, "apps": 0, "user_setup": 0}

    def test_hook_skips_auto_create_for_new_users_false(self, tmp_path, temp_db):
        """Items where ``auto_create_for_new_users=False`` are NOT seeded for
        late joiners (only the installer's items at install time)."""
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        tdir = _write_template(tmp_path, slug="noauto", tasks=[{
            "slug": "no-auto", "description": "Don't auto-create",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
            "auto_create_for_new_users": False,
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir, target_slug="noauto-agent")
        _make_user("user-late", "late@example.com")
        db.add_user_agent("user-late", "noauto-agent", "manager", "system")
        counts = on_user_added_to_agent("noauto-agent", "user-late", "manager")
        assert counts["tasks"] == 0

    def test_install_writes_default_for_new_users_role(self, tmp_path, temp_db):
        from storage.agents import agent_store
        agent_slug = _install_with_user_items(
            tmp_path,
            default_for_new_users={"enabled": True, "role": "viewer"},
        )
        agent = agent_store.get_agent(agent_slug)
        assert agent["default_for_new_users_role"] == "viewer"

    def test_a_shared_only_template_keeps_no_below_editor_default(self, tmp_path, temp_db):
        from storage.agents import agent_store
        agent_slug = _install_with_user_items(
            tmp_path,
            default_for_new_users={"enabled": True, "role": "viewer"},
            agent_json_extra={"collaborative": False, "default_scope": "agent"},
        )
        agent = agent_store.get_agent(agent_slug)
        assert agent["collaborative"] is False and agent["default_scope"] == "agent"
        assert agent["default_for_new_users_role"] == ""

    def test_install_default_for_new_users_disabled_keeps_empty(self, tmp_path, temp_db):
        from storage.agents import agent_store
        agent_slug = _install_with_user_items(
            tmp_path,
            default_for_new_users={"enabled": False},
        )
        agent = agent_store.get_agent(agent_slug)
        assert agent["default_for_new_users_role"] == ""

    def test_invalid_default_role_rejected_at_load(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = _write_template(tmp_path, slug="badrole")
        agent_json_path = tdir / "agent.json"
        existing = json.loads(agent_json_path.read_text())
        existing["default_for_new_users"] = {"enabled": True, "role": "owner"}
        agent_json_path.write_text(json.dumps(existing))
        with pytest.raises(TemplateValidationError, match="default_for_new_users"):
            load_template_from_dir(tdir)

    def test_core_mcps_field_parses_and_validates(self, tmp_path, temp_db):
        """``core_mcps``: absent → "all" (the default install behavior);
        "none" round-trips; anything else is a schema violation."""
        from storage.agents.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = _write_template(tmp_path, slug="coreopt")
        assert load_template_from_dir(tdir).core_mcps == "all"

        agent_json_path = tdir / "agent.json"
        existing = json.loads(agent_json_path.read_text())
        existing["core_mcps"] = "none"
        agent_json_path.write_text(json.dumps(existing))
        assert load_template_from_dir(tdir).core_mcps == "none"

        existing["core_mcps"] = "most"
        agent_json_path.write_text(json.dumps(existing))
        with pytest.raises(TemplateValidationError, match="core_mcps"):
            load_template_from_dir(tdir)


# ---------------------------------------------------------------------------
# Skill packages (skills.json)
# ---------------------------------------------------------------------------

class TestSkillPackages:
    @pytest.fixture(autouse=True)
    def _theme_factory_not_installed(self, monkeypatch):
        """Pin the installed-package view so these tests don't depend on
        whether the registry happens to have been scanned in this process.

        The repo ships a REAL ``mcps/skills/theme-factory`` and ``MCPS_DIR``
        points at the repo, so any earlier test that triggers
        ``mcp_registry.scan_manifests()`` — the skills installer calls it —
        registers theme-factory as genuinely INSTALLED for the rest of the
        worker. Every test in this class asserts on the package NOT being
        installed yet, so they passed alone and failed whenever xdist put a
        scanning test in the same worker first. (Root-caused 2026-08-16; the
        symptom was two tests flapping in full-suite runs.)

        Only theme-factory is removed — the rest of the registry view stays
        real, so nothing else silently changes shape. BOTH readers are
        covered: the preflight asks ``get_all_manifests``, the cascade asks
        ``get_manifest`` per package. A test that patches ``get_manifest``
        itself still wins inside its own ``with`` block.
        """
        from services.mcp import mcp_registry
        real_all = mcp_registry.get_all_manifests
        real_one = mcp_registry.get_manifest

        def _without_theme_factory():
            return {k: v for k, v in real_all().items() if k != "theme-factory"}

        def _one(name):
            return None if name == "theme-factory" else real_one(name)

        monkeypatch.setattr(mcp_registry, "get_all_manifests",
                            _without_theme_factory)
        monkeypatch.setattr(mcp_registry, "get_manifest", _one)

    def test_skills_json_validation(self, tmp_path, temp_db):
        from storage.agents.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = _write_template(tmp_path, skills=[{"name": "theme-factory"}])
        template = load_template_from_dir(tdir)
        assert [r.name for r in template.skill_packages] == ["theme-factory"]
        assert template.skill_packages[0].skills == []

        bad = _write_template(tmp_path, slug="bad-skills")
        (bad / "skills.json").write_text(json.dumps({"required": [{"skills": []}]}))
        with pytest.raises(TemplateValidationError, match="name"):
            load_template_from_dir(bad)

    def test_preflight_unknown_package_fails(self, tmp_path, temp_db):
        from fastapi import HTTPException
        tdir = _write_template(tmp_path, skills=[{"name": "no-such-package"}])
        with patch(
            "services.community.community_catalog.fetch_skills_registry",
            new=AsyncMock(return_value={"skills": []}),
        ):
            with pytest.raises(HTTPException) as exc:
                _install_admin(tdir)
        assert exc.value.status_code == 400
        assert exc.value.detail["error"] == "missing_skills"

    def test_preflight_manager_queues_skill_requests(self, tmp_path, temp_db):
        """Request-flow parity (2026-08-27): a manager installing a template
        whose package is only in the catalog gets the AGENT plus a queued
        ``kind: "skill"`` request per missing package — not the old
        ``skills_require_admin`` hard-fail."""
        from storage.mcp import mcp_request_store
        tdir = _write_template(tmp_path, skills=[{"name": "theme-factory"}])
        with patch(
            "services.community.community_catalog.fetch_skills_registry",
            new=AsyncMock(return_value={"skills": [{"name": "theme-factory"}]}),
        ), patch(
            "services.mcp.mcp_registry.get_manifest", return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(tdir, target_slug="mgr-skills-agent",
                                    installer_sub="user-manager",
                                    installer_role="manager")
        reqs = result["skill_packages"]["requested"]
        assert [r["mcp_name"] for r in reqs] == ["theme-factory"]
        assert reqs[0]["kind"] == "skill"
        assert result["skill_packages"]["ready"] == []
        row = mcp_request_store.get_request(reqs[0]["id"])
        assert row["status"] == "pending" and row["kind"] == "skill"
        # The queued request keeps the install batch alive for the
        # one-collapsed-notification flow.
        assert result.get("batch_id")

    def test_admin_cascade_installs_assigns_and_seeds(self, tmp_path, temp_db):
        from types import SimpleNamespace
        from storage.mcp import mcp_store
        tdir = _write_template(
            tmp_path, skills=[{"name": "theme-factory", "skills": ["theme-factory"]}],
        )
        skill = SimpleNamespace(
            id="theme-factory", default_exclude_from=[],
        )
        pkg_manifest = SimpleNamespace(
            name="theme-factory", skills=[skill],
            server=SimpleNamespace(runtime="none"),
        )
        install_mock = AsyncMock(return_value={"status": "installed"})
        # get_manifest: None on the cascade's first probe (not installed),
        # the package manifest afterwards (post-install seeding).
        calls = {"n": 0}
        def _get_manifest(name):
            if name != "theme-factory":
                return None
            calls["n"] += 1
            return None if calls["n"] == 1 else pkg_manifest
        with patch(
            "services.community.community_catalog.fetch_skills_registry",
            new=AsyncMock(return_value={"skills": [{"name": "theme-factory"}]}),
        ), patch(
            "services.community.skills_installer.install_skill_package_from_catalog",
            new=install_mock,
        ), patch(
            "services.mcp.mcp_registry.get_manifest", side_effect=_get_manifest,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(tdir, target_slug="skills-agent")
        install_mock.assert_awaited_once()
        assert result["skill_packages"]["ready"] == ["theme-factory"]
        assert result["skill_packages"]["failed"] == []
        # Assignment is what surfaces the package's skills in sessions.
        assert "theme-factory" in mcp_store.get_manager_enabled_mcps("skills-agent")
        rows = mcp_store.get_agent_skills("skills-agent")
        assert any(r["skill_id"] == "theme-factory" for r in rows)

    def test_skill_install_failure_never_aborts_agent(self, tmp_path, temp_db):
        from fastapi import HTTPException as HX
        from storage.agents import agent_store
        tdir = _write_template(
            tmp_path, slug="resilient", skills=[{"name": "theme-factory"}],
        )
        with patch(
            "services.community.community_catalog.fetch_skills_registry",
            new=AsyncMock(return_value={"skills": [{"name": "theme-factory"}]}),
        ), patch(
            "services.community.skills_installer.install_skill_package_from_catalog",
            new=AsyncMock(side_effect=HX(502, "tarball fetch failed")),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(tdir, target_slug="resilient-agent")
        # Agent exists and works; the failure is captured, not fatal.
        assert agent_store.agent_exists("resilient-agent")
        assert result["skill_packages"]["ready"] == []
        assert result["skill_packages"]["failed"][0]["name"] == "theme-factory"


# ---------------------------------------------------------------------------
# Template dashboards (S5)
# ---------------------------------------------------------------------------

class TestDashboardSeeding:
    """Template-shipped app dashboards: shared pins at install, per-user
    pins for the installer + late joiners, idempotent by the pinned_apps
    (agent, username, slug) upsert key. HTML-only v1 (actions '[]')."""

    def _install_dash(self, tmp_path, *, dashboards, dashboard_files,
                      slug="dashtpl", target="dash-agent",
                      installer=ADMIN_SUB):
        from unittest.mock import AsyncMock, patch
        _make_user(ADMIN_SUB, "admin@test.com", "admin")
        tdir = _write_template(
            tmp_path, slug=slug,
            dashboards=dashboards, dashboard_files=dashboard_files,
        )
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            return _install_admin(tdir, target_slug=target,
                                  installer_sub=installer)

    def test_shared_dashboard_seeded(self, tmp_path, temp_db):
        import config as app_config
        from storage import database as db
        result = self._install_dash(
            tmp_path,
            dashboards=[{"slug": "team-board", "title": "Team Board",
                         "file": "board.html", "visibility": "agent"}],
            dashboard_files={"board.html": "<h1>Board for {agent_slug}</h1>"},
        )
        assert result["seeded_dashboards"] == 1
        row = db.get_app_by_slug("dash-agent", "", "team-board")
        assert row is not None
        assert row["owner_sub"] is None and row["actions"] == "[]"
        assert row["rel_path"] == "workspace/apps/team-board.html"
        agent_dir = app_config.get_agent_dir("dash-agent")
        target = agent_dir / "workspace/apps/team-board.html"
        assert target.is_file()
        # {agent_slug} substituted; late-joiner source copy materialized.
        assert "Board for dash-agent" in target.read_text()
        assert (agent_dir / "config/community/dashboards/team-board.html").is_file()

    def test_user_dashboard_installer_and_late_joiner(self, tmp_path, temp_db):
        import config as app_config
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent
        result = self._install_dash(
            tmp_path,
            dashboards=[{"slug": "my-brief", "file": "brief.html",
                         "visibility": "user"}],
            dashboard_files={"brief.html": "<h1>Brief</h1>"},
        )
        assert result["seeded_dashboards"] == 1
        admin_name = db.get_username_by_sub(ADMIN_SUB)
        assert db.get_app_by_slug("dash-agent", admin_name, "my-brief") is not None
        # Late joiner gets their own copy…
        _make_user("bob-sub", "bob@test.com")
        db.add_user_agent("bob-sub", "dash-agent", "viewer", "test")
        counts = on_user_added_to_agent("dash-agent", "bob-sub", "viewer")
        assert counts["dashboards"] == 1
        bob_name = db.get_username_by_sub("bob-sub")
        row = db.get_app_by_slug("dash-agent", bob_name, "my-brief")
        assert row is not None and row["owner_sub"] == "bob-sub"
        assert (app_config.get_agent_dir("dash-agent")
                / f"users/{bob_name}/workspace/apps/my-brief.html").is_file()
        # …and a re-fire upserts instead of duplicating.
        again = on_user_added_to_agent("dash-agent", "bob-sub", "viewer")
        assert again["dashboards"] == 1
        rows = [a for a in db.list_apps("dash-agent", bob_name)
                if a["slug"] == "my-brief"]
        assert len(rows) == 1

    def test_auto_pin_opt_out_skips_late_joiners(self, tmp_path, temp_db):
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent
        self._install_dash(
            tmp_path,
            dashboards=[{"slug": "opt", "file": "o.html", "visibility": "user",
                         "auto_pin_for_new_users": False}],
            dashboard_files={"o.html": "<p>o</p>"},
        )
        _make_user("carol-sub", "carol@test.com")
        db.add_user_agent("carol-sub", "dash-agent", "viewer", "test")
        counts = on_user_added_to_agent("dash-agent", "carol-sub", "viewer")
        assert counts["dashboards"] == 0

    def test_validation_mode_and_files(self, tmp_path, temp_db):
        import json as _json
        import pytest as _pytest
        from storage.agents.community_agent_template_store import (
            TemplateValidationError, load_template_from_dir)
        # visibility the template's mode doesn't offer → manifest error.
        tdir = _write_template(
            tmp_path, slug="po-tpl",
            dashboards=[{"slug": "x", "file": "x.html", "visibility": "agent"}],
            dashboard_files={"x.html": "<p>x</p>"},
        )
        agent_json = _json.loads((tdir / "agent.json").read_text())
        agent_json.update(collaborative=False, default_scope="user")
        (tdir / "agent.json").write_text(_json.dumps(agent_json))
        with _pytest.raises(TemplateValidationError, match="not offered"):
            load_template_from_dir(tdir)
        # Missing file → manifest error.
        tdir2 = _write_template(
            tmp_path, slug="missing-tpl",
            dashboards=[{"slug": "x", "file": "nope.html"}],
            dashboard_files={},
        )
        with _pytest.raises(TemplateValidationError, match="missing file"):
            load_template_from_dir(tdir2)


def test_the_installer_names_the_agent_as_the_dialog_said(tmp_path, temp_db):
    """The install dialog's display name is the agent's; empty means the
    template's own."""
    from storage.agents import agent_store
    from storage.agents.community_agent_template_store import load_template_from_dir
    from services.community.community_agent_installer import install_from_extracted_template
    _make_user(ADMIN_SUB, "admin@test.com", "admin")
    template = load_template_from_dir(_write_template(tmp_path, slug="named-tpl"))
    with patch("services.community.community_agents_catalog.fetch_registry",
               new=AsyncMock(return_value={"mcps": []})), \
            patch("services.mcp.mcp_registry.get_all_manifests", return_value={}), \
            patch("services.notifications.notification_manager.fire_notification", new=AsyncMock()):
        for target, name, expect in (("named-a", "  Mine  ", "Mine"), ("named-b", "", "Named Tpl")):
            asyncio.run(install_from_extracted_template(
                template=template, target_slug=target, installer_user_sub=ADMIN_SUB,
                installer_role="admin", source_label="test", display_name=name))
            assert agent_store.get_agent(target)["display_name"] == expect


class TestInstallerManagerRow:
    """The installer's manager row is written atomically under the person's
    row lock (``add_user_agent``), never as a read of every row followed by
    a rewrite of the whole set."""

    def test_manager_installer_gets_an_atomic_row(self, tmp_path, temp_db, monkeypatch):
        from storage import database as db
        _make_user(MANAGER_SUB, "manager@example.com")
        seen: list[tuple] = []
        real_add = db.add_user_agent

        def _add(*args, **kwargs):
            seen.append(args)
            return real_add(*args, **kwargs)

        def _never(*args, **kwargs):
            raise AssertionError("set_user_agents must not run for the installer row")

        monkeypatch.setattr(db, "add_user_agent", _add)
        monkeypatch.setattr(db, "set_user_agents", _never)
        tdir = _write_template(tmp_path, slug="mgr-template")
        _install_admin(tdir, target_slug="mgr-agent", installer_sub=MANAGER_SUB,
                       installer_role="manager")
        assert seen == [(MANAGER_SUB, "mgr-agent", "manager", MANAGER_SUB)]
        assert db.get_user_agent_roles(MANAGER_SUB)["mgr-agent"] == "manager"
