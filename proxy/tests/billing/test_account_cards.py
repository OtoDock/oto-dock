"""The accounts on both AI Engines cards: in the order the pool uses them, each with the sessions working on it now
and how many run each model, as the list's reader may see them.

The order is the selection's own (``pool_select._ranked``), the accounts in use first and those the list's owner keeps
out last. A person's count never shows what another person runs, and the admin's list names no other admin's email or
label. A light read serves the page's poll: the numbers, never the order.
"""

from __future__ import annotations

import pytest

from services.engines import pool_select, subscription_pool as sp
from services.engines.pool_state import (
    _session_binding_ctx,
    _session_models,
    _session_subscriptions,
)


@pytest.fixture(autouse=True)
def _clean_maps():
    yield
    for d in (_session_subscriptions, _session_binding_ctx, _session_models):
        d.clear()


def _bind(sid, sub, *, person, model=""):
    _session_subscriptions[sid] = sub
    _session_binding_ctx[sid] = ("claude-code-cli", person)
    if model:
        _session_models[sid] = model


def _row(sid, *, owner="alice", personal=True, shared=False, status="active", auth="oauth"):
    return {"id": sid, "layer": "claude-code-cli", "owner_sub": owner, "use_personal": personal,
            "contribute_platform": shared, "status": status, "auth_type": auth, "provider": "anthropic"}


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------

def _no_readings(monkeypatch, used=None):
    monkeypatch.setattr(pool_select, "_window_readings", lambda rows, now=None: {})
    monkeypatch.setattr(pool_select, "_is_throttled", lambda sid: False)
    monkeypatch.setattr(pool_select, "_scope_resting", lambda sid, scope: False)
    monkeypatch.setattr(pool_select.subscription_store, "consumption_of",
                        lambda ids, a, b: {i: (used or {}).get(i, (0.0, 0.0)) for i in ids})


def test_the_list_is_the_pools_order_in_use_first_kept_out_last(monkeypatch):
    """In use first, then the least consumed (the selection's key), and last an account the person keeps out of
    their own chats, even with a session left on it (a key cannot change under a running session)."""
    _no_readings(monkeypatch, used={"a": (5.0, 5.0), "b": (1.0, 1.0), "c": (3.0, 3.0)})
    rows = [_row("a"), _row("b"), _row("c"), _row("off", personal=False), _row("gone", status="expired")]
    order = sp.display_order(rows, sessions={"c": 1, "off": 2},
                             kept_out=lambda r: r["status"] != "active" or not r["use_personal"])
    assert [r["id"] for r in order] == ["c", "b", "a", "off", "gone"]


def test_an_account_that_rests_comes_after_the_ready_ones(monkeypatch):
    _no_readings(monkeypatch)
    monkeypatch.setattr(pool_select, "_is_throttled", lambda sid: sid == "a")
    order = sp.display_order([_row("a"), _row("b")], sessions={}, kept_out=lambda r: False)
    assert [r["id"] for r in order] == ["b", "a"]


def test_the_spawn_never_falls_through_to_a_resting_account(monkeypatch):
    """Two ready accounts with no usable credential and one resting: the spawn returns None as before, never the
    resting one (the ranking keeps the two groups apart)."""
    _no_readings(monkeypatch)
    monkeypatch.setattr(pool_select, "_is_throttled", lambda sid: sid == "rest")
    monkeypatch.setattr(pool_select.subscription_store, "get_subscription_consumption", lambda sid, since: 0.0)
    built: list[str] = []

    def _handle(row):
        built.append(row["id"])
        return sp.SubscriptionHandle(subscription_id=row["id"], layer="claude-code-cli", provider="anthropic",
                                     auth_type="oauth", api_key="", oauth_access_token="", endpoint_url="")
    monkeypatch.setattr(pool_select, "_build_handle", _handle)
    rows = [dict(_row(i), active_sessions=0) for i in ("r1", "r2", "rest")]
    assert pool_select._select(rows, allowed_auth=None) is None
    assert sorted(built) == ["r1", "r2"]


def test_the_grouped_consumption_equals_the_sums_per_account(temp_db):
    from storage.billing import subscription_store
    from storage.pg import get_conn
    with get_conn() as conn:
        for key, cost, at in (("s1", 2.0, "2026-11-12T10:00:00+00:00"), ("s1", 3.0, "2026-11-01T10:00:00+00:00"),
                              ("s2", 4.0, "2026-11-12T11:00:00+00:00")):
            conn.execute(
                """INSERT INTO usage_records (user_sub, agent, source_type, cost_usd, source_key, created_at)
                   VALUES ('u', 'a', 'chat', %s, %s, %s)""", (cost, key, at))
        conn.commit()
    short, long = "2026-11-12T00:00:00+00:00", "2026-10-30T00:00:00+00:00"
    grouped = subscription_store.consumption_of(["s1", "s2", "s3"], short, long)
    for sid in ("s1", "s2", "s3"):
        assert grouped[sid] == (subscription_store.get_subscription_consumption(sid, short),
                                subscription_store.get_subscription_consumption(sid, long))
    assert grouped["s1"] == (2.0, 5.0)


