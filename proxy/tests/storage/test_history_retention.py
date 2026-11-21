"""The history window (services/infra/history_retention.py): what it deletes
and, above all, what it must leave."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from services.infra import history_retention, retention
from storage import database as task_store
from storage.checks import db_checks
from storage.pg import get_conn

AGENT = "alpha"
OTHER = "beta"
OWNER = "user-admin"


def _uuid() -> str:
    return str(uuid.uuid4())


def _iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _window(agent: str = AGENT, days: int = 30) -> None:
    from storage.agents import agent_store
    if not agent_store.agent_exists(agent):
        agent_store.create_agent(agent, agent)
    assert agent_store.update_agent(agent, history_retention_days=days) is not None


def _chat(*, agent: str = AGENT, days: int = 90, sid: str = "", tid: str = "",
          owner: str = OWNER, **kw) -> str:
    cid = _uuid()
    task_store.create_chat(cid, owner, agent, **kw)
    if sid or tid:
        task_store.update_chat(cid, session_id=sid or None, codex_thread_id=tid or None)
    task_store.add_chat_message(cid, "user", "the words")
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (_iso(days), cid))
        conn.commit()
    return cid


def _session_file(sid: str, agent: str = AGENT) -> Path:
    folder = Path(config.AGENTS_DIR) / agent / "workspace" / ".claude" / "projects" / "-workspace"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("x" * 100)
    return path


def _rollout_file(tid: str, agent: str = AGENT) -> Path:
    folder = Path(config.AGENTS_DIR) / agent / "workspace" / ".codex" / "sessions" / "2026" / "01" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-01-01T00-00-00-{tid}.jsonl"
    path.write_text("y" * 100)
    return path


def _run_pass(*, dry_run: bool = False) -> tuple[dict, set[str]]:
    stats: dict = {"errors": 0}

    async def go():
        return await history_retention.run_pass(stats, dry_run)

    gone = asyncio.run(go())
    return stats, gone


def _exists(cid: str) -> bool:
    return task_store.get_chat(cid) is not None


# ── what goes ───────────────────────────────────────────────────────────────


def test_an_old_chat_goes_with_its_session_file(temp_db):
    _window()
    sid = _uuid()
    path = _session_file(sid)
    cid = _chat(sid=sid)

    stats, _ = _run_pass()

    assert not _exists(cid) and not path.exists()
    assert stats["history_chats_deleted"] == 1 and stats["history_files_deleted"] == 1
    assert stats["history_bytes_freed"] == 100


def test_a_deletion_is_logged_without_the_words_of_the_chat(temp_db, caplog):
    import logging
    _window()
    cid = _chat()
    with caplog.at_level(logging.INFO, logger="otodock"):
        _run_pass()

    assert not _exists(cid)
    assert any(cid[:8] in r.message and "deleted chat" in r.message for r in caplog.records)
    assert "the words" not in caplog.text


def test_runs_verdicts_and_judge_chats_go_under_the_agent_window(temp_db):
    _window()
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "the prompt")
    task_store.update_run(run_id, status="completed", started_at=_iso(90), completed_at=_iso(90))
    judge_run = _uuid()
    task_store.create_run(judge_run, "t2", AGENT, "check", None, "judge this")
    task_store.update_run(judge_run, status="completed", started_at=_iso(90), completed_at=_iso(90))
    judge_chat = f"task-{judge_run}"
    task_store.create_chat(judge_chat, OWNER, AGENT, source_type="task")
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (_iso(90), judge_chat))
        conn.commit()
    verdict = _verdict(days=90)
    keep_verdict = _verdict(days=2)
    other_verdict = _verdict(days=90, agent=OTHER)

    stats, _ = _run_pass()

    assert task_store.get_run(run_id) is None and task_store.get_run(judge_run) is None
    assert not _exists(judge_chat)
    assert stats["history_runs_deleted"] == 2
    assert stats["history_verdicts_deleted"] == 1
    assert _verdict_ids() == {keep_verdict, other_verdict}
    assert verdict not in _verdict_ids()


def _verdict(*, days: int, agent: str = AGENT) -> str:
    row = db_checks.insert_verdict(
        agent=agent, owner="", check_name="c", section="schema", status="pass", passed=True,
        score=None, findings=[{"text": "finding"}], summary="", reason="", session_id="",
        chat_id="", run_id="", judge_run_id="", user_sub="", round_no=1, ran_on="local",
        engine="", model="", cost_usd=0.0, duration_ms=0, script_sha256="")
    with get_conn() as conn:
        conn.execute("UPDATE check_verdicts SET created_at=%s WHERE id=%s", (_iso(days), row["id"]))
        conn.commit()
    return row["id"]


def _verdict_ids() -> set[str]:
    with get_conn() as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM check_verdicts").fetchall()}


# ── what never goes ─────────────────────────────────────────────────────────


def test_a_chat_newer_than_the_window_stays(temp_db):
    _window()
    cid = _chat(days=5)

    stats, _ = _run_pass()

    assert _exists(cid) and stats["history_chats_deleted"] == 0


def test_no_window_set_deletes_nothing(temp_db):
    cid = _chat()

    stats, _ = _run_pass()

    assert _exists(cid) and stats["history_chats_deleted"] == 0


def test_another_agents_chat_stays_and_its_files_too(temp_db):
    _window()
    sid = _uuid()
    path = _session_file(sid, agent=OTHER)
    cid = _chat(agent=OTHER, sid=sid)

    _run_pass()

    assert _exists(cid) and path.exists()


def test_a_phone_chat_is_not_this_pass(temp_db):
    _window()
    cid = _chat(source_type="phone")

    _run_pass()

    assert _exists(cid)


def test_a_meetings_parent_chat_stays(temp_db):
    from storage.chat import db_meetings
    _window()
    cid = _chat()
    db_meetings.create_meeting(_uuid(), "t", "[]", "mod", "free", 5, cid, None, None, "user", OWNER)

    _run_pass()

    assert _exists(cid)


def test_a_shared_chat_stays(temp_db):
    _window()
    cid = _chat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO shares (id, target_kind, chat_id, scope, grantee_kind, grantee_sub, created_by, created_at)"
            " VALUES (%s,'chat',%s,'internal','person','user-viewer',%s,%s)",
            (_uuid(), cid, OWNER, _iso(1)))
        conn.commit()

    stats, _ = _run_pass()

    assert _exists(cid) and stats["history_chats_busy"] == 1


def test_a_revoked_share_does_not_keep_a_chat(temp_db):
    _window()
    cid = _chat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO shares (id, target_kind, chat_id, scope, grantee_kind, grantee_sub, created_by,"
            " created_at, revoked_at) VALUES (%s,'chat',%s,'internal','person','user-viewer',%s,%s,%s)",
            (_uuid(), cid, OWNER, _iso(10), _iso(9)))
        conn.commit()

    _run_pass()

    assert not _exists(cid)


def test_a_live_pump_keeps_a_chat(temp_db, monkeypatch):
    from core.events import stream_pump
    _window()
    cid = _chat()
    monkeypatch.setitem(stream_pump._active_pumps, cid, object())

    stats, _ = _run_pass()

    assert _exists(cid) and stats["history_chats_busy"] == 1


def test_a_live_session_on_a_machine_keeps_a_chat(temp_db, monkeypatch):
    _window()
    sid = _uuid()
    cid = _chat(sid=sid)
    monkeypatch.setattr(retention, "_remote_live_ids", lambda: ({sid}, {}))

    stats, _ = _run_pass()

    assert _exists(cid) and stats["history_chats_busy"] == 1


def test_a_warming_chat_stays(temp_db, monkeypatch):
    from core.session import warmup_registry
    _window()
    cid = _chat()
    monkeypatch.setattr(warmup_registry, "get", lambda chat_id: object() if chat_id == cid else None)

    _run_pass()

    assert _exists(cid)


def test_a_chat_a_terminal_holds_stays(temp_db, monkeypatch):
    from core.session import interactive_registry
    _window()
    cid = _chat()
    monkeypatch.setattr(interactive_registry, "find_live_for_chat",
                        lambda chat_id, **_: object() if chat_id == cid else None)

    _run_pass()

    assert _exists(cid)


def test_a_moving_chat_stays(temp_db, monkeypatch):
    from services.remote import chat_move
    _window()
    cid = _chat()
    monkeypatch.setattr(chat_move, "moving", lambda chat_id: chat_id == cid)

    _run_pass()

    assert _exists(cid)


def _live_run(chat_id: str) -> None:
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, chat_id=chat_id)


def test_a_chat_with_a_live_run_stays(temp_db):
    _window()
    cid = _chat()
    _live_run(cid)

    _run_pass()

    assert _exists(cid)


def _insert_task(task_id: str, **cols) -> None:
    names = ["id", "agent", "name", "prompt", "llm_mode", "task_type", *cols]
    values = [task_id, AGENT, "n", "p", "cli", cols.pop("task_type", "one_time"), *cols.values()]
    names.remove("task_type") if names.count("task_type") > 1 else None
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO dynamic_tasks ({','.join(names)}, created_at) VALUES "
            f"({','.join(['%s'] * len(values))}, %s)", (*values, _iso(1)))
        conn.commit()


def test_a_pending_continuation_keeps_a_chat(temp_db):
    _window()
    cid = _chat()
    _insert_task(_uuid(), task_type="continuation", target_chat_id=cid)

    _run_pass()

    assert _exists(cid)


def test_a_task_that_reports_here_keeps_a_chat(temp_db):
    _window()
    cid = _chat()
    _insert_task(_uuid(), task_type="delegate", on_complete_chat_id=cid)

    _run_pass()

    assert _exists(cid)


def test_a_task_that_continues_the_session_keeps_a_chat(temp_db):
    _window()
    sid = _uuid()
    cid = _chat(sid=sid)
    _insert_task(_uuid(), task_type="scheduled", continue_session=sid, schedule="0 9 * * 1")

    _run_pass()

    assert _exists(cid)


def test_a_queued_message_keeps_a_chat(temp_db):
    _window()
    cid = _chat()
    task_store.enqueue_chat_input(cid, "q-1", OWNER, text="waiting", cli_text="", event_data="",
                                  images="", origin_conn="")

    _run_pass()

    assert _exists(cid)


def test_an_open_turn_keeps_a_chat(temp_db):
    from storage.chat import open_turns
    _window()
    cid = _chat()
    open_turns.open_turn(cid, "tok", "")

    _run_pass()

    assert _exists(cid)


def test_a_turn_still_to_continue_keeps_a_chat(temp_db):
    from storage.chat import cut_records
    _window()
    cid = _chat()
    cut_records.add(cid, cause=cut_records.CAUSE_CRASH, state=cut_records.PENDING)

    _run_pass()

    assert _exists(cid)


def test_a_pending_rebuild_keeps_a_chat(temp_db):
    from storage.chat import pending_rebuilds
    _window()
    cid = _chat()
    pending_rebuilds.save(cid, "reload", person="", wait_s=60.0)

    _run_pass()

    assert _exists(cid)


def test_an_undelivered_worker_result_keeps_a_chat(temp_db):
    _window()
    cid = _chat()
    with get_conn() as conn:
        conn.execute("UPDATE chats SET pending_delegate_wake=%s WHERE id=%s", ('[{"prompt":"x"}]', cid))
        conn.commit()

    _run_pass()

    assert _exists(cid)


def test_a_child_newer_than_the_window_keeps_its_parent_and_itself(temp_db):
    _window()
    parent = _chat()
    child = _chat(days=2, parent_chat_id=parent)

    _run_pass()

    assert _exists(parent) and _exists(child)


def test_a_parent_newer_than_the_window_keeps_its_child(temp_db):
    _window()
    parent = _chat(days=2)
    child = _chat(parent_chat_id=parent)

    _run_pass()

    assert _exists(parent) and _exists(child)


def test_a_live_child_keeps_the_group(temp_db):
    _window()
    parent = _chat()
    child = _chat(parent_chat_id=parent)
    _live_run(child)

    stats, _ = _run_pass()

    assert _exists(parent) and _exists(child)
    assert stats["history_chats_busy"] == 2


def test_a_project_member_newer_than_the_window_keeps_the_project(temp_db):
    _window()
    old = _chat(project_id="lane-x")
    new = _chat(days=2, project_id="lane-x")

    _run_pass()

    assert _exists(old) and _exists(new)


def test_a_group_past_the_window_goes_together(temp_db):
    _window()
    parent = _chat()
    child = _chat(parent_chat_id=parent)

    _run_pass()

    assert not _exists(parent) and not _exists(child)


def test_a_session_id_a_fresher_chat_shares_keeps_its_file(temp_db):
    _window()
    sid = _uuid()
    path = _session_file(sid)
    gone = _chat(sid=sid)
    kept = _chat(days=2, sid=sid)

    _run_pass()

    assert not _exists(gone) and _exists(kept) and path.exists()


@pytest.mark.parametrize("key", ["sid", "tid"])
def test_a_session_an_older_kept_chat_names_keeps_its_file(temp_db, key):
    """Past the window too, but held by a share: the file is still its chat's."""
    _window()
    ref = _uuid()
    path = _session_file(ref) if key == "sid" else _rollout_file(ref)
    gone = _chat(**{key: ref})
    kept = _chat(**{key: ref})
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO shares (id, target_kind, chat_id, scope, grantee_kind, grantee_sub, created_by, created_at)"
            " VALUES (%s,'chat',%s,'internal','person','user-viewer',%s,%s)",
            (_uuid(), kept, OWNER, _iso(1)))
        conn.commit()

    _run_pass()

    assert not _exists(gone) and _exists(kept) and path.exists()


