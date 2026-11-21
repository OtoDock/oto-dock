"""The compute machine's start (``proxy/compute_start.py``): the credential
waited for, an agents folder that is not the proxy's refused by its marker,
the tools built only on its own copy, the sandbox's preflight read and its
reason kept for the healthcheck, then the satellite in its place.

No process is started: ``subprocess.run`` and ``os.execve`` are fakes that
record their argv, and a real one would fail the test."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

import compute_start as cs


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = tmp_path / "home" / ".oto-dock"
    monkeypatch.setattr(cs, "STATE", state)
    monkeypatch.setattr(cs, "STATUS", state / "compute-status")
    monkeypatch.setattr(cs, "CONF", state / "satellite.conf")
    monkeypatch.setattr(cs, "REFUSED", state / "compute-refused")
    monkeypatch.setattr(cs, "ROOT", tmp_path / "opt")
    link, agents, mcps = tmp_path / "link", tmp_path / "agents", tmp_path / "mcps"
    for folder in (link, agents, mcps):
        folder.mkdir()
    (agents / cs.MARKER).write_text("tok-1\n")
    (link / "credential").write_text(json.dumps({"machine_id": "compute-1", "machine_secret": "s3",
                                                 "marker": "tok-1"}))
    for name, value in (("OTODOCK_COMPUTE_LINK_DIR", link), ("OTODOCK_COMPUTE_PLATFORM_URL", "http://p:8400"),
                        ("OTODOCK_COMPUTE_AGENTS_DIR", agents), ("OTODOCK_COMPUTE_MCPS_DIR", mcps)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)
    ran: list[list[str]] = []
    preflight = {"code": 0, "out": ""}

    def _run(argv, **kw):
        ran.append([str(a) for a in argv])
        if "-c" in argv and "preflight" in argv[argv.index("-c") + 1]:
            return SimpleNamespace(returncode=preflight["code"], stdout=preflight["out"], stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    class _Became(Exception):
        pass

    def _execve(path, argv, env):
        ran.append(["exec", *argv])
        raise _Became

    def _pause():
        raise SystemExit("paused")

    monkeypatch.setattr(cs.subprocess, "run", _run)
    monkeypatch.setattr(cs.os, "execve", _execve)
    monkeypatch.setattr(cs.os, "chdir", lambda _p: None)
    monkeypatch.setattr(cs.signal, "pause", _pause)
    monkeypatch.setattr(cs.shutil, "which", lambda b: f"/usr/local/bin/{b}")
    return SimpleNamespace(state=state, agents=agents, link=link, mcps=mcps, ran=ran, preflight=preflight,
                           became=_Became)


def _status(w) -> str:
    return (w.state / "compute-status").read_text().strip()


def test_a_good_start_writes_the_conf_and_becomes_the_satellite(world):
    with pytest.raises(world.became):
        cs.main()
    conf = (world.state / "satellite.conf").read_text()
    assert "machine_id = compute-1" in conf and "machine_secret = s3" in conf
    assert "platform_url = ws://p:8400/v1/satellite" in conf and "kind = compute" in conf
    assert f"agents_dir = {world.agents}" in conf
    assert "[cli]\nclaude_bin = /usr/local/bin/claude" in conf
    assert "[codex]\ncodex_bin = /usr/local/bin/codex" in conf
    assert oct(os.stat(world.state / "satellite.conf").st_mode & 0o777) == "0o600"
    assert _status(world) == "running"
    assert world.ran[-1][:4] == ["exec", str(cs.ROOT / "satellite" / "venv" / "bin" / "python"), "-m", "satellite"]
    # The tools' environments come with the image: this start builds nothing.
    assert not any("mcp_venv_bootstrap" in " ".join(a) for a in world.ran)


class _Stopped(Exception):
    pass


def test_an_agents_folder_that_is_not_the_proxys_is_refused_until_the_two_agree(world, monkeypatch):
    """A stale credential (a restore onto a new host: the compute service
    starts before the proxy writes its own) names another marker: the start
    says why and reads the credential again until the proxy's write agrees
    with the folder, then goes on."""
    (world.agents / cs.MARKER).write_text("another\n")
    naps: list[str] = []

    def _nap(_s):
        naps.append(_status(world))
        if len(naps) == 2:  # the proxy's boot writes the credential for this folder
            (world.link / "credential").write_text(json.dumps(
                {"machine_id": "compute-2", "machine_secret": "s4", "marker": "another"}))
    monkeypatch.setattr(cs.time, "sleep", _nap)
    with pytest.raises(world.became):
        cs.main()
    assert naps[0].startswith("refused: the agents folder is not the platform's")
    assert "machine_id = compute-2" in (world.state / "satellite.conf").read_text()
    assert _status(world) == "running"


def test_a_refused_start_waits_until_it_is_stopped(world, monkeypatch):
    (world.agents / cs.MARKER).unlink()
    naps: list[int] = []

    def _nap(_s):
        naps.append(1)
        if len(naps) == 3:
            raise _Stopped
    monkeypatch.setattr(cs.time, "sleep", _nap)
    with pytest.raises(_Stopped):
        cs.main()
    assert "otodock-compute too" in _status(world)
    assert not any(a and a[0] == "exec" for a in world.ran)


def test_a_host_that_cannot_run_the_sandbox_is_refused_with_its_reason(world):
    world.preflight.update(code=1, out="unprivileged user namespaces are not allowed")
    with pytest.raises(SystemExit):
        cs.main()
    assert _status(world) == ("refused: this machine cannot run the platform's sandbox: "
                              "unprivileged user namespaces are not allowed")
    assert not any(a and a[0] == "exec" for a in world.ran)


def test_a_note_of_the_preflight_is_said_and_refuses_nothing(world, capsys):
    """The preflight is handed the pasta this release names
    (``PASST_MIN_VERSION`` of VERSIONS.md). One that passes and prints a line
    (a pasta older than that) is said by the start, which goes on to become
    the satellite. One that passes and prints nothing adds no line."""
    cs.ROOT.mkdir()
    (cs.ROOT / "VERSIONS.md").write_text("PASST_VERSION=2026_10_02.cba3570\nPASST_MIN_VERSION=2023_12_30\n")
    note = ("pasta 0.0~git20230309.7c7625d-1 is older than 2023_12_30, the pasta the sandbox's launcher "
            "options are written against. Sessions still run.")
    world.preflight.update(code=0, out=note + "\n")
    with pytest.raises(world.became):
        cs.main()
    asked = next(a for a in world.ran if "-c" in a and "preflight" in a[a.index("-c") + 1])
    assert asked[-2:] == [str(world.state / "satellite.conf"), "2023_12_30"]
    assert capsys.readouterr().out.splitlines() == [f"compute: {note}", "compute: signing in"]
    assert _status(world) == "running" and world.ran[-1][0] == "exec"
    world.ran.clear()
    world.preflight.update(out="")
    with pytest.raises(world.became):
        cs.main()
    assert capsys.readouterr().out.splitlines() == ["compute: signing in"]
    assert _status(world) == "running" and world.ran[-1][0] == "exec"


def test_the_old_own_tools_key_is_ignored(world, monkeypatch):
    """A compose file from before the tools came with the image still sets
    the key: nothing builds."""
    monkeypatch.setenv("OTODOCK_COMPUTE_OWN_TOOLS", "1")
    with pytest.raises(world.became):
        cs.main()
    assert not any("mcp_venv_bootstrap" in " ".join(a) for a in world.ran)


def test_the_engines_binaries_dotenv_names_win_over_the_images(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/opt/rig-bin/claude")
    with pytest.raises(world.became):
        cs.main()
    conf = (world.state / "satellite.conf").read_text()
    assert "[cli]\nclaude_bin = /opt/rig-bin/claude" in conf
    assert "[codex]\ncodex_bin = /usr/local/bin/codex" in conf


def test_the_platforms_address_becomes_the_satellites_link():
    assert cs.satellite_url("http://otodock-proxy-compute:8400") == "ws://otodock-proxy-compute:8400/v1/satellite"
    assert cs.satellite_url("https://agents.example.com/") == "wss://agents.example.com/v1/satellite"
    assert cs.satellite_url("ws://127.0.0.1:8400/v1/satellite") == "ws://127.0.0.1:8400/v1/satellite"


def test_without_its_settings_it_refuses_and_names_them(world, monkeypatch):
    monkeypatch.delenv("OTODOCK_COMPUTE_PLATFORM_URL")
    with pytest.raises(SystemExit):
        cs.main()
    assert "OTODOCK_COMPUTE_PLATFORM_URL" in _status(world)


def test_on_another_host_it_keeps_its_own_copy_with_no_marker(world, monkeypatch, tmp_path):
    # Another host: the credential its admin put in the folder (no marker),
    # its own folders made here, and the configuration says so.
    own_agents, own_mcps = tmp_path / "own" / "agents", tmp_path / "own" / "mcps"
    (world.link / "credential").write_text(json.dumps({"machine_id": "rack-2", "machine_secret": "s9"}))
    monkeypatch.setenv("OTODOCK_COMPUTE_FILES", "own")
    monkeypatch.setenv("OTODOCK_COMPUTE_AGENTS_DIR", str(own_agents))
    monkeypatch.setenv("OTODOCK_COMPUTE_MCPS_DIR", str(own_mcps))
    with pytest.raises(world.became):
        cs.main()
    conf = (world.state / "satellite.conf").read_text()
    assert "machine_id = rack-2" in conf and "kind = compute\nfiles = own" in conf
    assert own_agents.is_dir() and own_mcps.is_dir()
    assert not (own_agents / cs.MARKER).exists()


def test_beside_the_platform_the_configuration_names_no_own_copy(world):
    with pytest.raises(world.became):
        cs.main()
    assert "files = own" not in (world.state / "satellite.conf").read_text()


def test_the_installs_own_machine_keeps_the_community_tools_itself(world, monkeypatch, tmp_path):
    """Beside the platform the configuration says ``tools = own`` and the
    community folder is made. On another server every folder is the
    machine's own, so the line is never written with ``files = own``."""
    with pytest.raises(world.became):
        cs.main()
    conf = (world.state / "satellite.conf").read_text()
    assert "kind = compute\ntools = own\n" in conf
    assert (world.mcps / "community").is_dir()
    _own(world, monkeypatch, tmp_path)
    with pytest.raises(world.became):
        cs.main()
    conf = (world.state / "satellite.conf").read_text()
    assert "files = own" in conf and "tools = own" not in conf


