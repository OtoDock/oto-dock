"""The one render of a session's sandbox: a description with nothing of
the resolving host in it, rendered by the machine that runs the session.

These tests pin what the description carries, what a machine's render does
with it, and the machine's check of itself before it serves sessions
(``preflight``): the cause it names when the sandbox does not start, and the
note about a pasta older than the release names.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import config as app_config
from core.sandbox import sandbox as _sandbox_mod, sandbox_render as render
from core.sandbox.sandbox import SandboxBuilder, SandboxConfig


def _cfg(agents_dir: Path, mcps_dir: Path, *, role="manager", username="alice",
         forwards=None, allow_hosts=(), mcp_dir_binds=()) -> SandboxConfig:
    for sub in ("workspace", "context", ".claude"):
        (agents_dir / "personal-assistant" / "users" / username / sub).mkdir(parents=True, exist_ok=True)
    (agents_dir / "personal-assistant" / "workspace").mkdir(parents=True, exist_ok=True)
    (agents_dir / "personal-assistant" / "knowledge").mkdir(parents=True, exist_ok=True)
    (agents_dir / "personal-assistant" / "config").mkdir(parents=True, exist_ok=True)
    return SandboxConfig(
        role=role, username=username, agent_name="personal-assistant", is_admin_agent=False,
        host_agents_dir=agents_dir, host_mcps_dir=mcps_dir,
        host_claude_dir=agents_dir / "personal-assistant" / "users" / username / ".claude",
        net_forwards=list(forwards if forwards is not None else [str(app_config.PORT), "8931"]),
        net_allow_hosts=list(allow_hosts), mcp_dir_binds=list(mcp_dir_binds),
    )


@pytest.fixture
def roots(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    mcps = tmp_path / "mcps"
    (mcps / "custom" / "memory-mcp").mkdir(parents=True)
    (mcps / ".uv-python").mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(app_config, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(app_config, "INTERNAL_LISTENER_PORT", 0, raising=False)
    return agents.resolve(), mcps.resolve()


def test_the_description_names_nothing_of_the_host(roots):
    agents, mcps = roots
    cfg = _cfg(agents, mcps, mcp_dir_binds=[str(mcps / "custom" / "memory-mcp"), str(mcps / ".uv-python")])
    desc = SandboxBuilder(cfg).describe()
    text = json.dumps(desc.to_dict())
    # No bind source of a role table is absolute: every one hangs off a root.
    assert all(b.root in (render.ROOT_AGENT, render.ROOT_EMPTY) for b in desc.binds), desc.binds
    assert all(not b.rel.startswith("/") for b in desc.binds)
    assert str(agents) not in text.replace(str(mcps), "")
    assert str(os.getuid()) not in [b.rel for b in desc.binds]
    # The proxy port is its marker, the other forwards literal.
    assert desc.forwards == (render.PROXY_FORWARD, "8931")
    assert desc.proxy_port == app_config.PORT
    # The tool folders are relative to the tools root, which is the sandbox-side root.
    assert desc.mcp_binds == ("custom/memory-mcp", ".uv-python")
    assert desc.mcps_root == str(mcps)
    assert desc.cwd == "/users/alice"
    assert desc.beneath is True
    # And it survives the wire.
    assert render.SandboxDescription.from_dict(json.loads(text)) == desc


def test_a_machine_renders_the_same_description_with_its_own_values(roots, tmp_path):
    agents, mcps = roots
    cfg = _cfg(agents, mcps, allow_hosts=["10.200.0.20", "fd00::5"],
               mcp_dir_binds=[str(mcps / "custom" / "memory-mcp")])
    desc = SandboxBuilder(cfg).describe()
    # Another machine: its own agents folder (a shared mount of the platform's,
    # here a different path), its own tools folder, its own empty folder, its
    # own uid and the tunnel port the proxy port is spliced onto.
    m_agents = tmp_path / "machine" / "agents"
    (m_agents / "personal-assistant" / "users" / "alice").mkdir(parents=True)
    m_mcps = tmp_path / "machine" / "mcps"
    (m_mcps / "custom" / "memory-mcp").mkdir(parents=True)
    empty = tmp_path / "machine" / "state" / ".empty"
    empty.mkdir(parents=True)
    resolv = tmp_path / "machine" / "state" / "netns-resolv.conf"
    resolv.write_text("nameserver 169.254.1.1\n")
    host = render.RenderHost(
        agents_root=str(m_agents), mcps_root=str(m_mcps), empty_dir=str(empty),
        launcher="/opt/sat/oto-sandbox-net", uid=1000, gid=1000, proxy_target_port=43210,
        resolv_conf=str(resolv), tmp_cap_mb=512, extra_ro_binds=("/opt/npm",),
    )
    argv = render.render(desc, host, ["claude", "-p"])
    assert argv[0] == "/opt/sat/oto-sandbox-net"
    assert argv[1:3] == ["--beneath", str((m_agents / "personal-assistant").resolve())]
    # The launcher's own flags end at the first separator, and bwrap follows it.
    prefix = argv[:argv.index("--")]
    assert "--block-private" in prefix and argv[len(prefix) + 1] == "bwrap"
    # The proxy port alone is spliced onto the tunnel port; every other
    # forward and every carve is passed as it was resolved, in its order.
    assert [prefix[k + 1] for k, a in enumerate(prefix) if a == "--forward"] == [f"{app_config.PORT}:43210", "8931"]
    assert [prefix[k + 1] for k, a in enumerate(prefix) if a == "--allow-host"] == ["10.200.0.20", "fd00::5"]
    assert ["--dns-forward", render.DNS_FORWARD_ADDR] == argv[argv.index("--dns-forward"):argv.index("--dns-forward") + 2]
    assert ["--ro-bind", str(resolv), "/etc/resolv.conf"] == argv[argv.index(str(resolv)) - 1:argv.index(str(resolv)) + 2]
    assert ["--unshare-user", "--uid", "1000", "--gid", "1000"] == argv[argv.index("--unshare-user"):argv.index("--unshare-user") + 5]
    assert ["--size", str(512 * 1024 * 1024), "--tmpfs", "/tmp"] == argv[argv.index("--size"):argv.index("--size") + 4]
    # Every agent bind hangs off the machine's agent folder, the mask off its empty folder.
    sources = [argv[k + 1] for k, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    machine_root = str((m_agents / "personal-assistant").resolve())
    under_agent = [s for s in sources if s.startswith(machine_root)]
    assert under_agent and str(agents) not in " ".join(sources)
    assert str(empty) in sources
    # The tool folder binds the machine's copy onto the proxy's path, which the MCP config names.
    j = argv.index(str(m_mcps / "custom" / "memory-mcp"))
    assert argv[j - 1:j + 2] == ["--ro-bind", str(m_mcps / "custom" / "memory-mcp"), str(mcps / "custom" / "memory-mcp")]
    assert argv[-5:] == ["--chdir", "/users/alice", "--", "claude", "-p"]


def test_an_absolute_source_is_the_proxys_alone(roots, tmp_path):
    agents, mcps = roots
    desc = render.SandboxDescription(
        agent="personal-assistant", binds=(render.Bind(render.ROOT_ABS, "/usr/local/bin/bun", "/app/bun", False),),
        mcp_binds=(), mcps_root=str(mcps), extra_binds=(), cwd="/app", proxy_port=8400,
        forwards=(render.PROXY_FORWARD,), beneath=False,
    )
    (agents / "personal-assistant").mkdir(parents=True, exist_ok=True)
    machine = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                                launcher="/x", uid=1000, gid=1000)
    with pytest.raises(render.RenderRefused):
        render.render(desc, machine, ["bun"])
    proxy = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                              launcher="/x", uid=1000, gid=1000, allow_abs=True)
    assert "/usr/local/bin/bun" in render.render(desc, proxy, ["bun"])


def test_no_route_is_refused_before_anything_is_rendered(roots, tmp_path):
    agents, mcps = roots
    desc = render.SandboxDescription(agent="a", binds=(), mcp_binds=(), mcps_root="", extra_binds=(),
                                     cwd="/", proxy_port=8400, forwards=())
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000)
    with pytest.raises(RuntimeError):
        render.render(desc, host, ["true"])
    # A session whose egress was never resolved is described with no route,
    # and refused the same way: never run without the proxy port.
    unresolved = SandboxBuilder(_cfg(agents, mcps, forwards=[])).describe()
    assert unresolved.forwards == ()
    with pytest.raises(render.RenderRefused, match="net_forwards"):
        render.render(unresolved, host, ["true"])


def test_a_field_the_render_does_not_know_is_refused_not_dropped(roots):
    """A rolling upgrade: a proxy that adds a mask or a refusal to the
    description must not get a sandbox without it from an older render."""
    agents, mcps = roots
    desc = SandboxBuilder(_cfg(agents, mcps)).describe().to_dict()
    assert render.SandboxDescription.from_dict(dict(desc)).to_dict() == desc
    with pytest.raises(render.RenderRefused, match="deny_paths"):
        render.SandboxDescription.from_dict({**desc, "deny_paths": ["/etc"]})


def test_the_tmp_cap_is_bounded_by_half_the_hosts_ram():
    assert render.effective_tmp_cap_mb(4096, True, 16000) == 4096
    assert render.effective_tmp_cap_mb(4096, True, 4000) == 2000
    assert render.effective_tmp_cap_mb(4096, False, 16000) == 0
    assert render.effective_tmp_cap_mb(0, True, 16000) == 0
    assert render.effective_tmp_cap_mb(4096, True, 0) == 4096


def _bound_at(argv: list[str], dest: str) -> list[str]:
    """The sources bwrap is told to bind at ``dest``."""
    return [argv[k + 1] for k, a in enumerate(argv[:-2]) if a in ("--bind", "--ro-bind") and argv[k + 2] == dest]


def test_a_host_that_enforces_no_cap_mounts_the_private_tmp_with_no_size(roots, tmp_path, monkeypatch):
    """The description carries the platform's cap for a sandbox's ``/tmp``
    (0 when the configuration turns it off) and the host bounds it
    (``effective_tmp_cap_mb``: 0 too when its bubblewrap has no ``--size``).
    A host whose cap is 0 still mounts the private ``/tmp``, with no size."""
    agents, mcps = roots
    monkeypatch.setattr(app_config, "SANDBOX_TMP_SIZE_MB", 4096)
    capped = SandboxBuilder(_cfg(agents, mcps)).describe()
    monkeypatch.setattr(app_config, "SANDBOX_TMP_SIZE_MB", 0)
    off = SandboxBuilder(_cfg(agents, mcps)).describe()
    assert (capped.tmp_size_mb, off.tmp_size_mb) == (4096, 0)
    # A bubblewrap without ``--size``, then the cap turned off on one that has it.
    for desc, has_size in ((capped, False), (off, True)):
        host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                                 launcher="/x", uid=1000, gid=1000,
                                 tmp_cap_mb=render.effective_tmp_cap_mb(desc.tmp_size_mb, has_size, 16000))
        argv = render.render(desc, host, ["claude"])
        assert "--size" not in argv and argv[argv.index("--tmpfs") + 1] == "/tmp"


def test_a_host_that_runs_as_root_maps_no_uid_back(roots, tmp_path):
    """Rootful pasta nests no user namespace, so bwrap already runs the
    session as the host's own uid: the render emits no map-back. The
    launcher still wraps bwrap and the session still holds no capability."""
    agents, mcps = roots
    desc = SandboxBuilder(_cfg(agents, mcps)).describe()
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=0, gid=0)
    argv = render.render(desc, host, ["claude"])
    assert argv[0] == "/x" and argv[argv.index("--") + 1] == "bwrap"
    assert not {"--unshare-user", "--uid", "--gid"} & set(argv)
    assert argv[argv.index("--cap-drop") + 1] == "ALL"


def test_a_host_with_no_resolver_swap_forwards_no_dns(roots, tmp_path):
    """A host whose own resolver file serves (it names no generated one),
    or whose generated file is not there: the launcher gets no
    ``--dns-forward`` and nothing but the host's own file is bound at
    ``/etc/resolv.conf``."""
    agents, mcps = roots
    desc = SandboxBuilder(_cfg(agents, mcps)).describe()
    for resolv in ("", str(tmp_path / "netns-resolv.conf")):
        host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                                 launcher="/x", uid=1000, gid=1000, resolv_conf=resolv)
        argv = render.render(desc, host, ["claude"])
        assert "--dns-forward" not in argv
        assert _bound_at(argv, "/etc/resolv.conf") in ([], ["/etc/resolv.conf"])


def test_a_manifest_mount_outside_both_trees_is_refused_at_the_description(roots, tmp_path):
    agents, mcps = roots
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    inside = mcps / "custom" / "memory-mcp" / "cache"
    inside.mkdir()
    cfg = _cfg(agents, mcps)
    cfg = SandboxConfig(**{**cfg.__dict__, "mcp_sandbox_mounts": [
        _sandbox_mod.SandboxMount(str(outside), "/cache-out", "rw"),
        _sandbox_mod.SandboxMount(str(inside), "/cache", "rw"),
        _sandbox_mod.SandboxMount(str(inside), "/etc/cache", "rw"),
    ]})
    desc = SandboxBuilder(cfg).describe()
    assert desc.extra_binds == (render.Bind(render.ROOT_MCPS, "custom/memory-mcp/cache", "/cache", True),)


def test_the_exit_and_the_agent_socket_are_left_out_at_their_default(roots):
    """The two fields of 2.0's credential exit: a description without them
    is byte for byte the one a 0.5.172 render knows, so a machine on that
    render takes it; with them set the render emits the splice and the bind
    and an older render refuses the description (the rolling-upgrade rule)."""
    agents, mcps = roots
    desc = SandboxBuilder(_cfg(agents, mcps)).describe()
    assert "exit_port" not in desc.to_dict() and "ssh_agent" not in desc.to_dict()
    assert render.SandboxDescription.from_dict(desc.to_dict()) == desc
    carried = render.SandboxDescription.from_dict({**desc.to_dict(), "exit_port": 3128,
                                                   "ssh_agent": "0123456789ab.sock"})
    assert carried.exit_port == 3128 and carried.ssh_agent == "0123456789ab.sock"
    assert carried.to_dict()["exit_port"] == 3128 and carried.to_dict()["ssh_agent"] == "0123456789ab.sock"
    # The 0.5.172 render's field set, frozen: a flag-off dict names nothing
    # outside it (that render takes it), and a dict that carries either new
    # field is one it refuses. The sessions' homes' masks ride every
    # description over a store: a machine on a render without them is
    # refused at its link by its satellite version, never sent one.
    assert set(desc.to_dict()) - {"home_masks"} <= _RENDER_0_5_172_FIELDS
    assert set(carried.to_dict()) - _RENDER_0_5_172_FIELDS - {"home_masks"} == {"exit_port", "ssh_agent"}


# The fields of the render satellite 0.5.172 vendored (``from_dict`` refuses
# any other name).
_RENDER_0_5_172_FIELDS = frozenset({
    "agent", "binds", "mcp_binds", "mcps_root", "extra_binds", "cwd", "proxy_port", "forwards",
    "allow_hosts", "hosted", "inbound", "egress_deny", "beneath", "tmp_size_mb",
})


def test_the_exit_port_is_a_splice_and_the_socket_a_bind(roots, tmp_path):
    agents, mcps = roots
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    (sockets / "0123456789ab.sock").touch()
    desc = render.SandboxDescription(
        agent="a", binds=(), mcp_binds=(), mcps_root="", extra_binds=(), cwd="/", proxy_port=8400,
        forwards=(render.PROXY_FORWARD,), exit_port=3128, ssh_agent="0123456789ab.sock")
    proxy = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                              launcher="/x", uid=1000, gid=1000, allow_abs=True, ssh_agent_dir=str(sockets))
    argv = render.render(desc, proxy, ["true"])
    assert argv[argv.index("--forward") + 1] == "8400"
    forwards = [argv[i + 1] for i, a in enumerate(argv) if a == "--forward"]
    assert forwards == ["8400", "3128"]
    i = argv.index(str(sockets / "0123456789ab.sock"))
    assert argv[i - 1] == "--bind" and argv[i + 1] == render.SSH_AGENT_SOCKET
    # The bind comes after the private /tmp the system mounts make.
    assert argv.index("--tmpfs") < i
    # The proxy's render binds no bundle: a local session's config dir carries it.
    assert render.EXIT_BUNDLE_PATH not in argv
    # A compute machine splices the exit port onto its own, binds its own
    # socket folder and its own exit's trust bundle read-only.
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
    machine = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                                launcher="/x", uid=1000, gid=1000, proxy_target_port=47001,
                                exit_target_port=47002, exit_bundle=str(bundle), ssh_agent_dir=str(sockets))
    argv = render.render(desc, machine, ["true"])
    forwards = [argv[i + 1] for i, a in enumerate(argv) if a == "--forward"]
    assert forwards == ["8400:47001", "3128:47002"]
    j = argv.index(str(bundle))
    assert argv[j - 1] == "--ro-bind" and argv[j + 1] == render.EXIT_BUNDLE_PATH
    # A compute machine whose exit is not up refuses a description that
    # names one: never a splice onto nothing and a missing trust file.
    down = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000, proxy_target_port=47001, ssh_agent_dir=str(sockets))
    with pytest.raises(render.RenderRefused, match="serves no credential exit"):
        render.render(desc, down, ["true"])
    gone = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000, proxy_target_port=47001,
                             exit_target_port=47002, exit_bundle=str(tmp_path / "missing.pem"),
                             ssh_agent_dir=str(sockets))
    with pytest.raises(render.RenderRefused, match="not there"):
        render.render(desc, gone, ["true"])
    # Without an exit on the description a compute machine binds nothing.
    plain = render.SandboxDescription(
        agent="a", binds=(), mcp_binds=(), mcps_root="", extra_binds=(), cwd="/", proxy_port=8400,
        forwards=(render.PROXY_FORWARD,))
    assert render.EXIT_BUNDLE_PATH not in render.render(plain, down, ["true"])


@pytest.mark.parametrize(("name", "folder"), [
    ("0123456789ab.sock", ""),            # the host serves none
    ("../etc/passwd", "sockets"),         # not a session's name
    ("0123456789ab.sock", "sockets"),     # the socket is not there
])
def test_a_socket_the_host_cannot_bind_refuses_the_render(roots, tmp_path, name, folder):
    agents, mcps = roots
    desc = render.SandboxDescription(
        agent="a", binds=(), mcp_binds=(), mcps_root="", extra_binds=(), cwd="/", proxy_port=8400,
        forwards=(render.PROXY_FORWARD,), ssh_agent=name)
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000,
                             ssh_agent_dir=str(tmp_path / folder) if folder else "")
    with pytest.raises(render.RenderRefused):
        render.render(desc, host, ["true"])


# ---------------------------------------------------------------------------
# A machine's check of itself before it serves sessions (``preflight``), the
# cause it names when the sandbox does not start, and its note about an old
# pasta. No process is started: ``subprocess.run`` records what it is given.
# ---------------------------------------------------------------------------

_APPARMOR_SWITCH = "apparmor_restrict_unprivileged_userns"
_DEBIAN_SWITCH = "unprivileged_userns_clone"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A host that has the three tools, its launcher and a tap device, with
    neither kernel switch on user namespaces, whose probe answers what
    ``probe.answer`` says (exit 0 until a test changes it). A test sets a
    switch by writing its file under ``tmp``."""
    launcher = tmp_path / "oto-sandbox-net"
    launcher.write_text("#!/bin/sh\n")
    monkeypatch.setattr(render.shutil, "which", lambda tool, **_kw: f"/usr/bin/{tool}")
    # A character device stands in for the host's tun.
    monkeypatch.setattr(render, "_TUN_DEVICE", os.devnull)
    # The switches of the host that runs the suite must not steer a sentence.
    monkeypatch.setattr(render, "_APPARMOR_USERNS_SYSCTL", str(tmp_path / _APPARMOR_SWITCH))
    monkeypatch.setattr(render, "_DEBIAN_USERNS_SYSCTL", str(tmp_path / _DEBIAN_SWITCH))
    probe = SimpleNamespace(ran=[], bounds=[],
                            answer=lambda argv: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))

    def _run(argv, **kw):
        probe.ran.append(list(argv))
        probe.bounds.append(kw.get("timeout"))
        return probe.answer(argv)
    monkeypatch.setattr(render.subprocess, "run", _run)
    host = render.RenderHost(agents_root=str(tmp_path / "agents"), mcps_root=str(tmp_path / "mcps"),
                             empty_dir=str(tmp_path / "empty"), launcher=str(launcher), uid=1000, gid=1000)
    return SimpleNamespace(host=host, probe=probe, tmp=tmp_path)


