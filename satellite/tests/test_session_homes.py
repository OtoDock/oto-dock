"""The satellite's side of a session's home (``sessions/session_homes.py``):
the start's word, the sign-in writes and the start-time sweep, a compute
machine with its own copy, the store's old files at a home start, and the
session's own close when its engine ends or its start fails."""

import asyncio
import json
import os
import stat

import pytest

from satellite.config import SatelliteConfig
from satellite.sessions import session_homes
from satellite.sessions.session_manager import SessionManager

SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
OTHER = "11111111-2222-3333-4444-555555555555"
SPEC = {"links": ["projects"], "seed": {}}
CODEX_SPEC = {"links": ["sessions", "skills", "prompts", "thread-writer-locks", "rules", "archived_sessions"],
              "own_files": ["auth.json", "config.toml", "hooks.json", "models.json"],
              "seed": {"installation_id": []}}


@pytest.fixture
def tmp_agent_dir(tmp_path):
    agent_dir = tmp_path / "agents" / "test-agent"
    agent_dir.mkdir(parents=True)
    return agent_dir


@pytest.fixture
def sat_config(tmp_path):
    return SatelliteConfig(
        machine_id="test-machine",
        machine_secret="test-secret",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents",
        mcps_dir=tmp_path / "mcps",
    )


class _CapturingWS:
    def __init__(self):
        self.sent = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)

    async def enqueue_bulk(self, msg):
        self.sent.append(msg)
        return True


def test_no_word_no_home(tmp_path):
    assert session_homes.open_home(tmp_path, SID, None) == tmp_path
    assert not (tmp_path / "own").exists()
    session_homes.close_home(tmp_path, SID, None)


@pytest.mark.skipif(os.name == "nt", reason="links stand in for junctions on POSIX")
def test_a_sign_in_replaces_a_link_at_its_name(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    folder = tmp_path / "f"
    folder.mkdir()
    os.symlink(victim, folder / ".credentials.json")
    session_homes.write_sign_in(folder, ".credentials.json", {"a": 1})
    assert victim.read_text() == "untouched"
    assert json.loads((folder / ".credentials.json").read_text()) == {"a": 1}
    assert stat.S_IMODE((folder / ".credentials.json").stat().st_mode) == 0o600


def test_the_shapes_a_push_may_name():
    ok = (["workspace", ".claude"], ["users", "u", ".claude", "own", SID])
    bad = (["workspace", ".claude", "own"], ["users", "u", ".claude", "own", "x"],
           ["users", "u", ".codex", "own", SID], ["workspace", ".claude", "other", SID])
    assert all(session_homes.sign_in_dir(p, ".claude") for p in ok)
    assert not any(session_homes.sign_in_dir(p, ".claude") for p in bad)


@pytest.mark.skipif(os.name == "nt", reason="links stand in for junctions on POSIX")
def test_the_start_sweep_removes_every_home_and_never_follows_a_link(tmp_path):
    agents = tmp_path / "agents"
    stores = [agents / "a" / "workspace" / ".claude", agents / "a" / "users" / "u" / ".claude",
              agents / "b" / "workspace" / ".codex"]
    for store in stores:
        store.mkdir(parents=True)
        session_homes.open_home(store, SID, SPEC)
    session_homes.open_home(stores[0], OTHER, SPEC)
    outside = tmp_path / "outside"
    (outside / "workspace" / ".claude" / "own" / SID).mkdir(parents=True)
    os.symlink(outside, agents / "linked")
    assert session_homes.sweep_all(agents) == 4
    assert all(not (s / "own" / SID).exists() for s in stores)
    assert (outside / "workspace" / ".claude" / "own" / SID).is_dir()


@pytest.mark.skipif(os.name == "nt", reason="links stand in for junctions on POSIX")
def test_an_own_copy_machine_makes_the_homes_links_after_its_files(tmp_path):
    """A compute machine with its own copy: the home's files come with the
    session's other prepared files, the links are this copy's own."""
    import asyncio
    import base64
    from types import SimpleNamespace

    from satellite.sessions import sandbox_host
    agent = tmp_path / "agents" / "pa"
    (agent / "workspace" / ".claude").mkdir(parents=True)
    payload = {
        "claude_dir_relative": "workspace/.claude",
        "session_home": {"links": ["projects"], "seed": {}},
        "host_files": {f"workspace/.claude/own/{SID}/.credentials.json":
                       {"content_b64": base64.b64encode(b"token").decode(), "mode": 0o600}},
    }
    asyncio.run(sandbox_host.ready_own_copy(payload, SimpleNamespace(own_copy=True), SID, agent))
    home = agent / "workspace" / ".claude" / "own" / SID
    assert (home / ".credentials.json").read_text() == "token"
    assert (home / "projects").is_symlink()
    # The files' writer made both folders with the process's default mode:
    # the home's opening makes them the owner's alone.
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home.parent).st_mode) == 0o700
    session_homes.close_home(agent / "workspace" / ".claude", SID, payload["session_home"])
    assert not home.exists() and (agent / "workspace" / ".claude" / "projects").is_dir()


