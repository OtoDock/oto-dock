"""The apps tests' shared rig: the compute machine an app runs on.

The platform starts no app server and runs no step itself: both go to the
compute machine that serves the platform's place, and are refused with none
(``app_supervisor._launch``, ``app_steps.placement_for``). ``FakeCM`` is the
links of the machines as the proxy holds them: it records every frame and
answers each one. ``placed`` makes its machine the one that serves, and
``healthy`` answers the supervisor's health probe, which no test server is
there to answer. The satellite's own suite tests what a machine does with a
frame.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

MACHINE = "m-compute-1"


class FakeCM:
    """One compute machine beside the platform, signed in. ``answer`` maps a
    frame to its ack, or to the error the link would raise."""

    def __init__(self, *, peer="127.0.0.1"):
        self.sent: list[dict] = []
        self.compute = {MACHINE}
        self.connected = {MACHINE}
        self.conn = SimpleNamespace(ws=SimpleNamespace(scope={"client": (peer, 50000), "headers": []}))
        self.answer = lambda msg: {"status": "ok", "host_port": 45001, "pid": 77, "live": [], "stopped": True,
                                   "exit_code": 0, "output": "", "timed_out": False}

    def is_compute(self, machine_id):
        return machine_id in self.compute

    def shares_folders(self, machine_id):
        return machine_id in self.compute

    def owns_copy(self, machine_id):
        return False

    def compute_machine_ids(self):
        return frozenset(self.compute)

    def is_connected(self, machine_id):
        return machine_id in self.connected

    def get_connection(self, machine_id):
        return self.conn if machine_id in self.connected else None

    def register_step_output(self, command_id, callback):
        return None

    def unregister_step_output(self, command_id):
        return None

    def set_line_listener(self, listener):
        self.line_listener = listener

    async def send_command(self, machine_id, msg, *, timeout=30.0, command_id=None):
        self.sent.append(msg)
        result = self.answer(msg)
        if isinstance(result, Exception):
            raise result
        return result

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == kind]


@pytest.fixture
def cm(monkeypatch):
    from services.apps import app_compute
    fake = FakeCM()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: fake)
    yield fake
    app_compute._servers.clear()


@pytest.fixture
def placed(monkeypatch, cm):
    from services.apps import app_compute
    monkeypatch.setattr(app_compute, "place", lambda: {"id": MACHINE, "name": "Compute"})
    return cm


@pytest.fixture
def healthy(monkeypatch):
    from services.apps import app_supervisor

    async def ok(inst):
        return True
    monkeypatch.setattr(app_supervisor, "_health", ok)
