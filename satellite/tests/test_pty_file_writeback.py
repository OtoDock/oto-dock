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
import logging
import os
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


def _transcript(sess, rel: str, body: bytes = b""):
    """A transcript at ``rel`` beneath the session's agent folder, located."""
    path = sess.agent_dir.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    sess._transcript_path = path
    sess._transcript_rel = rel
    return path


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


@pytest.mark.asyncio
async def test_lines_past_the_last_ack_go_again_at_the_next_sign_in(tmp_path):
    """A machine paused past the platform's patience sends its first lines on
    a link that is already gone: the platform acks what it took, and the next
    sign-in sends the rest again. A platform that never acks gets no line twice."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    transcript = _transcript(sess, "t.jsonl", b'{"a": 1}\n')
    assert await sess._forward_transcript_delta()
    first_end = ws.sent[-1]["offset"]
    assert ws.sent[-1]["lines"] == ['{"a": 1}'] and first_end == transcript.stat().st_size
    # No ack yet: the platform may be one that never acks, so no rewind.
    sess.rewind_transcript()
    assert not await sess._forward_transcript_delta()
    sess.note_transcript_ack(first_end)
    with transcript.open("a") as fh:
        fh.write('{"b": 2}\n')
    assert await sess._forward_transcript_delta()      # sent on a link that was gone
    sess.rewind_transcript()
    assert await sess._forward_transcript_delta()
    assert ws.sent[-1]["lines"] == ['{"b": 2}']
    # An ack past what was read is held at what was read.
    sess.note_transcript_ack(10 ** 9)
    sess.rewind_transcript()
    assert not await sess._forward_transcript_delta()


@pytest.mark.asyncio
async def test_lines_before_the_first_ack_go_again_when_the_platform_acks(tmp_path, caplog):
    """A terminal frozen in its first second sends its whole first turn on a
    link that is already gone, before the platform acked anything of it. A
    platform that says it acks (``transcript_acks``) gets
    those lines again from the terminal's start; one that does not say so is
    never sent a line twice."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    transcript = _transcript(sess, "t.jsonl", b'{"a": 1}\n{"b": 2}\n')
    assert await sess._forward_transcript_delta()      # lost on the old link
    assert not sess.rewind_transcript()
    assert not await sess._forward_transcript_delta()
    with caplog.at_level(logging.INFO, logger="satellite"):
        assert sess.rewind_transcript(platform_acks=True)
    assert "18 bytes" in caplog.text
    assert await sess._forward_transcript_delta()
    assert ws.sent[-1]["lines"] == ['{"a": 1}', '{"b": 2}']
    assert ws.sent[-1]["offset"] == transcript.stat().st_size
    # Acked: the rule of every acked terminal, whatever the platform says.
    sess.note_transcript_ack(ws.sent[-1]["offset"])
    assert not sess.rewind_transcript(platform_acks=True)


@pytest.mark.asyncio
async def test_a_resumed_terminal_goes_again_only_from_the_end_of_its_history(tmp_path):
    """A resumed terminal's history is already in the chat: with no ack yet
    it goes again from the end of that history, never from the file's start."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    sess._transcript_skip_existing = True
    sess._locate_transcript_file = lambda: "t.jsonl"
    path = sess.agent_dir / "t.jsonl"
    path.write_bytes(b'{"old": 1}\n')
    assert not await sess._forward_transcript_delta()  # seeks past the history
    with path.open("ab") as fh:
        fh.write(b'{"new": 2}\n')
    assert await sess._forward_transcript_delta()
    assert sess.rewind_transcript(platform_acks=True)
    assert await sess._forward_transcript_delta()
    assert ws.sent[-1]["lines"] == ['{"new": 2}']


@pytest.mark.asyncio
async def test_a_terminal_sent_again_drops_its_queued_lines_and_reads_at_once(tmp_path):
    """The lines a terminal queued while its link was down would go at the
    sign-in, and the rewind would send them again: a Codex row twice. A
    terminal that rewinds drops them first and reads at once, so its lines go
    right behind the terminal report, once. One that does not rewind keeps them."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    _transcript(sess, "t.jsonl", b'{"a": 1}\n')
    assert await sess._forward_transcript_delta()
    dropped = []
    await sess.send_again(False, lambda: dropped.append(1))
    assert dropped == [] and len(ws.sent) == 1
    await sess.send_again(True, lambda: dropped.append(1))
    assert dropped == [1]
    assert [m["lines"] for m in ws.sent] == [['{"a": 1}'], ['{"a": 1}']]


