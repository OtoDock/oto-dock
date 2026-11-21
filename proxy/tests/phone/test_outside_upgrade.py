"""The first boot of 2.0 with outside callers: the callers' rows from disk,
one word to the admins and to each agent's managers, once."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from core.session import external_identity
from services.phone import outside_upgrade
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import external_identity_store
from storage.pg import get_conn
from storage.phone import phone_route_store


def _server() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO phone_servers (name, adapter_type, host, created_at, updated_at) "
            "VALUES (%s, 'asterisk_manual', '', NOW()::text, NOW()::text) RETURNING id",
            (f"pbx-{uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.commit()
    return row["id"]


def _route(agent: str, **extra) -> dict:
    return phone_route_store.create_route({
        "direction": "inbound", "name": f"line-{uuid.uuid4().hex[:6]}", "agent": agent,
        "phone_server_id": _server(), "audiosocket_uuid": str(uuid.uuid4()), **extra,
    })


@pytest.fixture
def agents(temp_db):
    for slug in ("ext-up-a", "ext-up-b", "ext-up-c", "ext-up-d"):
        if not agent_store.agent_exists(slug):
            agent_store.create_agent(slug, slug.replace("ext-up-", "Line ").title())
    return ("ext-up-a", "ext-up-b", "ext-up-c", "ext-up-d")


@pytest.mark.asyncio
async def test_the_first_boot_records_the_callers_and_tells_once(agents, monkeypatch):
    import config
    a, b, c, d = agents
    _route(a)                                   # caller mode, enabled: told
    _route(b, identity_mode="shared")           # shared mode: an outside caller too
    _route(c, enabled=False)                    # off: not told
    _route(d, identity_mode="user")             # a user's own line: not told
    home = config.get_agent_dir(a) / external_identity.EXTERNALS_DIRNAME / "phone" / "302101234567"
    (home / "context").mkdir(parents=True)
    fired = AsyncMock(return_value=[])
    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fired)

    done = await outside_upgrade.first_boot()

    assert done == "1 caller rows recorded, 2 agents with an outside line told"
    assert external_identity_store.get(a, "phone", "302101234567") is not None
    scopes = [(k["scope"], k.get("target")) for _, k in [(c_.args, c_.kwargs) for c_ in fired.call_args_list]]
    assert scopes == [("admin", None), ("agent", a), ("agent", b)]
    admin_body = fired.call_args_list[0].kwargs["body"]
    assert "Line A" in admin_body and "Line B" in admin_body and "Line C" not in admin_body
    manager = fired.call_args_list[1].kwargs
    assert manager["audience"] == "manager" and manager["href"] == f"/agents/{a}/config#external-identities"
    assert "Agent Settings, Configuration, External identities" in manager["body"]
    assert "Outside people" not in manager["body"] + manager["title"]
    assert task_store.get_platform_setting(outside_upgrade.FLAG) == "1"
    # Once: the second boot says and does nothing.
    fired.reset_mock()
    assert await outside_upgrade.first_boot() is None
    fired.assert_not_called()


@pytest.mark.asyncio
async def test_a_fresh_install_sets_the_flag_in_silence(temp_db, monkeypatch):
    fired = AsyncMock(return_value=[])
    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fired)
    assert await outside_upgrade.first_boot() is None
    fired.assert_not_called()
    assert task_store.get_platform_setting(outside_upgrade.FLAG) == "1"
