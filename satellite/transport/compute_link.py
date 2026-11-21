"""What a compute machine's link takes from the platform, by its file mode.

A compute machine beside the platform (``kind = compute``) reads the
platform's folders itself, so no file, tool, login file or session move is
taken from the link. One on another host (``files = own``) keeps its own copy
of the folders, which the platform syncs as it does a paired machine's: it
takes the file and tool frames, the login file's rotation and the transcript
sweep, and still no session move (a chat whose engine record lives here
moves with its history). Either kind takes no code and no uninstall
(``ws_client``), and a gateway frame is the exit's to judge
(``credential_exit.takes_gateway_frame``).

The install's own machine may keep the community tools itself (``tools =
own``): it then takes the two tool frames and still no file, login file or
session move, and it claims the mode at its sign-in.

A machine on another server started by its host service (the host's shape
in ``OTODOCK_COMPUTE_HOST_SHAPE``) rings that service for two of the
platform's refusals: a new image tag (exit 76) and its removal (exit 77). The
exit code is a doorbell and carries nothing: the service asks the platform
itself which tag to run and whether this machine was removed, so nothing in
this container can make it change the tag or remove the machine.
"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger("satellite.compute-link")


class ComputeSignInRefused(ConnectionError):
    """A compute machine's sign-in was refused for good: its secret did not
    verify, its row is gone, or the platform named a new image tag. The
    process ends with ``exit_code``; only a refused secret leaves the mark
    the start waits on (``mark``)."""

    def __init__(self, message: str, *, exit_code: int = 0, mark: bool = True):
        super().__init__(message)
        self.exit_code = exit_code or COMPUTE_REREAD_EXIT
        self.mark = mark


#: The platform's reason when a machine's secret does not verify (``ws/satellite.py``).
SECRET_REFUSED = "Invalid credentials"
#: The exit code a compute machine ends with to read its sign-in again: not
#: 0, so the bare-metal unit's ``Restart=on-failure`` starts it again too.
COMPUTE_REREAD_EXIT = 75
#: The exit codes a machine on another server ends with for its host service,
#: which then asks the platform: a new image tag; this machine's removal.
UPDATE_EXIT, REMOVED_EXIT = 76, 77
#: What its host service tells it: the image it runs, the host's shape (the
#: number its options and mounts carry; the platform says when it is behind).
IMAGE_ENV, HOST_SHAPE_ENV = "OTODOCK_COMPUTE_IMAGE", "OTODOCK_COMPUTE_HOST_SHAPE"

#: The file a machine on another host leaves in its state folder when the
#: platform refused its sign-in (a new one was made, or it was removed): the
#: sign-in's mark, so its start (``proxy/compute_start.py``) waits for a new
#: sign-in instead of restarting into the same refusal.
REFUSED_FILE = "compute-refused"

#: The frames a compute machine beside the platform refuses.
SHARED_REFUSES: frozenset[str] = frozenset({
    "sync_mcps", "sync_mcps_verify", "file_push", "file_pull", "file_stat",
    "request_manifest", "session_files_retention", "session_move",
    "credentials_update",
})
#: The frames a compute machine with its own copy refuses.
OWN_COPY_REFUSES: frozenset[str] = frozenset({"session_move"})
#: The frames a machine beside the platform takes when it keeps the
#: community tools itself.
TOOL_FRAMES: frozenset[str] = frozenset({"sync_mcps", "sync_mcps_verify"})
#: What such a machine claims at its sign-in (``compute_tools``).
TOOLS_OWN = "own"


def refuses(config, msg_type: str) -> bool:
    """Whether this compute machine refuses ``msg_type`` by its file mode
    and, beside the platform, by whether it keeps the community tools."""
    if getattr(config, "own_copy", False):
        return msg_type in OWN_COPY_REFUSES
    if getattr(config, "own_tools", False):
        return msg_type in SHARED_REFUSES - TOOL_FRAMES
    return msg_type in SHARED_REFUSES


def files_claim(config) -> str:
    """The ``compute_files`` capability: the platform admits the link only
    when it names the mode the machine's row was approved for."""
    return "own" if getattr(config, "own_copy", False) else "shared"


