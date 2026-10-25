"""Reporting an agent's reply (``api/agents/reply_reports.py``).

A person reports in a chat they can read, from the dashboard only; the
message must be an agent's row of that chat, and any row of a reply names
the reply by its first row; one report per person and reply (a repeat
answers the same and tells nobody); twenty an hour. The reply's text is
read from the chat's rows. The admins get one notification each (one per
person per ten minutes), with the speaker, the time and the reason, never
the note or the reply; the gap runs from the person's last NOTIFIED report
and the next notice counts the ones in between. Admins read the rows through
one paged GET (the Reports page), and may dismiss a report to the page's
history and restore it (nothing is deleted). The table comes with the boot
on a database from before it, the dismiss columns on one that made the table
before them, and a chat's deletion takes its reports.
"""

import json

import pytest
from fastapi.testclient import TestClient

import config
from api.agents import reply_reports
from app import app
from auth import rate_limiter
from auth.providers import UserContext, get_current_user
from services.notifications import notification_manager as nm
from storage import database as task_store, pg, schema as pg_schema
from storage.pg import get_conn

client = TestClient(app)

AGENT = "agent-rep"
OWNER = UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User",
                    role="member", agents=[AGENT], agent_roles={AGENT: "editor"})
OTHER = UserContext(sub="user-viewer2", email="viewer2@test.com", name="Viewer Two",
                    role="member", agents=[AGENT], agent_roles={AGENT: "contributor"})
ADMIN = UserContext(sub="user-admin", email="admin@test.com", name="Admin User", role="admin")
SESSION = UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User",
                      role="member", agents=[AGENT], agent_roles={AGENT: "editor"},
                      is_api_key=True)


@pytest.fixture
def as_user():
    def setup(user: UserContext | None):
        app.dependency_overrides[get_current_user] = lambda: user
    yield setup
    app.dependency_overrides.pop(get_current_user, None)


MANAGER = UserContext(sub="user-manager", email="manager@test.com", name="Manager User",
                      role="creator", agents=[AGENT], agent_roles={AGENT: "manager"})


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    monkeypatch.setattr(config, "get_platform_tz", lambda: __import__("zoneinfo").ZoneInfo("UTC"))


@pytest.fixture(autouse=True)
def _fresh_limiter():
    rate_limiter._attempts.clear()
    yield
    rate_limiter._attempts.clear()


@pytest.fixture
def fired(monkeypatch):
    calls: list[dict] = []

    async def fake_fire(**kw):
        calls.append(kw)
        return [{"id": "d"}]
    monkeypatch.setattr(nm, "fire_notification", fake_fire)
    return calls


def _chat(chat_id="chat-rep", owner="user-viewer"):
    task_store.create_chat(chat_id, owner, AGENT)
    person = task_store.add_chat_message(chat_id, "user", "Tell me a story")
    reply = task_store.add_chat_message(chat_id, "assistant", "Once upon a time.")
    return chat_id, person, reply


def _report(chat_id, message_id, reason="harmful", note=""):
    return client.post(f"/v1/chats/{chat_id}/reports",
                       json={"message_id": message_id, "reason": reason, "note": note})


def _rows():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM reply_reports ORDER BY id").fetchall()


# --- who may report ---------------------------------------------------------

def test_the_owner_reports_a_reply_and_the_admins_hear_of_it(as_user, fired):
    cid, _, reply = _chat()
    as_user(OWNER)
    r = _report(cid, reply, note="It said something cruel\nsecond line")
    assert r.status_code == 200 and r.json() == {"status": "reported"}
    [row] = _rows()
    assert (row["reporter_sub"], row["chat_id"], row["message_id"]) == ("user-viewer", cid, reply)
    assert (row["agent"], row["reason"]) == (AGENT, "harmful")
    assert row["note"] == "It said something cruel\nsecond line"
    assert row["reply_text"] == "Once upon a time."
    [call] = fired
    assert call["scope"] == "admin" and call["source"] == "report"
    assert call["source_id"] == f"report:{row['id']}"
    assert call["agent_slug"] == AGENT and call["chat_id"] == cid
    assert "Once upon a time" not in call["body"] and "Once upon a time" not in call["title"]
    assert "cruel" not in call["body"]
    assert call["body"].startswith(f"Viewer User reported a reply of {AGENT} from ")
    assert call["body"].endswith(". Reason: Harmful or offensive. A note came with it.")