def test_a_dry_run_counts_a_session_two_old_chats_share(temp_db):
    """Each round of a delegation chain is a chat on one session id."""
    _window()
    sid = _uuid()
    path = _session_file(sid)
    first, second = _chat(sid=sid), _chat(days=80, sid=sid)
    keys = ("history_chats_deleted", "history_files_deleted", "history_bytes_freed")

    dry, _ = _run_pass(dry_run=True)

    assert _exists(first) and _exists(second) and path.exists()

    real, _ = _run_pass()

    assert [dry[k] for k in keys] == [real[k] for k in keys] == [2, 1, 100]
    assert not path.exists()


def test_a_dry_run_counts_a_runs_session_once(temp_db):
    _window()
    sid = _uuid()
    path = _session_file(sid)
    cid = _chat(sid=sid)
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, status="completed", started_at=_iso(90), completed_at=_iso(90),
                          chat_id=cid, session_id=sid)

    dry, _ = _run_pass(dry_run=True)
    real, _ = _run_pass()

    assert dry["history_files_deleted"] == real["history_files_deleted"] == 1
    assert dry["history_runs_deleted"] == real["history_runs_deleted"] == 1
    assert not path.exists()


def test_a_runs_session_a_fresher_chat_names_keeps_its_file(temp_db):
    _window()
    sid = _uuid()
    path = _session_file(sid)
    _chat(days=2, sid=sid)
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, status="completed", started_at=_iso(90), completed_at=_iso(90),
                          session_id=sid)

    _run_pass()

    assert task_store.get_run(run_id) is None and path.exists()