# ---------------------------------------------------------------------------
# The count and the models
# ---------------------------------------------------------------------------

def test_a_person_sees_only_their_own_sessions_on_an_account_shared_with_agents(monkeypatch):
    monkeypatch.setattr("storage.database.models_of_sessions", lambda sids: {"s-own": "claude-opus-5-5"})
    shared, personal = _row("acc-shared", shared=True), _row("acc-own")
    _bind("s-own", "acc-shared", person="alice", model="claude-fable-5-2")
    _bind("s-agent", "acc-shared", person="", model="claude-sonnet-5-5")
    _bind("s-bob", "acc-shared", person="bob", model="claude-haiku-5-5")    # a borrower's
    _bind("s-p", "acc-own", person="alice")
    _session_subscriptions["s-noctx"] = "acc-own"        # a session bound with no context: the owner's own
    seen = sp.sessions_on([shared, personal], viewer="alice", admin=False,
                          working={"s-own", "s-agent", "s-bob", "s-p", "s-noctx"})
    # The chat row's model wins over the one the session was bound with; one bound with none has "".
    assert seen == {"acc-shared": (1, ["claude-opus-5-5"]), "acc-own": (2, ["", ""])}


def test_an_account_that_stopped_being_shared_counts_only_the_persons_own(monkeypatch):
    """An admin unticks "Shared with agents" on their API key: the agents' and a borrower's sessions stay on
    it (a key cannot change under a running session). The owner's own list counts only their own."""
    monkeypatch.setattr("storage.database.models_of_sessions", lambda sids: {})
    for sid, person, model in (("s-agent", "", "claude-opus-5-5"), ("s-bob", "bob", "claude-sonnet-5-5"),
                               ("s-own", "alice", "claude-fable-5-2")):
        _bind(sid, "key", person=person, model=model)
    seen = sp.sessions_on([_row("key", shared=False, auth="api_key")], viewer="alice", admin=False,
                          working={"s-agent", "s-bob", "s-own"})
    assert seen == {"key": (1, ["claude-fable-5-2"])}


def test_the_admin_sees_the_pool_but_not_another_admins_own_chats(monkeypatch):
    monkeypatch.setattr("storage.database.models_of_sessions", lambda sids: {})
    theirs = _row("acc-b", owner="bob", shared=True)
    mine = _row("acc-a", owner="alice", shared=True)
    _bind("s-bob", "acc-b", person="bob", model="claude-opus-5-5")
    _bind("s-agent", "acc-b", person="", model="claude-sonnet-5-5")
    _bind("s-carol", "acc-b", person="carol", model="claude-haiku-5-5")
    _bind("s-alice", "acc-a", person="alice")
    seen = sp.sessions_on([theirs, mine], viewer="alice", admin=True,
                          working={"s-bob", "s-agent", "s-carol", "s-alice"})
    assert seen == {"acc-b": (2, ["claude-sonnet-5-5", "claude-haiku-5-5"]), "acc-a": (1, [""])}


def test_only_the_sessions_working_now_are_running_and_the_bound_count_stays(monkeypatch):
    """Three sessions bound to the account, one working: the card runs one, and the order still reads three."""
    asked = []
    monkeypatch.setattr("storage.database.models_of_sessions",
                        lambda sids: asked.append(sorted(sids)) or {"s-busy": "claude-opus-5-5"})
    for sid in ("s-busy", "s-idle", "s-idle2"):
        _bind(sid, "acc", person="alice", model="claude-fable-5-2")
    assert sp.sessions_on([_row("acc")], viewer="alice", admin=False, working={"s-busy"}) == {
        "acc": (3, ["claude-opus-5-5"])}
    assert sp.sessions_on([_row("acc")], viewer="alice", admin=False, working=set()) == {"acc": (3, [])}
    assert asked == [["s-busy"]]             # only the working sessions' models are read


def test_the_models_come_from_the_chat_rows_in_one_query(temp_db):
    from storage import database as task_store
    temp_db.create_chat("c-m1", "alice", "a1")
    task_store.update_chat("c-m1", session_id="s-m1", model="claude-opus-5-5")
    assert task_store.models_of_sessions(["s-m1", "s-none"]) == {"s-m1": "claude-opus-5-5"}


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

def _client(sub, role):
    from fastapi.testclient import TestClient

    from app import app
    from auth.providers import UserContext, get_current_user

    async def _who():
        return UserContext(sub=sub, email=f"{sub}@t.com", name=sub, role=role)
    app.dependency_overrides[get_current_user] = _who
    return TestClient(app), lambda: app.dependency_overrides.pop(get_current_user, None)


