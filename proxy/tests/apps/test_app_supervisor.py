"""The app sandbox and supervisor (APPS.md "The app sandbox", "The
supervisor").

Load-bearing: a server's environment carries only what it needs. The launch
token and the viewer claims are signed with keys the session validators
reject. A start is refused by a machine that cannot run it, and before any
machine for a manifest nobody approved. An idle server stops, an open
bridged socket holds one, and a server goes with its row. The smoke runs the
working tree on scratch data and never touches the live data. The table a
machine renders, a crash and a lost machine are ``test_app_compute.py``.
What the sandbox lets a server see and reach is the satellite's suite
(``satellite/tests/test_app_server.py``).
"""

from __future__ import annotations

import asyncio
import os
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from services.apps import app_compute, app_sandbox, app_supervisor, app_tokens, releases
from storage import database as task_store
from tests.apps.conftest import MACHINE

AGENT = "app-sup-agent"

SERVER = 'Bun.serve({ port: Number(process.env.PORT), fetch() { return new Response("ok"); } });\n'


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    return agents_root / AGENT


def _row(slug: str = "probe", username: str = "alice", owner: str | None = "alice-sub") -> dict:
    root = f"users/{username}/workspace" if username else "workspace"
    return task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                 rel_path=f"{root}/apps/{slug}", kind="folder")


def _tree(root: Path, server: str = SERVER) -> Path:
    (root / "client").mkdir(parents=True, exist_ok=True)
    (root / "server").mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text('{"title": "Probe"}')
    (root / "client" / "index.html").write_text("<p>probe</p>")
    (root / "server" / "index.ts").write_text(server)
    return root


def _released(agent_tree: Path, slug: str = "probe") -> dict:
    row = _row(slug)
    src = _tree(agent_tree / row["rel_path"])
    rel, sha, _n = releases.cut_folder_release(row, src)
    return task_store.set_app_release(row["id"], rel, sha)


# ───────────────────────────── the environment and a start ──────────────────


def test_app_env_carries_only_what_the_server_needs(agent_tree):
    row = _row()
    env = app_sandbox.build_env(row, "tok", "pub", preview=True)
    assert env["PORT"] == "3000" and env["OTODOCK_APP_ID"] == row["id"]
    assert env["OTODOCK_APP_TOKEN"] == "tok" and env["OTODOCK_APP_PUBLIC_KEY"] == "pub"
    assert env["OTODOCK_PROXY_URL"] == f"http://127.0.0.1:{config.PORT}"
    assert env["OTODOCK_PREVIEW"] == "1" and env["HOME"] == "/tmp"
    assert "OTODOCK_PREVIEW" not in app_sandbox.build_env(row, "t", "p")
    assert not any(k in env for k in ("JWT_SECRET", "PROXY_API_KEY", "DATABASE_URL"))
    # Production mode, and an env secret cannot switch it back: the runtime's
    # names win the merge (the validator refuses the declaration anyway).
    assert env["NODE_ENV"] == "production"
    live = app_sandbox.build_env(row, "t", "p", secrets={"NODE_ENV": "development", "SMTP_PASSWORD": "x"})
    assert live["NODE_ENV"] == "production" and live["SMTP_PASSWORD"] == "x"


def test_start_refuses_without_bun_or_approval(agent_tree, tmp_path, placed):
    row = _released(agent_tree)
    # A machine with no Bun refuses, and its sentence is the reason.
    placed.answer = lambda msg: RuntimeError("Satellite command error: Bun is not installed on this machine.")
    with pytest.raises(app_sandbox.AppStartError, match="Bun is not installed on this machine"):
        asyncio.run(app_supervisor.start(row))
    sent = len(placed.sent)
    # A manifest waiting for approval never reaches a machine (checked
    # before the launch).
    row = task_store.upsert_app(AGENT, "alice", "alice-sub", "probe", actions_json='[{"id":"x"}]')
    row = task_store.get_app(row["id"])
    with pytest.raises(app_supervisor.AppUnavailable) as e:
        asyncio.run(app_supervisor.start(row))
    assert e.value.state == "unapproved" and len(placed.sent) == sent
    # A folder without a server is a static app: no process, "static".
    static = _row("static")
    tree = tmp_path / "static"
    (tree / "client").mkdir(parents=True)
    (tree / "client" / "index.html").write_text("<p>s</p>")
    inst = asyncio.run(app_supervisor.start(static, release_dir=tree, data_dir=tmp_path / "d"))
    assert inst.state == "static" and inst.proc is None and len(placed.sent) == sent


