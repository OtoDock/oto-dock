"""The SessionStart hook that gives a resumed Codex thread its session's text
once (``hooks/session_text.py``): run as Codex runs it, a separate process
with the session's environment and the event on stdin."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config as app_config
from core.layers.codex import session_text
from core.sandbox.session_config_dir import write_if_changed as _W

HOOK = Path(app_config.HOOKS_DIR) / "session_text.py"


def _run(home: Path, session_id: str, thread: str, source: str, transcript: str = "",
         store: Path | None = None) -> str:
    env = {"PATH": os.environ.get("PATH", ""), "CODEX_HOME": str(home),
           "OTO_SESSION_ID": session_id, **({"OTO_HOOKS_DIR": str(store)} if store else {})}
    out = subprocess.run(
        [sys.executable, "-I", str(HOOK)], env=env, capture_output=True, text=True, timeout=10,
        input=json.dumps({"session_id": thread, "source": source, "hook_event_name": "SessionStart",
                          "transcript_path": transcript or None}),
    )
    assert out.returncode == 0, out.stderr
    if not out.stdout:
        return ""
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


def _record(home: Path, thread: str) -> str:
    path = home / f"{thread}{session_text.RECORD_SUFFIX}"
    return path.read_text().strip() if path.exists() else ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_a_new_thread_records_and_adds_nothing(tmp_path):
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)
    assert _run(tmp_path, "s-1", "t-1", "startup") == ""
    assert _record(tmp_path, "t-1") == _sha("INSTR-A")


def test_a_resume_with_the_same_text_adds_nothing(tmp_path):
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)
    _run(tmp_path, "s-1", "t-1", "startup")
    session_text.write_session_files(tmp_path, "s-2", text="INSTR-A", config={}, writer=_W)
    assert _run(tmp_path, "s-2", "t-1", "resume") == ""


def test_a_resume_with_another_text_adds_it_once(tmp_path):
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)
    _run(tmp_path, "s-1", "t-1", "startup")
    session_text.write_session_files(tmp_path, "s-2", text="INSTR-A2", config={}, writer=_W)
    added = _run(tmp_path, "s-2", "t-1", "resume")
    assert added.endswith("\n\nINSTR-A2") and added.startswith("These instructions replace")
    assert _record(tmp_path, "t-1") == _sha("INSTR-A2")
    # The next start of the same session (a reload) adds nothing again.
    assert _run(tmp_path, "s-2", "t-1", "resume") == ""


def test_a_threads_record_outlives_the_home_it_was_made_in(tmp_path):
    """A session in a home of its own reads its text there and keeps the
    thread's record in the store (``OTO_HOOKS_DIR``): the home goes at the
    close, and the same chat's next home resumes the thread with no second
    copy of the same text."""
    store = tmp_path / "store"
    first, second = store / "own" / "a", store / "own" / "b"
    for home in (first, second):
        home.mkdir(parents=True)
        session_text.write_session_files(home, "s-1", text="INSTR-A", config={}, writer=_W)
    assert _run(first, "s-1", "t-1", "startup", store=store) == ""
    assert _record(store, "t-1") == _sha("INSTR-A") and _record(first, "t-1") == ""
    assert _run(second, "s-1", "t-1", "resume", store=store) == ""


def test_a_thread_from_before_gets_the_text_once(tmp_path):
    """No record: the thread held the home's AGENTS.md (an upgrade)."""
    big = "\n".join(f"Rule {i}." for i in range(5000))
    session_text.write_session_files(tmp_path, "s-1", text=big, config={}, writer=_W)
    assert _run(tmp_path, "s-1", "t-old", "resume").endswith(big)
    assert _run(tmp_path, "s-1", "t-old", "resume") == ""


def test_compaction_and_a_clear_record_only(tmp_path):
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)
    for source in ("compact", "clear"):
        assert _run(tmp_path, "s-1", f"t-{source}", source) == ""
        assert _record(tmp_path, f"t-{source}") == _sha("INSTR-A")