def test_a_community_folder_that_cannot_be_written_is_refused_with_the_way_out(world):
    """A compose file that still mounts the proxy's tools there read-only,
    or a unit without the bind: the start refuses and names both fixes. It
    never goes on to read the proxy's tools."""
    community = world.mcps / "community"
    community.mkdir()
    os.chmod(community, 0o555)
    try:
        with pytest.raises(SystemExit):
            cs.main()
    finally:
        os.chmod(community, 0o755)
    assert _status(world) == (
        f"refused: the community tools folder cannot be written: {community}. With Docker, use this "
        "release's docker-compose.yml: it mounts the volume otodock-compute-tools there for otodock-compute. "
        "Without Docker, run scripts/dev-setup.sh --service again.")
    assert not (world.state / "satellite.conf").exists()
    assert not any(a and a[0] == "exec" for a in world.ran)


def test_a_community_folder_that_cannot_be_made_is_refused(world):
    os.chmod(world.mcps, 0o555)
    try:
        assert cs.community_problem(world.mcps).startswith("the community tools folder cannot be written")
    finally:
        os.chmod(world.mcps, 0o755)
    assert cs.community_problem(world.mcps) == ""


def _own(world, monkeypatch, tmp_path, secret="s9"):
    (world.link / "credential").write_text(json.dumps({"machine_id": "rack-2", "machine_secret": secret}))
    monkeypatch.setenv("OTODOCK_COMPUTE_FILES", "own")
    monkeypatch.setenv("OTODOCK_COMPUTE_AGENTS_DIR", str(tmp_path / "own" / "agents"))
    monkeypatch.setenv("OTODOCK_COMPUTE_MCPS_DIR", str(tmp_path / "own" / "mcps"))


