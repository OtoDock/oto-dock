"""Placement — where a session runs, named once.

A session runs in the platform's bwrap sandbox or on a paired machine (a
satellite). The tree spells that in three vocabularies, every one of them
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
  default machine, or the person's own paired machine — ``KIND_*`` (the
  security index persists it as ``target_kind``), and the scope a machine
  row was paired under (``remote_machines.pairing_scope``): ``PAIRING_*``;
- the SITE a check input document carries under ``session.placement.kind``
  and an app's ``step_target.kind``: ``"local"`` or ``"machine"`` —
  ``SITE_*``.

``PlacementCapabilities`` is the resolved placement of ONE session with the
facts the path policy, the prompt, the MCP gates, the checks and the
capacity accounting ask. ``storage.remote_store.placement_of`` builds it once
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
from dataclasses import dataclass, field

from core import host_os

# --- the stored target value ------------------------------------------------

LOCAL = "local"
OFFLINE_PREFIX = "__offline__:"


def is_local(target: str | None) -> bool:
    """The platform sandbox: ``local``, or empty (a row minted before the
    column was stamped on every path)."""
    return not target or target == LOCAL


def is_offline_sentinel(target: str | None) -> bool:
    """The resolver's hard-fail answer: the intended machine is unreachable
    and no fallback is allowed; the session must not start anywhere."""
    return bool(target) and target.startswith(OFFLINE_PREFIX)


def offline_sentinel(machine_id: str) -> str:
    return f"{OFFLINE_PREFIX}{machine_id}"


def offline_machine_of(target: str | None) -> str:
    """The machine a sentinel names; ``""`` for any other value."""
    return target[len(OFFLINE_PREFIX):] if is_offline_sentinel(target) else ""


def machine_of(target: str | None) -> str:
    """The machine id a stored value names: the id itself, the id inside a
    sentinel (it still names the intended machine), ``""`` for the sandbox."""
    if is_local(target):
        return ""
    if is_offline_sentinel(target):
        return offline_machine_of(target)
    return target or ""


def runs_on(target: str | None, machine_id: str) -> bool:
    """The value names THIS machine and is not a sentinel — what a
    satellite-initiated session asks of a chat row or a resolved target
    (an offline answer never equals a live machine)."""
    return bool(machine_id) and not is_offline_sentinel(target) and machine_of(target) == machine_id


# --- the resolved kind and the pairing scope --------------------------------

KIND_LOCAL = "local"
KIND_ADMIN_REMOTE = "admin_remote"
KIND_USER_REMOTE = "user_remote"
KINDS = (KIND_LOCAL, KIND_ADMIN_REMOTE, KIND_USER_REMOTE)
REMOTE_KINDS = (KIND_ADMIN_REMOTE, KIND_USER_REMOTE)

PAIRING_ADMIN = "admin"
PAIRING_USER = "user"
PAIRING_SCOPES = (PAIRING_ADMIN, PAIRING_USER)


def machine_is_admin_paired(machine: dict | None) -> bool:
    """An admin-paired row (platform infrastructure); a missing row is not."""
    return bool(machine) and (machine.get("pairing_scope") or "") == PAIRING_ADMIN


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
_STATE_DIRNAMES = frozenset(host_os.of(f).dirname for f in host_os.FAMILIES)


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

    @property
    def is_remote(self) -> bool:
        """A satellite of either kind, as opposed to the local sandbox."""
        return self.kind in REMOTE_KINDS

    @property
    def is_local(self) -> bool:
        return not self.is_remote

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
        """The kernel sandbox exists only on the platform host."""
        return self.is_local

    @property
    def needs_path_translation(self) -> bool:
        """Sandbox-virtual paths translate to the machine's native ones."""
        return self.is_remote

    @property
    def counts_toward_capacity(self) -> bool:
        """A local session takes a slot of the host's budget; a remote one
        is budgeted by its satellite."""
        return self.is_local

    @property
    def site(self) -> str:
        """The two-word answer a check input document and an app's step
        target carry: a machine only when one is known — a remote kind on
        a missing row probes locally, as the checks always did."""
        return SITE_MACHINE if (self.is_remote and self.machine_id) else SITE_LOCAL

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
        dirname = host_os.of(self.os_family).dirname
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
        dirname = host_os.of(self.os_family).dirname
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


def from_machine(kind: str, machine: dict | None) -> PlacementCapabilities:
    """The placement of ``kind`` on a machine row. A missing row, or one whose
    capabilities cannot be read, answers the kind with every fact empty
    (``label`` survives — it needs no parsing): the path gate fail-closes,
    the revocation check sees no machine id, exactly as the disassembled
    readers answered."""
    if not machine:
        return PlacementCapabilities(kind=kind)
    label = str(machine.get("name") or "")
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
        )
    except Exception:
        return PlacementCapabilities(kind=kind, label=label)