# ───────────────────────────── the claims ───────────────────────────────────


def test_tokens_are_per_app_per_purpose_and_foreign_to_the_session_validators():
    from auth.session_token import validate_session_token
    a, b = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    tok = app_tokens.mint(a, app_tokens.PURPOSE_VIEWER, {"sub": "alice-sub", "role": "manager"}, 60)
    claims = app_tokens.verify(tok, a, app_tokens.PURPOSE_VIEWER)
    assert claims and claims["sub"] == "alice-sub" and claims["aud"] == f"app:{a}"
    assert app_tokens.verify(tok, b, app_tokens.PURPOSE_VIEWER) is None
    assert app_tokens.verify(tok, a, app_tokens.PURPOSE_LAUNCH) is None
    assert app_tokens.verify("not-a-token", a, app_tokens.PURPOSE_VIEWER) is None
    assert app_tokens.peek_app_id(tok) == a
    # Never a session principal.
    assert validate_session_token(tok) is None
    from auth.providers import validate_session_jwt
    assert validate_session_jwt(tok) is None
    # Expired claims die.
    old = app_tokens.mint(a, app_tokens.PURPOSE_VIEWER, {"sub": "x"}, -5)
    assert app_tokens.verify(old, a, app_tokens.PURPOSE_VIEWER) is None
    # The public key is stable and raw (32 bytes, base64url).
    pub = app_tokens.public_key_b64(a)
    assert pub == app_tokens.public_key_b64(a) and pub != app_tokens.public_key_b64(b)
    assert len(pub) == 43


# ───────────────────────────── the launcher ─────────────────────────────────


def test_launcher_takes_the_inbound_splice_and_egress_deny():
    path = Path(config.BASE_DIR) / "scripts" / "oto-sandbox-net"
    mod = SourceFileLoader("oto_sandbox_net", str(path)).load_module()
    fwd, allow, dns, block, inbound, deny, inner = mod._parse_argv(
        ["--forward", "8400", "--inbound", "18081:3000", "--egress-deny",
         "--block-private", "--", "true"])
    assert (fwd, inbound, deny, inner) == (["8400"], "18081:3000", True, ["true"])
    with pytest.raises(SystemExit):
        mod._parse_argv(["--inbound", "nope", "--", "true"])
    shim = mod._build_pyshim(["8400"], ["203.0.113.5"], "", True, True)
    assert "_EGRESS_DENY = True" in shim and "0.0.0.0/1" in shim and "128.0.0.0/1" in shim
    assert "203.0.113.5" in shim
    assert "_EGRESS_DENY = False" in mod._build_pyshim(["8400"], [], "", True, False)
    # The namespace loses ::1 (address off, then the unreachable route).
    assert shim.index('"addr", "del", "::1/128"') < shim.index('"unreachable", "::1/128"')
    # A failed route puts the address back, or the launch stops.
    restore = shim.index('"addr", "add", "::1/128"')
    assert 0 < shim.index("os._exit(41)", restore) - restore < 200


def test_the_pump_keeps_reading_past_a_line_over_the_limit(agent_tree, tmp_path):
    """A line past the reader's limit must not end the pump: nobody would
    read the pipe again and the child would block on its next write."""
    row = _row()
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=tmp_path, data_dir=tmp_path, entry="server/index.ts")

    class _Proc:
        def __init__(self):
            self.stdout = asyncio.StreamReader(limit=1024)

    async def run():
        inst.proc = _Proc()
        inst.proc.stdout.feed_data(b"x" * 5000 + b"\nafter\n")
        inst.proc.stdout.feed_eof()
        await app_supervisor._pump(inst)

    asyncio.run(run())
    assert inst._tail[-1] == "after" and any("was not kept" in t for t in inst._tail)


