"""Tests for session manager routing and capabilities."""

import asyncio
import json
import os
import platform
import shutil
import stat
import sys
from unittest.mock import AsyncMock, patch

import pytest

from satellite import config as satcfg
from satellite.config import SatelliteConfig
from satellite.sessions.session_manager import SessionManager


@pytest.fixture
def sat_config(tmp_path):
    return SatelliteConfig(
        machine_id="test-machine",
        machine_secret="test-secret",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents",
        mcps_dir=tmp_path / "mcps",
    )


class TestDetectCapabilities:
    def test_returns_os_and_arch(self, sat_config):
        sm = SessionManager(sat_config)
        caps = sm.detect_capabilities()
        assert caps["os"] == platform.system().lower()
        assert caps["arch"] == platform.machine()

    def test_detects_installed_clis(self, sat_config):
        sm = SessionManager(sat_config)
        with patch.object(shutil, "which", side_effect=lambda x: "/usr/bin/claude" if "claude" in str(x) else None):
            caps = sm.detect_capabilities()
        assert "claude-code" in caps["installed_clis"]
        assert "codex" not in caps["installed_clis"]

    def test_detects_both_clis(self, sat_config):
        sm = SessionManager(sat_config)
        with patch.object(shutil, "which", return_value="/usr/bin/tool"):
            caps = sm.detect_capabilities()
        assert "claude-code" in caps["installed_clis"]
        assert "codex" in caps["installed_clis"]

    def test_says_it_streams_between_turns(self, sat_config):
        assert SessionManager(sat_config).detect_capabilities()["between_turns_stream"] is True

    @pytest.mark.asyncio
    async def test_stop_turn_passes_the_stream_ask_and_an_older_frame_keeps_the_bound(
            self, sat_config, tmp_path):
        """``stream_between_turns`` reaches the session; a frame from an older
        platform (no such field) keeps the bounded rule."""
        from satellite.sessions.cli_session import CLISession
        sm = SessionManager(sat_config)
        session = CLISession("sess-st", tmp_path, {
            "cwd_relative": "users/alice", "claude_dir_relative": "users/alice/.claude",
            "system_prompt": "", "mcp_config": {"mcpServers": {}}, "model": "claude-sonnet-5",
            "env": {}}, sat_config)
        sm.sessions["sess-st"] = session
        await sm.stop_turn({"session_id": "sess-st", "drain_bg": False, "drain_bg_s": 60,
                            "stream_between_turns": True})
        assert session._drain_bg and session._drain_bg_s == float("inf")
        await sm.stop_turn({"session_id": "sess-st", "drain_bg": True, "drain_bg_s": 60})
        assert session._drain_bg and session._drain_bg_s == 60.0

    def test_detects_installed_mcps(self, sat_config):
        sm = SessionManager(sat_config)
        # Create a mock MCP directory
        sat_config.mcps_dir.mkdir(parents=True)
        mcp_dir = sat_config.mcps_dir / "test-mcp"
        mcp_dir.mkdir()
        (mcp_dir / "manifest.json").write_text("{}")

        caps = sm.detect_capabilities()
        assert "test-mcp" in caps["installed_mcps"]

    def test_no_mcps_dir(self, sat_config):
        sm = SessionManager(sat_config)
        # mcps_dir doesn't exist
        caps = sm.detect_capabilities()
        assert caps["installed_mcps"] == []


class TestSessionRegistry:
    def test_starts_empty(self, sat_config):
        sm = SessionManager(sat_config)
        assert len(sm.sessions) == 0

    def test_cleanup_orphans_no_crash(self, sat_config):
        sm = SessionManager(sat_config)
        # Should not crash even without pid dir
        sm.cleanup_orphans()


class TestStartSessionReplacement:
    @pytest.mark.asyncio
    async def test_same_id_respawn_closes_stale_process(self, sat_config):
        """A start for a live session id (platform lost its handle, e.g. proxy
        restart mid-turn) must close the old process before spawning the
        replacement — otherwise the old CLI lingers satellite-parented forever."""
        sm = SessionManager(sat_config)
        stale = AsyncMock()
        stale.pid = 111
        sm.sessions["sess-1"] = stale

        replacement = AsyncMock()
        replacement.pid = 222
        ws = AsyncMock()
        # The ENGINES table resolves the class from its own module at
        # dispatch, so the patch lands where the class lives.
        with patch(
            "satellite.sessions.cli_session.CLISession",
            return_value=replacement,
        ):
            await sm.start_session({
                "session_id": "sess-1",
                "execution_path": "claude-code-cli",
                "config": {},
                "command_id": "cmd-1",
                "agent_slug": "test-agent",
            }, ws)

        stale.close.assert_awaited_once()
        replacement.start.assert_awaited_once()
        assert sm.sessions["sess-1"] is replacement

    @pytest.mark.asyncio
    async def test_an_engine_gone_with_no_turn_reading_is_told_to_the_platform(self, sat_config):
        """The session's own word (``on_exit``) becomes ``session_ended``
        with the reason, so the platform reads the session dead before the
        next message. The session stays held: the platform's close cleans up."""
        sm = SessionManager(sat_config)
        started = AsyncMock()
        started.pid, started.session_id = 222, "sess-1"
        ws = _CapturingWS()
        with patch("satellite.sessions.cli_session.CLISession", return_value=started):
            await sm.start_session({"session_id": "sess-1", "execution_path": "claude-code-cli",
                                    "config": {}, "command_id": "cmd-1", "agent_slug": "test-agent"}, ws)
        ack = next(m for m in ws.sent if m["type"] == "ack")
        assert ack["status"] == "ok" and ack["incarnation"] == started.incarnation != ""
        ws.sent.clear()
        await started.on_exit(137)
        assert ws.sent == [{"type": "session_ended", "session_id": "sess-1", "incarnation": started.incarnation,
                            "reason": "exited", "exit_code": 137}]
        assert sm.sessions["sess-1"] is started
        # The memory guard's end is named as such, and a code that is no number is left out.
        ws.sent.clear()
        sm.mark_ended_for_memory("sess-1")
        await started.on_exit(None)
        assert ws.sent == [{"type": "session_ended", "session_id": "sess-1", "incarnation": started.incarnation,
                            "reason": "memory"}]

    @pytest.mark.asyncio
    async def test_a_session_the_platform_closed_or_replaced_tells_nothing(self, sat_config):
        sm = SessionManager(sat_config)
        first, second = AsyncMock(), AsyncMock()
        first.pid, first.session_id, second.pid, second.session_id = 1, "sess-1", 2, "sess-1"
        ws = _CapturingWS()
        msg = {"session_id": "sess-1", "execution_path": "claude-code-cli", "config": {},
               "command_id": "cmd-1", "agent_slug": "test-agent"}
        with patch("satellite.sessions.cli_session.CLISession", side_effect=[first, second]):
            await sm.start_session(msg, ws)
            await sm.start_session(msg, ws)                 # the same id again: the first is replaced
        ws.sent.clear()
        await first.on_exit(0)
        assert ws.sent == []
        await sm.close_session({"session_id": "sess-1", "command_id": "cmd-2"}, ws)
        ws.sent.clear()
        await second.on_exit(0)
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_same_id_respawn_for_the_codex_engine(self, sat_config):
        sm = SessionManager(sat_config)
        stale = AsyncMock()
        stale.pid = 111
        sm.sessions["sess-2"] = stale
        replacement = AsyncMock()
        replacement.pid = 222
        replacement.thread_id = "thread-9"
        ws = AsyncMock()
        with patch(
            "satellite.sessions.codex_session.CodexSession",
            return_value=replacement,
        ):
            await sm.start_session({
                "session_id": "sess-2",
                "execution_path": "codex-cli",
                "config": {},
                "command_id": "cmd-2",
                "agent_slug": "test-agent",
            }, ws)
        stale.close.assert_awaited_once()
        replacement.start.assert_awaited_once()
        assert sm.sessions["sess-2"] is replacement
        # A session that captured its resume handle at start reports it —
        # under the frame type that is FROZEN wire.
        frames = [c.args[0] for c in ws.enqueue_send.await_args_list]
        assert {"type": "codex_thread_id", "session_id": "sess-2",
                "thread_id": "thread-9"} in frames

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refused", [True, False])
    async def test_the_ack_says_when_the_engine_refused_the_resume(self, sat_config, refused):
        sm = SessionManager(sat_config)
        started = AsyncMock()
        started.pid = 7
        started.thread_id = "thread-fresh"
        started.resume_refused = refused
        ws = AsyncMock()
        with patch("satellite.sessions.codex_session.CodexSession", return_value=started):
            await sm.start_session({
                "session_id": "sess-r", "execution_path": "codex-cli", "config": {},
                "command_id": "cmd-r", "agent_slug": "test-agent",
            }, ws)
        ack = ws.enqueue_send.await_args_list[0].args[0]
        assert ack["status"] == "ok"
        assert ack.get("resume_refused") is (True if refused else None)

    @pytest.mark.asyncio
    async def test_unknown_engine_is_an_ack_error_before_any_spawn(self, sat_config):
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        await sm.start_session({
            "session_id": "sess-x", "execution_path": "acme-cli", "config": {},
            "command_id": "cmd-x", "agent_slug": "test-agent",
        }, ws)
        ack = ws.enqueue_send.await_args_list[0].args[0]
        assert ack["status"] == "error" and "acme-cli" in ack["error"]
        assert "sess-x" not in sm.sessions


