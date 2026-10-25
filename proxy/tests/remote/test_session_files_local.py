"""A chat's engine session files on the platform's own tree and between
places (``core/remote/session_files``): the no-link read, the staging, the
commit that leaves one copy, the copy's re-list and its compare."""

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import pytest

import config
from core.remote import session_files as sf

CLAUDE = "claude-code-cli"
CODEX = "codex-cli"


def _scope(agent="sfa", user="alice", sid=None, thread=""):
    return sf.SessionScope(agent=agent, engine=CLAUDE if not thread else CODEX, home_user=user,
                           session_id=sid or str(uuid.uuid4()), resume_handle=thread)


def _home(scope) -> Path:
    h = Path(config.AGENTS_DIR) / scope.home_rel
    h.mkdir(parents=True, exist_ok=True)
    return h


def _transcript(scope, key="-users-alice", body=b'{"type":"user"}\n', side=True):
    d = _home(scope) / ".claude" / "projects" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{scope.session_id}.jsonl").write_bytes(body)
    if side:
        (d / scope.session_id / "subagents").mkdir(parents=True, exist_ok=True)
        (d / scope.session_id / "subagents" / "agent-a.jsonl").write_bytes(b"sub")
    return d


def test_the_session_files_are_listed_relative_to_the_home():
    s = _scope()
    _transcript(s)
    files = {f.rel: f for f in sf.local_list(s)}
    assert set(files) == {f".claude/projects/-users-alice/{s.session_id}.jsonl",
                          f".claude/projects/-users-alice/{s.session_id}/subagents/agent-a.jsonl"}
    main = files[f".claude/projects/-users-alice/{s.session_id}.jsonl"]
    assert main.sha256 == "sha256:" + hashlib.sha256(b'{"type":"user"}\n').hexdigest()


@pytest.mark.skipif(os.name == "nt", reason="POSIX links")
def test_a_linked_or_doubly_linked_file_is_never_read(tmp_path):
    s = _scope()
    projects = _home(s) / ".claude" / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret"
    secret.write_bytes(b"the platform's key")
    elsewhere = tmp_path / "proj"
    elsewhere.mkdir()
    (elsewhere / f"{s.session_id}.jsonl").write_bytes(b"x")
    os.symlink(elsewhere, projects / "-linked")
    assert sf.local_list(s) == []
    real = projects / "-real"
    real.mkdir()
    os.link(secret, real / f"{s.session_id}.jsonl")
    assert sf.local_list(s) == []


def test_ids_that_are_not_uuids_never_reach_a_glob():
    s = sf.SessionScope(agent="sfa", engine=CLAUDE, home_user="alice",
                        session_id="../*", resume_handle="")
    assert s.refs == [] and sf.local_list(s) == []


@pytest.mark.parametrize("rel", ["../x", ".claude/../.ssh/id", ".claude//x", "/abs",
                                 ".claude/a\\b", ".claude/c:x", ".codex/sessions/x",
                                 ".claude/con/x", ".claude/x."])
def test_a_rel_a_place_answers_is_checked(rel):
    with pytest.raises(sf.TransferError):
        sf.check_rel(_scope(), rel)