def test_the_sign_ins_mark_is_the_satellites():
    # The satellite leaves this mark of a refused sign-in
    # (``satellite/transport/compute_link.sign_in_mark``): one formula.
    import hashlib
    assert cs.sign_in_mark({"machine_id": "rack-2", "machine_secret": "s9"}) == \
        hashlib.sha256(b"rack-2\ns9").hexdigest()


def test_a_refused_sign_in_on_another_host_waits_for_a_new_one(world, monkeypatch, tmp_path):
    # The platform refused it (a new sign-in was made, or the machine was
    # removed): a restart would only be refused again, so the start says so
    # and waits until the file holds another sign-in.
    _own(world, monkeypatch, tmp_path)
    world.state.mkdir(parents=True, exist_ok=True)
    (world.state / "compute-refused").write_text(cs.sign_in_mark({"machine_id": "rack-2", "machine_secret": "s9"}))
    said: list[str] = []

    def _sleep(_s):
        said.append(_status(world))
        if len(said) == 2:
            (world.link / "credential").write_text(json.dumps({"machine_id": "rack-2", "machine_secret": "s10"}))
    monkeypatch.setattr(cs.time, "sleep", _sleep)
    with pytest.raises(world.became):
        cs.main()
    assert said[0] == "refused: the platform refused this sign-in. Pair it again from its card."
    assert "machine_secret = s10" in (world.state / "satellite.conf").read_text()
    assert not (world.state / "compute-refused").exists()