SIGN_IN = ".credentials" + ".json"


def _home_of(agents, slug="pa"):
    rel = f"workspace/.claude/own/{SID}"
    (agents / slug / rel).mkdir(parents=True)
    return rel


def test_a_pushed_sign_in_lands_in_the_home_private(tmp_path):
    agents = tmp_path / "agents"
    rel = _home_of(agents)
    folder = session_homes.write_sign_in_beneath(agents, "pa", rel, SIGN_IN, {"t": 1}, ".claude")
    target = agents / "pa" / rel / SIGN_IN
    assert folder == agents / "pa" / rel and json.loads(target.read_text()) == {"t": 1}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_pushed_sign_in_for_a_home_that_is_gone_makes_no_folder(tmp_path):
    agents = tmp_path / "agents"
    rel = f"workspace/.claude/own/{SID}"
    assert session_homes.write_sign_in_beneath(agents, "pa", rel, SIGN_IN, {"t": 1}, ".claude") is None
    assert not (agents / "pa" / rel).exists()


@pytest.mark.skipif(os.name == "nt", reason="links stand in for junctions on POSIX")
def test_a_link_swapped_in_after_the_check_is_never_followed(tmp_path, monkeypatch):
    """The folder is opened by descriptor beneath the agents' folder: a link
    put in its place after the check is refused, nothing lands through it."""
    from satellite.host import safe_fs
    agents, outside = tmp_path / "agents", tmp_path / "outside"
    outside.mkdir()
    rel = _home_of(agents)
    real = safe_fs.lstat_beneath

    def _swap(root, path):
        st = real(root, path)
        home = agents / "pa" / rel
        home.rmdir()
        home.symlink_to(outside)
        return st
    monkeypatch.setattr(safe_fs, "lstat_beneath", _swap)
    with pytest.raises(OSError):
        session_homes.write_sign_in_beneath(agents, "pa", rel, SIGN_IN, {"t": 1}, ".claude")
    assert not (outside / SIGN_IN).exists()


@pytest.mark.skipif(os.name == "nt", reason="the descriptor writer runs on POSIX")
def test_an_unchanged_renewal_is_not_written_again(tmp_path):
    """A renewal push that carries the bytes the file holds renames nothing:
    a rename onto a file an engine's sandbox holds a mount over would lift
    it, and the old writer skipped such a write too."""
    agents = tmp_path / "agents"
    rel = _home_of(agents)
    session_homes.write_sign_in_beneath(agents, "pa", rel, SIGN_IN, {"t": 1}, ".claude")
    target = agents / "pa" / rel / SIGN_IN
    before = target.stat()
    session_homes.write_sign_in_beneath(agents, "pa", rel, SIGN_IN, {"t": 1}, ".claude")
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    session_homes.write_sign_in_beneath(agents, "pa", rel, SIGN_IN, {"t": 2}, ".claude")
    assert json.loads(target.read_text()) == {"t": 2}


def test_a_windows_computer_keeps_the_writer_that_falls_back_in_place(tmp_path, monkeypatch):
    """On Windows a rename onto a file its engine holds open fails: the
    write keeps ``config.write_if_changed``, which writes in place then."""
    import dataclasses

    from satellite import config
    agents = tmp_path / "agents"
    rel = _home_of(agents)
    written = []
    monkeypatch.setattr(config, "HOST", dataclasses.replace(config.HOST, posix=False))
    monkeypatch.setattr(session_homes, "write_if_changed",
                        lambda path, data, mode=0o644: written.append((path.name, json.loads(data), mode)))
    folder = session_homes.write_sign_in_beneath(agents, "pa", rel, SIGN_IN, {"t": 1}, ".claude")
    assert folder == agents / "pa" / rel and written == [(SIGN_IN, {"t": 1}, 0o600)]


