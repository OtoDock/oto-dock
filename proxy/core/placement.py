"""Placement — where a session runs, named once.

A session runs in the platform's bwrap sandbox on one of its compute
machines, or on a paired machine (a satellite). The tree spells that in three vocabularies, every one of them
frozen (stored, on the wire, or in a document a script reads):

- the STORED target value — ``agents.execution_target``,
  ``chats.execution_target``, the automation rows, ``checks.ran_on`` and an
  app step's ``ran_on`` — is ``"local"`` or a machine id; the resolver
  hands a session builder the offline sentinel ``__offline__:<machine_id>``
  when the intended machine is unreachable and no fallback is allowed
  (never stored, never on the wire): ``LOCAL``, ``OFFLINE_PREFIX`` and the
  predicates ``is_local`` / ``machine_of`` / ``is_offline_sentinel`` /
  ``offline_machine_of`` / ``offline_sentinel``;
- the resolved KIND of a placement — the sandbox, the agent's admin-paired
  default machine, the person's own paired machine, or a compute machine
  (the platform's own, sandboxed, on the platform's folder) — ``KIND_*``
  (the security index persists it as ``target_kind``), and the scope a
  machine row was registered under (``remote_machines.pairing_scope``):
  ``PAIRING_*``;
- the SITE a check input document carries under ``session.placement.kind``
  and an app's ``step_target.kind``: ``"local"`` or ``"machine"`` —
  ``SITE_*``.

``PlacementCapabilities`` is the resolved placement of ONE session with the
facts the path policy, the prompt, the MCP gates, the checks and the
capacity accounting ask. Each kind's facts are one row of ``_FACTS``: a
site reads the fact it means (the process's place, the sandbox, the path
translation, a synced copy, the memory gates, the device tools), never
``is_remote`` for another of them. ``storage.remote_store.placement_of`` builds it once
per session build from the machine row; ``SecurityContext.placement``
carries it (persisted through ``core.session.session_state``'s codec under
the index's own ``target_*`` keys); ``PathPolicyContext.placement`` copies
it for the resolver. An offline sentinel resolves to the local placement
on purpose: every consumer of a sentinel refuses before a session starts
(``session_manager.get_layer`` raises; the warmup, the headless heal and
the task runner return the tailored offline error before the registration),
so the placement built for it is never registered, prompted or gated.

Generic code asks the questions here or compares the constants; an
identity comparison on a member is legal in this module only
(``tests/core/test_placement_surface.py`` keeps it so). The dashboard
mirrors the constants in ``lib/placement.ts``;
``tests/remote/test_placement.py`` binds the two.

A stdlib leaf: ``storage/`` and ``core/execution_layer.py`` import it, it
imports nothing of the tree but the OS leaf (``core/host_os.py``, the
family words ``os_family`` answers with).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from core import host_os

# --- the stored target value ------------------------------------------------

LOCAL = "local"
OFFLINE_PREFIX = "__offline__:"
#: Admission's word for a session of the platform's place that no compute
#: machine can take (``admission_target``): never a box, never a paired
#: computer, never stored.
NO_COMPUTE = "__no_compute__"


def is_local(target: str | None) -> bool:
    """The platform sandbox: ``local``, or empty (a row minted before the
    column was stamped on every path)."""
    return not target or target == LOCAL


def is_offline_sentinel(target: str | None) -> bool:
    """The resolver's hard-fail answer: the intended machine is unreachable
    and no fallback is allowed; the session must not start anywhere."""
    return bool(target) and cast("str", target).startswith(OFFLINE_PREFIX)


def offline_sentinel(machine_id: str) -> str:
    return f"{OFFLINE_PREFIX}{machine_id}"


def offline_machine_of(target: str | None) -> str:
    """The machine a sentinel names; ``""`` for any other value."""
    return cast("str", target)[len(OFFLINE_PREFIX):] if is_offline_sentinel(target) else ""


def machine_of(target: str | None) -> str:
    """The machine id a stored value names: the id itself, the id inside a
    sentinel (it still names the intended machine), ``""`` for the
    platform's place (and for admission's word for it with no machine)."""
    if is_local(target) or target == NO_COMPUTE:
        return ""
    if is_offline_sentinel(target):
        return offline_machine_of(target)
    return target or ""


def runs_on(target: str | None, machine_id: str, *, platform_machine: bool = False) -> bool:
    """The value names THIS machine and is not a sentinel — what a
    satellite-initiated session asks of a chat row or a resolved target
    (an offline answer never equals a live machine), and the rule a chat's
    pin and its session's own record agree by. ``platform_machine``: the
    machine is a compute machine, the platform's own, which serves the
    platform's place, so the pin ``local`` agrees with it."""
    if not machine_id or is_offline_sentinel(target):
        return False
    return machine_of(target) == machine_id or (platform_machine and is_local(target))


