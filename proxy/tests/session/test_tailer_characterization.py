"""Characterization (golden master) of the two interactive transcript tailers.

Recorded transcripts (``tests/fixtures/characterization/``: a Claude session
JSONL and a Codex rollout from T1, and one composed edge file per engine, see
the README there) are replayed through ``transcript_tailer._process_lines``
and ``codex_rollout_tailer._process_rollout_lines`` by their public entry
points, in every shape a batch arrives in: the whole file at once (the
satellite's forwarded lines), one line per batch, a local file read through
the line cursor at chosen cuts, the resume-replay guard, and the Codex first
tail's prefix merge. Every result dict, every persisted row and every side
call (the title fan-out, the usage rows, the limit throttle, the window
sample) is compared with the golden under ``golden/`` byte for byte. The
goldens were written once from the tree BEFORE the tailers were decomposed
(``CHARACTERIZATION_WRITE=1``) and are never rewritten by a refactor: a
drift is a behaviour change to explain, not a golden to regenerate.

DB-free: the store functions the tailers call are recorders at their own
module (``storage.database``), as the synthetic tailer tests do.
"""
import copy
import json

import pytest

from core.session import (
    codex_rollout_tailer as C,
    session_events,
    session_state,
    transcript_tailer as T,
    transcript_tool_events as _TE,
)
from tests.fixtures.characterization.golden_master import (
    check_golden,
    load_lines as _lines,
)

CHAT = "chat-char-1"


class _Store:
    """The store the tailers write: every call recorded in order, the chat
    row a mutable dict so a title the backfill writes is seen by the next
    batch as a real row would show it."""

    def __init__(self, *, title: str = "", model: str = "claude-sonnet-5-5",
                 prefix_rows: list[dict] | None = None):
        self.row = {"id": CHAT, "user_sub": "u-person", "agent": "lite", "title": title,
                    "model": model, "source_type": "chat", "codex_thread_id": ""}
        self.rows: list = []
        self.updates: list[dict] = []
        self.rebuilds: list[str] = []
        self.titles: list = []
        self.usage: list[dict] = []
        self.throttles: list[str] = []
        self.snapshots: list = []
        self.prefix_rows = prefix_rows or []

    def add_chat_message(self, chat_id, role, content="", event_type="", event_data="",
                         author_sub="", **kw):
        if role == "event":
            self.rows.append({"role": "event", "event_type": event_type,
                              "event_data": json.loads(event_data)})
        else:
            self.rows.append({"role": role, "content": content, "author_sub": author_sub,
                              "sync_search": kw.get("sync_search", True)})

    def get_chat(self, chat_id):
        return self.row if chat_id == CHAT else None

    def update_chat(self, chat_id, **kw):
        self.updates.append(kw)
        self.row.update(kw)

    def get_chat_messages(self, chat_id, limit=500, **kw):
        return list(self.prefix_rows)

    def persisted_as_prefix(self) -> list[dict]:
        """The rows as ``get_chat_messages`` would hand them back (newest
        first) for a later first tail's prefix merge."""
        out = []
        for r in self.rows:
            if r["role"] == "event":
                out.append({"role": "event", "content": "", "event_type": r["event_type"],
                            "event_data": json.dumps(r["event_data"])})
            else:
                out.append({"role": r["role"], "content": r["content"]})
        return list(reversed(out))

    def snapshot(self) -> dict:
        return copy.deepcopy({
            "rows": self.rows, "updates": self.updates, "rebuilds": self.rebuilds,
            "titles": self.titles, "usage": self.usage, "throttles": self.throttles,
            "snapshots": self.snapshots})

    def clear(self) -> None:
        """A new life of the chat: the lists emptied in place (the seams hold
        bound methods of these very lists)."""
        for lst in (self.rows, self.updates, self.rebuilds, self.titles, self.usage,
                    self.throttles, self.snapshots):
            lst.clear()


@pytest.fixture
def store(monkeypatch):
    """One recorder store wired into every seam, state reset per test."""
    import storage.database as db
    from core.layers.codex import usage as codex_usage
    from services.billing import usage_service
    from services.engines import subscription_pool
    from services.notifications import notification_manager

    st = _Store()
    monkeypatch.setattr(db, "add_chat_message", st.add_chat_message)
    monkeypatch.setattr(db, "rebuild_chat_search", st.rebuilds.append)
    monkeypatch.setattr(db, "get_chat", st.get_chat)
    monkeypatch.setattr(db, "update_chat", st.update_chat)
    monkeypatch.setattr(db, "get_chat_messages", st.get_chat_messages)
    monkeypatch.setattr(usage_service, "record_turn_usage", st.usage.extend)
    monkeypatch.setattr(subscription_pool, "get_session_subscription", lambda sid: None)
    monkeypatch.setattr(subscription_pool, "get_session_payer_sub", lambda sid: None)
    monkeypatch.setattr(subscription_pool, "throttle_from_cli_error",
                        lambda sid, text: st.throttles.append(text))
    monkeypatch.setattr(notification_manager, "broadcast_chat_title",
                        lambda sub, cid, title, agent="": st.titles.append([sub, cid, title, agent]))

    def _snap(sid, snap, observed_at=None):
        st.snapshots.append([snap, observed_at])
        return True
    monkeypatch.setattr(codex_usage, "record_snapshot", _snap)
    _reset()
    yield st
    _reset()