def test_a_member_of_a_shared_only_agent_reports_in_the_shared_chat(as_user, fired):
    cid, _, reply = _chat(chat_id="chat-shared", owner=f"agent::{AGENT}")
    as_user(OTHER)
    assert _report(cid, reply).status_code == 200
    assert len(_rows()) == 1


def test_a_person_who_cannot_read_the_chat_is_refused(as_user, fired):
    cid, _, reply = _chat()
    as_user(OTHER)
    assert _report(cid, reply).status_code == 403
    assert not _rows() and not fired


def test_a_phone_conversation_is_reported_by_its_managers_only(as_user, fired):
    task_store.create_chat("phone-rep", "phone", AGENT, source_type="phone")
    task_store.add_chat_message("phone-rep", "user", "Caller asks")
    reply = task_store.add_chat_message("phone-rep", "assistant", "The agent answers.")
    as_user(OTHER)
    assert _report("phone-rep", reply).status_code == 403
    as_user(MANAGER)
    assert _report("phone-rep", reply).status_code == 200


def test_an_agent_scope_task_runs_chat_is_reported_by_its_readers(as_user, fired):
    run_id = "run-rep-1"
    cid = f"task-{run_id}"
    task_store.create_chat(cid, f"task::{AGENT}", AGENT)
    task_store.create_run(run_id, "t-rep", AGENT, "schedule", None, "check backups", scope="agent")
    task_store.update_run(run_id, status="completed", chat_id=cid)
    task_store.add_chat_message(cid, "user", "check backups")
    reply = task_store.add_chat_message(cid, "assistant", "Backups are fine.")
    as_user(OTHER)
    assert _report(cid, reply).status_code == 200
    assert fired[0]["agent_slug"] == AGENT and fired[0]["chat_id"] == cid


def test_the_limiter_counts_repeats_before_any_row_is_read(as_user, fired, monkeypatch):
    cid, _, reply = _chat()
    as_user(OWNER)
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "reply_report",
                        {"max": 2, "window": 3600, "base_block": 600, "max_block": 3600})
    assert _report(cid, reply).status_code == 200
    assert _report(cid, reply).status_code == 200
    r = _report(cid, reply)
    assert r.status_code == 429 and "Too many reports" in r.json()["detail"]


def test_the_reasons_and_the_note_cap_are_the_dashboards():
    """Mirrored by hand in dashboard api/replyReports.ts (its test pins the same)."""
    assert list(reply_reports.REASONS) == ["harmful", "misleading", "private", "other"]
    assert reply_reports.NOTE_MAX == 1000


def test_an_id_past_int4_is_a_clean_refusal(as_user, fired):
    cid, _, _ = _chat()
    as_user(OWNER)
    assert _report(cid, 2_147_483_648).status_code == 422
    as_user(ADMIN)
    assert client.get("/v1/admin/reply-reports?before_id=2147483648").status_code == 422


def test_an_admin_may_report_in_any_chat(as_user, fired):
    cid, _, reply = _chat()
    as_user(ADMIN)
    assert _report(cid, reply).status_code == 200


def test_an_agent_session_never_files_a_report(as_user, fired):
    cid, _, reply = _chat()
    as_user(SESSION)
    assert _report(cid, reply).status_code == 403
    assert not _rows()


def test_an_anonymous_caller_is_refused(as_user, fired):
    cid, _, reply = _chat()
    as_user(None)
    assert _report(cid, reply).status_code == 401


def test_a_message_of_another_chat_is_not_found(as_user, fired):
    cid, _, _ = _chat()
    _, _, foreign = _chat(chat_id="chat-other", owner="user-viewer")
    task_store.create_chat("chat-third", "user-viewer2", AGENT)
    as_user(OWNER)
    assert _report(cid, foreign + 1000).status_code == 404
    assert _report("chat-third", foreign).status_code == 403
    assert _report("chat-missing", foreign).status_code == 404


