"""The session move (0.5.156): a chat's engine session files on and off this
machine through the ``session_move`` frame, scoped to a named session."""

import asyncio
import base64
import dataclasses
import hashlib
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from satellite import config as sat_config_mod
from satellite.config import SatelliteConfig
from satellite.sessions import session_manager as smod, session_move as mv
from satellite.sessions.session_manager import SessionManager
from satellite.transport import ws_client

CLAUDE = "claude-code-cli"
CODEX = "codex-cli"


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def agents(tmp_path) -> Path:
    d = tmp_path / "agents"
    (d / "alpha" / "users" / "alice").mkdir(parents=True)
    (d / "alpha" / "workspace").mkdir(parents=True)
    return d


def _scope(engine=CLAUDE, refs=None, *, username="alice", move=True, slug="alpha"):
    msg = {"engine": engine, "agent_slug": slug, "username": username, "refs": refs or [_uuid()]}
    if move:
        msg["move_id"] = _uuid()
    return mv.parse_scope(msg)


def _push_file(agents, scope, rel, data: bytes, chunk=4):
    pieces = [data[i:i + chunk] for i in range(0, len(data), chunk)] or [b""]
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    for i, piece in enumerate(pieces):
        mv.write_chunk(agents, scope, rel, piece, index=i, total=len(pieces),
                       digest=digest if i == len(pieces) - 1 else "", max_file=1 << 20)


def _claude_rels(sid, key="-users-alice"):
    return (f".claude/projects/{key}/{sid}.jsonl",
            f".claude/projects/{key}/{sid}/subagents/agent-a1.jsonl")


# -- the scope and the names ----------------------------------------------------

@pytest.mark.parametrize("msg, why", [
    ({"engine": "nope"}, "unknown engine"),
    ({"agent_slug": "../x"}, "unsafe agent_slug"),
    ({"username": "../bob"}, "unsafe username"),
    ({"username": ".hidden"}, "unsafe username"),
    ({"refs": []}, "refs"),
    ({"refs": ["not-a-uuid"]}, "refs"),
    ({"refs": [str(uuid.uuid4()) for _ in range(3)]}, "refs"),
    ({"move_id": "x/../y"}, "unsafe move_id"),
])
def test_a_frame_naming_anything_unsafe_is_refused(msg, why):
    base = {"engine": CLAUDE, "agent_slug": "alpha", "username": "alice", "refs": [_uuid()]}
    with pytest.raises(mv.MoveRefused, match=why):
        mv.parse_scope({**base, **msg})


def test_the_home_is_the_scope_root():
    assert _scope(username="alice").home_rel == "alpha/users/alice"
    assert _scope(username="").home_rel == "alpha/workspace"


@pytest.mark.parametrize("rel", [
    "../.claude/projects/p/{sid}.jsonl", ".claude/./projects/p/{sid}.jsonl",
    ".claude//projects/p/{sid}.jsonl", ".claude/projects/p\\x/{sid}.jsonl",
    ".claude/projects/c:/{sid}.jsonl", ".claude/projects/con/{sid}.jsonl",
    ".claude/projects/p./{sid}.jsonl", ".claude/projects/p/{sid}.jsonl\x00",
    ".codex/sessions/2026/01/01/rollout-x-{sid}.jsonl",   # another engine's folder
    ".claude/settings.json", ".claude/projects/p/other.jsonl",
    ".claude/projects/p/{sid}/a/b/c/d/e.jsonl",            # too deep
    "", "/abs/{sid}.jsonl",
])
def test_a_rel_outside_the_named_session_is_refused(rel):
    sid = _uuid()
    scope = _scope(refs=[sid])
    with pytest.raises(mv.MoveRefused):
        mv.check_rel(scope, rel.format(sid=sid))


def test_the_session_file_and_its_sibling_folder_are_named():
    sid = _uuid()
    scope = _scope(refs=[sid])
    main, side = _claude_rels(sid)
    assert mv.check_rel(scope, main)[1] == "main"
    assert mv.check_rel(scope, side)[1] == "sibling"
    tid = _uuid()
    cscope = _scope(CODEX, [tid])
    assert mv.check_rel(cscope, f".codex/sessions/2026/10/19/rollout-2026-10-19T02-36-16-{tid}.jsonl")[1] == "main"
    with pytest.raises(mv.MoveRefused):  # Codex keeps no sibling folder
        mv.check_rel(cscope, f".codex/sessions/2026/10/19/rollout-x-{tid}/a.txt")


