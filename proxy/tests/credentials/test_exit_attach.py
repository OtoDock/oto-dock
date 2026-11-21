"""What a session's start attaches for the credential exit: nothing of the
exit for a start on no machine, the port a compute session's exit is
spliced onto (``credential_exit.attach_sandbox``, ``_sandbox_exit_port``),
the SSH agent socket served locally or named for a compute machine
(``ssh_signer.attach_sandbox``), the public halves in the key dir in place
of the private files, the boot sweep of the private files earlier releases
wrote."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from auth.path_policy import SecurityContext
from core import placement
from core.credentials import credential_exit, ssh_signer
from core.sandbox import session_config_dir as scd

AGENT = "ops-agent"


def _builder(*carves: str):
    return SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=tuple(carves)), exit_port=0, ssh_agent="")


def _ctx(role: str = "editor", **kw) -> SecurityContext:
    return SecurityContext(role=role, username="alice", agent=AGENT, is_admin_agent=False, **kw)


def _compute(version: str = "0.5.173") -> placement.PlacementCapabilities:
    return placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1",
                                           satellite_version=version)


@pytest.fixture
def exit_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PLATFORM_DATA_DIR", tmp_path / "data")
    # A socket path must stay under the signer's 100 bytes: a short dir.
    short = Path(tempfile.mkdtemp(prefix="oto-", dir="/tmp"))
    monkeypatch.setattr(config, "SESSIONS_DIR", short / "sessions")
    yield tmp_path
    shutil.rmtree(short, ignore_errors=True)


# --- the exit's half -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_start_on_no_machine_attaches_no_exit_and_is_logged_once(exit_setup, caplog):
    """Every session runs on a machine, whose satellite runs the exit: a
    start that names none gets no port on its description, no variable, no
    door and no file in its config dir, and the start is logged."""
    cfg = exit_setup / "agents" / AGENT / "users" / "alice" / ".claude"
    cfg.mkdir(parents=True)
    builder, env, sid = _builder("10.0.0.5", "printer.lan"), {"PATH": "/bin"}, str(uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="otodock.credential-exit"):
        await credential_exit.attach_sandbox(sid, builder, env, cfg, sandbox_config_dir="/users/alice/.claude",
                                             direct_hosts=("file-tools",))
    assert builder.exit_port == 0 and env == {"PATH": "/bin"}
    assert sid not in credential_exit._sessions
    assert credential_exit.machine_secret(sid) == "" and credential_exit.env_of(sid) == {}
    assert list(cfg.iterdir()) == []
    logged = [r for r in caplog.records if r.name == "otodock.credential-exit"]
    assert len(logged) == 1 and logged[0].levelno == logging.WARNING
    assert sid[:8] in logged[0].getMessage()


def test_a_compute_sessions_exit_port_moves_past_a_port_the_description_holds(monkeypatch):
    """41443 inside the namespace, unless the description forwards that
    port already or it is the platform's own (``config.PORT``): then the
    next one free of both. A forward by name (``proxy``) names no port, and
    a splice holds the port on its namespace side."""
    def port(*forwards):
        return credential_exit._sandbox_exit_port(SimpleNamespace(cfg=SimpleNamespace(net_forwards=list(forwards))))
    monkeypatch.setattr(config, "PORT", 8400)
    assert credential_exit._SANDBOX_EXIT_PORT == 41443
    assert port() == 41443 and port("proxy", "8400", "3128") == 41443
    assert credential_exit._sandbox_exit_port(SimpleNamespace(cfg=SimpleNamespace())) == 41443
    assert port("41443") == 41444 and port("41443", "41444") == 41445
    assert port("41443:3128") == 41444 and port("3128:41443") == 41443
    monkeypatch.setattr(config, "PORT", 41443)
    assert port() == 41444 and port("proxy") == 41444
    assert port("41444") == 41445


# --- the signer's half ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_editors_local_session_gets_a_served_socket(exit_setup):
    builder, env, sid = _builder(), {}, str(uuid.uuid4())
    try:
        await ssh_signer.attach_sandbox(sid, _ctx("editor"), builder, env, compute=False)
        assert builder.ssh_agent == ssh_signer.socket_name(sid)
        assert env == {"SSH_AUTH_SOCK": "/tmp/oto-ssh-agent.sock"}
        path = ssh_signer.socket_dir() / builder.ssh_agent
        assert stat.S_ISSOCK(os.stat(path).st_mode)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        await ssh_signer.close(sid)
    assert not (ssh_signer.socket_dir() / builder.ssh_agent).exists()


@pytest.mark.asyncio
async def test_a_compute_session_gets_the_name_and_no_socket_here(exit_setup):
    """The machine's satellite opens the socket under that name; the
    render binds it from the machine's socket dir."""
    builder, env, sid = _builder(), {}, str(uuid.uuid4())
    await ssh_signer.attach_sandbox(sid, _ctx("manager", placement=_compute()), builder, env, compute=True)
    assert builder.ssh_agent == ssh_signer.socket_name(sid)
    assert env == {"SSH_AUTH_SOCK": "/tmp/oto-ssh-agent.sock"}
    assert not ssh_signer.socket_dir().exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "contributor"])
