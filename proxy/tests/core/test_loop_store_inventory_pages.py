"""The loop-side store inventory of the memory tool's route, the card catalog,
the admin's phone servers, the target status poll, a manual compact, a
session's payer and the attach paths.

Each flow runs on the test thread's loop with ``loop_db_guard.recording()``
around the exercised call and records nothing, with the agents' table cache
cold unless its comment says why not. Route flows call the route function
directly: the recorder is pinned to the test thread and never sees
TestClient's portal thread.
"""
import asyncio
import json
import threading
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from auth.providers import UserContext
from storage.agents import agent_store
from tests import _engines
from tests.core.test_loop_store_inventory import (
    _check,
    _frame,
    _record,
    _warmup_rig,
    _ws_record,
)
from tests.session.test_ws_dashboard_resume_controls import _bare_connection

AGENT = "page-agent"
SOURCE = "page-source"
TARGET = "page-target"
ADMIN = "user-admin"
MEMBER = "user-viewer"
USERNAME = "ada"


def _read_through() -> dict[str, dict]:
    return {a["slug"]: a for a in agent_store.get_all_agents()}


@pytest.fixture
def always_cold_agents(monkeypatch):
    """Every cached agent read goes to the table: any agent write empties the
    cache between two awaits, so a read left on the loop must record whatever
    ran before it. The stand-in carries ``agent_store``'s file name because
    the recorder names the first frame outside ``storage/``: the caller."""
    code = _read_through.__code__.replace(co_filename=agent_store.__file__)
    monkeypatch.setattr(agent_store, "_get_cached", types.FunctionType(code, globals()))