def compute_of(pl: PlacementCapabilities | None) -> PlacementCapabilities | None:
    """``pl`` when it is a compute machine's placement (its kind and its
    machine), else None: what a caller passes on to the layer and to
    admission, and nothing when the platform's own sandbox serves."""
    if pl is not None and pl.kind == KIND_COMPUTE and pl.machine_id:
        return pl
    return None


def admission_target(target: str | None, pl: PlacementCapabilities | None) -> str:
    """The value admission judges a session by: a compute placement's own
    machine (its sessions are the machine's to budget), a paired computer's
    stored target as it is, and ``NO_COMPUTE`` for the platform's place with
    no compute machine to take it: the platform runs no session itself, so
    admission refuses that one with the sentence and reserves nothing."""
    if pl is not None and pl.kind == KIND_COMPUTE and pl.machine_id:
        return pl.machine_id
    return NO_COMPUTE if is_local(target) else cast("str", target)


# --- the resolved kind and the pairing scope --------------------------------

KIND_LOCAL = "local"
KIND_ADMIN_REMOTE = "admin_remote"
KIND_USER_REMOTE = "user_remote"
KIND_COMPUTE = "compute"
KINDS = (KIND_LOCAL, KIND_ADMIN_REMOTE, KIND_USER_REMOTE, KIND_COMPUTE)

PAIRING_ADMIN = "admin"
PAIRING_USER = "user"
PAIRING_COMPUTE = "compute"
#: What a pairing mints: an admin's machine or a person's own.
PAIRING_SCOPES = (PAIRING_ADMIN, PAIRING_USER)
#: Every scope a machine row may hold: a compute machine is registered,
#: never paired, and nobody owns it.
MACHINE_SCOPES = (PAIRING_ADMIN, PAIRING_USER, PAIRING_COMPUTE)


def _scope(machine: dict | None) -> str:
    return (machine.get("pairing_scope") or "") if machine else ""


def machine_is_admin_paired(machine: dict | None) -> bool:
    """An admin-paired row (platform infrastructure); a missing row is not."""
    return _scope(machine) == PAIRING_ADMIN


def machine_is_personal(machine: dict | None) -> bool:
    """A person's own machine (paired from User Settings); a missing row, an
    admin's machine and a compute machine are not."""
    return _scope(machine) == PAIRING_USER


def machine_is_compute(machine: dict | None) -> bool:
    """A compute machine: the platform's own, registered with no owner. The
    sessions of the platform's place run on them."""
    return _scope(machine) == PAIRING_COMPUTE


def compute_own_copy(machine: dict | None) -> bool:
    """A compute machine on another host: it was paired from an address
    (recorded at its pairing, then the one it last signed in from) and works
    on its own copy of the folders, which the platform syncs as it does a
    paired machine's. One with no address yet, or the install's own, shares
    the platform's folders. The one reader of the row's mode."""
    return machine_is_compute(machine) and bool(str((machine or {}).get("compute_address") or ""))


# The ids the install makes for its compute machines (its own, an added one).
_COMPUTE_ID = re.compile(r"compute-(?:[0-9a-f]{12}|[0-9a-f]{16})")


