"""Who pays for a session (``core/config/spender.py``): the rule as a table,
the standing check, what each source hands the rule, and the one call to
the pool.

A delegated chat spends exactly what its person's own chat on that agent
would spend; agent-scope tasks, any task on a Shared-only agent and runs
with no person spend the subscriptions shared with agents. A person who can
no longer use the agent is refused, never moved to the shared ones.

Run: cd proxy && venv/bin/pytest tests/billing/test_spender.py -v
"""

from types import SimpleNamespace

import pytest

from auth import roles
from core.config import spender
from core.config.spender import Kind
from services.engines import subscription_pool as sp
from services.scheduler import task_kinds
from storage import database as task_store
from storage.agents import agent_store

PERSONAL, COLLAB, SHARED = "sp-personal", "sp-collab", "sp-shared"
ALICE, BOB, ROOT = "local:alice", "local:bob", "local:root"


@pytest.fixture(autouse=True)
def _agents_and_people():
    agent_store.create_agent(PERSONAL, "Personal", default_scope="user", collaborative=False)
    agent_store.create_agent(COLLAB, "Collab", default_scope="user", collaborative=True)
    agent_store.create_agent(SHARED, "Shared", default_scope="agent", collaborative=False)
    task_store.upsert_user(ALICE, "alice@x.test", "Alice", "member")
    task_store.upsert_user(BOB, "bob@x.test", "Bob", "member")
    task_store.upsert_user(ROOT, "root@x.test", "Root", "admin")
    for agent in (PERSONAL, COLLAB, SHARED):
        task_store.add_user_agent(ALICE, agent, roles.EDITOR, ROOT)
        task_store.add_user_agent(BOB, agent, roles.CONTRIBUTOR, ROOT)
    yield
    with sp._session_maps_lock:
        sp._session_subscriptions.clear()
        sp._session_binding_ctx.clear()
        sp._session_scope_keys.clear()


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

_AGENTS = (PERSONAL, COLLAB, SHARED)

# (kind, scope, route, {agent: expected payer or None for the pool})
_TABLE = [
    (Kind.CHAT, "user", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.CHAT, "agent", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.CHAT_LANE, "user", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.CHAT_LANE, "agent", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.TASK, "user", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: None}),
    (Kind.TASK, "agent", False, {PERSONAL: None, COLLAB: None, SHARED: None}),
    (Kind.JUDGE, "user", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.JUDGE, "agent", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.MEETING, "user", False, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.MEETING, "agent", False, {PERSONAL: None, COLLAB: None, SHARED: None}),
    (Kind.CALL, "user", True, {PERSONAL: ALICE, COLLAB: ALICE, SHARED: ALICE}),
    (Kind.CALL, "agent", False, {PERSONAL: None, COLLAB: None, SHARED: None}),
]


@pytest.mark.parametrize("kind,scope,route,expected", _TABLE,
                         ids=[f"{k.value}-{s}" for k, s, _r, _e in _TABLE])
def test_the_rule_on_every_kind_of_agent(kind, scope, route, expected):
    for agent in _AGENTS:
        got = spender.session_spender(agent, person=ALICE, kind=kind, scope=scope, route=route)
        assert got.sub == expected[agent], (kind, scope, agent, got)
        assert not got.refused


@pytest.mark.parametrize("kind", list(Kind))
def test_no_person_spends_the_shared_subscriptions(kind):
    for agent in _AGENTS:
        for scope in ("user", "agent"):
            got = spender.session_spender(agent, person=None, kind=kind, scope=scope, route=True)
            assert got == spender.Spender(None, "no-person")


def test_bucket_follows_the_payer_not_the_scope():
    # A person's own task on a Shared-only agent is user scope, paid by the
    # shared subscriptions: the agent's bucket.
    assert spender.bucket(spender.Spender(None, "agent-task"), "user") == "agent"
    # A person-paid lane keeps its own scope: an agent-scope row the pump's
    # writer moves to the session's payer.
    assert spender.bucket(spender.Spender(ALICE, "chat-lane"), "agent") == "agent"
    assert spender.bucket(spender.Spender(ALICE, "task"), "user") == "user"


def test_a_re_adopted_runs_bucket_reads_its_row():
    task_store.create_run("r-own", "dyn-own", SHARED, "manual", None, "go", "scheduled", "user", ALICE)
    task_store.create_run("r-col", "dyn-col", COLLAB, "manual", None, "go", "scheduled", "user", ALICE)
    assert spender.run_bucket("r-own") == "agent"
    assert spender.run_bucket("r-col") == "user"
    assert spender.run_bucket("r-gone") == "agent"


# ---------------------------------------------------------------------------
# Standing
# ---------------------------------------------------------------------------

class TestMaySpend:
    def test_a_contributor_pays_for_work_in_their_own_space(self):
        assert spender.may_spend(BOB, COLLAB, as_agent=False)

    def test_a_contributor_cannot_pay_for_work_that_runs_as_the_agent(self):
        assert not spender.may_spend(BOB, COLLAB, as_agent=True)
        assert not spender.may_spend(BOB, SHARED, as_agent=True)

    def test_an_editor_pays_for_work_that_runs_as_the_agent(self):
        assert spender.may_spend(ALICE, SHARED, as_agent=True)

    def test_a_platform_admin_with_no_agent_row(self):
        assert spender.may_spend(ROOT, SHARED, as_agent=True)

    def test_a_person_without_the_agent_or_gone(self):
        task_store.upsert_user("local:carol", "carol@x.test", "Carol", "member")
        assert not spender.may_spend("local:carol", COLLAB, as_agent=False)
        assert not spender.may_spend("local:nobody", COLLAB, as_agent=False)
        assert not spender.may_spend("", COLLAB, as_agent=False)


def _lane(agent, *, scope="agent", spender_sub=ALICE, created_by=ALICE, chat="chat-lane-1",
          task_type=task_kinds.DELEGATE):
    return SimpleNamespace(agent=agent, scope=scope, created_by=created_by, task_type=task_type,
                           spender_sub=spender_sub, target_chat_id=chat, judge=None)


class TestRunSpender:
    def test_a_shared_only_chat_lane_pays_as_its_person(self):
        got = spender.run_spender(_lane(SHARED))
        assert (got.sub, got.row, got.refused) == (ALICE, "chat-lane", "")

    def test_an_agent_scope_chat_lane_pays_as_its_person(self):
        assert spender.run_spender(_lane(COLLAB)).sub == ALICE

    def test_a_task_lane_that_runs_as_the_agent_is_the_pool(self):
        for agent in (COLLAB, SHARED):
            got = spender.run_spender(_lane(agent, chat=None))
            assert got.sub is None and got.row == "agent-task"

    def test_a_task_lane_with_a_task_chat_is_a_task(self):
        assert spender.run_kind(_lane(SHARED, chat="task-run-1")) is Kind.TASK

    def test_a_persons_own_task_on_a_shared_only_agent_is_the_pool(self):
        task = SimpleNamespace(agent=SHARED, scope="user", created_by=ALICE, task_type="scheduled",
                               spender_sub=None, target_chat_id=None, judge=None)
        assert spender.run_spender(task).sub is None

    def test_a_persons_own_task_elsewhere_is_theirs(self):
        task = SimpleNamespace(agent=COLLAB, scope="user", created_by=ALICE, task_type="scheduled",
                               spender_sub=None, target_chat_id=None, judge=None)
        assert spender.run_spender(task).sub == ALICE

    def test_the_recorded_pool_wins_over_a_person_creator(self):
        assert spender.run_spender(_lane(SHARED, spender_sub="")).sub is None

    def test_an_old_lane_row_reads_its_person_creator(self):
        assert spender.run_spender(_lane(SHARED, spender_sub=None)).sub == ALICE

    def test_an_old_lane_row_by_an_agent_is_the_pool(self):
        assert spender.run_spender(_lane(SHARED, spender_sub=None, created_by=COLLAB)).sub is None

    def test_an_agent_slug_equal_to_a_users_id_never_pays_as_that_user(self):
        task_store.upsert_user(COLLAB, "slug@x.test", "Slug", "member")
        task_store.add_user_agent(COLLAB, SHARED, roles.EDITOR, ROOT)
        assert spender.run_spender(_lane(SHARED, spender_sub=None, created_by=COLLAB)).sub is None

    def test_a_judge_pays_as_the_turn_it_judges(self):
        judge = SimpleNamespace(agent=SHARED, scope="agent", created_by=None, task_type=task_kinds.CHECK,
                                spender_sub=ALICE, target_chat_id=None, judge={"name": "c"})
        got = spender.run_spender(judge)
        assert (got.sub, got.row) == (ALICE, "judge")

    def test_a_judge_of_the_agents_own_work_is_the_pool(self):
        judge = SimpleNamespace(agent=SHARED, scope="agent", created_by=None, task_type=task_kinds.CHECK,
                                spender_sub="", target_chat_id=None, judge={"name": "c"})
        assert spender.run_spender(judge).sub is None


