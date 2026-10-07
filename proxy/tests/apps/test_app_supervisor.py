"""The app sandbox and supervisor (APPS.md "The app sandbox", "The
supervisor").

Load-bearing: an app server sees its release read-only, its data
read-write and nothing else; it reaches the proxy port and nothing on the
host or the internet; a sibling app's port is unreachable; the launch
token and the viewer claims are signed with keys the session validators
reject; a crash goes to backoff and the routes learn a retry time; an idle
server stops; the smoke never touches the live data.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from services.apps import app_sandbox, app_supervisor, app_tokens, releases
from storage import database as task_store

AGENT = "app-sup-agent"

_needs_runtime = pytest.mark.skipif(
    not (shutil.which("pasta") and shutil.which("bwrap") and app_sandbox.bun_binary()),
    reason="requires pasta + bwrap + Bun on this host",
)

# A server that reports what it can see and reach: the acceptance probes.
PROBE_SERVER = """
const port = Number(process.env.PORT || 3000);
console.log("probe server starting on " + port);
Bun.serve({ port, hostname: "0.0.0.0", async fetch(req) {
  const u = new URL(req.url);
  if (u.pathname === "/_health") return new Response("ok");
  if (u.pathname === "/env") return Response.json({
    app: process.env.OTODOCK_APP_ID, token: !!process.env.OTODOCK_APP_TOKEN,
    key: process.env.OTODOCK_APP_PUBLIC_KEY || "", cwd: process.cwd(),
    home: process.env.HOME, preview: process.env.OTODOCK_PREVIEW || "" });
  if (u.pathname === "/probe") {
    const t = u.searchParams.get("url");
    try { const r = await fetch(t, { signal: AbortSignal.timeout(2500) });
          return new Response("reached:" + r.status); }
    catch (e) { return new Response("err:" + String(e).slice(0, 80)); }
  }
  if (u.pathname === "/write") { await Bun.write("/app/data/probe.txt", "x"); return new Response("written"); }
  if (u.pathname === "/readonly") {
    try { await Bun.write("/app/client/x.txt", "x"); return new Response("wrote"); }
    catch (e) { return new Response("refused"); }
  }
  if (u.pathname === "/ls") {
    const { readdirSync } = await import("node:fs");
    const seen = {};
    for (const p of ["/workspace", "/users", "/config", "/knowledge", "/home", "/app", "/app/data"]) {
      try { seen[p] = readdirSync(p).length; } catch (e) { seen[p] = "absent"; }
    }
    return Response.json(seen);
  }
  if (u.pathname === "/crash") { setTimeout(() => process.exit(3), 50); return new Response("bye"); }
  return new Response("hello");
}});
"""


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    # A test's loop is gone by now: kill what is left synchronously (the
    # tests stop their own servers; this catches a failed assertion).
    import contextlib
    import signal
    for inst in list(app_supervisor._instances.values()):
        proc = inst.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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


def _tree(root: Path, server: str = PROBE_SERVER) -> Path:
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


def _get(url: str, timeout: float = 5.0) -> str:
    import httpx
    return httpx.get(url, timeout=timeout).text


# ───────────────────────────── the sandbox (no process) ─────────────────────


def test_app_sandbox_mounts_only_the_release_and_its_data(agent_tree, tmp_path, monkeypatch):
    fake_bun = tmp_path / "bun"
    fake_bun.write_text("#!/bin/sh\n")
    fake_bun.chmod(0o755)
    monkeypatch.setattr(config, "BUN_BIN", str(fake_bun))
    row = _row()
    release = tmp_path / "rel"
    _tree(release)
    data = tmp_path / "data"
    data.mkdir()
    argv = app_sandbox.build_command(row, release, data, 18081, "server/index.ts",
                                     ["203.0.113.5"])
    assert argv[0].endswith("oto-sandbox-net")
    launcher = argv[: argv.index("--")]
    assert "--block-private" in launcher and "--egress-deny" in launcher
    assert launcher[launcher.index("--inbound") + 1] == "18081:3000"
    assert launcher[launcher.index("--forward") + 1] == str(config.PORT)
    assert launcher[launcher.index("--allow-host") + 1] == "203.0.113.5"
    bwrap = argv[argv.index("--") + 1:]
    joined = " ".join(bwrap)
    assert f"--ro-bind {release} /app" in joined
    assert f"--bind {data} /app/data" in joined
    assert f"--ro-bind {fake_bun} /opt/otodock/bin/bun" in joined
    assert "--chdir /app" in joined
    assert "--cap-drop ALL" in joined and "--unshare-pid" in joined
    # No workspace, no users, no config, no knowledge, no MCP tree.
    for forbidden in ("/workspace", "/users", "/config", "/knowledge", str(config.MCPS_DIR)):
        assert forbidden not in bwrap
    assert bwrap[-3:] == ["/opt/otodock/bin/bun", "run", "server/index.ts"]
    # A Bun under /usr rides the system bind; nothing extra is bound.
    monkeypatch.setattr(config, "BUN_BIN", "/usr/local/bin/bun")
    monkeypatch.setattr(app_sandbox, "bun_binary", lambda: "/usr/local/bin/bun")
    argv2 = app_sandbox.build_command(row, release, data, 18081, "server/index.ts")
    assert "/opt/otodock/bin/bun" not in argv2 and argv2[-3] == "/usr/local/bin/bun"


def test_app_env_carries_only_what_the_server_needs(agent_tree):
    row = _row()
    env = app_sandbox.build_env(row, "tok", "pub", preview=True)
    assert env["PORT"] == "3000" and env["OTODOCK_APP_ID"] == row["id"]
    assert env["OTODOCK_APP_TOKEN"] == "tok" and env["OTODOCK_APP_PUBLIC_KEY"] == "pub"
    assert env["OTODOCK_PROXY_URL"] == f"http://127.0.0.1:{config.PORT}"
    assert env["OTODOCK_PREVIEW"] == "1" and env["HOME"] == "/tmp"
    assert "OTODOCK_PREVIEW" not in app_sandbox.build_env(row, "t", "p")
    assert not any(k in env for k in ("JWT_SECRET", "PROXY_API_KEY", "DATABASE_URL"))


def test_start_refuses_without_bun_or_approval(agent_tree, tmp_path, monkeypatch):
    row = _released(agent_tree)
    monkeypatch.setattr(config, "BUN_BIN", "")
    with pytest.raises(app_sandbox.AppStartError):
        asyncio.run(app_supervisor.start(row))
    # A manifest waiting for approval never starts a process (checked before
    # the launch, so no Bun is needed to prove it).
    fake_bun = tmp_path / "bun"
    fake_bun.write_text("#!/bin/sh\n")
    fake_bun.chmod(0o755)
    monkeypatch.setattr(config, "BUN_BIN", str(fake_bun))
    row = task_store.upsert_app(AGENT, "alice", "alice-sub", "probe", actions_json='[{"id":"x"}]')
    row = task_store.get_app(row["id"])
    with pytest.raises(app_supervisor.AppUnavailable) as e:
        asyncio.run(app_supervisor.start(row))
    assert e.value.state == "unapproved"
    # A folder without a server is a static app: no process, "static".
    static = _row("static")
    tree = tmp_path / "static"
    (tree / "client").mkdir(parents=True)
    (tree / "client" / "index.html").write_text("<p>s</p>")
    inst = asyncio.run(app_supervisor.start(static, release_dir=tree, data_dir=tmp_path / "d"))
    assert inst.state == "static" and inst.proc is None


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


def test_a_start_the_caller_gives_up_on_leaves_nothing_running(agent_tree, tmp_path, monkeypatch):
    """A cancelled start (a check's budget ran out, the request went away)
    kills the process it spawned instead of leaving it unregistered."""
    row = _row()
    tree = _tree(tmp_path / "tree", "export {}")
    monkeypatch.setattr(app_sandbox, "build_command", lambda *a, **k: ["sleep", "60"])
    monkeypatch.setattr(app_sandbox, "bun_binary", lambda: "/bin/true")

    async def never_healthy(inst):
        await asyncio.sleep(60)
        return False

    monkeypatch.setattr(app_supervisor, "_health", never_healthy)
    seen: dict = {}

    async def run():
        task = asyncio.create_task(app_supervisor.start(row, "check", release_dir=tree,
                                                        data_dir=tmp_path / "d", allow_hosts=[]))
        for _ in range(50):
            if pid_box:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.2)       # past the launch's first second: in the health wait
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)
        seen["pid"] = pid_box[0]

    pid_box: list[int] = []
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        proc = await real(*argv, **kw)
        pid_box.append(proc.pid)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    asyncio.run(run())
    pid = seen["pid"]
    alive = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    assert alive == "" or alive.startswith("Z"), alive
    assert app_supervisor._instances.get((row["id"], "check")) is None


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


# ───────────────────────────── real processes ───────────────────────────────


def _host_ip() -> str:
    try:
        out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return out[out.index("src") + 1] if "src" in out else ""
    except Exception:
        return ""


@_needs_runtime
def test_server_runs_isolated_reaches_nothing_and_stops_when_idle(agent_tree, monkeypatch):
    row1 = _released(agent_tree, "one")
    row2 = _released(agent_tree, "two")

    async def run() -> None:
        inst1 = await app_supervisor.start(row1)
        assert inst1.state == "up" and inst1.proc is not None
        assert _get(f"{inst1.base_url}/_health") == "ok"
        env = __import__("json").loads(_get(f"{inst1.base_url}/env"))
        assert env["app"] == row1["id"] and env["token"] and env["cwd"] == "/app"
        assert env["key"] == app_tokens.public_key_b64(row1["id"]) and env["preview"] == ""
        # Its data is the only writable place; the release is read-only.
        assert _get(f"{inst1.base_url}/write") == "written"
        assert (releases.app_data_dir(row1) / "probe.txt").is_file()
        assert _get(f"{inst1.base_url}/readonly") == "refused"
        seen = __import__("json").loads(_get(f"{inst1.base_url}/ls"))
        assert seen["/workspace"] == "absent" and seen["/users"] == "absent"
        assert seen["/config"] == "absent" and seen["/home"] == "absent"
        assert seen["/app"] != "absent" and seen["/app/data"] != "absent"
        # The launch token is the one the registry holds and verifies.
        assert app_tokens.verify(inst1.token, row1["id"], app_tokens.PURPOSE_LAUNCH)
        # A sibling app cannot reach it, on loopback or on the host's LAN
        # address; the public internet is closed too.
        inst2 = await app_supervisor.start(row2)
        assert inst2.host_port != inst1.host_port
        assert _get(f"{inst2.base_url}/probe?url=http://127.0.0.1:{inst1.host_port}/").startswith("err")
        host_ip = _host_ip()
        if host_ip:
            assert _get(f"{inst2.base_url}/probe?url=http://{host_ip}:{inst1.host_port}/",
                        timeout=8).startswith("err")
        assert _get(f"{inst2.base_url}/probe?url=https://example.com/", timeout=8).startswith("err")
        # The log has the server's own lines, timestamped and tagged.
        await asyncio.sleep(0.2)
        assert "probe server starting on 3000" in app_supervisor.read_log_tail(row1)
        assert app_supervisor.status(row1["id"])["server"] == "up"
        # Idle: nothing happened for longer than the limit → stopped; an
        # open bridged socket keeps the other server alive through the
        # same sweep.
        monkeypatch.setattr(app_supervisor, "IDLE_STOP_S", 0)
        app_supervisor.ws_opened(row2["id"])
        inst1.last_activity -= 1
        inst2.last_activity -= 1
        await app_supervisor.sweep_once()
        assert app_supervisor.get(row1["id"]) is None and inst1.proc.returncode is not None
        assert app_supervisor.status(row1["id"])["server"] == "stopped"
        assert app_supervisor.get(row2["id"]) is inst2
        app_supervisor.ws_closed(row2["id"])
        # A row that disappeared takes its process with it.
        task_store.delete_app(row2["id"])
        await app_supervisor.sweep_once()
        assert app_supervisor.get(row2["id"]) is None and inst2.proc.returncode is not None
        await app_supervisor.stop_all()

    asyncio.run(run())


@_needs_runtime
def test_crash_goes_to_backoff_with_a_retry_time_and_one_notification(agent_tree):
    row = _released(agent_tree, "crashy")
    notified: list[str] = []

    async def fake_notify(inst):
        notified.append(inst.row_id)

    async def run() -> None:
        with patch.object(app_supervisor, "_notify_crash", fake_notify):
            inst = await app_supervisor.start(row)
            assert _get(f"{inst.base_url}/crash") == "bye"
            await asyncio.wait_for(inst.closed.wait(), timeout=10)
            assert inst.state == "backoff" and "code 3" in inst.last_error
            with pytest.raises(app_supervisor.AppUnavailable) as e:
                await app_supervisor.ensure_up(row)
            assert e.value.retry_after >= 1 and e.value.state == "backoff"
            assert app_supervisor.status(row["id"])["retry_after"] >= 0
            assert notified == [row["id"]]
            # After the backoff a request starts it again; the backoff
            # doubled and resets on a healthy start.
            await asyncio.sleep(max(0.0, inst.next_start_at - time.monotonic()) + 0.1)
            fresh = await app_supervisor.ensure_up(row)
            assert fresh.state == "up" and fresh is not inst
            assert fresh.backoff_s == app_supervisor.BACKOFF_MIN_S
            assert notified == [row["id"]]
            await app_supervisor.stop_all()

    asyncio.run(run())


@_needs_runtime
def test_smoke_runs_the_working_tree_on_scratch_data(agent_tree):
    row = _row("smoke")
    tree = _tree(agent_tree / row["rel_path"])

    async def run() -> None:
        report = await app_supervisor.smoke(row, tree)
        assert report["ok"] and report["server"] == "up"
        assert "probe server starting" in report["log"]
        assert not releases.app_data_dir(row).exists()
        assert app_supervisor.get(row["id"], "check") is None
        broken = _tree(agent_tree / "users/alice/workspace/apps/broken",
                       server='console.log("dying"); process.exit(7);\n')
        report = await app_supervisor.smoke(_row("broken"), broken)
        assert not report["ok"] and "dying" in report["log"]
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_free_port_is_a_loopback_port():
    port = app_supervisor._free_port()
    assert 1024 < port < 65536
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


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
