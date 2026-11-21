"""A compute machine on another server: its pairing, its address and its image.

An admin adds one in person and gets one command (``install_command``) that
pairs the server the way a paired computer's does: the same bootstrap door
(``/v1/satellite/bootstrap``) with a one-time pairing token. For a compute
row the door answers the compute install script (``bootstrap_script``,
``scripts/remote-compute-install.sh`` with the host service
``scripts/otodock-remote-compute.sh``), which checks the server, makes the
machine's secret there, sends only its hash in the exchange and starts the
platform's image under a systemd unit.

- **The address** (``link_address``, ``address_refusal``): the address the
  machine last signed in from, read by one rule for its pairing and every
  sign-in. By default any address is taken (the pairing is the proof); an
  admin's "Lock to this address" pins it, and a sign-in from another address
  is refused and every admin told, at most once per machine in
  ``NOTICE_EVERY_S``.
- **The image** (``wanted_image``, ``image_refusal``): the one
  ``OTODOCK_REMOTE_COMPUTE_IMAGE`` names (an install built from source loads
  it on its servers), else the image this proxy runs (``OTODOCK_PROXY_IMAGE``,
  set by the compose file, rc tags included), else none, and then no machine
  is added. A machine on another tag of that repository is refused with the
  update word; its host service asks this platform for the tag
  (``host_answer``), never the machine.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger("otodock")

#: The public image a released install's compute machines run.
PUBLIC_REPOSITORY = "ghcr.io/otodock/otodock-proxy"
#: Where the platform's image keeps its tools. A session's tool config names
#: each tool by the platform's own tools path, and a machine on another
#: server runs that image, so the two must be one path: an install in Docker.
IMAGE_TOOLS_PATH = "/opt/otodock/mcps"
#: Why an install whose tools sit elsewhere (bare metal) adds no such machine.
NOT_THIS_INSTALL = ("This install runs outside Docker, so a compute machine on another server cannot run its "
                    "tools. Only an install in Docker can add one.")
#: Why an install that knows no image for such a machine adds none.
NO_IMAGE = ("This install was built from source. Set OTODOCK_REMOTE_COMPUTE_IMAGE to the image you load on "
            "its servers.")
BAD_IMAGE = "OTODOCK_REMOTE_COMPUTE_IMAGE must name an image with a tag, like registry.example.com/otodock:7."
#: The shape of the host service the install command writes: the options,
#: mounts and environment it gives the container. They are the installed
#: tool's, never the platform's word, so a change to them raises this number
#: and a server whose host claims a lower one is paired again (its card says
#: so). The lane that raises it adds the refusal at sign-in.
HOST_SHAPE = 1
#: At most one "sign-in refused" notice per machine in this many seconds.
NOTICE_EVERY_S = 15 * 60
#: The refused addresses kept per machine for the next notice.
_REFUSED_KEPT = 8

#: The install body, the host service and the AppArmor setup, under the
#: platform's ``scripts`` folder (in the image and in a checkout).
INSTALL_SCRIPT, HOST_TOOL, APPARMOR_SETUP = (
    "remote-compute-install.sh", "otodock-remote-compute.sh", "setup-apparmor-userns.sh")

_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
# A registry host is Docker's: a name with a dot or a port, or localhost.
_REGISTRY = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::[0-9]+)?|localhost(?::[0-9]+)?|[A-Za-z0-9-]+:[0-9]+)"
_REPOSITORY = re.compile(rf"(?:{_REGISTRY}/)?{_COMPONENT}(?:/{_COMPONENT})*")
_MACHINE_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")
_WEB_URL = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]{1,300}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9]+)*")


@dataclass(frozen=True)
class Image:
    """An image as the host service runs it: a repository and a tag.
    ``public``: the platform's released image, which a server downloads."""

    repository: str
    tag: str
    public: bool

    @property
    def ref(self) -> str:
        return f"{self.repository}:{self.tag}"