class TestRefusedNeverPool:
    def test_a_check_of_a_contributors_own_turn_on_a_shared_only_agent_is_paid_like_it(self):
        """A judge is paid like the turn it judges: that turn took its own
        rule (a person's chat takes no editor tier), so the judge takes none
        either. The person must still exist and hold the agent."""
        assert spender.chat_spender(task_store.create_chat("c-bob", BOB, SHARED), BOB).sub == BOB
        judge = SimpleNamespace(agent=SHARED, scope="agent", created_by=BOB, task_type=task_kinds.CHECK,
                                spender_sub=BOB, target_chat_id=None, judge={"check": "c"})
        got = spender.run_spender(judge)
        assert (got.sub, got.refused) == (BOB, "")

    def test_a_check_of_a_person_who_lost_the_agent_is_refused(self):
        judge = SimpleNamespace(agent=SHARED, scope="agent", created_by=BOB, task_type=task_kinds.CHECK,
                                spender_sub=BOB, target_chat_id=None, judge={"check": "c"})
        task_store.set_user_agents(BOB, [], ROOT)
        assert spender.run_spender(judge).refused

    def test_a_person_removed_from_the_agent_is_refused(self):
        task_store.set_user_agents(ALICE, [], ROOT)
        got = spender.run_spender(_lane(SHARED))
        assert got.sub == ALICE and "cannot run work on sp-shared now" in got.refused
        assert "does not move to the subscriptions shared with agents" in got.refused

    def test_a_person_deleted_is_refused(self):
        task_store.delete_user(ALICE)
        assert spender.run_spender(_lane(COLLAB)).refused

    def test_a_person_below_editor_on_a_lane_that_runs_as_the_agent_is_refused(self):
        assert spender.run_spender(_lane(COLLAB, spender_sub=BOB)).refused
        assert spender.run_spender(_lane(SHARED, spender_sub=BOB)).refused

    def test_a_contributor_on_their_own_user_scope_lane_holds(self):
        assert not spender.run_spender(_lane(COLLAB, scope="user", spender_sub=BOB)).refused

    def test_the_pool_is_never_asked_for_a_refused_spender(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sp, "resolve_subscription_env", lambda *a, **k: calls.append(a) or ("s", {}))
        task_store.set_user_agents(ALICE, [], ROOT)
        with pytest.raises(sp.NoSubscriptionError) as e:
            spender.spend_env(spender.run_spender(_lane(SHARED)), "claude-code-cli", agent=SHARED, lane=True)
        assert "cannot run work on" in str(e.value) and calls == []


# ---------------------------------------------------------------------------
# A chat, a session with no person, a meeting
# ---------------------------------------------------------------------------

def _run(run_id, agent, *, task_type, scope, created_by, spender_sub, chat_id, started="2026-11-13T06:00:00"):
    task_store.create_run(run_id, "task-x", agent, "manual", None, "go", task_type, scope, created_by,
                          spender_sub=spender_sub)
    task_store.update_run(run_id, chat_id=chat_id, started_at=started)


class TestChatSpender:
    def test_a_persons_own_turn(self):
        chat = task_store.create_chat("c-own", ALICE, SHARED)
        assert spender.chat_spender(chat, ALICE) == spender.Spender(ALICE, "chat")

    def test_a_task_chat_pays_as_its_run(self):
        _run("r-task", SHARED, task_type="scheduled", scope="agent", created_by=ALICE,
             spender_sub=None, chat_id="task-r-task")
        chat = task_store.create_chat("task-r-task", "agent::" + SHARED, SHARED)
        assert spender.chat_spender(chat, None).sub is None

    def test_a_lane_chat_woken_with_nobody_asking_pays_as_its_newest_round(self):
        chat = task_store.create_chat("c-lane", "agent::" + SHARED, SHARED)
        _run("r-1", SHARED, task_type=task_kinds.DELEGATE, scope="agent", created_by=BOB,
             spender_sub=BOB, chat_id="c-lane", started="2026-11-13T05:00:00")
        _run("r-2", SHARED, task_type=task_kinds.DELEGATE, scope="agent", created_by=ALICE,
             spender_sub=ALICE, chat_id="c-lane", started="2026-11-13T06:00:00")
        got = spender.chat_spender(chat, None)
        assert (got.sub, got.row, got.refused) == (ALICE, "chat-lane", "")

    def test_a_lane_chat_a_person_types_into_is_their_turn(self):
        chat = task_store.create_chat("c-lane2", "agent::" + SHARED, SHARED)
        _run("r-3", SHARED, task_type=task_kinds.DELEGATE, scope="agent", created_by=ALICE,
             spender_sub=ALICE, chat_id="c-lane2")
        assert spender.chat_spender(chat, BOB) == spender.Spender(BOB, "chat")


class TestAskerSpender:
    def test_a_lane_session_passes_its_person_on(self):
        task_store.create_chat("c-l", "agent::" + SHARED, SHARED)
        task_store.update_chat("c-l", session_id="sess-lane")
        _run("r-l", SHARED, task_type=task_kinds.DELEGATE, scope="agent", created_by=ALICE,
             spender_sub=ALICE, chat_id="c-l")
        assert spender.asker_spender("sess-lane", SHARED).sub == ALICE
        assert spender.spawn_spender(None, "sess-lane", SHARED).sub == ALICE

    def test_an_agent_scope_task_session_passes_the_pool_on(self):
        task_store.create_chat("task-r-a", "agent::" + SHARED, SHARED)
        task_store.update_chat("task-r-a", session_id="sess-task")
        _run("r-a", SHARED, task_type="scheduled", scope="agent", created_by=None,
             spender_sub=None, chat_id="task-r-a")
        assert spender.asker_spender("sess-task", SHARED).sub is None

    def test_a_shared_only_participant_of_a_persons_meeting_passes_its_payer_on(self):
        sp.bind_session("sess-meet", "sub-1", layer="claude-code-cli", user_sub=ALICE)
        got = spender.asker_spender("sess-meet", SHARED)
        assert (got.sub, got.row) == (ALICE, "asker-binding")

    def test_the_agents_own_line_passes_the_pool_on(self):
        assert spender.asker_spender("sess-none", SHARED) == spender.POOL
        assert spender.asker_spender("", SHARED) == spender.POOL

    def test_the_tokens_own_person_wins(self):
        assert spender.spawn_spender(BOB, "sess-none", SHARED) == spender.Spender(BOB, "spawn-person")