def test_a_persons_own_message_is_not_a_reply(as_user, fired):
    cid, person, _ = _chat()
    as_user(OWNER)
    r = _report(cid, person)
    assert r.status_code == 400 and "agent's reply" in r.json()["detail"]


def test_an_unknown_reason_and_a_long_note_are_refused(as_user, fired):
    cid, _, reply = _chat()
    as_user(OWNER)
    assert _report(cid, reply, reason="spam").status_code == 422
    assert _report(cid, reply, note="x" * (reply_reports.NOTE_MAX + 1)).status_code == 422
    assert not _rows()


def test_a_second_report_answers_the_same_and_tells_nobody(as_user, fired):
    cid, _, reply = _chat()
    as_user(OWNER)
    assert _report(cid, reply).json() == {"status": "reported"}
    second = _report(cid, reply, reason="other", note="again")
    assert second.status_code == 200 and second.json() == {"status": "reported"}
    [row] = _rows()
    assert row["reason"] == "harmful"
    assert len(fired) == 1


def test_any_row_of_a_reply_names_the_reply(as_user, fired):
    cid = "chat-rows"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Q")
    first = task_store.add_chat_message(cid, "assistant", "Part one.")
    tool = task_store.add_chat_message(cid, "event", "", event_type="tool_use", event_data="{}")
    last = task_store.add_chat_message(cid, "assistant", "Part two.")
    as_user(OWNER)
    for named in (last, tool, first):
        assert _report(cid, named).json() == {"status": "reported"}
    [row] = _rows()
    assert row["message_id"] == first and row["reply_text"] == "Part one.\n\nPart two."
    assert len(fired) == 1


def test_a_repeat_at_the_cap_still_answers(as_user, fired, monkeypatch):
    cid, _, reply = _chat()
    as_user(OWNER)
    _report(cid, reply)
    monkeypatch.setattr(reply_reports, "HOURLY_CAP", 1)
    assert _report(cid, reply).status_code == 200


