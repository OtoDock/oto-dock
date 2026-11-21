"""What each machine holds of the platform's tools: one memo.

Per machine: each tool's hash and health as the machine last said (its
verify, a sync's acknowledgement), and what an ahead run is installing and
what failed, which is what a paired machine's card shows. It is the memo of the
sync's own reads (``mcp_sync._fetch_satellite_state``, ``mcp_sync._concluded``),
never a second question to the machine, so its readers never wait: a
session's build on a compute machine that builds its own tools (``refusal``),
the relay's open (``relay_gate``), the machine's card (``tools_ahead.card``)
and the updater's rebuild offer (``missing_on_builder``).

In memory: a platform restart starts empty and the machine's sign-in fills
it (its first verify). Until that verify answers, the tools the machine
named at its sign-in read as held (``_named_at_signin``), so a chat that
starts in that second is not told its tools are missing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import placement

#: What the agent reads for a tool its compute machine has not built yet,
#: and for one whose build there failed.
INSTALLING_SENTENCE = "This tool is being installed. Try again in a minute."
FAILED_SENTENCE = "This tool could not be installed on OtoDock's compute machine."
#: The failure of a tool whose folder a machine refuses: it is never sent.
UNSAFE_KEY_CAUSE = ("Its folder name cannot be used on a machine. Uninstall it and install it again "
                    "under a name of letters, digits, dashes and underscores.")

#: How long a chat's start and an open leave a build that just failed alone
#: before they ask the machine's run for it again. A door (an update, an
#: enable, an assignment, a sign-in) starts the run itself, at once.
RETRY_FAILED_S = 600.0

#: The most the platform shows or keeps of anything a machine says: a
#: failure's cause, a progress line. A machine cuts its own cause to the
#: same length. This is the bound for one that says more.
SAID_LIMIT = 600

# The machine's own rule for a tool's folder (the satellite's
# ``auth_paths.is_safe_slug``): it installs and removes no other.
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.IGNORECASE)


def said(text) -> str:
    """A machine's words as the platform takes them: one line of at most
    ``SAID_LIMIT`` characters, with the sign-in of any address masked
    (``mcp_installer.mask_sign_ins``, the mask the machine applies to its
    own cause)."""
    from services.mcp import mcp_installer
    line = mcp_installer.mask_sign_ins(" ".join(str(text or "").split()))
    return line if len(line) <= SAID_LIMIT else line[:SAID_LIMIT - 3].rstrip() + "..."


def key_of(manifest) -> str:
    """The name a machine keeps the tool under: its folder on the platform,
    which a session's config names too. It is the tool's name for every
    tool but one uploaded before names had the machine's rule."""
    return Path(manifest.mcp_dir).name


def safe_key(key: str) -> bool:
    """Whether a machine takes a tool under the folder name ``key``."""
    return bool(_KEY_RE.fullmatch(key or ""))


@dataclass
class MachineTools:
    """One machine's tools: ``held`` as it last said them
    (``{name: {"version_hash", "healthy", "category"}}``, with ``verified``
    once a verify answered), and the ahead run's ``installing`` and
    ``failed`` (each failure's cause) for its card, with when the run noted
    each failure (``failed_at``, the monotonic clock)."""
    held: dict[str, dict] = field(default_factory=dict)
    verified: bool = False
    installing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    failed_at: dict[str, float] = field(default_factory=dict)


_machines: dict[str, MachineTools] = {}


def of(machine_id: str) -> MachineTools:
    """The machine's memo, made when it has none."""
    return _machines.setdefault(machine_id, MachineTools())


def forget(machine_id: str) -> None:
    """The machine was removed: its memo goes."""
    _machines.pop(machine_id, None)
    _wanted.discard(machine_id)


def note_verify(machine_id: str, mcps: dict) -> None:
    """The machine's verify answered: what it holds, whole."""
    memo = of(machine_id)
    memo.held = {str(name): dict(info) for name, info in (mcps or {}).items() if isinstance(info, dict)}
    memo.verified = True


def note_installed(machine_id: str, name: str, version_hash: str, *, category: str = "") -> None:
    """A sync's acknowledgement: the machine built ``name`` at ``version_hash``."""
    held = of(machine_id).held
    held[name] = {"version_hash": version_hash, "healthy": True,
                  "category": category or (held.get(name) or {}).get("category", "")}


def note_removed(machine_id: str, name: str) -> None:
    of(machine_id).held.pop(name, None)


def note_failed(machine_id: str, name: str, cause) -> None:
    """The machine's run could not build ``name``: its cause for the card
    and the chat, and when, so a chat's start does not ask for the same
    build again at once (``just_failed``)."""
    memo = of(machine_id)
    memo.failed[name] = said(cause)
    memo.failed_at[name] = time.monotonic()


def note_built(machine_id: str, name: str) -> None:
    """The machine's run built ``name``: its failure is forgotten."""
    memo = of(machine_id)
    memo.failed.pop(name, None)
    memo.failed_at.pop(name, None)


def just_failed(machine_id: str, name: str) -> bool:
    """Whether the machine's build of ``name`` failed within the last
    ``RETRY_FAILED_S``. A failure nobody timed reads as old."""
    memo = _machines.get(machine_id)
    at = memo.failed_at.get(name) if memo is not None and name in memo.failed else None
    return at is not None and time.monotonic() - at < RETRY_FAILED_S


def _want(machine_id: str, name: str) -> None:
    """A build or an open found ``name`` missing on the machine: its run is
    wanted (``ask``), unless that build just failed there."""
    if not just_failed(machine_id, name):
        _wanted.add(machine_id)