@pytest.mark.skipif(os.name == "nt", reason="the descriptor writer runs on POSIX")
def test_an_empty_folder_at_the_files_name_is_removed_and_the_write_runs(tmp_path):
    agents = tmp_path / "agents"
    scope = agents / "pa" / "workspace" / ".claude"
    (scope / SIGN_IN).mkdir(parents=True)
    session_homes.write_sign_in_beneath(agents, "pa", "workspace/.claude", SIGN_IN, {"t": 1}, ".claude")
    assert json.loads((scope / SIGN_IN).read_text()) == {"t": 1}


@pytest.mark.skipif(os.name == "nt", reason="the descriptor writer runs on POSIX")
def test_a_folder_that_holds_something_stays_refused_in_one_line(tmp_path):
    agents = tmp_path / "agents"
    held = agents / "pa" / "workspace" / ".claude" / SIGN_IN
    held.mkdir(parents=True)
    (held / "keep").write_text("someone's file")
    with pytest.raises(session_homes.FolderAtSignIn):
        session_homes.write_sign_in_beneath(agents, "pa", "workspace/.claude", SIGN_IN, {"t": 1}, ".claude")
    assert (held / "keep").read_text() == "someone's file"


@pytest.mark.skipif(os.name == "nt", reason="the descriptor writer runs on POSIX")
def test_a_temp_a_killed_satellite_left_is_swept_and_a_fresh_one_stays(tmp_path):
    agents = tmp_path / "agents"
    scope = agents / "pa" / "workspace" / ".claude"
    scope.mkdir(parents=True)
    stale = scope / f".{SIGN_IN}.0123456789ab.partial"
    stale.write_text('{"an": "older token"}')
    os.utime(stale, (1, 1))
    fresh = scope / f".{SIGN_IN}.ba9876543210.partial"
    fresh.write_text("a write in progress")
    other = scope / ".other.json.0123456789ab.partial"
    other.write_text("another file's temp")
    os.utime(other, (1, 1))
    session_homes.write_sign_in_beneath(agents, "pa", "workspace/.claude", SIGN_IN, {"t": 1}, ".claude")
    assert json.loads((scope / SIGN_IN).read_text()) == {"t": 1}
    assert not stale.exists()
    assert fresh.exists() and other.exists()


# ---------------------------------------------------------------------------
# a compute machine with its own copy, and the store's old files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_machine_with_its_own_copy_makes_a_codex_homes_links(tmp_agent_dir, sat_config, monkeypatch):
    from satellite.sessions import sandbox_host, session_files
    monkeypatch.setattr(sat_config, "own_copy", True, raising=False)
    monkeypatch.setattr(session_files, "materialize_agent_files", lambda *a: 0)
    (tmp_agent_dir / "users" / "alice" / ".codex").mkdir(parents=True)
    payload = {"codex_dir_relative": "users/alice/.codex", "session_home": CODEX_SPEC}
    assert await sandbox_host.ready_own_copy(payload, sat_config, SID, tmp_agent_dir) is True
    home = tmp_agent_dir / "users" / "alice" / ".codex" / "own" / SID
    for link in CODEX_SPEC["links"]:
        assert (home / link).is_symlink(), link


@pytest.mark.asyncio
async def test_a_machine_with_its_own_copy_clears_its_stores_old_files(tmp_agent_dir, sat_config, monkeypatch):
    """The platform clears its own disk's store, never this copy: a frame of
    an earlier version left its files here, which a terminal in a home would
    read as its project's. An empty sign-in stays (a sandbox's mount point)."""
    from satellite.sessions import sandbox_host, session_files
    monkeypatch.setattr(sat_config, "own_copy", True, raising=False)
    monkeypatch.setattr(session_files, "materialize_agent_files", lambda *a: 0)
    store = tmp_agent_dir / "users" / "alice" / ".codex"
    store.mkdir(parents=True)
    for name in ("config.toml", "hooks.json", "models.json"):
        (store / name).write_text("an earlier version's")
    (store / "auth.json").write_text("")
    (store / "state_5.sqlite").write_text("every session's")
    payload = {"codex_dir_relative": "users/alice/.codex", "session_home": CODEX_SPEC}
    assert await sandbox_host.ready_own_copy(payload, sat_config, SID, tmp_agent_dir) is True
    assert sorted(p.name for p in store.iterdir() if p.is_file()) == ["auth.json", "state_5.sqlite"]
    assert (store / "auth.json").read_text() == ""


