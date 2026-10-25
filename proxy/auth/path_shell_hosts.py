"""The SSH hosts an admin attached to an agent, as the command gate reads
them (``auth/path_shell.py``).

On the local sandbox the admin tier is a platform admin's, with one opening:
a manager or an editor runs ``ssh``, ``scp`` and ``rsync`` to a host the
agent's ssh-hosts instances name, as that instance's user
(``local_refusal``). The hosts reach the gate on the session's context
(``SecurityContext.ssh_hosts``, filled by the permission hook for one call),
never from the command. The match is literal after normalising both sides:
no DNS (the gate runs on the event loop, and a lookup there could be answered
by anyone), so a name and the address it resolves to are different hosts.
The port is not compared: the sandbox network opens every port of an
attached private address, so a port check would bound nothing. Every user
the command names must be the instance's.

A form the gate cannot follow is a platform admin's: a proxy or jump host, a
config file, a forward, a local program run as the transport, an rsync
server, an option outside the lists below. The gate guards the spelling; the
network carve and the keys' role rule are what bound the reach.
"""

from __future__ import annotations

import ipaddress
import posixpath
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from auth import roles
from auth.path_shell_remote import ssh_call, ssh_keyword
from auth.path_shell_subst import has_placeholder
from auth.path_shell_words import (
    SCP_VALUE_LETTERS,
    remote_operand,
    rsync_remote_shells,
    rsync_words,
)
from core.session.external_identity import is_external_ctx

# The commands the opening covers.
SSH_COMMANDS = frozenset({"ssh", "scp", "sftp", "rsync"})

# ssh's options that bound nothing about where the connection goes, with or
# without a value (``-c`` ciphers, ``-e`` escape, ``-m`` MACs, ``-P`` tag,
# ``-p`` port, ``-Q`` query); ``-E`` (a log) and ``-S`` (a control socket)
# name local files, path-checked as writes.
_SSH_FLAGS = frozenset("46aCfGkMNnqsTtVvxy")
_SSH_VALUES = frozenset("cemPpQ")
_SSH_FILE_LETTERS = frozenset("ES")
_SSH_CONTROL = frozenset({"check", "exit", "stop"})
# ssh_config keywords that bound nothing about where the connection goes or
# what runs here (crypto, timeouts, host-key checking, keep-alives, the tty,
# multiplexing over the session's own control socket).
_SSH_KEYWORDS = frozenset({
    "addressfamily", "batchmode", "casignaturealgorithms", "checkhostip", "ciphers",
    "clearallforwardings", "compression", "connectionattempts", "connecttimeout",
    "controlmaster", "controlpersist", "escapechar", "exitonforwardfailure", "fingerprinthash", "forkafterauthentication",
    "gssapiauthentication", "hashknownhosts", "hostbasedacceptedalgorithms",
    "hostbasedauthentication", "hostkeyalgorithms", "hostkeyalias", "identitiesonly",
    "ipqos", "kbdinteractiveauthentication", "kbdinteractivedevices", "kexalgorithms",
    "loglevel", "logverbose", "macs", "nohostauthenticationforlocalhost",
    "numberofpasswordprompts", "obscurekeystroketiming", "passwordauthentication", "port",
    "preferredauthentications", "pubkeyacceptedalgorithms", "pubkeyacceptedkeytypes",
    "pubkeyauthentication", "rekeylimit", "remotecommand", "requesttty", "requiredrsasize",
    "sendenv", "serveralivecountmax", "serveraliveinterval", "sessiontype", "setenv",
    "stdinnull", "stricthostkeychecking", "tag", "tcpkeepalive", "updatehostkeys",
    "verifyhostkeydns", "visualhostkey",
})
_USER = "user"
# Keywords whose argument is a local file ssh reads, or appends to.
_READ_KEYWORDS = frozenset({"identityfile", "globalknownhostsfile"})
_WRITE_KEYWORD = "userknownhostsfile"
_SOCKET_KEYWORD = "controlpath"
# A file value that names no file to check: ssh_config's "none" and the
# null device and standard streams.
_NO_FILE = frozenset({"none", "/dev/null", "/dev/stdout", "/dev/stderr"})
# scp's options (``-c`` cipher, ``-l`` bandwidth, ``-P`` port, ``-X`` sftp
# option); ``-i`` and ``-o`` are read like ssh's.
_SCP_FLAGS = frozenset("346BCOpqRrsTv")
_SCP_VALUES = frozenset("clPX")
_SCP_VALUE_LETTERS = SCP_VALUE_LETTERS
# sftp's (``-B`` buffer, ``-c`` cipher, ``-l`` bandwidth, ``-P`` port, ``-R``
# requests, ``-s`` the far side's subsystem, ``-X`` sftp option); ``-b`` (a
# batch file, whose ``!`` lines run here), ``-D``, ``-F``, ``-J``, ``-S`` and
# ``-A`` are a platform admin's.
_SFTP_FLAGS = frozenset("46aCfNpqrv")
_SFTP_VALUES = frozenset("BclPRsX")
_SFTP_VALUE_LETTERS = frozenset("BbcDFiJloPRSsX")
_SSH_SCHEME = "ssh://"
_SCP_SCHEME = "scp://"
_SFTP_SCHEME = "sftp://"
_RSYNC_SCHEME = "rsync://"
_DEFAULT_SHELL = "ssh"


