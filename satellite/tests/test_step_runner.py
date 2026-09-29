"""App steps on the satellite (proxy APPS.md "Steps", 0.5.122).

Load-bearing: the frame is validated before anything is written (the
delivery id, the slug, the hash, the working folder inside the agent's
tree, the bounds); the script and the payload land in the private steps
directory, never the synced workspace, and go with the run; the
interpreter the script names runs in the synced folder with the shipped
environment plus the four paths only this machine knows; the output
streams as ``step_output`` frames and the ack carries the exit code, the
first 32 KB and the timeout; a timed-out script is killed; the link
dropping kills every running step; the capability is advertised.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid

import pytest

from satellite.config import SatelliteConfig
from satellite.sessions import step_runner
from satellite.sessions.session_manager import SessionManager

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix shell scripts")


class _WS:
    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)

    def ack(self) -> dict:
        acks = [m for m in self.sent if m.get("type") == "ack"]
        assert len(acks) == 1, self.sent
        return acks[0]

    def output(self) -> str:
        return "".join(m.get("text", "") for m in self.sent if m.get("type") == "step_output")


@pytest.fixture
def sat(tmp_path, monkeypatch):
    monkeypatch.setattr(step_runner, "otodock_dir", lambda: tmp_path / ".oto-dock")
    agents = tmp_path / "agents"
    (agents / "dev-agent" / "workspace" / "notes").mkdir(parents=True)
    (agents / "dev-agent" / "knowledge").mkdir(parents=True)
    (agents / "dev-agent" / "users" / "alice" / "workspace").mkdir(parents=True)
    cfg = SatelliteConfig(machine_id="m", machine_secret="s", platform_url="ws://x",
                          agents_dir=agents, mcps_dir=tmp_path / "mcps")
    return cfg, agents


def _frame(script: str, **over) -> dict:
    data = script.encode()
    msg = {
        "type": "step_run", "command_id": "step-abc", "delivery_id": str(uuid.uuid4()),
        "app_id": str(uuid.uuid4()), "agent_slug": "dev-agent", "run": "scripts/sync.sh",
        "script": script, "sha256": hashlib.sha256(data).hexdigest(),
        "workspace_relative": "workspace", "knowledge_relative": "knowledge",
        "env": {"OTODOCK_STEP_TOKEN": "fake-step-claim", "OTODOCK_APP_SLUG": "flow"},
        "payload_json": json.dumps({"n": 1}), "timeout": 30,
    }
    msg.update(over)
    return msg


SCRIPT = """#!/bin/sh
echo hello from the machine
echo payload: $(cat "$OTODOCK_STEP_PAYLOAD")
echo cwd: $(pwd)
echo ws: $OTODOCK_WORKSPACE_DIR
echo kn: $OTODOCK_KNOWLEDGE_DIR
echo proxy: $OTODOCK_PROXY_URL
echo token: $OTODOCK_STEP_TOKEN
echo written > "$OTODOCK_WORKSPACE_DIR/notes/step.txt"
echo script-dir: $(dirname "$OTODOCK_STEP_SCRIPT")
exit 3
"""


@pytest.mark.asyncio
async def test_a_step_runs_in_the_synced_folder_from_the_private_directory(sat):
    cfg, agents = sat
    sm = SessionManager(cfg)
    sm.local_tunnel_port = 4321
    ws = _WS()
    await sm.step_run(_frame(SCRIPT), ws)
    ack = ws.ack()
    assert ack["status"] == "ok" and ack["exit_code"] == 3 and ack["timed_out"] is False
    out = ack["output"]
    assert "hello from the machine" in out and 'payload: {"n": 1}' in out
    ws_dir = str((agents / "dev-agent" / "workspace").resolve())
    assert f"cwd: {ws_dir}" in out and f"ws: {ws_dir}" in out
    assert f"kn: {(agents / 'dev-agent' / 'knowledge').resolve()}" in out
    assert "proxy: http://127.0.0.1:4321" in out and "token: fake-step-claim" in out
    assert (agents / "dev-agent" / "workspace" / "notes" / "step.txt").read_text().strip() == "written"
    # The script ran from the private directory, outside the agents tree,
    # and the directory is gone with the run.
    assert "script-dir: " + str(step_runner.steps_root()) in out
    assert not step_runner.steps_root().exists() or not any(step_runner.steps_root().iterdir())
    # What the script printed also streamed as frames.
    assert "hello from the machine" in ws.output()
    assert sm._steps == {}


@pytest.mark.asyncio
async def test_a_personal_apps_step_runs_in_the_owners_workspace_without_knowledge(sat):
    cfg, agents = sat
    sm = SessionManager(cfg)
    ws = _WS()
    await sm.step_run(_frame("#!/bin/sh\necho cwd: $(pwd)\necho kn: [$OTODOCK_KNOWLEDGE_DIR]\n",
                             workspace_relative="users/alice/workspace", knowledge_relative=""), ws)
    ack = ws.ack()
    assert ack["exit_code"] == 0
    assert f"cwd: {(agents / 'dev-agent' / 'users' / 'alice' / 'workspace').resolve()}" in ack["output"]
    assert "kn: []" in ack["output"]


@pytest.mark.asyncio
async def test_a_checks_fields_point_the_payload_and_the_working_folder(sat, tmp_path):
    """0.5.123 (proxy CHECKS.md): ``payload_env`` names more env variables
    that point at the payload file; ``cwd_absolute`` runs the script in a
    session's folder outside the synced tree, through the same guard the
    PTY spawn uses."""
    cfg, agents = sat
    sm = SessionManager(cfg)
    ws = _WS()
    await sm.step_run(_frame("#!/bin/sh\necho in: $(cat \"$OTODOCK_CHECK_INPUT\")\necho cwd: $(pwd)\n",
                             payload_env=["OTODOCK_CHECK_INPUT"]), ws)
    ack = ws.ack()
    assert ack["exit_code"] == 0 and 'in: {"n": 1}' in ack["output"]
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _WS()
    await sm.step_run(_frame("#!/bin/sh\necho cwd: $(pwd)\n", cwd_absolute=str(repo)), ws)
    ack = ws.ack()
    assert ack["exit_code"] == 0 and f"cwd: {repo.resolve()}" in ack["output"]
    for fragment, over in [("env names", {"payload_env": ["bad name"]}),
                           ("env names", {"payload_env": ["A"] * 5}),
                           ("refused", {"cwd_absolute": str(tmp_path / "missing")}),
                           ("absolute", {"cwd_absolute": "relative/path"})]:
        ws = _WS()
        await sm.step_run(_frame("#!/bin/sh\necho x\n", **over), ws)
        ack = ws.ack()
        assert ack["status"] == "error" and fragment in ack["error"], ack


@pytest.mark.asyncio
async def test_the_frame_is_refused_before_anything_is_written(sat):
    cfg, agents = sat
    sm = SessionManager(cfg)

    async def refused(fragment, **over):
        ws = _WS()
        script = over.pop("script", "#!/bin/sh\necho x\n")
        await sm.step_run(_frame(script, **over), ws)
        ack = ws.ack()
        assert ack["status"] == "error" and fragment in ack["error"], ack
        assert not step_runner.steps_root().exists() or not any(step_runner.steps_root().iterdir())

    await refused("not a uuid", delivery_id="../x")
    await refused("slug", agent_slug="../etc")
    await refused("hash", sha256="0" * 64)
    await refused("workspace", workspace_relative="../../etc")
    await refused("workspace", workspace_relative="config")
    await refused("knowledge", knowledge_relative="users/bob")
    await refused("missing on this machine", workspace_relative="users/bob/workspace")
    await refused("timeout", timeout=0)
    await refused("timeout", timeout=99999)
    await refused("environment", env={"A": 1})
    await refused("no script", script="")
    big = "#!/bin/sh\n" + "#" * (257 * 1024)
    await refused("256 KB", script=big, sha256=hashlib.sha256(big.encode()).hexdigest())


@pytest.mark.asyncio
async def test_a_timeout_kills_the_script_and_a_dropped_link_kills_them_all(sat):
    cfg, agents = sat
    sm = SessionManager(cfg)
    ws = _WS()
    await sm.step_run(_frame("#!/bin/sh\necho start\nsleep 30\necho never\n", timeout=1), ws)
    ack = ws.ack()
    assert ack["status"] == "ok" and ack["timed_out"] is True and ack["exit_code"] is None
    assert "start" in ack["output"] and "never" not in ack["output"]
    # A running step killed when the link drops.
    ws2 = _WS()
    task = asyncio.create_task(sm.step_run(_frame("#!/bin/sh\nsleep 30\n", timeout=60), ws2))
    for _ in range(50):
        if sm._steps:
            break
        await asyncio.sleep(0.05)
    assert len(sm._steps) == 1
    assert sm.kill_steps("test") == 1
    await asyncio.wait_for(task, timeout=10)
    assert ws2.ack()["status"] == "ok" and ws2.ack()["exit_code"] not in (0, None)
    assert sm._steps == {}


@pytest.mark.asyncio
async def test_the_scripts_exit_is_the_verdict_and_nothing_it_left_behind_survives(sat):
    """A child left holding the pipe neither stretches the run to its
    timeout nor outlives it: the verdict is the script's own exit."""
    import os
    cfg, agents = sat
    sm = SessionManager(cfg)
    ws = _WS()
    marker = agents / "dev-agent" / "workspace" / "child.pid"
    script = f"#!/bin/sh\nsleep 300 &\necho $! > {marker}\necho done\nexit 0\n"
    started = asyncio.get_running_loop().time()
    await sm.step_run(_frame(script, timeout=60), ws)
    ack = ws.ack()
    assert ack["status"] == "ok" and ack["exit_code"] == 0 and ack["timed_out"] is False, ack
    assert "done" in ack["output"] and asyncio.get_running_loop().time() - started < 20
    pid = int(marker.read_text())
    await asyncio.sleep(0.2)
    try:
        os.kill(pid, 0)
        alive = open(f"/proc/{pid}/stat").read().split()[2] != "Z" if os.path.exists(f"/proc/{pid}/stat") else True
    except ProcessLookupError:
        alive = False
    assert not alive