def _age(report_id: int, *, created_min: int, notified_min: int | None = None) -> None:
    """Move a report back in time (the gap and the cap read the clock)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE reply_reports SET created_at = now() - make_interval(mins => %s), "
            "notified_at = CASE WHEN %s::int IS NULL THEN notified_at "
            "ELSE now() - make_interval(mins => %s::int) END WHERE id = %s",
            (created_min, notified_min, notified_min, report_id))
        conn.commit()


def test_a_report_every_few_minutes_is_told_and_counted(as_user, fired):
    """The gap runs from the last NOTIFIED report: a report every 6 minutes
    is not silenced forever, and the next notice counts the ones between."""
    task_store.create_chat("chat-steady", "user-viewer", AGENT)
    replies = []
    for i in range(3):
        task_store.add_chat_message("chat-steady", "user", f"q{i}")
        replies.append(task_store.add_chat_message("chat-steady", "assistant", f"reply {i}"))
    as_user(OWNER)
    _report("chat-steady", replies[0])
    first = _rows()[0]["id"]
    _age(first, created_min=6, notified_min=6)
    _report("chat-steady", replies[1], reason="misleading")
    assert len(fired) == 1
    second = _rows()[1]["id"]
    _age(first, created_min=12, notified_min=12)
    _age(second, created_min=6)
    _report("chat-steady", replies[2], reason="private")
    assert len(fired) == 2
    assert fired[1]["body"].endswith("Reason: Private information. And 1 more since.")
    notified = [r["notified_at"] is not None for r in _rows()]
    assert notified == [True, False, True]


def test_one_notice_per_person_in_ten_minutes(as_user, fired):
    task_store.create_chat("chat-gap", "user-viewer", AGENT)
    replies = []
    for i in range(3):
        task_store.add_chat_message("chat-gap", "user", f"q{i}")
        replies.append(task_store.add_chat_message("chat-gap", "assistant", f"reply {i}"))
    as_user(OWNER)
    for m in replies:
        assert _report("chat-gap", m).status_code == 200
    assert len(_rows()) == 3 and len(fired) == 1


def test_the_hourly_cap(as_user, fired, monkeypatch):
    monkeypatch.setattr(reply_reports, "HOURLY_CAP", 2)
    task_store.create_chat("chat-cap", "user-viewer", AGENT)
    replies = []
    for i in range(3):
        task_store.add_chat_message("chat-cap", "user", f"q{i}")
        replies.append(task_store.add_chat_message("chat-cap", "assistant", f"reply {i}"))
    as_user(OWNER)
    assert _report("chat-cap", replies[0]).status_code == 200
    assert _report("chat-cap", replies[1]).status_code == 200
    r = _report("chat-cap", replies[2])
    assert r.status_code == 429 and "Try again later" in r.json()["detail"]


# --- the reply's text --------------------------------------------------------

def test_the_text_ends_at_the_next_persons_message(as_user, fired):
    cid = "chat-text"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Q1")
    first = task_store.add_chat_message(cid, "assistant", "Part one.")
    task_store.add_chat_message(cid, "event", "", event_type="tool_use", event_data='{"name": "x"}')
    task_store.add_chat_message(cid, "assistant", "Part two.")
    task_store.add_chat_message(cid, "user", "Q2")
    task_store.add_chat_message(cid, "assistant", "Next answer.")
    as_user(OWNER)
    _report(cid, first)
    assert _rows()[0]["reply_text"] == "Part one.\n\nPart two."


def test_the_text_ends_where_a_delegate_result_opens_a_bubble(as_user, fired):
    cid = "chat-del"
    task_store.create_chat(cid, "user-viewer", AGENT)
    first = task_store.add_chat_message(cid, "assistant", "I asked a colleague.")
    result = task_store.add_chat_message(
        cid, "event", "", event_type="delegate_result",
        event_data=json.dumps({"agent": "helper", "output_text": "The helper's report."}))
    task_store.add_chat_message(cid, "assistant", "Here is the summary.")
    as_user(OWNER)
    _report(cid, first)
    _report(cid, result, reason="misleading")
    texts = [(r["message_id"], r["agent"], r["reply_text"]) for r in _rows()]
    assert texts == [(first, AGENT, "I asked a colleague."),
                     (result, "helper", "The helper's report.")]


def test_a_meeting_speakers_turn_is_their_own_reply(as_user, fired):
    cid = "chat-meet"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Discuss.")
    start = task_store.add_chat_message(cid, "event", "", event_type="system", event_data=json.dumps(
        {"subtype": "meeting_turn_start", "agent": "alpha"}))
    alpha = task_store.add_chat_message(cid, "assistant", "Alpha speaks.",
                                        event_data=json.dumps({"agent_slug": "alpha"}))
    beta = task_store.add_chat_message(cid, "assistant", "Beta speaks.",
                                       event_data=json.dumps({"agent_slug": "beta"}))
    as_user(OWNER)
    _report(cid, alpha)
    _report(cid, beta, reason="other")
    rows = [(r["message_id"], r["agent"], r["reply_text"]) for r in _rows()]
    assert rows == [(start, "alpha", "Alpha speaks."), (beta, "beta", "Beta speaks.")]
    # The link is the chat's own: never the speaker's page.
    assert fired[0]["agent_slug"] == AGENT


def test_a_long_reply_is_capped(as_user, fired):
    cid = "chat-long"
    task_store.create_chat(cid, "user-viewer", AGENT)
    reply = task_store.add_chat_message(cid, "assistant", "y" * (reply_reports.REPLY_TEXT_MAX + 50))
    as_user(OWNER)
    _report(cid, reply)
    text = _rows()[0]["reply_text"]
    assert len(text) == reply_reports.REPLY_TEXT_MAX and text.endswith("…")


def test_a_reply_with_no_text_is_refused(as_user, fired):
    cid = "chat-empty"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Draw it")
    tool = task_store.add_chat_message(cid, "event", "", event_type="tool_use", event_data="{}")
    as_user(OWNER)
    r = _report(cid, tool)
    assert r.status_code == 400 and r.json()["detail"] == "This reply has no text to report."
    assert not _rows() and not fired


def test_an_event_row_inside_a_reply_names_the_reply(as_user, fired):
    cid = "chat-event"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Q")
    tool = task_store.add_chat_message(cid, "event", "", event_type="tool_use", event_data="{}")
    task_store.add_chat_message(cid, "assistant", "Found it.")
    as_user(OWNER)
    _report(cid, tool)
    [row] = _rows()
    assert (row["message_id"], row["reply_text"]) == (tool, "Found it.")


def test_a_review_naming_both_kinds_is_one_reply(as_user, fired):
    """A run's review that names agents and commands stores two nudge rows
    in a row; the chat draws one bubble, so any of its rows names the first
    and the text is the review's."""
    cid = "chat-review"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Run the jobs")
    task_store.add_chat_message(cid, "assistant", "Started them.")
    task_store.add_chat_message(cid, "event", "", event_type="metadata", event_data="{}")
    agents = task_store.add_chat_message(cid, "event", "", event_type="bg_nudge",
                                         event_data='{"count": 1, "labels": []}')
    commands = task_store.add_chat_message(cid, "event", "", event_type="bg_command_nudge",
                                           event_data='{"count": 2, "labels": []}')
    text = task_store.add_chat_message(cid, "assistant", "All three finished.")
    task_store.add_chat_message(cid, "user", "Thanks")
    as_user(OWNER)
    for named in (agents, commands, text):
        rate_limiter._attempts.clear()
        assert _report(cid, named).status_code == 200
    # One row: the reports from the second kind's row and the text row were
    # repeats of the first.
    assert [(r["message_id"], r["reply_text"]) for r in _rows()] == [(agents, "All three finished.")]