class TestMeetingSpender:
    def test_a_meeting_a_person_starts_stays_theirs(self):
        got = spender.meeting_spender({"scope": "user", "created_by": ALICE}, SHARED)
        assert got.sub == ALICE and not got.refused

    def test_an_agent_scope_meeting_is_the_agents(self):
        assert spender.meeting_spender({"scope": "agent", "created_by": ALICE}, SHARED).sub is None

    def test_a_meeting_of_a_person_who_lost_the_participant_is_refused(self):
        task_store.set_user_agents(ALICE, [], ROOT)
        assert spender.meeting_spender({"scope": "user", "created_by": ALICE}, SHARED).refused


# ---------------------------------------------------------------------------
# The pool, through this one door
# ---------------------------------------------------------------------------

class TestSpendEnv:
    @pytest.fixture
    def pool(self, monkeypatch):
        calls = []
        accounts = {}

        def _resolve(layer, user_sub, *, model="", agent_info=None, sticky_scope=""):
            calls.append((layer, user_sub, model, sticky_scope))
            sub = accounts.get(user_sub, "")
            return (sub, {"K": "v"}) if sub else ("", {})
        monkeypatch.setattr(sp, "resolve_subscription_env", _resolve)
        monkeypatch.setattr(sp, "user_scope_block_reason", lambda layer, person: "none")
        return SimpleNamespace(calls=calls, accounts=accounts)

    def test_the_person_draws_on_their_own_accounts(self, pool):
        pool.accounts[ALICE] = "sub-alice"
        got = spender.spend_env(spender.Spender(ALICE, "chat-lane"), "claude-code-cli",
                                model="m", sticky_scope="k")
        assert got == spender.Spent("sub-alice", {"K": "v"}, ALICE)
        assert pool.calls == [("claude-code-cli", ALICE, "m", "k")]

    def test_the_pool_binds_nobody(self, pool):
        pool.accounts[None] = "sub-pool"
        assert spender.spend_env(spender.POOL, "claude-code-cli").bound_sub == ""

    def test_a_person_with_nothing_is_refused_never_the_pool(self, pool):
        pool.accounts[None] = "sub-pool"
        with pytest.raises(sp.NoSubscriptionError) as e:
            spender.spend_env(spender.Spender(ALICE, "chat-lane"), "claude-code-cli", agent=SHARED, lane=True)
        assert "as their own chat on sp-shared would" in str(e.value)
        assert [c[1] for c in pool.calls] == [ALICE]

    def test_a_call_to_a_persons_route_falls_back_and_binds_nobody(self, pool):
        pool.accounts[None] = "sub-pool"
        got = spender.spend_env(spender.Spender(ALICE, "call-route"), "claude-code-cli", call_fallback=True)
        assert got.subscription_id == "sub-pool" and got.bound_sub == ""
        assert [c[1] for c in pool.calls] == [ALICE, None]

    def test_an_engine_with_no_single_vendor_carries_the_person(self, pool):
        pool.accounts[ALICE] = "sub-alice"
        got = spender.spend_env(spender.Spender(ALICE, "task"), "direct-llm")
        assert got.env["_USER_SUB"] == ALICE
        pool.accounts[None] = "sub-pool"
        assert spender.spend_env(spender.POOL, "direct-llm").env["_USER_SUB"] == ""

    def test_a_vendor_engine_carries_no_person(self, pool):
        pool.accounts[ALICE] = "sub-alice"
        got = spender.spend_env(spender.Spender(ALICE, "chat"), "claude-code-cli")
        assert "_USER_SUB" not in got.env


class TestServes:
    def test_a_session_bound_to_the_person_serves_them_only(self):
        sp.bind_session("sess-a", "sub-1", layer="claude-code-cli", user_sub=ALICE)
        assert spender.serves("sess-a", spender.Spender(ALICE, "chat-lane"))
        assert not spender.serves("sess-a", spender.Spender(BOB, "chat-lane"))
        assert not spender.serves("sess-a", spender.POOL)

    def test_a_pool_session_serves_the_pool(self):
        sp.bind_session("sess-p", "sub-1", layer="claude-code-cli", user_sub="")
        assert spender.serves("sess-p", spender.POOL)
        assert not spender.serves("sess-p", spender.Spender(ALICE, "chat-lane"))


# ---------------------------------------------------------------------------
# The record, on an install upgraded from 1.9.1
# ---------------------------------------------------------------------------

class TestTheRecordAndItsMigration:
    def test_the_migration_adds_both_columns_on_an_existing_database(self):
        from storage.automation import schema as automation_schema
        from storage.database import get_conn
        query = ("SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema() "
                 "AND column_name = 'spender_sub' AND table_name IN ('dynamic_tasks', 'task_runs')")
        with get_conn() as conn:
            conn.execute("ALTER TABLE dynamic_tasks DROP COLUMN spender_sub")
            conn.execute("ALTER TABLE task_runs DROP COLUMN spender_sub")
            assert conn.execute(query).fetchall() == []
            automation_schema.migrate_automation(conn)
            assert sorted(r["table_name"] for r in conn.execute(query).fetchall()) == [
                "dynamic_tasks", "task_runs"]
            conn.rollback()

    def test_the_record_survives_the_store_and_the_run_copies_it(self):
        from services.scheduler import scheduler
        task_store.create_dynamic_task("dyn-l", SHARED, "lane", "go", "cli", task_kinds.DELEGATE,
                                       None, None, None, 600, SHARED, scope="agent",
                                       target_chat_id="c-x", spender_sub=ALICE)
        task = scheduler._row_to_task(task_store.get_dynamic_task("dyn-l"))
        assert task.spender_sub == ALICE
        assert spender.run_spender(task).sub == ALICE

    def test_an_old_delegate_row_with_no_record_reads_its_person_creator(self):
        from services.scheduler import scheduler
        task_store.create_dynamic_task("dyn-old", SHARED, "lane", "go", "cli", task_kinds.DELEGATE,
                                       None, None, None, 600, ALICE, scope="agent",
                                       target_chat_id="c-y")
        task = scheduler._row_to_task(task_store.get_dynamic_task("dyn-old"))
        assert task.spender_sub is None
        assert spender.run_spender(task).sub == ALICE


# ---------------------------------------------------------------------------
# The builders ask the owner (the loop builder rig records the pool's call)
# ---------------------------------------------------------------------------

