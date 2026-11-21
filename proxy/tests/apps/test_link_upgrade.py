"""The first 2.0 start of an install with shared links: one notice for the
admins naming every app with a live link, one per agent for its managers,
one per owner of a personal app, the links an app's folders now stop named
on their own line; once, and silent on an install with no link."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from services.apps import link_upgrade
from storage import database as task_store
from storage.agents import agent_store
from storage.sharing import share_store


def _link(app: dict) -> None:
    share_store.create_external_share(target_kind="app", target_id=app["id"], created_by="user-admin",
                                      token_hash=f"hash-{app['id']}", password_hash="", public=True,
                                      allow_actions=False, expires_at=None)


@pytest.mark.asyncio
async def test_every_app_with_a_live_link_is_named_once(temp_db, monkeypatch):
    agent_store.create_agent("links-a", "Links A", created_by="user-admin")
    agent_store.create_agent("links-b", "Links B", created_by="user-admin")
    board = task_store.upsert_app("links-a", "", None, "board", title="Team board",
                                  rel_path="workspace/apps/board", kind="folder")
    reports = task_store.upsert_app("links-a", "", None, "reports", title="Reports",
                                    rel_path="workspace/apps/reports", kind="folder",
                                    blocks={"files": json.dumps({"read": ["workspace/reports"]})})
    task_store.upsert_user("user-member", "member@test.com", "Member", "member")
    mine = task_store.upsert_app("links-b", "member", "user-member", "mine", title="My page",
                                 rel_path="users/member/workspace/apps/mine")
    task_store.upsert_app("links-b", "", None, "quiet", title="No link", rel_path="workspace/apps/quiet")
    for app in (board, reports, mine):
        _link(app)
    fired = AsyncMock(return_value=[])
    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fired)
    assert await link_upgrade.first_boot() == "3 apps with a shared link told"
    calls = [c.kwargs for c in fired.call_args_list]
    assert [(c["scope"], c.get("target")) for c in calls] == [
        ("admin", None), ("agent", "links-a"), ("user", "user-member")]
    admin = calls[0]["body"]
    assert "Team board" in admin and "Reports" in admin and "My page" in admin and "No link" not in admin
    assert "external identities" in admin and "The links of these apps no longer open" in admin
    # The admins' list spans agents: each app with its agent. A manager's
    # list is one agent's, and both say where a knowledge folder is added.
    assert "Team board on Links A" in admin and "Reports on Links A" in admin
    assert "Team board on" not in calls[1]["body"]
    assert "Configuration, External identities" in admin
    assert calls[0]["title"] == link_upgrade.NOTICE_TITLE
    assert calls[1]["audience"] == "manager" and "My page" not in calls[1]["body"]
    assert "My page" in calls[2]["body"] and "Team board" not in calls[2]["body"]
    for c in calls:
        assert ";" not in c["body"] and "—" not in c["body"]
    assert task_store.get_platform_setting(link_upgrade.FLAG) == "1"
    fired.reset_mock()
    assert await link_upgrade.first_boot() is None
    fired.assert_not_called()


@pytest.mark.asyncio
async def test_an_install_with_no_link_is_told_nothing(temp_db, monkeypatch):
    fired = AsyncMock(return_value=[])
    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fired)
    assert await link_upgrade.first_boot() is None
    fired.assert_not_called()
    assert task_store.get_platform_setting(link_upgrade.FLAG) == "1"


def test_the_boot_runs_the_step():
    import inspect

    import startup
    assert "link_upgrade.first_boot" in inspect.getsource(startup._boot_continue)