# -- push, commit, list ----------------------------------------------------------

def test_a_pushed_transcript_lands_at_its_path_and_lists_back(agents):
    sid = _uuid()
    scope = _scope(refs=[sid])
    main, side = _claude_rels(sid)
    _push_file(agents, scope, main, b'{"type":"user"}\n' * 3)
    _push_file(agents, scope, side, b"sub-agent transcript")
    placed = mv.commit(agents, scope)
    home = agents / "alpha" / "users" / "alice"
    assert (home / main).read_bytes() == b'{"type":"user"}\n' * 3
    assert (home / side).read_bytes() == b"sub-agent transcript"
    assert {p["rel"] for p in placed} == {main, side}
    assert not (agents / "alpha" / mv.STAGING_DIR).exists()
    listed = {f["rel"]: f for f in mv.list_files(agents, scope)}
    assert set(listed) == {main, side}
    assert listed[main]["sha256"] == "sha256:" + hashlib.sha256(b'{"type":"user"}\n' * 3).hexdigest()


def test_a_commit_leaves_exactly_one_copy_under_the_home(agents):
    # A second copy under another project key makes Claude's resume by id
    # fail: the commit removes it with its sibling folder.
    sid = _uuid()
    home = agents / "alpha" / "users" / "alice"
    old = home / ".claude" / "projects" / "-old-key"
    (old / sid / "tool-results").mkdir(parents=True)
    (old / f"{sid}.jsonl").write_bytes(b"stale")
    (old / sid / "tool-results" / "r.png").write_bytes(b"png")
    keep = home / ".claude" / "projects" / "-old-key" / f"{_uuid()}.jsonl"
    keep.write_bytes(b"another chat")
    scope = _scope(refs=[sid])
    main, _side = _claude_rels(sid)
    _push_file(agents, scope, main, b"fresh")
    mv.commit(agents, scope)
    assert not (old / f"{sid}.jsonl").exists() and not (old / sid).exists()
    assert keep.read_bytes() == b"another chat"
    found = [f for f, ref, _s in __import__("satellite.host.session_retention", fromlist=["x"])
             .iter_home_session_files(home) if ref == sid]
    assert len(found) == 1


def test_a_codex_rollout_keeps_its_date_path(agents):
    tid = _uuid()
    scope = _scope(CODEX, [tid], username="")
    rel = f".codex/sessions/2026/10/19/rollout-2026-10-19T02-36-16-{tid}.jsonl"
    _push_file(agents, scope, rel, b'{"type":"session_meta"}\n')
    mv.commit(agents, scope)
    assert (agents / "alpha" / "workspace" / rel).is_file()


def test_chunks_out_of_order_and_a_wrong_hash_are_refused(agents):
    sid = _uuid()
    scope = _scope(refs=[sid])
    main, _ = _claude_rels(sid)
    mv.write_chunk(agents, scope, main, b"aa", index=0, total=3, digest="", max_file=1 << 20)
    with pytest.raises(mv.MoveRefused, match="out of order"):
        mv.write_chunk(agents, scope, main, b"cc", index=2, total=3, digest="", max_file=1 << 20)
    with pytest.raises(mv.MoveRefused, match="hash mismatch"):
        mv.write_chunk(agents, scope, main, b"x", index=0, total=1, digest="sha256:00", max_file=1 << 20)
    assert not list((agents / "alpha").rglob("*.partial"))
    with pytest.raises(mv.MoveRefused, match="nothing staged"):
        mv.commit(agents, scope)


def test_a_file_over_the_cap_is_refused(agents):
    sid = _uuid()
    scope = _scope(refs=[sid])
    with pytest.raises(mv.MoveRefused, match="size cap"):
        mv.write_chunk(agents, scope, _claude_rels(sid)[0], b"x" * 10, index=0, total=1,
                       digest="", max_file=5)


def test_discard_drops_the_staging(agents):
    sid = _uuid()
    scope = _scope(refs=[sid])
    _push_file(agents, scope, _claude_rels(sid)[0], b"x")
    assert (agents / "alpha" / mv.STAGING_DIR).is_dir()
    mv.discard(agents, scope)
    assert not (agents / "alpha" / mv.STAGING_DIR).exists()