class TestFileSyncHandlers:
    @pytest.mark.asyncio
    async def test_file_push_creates_file(self, sat_config):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)

        import base64
        content = base64.b64encode(b"hello world").decode()
        msg = {
            "agent_slug": "test-agent",
            "path": "workspace/test.txt",
            "action": "write",
            "content_b64": content,
        }
        await sm.file_push(msg)

        target = sat_config.agents_dir / "test-agent" / "workspace" / "test.txt"
        assert target.exists()
        assert target.read_text() == "hello world"

    @pytest.mark.asyncio
    async def test_a_push_into_an_engine_home_outside_skills_is_refused(self, sat_config, caplog):
        # A platform before 1.9.0 still pushes the engine's own files there
        # (its sandbox-path wrappers among them): refused with a rejected
        # ack, said once per path; skills and a nested project config land.
        import base64
        import logging
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        ws = _CapturingWS()
        body = base64.b64encode(b"x").decode()

        def _msg(rel, cid):
            return {"agent_slug": "test-agent", "path": rel, "action": "write",
                    "content_b64": body, "command_id": cid}

        with caplog.at_level(logging.INFO, logger="satellite"):
            await sm.file_push(_msg("users/alice/.claude/bin/gh", "c1"), ws)
            await sm.file_push(_msg("users/alice/.claude/bin/gh", "c2"), ws)
        assert [a["status"] for a in ws.sent] == ["rejected", "rejected"]
        assert ws.sent[0]["error"] == "engine home path is host-local"
        assert not (sat_config.agents_dir / "test-agent" / "users" / "alice" / ".claude").exists()
        assert sum("under an engine home only a person's own folders sync" in r.getMessage()
                   for r in caplog.records) == 1
        await sm.file_push(_msg("users/alice/.claude/skills/x/SKILL.md", "c3"), ws)
        await sm.file_push(_msg("workspace/repo/.claude/commands/deploy.md", "c4"), ws)
        await sm.file_push(_msg("users/alice/.claude/commands/deploy.md", "c5"), ws)
        await sm.file_push(_msg("workspace/.codex/prompts/review.md", "c6"), ws)
        assert [a["status"] for a in ws.sent[2:]] == ["ok", "ok", "ok", "ok"]
        assert (sat_config.agents_dir / "test-agent" / "users" / "alice" / ".claude" / "skills"
                / "x" / "SKILL.md").read_bytes() == b"x"

    @pytest.mark.asyncio
    async def test_the_retention_frame_keeps_the_platforms_and_the_daemons_ids(self, sat_config):
        import os
        import time
        import uuid
        from types import SimpleNamespace
        sm = SessionManager(sat_config)
        home = sat_config.agents_dir / "a1" / "users" / "alice"
        d = home / ".claude" / "projects" / "-p"
        d.mkdir(parents=True)
        kept, live_sid, live_tid, gone, recent = (str(uuid.uuid4()) for _ in range(5))
        for ref in (kept, live_sid, live_tid, gone, recent):
            f = d / f"{ref}.jsonl"
            f.write_bytes(b"x" * 10)
            if ref != recent:
                then = time.time() - 10 * 86400
                os.utime(f, (then, then))
        # A live Codex session is known by its session id AND its thread.
        sm.sessions[live_sid] = SimpleNamespace(thread_id=live_tid)
        ws = _CapturingWS()
        # The platform's grace is clamped to an hour at least: a file ten
        # minutes old stays whatever the frame says.
        then = time.time() - 600
        os.utime(d / f"{recent}.jsonl", (then, then))
        await sm.session_files_retention(
            {"command_id": "c1", "keep": [kept], "min_age_s": 60, "dry_run": False}, ws)
        assert ws.sent[-1] == {
            "type": "ack", "command_id": "c1", "status": "ok", "error": "",
            "files": 4, "bytes": 40, "deleted": 1, "deleted_bytes": 10,
            "candidates": [f"a1/users/alice/.claude/projects/-p/{gone}.jsonl"],
        }
        assert sorted(p.stem for p in d.glob("*.jsonl")) == sorted([kept, live_sid, live_tid, recent])

    @pytest.mark.asyncio
    async def test_the_retention_frame_fails_closed(self, sat_config):
        """The one irreversible delete on the wire: a frame with no keep list
        or one that is not a list, and an empty list outside a dry run, are
        refused and delete nothing; a frame that names no grace gets the
        seven-day default, never the one-hour floor."""
        import os
        import time
        import uuid

        from satellite.host import session_retention
        sm = SessionManager(sat_config)
        d = sat_config.agents_dir / "a1" / "workspace" / ".claude" / "projects" / "-p"
        d.mkdir(parents=True)
        two_days, nine_days = str(uuid.uuid4()), str(uuid.uuid4())
        for ref, days in ((two_days, 2), (nine_days, 9)):
            f = d / f"{ref}.jsonl"
            f.write_bytes(b"x" * 10)
            then = time.time() - days * 86400
            os.utime(f, (then, then))
        ws = _CapturingWS()
        for cid, frame in (("c1", {}), ("c2", {"keep": None}), ("c3", {"keep": "abc"}),
                           ("c4", {"keep": []}), ("c5", {"keep": ["x"], "min_age_s": "soon"})):
            await sm.session_files_retention({"command_id": cid, **frame}, ws)
        assert [(a["status"], a["error"]) for a in ws.sent] == [
            ("error", "keep list missing"), ("error", "keep list missing"),
            ("error", "keep list missing"), ("error", "keep list empty"),
            ("error", "min_age_s is not a number"),
        ]
        assert sorted(p.stem for p in d.glob("*.jsonl")) == sorted([two_days, nine_days])
        # An empty list on a dry run only lists (the seven-day default grace
        # keeps the two-day-old file out of the list).
        await sm.session_files_retention({"command_id": "c6", "keep": [], "dry_run": True}, ws)
        assert ws.sent[-1]["status"] == "ok" and ws.sent[-1]["deleted"] == 1
        assert sorted(p.stem for p in d.glob("*.jsonl")) == sorted([two_days, nine_days])
        # No grace named: seven days, so the two-day-old file stays.
        assert session_retention.DEFAULT_MIN_AGE_S == 7 * 86400
        await sm.session_files_retention({"command_id": "c7", "keep": ["x"]}, ws)
        assert ws.sent[-1]["status"] == "ok" and ws.sent[-1]["deleted"] == 1
        assert [p.stem for p in d.glob("*.jsonl")] == [two_days]

    @pytest.mark.asyncio
    async def test_a_stale_push_acks_the_files_stat_and_a_committed_one_its_own(self, sat_config):
        import base64
        import os
        import time
        sm = SessionManager(sat_config)
        target = sat_config.agents_dir / "test-agent" / "workspace" / "run.log"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"busy")  # written now: live
        ws = _CapturingWS()
        msg = {
            "agent_slug": "test-agent", "path": "workspace/run.log", "action": "write",
            "content_b64": base64.b64encode(b"platform").decode(),
            "command_id": "c1", "expect": "live",
        }
        await sm.file_push(msg, ws)
        assert ws.sent[-1] == {
            "type": "ack", "command_id": "c1", "status": "stale", "error": "",
            "reason": "live", "size": 4, "mtime_ns": target.stat().st_mtime_ns,
        }
        assert target.read_bytes() == b"busy"
        then = time.time() - 60
        os.utime(target, (then, then))
        await sm.file_push({**msg, "command_id": "c2"}, ws)
        assert target.read_bytes() == b"platform"
        assert ws.sent[-1] == {
            "type": "ack", "command_id": "c2", "status": "ok", "error": "",
            "size": 8, "mtime_ns": target.stat().st_mtime_ns,
        }


class _DeadCLISession:
    execution_path = "claude-code-cli"
    is_alive = False


class _CapturingWS:
    def __init__(self):
        self.sent = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)

    async def enqueue_bulk(self, msg):
        self.sent.append(msg)
        return True


class TestCredentialsUpdate:
    """Rotation fan-out: the platform pushes a rewritten CLI credential file
    (the generic file sync excludes these — this push is the only channel)."""

    @pytest.mark.asyncio
    async def test_claude_rewrite(self, sat_config):
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        blob = {"claudeAiOauth": {"accessToken": "new", "refreshToken": ""}}
        await sm.credentials_update({
            "command_id": "c1", "agent_slug": "test-agent",
            "dir_relative": "users/alice/.claude", "kind": "claude",
            "content": blob,
        }, ws)
        assert ws.sent[0]["status"] == "ok"
        path = (sat_config.agents_dir / "test-agent" / "users" / "alice"
                / ".claude" / ".credentials.json")
        assert json.loads(path.read_text()) == blob

    @pytest.mark.asyncio
    async def test_codex_rewrite(self, sat_config):
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        auth = {"auth_mode": "chatgpt",
                "tokens": {"access_token": "new", "refresh_token": ""}}
        await sm.credentials_update({
            "command_id": "c2", "agent_slug": "test-agent",
            "dir_relative": "workspace/.codex", "kind": "codex",
            "content": auth,
        }, ws)
        assert ws.sent[0]["status"] == "ok"
        path = (sat_config.agents_dir / "test-agent" / "workspace"
                / ".codex" / "auth.json")
        assert json.loads(path.read_text()) == auth

    @pytest.mark.asyncio
    async def test_rejects_traversal_and_wrong_dir(self, sat_config):
        sm = SessionManager(sat_config)
        for bad in (
            {"agent_slug": "a", "dir_relative": "../../etc", "kind": "claude"},
            {"agent_slug": "a", "dir_relative": "users/u/.claude", "kind": "wat"},
            {"agent_slug": "a", "dir_relative": "users/u/.codex", "kind": "claude"},
            {"agent_slug": "../up", "dir_relative": "users/u/.claude", "kind": "claude"},
            {"agent_slug": "a", "dir_relative": "users/u/.claude", "kind": "claude",
             "content": "not-a-dict"},
        ):
            ws = _CapturingWS()
            msg = {"command_id": "cx", "content": {}, **bad}
            await sm.credentials_update(msg, ws)
            assert ws.sent[0]["status"] == "error", f"accepted bad target: {bad}"


class TestCredentialsUpdateHomes:
    """A session in a home of its own (``sessions/session_homes.py``) gets its
    rotated sign-in in that home; nothing reaches a write through a link
    (the plan's security rule 4)."""

    SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    BLOB = {"claudeAiOauth": {"accessToken": "new", "refreshToken": ""}}

    async def _push(self, sm, dir_relative, kind="claude", slug="test-agent"):
        ws = _CapturingWS()
        await sm.credentials_update({
            "command_id": "c", "agent_slug": slug, "dir_relative": dir_relative,
            "kind": kind, "content": self.BLOB}, ws)
        self.ack = ws.sent[0]
        return ws.sent[0]["status"]

    @pytest.mark.asyncio
    async def test_a_homes_sign_in_is_written_there(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions[self.SID] = object()
        home = sat_config.agents_dir / "test-agent" / "users" / "alice" / ".claude" / "own" / self.SID
        home.mkdir(parents=True)
        assert await self._push(sm, f"users/alice/.claude/own/{self.SID}") == "ok"
        assert "note" not in self.ack
        path = home / ".credentials.json"
        assert json.loads(path.read_text()) == self.BLOB
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not (home.parent.parent / ".credentials.json").exists()

    @pytest.mark.asyncio
    async def test_a_live_home_sessions_start_copy_follows_the_rotation(self, sat_config):
        sm = SessionManager(sat_config)
        home = sat_config.agents_dir / "test-agent" / "workspace" / ".claude" / "own" / self.SID

        class _Live:
            config = {"credentials_json": {"claudeAiOauth": {"accessToken": "old"}}}
            _claude_dir = home.parent.parent
            _engine_dir = home
        sm.sessions[self.SID] = _Live()
        home.mkdir(parents=True)
        assert await self._push(sm, f"workspace/.claude/own/{self.SID}") == "ok"
        assert sm.sessions[self.SID].config["credentials_json"] == self.BLOB

    @pytest.mark.asyncio
    async def test_a_home_no_live_session_of_this_machine_holds_takes_no_sign_in(self, sat_config):
        """A push that lands while the session's close removes its home, or
        after it, writes nothing: a token would stay on disk with no session
        to read it (the home's removal then fails on it)."""
        from satellite.sessions import close_hold
        sm = SessionManager(sat_config)
        home = sat_config.agents_dir / "test-agent" / "workspace" / ".claude" / "own" / self.SID
        home.mkdir(parents=True)
        assert await self._push(sm, f"workspace/.claude/own/{self.SID}") == "ok"
        assert self.ack["note"] == "session gone, not written"
        assert not (home / ".credentials.json").exists()
        sm.pty_sessions[self.SID] = object()
        async with close_hold.closing(self.SID):
            assert await self._push(sm, f"workspace/.claude/own/{self.SID}") == "ok"
            assert self.ack["note"] == "session gone, not written"
        assert not (home / ".credentials.json").exists()
        assert await self._push(sm, f"workspace/.claude/own/{self.SID}") == "ok"
        assert json.loads((home / ".credentials.json").read_text()) == self.BLOB

    @pytest.mark.asyncio
    async def test_a_codex_home_no_live_session_of_this_machine_holds_takes_no_sign_in(self, sat_config):
        """The same for a Codex home: its push names the Codex kind and folder."""
        from satellite.sessions import close_hold
        sm = SessionManager(sat_config)
        rel = f"workspace/.codex/own/{self.SID}"
        home = sat_config.agents_dir / "test-agent" / "workspace" / ".codex" / "own" / self.SID
        home.mkdir(parents=True)
        assert await self._push(sm, rel, kind="codex") == "ok"
        assert self.ack["note"] == "session gone, not written"
        assert not (home / "auth.json").exists()
        sm.pty_sessions[self.SID] = object()
        async with close_hold.closing(self.SID):
            assert await self._push(sm, rel, kind="codex") == "ok"
            assert self.ack["note"] == "session gone, not written"
        assert not (home / "auth.json").exists()
        assert await self._push(sm, rel, kind="codex") == "ok"
        assert "note" not in self.ack
        assert (home / "auth.json").is_file()

    @pytest.mark.asyncio
    async def test_a_person_named_own_is_a_scope_not_a_home(self, sat_config):
        """``users/own/.claude`` is the scope of a person named ``own``: its
        sign-in is written, its folder made as any scope's."""
        sm = SessionManager(sat_config)
        assert await self._push(sm, "users/own/.claude") == "ok"
        assert "note" not in self.ack
        assert (sat_config.agents_dir / "test-agent" / "users" / "own" / ".claude").is_dir()

    @pytest.mark.asyncio
    async def test_a_push_for_a_gone_home_makes_no_folder_at_all(self, sat_config):
        """A home in a scope whose engine folder is not there: nothing is
        made, not the scope's folder either."""
        sm = SessionManager(sat_config)
        assert await self._push(sm, f"users/bob/.claude/own/{self.SID}") == "ok"
        assert self.ack["note"] == "session gone, not written"
        assert not (sat_config.agents_dir / "test-agent" / "users" / "bob").exists()

    @pytest.mark.asyncio
    async def test_a_renewal_after_the_close_makes_no_home(self, sat_config):
        """The close removed the home before the platform let go of its
        target: the renewal writes nothing, makes no folder, and says so."""
        sm = SessionManager(sat_config)
        store = sat_config.agents_dir / "test-agent" / "workspace" / ".claude"
        assert await self._push(sm, f"workspace/.claude/own/{self.SID}") == "ok"
        assert self.ack["note"] == "session gone, not written"
        assert not (store / "own" / self.SID).exists()
        assert not (store / ".credentials.json").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [
        "users/alice/.claude/own/not an id",
        "users/alice/.claude/own/short",
        f"users/alice/.claude/own/{SID}/deeper",
        f"users/alice/.claude/elsewhere/{SID}",
        f"users/alice/.codex/own/{SID}",
    ])
    async def test_other_shapes_are_refused(self, sat_config, bad):
        sm = SessionManager(sat_config)
        assert await self._push(sm, bad) == "error"

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="links stand in for junctions on POSIX")
    @pytest.mark.parametrize("linked", ["own", "home", "store"])
    async def test_a_link_on_the_path_is_refused(self, sat_config, tmp_path, linked):
        sm = SessionManager(sat_config)
        sm.sessions[self.SID] = object()
        store = sat_config.agents_dir / "test-agent" / "workspace" / ".claude"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        if linked == "store":
            store.parent.mkdir(parents=True)
            os.symlink(elsewhere, store)
        else:
            (store / "own").mkdir(parents=True)
            if linked == "own":
                os.rmdir(store / "own")
                os.symlink(elsewhere, store / "own")
            else:
                os.symlink(elsewhere, store / "own" / self.SID)
        assert await self._push(sm, f"workspace/.claude/own/{self.SID}") == "error"
        assert list(elsewhere.rglob("*")) == []

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="links stand in for junctions on POSIX")
    async def test_a_link_at_the_files_name_is_replaced_never_followed(self, sat_config, tmp_path):
        sm = SessionManager(sat_config)
        store = sat_config.agents_dir / "test-agent" / "workspace" / ".claude"
        store.mkdir(parents=True)
        victim = tmp_path / "victim.json"
        victim.write_text("untouched")
        os.symlink(victim, store / ".credentials.json")
        assert await self._push(sm, "workspace/.claude") == "ok"
        assert victim.read_text() == "untouched"
        assert not (store / ".credentials.json").is_symlink()

    def test_the_satellite_reports_that_it_makes_homes(self, sat_config):
        caps = SessionManager(sat_config).detect_capabilities()
        assert caps["session_homes"] is True
        # Each engine folder it makes homes in (a platform reads Claude Code's
        # alone from a report of ``session_homes`` without this list).
        assert caps["session_home_dirs"] == [".claude", ".codex"]