class TestTheBuildersAskTheOwner:
    @pytest.fixture
    def rig(self, temp_db, monkeypatch, tmp_path):
        from tests._compute import serve
        from tests.fixtures.loop_builder_rig import install
        serve(monkeypatch)
        return install(monkeypatch, tmp_path)

    @staticmethod
    def _task(agent, *, scope, created_by, task_type="scheduled", chat=None, spender_sub=None):
        return SimpleNamespace(agent=agent, scope=scope, created_by=created_by, task_type=task_type,
                               notification_mode="manual", name="t", id="dyn-spend",
                               target_chat_id=chat, spender_sub=spender_sub, judge=None)

    @staticmethod
    def _draws(rig, build):
        from tests.fixtures.loop_builder_rig import run
        rig.calls.clear()
        cfg = run(build())
        return [c["args"][1] for c in rig.calls if c["call"] == "resolve_subscription_env"], cfg

    def _task_draws(self, rig, agent, task):
        from core.config.task_config_builder import build_task_agent_config
        return self._draws(rig, lambda: build_task_agent_config(agent, task, "s-task"))

    @pytest.mark.parametrize("source", ["continue", "reload", "account_limit"])
    def test_a_resume_of_a_persons_cut_turn_in_a_lanes_chat_draws_that_person(self, rig, monkeypatch, source):
        """BOB typed into ALICE's Shared-only lane chat, and a restart or BOB's
        own limit cut the turn. The platform resumes it as BOB's own session
        (``AS_CHAT``): BOB pays, never the lane's person."""
        from core.session import chat_rebuild
        from core.session.visibility import shared_chat_owner
        from services.engines import subscription_pool
        from services.scheduler import wake_session
        from tests.fixtures.loop_builder_rig import SHARED as RIG_SHARED, run
        chat = task_store.create_chat(f"c-cut-{source}", shared_chat_owner(RIG_SHARED), RIG_SHARED, "auto",
                                      origin="delegated")
        _run(f"r-cut-{source}", RIG_SHARED, task_type=task_kinds.DELEGATE, scope="agent",
             created_by="user-admin", spender_sub="user-admin", chat_id=chat["id"])
        drawn = []

        def _stop(_layer, person, **_kw):
            drawn.append(person)
            raise subscription_pool.NoSubscriptionError("the test ends at the draw")
        monkeypatch.setattr(subscription_pool, "resolve_subscription_env", _stop)
        started = run(wake_session._spawn_wake_session(
            SimpleNamespace(), "s-cut", RIG_SHARED, "go on", user_sub="user-viewer", role=roles.EDITOR,
            chat_id=chat["id"], chat_row=chat, exec_path="claude-code-cli", target="", username="viewer",
            permission_mode="auto", client_type="dashboard", build=chat_rebuild.AS_CHAT, driven_by=source))
        assert started == (False, None)
        assert drawn == ["user-viewer"], "the session runs as user-viewer on the lane's person's account"

    def test_a_shared_only_chat_lane_draws_for_its_person(self, rig):
        from tests.fixtures.loop_builder_rig import AGENT, SHARED
        lane = self._task(SHARED, scope="agent", created_by=AGENT, task_type=task_kinds.DELEGATE,
                          chat="c-lane-s", spender_sub="user-admin")
        draws, cfg = self._task_draws(rig, SHARED, lane)
        assert draws == ["user-admin"] and cfg.subscription_user_sub == "user-admin"

    def test_an_agent_scope_chat_lane_from_a_person_draws_for_them(self, rig):
        from tests.fixtures.loop_builder_rig import AGENT
        lane = self._task(AGENT, scope="agent", created_by="user-viewer", task_type=task_kinds.DELEGATE,
                          chat="c-lane-c", spender_sub="user-viewer")
        draws, cfg = self._task_draws(rig, AGENT, lane)
        assert draws == ["user-viewer"] and cfg.subscription_user_sub == "user-viewer"

    def test_a_user_scope_lane_holds(self, rig):
        from tests.fixtures.loop_builder_rig import AGENT
        lane = self._task(AGENT, scope="user", created_by="user-viewer", task_type=task_kinds.DELEGATE,
                          chat="c-lane-u", spender_sub="user-viewer")
        assert self._task_draws(rig, AGENT, lane)[0] == ["user-viewer"]

    def test_task_lanes_and_the_agents_own_work_draw_on_the_shared_subscriptions(self, rig):
        from tests.fixtures.loop_builder_rig import AGENT, SHARED
        task_lane = self._task(SHARED, scope="agent", created_by="user-admin", task_type=task_kinds.DELEGATE)
        agent_task = self._task(AGENT, scope="agent", created_by="user-admin")
        own_on_shared = self._task(SHARED, scope="user", created_by="user-admin")
        for agent, task in ((SHARED, task_lane), (AGENT, agent_task), (SHARED, own_on_shared)):
            draws, cfg = self._task_draws(rig, agent, task)
            assert draws == [None] and cfg.subscription_user_sub == "", task

    def test_a_lane_whose_person_is_below_editor_is_refused_and_the_pool_never_asked(self, rig):
        from core.config.task_config_builder import build_task_agent_config
        from tests.fixtures.loop_builder_rig import SHARED, run
        lane = self._task(SHARED, scope="agent", created_by="user-viewer", task_type=task_kinds.DELEGATE,
                          chat="c-lane-v", spender_sub="user-viewer")
        rig.calls.clear()
        with pytest.raises(sp.NoSubscriptionError) as e:
            run(build_task_agent_config(SHARED, lane, "s-task"))
        assert "cannot run work on char-shared now" in str(e.value)
        assert not [c for c in rig.calls if c["call"] == "resolve_subscription_env"]

    def test_a_persons_text_in_a_chat_a_call_wrote_is_their_own_chat(self, rig):
        """The session decides, never the chat row's kind: after a call on a
        route tied to a person, their first text builds a chat's session on
        a phone-kind row, and they pay as in any chat of theirs."""
        from core.config.config_builder import build_agent_config
        from core.session import session_kind
        from tests.fixtures.loop_builder_rig import AGENT
        task_store.create_chat("c-after-call", "user-viewer", AGENT,
                               source_type=session_kind.PHONE.source_type)
        user = task_store.get_user("user-viewer")
        draws, cfg = self._draws(rig, lambda: build_agent_config(
            AGENT, user, "user-viewer", "editor", client_type="dashboard",
            chat_id="c-after-call", session_id="s-text"))
        assert draws == ["user-viewer"] and cfg.subscription_user_sub == "user-viewer"
        assert spender.build_spender("c-after-call", AGENT, "user-viewer").row == "chat"
        assert spender.build_spender("c-after-call", AGENT, "user-viewer", call=True, route=True).row \
            == "call-route"

    def test_a_meeting_a_person_starts_draws_for_them_every_participant_included(self, rig):
        from services.meetings.meeting_context import build_meeting_agent_config
        from tests.fixtures.loop_builder_rig import SHARED
        meeting = {"id": "meet-s", "scope": "user", "created_by": "user-admin"}
        draws, cfg = self._draws(rig, lambda: build_meeting_agent_config(SHARED, meeting, "s-meet"))
        assert draws == ["user-admin"] and cfg.subscription_user_sub == "user-admin"


class TestTheRunnerReadsTheOwner:
    def test_the_pool_cap_is_the_payers(self, monkeypatch):
        import asyncio

        from services.billing import pool_caps
        from services.scheduler import runner
        seen = []

        def _evaluate(scope, target, layer):
            seen.append((scope, target))
            return SimpleNamespace(allowed=True)
        monkeypatch.setattr(pool_caps, "evaluate", _evaluate)
        monkeypatch.setattr(runner, "_task_layer", lambda task: "claude-code-cli")
        lane = _lane(SHARED)
        lane.id, lane.override_execution_path, lane.override_model = "dyn-x", None, None
        own = SimpleNamespace(agent=SHARED, scope="user", created_by=ALICE, task_type="scheduled",
                              spender_sub=None, target_chat_id=None, judge=None, id="dyn-y")
        asyncio.run(runner._pool_cap_block(lane))
        asyncio.run(runner._pool_cap_block(own))
        assert seen == [("user", ALICE), ("platform", "")]

    def test_a_chat_lanes_engine_is_its_persons(self, monkeypatch):
        from services.engines import engine_resolution as er
        from services.scheduler import runner
        asked = []

        def _resolve(agent, sub):
            asked.append(sub)
            return er.Resolution("direct-llm", "m", er.REASON_OWN, ("direct-llm",), ("direct-llm",), {})
        monkeypatch.setattr(er, "resolve_session_engine", _resolve)
        lane = _lane(SHARED)
        lane.override_execution_path, lane.override_model = None, None
        assert runner._task_engine(lane) == ("direct-llm", "m") and asked == [ALICE]