def normal_host(text: str) -> str:
    """A host as the match compares it: lower case, brackets and one
    trailing dot removed, an IP address in its canonical form."""
    host = text.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.endswith("."):
        host = host[:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host


def attached_host(host: str, user: str) -> tuple[str, str]:
    """One attached host and its user, normalised as the match compares."""
    return normal_host(host), (user or "").strip()


def takes_attached_hosts(ctx) -> bool:
    """Whether a session holds the agent's SSH access: the editor tier of its
    context (the keys and the hosts are the agent's own, for the tiers that
    act as the agent), never an external caller, never a check's judge, never
    a session with no context or no role."""
    if ctx is None or is_external_ctx(ctx) or getattr(ctx, "read_only", False) is True:
        return False
    role = getattr(ctx, "role", "")
    return isinstance(role, str) and roles.can_edit(role)


@dataclass
class Reach:
    """The gate's answer for one command on the local sandbox: a refusal
    (None when the command may run) and the local files its options read
    (identities, judged by the folder that holds them) and write."""
    refusal: str | None = None
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)


@dataclass
class _Reading:
    form: str = ""                     # an option or argument the gate cannot follow
    unreadable: str = ""               # a host written through an expansion
    daemon: str = ""                   # an rsync server operand
    shell: str = ""                    # an rsync remote shell that is not plain ssh
    no_host: bool = False              # names no host: one may arrive at run time (xargs)
    users: list[str] = field(default_factory=list)
    targets: list[tuple[str, str]] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)


def _option_form(letter: str, value: str) -> str:
    if letter == "o":
        word = value.strip().split("=", 1)[0].split()
        return f"-o {word[0] if word else value}"
    return f"-{letter} {value}" if letter == "O" else f"-{letter}"


def _files(values) -> list[str]:
    """The local files option values name, as the path check reads them:
    no "none", null device or standard stream. A value written through a
    shell expansion is not a path the gate can read: the prompt's own
    ``ControlPath`` is one, and it stays in the session's private /tmp."""
    return [v for v in values if v and v not in _NO_FILE and "$" not in v]


def _keyword(value: str, reading: _Reading, *, users: bool) -> bool:
    """One ``-o``: True when the gate can follow it."""
    keyword, arg = ssh_keyword(value)
    if keyword == _USER and users:
        reading.users.append(arg)
    elif keyword in _READ_KEYWORDS:
        reading.reads.extend(_files([arg]))
    elif keyword in (_WRITE_KEYWORD, _SOCKET_KEYWORD):
        reading.writes.extend(_files(arg.split() if keyword == _WRITE_KEYWORD else [arg]))
    elif keyword not in _SSH_KEYWORDS:
        return False
    return True


def _ssh_options(options, reading: _Reading, *, users: bool) -> None:
    for letter, value in options:
        if letter in _SSH_FLAGS or letter in _SSH_VALUES:
            continue
        if letter == "l" and users:
            reading.users.append(value)
        elif letter == "i":
            reading.reads.extend(_files([value]))
        elif letter in _SSH_FILE_LETTERS:
            reading.writes.extend(_files([value]))
        elif letter == "O" and value in _SSH_CONTROL:
            continue
        elif not (letter == "o" and _keyword(value, reading, users=users)):
            reading.form = _option_form(letter, value)
            return


def _unreadable(text: str) -> bool:
    return not text or has_placeholder(text) or any(c in text for c in "$`")


def _target(reading: _Reading, host: str, user: str | None) -> None:
    if _unreadable(host):
        reading.unreadable = reading.unreadable or host or "(no host)"
    reading.targets.append((host, user if user is not None else ""))