@pytest.mark.asyncio
async def test_an_ack_reaches_its_own_open_only(tmp_path):
    sm = SessionManager.__new__(SessionManager)
    sm.pty_sessions = {}
    sess = _make_pty_session(tmp_path, _CapturingWS())
    sess.open_token = "aa11"
    sess._transcript_offset = 50
    sm.pty_sessions["sess-1"] = sess
    await sm.transcript_ack({"session_id": "sess-1", "offset": 30, "open_token": "zz99"})
    assert not sess._acks_seen
    await sm.transcript_ack({"session_id": "sess-1", "offset": True})
    assert not sess._acks_seen
    await sm.transcript_ack({"session_id": "sess-1", "offset": 30, "open_token": "aa11"})
    assert sess._acks_seen and sess._acked_offset == 30


@pytest.mark.asyncio
async def test_the_transcript_is_read_beneath_the_agent_folder_never_through_a_link(tmp_path):
    """Each poll opens the located transcript beneath the agent's folder: a
    project folder or a transcript name that became a link, or a pipe at the
    name, gives no line (and never blocks)."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    path = _transcript(sess, "workspace/.claude/projects/-p/s.jsonl", b'{"a": 1}\n')
    assert await sess._forward_transcript_delta()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.jsonl").write_bytes(b'{"a": 1}\n{"stand-in": "outside"}\n')
    project = path.parent
    path.unlink()
    project.rmdir()
    project.symlink_to(outside, target_is_directory=True)
    assert not await sess._forward_transcript_delta()
    project.unlink()
    project.mkdir()
    (project / "s.jsonl").symlink_to(outside / "s.jsonl")
    assert not await sess._forward_transcript_delta()
    (project / "s.jsonl").unlink()
    os.mkfifo(project / "s.jsonl")
    assert not await sess._forward_transcript_delta()
    assert [m["lines"] for m in ws.sent] == [['{"a": 1}']]


@pytest.mark.asyncio
async def test_a_line_longer_than_a_polls_cap_is_skipped_whole(tmp_path, caplog):
    """A poll reads at most 15 MiB (the frame stops at 16): a longer line is
    read past to its newline and never sent, with one log line; the lines
    around it go as before."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    big = b'{"big": "' + b"x" * (16 * 1024 * 1024) + b'"}\n'
    path = _transcript(sess, "t.jsonl", b'{"a": 1}\n' + big + b'{"b": 2}\n')
    with caplog.at_level(logging.INFO, logger="satellite"):
        for _ in range(6):
            await sess._forward_transcript_delta()
    assert [m["lines"] for m in ws.sent] == [['{"a": 1}'], ['{"b": 2}']]
    assert ws.sent[-1]["offset"] == path.stat().st_size
    assert len([r for r in caplog.records if "skipped" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_the_claude_terminal_finds_its_transcript_in_a_real_project_folder(tmp_path, monkeypatch):
    """The ``projects`` folder is listed beneath the agent's folder: a
    project folder that is a link is not searched, a transcript name that is
    a link is not taken, a listing past its cap finds nothing."""
    from satellite.terminal import pty_session
    agent_dir = tmp_path / "agents" / "a1"
    claude = agent_dir / "workspace" / ".claude"
    (claude / "projects").mkdir(parents=True)
    ws = _CapturingWS()
    sess = pty_session.PtySession("s-1", agent_dir, {"claude_dir_relative": "workspace/.claude"},
                                  SimpleNamespace(), ws)
    sess._claude_dir = claude
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s-1.jsonl").write_text('{"stand-in": 1}\n')
    (claude / "projects" / "-a-linked").symlink_to(outside, target_is_directory=True)
    (claude / "projects" / "-b-real").mkdir()
    (claude / "projects" / "-b-real" / "s-1.jsonl").symlink_to(outside / "s-1.jsonl")
    assert sess._locate_transcript_file() is None
    (claude / "projects" / "-c-real").mkdir()
    (claude / "projects" / "-c-real" / "s-1.jsonl").write_text('{"a": 1}\n')
    assert sess._locate_transcript_file() == "workspace/.claude/projects/-c-real/s-1.jsonl"
    assert await sess._forward_transcript_delta()
    assert ws.sent[-1]["lines"] == ['{"a": 1}']
    assert sess._transcript_path == claude / "projects" / "-c-real" / "s-1.jsonl"
    monkeypatch.setattr(pty_session, "_PROJECTS_MAX_ENTRIES", 2)
    assert sess._locate_transcript_file() is None


@pytest.mark.asyncio
async def test_the_close_reads_what_is_left_a_polls_cap_at_a_time(tmp_path, monkeypatch):
    from satellite.terminal import pty_session_base as mod
    monkeypatch.setattr(mod, "_TRANSCRIPT_POLL_MAX_BYTES", 20)
    monkeypatch.setattr("satellite.sessions.session_files.wipe", lambda _sid: None)
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    _transcript(sess, "t.jsonl", b"".join(b'{"n": %d}\n' % i for i in range(5)))
    await sess.close()
    lines = [ln for m in ws.sent if m["type"] == "transcript_lines" for ln in m["lines"]]
    assert lines == [f'{{"n": {i}}}' for i in range(5)]


@pytest.mark.asyncio
async def test_a_rewind_inside_a_skipped_line_sends_the_lines_before_it_again(tmp_path, monkeypatch):
    from satellite.terminal import pty_session_base as mod
    monkeypatch.setattr(mod, "_TRANSCRIPT_POLL_MAX_BYTES", 32)
    monkeypatch.setattr(mod, "_TRANSCRIPT_SKIP_CHUNK", 8)
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    _transcript(sess, "t.jsonl", b'{"a": 1}\n' + b"x" * 100 + b'\n{"b": 2}\n')
    assert await sess._forward_transcript_delta()
    sess.note_transcript_ack(0)
    assert not await sess._forward_transcript_delta()  # into the long line
    sess.rewind_transcript()
    assert await sess._forward_transcript_delta()
    assert ws.sent[-1]["lines"] == ['{"a": 1}']


@pytest.mark.asyncio
async def test_a_late_ack_while_a_long_line_is_skipped_never_lands_inside_it(tmp_path, monkeypatch):
    """An ack that arrives while a long line is read past (a frame sent
    before the link dropped) is held at that line's start: no later sign-in
    sends a piece of the long line as a line."""
    from satellite.terminal import pty_session_base as mod
    monkeypatch.setattr(mod, "_TRANSCRIPT_POLL_MAX_BYTES", 32)
    monkeypatch.setattr(mod, "_TRANSCRIPT_SKIP_CHUNK", 8)
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    _transcript(sess, "t.jsonl", b'{"a": 1}\n' + b"x" * 100 + b'\n{"b": 2}\n')
    for _ in range(8):
        await sess._forward_transcript_delta()
    assert [m["lines"] for m in ws.sent] == [['{"a": 1}'], ['{"b": 2}']]
    b_end = ws.sent[-1]["offset"]
    sess.note_transcript_ack(ws.sent[0]["offset"])     # only the first frame was taken
    sess.rewind_transcript()
    assert not await sess._forward_transcript_delta()  # back inside the long line
    sess.note_transcript_ack(b_end)                     # the late ack of the frame sent before the drop
    assert sess._acked_offset == ws.sent[0]["offset"]
    sess.rewind_transcript()
    for _ in range(8):
        await sess._forward_transcript_delta()
    assert all(ln in ('{"a": 1}', '{"b": 2}') for m in ws.sent for ln in m["lines"]), ws.sent


@pytest.mark.asyncio
async def test_a_linked_projects_folder_is_said_once(tmp_path, caplog):
    """A link below the agent's folder stops the terminal's history: the
    search says so once a session, naming the folder."""
    from satellite.terminal import pty_session
    agent_dir = tmp_path / "agents" / "a1"
    claude = agent_dir / "workspace" / ".claude"
    claude.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "-p").mkdir(parents=True)
    (outside / "-p" / "s-1.jsonl").write_text('{"a": 1}\n')
    (claude / "projects").symlink_to(outside, target_is_directory=True)
    sess = pty_session.PtySession("s-1", agent_dir, {"claude_dir_relative": "workspace/.claude"},
                                  SimpleNamespace(), _CapturingWS())
    sess._claude_dir = claude
    with caplog.at_level(logging.WARNING):
        assert sess._locate_transcript_file() is None
        assert sess._locate_transcript_file() is None
    said = [r.getMessage() for r in caplog.records if "link" in r.getMessage()]
    assert len(said) == 1 and "workspace/.claude/projects" in said[0]


@pytest.mark.asyncio
async def test_a_linked_project_folder_is_said_once(tmp_path, caplog):
    from satellite.terminal import pty_session
    agent_dir = tmp_path / "agents" / "a1"
    claude = agent_dir / "workspace" / ".claude"
    (claude / "projects").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s-1.jsonl").write_text('{"a": 1}\n')
    (claude / "projects" / "-p").symlink_to(outside, target_is_directory=True)
    sess = pty_session.PtySession("s-1", agent_dir, {"claude_dir_relative": "workspace/.claude"},
                                  SimpleNamespace(), _CapturingWS())
    sess._claude_dir = claude
    with caplog.at_level(logging.WARNING):
        assert sess._locate_transcript_file() is None
        assert sess._locate_transcript_file() is None
    said = [r.getMessage() for r in caplog.records if "link" in r.getMessage()]
    assert len(said) == 1 and "workspace/.claude/projects/-p" in said[0]


@pytest.mark.asyncio
async def test_a_frame_holds_the_lines_that_fit_once_encoded(tmp_path, monkeypatch, caplog):
    """A frame is filled by the lines' encoded size (Greek text grows about
    threefold once escaped), its offset at the end of the last line taken;
    the rest goes in the next polls, and a line that alone encodes past the
    frame is passed over with one log line."""
    import json

    from satellite.terminal import pty_session_base as mod
    monkeypatch.setattr(mod, "_TRANSCRIPT_FRAME_MAX_BYTES", 200)
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    small = [json.dumps({"n": i, "t": "αβγ"}, ensure_ascii=False) for i in range(8)]
    huge = json.dumps({"t": "λ" * 60}, ensure_ascii=False)   # 130 bytes raw, over 200 once escaped
    body = "\n".join(small[:4] + [huge] + small[4:]) + "\n"
    path = _transcript(sess, "t.jsonl", body.encode("utf-8"))
    with caplog.at_level(logging.INFO, logger="satellite"):
        for _ in range(10):
            await sess._forward_transcript_delta()
    frames = [m for m in ws.sent if m["type"] == "transcript_lines"]
    assert len(frames) > 1
    assert all(len(json.dumps(m["lines"])) <= 200 for m in frames)
    assert [ln for m in frames for ln in m["lines"]] == small
    assert frames[-1]["offset"] == path.stat().st_size
    for m in frames:
        assert path.read_bytes()[: m["offset"]].endswith(b"\n")
    assert len([r for r in caplog.records if "encoded" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_an_ack_while_a_long_line_is_skipped_reaches_that_lines_start(tmp_path, monkeypatch):
    """The lines before a line being read past were sent: their ack is taken
    in full, up to where the long line starts."""
    from satellite.terminal import pty_session_base as mod
    monkeypatch.setattr(mod, "_TRANSCRIPT_POLL_MAX_BYTES", 32)
    monkeypatch.setattr(mod, "_TRANSCRIPT_SKIP_CHUNK", 8)
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    _transcript(sess, "t.jsonl", b'{"a": 1}\n' + b"x" * 100 + b'\n{"b": 2}\n')
    assert await sess._forward_transcript_delta()
    assert not await sess._forward_transcript_delta()
    assert sess._transcript_skipping
    sess.note_transcript_ack(ws.sent[0]["offset"])
    assert sess._acked_offset == ws.sent[0]["offset"]


@pytest.mark.asyncio
async def test_a_transcript_that_shrank_drops_its_old_ack(tmp_path):
    """A file replaced by a shorter one: the old ack names no line's start in
    the new file, so it is dropped, and the next sign-in never sends the tail
    of a line as a line."""
    ws = _CapturingWS()
    sess = _make_pty_session(tmp_path, ws)
    first = b"".join(b'{"n": %d, "long": "%s"}\n' % (i, b"q" * 40) for i in range(6))
    path = _transcript(sess, "t.jsonl", first)
    assert await sess._forward_transcript_delta()
    sess.note_transcript_ack(ws.sent[-1]["offset"])
    path.write_bytes(b'{"a": 1}\n')                 # shorter: the CLI started the file again
    assert await sess._forward_transcript_delta()
    with path.open("ab") as fh:
        fh.write(b"".join(b'{"m": %d, "long": "%s"}\n' % (i, b"r" * 40) for i in range(6)))
    assert await sess._forward_transcript_delta()
    sess.rewind_transcript()
    await sess._forward_transcript_delta()
    import json
    for m in ws.sent:
        for line in m["lines"]:
            json.loads(line)


@pytest.mark.asyncio
async def test_the_sign_in_sends_a_terminals_unacked_lines_behind_its_report(tmp_path, monkeypatch):
    """The sign-in answer's ``transcript_acks`` reaches each terminal: its
    lines queued from before go, read again, right behind ``pty_alive``.
    Without the word, the queued lines go as they are and nothing goes twice."""
    import json

    from satellite.host import cli_versions
    from satellite.transport import ws_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ws_client, "_finalize_post_update_state", lambda *a: None)
    monkeypatch.setattr(cli_versions, "set_pins", lambda *a: None)
    cfg = SatelliteConfig(
        machine_id="m", machine_secret="s", platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps",
    )

    def _answering(**fields):
        class _Socket:
            async def send(self, text):
                pass

            async def recv(self):
                return json.dumps({"type": "auth_result", "status": "ok", **fields})
        return _Socket()

    def _queued(client):
        out = []
        while not client._send_queue.empty():
            out.append(client._send_queue.get_nowait())
        return [(m["type"], m.get("lines")) for m in out
                if m["type"] in ("pty_alive", "transcript_lines")]

    for fields, expected in (
        ({"transcript_acks": True}, [("pty_alive", None), ("transcript_lines", ['{"a": 1}'])]),
        ({}, [("transcript_lines", ['{"a": 1}']), ("pty_alive", None)]),
    ):
        client = ws_client.SatelliteWSClient(cfg, SessionManager(cfg))
        sess = _make_pty_session(tmp_path / str(len(fields)), client)
        _transcript(sess, "t.jsonl", b'{"a": 1}\n')
        client.sm.pty_sessions["sess-1"] = sess
        assert await sess._forward_transcript_delta()  # queued while the link was down
        client._ws = _answering(**fields)
        await client._authenticate()
        assert _queued(client) == expected


@pytest.mark.asyncio
async def test_a_terminal_that_fails_its_send_again_leaves_the_others_and_the_sign_in(caplog):
    """The sign-in reads each rewound terminal's transcript: one that raises
    is logged and passed over, so it never ends the sign-in, and the next
    terminal still goes again."""
    from satellite.transport import ws_client
    client = ws_client.SatelliteWSClient(SimpleNamespace(), SimpleNamespace())
    done = []

    class _Broken:
        async def send_again(self, platform_acks, drop_queued):
            raise RuntimeError("unreadable")

    class _Fine:
        async def send_again(self, platform_acks, drop_queued):
            done.append(platform_acks)

    with caplog.at_level(logging.WARNING, logger="satellite"):
        await client._send_terminals_again({"aaaa1111": _Broken(), "bbbb2222": _Fine()}, True)
    assert done == [True]
    assert "aaaa1111" in caplog.text
