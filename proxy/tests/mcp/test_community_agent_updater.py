"""Template updates of installed community agents (COMMUNITY-AGENTS-
REGISTRY.md "Updates").

Load-bearing: an untouched install takes every piece of the new version
and gains what it adds; a piece someone edited is kept with the new
version stored beside it, and "take" swaps it in; a member's onboarding
copy follows the old canonical only while it still equals it; an install
without a baseline keeps everything and adds; the version moves last and
a second apply changes nothing; the apply refuses a moved version and a
running update; a local template never updates.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from tests.mcp.test_community_agent_installer import ADMIN_SUB, _make_user, _write_template

MEMBER_SUB = "user-viewer"
AGENT = "up-agent"
HOME = {"title": "Home", "egress": ["api.open-meteo.com"],
        "actions": [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]}
LINT = {"name": "lint", "mandatory": True, "applies": ["chats"], "judge": {"rubric": "Is it tidy?"}}
DAILY = {"slug": "daily", "description": "Daily", "scope": "user", "prompt": "p1",
         "schedule": {"type": "cron", "cron": "0 8 * * *"}, "default_state": "paused"}
MONTHLY = {"slug": "monthly", "title": "Tidy up", "body": "b1", "scope": "user",
           "schedule": {"type": "cron", "cron": "0 9 1 * *"}, "default_state": "active"}


@pytest.fixture(autouse=True)
def _clean():
    from services.apps import app_supervisor
    from services.community import community_agent_updater as upd, template_app_seeder
    yield
    template_app_seeder._locks.clear()
    template_app_seeder._healed.clear()
    template_app_seeder._failure_told.clear()
    app_supervisor._instances.clear()
    upd._agent_locks.clear()
    upd._jobs.clear()
    from api.apps import apps as apps_api
    apps_api._deploy_locks.clear()


def _patches():
    return (patch("services.community.community_agents_catalog.fetch_registry",
                  new=AsyncMock(return_value={"mcps": [], "agents": []})),
            patch("services.community.community_catalog.fetch_registry",
                  new=AsyncMock(return_value={"mcps": []})),
            patch("services.mcp.mcp_registry.get_all_manifests", return_value={}),
            patch("services.notifications.notification_manager.fire_notification", new=AsyncMock()))


def _template(root: Path, version: str, *, persona="# P v1", notes="notes v1", extra=None,
              setup="setup v1", user_setup="user setup v1", daily_prompt="p1", weekly=False,
              monthly_body="b1", home_html="<p>v1</p>", lint_rubric="Is it tidy?", style=False,
              home_doc=None):
    tasks = [dict(DAILY, prompt=daily_prompt)]
    if weekly:
        tasks.append({"slug": "weekly", "description": "Weekly", "scope": "agent", "prompt": "w",
                      "schedule": {"type": "cron", "cron": "0 8 * * 1"}, "default_state": "paused"})
    checks = {"lint": dict(LINT, judge={"rubric": lint_rubric})}
    if style:
        checks["style"] = {"name": "style", "applies": ["tasks"], "judge": {"rubric": "House style?"}}
    context = {"notes.md": notes}
    if extra is not None:
        context["extra.md"] = extra
    d = _write_template(
        root, slug="uptpl", tasks=tasks, notifications=[dict(MONTHLY, body=monthly_body)],
        setup_md=setup, user_setup_md=user_setup, context_files=context,
        user_apps={"home": dict(home_doc or HOME, _files={"client/index.html": home_html})}, checks=checks,
        agent_json_extra={"collaborative": False, "default_scope": "user", "version": version},
    )
    (d / "agent.md").write_text(persona)
    return d


def _install_v1(tmp_path, **kw) -> None:
    from storage.agents.community_agent_template_store import load_template_from_dir
    from services.community.community_agent_installer import install_from_extracted_template
    _make_user(ADMIN_SUB, "admin@test.com", "admin")
    _make_user(MEMBER_SUB, "viewer@test.com", "member")
    template = load_template_from_dir(_template(tmp_path / "v1", "1.0.0", **kw))
    p = _patches()
    with p[0], p[1], p[2], p[3]:
        out = asyncio.run(install_from_extracted_template(
            template=template, target_slug=AGENT, installer_user_sub=ADMIN_SUB, installer_role="admin",
            source_label="uptpl", app_consent={a.slug: a.sig for a in template.apps},
            check_consent={c.name: c.sig for c in template.checks}, consent_by=ADMIN_SUB))
    assert out["seeded_apps"]["user"] == ["home"] and out["seeded_checks"]["consented"] == ["lint"]
    from services.community import community_agent_installer as inst
    from storage import database as db
    db.add_user_agent(MEMBER_SUB, AGENT, "viewer", assigned_by=ADMIN_SUB)
    inst.on_user_added_to_agent(AGENT, MEMBER_SUB, "viewer")   # the member's guide; no app worker in tests


def _v2(tmp_path, sub="v2", **kw):
    from storage.agents.community_agent_template_store import load_template_from_dir
    defaults = dict(persona="# P v2", notes="notes v2", extra="extra v2", setup="setup v2",
                    user_setup="user setup v2", daily_prompt="p2", weekly=True, monthly_body="b2",
                    home_html="<p>v2</p>", lint_rubric="Is it tidy now?", style=True)
    defaults.update(kw)
    return load_template_from_dir(_template(tmp_path / sub, "2.0.0", **defaults))


def _apply(template, *, consent=True, by=ADMIN_SUB, role="admin"):
    from services.community import community_agent_updater as upd
    p = _patches()
    with p[0], p[1], p[2], p[3]:
        return asyncio.run(upd._apply_now(
            AGENT, template, by, role,
            app_consent={a.slug: a.sig for a in template.apps} if consent else None,
            check_consent={c.name: c.sig for c in template.checks} if consent else None))


def _agent_dir() -> Path:
    import config
    return config.get_agent_dir(AGENT)


def test_an_untouched_install_takes_every_piece_and_gains_the_new_ones(tmp_path, temp_db):
    from storage import database as db
    from storage.agents import agent_store
    from services.checks import documents
    _install_v1(tmp_path)
    report = _apply(_v2(tmp_path))
    replaced = report["replaced"]
    for what in ("the persona (config/agent.md)", "context/notes.md", "the setup guide (setup.md)",
                 "the per-user guide (user-setup.md)", "the notification 'monthly'", "the check 'lint'"):
        assert what in replaced, (what, report)
    assert any(r.startswith("the task 'daily'") for r in replaced)
    assert any(r.startswith("the app 'home'") for r in replaced)
    assert set(report["added"]) >= {"context/extra.md", "the task 'weekly'", "the check 'style'"}
    assert report["kept"] == [] and report["pending_apps"] == []
    d = _agent_dir()
    assert (d / "config" / "agent.md").read_text() == "# P v2"
    assert (d / "config" / "context" / "notes.md").read_text() == "notes v2"
    assert (d / "config" / "context" / "extra.md").read_text() == "extra v2"
    assert (d / "config" / "context" / "setup.md").read_text() == "setup v2"
    assert (d / "config" / "user-setup.md").read_text() == "user setup v2"
    # The untouched onboarding copies (the installer's and the member's)
    # followed the canonical.
    member = db.get_username_by_sub(MEMBER_SUB)
    assert (d / "users" / member / "context" / "user-setup.md").read_text() == "user setup v2"
    assert report["members"] == {"user_setup_replaced": 2, "user_setup_kept": 0}
    # The seeded rows took the new fields; the new task exists for the agent.
    from storage.automation import db_tasks
    assert db_tasks.find_template_task(AGENT, "daily", ADMIN_SUB)["prompt"] == "p2"
    assert db_tasks.find_template_task(AGENT, "weekly") is not None
    assert documents.load_check(AGENT, "", "lint").doc["judge"]["rubric"] == "Is it tidy now?"
    assert documents.load_check(AGENT, "", "style") is not None
    # The admin's app got a new release, approved by the new consent; the
    # member, who had no copy, got one.
    admin = db.get_username_by_sub(ADMIN_SUB)
    row = db.get_app_by_slug(AGENT, admin, "home")
    assert row["release_path"].endswith("/2") and db.app_actions_approved(row)
    assert (d / "users" / admin / "workspace" / "apps" / "home" / "client" / "index.html").read_text() == "<p>v2</p>"
    assert db.get_app_by_slug(AGENT, member, "home") is not None
    assert f"the app 'home' for {member}" in report["added"]
    # The version moved last, with the new record.
    agent = agent_store.get_agent(AGENT)
    assert agent["community_template_version"] == "2.0.0"
    data = agent_store.get_community_template_data(AGENT)
    assert data["version"] == "2.0.0" and data["consent"]["apps"]["home"]["by"] == ADMIN_SUB
    # And a second apply of the same version changes nothing.
    again = _apply(_v2(tmp_path, sub="v2-again"))
    assert again["replaced"] == [] and again["added"] == [] and again["unchanged"] > 5


def test_edited_pieces_are_kept_with_the_new_version_beside_and_taken_on_request(tmp_path, temp_db):
    from storage import database as db
    from services.checks import documents
    from services.community import community_agent_updater as upd
    _install_v1(tmp_path)
    d = _agent_dir()
    (d / "config" / "agent.md").write_text("# mine")
    (d / "config" / "context" / "notes.md").write_text("my notes")
    admin = db.get_username_by_sub(ADMIN_SUB)
    (d / "users" / admin / "workspace" / "apps" / "home" / "client" / "index.html").write_text("<p>mine</p>")
    documents.write_check(AGENT, "", dict(LINT, judge={"rubric": "My own rubric"}), None, updated_by="admin")
    member = db.get_username_by_sub(MEMBER_SUB)
    (d / "users" / member / "context" / "user-setup.md").write_text("my onboarding")
    report = _apply(_v2(tmp_path))
    kept = {k["what"]: k for k in report["kept"]}
    for what in ("the persona (config/agent.md)", "context/notes.md", f"the app 'home' of {admin}", "the check 'lint'"):
        assert what in kept and kept[what]["reason"] == "edited locally", (what, report)
    assert kept["the persona (config/agent.md)"]["new_path"] == "config/community/2.0.0/agent.md"
    assert (d / "config" / "community" / "2.0.0" / "agent.md").read_text() == "# P v2"
    assert (d / "config" / "community" / "2.0.0" / "context" / "notes.md").read_text() == "notes v2"
    assert (d / "config" / "community" / "2.0.0" / "user-apps" / "home" / "client" / "index.html").read_text() == "<p>v2</p>"
    assert (d / "config" / "community" / "2.0.0" / "checks" / "lint" / "check.json").is_file()
    # The live pieces stayed the manager's.
    assert (d / "config" / "agent.md").read_text() == "# mine"
    assert documents.load_check(AGENT, "", "lint").doc["judge"]["rubric"] == "My own rubric"
    assert (d / "users" / member / "context" / "user-setup.md").read_text() == "my onboarding"
    # The installer's own untouched copy followed; the member's edited one stayed.
    assert report["members"] == {"user_setup_replaced": 1, "user_setup_kept": 1}
    # What was not edited still moved.
    assert "the setup guide (setup.md)" in report["replaced"]
    # "Take the new version" swaps the persona and the app.
    p = _patches()
    with p[0], p[1], p[2], p[3]:
        out = asyncio.run(upd.take(AGENT, "config/community/2.0.0/agent.md", ADMIN_SUB, "admin"))
        assert out["status"] == "replaced" and (d / "config" / "agent.md").read_text() == "# P v2"
        out = asyncio.run(upd.take(AGENT, "config/community/2.0.0/user-apps/home", ADMIN_SUB, "admin"))
    assert (d / "users" / admin / "workspace" / "apps" / "home" / "client" / "index.html").read_text() == "<p>v2</p>"
    with pytest.raises(HTTPException) as e:
        asyncio.run(upd.take(AGENT, "config/context/notes.md", ADMIN_SUB, "admin"))
    assert e.value.status_code == 400


def test_an_install_without_a_baseline_keeps_everything_and_adds(tmp_path, temp_db):
    from storage.agents import agent_store
    _install_v1(tmp_path)
    data = agent_store.get_community_template_data(AGENT)
    data.pop("baseline", None)          # an install made before 1.7
    agent_store.set_community_template_data(AGENT, data)
    report = _apply(_v2(tmp_path))
    kept = {k["what"]: k["reason"] for k in report["kept"]}
    assert kept["the persona (config/agent.md)"] == "no record of the installed version"
    assert kept["context/notes.md"] == "no record of the installed version"
    assert "context/extra.md" in report["added"] and "the check 'style'" in report["added"]
    assert agent_store.get_agent(AGENT)["community_template_version"] == "2.0.0"
    assert agent_store.get_community_template_data(AGENT)["baseline"]   # the next update compares properly


def test_a_completed_setup_is_not_resurrected(tmp_path, temp_db):
    _install_v1(tmp_path)
    (_agent_dir() / "config" / "context" / "setup.md").unlink()
    report = _apply(_v2(tmp_path))
    assert not (_agent_dir() / "config" / "context" / "setup.md").exists()
    assert "the setup guide (setup.md)" not in report["replaced"] + report["added"]
    assert any("completed" in n for n in report.get("notes", []))


def test_unconsented_apps_wait_only_when_their_manifest_changed_and_checks_stay_offered(tmp_path, temp_db):
    """Without consent, a re-release whose manifest is the one already
    approved goes live under that approval (the tree is content, as on
    any deploy); one whose manifest changed parks on the card with the
    old release still live."""
    from storage import database as db
    from services.checks import documents
    _install_v1(tmp_path)
    admin = db.get_username_by_sub(ADMIN_SUB)
    report = _apply(_v2(tmp_path), consent=False)
    row = db.get_app_by_slug(AGENT, admin, "home")
    # The admin's approved copy took release 2 live; the member's first
    # copy, seeded now without consent, waits on their card.
    assert f"home ({admin})" not in report["pending_apps"], report
    assert db.app_actions_approved(row) and row["release_path"].endswith("/2")
    member = db.get_username_by_sub(MEMBER_SUB)
    assert f"home ({member})" in report["pending_apps"]
    assert set(report["offered_checks"]) == {"lint", "style"}
    assert documents.load_check(AGENT, "", "style").doc["mandatory"] is False
    # A new manifest (more egress) without consent: pending, the old live.
    report = _apply(_v2(tmp_path, sub="v2-egress", home_doc=dict(HOME, egress=["api.open-meteo.com", "x.example"])),
                    consent=False)
    row = db.get_app_by_slug(AGENT, admin, "home")
    assert any(p.startswith("home") for p in report["pending_apps"]), report
    assert not db.app_actions_approved(row)
    assert row["release_path"].endswith("/2") and int(row["pending_release"] or 0) == 3


def test_a_copy_its_owner_hid_is_left_alone(tmp_path, temp_db):
    from storage import database as db
    _install_v1(tmp_path)
    admin = db.get_username_by_sub(ADMIN_SUB)
    row = db.get_app_by_slug(AGENT, admin, "home")
    assert db.set_app_hidden(row["id"], True)
    report = _apply(_v2(tmp_path))
    kept = {k["what"]: k["reason"] for k in report["kept"]}
    assert kept[f"the app 'home' of {admin}"] == "hidden by its owner", report
    row = db.get_app_by_slug(AGENT, admin, "home")
    assert row["hidden"] and row["release_path"].endswith("/1")
    # Hiding never prunes a template copy, even past the scope's cap.
    from storage import db_apps
    old_cap = db_apps.MAX_APPS_PER_SCOPE
    db_apps.MAX_APPS_PER_SCOPE = 0
    try:
        assert db.set_app_hidden(row["id"], True)
    finally:
        db_apps.MAX_APPS_PER_SCOPE = old_cap
    assert db.get_app_by_slug(AGENT, admin, "home") is not None


def test_blueprint_triggers_follow_the_update(tmp_path, temp_db):
    """A copy's seeded triggers across versions: one the new blueprint adds
    is created for every copy; one the member deleted stays deleted
    ("removed locally"); a fresh copy gets them all; a handler the new
    version renamed re-points the trigger, a handler it dropped pauses the
    trigger with the reason, never deletes it."""
    from storage import database as db
    from storage.automation import trigger_store

    def doc(handlers, triggers):
        return dict(HOME, handlers={"on_trigger": handlers},
                    _blueprint={"triggers": [{"slug": s, "handler": h, "description": s.title()}
                                             for s, h in triggers]})

    _install_v1(tmp_path, home_doc=doc(["ping"], [("ping", "ping")]))
    admin = db.get_username_by_sub(ADMIN_SUB)
    mine = trigger_store.find_template_trigger(AGENT, "home__ping", ADMIN_SUB)
    assert mine and mine["app_id"] == db.get_app_by_slug(AGENT, admin, "home")["id"]
    trigger_store.delete_trigger(mine["id"])
    # v2 keeps ping and adds pong: pong for the admin's copy, ping stays
    # gone there; the member's first copy gets both.
    report = _apply(_v2(tmp_path, home_doc=doc(["ping", "pong"], [("ping", "ping"), ("pong", "pong")])))
    kept = {k["what"]: k["reason"] for k in report["kept"]}
    assert kept.get("the trigger 'ping' of the app 'home'") == "removed locally", report
    assert "the trigger 'pong' of the app 'home'" in report["added"], report
    assert trigger_store.find_template_trigger(AGENT, "home__ping", ADMIN_SUB) is None
    pong = trigger_store.find_template_trigger(AGENT, "home__pong", ADMIN_SUB)
    assert pong and pong["handler"] == "pong" and pong["app_id"] == db.get_app_by_slug(AGENT, admin, "home")["id"]
    member = db.get_username_by_sub(MEMBER_SUB)
    theirs = db.get_app_by_slug(AGENT, member, "home")
    for slug in ("ping", "pong"):
        t = trigger_store.find_template_trigger(AGENT, f"home__{slug}", MEMBER_SUB)
        assert t and t["app_id"] == theirs["id"] and t["enabled"], slug
    # v3 renames pong's handler and drops ping: the admin's pong is
    # re-pointed; the member's ping is paused with the reason, still there.
    report = _apply(_v2(tmp_path, sub="v3", home_html="<p>v3</p>",
                        home_doc=doc(["pong2"], [("pong", "pong2")])))
    assert trigger_store.find_template_trigger(AGENT, "home__pong", ADMIN_SUB)["handler"] == "pong2"
    ping = trigger_store.find_template_trigger(AGENT, "home__ping", MEMBER_SUB)
    assert ping and not ping["enabled"] and ping["last_error"] == "the app's new version has no handler 'ping'"
    assert "the trigger 'ping' of the app 'home' is paused: the new version has no handler 'ping'" in report["notes"]
    assert trigger_store.find_template_trigger(AGENT, "home__pong", MEMBER_SUB)["handler"] == "pong2"


def test_an_update_enables_the_core_mcps_the_platform_gained_since(tmp_path, temp_db, monkeypatch):
    """The record remembers the platform's core MCPs when it was written;
    an update enables the ones the platform gained since (the agent's own
    migration to a new core tool), leaves one a manager disabled alone,
    and a record from before the rule treats every core MCP not enabled
    as new."""
    from services.mcp import mcp_registry
    from storage.agents import agent_store
    from storage.mcp import mcp_store
    core = ["memory-mcp"]
    monkeypatch.setattr(mcp_registry, "core_mcp_names", lambda: list(core))
    _install_v1(tmp_path)
    assert agent_store.get_community_template_data(AGENT)["core_mcps"] == ["memory-mcp"]
    assert "memory-mcp" in mcp_store.get_manager_enabled_mcps(AGENT)
    # The platform gains checks-mcp: the update enables it and says so.
    core.append("checks-mcp")
    report = _apply(_v2(tmp_path))
    assert "the core MCP 'checks-mcp'" in report["added"] and report["mcps"]["core"] == ["checks-mcp"]
    assert "checks-mcp" in mcp_store.get_manager_enabled_mcps(AGENT)
    assert agent_store.get_community_template_data(AGENT)["core_mcps"] == ["memory-mcp", "checks-mcp"]
    # A manager disables it: the next update leaves it disabled.
    mcp_store.remove_agent_mcp(AGENT, "checks-mcp")
    report = _apply(_v2(tmp_path, sub="v2b", home_html="<p>v2b</p>"))
    assert report["mcps"]["core"] == [] and "checks-mcp" not in mcp_store.get_manager_enabled_mcps(AGENT)
    # A record from before the rule (no list): every core MCP not enabled is new.
    data = agent_store.get_community_template_data(AGENT)
    data.pop("core_mcps")
    agent_store.set_community_template_data(AGENT, data)
    report = _apply(_v2(tmp_path, sub="v2c", home_html="<p>v2c</p>"))
    assert report["mcps"]["core"] == ["checks-mcp"] and "checks-mcp" in mcp_store.get_manager_enabled_mcps(AGENT)


def test_a_second_press_during_the_download_is_refused(tmp_path, temp_db):
    from services.community import community_agent_updater as upd
    _install_v1(tmp_path)
    template = _v2(tmp_path)
    gate = asyncio.Event()

    async def slow_fetch(_row):
        await gate.wait()
        return template, template.source_dir, {"slug": "uptpl", "version": "2.0.0"}

    async def go():
        with patch.object(upd, "_fetch_new", new=slow_fetch):
            p = _patches()
            with p[0], p[1], p[2], p[3]:
                first = asyncio.create_task(upd.apply(AGENT, ADMIN_SUB, "admin", from_version="1.0.0",
                                                      app_consent=None, check_consent=None))
                await asyncio.sleep(0.05)
                assert upd.status(AGENT)["status"] == "starting"
                with pytest.raises(HTTPException) as e:
                    await upd.apply(AGENT, ADMIN_SUB, "admin", from_version="1.0.0",
                                    app_consent=None, check_consent=None)
                assert e.value.status_code == 409 and "running" in e.value.detail
                gate.set()
                out = await first
                await upd._jobs[AGENT]["task"]
        assert out["to_version"] == "2.0.0" and upd.status(AGENT)["status"] == "done"

    asyncio.run(go())


def test_apply_refuses_a_moved_version_a_running_update_and_a_local_template(tmp_path, temp_db):
    from storage.agents import agent_store
    from services.community import community_agent_updater as upd
    _install_v1(tmp_path)
    template = _v2(tmp_path)

    async def fake_fetch(_row):
        return template, template.source_dir, {"slug": "uptpl", "version": "2.0.0"}

    async def go():
        with patch.object(upd, "_fetch_new", new=fake_fetch):
            with pytest.raises(HTTPException) as e:
                await upd.apply(AGENT, ADMIN_SUB, "admin", from_version="0.9.0", app_consent=None, check_consent=None)
            assert e.value.status_code == 409 and "already at" in e.value.detail
            lock = upd._agent_locks.setdefault(AGENT, asyncio.Lock())
            async with lock:
                with pytest.raises(HTTPException) as e:
                    await upd.apply(AGENT, ADMIN_SUB, "admin", from_version="1.0.0", app_consent=None,
                                    check_consent=None)
                assert e.value.status_code == 409 and "running" in e.value.detail
            p = _patches()
            with p[0], p[1], p[2], p[3]:
                out = await upd.apply(AGENT, ADMIN_SUB, "admin", from_version="1.0.0",
                                      app_consent={a.slug: a.sig for a in template.apps}, check_consent=None)
                assert out["to_version"] == "2.0.0" and upd.status(AGENT)["running"]
                await upd._jobs[AGENT]["task"]
        st = upd.status(AGENT)
        assert st["status"] == "done" and st["report"]["to_version"] == "2.0.0"
        assert agent_store.get_agent(AGENT)["community_template_version"] == "2.0.0"
        # The real fetch refuses a local template and an older or equal catalog.
        with pytest.raises(HTTPException) as e:
            await upd._fetch_new({"community_template": "local:uptpl", "community_template_version": "1.0.0"})
        assert e.value.status_code == 400
        with patch("services.community.community_agents_catalog.fetch_registry",
                   new=AsyncMock(return_value={"agents": [{"slug": "uptpl", "version": "2.0.0"}]})):
            with pytest.raises(HTTPException) as e:
                await upd._fetch_new({"community_template": "uptpl", "community_template_version": "2.0.0"})
            assert e.value.status_code == 409

    asyncio.run(go())


def test_detect_and_the_listing_carry_the_installed_versions(tmp_path, temp_db):
    from services.community import community_agent_updater as upd
    assert upd.detect({"community_template": "x", "community_template_version": "1.0.0"},
                      {"slug": "x", "version": "1.1.0"})["update_available"] is True
    assert upd.detect({"community_template": "local:x", "community_template_version": "1.0.0"},
                      {"slug": "x", "version": "1.1.0"})["update_available"] is False
    assert upd.detect({"community_template": "x", "community_template_version": "1.1.0"},
                      {"slug": "x", "version": "1.1.0"})["update_available"] is False
    assert upd.detect({"community_template": "x", "community_template_version": "1.0.0"}, None)["update_available"] is False
    _install_v1(tmp_path)
    from fastapi.testclient import TestClient
    from app import app
    from auth.providers import UserContext, get_current_user
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        sub=ADMIN_SUB, email="admin@test.com", name="Admin", role="admin", agents=[], agent_roles={})
    try:
        with patch("services.community.community_agents_catalog.fetch_registry",
                   new=AsyncMock(return_value={"agents": [{"slug": "uptpl", "version": "2.0.0", "required_mcps": []}]})):
            body = TestClient(app).get("/v1/community/agents").json()
            info = TestClient(app).get(f"/v1/agents/{AGENT}/info").json()
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    (entry,) = body["agents"]
    assert entry["installed"] == [{"agent_slug": AGENT, "version": "1.0.0", "update_available": True}]
    assert info["template_update"]["update_available"] is True and info["template_update"]["catalog_version"] == "2.0.0"


def test_the_daily_sweep_tells_the_managers_once_per_version(tmp_path, temp_db):
    from services.community import community_agent_updater as upd
    from storage.agents import agent_store
    _install_v1(tmp_path)
    fired = AsyncMock()
    with patch("services.community.community_agents_catalog.fetch_registry",
               new=AsyncMock(return_value={"agents": [{"slug": "uptpl", "version": "2.0.0"}]})), \
            patch("services.notifications.notification_manager.fire_notification", new=fired):
        upd._last_notify_sweep = 0.0
        assert asyncio.run(upd.maybe_notify_updates()) == 1
        assert fired.call_args.kwargs["target"] == ADMIN_SUB and "2.0.0" in fired.call_args.kwargs["title"]
        assert agent_store.get_community_template_data(AGENT)["update_notified"] == "2.0.0"
        upd._last_notify_sweep = 0.0
        assert asyncio.run(upd.maybe_notify_updates()) == 0


def test_the_home_app_test_data_is_json(tmp_path):
    assert json.loads(json.dumps(HOME))["title"] == "Home"


def test_pieces_the_manager_removed_stay_removed(tmp_path, temp_db):
    """A task, a notification, a check or a context file the installed
    version had and the manager deleted is not brought back by an update:
    it is reported as removed locally, and the new version's additions
    still land."""
    from storage.automation import db_tasks, notification_store
    from services.checks import documents
    from services.community import community_agent_updater as upd
    _install_v1(tmp_path)
    d = _agent_dir()
    (d / "config" / "context" / "notes.md").unlink()
    for sub in (ADMIN_SUB, MEMBER_SUB):
        row = db_tasks.find_template_task(AGENT, "daily", sub)
        if row:
            assert db_tasks.delete_dynamic_task(row["id"])
    for sub in (ADMIN_SUB, MEMBER_SUB):
        nid = f"notif-uptpl-monthly-{AGENT}-{sub}"
        if notification_store.get_notification(nid):
            assert notification_store.delete_notification(nid)
    assert documents.delete_check(AGENT, "", "lint")
    report = _apply(_v2(tmp_path))
    removed = {k["what"] for k in report["kept"] if k["reason"] == upd.REMOVED}
    assert removed == {"context/notes.md", "the task 'daily'", "the notification 'monthly'", "the check 'lint'"}, report
    assert not (d / "config" / "context" / "notes.md").exists()
    assert db_tasks.find_template_task(AGENT, "daily", ADMIN_SUB) is None
    assert notification_store.get_notification(f"notif-uptpl-monthly-{AGENT}-{ADMIN_SUB}") is None
    assert documents.load_check(AGENT, "", "lint") is None
    # What the new version adds still lands, and the removed pieces are not
    # among the replaced or added ones.
    assert set(report["added"]) >= {"context/extra.md", "the task 'weekly'", "the check 'style'"}
    for what in removed:
        assert what not in report["replaced"] + report["added"]
    assert "left out (removed locally)" in upd._report_words(report)
    # A second update of the same agent still leaves them out.
    again = _apply(_v2(tmp_path, sub="v2-again"))
    assert {k["what"] for k in again["kept"] if k["reason"] == upd.REMOVED} == removed


def test_an_mcp_the_manager_disabled_is_not_re_enabled(tmp_path, temp_db):
    """The installed version required an MCP; the manager disabled it; the
    update leaves it disabled and says so. An MCP the new version adds
    cascades like an install."""
    from storage.agents.community_agent_template_store import load_template_from_dir
    from services.community import community_agent_updater as upd
    from storage.mcp import mcp_store
    _make_user(ADMIN_SUB, "admin@test.com", "admin")
    from services.community.community_agent_installer import install_from_extracted_template
    v1 = _write_template(tmp_path / "m1", slug="mtpl", mcps=[{"name": "alpha-mcp"}],
                         agent_json_extra={"version": "1.0.0"})
    template = load_template_from_dir(v1)
    p = _patches()
    with p[0], p[1], patch("services.mcp.mcp_registry.get_all_manifests",
                           return_value={"alpha-mcp": object(), "beta-mcp": object()}), \
            patch("services.community.community_agent_installer._cascade_required_mcps",
                  new=AsyncMock(return_value={"ready_mcps": ["alpha-mcp"], "created_requests": []})), p[3]:
        asyncio.run(install_from_extracted_template(
            template=template, target_slug=AGENT, installer_user_sub=ADMIN_SUB, installer_role="admin",
            source_label="mtpl"))
    mcp_store.add_agent_mcp(AGENT, "alpha-mcp")
    mcp_store.remove_agent_mcp(AGENT, "alpha-mcp")          # the manager's choice
    v2 = load_template_from_dir(_write_template(
        tmp_path / "m2", slug="mtpl", mcps=[{"name": "alpha-mcp"}, {"name": "beta-mcp"}],
        agent_json_extra={"version": "2.0.0"}))
    cascade = AsyncMock(return_value={"ready_mcps": ["beta-mcp"], "created_requests": []})
    with p[0], p[1], p[2], p[3], \
            patch("services.community.community_agent_installer._cascade_required_mcps", new=cascade):
        report = asyncio.run(upd._apply_now(AGENT, v2, ADMIN_SUB, "admin", app_consent=None, check_consent=None))
    assert report["mcps"]["removed"] == ["alpha-mcp"] and report["mcps"]["new"] == ["beta-mcp"]
    assert "the MCP 'alpha-mcp'" in {k["what"] for k in report["kept"] if k["reason"] == upd.REMOVED}
    (called,) = cascade.call_args_list
    assert [m.name for m in called.kwargs["template"].mcps] == ["beta-mcp"]
    assert "alpha-mcp" not in mcp_store.get_manager_enabled_mcps(AGENT)


def test_the_update_and_take_never_go_through_a_link(tmp_path, temp_db):
    # /config is writable from a manager's session: a symlink planted where
    # the update keeps a new version, or where "Take" reads or writes, is
    # never followed — no host file is read into the agent or overwritten.
    from services.community import community_agent_updater as upd
    from services.community.template_app_seeder import LinkedPath
    _install_v1(tmp_path)
    d = _agent_dir()
    (d / "config" / "agent.md").write_text("# mine")
    outside = tmp_path / "outside.txt"
    outside.write_text("HOST SECRET")
    (d / "config" / "community" / "2.0.0").mkdir(parents=True)
    (d / "config" / "community" / "2.0.0" / "agent.md").symlink_to(outside)
    with pytest.raises(LinkedPath):
        _apply(_v2(tmp_path))
    assert outside.read_text() == "HOST SECRET"
    (d / "config" / "community" / "2.0.0" / "agent.md").unlink()
    _apply(_v2(tmp_path, sub="v2b"))
    assert (d / "config" / "community" / "2.0.0" / "agent.md").read_text() == "# P v2"
    # Take: a stored copy swapped for a link is not read...
    (d / "config" / "community" / "2.0.0" / "agent.md").unlink()
    (d / "config" / "community" / "2.0.0" / "agent.md").symlink_to(outside)
    p = _patches()
    with p[0], p[1], p[2], p[3]:
        with pytest.raises(HTTPException) as e:
            asyncio.run(upd.take(AGENT, "config/community/2.0.0/agent.md", ADMIN_SUB, "admin"))
    assert e.value.status_code == 400 and (d / "config" / "agent.md").read_text() == "# mine"
    # ...and a live piece swapped for a link is not written through.
    (d / "config" / "community" / "2.0.0" / "agent.md").unlink()
    (d / "config" / "community" / "2.0.0" / "agent.md").write_text("# P v2")
    (d / "config" / "agent.md").unlink()
    (d / "config" / "agent.md").symlink_to(outside)
    with p[0], p[1], p[2], p[3]:
        with pytest.raises(HTTPException) as e:
            asyncio.run(upd.take(AGENT, "config/community/2.0.0/agent.md", ADMIN_SUB, "admin"))
    assert e.value.status_code == 400 and outside.read_text() == "HOST SECRET"


def test_a_catalog_version_is_a_release_number():
    # The version names a folder under config/community.
    from services.community import community_agent_updater as upd
    for ok in ("2.0.0", "3.1.2", "1.0.0-beta.1", "10.2+build.7"):
        assert upd._VERSION_RE.match(ok), ok
    for bad in ("", "../x", "user-apps", "dashboards", "1/2", "v2.0.0", ".."):
        assert not upd._VERSION_RE.match(bad), bad


def test_a_check_script_without_a_final_newline_is_untouched_after_install(tmp_path, temp_db):
    # The folder keeps the script with a final newline; the record's hash is
    # the written one, so an update does not take the untouched check for
    # an edited one and keep it forever.
    from services.checks import documents
    from storage.agents import agent_store
    from storage.agents.community_agent_template_store import load_template_from_dir
    from services.community.community_agent_installer import install_from_extracted_template
    _make_user(ADMIN_SUB, "admin@test.com", "admin")
    check = {"name": "run-it", "script": {"run": "check.sh"}, "_script": "#!/bin/sh\nexit 0"}
    template = load_template_from_dir(_write_template(tmp_path / "t", slug="nltpl", checks={"run-it": check}))
    p = _patches()
    with p[0], p[1], p[2], p[3]:
        asyncio.run(install_from_extracted_template(
            template=template, target_slug=AGENT, installer_user_sub=ADMIN_SUB, installer_role="admin",
            source_label="nltpl", check_consent={c.name: c.sig for c in template.checks},
            consent_by=ADMIN_SUB))
    rec = agent_store.get_community_template_data(AGENT)
    assert rec["checks"][0]["script_sha256"] == documents.load_check(AGENT, "", "run-it").script_sha256


def test_auto_attach_an_admin_turned_off_stays_off(tmp_path, temp_db):
    # The installed version asked for auto-attach; an admin cleared it; an
    # update of a version that still asks for it does not turn it back on.
    # A version that newly asks for it is still offered to an admin.
    from storage.agents import agent_store
    from storage.agents.community_agent_template_store import load_template_from_dir
    from services.community import community_agent_updater as upd
    from services.community.community_agent_installer import install_from_extracted_template
    _make_user(ADMIN_SUB, "admin@test.com", "admin")
    auto = {"default_for_new_users": {"enabled": True, "role": "viewer"}}
    v1 = load_template_from_dir(_write_template(tmp_path / "a1", slug="atpl",
                                                agent_json_extra={"version": "1.0.0", **auto}))
    p = _patches()
    with p[0], p[1], p[2], p[3]:
        asyncio.run(install_from_extracted_template(
            template=v1, target_slug=AGENT, installer_user_sub=ADMIN_SUB, installer_role="admin",
            source_label="atpl"))
    assert agent_store.get_agent(AGENT)["default_for_new_users_role"] == "viewer"
    agent_store.set_default_for_new_users_role(AGENT, "")        # the admin's choice
    v2 = load_template_from_dir(_write_template(tmp_path / "a2", slug="atpl",
                                                agent_json_extra={"version": "2.0.0", **auto}))
    with p[0], p[1], p[2], p[3]:
        report = asyncio.run(upd._apply_now(AGENT, v2, ADMIN_SUB, "admin", app_consent=None, check_consent=None))
    assert not agent_store.get_agent(AGENT)["default_for_new_users_role"]
    assert "auto-attach for new users" not in report["added"]


def test_a_managers_mandatory_choice_survives_an_update(tmp_path, temp_db):
    # Installed as consented (mandatory), turned off by a manager: the new
    # version's document lands, still offered. Installed offered, made
    # mandatory by a manager: it stays mandatory without a new consent.
    from services.checks import documents
    _install_v1(tmp_path)
    local = documents.load_check(AGENT, "", "lint")
    assert local.mandatory is True
    documents.write_check(AGENT, "", dict(local.doc, mandatory=False), None, updated_by="manager")
    report = _apply(_v2(tmp_path))
    lint = documents.load_check(AGENT, "", "lint")
    assert "the check 'lint'" in report["replaced"], report
    assert lint.mandatory is False and lint.doc["judge"]["rubric"] == "Is it tidy now?"
    style = documents.load_check(AGENT, "", "style")
    documents.write_check(AGENT, "", dict(style.doc, mandatory=True), None, updated_by="manager")
    report = _apply(_v2(tmp_path, sub="v3", lint_rubric="Tidier?"), consent=False)
    style = documents.load_check(AGENT, "", "style")
    assert style.mandatory is True and "style" not in report["offered_checks"], report


def test_member_guides_made_from_an_edited_guide_keep_it(tmp_path, temp_db):
    # The manager edited the per-user guide (kept by the update); a copy
    # made from the edited guide stays; a copy still on the installed
    # version's text moves to the new version.
    from storage import database as db
    _install_v1(tmp_path)
    d = _agent_dir()
    (d / "config" / "user-setup.md").write_text("our own onboarding")
    admin = db.get_username_by_sub(ADMIN_SUB)
    member = db.get_username_by_sub(MEMBER_SUB)
    (d / "users" / member / "context" / "user-setup.md").write_text("our own onboarding")
    report = _apply(_v2(tmp_path))
    assert (d / "config" / "user-setup.md").read_text() == "our own onboarding"
    assert (d / "users" / member / "context" / "user-setup.md").read_text() == "our own onboarding"
    assert (d / "users" / admin / "context" / "user-setup.md").read_text() == "user setup v2"
    assert report["members"] == {"user_setup_replaced": 1, "user_setup_kept": 1}


def test_take_is_offered_only_on_the_pressers_own_copy(tmp_path, temp_db):
    # "Take the new version" acts on the presser's own copy, so another
    # member's kept copy is listed without it (it would replace the
    # presser's copy, not theirs).
    from storage import database as db
    from services.community import template_app_seeder
    _install_v1(tmp_path)
    with patch("services.notifications.notification_manager.fire_notification", new=AsyncMock()):
        asyncio.run(template_app_seeder.seed_user_copy(AGENT, MEMBER_SUB, "home"))
    d = _agent_dir()
    admin = db.get_username_by_sub(ADMIN_SUB)
    member = db.get_username_by_sub(MEMBER_SUB)
    for who in (admin, member):
        (d / "users" / who / "workspace" / "apps" / "home" / "client" / "index.html").write_text(f"<p>{who}</p>")
    report = _apply(_v2(tmp_path))
    kept = {k["what"]: k for k in report["kept"]}
    assert kept[f"the app 'home' of {admin}"]["new_path"] == "config/community/2.0.0/user-apps/home"
    assert kept[f"the app 'home' of {member}"]["new_path"] == ""


def test_an_edited_manifest_of_an_app_with_task_buttons_is_kept(tmp_path, temp_db):
    # The seed rewrites a fire_task button's target, so its manifest is
    # compared without the targets: an untouched copy takes the new
    # version, one whose manifest was edited is kept.
    import json as _json
    from storage import database as db
    doc = dict(HOME, actions=[{"id": "go", "label": "Go", "type": "fire_task", "task": "brief"}],
               _blueprint={"tasks": [{"slug": "brief", "prompt": "Write the brief"}]})
    _install_v1(tmp_path, home_doc=doc)
    d = _agent_dir()
    admin = db.get_username_by_sub(ADMIN_SUB)
    manifest = d / "users" / admin / "workspace" / "apps" / "home" / "app.json"
    assert "task_id" in manifest.read_text()
    report = _apply(_v2(tmp_path, home_doc=doc))
    assert f"the app 'home' of {admin}" in report["replaced"], report
    local = _json.loads(manifest.read_text())
    local["title"] = "My home"
    manifest.write_text(_json.dumps(local))
    report = _apply(_v2(tmp_path, sub="v3", home_doc=doc, home_html="<p>v3</p>"))
    kept = {k["what"]: k for k in report["kept"]}
    assert kept[f"the app 'home' of {admin}"]["reason"] == "edited locally", report
    assert _json.loads(manifest.read_text())["title"] == "My home"


def test_a_retry_after_a_failed_update_does_not_call_moved_copies_edited(tmp_path, temp_db):
    # The update failed after the apps moved (the record and the version
    # stay old): the retry sees copies already on the new version as such,
    # never as edited locally with a copy stored beside them.
    from services.community import community_agent_updater as upd
    from storage import database as db
    _install_v1(tmp_path)
    admin = db.get_username_by_sub(ADMIN_SUB)
    with patch.object(upd, "_layer_checks", side_effect=RuntimeError("the checks step broke")):
        with pytest.raises(RuntimeError):
            _apply(_v2(tmp_path))
    report = _apply(_v2(tmp_path, sub="v2-retry"))
    assert f"the app 'home' of {admin}" not in {k["what"] for k in report["kept"]}, report
    d = _agent_dir()
    assert (d / "users" / admin / "workspace" / "apps" / "home" / "client" / "index.html").read_text() == "<p>v2</p>"


def test_the_daily_sweep_never_undoes_a_record_saved_meanwhile(tmp_path, temp_db):
    # An update that saves the new version's record while the notices go
    # out keeps it: the sweep's mark lands on the record as it is then.
    from services.community import community_agent_updater as upd
    from storage.agents import agent_store

    async def an_update_lands(*_a, **_kw):
        data = agent_store.get_community_template_data(AGENT)
        agent_store.set_community_template_data(AGENT, dict(data, version="2.0.0", saved_by="the update"))
    _install_v1(tmp_path)
    with patch("services.community.community_agents_catalog.fetch_registry",
               new=AsyncMock(return_value={"agents": [{"slug": "uptpl", "version": "2.0.0"}]})), \
            patch("services.notifications.notification_manager.fire_notification",
                  new=AsyncMock(side_effect=an_update_lands)):
        upd._last_notify_sweep = 0.0
        asyncio.run(upd.maybe_notify_updates())
    data = agent_store.get_community_template_data(AGENT)
    assert data["version"] == "2.0.0" and data["saved_by"] == "the update"


def _fake_running_scheduler(monkeypatch):
    from types import SimpleNamespace
    from services.community import community_agent_installer as inst
    from services.notifications import notification_manager
    from services.scheduler import scheduler
    tasks: list = []
    notifs: list = []
    monkeypatch.setattr(inst.config, "SCHEDULER_MODE", "embedded")
    monkeypatch.setattr(scheduler, "get_scheduler",
                        lambda: SimpleNamespace(running=True, remove_job=lambda job_id: None))
    monkeypatch.setattr(scheduler, "_register_task", tasks.append)
    monkeypatch.setattr(notification_manager, "_register_notification", notifs.append)
    monkeypatch.setattr(notification_manager, "unregister_notification", lambda nid: None)
    return tasks, notifs


def test_seeded_and_replaced_rows_reach_the_running_scheduler(tmp_path, temp_db, monkeypatch):
    # The store writes alone registered a template's tasks and notifications
    # only at the next start (a job also keeps the prompt it was registered
    # with): a notification seeded at install, and a task and a notification
    # an update rewrote, reach the running scheduler at once; a paused task
    # stays unregistered.
    from storage.pg import get_conn
    tasks, notifs = _fake_running_scheduler(monkeypatch)
    _install_v1(tmp_path)
    assert tasks == [] and [n["body"] for n in notifs] == ["b1", "b1"]   # installer + member
    with get_conn() as conn:                                  # a member resumed the task
        conn.execute("UPDATE dynamic_tasks SET enabled = TRUE WHERE agent = %s", (AGENT,))
        conn.commit()
    tasks.clear()
    notifs.clear()
    report = _apply(_v2(tmp_path))
    assert any(w.startswith("the task 'daily'") for w in report["replaced"]), report
    assert {t.prompt for t in tasks if "-daily-" in t.id} == {"p2"}
    assert {n["body"] for n in notifs} == {"b2"}


def test_an_active_template_task_is_scheduled_at_install(tmp_path, temp_db, monkeypatch):
    from storage.agents.community_agent_template_store import load_template_from_dir
    from services.community.community_agent_installer import install_from_extracted_template
    tasks, _notifs = _fake_running_scheduler(monkeypatch)
    _make_user(ADMIN_SUB, "admin@test.com", "admin")
    template = load_template_from_dir(_write_template(
        tmp_path / "act", slug="acttpl", tasks=[dict(DAILY, scope="agent", default_state="active")],
        agent_json_extra={"version": "1.0.0"}))
    p = _patches()
    with p[0], p[1], p[2], p[3]:
        asyncio.run(install_from_extracted_template(
            template=template, target_slug=AGENT, installer_user_sub=ADMIN_SUB, installer_role="admin",
            source_label="acttpl"))
    assert [t.prompt for t in tasks] == ["p1"] and tasks[0].agent == AGENT