def test_delete_removes_the_session_and_its_folder_only(agents):
    sid, other = _uuid(), _uuid()
    home = agents / "alpha" / "users" / "alice"
    p = home / ".claude" / "projects" / "-k"
    (p / sid / "subagents").mkdir(parents=True)
    (p / f"{sid}.jsonl").write_bytes(b"a")
    (p / sid / "subagents" / "agent-1.jsonl").write_bytes(b"b")
    (p / f"{other}.jsonl").write_bytes(b"c")
    assert mv.delete(agents, _scope(refs=[sid], move=False)) == 2
    assert not (p / f"{sid}.jsonl").exists() and not (p / sid).exists()
    assert (p / f"{other}.jsonl").exists()


def test_a_long_path_on_the_receiver_goes_under_the_short_key(agents, monkeypatch):
    monkeypatch.setattr(mv, "LONG_PATH", 20)
    sid = _uuid()
    scope = _scope(refs=[sid])
    main, side = _claude_rels(sid, key="-home-someone-with-a-long-key")
    _push_file(agents, scope, main, b"t")
    _push_file(agents, scope, side, b"s")
    placed = {p["rel"] for p in mv.commit(agents, scope)}
    assert placed == {f".claude/projects/{mv.SHORT_KEY}/{sid}.jsonl",
                      f".claude/projects/{mv.SHORT_KEY}/{sid}/subagents/agent-a1.jsonl"}


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX links")
def test_links_are_never_followed(agents, tmp_path):
    sid = _uuid()
    home = agents / "alpha" / "users" / "alice"
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"machine secret")
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)
    # A linked project folder is not walked; a hard-linked transcript is refused.
    os.symlink(tmp_path, projects / "-linked")
    (tmp_path / f"{sid}.jsonl").write_bytes(b"x")
    assert mv.list_files(agents, _scope(refs=[sid], move=False)) == []
    real = projects / "-real"
    real.mkdir()
    os.link(secret, real / f"{sid}.jsonl")
    with pytest.raises(mv.MoveRefused, match="more than one link"):
        mv.list_files(agents, _scope(refs=[sid], move=False))


def test_an_old_staging_folder_is_swept(agents):
    root = agents / "alpha" / mv.STAGING_DIR / _uuid()
    root.mkdir(parents=True)
    then = 0.0
    os.utime(root, (then, then))
    assert mv.sweep_staging(agents, "alpha") == 1
    assert not root.exists()