def test_a_hidden_slash_command_row_does_not_end_the_reply(as_user, fired):
    cid = "chat-slash"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Q")
    first = task_store.add_chat_message(cid, "assistant", "Before.")
    task_store.add_chat_message(cid, "user", "<command-name>/compact</command-name><command-args></command-args>")
    task_store.add_chat_message(cid, "assistant", "After.")
    as_user(OWNER)
    _report(cid, first)
    assert _rows()[0]["reply_text"] == "Before.\n\nAfter."


def test_the_walk_back_stops_at_rows_max(as_user, fired, monkeypatch):
    monkeypatch.setattr(reply_reports, "ROWS_MAX", 3)
    cid = "chat-rowsmax"
    task_store.create_chat(cid, "user-viewer", AGENT)
    task_store.add_chat_message(cid, "user", "Q")
    rows = [task_store.add_chat_message(cid, "assistant", f"part {i}") for i in range(6)]
    as_user(OWNER)
    assert _report(cid, rows[-1]).status_code == 200
    [row] = _rows()
    # Three rows back from the named one: the walk ends there; three rows on
    # from that, the text is cut and says so.
    assert row["message_id"] == rows[2]
    assert row["reply_text"] == "part 2\n\npart 3\n\npart 4…"


def test_the_body_shows_no_link_and_no_note(as_user, fired):
    with get_conn() as conn:
        conn.execute("UPDATE users SET display_name = 'Eve https://example.com' WHERE sub = 'user-viewer'")
        conn.commit()
    cid, _, reply = _chat()
    as_user(OWNER)
    _report(cid, reply, reason="other", note="see https://example.com/x for proof")
    body = fired[0]["body"]
    assert "://" not in body and "proof" not in body
    assert body.startswith("Eve example.com reported a reply of ")
    assert _rows()[0]["note"] == "see https://example.com/x for proof"


def test_the_body_names_the_replys_time():
    first = {"created_at": "2026-10-21T11:32:05+00:00"}
    assert reply_reports._reply_time(first) == "21 Oct, 11:32"
    assert reply_reports._reply_time({"created_at": "not a time"}) == ""