def _fails(machine, said: str, code: int = 1) -> None:
    """The probe exits ``code`` after printing ``said``."""
    machine.probe.answer = lambda argv: subprocess.CompletedProcess(argv, code, stdout="", stderr=said)


def test_a_host_that_runs_the_shape_passes_its_check(machine):
    assert render.preflight(machine.host) is None
    # One probe, bounded: the host's launcher around one bwrap level with the
    # sessions' namespaces, the host's uid mapped back and a fresh /proc,
    # running ``true``.
    (argv,) = machine.probe.ran
    assert machine.probe.bounds == [20.0]
    sep = argv.index("--")
    assert argv[0] == machine.host.launcher and "--block-private" in argv[:sep]
    assert argv[sep + 1] == "bwrap" and argv.count("bwrap") == 1
    assert {"--unshare-pid", "--unshare-ipc", "--unshare-uts"} <= set(argv)
    assert argv[argv.index("--uid") + 1] == "1000"
    assert argv[argv.index("--proc") + 1] == "/proc"
    assert argv[-2:] == ["--", "true"]


@pytest.mark.parametrize("tool", ["pasta", "bwrap", "ip"])
def test_a_missing_tool_is_named_and_nothing_is_probed(machine, monkeypatch, tool):
    monkeypatch.setattr(render.shutil, "which", lambda t, **_kw: None if t == tool else f"/usr/bin/{t}")
    assert render.preflight(machine.host) == f"{tool} is not installed"
    assert machine.probe.ran == []