def test_egress_carves_are_public_addresses_only():
    """A carve outranks the private-range blackholes, so an approved name
    its author points at the LAN, a container or the metadata service must
    not become one."""
    answers = {"api.vendor.test": ["93.184.216.34", "10.0.0.5", "169.254.169.254",
                                   "::ffff:192.168.1.1", "2606:4700::1111"]}

    def fake(host, *_a, **_k):
        return [(0, 0, 0, "", (a, 0)) for a in answers[host]]

    with patch.object(app_supervisor.socket, "getaddrinfo", side_effect=fake):
        got = app_supervisor._allow_hosts({"slug": "s", "egress": '["api.vendor.test"]'})
    assert got == ["93.184.216.34", "2606:4700::1111"]


# ───────────────────────────── on the compute machine ───────────────────────


def test_an_idle_server_stops_and_an_open_socket_holds_another_through_the_same_sweep(
        agent_tree, placed, healthy, monkeypatch):
    row1, row2 = _released(agent_tree, "one"), _released(agent_tree, "two")

    async def run() -> None:
        inst1 = await app_supervisor.start(row1)
        inst2 = await app_supervisor.start(row2)
        first, second = [m["server_id"] for m in placed.of("app_server_start")]
        assert app_supervisor.status(row1["id"])["server"] == "up"
        monkeypatch.setattr(app_supervisor, "IDLE_STOP_S", 0)
        app_supervisor.ws_opened(row2["id"])
        inst1.last_activity -= 1
        inst2.last_activity -= 1
        await app_supervisor.sweep_once()
        assert app_supervisor.get(row1["id"]) is None and inst1.proc.returncode is not None
        assert app_supervisor.status(row1["id"])["server"] == "stopped"
        assert [m["server_id"] for m in placed.of("app_server_stop")] == [first]
        assert app_supervisor.get(row2["id"]) is inst2 and inst2.proc.returncode is None
        # Its socket closed: the next sweep stops it too.
        app_supervisor.ws_closed(row2["id"])
        inst2.last_activity -= 1
        await app_supervisor.sweep_once()
        assert app_supervisor.get(row2["id"]) is None and inst2.proc.returncode is not None
        assert [m["server_id"] for m in placed.of("app_server_stop")] == [first, second]

    asyncio.run(run())


