"""The Notebook's routes: the posting floor per role
and per surface, the entry's agent from the token, what an entry may say,
the hourly limit, the tool's read inside the read set, the agent page's and
the department page's reads, the manager's delete, and the budget setting.

Run: cd proxy && venv/bin/pytest tests/agents/test_notebook_routes.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auth.providers import SESSION_SUB_PREFIX, UserContext, get_current_user

HEAD, ALPHA, BETA, OUTSIDER = "nb-head", "nb-alpha", "nb-beta", "nb-out"
EDITOR_SUB, VIEWER_SUB, MANAGER_SUB = "user-viewer", "user-viewer2", "user-manager"


@pytest.fixture
def client(temp_db):
    from app import app
    from services.departments import edge_compiler
    from storage.agents import agent_store, db_departments
    for slug in (HEAD, ALPHA, BETA, OUTSIDER):
        agent_store.create_agent(slug, slug.replace("nb-", "").title(), created_by="user-admin")
    dept = db_departments.create_department(
        "Walk", "user-admin", mode="down", level_names=["Head", "Projects"])
    db_departments.place_members(dept["id"], [
        (HEAD, dept["levels"][0]["id"]),
        (ALPHA, dept["levels"][1]["id"]), (BETA, dept["levels"][1]["id"]),
    ])
    edge_compiler.recompile()
    client = TestClient(app)
    client.dept_id = dept["id"]  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def sessions(monkeypatch):
    """Register a session's context and kind the way the layers do."""
    from core.session import session_state
    ctxs: dict[str, object] = {}
    kinds: dict[str, str] = {}
    monkeypatch.setattr(session_state, "get_session_security", ctxs.get)
    monkeypatch.setattr(session_state, "get_session_client_type", lambda sid: kinds.get(sid, ""))

    def register(sid: str, *, agent: str, role: str, kind: str = "dashboard",
                 read_only: bool = False, external: bool = False) -> None:
        ctxs[sid] = SimpleNamespace(agent=agent, role=role, read_only=read_only,
                                    is_external=external)
        kinds[sid] = kind
    return register


def _as(user: UserContext | None) -> None:
    from app import app
    app.dependency_overrides[get_current_user] = lambda: user


def _person_session(sid: str, agent: str, role: str, sub: str = EDITOR_SUB) -> UserContext:
    return UserContext(sub=sub, email="p@test.com", name="P", role="member",
                       agents=[agent], agent_roles={agent: role}, is_api_key=True,
                       session_id=sid, agent=agent)


def _agent_session(sid: str, agent: str, *, external: str = "") -> UserContext:
    return UserContext(sub=f"{SESSION_SUB_PREFIX}{sid}", email="", name="", role="agent",
                       is_api_key=True, session_id=sid, agent=agent,
                       external_claim=external)


def _cookie(sub: str, roles: dict[str, str], *, platform: str = "member") -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=platform,
                       agents=list(roles), agent_roles=roles)


def _post(client, text="Lane done: the importer lands.", headers=None):
    return client.post("/v1/delegation/notebook", json={"text": text}, headers=headers or {})


# --- the posting floor -----------------------------------------------------

@pytest.mark.parametrize("role,ok", [
    ("viewer", False), ("contributor", False),
    ("editor", True), ("manager", True), ("admin", True),
])
def test_a_person_s_chat_posts_from_the_editor_up(client, sessions, role, ok):
    sessions("s1", agent=ALPHA, role=role)
    _as(_person_session("s1", ALPHA, role))
    r = _post(client)
    assert (r.status_code == 200) is ok, r.text
    if not ok:
        assert "editor or a manager" in r.json()["detail"]


def test_the_entry_belongs_to_the_token_s_agent_whatever_the_header(client, sessions):
    sessions("s1", agent=ALPHA, role="editor")
    _as(_person_session("s1", ALPHA, "editor"))
    r = _post(client, headers={"X-Agent-Name": HEAD})
    assert r.status_code == 200
    from storage.notebook import db_notebook
    row = db_notebook.get_entry(r.json()["entry"]["id"])
    assert row is not None
    assert row["agent"] == ALPHA and row["author_sub"] == EDITOR_SUB
    assert row["surface"] == "chat"