class TestJudgedSpender:
    def test_a_check_pays_like_the_turn_it_judges(self):
        sp.bind_session("sess-judged", "sub-1", layer="claude-code-cli", user_sub=ALICE)
        assert spender.judged_spender("sess-judged", None, None) == spender.Spender(ALICE, "judged-binding")

    def test_a_check_of_the_agents_own_turn_is_the_pool(self):
        sp.bind_session("sess-agent", "sub-1", layer="claude-code-cli", user_sub="")
        assert spender.judged_spender("sess-agent", None, ALICE).sub is None

    def test_an_unbound_turn_pays_as_its_chat(self):
        chat = task_store.create_chat("c-j", ALICE, COLLAB)
        assert spender.judged_spender("sess-gone", chat, ALICE) == spender.Spender(ALICE, "chat")


class TestWokenLaneChats:
    def test_a_wake_of_a_lane_chat_pays_as_its_round_whoever_it_runs_as(self):
        chat = task_store.create_chat("c-wl", "agent::" + SHARED, SHARED)
        _run("r-wl", SHARED, task_type=task_kinds.DELEGATE, scope="agent", created_by=ALICE,
             spender_sub=ALICE, chat_id="c-wl")
        assert spender.chat_spender(chat, BOB, woken=True).sub == ALICE
        assert spender.chat_spender(chat, BOB).sub == BOB

    def test_a_wake_of_a_persons_own_chat_is_theirs(self):
        chat = task_store.create_chat("c-wp", "agent::" + SHARED, SHARED)
        assert spender.chat_spender(chat, ALICE, woken=True) == spender.Spender(ALICE, "chat")

    def test_a_shared_only_chat_never_wakes_as_an_agents_slug(self):
        from core.session.visibility import shared_chat_owner
        from services.scheduler.delivery_identity import _shared_chat_person
        chat = {"agent": SHARED, "user_sub": shared_chat_owner(SHARED)}
        task_store.upsert_user(COLLAB, "slug@x.test", "Slug", "member")
        assert _shared_chat_person(chat, COLLAB) == ""
        assert _shared_chat_person(chat, ALICE) == ALICE


# ---------------------------------------------------------------------------
# Row 8: whoever continues a lane pays for that round
# ---------------------------------------------------------------------------

class TestAContinueByAnotherPerson:
    def test_a_live_session_on_the_first_persons_account_is_another_payers(self):
        from services.scheduler import lanes
        sp.bind_session("sess-lane", "sub-a", layer="claude-code-cli", user_sub=ALICE)
        assert lanes.serves_another_payer("sess-lane", _lane(SHARED, spender_sub=BOB, created_by=BOB))
        assert not lanes.serves_another_payer("sess-lane", _lane(SHARED))

    def test_no_live_session_is_never_another_payers(self):
        from services.scheduler import lanes
        assert not lanes.serves_another_payer("sess-none", _lane(SHARED, spender_sub=BOB))
        assert not lanes.serves_another_payer("", _lane(SHARED, spender_sub=BOB))

    def test_a_warm_session_on_a_machine_is_not_reused_for_another_payer(self, monkeypatch):
        import asyncio

        from core.session import session_manager, session_state
        from services.scheduler import lanes
        info = SimpleNamespace(alive=True, cli_dead=False, turn_active=False, machine_id="m-1", compute=False)

        class _Remote:
            _sessions = {"sess-warm": info}

            async def is_session_process_dead(self, sid):
                return False
        remote = _Remote()
        monkeypatch.setattr(session_manager, "_remote_layer", remote)
        monkeypatch.setitem(session_state._sessions, "sess-warm", {"is_task": True})
        sp.bind_session("sess-warm", "sub-a", layer="claude-code-cli", user_sub=ALICE)
        cfg = SimpleNamespace(execution_target="m-1", subscription_user_sub=ALICE)
        assert asyncio.run(lanes._try_reuse_warm_session(remote, cfg, "sess-warm", "run-1"))
        cfg.subscription_user_sub = BOB
        assert not asyncio.run(lanes._try_reuse_warm_session(remote, cfg, "sess-warm", "run-1"))


class TestOffboardingByBinding:
    def test_a_lane_that_runs_as_the_agent_on_a_persons_accounts_is_theirs_to_lose(self, monkeypatch):
        from core.session import session_manager, session_state
        from services.agents import offboarding_sessions as closer
        ctx = SimpleNamespace(username="", is_external=False, session_scope="agent", agent=SHARED,
                              role="manager", placement=SimpleNamespace(user_paired=False))

        class _Holder:
            def local_session_ids(self):
                return ["sess-paid", "sess-pool"]
        monkeypatch.setattr(session_manager, "_iter_session_holders", lambda: [_Holder()])
        monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
        monkeypatch.setattr(session_state, "get_session_client_type", lambda sid: "task")
        sp.bind_session("sess-paid", "sub-a", layer="claude-code-cli", user_sub=ALICE)
        sp.bind_session("sess-pool", "sub-a", layer="claude-code-cli", user_sub="")
        live = {v.session_id: v for v in closer.live_sessions()}
        assert set(live) == {"sess-paid"}
        v = live["sess-paid"]
        assert (v.kind, v.user_sub, v.role) == (closer.WORKER, ALICE, roles.EDITOR)
        # Below the editor tier it may not run on; an editor's lane may.
        assert closer._outranked(v.role, roles.CONTRIBUTOR, True)
        assert not closer._outranked(v.role, roles.EDITOR, True)


class TestTheEngineSwitchInATaskChat:
    def test_a_persons_own_task_on_a_shared_only_agent_switches_on_the_shared_subscriptions(self):
        task_store.create_run("r-sw", "dyn-sw", SHARED, "manual", None, "go", "scheduled", "user", ALICE)
        chat = task_store.create_chat("task-r-sw", ALICE, SHARED)
        assert spender.chat_spender(chat, None).sub is None

    def test_a_persons_own_task_elsewhere_switches_on_their_accounts(self):
        task_store.create_run("r-sw2", "dyn-sw2", COLLAB, "manual", None, "go", "scheduled", "user", ALICE)
        chat = task_store.create_chat("task-r-sw2", ALICE, COLLAB)
        assert spender.chat_spender(chat, None).sub == ALICE


class TestTheCards:
    def test_a_shared_only_lane_counts_on_its_persons_list_and_on_admins(self):
        row = {"id": "sub-a", "owner_sub": ROOT, "contribute_platform": 1, "share_agents": 1}
        sp.bind_session("sess-card", "sub-a", layer="claude-code-cli", user_sub=ALICE)
        assert sp.sessions_on([row], viewer=ALICE, admin=False)["sub-a"][0] == 1
        assert sp.sessions_on([row], viewer=ROOT, admin=True)["sub-a"][0] == 1
        assert "sub-a" not in sp.sessions_on([row], viewer=BOB, admin=False)


def test_the_roster_legend_says_who_each_mark_serves():
    from services.mcp.dynamic_providers import ROSTER_LEGEND
    assert "their chats, every chat lane, their own tasks" in ROSTER_LEGEND
    assert "the agent's own tasks, task lanes on a Shared-only agent" in ROSTER_LEGEND


# ---------------------------------------------------------------------------
# One rule for "nobody typed", on both builders (the fix round)
# ---------------------------------------------------------------------------

def _shared_lane_chat(chat_id="c-sl"):
    from core.session.visibility import shared_chat_owner
    chat = task_store.create_chat(chat_id, shared_chat_owner(SHARED), SHARED, "auto", origin="delegated")
    _run(f"r-{chat_id}", SHARED, task_type=task_kinds.DELEGATE, scope="agent", created_by=ALICE,
         spender_sub=ALICE, chat_id=chat_id)
    return chat