def add_refusal() -> str:
    """Why this install cannot add (or pair again) a compute machine on
    another server, ``""`` when it can: its tools path must be the image's,
    and it must know which image such a machine runs."""
    if str(config.MCPS_DIR) != IMAGE_TOOLS_PATH:
        return NOT_THIS_INSTALL
    if wanted_image() is None:
        return BAD_IMAGE if (config.OTODOCK_REMOTE_COMPUTE_IMAGE or "").strip() else NO_IMAGE
    return ""


def split_image(ref: str) -> tuple[str, str] | None:
    """``repository:tag`` split at the last colon after the last slash; None
    for anything else (no tag, a digest, a word outside Docker's grammar)."""
    ref = (ref or "").strip()
    head, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag or "@" in ref or not _TAG.fullmatch(tag) or not _REPOSITORY.fullmatch(head):
        return None
    return head, tag


def wanted_image() -> Image | None:
    """The image a compute machine on another server runs now: the key's,
    else this proxy's own (public when it is the released repository), else
    none (an install built from source that names none)."""
    named = (config.OTODOCK_REMOTE_COMPUTE_IMAGE or "").strip()
    if named:
        parts = split_image(named)
        if parts:
            return Image(parts[0], parts[1], False)
        logger.warning("OTODOCK_REMOTE_COMPUTE_IMAGE=%r is not an image with a tag", named)
        return None
    parts = split_image(config.OTODOCK_PROXY_IMAGE or "")
    if parts:
        return Image(parts[0], parts[1], parts[0] == PUBLIC_REPOSITORY)
    return None


def _host_shape(claimed: object) -> int:
    """A host service's claimed shape when it is a small whole number, else
    0: the claim is the machine's own word."""
    if isinstance(claimed, bool) or not isinstance(claimed, int) or not 0 < claimed < 10_000:
        return 0
    return claimed


def host_behind(capabilities: dict | None) -> bool:
    """Whether a machine's host service claims a shape older than this
    platform's (the card then offers "Pair again")."""
    claimed = capabilities.get("compute_host") if isinstance(capabilities, dict) else None
    return 0 < _host_shape(claimed) < HOST_SHAPE


def kept_claims(capabilities: dict) -> dict:
    """A sign-in's capabilities as the platform keeps them (the row the
    admins' list reads): a host shape that is not a small whole number is
    dropped."""
    if "compute_host" not in capabilities or _host_shape(capabilities["compute_host"]):
        return capabilities
    return {k: v for k, v in capabilities.items() if k != "compute_host"}


def image_refusal(capabilities: dict | None, sat_version: str, latest: str) -> dict | None:
    """Why a compute machine on another server that claims its image may
    not sign in, as the refusal's fields; None when it runs the platform's
    image tag and satellite. A new tag of its repository carries the doorbell
    (``update_image``, no tag: the host asks the platform); the same tag on another
    satellite, or another repository, carries none (the word would loop)."""
    claimed = str((capabilities or {}).get("compute_image") or "")
    if not claimed:
        return None
    wanted = wanted_image()
    if wanted is None:
        return {"reason": "This install names no image for its compute machines on other servers. "
                          "Set OTODOCK_REMOTE_COMPUTE_IMAGE."}
    parts = split_image(claimed)
    if parts is None or parts[0] != wanted.repository:
        return {"reason": f"This compute machine runs the image {claimed[:200]}, not the platform's "
                          f"{wanted.ref}. Pair it again from its card."}
    if parts[1] != wanted.tag:
        return {"reason": f"This compute machine runs the image tag {parts[1]}. The platform runs {wanted.tag}.",
                "action": "update_image"}
    if latest and sat_version != latest:
        return {"reason": f"This compute machine runs satellite {sat_version or 'unknown'} from the image "
                          f"{claimed}. The platform runs {latest}. Load the platform's image on that server "
                          "under a new tag."}
    return None