def log_id(machine_id: str) -> str:
    """A machine's id in a log line: a compute id of the install's own shape
    whole, any other id cut to eight with nothing but letters, digits and
    dashes (a sign-in sends its id before anything verifies it)."""
    if _COMPUTE_ID.fullmatch(machine_id):
        return machine_id
    return re.sub(r"[^A-Za-z0-9-]", "?", machine_id[:8])


def owner_of(machine: dict | None) -> str:
    """The person who owns a PERSONAL machine; ``""`` for an admin's machine,
    a compute machine and no row. Compare it only with a non-empty sub: an
    empty one owns nothing."""
    return str(machine.get("registered_by") or "") if machine and machine_is_personal(machine) else ""


class PlacementRefused(RuntimeError):
    """A session refused by the placement guard (``may_run_on``); the message
    is the sentence the person sees."""


#: The guard's sentence when no caller named the place more precisely.
REFUSED_SENTENCE = ("This chat is on a machine you can't use. Pick one of your own places "
                    "from the Runs on menu.")

#: What every door says when no compute machine serves the platform's place.
NO_COMPUTE_SENTENCE = ("OtoDock's compute machine is not connected, so this cannot run now. "
                       "Try again in a few minutes.")


class NoComputeMachine(PlacementRefused):
    """A session of the platform's place with no compute machine to run it:
    the platform starts no session itself. Raised by the one reader of where
    such a session goes (``remote_status.compute_row_for``) and by the layer
    lookup, and shown by every door as the refusals of this family are."""

    def __init__(self) -> None:
        super().__init__(NO_COMPUTE_SENTENCE)


def admits_every_role(machine: dict | None, agent_default: str | None) -> bool:
    """Whether an admin machine lets a person below the owner tier run a
    session of this agent there: its admin turned on "Every role of its
    agents runs here" (``remote_machines.all_roles_run_here``) and the
    machine is the agent's own default place (``agent_default``, the
    agent's ``execution_target``). A personal machine, another agent's
    machine or no row: never."""
    if not machine_is_admin_paired(machine):
        return False
    row = cast("dict", machine)
    return (bool(row.get("all_roles_run_here")) and bool(row.get("id"))
            and machine_of(agent_default) == row.get("id"))


def may_run_on(machine: dict | None, *, run_sub: str | None, run_can_manage: bool,
               user_paired_allowed: bool, agent_default: str | None = "") -> bool:
    """The placement guard: whether a session of this person may run on the
    machine row ``machine`` (the caller passes ``None`` for no row: a
    deleted machine, never the sandbox, which is always allowed and never
    asked). A person's own paired machine, only theirs and only while the
    platform allows personal machines; an admin machine for a run with no
    person (a task, a call, a meeting), a person at the owner tier there
    (``run_can_manage``, the caller's ``roles.can_manage``), or any person
    when its admin let every role of its agents run there and it is this
    agent's default (``admits_every_role``; ``agent_default`` is the agent's
    ``execution_target``, empty refuses as before): the machine has no
    sandbox, so a role below the owner tier runs there only by that choice.
    A compute machine, and a scope no release writes: never.
    Every spawn on a machine passes it
    (``remote_session_start.placement_refuses``), and the config builder
    asks it first (``config_builder._placement_refusal``)."""
    if not machine or machine_is_compute(machine):
        return False
    if machine_is_admin_paired(machine):
        return not run_sub or run_can_manage or admits_every_role(machine, agent_default)
    return (machine_is_personal(machine) and bool(run_sub) and user_paired_allowed
            and (machine.get("registered_by") or "") == run_sub)


# --- the site a document names ----------------------------------------------

SITE_LOCAL = "local"
SITE_MACHINE = "machine"


# --- the machine row's facts ------------------------------------------------

def parse_device_grants(raw) -> set:
    """The ``device_grants`` column (a JSON array) as the set of granted
    capability keys. None / malformed / not a list → the empty set
    (fail-closed: an empty set blocks every device-local MCP)."""
    if not raw:
        return set()
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return set()
    return {str(x) for x in val} if isinstance(val, list) else set()