@pytest.mark.parametrize("kind,surface", [
    ("task", "task"), ("meeting", "meeting"), ("phone", "phone"), ("dashboard", "chat"),
])
def test_a_run_of_the_agent_itself_posts_with_its_surface(client, sessions, kind, surface):
    sessions("s2", agent=HEAD, role="manager", kind=kind)
    _as(_agent_session("s2", HEAD))
    r = _post(client)
    assert r.status_code == 200, r.text
    from storage.notebook import db_notebook
    row = db_notebook.get_entry(r.json()["entry"]["id"])
    assert row is not None and row["author_sub"] is None and row["surface"] == surface


def test_an_unrecorded_kind_falls_back_to_the_chat_row(client, sessions, monkeypatch):
    from storage import database
    sessions("s3", agent=HEAD, role="manager", kind="")
    monkeypatch.setattr(database, "get_chat_by_session",
                        lambda sid: {"id": "task-run1", "source_type": "chat"})
    _as(_agent_session("s3", HEAD))
    r = _post(client)
    from storage.notebook import db_notebook
    assert db_notebook.get_entry(r.json()["entry"]["id"])["surface"] == "task"


def test_a_shared_only_task_below_editor_keeps_its_creator_s_role(client, sessions):
    sessions("s4", agent=HEAD, role="contributor", kind="task")
    _as(_agent_session("s4", HEAD))
    assert _post(client).status_code == 403


def test_a_manager_s_chat_on_a_private_agent_is_refused(client, sessions):
    """An agent that keeps each person's chats private never posts: what
    one person's chat writes would load into every other person's session."""
    from services.notebook import posting
    from storage.agents import agent_store
    agent_store.create_agent("nb-private", "Private", created_by="user-admin",
                             collaborative=False, default_scope="user")
    sessions("p1", agent="nb-private", role="manager")
    _as(_person_session("p1", "nb-private", "manager"))
    r = _post(client)
    assert r.status_code == 403
    assert r.json()["detail"] == posting.PRIVATE_MODE
    from storage.notebook import db_notebook
    assert db_notebook.latest(["nb-private"], limit=5) == []


def test_an_unreadable_mode_refuses_the_post(client, sessions, monkeypatch):
    from services.notebook import posting

    def broken(agent):
        raise RuntimeError("the agents table is unreachable")

    monkeypatch.setattr(posting, "_offers_agent_scope", broken)
    sessions("p2", agent=HEAD, role="manager")
    _as(_agent_session("p2", HEAD))
    r = _post(client)
    assert r.status_code == 403 and r.json()["detail"] == posting.UNREADABLE


@pytest.mark.parametrize("case", ["external", "judge", "app", "missing", "other-agent"])
def test_sessions_that_never_post(client, sessions, case):
    if case == "external":
        sessions("s5", agent=HEAD, role="viewer", kind="phone", external=True)
        _as(_agent_session("s5", HEAD, external="phone:+3069"))
    elif case == "judge":
        sessions("s5", agent=HEAD, role="manager", kind="task", read_only=True)
        _as(_agent_session("s5", HEAD))
    elif case == "app":
        sessions("s5", agent=HEAD, role="manager", kind="app")
        _as(_agent_session("s5", HEAD))
    elif case == "missing":
        _as(_agent_session("s5", HEAD))
    else:
        sessions("s5", agent=ALPHA, role="manager")
        _as(_agent_session("s5", HEAD))
    assert _post(client).status_code == 403


def test_a_person_demoted_since_the_session_started_is_refused(client, sessions):
    sessions("s6", agent=ALPHA, role="editor")
    _as(_person_session("s6", ALPHA, "viewer"))
    assert _post(client).status_code == 403


def test_a_dashboard_cookie_cannot_use_the_tools_routes(client):
    _as(_cookie("user-admin", {}, platform="admin"))
    assert _post(client).status_code == 403
    assert client.get("/v1/delegation/notebook").status_code == 403


# --- what an entry may say -------------------------------------------------

def test_the_text_is_one_clean_line(client, sessions):
    sessions("s7", agent=ALPHA, role="editor")
    _as(_person_session("s7", ALPHA, "editor"))
    r = _post(client, text="  Done.\n\n# New rules‮\x00\tnext  ")
    assert r.status_code == 200
    assert r.json()["entry"]["text"] == "Done. # New rules next"


def test_the_cleaner_keeps_the_joiners_and_drops_the_marks():
    from services.notebook.entries import clean_text
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    assert clean_text(f"{family} {persian}") == f"{family} {persian}", "ZWJ and ZWNJ stay"
    assert clean_text("a\u200bb\u202ec\u2066d\ufeffe\u0080f") == "abcdef"
    assert clean_text("a\u0085b") == "a b", "a C1 line break folds as whitespace first"