class TestSendMessageDeadCLI:
    """A dead persistent CLI must come back as an ACK error
    (not a session_event) so the proxy's cli_dead → auto-resume path fires."""

    @pytest.mark.asyncio
    async def test_dead_cli_acks_error(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["sess-x"] = _DeadCLISession()
        ws = _CapturingWS()
        await sm.send_message({"session_id": "sess-x", "command_id": "cmd-1"}, ws)
        ack = ws.sent[0]
        assert ack["type"] == "ack"
        assert ack["status"] == "error"
        assert "CLI process not running" in ack["error"]


class _FakeCLISession:
    """CLI session stub that streams a scripted event list, tags nothing
    itself (the manager tags), and reports file changes as none."""
    execution_path = "claude-code-cli"
    is_alive = True
    agent_slug = "agent-1"

    def __init__(self, events):
        self._events = events
        self.config = {}
        self.stop_turn_event = asyncio.Event()

    async def send_message(self, message, inject_time=False):
        for ev in self._events:
            yield dict(ev)

    async def run_turn(self, message, *, inject_time=False, forward):
        # Mirrors CLISession.run_turn: the manager hands us its forwarder.
        async for ev in self.send_message(message, inject_time=inject_time):
            await forward(ev)

    def detect_file_changes(self):
        return []


class TestModeCTurnRetention:
    """The satellite retains the current CLI turn's forwarded events + a
    _seq/_command_id tag, reports live sessions post-auth, and replays the
    buffer on resume_session_stream — the primitives Mode C re-adopt uses."""

    @pytest.mark.asyncio
    async def test_send_message_buffers_and_tags(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _FakeCLISession([
            {"type": "assistant"}, {"type": "result", "subtype": "success"},
        ])
        ws = _CapturingWS()
        await sm.send_message({"session_id": "s1", "command_id": "cmd-1",
                               "message": "hi"}, ws)
        evs = [m["event"] for m in ws.sent if m["type"] == "session_event"]
        assert all(e["_command_id"] == "cmd-1" for e in evs)
        assert [e["_seq"] for e in evs] == [1, 2]
        # Buffer holds the turn + a finished sentinel; state marked inactive.
        buf = list(sm.turn_buffers["s1"])
        assert buf[-1]["type"] == "_turn_sentinel"
        assert sm.turn_state["s1"]["active"] is False

    @pytest.mark.asyncio
    async def test_the_starts_data_reaches_the_session_only_when_sent(self, sat_config):
        """The proxy's ``start_context`` on a send goes to the session's turn
        (Claude Code writes it as its own message); a send without it hands
        the session no such argument, as every engine's turn takes."""
        seen: list[dict] = []

        class _Recording(_FakeCLISession):
            async def run_turn(self, message, *, inject_time=False, forward, **kw):
                seen.append(kw)
                await super().run_turn(message, inject_time=inject_time, forward=forward)

        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _Recording([{"type": "result", "subtype": "success"}])
        await sm.send_message({"session_id": "s1", "command_id": "cmd-1", "message": "hi",
                               "start_context": "<start-context>X</start-context>"}, _CapturingWS())
        await sm.send_message({"session_id": "s1", "command_id": "cmd-2", "message": "hi"}, _CapturingWS())
        assert seen == [{"start_context": "<start-context>X</start-context>"}, {}]

    @pytest.mark.asyncio
    async def test_turn_ended_precedes_file_scan_frames(self, sat_config):
        # turn_ended must be sent BEFORE the end-of-turn file scan: on a big
        # workspace the scan outlives the proxy's post-stop drain budget, and
        # a marker sent after it lands as a stale leftover — the turn
        # accounting then goes permanently one turn behind (every response
        # persists one prompt late; live repro 2026-07-21).
        class _ScanningSession(_FakeCLISession):
            def detect_file_changes(self):
                return [{"path": "notes.md", "content_b64": "eA=="}]

        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _ScanningSession([
            {"type": "assistant"}, {"type": "result", "subtype": "success"},
        ])
        ws = _CapturingWS()
        await sm.send_message({"session_id": "s1", "command_id": "cmd-1",
                               "message": "hi"}, ws)
        types = [m["type"] for m in ws.sent]
        assert "turn_ended" in types and "file_changed" in types
        assert types.index("turn_ended") < types.index("file_changed")

    @pytest.mark.asyncio
    async def test_the_turn_end_scan_runs_off_the_loop(self, sat_config):
        """A turn's end scans the agent's folder in a worker thread: a large
        folder never holds the socket's loop."""
        import threading
        scanned: list = []

        class _ScanningSession(_FakeCLISession):
            def detect_file_changes(self):
                scanned.append(threading.get_ident())
                return []

        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _ScanningSession([{"type": "result", "subtype": "success"}])
        await sm.send_message({"session_id": "s1", "command_id": "cmd-1", "message": "hi"}, _CapturingWS())
        assert scanned and scanned[0] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_the_guards_end_is_told_by_the_turn_it_ended_alone(self, sat_config):
        """Ended inside a turn: that turn's end says ``memory``. A Codex
        session ended between turns re-warms in place, and its next turn,
        which answered, says nothing of it (mutation: a flag never cleared)."""
        sm = SessionManager(sat_config)

        class _Ended(_FakeCLISession):
            async def run_turn(self, message, *, inject_time=False, forward):
                sm.mark_ended_for_memory("s1")
                await forward({"type": "result", "subtype": "error_during_execution"})

        sm.sessions["s1"] = _Ended([])
        ws = _CapturingWS()
        await sm.send_message({"session_id": "s1", "command_id": "cmd-1", "message": "hi"}, ws)
        ended = [m for m in ws.sent if m["type"] == "turn_ended"]
        assert [m.get("reason") for m in ended] == ["memory"]

        codex = _FakeCLISession([{"type": "result", "subtype": "success"}])
        codex.execution_path = "codex-cli"
        sm.sessions["s2"] = codex
        sm.mark_ended_for_memory("s2")          # between turns: the engine re-warms at the next
        ws = _CapturingWS()
        await sm.send_message({"session_id": "s2", "command_id": "cmd-2", "message": "hi"}, ws)
        await sm.send_message({"session_id": "s1", "command_id": "cmd-3", "message": "again"}, ws)
        ended = [m for m in ws.sent if m["type"] == "turn_ended"]
        assert [m.get("reason") for m in ended] == [None, "memory"]

    @pytest.mark.asyncio
    async def test_a_turns_end_says_why_the_session_ended_it(self, sat_config):
        """A session that ended its turn itself says why, once (a Codex
        daemon dropped for an interrupt it did not take): the reason rides
        that turn's end and no later one. The memory guard's end comes first."""
        sm = SessionManager(sat_config)

        class _Dropped(_FakeCLISession):
            ends = [{"reason": "interrupted"}, {}, {"reason": "interrupted"}]

            def take_turn_end(self):
                return self.ends.pop(0)

            async def run_turn(self, message, *, inject_time=False, forward):
                if message == "guarded":
                    sm.mark_ended_for_memory("s1")

        sm.sessions["s1"] = _Dropped([])
        ws = _CapturingWS()
        for n, message in enumerate(("hi", "hi", "guarded")):
            await sm.send_message({"session_id": "s1", "command_id": f"cmd-{n}", "message": message}, ws)
        ended = [m for m in ws.sent if m["type"] == "turn_ended"]
        assert [m.get("reason") for m in ended] == ["interrupted", None, "memory"]
        assert all(set(m) <= {"type", "session_id", "command_id", "reason"} for m in ended)

    def test_sessions_alive_report(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _FakeCLISession([])
        sm.turn_state["s1"] = {"seq": 3, "active": True, "command_id": "c9"}
        sm.sessions["s2"] = _DeadCLISession()  # not alive → excluded
        report = sm.headless_sessions_alive()
        ids = {r["session_id"]: r for r in report}
        assert "s1" in ids and "s2" not in ids
        assert ids["s1"]["turn_active"] is True
        assert ids["s1"]["command_id"] == "c9"

    @pytest.mark.asyncio
    async def test_resume_replay_order(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _FakeCLISession([])
        # Simulate a FINISHED buffered turn.
        from collections import deque
        sm.turn_state["s1"] = {"seq": 3, "active": False,
                               "command_id": "cmd-1", "start_seq": 1}
        sm.turn_buffers["s1"] = deque([
            {"type": "assistant", "_seq": 1, "_command_id": "cmd-1"},
            {"type": "result", "_seq": 2, "_command_id": "cmd-1"},
            {"type": "_turn_sentinel", "_seq": 3, "command_id": "cmd-1"},
        ])
        ws = _CapturingWS()
        await sm.resume_session_stream({"session_id": "s1"}, ws)
        types = [
            (m.get("type"), m.get("event", {}).get("type"))
            for m in ws.sent
        ]
        # begin marker → replayed events → trailing turn_ended.
        assert types[0] == ("session_event", "_resume_replay_begin")
        assert ("session_event", "assistant") in types
        assert ("session_event", "result") in types
        assert types[-1][0] == "turn_ended"
        # The sentinel itself is NOT forwarded as a session_event.
        assert ("session_event", "_turn_sentinel") not in types


class TestReplayPauseRelease:
    """A forwarder parked while a replay is enqueued goes on when the replay
    ends, and when the session closes during the replay (the event is set,
    never only dropped)."""

    @pytest.mark.asyncio
    async def test_the_replay_end_releases_a_parked_forwarder(self, sat_config):
        sm = SessionManager(sat_config)
        sm.forward_paused["s1"] = asyncio.Event()
        waiter = asyncio.create_task(sm._wait_forward_unpaused("s1"))
        await asyncio.sleep(0)
        assert not waiter.done()
        sm._release_forward("s1")
        await asyncio.wait_for(waiter, 1)
        assert "s1" not in sm.forward_paused

    @pytest.mark.asyncio
    async def test_a_close_during_the_replay_releases_a_parked_forwarder(self, sat_config):
        sm = SessionManager(sat_config)
        sm.forward_paused["s1"] = asyncio.Event()
        waiter = asyncio.create_task(sm._wait_forward_unpaused("s1"))
        await asyncio.sleep(0)
        await sm.close_session({"session_id": "s1", "command_id": "c1"}, _CapturingWS())
        await asyncio.wait_for(waiter, 1)
        # The replay's own end then finds nothing left to release.
        sm._release_forward("s1")
        assert "s1" not in sm.forward_paused


class _HeldCodexSession:
    """A headless session of an engine without turn replay, as the report
    reads it."""
    execution_path = "codex-cli"
    agent_slug = "agent-c"

    def __init__(self, *, alive=True, turn_active=False, handle="th-1"):
        self.is_alive = alive
        self.turn_active = turn_active
        self.resume_handle = handle
        self.mcp_server_names = ["file-tools", "memory"]
        self.config = {"model": "gpt-6.1-sol", "use_native_permissions": False}
        self.incarnation = "boot-7"


class TestOtherSessionsHeld:
    """Every headless session the satellite holds that the turn-replay list
    does not name: the platform takes an idle one back or closes it, so none
    stays counted against the machine after a platform restart."""

    def test_lists_engines_without_replay_and_dead_replay_sessions(self, sat_config):
        from satellite.sessions import alive_report
        sm = SessionManager(sat_config)
        sm.sessions["c-idle"] = _HeldCodexSession()
        sm.sessions["c-turn"] = _HeldCodexSession(turn_active=True)
        sm.sessions["c-dead"] = _HeldCodexSession(alive=False, handle="")
        sm.sessions["cli-live"] = _FakeCLISession([])
        sm.sessions["cli-dead"] = _DeadCLISession()
        report = {e["session_id"]: e for e in alive_report.other_sessions_held(sm)}
        assert set(report) == {"c-idle", "c-turn", "c-dead", "cli-dead"}
        assert report["c-idle"] == {
            "session_id": "c-idle", "execution_path": "codex-cli", "agent_slug": "agent-c",
            "alive": True, "turn_active": False, "incarnation": "boot-7",
            "resume_handle": "th-1", "model": "gpt-6.1-sol",
            "mcp_servers": ["file-tools", "memory"], "use_native_permissions": False,
        }
        assert report["c-turn"]["turn_active"] is True
        assert report["c-dead"]["alive"] is False
        assert report["cli-dead"]["alive"] is False
        assert report["cli-dead"]["resume_handle"] == ""

    def test_the_frame_keeps_the_replay_list_when_the_new_one_fails(self, sat_config):
        from satellite.sessions import alive_report
        sm = SessionManager(sat_config)
        sm.sessions["cli-live"] = _FakeCLISession([])
        with patch.object(alive_report, "other_sessions_held", side_effect=RuntimeError("x")):
            frame = alive_report.sessions_alive_frame(sm)
        assert frame["type"] == "sessions_alive"
        assert [e["session_id"] for e in frame["sessions"]] == ["cli-live"]
        assert "other_sessions" not in frame

    def test_no_frame_when_the_replay_list_fails(self, sat_config):
        from satellite.sessions import alive_report
        sm = SessionManager(sat_config)
        with patch.object(sm, "headless_sessions_alive", side_effect=RuntimeError("x")):
            assert alive_report.sessions_alive_frame(sm) is None

    def test_a_complete_frame_carries_both_lists(self, sat_config):
        from satellite.sessions import alive_report
        sm = SessionManager(sat_config)
        sm.sessions["c-idle"] = _HeldCodexSession()
        frame = alive_report.sessions_alive_frame(sm)
        assert frame["sessions"] == []
        assert [e["session_id"] for e in frame["other_sessions"]] == ["c-idle"]

    def test_every_session_reports_its_incarnation(self, sat_config):
        sm = SessionManager(sat_config)
        live = _FakeCLISession([])
        live.incarnation = "boot-1"
        sm.sessions["s1"] = live
        assert sm.headless_sessions_alive()[0]["incarnation"] == "boot-1"

    def test_incarnations_are_unique_and_carry_the_boot(self):
        from satellite.sessions import alive_report
        a, b = alive_report.next_incarnation(), alive_report.next_incarnation()
        assert a != b
        assert a.split("-")[0] == b.split("-")[0]


class TestCloseByIncarnation:
    @pytest.mark.asyncio
    async def test_a_close_for_an_earlier_object_leaves_the_live_one(self, sat_config):
        sm = SessionManager(sat_config)
        live = AsyncMock()
        live.incarnation = "boot-2"
        sm.sessions["s1"] = live
        ws = _CapturingWS()
        await sm.close_session({"session_id": "s1", "command_id": "c1",
                                "incarnation": "boot-1"}, ws)
        live.close.assert_not_awaited()
        assert sm.sessions["s1"] is live
        assert ws.sent[0]["status"] == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("incarnation", ["boot-2", None])
    async def test_a_matching_or_unnamed_close_ends_it(self, sat_config, incarnation):
        sm = SessionManager(sat_config)
        live = AsyncMock()
        live.incarnation = "boot-2"
        sm.sessions["s1"] = live
        ws = _CapturingWS()
        msg = {"session_id": "s1", "command_id": "c1"}
        if incarnation:
            msg["incarnation"] = incarnation
        await sm.close_session(msg, ws)
        live.close.assert_awaited_once()
        assert "s1" not in sm.sessions

    @pytest.mark.asyncio
    async def test_a_started_session_gets_an_incarnation(self, sat_config):
        sm = SessionManager(sat_config)
        replacement = AsyncMock()
        replacement.pid = 5
        replacement.thread_id = ""
        with patch("satellite.sessions.codex_session.CodexSession", return_value=replacement):
            await sm.start_session({
                "session_id": "s-new", "execution_path": "codex-cli", "config": {},
                "command_id": "cmd-n", "agent_slug": "test-agent",
            }, AsyncMock())
        assert isinstance(sm.sessions["s-new"].incarnation, str)
        assert sm.sessions["s-new"].incarnation


class TestCheckSessionProcess:
    """The proxy's stall-reap probes real process liveness before killing a
    turn that went event-silent — a live CLI with a quiet stream is a network
    stall, not a death."""

    @pytest.mark.asyncio
    async def test_live_session_reports_alive(self, sat_config):
        sm = SessionManager(sat_config)

        class _LiveCLI:
            execution_path = "claude-code-cli"
            is_alive = True
        sm.sessions["sess-a"] = _LiveCLI()
        ws = _CapturingWS()
        await sm.check_session_process(
            {"session_id": "sess-a", "command_id": "cmd-p1"}, ws)
        ack = ws.sent[0]
        assert ack["status"] == "ok" and ack["alive"] is True

    @pytest.mark.asyncio
    async def test_dead_and_missing_sessions_report_dead(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["sess-d"] = _DeadCLISession()
        ws = _CapturingWS()
        await sm.check_session_process(
            {"session_id": "sess-d", "command_id": "cmd-p2"}, ws)
        await sm.check_session_process(
            {"session_id": "sess-gone", "command_id": "cmd-p3"}, ws)
        assert ws.sent[0]["alive"] is False
        assert ws.sent[1]["alive"] is False


class TestCheckSessionResumable:
    """Verifies the new check_session_resumable RPC stats the CLI session
    JSONL the same way the proxy's local layer does, so stop+resume on a
    remote satellite doesn't silently lose chat memory (the file lives on
    the satellite — the proxy can't stat it locally).
    """

    @pytest.fixture
    def fake_ws(self):
        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def enqueue_send(self, msg):
                self.sent.append(msg)
        return _FakeWS()

    def _write_session_file(
        self, agents_dir, agent_slug, username, session_id, content,
    ):
        claude_dir = (
            agents_dir / agent_slug / "users" / username / ".claude"
        )
        # The project-hash subdir name varies — any name works for the test.
        project_dir = claude_dir / "projects" / "-some-project-hash"
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_id}.jsonl").write_text(
            content, encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_returns_true_when_session_file_has_user_message(
        self, sat_config, fake_ws,
    ):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        self._write_session_file(
            sat_config.agents_dir, "agent-1", "alice", "sess-X",
            '{"type":"user","content":"hi"}\n',
        )

        await sm.check_session_resumable({
            "command_id": "cmd-1",
            "session_id": "sess-X",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert len(fake_ws.sent) == 1
        ack = fake_ws.sent[0]
        assert ack["type"] == "ack"
        assert ack["command_id"] == "cmd-1"
        assert ack["status"] == "ok"
        assert ack["resumable"] is True

    @pytest.mark.asyncio
    async def test_returns_false_when_session_file_empty(
        self, sat_config, fake_ws,
    ):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        self._write_session_file(
            sat_config.agents_dir, "agent-1", "alice", "sess-X",
            "",  # no user message
        )

        await sm.check_session_resumable({
            "command_id": "cmd-2",
            "session_id": "sess-X",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is False
        assert fake_ws.sent[0]["reason"] == "empty"

    @pytest.mark.asyncio
    async def test_returns_false_when_file_missing(self, sat_config, fake_ws):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)

        await sm.check_session_resumable({
            "command_id": "cmd-3",
            "session_id": "sess-Y",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is False
        assert fake_ws.sent[0]["reason"] == "missing"

    @pytest.mark.asyncio
    async def test_a_user_line_deep_in_a_long_file_is_read_without_the_whole_file(
        self, sat_config, fake_ws, monkeypatch,
    ):
        """The probe reads line by line in a thread and stops at the first
        user line: a whole-file read would hold a transcript of any size
        on the loop (the 2026-10-10 stall read a 27 MB string there)."""
        from pathlib import Path
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        filler = '{"type":"assistant","content":"x"}\n' * 5000
        self._write_session_file(
            sat_config.agents_dir, "agent-1", "alice", "sess-L",
            filler + '{"type": "user","content":"hi"}\n' + filler,
        )

        def _refused(*a, **k):
            raise AssertionError("the probe read the whole file")

        monkeypatch.setattr(Path, "read_text", _refused)
        await sm.check_session_resumable({
            "command_id": "cmd-6",
            "session_id": "sess-L",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)
        assert fake_ws.sent[0]["resumable"] is True
        assert "reason" not in fake_ws.sent[0]

    @pytest.mark.asyncio
    async def test_a_name_nobody_can_load_is_a_definite_no(
        self, sat_config, fake_ws,
    ):
        """A directory planted at the transcript's name: the resume could
        not load it either, so the answer is final (never a stuck chat)."""
        sm = SessionManager(sat_config)
        project_dir = (sat_config.agents_dir / "agent-1" / "users" / "alice"
                       / ".claude" / "projects" / "-h")
        (project_dir / "sess-D.jsonl").mkdir(parents=True)

        await sm.check_session_resumable({
            "command_id": "cmd-7",
            "session_id": "sess-D",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)
        ack = fake_ws.sent[0]
        assert ack["status"] == "ok"
        assert ack["resumable"] is False
        assert ack["reason"] == "unreadable"

    @pytest.mark.asyncio
    async def test_a_read_the_machine_could_not_finish_is_an_error_ack(
        self, sat_config, fake_ws, monkeypatch,
    ):
        """Memory or I/O trouble is the machine's moment, not the file's:
        the platform reads the error ack as undecided and asks again."""
        import errno

        from satellite.sessions import session_manager as smod
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)

        def _enomem(*a):
            raise OSError(errno.ENOMEM, "Cannot allocate memory")

        monkeypatch.setattr(smod, "_probe_session_file", _enomem)
        await sm.check_session_resumable({
            "command_id": "cmd-8",
            "session_id": "sess-E",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)
        ack = fake_ws.sent[0]
        assert ack["status"] == "error"
        assert "resumable" not in ack
        assert "Cannot allocate memory" in ack["error"]

    @pytest.mark.asyncio
    async def test_returns_false_when_agent_dir_missing(
        self, sat_config, fake_ws,
    ):
        """Pristine satellite with no agents_dir at all."""
        sm = SessionManager(sat_config)
        # Don't create agents_dir.

        await sm.check_session_resumable({
            "command_id": "cmd-4",
            "session_id": "sess-Z",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is False

    @pytest.mark.asyncio
    async def test_uses_workspace_claude_dir_without_username(
        self, sat_config, fake_ws,
    ):
        """Internal / agent-scoped sessions have no username — the JSONL
        lives under <slug>/workspace/.claude/ instead of users/<u>/.claude/.
        """
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        claude_dir = (
            sat_config.agents_dir / "agent-1" / "workspace" / ".claude"
        )
        project_dir = claude_dir / "projects" / "-h"
        project_dir.mkdir(parents=True)
        (project_dir / "sess-W.jsonl").write_text(
            '{"type": "user","content":"hi"}\n', encoding="utf-8",
        )

        await sm.check_session_resumable({
            "command_id": "cmd-5",
            "session_id": "sess-W",
            "agent_slug": "agent-1",
            "username": "",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is True


class TestResumeProbeBeneathTheAgentFolder:
    """The probe lists ``projects`` and opens the transcript beneath the
    agent's folder: a linked project folder is not searched; a link or a
    special file at the transcript's name is a definite no; a ``projects``
    folder past its cap is an error the platform asks again on; a record
    over 64 MiB is read past."""

    @staticmethod
    async def _probe(sm, sid="sess-P"):
        class _WS:
            sent: list = []

            async def enqueue_send(self, msg):
                self.sent.append(msg)
        ws = _WS()
        ws.sent = []
        await sm.check_session_resumable({"command_id": "c", "session_id": sid,
                                          "agent_slug": "agent-1", "username": "alice"}, ws)
        return ws.sent[0]

    @staticmethod
    def _projects(sat_config):
        d = sat_config.agents_dir / "agent-1" / "users" / "alice" / ".claude" / "projects"
        d.mkdir(parents=True)
        return d

    @pytest.mark.asyncio
    async def test_a_linked_project_folder_is_not_searched(self, sat_config, tmp_path):
        projects = self._projects(sat_config)
        stand_in = tmp_path / "outside"
        stand_in.mkdir()
        (stand_in / "sess-P.jsonl").write_text('{"type":"user"}\n')
        (projects / "-linked").symlink_to(stand_in, target_is_directory=True)
        ack = await self._probe(SessionManager(sat_config))
        assert (ack["status"], ack["resumable"], ack["reason"]) == ("ok", False, "missing")

    @pytest.mark.asyncio
    async def test_a_link_or_a_pipe_at_the_name_is_unreadable(self, sat_config, tmp_path):
        projects = self._projects(sat_config)
        (projects / "-p").mkdir()
        stand_in = tmp_path / "outside.jsonl"
        stand_in.write_text('{"type":"user"}\n')
        (projects / "-p" / "sess-P.jsonl").symlink_to(stand_in)
        ack = await self._probe(SessionManager(sat_config))
        assert (ack["status"], ack["resumable"], ack["reason"]) == ("ok", False, "unreadable")
        (projects / "-p" / "sess-P.jsonl").unlink()
        os.mkfifo(projects / "-p" / "sess-P.jsonl")
        # A writer holds the pipe with a line in it, so a read by name would
        # not wait: the answer is about the kind of the name, not its bytes.
        held = os.open(projects / "-p" / "sess-P.jsonl", os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(held, b'{"type":"user"}\n')
            ack = await self._probe(SessionManager(sat_config))
        finally:
            os.close(held)
        assert (ack["status"], ack["resumable"], ack["reason"]) == ("ok", False, "unreadable")

    @pytest.mark.asyncio
    async def test_a_linked_projects_folder_is_unreadable(self, sat_config, tmp_path):
        claude = self._projects(sat_config).parent
        (claude / "projects").rmdir()
        stand_in = tmp_path / "outside" / "-p"
        stand_in.mkdir(parents=True)
        (stand_in / "sess-P.jsonl").write_text('{"type":"user"}\n')
        (claude / "projects").symlink_to(stand_in.parent, target_is_directory=True)
        ack = await self._probe(SessionManager(sat_config))
        assert (ack["status"], ack["resumable"], ack["reason"]) == ("ok", False, "unreadable")

    @pytest.mark.asyncio
    async def test_a_projects_folder_past_its_cap_could_not_tell(self, sat_config):
        projects = self._projects(sat_config)
        for i in range(10_001):
            (projects / f"-{i}").mkdir()
        (projects / "-0" / "sess-P.jsonl").write_text('{"type":"user"}\n')
        ack = await self._probe(SessionManager(sat_config))
        assert ack["status"] == "error" and "resumable" not in ack

    @pytest.mark.asyncio
    async def test_a_record_over_64_mib_is_read_past(self, sat_config):
        projects = self._projects(sat_config)
        (projects / "-p").mkdir()
        (projects / "-p" / "sess-P.jsonl").write_bytes(
            b'{"type":"user","content":"' + b"x" * (64 * 1024 * 1024) + b'"}\n{"type":"assistant"}\n')
        ack = await self._probe(SessionManager(sat_config))
        assert (ack["status"], ack["resumable"], ack["reason"]) == ("ok", False, "empty")


class TestFixWindowsExePathsAfterSwap:
    """Verifies pip/distlib .exe wrappers get their embedded python.exe path
    rewritten after the ``<X>.new/`` → ``<X>/`` rename on Windows.

    The bug: pip on Windows builds entry-point .exe wrappers with structure
    [launcher PE][shebang ``#!<python_exe>\\n``][zip data]. The shebang
    embeds an absolute path to ``<X>.new/venv/Scripts/python.exe``, which
    is deleted by the swap — every spawn fails with WinError 2. Fix
    rewrites the embedded path in place with trailing-space padding so
    the zip's byte offsets stay stable.
    """

    def _build_fake_exe(self, python_exe_path: str) -> bytes:
        """Fake distlib launcher: PE-ish header, shebang, then zip data."""
        launcher_blob = b"MZ\x90\x00" + b"\x00" * 60 + b"PE\x00\x00"
        shebang = f"#!{python_exe_path}\n".encode()
        # Real zip data not required for the unit test — any bytes after
        # the shebang stand in for the appended zip payload.
        zip_payload = b"PK\x03\x04zipped-script-bytes"
        return launcher_blob + shebang + zip_payload

    def test_rewrites_embedded_python_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(satcfg, "HOST", satcfg.ROWS[satcfg.WINDOWS])
        from satellite.sessions.mcp_install_support import (
            _fix_windows_exe_paths_after_swap,
        )

        old_root = tmp_path / "workspace-mcp.new"
        new_root = tmp_path / "workspace-mcp"
        old_python = str(old_root / "venv" / "Scripts" / "python.exe")
        new_python = str(new_root / "venv" / "Scripts" / "python.exe")

        # Simulate the post-swap state: new_root exists with the .exe
        # wrappers that pip baked when the dir was still <X>.new.
        scripts_dir = new_root / "venv" / "Scripts"
        scripts_dir.mkdir(parents=True)
        wrapper = scripts_dir / "workspace-mcp.exe"
        original_size = 0
        wrapper.write_bytes(self._build_fake_exe(old_python))
        original_size = wrapper.stat().st_size

        _fix_windows_exe_paths_after_swap(old_root, new_root)

        rewritten = wrapper.read_bytes()
        # Old path is gone.
        assert old_python.encode("utf-8") not in rewritten
        # New path is present.
        assert new_python.encode("utf-8") in rewritten
        # Byte length preserved (zip-offset stability).
        assert len(rewritten) == original_size
        # Zip payload still intact (the launcher reads it from a fixed offset).
        assert b"PK\x03\x04zipped-script-bytes" in rewritten

    def test_noop_on_non_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(satcfg, "HOST", satcfg.ROWS[satcfg.LINUX])
        from satellite.sessions.mcp_install_support import (
            _fix_windows_exe_paths_after_swap,
        )

        old_root = tmp_path / "workspace-mcp.new"
        new_root = tmp_path / "workspace-mcp"
        old_python = str(old_root / "venv" / "Scripts" / "python.exe")
        scripts_dir = new_root / "venv" / "Scripts"
        scripts_dir.mkdir(parents=True)
        wrapper = scripts_dir / "workspace-mcp.exe"
        wrapper.write_bytes(self._build_fake_exe(old_python))

        _fix_windows_exe_paths_after_swap(old_root, new_root)
        # On non-Windows, the function returns early — content untouched.
        assert old_python.encode("utf-8") in wrapper.read_bytes()

    def test_noop_when_no_scripts_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(satcfg, "HOST", satcfg.ROWS[satcfg.WINDOWS])
        from satellite.sessions.mcp_install_support import (
            _fix_windows_exe_paths_after_swap,
        )

        old_root = tmp_path / "workspace-mcp.new"
        new_root = tmp_path / "workspace-mcp"
        new_root.mkdir()  # No venv/Scripts/ subdir — node MCP, source-only, etc.
        # Should return without error.
        _fix_windows_exe_paths_after_swap(old_root, new_root)


class TestResolveWarmupCommand:
    """Unit-tests the manifest stdio command/args → argv resolution used by
    the pre-warm boot check. Mirrors resolve_server_config's rules
    but resolves venv binaries via the satellite's own layout (venv_exe), so
    the assertions hold on Linux/macOS (venv/bin) and Windows (venv/Scripts/
    *.exe) alike."""

    def test_python_with_server_py(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "display-mcp"
        cmd = _resolve_warmup_command(root, "venv/bin/python", ["display_server.py"])
        assert cmd == [
            str(venv_exe(root / "venv", "python")),
            str(root / "display_server.py"),
        ]

    def test_console_script_no_args(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "google-maps"
        cmd = _resolve_warmup_command(root, "venv/bin/google-maps-mcp-server", [])
        assert cmd == [str(venv_exe(root / "venv", "google-maps-mcp-server"))]

    def test_console_script_with_flags_and_plain_token(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "workspace-mcp"
        cmd = _resolve_warmup_command(
            root, "venv/bin/workspace-mcp",
            ["--single-user", "--transport", "stdio"],
        )
        assert cmd == [
            str(venv_exe(root / "venv", "workspace-mcp")),
            "--single-user", "--transport", "stdio",  # flags + plain token kept
        ]

    def test_node_with_mcp_dir_template(self, tmp_path):
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "email-server"
        cmd = _resolve_warmup_command(
            root, "node",
            ["${mcp_dir}/node_modules/mcp-mail-server/dist/index.js"],
        )
        assert cmd == [
            "node",  # bare command stays (resolved via PATH)
            str(root / "node_modules" / "mcp-mail-server" / "dist" / "index.js"),
        ]

    def test_python3_collapses_to_python(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "x"
        cmd = _resolve_warmup_command(root, "venv/bin/python3", ["server.py"])
        assert cmd[0] == str(venv_exe(root / "venv", "python"))


# Minimal stdio MCP stubs for the warm-up handshake integration tests.
_FAKE_OK_SERVER = (
    "import sys, json\n"
    "line = sys.stdin.readline()\n"
    "msg = json.loads(line)\n"
    "sys.stdout.write(json.dumps("
    "{'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {'ok': True}}) + '\\n')\n"
    "sys.stdout.flush()\n"
)
_FAKE_HANG_SERVER = "import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n"
_FAKE_CRASH_SERVER = "import sys\nsys.exit(1)\n"


class TestWarmOneMcp:
    """Integration: spawn a fake stdio MCP and drive the MCP ``initialize``
    handshake. Uses ``sys.executable`` as the (absolute) command so no fake
    venv is needed — the venv-path resolution itself is covered by
    TestResolveWarmupCommand. This is the GENERIC protocol check: any
    compliant stdio server answers initialize."""

    def _make_mcp(self, root, server_body, *, transport="stdio", command=None, args=None):
        root.mkdir(parents=True, exist_ok=True)
        (root / "server.py").write_text(server_body, encoding="utf-8")
        manifest = {
            "name": root.name,
            "server": {
                "transport": transport,
                "command": command if command is not None else sys.executable,
                "args": args if args is not None else ["server.py"],
            },
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    @pytest.mark.asyncio
    async def test_ok_on_initialize_reply(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "fake-mcp"
        self._make_mcp(root, _FAKE_OK_SERVER)
        name, status = await _warm_one_mcp(root, "fake-mcp", asyncio.Semaphore(2))
        assert name == "fake-mcp"
        assert status == "ok"

    @pytest.mark.asyncio
    async def test_warn_on_timeout(self, tmp_path, monkeypatch):
        from satellite.sessions import mcp_install_support
        monkeypatch.setattr(mcp_install_support, "_WARMUP_TIMEOUT_S", 1.0)
        root = tmp_path / "hang-mcp"
        self._make_mcp(root, _FAKE_HANG_SERVER)
        name, status = await mcp_install_support._warm_one_mcp(
            root, "hang-mcp", asyncio.Semaphore(2),
        )
        assert status == "warn:timeout"

    @pytest.mark.asyncio
    async def test_warn_on_immediate_crash(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "crash-mcp"
        self._make_mcp(root, _FAKE_CRASH_SERVER)
        name, status = await _warm_one_mcp(root, "crash-mcp", asyncio.Semaphore(2))
        assert status.startswith("warn:")  # advisory — never raises, never excludes

    @pytest.mark.asyncio
    async def test_skip_non_stdio(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "http-mcp"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({
            "name": "http-mcp",
            "server": {"transport": "sse", "url_template": "https://x/sse"},
        }), encoding="utf-8")
        name, status = await _warm_one_mcp(root, "http-mcp", asyncio.Semaphore(2))
        assert status == "skip:not-stdio"

    @pytest.mark.asyncio
    async def test_skip_no_command(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "weird-mcp"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({
            "name": "weird-mcp",
            "server": {"transport": "stdio", "command": ""},
        }), encoding="utf-8")
        name, status = await _warm_one_mcp(root, "weird-mcp", asyncio.Semaphore(2))
        assert status == "skip:no-command"


class _InterruptibleCLISession:
    """Fake CLI session: live proc + control-request recorder."""

    def __init__(self, returncode=None):
        self.proc = type("P", (), {"returncode": returncode})()
        self.control_requests: list = []

    async def send_control_request(self, subtype, **kwargs):
        self.control_requests.append(subtype)


class TestInterruptTurn:
    """Soft headless-CLI abort: interrupt_turn writes control_request
    {interrupt} into the CLI's stdin so the turn closes with a normal result
    event and the process + MCP sidecars survive (no re-warm)."""

    @pytest.mark.asyncio
    async def test_writes_interrupt_control_request(self, sat_config):
        sm = SessionManager(sat_config)
        sess = _InterruptibleCLISession()
        sm.sessions["s1"] = sess
        await sm.interrupt_turn({"session_id": "s1"})
        assert sess.control_requests == ["interrupt"]

    @pytest.mark.asyncio
    async def test_dead_process_is_a_noop(self, sat_config):
        # The proxy-side watchdog escalates to the hard abort; nothing to
        # write into a dead CLI's stdin.
        sm = SessionManager(sat_config)
        sess = _InterruptibleCLISession(returncode=1)
        sm.sessions["s1"] = sess
        await sm.interrupt_turn({"session_id": "s1"})
        assert sess.control_requests == []

    @pytest.mark.asyncio
    async def test_unknown_session_is_a_noop(self, sat_config):
        sm = SessionManager(sat_config)
        await sm.interrupt_turn({"session_id": "ghost"})  # must not raise

    @pytest.mark.asyncio
    async def test_session_without_control_seam_is_a_noop(self, sat_config):
        # interrupt_turn is CLI-only — a session object without the
        # send_control_request seam (codex) is skipped.
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = object()
        await sm.interrupt_turn({"session_id": "s1"})


class TestFileStat:
    """file_stat (0.5.95): cheap pull-cache revalidation probe. Validation
    mirrors file_pull; replies as a command ack."""

    def _ws(self):
        ws = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_existing_file_reports_size_and_mtime(self, sat_config):
        sm = SessionManager(sat_config)
        target = sat_config.agents_dir / "work" / "workspace" / "a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789")
        ws = self._ws()
        await sm.file_stat({
            "command_id": "c1", "path_kind": "agent_tree",
            "agent_slug": "work", "path": "workspace/a.txt",
        }, ws)
        reply = ws.enqueue_send.await_args.args[0]
        assert reply["type"] == "ack" and reply["command_id"] == "c1"
        assert reply["status"] == "ok"
        assert reply["exists"] is True
        assert reply["size"] == 10
        assert reply["mtime_ns"] == target.stat().st_mtime_ns

    @pytest.mark.asyncio
    async def test_missing_file_is_ok_but_absent(self, sat_config):
        sm = SessionManager(sat_config)
        (sat_config.agents_dir / "work").mkdir(parents=True)
        ws = self._ws()
        await sm.file_stat({
            "command_id": "c2", "path_kind": "agent_tree",
            "agent_slug": "work", "path": "workspace/nope.txt",
        }, ws)
        reply = ws.enqueue_send.await_args.args[0]
        assert reply["status"] == "ok"
        assert reply["exists"] is False

    @pytest.mark.asyncio
    async def test_traversal_rejected_as_error(self, sat_config):
        """A stat leaks existence — gated exactly as hard as a read."""
        sm = SessionManager(sat_config)
        (sat_config.agents_dir / "work").mkdir(parents=True)
        ws = self._ws()
        await sm.file_stat({
            "command_id": "c3", "path_kind": "agent_tree",
            "agent_slug": "work", "path": "../../../etc/passwd",
        }, ws)
        reply = ws.enqueue_send.await_args.args[0]
        assert reply["status"] == "error"
        assert "not authorized" in reply["error"]


class TestSyncMcpsCategoryGate:
    """sync_mcps validates name + category before using them as path
    components under mcps_dir. Every category the platform ships must
    install — the platform's own MCPs are ``core`` — and anything else is
    refused without touching the tree."""

    @staticmethod
    def _tarball(name):
        import base64
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = json.dumps({"name": name, "server": {"transport": "stdio"}}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return base64.b64encode(buf.getvalue()).decode()

    def _spec(self, name, category):
        return {
            "name": name, "category": category, "runtime": "python",
            "source": "", "tarball_b64": self._tarball(name), "version_hash": "h1",
        }

    async def _run(self, sat_config, specs):
        from types import SimpleNamespace
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        installed = SimpleNamespace(ok=True, log="", version_hash="h1")
        with patch("satellite._vendored.mcp_installer.install_mcp",
                   AsyncMock(return_value=installed)), \
             patch("satellite.sessions.session_manager._warm_one_mcp",
                   AsyncMock(side_effect=lambda root, name, sem: (name, "ok"))):
            await sm.sync_mcps(
                {"command_id": "c1", "mcps_to_install": specs, "mcps_to_remove": []},
                ws,
            )
        ack = ws.enqueue_send.call_args_list[-1].args[0]
        assert ack["type"] == "ack" and ack["command_id"] == "c1"
        return ack

    @pytest.mark.asyncio
    async def test_every_shipped_category_installs(self, sat_config):
        from satellite.sessions.session_manager import MCP_CATEGORIES
        specs = [self._spec(f"{cat}-mcp", cat) for cat in sorted(MCP_CATEGORIES)]
        ack = await self._run(sat_config, specs)
        for cat in sorted(MCP_CATEGORIES):
            name = f"{cat}-mcp"
            assert ack["results"][name]["status"] == "ok", ack["results"][name]
            assert (sat_config.mcps_dir / cat / name / "manifest.json").is_file()
            assert name in ack["installed_mcps"]
        assert "core" in MCP_CATEGORIES  # the platform's own MCPs

    @pytest.mark.asyncio
    async def test_a_tool_installs_on_a_python_without_the_tar_filter(self, sat_config,
                                                                      python_without_tar_filter):
        ack = await self._run(sat_config, [self._spec("plain-mcp", "core")])
        assert ack["results"]["plain-mcp"]["status"] == "ok", ack["results"]["plain-mcp"]
        assert (sat_config.mcps_dir / "core" / "plain-mcp" / "manifest.json").is_file()

    @pytest.mark.asyncio
    async def test_unknown_category_and_bad_name_are_refused(self, sat_config):
        specs = [
            self._spec("escaper", "../outside"),
            self._spec("stranger", "skill"),
            self._spec("../traversal", "core"),
        ]
        ack = await self._run(sat_config, specs)
        assert ack["results"]["escaper"]["error"] == "unsafe mcp category: '../outside'"
        assert ack["results"]["stranger"]["error"] == "unsafe mcp category: 'skill'"
        assert ack["results"]["../traversal"]["error"] == "unsafe mcp name: '../traversal'"
        assert all(r["status"] == "error" for r in ack["results"].values())
        assert not (sat_config.mcps_dir.parent / "outside").exists()
        assert not (sat_config.mcps_dir / "skill").exists()
        assert ack["installed_mcps"] == []

    @pytest.mark.asyncio
    async def test_a_fresh_venv_is_pinned_and_marked_reconciled(self, sat_config):
        """The install runs on the satellite's own Python, and a venv made on
        it reads reconciled to the boot's check (the marker lands after the
        swap, which replaces the folder). One made on uv's default does not."""
        from types import SimpleNamespace

        from satellite.sessions import mcp_install_support as mis
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        made = {"pinned-mcp": "/opt/py/bin/python3.13", "fallback-mcp": ""}

        async def fake_install(root, runtime, source, **kw):
            name = root.name.removesuffix(".new")
            return SimpleNamespace(ok=True, log="", version_hash="h1", venv_python=made[name],
                                   spec=kw.get("python_spec"))

        inst = AsyncMock(side_effect=fake_install)
        with patch("satellite._vendored.mcp_installer.install_mcp", inst), \
             patch("satellite.sessions.session_manager.mcp_python_spec", return_value="/opt/py/bin/python3.13"), \
             patch("satellite.sessions.session_manager._warm_one_mcp",
                   AsyncMock(side_effect=lambda root, name, sem: (name, "ok"))):
            await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [
                self._spec("pinned-mcp", "core"), self._spec("fallback-mcp", "core")],
                "mcps_to_remove": []}, ws)
        assert {c.kwargs["python_spec"] for c in inst.await_args_list} == {"/opt/py/bin/python3.13"}
        minor = f"{sys.version_info[0]}.{sys.version_info[1]}"
        pinned = sat_config.mcps_dir / "core" / "pinned-mcp" / mis._RUNTIME_MARKER
        assert json.loads(pinned.read_text()) == {"python": minor}
        assert not (sat_config.mcps_dir / "core" / "fallback-mcp" / mis._RUNTIME_MARKER).exists()

    @pytest.mark.asyncio
    async def test_a_failed_install_reports_its_cause_alone(self, sat_config):
        from types import SimpleNamespace
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        failed = SimpleNamespace(ok=False, log="Resolved 3 packages\n  × Failed to build `rpds-py==0.30.0`\n"
                                              "      error: rustc not found\n", version_hash="")
        with patch("satellite._vendored.mcp_installer.install_mcp", AsyncMock(return_value=failed)):
            await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [self._spec("memory-mcp", "core")],
                                "mcps_to_remove": []}, ws)
        ack = ws.enqueue_send.call_args_list[-1].args[0]
        assert ack["results"]["memory-mcp"] == {
            "status": "error", "error": "Failed to build `rpds-py==0.30.0` / error: rustc not found"}

    def test_allow_list_covers_every_shipped_manifest(self):
        """The platform ships install specs with the manifest's ``category``
        verbatim; a category the satellite refuses drops that MCP from every
        remote session. Docker and context-only MCPs never install on a
        satellite, so only satellite-installable manifests are checked."""
        from pathlib import Path

        from satellite.sessions.session_manager import MCP_CATEGORIES
        root = Path(__file__).resolve().parents[2] / "mcps"
        manifests = sorted(root.glob("*/*/manifest.json"))
        if not manifests:
            pytest.skip("platform mcps tree not present in this checkout")
        for mf in manifests:
            data = json.loads(mf.read_text())
            if (data.get("server") or {}).get("runtime") in ("docker", "none"):
                continue
            assert data.get("category") in MCP_CATEGORIES, mf



class TestEngineTableDispatch:
    """Every dispatch runs through the ENGINES table and fails closed on an id
    the table lacks — on BOTH paths (the headless start and the PTY open)."""

    @pytest.mark.asyncio
    async def test_pty_open_refuses_an_unknown_or_absent_engine(self, sat_config):
        sm = SessionManager(sat_config)
        for msg in (
            {"execution_path": "acme-cli"},
            {},                                    # no key: never a default engine
        ):
            ws = _CapturingWS()
            await sm.pty_open({
                "session_id": "pty-x", "command_id": "cmd-p", "agent_slug": "test-agent",
                "config": {}, **msg,
            }, ws)
            ack = ws.sent[0]
            assert ack["status"] == "error", msg
            assert "pty-x" not in sm.pty_sessions

    def test_capabilities_advertise_the_dispatchable_engines(self, sat_config):
        sm = SessionManager(sat_config)
        with patch.object(shutil, "which", return_value=None):
            caps = sm.detect_capabilities()
        # The engines this CODE dispatches, whatever binaries are installed.
        assert caps["engines"] == ["claude-code-cli", "codex-cli"]
        assert caps["installed_clis"] == []

    @pytest.mark.asyncio
    async def test_a_dead_codex_daemon_is_not_an_ack_error(self, sat_config):
        """The Codex session re-warms its own dead daemon on the next turn
        (thread/resume) — the dead-process ack error is the Claude CLI's,
        whose process the PROXY must respawn."""
        class _DeadCodex:
            execution_path = "codex-cli"
            is_alive = False
            agent_slug = "agent-1"

            def __init__(self):
                self.turns = []

            async def run_turn(self, message, *, inject_time=False, forward):
                self.turns.append(message)

            def detect_file_changes(self):
                return []

        sm = SessionManager(sat_config)
        dead = _DeadCodex()
        sm.sessions["sess-c"] = dead
        ws = _CapturingWS()
        await sm.send_message({"session_id": "sess-c", "command_id": "cmd-1",
                               "message": "hi"}, ws)
        assert ws.sent[0] == {"type": "ack", "command_id": "cmd-1", "status": "ok"}
        assert dead.turns == ["hi"]

    @pytest.mark.asyncio
    async def test_a_codex_turn_is_kept_and_its_later_events_ride_plain(self, sat_config):
        """A Codex turn's events are tagged and kept for a replay as a Claude
        turn's are (0.5.171). The persistent forwarder outlives the turn: an
        event it forwards after the turn (a background sub-agent's) is
        neither tagged nor kept, and waits on no replay's pause."""
        class _Codex:
            execution_path = "codex-cli"
            is_alive = True
            agent_slug = "agent-1"

            def __init__(self):
                self.forward = None

            async def run_turn(self, message, *, inject_time=False, forward):
                self.forward = forward
                await forward({"method": "item/started", "params": {}})
                await forward({"method": "turn/completed", "params": {}})

            def detect_file_changes(self):
                return []

        sm = SessionManager(sat_config)
        codex = sm.sessions["sess-c"] = _Codex()
        ws = _CapturingWS()
        await sm.send_message({"session_id": "sess-c", "command_id": "cmd-1",
                               "message": "hi"}, ws)
        events = [m["event"] for m in ws.sent if m["type"] == "session_event"]
        assert [e["_command_id"] for e in events] == ["cmd-1", "cmd-1"]
        assert [e["_seq"] for e in events] == [1, 2]
        kept = list(sm.turn_buffers["sess-c"])
        assert [e.get("method") or e.get("type") for e in kept] == [
            "item/started", "turn/completed", "_turn_sentinel"]
        assert sm.turn_state["sess-c"]["active"] is False
        assert [m["type"] for m in ws.sent][-1] == "turn_ended"
        # After the turn: a background sub-agent's event, plain, even while
        # a replay holds the forwarder.
        sm.forward_paused["sess-c"] = asyncio.Event()
        await asyncio.wait_for(codex.forward({"method": "item/completed", "params": {"threadId": "sub"}}), 1.0)
        late = ws.sent[-1]["event"]
        assert late == {"method": "item/completed", "params": {"threadId": "sub"}}
        assert len(sm.turn_buffers["sess-c"]) == 3

    @pytest.mark.asyncio
    async def test_resume_replay_names_the_engine_from_the_turn_state(self, sat_config):
        sm = SessionManager(sat_config)
        from collections import deque
        sm.turn_state["s1"] = {"seq": 2, "active": False, "command_id": "cmd-1",
                               "start_seq": 1, "execution_path": "claude-code-cli"}
        sm.turn_buffers["s1"] = deque([
            {"type": "assistant", "_seq": 1, "_command_id": "cmd-1"},
            {"type": "_turn_sentinel", "_seq": 2, "command_id": "cmd-1"},
        ])
        ws = _CapturingWS()
        await sm.resume_session_stream({"session_id": "s1"}, ws)   # session object gone
        paths = {m["execution_path"] for m in ws.sent if m["type"] == "session_event"}
        assert paths == {"claude-code-cli"}


class TestStateDirectoriesReported:
    def test_capabilities_say_the_start_hook_runs(self, sat_config):
        """The platform sends a terminal's start data to a satellite that
        says so, and puts it in the text of one that does not."""
        assert SessionManager(sat_config).detect_capabilities()["start_context_hook"] is True

    def test_capabilities_carry_the_state_root_and_the_mcps_folder(self, sat_config):
        from satellite import config
        caps = SessionManager(sat_config).detect_capabilities()
        assert caps["otodock_dir"] == str(config.otodock_dir().resolve()).replace("\\", "/")
        assert caps["mcps_dir"] == str(sat_config.mcps_dir.resolve()).replace("\\", "/")
        assert "/" in caps["otodock_dir"] and "\\" not in caps["otodock_dir"]

    def test_capabilities_carry_the_home_and_the_root_as_the_os_names_them(
            self, sat_config, tmp_path, monkeypatch):
        """A home reached through a link: the resolved forms above, and the
        unresolved ones beside them, so the proxy refuses both spellings."""
        from pathlib import Path

        from satellite import config
        real = tmp_path / "data" / "home" / "bob"
        real.mkdir(parents=True)
        link = tmp_path / "home-bob"
        link.symlink_to(real)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: link))
        caps = SessionManager(sat_config).detect_capabilities()
        assert caps["home_dir_unresolved"] == str(link).replace("\\", "/")
        assert caps["otodock_dir_unresolved"] == (
            str(link / config.OTODOCK_DIRNAME).replace("\\", "/"))
        assert caps["otodock_dir"] == str(real / config.OTODOCK_DIRNAME).replace("\\", "/")


class TestManifestPaging:
    def _tree(self, sat_config, n):
        d = sat_config.agents_dir / "test-agent" / "workspace"
        d.mkdir(parents=True)
        for i in range(n):
            (d / f"f{i}.txt").write_text(str(i))

    @pytest.mark.asyncio
    async def test_a_request_without_page_size_answers_one_frame(self, sat_config):
        self._tree(sat_config, 5)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.request_manifest({"agent_slug": "test-agent", "command_id": "c1"}, ws)
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "file_manifest" and "more" not in ws.sent[0]
        assert len(ws.sent[0]["files"]) == 5

    @pytest.mark.asyncio
    async def test_a_request_with_page_size_answers_pages_in_order(self, sat_config, monkeypatch):
        from satellite.sessions import session_manager as smod
        monkeypatch.setattr(smod, "_MANIFEST_PAGE_MIN", 1)
        self._tree(sat_config, 5)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.request_manifest(
            {"agent_slug": "test-agent", "command_id": "c1", "page_size": 2}, ws)
        assert [(m["page"], m["more"], len(m["files"])) for m in ws.sent] == [
            (0, True, 2), (1, True, 2), (2, False, 1)]
        assert {m["command_id"] for m in ws.sent} == {"c1"}
        paths = [e["path"] for m in ws.sent for e in m["files"]]
        assert sorted(paths) == sorted(f"workspace/f{i}.txt" for i in range(5))

    @pytest.mark.asyncio
    async def test_the_pages_are_sized_off_the_loop(self, sat_config, monkeypatch):
        """Sizing a big tree's pages is a serialization per entry: it runs
        with the walk, off the loop that carries heartbeats and PTY frames."""
        import threading

        from satellite.sessions import session_manager as smod
        real = smod._manifest_pages
        threads: list[bool] = []

        def _spy(entries, page_size):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(entries, page_size)
        monkeypatch.setattr(smod, "_manifest_pages", _spy)
        self._tree(sat_config, 3)
        ws = _CapturingWS()
        await SessionManager(sat_config).request_manifest(
            {"agent_slug": "test-agent", "command_id": "c1", "page_size": 4096}, ws)
        assert threads == [False]
        assert [len(m["files"]) for m in ws.sent] == [3]

    @pytest.mark.asyncio
    async def test_skips_are_listed_only_for_a_proxy_that_asks(self, sat_config, monkeypatch):
        from satellite.transport import file_sync
        self._tree(sat_config, 2)
        real = file_sync._open_listed

        def open_listed(root, rel, path, cap):
            if path.name == "f0.txt":
                raise PermissionError(13, "locked")
            return real(root, rel, path, cap)
        monkeypatch.setattr(file_sync, "_open_listed", open_listed)
        sm = SessionManager(sat_config)
        plain, asked = _CapturingWS(), _CapturingWS()
        await sm.request_manifest({"agent_slug": "test-agent", "command_id": "c1"}, plain)
        await sm.request_manifest(
            {"agent_slug": "test-agent", "command_id": "c2", "report_skipped": True}, asked)
        assert [e["path"] for e in plain.sent[0]["files"]] == ["workspace/f1.txt"]
        assert {e["path"]: e.get("skipped") for e in asked.sent[0]["files"]} == {
            "workspace/f0.txt": "unreadable", "workspace/f1.txt": None}

    def test_pages_are_bounded_by_entries_and_by_bytes(self):
        from satellite.sessions.session_manager import _manifest_pages
        assert _manifest_pages([], 4096) == [[]]
        many = [{"path": f"p{i}", "hash": "h", "size": 1, "mtime": 1.0} for i in range(10000)]
        pages = _manifest_pages(many, 1)          # clamped up to the floor
        assert all(len(p) <= 256 for p in pages) and sum(len(p) for p in pages) == 10000
        pages = _manifest_pages(many, 10 ** 9)    # clamped down to the ceiling
        assert all(len(p) <= 8192 for p in pages) and len(pages) == 2
        fat = [{"path": "x" * (1100 * 1024), "hash": "h", "size": 1, "mtime": 1.0} for _ in range(5)]
        pages = _manifest_pages(fat, 4096)
        assert len(pages) == 5 and all(len(p) == 1 for p in pages)


class TestSourceBuildOnTheSpec:
    def test_the_field_is_validated(self):
        from satellite.sessions.session_manager import _source_build_of
        assert _source_build_of({}) == []
        assert _source_build_of({"source_build": None}) == []
        assert _source_build_of({"source_build": "unifi-network"}) == []
        assert _source_build_of({"source_build": ["unifi-network", "bad pkg!", 3, "a.b_c-d"]}) == [
            "unifi-network", "a.b_c-d"]

    @pytest.mark.asyncio
    async def test_the_install_receives_it(self, sat_config):
        import base64
        import io
        import tarfile
        from types import SimpleNamespace
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = json.dumps({"name": "unifi-mcp", "server": {"transport": "stdio"}}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        spec = {
            "name": "unifi-mcp", "category": "community", "runtime": "python", "source": "",
            "tarball_b64": base64.b64encode(buf.getvalue()).decode(), "version_hash": "h1",
            "source_build": ["unifi-network", "not ok"],
        }
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        installed = SimpleNamespace(ok=True, log="", version_hash="h1")
        with patch("satellite._vendored.mcp_installer.install_mcp",
                   AsyncMock(return_value=installed)) as inst, \
             patch("satellite.sessions.session_manager._warm_one_mcp",
                   AsyncMock(side_effect=lambda root, name, sem: (name, "ok"))):
            await sm.sync_mcps(
                {"command_id": "c1", "mcps_to_install": [spec], "mcps_to_remove": []}, ws)
        assert inst.await_args.kwargs["source_build"] == ["unifi-network"]


class TestPullAndStatOpenBeneathTheRoot:
    def _tree(self, sat_config):
        import os
        agent = sat_config.agents_dir / "test-agent"
        (agent / "workspace").mkdir(parents=True)
        (agent / "users" / "other").mkdir(parents=True)
        (agent / "users" / "other" / "f.txt").write_bytes(b"PRIVATE")
        os.symlink("../users/other", agent / "workspace" / "alias")
        return agent

    @pytest.mark.asyncio
    async def test_pull_refuses_an_in_tree_link_component(self, sat_config):
        self._tree(sat_config)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.file_pull({"agent_slug": "test-agent", "path": "workspace/alias/f.txt",
                            "request_id": "r1"}, ws)
        assert len(ws.sent) == 1
        assert ws.sent[0]["error"].startswith("path not authorized")
        assert "content_b64" not in ws.sent[0]

    @pytest.mark.asyncio
    async def test_pull_streams_a_regular_file(self, sat_config):
        agent = self._tree(sat_config)
        (agent / "workspace" / "ok.txt").write_bytes(b"ok")
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.file_pull({"agent_slug": "test-agent", "path": "workspace/ok.txt",
                            "request_id": "r1"}, ws)
        import base64
        assert base64.b64decode(ws.sent[-1]["content_b64"]) == b"ok"
        assert ws.sent[-1]["hash"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_stat_answers_from_the_name_itself(self, sat_config):
        agent = self._tree(sat_config)
        (agent / "workspace" / "ok.txt").write_bytes(b"ok")
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.file_stat({"agent_slug": "test-agent", "path": "workspace/alias/f.txt",
                            "command_id": "s1"}, ws)
        assert ws.sent[0]["status"] == "error"
        ws = _CapturingWS()
        await sm.file_stat({"agent_slug": "test-agent", "path": "workspace/ok.txt",
                            "command_id": "s2"}, ws)
        assert ws.sent[0]["exists"] is True and ws.sent[0]["size"] == 2
        ws = _CapturingWS()
        await sm.file_stat({"agent_slug": "test-agent", "path": "workspace/none.txt",
                            "command_id": "s3"}, ws)
        assert ws.sent[0]["status"] == "ok" and ws.sent[0]["exists"] is False


class TestSyncMcpsRemovals:
    """A 1.7.1 platform removes only MCPs it no longer ships and says so with
    ``remove_any_category``: those go from any category. Without the flag
    (an older platform, which also names MCPs a session merely left out) a
    ``core`` folder stays, as in 0.5.136."""

    def _installed(self, sat_config, category, name):
        d = sat_config.mcps_dir / category / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text("{}")
        return d

    @pytest.mark.asyncio
    async def test_a_core_folder_goes_when_asked_for_any_category(self, sat_config, caplog):
        d = self._installed(sat_config, "core", "gone-mcp")
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        with caplog.at_level("INFO"):
            await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [],
                                "mcps_to_remove": ["gone-mcp"], "remove_any_category": True}, ws)
        assert not d.exists()
        ack = ws.enqueue_send.await_args.args[0]
        assert ack["results"]["gone-mcp"]["status"] == "removed"
        assert "sync_mcps: removed core/gone-mcp" in caplog.text

    @pytest.mark.asyncio
    async def test_without_the_flag_a_core_folder_is_left(self, sat_config):
        d = self._installed(sat_config, "core", "task-left-mcp")
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [],
                            "mcps_to_remove": ["task-left-mcp"]}, ws)
        assert d.is_dir()
        ack = ws.enqueue_send.await_args.args[0]
        assert ack["results"]["task-left-mcp"]["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_a_custom_removal_is_logged_either_way(self, sat_config, caplog):
        d = self._installed(sat_config, "custom", "old-mcp")
        sm = SessionManager(sat_config)
        with caplog.at_level("INFO"):
            await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [],
                                "mcps_to_remove": ["old-mcp"]}, AsyncMock())
        assert not d.exists()
        assert "sync_mcps: removed custom/old-mcp" in caplog.text


class TestWhatThePlatformReplays:
    """The connect report lists a live session with its turn only for an
    engine the platform signed in replays (``replays_turns_of``); Claude
    Code alone under a platform that names none."""

    def test_codex_is_in_the_other_list_under_a_platform_that_names_none(self, sat_config):
        from satellite.sessions import alive_report
        sm = SessionManager(sat_config)
        sm.sessions["c-idle"] = _HeldCodexSession()
        assert sm.headless_sessions_alive() == []
        assert [e["session_id"] for e in alive_report.other_sessions_held(sm)] == ["c-idle"]

    def test_codex_is_in_the_turn_list_under_a_platform_that_replays_it(self, sat_config):
        from satellite.sessions import alive_report
        sm = SessionManager(sat_config)
        sm.replays_turns_of = frozenset({"claude-code-cli", "codex-cli"})
        sm.sessions["c-idle"] = _HeldCodexSession()
        sm.sessions["c-dead"] = _HeldCodexSession(alive=False)
        sm.turn_state["c-idle"] = {"active": True, "command_id": "cmd-9", "seq": 3}
        [entry] = sm.headless_sessions_alive()
        assert entry["session_id"] == "c-idle" and entry["turn_active"] is True
        assert (entry["resume_handle"], entry["model"], entry["mcp_servers"]) == (
            "th-1", "gpt-6.1-sol", ["file-tools", "memory"])
        assert entry["command_id"] == "cmd-9"
        assert [e["session_id"] for e in alive_report.other_sessions_held(sm)] == ["c-dead"]

    def test_a_claude_entry_carries_its_tools_and_an_empty_handle(self, sat_config):
        sm = SessionManager(sat_config)
        live = _FakeCLISession([])
        live.config = {"model": "claude-sonnet-5-5",
                       "mcp_config": {"mcpServers": {"memory": {}, "display": {}}}}
        live.mcp_server_names = ["display", "memory"]
        sm.sessions["s1"] = live
        [entry] = sm.headless_sessions_alive()
        assert (entry["resume_handle"], entry["model"], entry["mcp_servers"]) == (
            "", "claude-sonnet-5-5", ["display", "memory"])

    def test_the_auth_answer_names_the_engines(self):
        from unittest.mock import MagicMock

        from satellite.transport.ws_client import SatelliteWSClient
        client = SatelliteWSClient.__new__(SatelliteWSClient)
        client.sm = MagicMock()
        client._note_replays_turns_of({"replays_turns_of": ["claude-code-cli", "codex-cli", 7]})
        assert client.sm.replays_turns_of == frozenset({"claude-code-cli", "codex-cli"})
        client._note_replays_turns_of({})
        assert client.sm.replays_turns_of == frozenset({"claude-code-cli"})


class TestCapabilitiesCount:
    def test_the_capabilities_carry_the_live_count(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["h"] = _FakeCLISession([])
        sm.pty_sessions["t"] = object()
        assert sm.detect_capabilities()["active_sessions"] == 2


class TestCredentialsReachALiveSession:
    @pytest.mark.asyncio
    async def test_a_rotated_codex_token_reaches_the_sessions_own_config(self, sat_config, tmp_path):
        """A Codex session re-warms its daemon from its start config: the
        rotated token replaces the copy there too, so a revive writes the
        current one."""
        from types import SimpleNamespace
        sm = SessionManager(sat_config)
        home = sat_config.agents_dir / "agent-c" / "users" / "alice" / ".codex"
        home.mkdir(parents=True)
        other = sat_config.agents_dir / "agent-c" / "users" / "bob" / ".codex"
        other.mkdir(parents=True)
        mine = SimpleNamespace(_codex_dir=home, config={"auth_json": {"tokens": {"access_token": "old"}}})
        bobs = SimpleNamespace(_codex_dir=other, config={"auth_json": {"tokens": {"access_token": "bob"}}})
        sm.sessions["mine"], sm.sessions["bobs"] = mine, bobs
        ws = _CapturingWS()
        await sm.credentials_update({
            "command_id": "c1", "agent_slug": "agent-c", "dir_relative": "users/alice/.codex",
            "kind": "codex", "content": {"tokens": {"access_token": "new"}},
        }, ws)
        assert ws.sent[-1]["status"] == "ok"
        assert mine.config["auth_json"] == {"tokens": {"access_token": "new"}}
        assert bobs.config["auth_json"] == {"tokens": {"access_token": "bob"}}


class TestTheTokenPerOpen:
    """A ``pty_open`` carries the platform's token for that open: the ack and
    the exit name it, a close or a death under another token leaves the live
    PTY alone, and a PTY an open replaced leaves its successor's files."""

    def _session(self, token=""):
        from unittest.mock import AsyncMock
        s = AsyncMock()
        s.pid = 5
        s.open_token = token
        s.superseded = False
        return s

    @pytest.mark.asyncio
    async def test_the_ack_names_the_token_and_a_replaced_pty_is_closed_first(self, sat_config):
        sm = SessionManager(sat_config)
        old = self._session("tok-old")
        sm.pty_sessions["t1"] = old
        new = self._session()
        ws = _CapturingWS()
        with patch("satellite.terminal.pty_session.PtySession", return_value=new):
            await sm.pty_open({"session_id": "t1", "command_id": "c1", "agent_slug": "agent-c",
                               "config": {}, "execution_path": "claude-code-cli",
                               "open_token": "tok-new"}, ws)
        old.close.assert_awaited_once()
        assert old.superseded is True
        assert sm.pty_sessions["t1"] is new and new.open_token == "tok-new"
        assert ws.sent[-1] == {"type": "ack", "command_id": "c1", "status": "ok", "pid": 5,
                               "open_token": "tok-new"}

    @pytest.mark.asyncio
    async def test_a_close_and_a_death_under_another_token_leave_the_live_pty(self, sat_config):
        sm = SessionManager(sat_config)
        live = self._session("tok-new")
        sm.pty_sessions["t1"] = live
        await sm.pty_close({"session_id": "t1", "open_token": "tok-old"})
        assert sm.pty_sessions["t1"] is live
        live.close.assert_not_awaited()
        sm._on_pty_dead("t1", "tok-old")
        assert sm.pty_sessions["t1"] is live
        sm._on_pty_dead("t1", "tok-new")
        assert "t1" not in sm.pty_sessions

    @pytest.mark.asyncio
    async def test_a_close_with_the_right_token_or_none_closes(self, sat_config):
        sm = SessionManager(sat_config)
        live = self._session("tok-new")
        sm.pty_sessions["t1"] = live
        await sm.pty_close({"session_id": "t1", "open_token": "tok-new"})
        live.close.assert_awaited_once()
        assert "t1" not in sm.pty_sessions
        again = self._session("tok-x")
        sm.pty_sessions["t2"] = again
        await sm.pty_close({"session_id": "t2"})
        again.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_replaced_ptys_close_keeps_the_successors_files(self, tmp_path):
        from types import SimpleNamespace

        from satellite.sessions import session_files
        from satellite.terminal.pty_session_base import BasePtySession
        ws = _CapturingWS()
        sess = BasePtySession("t9", tmp_path / "agent", config={}, sat_config=SimpleNamespace(), ws=ws)
        wiped = []
        with patch.object(session_files, "wipe", side_effect=wiped.append):
            sess.superseded = True
            await sess.close()
            assert wiped == []
            sess.superseded = False
            await sess.close()
            assert wiped == ["t9"]

    def test_the_exit_frame_names_the_token(self, tmp_path):
        from types import SimpleNamespace

        from satellite.terminal.pty_session_base import BasePtySession
        sent = []

        class _WS:
            async def enqueue_send(self, msg):
                sent.append(msg)

            async def enqueue_pty(self, msg):
                sent.append(msg)

        sess = BasePtySession("t3", tmp_path / "agent", config={}, sat_config=SimpleNamespace(), ws=_WS())
        sess.open_token = "tok-3"
        captured = {}

        def spawn_pty(cmd, *, env, cwd, rows, cols, on_output, on_exit):
            captured["on_exit"] = on_exit
            return SimpleNamespace(pid=1)

        from satellite.terminal import pty_relay
        with patch.object(pty_relay, "spawn_pty", spawn_pty), \
                patch("satellite.transport.file_sync.snapshot_agent_dir", return_value={}):
            (tmp_path / "agent").mkdir()
            asyncio.run(sess._wire_and_spawn(["true"], {}, str(tmp_path)))
            sess._tail_task.cancel()
            asyncio.run(captured["on_exit"](0))
        assert sent[-1] == {"type": "pty_exit", "session_id": "t3", "code": 0, "open_token": "tok-3"}
        # The memory guard ended the terminal's engine: its exit says so.
        sess.ended_for_memory = True
        asyncio.run(captured["on_exit"](-9))
        assert sent[-1] == {"type": "pty_exit", "session_id": "t3", "code": -9, "open_token": "tok-3",
                            "reason": "memory"}


class TestTheLastStartCheck:
    """``_check_capacity``: an id the machine holds always passes; a machine
    whose memory guard runs refuses by its own reading, with a reason the
    platform shows; elsewhere the count stays."""

    def test_an_id_it_holds_always_passes(self, sat_config, monkeypatch):
        from satellite.sessions import memory_guard
        sm = SessionManager(sat_config)
        sm.sessions["s-1"] = object()
        monkeypatch.setattr(memory_guard, "running", lambda: True)
        monkeypatch.setattr(memory_guard, "start_refusal", lambda: memory_guard.START_REFUSAL)
        sm._check_capacity("s-1")   # a replacing start after a platform restart
        with pytest.raises(memory_guard.MachineMemoryRefused) as refused:
            sm._check_capacity("s-2")
        assert str(refused.value) == memory_guard.START_REFUSAL
        assert refused.value.reason == "machine_memory"

    def test_with_the_guard_the_count_no_longer_caps(self, sat_config, monkeypatch):
        from satellite.sessions import memory_guard
        sm = SessionManager(sat_config)
        for i in range(100):
            sm.sessions[f"s-{i}"] = object()
        monkeypatch.setattr(memory_guard, "running", lambda: True)
        monkeypatch.setattr(memory_guard, "start_refusal", lambda: None)
        sm._check_capacity("s-new")

    def test_without_the_guard_the_count_caps_as_before(self, sat_config, monkeypatch):
        from satellite.sessions import memory_guard, session_manager as smod
        sm = SessionManager(sat_config)
        monkeypatch.setattr(memory_guard, "running", lambda: False)
        monkeypatch.setattr(smod, "_recommended_max_sessions", lambda: 1)
        sm.sessions["s-1"] = object()
        with pytest.raises(RuntimeError, match="at capacity"):
            sm._check_capacity("s-2")

    def test_a_refused_start_names_its_reason_in_the_ack(self, sat_config, monkeypatch):
        from satellite.sessions import memory_guard
        sm = SessionManager(sat_config)
        monkeypatch.setattr(memory_guard, "running", lambda: True)
        monkeypatch.setattr(memory_guard, "start_refusal", lambda: memory_guard.START_REFUSAL)
        ws = AsyncMock()
        sent: list[dict] = []
        ws.enqueue_send = AsyncMock(side_effect=sent.append)
        asyncio.run(sm.start_session({"command_id": "c-1", "session_id": "s-9", "agent_slug": "a",
                                      "execution_path": "claude-code-cli", "config": {}}, ws))
        ack = [f for f in sent if f.get("type") == "ack"][-1]
        assert ack["status"] == "error" and ack["reason"] == "machine_memory"
        assert ack["error"] == memory_guard.START_REFUSAL


class TestMemoryUnits:
    def test_every_table_gives_its_units(self, sat_config):
        from types import SimpleNamespace

        sm = SessionManager(sat_config)
        sm.sessions["s-1"] = SimpleNamespace(pid=11, execution_path="claude-code-cli")
        sm.sessions["s-2"] = SimpleNamespace(pid=None, execution_path="claude-code-cli")
        sm.pty_sessions["t-1"] = SimpleNamespace(pid=12, execution_path="codex-cli")
        sm.app_servers.servers["a-1"] = SimpleNamespace(proc=SimpleNamespace(pid=13))
        sm._steps["st-1"] = SimpleNamespace(pid=14)
        units = {(u.pid, u.session_id, u.kind, u.engine) for u in sm.memory_units()}
        assert units == {(11, "s-1", "session", "claude"), (12, "t-1", "terminal", "codex"),
                         (13, "", "app", ""), (14, "", "step", "")}

    def test_the_guards_end_marks_the_session(self, sat_config):
        from types import SimpleNamespace

        sm = SessionManager(sat_config)
        held = SimpleNamespace()
        sm.sessions["s-1"] = held
        sm.mark_ended_for_memory("s-1")
        sm.mark_ended_for_memory("gone")
        assert held.ended_for_memory is True


class TestACloseInFlightHoldsTheNextStart:
    """A close of an id still running (a terminal's close is never
    acknowledged; a headless close may outlast the platform's wait) and a
    start of the same id arriving behind it: the start waits for the close,
    so the old close never removes the home the new start just made."""

    SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"

    class _Session:
        def __init__(self, sid, agent_dir, config, sat_config, ws=None, rows=24, cols=80):
            self.home = agent_dir / "users" / "alice" / ".claude" / "own" / sid
            self.open_token, self.pid, self.superseded, self.incarnation = "", 1, False, ""

        async def start(self):
            self.home.mkdir(parents=True, exist_ok=True)

        async def close(self):
            await asyncio.sleep(0.2)   # the transcript's last flush, the folder's scan
            shutil.rmtree(self.home, ignore_errors=True)

    def _engine(self, monkeypatch):
        from types import SimpleNamespace

        from satellite.sessions import session_manager as sm_mod
        engine = SimpleNamespace(pty_cls=lambda: self._Session, headless_cls=lambda: self._Session)
        monkeypatch.setattr(sm_mod, "engine_for", lambda path: engine)

    @pytest.mark.asyncio
    async def test_a_terminal_opened_behind_its_close(self, sat_config, monkeypatch):
        self._engine(monkeypatch)
        sm = SessionManager(sat_config)
        old = self._Session(self.SID, sat_config.agents_dir / "pa", {}, sat_config)
        await old.start()
        sm.pty_sessions[self.SID] = old
        closing = asyncio.create_task(sm.pty_close({"session_id": self.SID}))
        await asyncio.sleep(0)
        ws = _CapturingWS()
        await sm.pty_open({"session_id": self.SID, "command_id": "c", "agent_slug": "pa", "config": {},
                           "execution_path": "claude-code-cli"}, ws)
        await closing
        assert ws.sent[-1]["status"] == "ok"
        assert old.home.is_dir()

    @pytest.mark.asyncio
    async def test_a_session_started_behind_its_close(self, sat_config, monkeypatch):
        self._engine(monkeypatch)
        sm = SessionManager(sat_config)
        old = self._Session(self.SID, sat_config.agents_dir / "pa", {}, sat_config)
        await old.start()
        sm.sessions[self.SID] = old
        closing = asyncio.create_task(sm.close_session({"session_id": self.SID, "command_id": "x"},
                                                       _CapturingWS()))
        await asyncio.sleep(0)
        ws = _CapturingWS()
        await sm.start_session({"session_id": self.SID, "command_id": "c", "agent_slug": "pa", "config": {},
                                "execution_path": "claude-code-cli"}, ws)
        await closing
        assert ws.sent[0]["status"] == "ok", ws.sent
        assert old.home.is_dir()