@pytest.mark.asyncio
async def test_a_failure_before_the_script_runs_still_answers(sat, monkeypatch):
    """No verdict would leave the proxy waiting the whole timeout for it."""
    cfg, agents = sat
    sm = SessionManager(cfg)
    ws = _WS()

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(step_runner.Path, "write_bytes", full)
    await sm.step_run(_frame("#!/bin/sh\necho hi\n"), ws)
    ack = ws.ack()
    assert ack["status"] == "error" and "No space left" in ack["error"]
    assert sm._steps == {}


@pytest.mark.asyncio
async def test_the_same_delivery_never_runs_twice_at_once(sat):
    cfg, agents = sat
    sm = SessionManager(cfg)
    frame = _frame("#!/bin/sh\nsleep 2\n", timeout=30)
    ws1, ws2 = _WS(), _WS()
    t1 = asyncio.create_task(sm.step_run(frame, ws1))
    for _ in range(50):
        if sm._steps:
            break
        await asyncio.sleep(0.05)
    await sm.step_run(dict(frame), ws2)
    assert ws2.ack()["status"] == "error" and "already running" in ws2.ack()["error"]
    sm.kill_steps("test")
    await asyncio.wait_for(t1, timeout=10)


def test_the_argv_rule_and_the_capability(sat):
    cfg, _agents = sat
    assert step_runner.argv_for(b"#!/usr/bin/env bash\n", "/x.sh") == ["/usr/bin/env", "bash", "/x.sh"]
    assert step_runner.argv_for(b"echo\n", "/x.sh") == ["/bin/sh", "/x.sh"]
    caps = SessionManager(cfg).detect_capabilities()
    assert caps["steps"] is True