def test_a_refused_sign_in_tries_once_more_every_hour(world, monkeypatch, tmp_path):
    # A machine removed while it waited hears of it at that try: its sign-in
    # then finds no row, and its host service removes it.
    _own(world, monkeypatch, tmp_path)
    world.state.mkdir(parents=True, exist_ok=True)
    (world.state / "compute-refused").write_text(cs.sign_in_mark({"machine_id": "rack-2", "machine_secret": "s9"}))
    waited: list[float] = []
    monkeypatch.setattr(cs.time, "sleep", waited.append)
    with pytest.raises(world.became):
        cs.main()
    assert sum(waited) >= cs._RETRY_REFUSED_S
    assert "machine_secret = s9" in (world.state / "satellite.conf").read_text()


def test_the_install_looks_at_its_image_before_it_pairs(world, monkeypatch, tmp_path, capsys):
    """``--check``: the satellite's version and the sandbox's preflight, one
    line, signed in to nothing."""
    calls: list[list[str]] = []

    def _run(argv, **_kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(cs.subprocess, "run", _run)
    monkeypatch.setattr(cs, "STATE", tmp_path / "check-home")
    monkeypatch.setattr(cs, "CONF", tmp_path / "check-home" / "satellite.conf")
    assert cs.check("0.5.177") == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert calls[0][-1] == "0.5.177" and "SATELLITE_VERSION" in calls[0][2]
    conf = (tmp_path / "check-home" / "satellite.conf").read_text()
    assert "machine_id = check" in conf and "files = own" in conf and "tools" not in conf

    def _refused(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, stdout="this image runs satellite 0.5.170, the platform runs 0.5.177\n",
                                           stderr="")
    monkeypatch.setattr(cs.subprocess, "run", _refused)
    assert cs.check("0.5.177") == 1
    assert "runs satellite 0.5.170" in capsys.readouterr().out


def test_a_mark_of_another_sign_in_does_not_hold_the_start(world, monkeypatch, tmp_path):
    _own(world, monkeypatch, tmp_path)
    world.state.mkdir(parents=True, exist_ok=True)
    (world.state / "compute-refused").write_text(cs.sign_in_mark({"machine_id": "rack-2", "machine_secret": "old"}))
    monkeypatch.setattr(cs.time, "sleep", lambda _s: pytest.fail("the start waited"))
    with pytest.raises(world.became):
        cs.main()
    assert not (world.state / "compute-refused").exists()


def test_the_refused_mark_sits_where_the_satellite_leaves_it():
    # Unpatched: the start reads the satellite's state folder
    # (``satellite.config.otodock_dir``, ``compute_link.REFUSED_FILE``).
    from pathlib import Path
    assert cs.REFUSED == Path.home() / ".oto-dock" / "compute-refused"


def test_on_another_host_the_start_waits_for_the_sign_in_its_admin_saves(world, monkeypatch, tmp_path):
    # Nobody but its admin writes the file there: the words say so.
    _own(world, monkeypatch, tmp_path)
    (world.link / "credential").unlink()
    seen: list[str] = []

    def _sleep(_s):
        seen.append(_status(world))
        (world.link / "credential").write_text(json.dumps({"machine_id": "rack-2", "machine_secret": "s9"}))
    monkeypatch.setattr(cs.time, "sleep", _sleep)
    with pytest.raises(world.became):
        cs.main()
    assert seen == ["waiting for the sign-in file"]


def test_beside_the_platform_the_start_waits_for_the_platform(world, monkeypatch):
    (world.link / "credential").unlink()
    seen: list[str] = []

    def _sleep(_s):
        seen.append(_status(world))
        (world.link / "credential").write_text(json.dumps({"machine_id": "compute-1", "machine_secret": "s3",
                                                           "marker": "tok-1"}))
    monkeypatch.setattr(cs.time, "sleep", _sleep)
    with pytest.raises(world.became):
        cs.main()
    assert seen == ["waiting for the platform"]


def test_the_refused_wait_says_so_again_after_the_file_was_saved_again(world, monkeypatch, tmp_path):
    # The same refused sign-in saved again (the file briefly missing on the
    # way): the status goes back to the refusal, not to a wait for a file.
    _own(world, monkeypatch, tmp_path)
    world.state.mkdir(parents=True, exist_ok=True)
    same = {"machine_id": "rack-2", "machine_secret": "s9"}
    (world.state / "compute-refused").write_text(cs.sign_in_mark(same))
    seen: list[str] = []

    def _sleep(_s):
        seen.append(_status(world))
        n = len(seen)
        if n == 1:
            (world.link / "credential").unlink()          # the admin saves it again
        elif n == 2:
            (world.link / "credential").write_text(json.dumps(same))
        elif n == 3:
            (world.link / "credential").write_text(json.dumps({**same, "machine_secret": "s10"}))
    monkeypatch.setattr(cs.time, "sleep", _sleep)
    with pytest.raises(world.became):
        cs.main()
    refused = "refused: the platform refused this sign-in. Pair it again from its card."
    assert seen == [refused, "waiting for the sign-in file", refused]