def test_a_server_goes_with_its_row(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run() -> None:
        inst = await app_supervisor.start(row)
        task_store.delete_app(row["id"])
        await app_supervisor.sweep_once()
        assert app_supervisor.get(row["id"]) is None and inst.proc.returncode is not None
        assert [m["server_id"] for m in placed.of("app_server_stop")] == [inst.proc.server_id]

    asyncio.run(run())


def _printing(placed, monkeypatch) -> list[str]:
    """What a server prints reaches the platform ahead of its start's ack:
    the list holds the lines of the next start."""
    lines: list[str] = []
    answer = placed.send_command

    async def send(machine_id, msg, *, timeout=30.0, command_id=None):
        if msg["type"] == "app_server_start":
            app_compute.on_frame(MACHINE, {"type": "app_server_output", "server_id": msg["server_id"],
                                           "text": "".join(lines)})
            await asyncio.sleep(0.05)
        return await answer(machine_id, msg, timeout=timeout, command_id=command_id)

    monkeypatch.setattr(placed, "send_command", send)
    return lines


def test_smoke_runs_the_working_tree_on_scratch_data(agent_tree, placed, healthy, monkeypatch):
    row = _row("smoke")
    tree = _tree(agent_tree / row["rel_path"])
    printed = _printing(placed, monkeypatch)

    async def run() -> None:
        printed[:] = ["probe server starting on 3000\n"]
        report = await app_supervisor.smoke(row, tree)
        assert report["ok"] and report["server"] == "up"
        assert "probe server starting" in report["log"]
        # The machine ran the working tree read-only as a check instance, on
        # a scratch folder beside the releases, with no host opened.
        start = placed.of("app_server_start")[0]
        binds = {b["sandbox"]: b for b in start["sandbox"]["binds"]}
        assert start["instance"] == "check" and not start["sandbox"]["allow_hosts"]
        assert binds["/app"]["rel"] == row["rel_path"] and not binds["/app"]["rw"]
        scratch = agent_tree / binds["/app/data"]["rel"]
        assert binds["/app/data"]["rw"] and scratch.parent == releases.app_release_dir(row)
        assert scratch.name.startswith(releases.SCRATCH_DATA_PREFIX)
        # The smoke stopped it and left nothing: no scratch, no live data.
        assert [m["server_id"] for m in placed.of("app_server_stop")] == [start["server_id"]]
        assert not scratch.exists() and not releases.app_data_dir(row).exists()
        assert app_supervisor.get(row["id"], "check") is None
        # A server that dies at once: the machine's refusal is the reason,
        # and what the server printed is in the report.
        other = _row("broken")
        broken = _tree(agent_tree / other["rel_path"])
        printed[:] = ["dying\n"]
        placed.answer = lambda msg: RuntimeError("Satellite command error: The app server exited at once.\ndying")
        report = await app_supervisor.smoke(other, broken)
        assert not report["ok"] and report["server"] == "failed"
        assert "exited at once" in report["reason"] and "dying" in report["log"]
        assert not list(releases.app_release_dir(other).glob(f"{releases.SCRATCH_DATA_PREFIX}*"))
        assert app_supervisor.get(other["id"], "check") is None

    asyncio.run(run())


def test_a_check_instances_scratch_sits_beside_the_releases(agent_tree):
    row = _row("scratch")
    d = releases.scratch_data_dir(row)
    assert d.is_dir() and d.parent == releases.app_release_dir(row)
    assert d.resolve().is_relative_to(agent_tree.resolve())


def test_a_stale_scratch_is_swept_and_a_fresh_one_kept(agent_tree):
    row = _row("scratch")
    stale, fresh = releases.scratch_data_dir(row), releases.scratch_data_dir(row)
    old = time.time() - releases.STALE_SCRATCH_S - 60
    os.utime(stale, (old, old))
    releases.scratch_data_dir(row)
    assert not stale.exists() and fresh.exists()


def test_a_scratch_folder_is_never_served_as_a_release(agent_tree):
    """Code nobody approved writes this folder: a manifest.json it plants
    must not make it addressable by hash as a client release."""
    row = _row("scratch")
    d = releases.scratch_data_dir(row)
    (d / releases.MANIFEST_NAME).write_text("{}")
    assert not d.name.startswith(releases.CHECK_DIR_PREFIX)
    assert releases.find_release_by_sha(row, releases.tree_sha(d)) is None


def test_the_smoke_runs_on_a_scratch_folder_it_removes(agent_tree, monkeypatch):
    row = _row("smoke")
    tree = _tree(agent_tree / row["rel_path"])
    seen: dict = {}

    async def fake_start(r, name, *, release_dir=None, data_dir=None, allow_hosts=None):
        seen["data_dir"] = data_dir
        assert data_dir.is_dir()
        return app_supervisor.Instance(row_id=r["id"], name=name, row=r, release_dir=release_dir,
                                       data_dir=data_dir)

    async def fake_stop(row_id, name=None):
        return 1

    monkeypatch.setattr(app_supervisor, "start", fake_start)
    monkeypatch.setattr(app_supervisor, "stop", fake_stop)
    report = asyncio.run(app_supervisor.smoke(row, tree))
    assert report["ok"]
    assert seen["data_dir"].parent == releases.app_release_dir(row)
    assert not seen["data_dir"].exists()


def test_the_start_probe_waits_out_a_server_that_binds_late(monkeypatch):
    """A server still binding answers the probe with connect errors (anyio's
    ``getpeername`` AttributeError among them); the probe retries inside its
    window instead of failing the start."""
    import httpx
    answers = [AttributeError("'NoneType' object has no attribute 'getpeername'"),
               httpx.ConnectError("refused"), ConnectionResetError(), 200]

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            got = answers.pop(0)
            if isinstance(got, BaseException):
                raise got
            return httpx.Response(got)

    async def _no_wait(_s):
        return None

    monkeypatch.setattr(app_supervisor.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(app_supervisor.asyncio, "sleep", _no_wait)
    inst = app_supervisor.Instance.__new__(app_supervisor.Instance)
    inst.proc = type("_P", (), {"returncode": None})()
    inst.host_port = 1
    assert asyncio.run(app_supervisor._health(inst)) is True
    assert answers == []


def test_supervisor_log_rotates_at_the_cap(agent_tree, monkeypatch):
    row = _row("logs")
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=Path("/nonexistent"), data_dir=Path("/nonexistent"))
    monkeypatch.setattr(app_supervisor, "LOG_MAX_BYTES", 40)
    for i in range(6):
        asyncio.run(app_supervisor._write_log(inst, f"line {i} " + "x" * 10 + "\n"))
    path = releases.log_path(row)
    assert path.is_file() and path.with_suffix(".log.1").is_file()
    assert os.path.getsize(path) < 80
    assert "line 5" in app_supervisor.read_log_tail(row)


def test_the_launchers_startup_chatter_stays_out_of_the_log(agent_tree):
    """pasta prints four lines on every start when the host has no IPv6
    route, a loopback resolver and no syslog; they buried every real line
    of app_logs on the internal install. Only those exact lines are
    dropped — an app's own message that merely resembles them is kept."""
    noise = ["Failed to send 46 bytes to syslog\n", "Failed to send 42 bytes to syslog",
             "No external routable interface for IPv6\n", "Couldn't get any nameserver address\n"]
    assert all(app_supervisor.is_launcher_noise(line) for line in noise)
    kept = ["board listening on 3000\n", "Failed to send the digest: timeout\n",
            "warn: No external routable interface for IPv6 (ignored)\n", "", "   \n"]
    assert not any(app_supervisor.is_launcher_noise(line) for line in kept)

    async def run():
        row = _row("quiet")
        inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                       release_dir=Path("/nonexistent"), data_dir=Path("/nonexistent"))
        reader = asyncio.StreamReader()
        for line in noise + ["board listening on 3000\n"]:
            reader.feed_data((line if line.endswith("\n") else line + "\n").encode())
        reader.feed_eof()

        class _Proc:
            stdout = reader
        inst.proc = _Proc()  # type: ignore[assignment]
        await app_supervisor._pump(inst)
        assert inst._tail == ["board listening on 3000"]
        assert "syslog" not in app_supervisor.read_log_tail(row)
        assert "listening on 3000" in app_supervisor.read_log_tail(row)

    asyncio.run(run())