def link_address(scope) -> tuple[str, str, bool]:
    """The address a compute machine on another server came from, by the one
    rule every machine link follows (``deployment.link_schemes``): straight
    from it on an install served over plain HTTP on a LAN, through the
    trusted edge over https otherwise. ``(address, "", shared)``, or
    ``("", why, False)``; ``shared`` when other clients reach the platform
    from the same address (a hop, the container gateway): it is recorded and
    shown, but a lock cannot hold on it."""
    from auth import lan_check
    from core.config import deployment
    scope = scope or {}
    addr = lan_check.resolve(scope)
    if addr.internal:
        return "", "A compute machine does not link through the internal listener.", False
    forwarded = any(name in lan_check.EDGE_SIGNALS for name, _v in (scope.get("headers") or ()))
    if deployment.link_is_plain():
        if forwarded:
            return "", "A compute machine on another server links straight to this server's address.", False
        seen = addr.peer
    else:
        # The edge forwards the link's own web scheme (``link_schemes``:
        # https on an install that is not plain).
        if (not forwarded or addr.case or addr.client == addr.peer
                or lan_check.trusted_forwarded_proto(scope) != deployment.link_schemes()[0]):
            return "", "A compute machine on another server links through this install's https address.", False
        seen = addr.client
    seen = deployment.plain_address(seen or "")
    problem = deployment.compute_address_problem(seen)
    if problem:
        return "", f"A compute machine does not link from {seen or 'an unknown address'}. {problem}", False
    return seen, "", bool(addr.shared)


# The refused addresses per machine since its last notice, and that notice's time.
_refused: dict[str, tuple[float, list[str]]] = {}


async def address_refusal(machine: dict, seen: str, shared: bool = False) -> str:
    """After a compute machine on another server's secret verified: record
    the address it signed in from, or refuse it when its lock pins another
    one, or when it is locked and came from an address other clients share
    (every admin told, within the bound). ``""`` when admitted."""
    from services.remote import compute_bootstrap
    from storage import compute_store
    from storage.pg import run_db
    mid = str(machine.get("id") or "")
    noted = await run_db(compute_store.note_sign_in, mid, seen, refuse_locked=shared)
    if noted.admitted:
        if noted.changed:
            await run_db(compute_bootstrap.load_addresses)
        return ""
    compute_bootstrap.give_up(mid)
    await _tell_refused(machine, noted.address, seen, shared and noted.address == seen)
    if shared:
        return (f"This compute machine is locked to an address. It signed in from {seen}, which other clients "
                "share, so the lock cannot hold. An admin can turn its lock off on its card.")
    return (f"This compute machine is locked to {noted.address or 'an address'}. It tried to sign in from {seen}. "
            "An admin can turn its lock off on its card.")


def refused_from(machine_id: str) -> str:
    """The last address the lock refused for a machine since this process
    began, for its card; ``""`` for none."""
    held = _refused.get(machine_id)
    return held[1][-1] if held and held[1] else ""


def forget_refused(machine_id: str) -> None:
    _refused.pop(machine_id, None)


async def _tell_refused(machine: dict, locked: str, seen: str, shared: bool = False) -> None:
    mid = str(machine.get("id") or "")
    last, addresses = _refused.get(mid, (0.0, []))
    if seen not in addresses:
        addresses = (addresses + [seen])[-_REFUSED_KEPT:]
    now = time.monotonic()
    if last and now - last < NOTICE_EVERY_S:
        _refused[mid] = (last, addresses)
        return
    _refused[mid] = (now, [seen])
    others = [a for a in addresses if a != seen]
    tried = seen if not others else f"{seen}, and before that from {', '.join(others)}"
    await tell_admins(None, machine, "refused_shared" if shared else "refused", locked=locked, address=tried)


#: The admins' notice per event, filled with the admin who acted, the
#: machine's name and its addresses.
_NOTICES = {
    "added": ("Compute machine added: {name}",
              "{who} added {name}. Chats, terminals and tasks run there with the accounts of the people "
              "who use them. It signs in once its command runs on its server."),
    "pair_again": ("New pairing command: {name}",
                   "{who} made a new pairing command for {name}. Its sign-in changes when the command runs "
                   "on a server."),
    "paired": ("Compute machine paired: {name}", "{name} was paired from {address}."),
    "refused": ("Sign-in refused: {name}", "{name} is locked to {locked}. It tried to sign in from {address}."),
    "refused_shared": ("Sign-in refused: {name}", "{name} signed in from {address}, which other clients share, "
                                                   "so its lock cannot hold. Turn its lock off on its card."),
}