@pytest.mark.asyncio
async def test_a_machine_with_its_own_copy_keeps_no_file_of_a_start_that_fails(tmp_agent_dir, sat_config,
                                                                               monkeypatch):
    """The frame's files are written before the home is opened. A start
    that fails there never reaches a close that knows this copy, so the
    sign-in it wrote goes at once, and the home with it."""
    import base64

    from satellite.sessions import sandbox_host, session_homes
    monkeypatch.setattr(sat_config, "own_copy", True, raising=False)
    home_rel = f"users/alice/.codex/own/{SID}"
    payload = {"codex_dir_relative": "users/alice/.codex", "session_home": CODEX_SPEC,
               "host_files": {f"{home_rel}/auth.json": {"content_b64": base64.b64encode(b"{}").decode(),
                                                        "mode": 0o600}}}
    real = session_homes.open_home

    def _fails(store, sid, spec):
        real(store, sid, spec)
        raise OSError("no space left on device")
    monkeypatch.setattr(session_homes, "open_home", _fails)
    with pytest.raises(OSError):
        await sandbox_host.ready_own_copy(payload, sat_config, SID, tmp_agent_dir)
    assert not (tmp_agent_dir / home_rel).exists()


@pytest.mark.asyncio
async def test_a_machine_with_its_own_copy_keeps_the_store_of_a_session_with_no_home(tmp_agent_dir, sat_config,
                                                                                     monkeypatch):
    """A Codex start that runs from the store itself reads those files."""
    from satellite.sessions import sandbox_host, session_files
    monkeypatch.setattr(sat_config, "own_copy", True, raising=False)
    monkeypatch.setattr(session_files, "materialize_agent_files", lambda *a: 0)
    store = tmp_agent_dir / "users" / "alice" / ".codex"
    store.mkdir(parents=True)
    (store / "config.toml").write_text("this session's")
    payload = {"codex_dir_relative": "users/alice/.codex"}
    assert await sandbox_host.ready_own_copy(payload, sat_config, SID, tmp_agent_dir) is True
    assert (store / "config.toml").read_text() == "this session's"


def test_the_stores_empty_sign_in_stays_at_a_home_start(tmp_path):
    """Twin of the platform's rule: an empty sign-in in the store is a mount
    point a sandbox may hold, and holds nothing; one with content goes."""
    from satellite.sessions.codex_files import drop_store_leftovers
    store = tmp_path / "workspace" / ".codex"
    store.mkdir(parents=True)
    (store / "auth.json").write_bytes(b"")
    drop_store_leftovers(tmp_path, "workspace/.codex")
    assert (store / "auth.json").exists()
    (store / "auth.json").write_text("an earlier version's sign-in")
    drop_store_leftovers(tmp_path, "workspace/.codex")
    assert not (store / "auth.json").exists()


def _store_behind_a_link(agent_dir, tmp_path):
    """``users/alice`` of the agent as a link to a folder outside it, whose
    Codex store holds an earlier version's files: the outside store."""
    outside = tmp_path / "outside" / ".codex"
    outside.mkdir(parents=True)
    for name in ("config.toml", "hooks.json", "models.json", "auth.json"):
        (outside / name).write_text("another folder's")
    (agent_dir / "users").mkdir(parents=True, exist_ok=True)
    (agent_dir / "users" / "alice").symlink_to(tmp_path / "outside", target_is_directory=True)
    return outside


def _names(folder):
    return sorted(p.name for p in folder.iterdir() if p.is_file())