def test_a_task_that_continues_a_group_members_session_keeps_the_group(temp_db):
    _window()
    sid = _uuid()
    parent = _chat()
    child = _chat(sid=sid, parent_chat_id=parent)
    _insert_task(_uuid(), task_type="scheduled", continue_session=sid, schedule="0 9 * * 1")

    _run_pass()

    assert _exists(parent) and _exists(child)


def test_a_live_pump_on_a_group_member_keeps_the_group(temp_db, monkeypatch):
    from core.events import stream_pump
    _window()
    parent = _chat()
    child = _chat(parent_chat_id=parent)
    monkeypatch.setitem(stream_pump._active_pumps, child, object())

    _run_pass()

    assert _exists(parent) and _exists(child)


def test_a_terminal_on_a_group_member_keeps_the_group(temp_db, monkeypatch):
    from core.session import interactive_registry
    _window()
    parent = _chat()
    child = _chat(parent_chat_id=parent)
    monkeypatch.setattr(interactive_registry, "find_live_for_chat",
                        lambda chat_id, **_: object() if chat_id == child else None)

    _run_pass()

    assert _exists(parent) and _exists(child)


def test_a_group_too_large_to_read_waits(temp_db, monkeypatch):
    from storage.chat import history_store
    monkeypatch.setattr(history_store, "_GROUP_MAX", 1)
    _window()
    parent = _chat()
    child = _chat(parent_chat_id=parent)

    stats, _ = _run_pass()

    assert _exists(parent) and _exists(child) and stats["history_chats_busy"] == 2