def _oauth(owner, *, shared=False, personal=True, label="Claude Max", email=""):
    from storage.billing import subscription_store
    return subscription_store.add_subscription(
        "claude-code-cli", "anthropic", "oauth", owner_sub=owner, use_personal=personal,
        contribute_platform=shared, label=label, credential_data={"oauth_token": {"accessToken": ""}},
        oauth_email=email or f"{owner}@example.com")


def test_the_person_s_list_comes_in_the_pools_order_with_its_count(temp_db, monkeypatch):
    monkeypatch.setattr("storage.database.models_of_sessions", lambda sids: {})
    first = _oauth("local:m1", email="one@example.com")
    used = _oauth("local:m1", email="two@example.com")
    off = _oauth("local:m1", personal=False, email="three@example.com")
    _bind("s-1", used["id"], person="local:m1", model="claude-fable-5-2")
    monkeypatch.setattr("api.admin.execution_layers._working_now", lambda: {"s-1"})
    client, done = _client("local:m1", "member")
    try:
        layer = next(l for l in client.get("/v1/users/me/execution-layers").json()["layers"]
                     if l["name"] == "claude-code-cli")
        live = client.get("/v1/users/me/execution-layers/live").json()["accounts"]
    finally:
        done()
    rows = layer["user_subscriptions"]
    assert [r["id"] for r in rows] == [used["id"], first["id"], off["id"]]
    assert rows[0]["live"] == {"sessions": 1, "per_model": [{"name": "Fable 5.2", "sessions": 1}]}
    assert rows[1]["live"] == {"sessions": 0, "per_model": []}
    assert live[used["id"]]["live"] == {"sessions": 1, "per_model": [{"name": "Fable 5.2", "sessions": 1}]}
    assert set(live) == {first["id"], used["id"], off["id"]}


def test_the_persons_poll_counts_only_their_own_sessions(temp_db, monkeypatch):
    """The poll of a person's list keeps the list's rules: on their account shared with agents, the agents'
    work and another person's borrowed session are not theirs to count."""
    monkeypatch.setattr("storage.database.models_of_sessions", lambda sids: {})
    shared = _oauth("local:m1", shared=True, email="shared@example.com")
    _bind("s-own", shared["id"], person="local:m1", model="claude-fable-5-2")
    _bind("s-agent", shared["id"], person="", model="claude-opus-5-5")
    _bind("s-other", shared["id"], person="local:m2", model="claude-sonnet-5-5")
    monkeypatch.setattr("api.admin.execution_layers._working_now", lambda: {"s-own", "s-agent", "s-other"})
    client, done = _client("local:m1", "member")
    try:
        live = client.get("/v1/users/me/execution-layers/live").json()["accounts"]
    finally:
        done()
    assert live[shared["id"]]["live"] == {"sessions": 1, "per_model": [{"name": "Fable 5.2", "sessions": 1}]}


def test_the_count_per_model_most_first_and_an_idle_account_keeps_its_place(temp_db, monkeypatch):
    """Look A's numbers: the working sessions by model, most first, one with no model counted and not named.
    An account whose sessions are all idle shows none running and keeps its place in the order."""
    monkeypatch.setattr("storage.database.models_of_sessions", lambda sids: {})
    empty = _oauth("local:m1", email="empty@example.com")
    busy = _oauth("local:m1", email="busy@example.com")
    idle = _oauth("local:m1", email="idle@example.com")
    for sid, model in (("o1", "claude-opus-5-5"), ("o2", "claude-opus-5-5"), ("f1", "claude-fable-5-2"),
                       ("n1", ""), ("o-idle", "claude-opus-5-5")):
        _bind(sid, busy["id"], person="local:m1", model=model)
    for sid in ("i1", "i2", "i3", "i4", "i5", "i6"):
        _bind(sid, idle["id"], person="local:m1", model="claude-opus-5-5")
    monkeypatch.setattr("api.admin.execution_layers._working_now", lambda: {"o1", "o2", "f1", "n1"})
    client, done = _client("local:m1", "member")
    try:
        layer = next(l for l in client.get("/v1/users/me/execution-layers").json()["layers"]
                     if l["name"] == "claude-code-cli")
    finally:
        done()
    rows = {r["id"]: r for r in layer["user_subscriptions"]}
    assert rows[busy["id"]]["live"] == {"sessions": 4, "per_model": [
        {"name": "Opus 5.5", "sessions": 2}, {"name": "Fable 5.2", "sessions": 1}]}
    assert rows[idle["id"]]["live"] == {"sessions": 0, "per_model": []}
    order = [r["id"] for r in layer["user_subscriptions"]]
    # The bound sessions order the cards: the idle account is in use, before the empty one.
    assert order.index(idle["id"]) < order.index(empty["id"]) and order.index(busy["id"]) < order.index(empty["id"])