_WINDOWS_DRIVE_RE = re.compile(r"^([a-zA-Z]):[\\/]")
# The per-user install root's name on each family: what an agents root
# shaped ``<root>/agents`` is recognised by when no home was reported.
_STATE_DIRNAMES = frozenset(cast("host_os.HostOS", host_os.of(f)).dirname for f in host_os.FAMILIES)


@dataclass(frozen=True)
class KindFacts:
    """What one kind of placement is, each fact named once.

    ``on_machine``: the session's process is a machine's, reached over its
    link. ``files_synced``: the machine works on a synced copy of the
    folders. ``sandboxed``: the platform's kernel sandbox wraps the session.
    ``translates_paths``: sandbox paths become the machine's own. ``memory_admitted``:
    the memory gates count it (a machine of a person or an admin is budgeted
    by its own session count). ``device_tools``: device tools may be offered,
    by the machine's grants."""

    on_machine: bool
    files_synced: bool
    sandboxed: bool
    translates_paths: bool
    memory_admitted: bool
    device_tools: bool


#: A compute machine is a machine the platform owns: its process is on the
#: machine, as a satellite's, and everything else is the platform sandbox's.
_FACTS: dict[str, KindFacts] = {
    KIND_LOCAL: KindFacts(on_machine=False, files_synced=False, sandboxed=True,
                          translates_paths=False, memory_admitted=True, device_tools=False),
    KIND_ADMIN_REMOTE: KindFacts(on_machine=True, files_synced=True, sandboxed=False,
                                 translates_paths=True, memory_admitted=False, device_tools=True),
    KIND_USER_REMOTE: KindFacts(on_machine=True, files_synced=True, sandboxed=False,
                                translates_paths=True, memory_admitted=False, device_tools=True),
    KIND_COMPUTE: KindFacts(on_machine=True, files_synced=False, sandboxed=True,
                            translates_paths=False, memory_admitted=True, device_tools=False),
}


def facts_of(kind: str) -> KindFacts:
    """The facts of ``kind``; an unknown word answers as a person's machine,
    the kind with the most rules on it."""
    return _FACTS.get(kind) or _FACTS[KIND_USER_REMOTE]