def test_a_running_worker_whose_task_fired_keeps_its_chat(temp_db):
    _window()
    cid = _chat()
    task_id = _uuid()
    _insert_task(task_id, task_type="delegate", target_chat_id=cid, fired=True)
    run_id = _uuid()
    task_store.create_run(run_id, task_id, AGENT, "delegate", None, "p")
    task_store.update_run(run_id, status="running", started_at=_iso(1))

    stats, _ = _run_pass()

    assert _exists(cid) and stats["history_chats_busy"] == 1


def test_a_chat_past_only_the_agent_window_keeps_its_reports(temp_db, monkeypatch):
    from storage import db_reply_reports
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {history_retention.ROWS_KEY: "365"})
    _window(days=30)
    cid = _chat(days=90)
    with get_conn() as conn:
        message_id = conn.execute(
            "SELECT id FROM chat_messages WHERE chat_id=%s LIMIT 1", (cid,)).fetchone()["id"]
    assert db_reply_reports.add(OWNER, cid, message_id, agent=AGENT, reason="wrong",
                                note="", reply_text="r") is not None

    _run_pass()

    assert not _exists(cid)
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM reply_reports").fetchone()["n"] == 1


def test_a_chat_touched_during_the_pass_is_left_whole(temp_db, monkeypatch):
    from storage.chat import history_store
    _window()
    cid = _chat()
    real = history_store.blocker

    def touch_then_answer(ids):
        with get_conn() as conn:
            conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s",
                         (datetime.now(timezone.utc).isoformat(), cid))
            conn.commit()
        return real(ids)

    monkeypatch.setattr(history_store, "blocker", touch_then_answer)

    stats, _ = _run_pass()

    assert _exists(cid) and stats["history_chats_deleted"] == 0


