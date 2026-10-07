"""The systemd user unit the Linux installer writes, and whether it is ours.

One OS user can run more than one satellite (a second install, a rig started
from a checkout under another HOME), but ``systemctl --user`` always reaches
the user's one manager, where the unit name ``oto-dock-satellite`` belongs to
whichever install wrote it. Anything that acts on the unit asks first whether
its ``WorkingDirectory`` is this install's ``satellite/`` dir.
"""
import contextlib
import logging
import os
import subprocess
from pathlib import Path

from .. import config

logger = logging.getLogger("satellite")

UNIT_NAME = "oto-dock-satellite"
OOM_DROPIN_NAME = "oom-policy.conf"
# One child the kernel's OOM killer ends (a headless browser) must not stop
# the whole unit and every session it parents (systemd's default is "stop").
OOM_DROPIN_TEXT = "[Service]\nOOMPolicy=continue\n"


def systemctl_env() -> dict[str, str]:
    """The environment ``systemctl --user`` needs from a detached context."""
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    }


def _show(prop: str) -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", prop, "--value", UNIT_NAME],
            env=systemctl_env(), timeout=15, capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def install_dir() -> Path:
    """This install's ``satellite/`` dir, the unit's working directory."""
    return config.otodock_dir() / "satellite"


def is_own_unit() -> bool:
    """True when the user manager's ``oto-dock-satellite`` unit runs THIS
    install. False off systemd, when the unit is absent, or when it belongs
    to another install of the same OS user."""
    if config.HOST.service_manager != config.SERVICE_SYSTEMD:
        return False
    wd = _show("WorkingDirectory")
    if not wd:
        return False
    try:
        return Path(wd).resolve() == install_dir().resolve()
    except OSError:
        return False


def ensure_oom_policy() -> bool:
    """Give an existing install's unit ``OOMPolicy=continue`` through a
    drop-in (an install written before the installer carried the line), then
    reload the user manager. Only for this install's own unit; returns True
    when a drop-in was written. Best-effort: a failure logs and changes
    nothing else."""
    if not is_own_unit():
        return False
    if _show("OOMPolicy") == "continue":
        return False
    dropin = Path.home() / ".config/systemd/user" / f"{UNIT_NAME}.service.d" / OOM_DROPIN_NAME
    try:
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(OOM_DROPIN_TEXT, encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write the unit's OOM policy drop-in (%s): %s", dropin, e)
        return False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            env=systemctl_env(), timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    logger.info("Set OOMPolicy=continue on the %s unit (%s)", UNIT_NAME, dropin)
    return True