def test_a_missing_launcher_is_named_and_nothing_is_probed(machine):
    os.unlink(machine.host.launcher)
    reason = render.preflight(machine.host)
    assert "launcher is missing" in reason and machine.host.launcher in reason
    assert machine.probe.ran == []


def test_a_host_without_the_tap_device_is_refused_before_the_probe(machine, monkeypatch):
    """pasta opens the sandbox's tap through ``/dev/net/tun``: a host that
    lacks the device, or holds something else at its name, is told so with
    the fix for each kind of install, and the shape is never run."""
    tun = machine.tmp / "tun"
    monkeypatch.setattr(render, "_TUN_DEVICE", str(tun))
    assert render.tun_device_problem() == f"{tun} is missing"
    reason = render.preflight(machine.host)
    assert f"{tun} is missing" in reason and "tap device" in reason
    assert "devices: /dev/net/tun" in reason and "modprobe tun" in reason
    tun.write_text("")
    assert render.tun_device_problem() == f"{tun} is not a character device"
    assert f"{tun} is not a character device" in render.preflight(machine.host)
    assert machine.probe.ran == []
    monkeypatch.setattr(render, "_TUN_DEVICE", os.devnull)
    assert render.tun_device_problem() is None


def test_a_probe_whose_words_name_the_tap_is_answered_with_the_device_and_its_fix(machine):
    """The device is there and the launcher still cannot open its tap: the
    probe's own words (``tun``, ``tap``) are the cause."""
    said = "Failed to open tun socket in namespace: No such device"
    _fails(machine, said)
    reason = render.preflight(machine.host)
    assert said in reason and "tap device" in reason
    assert "devices: /dev/net/tun" in reason and "modprobe tun" in reason
    assert len(machine.probe.ran) == 1
    # The words alone decide it: a failure that names neither is not the tap's.
    assert render.probe_cause("pasta: Failed to open tun socket in namespace") == render.tun_sentence(
        "pasta: Failed to open tun socket in namespace")
    assert render.probe_cause("bwrap: setting up uid map: Permission denied") == ""