def test_a_run_whose_chat_survives_stays(temp_db):
    """Who may open a chat is read from its run."""
    _window()
    cid = _chat(days=2)
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, status="completed", started_at=_iso(90),
                          completed_at=_iso(90), chat_id=cid)

    _run_pass()

    assert task_store.get_run(run_id) is not None and _exists(cid)


def test_a_live_run_stays(temp_db):
    _window()
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, status="running", started_at=_iso(90))

    _run_pass()

    assert task_store.get_run(run_id) is not None


def test_a_run_newer_than_the_window_stays(temp_db):
    _window()
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, status="completed", started_at=_iso(5), completed_at=_iso(5))

    _run_pass()

    assert task_store.get_run(run_id) is not None


# ── the windows, the dry run, reply reports ─────────────────────────────────


def test_a_dry_run_deletes_nothing_and_counts_the_same(temp_db):
    _window()
    sid = _uuid()
    path = _session_file(sid)
    chat_with_run = _chat(sid=sid)
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, status="completed", started_at=_iso(90),
                          completed_at=_iso(90), chat_id=chat_with_run)
    _verdict(days=90)
    keys = history_retention.STAT_KEYS

    first, dry_refs = _run_pass(dry_run=True)

    assert _exists(chat_with_run) and path.exists() and task_store.get_run(run_id) is not None
    assert len(_verdict_ids()) == 1
    assert dry_refs == {sid}

    second, _ = _run_pass()

    assert {k: first[k] for k in keys} == {k: second[k] for k in keys}
    assert second["history_chats_deleted"] == 1 and second["history_runs_deleted"] == 1
    assert second["history_verdicts_deleted"] == 1


def test_the_chat_rows_window_applies_to_every_agent_and_takes_reply_reports(temp_db, monkeypatch):
    from storage import db_reply_reports
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {history_retention.ROWS_KEY: "60"})
    gone = _chat(agent=OTHER, days=90)
    young = _chat(agent=OTHER, days=30)
    with get_conn() as conn:
        message_id = conn.execute(
            "SELECT id FROM chat_messages WHERE chat_id=%s LIMIT 1", (gone,)).fetchone()["id"]
    assert db_reply_reports.add(OWNER, gone, message_id, agent=OTHER, reason="wrong",
                                note="", reply_text="r") is not None

    stats, _ = _run_pass()

    assert not _exists(gone) and _exists(young)
    assert stats["history_chats_deleted"] == 1
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM reply_reports").fetchone()["n"] == 0


def test_the_chat_rows_window_leaves_runs_and_verdicts(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {history_retention.ROWS_KEY: "60"})
    _chat()
    run_id = _uuid()
    task_store.create_run(run_id, "t1", AGENT, "scheduled", None, "p")
    task_store.update_run(run_id, status="completed", started_at=_iso(90), completed_at=_iso(90))
    verdict = _verdict(days=90)

    stats, _ = _run_pass()

    assert stats["history_runs_deleted"] == 0 and stats["history_verdicts_deleted"] == 0
    assert task_store.get_run(run_id) is not None and verdict in _verdict_ids()