@dataclass(frozen=True)
class PlacementCapabilities:
    """The resolved placement of one session. The defaults are the local
    sandbox; a machine's facts come from its row's last-reported
    capabilities (``from_machine``), empty when the row is gone or its
    capabilities cannot be read — the path gate then fail-closes to
    sandbox-virtual paths."""

    kind: str = KIND_LOCAL
    machine_id: str = ""            # for mid-session revocation detection
    label: str = ""                 # the machine's name (the prompt)
    os: str = ""                    # linux | darwin | windows | "" (unreported)
    satellite_version: str = ""     # the machine's satellite, as its row last recorded it
    exit_port: int = 0              # the machine's credential exit listener (0: it runs none)
    exit_ok: bool = False           # whether that listener answers now
    exit_why: str = ""              # why it does not ("installing", "packages", "bind", "socks")
    exit_upstream: bool = False     # whether it reaches the world through an outgoing proxy
    home_dir: str = ""              # the OS user's home on the machine
    agents_dir: str = ""            # the machine's agent-tree root
    os_user: str = ""               # the OS account the satellite runs as
    allow_full_fs: bool = False     # the machine's pairing flag
    claude_runtime_root: str = ""   # ``<tempdir>/claude-<uid>`` on the machine
    user_dirs: dict = field(default_factory=dict)   # desktop, downloads, …
    device_grants: set = field(default_factory=set)  # computer / browser / app
    has_display: bool | None = None  # None = unreported
    reported_otodock_dir: str = ""  # the machine's per-user OtoDock root, as it reported it
    mcps_dir: str = ""              # the machine's MCP folder, as it reported it
    # The home and the OtoDock root as the OS names them, links and short
    # names unresolved (the two above are resolved); "" when unreported.
    unresolved_home_dir: str = ""
    unresolved_otodock_dir: str = ""
    # A compute machine on another host: its own copy of the folders
    # (``compute_own_copy``), synced as a paired machine's.
    own_copy: bool = False
    # The engine folders (``.claude``, ``.codex``) where the machine's
    # satellite gives each session a home of its own (``home_dirs`` of its
    # report): the pool picks an account per session there
    # (``session_config_dir.session_home_for``).
    session_homes: frozenset[str] = frozenset()

    @property
    def facts(self) -> KindFacts:
        return facts_of(self.kind)

    @property
    def is_remote(self) -> bool:
        """The session's process is a machine's, reached over its link (a
        person's, an admin's or a compute machine), as opposed to the
        proxy's own. This one fact only: the sandbox, the path translation,
        a synced copy and the device tools each have their own."""
        return self.facts.on_machine

    @property
    def admin_paired(self) -> bool:
        """The KIND is the agent's admin-paired default machine. An
        admin-scoped row a person attached as their own override resolves
        user-remote: ``admin_paired`` is false there while
        ``machine_is_admin_paired(row)`` is true — the prompt section, the
        ssh-hosts delivery and the context-only MCPs follow the kind."""
        return self.kind == KIND_ADMIN_REMOTE

    @property
    def user_paired(self) -> bool:
        return self.kind == KIND_USER_REMOTE

    @property
    def isolates_with_bwrap(self) -> bool:
        """The platform's kernel sandbox wraps the session: on the platform
        host and on a compute machine."""
        return self.facts.sandboxed

    @property
    def needs_path_translation(self) -> bool:
        """Sandbox-virtual paths translate to the machine's native ones: a
        person's or an admin's machine."""
        return self.facts.translates_paths

    @property
    def files_synced(self) -> bool:
        """The session works on a synced copy of the folders on its machine:
        a person's or an admin's machine, and a compute machine on another
        host (one beside the platform works on the platform's own folder)."""
        return self.facts.files_synced or self.own_copy

    @property
    def counts_toward_capacity(self) -> bool:
        """The memory gates count the session; a person's or an admin's
        machine is budgeted by its own session count."""
        return self.facts.memory_admitted

    @property
    def offers_device_tools(self) -> bool:
        """Device tools may be offered, by the machine's own grants: a
        person's or an admin's machine, never the platform's places."""
        return self.facts.device_tools

    @property
    def site(self) -> str:
        """The two-word answer a check input document and an app's step
        target carry: where the files a check reads are. A machine only when
        it holds a synced copy and is known: a remote kind on a missing row
        probes locally, as the checks always did, and the files of a compute
        machine beside the platform are the platform's own."""
        return SITE_MACHINE if (self.files_synced and self.machine_id) else SITE_LOCAL

    @property
    def os_family(self) -> str:
        """The family path normalisation keys on: the reported ``os`` when
        the machine reported one, else the shape of its paths (a drive
        letter → windows, ``/Users/`` → darwin), else linux."""
        if self.os in host_os.FAMILIES:
            return self.os
        for s in (self.agents_dir, self.home_dir):
            if s and _WINDOWS_DRIVE_RE.match(s.replace("\\", "/")):
                return host_os.WINDOWS
        if "/Users/" in (self.home_dir or ""):
            return host_os.DARWIN
        return host_os.LINUX

    @property
    def otodock_dir(self) -> str:
        """The machine's per-user OtoDock root: the one a 0.5.130 satellite
        reports, else the derivation (``_derived_otodock_dir``) an older
        report leaves to the proxy."""
        return self.reported_otodock_dir or self._derived_otodock_dir()

    def _derived_otodock_dir(self) -> str:
        """The root derived the way the satellite defines it (its home joined
        with the family's dirname: ``.oto-dock``, ``OtoDock`` on Windows);
        with no home reported, the parent of an agents root shaped
        ``<root>/agents``; else ``""``."""
        dirname = cast("host_os.HostOS", host_os.of(self.os_family)).dirname
        if self.home_dir:
            return self.home_dir.replace("\\", "/").rstrip("/") + "/" + dirname
        agents = (self.agents_dir or "").replace("\\", "/").rstrip("/")
        parent, _, last = agents.rpartition("/")
        if last == "agents" and parent.rsplit("/", 1)[-1] in _STATE_DIRNAMES:
            return parent
        return ""

    @property
    def state_dirs(self) -> tuple:
        """The roots the path gate refuses to every remote session: the
        reported OtoDock root AND the one derived from the reported home (an
        older satellite reports no root), the same two spelled through the
        home as the OS names it (a home reached through a link, or by its
        8.3 short name on Windows, resolves to the reported one only on the
        machine), the reported MCP folder (wherever the operator put it),
        plus ``<home>/.oto-dock`` on Windows, where the browser profiles live
        under the posix name whatever the family. A root already under a
        listed one is not repeated; Windows folds case."""
        fold = self.os_family == host_os.WINDOWS
        dirname = cast("host_os.HostOS", host_os.of(self.os_family)).dirname
        homes = [h.replace("\\", "/").rstrip("/")
                 for h in (self.home_dir, self.unresolved_home_dir) if h]
        roots: list[str] = []

        def _add(raw: str) -> None:
            r = (raw or "").replace("\\", "/").rstrip("/")
            if not r:
                return
            key = r.lower() if fold else r
            for have in roots:
                h = have.lower() if fold else have
                if key == h or key.startswith(h + "/"):
                    return
            roots.append(r)

        _add(self.reported_otodock_dir)
        _add(self._derived_otodock_dir())
        _add(self.unresolved_otodock_dir)
        if self.unresolved_home_dir:
            _add(homes[-1] + "/" + dirname)
        _add(self.mcps_dir)
        if fold:
            for home in homes:
                _add(home + "/.oto-dock")
        return tuple(roots)