_DOCTOR = ("Run scripts/sandbox-doctor.sh where the sessions run to find what refuses it (in a Docker install: "
           "docker compose exec otodock-compute /opt/otodock/scripts/sandbox-doctor.sh).")


def test_any_other_failure_says_the_probes_last_line_and_blames_nothing(machine):
    """Neither switch is set and the probe's words name no tap: the reason is
    the exit code and the last line the probe printed, with no cause of the
    host's guessed beside it, and where the doctor script is run."""
    _fails(machine, "bwrap: setting up the sandbox\nbwrap: Can't mount proc on /newroot/proc: Operation not permitted\n")
    reason = render.preflight(machine.host)
    assert reason == ("the sandbox probe failed (exit 1): bwrap: Can't mount proc on /newroot/proc: "
                      "Operation not permitted. " + _DOCTOR)
    assert "setting up the sandbox" not in reason
    for word in ("tap device", "apparmor", _DEBIAN_SWITCH):
        assert word not in reason.lower()
    assert render.probe_cause("unshare: unshare failed: Operation not permitted") == ""
    # A probe that prints nothing still says how it ended.
    _fails(machine, "", code=255)
    assert "exit 255" in render.preflight(machine.host)

    def unrunnable(argv):
        raise OSError(8, "Exec format error")
    machine.probe.answer = unrunnable
    reason = render.preflight(machine.host)
    assert "could not start" in reason and "Exec format error" in reason