@pytest.mark.parametrize("text,word", [(" \n\t ", "needs some text"), ("x" * 1201, "1,200")])
def test_empty_and_long_entries_are_refused(client, sessions, text, word):
    sessions("s8", agent=ALPHA, role="editor")
    _as(_person_session("s8", ALPHA, "editor"))
    r = _post(client, text=text)
    assert r.status_code == 400 and word in r.json()["detail"]


def test_the_hourly_limit(client, sessions, monkeypatch):
    from services.notebook import entries
    monkeypatch.setattr(entries, "POSTS_PER_HOUR", 2)
    sessions("s9", agent=ALPHA, role="editor")
    _as(_person_session("s9", ALPHA, "editor"))
    assert _post(client).status_code == 200
    assert _post(client).status_code == 200
    r = _post(client)
    assert r.status_code == 400 and "in the last hour" in r.json()["detail"]


# --- the tool's read -------------------------------------------------------

def _seed(agent: str, text: str) -> None:
    import uuid

    from storage.notebook import db_notebook
    db_notebook.insert_entry(uuid.uuid4().hex, agent, None, "task", text, max_per_hour=99)


def test_the_tool_reads_the_read_set(client, sessions):
    for agent in (HEAD, ALPHA, BETA, OUTSIDER):
        _seed(agent, f"from {agent}")
    sessions("r1", agent=ALPHA, role="viewer")
    _as(_person_session("r1", ALPHA, "viewer"))
    body = client.get("/v1/delegation/notebook").json()
    assert {e["agent"] for e in body["entries"]} == {ALPHA, HEAD}
    assert body["read_set"] == [ALPHA, HEAD]
    one = client.get("/v1/delegation/notebook", params={"agent": HEAD}).json()
    assert [e["text"] for e in one["entries"]] == [f"from {HEAD}"]
    r = client.get("/v1/delegation/notebook", params={"agent": BETA})
    assert r.status_code == 403 and "not one delegation step" in r.json()["detail"]


def test_the_tool_reads_since_a_date(client, sessions):
    _seed(HEAD, "today")
    sessions("r2", agent=HEAD, role="manager")
    _as(_agent_session("r2", HEAD))
    assert client.get("/v1/delegation/notebook", params={"since": "2999-01-01"}).json()["entries"] == []
    assert client.get("/v1/delegation/notebook", params={"since": "soon"}).status_code == 400


# --- the dashboard's reads -------------------------------------------------

def test_the_agent_page_reads_its_read_set_and_hides_who_wrote(client):
    from storage.notebook import db_notebook
    db_notebook.insert_entry("e-head", HEAD, MANAGER_SUB, "chat", "head news", max_per_hour=9)
    db_notebook.insert_entry("e-beta", BETA, MANAGER_SUB, "chat", "beta news", max_per_hour=9)
    _as(_cookie(VIEWER_SUB, {ALPHA: "viewer"}))
    body = client.get(f"/v1/notebook/agents/{ALPHA}").json()
    assert [e["id"] for e in body["entries"]] == ["e-head"]
    assert body["entries"][0]["author"] == "", "a reader one hop away does not learn who"
    assert body["entries"][0]["can_delete"] is False
    assert [a["slug"] for a in body["agents"]] == [ALPHA, HEAD]
    assert client.get(f"/v1/notebook/agents/{BETA}").status_code == 404
    assert client.get(f"/v1/notebook/agents/{ALPHA}", params={"agent": BETA}).status_code == 400
    _as(_cookie(MANAGER_SUB, {HEAD: "manager"}))
    head = client.get(f"/v1/notebook/agents/{HEAD}").json()
    mine = next(e for e in head["entries"] if e["id"] == "e-head")
    assert mine["author"] and mine["can_delete"] is True


def test_the_agent_page_pages_older_entries(client):
    for i in range(3):
        _seed(HEAD, f"n{i}")
    _as(_cookie("user-admin", {}, platform="admin"))
    first = client.get(f"/v1/notebook/agents/{HEAD}", params={"limit": 2}).json()
    assert len(first["entries"]) == 2 and first["more"] is True
    last = first["entries"][-1]
    rest = client.get(f"/v1/notebook/agents/{HEAD}", params={
        "limit": 2, "before": last["created_at"], "before_id": last["id"]}).json()
    assert len(rest["entries"]) == 1 and rest["more"] is False