@pytest.mark.skipif(os.name == "nt", reason="a folder link needs a privilege on Windows")
def test_the_stores_old_files_go_beneath_the_agents_folder_only(tmp_agent_dir, tmp_path):
    """A link on a folder on the way to the store is never followed: nothing
    outside the agent's folder is removed. A link at a file's own name goes,
    its target stays. A folder at a name is left."""
    from satellite.sessions.codex_files import drop_store_leftovers
    outside = _store_behind_a_link(tmp_agent_dir, tmp_path)
    drop_store_leftovers(tmp_agent_dir, "users/alice/.codex")
    assert _names(outside) == ["auth.json", "config.toml", "hooks.json", "models.json"]
    store = tmp_agent_dir / "workspace" / ".codex"
    (store / "hooks.json").mkdir(parents=True)
    (store / "config.toml").symlink_to(outside / "config.toml")
    drop_store_leftovers(tmp_agent_dir, "workspace/.codex")
    assert not (store / "config.toml").is_symlink() and (outside / "config.toml").is_file()
    assert (store / "hooks.json").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="a folder link needs a privilege on Windows")
@pytest.mark.asyncio
async def test_a_machine_with_its_own_copy_removes_nothing_through_a_link_on_the_way(tmp_agent_dir, sat_config,
                                                                                      monkeypatch, tmp_path):
    """The own copy's caller runs with the machine's rights, not the
    session's: the store's old files are removed beneath the agent's folder."""
    from satellite.sessions import sandbox_host, session_files, session_homes
    monkeypatch.setattr(sat_config, "own_copy", True, raising=False)
    monkeypatch.setattr(session_files, "materialize_agent_files", lambda *a: 0)
    monkeypatch.setattr(session_homes, "open_home", lambda store, sid, spec: store)
    outside = _store_behind_a_link(tmp_agent_dir, tmp_path)
    payload = {"codex_dir_relative": "users/alice/.codex", "session_home": CODEX_SPEC}
    assert await sandbox_host.ready_own_copy(payload, sat_config, SID, tmp_agent_dir) is True
    assert _names(outside) == ["auth.json", "config.toml", "hooks.json", "models.json"]