class TestOneRuleForNobodyTyped:
    def test_only_a_key_turn_is_typed(self):
        from core.session import chat_rebuild
        assert chat_rebuild.TYPED == frozenset({"api_key"})

    def test_nobody_typed_is_neither_a_keys_turn_nor_a_persons_own_build(self):
        """A reload, a continue and a limit's continuation resume a person's
        cut turn as their own session (``AS_CHAT``): never nobody's."""
        from core.session import chat_rebuild
        for source in ("api_key", "delegate_result", "delegate_result_replay", "schedule_wake",
                       "move_resume", "continue", "reload", "account_limit", ""):
            for build in chat_rebuild.BUILDS:
                expected = source != "api_key" and build == chat_rebuild.AS_WAKE
                assert chat_rebuild.nobody_typed(source, build) is expected, (source, build)

    @pytest.mark.parametrize("driven_by", ["api_key", "delegate_result", "schedule_wake", "move_resume",
                                           "continue", "reload", "account_limit", ""])
    def test_the_wake_session_tells_the_wake_builder_who_typed_and_the_chat_builder_nothing(
            self, monkeypatch, driven_by):
        import asyncio

        from core.session import chat_rebuild
        from services.scheduler import wake_session
        seen = {}

        async def _wake(*_a, **k):
            seen["wake"] = k["woken"]

        async def _chat(*_a, **k):
            seen["chat"] = "woken" in k
        monkeypatch.setattr(wake_session, "_wake_built_config", _wake)
        monkeypatch.setattr(wake_session, "_chat_built_config", _chat)
        for build in (chat_rebuild.AS_WAKE, chat_rebuild.AS_CHAT):
            asyncio.run(wake_session._spawn_wake_session(
                SimpleNamespace(), "s-1", SHARED, "go", user_sub=BOB, role=roles.EDITOR, chat_id="c-x",
                chat_row={"id": "c-x"}, exec_path="claude-code-cli", target="local", username="bob",
                permission_mode="auto", client_type="", build=build, driven_by=driven_by))
        assert seen == {"wake": driven_by != "api_key", "chat": False}

    @staticmethod
    async def _drawn_by_the_wake_builder(monkeypatch, chat_row, *, user_sub, woken):
        from core.config import task_config_builder
        from core.sandbox import sandbox
        from services.scheduler import wake_session
        from storage import remote_store
        drawn = []

        def _stop(layer, person, **kw):
            drawn.append(person)
            raise sp.NoSubscriptionError("the walk ends here")

        async def _none(*_a, **_k):
            return None

        async def _empty(*_a, **_k):
            return []
        monkeypatch.setattr(remote_store, "placement_of", lambda *_a, **_k: SimpleNamespace(
            is_remote=False, user_paired=False, admin_paired=False))
        monkeypatch.setattr(remote_store, "get_target_browser_settings", lambda _p: None)
        monkeypatch.setattr(wake_session, "_wake_visibility",
                            lambda *_a: SimpleNamespace(mount_username="", mount_scope="agent"))
        monkeypatch.setattr(task_config_builder, "build_delivery_security_context", _none)
        monkeypatch.setattr(wake_session, "_wake_delegation_targets", lambda *_a: [])
        monkeypatch.setattr(wake_session, "_wake_mcp_build", lambda *_a, **_k: (None, {}, {}, {}, set()))
        monkeypatch.setattr(wake_session, "_wake_dynamic_contexts", _empty)
        monkeypatch.setattr(wake_session, "_wake_prompt", lambda *_a, **_k: "")
        monkeypatch.setattr(wake_session, "_wake_credential_env", lambda *_a, **_k: (None, {}, {}, 0))
        monkeypatch.setattr(sandbox, "ensure_persistent_agent_dir", lambda *_a, **_k: "/tmp/engine-folder")
        monkeypatch.setattr(sp, "resolve_subscription_env", _stop)
        built = await wake_session._wake_built_config(
            "s-1", SHARED, user_sub=user_sub, role=roles.EDITOR, chat_id=chat_row["id"], chat_row=chat_row,
            exec_path="claude-code-cli", target="local", username="", fresh=False,
            permission_mode="auto", client_type="", woken=woken)
        assert built is None
        return drawn

    def test_a_wake_of_a_shared_only_lanes_chat_binds_the_lanes_person(self, monkeypatch):
        import asyncio
        chat = _shared_lane_chat()
        assert asyncio.run(self._drawn_by_the_wake_builder(monkeypatch, chat, user_sub=BOB, woken=True)) == [ALICE]

    def test_a_keys_turn_into_a_shared_only_lanes_chat_binds_the_keys_person(self, monkeypatch):
        import asyncio
        chat = _shared_lane_chat("c-sl2")
        assert asyncio.run(self._drawn_by_the_wake_builder(monkeypatch, chat, user_sub=BOB, woken=False)) == [BOB]