async def test_below_the_editor_tier_no_socket_is_attached(exit_setup, role):
    builder, env, sid = _builder(), {}, str(uuid.uuid4())
    await ssh_signer.attach_sandbox(sid, _ctx(role), builder, env, compute=False)
    assert builder.ssh_agent == "" and env == {}
    assert not ssh_signer.socket_dir().exists()


@pytest.mark.asyncio
async def test_a_compute_start_with_no_compute_placement_gets_no_name(exit_setup):
    """A compute start names a socket only for a compute machine's placement:
    with none in the context its description gets no name and its
    environment no socket."""
    builder, env, sid = _builder(), {}, str(uuid.uuid4())
    await ssh_signer.attach_sandbox(sid, _ctx("manager"), builder, env, compute=True)
    assert builder.ssh_agent == "" and env == {}


# --- the key dir -----------------------------------------------------------------


def test_the_public_halves_replace_the_private_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    cfg = tmp_path / "agents" / "agent" / "users" / "pm" / ".claude"
    (cfg / "ssh").mkdir(parents=True)
    (cfg / "ssh" / "prod").write_text("PRIVATE")
    assert scd.materialize_ssh_public_keys({"prod": "ssh-ed25519 AAAA prod\n"}, cfg, agent_name="agent") is True
    assert sorted(p.name for p in (cfg / "ssh").iterdir()) == ["prod.pub"]
    assert (cfg / "ssh" / "prod.pub").read_text() == "ssh-ed25519 AAAA prod\n"
    assert stat.S_IMODE((cfg / "ssh" / "prod.pub").stat().st_mode) == 0o644
    assert stat.S_IMODE((cfg / "ssh").stat().st_mode) == 0o700
    # No key authorised: the dir is gone, nothing to point at.
    assert scd.materialize_ssh_public_keys({}, cfg, agent_name="agent") is False
    assert not (cfg / "ssh").exists()


def test_the_boot_sweep_removes_private_files_and_keeps_the_public_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    agent = tmp_path / "agents" / AGENT
    dirs = [agent / "workspace" / ".claude" / "ssh", agent / "workspace" / ".codex" / "ssh",
            agent / "users" / "alice" / ".claude" / "ssh"]
    for d in dirs:
        d.mkdir(parents=True)
        (d / "prod").write_text("PRIVATE")
        (d / "prod.pub").write_text("ssh-ed25519 AAAA prod\n")
    # A link planted at a key's name goes too; a linked dir is never entered.
    os.symlink("/etc/hostname", dirs[0] / "linked")
    (agent / "users" / "bob" / ".claude").mkdir(parents=True)
    os.symlink(str(dirs[2]), agent / "users" / "bob" / ".claude" / "ssh")
    assert scd.sweep_private_ssh_keys() == 4
    for d in dirs:
        assert sorted(p.name for p in d.iterdir()) == ["prod.pub"], d
    assert (agent / "users" / "bob" / ".claude" / "ssh").is_symlink()
    assert scd.sweep_private_ssh_keys() == 0


def test_the_boot_sweep_removes_through_the_folder_it_listed(tmp_path, monkeypatch):
    """A compute machine's session outlives the server's restart and can be
    at work in its engine folder while the boot sweeps it: ``ssh`` made a
    link after the listing, the folder the link names keeps its files."""
    from services.infra import safe_fs
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    ssh = tmp_path / "agents" / AGENT / "workspace" / ".claude" / "ssh"
    ssh.mkdir(parents=True)
    (ssh / "prod").write_text("PRIVATE")
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    (stand_in / "prod").write_text("stand-in")
    real = safe_fs.unlink_beneath
    swapped = []

    def swap_then_unlink(*a, **kw):
        if not swapped:
            ssh.rename(ssh.with_name("ssh-listed"))
            ssh.symlink_to(stand_in)
            swapped.append(True)
        return real(*a, **kw)

    monkeypatch.setattr(safe_fs, "unlink_beneath", swap_then_unlink)
    scd.sweep_private_ssh_keys()
    assert swapped and (stand_in / "prod").read_text() == "stand-in"
    assert not (ssh.with_name("ssh-listed") / "prod").exists()


def test_the_boot_sweep_never_enters_a_key_folder_that_is_a_link(tmp_path, monkeypatch):
    """A scope's ``ssh`` that is a link to a folder outside the agent is
    passed over, and that folder keeps its files."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    (stand_in / "prod").write_text("stand-in")
    cfg = tmp_path / "agents" / AGENT / "users" / "bob" / ".claude"
    cfg.mkdir(parents=True)
    (cfg / "ssh").symlink_to(stand_in)
    assert scd.sweep_private_ssh_keys() == 0
    assert (stand_in / "prod").read_text() == "stand-in"


def test_the_boot_sweep_reaches_an_agent_folder_that_is_itself_a_link(tmp_path, monkeypatch):
    """The operator's layout: the agent's folder is a link to its real place,
    followed once, and its sessions' private keys go as anywhere else."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    real = tmp_path / "disk2" / AGENT
    ssh = real / "users" / "alice" / ".claude" / "ssh"
    ssh.mkdir(parents=True)
    (ssh / "prod").write_text("PRIVATE")
    (ssh / "prod.pub").write_text("ssh-ed25519 AAAA prod\n")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / AGENT).symlink_to(real)
    assert scd.sweep_private_ssh_keys() == 1
    assert sorted(p.name for p in ssh.iterdir()) == ["prod.pub"]