LOCAL_PLACEMENT = PlacementCapabilities()


#: The live connection's report of a machine's credential exit, registered
#: by the connection manager's module (this leaf imports nothing of it);
#: None before the remote side is up, when the row's copy answers alone.
_live_exit_state: Callable[[str], tuple[int, bool]] | None = None


def register_live_exit_state(reader: Callable[[str], tuple[int, bool]]) -> None:
    global _live_exit_state
    _live_exit_state = reader


#: The live connection's report beside the port and the answer (``why``,
#: ``upstream``), registered the same way.
_live_exit_report: Callable[[str], dict] | None = None


def register_live_exit_report(reader: Callable[[str], dict]) -> None:
    global _live_exit_report
    _live_exit_report = reader


def _exit_report(machine: dict) -> tuple[str, bool]:
    """``(why, upstream)`` of the machine's credential exit: the live
    connection's report first, the row's capabilities as the fallback."""
    machine_id = str(machine.get("id") or "")
    live = _live_exit_report(machine_id) if (_live_exit_report is not None and machine_id) else {}
    if live:
        return str(live.get("why") or ""), live.get("upstream") is True
    try:
        caps_raw = machine.get("capabilities") or "{}"
        caps = json.loads(caps_raw) if isinstance(caps_raw, str) else (caps_raw or {})
    except (TypeError, ValueError):
        return "", False
    if not isinstance(caps, dict):
        return "", False
    why = caps.get("exit_why")
    return (why if isinstance(why, str) else ""), caps.get("exit_upstream") is True


def _exit_state(machine: dict) -> tuple[int, bool]:
    """The machine's credential exit as it stands now: the live connection's
    report first (the heartbeat moves it, the row's copy lands off the
    loop), the row's capabilities as the fallback. ``(0, False)`` for a
    satellite that reports none (one that runs no exit)."""
    machine_id = str(machine.get("id") or "")
    live = _live_exit_state(machine_id) if (_live_exit_state is not None and machine_id) else (0, False)
    if live[0]:
        return live
    try:
        caps_raw = machine.get("capabilities") or "{}"
        caps = json.loads(caps_raw) if isinstance(caps_raw, str) else (caps_raw or {})
    except (TypeError, ValueError):
        return 0, False
    if not isinstance(caps, dict):
        return 0, False
    port = caps.get("exit_port")
    port = port if type(port) is int and 0 < port < 65536 else 0
    return port, bool(port and caps.get("exit_ok") is True)


