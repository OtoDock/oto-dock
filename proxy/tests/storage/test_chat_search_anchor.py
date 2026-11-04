"""A search result carries its way to the match: the newest message whose own
text holds every word of the query (``match_message_id``) and the words
around it, the matched ones between U+0002 and U+0003 (``match_snippet``).
A chat found by its title alone, or whose words sit in different messages,
has none; the pass is bounded and never costs the search its rows."""

import uuid

import psycopg
import pytest

from storage import database as task_store
from storage.chat import search_anchor
from storage.pg import get_conn

AGENT = "agent-anchor"
OWNER = "user-anchor"
START, END = search_anchor.MARK_START, search_anchor.MARK_END


def _chat(*rows: tuple[str, str], title: str = "") -> tuple[str, list[int]]:
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, OWNER, AGENT, title=title)
    ids = [task_store.add_chat_message(cid, role, text) for role, text in rows]
    return cid, ids


def _search(q: str) -> dict[str, dict]:
    return {r["id"]: r for r in task_store.search_chats(OWNER, AGENT, q)}


def test_the_newest_message_holding_the_words_is_the_anchor_with_its_words_marked():
    cid, ids = _chat(
        ("user", "Where did the zeppelin manifest go last week"),
        ("assistant", "The zeppelin manifest is in the archive folder, under the old name."),
        *[("assistant", f"Filler reply number {i} about nothing in particular.") for i in range(30)],
    )
    row = _search("zeppelin manifest")[cid]
    assert row["match_message_id"] == ids[1]
    assert f"{START}zeppelin{END} {START}manifest{END}" in row["match_snippet"]
    assert "\n" not in row["match_snippet"]
    assert len(row["match_snippet"]) <= 162


def test_a_stem_matches_and_is_marked_as_the_server_matched_it():
    cid, ids = _chat(("user", "We kept deploying the gateway on Fridays"))
    row = _search("deployed")[cid]
    assert row["match_message_id"] == ids[0]
    assert f"{START}deploying{END}" in row["match_snippet"]


def test_a_chat_found_by_its_title_alone_has_no_anchor_and_no_scan(monkeypatch):
    cid, _ = _chat(("user", "nothing relevant here"), title="Quarterly walrus review")
    scanned: list[str] = []
    real = search_anchor._anchor
    monkeypatch.setattr(search_anchor, "_anchor", lambda conn, c, q: scanned.append(c) or real(conn, c, q))
    row = _search("walrus")[cid]
    assert row["match_message_id"] is None and row["match_snippet"] == ""
    assert scanned == []


def test_words_in_different_messages_give_no_anchor():
    cid, _ = _chat(("user", "the falcon landed"), ("assistant", "and the harbour was quiet"))
    row = _search("falcon harbour")[cid]
    assert row["match_message_id"] is None


def test_a_match_only_in_a_time_prelude_is_passed_over_for_the_next_one():
    stamp = "[Current time: Monday, October 26, 2026 20:30 (8:30 PM) UTC (UTC+00:00)]"
    cid, ids = _chat(
        ("user", "Please check the Monday backups"),
        ("user", f"{stamp}\n\nhow is the cluster"),
    )
    row = _search("monday")[cid]
    assert row["match_message_id"] == ids[0]


def test_the_scan_looks_only_at_the_chat_s_newest_rows(monkeypatch):
    monkeypatch.setattr(search_anchor, "_WINDOW_ROWS", 5)
    cid, _ = _chat(("user", "the quokka sentence"),
                   *[("assistant", f"reply {i}") for i in range(6)])
    assert _search("quokka")[cid]["match_message_id"] is None
    monkeypatch.setattr(search_anchor, "_WINDOW_ROWS", 7)
    assert _search("quokka")[cid]["match_message_id"] is not None