def _set_username(sub: str, username: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


def _admin(sub: str = ADMIN) -> UserContext:
    return UserContext(sub=sub, email="a@test.com", name="Ada", role="admin", agents=[], agent_roles={})


# ---------------------------------------------------------------------------
# The memory tool's route
# ---------------------------------------------------------------------------

def _memory_session(user_sub: str = MEMBER, *, external_claim: str = "") -> UserContext:
    return UserContext(sub=user_sub, email="", name="", role="member", is_api_key=True, session_id="s-memory",
                       agent=AGENT, agents=[AGENT], agent_roles={AGENT: "editor"}, external_claim=external_claim)


async def _memory_op(user: UserContext, body: dict) -> dict:
    from api.agent_data import memory

    async def _json():
        return body
    return await memory.memory_op(SimpleNamespace(json=_json), user=user, x_agent_name=AGENT, x_on_behalf_of=None)


def test_memory_route(temp_db, loop_db_guard):
    """Every command of the tool as a person's session, and a caller on an
    external route, who is refused: an outside session writes no memory."""
    agent_store.create_agent(AGENT, "Page Agent", created_by=ADMIN)
    _set_username(MEMBER, "vic")
    session = _memory_session()
    caller = _memory_session("session:s-call", external_claim="phone:+302100000000")
    answers = []

    async def every_command():
        for body in ({"command": "view", "path": "/memories"},
                     {"command": "create", "path": "/memories/user/a.md", "file_text": "one\n"},
                     {"command": "str_replace", "path": "/memories/user/a.md", "old_str": "one", "new_str": "two"},
                     {"command": "insert", "path": "/memories/user/a.md", "insert_line": 1, "insert_text": "three\n"},
                     {"command": "view", "path": "/memories/user/a.md"},
                     {"command": "rename", "old_path": "/memories/user/a.md", "new_path": "/memories/user/b.md"},
                     {"command": "delete", "path": "/memories/user/b.md"},
                     {"command": "no-such-command"}):
            answers.append(await _memory_op(session, body))
        answers.append(await _memory_op(caller, {"command": "create", "path": "/memories/user/c.md",
                                                 "file_text": "the caller's\n"}))

    _check("memory route", _record(loop_db_guard, every_command))
    assert [a["is_error"] for a in answers] == [False] * 7 + [True, True]
    assert "not written during an outside conversation" in answers[-1]["output"]


def test_memory_route_keeps_its_refusals(temp_db):
    """An unknown agent stays a 404 ahead of the agent match, and a session
    for another agent stays a 403, with the reads in one job."""
    agent_store.create_agent(SOURCE, "Page Source", created_by=ADMIN)
    with pytest.raises(HTTPException) as gone:
        asyncio.run(_memory_op(_memory_session(), {"command": "view", "path": "/memories"}))
    assert gone.value.status_code == 404
    agent_store.create_agent(AGENT, "Page Agent", created_by=ADMIN)
    foreign = _memory_session()
    foreign.agent = SOURCE
    foreign.agents = [SOURCE]
    with pytest.raises(HTTPException) as refused:
        asyncio.run(_memory_op(foreign, {"command": "view", "path": "/memories"}))
    assert refused.value.status_code == 403


# ---------------------------------------------------------------------------
# The card catalog
# ---------------------------------------------------------------------------

REGISTRY = {"agents": [
    {"slug": "yacht-crew", "display_name": "Yacht Crew", "version": "0.2.2", "platform_min_version": "1.0.0"},
], "departments": [{
    "slug": "yacht-office", "name": "Yacht office", "version": "0.1.0", "levels": ["Head", "Team"],
    "members": [{"template": "yacht-crew", "level": "Team"}]}]}


@pytest.fixture
def card_world(temp_db, monkeypatch, always_cold_agents):
    """Every action's params, for a person each check lets through to its
    last read: the agents, a department, a person's file and memory topic,
    an app, a library and an app that follows its source. The follow rule
    and state take their files' place, as in tests/api/test_cards.py."""
    import config
    from services.apps import app_follow, releases
    from services.cards import status as st
    from services.memory import memory_file
    from storage import db_apps
    from storage.agents import db_departments
    from storage.knowledge import db_knowledge_libraries

    monkeypatch.setattr("services.community.community_agents_catalog.fetch_registry",
                        AsyncMock(return_value=REGISTRY))
    _set_username(ADMIN, USERNAME)
    for slug, name in ((AGENT, "Page Agent"), (SOURCE, "Page Source"), (TARGET, "Page Target")):
        agent_store.create_agent(slug, name, created_by=ADMIN)
    # An agent from a set's template in no department: the set's words name it.
    agent_store.create_agent("yacht-crew-2", "Crew Two", created_by=ADMIN, community_template="yacht-crew",
                             community_template_version="0.2.2")
    dept = db_departments.create_department("Eng", ADMIN, level_names=["Head", "Team"])
    level = dept["levels"][1]["id"]

    root = config.get_agent_dir(AGENT)
    (root / "users" / USERNAME / "workspace").mkdir(parents=True)
    (root / "users" / USERNAME / "workspace" / "a.md").write_text("a")
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    topics = memory_file.scope_root(root, "user", USERNAME)
    topics.mkdir(parents=True)
    (topics / "suppliers.md").write_text("# Suppliers\n")

    source_app = db_apps.upsert_app(SOURCE, "", None, "board", title="Crew board", rel_path="workspace/apps/board",
                                    kind="folder")
    copy = db_apps.upsert_app(AGENT, "", None, "board", title="Crew board", rel_path="workspace/apps/board",
                              kind="folder")
    db_apps.set_follows(copy["id"], json.dumps({"id": source_app["id"], "agent": SOURCE, "username": "",
                                                "slug": "board", "release": 1, "copy_release": 1}))
    monkeypatch.setattr(app_follow, "refusal", lambda row, u, move: None)
    monkeypatch.setattr(app_follow, "state", lambda row: {
        "source": {"agent": SOURCE, "agent_name": "Page Source", "slug": "board", "release": 1, "exists": True},
        "update": {"release": 3, "mode": "update"}})
    monkeypatch.setattr(releases, "current_number", lambda row: 2)
    db_knowledge_libraries.promote(SOURCE, created_by=ADMIN)

    params = {
        "install_agent": {"template_slug": "yacht-crew", "target_slug": "crew-new", "display_name": "New crew",
                          "department_id": dept["id"], "level_id": level, "_agent": AGENT},
        "install_department_set": {"set_slug": "yacht-office", "department_name": "Lady G"},
        "move_agent": {"agent": AGENT, "department_id": dept["id"], "level_id": level},
        "hand_files": {"paths": ["a.md"], "_agent": AGENT},
        "promote_memory": {"topic": "suppliers.md", "_agent": AGENT},
        "share_app": {"app_slug": "board", "target_agent": TARGET, "_agent": SOURCE},
        "attach_library": {"source_agent": SOURCE, "target_agent": TARGET, "_agent": AGENT},
        "update_followed_app": {"app_slug": "board", "mode": "update", "_agent": AGENT},
        "send_release_to_source": {"app_slug": "board", "_agent": AGENT},
        "create_department": {"name": "Marketing"},
        "set_department": {"department_id": dept["id"], "mode": "down"},
    }
    return SimpleNamespace(dept=dept, level=level, params=params, st=st, topics=topics)


def _catalog():
    from api.cards import (
        cards as cards_route,  # noqa: F401  # it registers the team and department actions
    )
    from services.cards import actions
    return actions


def test_card_catalog_check_and_words(card_world, loop_db_guard):
    """``check``, ``words`` and ``personal`` of every action in the catalog,
    each check let through to its last read."""
    actions = _catalog()
    assert sorted(card_world.params) == sorted(actions.CATALOG)
    u = _admin()
    refusals, words = {}, {}
    recorded: set = set()
    for name, params in card_world.params.items():
        action = actions.get(name)

        async def one(action=action, params=params, name=name):
            refusals[name] = await action.check(params, u)
            words[name] = await action.words(params, u)
            action.personal(params)

        recorded |= _record(loop_db_guard, one)
    _check("card catalog: check and words", recorded)
    assert refusals == dict.fromkeys(card_world.params)
    assert 'It is placed in "Eng" as Team.' in words["install_agent"]["text"]
    assert "Crew Two (yacht-crew-2) joins it" in words["install_department_set"]["text"]
    assert words["move_agent"]["title"] == 'Move Page Agent to "Eng"'
    assert words["share_app"]["title"] == 'Share the app "Crew board" with Page Target'
    assert words["attach_library"]["title"] == "Attach the library of Page Source to Page Target"


def test_card_catalog_refusals_that_name_an_agent(card_world, loop_db_guard):
    """The refusals that read an agent's name or scopes on their way out, and
    the attach words of an attachment that exists."""
    actions = _catalog()
    from storage.knowledge import db_knowledge_libraries
    agent_store.create_agent("page-solo", "Page Solo", created_by=ADMIN, collaborative=False, default_scope="user")
    db_knowledge_libraries.attach(SOURCE, TARGET, writable=False, created_by=ADMIN)
    u = _admin()
    cases = (
        ("hand_files", {"paths": ["a.md"], "_agent": "page-solo"}, "Page Solo has no shared space to hand files to."),
        ("hand_files", {"paths": ["a.md"], "_agent": "nobody"}, "There is no agent named nobody."),
        ("share_app", {"app_slug": "gone", "target_agent": TARGET, "_agent": SOURCE},
         'There is no app named "gone" on Page Source.'),
        ("share_app", {"app_slug": "board", "target_agent": "page-solo", "_agent": SOURCE},
         "Page Solo has no shared apps."),
        ("share_app", {"app_slug": "board", "target_agent": AGENT, "_agent": SOURCE},
         'An app named "board" is already on Page Agent.'),
        ("attach_library", {"source_agent": TARGET, "target_agent": AGENT, "_agent": AGENT},
         "Page Target has no shared knowledge library there."),
        ("attach_library", {"source_agent": SOURCE, "target_agent": TARGET, "_agent": AGENT},
         "Page Target already reads the shared knowledge library of Page Source."),
        ("update_followed_app", {"app_slug": "gone", "mode": "update", "_agent": AGENT},
         'There is no app named "gone" on Page Agent.'),
        ("move_agent", {"agent": "nobody", "department_id": "d", "level_id": "l"}, "There is no agent named nobody."),
        ("move_agent", {"agent": AGENT, "department_id": "no-such", "level_id": "l"}, "Department not found."),
    )
    recorded: set = set()
    for name, params, expected in cases:
        async def one(name=name, params=params, expected=expected):
            assert await actions.get(name).check(params, u) == expected

        recorded |= _record(loop_db_guard, one)

    async def writable_words():
        words = await actions.get("attach_library").words(
            {"source_agent": SOURCE, "target_agent": TARGET, "writable": True, "_agent": AGENT}, u)
        assert words["title"] == "Let Page Target write to the library of Page Source"

    recorded |= _record(loop_db_guard, writable_words)
    _check("card catalog: refusals", recorded)


def test_card_catalog_draw_and_click(card_world, loop_db_guard):
    """A real draw with its options, and a real click of ``move_agent`` by
    the person who asked and by another person, whose press meets the
    action's check first."""
    from services.cards import cards
    from storage import database as db
    from storage.agents import db_departments
    _catalog()
    st = card_world.st
    other = db_departments.create_department("Ops", ADMIN, level_names=["Head", "Team"])
    db.create_chat("chat-page", ADMIN, AGENT)
    db.update_chat("chat-page", session_id="sid-page")
    u = _admin()

    async def draw(action, params, **kw):
        return await cards.draw(chat_id="chat-page", session_id="sid-page", agent=AGENT, person_sub=ADMIN,
                                buttons=[{"id": "go", "kind": st.BUTTON_DO, "action": action, "params": params}],
                                note="", u=u, **kw)

    drawn = {}

    async def the_draws():
        drawn["move"] = await draw("move_agent", card_world.params["move_agent"])
        drawn["install"] = await draw("install_agent", card_world.params["install_agent"])

    recorded = _record(loop_db_guard, the_draws)
    assert drawn["install"]["options"] == [{"id": "delegation_edge", "label": "Let Page Agent hand work to the new agent",
                                            "default": True}]
    outcomes = {}

    async def the_askers_click():
        outcomes["asker"] = await cards.act(drawn["move"], "go", None, u)

    recorded |= _record(loop_db_guard, the_askers_click)
    assert outcomes["asker"]["outcome"]["text"] == 'Page Agent is now in "Eng" as Team.'
    assert agent_store.get_agent(AGENT)["department_id"] == card_world.dept["id"]

    other_move = {"agent": AGENT, "department_id": other["id"], "level_id": other["levels"][1]["id"]}
    card = asyncio.run(draw("move_agent", other_move))
    with_row = _admin("user-manager")

    async def another_persons_click():
        outcomes["other"] = await cards.act(card, "go", None, with_row)

    recorded |= _record(loop_db_guard, another_persons_click)
    _check("card catalog: draw and click", recorded)
    assert outcomes["other"]["outcome"]["text"] == 'Page Agent is now in "Ops" as Team.'


def test_card_catalog_runs(card_world, loop_db_guard, monkeypatch, tmp_path):
    """Every ``run``, with what leaves the catalog stubbed: the catalog's
    install, the set's job, the promotion, the bundle's copy, the follow
    moves and the file and library routes (network, background jobs, whole
    deploys, reads of their own) are other modules'. The words a run ends on
    are read for real."""
    actions = _catalog()
    from api.cards import actions_team
    from services.apps import app_blueprints, app_deploy, app_follow, releases

    async def fake_install(*, template_slug, target_slug, installer_user_sub, installer_role, display_name):
        await asyncio.to_thread(agent_store.create_agent, target_slug, display_name or target_slug,
                                created_by=installer_user_sub)
        return {"agent_slug": target_slug}

    async def fake_start(dset, registry, name, u, *, consent, choices=None):
        return {"id": "job-page"}

    async def fake_promote(agent, username, rel, *, role, user_sub, dst_rel, caps):
        return object()

    monkeypatch.setattr(actions.community_agent_installer, "install_from_catalog", fake_install)
    monkeypatch.setattr(actions.department_set_installer, "start", fake_start)
    monkeypatch.setattr("services.memory.memory_promote.promote", fake_promote)
    monkeypatch.setattr(releases, "live_release_dir", lambda row: None)
    monkeypatch.setattr(app_blueprints, "export_bundle", lambda row, release_dir=None: (b"", {}))
    monkeypatch.setattr(app_blueprints, "read_bundle", lambda data: (tmp_path / "bundle", {}))
    monkeypatch.setattr(app_blueprints, "import_folder", AsyncMock(return_value={"status": app_deploy.RESULT_OK}))
    # Their own routes' cached reads: api/agents/_common.py, knowledge_libraries.py.
    monkeypatch.setattr(actions_team, "move_agent_paths", AsyncMock(return_value={"moved": ["a.md"], "failed": []}))
    monkeypatch.setattr(actions_team.libraries_api, "attach_knowledge_library", AsyncMock(return_value={"created": True}))
    done = {"release": 2, "text": "Release 2 waits for approval."}
    monkeypatch.setattr(app_follow, "take", AsyncMock(return_value=done))
    monkeypatch.setattr(app_follow, "send_back", AsyncMock(return_value=done))
    u = _admin()
    outcomes = {}
    recorded: set = set()
    for name, params in card_world.params.items():
        options = {"delegation_edge": True} if name == "install_agent" else {}

        async def one(name=name, params=params, options=options):
            outcomes[name] = await actions.get(name).run(params, options, u)

        recorded |= _record(loop_db_guard, one)
    _check("card catalog: runs", recorded)
    st = card_world.st
    assert {name: o.status for name, o in outcomes.items()} == {
        "install_agent": st.OUTCOME_DONE, "install_department_set": st.OUTCOME_RUNNING, "move_agent": st.OUTCOME_DONE,
        "hand_files": st.OUTCOME_DONE, "promote_memory": st.OUTCOME_DONE, "share_app": st.OUTCOME_DONE,
        "attach_library": st.OUTCOME_DONE, "update_followed_app": st.OUTCOME_DONE,
        "send_release_to_source": st.OUTCOME_DONE, "create_department": st.OUTCOME_DONE,
        "set_department": st.OUTCOME_DONE}
    assert outcomes["install_agent"].text.startswith("New crew is installed as crew-new")
    assert "Page Agent can hand work to it." in outcomes["install_agent"].text
    assert outcomes["promote_memory"].text == 'The topic "suppliers.md" is now shared with everyone on Page Agent.'
    assert outcomes["share_app"].text == 'The app "board" is live on Page Target.'
    assert outcomes["attach_library"].text == "Page Target now reads the library of Page Source."


def test_assign_reads_nothing_on_the_loop(card_world, loop_db_guard):
    """The agent PATCH route's and the set installer's own way in, with the
    refusal of an agent that is gone."""
    from services.departments import assignment
    u = _admin()

    async def assign():
        await assignment.assign(AGENT, card_world.dept["id"], card_world.level, u)
        with pytest.raises(assignment.AssignmentRefused):
            await assignment.assign("nobody", card_world.dept["id"], card_world.level, u)

    _check("department assignment", _record(loop_db_guard, assign))


# ---------------------------------------------------------------------------
# The phone servers page
# ---------------------------------------------------------------------------

def test_phone_servers(temp_db, loop_db_guard, monkeypatch):
    """Every route that answers a decorated server, with the adapter stubbed
    where the route talks to the box."""
    from api.phone import phone
    monkeypatch.setattr(phone, "notify_phone_config_changed", AsyncMock())
    result = SimpleNamespace(status="verified", detail="fine")
    health = SimpleNamespace(healthy=True, detail="fine")
    adapter = SimpleNamespace(verify_bootstrap=AsyncMock(return_value=result),
                              apply_bootstrap=AsyncMock(return_value=result),
                              health_check=AsyncMock(return_value=health))
    monkeypatch.setattr(phone, "_load_adapter", AsyncMock(return_value=adapter))
    u = _admin()
    seen = {}

    async def the_pages():
        made = await phone.create_phone_server(
            phone.PhoneServerCreate(name="pbx1", host="h1", ami_secret="s3cret"), user=u)
        sid = made["id"]
        seen["created"] = made
        seen["listed"] = await phone.list_phone_servers(user=u)
        seen["updated"] = await phone.update_phone_server(sid, phone.PhoneServerUpdate(name="pbx2"), user=u)
        seen["default"] = await phone.set_phone_server_default(sid, user=u)
        seen["verified"] = await phone.verify_phone_server_bootstrap(sid, user=u)
        seen["applied"] = await phone.apply_phone_server_bootstrap(sid, {}, user=u)
        seen["health"] = await phone.check_phone_server_health(sid, user=u)

    _check("phone servers", _record(loop_db_guard, the_pages))
    assert seen["created"]["ami_secret_configured"] is True
    assert seen["listed"]["servers"][0]["ami_secret_configured"] is True
    assert seen["listed"]["servers"][0]["twilio_auth_token_configured"] is False
    assert seen["updated"]["name"] == "pbx2"
    assert seen["health"]["last_health_status"] == "healthy"


# ---------------------------------------------------------------------------
# The target status poll
# ---------------------------------------------------------------------------

def test_target_status(temp_db, loop_db_guard, monkeypatch):
    """The three shapes of the dot, in the states where
    ``get_live_machine_status`` would read the machine again when not handed
    it: a machine connected with no last-seen of its own, a machine away, a
    machine paused, and a target whose row is gone."""
    from api.agents.discovery import get_agent_target_status
    from core.remote import satellite_connection
    from storage import remote_store
    agent_store.create_agent(AGENT, "Page Agent", created_by=ADMIN)
    agent_store.create_agent(SOURCE, "Page Source", created_by=ADMIN)
    agent_store.create_agent(TARGET, "Page Target", created_by=ADMIN)
    agent_store.update_agent(SOURCE, execution_target="m-rack")
    agent_store.update_agent(TARGET, execution_target="m-gone")
    remote_store.create_remote_machine("m-own", "Desk", ADMIN, pairing_scope="user")
    remote_store.set_user_remote_target(ADMIN, "m-own", AGENT)
    remote_store.create_remote_machine("m-rack", "Rack", ADMIN)
    connected = {"m-own": SimpleNamespace(last_heartbeat=time.monotonic(), last_seen_iso="")}
    cm = satellite_connection.get_connection_manager()
    monkeypatch.setattr(cm, "get_connection", connected.get)
    u = _admin()
    seen = {}

    async def three_shapes():
        seen["own"] = await get_agent_target_status(AGENT, user=u)
        seen["admin"] = await get_agent_target_status(SOURCE, user=u)
        seen["gone"] = await get_agent_target_status(TARGET, user=u)
        seen["local"] = await get_agent_target_status("page-local", user=u)

    _check("target status", _record(loop_db_guard, three_shapes))
    assert seen["own"]["scope"] == "user" and seen["own"]["machine_name"] == "Desk" and seen["own"]["state"] == "online"
    assert seen["admin"]["scope"] == "admin" and seen["admin"]["machine_name"] == "Rack"
    assert seen["admin"]["state"] == "never_connected"
    assert seen["gone"] == {"state": None} and seen["local"] == {"state": None}

    remote_store.set_paused("m-rack", True)

    async def paused():
        seen["paused"] = await get_agent_target_status(SOURCE, user=u)

    _check("target status", _record(loop_db_guard, paused))
    assert seen["paused"]["state"] == "paused"


# ---------------------------------------------------------------------------
# A session's payer
# ---------------------------------------------------------------------------

SHARED = "page-shared"
PAYER = "user-admin"


@pytest.fixture
def payer_world(temp_db, monkeypatch):
    """A Shared-only agent's chat on a live session warmed as ``vic``, bound
    to a payer before a restart: the maps do not know it, so its read goes to
    the store. The agents' cache stays warm in these flows: the gate in front
    of the payer (``is_shared_only``) stays on the loop, since moving it would
    cost every send a hop for a read that is cached."""
    from auth.path_policy import SecurityContext
    from core.events.stream_pump import _active_pumps
    from core.session.session_state import set_session_security
    from core.session.visibility import shared_chat_owner
    from services.engines import subscription_pool
    from storage import database as db
    from tests.fixtures.ws_dashboard_harness import FakeExecutionLayer
    agent_store.create_agent(SHARED, "Page Shared", collaborative=False, default_scope="agent", created_by=ADMIN)
    cid = str(uuid.uuid4())
    sid = f"sid-{uuid.uuid4().hex[:8]}"
    db.create_chat(cid, shared_chat_owner(SHARED), SHARED)
    db.update_chat(cid, session_id=sid)
    set_session_security(sid, SecurityContext(role="manager", username="vic", agent=SHARED, is_admin_agent=False))

    def bind(session_id: str, payer: str) -> None:
        subscription_pool.bind_session(session_id, "sub-page", layer="claude-code-cli", user_sub=payer)
        with subscription_pool._session_maps_lock:
            subscription_pool._session_subscriptions.pop(session_id, None)
            subscription_pool._session_binding_ctx.pop(session_id, None)

    bind(sid, PAYER)
    layer = FakeExecutionLayer()
    layer.alive.add(sid)
    conn = _bare_connection(cid, sid, user_sub=MEMBER, agent=SHARED, layer=layer)
    conn.user = {"role": "member", "username": "vic"}
    conn.user_agents = [SHARED]
    conn.agent_roles = {SHARED: "manager"}
    recycled: list[str] = []

    async def _recycle(why):
        recycled.append(why)

    conn._recycle_session = _recycle
    yield SimpleNamespace(cid=cid, sid=sid, layer=layer, conn=conn, recycled=recycled, bind=bind, db=db,
                          security=SecurityContext, set_security=set_session_security)
    _active_pumps.pop(cid, None)
    with subscription_pool._session_maps_lock:
        subscription_pool._session_binding_ctx.clear()
        subscription_pool._session_subscriptions.clear()
        subscription_pool._session_scope_keys.clear()


def _ready_to_deliver(world) -> None:
    """``_deliver_queued``'s gates ahead of the sender check and the turn
    behind it, which have flows and tests of their own."""
    conn = world.conn
    conn._closed_by_revalidation = False
    conn._held_gate = ""
    conn._revalidate_session = lambda: True
    conn._deny_task_continue = AsyncMock(return_value="")
    conn._sender_refusal = AsyncMock(return_value="")
    conn._resolve_layer_for_chat_async = AsyncMock(return_value=world.layer)
    conn._push_batch_attachments = AsyncMock()
    conn._prepared_prompt = AsyncMock(return_value=("the prompt", None))
    conn._restore_abort_flags = lambda *a: None
    conn._start_new_stream = AsyncMock(return_value=None)


def _queued_item() -> SimpleNamespace:
    from core.events.common_events import TurnInput
    return SimpleNamespace(item=TurnInput("hi"), queue_id="q-page", waiting="")


async def _warm_agents() -> None:
    await asyncio.to_thread(agent_store.get_agent, SHARED)


def test_session_payer_on_a_send(payer_world, loop_db_guard):
    """A send on a session another person pays for, its binding read from the
    store: recycled, and nothing read on the loop."""
    w = payer_world
    outcomes = []

    async def heal():
        await _warm_agents()
        outcomes.append(await w.conn._heal_viewed_session({}))

    _check("session payer: a send", _record(loop_db_guard, heal))
    assert w.recycled and outcomes[0] == "ready"


def test_session_payer_on_a_queued_turn(payer_world, loop_db_guard):
    """A queued batch on a session another person pays for: closed, and
    nothing read on the loop."""
    from core.events import input_queue
    w = payer_world
    _ready_to_deliver(w)
    outcomes = []

    async def deliver():
        await _warm_agents()
        outcomes.append(await w.conn._deliver_queued(w.cid, [_queued_item()]))

    _check("session payer: a queued turn", _record(loop_db_guard, deliver))
    assert w.layer.closed_sessions == [w.sid]
    assert outcomes[0] == (input_queue.DELIVERY_DEFERRED, "the turn could not start")


def test_session_payer_on_a_key_turn(payer_world, loop_db_guard, monkeypatch):
    """A key's turn on a session that does not run as its sender: the
    username and the payer come in one job, and the close follows it."""
    from api.public import key_turns
    from core.session import session_delivery
    w = payer_world
    monkeypatch.setattr(session_delivery, "chat_layer", AsyncMock(return_value=w.layer))
    key = UserContext(sub=MEMBER, email="", name="", role="member", api_key_runs_unasked=True)
    _set_username(MEMBER, "vic")
    chat = w.db.get_chat(w.cid)

    async def ready():
        await key_turns._session_ready(key, chat, "default")

    _check("session payer: a key turn", _record(loop_db_guard, ready))
    assert w.layer.closed_sessions == [w.sid]




@pytest.mark.parametrize("bound_to,closed", [(MEMBER, False), (PAYER, True)])
def test_the_key_door_closes_only_a_session_bound_to_another_person(payer_world, monkeypatch, bound_to,
                                                                     closed):
    """The session runs as the key's sender in every other way (their
    username, the chat's mode, the dashboard kind, no remembered Allow): it
    is kept when it is bound to the sender, and closed when another person
    pays for it."""
    from api.public import key_turns
    from core.session import session_delivery, session_kind, session_state
    w = payer_world
    w.bind(w.sid, bound_to)
    monkeypatch.setattr(session_delivery, "chat_layer", AsyncMock(return_value=w.layer))
    monkeypatch.setattr(session_state, "recorded_session_mode", lambda sid: "default")
    monkeypatch.setattr(session_state, "get_session_client_type", lambda sid: session_kind.DASHBOARD.name)
    monkeypatch.setattr(session_state, "remembers_tool_allows", lambda sid: False)
    key = UserContext(sub=MEMBER, email="", name="", role="member", api_key_runs_unasked=True)
    _set_username(MEMBER, "vic")
    asyncio.run(key_turns._session_ready(key, w.db.get_chat(w.cid), "default"))
    assert (w.layer.closed_sessions == [w.sid]) is closed
def test_the_payer_of_a_known_session_takes_no_hop(payer_world):
    """The common case: the in-memory maps know the payer, so nothing is
    awaited between the checks around it."""
    from services.engines import subscription_pool
    from ws.dashboard_chat_send import ChatSendMixin
    w = payer_world
    subscription_pool.bind_session(w.sid, "sub-page", layer="claude-code-cli", user_sub=PAYER)
    read = ChatSendMixin._session_payer(w.sid)
    with pytest.raises(StopIteration) as answered:
        read.send(None)
    assert answered.value.value == PAYER


def _parked_payer_read(monkeypatch) -> SimpleNamespace:
    """``get_session_payer_sub`` parked in its worker until the test lets it
    go, with the sessions it was asked about."""
    from services.engines import subscription_pool
    real = subscription_pool.get_session_payer_sub
    parked = SimpleNamespace(entered=threading.Event(), release=threading.Event(), asked=[])

    def read(sid):
        parked.asked.append(sid)
        parked.entered.set()
        assert parked.release.wait(5)
        return real(sid)

    monkeypatch.setattr(subscription_pool, "get_session_payer_sub", read)
    return parked


def test_a_pump_started_during_the_payers_read_keeps_its_session_on_a_send(payer_world, monkeypatch):
    """Another socket's send starts a turn on the session while this send
    reads the payer: the session stays, so that turn runs on."""
    from core.events.stream_pump import _active_pumps
    w = payer_world
    parked = _parked_payer_read(monkeypatch)

    async def scenario():
        await _warm_agents()
        heal = asyncio.create_task(w.conn._heal_viewed_session({}))
        await asyncio.to_thread(parked.entered.wait, 5)
        _active_pumps[w.cid] = SimpleNamespace(is_done=False)
        parked.release.set()
        return await heal

    assert asyncio.run(scenario()) == "ready"
    assert w.recycled == []


def test_a_pump_started_during_the_payers_read_keeps_its_session_on_a_queued_turn(payer_world, monkeypatch):
    """The same for a queued batch: the session stays and the batch waits for
    that pump's end, which arms the queue again."""
    from core.events import input_queue
    from core.events.stream_pump import _active_pumps
    w = payer_world
    _ready_to_deliver(w)
    parked = _parked_payer_read(monkeypatch)

    async def scenario():
        await _warm_agents()
        deliver = asyncio.create_task(w.conn._deliver_queued(w.cid, [_queued_item()]))
        await asyncio.to_thread(parked.entered.wait, 5)
        _active_pumps[w.cid] = SimpleNamespace(is_done=False)
        parked.release.set()
        return await deliver

    outcome, _why = asyncio.run(scenario())
    assert outcome == input_queue.DELIVERY_DEFERRED
    assert w.layer.closed_sessions == []


def test_a_session_replaced_during_the_payers_read_is_judged_on_its_own_payer(payer_world, monkeypatch):
    """A warmup re-homes the connection during the read: the old session's
    payer says nothing of the new one, which this person pays for."""
    w = payer_world
    fresh = f"sid-{uuid.uuid4().hex[:8]}"
    w.bind(fresh, MEMBER)
    w.set_security(fresh, w.security(role="manager", username="vic", agent=SHARED, is_admin_agent=False))
    parked = _parked_payer_read(monkeypatch)

    async def scenario():
        await _warm_agents()
        heal = asyncio.create_task(w.conn._heal_viewed_session({}))
        await asyncio.to_thread(parked.entered.wait, 5)
        w.conn.session_id = fresh
        parked.release.set()
        return await heal

    assert asyncio.run(scenario()) == "ready"
    assert parked.asked == [w.sid, fresh]
    assert w.recycled == []


# ---------------------------------------------------------------------------
# The attach paths and the turn's token guard
# ---------------------------------------------------------------------------

@pytest.fixture
def attach_world(temp_db, monkeypatch):
    """A chat on a live session, its layer the engine's own through the real
    resolver."""
    from storage import database as db
    from tests.fixtures.ws_dashboard_harness import FakeExecutionLayer
    agent_store.create_agent(AGENT, "Page Agent", created_by=ADMIN)
    cid = str(uuid.uuid4())
    sid = f"sid-{uuid.uuid4().hex[:8]}"
    db.create_chat(cid, ADMIN, AGENT, execution_path="claude-code-cli")
    db.update_chat(cid, session_id=sid)
    layer = FakeExecutionLayer()
    layer.alive.add(sid)
    _engines.hold(monkeypatch, "claude-code-cli", layer)
    return SimpleNamespace(cid=cid, sid=sid, layer=layer)


def test_phone_attach(attach_world, loop_db_guard):
    """A call's warmup with its grant, through the real resolver."""
    from services.phone import call_control, in_session_calls
    from ws import phone_attach
    w = attach_world
    grant = in_session_calls.mint(session_id=w.sid, chat_id=w.cid, agent=AGENT, user_sub=ADMIN,
                                  phone_number="+30210")
    seen = {}

    async def send(frame):
        return None

    async def attach():
        seen["attach"] = await phone_attach.begin(
            {"model": AGENT, "attach_session_id": w.sid, "attach_grant": grant, "caller_phone": "+30210"}, send)

    try:
        _check("phone attach", _record(loop_db_guard, attach))
        assert seen["attach"].layer is w.layer
    finally:
        phone_attach._attaches.pop(w.cid, None)
        call_control.unbind(w.sid)
        in_session_calls.release(grant)


def test_duplex_attach(attach_world, loop_db_guard):
    """A voice session's first utterance, through the real resolver."""
    from ws import duplex_attach
    w = attach_world
    bridge = SimpleNamespace(duplex_id=f"dx-{w.cid[:8]}", sub=ADMIN, chat_id=w.cid)
    seen = {}

    async def resolve():
        seen["state"] = await duplex_attach._resolve(bridge)

    try:
        _check("duplex attach", _record(loop_db_guard, resolve))
        assert seen["state"].layer is w.layer
    finally:
        duplex_attach._states.pop(bridge.duplex_id, None)


def test_turn_token_guard(payer_world, loop_db_guard, monkeypatch):
    """A turn whose session's token is near its end, on a session bound before
    a restart (the store's answer) and on one the maps know (no read at all).
    The stand-in for the refresh ends the turn's start right there."""
    from services.engines import subscription_pool
    w = payer_world
    w.conn.deferred_model = None
    monkeypatch.setattr(subscription_pool, "session_token_expiry_ms", lambda sid: 1)
    refreshed: list[str] = []

    def refresh(sub_id, _runway):
        refreshed.append(sub_id)
        w.layer.alive.discard(w.sid)
        return True

    monkeypatch.setattr(subscription_pool, "ensure_fresh_and_fan_out", refresh)

    async def start():
        return await w.conn._start_new_stream("hi", target_session_id=w.sid, target_chat_id=w.cid,
                                              target_layer=w.layer)

    _check("turn token guard", _record(loop_db_guard, start))
    assert refreshed == ["sub-page"]
    w.layer.alive.add(w.sid)
    subscription_pool.bind_session(w.sid, "sub-page", layer="claude-code-cli", user_sub=PAYER)
    monkeypatch.setattr(subscription_pool, "get_session_subscription",
                        lambda sid: pytest.fail("the maps know this session"))
    _check("turn token guard", _record(loop_db_guard, start))
    assert refreshed == ["sub-page", "sub-page"]


def test_threads_and_the_loop_build_one_remote_layer(monkeypatch):
    """The first remote use races between the loop and store threads: one
    instance, or a session on the other is unseen by ``owns_session``."""
    from core.remote import remote_execution
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "_remote_layer", None)
    built: list[object] = []

    class _SlowLayer(remote_execution.RemoteExecutionLayer):
        def __init__(self, connection_manager):
            built.append(self)
            time.sleep(0.02)
            super().__init__(connection_manager)

    monkeypatch.setattr(remote_execution, "RemoteExecutionLayer", _SlowLayer)
    with ThreadPoolExecutor(8) as pool:
        layers = list(pool.map(lambda _i: session_manager._get_remote_layer(), range(8)))
    assert len(built) == 1
    assert {id(layer) for layer in layers} == {id(built[0])}


