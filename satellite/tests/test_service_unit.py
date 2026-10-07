"""The systemd user unit: only this install's unit is acted on, and an
existing unit gets OOMPolicy=continue (``host/service_unit.py``)."""
import asyncio
import dataclasses
import subprocess
from pathlib import Path

import pytest

from satellite import config
from satellite.host import service_unit
from satellite.transport import lifecycle_update


class _Systemctl:
    """A fake ``systemctl --user``: answers ``show`` from a property map and
    records every other call."""

    def __init__(self, props: dict[str, str] | None, rc: int = 0):
        self.props = props
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:3] == ["systemctl", "--user", "show"]:
            if self.props is None:
                raise FileNotFoundError("systemctl")
            prop = cmd[cmd.index("-p") + 1]
            return subprocess.CompletedProcess(cmd, self.rc, stdout=self.props.get(prop, "") + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def acted(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] != ["systemctl", "--user", "show"]]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config, "otodock_dir", lambda: tmp_path / ".oto-dock")
    monkeypatch.setattr(config, "HOST", dataclasses.replace(config.HOST, service_manager=config.SERVICE_SYSTEMD))
    return tmp_path


def test_the_unit_is_ours_only_when_it_runs_this_install(home, monkeypatch):
    mine = str(home / ".oto-dock" / "satellite")
    monkeypatch.setattr(service_unit.subprocess, "run", _Systemctl({"WorkingDirectory": mine}))
    assert service_unit.is_own_unit()
    monkeypatch.setattr(service_unit.subprocess, "run",
                        _Systemctl({"WorkingDirectory": "/home/other/.oto-dock/satellite"}))
    assert not service_unit.is_own_unit()
    monkeypatch.setattr(service_unit.subprocess, "run", _Systemctl({"WorkingDirectory": mine}, rc=1))
    assert not service_unit.is_own_unit()
    monkeypatch.setattr(service_unit.subprocess, "run", _Systemctl(None))
    assert not service_unit.is_own_unit()


def test_no_service_manager_means_no_unit(home, monkeypatch):
    monkeypatch.setattr(config, "HOST", dataclasses.replace(config.HOST, service_manager=config.SERVICE_LAUNCHD))
    fake = _Systemctl({"WorkingDirectory": str(home / ".oto-dock" / "satellite")})
    monkeypatch.setattr(service_unit.subprocess, "run", fake)
    assert not service_unit.is_own_unit()
    assert not service_unit.ensure_oom_policy()
    assert fake.calls == []


def test_an_existing_unit_gets_the_oom_policy_drop_in_once(home, monkeypatch):
    fake = _Systemctl({"WorkingDirectory": str(home / ".oto-dock" / "satellite"), "OOMPolicy": "stop"})
    monkeypatch.setattr(service_unit.subprocess, "run", fake)
    assert service_unit.ensure_oom_policy()
    dropin = home / ".config/systemd/user/oto-dock-satellite.service.d/oom-policy.conf"
    assert dropin.read_text() == "[Service]\nOOMPolicy=continue\n"
    assert fake.acted() == [["systemctl", "--user", "daemon-reload"]]
    fake.props["OOMPolicy"] = "continue"
    fake.calls.clear()
    assert not service_unit.ensure_oom_policy()
    assert fake.acted() == []


def test_another_installs_unit_gets_no_drop_in(home, monkeypatch):
    fake = _Systemctl({"WorkingDirectory": "/home/other/.oto-dock/satellite", "OOMPolicy": "stop"})
    monkeypatch.setattr(service_unit.subprocess, "run", fake)
    assert not service_unit.ensure_oom_policy()
    assert not (home / ".config/systemd/user/oto-dock-satellite.service.d").exists()
    assert fake.acted() == []


@pytest.mark.parametrize("own", [True, False])
def test_the_inline_uninstall_touches_only_its_own_unit(home, monkeypatch, own):
    if not config.HOST.posix:
        pytest.skip("the Unix inline fallback")
    oto = home / ".oto-dock"
    (oto / "satellite").mkdir(parents=True)  # no uninstall.sh: the inline fallback runs
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "oto-dock-satellite.service").write_text("[Service]\n")
    wd = str(oto / "satellite") if own else "/home/other/.oto-dock/satellite"
    fake = _Systemctl({"WorkingDirectory": wd})
    monkeypatch.setattr(service_unit.subprocess, "run", fake)
    monkeypatch.setattr(lifecycle_update, "otodock_dir", lambda: oto)
    exits: list[int] = []
    monkeypatch.setattr(lifecycle_update.os, "_exit", exits.append)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(lifecycle_update.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(lifecycle_update._self_uninstall_and_exit, "_started", False, raising=False)
    asyncio.run(lifecycle_update._self_uninstall_and_exit())
    assert exits == [0]
    assert not oto.exists()  # this install's folder goes either way
    stopped = [c for c in fake.acted() if c[:3] == ["systemctl", "--user", "stop"]]
    if own:
        assert stopped and not (unit_dir / "oto-dock-satellite.service").exists()
    else:
        assert stopped == [] and (unit_dir / "oto-dock-satellite.service").exists()


def test_the_installer_unit_carries_the_oom_policy():
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    unit = text[text.index("[Service]"):text.index("[Install]")]
    assert "OOMPolicy=continue" in unit
