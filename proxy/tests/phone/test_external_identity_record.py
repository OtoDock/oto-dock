"""The outside person's row around a call: touched at warmup for a caller
with an id and a tree, never for the rest, gone with the tree."""

from __future__ import annotations

import uuid

import pytest

from core.session import external_identity
from services.infra import external_retention
from services.phone.phone_identity import RouteIdentity
from storage.agents import agent_store
from storage.identity import external_identity_store
from ws import phone as ws_phone

AGENT = "ext-record"
SID = str(uuid.uuid4())


@pytest.fixture
def agent(temp_db):
    if not agent_store.agent_exists(AGENT):
        agent_store.create_agent(AGENT, "Ext Record")
    return AGENT


def _identity(phone="+302101234567", **kw) -> RouteIdentity:
    ident = external_identity.resolve(external_identity.PHONE, phone, session_id=SID, **kw)
    return RouteIdentity(mode="caller", role="external", external=ident)


def test_a_caller_with_a_tree_is_touched(agent):
    ident_id = ws_phone._touch_identity(_identity(verified=True), agent)
    row = external_identity_store.get(agent, "phone", "302101234567")
    assert row and row["id"] == ident_id and row["verified_at"] and row["conversations"] == 1


@pytest.mark.parametrize("identity", [
    None,
    _identity(phone="anonymous"),                       # withheld: an ephemeral tree
    _identity(remember=False),                          # the line keeps no callers
    RouteIdentity(mode="caller", role="external",
                  external=external_identity.resolve(
                      external_identity.PHONE, "+3021", session_id=SID, shared=True)),
    RouteIdentity(mode="user", role="external", external=None, user={"sub": "u1"}),
])
def test_the_rest_get_no_row(agent, identity):
    assert ws_phone._touch_identity(identity, agent) == ""
    assert external_identity_store.list_for_agent(agent) == []


def test_the_row_goes_with_the_tree(agent, monkeypatch):
    import config
    ws_phone._touch_identity(_identity(), agent)
    home = config.get_agent_dir(agent) / external_identity.EXTERNALS_DIRNAME / "phone" / "302101234567"
    (home / "context").mkdir(parents=True, exist_ok=True)
    stats: dict = {"errors": 0, "caller_bytes_freed": 0}
    assert external_retention.remove_home(agent, home, stats, dry_run=False) is True
    assert not home.exists()
    assert external_identity_store.get(agent, "phone", "302101234567") is None
