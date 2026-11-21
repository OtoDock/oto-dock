"""An app server on a compute machine.

Load-bearing: the description names no path of this host; the proxy dials
the link's own peer and nothing a frame says; a machine's refusal is scrubbed
before anything reads it; a server the machine lost is neither a crash nor a
leak; a restart closes what another process started; a start in flight is
never read as lost. With no compute machine nothing starts.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import config
from services.apps import app_compute, app_sandbox, app_supervisor, app_tokens, releases
from storage import database as task_store
from tests.apps.conftest import MACHINE, FakeCM

AGENT = "app-compute-agent"


@pytest.fixture(autouse=True)
def _clean():
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_compute._servers.clear()
    yield
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_compute._servers.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    (root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", root.resolve())
    return root / AGENT


def _released(agent_tree: Path, slug: str = "probe") -> dict:
    row = task_store.upsert_app(AGENT, "", None, slug, title=slug.title(), rel_path=f"workspace/apps/{slug}", kind="folder")
    src = agent_tree / row["rel_path"]
    (src / "client").mkdir(parents=True)
    (src / "server").mkdir()
    (src / "app.json").write_text('{"title": "Probe"}')
    (src / "client" / "index.html").write_text("<p>x</p>")
    (src / "server" / "index.ts").write_text("console.log(1)")
    rel, sha, _n = releases.cut_folder_release(row, src)
    return task_store.set_app_release(row["id"], rel, sha)


def _inst(row: dict, proc=None, name="live") -> app_supervisor.Instance:
    return app_supervisor.Instance(row_id=row["id"], name=name, row=row, release_dir=Path("/x"), data_dir=Path("/y"),
                                   proc=proc)


# ─────────────────────────── the description ───────────────────────────────


def test_the_description_names_nothing_of_this_host(agent_tree):
    row = _released(agent_tree)
    rel, data = releases.live_release_dir(row), releases.app_data_dir(row)
    data.mkdir(parents=True)
    desc = app_compute.describe(row, rel, data, ["1.2.3.4"])
    assert {(b["root"], b["sandbox"], b["rw"]) for b in desc["binds"]} == {("agents", "/app", False),
                                                                           ("agents", "/app/data", True)}
    assert all(not b["rel"].startswith("/") for b in desc["binds"])
    assert not desc["extra_binds"] and desc["beneath"] is True and desc["cwd"] == "/app"
    assert desc["inbound"] == "0:3000" and desc["egress_deny"] is True
    assert list(desc["forwards"]) == ["proxy"] and list(desc["allow_hosts"]) == ["1.2.3.4"]


def test_a_personal_apps_description_stays_in_its_owners_buckets(agent_tree):
    task_store.upsert_user("owner-sub", "owner@test.com", "Owner", "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("owner", "owner-sub"))
        conn.commit()
    task_store.add_user_agent("owner-sub", AGENT, "manager", "test")
    row = task_store.upsert_app(AGENT, "owner", "owner-sub", "home", title="Home",
                                rel_path="users/owner/workspace/apps/home", kind="folder")
    src = agent_tree / row["rel_path"]
    (src / "client").mkdir(parents=True)
    (src / "server").mkdir()
    (src / "app.json").write_text('{"title": "Home"}')
    (src / "client" / "index.html").write_text("<p>x</p>")
    (src / "server" / "index.ts").write_text("console.log(1)")
    rel, sha, _n = releases.cut_folder_release(row, src)
    row = task_store.set_app_release(row["id"], rel, sha)
    data = releases.app_data_dir(row)
    data.mkdir(parents=True)
    desc = app_compute.describe(row, releases.live_release_dir(row), data, None)
    binds = {b["sandbox"]: b for b in desc["binds"]}
    assert all(b["root"] == "agents" for b in desc["binds"])
    assert binds["/app"]["rel"].startswith("app-releases/users/owner/") and not binds["/app"]["rw"]
    assert binds["/app/data"]["rel"] == "app-data/users/owner/home" and binds["/app/data"]["rw"]


def test_a_compute_table_needs_no_bun_on_the_proxy(agent_tree, monkeypatch):
    """The machine runs its own Bun: the table is built on a host that has
    none, and no bind of it names one."""
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setenv("PATH", "")
    row = _released(agent_tree)
    rel = releases.live_release_dir(row)
    desc = app_compute.describe(row, rel, rel / "data", None)
    assert {b["sandbox"] for b in desc["binds"]} == {"/app", "/app/data"}
    assert not any("bun" in b["rel"] or "bun" in b["sandbox"] for b in (*desc["binds"], *desc["extra_binds"]))


def test_a_source_outside_the_agents_folder_is_refused(agent_tree, tmp_path):
    row = _released(agent_tree)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    planted = agent_tree / "app-data" / "planted"
    planted.parent.mkdir(parents=True)
    planted.symlink_to(outside)
    with pytest.raises(app_sandbox.AppStartError):
        app_compute.describe(row, releases.live_release_dir(row), planted, None)


def test_an_agents_folder_reached_by_a_link_still_spells_agent_roots(tmp_path, monkeypatch):
    real = tmp_path / "real-agents"
    (real / AGENT / "workspace").mkdir(parents=True)
    link = tmp_path / "agents"
    link.symlink_to(real)
    monkeypatch.setattr(config, "AGENTS_DIR", link)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", real.resolve())
    row = _released(link / AGENT)
    rel = releases.live_release_dir(row)
    assert {b["root"] for b in app_compute.describe(row, rel, rel / "data", None)["binds"]} == {"agents"}


# ─────────────────────────── the address ───────────────────────────────────


def test_the_address_is_the_links_own_peer(cm):
    assert app_compute.machine_address(MACHINE) == "127.0.0.1"


@pytest.mark.parametrize("peer", ["::1", "10.0.0.9"])
def test_an_address_the_link_rule_would_not_admit_is_refused(monkeypatch, peer):
    fake = FakeCM(peer=peer)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: fake)
    with pytest.raises(app_sandbox.AppStartError):
        app_compute.machine_address(MACHINE)


def test_a_machine_that_is_not_connected_has_no_address(cm):
    cm.connected.clear()
    with pytest.raises(app_sandbox.AppStartError):
        app_compute.machine_address(MACHINE)


# ─────────────────────────── a start ───────────────────────────────────────


def test_a_start_goes_to_the_machine_and_dials_the_links_peer(agent_tree, placed, healthy, monkeypatch):
    async def no_local(*a, **k):
        raise AssertionError("a compute start spawned a local process")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_local)
    row = _released(agent_tree)

    async def run():
        inst = await app_supervisor.start(row)
        assert isinstance(inst.proc, app_compute.RemoteProcess) and inst.state == app_supervisor.UP
        assert inst.base_url == "http://127.0.0.1:45001" and inst.ws_url("x") == "ws://127.0.0.1:45001/x"
        assert inst.proc.pid == 77
        frame = placed.of("app_server_start")[0]
        assert frame["boot"] == app_compute.BOOT and frame["app_id"] == row["id"] and frame["instance"] == "live"
        assert frame["argv"] == ["bun", "run", "server/index.ts"]
        assert frame["env"]["OTODOCK_APP_TOKEN"] == inst.token
        assert frame["sandbox"]["inbound"] == "0:3000"
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_with_no_compute_machine_no_server_starts(agent_tree, cm, monkeypatch):
    """The platform starts no app server itself: with no compute machine a
    page is told so and asked to come back, a deploy fails with the same
    sentence, nothing is started as a child of the platform and nothing is
    sent to any machine."""
    monkeypatch.setattr(app_compute, "place", lambda: None)
    monkeypatch.setattr("services.remote.compute_bootstrap.runs_own", lambda: False)

    async def no_local(*argv, **kw):
        raise AssertionError("a server was started inside the platform")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_local)
    row = _released(agent_tree)

    async def run():
        with pytest.raises(app_supervisor.AppUnavailable) as page:
            await app_supervisor.ensure_up(row)
        assert page.value.reason == app_compute.NOT_CONNECTED == (
            "The compute service is not connected, so the app did not start.")
        assert page.value.state == app_supervisor.STARTING and page.value.retry_after == 30
        assert app_supervisor.get(row["id"]) is None
        with pytest.raises(app_sandbox.AppStartError) as deploy:
            await app_supervisor.start(row, release_dir=releases.live_release_dir(row))
        assert app_compute.NOT_CONNECTED in str(deploy.value)
        assert not cm.sent
        app_supervisor._instances.clear()

    asyncio.run(run())


def test_a_refusal_is_scrubbed_before_anything_reads_it(agent_tree, placed):
    row = _released(agent_tree)

    def refuse(msg):
        if msg["type"] == "app_server_start":
            return RuntimeError(f"Satellite command error: the server said {msg['env']['OTODOCK_APP_TOKEN']}")
        return {"status": "ok"}

    placed.answer = refuse

    async def run():
        with pytest.raises(app_sandbox.AppStartError) as e:
            await app_supervisor.start(row)
        token = placed.of("app_server_start")[0]["env"]["OTODOCK_APP_TOKEN"]
        assert token not in str(e.value) and "[redacted]" in str(e.value)
        assert "Satellite command error" not in str(e.value)
        assert token not in app_supervisor.get(row["id"]).last_error

    asyncio.run(run())


def test_a_start_with_no_answer_is_stopped_on_the_machine(agent_tree, placed):
    """A timeout says nothing about the machine, which may still bring the
    server up, and with the link up no sweep would ever find it."""
    row = _released(agent_tree)

    def no_answer(msg):
        if msg["type"] == "app_server_start":
            return RuntimeError(f"Satellite {MACHINE[:8]} command timeout (30.0s)")
        return {"status": "ok", "stopped": True}

    placed.answer = no_answer

    async def run():
        with pytest.raises(app_sandbox.AppStartError) as e:
            await app_supervisor.start(row)
        assert str(e.value) == "The compute service did not answer, so the app did not start."
        for _ in range(200):
            if placed.of("app_server_stop"):
                break
            await asyncio.sleep(0.01)
        assert [m["server_id"] for m in placed.of("app_server_stop")] == [placed.of("app_server_start")[0]["server_id"]]

    asyncio.run(run())


def test_a_refused_start_needs_no_stop(agent_tree, placed):
    """The machine answered: it cleaned up after itself."""
    row = _released(agent_tree)

    def refuse(msg):
        return RuntimeError("Satellite command error: Bun is not installed on this machine.")

    placed.answer = refuse

    async def run():
        with pytest.raises(app_sandbox.AppStartError) as e:
            await app_supervisor.start(row)
        assert str(e.value) == "Bun is not installed on this machine."
        await asyncio.sleep(0.05)
        assert not placed.of("app_server_stop")

    asyncio.run(run())


def test_a_start_with_no_port_in_its_ack_fails(agent_tree, placed, healthy):
    row = _released(agent_tree)
    placed.answer = lambda msg: {"status": "ok"}

    async def run():
        with pytest.raises(app_sandbox.AppStartError):
            await app_supervisor.start(row)

    asyncio.run(run())


def test_a_start_the_caller_abandons_is_stopped_on_the_machine(agent_tree, placed, monkeypatch):
    row = _released(agent_tree)
    release = asyncio.Event()
    original = placed.send_command

    async def slow(machine_id, msg, *, timeout=30.0, command_id=None):
        if msg["type"] == "app_server_start":
            placed.sent.append(msg)
            await release.wait()
            return {"status": "ok"}
        return await original(machine_id, msg, timeout=timeout)

    monkeypatch.setattr(placed, "send_command", slow)

    async def run():
        task = asyncio.create_task(app_supervisor.start(row))
        for _ in range(200):
            if placed.of("app_server_start"):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(200):
            if placed.of("app_server_stop"):
                break
            await asyncio.sleep(0.01)
        assert placed.of("app_server_stop")[0]["server_id"] == placed.of("app_server_start")[0]["server_id"]
        assert app_supervisor.get(row["id"]) is None

    asyncio.run(run())


# ─────────────────────────── the boot's grace ──────────────────────────────


@pytest.fixture
def in_the_grace(monkeypatch, cm):
    monkeypatch.setattr(app_compute, "_grace_until", time.monotonic() + 30)
    monkeypatch.setattr("services.remote.compute_bootstrap.runs_own", lambda: True)
    monkeypatch.setattr(app_compute, "place", lambda: None)


def _spawns_here(monkeypatch):
    """Every process the platform would start itself, faked and listed."""
    spawned: list = []

    async def spawn_local(*argv, **kw):
        spawned.append(argv)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return SimpleNamespace(pid=os.getpid(), returncode=None, stdout=reader, wait=asyncio.Event().wait)

    async def ok(inst):
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_local)
    monkeypatch.setattr(app_supervisor, "_health", ok)
    return spawned


def test_a_live_server_waits_for_the_compute_machine_after_a_boot(agent_tree, in_the_grace, monkeypatch):
    spawned = _spawns_here(monkeypatch)
    row = _released(agent_tree)

    async def run():
        with pytest.raises(app_supervisor.AppUnavailable) as e:
            await app_supervisor.ensure_up(row)
        assert e.value.state == app_supervisor.STARTING and e.value.retry_after == 2
        assert not spawned and app_supervisor.get(row["id"]) is None

    asyncio.run(run())


def test_the_grace_ends_when_the_compute_machine_signs_in(agent_tree, in_the_grace, cm, healthy, monkeypatch):
    spawned = _spawns_here(monkeypatch)
    row = _released(agent_tree)
    monkeypatch.setattr(app_compute, "place", lambda: {"id": MACHINE, "name": "Compute"})

    async def run():
        inst = await app_supervisor.start(row)
        assert isinstance(inst.proc, app_compute.RemoteProcess) and not spawned
        await app_supervisor.stop_all()

    asyncio.run(run())


@pytest.mark.parametrize("own", [True, False])
def test_past_the_grace_a_page_is_told_the_compute_service_is_not_connected(
        agent_tree, in_the_grace, monkeypatch, own):
    """After the start's grace, and at once on an install that runs no
    compute machine of its own, the page's 503 says why and nothing is
    started inside the platform."""
    spawned = _spawns_here(monkeypatch)
    if own:
        monkeypatch.setattr(app_compute, "_grace_until", time.monotonic() - 1)
    else:
        monkeypatch.setattr("services.remote.compute_bootstrap.runs_own", lambda: False)
    row = _released(agent_tree)

    async def run():
        with pytest.raises(app_supervisor.AppUnavailable) as e:
            await app_supervisor.ensure_up(row)
        assert e.value.reason == app_compute.NOT_CONNECTED and e.value.retry_after == 30
        assert not spawned and app_supervisor.get(row["id"]) is None

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["deploy", "preview"])
def test_a_deploy_and_a_preview_do_not_wait(agent_tree, in_the_grace, monkeypatch, kind):
    """A deploy names its release and a person is waiting on it; a preview
    has a database of its own, so it opens nothing the old server holds.
    Neither waits out the grace: with no compute machine each fails at once
    with the sentence, and nothing is started inside the platform."""
    spawned = _spawns_here(monkeypatch)
    row = _released(agent_tree)
    release = releases.live_release_dir(row)
    if kind == "preview":
        (releases.preview_dir(row)).mkdir(parents=True)
        (releases.preview_dir(row) / "server").mkdir()
        (releases.preview_dir(row) / "server" / "index.ts").write_text("console.log(1)")

    async def run():
        with pytest.raises(app_sandbox.AppStartError) as e:
            await (app_supervisor.start(row, release_dir=release) if kind == "deploy"
                   else app_supervisor.start(row, app_supervisor.PREVIEW))
        assert app_compute.NOT_CONNECTED in str(e.value) and not spawned
        app_supervisor._instances.clear()

    asyncio.run(run())


def test_the_grace_is_counted_from_the_boots_registration(monkeypatch):
    monkeypatch.setattr(app_compute, "_grace_until", 0.0)
    monkeypatch.setattr("core.remote.satellite_link.add_alive_report_hook", lambda fn, *, name: None)
    before = time.monotonic()
    app_compute.register()
    assert before + app_compute.BOOT_GRACE_S <= app_compute._grace_until <= time.monotonic() + app_compute.BOOT_GRACE_S


def test_a_held_request_answers_the_503_the_page_retries(agent_tree, in_the_grace, monkeypatch):
    from api.apps import app_proxy
    row = _released(agent_tree)

    async def run():
        request = SimpleNamespace(scope={"query_string": b""}, method="GET")
        caller = SimpleNamespace(instance=app_supervisor.LIVE)
        response = await app_proxy.proxy_request(row, caller, request, "/x")
        assert response.status_code == 503 and response.headers["retry-after"] == "2"
        assert response.headers["x-otodock-server"] == app_supervisor.STARTING

    asyncio.run(run())


# ─────────────────────────── output and exit ───────────────────────────────


def test_output_reaches_the_log_with_every_secret_scrubbed(agent_tree, placed, healthy, monkeypatch):
    """The tail is filled before each line's write lands in the log (a disk
    under load, here a slow write): the test waits on the log itself."""
    row = _released(agent_tree)
    write = app_supervisor.append_log

    async def slow_append(r, line):
        await asyncio.sleep(0.05)
        await write(r, line)
    monkeypatch.setattr(app_supervisor, "append_log", slow_append)

    async def run():
        inst = await app_supervisor.start(row)
        frame = placed.of("app_server_start")[0]
        app_compute.on_frame(MACHINE, {"type": "app_server_output", "server_id": frame["server_id"],
                                       "text": f"token is {inst.token}\nready\n"})
        for _ in range(500):
            if "ready" in app_supervisor.read_log_tail(row):
                break
            await asyncio.sleep(0.01)
        assert not any(inst.token in line for line in inst._tail)
        assert any("[redacted]" in line for line in inst._tail)
        log = releases.log_path(row).read_text()
        assert inst.token not in log and "ready" in log
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_a_print_loop_cannot_grow_the_proxys_memory(agent_tree, placed):
    """No pipe backs this reader, so nothing pauses the sender: past the cap
    a line is dropped, and what the log writer takes makes room again."""
    row = _released(agent_tree)

    async def run():
        proc = app_compute.RemoteProcess(MACHINE, _inst(row))
        chunk = ("x" * 63 + "\n") * 1024
        for _ in range(20 * 16):
            app_compute.on_frame(MACHINE, {"type": "app_server_output", "server_id": proc.server_id, "text": chunk})
        held = len(proc.stdout._buffer)
        assert app_compute._OUTPUT_HELD_MAX <= held <= app_compute._OUTPUT_HELD_MAX + len(chunk)
        while proc.stdout._buffer:
            await proc.stdout.readline()
        app_compute.on_frame(MACHINE, {"type": "app_server_output", "server_id": proc.server_id, "text": "again\n"})
        assert await proc.stdout.readline() == b"again\n"
        proc.finish(0)

    asyncio.run(run())


def test_a_frame_for_another_machines_server_is_ignored(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run():
        inst = await app_supervisor.start(row)
        sid = placed.of("app_server_start")[0]["server_id"]
        app_compute.on_frame("another", {"type": "app_server_exit", "server_id": sid, "code": 1})
        await asyncio.sleep(0)
        assert inst.proc.returncode is None and inst.state == app_supervisor.UP
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_a_crash_on_the_machine_goes_to_backoff_and_tells_the_owner(agent_tree, placed, healthy, monkeypatch):
    row = _released(agent_tree)
    told: list = []

    async def notify(inst):
        told.append(inst.last_error)

    monkeypatch.setattr(app_supervisor, "_notify_crash", notify)

    async def run():
        inst = await app_supervisor.start(row)
        sid = placed.of("app_server_start")[0]["server_id"]
        app_compute.on_frame(MACHINE, {"type": "app_server_exit", "server_id": sid, "code": 3})
        await inst.closed.wait()
        assert inst.state == app_supervisor.BACKOFF and "code 3" in inst.last_error
        assert told == [inst.last_error]

    asyncio.run(run())


def test_a_crashed_server_tells_the_routes_when_to_retry_and_starts_again_after_its_backoff(
        agent_tree, placed, healthy, monkeypatch):
    row = _released(agent_tree)
    told: list = []

    async def notify(inst):
        told.append(inst.row_id)

    monkeypatch.setattr(app_supervisor, "_notify_crash", notify)

    async def run():
        inst = await app_supervisor.start(row)
        sid = placed.of("app_server_start")[0]["server_id"]
        app_compute.on_frame(MACHINE, {"type": "app_server_exit", "server_id": sid, "code": 3})
        await inst.closed.wait()
        with pytest.raises(app_supervisor.AppUnavailable) as e:
            await app_supervisor.ensure_up(row)
        assert e.value.retry_after >= 1 and e.value.state == app_supervisor.BACKOFF
        assert app_supervisor.status(row["id"])["retry_after"] >= 0
        assert len(placed.of("app_server_start")) == 1
        # Past the backoff a request starts it again. The wait doubled for
        # the next crash and a healthy start puts it back. The owner was
        # told once.
        assert inst.backoff_s == 2 * app_supervisor.BACKOFF_MIN_S
        inst.next_start_at = time.monotonic() - 1
        fresh = await app_supervisor.ensure_up(row)
        assert fresh is not inst and fresh.state == app_supervisor.UP
        assert fresh.backoff_s == app_supervisor.BACKOFF_MIN_S
        assert len(placed.of("app_server_start")) == 2 and told == [row["id"]]
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_a_stop_reaches_the_machine_and_is_no_crash(agent_tree, placed, healthy, monkeypatch):
    row = _released(agent_tree)

    async def notify(inst):
        raise AssertionError("a stop notified the owner")

    monkeypatch.setattr(app_supervisor, "_notify_crash", notify)

    async def run():
        inst = await app_supervisor.start(row)
        assert await app_supervisor.stop(row["id"]) == 1
        assert placed.of("app_server_stop")
        assert inst.state == app_supervisor.STOPPED and app_supervisor.get(row["id"]) is None
        assert inst.proc.server_id not in app_compute._servers

    asyncio.run(run())


def test_a_stop_with_the_machine_gone_still_ends_the_instance(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run():
        inst = await app_supervisor.start(row)
        placed.answer = lambda msg: RuntimeError("Satellite m not connected")
        await app_supervisor.stop(row["id"])
        assert inst.proc.returncode == -15 and inst.state == app_supervisor.STOPPED

    asyncio.run(run())


def test_a_blue_green_replace_stops_the_old_server_on_the_machine(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run():
        old = await app_supervisor.start(row)
        first = placed.of("app_server_start")[0]["server_id"]
        new = await app_supervisor.start(row, release_dir=releases.live_release_dir(row))
        assert new is not old and app_supervisor.get(row["id"]) is new
        assert [m["server_id"] for m in placed.of("app_server_stop")] == [first]
        assert old.state == app_supervisor.STOPPED
        await app_supervisor.stop_all()

    asyncio.run(run())


# ─────────────────────────── lost servers and the sweep ────────────────────


def test_a_server_lost_with_its_machine_is_no_crash_and_starts_anew(agent_tree, placed, healthy, monkeypatch):
    row = _released(agent_tree)

    async def notify(inst):
        raise AssertionError("a lost machine notified the owner")

    monkeypatch.setattr(app_supervisor, "_notify_crash", notify)

    async def run():
        first = await app_supervisor.start(row)
        first.proc.lost()
        await first.closed.wait()
        assert first.state == app_supervisor.STOPPED
        again = await app_supervisor.ensure_up(row)
        assert again is not first and again.state == app_supervisor.UP
        await app_supervisor.stop_all()

    asyncio.run(run())


def _report(current=True):
    return SimpleNamespace(machine_id=MACHINE, is_current=lambda: current, complete=True)


def test_the_sweep_names_this_process_and_loses_only_what_the_machine_lost(agent_tree, placed, healthy):
    row, other = _released(agent_tree, "a"), _released(agent_tree, "b")

    async def run():
        kept = await app_supervisor.start(row)
        gone = await app_supervisor.start(other)
        await asyncio.sleep(0.01)
        placed.answer = lambda msg: {"status": "ok", "live": [kept.proc.server_id]}
        await app_compute._sweep(_report())
        assert placed.of("app_server_sweep")[0]["boot"] == app_compute.BOOT
        assert gone.proc.returncode == -1 and kept.proc.returncode is None
        await gone.closed.wait()
        placed.answer = lambda msg: {"status": "ok", "stopped": True}
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_the_sweep_never_loses_a_start_still_in_flight(agent_tree, placed):
    row = _released(agent_tree)

    async def run():
        proc = app_compute.RemoteProcess(MACHINE, _inst(row))
        original = placed.answer

        def answer(msg):
            # Registered after the sweep went out, as a racing start would be.
            proc.registered_at = 10 ** 9
            return {"status": "ok", "live": []} if msg["type"] == "app_server_sweep" else original(msg)

        placed.answer = answer
        await app_compute._sweep(_report())
        assert proc.returncode is None

    asyncio.run(run())


def test_the_sweep_stops_a_server_nobody_here_holds(agent_tree, placed):
    async def run():
        placed.answer = lambda msg: {"status": "ok", "live": ["orphan-id"], "stopped": True}
        await app_compute._sweep(_report())
        for _ in range(200):
            if placed.of("app_server_stop"):
                break
            await asyncio.sleep(0.01)
        assert placed.of("app_server_stop")[0]["server_id"] == "orphan-id"

    asyncio.run(run())


def test_a_stale_report_acts_on_nothing(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run():
        inst = await app_supervisor.start(row)
        placed.answer = lambda msg: {"status": "ok", "live": []}
        await app_compute._sweep(_report(current=False))
        assert inst.proc.returncode is None
        placed.answer = lambda msg: {"status": "ok", "stopped": True}
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_a_sweep_runs_for_a_compute_machine_alone(placed):
    async def run():
        placed.compute.clear()
        await app_compute.on_alive_report(_report())
        await asyncio.sleep(0)
        assert not placed.of("app_server_sweep")

    asyncio.run(run())


# ─────────────────────────── a failed dial ─────────────────────────────────


def test_a_failed_dial_with_the_link_up_leaves_the_exit_frame_to_decide(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run():
        inst = await app_supervisor.start(row)
        app_compute.unreachable(inst)
        assert inst.proc.returncode is None and not inst.stop_requested
        await app_supervisor.stop_all()

    asyncio.run(run())


def test_a_failed_dial_with_the_link_down_loses_the_server(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run():
        inst = await app_supervisor.start(row)
        placed.connected.clear()
        placed.answer = lambda msg: RuntimeError("not connected")
        app_compute.unreachable(inst)
        assert inst.proc.returncode == -1 and inst.stop_requested
        await inst.closed.wait()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_the_forward_tells_the_supervisor_about_a_failed_dial_on_a_short_timeout(agent_tree, placed, healthy, monkeypatch):
    row = _released(agent_tree)
    seen: dict = {}

    class _Client:
        def build_request(self, method, url, headers, content, timeout):
            seen.update(url=url, timeout=timeout)
            return object()

        async def send(self, req, stream):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(app_supervisor, "_http", _Client)

    async def run():
        inst = await app_supervisor.start(row)
        placed.connected.clear()
        placed.answer = lambda msg: RuntimeError("not connected")
        with pytest.raises(httpx.ConnectError):
            await app_supervisor.forward(inst, "GET", "/x", [], b"")
        assert seen["url"] == "http://127.0.0.1:45001/x"
        assert seen["timeout"].connect == app_supervisor.REMOTE_CONNECT_S
        assert inst.stop_requested
        await inst.closed.wait()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_a_compute_servers_failed_dial_answers_the_503_the_page_retries(agent_tree, placed, monkeypatch):
    from api.apps import app_proxy
    row = _released(agent_tree)

    async def run():
        inst = _inst(row, proc=app_compute.RemoteProcess(MACHINE, None))

        async def fake_instance(r, c):
            return inst

        async def fake_forward(*a, **k):
            raise httpx.ConnectTimeout("no answer")

        async def fake_body(request):
            return b""

        monkeypatch.setattr(app_proxy, "instance_for", fake_instance)
        monkeypatch.setattr(app_proxy, "forward", fake_forward)
        monkeypatch.setattr(app_proxy, "_read_body", fake_body)
        monkeypatch.setattr(app_proxy, "_forward_headers", lambda r, c: [])
        request = SimpleNamespace(scope={"query_string": b""}, method="GET")
        response = await app_proxy.proxy_request(row, SimpleNamespace(), request, "/x")
        assert response.status_code == 503 and response.headers["retry-after"] == "1"
        assert response.headers["x-otodock-server"] == app_supervisor.STARTING

    asyncio.run(run())


def test_the_launch_token_stays_valid_only_through_the_registry(agent_tree, placed, healthy):
    """A server another process started holds a token this process never
    minted: nothing here can match it."""
    row = _released(agent_tree)

    async def run():
        inst = await app_supervisor.start(row)
        token = inst.token
        assert app_tokens.verify(token, row["id"], app_tokens.PURPOSE_LAUNCH)
        await app_supervisor.stop_all()
        assert app_supervisor.get(row["id"]) is None

    asyncio.run(run())


def test_a_second_start_while_one_runs_returns_the_running_instance(agent_tree, placed, healthy):
    row = _released(agent_tree)

    async def run():
        first = await app_supervisor.start(row)
        second = await app_supervisor.start(row)
        assert first is second and len(placed.of("app_server_start")) == 1
        await app_supervisor.stop_all()

    asyncio.run(run())