def sign_in_mark(machine_id: str, machine_secret: str) -> str:
    """A sign-in's mark: the start computes the same from its credential."""
    return hashlib.sha256(f"{machine_id}\n{machine_secret}".encode()).hexdigest()


def note_refused_sign_in(config) -> None:
    """Leave the refused sign-in's mark for the start, on a machine with its
    own copy (one beside the platform reads the credential the proxy writes
    again). Best effort: without it the start retries as before."""
    if not getattr(config, "own_copy", False):
        return
    from satellite.sessions import sandbox_host
    path = sandbox_host.otodock_dir() / REFUSED_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{REFUSED_FILE}.tmp")
        tmp.write_text(sign_in_mark(config.machine_id, config.machine_secret) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("compute mode: the refused sign-in was not noted: %s", e)


def host_shape(config) -> int:
    """The shape of the host service that started this machine, 0 when no
    host service did (the install's own, or one started by hand)."""
    word = (os.environ.get(HOST_SHAPE_ENV) or "").strip()
    if not getattr(config, "own_copy", False) or not word.isdigit() or len(word) > 4:
        return 0
    return int(word)


def claims(config) -> dict:
    """The claims a compute machine signs in with: its kind, its file mode
    (the platform admits the link only for its row's), that it keeps the
    community tools itself (the platform then sends it the tool frames) and,
    on another server started by its host service, the image it runs and the
    host's shape."""
    out: dict = {"compute": True, "compute_files": files_claim(config)}
    if getattr(config, "own_tools", False):
        out["compute_tools"] = TOOLS_OWN
    image = (os.environ.get(IMAGE_ENV) or "").strip()
    if getattr(config, "own_copy", False) and image:
        out["compute_image"] = image
    shape = host_shape(config)
    if shape:
        out["compute_host"] = shape
    return out


def refusal(config, resp: dict) -> ComputeSignInRefused | None:
    """A compute machine's answer to a refused sign-in, ``None`` to sign in
    again later (a lock, a version it cannot follow). On another server
    started by its host service: ring it for a new image tag (76) or a
    removed row (77), which it confirms with the platform; elsewhere a
    removed row ends the process to read its sign-in again, as a refused
    secret does (75)."""
    action, hosted = resp.get("action"), host_shape(config) > 0
    if action == "update_image":
        if not hosted:
            logger.error("compute mode: the platform runs another image, which this machine cannot follow: %s",
                         resp.get("reason"))
            return None
        return ComputeSignInRefused("the platform runs another image tag", exit_code=UPDATE_EXIT, mark=False)
    if action == "uninstall":
        if hosted:
            return ComputeSignInRefused("the platform no longer has this machine", exit_code=REMOVED_EXIT,
                                        mark=False)
        return ComputeSignInRefused(f"Auth rejected: {resp}")
    if resp.get("reason") == SECRET_REFUSED:
        return ComputeSignInRefused(f"Auth rejected: {resp}")
    return None


def ended(config, refused: ComputeSignInRefused) -> int:
    """The exit code a refused compute machine ends with, its mark left
    for a refused secret (its start waits for a new sign-in)."""
    log = logger.error if refused.exit_code == COMPUTE_REREAD_EXIT else logger.info
    log("compute mode: %s: ending with %d", refused, refused.exit_code)
    if refused.mark:
        note_refused_sign_in(config)
    return refused.exit_code


def not_dumpable() -> bool:
    """A machine on another server takes its own process out of reach of the
    server's other processes of its user id through ``/proc`` (its memory
    holds the machine's secret), as ``prctl(PR_SET_DUMPABLE, 0)`` does. True
    when set; a session's processes start dumpable again at their exec."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise OSError(ctypes.get_errno(), "prctl")
    except (OSError, AttributeError) as e:
        logger.warning("compute mode: this process could not take itself out of reach of /proc: %s", e)
        return False
    return True