async def tell_admins(user, machine: dict, event: str, **words: str) -> None:
    """Every admin hears what happened to a compute machine on another
    server (``_NOTICES``): every chat, terminal and task that runs there runs
    with its people's accounts."""
    from services.notifications import notification_manager
    title, body = _NOTICES[event]
    fill = {"who": (getattr(user, "display_name", "") or getattr(user, "name", "") or "An admin"),
            "name": machine.get("name") or "A compute machine", "address": "", "locked": "", **words}
    try:
        await notification_manager.fire_notification(
            title=title.format(**fill), body=body.format(**fill),
            severity="warning", scope="admin", source="satellite",
            source_id=str(machine.get("id") or ""), href="/admin/remote-machines",
        )
    except Exception:
        logger.exception("Compute: the admins were not told about %s", machine.get("id"))


def install_command(public_host: str, pairing_token: str) -> str:
    """The one command an admin pastes on the server, as root: the bootstrap
    door every machine pairs through, its refusal running nothing (``-f``).
    The token reaches curl on its input (``printf`` is the shell's own), never
    in a command line another account of the server can read while the
    image downloads; an https install pins curl to https."""
    from core.config import deployment
    url = deployment.bootstrap_url(public_host)
    proto = "" if deployment.link_is_plain() else "--proto '=https' "
    return (f"printf 'X-Pairing-Token: %s\\n' '{pairing_token}' | "
            f'curl -fsSL {proto}-H @- "{url}?os=linux" | sudo bash')


def _value(name: str, value: str, grammar: re.Pattern) -> str:
    if not grammar.fullmatch(value):
        raise ValueError(f"{name} is not what the install script takes: {value[:80]!r}")
    return f"{name}={value}"


def bootstrap_script(machine_id: str, pairing_token: str, public_host: str, satellite_version: str) -> bytes:
    """The compute install script for one machine: a header of values, each
    checked against its grammar and carried base64 (the script decodes and
    checks them again), the host service and the AppArmor setup as payloads,
    then the install body. No name and no secret: the secret is made on the
    server."""
    from core.config import deployment
    scripts = Path(config.PLATFORM_ROOT) / "scripts"
    image = wanted_image()
    if image is None:
        raise ValueError("this install names no image for a compute machine on another server")
    web_scheme = deployment.link_schemes()[0]
    values = "\n".join([
        _value("MACHINE_ID", machine_id, _MACHINE_ID),
        _value("PAIRING_TOKEN", pairing_token, _TOKEN),
        _value("PLATFORM_URL", f"{web_scheme}://{public_host}", _WEB_URL),
        _value("IMAGE", image.ref, re.compile(r"[A-Za-z0-9./_:-]{3,300}")),
        _value("IMAGE_PUBLIC", "1" if image.public else "0", re.compile(r"[01]")),
        _value("SATELLITE_VERSION", satellite_version, _VERSION),
    ]) + "\n"

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    body = (scripts / INSTALL_SCRIPT).read_text(encoding="utf-8")
    if body.startswith("#!"):
        body = body.split("\n", 1)[1]
    header = (
        "#!/bin/bash\n"
        "# Made by OtoDock for one compute machine. Do not edit.\n"
        "set -euo pipefail\n"
        f"VALUES_B64='{b64(values.encode())}'\n"
        f"HOST_TOOL_B64='{b64((scripts / HOST_TOOL).read_bytes())}'\n"
        f"APPARMOR_SETUP_B64='{b64((scripts / APPARMOR_SETUP).read_bytes())}'\n"
        "# --- remote-compute-install.sh ---\n"
    )
    return header.encode("ascii") + body.encode("utf-8")