# ── the app's folders beside its releases, reached with no link ─────────────


def _outside(tmp_path: Path) -> Path:
    """A stand-in folder beside the agents folder, with a kept file."""
    out = tmp_path / "outside"
    out.mkdir()
    (out / "kept.log").write_text("kept\n")
    return out


def _swap_for_link(path: Path, target: Path) -> None:
    path.rename(path.with_name(path.name + "-moved"))
    path.symlink_to(target, target_is_directory=target.is_dir())


def test_the_log_is_never_written_or_read_through_a_link(agent_tree, tmp_path):
    row = _row("logs")
    out = _outside(tmp_path)
    log = releases.log_path(row)
    log.parent.mkdir(parents=True)
    log.symlink_to(out / "kept.log")
    asyncio.run(app_supervisor.append_log(row, "a line\n"))
    assert (out / "kept.log").read_text() == "kept\n"
    assert not log.is_symlink() and log.read_text() == "a line\n"
    log.unlink()
    log.symlink_to(out / "kept.log")
    assert app_supervisor.read_log_tail(row) == ""


def test_a_link_put_at_the_log_after_its_check_is_never_written_through(agent_tree, tmp_path, monkeypatch):
    row = _row("logs")
    out = _outside(tmp_path)
    log = releases.log_path(row)
    log.parent.mkdir(parents=True)
    real = app_supervisor._clear_odd_name

    def cleared_then_planted(root, rel):
        real(root, rel)
        if rel.endswith("app.log") and not log.is_symlink():
            log.symlink_to(out / "kept.log")
    monkeypatch.setattr(app_supervisor, "_clear_odd_name", cleared_then_planted)
    asyncio.run(app_supervisor.append_log(row, "a line\n"))
    assert (out / "kept.log").read_text() == "kept\n"