def test_a_probe_that_never_answers_is_refused(machine):
    def silent(argv):
        raise subprocess.TimeoutExpired(argv, 20)
    machine.probe.answer = silent
    assert "did not answer within 20 s" in render.preflight(machine.host)
    assert machine.probe.bounds == [20.0]


def test_a_failed_probe_under_apparmors_rule_names_the_switch_and_the_script(machine):
    """Ubuntu 24.04's default: the reason names the switch and the script
    that installs the scoped profile, and warns against turning the switch
    off for the whole host."""
    (machine.tmp / _APPARMOR_SWITCH).write_text("1\n")
    _fails(machine, "bwrap: setting up uid map: Permission denied")
    reason = render.preflight(machine.host)
    assert "setting up uid map: Permission denied" in reason
    assert "kernel.apparmor_restrict_unprivileged_userns=1" in reason
    assert "scripts/setup-apparmor-userns.sh" in reason and "scripts/compose.sh" in reason
    # An install that runs `docker compose` itself has no wrapper to pick the profile.
    assert ("then restart with scripts/compose.sh. Running `docker compose` yourself, set "
            "OTODOCK_APPARMOR_PROFILE=otodock_userns in .env.") in reason
    assert "Do not set the switch to 0" in reason
    # The cause is named: the doctor's pointer is for a failure nobody could name.
    assert "sandbox-doctor" not in reason
    # A switch that reads 0 keeps no process from making a user namespace.
    (machine.tmp / _APPARMOR_SWITCH).write_text("0\n")
    assert "apparmor" not in render.preflight(machine.host).lower()