def test_the_start_sweep_drops_day_old_staging_in_every_agent(agents):
    now = 100 * 86400.0
    old = agents / "alpha" / mv.STAGING_DIR / _uuid()
    fresh = agents / "beta" / mv.STAGING_DIR / _uuid()
    for d, age in ((old, mv.STAGING_MAX_AGE_S + 1), (fresh, 60.0)):
        d.mkdir(parents=True)
        os.utime(d, (now - age, now - age))
    unsafe = agents / ".hidden" / mv.STAGING_DIR / _uuid()  # not an agent slug
    unsafe.mkdir(parents=True)
    os.utime(unsafe, (0.0, 0.0))
    assert mv.sweep_all_staging(agents, now=now) == 1
    assert not old.exists()
    assert fresh.is_dir() and unsafe.is_dir()


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX links")
def test_the_start_sweep_never_follows_a_linked_agent_folder(agents, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    old = elsewhere / mv.STAGING_DIR / _uuid()
    old.mkdir(parents=True)
    os.utime(old, (0.0, 0.0))
    os.symlink(elsewhere, agents / "gamma")
    assert mv.sweep_all_staging(agents) == 0
    assert old.is_dir()


# -- the engine's own folder, the write side's links, the stat ------------------

def _plant_claude(home, sid, key="-k"):
    p = home / ".claude" / "projects" / key
    (p / sid / "subagents").mkdir(parents=True)
    (p / f"{sid}.jsonl").write_bytes(b"claude transcript")
    (p / sid / "subagents" / "agent-1.jsonl").write_bytes(b"sidecar")
    return p / f"{sid}.jsonl", p / sid / "subagents" / "agent-1.jsonl"


def _plant_codex(home, tid):
    f = home / ".codex" / "sessions" / "2026" / "10" / "19" / f"rollout-2026-10-19T02-36-16-{tid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"codex rollout")
    return f


def test_delete_under_one_engine_leaves_another_engines_files_of_the_same_id(agents):
    sid = _uuid()
    home = agents / "alpha" / "users" / "alice"
    claude_main, claude_side = _plant_claude(home, sid)
    rollout = _plant_codex(home, sid)
    assert mv.delete(agents, _scope(CODEX, [sid], move=False)) == 1
    assert not rollout.exists()
    assert claude_main.read_bytes() == b"claude transcript"
    assert claude_side.read_bytes() == b"sidecar"
    rollout = _plant_codex(home, _uuid())
    assert mv.delete(agents, _scope(CLAUDE, [sid], move=False)) == 2
    assert not claude_main.exists() and rollout.exists()


def test_a_commits_stale_pass_leaves_another_engines_files_of_the_same_id(agents):
    sid = _uuid()
    home = agents / "alpha" / "users" / "alice"
    old_main, old_side = _plant_claude(home, sid, key="-old-key")
    rollout = _plant_codex(home, sid)
    scope = _scope(refs=[sid])
    main, _side = _claude_rels(sid)
    _push_file(agents, scope, main, b"fresh")
    mv.commit(agents, scope)
    assert not old_main.exists() and not old_side.exists()  # the stale Claude copy goes
    assert rollout.read_bytes() == b"codex rollout"
    cscope = _scope(CODEX, [sid])
    rel = rollout.relative_to(home).as_posix()
    _push_file(agents, cscope, rel, b"codex fresh")
    mv.commit(agents, cscope)
    assert (home / main).read_bytes() == b"fresh"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_a_hard_link_at_the_partial_name_is_never_written_through(agents, tmp_path):
    sid = _uuid()
    scope = _scope(refs=[sid])
    main, _ = _claude_rels(sid)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes")
    staging = agents / "alpha" / mv.STAGING_DIR / scope.move_id
    partial = staging / f"{main}.partial"
    partial.parent.mkdir(parents=True)
    os.link(outside, partial)
    _push_file(agents, scope, main, b"pushed transcript")
    assert outside.read_bytes() == b"outside bytes"
    assert (staging / main).read_bytes() == b"pushed transcript"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_a_hard_link_planted_mid_push_is_refused(agents, tmp_path):
    sid = _uuid()
    scope = _scope(refs=[sid])
    main, _ = _claude_rels(sid)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes")
    mv.write_chunk(agents, scope, main, b"aa", index=0, total=2, digest="", max_file=1 << 20)
    partial = agents / "alpha" / mv.STAGING_DIR / scope.move_id / f"{main}.partial"
    partial.unlink()
    os.link(outside, partial)
    with pytest.raises(mv.MoveRefused, match="more than one link"):
        mv.write_chunk(agents, scope, main, b"bb", index=1, total=2, digest="", max_file=1 << 20)
    assert outside.read_bytes() == b"outside bytes"


def test_stat_names_the_transcripts_with_their_sizes_and_nothing_else(agents):
    sid = _uuid()
    home = agents / "alpha" / "users" / "alice"
    claude_main, _side = _plant_claude(home, sid)
    _plant_codex(home, sid)                                   # another engine's folder
    (claude_main.parent / f"{_uuid()}.jsonl").write_bytes(b"another chat")
    assert mv.OP_STAT in mv.OPS
    assert mv.stat_files(agents, _scope(refs=[sid], move=False)) == [
        {"rel": f".claude/projects/-k/{sid}.jsonl", "size": len(b"claude transcript")},
    ]
    assert mv.stat_files(agents, _scope(refs=[_uuid()], move=False)) == []


# -- the session manager and the link -------------------------------------------

@pytest.fixture
def sat(tmp_path, agents):
    return SatelliteConfig(machine_id="m", machine_secret="s",
                           platform_url="ws://localhost/v1/satellite",
                           agents_dir=agents, mcps_dir=tmp_path / "mcps")


class _WS:
    def __init__(self):
        self.sent = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)

    async def enqueue_bulk(self, msg):
        self.sent.append(msg)
        return True


def _frame(op, scope_refs, **kw):
    return {"type": "session_move", "op": op, "engine": CLAUDE, "agent_slug": "alpha",
            "username": "alice", "refs": scope_refs, **kw}


def test_the_capability_is_advertised(sat):
    assert SessionManager(sat).detect_capabilities()["session_move"] is True