def test_a_commit_puts_the_staging_in_place_and_leaves_one_copy():
    s = _scope()
    old = _transcript(s, key="-old-key", body=b"stale")
    move_id = str(uuid.uuid4())
    for rel, body in ((f".claude/projects/-home-x/{s.session_id}.jsonl", b"fresh"),
                      (f".claude/projects/-home-x/{s.session_id}/subagents/agent-a.jsonl", b"sub")):
        p = sf.staged_path(s, move_id, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    placed = {f.rel for f in sf.local_commit(s, move_id)}
    home = _home(s)
    assert (home / ".claude/projects/-home-x" / f"{s.session_id}.jsonl").read_bytes() == b"fresh"
    assert not (old / f"{s.session_id}.jsonl").exists()
    assert f".claude/projects/-home-x/{s.session_id}.jsonl" in placed
    assert not (Path(config.AGENTS_DIR) / s.agent / sf.STAGING_DIR / move_id).exists()


def test_a_commit_drops_what_the_engine_does_not_name():
    s = _scope()
    move_id = str(uuid.uuid4())
    for rel, body in ((f".claude/projects/k/{s.session_id}.jsonl", b"t"),
                      (".claude/settings.json", b"{}")):
        p = sf.staged_path(s, move_id, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    sf.local_commit(s, move_id)
    assert not (_home(s) / ".claude" / "settings.json").exists()


def test_delete_removes_the_session_files():
    s = _scope()
    d = _transcript(s)
    assert sf.local_delete(s) == 2
    assert not (d / f"{s.session_id}.jsonl").exists()
    assert not (d / s.session_id).exists()          # the folder beside it goes too


def test_a_commit_removes_the_old_copys_folder():
    s = _scope()
    old = _transcript(s, key="-old-key", body=b"stale")
    move_id = str(uuid.uuid4())
    p = sf.staged_path(s, move_id, f".claude/projects/-new/{s.session_id}.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fresh")
    sf.local_commit(s, move_id)
    assert not (old / s.session_id).exists()


def test_the_proof_reads_sizes_of_the_session_file_only():
    s = _scope()
    _transcript(s, body=b"12345")
    assert sf.local_stat(s) == [(f".claude/projects/-users-alice/{s.session_id}.jsonl", 5)]
    rel = f".claude/projects/-users-alice/{s.session_id}.jsonl"
    assert asyncio.run(sf.main_size("local", s, rel)) == 5


def test_the_staging_sweep_drops_old_moves_and_never_follows_a_link(tmp_path):
    import time
    s = _scope(agent="sweep-a")
    root = Path(config.AGENTS_DIR) / s.agent / sf.STAGING_DIR
    old, fresh = root / str(uuid.uuid4()), root / str(uuid.uuid4())
    for d in (old, fresh):
        (d / ".claude").mkdir(parents=True)
        (d / ".claude" / "x").write_bytes(b"x")
    past = time.time() - 2 * 86400
    os.utime(old, (past, past))
    outside = tmp_path / "keep"
    (outside / "inner").mkdir(parents=True)
    linked = Path(config.AGENTS_DIR) / "sweep-linked"
    linked.mkdir(exist_ok=True)
    if os.name != "nt":
        os.symlink(outside, linked / sf.STAGING_DIR)
        os.utime(outside / "inner", (past, past))
    assert sf.sweep_staging() >= 1
    assert not old.exists() and fresh.exists()
    assert (outside / "inner").exists()


def test_main_is_the_file_named_by_an_id():
    s = _scope(thread=str(uuid.uuid4()))
    assert sf.is_main(s, f".codex/sessions/2026/10/19/rollout-2026-10-19T00-00-00-{s.resume_handle}.jsonl")
    assert not sf.is_main(s, f".claude/projects/k/{s.session_id}/subagents/agent-a.jsonl")


def _read(path) -> bytes:
    return Path(path).read_bytes()


class _Remote:
    """The satellite's side of the copy, in memory."""

    def __init__(self, monkeypatch, files: dict[str, bytes]):
        self.files = dict(files)
        self.staged: dict[str, bytes] = {}
        self.mutate_after_push = False

        def entry(rel, data):
            return sf.SessionFile(rel, len(data), "sha256:" + hashlib.sha256(data).hexdigest())

        async def remote_list(machine_id, scope):
            return [entry(r, d) for r, d in self.files.items()]

        async def remote_pull(machine_id, scope, move_id, f, *, progress=None):
            dest = sf.staged_path(scope, move_id, f.rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.files[f.rel])
            if progress is not None:     # two chunks, as the receive loop reports them
                progress(f.size // 2, f.size)
                progress(f.size, f.size)
            return dest

        async def remote_push(machine_id, scope, move_id, rel, src, digest, *, progress=None):
            self.staged[rel] = _read(src)
            if progress is not None:
                progress(len(self.staged[rel]), len(self.staged[rel]))

        async def remote_commit(machine_id, scope, move_id):
            self.files.update(self.staged)
            return [entry(r, d) for r, d in self.staged.items()]

        monkeypatch.setattr(sf, "remote_list", remote_list)
        monkeypatch.setattr(sf, "remote_pull", remote_pull)
        monkeypatch.setattr(sf, "remote_push", remote_push)
        monkeypatch.setattr(sf, "remote_commit", remote_commit)


def test_a_copy_from_the_platform_to_a_machine(monkeypatch):
    s = _scope()
    _transcript(s)
    remote = _Remote(monkeypatch, {})
    res = asyncio.run(sf.copy_session(s, source="local", target="m-1", move_id=str(uuid.uuid4())))
    assert res.main_rel.endswith(f"{s.session_id}.jsonl") and len(res.files) == 2
    assert remote.files[res.main_rel] == b'{"type":"user"}\n'
    assert not (Path(config.AGENTS_DIR) / s.agent / sf.STAGING_DIR).exists() or not any(
        (Path(config.AGENTS_DIR) / s.agent / sf.STAGING_DIR).iterdir())


def test_a_copy_from_a_machine_to_the_platform(monkeypatch):
    s = _scope()
    rel = f".claude/projects/-home-x/{s.session_id}.jsonl"
    _Remote(monkeypatch, {rel: b"from the laptop"})
    res = asyncio.run(sf.copy_session(s, source="m-1", target="local", move_id=str(uuid.uuid4())))
    assert (_home(s) / rel).read_bytes() == b"from the laptop" and res.main_size == 15


def test_the_progress_follows_the_bytes_of_every_leg(monkeypatch):
    """One total for the whole move: the listed bytes once per leg on the
    wire, so a machine-to-machine move counts the pull, then the push; the
    platform's own staging copy and the commit say their phase."""
    from ws import wire_events as wire
    s = _scope()
    rel = f".claude/projects/k/{s.session_id}.jsonl"
    _Remote(monkeypatch, {rel: b"x" * 100})
    seen: list[tuple[int, int, str]] = []
    asyncio.run(sf.copy_session(s, source="m-1", target="m-2", move_id=str(uuid.uuid4()),
                                progress=lambda d, t, p: seen.append((d, t, p))))
    assert seen == [(50, 200, wire.MOVE_PHASE_COPY), (100, 200, wire.MOVE_PHASE_COPY),
                    (200, 200, wire.MOVE_PHASE_COPY), (200, 200, wire.MOVE_PHASE_COMMIT)]

    t = _scope()
    _transcript(t, side=False, body=b"y" * 40)
    _Remote(monkeypatch, {})
    seen.clear()
    asyncio.run(sf.copy_session(t, source="local", target="m-1", move_id=str(uuid.uuid4()),
                                progress=lambda d, tot, p: seen.append((d, tot, p))))
    assert seen == [(0, 40, wire.MOVE_PHASE_STAGE), (40, 40, wire.MOVE_PHASE_COPY),
                    (40, 40, wire.MOVE_PHASE_COMMIT)]


def test_a_source_that_changed_during_the_copy_fails_it(monkeypatch):
    s = _scope()
    rel = f".claude/projects/k/{s.session_id}.jsonl"
    remote = _Remote(monkeypatch, {rel: b"v1"})
    real_pull = sf.remote_pull

    async def pull_then_write(machine_id, scope, move_id, f, **kw):
        out = await real_pull(machine_id, scope, move_id, f, **kw)
        remote.files[rel] = b"v2 a turn slipped in"
        return out
    monkeypatch.setattr(sf, "remote_pull", pull_then_write)
    with pytest.raises(sf.TransferError, match="changed at the source"):
        asyncio.run(sf.copy_session(s, source="m-1", target="local", move_id=str(uuid.uuid4())))


def test_a_source_with_no_session_file_says_so(monkeypatch):
    s = _scope()
    _Remote(monkeypatch, {})
    with pytest.raises(sf.TransferError) as e:
        asyncio.run(sf.copy_session(s, source="m-1", target="local", move_id=str(uuid.uuid4())))
    assert e.value.why == "no_files"


def test_a_commit_that_fails_says_the_target_may_hold_files(monkeypatch):
    s = _scope()
    _transcript(s)
    _Remote(monkeypatch, {})

    async def broken_commit(machine_id, scope, move_id):
        raise RuntimeError("the link dropped mid-commit")
    monkeypatch.setattr(sf, "remote_commit", broken_commit)
    with pytest.raises(sf.TransferError) as e:
        asyncio.run(sf.copy_session(s, source="local", target="m-1", move_id=str(uuid.uuid4())))
    assert e.value.committed

    async def busy_commit(machine_id, scope, move_id):
        raise sf.TransferError(sf.WHY_BUSY, "busy: a live session holds this session",
                               place=machine_id)
    monkeypatch.setattr(sf, "remote_commit", busy_commit)
    with pytest.raises(sf.TransferError) as e:
        asyncio.run(sf.copy_session(s, source="local", target="m-1", move_id=str(uuid.uuid4())))
    assert e.value.why == sf.WHY_BUSY and not e.value.committed


def test_a_malformed_list_is_a_transfer_failure(monkeypatch):
    s = _scope()

    async def send_command(machine_id, frame, *, timeout):
        return {"status": "ok", "files": [{"rel": f".claude/projects/k/{s.session_id}.jsonl",
                                           "size": "lots"}]}
    monkeypatch.setattr(sf._cm(), "send_command", send_command)
    with pytest.raises(sf.TransferError, match="malformed"):
        asyncio.run(sf.remote_list("m-1", s))


def test_a_busy_place_is_named_in_the_failure(monkeypatch):
    s = _scope()

    async def send_command(machine_id, frame, *, timeout):
        return {"status": "rejected", "error": "busy: a live session holds this session"}
    monkeypatch.setattr(sf._cm(), "send_command", send_command)
    with pytest.raises(sf.TransferError) as e:
        asyncio.run(sf.remote_list("m-1", s))
    assert e.value.why == sf.WHY_BUSY and e.value.place == "m-1"


def test_the_proof_on_a_machine_asks_for_sizes_only(monkeypatch):
    s = _scope()
    rel = f".claude/projects/k/{s.session_id}.jsonl"
    asked = []

    async def send_command(machine_id, frame, *, timeout):
        asked.append(frame["op"])
        return {"status": "ok", "files": [{"rel": rel, "size": 42}]}
    monkeypatch.setattr(sf._cm(), "send_command", send_command)
    assert asyncio.run(sf.main_size("m-1", s, rel)) == 42
    assert asked == [sf.OP_STAT]