def holds(machine_id: str, name: str, version_hash: str, *, key: str = "") -> bool:
    """Whether the machine holds ``name``, healthy, at ``version_hash`` (the
    platform's current one). A tool it has said nothing of before its first
    verify since the platform started: whether it named it at its sign-in,
    by its ``key`` there (``key_of``, the name when not given)."""
    memo = _machines.get(machine_id)
    info = memo.held.get(name) if memo is not None else None
    if info is None and (memo is None or not memo.verified):
        return (key or name) in _named_at_signin(machine_id)
    return bool(info and info.get("healthy") and version_hash
                and info.get("version_hash") == version_hash)


def _named_at_signin(machine_id: str) -> frozenset[str]:
    from core.remote.satellite_connection import get_connection_manager
    conn = get_connection_manager().get_connection(machine_id)
    return frozenset((getattr(conn, "capabilities", None) or {}).get("installed_mcps") or ())


def built_on_machine(manifest) -> bool:
    """A tool a compute machine that builds its own tools must have built
    before a session can use it: a community tool with a program to
    install. The bundled tools come with the machine's image."""
    from services.mcp import mcp_manifest_types as _mt
    return manifest.category == "community" and _mt.installs_on_host(manifest.server)


def _held_now(machine_id: str, manifest) -> bool:
    """Whether the machine holds the manifest's tool at the platform's
    current version. Reads memory and the cached hash alone."""
    from services.mcp import mcp_sync
    try:
        current = mcp_sync._version_hash(manifest)
    except (OSError, ValueError):
        current = ""
    return holds(machine_id, manifest.name, current, key=key_of(manifest))


def _failure(machine_id: str, name: str) -> str:
    """Why the machine's last install of ``name`` failed, ``""`` when it did
    not (the ahead run's record, which a paired machine's card shows too)."""
    memo = _machines.get(machine_id)
    return said(memo.failed.get(name)) if memo is not None and name in memo.failed else ""


def refusal(manifest, pl) -> str:
    """Why a session placed at ``pl`` is built without ``manifest``'s tool,
    ``""`` when it gets it: the placement's compute machine builds its own
    tools and does not hold this one at the platform's version (not built
    yet, an install running, an older build, a build that failed, which the
    sentence then says with its cause). The session never waits for the
    install: its start then asks for it (``ask``), but for a build that
    failed within the last ten minutes (``just_failed``). Synchronous, from
    the build's worker thread."""
    machine = placement.compute_of(pl)
    if machine is None or not built_on_machine(manifest):
        return ""
    from core.remote.satellite_connection import get_connection_manager
    if (not get_connection_manager().builds_own_tools(machine.machine_id)
            or _held_now(machine.machine_id, manifest)):
        return ""
    _want(machine.machine_id, manifest.name)
    cause = _failure(machine.machine_id, manifest.name)
    if cause:
        return f"{manifest.label} could not be installed on OtoDock's compute machine ({cause})"
    return (f"{manifest.label} is being installed on OtoDock's compute machine. "
            "A new chat can use it once it is done")


def _manifest_of(server: str):
    """The registry's tool whose key in a session's config is ``server``."""
    from services.mcp import mcp_registry
    return next((m for m in mcp_registry.get_all_manifests().values()
                 if (m.server_name or m.name) == server), None)


def relay_gate(machine_id: str, server: str) -> tuple[str, str]:
    """``(tool_dir, refusal)`` for a relayed open of the tool ``server`` (its
    key in the session's config) on ``machine_id``: the folder of a tool the
    machine builds itself, which the machine checks is there, and a sentence
    when it does not hold the tool at the platform's version
    (``INSTALLING_SENTENCE``, or ``FAILED_SENTENCE`` after a build that
    failed; the caller then asks for the install, ``ask``). ``("", "")``
    for every other tool and machine."""
    from core.remote.satellite_connection import get_connection_manager
    if not get_connection_manager().builds_own_tools(machine_id):
        return "", ""
    manifest = _manifest_of(server)
    if manifest is None or not built_on_machine(manifest):
        return "", ""
    if _held_now(machine_id, manifest):
        return str(manifest.mcp_dir), ""
    _want(machine_id, manifest.name)
    return str(manifest.mcp_dir), (FAILED_SENTENCE if _failure(machine_id, manifest.name)
                                   else INSTALLING_SENTENCE)


def note_not_there(machine_id: str, server: str) -> None:
    """The machine itself answered that the folder of the tool ``server`` is
    not there (a relayed open): the memo forgets the tool and a run is
    wanted, which verifies and builds it again."""
    manifest = _manifest_of(server)
    if manifest is not None:
        note_removed(machine_id, manifest.name)
        _wanted.add(machine_id)


_wanted: set[str] = set()


def ask(machine_id: str) -> None:
    """Start the machine's ahead run now when a build or an open found a
    tool missing on it since the last ask. On the platform's loop."""
    if machine_id in _wanted:
        _wanted.discard(machine_id)
        from services.remote import tools_ahead
        tools_ahead.schedule(machine_id, 0.0)


def missing_on_builder(name: str) -> bool:
    """Whether the install's building machine is connected, has answered a
    verify and does not hold ``name`` in a healthy build: the updater then
    offers the rebuild. Unknown (no machine, no verify yet) reads as held."""
    from core.remote.satellite_connection import get_connection_manager
    machine_id = get_connection_manager().building_machine()
    memo = _machines.get(machine_id) if machine_id else None
    if memo is None or not memo.verified:
        return False
    info = memo.held.get(name)
    return not (info and info.get("healthy"))