def test_the_admin_list_names_no_other_admins_email_or_label(temp_db, monkeypatch):
    monkeypatch.setattr("storage.database.models_of_sessions", lambda sids: {})
    mine = _oauth("local:admin", shared=True, label="My Max")
    theirs = _oauth("local:admin2", shared=True, label="Bob's Max")
    client, done = _client("local:admin", "admin")
    try:
        layers = client.get("/v1/admin/execution-layers").json()["layers"]
        rows = {s["id"]: s for l in layers for s in l["subscriptions"]["platform"]}
        per_layer = {s["id"]: s for s in client.get(
            "/v1/admin/execution-layers/claude-code-cli/subscriptions").json()["subscriptions"]}
        live = client.get("/v1/admin/execution-layers/live").json()["accounts"]
    finally:
        done()
    for listed in (rows, per_layer):
        assert (listed[mine["id"]]["oauth_email"], listed[mine["id"]]["label"]) == (
            "local:admin@example.com", "My Max")
        assert (listed[theirs["id"]]["oauth_email"], listed[theirs["id"]]["label"]) == ("", "")
    assert "local:admin2@example.com" not in str(live) and "Bob's Max" not in str(live)


def test_only_an_admin_reads_the_pools_live_numbers(temp_db):
    client, done = _client("local:m1", "member")
    try:
        assert client.get("/v1/admin/execution-layers/live").status_code == 403
        assert client.get("/v1/users/me/execution-layers/live").status_code == 200
    finally:
        done()


# ---------------------------------------------------------------------------
# Working now: the one reading the count, the snapshot and the Active-now list share
# ---------------------------------------------------------------------------

@pytest.fixture
def _live(monkeypatch):
    """The chats' live state: headless states, terminals and sub-agent registries, cleared after."""
    from types import SimpleNamespace

    from core.events.bg_command_state import get_bg_command_registry
    from core.session import interactive_session, session_state
    from core.session.session_state import _chat_streaming_state, get_subagent_registry
    terminals: list = []
    monkeypatch.setattr(interactive_session, "live_sessions", lambda: list(terminals))

    def headless(cid, sid, *, streaming):
        _chat_streaming_state[cid] = {"session_id": sid, "streaming": streaming}

    def terminal(cid, sid, *, turn_open):
        terminals.append(SimpleNamespace(chat_id=cid, session_id=sid, _turn_open=turn_open, alive=True))

    def sub_agent(sid):
        get_subagent_registry(sid).register_spawn(f"t-{sid}", f"t-{sid}")

    def bg_command(sid):
        get_bg_command_registry(sid).register_spawn(f"b-{sid}", f"b-{sid}")
    yield SimpleNamespace(headless=headless, terminal=terminal, sub_agent=sub_agent, bg_command=bg_command)
    for cid in ("c-h", "c-h2", "c-left", "c-cmd", "c-t", "c-t-idle", "c-t-sub"):
        _chat_streaming_state.pop(cid, None)
    from core.events import bg_command_state
    for sid in ("s-h", "s-h2", "s-left", "s-cmd", "s-t", "s-t-idle", "s-t-sub"):
        session_state._subagent_registries.pop(sid, None)
        bg_command_state._bg_command_registries.pop(sid, None)


def test_working_now_counts_a_turn_and_a_pending_sub_agent_not_a_bare_command(_live):
    from services.delegation import lane_status
    _live.headless("c-h", "s-h", streaming=True)
    _live.headless("c-left", "s-left", streaming=False)     # its turn ended, a sub-agent still runs
    _live.sub_agent("s-left")
    _live.headless("c-cmd", "s-cmd", streaming=False)       # its turn ended, a background command still runs
    _live.bg_command("s-cmd")
    _live.terminal("c-t", "s-t", turn_open=True)
    _live.terminal("c-t-idle", "s-t-idle", turn_open=False)
    _live.terminal("c-t-sub", "s-t-sub", turn_open=False)
    _live.sub_agent("s-t-sub")
    assert lane_status.working_sessions() == {"s-h": "c-h", "s-t": "c-t"}
    assert lane_status.working_sessions(sub_agents=True) == {
        "s-h": "c-h", "s-left": "c-left", "s-t": "c-t", "s-t-sub": "c-t-sub"}


def test_the_chats_working_now_come_once_headless_first(_live):
    from services.delegation import lane_status
    _live.terminal("c-t", "s-t", turn_open=True)
    _live.headless("c-h", "s-h", streaming=True)
    _live.headless("c-h2", "s-h2", streaming=True)
    _live.terminal("c-h", "s-h-term", turn_open=True)       # the same chat twice: once
    assert lane_status.working_chat_ids() == ["c-h", "c-h2", "c-t"]