def _reset() -> None:
    T._offsets.clear()
    T._tool_events.clear()
    T._attach_ts.clear()
    C._offsets.clear()
    C._resolved.clear()
    C._thread_saved.clear()
    C._tool_events.clear()
    C._usage_models.clear()
    _TE._sent_prompts.clear()
    for sid in list(session_state._subagent_registries):
        if sid.startswith("sid-char-"):
            session_state._subagent_registries.pop(sid, None)
    for sid in list(session_events._tool_records):
        if sid.startswith("sid-char-"):
            session_events._tool_records.pop(sid, None)


def _state(sid: str, mod) -> dict:
    """The per-session state a batch leaves behind."""
    reg = session_state.get_subagent_registry(sid)
    buf = mod._tool_events.get(sid)
    return {
        "registry": {"spawned": sorted(reg.spawned), "completed": sorted(reg.completed),
                     "has_pending": reg.has_pending},
        "pending": sorted(buf.pending) if buf is not None else [],
        "tool_records": [[r.tool_name, r.tool_use_id, r.is_error, r.source, sorted(r.paths)]
                         for r in session_events.tool_records(sid)],
        "offsets": dict(mod._offsets),
    }


def _result(sid: str, mod, store: _Store, results: list[dict]) -> dict:
    return {"results": results, **store.snapshot(), **_state(sid, mod)}


# ─── Claude ────────────────────────────────────────────────────────────────

CLAUDE_FILES = ("claude-transcript.jsonl", "claude-transcript-edges.jsonl")


@pytest.mark.parametrize("fname", CLAUDE_FILES)
def test_claude_whole(store, fname):
    sid = "sid-char-cw"
    results = [T.tail_lines(sid, CHAT, _lines(fname))]
    check_golden(f"tailer-claude-whole-{fname.split('.')[0]}", _result(sid, T, store, results))


@pytest.mark.parametrize("fname", CLAUDE_FILES)
def test_claude_line_by_line(store, fname):
    sid = "sid-char-cl"
    results = [T.tail_lines(sid, CHAT, [ln]) for ln in _lines(fname)]
    check_golden(f"tailer-claude-by-line-{fname.split('.')[0]}", _result(sid, T, store, results))