def test_a_failed_probe_with_debians_switch_off_names_it_and_how_to_turn_it_on(machine):
    (machine.tmp / _DEBIAN_SWITCH).write_text("0\n")
    _fails(machine, "bwrap: No permissions to create new namespace")
    reason = render.preflight(machine.host)
    assert "No permissions to create new namespace" in reason
    assert "kernel.unprivileged_userns_clone=0" in reason
    assert "sysctl kernel.unprivileged_userns_clone=1" in reason and "/etc/sysctl.d" in reason
    assert "apparmor" not in reason.lower()
    (machine.tmp / _DEBIAN_SWITCH).write_text("1\n")
    assert _DEBIAN_SWITCH not in render.preflight(machine.host)


@pytest.mark.parametrize("said", [
    "pasta: invalid option -- 'x'",
    "bwrap: Can't mount proc on /newroot/proc: Operation not permitted",
    "bwrap: execvp true: No such file or directory",
    "",
])
def test_a_failure_whose_words_name_no_namespace_is_blamed_on_no_kernel_switch(machine, said):
    """Inside the compute container the switch reads the host's 1, also on a
    host whose profile is installed. A probe that fails for another reason (a
    pasta option, a missing mount) is not answered with the AppArmor script:
    only the probe's own words about a namespace make a switch the cause."""
    (machine.tmp / _APPARMOR_SWITCH).write_text("1\n")
    (machine.tmp / _DEBIAN_SWITCH).write_text("0\n")
    assert render.probe_cause(said) == ""
    _fails(machine, said)
    reason = render.preflight(machine.host)
    assert "apparmor" not in reason.lower() and _DEBIAN_SWITCH not in reason
    assert reason.endswith(_DOCTOR)