@pytest.mark.asyncio
async def test_push_commit_list_through_the_manager(sat):
    sm = SessionManager(sat)
    ws = _WS()
    sid, move = _uuid(), _uuid()
    main, _ = _claude_rels(sid)
    data = b'{"type":"user"}\n'
    ack = sm.session_move_push(_frame("push", [sid], move_id=move, rel=main, command_id="c1",
                                      content_b64=base64.b64encode(data).decode(),
                                      chunk_index=0, total_chunks=1,
                                      hash="sha256:" + hashlib.sha256(data).hexdigest()))
    assert ack == {"type": "ack", "command_id": "c1", "status": "ok"}
    await sm.session_move(_frame("commit", [sid], move_id=move, command_id="c2"), ws)
    await sm.session_move(_frame("list", [sid], command_id="c3"), ws)
    assert [a["status"] for a in ws.sent] == ["ok", "ok"]
    assert ws.sent[1]["files"][0]["rel"] == main


@pytest.mark.asyncio
async def test_a_live_session_holding_an_id_makes_the_move_busy(sat):
    sm = SessionManager(sat)
    ws = _WS()
    tid = _uuid()
    sm.sessions["s1"] = types.SimpleNamespace(session_id="s1", thread_id=tid)
    for op in ("list", "commit", "delete"):
        await sm.session_move(_frame(op, [tid], move_id=_uuid(), command_id=op), ws)
    assert [a["status"] for a in ws.sent] == ["rejected"] * 3
    assert all("busy" in a["error"] for a in ws.sent)
    await sm.session_move_pull(_frame("pull", [tid], request_id="r1",
                                      rel=f".claude/projects/k/{tid}.jsonl"), ws)
    assert ws.sent[-1]["type"] == "file_content" and "busy" in ws.sent[-1]["error"]


@pytest.mark.asyncio
async def test_stat_is_answered_while_a_live_session_holds_the_id(sat, agents):
    sm = SessionManager(sat)
    ws = _WS()
    sid = _uuid()
    f = agents / "alpha" / "users" / "alice" / ".claude" / "projects" / "k" / f"{sid}.jsonl"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"growing")
    sm.sessions["s1"] = types.SimpleNamespace(session_id=sid)
    assert sm._session_holds([sid])
    await sm.session_move(_frame("stat", [sid], command_id="c"), ws)
    assert ws.sent == [{"type": "ack", "command_id": "c", "status": "ok",
                        "files": [{"rel": f".claude/projects/k/{sid}.jsonl", "size": 7}]}]


@pytest.mark.asyncio
async def test_a_pull_of_a_path_outside_the_session_is_refused(sat):
    sm = SessionManager(sat)
    ws = _WS()
    await sm.session_move_pull(_frame("pull", [_uuid()], request_id="r1",
                                      rel=".claude/.credentials.json"), ws)
    assert ws.sent == [{"type": "file_content", "request_id": "r1",
                        "path": ".claude/.credentials.json",
                        "error": "refused: rel is not a file of this session"}]


@pytest.mark.asyncio
async def test_a_pull_streams_the_file(sat, agents):
    sm = SessionManager(sat)
    ws = _WS()
    sid = _uuid()
    f = agents / "alpha" / "users" / "alice" / ".claude" / "projects" / "k" / f"{sid}.jsonl"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"hello")
    await sm.session_move_pull(_frame("pull", [sid], request_id="r2",
                                      rel=f".claude/projects/k/{sid}.jsonl"), ws)
    chunks = [m for m in ws.sent if m.get("type") == "file_content"]
    assert chunks and "error" not in chunks[-1]
    assert base64.b64decode(chunks[0]["content_b64"]) == b"hello"