#: The engine folders a satellite that reports ``session_homes`` alone makes
#: homes in (0.5.191 and older): Claude Code's.
_HOMES_BEFORE_DIRS = frozenset({".claude"})


def home_dirs(caps) -> frozenset[str]:
    """The engine folders a satellite's report says it makes session homes
    in: its ``session_home_dirs``, else Claude Code's for a report of
    ``session_homes`` alone, else none. A live connection's report and a
    machine row's are read the same way."""
    if not isinstance(caps, dict):
        return frozenset()
    dirs = caps.get("session_home_dirs")
    if isinstance(dirs, list):
        return frozenset(d for d in dirs if isinstance(d, str) and d)
    return _HOMES_BEFORE_DIRS if caps.get("session_homes") is True else frozenset()


def _reported_home_dirs(machine: dict) -> frozenset[str]:
    """``home_dirs`` of the report on a machine's row (a compute machine's row
    is read for this fact alone)."""
    raw = machine.get("capabilities") or "{}"
    try:
        caps = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return frozenset()
    return home_dirs(caps)


def from_machine(kind: str, machine: dict | None) -> PlacementCapabilities:
    """The placement of ``kind`` on a machine row. A missing row, or one whose
    capabilities cannot be read, answers the kind with every fact empty
    (``label`` survives — it needs no parsing): the path gate fail-closes,
    the revocation check sees no machine id, exactly as the disassembled
    readers answered. A compute placement carries its id and name only:
    its sessions see sandbox paths, and no path, OS user, full access or
    device grant of the row may reach a reader that reads them ungated."""
    if not machine:
        return PlacementCapabilities(kind=kind)
    label = str(machine.get("name") or "")
    version = str(machine.get("satellite_version") or "")
    exit_port, exit_ok = _exit_state(machine)
    exit_why, exit_upstream = _exit_report(machine)
    if kind == KIND_COMPUTE:
        return PlacementCapabilities(kind=kind, machine_id=str(machine.get("id") or ""), label=label,
                                     satellite_version=version, exit_port=exit_port, exit_ok=exit_ok,
                                     exit_why=exit_why, exit_upstream=exit_upstream,
                                     own_copy=compute_own_copy(machine),
                                     session_homes=_reported_home_dirs(machine))
    try:
        caps_raw = machine.get("capabilities") or "{}"
        caps = json.loads(caps_raw) if isinstance(caps_raw, str) else (caps_raw or {})
        if not isinstance(caps, dict):
            raise TypeError("capabilities is not an object")
        display = caps.get("display")
        has_display = (bool(display["has_display"])
                       if isinstance(display, dict) and "has_display" in display else None)
        user_dirs = caps.get("user_dirs") or {}
        return PlacementCapabilities(
            kind=kind,
            machine_id=str(machine.get("id") or ""),
            label=label,
            satellite_version=version,
            exit_port=exit_port,
            exit_ok=exit_ok,
            exit_why=exit_why,
            exit_upstream=exit_upstream,
            os=str(caps.get("os") or ""),
            home_dir=str(caps.get("home_dir") or ""),
            agents_dir=str(caps.get("agents_dir") or ""),
            os_user=str(caps.get("os_user") or ""),
            allow_full_fs=bool(machine.get("allow_full_fs") or False),
            claude_runtime_root=str(caps.get("claude_runtime_root") or ""),
            user_dirs=dict(user_dirs) if isinstance(user_dirs, dict) else {},
            device_grants=parse_device_grants(machine.get("device_grants")),
            has_display=has_display,
            reported_otodock_dir=str(caps.get("otodock_dir") or ""),
            mcps_dir=str(caps.get("mcps_dir") or ""),
            unresolved_home_dir=str(caps.get("home_dir_unresolved") or ""),
            unresolved_otodock_dir=str(caps.get("otodock_dir_unresolved") or ""),
            session_homes=home_dirs(caps),
        )
    except (ValueError, TypeError):
        return PlacementCapabilities(kind=kind, label=label, satellite_version=version)
