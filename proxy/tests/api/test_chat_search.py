"""GET /v1/chats/search: the newest matching chats first, every word of the
query parsed the way the index parses text, and a chat's newest words always
indexed.

The search used to rank by ``ts_rank`` and answer twenty rows: an old chat
that names a word often beat the recent one that names it once, which then
fell off the page the sidebar groups by day. Its query side also stripped
every character but letters, digits, ``-`` and ``_``, so ``config.env`` or
``1.8.0`` never matched the one token the index holds for them.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage import database as task_store
from storage.chat import db_chats
from storage.pg import get_conn

client = TestClient(app)

AGENT = "agent-search"
OWNER = "user-searcher"


def _user(sub=OWNER, agent_role="editor"):
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=[AGENT], agent_roles={AGENT: agent_role})


def _owner() -> UserContext:
    return _user()


@pytest.fixture(autouse=True)
def _as_owner():
    app.dependency_overrides[get_current_user] = _owner
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _chat(*texts: str, when: str | None = None, owner: str = OWNER) -> str:
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, owner, AGENT)
    for t in texts:
        task_store.add_chat_message(cid, "user", t)
    if when:
        with get_conn() as conn:
            conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (when, cid))
            conn.commit()
    return cid


def _search(q: str, **params) -> list[str]:
    r = client.get("/v1/chats/search", params={"agent": AGENT, "q": q, **params})
    assert r.status_code == 200, r.text
    return [c["id"] for c in r.json()["chats"]]


def test_a_word_said_once_a_minute_ago_comes_first_past_older_heavier_chats(temp_db):
    older = [_chat(" ".join(["the rollout went wrong"] * 40),
                   when=f"2026-01-{day:02d}T10:00:00+00:00") for day in range(1, 26)]
    recent = _chat("Lunch first, then we look at the rollout notes and the budget.")
    found = _search("rollout")
    # Every match fits the default page, the recent one first, then by age.
    assert found[0] == recent
    assert set(found) == {recent, *older}
    assert found[1:] == list(reversed(older))


def test_dotted_tokens_and_split_words_match_what_the_index_holds(temp_db):
    cid = _chat("Set the key in CONFIG.ENV, ship 1.8.0, then read dashboard_warmup.py "
                "and mail admin@otodock.local")
    for q in ("config.env", "1.8.0", "1.8", "dashboard_warmup", "dashboard_warmup.py",
              "admin@otodock", "Config", "shipped", "ship config.env"):
        assert _search(q) == [cid], q
    assert _search("config.env missingword") == []


def test_odd_input_finds_what_it_names_and_never_errors(temp_db, caplog):
    """Quotes, backslashes, operators and a long paste are words to find,
    never a query error (which the search would only log)."""
    cid = _chat("Ask o'brien about C:\\Users\\x\\notes.txt and the configuration")
    _chat("the and of")
    caplog.set_level("WARNING", logger="db_chats")
    assert _search("o'brien") == [cid]
    assert _search("C:\\Users\\x") == [cid]
    assert _search("the") == []
    assert _search("... !!! ()") == []
    assert _search("o'brien & | ! <-> :* 'quoted'") == []
    # A paste past the 200 characters is cut at a word boundary.
    assert _search("ask " * 49 + "configuration") == [cid]
    assert _search("ask " * 48 + "o'brien configuration") == [cid]
    assert "chat search" not in caplog.text


def test_search_follows_the_viewer_s_own_history(temp_db):
    mine = _chat("budget review")
    _chat("budget review", owner="user-someone-else")
    assert _search("budget") == [mine]


def test_task_mode_lists_the_newest_runs_first(temp_db):
    ids = []
    for day in (3, 9, 5):
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        cid = f"task-{run_id}"
        task_store.create_chat(cid, f"task::{AGENT}", AGENT)
        task_store.create_run(run_id, f"t-{uuid.uuid4().hex[:8]}", AGENT, "schedule", None,
                              "nightly backups", scope="agent", created_by=None)
        task_store.update_run(run_id, status="completed", chat_id=cid,
                              started_at=f"2026-07-{day:02d}T00:00:00+00:00")
        task_store.add_chat_message(cid, "user", "nightly backups")
        with get_conn() as conn:
            conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s",
                         (f"2026-07-{day:02d}T00:00:00+00:00", cid))
            conn.commit()
        ids.append((day, cid))
    newest_first = [cid for _, cid in sorted(ids, reverse=True)]
    assert _search("backups", kind="tasks") == newest_first


def test_a_vector_postgres_refuses_is_built_again_from_the_newest_half(temp_db):
    """Text dense in unique tokens passes the character cap and still makes
    a vector over Postgres's 1 MB: the row is built again from a halved
    window instead of staying stale, and the newest words are found."""
    dense = " ".join(f"tok{i:06d}x{(i * 7919) % 99991:05d}" for i in range(90_000))
    cid = _chat(dense, "freshword at the end")
    assert _search("freshword") == [cid]


def test_a_chat_past_the_indexed_size_keeps_its_newest_words(temp_db, monkeypatch):
    monkeypatch.setattr(db_chats, "_SEARCH_CONTENT_MAX", 60)
    cid = _chat("ancientword " + "x" * 50, "middle filler text", "freshword at the end")
    assert _search("freshword") == [cid]
    assert _search("ancientword") == []
    # One message bigger than the cap keeps its tail.
    big = _chat("headword " + "y" * 100 + " tailword")
    assert _search("tailword") == [big]
    assert _search("headword") == []