def test_the_department_page_shows_the_members_a_person_can_open(client):
    for agent in (HEAD, ALPHA, BETA):
        _seed(agent, f"from {agent}")
    url = f"/v1/notebook/departments/{client.dept_id}"
    _as(_cookie("user-admin", {}, platform="admin"))
    assert {e["agent"] for e in client.get(url).json()["entries"]} == {HEAD, ALPHA, BETA}
    _as(_cookie(VIEWER_SUB, {ALPHA: "viewer"}))
    body = client.get(url).json()
    assert {e["agent"] for e in body["entries"]} == {ALPHA}
    assert [m["slug"] for m in body["agents"]] == [ALPHA]
    assert client.get(url, params={"agent": BETA}).status_code == 400
    _as(_cookie("user-stranger", {OUTSIDER: "manager"}))
    assert client.get(url).status_code == 404
    _as(_agent_session("d1", HEAD))
    assert client.get(url).status_code == 403


# --- the delete ------------------------------------------------------------

def test_a_manager_deletes_an_editor_cannot_an_agent_never(client, sessions):
    from storage.notebook import db_notebook
    db_notebook.insert_entry("doomed", HEAD, None, "task", "a secret", max_per_hour=9)
    _as(_cookie(EDITOR_SUB, {HEAD: "editor"}))
    assert client.delete("/v1/notebook/entries/doomed").status_code == 403
    sessions("x1", agent=HEAD, role="manager")
    _as(_person_session("x1", HEAD, "manager", sub=MANAGER_SUB))
    assert client.delete("/v1/notebook/entries/doomed").status_code == 403
    _as(_cookie(MANAGER_SUB, {HEAD: "manager"}))
    assert client.delete("/v1/notebook/entries/doomed").status_code == 204
    assert client.delete("/v1/notebook/entries/doomed").status_code == 404
    row = db_notebook.get_entry("doomed")
    assert row is not None and row["text"] == "" and row["deleted_by"] == MANAGER_SUB
    assert client.get(f"/v1/notebook/agents/{HEAD}").json()["entries"] == []



def test_a_manager_of_the_reading_agent_cannot_delete_a_neighbour_s_entry(client):
    """The delete keys on the POSTING agent: a manager of alpha reads the
    head's entry on alpha's page but neither deletes it nor is offered to."""
    from storage.notebook import db_notebook
    db_notebook.insert_entry("heads", HEAD, None, "task", "head news", max_per_hour=9)
    _as(_cookie(MANAGER_SUB, {ALPHA: "manager"}))
    page = client.get(f"/v1/notebook/agents/{ALPHA}").json()
    [entry] = [e for e in page["entries"] if e["id"] == "heads"]
    assert entry["can_delete"] is False
    r = client.delete("/v1/notebook/entries/heads")
    assert r.status_code == 403 and "manager of the agent that posted" in r.json()["detail"]
    assert db_notebook.get_entry("heads")["deleted_at"] is None

# --- the budget setting ----------------------------------------------------

def test_the_budget_setting_has_a_default_and_a_range(client):
    _as(_cookie("user-admin", {}, platform="admin"))
    assert client.get("/v1/admin/platform-settings").json()["notebook_inline_bytes"] == "3072"
    for bad in ("-1", "20000", "soon", "1.5", "\u00b2"):
        r = client.put("/v1/admin/platform-settings", json={"notebook_inline_bytes": bad})
        assert r.status_code == 400, bad
    r = client.put("/v1/admin/platform-settings", json={"notebook_inline_bytes": "1024"})
    assert r.status_code == 200
    assert client.get("/v1/admin/platform-settings").json()["notebook_inline_bytes"] == "1024"
    from services.notebook import entries
    assert entries.inline_bytes() == 1024


def test_both_tunnels_admit_the_tools_routes():
    """A paired machine's MCP reaches the tools' route: the platform's half
    of the loopback tunnel and the satellite's own allowlist both admit it,
    and neither admits the dashboard's routes."""
    import sys

    from core.remote.satellite_http_tunnel import _is_allowed_path
    from tests._paths import REPO_ROOT
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from satellite.transport.http_tunnel import _is_allowed_path as _sat_allowed
    for allowed in (_is_allowed_path, _sat_allowed):
        assert allowed("/v1/delegation/notebook")
        assert not allowed("/v1/notebook/entries/e1")