# ---------------------------------------------------------------------------
# A manual compact
# ---------------------------------------------------------------------------

@pytest.fixture
def binding_writer():
    """The pool's binding writes on their own thread, as in production."""
    from services.engines import subscription_pool
    subscription_pool._reopen_binding_writes()
    yield
    subscription_pool.close_binding_writes(timeout=5)


def test_manual_compact(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """The button on a warm chat: the frames, the separator's row and the
    gauge's pin."""
    from storage import database as db
    from tests.fixtures.ws_dashboard_harness import warm_new_chat
    w = _warmup_rig(monkeypatch)

    async def compact(sid):
        return {"post_tokens": 1234}

    w.layer.compact = compact
    seen = {}

    async def drive(ws):
        seen["chat_id"], _sid = await warm_new_chat(ws, w.layer, w.slug)
        ws.client_send({"type": "compact_context"})
        await _frame(ws, "context_compact")
        seen["completed"] = await _frame(ws, "context_compact")

    _check("manual compact", _ws_record(loop_db_guard, w.cookie, drive))
    assert seen["completed"]["phase"] == "completed" and seen["completed"]["chat_id"] == seen["chat_id"]
    assert [r["role"] for r in db.get_chat_messages(seen["chat_id"])] == ["event"]
    assert db.get_chat(seen["chat_id"])["context_used"] == 1234


def test_a_compact_takes_its_place_in_the_chats_lane(temp_db, monkeypatch):
    """A job already in the chat's lane lands ahead of the separator and one
    queued after lands behind it; the reminder is armed before the write
    waits; a socket that moved to another chat during the compaction leaves
    the row and the frame on the chat that compacted."""
    from core.events import chat_writer
    from services.memory import memory_nudge
    from storage import database as db
    from tests.fixtures.ws_dashboard_harness import FakeExecutionLayer
    agent_store.create_agent(AGENT, "Page Agent", created_by=ADMIN)
    cid = str(uuid.uuid4())
    db.create_chat(cid, ADMIN, AGENT)
    sid = f"sid-{uuid.uuid4().hex[:8]}"
    layer = FakeExecutionLayer()
    conn = _bare_connection(cid, sid, user_sub=ADMIN, agent=AGENT, layer=layer)

    async def compact(_sid):
        conn.chat_id = "another-chat"
        return {"post_tokens": 7}

    layer.compact = compact
    open_lane = threading.Event()

    def held_job():
        assert open_lane.wait(5)
        db.add_chat_message(cid, "user", "before", author_sub=ADMIN)

    real_submit = chat_writer.submit
    armed_when_queued: list[bool] = []

    async def scenario():
        compact_queued = asyncio.Event()

        def submit(chat_id, job, *, label=""):
            queued = real_submit(chat_id, job, label=label)
            if label == "compact":
                armed_when_queued.append(sid in memory_nudge._armed)
                compact_queued.set()
            return queued

        monkeypatch.setattr(chat_writer, "submit", submit)
        held = real_submit(cid, held_job, label="held")
        handler = asyncio.create_task(conn._handle_compact_context())
        await asyncio.wait_for(compact_queued.wait(), 5)
        after = real_submit(cid, lambda: db.add_chat_message(cid, "user", "after", author_sub=ADMIN), label="after")
        open_lane.set()
        await asyncio.gather(held, handler, after)

    asyncio.run(scenario())
    assert armed_when_queued == [True]
    rows = db.get_chat_messages(cid)
    assert [r["content"] or r["role"] for r in rows] == ["before", "event", "after"]
    assert conn.sent[-1]["phase"] == "completed" and conn.sent[-1]["chat_id"] == cid
    assert db.get_chat(cid)["context_used"] == 7


def test_an_engine_switch_resolves_its_layer_off_the_loop(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """Narrower than a flow: the switch's re-check and rebind stay on the loop
    in their no-await window, so only the layer's re-homing with no pin is
    judged, through the real resolver. The liveness gates' own resolve
    (``chat_process_alive``, ws/dashboard.py) is not this row's."""
    from services.remote import remote_status
    from storage import database as db
    real_resolver = remote_status.resolve_execution_target
    w = _warmup_rig(monkeypatch)
    monkeypatch.setattr(remote_status, "resolve_execution_target", real_resolver)
    monkeypatch.setattr("ws.dashboard.chat_process_alive", AsyncMock(return_value=False))
    agent_store.update_agent(w.slug, execution_path="claude-code-cli", execution_paths=json.dumps(["codex-cli"]))
    from storage.billing import subscription_store
    subscription_store.add_subscription("codex-cli", "openai", "api_key", owner_sub=ADMIN, use_personal=True)
    subscription_store.add_model("codex-cli", w.model, "Test", provider="openai", tier=2)
    cid = str(uuid.uuid4())
    db.create_chat(cid, ADMIN, w.slug, "default", model=w.model, execution_path="claude-code-cli")
    db.update_chat(cid, session_id=str(uuid.uuid4()), execution_target="")
    seen = {}

    async def switch(ws):
        ws.client_send({"type": "resume_chat", "chat_id": cid})
        await _frame(ws, "queue_snapshot")
        ws.client_send({"type": "switch_engine", "execution_path": "codex-cli", "model": w.model})
        seen["ack"] = await _frame(ws, "engine_switched")

    recorded = _ws_record(loop_db_guard, w.cookie, switch)
    assert seen["ack"]["execution_path"] == "codex-cli"
    assert not {site for site in recorded
                if site[0] in ("core/session/session_manager.py", "services/remote/remote_status.py")}
    assert seen["ack"]["chat_id"] == cid


def test_a_memory_write_takes_no_store_hop_before_its_answer_on_a_shared_path(temp_db, monkeypatch, tmp_path):
    """Only a personal path has a Shared-only answer to read: a shared one takes
    no queue on the bulk lane before the tool's answer."""
    import config
    from services.memory import memory_file, memory_publish
    from services.notifications import notification_manager
    from services.remote import workspace_fanout
    agent_dir = config.AGENTS_DIR / "agent"   # where every memory root lives
    root = agent_dir / "users" / "u" / "memory"
    root.mkdir(parents=True)
    (root / "topic.md").write_text("x")
    shared = agent_dir / "memory"
    shared.mkdir()
    (shared / "topic.md").write_text("x")
    hop = AsyncMock(return_value=False)
    monkeypatch.setattr(workspace_fanout, "run_db", hop)
    monkeypatch.setattr(workspace_fanout, "has_fanout_candidates", lambda *a, **k: False)
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", AsyncMock())

    async def publish(where):
        await memory_publish.publish_changes(
            "agent", agent_dir, where, memory_file.OpResult(output="", changed=["topic.md"]), writer_slug=None)

    asyncio.run(publish(shared))
    hop.assert_not_awaited()
    asyncio.run(publish(root))
    hop.assert_awaited_once()