def test_the_agent_window_leaves_reply_reports(temp_db):
    from storage import db_reply_reports
    _window()
    cid = _chat()
    with get_conn() as conn:
        message_id = conn.execute(
            "SELECT id FROM chat_messages WHERE chat_id=%s LIMIT 1", (cid,)).fetchone()["id"]
    assert db_reply_reports.add(OWNER, cid, message_id, agent=AGENT, reason="wrong",
                                note="", reply_text="r") is not None

    _run_pass()

    assert not _exists(cid)
    with get_conn() as conn:
        row = conn.execute("SELECT chat_id FROM reply_reports").fetchone()
    assert row is not None and row["chat_id"] is None


def test_the_shorter_window_wins(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {history_retention.ROWS_KEY: "365"})
    _window(days=30)
    gone = _chat(days=90)

    _run_pass()

    assert not _exists(gone)


@pytest.mark.parametrize("bad", ["0", "-1", "3", "soon", "99999"])
def test_a_bad_forced_window_deletes_nothing(temp_db, monkeypatch, bad):
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {history_retention.ROWS_KEY: bad})
    cid = _chat(days=9000)

    _run_pass()

    assert _exists(cid)


def test_the_bound_per_sweep_leaves_the_rest_for_tomorrow(temp_db, monkeypatch):
    monkeypatch.setattr(history_retention, "MAX_PER_SWEEP", 1)
    _window()
    first, second = _chat(days=100), _chat(days=90)

    _run_pass()

    assert not _exists(first) and _exists(second)


# ── the machines' pass reads a dry run's ids ────────────────────────────────


def test_a_dry_runs_ids_leave_the_machines_keep_lists(temp_db, monkeypatch):
    from core.remote import satellite_connection
    from services.infra.retention import LiveSnapshot
    keeps: list[set] = []

    class _Cm:
        def get_connected_machines(self):
            return ["m-1"]

        def is_compute(self, _mid):
            return False


        def shares_folders(self, _mid):

            return False

        def satellite_name(self, _mid):
            return "m"

        async def send_command(self, _mid, msg, timeout):
            keeps.append(set(msg["keep"]))
            return {"status": "ok"}

    monkeypatch.setattr(satellite_connection, "get_connection_manager", _Cm)
    monkeypatch.setattr(retention, "_remote_live_ids", lambda: (set(), {}))
    sid = _uuid()
    task_store.create_chat("c-on-m", OWNER, AGENT)
    task_store.update_chat("c-on-m", session_id=sid, execution_target="m-1")
    stats = retention._run_sweep_sync(180, False, LiveSnapshot(), dry_run=True)

    async def go():
        await retention._pass_remote_sessions(180, False, LiveSnapshot(), stats, True, {sid})

    asyncio.run(go())

    assert keeps and sid not in keeps[0]


# ── the route ───────────────────────────────────────────────────────────────


def _update(user, days):
    from api.agents import agents as api
    return asyncio.run(api.update_agent(AGENT, api.UpdateAgentRequest(history_retention_days=days), user=user))


def _ctx(role: str, *, session: bool = False):
    from auth.providers import UserContext
    return UserContext(
        sub="user-admin" if role == "admin" else "user-manager", email="a@b.c", name="N",
        role=role, agent_roles={AGENT: "manager"},
        agent=AGENT if session else "", session_id="sid-1" if session else "",
        is_api_key=session)


def test_an_admin_sets_the_window_and_it_is_stored(temp_db):
    _window(days=0)
    result = _update(_ctx("admin"), 90)
    assert result["history_retention_days"] == 90


def test_a_manager_may_not_set_the_window(temp_db):
    from fastapi import HTTPException
    _window(days=0)
    with pytest.raises(HTTPException) as e:
        _update(_ctx("member"), 90)
    assert e.value.status_code == 403


def test_a_session_token_may_not_set_the_window(temp_db):
    from fastapi import HTTPException
    _window(days=0)
    with pytest.raises(HTTPException) as e:
        _update(_ctx("admin", session=True), 90)
    assert e.value.status_code == 403


@pytest.mark.parametrize("bad", [3, 6, 3651, -1])
def test_a_bad_window_is_refused(temp_db, bad):
    from fastapi import HTTPException
    _window(days=0)
    with pytest.raises(HTTPException) as e:
        _update(_ctx("admin"), bad)
    assert e.value.status_code == 400


def test_zero_turns_the_window_off(temp_db):
    _window(days=30)
    assert _update(_ctx("admin"), 0)["history_retention_days"] == 0


def test_the_column_arrives_on_an_existing_database(temp_db):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name='agents' AND column_name='history_retention_days'").fetchone()
    assert row is not None and row["is_nullable"] == "NO" and row["column_default"] == "0"