# --- the admins' notification, for real ------------------------------------

async def _no_delivery(user_sub, delivery):
    return None


def test_the_notification_reaches_the_admins_only(as_user, monkeypatch):
    monkeypatch.setattr(nm, "_deliver_to_user", _no_delivery)
    cid, _, reply = _chat()
    as_user(OWNER)
    assert _report(cid, reply, reason="private").status_code == 200
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_sub, title, body, scope, source FROM notification_deliveries").fetchall()
    assert [(r["user_sub"], r["scope"], r["source"]) for r in rows] == [("user-admin", "admin", "report")]
    assert rows[0]["title"] == "A reply was reported"
    assert "Once upon a time" not in rows[0]["body"]


# --- the admins' list -------------------------------------------------------

def test_the_list_is_for_admins_newest_first_and_paged(as_user, fired):
    task_store.create_chat("chat-list", "user-viewer", AGENT)
    replies = []
    for i in range(3):
        task_store.add_chat_message("chat-list", "user", f"q{i}")
        replies.append(task_store.add_chat_message("chat-list", "assistant", f"r{i}"))
    as_user(OWNER)
    for m in replies:
        _report("chat-list", m)
    assert client.get("/v1/admin/reply-reports").status_code == 403
    as_user(ADMIN)
    page = client.get("/v1/admin/reply-reports?limit=2").json()
    assert [r["message_id"] for r in page["reports"]] == [replies[2], replies[1]]
    assert page["has_more"] is True
    first = page["reports"][0]
    assert first["reporter_name"] == "Viewer User" and first["reply_text"] == "r2"
    assert first["chat_id"] == "chat-list" and first["reason"] == "harmful"
    assert first["chat_agent"] == AGENT and first["reply_at"]
    older = client.get(f"/v1/admin/reply-reports?limit=2&before_id={page['reports'][1]['id']}").json()
    assert [r["message_id"] for r in older["reports"]] == [replies[0]] and older["has_more"] is False


def test_an_admin_key_reads_no_list(as_user):
    as_user(UserContext(sub="user-admin", email="a@t", name="A", role="admin", is_api_key=True))
    assert client.get("/v1/admin/reply-reports").status_code == 403


def test_deleting_the_chat_takes_its_reports(as_user, fired):
    cid, _, reply = _chat()
    as_user(OWNER)
    _report(cid, reply)
    assert task_store.delete_chat(cid)
    assert not _rows()


# --- a released install's database -----------------------------------------

def test_the_table_comes_with_the_boot_on_a_database_without_it():
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS reply_reports")
        conn.commit()
    pg.close_pool()
    for _ in range(2):
        with get_conn() as conn:
            pg_schema.init_schema(conn)
            pg_schema.run_migrations(conn)
            conn.commit()
        pg.close_pool()
    with get_conn() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'reply_reports'").fetchall()}
    assert cols == {"id", "reporter_sub", "chat_id", "message_id", "agent", "reason",
                    "note", "reply_text", "created_at", "notified_at", "dismissed_at",
                    "dismissed_by"}


def test_a_table_made_before_dismiss_gains_its_columns_and_keeps_its_rows(as_user, fired):
    cid, _, reply = _chat()
    as_user(OWNER)
    _report(cid, reply)
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("ALTER TABLE reply_reports DROP COLUMN dismissed_at, DROP COLUMN dismissed_by")
        conn.commit()
    pg.close_pool()
    for _ in range(2):
        with get_conn() as conn:
            pg_schema.init_schema(conn)
            pg_schema.run_migrations(conn)
            conn.commit()
        pg.close_pool()
    [row] = _rows()
    assert row["dismissed_at"] is None and row["dismissed_by"] == ""
    as_user(ADMIN)
    page = client.get("/v1/admin/reply-reports").json()
    assert [r["message_id"] for r in page["reports"]] == [reply] and page["dismissed_count"] == 0


# --- dismiss and restore ------------------------------------------------------