def _fork_rollout(tmp_path: Path, parent: str) -> str:
    path = tmp_path / "rollout-fork.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "t-fork", "forked_from_id": parent}}) + "\n")
    return str(path)


def test_a_fork_is_recorded_only_when_its_parent_held_the_text(tmp_path):
    """A fork holds its parent's history: a parent that held an older text
    leaves the fork unrecorded, so the fork's next resume gets the text."""
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)
    _run(tmp_path, "s-1", "t-par", "startup")
    assert _run(tmp_path, "s-1", "t-fork", "fork", _fork_rollout(tmp_path, "t-par")) == ""
    assert _record(tmp_path, "t-fork") == _sha("INSTR-A")
    session_text.write_session_files(tmp_path, "s-2", text="INSTR-A2", config={}, writer=_W)
    assert _run(tmp_path, "s-2", "t-fork2", "fork", _fork_rollout(tmp_path, "t-par")) == ""
    assert _record(tmp_path, "t-fork2") == ""
    assert _run(tmp_path, "s-2", "t-fork2", "resume").endswith("INSTR-A2")
    # No readable rollout: no parent, no record.
    assert _run(tmp_path, "s-1", "t-fork3", "fork", str(tmp_path / "missing.jsonl")) == ""
    assert _record(tmp_path, "t-fork3") == ""


def test_the_record_is_written_only_after_the_text_went_out(tmp_path, monkeypatch):
    """A hook whose output fails (or that is cut at its timeout) leaves no
    record: the thread would otherwise count as holding a text it never got."""
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location("session_text_hook_once", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)

    class _Broken:
        def write(self, _data):
            raise OSError("the pipe closed")

        def flush(self):
            raise OSError("the pipe closed")

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("OTO_SESSION_ID", "s-1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "t-1", "source": "resume"})))
    monkeypatch.setattr(sys, "stdout", _Broken())
    with pytest.raises(OSError):
        hook.main()
    assert _record(tmp_path, "t-1") == ""


def test_a_session_without_its_own_text_is_left_alone(tmp_path):
    """A session started the old way (an older platform): nothing to add."""
    assert _run(tmp_path, "s-old", "t-1", "resume") == ""
    assert _record(tmp_path, "t-1") == ""
    session_text.write_session_files(tmp_path, "s-empty", text="", config={}, writer=_W)
    assert _run(tmp_path, "s-empty", "t-1", "resume") == ""


def test_a_name_that_is_a_path_is_refused(tmp_path):
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)
    assert _run(tmp_path, "s-1", "../t", "resume") == ""
    assert _run(tmp_path, "../s-1", "t-1", "resume") == ""


def test_a_fifo_a_neighbour_planted_never_blocks_the_hook(tmp_path):
    """A FIFO at the session's file or at a thread's record: the hook reads
    neither as text and ends at once (Codex would otherwise wait out its
    timeout at every start of the session)."""
    os.mkfifo(tmp_path / "s-1.session.json")
    assert _run(tmp_path, "s-1", "t-1", "resume") == ""
    (tmp_path / "s-1.session.json").unlink()
    session_text.write_session_files(tmp_path, "s-1", text="INSTR-A", config={}, writer=_W)
    os.mkfifo(tmp_path / "t-1.text-sha256")
    assert _run(tmp_path, "s-1", "t-1", "resume").endswith("INSTR-A")


def test_a_fifo_at_the_session_file_reads_as_none(tmp_path):
    """The compute machine reads the platform's ``.session.json`` in a home
    a session can write: a FIFO there is no file, never a wait."""
    import threading
    os.mkfifo(tmp_path / "s-1.session.json")
    got: list = []
    worker = threading.Thread(target=lambda: got.append(session_text.read_session(tmp_path, "s-1")),
                              daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        os.close(os.open(tmp_path / "s-1.session.json", os.O_WRONLY | os.O_NONBLOCK))
        raise AssertionError("read_session waited on a FIFO")
    assert got == [None]
