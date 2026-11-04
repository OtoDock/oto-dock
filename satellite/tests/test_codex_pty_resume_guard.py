"""Interactive-Codex resume guard (sat 0.5.98) + PTY output injection.

``codex resume <thread-id>`` HARD-EXITS (code 1) when the thread's rollout
isn't in this machine's CODEX_HOME — the empty-terminal instant-death class.
The guard degrades to a COLD start; the visible notice rides
``PtyProcess.inject_output`` so attach-replay keeps it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque

import pytest

if sys.platform == "win32":  # pragma: no cover - unix backend under test
    pytest.skip("unix pty backend", allow_module_level=True)

from satellite._vendored.terminal_queries import PasteModeTracker
from satellite.terminal.codex_pty_session import CodexPtySession
from satellite.terminal.pty_relay import PtyProcess


def _bare_session(tmp_path, *, resume: bool, thread_id: str, local: bool = False) -> CodexPtySession:
    s = CodexPtySession.__new__(CodexPtySession)
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
        """Codex 0.166 refuses a thread recorded with another model provider
        (the chat moved between a local model and OpenAI while idle): a cold
        start with its own notice instead of a terminal that exits."""
        s = _bare_session(tmp_path, resume=True, thread_id="tid-p", local=local)
        _write_rollout(s._codex_dir, "tid-p", recorded)
        s._apply_resume_guard()
        assert s._resume is resumes
        assert s._resume_degraded == ("" if resumes else "provider")

    def test_cold_start_untouched(self, tmp_path):
        s = _bare_session(tmp_path, resume=False, thread_id="")
        s._apply_resume_guard()
        assert s._resume is False
        assert not s._resume_degraded


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