class TestAWakeNeverRidesAViewersSession:
    def test_a_lanes_wake_rides_only_a_session_that_serves_the_lanes_person(self):
        chat = _shared_lane_chat("c-wr")
        task_store.update_chat("c-wr", session_id="s-viewer")
        sp.bind_session("s-viewer", "sub-1", layer="claude-code-cli", user_sub=BOB)
        assert not spender.wake_rides("c-wr", "s-viewer")
        sp.bind_session("s-own", "sub-1", layer="claude-code-cli", user_sub=ALICE)
        task_store.update_chat("c-wr", session_id="s-own")
        assert spender.wake_rides(chat["id"], "s-own")

    def test_any_other_chat_or_an_unbound_session_is_ridden(self):
        task_store.create_chat("c-mine", BOB, COLLAB)
        task_store.update_chat("c-mine", session_id="s-mine")
        sp.bind_session("s-mine", "sub-1", layer="claude-code-cli", user_sub=BOB)
        assert spender.wake_rides("c-mine", "s-mine")
        _shared_lane_chat("c-unbound")
        task_store.update_chat("c-unbound", session_id="s-unbound")
        assert spender.wake_rides("c-unbound", "s-unbound")

    @pytest.mark.parametrize("source,rides", [("delegate_result", False), ("api_key", True)])
    def test_the_ladder_passes_a_viewers_session_by_and_starts_none_beside_it(self, monkeypatch, source, rides):
        import asyncio

        from core.session import chat_rebuild, session_delivery
        _shared_lane_chat("c-lad")
        task_store.update_chat("c-lad", session_id="s-v")
        sp.bind_session("s-v", "sub-1", layer="claude-code-cli", user_sub=BOB)
        seen = {}

        async def _ladder(chat_id, text, **kw):
            seen.update(kw)
            return SimpleNamespace(path="none")
        monkeypatch.setattr(session_delivery, "deliver_prompt", _ladder)
        asyncio.run(chat_rebuild.rebuild_and_resume(
            "c-lad", "go", source=source, person=BOB, role=roles.EDITOR, mode="auto", kind="",
            build=chat_rebuild.AS_WAKE, session_id="s-v", agent=SHARED))
        assert (seen["allow_pty"], seen["allow_ws"], seen["allow_live_turn"]) == (rides, rides, rides)
        assert (seen["persistent_fn"] is not None, seen["oneshot_fn"] is not None) == (rides, rides)


    def test_a_persons_own_resume_rides_their_own_session_in_a_lanes_chat(self, monkeypatch):
        """BOB's cut turn in ALICE's lane chat, resumed by the platform
        (``AS_CHAT``): the gate never passes BOB's own session by."""
        import asyncio

        from core.session import chat_rebuild, session_delivery
        _shared_lane_chat("c-own-resume")
        task_store.update_chat("c-own-resume", session_id="s-bob")
        sp.bind_session("s-bob", "sub-1", layer="claude-code-cli", user_sub=BOB)
        seen = {}

        async def _ladder(chat_id, text, **kw):
            seen.update(kw)
            return SimpleNamespace(path="none")
        monkeypatch.setattr(session_delivery, "deliver_prompt", _ladder)
        asyncio.run(chat_rebuild.rebuild_and_resume(
            "c-own-resume", "go on", source="continue", person=BOB, role=roles.EDITOR, mode="auto",
            kind="dashboard", build=chat_rebuild.AS_CHAT, session_id="s-bob", agent=SHARED))
        assert seen["allow_live_turn"] and seen["persistent_fn"] is not None

    def test_the_gate_reads_a_live_terminals_payer_before_its_seat_is_bound(self, monkeypatch):
        from core.session import interactive_registry
        _shared_lane_chat("c-term-gate")
        task_store.update_chat("c-term-gate", session_id="s-term-start")
        monkeypatch.setitem(interactive_registry._sessions, "s-term-start", SimpleNamespace(payer=BOB))
        assert not spender.wake_rides("c-term-gate", "s-term-start")
        monkeypatch.setitem(interactive_registry._sessions, "s-term-start", SimpleNamespace(payer=ALICE))
        assert spender.wake_rides("c-term-gate", "s-term-start")

    def test_a_dead_sessions_stored_binding_never_holds_a_lanes_wake(self, monkeypatch):
        """BOB opened ALICE's lane chat, then the platform restarted and that
        session did not survive: its stored binding still names BOB. Past the
        boot window nothing is live, so the wake may start the lane's own
        session. Inside the window the row still counts, as for the pin."""
        import time

        from core.session import session_state
        from services.engines import pool_bindings
        from storage.billing import subscription_store
        _shared_lane_chat("c-dead")
        task_store.update_chat("c-dead", session_id="s-dead")
        sub = subscription_store.add_subscription("claude-code-cli", "anthropic", "api_key", owner_sub=BOB,
                                                  credential_data={"api_key": "sk-test"})
        subscription_store.upsert_session_binding("s-dead", sub["id"], layer="claude-code-cli", user_sub=BOB)
        assert not session_state.session_is_held("s-dead")
        assert spender.session_payer("s-dead") == BOB
        monkeypatch.setattr(pool_bindings, "_BOOT_MONOTONIC", time.monotonic() - 3600)
        assert spender.wake_rides("c-dead", "s-dead")
        monkeypatch.setattr(pool_bindings, "_BOOT_MONOTONIC", time.monotonic())
        assert not spender.wake_rides("c-dead", "s-dead")

    def test_the_gate_judges_the_chats_current_session_not_the_callers(self):
        _shared_lane_chat("c-cur")
        sp.bind_session("s-was-own", "sub-1", layer="claude-code-cli", user_sub=ALICE)
        sp.bind_session("s-now-viewer", "sub-1", layer="claude-code-cli", user_sub=BOB)
        task_store.update_chat("c-cur", session_id="s-now-viewer")
        assert not spender.wake_rides("c-cur", "s-was-own")
        assert spender.serves_lane("c-cur", "s-was-own")

    def test_a_starting_terminal_is_judged_by_the_payer_its_config_was_built_with(self):
        _shared_lane_chat("c-start")
        assert spender.serves_lane("c-start", "s-starting")
        assert not spender.serves_lane("c-start", "s-starting", built_for=BOB)
        assert not spender.serves_lane("c-start", "s-starting", built_for="")
        assert spender.serves_lane("c-start", "s-starting", built_for=ALICE)

    @staticmethod
    def _terminal_on(monkeypatch, chat_id, session_id, *, payer=None, bound_to=None):
        """A live terminal on the chat: taken back after a restart (``payer``
        None, judged by its binding) or starting (its config's payer)."""
        from core.session import interactive_session
        task_store.update_chat(chat_id, session_id=session_id)
        if bound_to is not None:
            sp.bind_session(session_id, "sub-1", layer="claude-code-cli", user_sub=bound_to)
        typed = []

        def _queue(text, source, **_kw):
            typed.append(source)
            return {"text": text, "source": source}
        terminal = SimpleNamespace(alive=True, session_id=session_id, payer=payer, queue_prompt=_queue)
        monkeypatch.setattr(interactive_session, "find_live_for_chat",
                            lambda cid: terminal if cid == chat_id else None)
        return typed

    @pytest.mark.parametrize("payer,bound_to", [(None, BOB), (BOB, None)])
    def test_a_stored_wake_is_not_handed_to_a_viewers_terminal(self, monkeypatch, payer, bound_to):
        import asyncio

        from services.scheduler import delivery
        _shared_lane_chat("c-store")
        typed = self._terminal_on(monkeypatch, "c-store", "s-store-v", payer=payer, bound_to=bound_to)
        assert asyncio.run(delivery._store_undelivered_wakes("c-store", ["the lane's result"],
                                                             role=roles.MANAGER))
        assert typed == []
        assert len(task_store.claim_pending_wake_records("c-store")) == 1

    @pytest.mark.parametrize("payer,bound_to", [(None, ALICE), (ALICE, None)])
    def test_the_lanes_own_terminal_takes_the_stored_wake(self, monkeypatch, payer, bound_to):
        import asyncio

        from services.scheduler import delivery
        _shared_lane_chat("c-store-own")
        typed = self._terminal_on(monkeypatch, "c-store-own", "s-store-own", payer=payer, bound_to=bound_to)
        assert asyncio.run(delivery._store_undelivered_wakes("c-store-own", ["the lane's result"],
                                                             role=roles.MANAGER))
        assert typed == ["delegate_result_replay"]

    def test_the_real_ladder_passes_a_viewer_by_and_the_wake_stays_stored(self, monkeypatch):
        """The gate passes BOB's terminal by, the ladder answers "none", the
        delivery's own hook stores the wake, and the store leaves it for a
        session that serves the lane: nothing is typed into BOB's terminal."""
        import asyncio

        from core.permission_modes import UNATTENDED_MODE
        from core.session import chat_rebuild
        from services.scheduler import delivery
        _shared_lane_chat("c-real")
        typed = self._terminal_on(monkeypatch, "c-real", "s-real-v", bound_to=BOB)

        async def _hook(outcome):
            if outcome.path == "none":
                await delivery._store_undelivered_wakes("c-real", ["the lane's result"], role=roles.MANAGER)
        outcome = asyncio.run(chat_rebuild.rebuild_and_resume(
            "c-real", "the lane's result", source="delegate_result", person=None, role=roles.MANAGER,
            mode=UNATTENDED_MODE, kind="", build=chat_rebuild.AS_WAKE, session_id="s-real-v", agent=SHARED,
            on_outcome=_hook))
        assert outcome.path == "none"
        assert typed == []
        assert len(task_store.claim_pending_wake_records("c-real")) == 1


class TestOneServesRule:
    def test_an_unbound_session_serves_anyone_a_bound_one_only_its_payer(self):
        assert spender.serves_payer(None, spender.Spender(ALICE, "chat"))
        assert spender.serves_payer(ALICE, spender.Spender(ALICE, "chat"))
        assert not spender.serves_payer(ALICE, spender.Spender(BOB, "chat"))
        assert not spender.serves_payer("", spender.Spender(BOB, "chat"))
        assert spender.serves_payer("", spender.POOL)
        assert spender.serves("s-never-bound", spender.Spender(BOB, "chat"))


