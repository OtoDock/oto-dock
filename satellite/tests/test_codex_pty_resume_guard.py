"""Interactive-Codex resume guard (sat 0.5.98) + PTY output injection.

``codex resume <thread-id>`` HARD-EXITS (code 1) when the thread's rollout
isn't in this machine's CODEX_HOME — the empty-terminal instant-death class.
The guard degrades to a COLD start; the visible notice rides
``PtyProcess.inject_output`` so attach-replay keeps it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from pathlib import Path

import pytest

if sys.platform == "win32":  # pragma: no cover - unix backend under test
    pytest.skip("unix pty backend", allow_module_level=True)

from satellite._vendored.terminal_queries import PasteModeTracker
from satellite.terminal.codex_pty_session import CodexPtySession
from satellite.terminal.pty_relay import PtyProcess


def _bare_session(tmp_path, *, resume: bool, thread_id: str, local: bool = False) -> CodexPtySession:
    s = CodexPtySession.__new__(CodexPtySession)
    s.session_id = "sess-bare-1"
    s.agent_dir = tmp_path
    s._codex_dir = tmp_path / ".codex"
    s._codex_dir.mkdir(parents=True, exist_ok=True)
    s._resume = resume
    s._thread_id = thread_id
    s._transcript_skip_existing = resume
    s.config = {"local_model_provider": {"base_url": "http://127.0.0.1:11434/v1"}} if local else {}
    return s


def _write_rollout(codex_dir, thread_id: str, provider: str = "") -> None:
    day = codex_dir / "sessions" / "2026" / "07" / "19"
    day.mkdir(parents=True)
    meta = (json.dumps({"type": "session_meta", "payload": {"model_provider": provider}})
            if provider else "{}")
    (day / f"rollout-2026-07-19T03-00-00-{thread_id}.jsonl").write_text(meta + "\n")


class TestRolloutOnDisk:
    def test_missing_sessions_dir(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-1")
        assert s._rollout_on_disk("tid-1") is False

    def test_found_by_thread_suffix(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-1")
        _write_rollout(s._codex_dir, "tid-1")
        assert s._rollout_on_disk("tid-1") is True
        assert s._rollout_on_disk("tid-other") is False

    def test_empty_thread_id(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="")
        assert s._rollout_on_disk("") is False


class TestResumeGuard:
    def test_degrades_when_rollout_missing(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-gone")
        s._apply_resume_guard()
        assert s._resume is False
        assert s._resume_degraded == "missing"
        assert s._transcript_skip_existing is False

    def test_keeps_resume_when_rollout_present(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-here")
        _write_rollout(s._codex_dir, "tid-here")
        s._apply_resume_guard()
        assert s._resume is True
        assert not s._resume_degraded
        assert s._transcript_skip_existing is True

    @pytest.mark.parametrize("recorded,local,resumes", [
        ("openai", False, True), ("oto_local", True, True),
        ("openai", True, False), ("oto_local", False, False), ("", True, True),
    ])
    def test_a_thread_of_another_provider_starts_fresh(self, tmp_path, recorded, local, resumes):
        """A terminal of Codex 0.168 and later resumes a thread on the provider
        its rollout recorded, whatever ``config.toml`` names (the chat moved
        between a local model and OpenAI while idle): a cold start with its
        own notice instead. A rollout that names no provider resumes."""
        s = _bare_session(tmp_path, resume=True, thread_id="tid-p", local=local)
        _write_rollout(s._codex_dir, "tid-p", recorded)
        s._apply_resume_guard()
        assert s._resume is resumes
        assert s._resume_degraded == ("" if resumes else "provider")

    @pytest.mark.parametrize("recorded,resumes", [("openai", True), ("oto_local", False)])
    def test_a_payload_with_a_blank_address_is_a_hosted_session(self, tmp_path, recorded, resumes):
        """No provider is written for a blank address (``local_provider_toml``),
        so the guard reads such a payload as hosted, as the headless session
        does."""
        s = _bare_session(tmp_path, resume=True, thread_id="tid-b")
        s.config = {"local_model_provider": {"base_url": "  "}}
        _write_rollout(s._codex_dir, "tid-b", recorded)
        s._apply_resume_guard()
        assert s._resume is resumes
        assert s._resume_degraded == ("" if resumes else "provider")

    def test_a_compute_session_on_a_local_model_resumes_its_local_thread(self, tmp_path):
        """A compute machine runs the platform's own config.toml: its payload
        says the plan runs on a local model (``local_model``) and carries no
        provider to write. A thread recorded on the local provider resumes."""
        s = _bare_session(tmp_path, resume=True, thread_id="tid-c")
        s.config = {"local_model": True}
        _write_rollout(s._codex_dir, "tid-c", "oto_local")
        s._apply_resume_guard()
        assert s._resume is True and not s._resume_degraded
        assert s._transcript_skip_existing is True

    def test_cold_start_untouched(self, tmp_path):
        s = _bare_session(tmp_path, resume=False, thread_id="")
        s._apply_resume_guard()
        assert s._resume is False
        assert not s._resume_degraded


def _tui_rollout(codex_dir, name: str, cwd: str, mtime: float) -> str:
    day = codex_dir / "sessions" / "2026" / "11" / "09"
    day.mkdir(parents=True, exist_ok=True)
    path = day / name
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "originator": "codex-tui", "cwd": cwd, "model_provider": "openai"}}) + "\n"
        + json.dumps({"type": "response_item", "payload": {"role": "user", "content": [
            {"type": "input_text", "text": "[Current time: 02:45] Say hello."}]}}) + "\n")
    os.utime(path, (mtime, mtime))
    return str(path)


class TestDegradedResumeTail:
    def test_a_degraded_resume_never_tails_the_old_threads_rollout(self, tmp_path):
        """A resume that starts cold (another provider, a missing rollout)
        writes a new rollout: the thread's old one, which holds the history
        the chat already shows, is never pinned and forwarded from its start."""
        import time as _time
        s = _bare_session(tmp_path, resume=True, thread_id="tid-old")
        s._compute, s._cwd, s._transcript_pin = False, tmp_path / "work", None
        now = _time.time()
        _tui_rollout(s._codex_dir, "rollout-2026-11-09T02-45-00-tid-old.jsonl", str(s._cwd), now - 600)
        s.config = {"local_model_provider": {"base_url": "http://127.0.0.1:11434/v1"}}
        s._apply_resume_guard()
        assert s._resume_degraded == "provider"
        s._started_at = now
        assert s._locate_transcript_file() is None
        new = _tui_rollout(s._codex_dir, "rollout-2026-11-09T02-52-00-tid-new.jsonl", str(s._cwd), now + 1)
        assert s._locate_transcript_file() == os.path.relpath(new, tmp_path)


def _day(codex_dir, *parts: str):
    d = codex_dir / "sessions" / Path(*(parts or ("2026", "07", "19")))
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestBoundedSearch:
    """The rollouts are searched beneath the agent's folder, the platform's
    ``find_rollout`` shape: real folders only, newest first, eight deep at
    most, a regular file named for a checked thread id; a search past its
    cap could not tell, and the resume stays."""

    def test_a_linked_day_folder_or_sessions_folder_is_not_searched(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-l")
        stand_in = tmp_path / "outside" / "2026" / "07" / "19"
        stand_in.mkdir(parents=True)
        (stand_in / "rollout-2026-07-19T03-00-00-tid-l.jsonl").write_text("{}\n")
        _day(s._codex_dir, "2026", "07")
        (s._codex_dir / "sessions" / "2026" / "07" / "19").symlink_to(stand_in, target_is_directory=True)
        assert s._rollout_on_disk("tid-l") is False
        import shutil
        shutil.rmtree(s._codex_dir / "sessions")
        (s._codex_dir / "sessions").symlink_to(tmp_path / "outside", target_is_directory=True)
        assert s._rollout_on_disk("tid-l") is False
        s._apply_resume_guard()
        assert s._resume_degraded == "missing"

    def test_an_id_that_cannot_name_a_file_finds_nothing(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-1")
        _write_rollout(s._codex_dir, "tid-1")
        for tid in ("*", "tid-?", "tid-[1]", "../tid-1", "a/b", "x" * 129):
            assert s._rollout_on_disk(tid) is False, tid
        assert s._rollout_on_disk("tid-1") is True

    def test_a_rollout_deeper_than_the_bound_is_not_found(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-d")
        deep = _day(s._codex_dir, *"abcdefghi")
        (deep / "rollout-2026-07-19T03-00-00-tid-d.jsonl").write_text("{}\n")
        assert s._rollout_on_disk("tid-d") is False
        shallow = _day(s._codex_dir, *"abcdefgh")
        (shallow / "rollout-2026-07-19T03-00-00-tid-d.jsonl").write_text("{}\n")
        assert s._rollout_on_disk("tid-d") is True

    def test_a_pipe_named_as_the_rollout_is_not_one(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-f")
        os.mkfifo(_day(s._codex_dir) / "rollout-2026-07-19T03-00-00-tid-f.jsonl")
        assert s._rollout_on_disk("tid-f") is False
        s._apply_resume_guard()  # never waits on the pipe
        assert s._resume_degraded == "missing"

    def test_a_first_record_over_64_kib_names_no_provider(self, tmp_path):
        s = _bare_session(tmp_path, resume=True, thread_id="tid-m", local=True)
        meta = {"type": "session_meta", "payload": {"model_provider": "openai", "pad": "x" * 70_000}}
        (_day(s._codex_dir) / "rollout-2026-07-19T03-00-00-tid-m.jsonl").write_text(json.dumps(meta) + "\n")
        s._apply_resume_guard()
        assert s._resume is True and not s._resume_degraded

    def test_a_search_past_its_cap_could_not_tell_and_keeps_the_resume(self, tmp_path, monkeypatch):
        from satellite.terminal import codex_pty_session as mod
        monkeypatch.setattr(mod, "_FIND_MAX_ENTRIES", 3)
        s = _bare_session(tmp_path, resume=True, thread_id="tid-c")
        for i in range(4):
            (_day(s._codex_dir) / f"note-{i}.txt").write_text("")
        assert s._rollout_on_disk("tid-c") is True
        s._apply_resume_guard()
        assert s._resume is True and not s._resume_degraded and s._transcript_skip_existing
        # Past the cap across folders, each under it.
        s = _bare_session(tmp_path / "b", resume=True, thread_id="tid-c")
        for year in ("2024", "2025"):
            for i in range(2):
                (_day(s._codex_dir, year) / f"note-{i}.txt").write_text("")
        assert s._rollout_on_disk("tid-c") is True

    def test_the_newest_dated_folder_is_searched_first(self, tmp_path):
        from satellite.terminal import codex_pty_session as mod
        s = _bare_session(tmp_path, resume=True, thread_id="tid-n")
        for year in ("2025", "2026", "2024"):
            (_day(s._codex_dir, year, "01", "01") / f"rollout-{year}-01-01T00-00-00-tid-n.jsonl").write_text("{}\n")
        assert s._find_rollout("tid-n") == (
            mod._FOUND, ".codex/sessions/2026/01/01/rollout-2026-01-01T00-00-00-tid-n.jsonl")

    def test_a_rollout_pinned_by_one_terminal_is_not_taken_by_another(self, tmp_path, monkeypatch):
        import time as _time

        from satellite.terminal import codex_pty_session as mod
        monkeypatch.setattr(mod, "_pinned_rollouts", set())
        now = _time.time()
        a, b = (_bare_session(tmp_path, resume=False, thread_id="") for _ in range(2))
        for s in (a, b):
            s._compute, s._cwd, s._transcript_pin, s._started_at = False, tmp_path / "work", None, now
        first = _tui_rollout(a._codex_dir, "rollout-2026-11-09T02-52-00-tid-a.jsonl", str(a._cwd), now + 1)
        assert a._locate_transcript_file() == os.path.relpath(first, tmp_path)
        assert mod._pinned_rollouts == {first}
        assert b._locate_transcript_file() is None
        second = _tui_rollout(b._codex_dir, "rollout-2026-11-09T02-53-00-tid-b.jsonl", str(b._cwd), now + 2)
        assert b._locate_transcript_file() == os.path.relpath(second, tmp_path)
        a._on_close()
        assert mod._pinned_rollouts == {second}

    def test_a_resume_names_no_rollout_by_an_id_that_cannot_name_a_file(self, tmp_path):
        import time as _time
        s = _bare_session(tmp_path, resume=True, thread_id="*")
        s._compute, s._cwd, s._transcript_pin, s._started_at = False, tmp_path / "work", None, _time.time()
        day = _day(s._codex_dir, "2026", "11", "10")
        (day / "rollout-2026-11-10T00-00-00-tid-e.jsonl").write_text(json.dumps(
            {"type": "session_meta", "payload": {"originator": "codex_exec", "cwd": str(s._cwd)}}) + "\n")
        assert s._locate_transcript_file() is None

    def test_the_fresh_search_takes_no_rollout_through_a_link(self, tmp_path):
        import time as _time
        s = _bare_session(tmp_path, resume=False, thread_id="")
        s._compute, s._cwd, s._transcript_pin = False, tmp_path / "work", None
        now = _time.time()
        s._started_at = now
        stand_in = tmp_path / "outside" / "sessions" / "2026" / "11" / "09"
        stand_in.mkdir(parents=True)
        _tui_rollout(tmp_path / "outside", "rollout-2026-11-09T02-45-00-tid-x.jsonl", str(s._cwd), now + 1)
        _day(s._codex_dir, "2026", "11")
        (s._codex_dir / "sessions" / "2026" / "11" / "10").symlink_to(stand_in, target_is_directory=True)
        assert s._locate_transcript_file() is None
        new = _tui_rollout(s._codex_dir, "rollout-2026-11-09T02-52-00-tid-n.jsonl", str(s._cwd), now + 1)
        assert s._locate_transcript_file() == os.path.relpath(new, tmp_path)


class TestInjectOutput:
    @pytest.mark.asyncio
    async def test_scrollback_and_lane(self):
        out: list[bytes] = []
        p = PtyProcess.__new__(PtyProcess)
        p._scrollback = deque()
        p._scrollback_len = 0
        p._scrollback_limit = 65536
        p._paste_mode = PasteModeTracker()
        p._on_output = out.append
        p._loop = asyncio.get_event_loop()
        p.inject_output(b"\x1b[2mnotice\x1b[0m\r\n")
        assert b"notice" in p.scrollback()
        assert out and b"notice" in out[0]

    @pytest.mark.asyncio
    async def test_async_on_output_scheduled(self):
        got = asyncio.Event()

        async def _sink(data: bytes):
            got.set()

        p = PtyProcess.__new__(PtyProcess)
        p._scrollback = deque()
        p._scrollback_len = 0
        p._scrollback_limit = 65536
        p._paste_mode = PasteModeTracker()
        p._on_output = _sink
        p._loop = asyncio.get_event_loop()
        p.inject_output(b"x")
        await asyncio.wait_for(got.wait(), timeout=2.0)


class TestLeadingFeaturesDedupe:
    """Older proxies prepend a bare [features] block to the shipped MCP TOML
    for dashboard chats; the interactive preamble ALREADY declares [features],
    and a duplicate table hard-exits the strict TUI with a blank terminal
    (root-caused live 2026-07-19: config.toml:13:2 duplicate key)."""

    def test_leading_features_block_is_stripped_and_valid(self):
        from satellite.terminal.codex_pty_session import (
            _build_codex_config_toml,
            _strip_leading_features_block,
        )
        from satellite.tests._toml import toml_loads
        shipped = (
            "[features]\ndefault_mode_request_user_input = true\n\n"
            '[mcp_servers.x]\ncommand = "node"\n'
        )
        assert _strip_leading_features_block(shipped).startswith("[mcp_servers.x]")
        out = _build_codex_config_toml("/tmp/cwd", shipped)
        data = toml_loads(out)  # duplicate [features] would raise
        # The preamble's own flag survives; the MCP section is intact.
        assert data["features"]["default_mode_request_user_input"] is True
        assert data["mcp_servers"]["x"]["command"] == "node"

    def test_non_features_toml_untouched(self):
        from satellite.terminal.codex_pty_session import (
            _strip_leading_features_block,
        )
        toml = '[mcp_servers.y]\ncommand = "python3"\n'
        assert _strip_leading_features_block(toml) == toml

    def test_features_only_toml_strips_to_empty(self):
        from satellite.terminal.codex_pty_session import (
            _strip_leading_features_block,
        )
        assert _strip_leading_features_block(
            "[features]\ndefault_mode_request_user_input = true"
        ) == ""


class TestSearchWarnings:
    """The fresh search past its cap keeps the newest rollout it met, and a
    link it meets is said once a session (the poll repeats every 0.8 s)."""

    def _fresh(self, tmp_path, monkeypatch):
        import time as _time

        from satellite.terminal import codex_pty_session as mod
        monkeypatch.setattr(mod, "_pinned_rollouts", set())
        s = _bare_session(tmp_path, resume=False, thread_id="")
        s._compute, s._cwd, s._transcript_pin = False, tmp_path / "work", None
        s._started_at = _time.time()
        return s

    def test_a_search_past_its_cap_keeps_the_newest_rollout_it_met(self, tmp_path, monkeypatch, caplog):
        import logging

        from satellite.terminal import codex_pty_session as mod
        monkeypatch.setattr(mod, "_FIND_MAX_ENTRIES", 5)
        s = self._fresh(tmp_path, monkeypatch)
        new = _tui_rollout(s._codex_dir, "rollout-2026-11-10T02-52-00-tid-n.jsonl", str(s._cwd),
                           s._started_at + 1)
        old = _day(s._codex_dir, "2025", "01", "01")
        for i in range(4):
            (old / f"note-{i}.txt").write_text("")
        with caplog.at_level(logging.WARNING):
            assert s._locate_transcript_file() == os.path.relpath(new, tmp_path)
            assert s._locate_transcript_file() == os.path.relpath(new, tmp_path)
        assert len([r for r in caplog.records if "search stopped" in r.getMessage()]) == 1

    def test_a_linked_day_folder_is_said_once(self, tmp_path, monkeypatch, caplog):
        import logging
        s = self._fresh(tmp_path, monkeypatch)
        stand_in = tmp_path / "outside"
        stand_in.mkdir()
        _day(s._codex_dir, "2026", "11")
        (s._codex_dir / "sessions" / "2026" / "11" / "10").symlink_to(stand_in, target_is_directory=True)
        with caplog.at_level(logging.WARNING):
            assert s._locate_transcript_file() is None
            assert s._locate_transcript_file() is None
        said = [r.getMessage() for r in caplog.records if "link" in r.getMessage()]
        assert len(said) == 1 and ".codex/sessions/2026/11/10" in said[0]


class TestResumePastTheCap:
    """A resume whose walk was cut at the cap before it met its own rollout
    takes no other session's rollout, says so once, and waits a minute
    before it searches again."""

    def _resumed(self, tmp_path, monkeypatch):
        import time as _time

        from satellite.terminal import codex_pty_session as mod
        monkeypatch.setattr(mod, "_pinned_rollouts", set())
        s = _bare_session(tmp_path, resume=True, thread_id="tid-own")
        s._compute, s._cwd, s._transcript_pin = False, tmp_path / "work", None
        s._started_at = _time.time()
        old = _day(s._codex_dir, "2025", "01", "01")
        (old / "rollout-2025-01-01T00-00-00-tid-own.jsonl").write_text(json.dumps(
            {"type": "session_meta", "payload": {"originator": "codex-tui", "cwd": str(s._cwd)}}) + "\n")
        other = _tui_rollout(s._codex_dir, "rollout-2026-11-09T02-45-00-tid-other.jsonl", str(s._cwd),
                             s._started_at - 60)
        return s, other

    def test_a_resume_cut_at_the_cap_takes_no_other_rollout(self, tmp_path, monkeypatch, caplog):
        import logging

        from satellite.terminal import codex_pty_session as mod
        s, _other = self._resumed(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "_FIND_MAX_ENTRIES", 5)
        with caplog.at_level(logging.WARNING):
            assert s._locate_transcript_file() is None
            assert s._locate_transcript_file() is None
        said = [r.getMessage() for r in caplog.records if "before it met" in r.getMessage()]
        assert len(said) == 1

    def test_a_capped_search_that_took_nothing_waits_a_minute(self, tmp_path, monkeypatch):
        from satellite.terminal import codex_pty_session as mod
        s, _other = self._resumed(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "_FIND_MAX_ENTRIES", 5)
        walks = []
        real = mod._walk_rollouts

        def counted(*a, **k):
            walks.append(1)
            return real(*a, **k)
        monkeypatch.setattr(mod, "_walk_rollouts", counted)
        now = [1000.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
        assert s._locate_transcript_file() is None
        assert s._locate_transcript_file() is None
        assert len(walks) == 1
        now[0] += 61
        s._locate_transcript_file()
        assert len(walks) == 2

    def test_with_the_real_cap_the_resume_finds_its_own(self, tmp_path, monkeypatch):
        s, _other = self._resumed(tmp_path, monkeypatch)
        assert s._locate_transcript_file() == ".codex/sessions/2025/01/01/rollout-2025-01-01T00-00-00-tid-own.jsonl"
