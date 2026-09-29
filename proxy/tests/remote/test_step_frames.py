"""App steps on a machine, the proxy side (APPS.md "Steps", satellite
0.5.122).

Load-bearing: the gate needs the version AND the capability; the output
frames reach the registered callback and nobody else; ``run_remote`` turns
the ack, an error ack, a timeout and a dropped link into the verdicts the
runner records, and never sends a frame to a satellite below the gate; both
tunnel allowlists admit exactly the action route a step's claim may press.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest

from core.remote.satellite_connection import SatelliteConnection, SatelliteConnectionManager
from core.remote.satellite_http_tunnel import _is_allowed_path
from services.apps import app_steps
from tests._paths import REPO_ROOT


def test_the_gate_needs_the_version_and_the_capability():
    cm = SatelliteConnectionManager()
    for mid, ver, caps in [("new", "0.5.122", {"steps": True}), ("future", "0.6.0", {"steps": True}),
                           ("old", "0.5.121", {"steps": True}), ("no-flag", "0.5.122", {}),
                           ("blank", "", {"steps": True})]:
        cm._connections[mid] = SatelliteConnection(machine_id=mid, ws=None, satellite_version=ver,
                                                   capabilities=caps)
    assert cm.satellite_supports_steps("new") is True
    assert cm.satellite_supports_steps("future") is True
    assert cm.satellite_supports_steps("old") is False
    assert cm.satellite_supports_steps("no-flag") is False
    assert cm.satellite_supports_steps("blank") is False
    assert cm.satellite_supports_steps("absent") is False


def test_step_output_reaches_the_registered_callback_only():
    cm = SatelliteConnectionManager()
    seen: list[str] = []

    async def cb(text):
        seen.append(text)

    cm.register_step_output("step-1", cb)
    asyncio.run(cm.handle_message("m", {"type": "step_output", "command_id": "step-1", "text": "a\n"}))
    asyncio.run(cm.handle_message("m", {"type": "step_output", "command_id": "step-9", "text": "b\n"}))
    cm.unregister_step_output("step-1")
    asyncio.run(cm.handle_message("m", {"type": "step_output", "command_id": "step-1", "text": "c\n"}))
    assert seen == ["a\n"]


def _plan(**over) -> app_steps.StepPlan:
    base = dict(row={"id": "app-1", "slug": "flow", "agent": "dev"}, delivery={"id": "d-1", "handler": "sync", "event": "trigger:t", "payload": {"n": 1}},
                name="sync", run="scripts/sync.sh", timeout=5, sha256="s" * 64, script=b"#!/bin/sh\necho hi\n",
                identity=None, vis=None, target="m-1", credential_env={"GH_TOKEN": "ghp_secret_value"},
                workspace_relative="workspace", knowledge_relative="knowledge")
    base.update(over)
    return app_steps.StepPlan(**base)


class _CM:
    def __init__(self, *, connected=True, supports=True, ack=None, raise_=None):
        self.connected, self.supports, self.ack, self.raise_ = connected, supports, ack, raise_
        self.sent: list[dict] = []
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def is_connected(self, mid):
        return self.connected

    def satellite_supports_steps(self, mid):
        return self.supports

    def satellite_version(self, mid):
        return "0.5.121"

    def register_step_output(self, cid, cb):
        self.registered.append(cid)
        self.cb = cb

    def unregister_step_output(self, cid):
        self.unregistered.append(cid)

    async def send_command(self, mid, msg, *, timeout, command_id=None):
        self.sent.append(msg)
        await self.cb("line one\n")
        if self.raise_:
            raise self.raise_
        return self.ack


def test_run_remote_sends_the_frame_and_reads_the_ack(monkeypatch):
    cm = _CM(ack={"status": "ok", "exit_code": 0, "output": "hi\n", "timed_out": False})
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    logged: list[str] = []

    async def fake_log(row, line):
        logged.append(line)

    monkeypatch.setattr("services.apps.app_supervisor.append_log", fake_log)
    res = asyncio.run(app_steps.run_remote(_plan(), "claim-token-xyz", "m-1"))
    assert res.exit_code == 0 and res.output == "hi\n" and res.ran_on == "m-1" and not res.timed_out
    frame = cm.sent[0]
    assert frame["type"] == "step_run" and frame["delivery_id"] == "d-1" and frame["agent_slug"] == "dev"
    assert frame["script"] == "#!/bin/sh\necho hi\n" and frame["sha256"] == "s" * 64
    assert frame["workspace_relative"] == "workspace" and frame["timeout"] == 5
    assert frame["env"]["OTODOCK_STEP_TOKEN"] == "claim-token-xyz" and frame["env"]["GH_TOKEN"] == "ghp_secret_value"
    assert frame["payload_json"] == '{"n":1}'
    assert cm.registered == cm.unregistered and len(cm.registered) == 1
    assert logged == ["line one\n"]


def test_run_remote_refuses_an_old_satellite_and_reports_the_link(monkeypatch):
    cm = _CM(supports=False)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    with pytest.raises(app_steps.StepRefused, match="too old for steps"):
        asyncio.run(app_steps.run_remote(_plan(), "c", "m-1"))
    assert cm.sent == []
    cm = _CM(connected=False)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    with pytest.raises(app_steps.StepUnavailable, match="offline"):
        asyncio.run(app_steps.run_remote(_plan(), "c", "m-1"))
    for exc, kind, fragment in [
        (RuntimeError("Satellite m-1 not connected"), app_steps.StepUnavailable, "offline"),
        (RuntimeError("Satellite m-1 command timeout (65.0s)"), app_steps.StepRefused, "did not answer"),
        (RuntimeError("Satellite command error: the script does not match its hash"),
         app_steps.StepRefused, "does not match its hash"),
        (RuntimeError("Connection lost"), app_steps.StepRefused, "link dropped"),
    ]:
        cm = _CM(raise_=exc)
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda cm=cm: cm)
        monkeypatch.setattr("services.apps.app_supervisor.append_log", _noop_log)
        with pytest.raises(kind, match=fragment):
            asyncio.run(app_steps.run_remote(_plan(), "c", "m-1"))
        assert cm.unregistered == cm.registered


async def _noop_log(row, line):
    return None


def test_both_allowlists_admit_the_action_route_a_step_presses():
    app = "0123abcd-0123-4567-89ab-0123456789ab"
    assert _is_allowed_path(f"/v1/apps/{app}/actions/run")
    assert _is_allowed_path(f"/v1/apps/{app}/actions/gh_release")
    assert not _is_allowed_path(f"/v1/apps/{app}/actions/batch/x")
    assert not _is_allowed_path(f"/v1/apps/{app}/actions/")
    assert not _is_allowed_path(f"/v1/apps/{app}/actions/run/extra")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from satellite.transport.http_tunnel import _is_allowed_path as _sat_allowed
    assert _sat_allowed(f"/v1/apps/{app}/actions/run")
    assert not _sat_allowed(f"/v1/apps/{app}/actions/run/extra")


def test_the_step_env_carries_the_claim_and_the_tokens_never_in_argv():
    plan = _plan()
    env = app_steps.step_env(plan, "claim-1")
    assert env["OTODOCK_STEP_TOKEN"] == "claim-1" and env["GH_TOKEN"] == "ghp_secret_value"
    assert env["OTODOCK_DELIVERY_ID"] == "d-1" and env["OTODOCK_STEP_HANDLER"] == "sync"
    argv = app_steps.argv_for(plan.script, "/app/scripts/sync.sh")
    assert "claim-1" not in " ".join(argv) and "ghp_secret_value" not in " ".join(argv)
    with patch.object(app_steps, "OUTPUT_KEEP_BYTES", 8):
        assert app_steps._output_text(b"12345678", 20) == "12345678\n… [12 more bytes not kept]"