def test_a_task_run_s_search_carries_the_anchor_too():
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    cid = f"task-{run_id}"
    task_store.create_chat(cid, f"task::{AGENT}", AGENT)
    task_store.create_run(run_id, "t-a", AGENT, "schedule", None, "p", scope="agent", created_by="")
    task_store.update_run(run_id, status="completed", chat_id=cid,
                          started_at="2026-07-12T00:00:00+00:00")
    task_store.add_chat_message(cid, "user", "check the narwhal exports")
    mid = task_store.add_chat_message(cid, "assistant", "The narwhal exports finished at noon.")
    rows = task_store.search_task_chats(AGENT, "narwhal finished", None)
    assert [(r["id"], r["match_message_id"]) for r in rows] == [(cid, mid)]


def test_only_the_first_rows_are_looked_at(monkeypatch):
    monkeypatch.setattr(search_anchor, "_ANCHOR_ROWS", 2)
    for _ in range(3):
        _chat(("user", "an ocelot sighting"))
    rows = task_store.search_chats(OWNER, AGENT, "ocelot")
    assert [r["match_message_id"] is not None for r in rows] == [True, True, False]


def test_a_spent_budget_leaves_the_rest_without_anchors(monkeypatch):
    for _ in range(3):
        _chat(("user", "a tapir crossing"))
    monkeypatch.setattr(search_anchor, "_BUDGET_S", 0.0)
    rows = task_store.search_chats(OWNER, AGENT, "tapir")
    assert len(rows) == 3
    assert all(r["match_message_id"] is None and r["match_snippet"] == "" for r in rows)


def test_a_cancelled_lookup_costs_only_its_row(monkeypatch):
    first, _ = _chat(("user", "an ibex on the ridge"))
    second, _ = _chat(("user", "an ibex in the valley"))
    real = search_anchor._anchor

    def slow_first(conn, cid, q):
        if cid == second:   # the newest row is looked at first
            conn.execute("SELECT pg_sleep(1)")
        return real(conn, cid, q)

    monkeypatch.setattr(search_anchor, "_anchor", slow_first)
    rows = {r["id"]: r for r in task_store.search_chats(OWNER, AGENT, "ibex")}
    assert set(rows) == {first, second}
    assert rows[second]["match_message_id"] is None
    assert rows[first]["match_message_id"] is not None


def test_an_error_in_the_pass_keeps_the_rows_and_their_order(monkeypatch, caplog):
    older, _ = _chat(("user", "a gecko note"))
    newer, _ = _chat(("user", "another gecko note"))

    def broken(conn, cid, q):
        raise psycopg.errors.InternalError("boom")

    monkeypatch.setattr(search_anchor, "_anchor", broken)
    rows = task_store.search_chats(OWNER, AGENT, "gecko")
    assert [r["id"] for r in rows] == [newer, older]
    assert all(r["match_message_id"] is None for r in rows)
    assert not [r for r in caplog.records if r.levelname == "WARNING" and "chat search" in r.getMessage()]


@pytest.mark.parametrize("headline,expected", [
    ("**Bold** `code` and # heading \x02word\x03", "Bold code and heading \x02word\x03"),
    ("a\n\n  b\tc", "a b c"),
])
def test_the_snippet_is_one_plain_line(headline, expected):
    assert search_anchor._snippet(headline) == expected


def test_a_cut_snippet_closes_its_mark():
    text = search_anchor._snippet("x" * 150 + " \x02" + "y" * 40 + "\x03")
    assert len(text) <= 161 and text.endswith(END) and text.count(START) == 1


def test_the_lookups_share_one_time_limit_on_the_search_connection():
    cid, _ = _chat(("user", "a pangolin in the yard"))
    with get_conn() as conn:
        rows = [{"id": cid}]
        search_anchor.stamp_match_anchors(conn, rows, "'pangolin':*", "'pangolin':*B")
        timeout = conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
    assert rows[0]["match_message_id"] is not None
    assert timeout == "150ms"
    with get_conn() as conn:   # gone with the search's transaction
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] != "150ms"