def test_a_log_whose_folder_is_a_link_is_left_and_the_pump_goes_on(agent_tree, tmp_path):
    row = _row("logs")
    out = _outside(tmp_path)
    folder = releases.app_release_dir(row)
    folder.parent.mkdir(parents=True)
    folder.symlink_to(out, target_is_directory=True)
    asyncio.run(app_supervisor.append_log(row, "a line\n"))
    assert sorted(p.name for p in out.iterdir()) == ["kept.log"]
    assert app_supervisor.read_log_tail(row) == ""


def test_a_long_log_tail_reads_its_last_window(agent_tree, monkeypatch):
    row = _row("logs")
    monkeypatch.setattr(app_supervisor, "LOG_MAX_BYTES", 32)
    log = releases.log_path(row)
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line {i}\n" for i in range(40)))
    tail = app_supervisor.read_log_tail(row, lines=3)
    assert tail == "line 37\nline 38\nline 39"
    assert "line 0\n" not in app_supervisor.read_log_tail(row, lines=2000)


def test_the_smokes_scratch_removal_never_follows_a_link_above_it(agent_tree, tmp_path, monkeypatch):
    row = _row("smoke")
    tree = _tree(agent_tree / row["rel_path"])
    out = tmp_path / "outside"

    async def fake_start(r, name, *, release_dir=None, data_dir=None, allow_hosts=None):
        bucket = releases.app_release_dir(r).parent
        out.mkdir()
        (out / r["id"] / data_dir.name).mkdir(parents=True)
        (out / r["id"] / data_dir.name / "kept").write_text("kept")
        _swap_for_link(bucket, out)
        return app_supervisor.Instance(row_id=r["id"], name=name, row=r, release_dir=release_dir,
                                       data_dir=data_dir)

    async def fake_stop(row_id, name=None):
        return 1

    monkeypatch.setattr(app_supervisor, "start", fake_start)
    monkeypatch.setattr(app_supervisor, "stop", fake_stop)
    asyncio.run(app_supervisor.smoke(row, tree))
    (scratch,) = (out / row["id"]).iterdir()
    assert (scratch / "kept").read_text() == "kept"


def test_the_start_makes_its_folders_with_no_link_followed(agent_tree, tmp_path, monkeypatch):
    row = _row("start")
    launched: list[str] = []

    async def fake_launch(inst, *, preview, allow_hosts=None, secrets=None):
        launched.append(inst.name)
        raise app_sandbox.AppStartError("not really")

    monkeypatch.setattr(app_supervisor, "_launch", fake_launch)
    release = releases.app_release_dir(row) / "1"
    _tree(release)
    out = tmp_path / "outside"
    out.mkdir()
    data = releases.app_data_dir(row)
    data.parent.parent.mkdir(parents=True)
    data.parent.symlink_to(out, target_is_directory=True)
    with pytest.raises(app_sandbox.AppStartError):
        asyncio.run(app_supervisor.start(row, app_supervisor.CHECK, release_dir=release, data_dir=data))
    assert list(out.iterdir()) == [] and launched == []
    data.parent.unlink()
    app_supervisor._instances.clear()
    with pytest.raises(app_sandbox.AppStartError):
        asyncio.run(app_supervisor.start(row, app_supervisor.CHECK, release_dir=release, data_dir=data))
    assert data.is_dir() and (release / "data").is_dir() and launched == ["check"]