def _read_ssh(segment: str) -> _Reading:
    reading = _Reading()
    call = ssh_call(segment)
    if call is None:
        reading.form = "ssh"
        return reading
    _ssh_options(call.options, reading, users=True)
    reading.no_host = not call.destination
    if reading.form or reading.no_host:
        return reading
    dest = call.destination
    if dest.lower().startswith(_SSH_SCHEME):
        try:
            parts = urlsplit(dest)
            parts.port  # noqa: B018  # an invalid port raises here
        except ValueError:
            reading.form = dest
            return reading
        user, host = parts.username, parts.hostname or ""
    else:
        name, at, host = dest.rpartition("@")
        user = name if at else None
    named = ([user] if user is not None else []) + reading.users
    for who in named or [""]:
        _target(reading, host, who)
    return reading


def _args(segment: str) -> list[str] | None:
    """A command's arguments: the words after its assignments and its name."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    while tokens and "=" in tokens[0] and tokens[0].split("=", 1)[0].isidentifier():
        tokens.pop(0)
    return tokens[1:]


def _operand_target(arg: str) -> tuple[str | None, str]:
    """The ``[user@]host`` of a remote operand (``host:path``,
    ``user@[v6]:path``, ``host::module``)."""
    bracket = arg.find("[")
    if bracket >= 0 and (bracket == 0 or arg[bracket - 1] == "@"):
        return (arg[:bracket - 1] if bracket else None), arg[bracket + 1:arg.find("]", bracket)]
    name, at, host = arg[:arg.find(":")].rpartition("@")
    return (name if at else None), host


def _copy_options(args: list[str], reading: _Reading, flags, values, value_letters) -> list[str]:
    """scp's and sftp's options (getopt clusters, ``--`` ends them): the
    operands; a letter the tables do not admit sets ``reading.form``."""
    operands: list[str] = []
    k, done = 0, False
    while k < len(args) and not reading.form:
        arg = args[k]
        k += 1
        if done or not arg.startswith("-") or arg == "-":
            operands.append(arg)
            continue
        if arg == "--":
            done = True
            continue
        for i, letter in enumerate(arg[1:], 1):
            if letter in flags:
                continue
            if letter not in value_letters:
                reading.form = f"-{letter}"
                break
            value = arg[i + 1:]
            if not value:
                value, k = (args[k] if k < len(args) else ""), k + 1
            if letter == "i":
                reading.reads.append(value)
            elif letter == "o":
                if not _keyword(value, reading, users=True):
                    reading.form = _option_form(letter, value)
            elif letter not in values:
                reading.form = f"-{letter}"
            break
    return operands


def _url_target(reading: _Reading, arg: str, default_user: str) -> None:
    try:
        parts = urlsplit(arg)
        parts.port  # noqa: B018  # an invalid port raises here
    except ValueError:
        reading.form = arg
        return
    _target(reading, parts.hostname or "", parts.username or default_user)


def _read_scp(segment: str) -> _Reading:
    reading = _Reading()
    args = _args(segment)
    if args is None:
        reading.form = "scp"
        return reading
    operands = _copy_options(args, reading, _SCP_FLAGS, _SCP_VALUES, _SCP_VALUE_LETTERS)
    if reading.form:
        return reading
    reading.no_host = not any(arg.lower().startswith(_SCP_SCHEME) or remote_operand(arg)
                              for arg in operands)
    default_user = reading.users[0] if reading.users else ""
    for arg in operands:
        if arg.lower().startswith(_SCP_SCHEME):
            _url_target(reading, arg, default_user)
        elif remote_operand(arg):
            user, host = _operand_target(arg)
            _target(reading, host, user if user is not None else default_user)
    return reading


def _read_sftp(segment: str) -> _Reading:
    reading = _Reading()
    args = _args(segment)
    if args is None:
        reading.form = "sftp"
        return reading
    operands = _copy_options(args, reading, _SFTP_FLAGS, _SFTP_VALUES, _SFTP_VALUE_LETTERS)
    reading.no_host = not operands
    if reading.form or reading.no_host:
        return reading
    default_user = reading.users[0] if reading.users else ""
    dest = operands[0]
    if dest.lower().startswith(_SFTP_SCHEME):
        _url_target(reading, dest, default_user)
        return reading
    if remote_operand(dest):
        user, host = _operand_target(dest)
    else:
        name, at, host = dest.rpartition("@")
        user = name if at else None
    _target(reading, host, user if user is not None else default_user)
    return reading


def _read_rsync(segment: str) -> _Reading:
    reading = _Reading()
    words = rsync_words(segment)
    for shell in words.shells:
        if shell.strip() in ("", _DEFAULT_SHELL):
            continue
        try:
            shell_words = shlex.split(shell)
        except ValueError:
            shell_words = []
        call = ssh_call(shell)
        if (not shell_words or posixpath.basename(shell_words[0]) != _DEFAULT_SHELL
                or call is None or call.destination or call.remote):
            reading.shell = shell
            return reading
        _ssh_options(call.options, reading, users=True)
        if reading.form:
            return reading
    for arg in words.operands:
        if not remote_operand(arg):
            continue
        user, host = _operand_target(arg)
        if arg.lower().startswith(_RSYNC_SCHEME) or arg[arg.find(":"):].startswith("::"):
            reading.daemon = arg
            return reading
        # The shell's user options come before the operand's -l, and ssh
        # keeps the first: every user named is compared.
        for who in reading.users + ([user] if user is not None else []) or [""]:
            _target(reading, host, who)
    reading.no_host = not reading.targets
    return reading


def rsync_shell_commands(segment: str) -> list[str]:
    """The remote shells an ``rsync`` segment names, each as rsync runs it:
    followed by the ``[user@]host`` of its remote operand, so the check of
    the shell as a command of its own reads the host it reaches (a shell
    with no host is a destination-less ssh, which the opening refuses)."""
    shells = rsync_remote_shells(segment)
    remote = [arg for arg in rsync_words(segment).operands
              if remote_operand(arg) and not arg.lower().startswith(_RSYNC_SCHEME)]
    if not shells or not remote:
        return shells
    user, host = _operand_target(remote[0])
    destination = shlex.quote(f"{user}@{host}" if user is not None else host)
    return [f"{shell} {destination}" for shell in shells]


_READERS = {"ssh": _read_ssh, "scp": _read_scp, "sftp": _read_sftp, "rsync": _read_rsync}


def _admin_only(cmd_name: str, role: str) -> str:
    return (f"'{cmd_name}' requires platform admin role on local sandbox sessions "
            f"(current role: {role}). These commands touch host state outside the sandbox.")


def local_refusal(cmd_name: str, segment: str, ctx) -> Reach:
    """The admin tier's role gate on the local sandbox for a session that is
    not a platform admin's: ``Reach.refusal`` is the sentence (without the
    ``Bash denied:`` prefix), None when the command may run."""
    if cmd_name not in SSH_COMMANDS:
        return Reach(_admin_only(cmd_name, ctx.role))
    if getattr(ctx, "read_only", False) is True:
        return Reach("a check's judge does not reach other hosts.")
    if not takes_attached_hosts(ctx):
        return Reach(f"on this agent's sandbox, '{cmd_name}' runs for a platform admin, or for a "
                     f"manager or editor to the SSH hosts attached to the agent (current role: "
                     f"{ctx.role}).")
    if ctx.ssh_hosts is None:
        return Reach("the agent's SSH hosts could not be read right now. Try again.")
    attached = {attached_host(host, user) for host, user in ctx.ssh_hosts}
    if not attached:
        return Reach(f"on this agent's sandbox, '{cmd_name}' reaches only the SSH hosts an admin "
                     "attached to the agent, and none is attached.")
    reading = _READERS[cmd_name](segment)
    if reading.form:
        return Reach(f"'{reading.form}' on '{cmd_name}' is for platform admins on this agent's "
                     "sandbox: the gate cannot tell where it leads.")
    if reading.shell:
        return Reach(f"rsync's remote shell must be plain 'ssh' to reach an attached host; "
                     f"'{reading.shell}' is for platform admins on this agent's sandbox.")
    if reading.daemon:
        return Reach(f"'{reading.daemon}' asks for an rsync server, which is for platform admins "
                     "on this agent's sandbox: rsync reaches an attached host over SSH.")
    if reading.no_host:
        return Reach(f"on this agent's sandbox, '{cmd_name}' must name the attached SSH host it "
                     "reaches; this command names none.")
    if reading.unreadable:
        return Reach(f"'{cmd_name}' reaches an attached SSH host written out as the admin attached "
                     f"it; the gate cannot read the host '{reading.unreadable}'.")
    for host, user in reading.targets:
        wanted = attached_host(host, user)
        if wanted in attached:
            continue
        users = sorted(u for h, u in attached if h == wanted[0])
        if not users:
            return Reach(f"on this agent's sandbox, '{cmd_name}' reaches only the SSH hosts an "
                         f"admin attached to the agent, and '{host}' is not one of them.")
        named = " or ".join(f"the user '{u}'" if u else "no user" for u in users)
        login = " or ".join(f"'{u}@{host}'" if u else f"'{host}'" for u in users)
        return Reach(f"the SSH host '{host}' is attached for {named}: log in as {login}.")
    return Reach(None, reading.reads, reading.writes)