@pytest.mark.parametrize("said", [
    "bwrap: setting up uid map: Permission denied",
    "bwrap: No permissions to create new namespace",
    "unshare: unshare failed: Operation not permitted",
    "bwrap: Creating new namespace failed: Operation not permitted",
    "clone3: Operation not permitted",
])
def test_a_failure_whose_words_name_a_namespace_is_blamed_on_the_switch_that_is_set(machine, said):
    (machine.tmp / _APPARMOR_SWITCH).write_text("1\n")
    assert _APPARMOR_SWITCH in render.probe_cause(said)
    (machine.tmp / _APPARMOR_SWITCH).write_text("0\n")
    (machine.tmp / _DEBIAN_SWITCH).write_text("0\n")
    assert _DEBIAN_SWITCH in render.probe_cause(said)


def test_the_cause_is_the_tap_first_then_apparmors_rule_then_debians_switch(machine):
    (machine.tmp / _APPARMOR_SWITCH).write_text("1\n")
    (machine.tmp / _DEBIAN_SWITCH).write_text("0\n")
    tap = render.probe_cause("pasta: Failed to open tun socket in namespace")
    assert "tap device" in tap and _APPARMOR_SWITCH not in tap
    userns = render.probe_cause("bwrap: setting up uid map: Permission denied")
    assert _APPARMOR_SWITCH in userns and _DEBIAN_SWITCH not in userns


def test_the_build_date_is_read_from_every_version_form():
    date = render.pasta_build_date
    assert date("pasta 2026_10_02.cba3570") == "2026_10_02"
    assert date("pasta 2026_06_11.a9c61ff-31-gbf21038") == "2026_06_11"
    assert date("pasta 0.0~git20230309.7c7625d-1") == "2023_03_09"
    assert date("pasta unknown version") == ""
    assert date("") == ""


def _pasta(monkeypatch, line: str) -> None:
    """The pasta on PATH answers ``line`` when asked its version."""
    monkeypatch.setattr(render, "pasta_version", lambda: (line, render.pasta_build_date(line)))


def test_a_pasta_older_than_the_floor_is_said_with_its_fix_and_refuses_nothing(machine, monkeypatch):
    """bookworm's package, as every 1.8.x image shipped it: the sandbox runs
    on it, so the check passes, and the note names the pasta, the floor and
    the fix for each kind of install."""
    _pasta(monkeypatch, "pasta 0.0~git20230309.7c7625d-1")
    note = render.pasta_floor_note("2023_12_30")
    assert "0.0~git20230309" in note and "2023_12_30" in note
    assert "this release's image" in note and "scripts/install-baseline-tools.sh" in note
    assert "Sessions still run" in note
    assert render.preflight(machine.host) is None


@pytest.mark.parametrize(("line", "floor"), [
    ("pasta 2023_12_30.f091893", "2023_12_30"),   # at the floor
    ("pasta 2026_10_02.cba3570", "2023_12_30"),   # the pin
    ("pasta unknown version", "2023_12_30"),      # nothing to compare
    ("", "2023_12_30"),                           # pasta did not answer
    ("pasta 0.0~git20230309.7c7625d-1", ""),      # no floor in VERSIONS.md
])
def test_no_note_at_or_above_the_floor_or_without_a_comparison(monkeypatch, line, floor):
    _pasta(monkeypatch, line)
    assert render.pasta_floor_note(floor) == ""


def test_the_version_survives_a_pasta_that_cannot_run(monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda tool: None)
    assert render.pasta_version() == ("", "")
    monkeypatch.setattr(render.shutil, "which", lambda tool: "/usr/local/bin/pasta")
    for exc in (FileNotFoundError("pasta"), subprocess.TimeoutExpired(["pasta"], 5)):
        def boom(*a, _exc=exc, **k):
            raise _exc
        monkeypatch.setattr(render.subprocess, "run", boom)
        assert render.pasta_version() == ("", "")