@pytest.mark.asyncio
async def test_an_unknown_op_is_rejected(sat):
    ws = _WS()
    await SessionManager(sat).session_move(_frame("teleport", [_uuid()], command_id="c"), ws)
    assert ws.sent[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_the_link_routes_the_frame_inline(monkeypatch):
    calls = []

    class _SM:
        _pulls: dict = {}

        async def session_move(self, msg, client):
            calls.append(("session_move", msg["op"]))

        async def session_move_pull(self, msg, client):
            calls.append(("pull", msg["op"]))
            await asyncio.sleep(0)

        def session_move_push(self, msg):
            calls.append(("push", msg["op"]))
            return {"type": "ack", "command_id": msg.get("command_id"), "status": "ok"}

    c = ws_client.SatelliteWSClient(types.SimpleNamespace(), _SM())
    sent = []

    async def _send(m):
        sent.append(m)

    monkeypatch.setattr(c, "enqueue_send", _send)
    await c._dispatch_frame("session_move", {"op": "pull", "request_id": "r"})
    assert "r" in c.sm._pulls  # registered before the next frame is read
    await c._dispatch_frame("session_move", {"op": "push", "command_id": "p"})
    assert sent == [{"type": "ack", "command_id": "p", "status": "ok"}]  # answered inline
    await c._dispatch_frame("session_move", {"op": "list"})
    for _ in range(3):
        await asyncio.sleep(0)
    assert ("push", "push") in calls and ("session_move", "list") in calls and ("pull", "pull") in calls


# -- the hardened file_push refusal and the resume probe -------------------------

@pytest.mark.parametrize("rel", [
    "users/alice/./.claude/projects/p/x.jsonl",
    "users/alice//.claude/settings.json",
    "workspace/.codex/sessions/2026/01/01/r.jsonl",
])
def test_canonical_engine_home_paths_are_caught(rel, agents):
    assert smod._under_engine_home(rel, "agent_tree", agents)


def test_the_case_fold_applies_where_the_filesystem_folds(agents, monkeypatch):
    rel = "users/alice/.CLAUDE/projects/p/x.jsonl"
    monkeypatch.setattr(sat_config_mod, "HOST", dataclasses.replace(sat_config_mod.HOST, case_insensitive=False))
    assert not smod._under_engine_home(rel, "agent_tree", agents)
    monkeypatch.setattr(sat_config_mod, "HOST", dataclasses.replace(sat_config_mod.HOST, case_insensitive=True))
    assert smod._under_engine_home(rel, "agent_tree", agents)


def test_a_host_path_into_an_engine_home_is_caught(agents):
    p = str(agents.resolve() / "alpha" / "users" / "alice" / ".claude" / "projects" / "k" / "x.jsonl")
    assert smod._under_engine_home(p, "satellite_host", agents)
    assert not smod._under_engine_home("/tmp/elsewhere/x.txt", "satellite_host", agents)


def _windows(monkeypatch):
    monkeypatch.setattr(sat_config_mod, "HOST", dataclasses.replace(
        sat_config_mod.HOST, name=sat_config_mod.WINDOWS, case_insensitive=True))


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX links")
def test_a_host_path_is_compared_canonical(agents, tmp_path):
    tail = ("alpha", "users", "u", ".claude", "projects", "k", "x.jsonl")
    canonical = agents.resolve().joinpath(*tail)
    alias = tmp_path / "alias"
    os.symlink(agents, alias)
    assert smod._under_engine_home(str(canonical), "satellite_host", agents)
    assert smod._under_engine_home(str(alias.joinpath(*tail)), "satellite_host", agents)
    assert smod._under_engine_home(str(canonical), "satellite_host", alias)
    # A link inside the home that names the engine folder is caught resolved.
    home = agents / "alpha" / "users" / "alice"
    (home / ".claude").mkdir()
    os.symlink(home / ".claude", home / "notes")
    assert smod._under_engine_home(str(home / "notes" / "projects" / "k" / "x.jsonl"),
                                   "satellite_host", agents)
    assert not smod._under_engine_home(str(home / ".claude" / "skills" / "s" / "SKILL.md"),
                                       "satellite_host", agents)


@pytest.mark.parametrize("rel", [
    "users/u/.claude./projects/k/x.jsonl",
    "users/u/.claude /projects/k/x.jsonl",
    "users/u/.claude. ./projects/k/x.jsonl",
    "users/u/ . /.codex/sessions/2026/01/01/r.jsonl",
])
def test_windows_drops_trailing_dots_and_spaces_first(rel, agents, monkeypatch):
    monkeypatch.setattr(sat_config_mod, "HOST", dataclasses.replace(
        sat_config_mod.HOST, name=sat_config_mod.LINUX, case_insensitive=False))
    assert not smod._under_engine_home(rel, "agent_tree", agents)  # distinct names off Windows
    _windows(monkeypatch)
    assert smod._under_engine_home(rel, "agent_tree", agents)
    host = str(agents.resolve() / "alpha") + "/" + rel
    assert smod._under_engine_home(host, "satellite_host", agents)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug, sid, user", [
    ("..", "sess-1", "alice"), ("alpha", "../../etc/passwd", "alice"), ("alpha", "sess-1", "../.."),
])
async def test_the_resume_probe_refuses_unsafe_names(sat, slug, sid, user):
    ws = _WS()
    await SessionManager(sat).check_session_resumable(
        {"command_id": "c", "session_id": sid, "agent_slug": slug, "username": user}, ws)
    assert ws.sent[0]["resumable"] is False and ws.sent[0]["reason"] == "missing"
