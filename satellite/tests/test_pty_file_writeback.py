"""PTY (interactive) sessions' file write-back — the TUI twin of the ``-p``
path's end-of-turn scan.

The bug: interactive sessions took a ``_file_snapshot`` at start "for parity
with CLISession" but NOTHING ever diffed it — no turn-end scan exists on the
PTY path, so a file the TUI session wrote (e.g. an owner curating
``config/context/``) never produced a ``file_changed`` and sat satellite-only
until some later merge... where the identity-less idle sweep could even scrub
it (see proxy core/remote/file_sync.py admin-shared scrub fix).

Fix: the transcript tail loop scans on QUIESCENCE — the first quiet poll after
a burst of forwarded transcript lines (≈ the turn just ended) — plus a forced
final scan at close(). Incoming platform pushes refresh PTY snapshots too
(``session_manager.file_push``), so a push never echoes back as a write-back.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from satellite.config import SatelliteConfig
from satellite.sessions.session_manager import SessionManager
from satellite.terminal.pty_session_base import BasePtySession
from satellite.transport import file_sync


class _CapturingWS:
    def __init__(self):
        self.sent = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)


def _make_pty_session(tmp_path, ws) -> BasePtySession:
    agent_dir = tmp_path / "agents" / "test-agent"
    (agent_dir / "config" / "context").mkdir(parents=True)
    sess = BasePtySession(
        "sess-1", agent_dir, config={}, sat_config=SimpleNamespace(), ws=ws,
    )
    sess._file_snapshot = file_sync.snapshot_agent_dir(agent_dir)
    return sess


@pytest.mark.asyncio
async def test_scan_forwards_new_file_once(tmp_path):
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)

    (sess.agent_dir / "config" / "context" / "notes.md").write_text("# creds")
    await sess._scan_and_forward_files()

    changed = [m for m in ws.sent if m["type"] == "file_changed"]
    assert len(changed) == 1
    assert changed[0]["path"] == "config/context/notes.md"
    assert changed[0]["agent_slug"] == "test-agent"
    assert changed[0]["session_id"] == "sess-1"
    assert changed[0]["action"] == "write"

    # Snapshot refreshed in place → a re-scan with no writes emits nothing.
    ws.sent.clear()
    await sess._scan_and_forward_files()
    assert ws.sent == []


@pytest.mark.asyncio
async def test_platform_push_never_echoes_back(tmp_path):
    """A file the PLATFORM pushes down must not bounce back as a write-back:
    file_push refreshes PTY sessions' snapshots (they scan now)."""
    sat_config = SatelliteConfig(
        machine_id="test-machine",
        machine_secret="test-secret",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents",
        mcps_dir=tmp_path / "mcps",
        claude_bin="claude",
        codex_bin="codex",
    )
    sat_config.agents_dir.mkdir(parents=True)
    sm = SessionManager(sat_config)

    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    sm.pty_sessions["sess-1"] = sess

    await sm.file_push({
        "agent_slug": "test-agent",
        "path": "config/context/pushed.md",
        "action": "write",
        "content_b64": base64.b64encode(b"platform copy").decode(),
    })

    await sess._scan_and_forward_files()
    assert [m for m in ws.sent if m["type"] == "file_changed"] == []


@pytest.mark.asyncio
async def test_tail_loop_scans_on_quiescence(tmp_path, monkeypatch):
    """Burst of transcript lines → dirty; the FIRST quiet poll runs the scan
    exactly once (not once per quiet poll)."""
    from satellite.terminal import pty_session_base as mod
    monkeypatch.setattr(mod, "_TRANSCRIPT_POLL_S", 0.001)

    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)

    forwards = iter([True, True, False, False, False])

    async def _scripted_forward():
        try:
            return next(forwards)
        except StopIteration:
            sess._tail_stop = True
            return False

    scans = []

    async def _counting_scan():
        scans.append(1)

    monkeypatch.setattr(sess, "_forward_transcript_delta", _scripted_forward)
    monkeypatch.setattr(sess, "_scan_and_forward_files", _counting_scan)

    await sess._transcript_tail_loop()
    assert len(scans) == 1


@pytest.mark.asyncio
async def test_close_runs_final_scan(tmp_path, monkeypatch):
    """close() force-scans — the last burst may never see a quiet poll."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    monkeypatch.setattr(
        "satellite.sessions.session_files.wipe", lambda _sid: None,
    )

    (sess.agent_dir / "config" / "context" / "late.md").write_text("last write")
    await sess.close()

    changed = [m for m in ws.sent if m["type"] == "file_changed"]
    assert [m["path"] for m in changed] == ["config/context/late.md"]