def test_debians_undated_pasta_reads_its_package_version(monkeypatch):
    """bookworm's package prints "pasta unknown version" and the package
    database knows it is 0.0~git20230309. A copy outside /usr/bin (the
    pinned build, an upstream static one) is never asked about a package."""
    answers = {"--version": "pasta unknown version\nCopyright Red Hat\n",
               "-W": "0.0~git20230309.7c7625d-1"}

    def fake_run(argv, **kwargs):
        return SimpleNamespace(stdout=answers["-W" if argv[0] == "dpkg-query" else argv[1]])
    monkeypatch.setattr(render.subprocess, "run", fake_run)
    monkeypatch.setattr(render.shutil, "which", lambda tool: "/usr/bin/pasta")
    monkeypatch.setattr(render.os.path, "realpath", lambda p: p)
    assert render.pasta_version() == ("pasta (package passt 0.0~git20230309.7c7625d-1)", "2023_03_09")
    # trixie's package prints nothing at all; a native Debian 13 host takes
    # it, since it is above the floor.
    answers.update({"--version": "", "-W": "0.0~git20250503.587980c-2+deb13u1"})
    assert render.pasta_version() == ("pasta (package passt 0.0~git20250503.587980c-2+deb13u1)", "2025_05_03")
    monkeypatch.setattr(render.shutil, "which", lambda tool: "/usr/local/bin/pasta")
    assert render.pasta_version() == ("", "")
    answers["--version"] = "pasta unknown version\nCopyright Red Hat\n"
    assert render.pasta_version() == ("pasta unknown version", "")


# --- the render's own check of the folders a description names ---------------

_UP = "." + "."


def _plain_description(**change) -> render.SandboxDescription:
    fields_ = dict(agent="personal-assistant", binds=(), mcp_binds=(), mcps_root="/opt/mcps", extra_binds=(),
                   cwd="/", proxy_port=8400, forwards=(render.PROXY_FORWARD,))
    return render.SandboxDescription(**{**fields_, **change})


@pytest.mark.parametrize("change", [
    {"mcp_binds": (f"{_UP}/sessions",)},
    {"mcp_binds": ("/etc",)},
    {"mcp_binds": (f"custom/{_UP}/{_UP}/sessions",)},
    {"mcp_binds": ("",)},
    {"mcp_binds": ("./custom",)},
    {"binds": (render.Bind(render.ROOT_AGENT, f"{_UP}/other-agent", "/x", False),)},
    {"binds": (render.Bind(render.ROOT_AGENT, "/etc", "/x", False),)},
    {"extra_binds": (render.Bind(render.ROOT_MCPS, f"custom/{_UP}/{_UP}", "/x", False),)},
    {"home_masks": (render.Bind(render.ROOT_AGENT, f"{_UP}/other-agent/own", "/x", False),)},
    {"home": render.Bind(render.ROOT_AGENT, f"{_UP}/other-agent", "/x", True)},
    {"agent": "", "binds": (render.Bind(render.ROOT_AGENT, "other-agent", "/x", False),)},
    {"agent": _UP}, {"agent": "."}, {"agent": "a/b"},
], ids=["tool-parent", "tool-absolute", "tool-climbs", "tool-empty", "tool-dot", "agent-bind-parent",
        "agent-bind-absolute", "manifest-mount-climbs", "home-mask-parent", "home-parent",
        "no-agent-binding-the-agents-root", "agent-parent", "agent-dot", "agent-slash"])
def test_the_render_refuses_a_folder_that_leaves_its_root(roots, tmp_path, change):
    """The render is a machine's last check of a description it is sent: a
    tool folder that is not plain names, a bind of the agent's or the
    tools' root with a parent step or an absolute path, or an agent name
    that is no folder name, is refused before anything is bound."""
    agents, mcps = roots
    for folder in (agents / "personal-assistant", agents / "other-agent" / "own", tmp_path / "sessions"):
        folder.mkdir(parents=True, exist_ok=True)
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000)
    with pytest.raises(render.RenderRefused) as refused:
        render.render(_plain_description(**change), host, ["true"])
    assert str(tmp_path) not in str(refused.value)


def test_the_tool_folder_list_takes_a_folder_by_its_real_place_or_drops_it(roots, tmp_path):
    """The builder hands the render only plain tool folder names: a folder
    named through a link to the tools folder by its place there, a folder
    outside the tools folder not at all."""
    agents, mcps = roots
    link = tmp_path / "tools-link"
    link.symlink_to(mcps)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cfg = _cfg(agents, mcps, mcp_dir_binds=[str(link / "custom" / "memory-mcp"), str(elsewhere),
                                             str(mcps / ".uv-python")])
    desc = SandboxBuilder(cfg).describe()
    assert desc.mcp_binds == ("custom/memory-mcp", ".uv-python")
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000)
    assert str(mcps / "custom" / "memory-mcp") in render.render(desc, host, ["true"])


def test_a_description_with_no_agent_that_binds_no_agent_folder_still_renders(roots, tmp_path):
    """The host check's probe: no agent and no bind of the agents' root."""
    agents, mcps = roots
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000)
    assert render.render(_plain_description(agent=""), host, ["true"])[-1] == "true"