@pytest.mark.parametrize("fname", CLAUDE_FILES)
def test_claude_file_cuts(store, tmp_path, fname):
    """The local file through the cursor, appended in cuts: one between a
    tool_use line and its result, the rest at thirds."""
    sid = "sid-char-cf"
    lines = _lines(fname)
    tool_use_at = next((i for i, ln in enumerate(lines) if '"tool_use"' in ln), 0)
    cuts = sorted({tool_use_at + 1, len(lines) // 3, 2 * len(lines) // 3, len(lines)})
    path = tmp_path / "transcript.jsonl"
    results = []
    start = 0
    for cut in cuts:
        with path.open("a") as fh:
            fh.write("\n".join(lines[start:cut]) + ("\n" if cut > start else ""))
        start = cut
        results.append(T.tail_transcript(sid, CHAT, str(path)))
    results.append(T.tail_transcript(sid, CHAT, str(path)))   # nothing new
    results.append(T.tail_transcript(sid, CHAT, str(tmp_path / "missing.jsonl")))
    check_golden(f"tailer-claude-file-cuts-{fname.split('.')[0]}", _result(sid, T, store, results))


@pytest.mark.parametrize("fname", CLAUDE_FILES)
def test_claude_replay_guard(store, tmp_path, fname):
    """A resume re-warm's transcript replays the earlier lines as copies with
    their original stamps: the attach-time guard skips what is older than
    the attach, which here sits two seconds after the file's middle line."""
    sid = "sid-char-cg"
    lines = _lines(fname)
    stamped = [json.loads(ln) for ln in lines if ln.strip() and ln.lstrip().startswith("{")]
    times = [t for t in (T.line_time(o) for o in stamped) if t is not None]
    T._attach_ts[sid] = times[len(times) // 2] + 2.0
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n")
    results = [T.tail_transcript(sid, CHAT, str(path))]
    check_golden(f"tailer-claude-replay-guard-{fname.split('.')[0]}", _result(sid, T, store, results))


def test_claude_sent_prompt_consumed(store):
    """The dashboard persisted the cold prompt at send time: the journaled
    line is skipped, its turn signal and the title kept."""
    sid = "sid-char-cs"
    lines = _lines("claude-transcript.jsonl")
    first = next(json.loads(ln) for ln in lines if '"type": "user"' in ln and '"role": "user"' in ln)
    _TE.note_sent_prompt(CHAT, first["message"]["content"])
    results = [T.tail_lines(sid, CHAT, lines)]
    check_golden("tailer-claude-sent-prompt", _result(sid, T, store, results))


# ─── Codex ─────────────────────────────────────────────────────────────────

CODEX_FILES = ("codex-rollout.jsonl", "codex-rollout-edges.jsonl")


@pytest.fixture
def codex_store(store):
    store.row["model"] = "gpt-6.1-sol"
    return store


@pytest.mark.parametrize("fname", CODEX_FILES)
def test_codex_whole(codex_store, fname):
    sid = "sid-char-xw"
    results = [C.tail_lines(sid, CHAT, _lines(fname))]
    check_golden(f"tailer-codex-whole-{fname.split('.')[0]}", _result(sid, C, codex_store, results))


@pytest.mark.parametrize("fname", CODEX_FILES)
def test_codex_line_by_line(codex_store, fname):
    sid = "sid-char-xl"
    results = [C.tail_lines(sid, CHAT, [ln]) for ln in _lines(fname)]
    check_golden(f"tailer-codex-by-line-{fname.split('.')[0]}", _result(sid, C, codex_store, results))


@pytest.mark.parametrize("fname", CODEX_FILES)
def test_codex_file_cuts(codex_store, tmp_path, fname):
    """The local rollout through the cursor: the first tail is a first tail
    (the prefix merge against an empty history, usage calibrated), the next
    cuts land between a call and its output and at thirds."""
    sid = "sid-char-xf"
    lines = _lines(fname)
    call_at = next((i for i, ln in enumerate(lines)
                    if '"function_call"' in ln or '"custom_tool_call"' in ln), 0)
    cuts = sorted({call_at + 1, len(lines) // 3, 2 * len(lines) // 3, len(lines)})
    path = tmp_path / "rollout-2026-10-16T22-09-16-x.jsonl"
    results = []
    start = 0
    for cut in cuts:
        with path.open("a") as fh:
            fh.write("\n".join(lines[start:cut]) + ("\n" if cut > start else ""))
        start = cut
        results.append(C.tail_rollout(sid, CHAT, str(path)))
    results.append(C.tail_rollout(sid, CHAT, str(path)))
    results.append(C.tail_rollout(sid, CHAT, str(tmp_path / "missing.jsonl")))
    check_golden(f"tailer-codex-file-cuts-{fname.split('.')[0]}", _result(sid, C, codex_store, results))


@pytest.mark.parametrize("fname", CODEX_FILES)
def test_codex_first_tail_prefix_merge(codex_store, tmp_path, fname):
    """A restart's first tail re-reads the rollout from line 0 against the
    rows an earlier life persisted: nothing lands twice, the usage is
    calibrated, and two lines appended after it persist as new."""
    sid = "sid-char-xp"
    lines = _lines(fname)
    first = C.tail_lines("sid-char-xp-earlier", CHAT, lines)
    codex_store.prefix_rows = codex_store.persisted_as_prefix()
    earlier = codex_store.snapshot()
    codex_store.clear()
    _reset()
    path = tmp_path / "rollout-2026-10-16T22-09-16-y.jsonl"
    path.write_text("\n".join(lines) + "\n")
    results = [C.tail_rollout(sid, CHAT, str(path))]
    tail = [
        {"timestamp": "2026-10-16T23:00:00.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "one more"}]}},
        {"timestamp": "2026-10-16T23:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "And one more."}]}},
        {"timestamp": "2026-10-16T23:00:02.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 7}, "total_token_usage": {"total_tokens": 99999}}}},
        {"timestamp": "2026-10-16T23:00:03.000Z", "type": "event_msg",
         "payload": {"type": "task_complete", "last_agent_message": "And one more."}},
    ]
    with path.open("a") as fh:
        fh.write("\n".join(json.dumps(o) for o in tail) + "\n")
    results.append(C.tail_rollout(sid, CHAT, str(path)))
    check_golden(f"tailer-codex-prefix-merge-{fname.split('.')[0]}", {
        "earlier": {"result": first, **earlier},
        **_result(sid, C, codex_store, results),
    })


def test_codex_sent_prompt_consumed(codex_store):
    """The single-lined launch argument's prompt against the note's shape:
    the sent-prompt match folds whitespace."""
    sid = "sid-char-xs"
    lines = _lines("codex-rollout.jsonl")
    first = next(json.loads(ln) for ln in lines if '"role": "user"' in ln and "Current time" in ln)
    text = first["payload"]["content"][0]["text"]
    _TE.note_sent_prompt(CHAT, text.replace("\n\n", "\n"))
    results = [C.tail_lines(sid, CHAT, lines)]
    check_golden("tailer-codex-sent-prompt", _result(sid, C, codex_store, results))