def _three_reports(as_user):
    task_store.create_chat("chat-dis", "user-viewer", AGENT)
    replies = []
    for i in range(3):
        task_store.add_chat_message("chat-dis", "user", f"q{i}")
        replies.append(task_store.add_chat_message("chat-dis", "assistant", f"r{i}"))
    as_user(OWNER)
    for m in replies:
        _report("chat-dis", m)
    return [r["id"] for r in _rows()]


def test_a_dismissed_report_moves_to_the_history_and_back(as_user, fired):
    ids = _three_reports(as_user)
    as_user(ADMIN)
    r = client.post(f"/v1/admin/reply-reports/{ids[1]}/dismiss")
    assert r.status_code == 200 and r.json() == {"status": "dismissed"}
    page = client.get("/v1/admin/reply-reports").json()
    assert [x["id"] for x in page["reports"]] == [ids[2], ids[0]]
    assert page["dismissed_count"] == 1
    history = client.get("/v1/admin/reply-reports?state=dismissed").json()
    assert [x["id"] for x in history["reports"]] == [ids[1]]
    assert history["reports"][0]["dismissed_by_name"] == "Admin User"
    assert history["reports"][0]["dismissed_at"] and "dismissed_count" not in history
    r = client.post(f"/v1/admin/reply-reports/{ids[1]}/restore")
    assert r.status_code == 200 and r.json() == {"status": "open"}
    page = client.get("/v1/admin/reply-reports").json()
    assert [x["id"] for x in page["reports"]] == [ids[2], ids[1], ids[0]]
    assert page["dismissed_count"] == 0
    assert page["reports"][1]["dismissed_at"] == "" and page["reports"][1]["dismissed_by_name"] == ""
    # Nothing was deleted on the way.
    assert len(_rows()) == 3


def test_dismiss_and_restore_are_idempotent_and_keep_the_first_dismisser(as_user, fired):
    ids = _three_reports(as_user)
    as_user(ADMIN)
    client.post(f"/v1/admin/reply-reports/{ids[0]}/dismiss")
    first = [r for r in _rows() if r["id"] == ids[0]][0]["dismissed_at"]
    as_user(UserContext(sub="user-manager", email="m@t", name="M", role="admin"))
    assert client.post(f"/v1/admin/reply-reports/{ids[0]}/dismiss").status_code == 200
    row = [r for r in _rows() if r["id"] == ids[0]][0]
    assert (row["dismissed_at"], row["dismissed_by"]) == (first, "user-admin")
    assert client.post(f"/v1/admin/reply-reports/{ids[1]}/restore").status_code == 200
    assert client.post(f"/v1/admin/reply-reports/{ids[0]}/restore").status_code == 200
    assert client.post(f"/v1/admin/reply-reports/{ids[0]}/restore").status_code == 200
    assert all(r["dismissed_at"] is None for r in _rows())


def test_dismiss_is_for_admins_and_names_a_real_report(as_user, fired):
    ids = _three_reports(as_user)
    as_user(OWNER)
    assert client.post(f"/v1/admin/reply-reports/{ids[0]}/dismiss").status_code == 403
    as_user(UserContext(sub="user-admin", email="a@t", name="A", role="admin", is_api_key=True))
    assert client.post(f"/v1/admin/reply-reports/{ids[0]}/dismiss").status_code == 403
    as_user(ADMIN)
    assert client.post("/v1/admin/reply-reports/999999/dismiss").status_code == 404
    assert client.post("/v1/admin/reply-reports/999999/restore").status_code == 404
    assert client.post("/v1/admin/reply-reports/2147483648/dismiss").status_code == 422
    assert client.get("/v1/admin/reply-reports?state=closed").status_code == 422
    assert all(r["dismissed_at"] is None for r in _rows())


def test_a_dismissed_report_still_goes_with_its_chat(as_user, fired):
    ids = _three_reports(as_user)
    as_user(ADMIN)
    client.post(f"/v1/admin/reply-reports/{ids[0]}/dismiss")
    assert task_store.delete_chat("chat-dis")
    assert not _rows()
    assert client.get("/v1/admin/reply-reports").json()["dismissed_count"] == 0