class TestTheReplayAtARelease:
    """A wake a lane's chat stored while its session served another person
    is replayed when that session's seat is released
    (``pool_bindings.release_subscription`` hands the id to
    ``delivery.replay_after_release``). A session that died with no release
    is the gate's: ``test_a_dead_sessions_stored_binding_never_holds_a_lanes_wake``."""

    @pytest.fixture(autouse=True)
    def _marks(self):
        from core.events import stream_pump
        stream_pump._stored_wake_chats.clear()
        yield
        stream_pump._stored_wake_chats.clear()

    @staticmethod
    def _viewer_holds_a_stored_wake(chat_id, session_id):
        from core.events import stream_pump
        _shared_lane_chat(chat_id)
        task_store.update_chat(chat_id, session_id=session_id)
        sp.bind_session(session_id, "sub-1", layer="claude-code-cli", user_sub=BOB)
        assert task_store.append_pending_delegate_wake(chat_id, "the lane's result", role=roles.MANAGER)
        stream_pump.note_stored_wake(chat_id)

    @staticmethod
    def _ladder(monkeypatch):
        """The ladder the replay reaches, recorded: what the gate let through."""
        from core.session import session_delivery
        asked = []

        async def _deliver(chat_id, text, **kw):
            asked.append({"chat_id": chat_id, "rides": kw["oneshot_fn"] is not None, **kw})
            return session_delivery.DeliveryOutcome("oneshot", chat_id=chat_id)
        monkeypatch.setattr(session_delivery, "deliver_prompt", _deliver)
        return asked

    @staticmethod
    async def _settle():
        import asyncio

        from core import background
        for _ in range(20):
            await asyncio.sleep(0)
            pending = background.running("release-replay")
            if pending:
                await asyncio.gather(*pending)

    def test_a_viewers_release_replays_the_wake_once_for_the_lanes_person(self, monkeypatch):
        import asyncio

        from core.session import chat_rebuild
        self._viewer_holds_a_stored_wake("c-rel", "s-rel-viewer")
        asked = self._ladder(monkeypatch)

        async def go():
            from core.events import stream_pump
            sp.release_subscription("s-rel-viewer")
            await self._settle()
            # A wake stored again on the chat: a second release of the same
            # session pops nothing and replays nothing.
            assert task_store.append_pending_delegate_wake("c-rel", "another", role=roles.MANAGER)
            stream_pump.note_stored_wake("c-rel")
            sp.release_subscription("s-rel-viewer")
            await self._settle()
        asyncio.run(go())
        assert [(a["chat_id"], a["rides"]) for a in asked] == [("c-rel", True)]
        assert chat_rebuild.nobody_typed(asked[0]["source"], asked[0]["oneshot_fn"].keywords["build"])

    def test_no_stored_wake_no_read(self, monkeypatch):
        import asyncio
        _shared_lane_chat("c-none")
        sp.bind_session("s-none", "sub-1", layer="claude-code-cli", user_sub=BOB)
        read = []
        monkeypatch.setattr(task_store, "get_chat_by_session", read.append)

        async def go():
            sp.release_subscription("s-none")
            await self._settle()
        asyncio.run(go())
        assert read == []

    def test_a_release_from_a_thread_is_handed_to_the_loop(self, monkeypatch):
        import asyncio

        from services.engines import token_fanout
        self._viewer_holds_a_stored_wake("c-thr", "s-thr-viewer")
        asked = self._ladder(monkeypatch)

        async def go():
            monkeypatch.setattr(token_fanout, "_loop", asyncio.get_running_loop())
            await asyncio.to_thread(sp.release_subscription, "s-thr-viewer")
            await self._settle()
        asyncio.run(go())
        assert [(a["chat_id"], a["rides"]) for a in asked] == [("c-thr", True)]

    def test_a_terminal_is_out_of_its_registry_before_its_seat_goes(self, monkeypatch):
        """A viewer's terminal closes: when its release hands the id over, no
        live terminal is left under it, so the replay is not stored again
        with nothing to fire it."""
        import asyncio

        from core.session import interactive_registry
        from services.scheduler import delivery
        from tests._terminal import register
        self._viewer_holds_a_stored_wake("c-term", "s-term-viewer")
        asked = self._ladder(monkeypatch)
        seen = []
        real = delivery.replay_after_release

        def _at_release(sid):
            seen.append(interactive_registry.get(sid))
            real(sid)
        monkeypatch.setattr(delivery, "replay_after_release", _at_release)

        async def go():
            from core import concurrency
            concurrency.init()
            terminal = await register(session_id="s-term-viewer", chat_id="c-term", agent_name=SHARED,
                                      payer=BOB)
            await terminal.close(reason="idle")
            await self._settle()
        asyncio.run(go())
        assert seen == [None]
        assert [(a["chat_id"], a["rides"]) for a in asked] == [("c-term", True)]


class TestAWakeNobodyCanStart:
    """A lane whose person cannot run it any more (lost the agent, or fell
    below the editor role on a Shared-only one) keeps no wake: no start
    would ever take it. A person whose account only rests keeps it."""

    @staticmethod
    def _ladder(monkeypatch):
        from core.session import chat_rebuild
        from core.session.session_delivery import DeliveryOutcome
        asked = []

        async def _rebuild(chat_id, text, *, on_outcome=None, **kw):
            asked.append(chat_id)
            outcome = DeliveryOutcome("none", chat_id=chat_id)
            await on_outcome(outcome)
            return outcome
        monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", _rebuild)
        return asked

    def test_the_writer_keeps_none_and_the_sweep_drops_one_stored_before(self, monkeypatch):
        import asyncio

        from services.scheduler import delivery
        _shared_lane_chat("c-lost")
        assert task_store.append_pending_delegate_wake("c-lost", "stored before", role=roles.MANAGER)
        task_store.set_user_agents(ALICE, [], ROOT)
        assert spender.round_spender("c-lost").refused
        assert not asyncio.run(delivery._store_undelivered_wakes("c-lost", ["the lane's result"],
                                                                 role=roles.MANAGER))
        asked = self._ladder(monkeypatch)
        for _ in range(3):
            asyncio.run(delivery._redeliver_chat(task_store.get_chat("c-lost")))
        assert asked == []
        assert task_store.claim_pending_wake_records("c-lost") == []

    def test_a_lane_whose_person_only_lacks_an_account_now_keeps_it(self, monkeypatch):
        import asyncio

        from services.scheduler import delivery
        _shared_lane_chat("c-rest")
        assert task_store.append_pending_delegate_wake("c-rest", "the lane's result", role=roles.MANAGER)
        assert not spender.round_spender("c-rest").refused
        asked = self._ladder(monkeypatch)
        asyncio.run(delivery._redeliver_chat(task_store.get_chat("c-rest")))
        assert asked == ["c-rest"]
        assert len(task_store.claim_pending_wake_records("c-rest")) == 1



class TestAPoolErrorStartsNoUnboundSession:
    def test_an_engine_with_a_sign_in_folder_is_refused(self, monkeypatch):
        def _broken(*_a, **_k):
            raise RuntimeError("the pool's store is gone")
        monkeypatch.setattr(sp, "resolve_subscription_env", _broken)
        with pytest.raises(sp.NoSubscriptionError) as e:
            spender.spend_env(spender.POOL, "codex-cli", agent=SHARED)
        assert e.value.reason == "none"

    def test_an_engine_with_none_still_starts_on_its_own_keys(self, monkeypatch):
        def _broken(*_a, **_k):
            raise RuntimeError("the pool's store is gone")
        monkeypatch.setattr(sp, "resolve_subscription_env", _broken)
        spent = spender.spend_env(spender.POOL, "direct-llm", agent=SHARED)
        assert spent.subscription_id == ""