class TestASessionReachesItsClose:
    """On a paired computer the session's own close is what removes its home
    and the sign-in in it. A terminal whose engine ends by itself and a
    start that raises after its home was opened both reach it."""

    SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    SPEC = {"links": ["sessions", "skills", "prompts", "thread-writer-locks", "rules", "archived_sessions"],
            "own_files": ["auth.json", "config.toml", "hooks.json", "models.json"],
            "seed": {"installation_id": []}}

    def _payload(self):
        return {"cwd_relative": "users/alice", "codex_dir_relative": "users/alice/.codex",
                "system_prompt": "You are a test agent.", "mcp_config_toml": "", "model": "m", "env": {},
                "session_home": self.SPEC, "codex_session_text": True,
                "auth_json": {"auth_mode": "chatgpt", "tokens": {"access_token": "at"}}}

    def _engine(self, monkeypatch, *, spawn_fails=False):
        from types import SimpleNamespace

        from satellite.sessions import session_manager as sm_mod
        from satellite.sessions.codex_session import CodexSession
        from satellite.terminal.codex_pty_session import CodexPtySession
        closes: list[str] = []

        class _Terminal(CodexPtySession):
            async def _wire_and_spawn(self, cmd, env, cwd):
                if spawn_fails:
                    raise RuntimeError("the engine did not start")
                self.pid = 7

            async def _session_agent_env(self):
                return {}

            async def close(self):
                closes.append(self.session_id)
                await super().close()

        class _Headless(CodexSession):
            async def _connect_with_retry(self, env):
                raise RuntimeError("the engine did not start")

        engine = SimpleNamespace(pty_cls=lambda: _Terminal, headless_cls=lambda: _Headless)
        monkeypatch.setattr(sm_mod, "engine_for", lambda path: engine)
        return closes

    def _home(self, sat_config):
        return sat_config.agents_dir / "pa" / "users" / "alice" / ".codex" / "own" / self.SID

    async def _gone(self, home) -> bool:
        for _ in range(100):
            if not home.exists():
                return True
            await asyncio.sleep(0.02)
        return False

    @pytest.mark.asyncio
    async def test_a_terminal_whose_engine_ends_by_itself_leaves_no_home(self, sat_config, monkeypatch):
        """The person's own exit, a crash, the memory guard: the platform
        sends no close for an exited terminal, so its exit runs its close."""
        closes = self._engine(monkeypatch)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.pty_open({"session_id": self.SID, "command_id": "c", "agent_slug": "pa",
                           "config": self._payload(), "execution_path": "codex-cli", "open_token": "tok"}, ws)
        assert ws.sent[-1]["status"] == "ok", ws.sent
        home = self._home(sat_config)
        assert (home / "auth.json").is_file()
        session = sm.pty_sessions[self.SID]
        session._on_dead()                      # what the PTY's exit calls
        assert self.SID not in sm.pty_sessions
        assert await self._gone(home)
        # Once: a second exit, or the platform's close crossing it, closes nothing again.
        session._on_dead()
        await sm.pty_close({"session_id": self.SID, "open_token": "tok"})
        await asyncio.sleep(0.05)
        assert closes == [self.SID]

    @pytest.mark.asyncio
    async def test_an_exit_of_a_terminal_another_close_took_closes_nothing(self, sat_config, monkeypatch):
        """A close already running owns the teardown: the exit it causes
        starts no second one, and a replaced terminal's exit leaves its
        successor alone."""
        closes = self._engine(monkeypatch)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.pty_open({"session_id": self.SID, "command_id": "c", "agent_slug": "pa",
                           "config": self._payload(), "execution_path": "codex-cli", "open_token": "tok"}, ws)
        first = sm.pty_sessions[self.SID]
        await sm.pty_close({"session_id": self.SID, "open_token": "tok"})
        first._on_dead()
        await asyncio.sleep(0.05)
        assert closes == [self.SID]
        await sm.pty_open({"session_id": self.SID, "command_id": "c2", "agent_slug": "pa",
                           "config": self._payload(), "execution_path": "codex-cli", "open_token": "tok2"}, ws)
        first._on_dead()
        await asyncio.sleep(0.05)
        assert closes == [self.SID] and self.SID in sm.pty_sessions
        assert (self._home(sat_config) / "auth.json").is_file()

    @pytest.mark.asyncio
    async def test_a_replaced_terminals_exit_closes_no_successor_when_no_open_is_named(self, sat_config,
                                                                                      monkeypatch):
        """An open that names no token cannot be told from its successor by
        one: the exit closes the terminal it was the exit of and no other,
        so the successor keeps its home."""
        closes = self._engine(monkeypatch)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.pty_open({"session_id": self.SID, "command_id": "c", "agent_slug": "pa",
                           "config": self._payload(), "execution_path": "codex-cli"}, ws)
        first = sm.pty_sessions[self.SID]
        await sm.pty_open({"session_id": self.SID, "command_id": "c2", "agent_slug": "pa",
                           "config": self._payload(), "execution_path": "codex-cli"}, ws)
        assert ws.sent[-1]["status"] == "ok", ws.sent
        assert sm.pty_sessions[self.SID] is not first
        closes.clear()
        first._on_dead()
        await asyncio.sleep(0.05)
        assert closes == []
        assert (self._home(sat_config) / "auth.json").is_file()

    @pytest.mark.asyncio
    async def test_a_terminals_start_that_raises_leaves_no_home(self, sat_config, monkeypatch):
        self._engine(monkeypatch, spawn_fails=True)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.pty_open({"session_id": self.SID, "command_id": "c", "agent_slug": "pa",
                           "config": self._payload(), "execution_path": "codex-cli"}, ws)
        assert ws.sent[-1]["status"] == "error" and "did not start" in ws.sent[-1]["error"]
        assert self.SID not in sm.pty_sessions
        assert not self._home(sat_config).exists()

    @pytest.mark.asyncio
    async def test_a_sessions_start_that_raises_leaves_no_home(self, sat_config, monkeypatch):
        self._engine(monkeypatch)
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        await sm.start_session({"session_id": self.SID, "command_id": "c", "agent_slug": "pa",
                                "config": self._payload(), "execution_path": "codex-cli"}, ws)
        assert ws.sent[-1]["status"] == "error" and "did not start" in ws.sent[-1]["error"]
        assert self.SID not in sm.sessions
        assert not self._home(sat_config).exists()
