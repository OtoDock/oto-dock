"""A Docker tool's container put to sleep and woken, under its tool's lock
(``state.locked``): the two compose runs the door and the idle sweep ask for.

A wake brings up only the tool's own service from the image already on this
host (no pull, no build: an update fetches images), applying a ``.env`` or a
compose config saved while it slept. A sleep stops only the tool's own
service, so a sidecar (the file tools' Collabora) never stops, and the
container, its writable layer and its logs stay. Blocking: the callers run
them in a thread.
"""

from __future__ import annotations

import logging
import subprocess

import config
from core.config import deployment
from services.containers import state
from services.mcp import docker_manager, mcp_manifest_types as _mt

logger = logging.getLogger("otodock.containers")

_SLEEP_TIMEOUT_S = 60
_WAKE_TIMEOUT_S = 120


def managed(manifest) -> bool:
    """Whether the platform drives this tool's container: every Docker tool
    but one an install in Docker runs as a platform service (no image of its
    own: the file tools there)."""
    if not _mt.is_container(getattr(manifest, "server", None)):
        return False
    return not (deployment.in_docker_compose() and not (getattr(manifest.server, "image", "") or ""))


def kept_running(name: str, keep_running: set[str] | None = None) -> bool:
    """An admin keeps the tool running (Keep running). Never on a hosted
    install, whatever is stored: every tool sleeps there."""
    if config.OTODOCK_CLOUD:
        return False
    if keep_running is None:
        from storage.mcp import mcp_store
        keep_running = mcp_store.get_keep_running()
    return name in keep_running


def runs_with_platform(manifest) -> bool:
    """One of the platform's own tools that runs while the platform runs
    (``server.starts: "with_platform"``; the manifest reader lowers that word
    for every other package)."""
    from services.mcp.mcp_manifest_blocks import STARTS_WITH_PLATFORM
    return getattr(manifest.server, "starts", "") == STARTS_WITH_PLATFORM


def stays_warm(manifest, keep_running: set[str] | None = None) -> bool:
    """A container that never sleeps: one of the platform's own tools that
    runs with it, a tool an admin keeps running, or one the platform does
    not drive."""
    if not managed(manifest) or runs_with_platform(manifest):
        return True
    return kept_running(manifest.name, keep_running)


def starts_at_boot(manifest) -> bool:
    """A boot starts a tool that stays warm, and one with no record of its
    tools for its container as it is (its first session reads it); every other
    sleeps until its first use."""
    from services.mcp import tool_listing
    if stays_warm(manifest):
        return True
    key = str(getattr(manifest, "server_name", "") or manifest.name)
    return tool_listing.saved_platform(key, tool_listing.container_fingerprint(manifest)) is None


@state.locked
def wake_container(manifest) -> bool:
    """Bring the tool's own service up. False when it did not start: why is
    the state's (``state.failed_start``, written here and by compose's own
    run)."""
    service = docker_manager._target_service(manifest)
    if service is None:
        state.failed_start(manifest.name, "Its own service cannot be named in its compose file.")
        return False
    try:
        env_changed = docker_manager._inject_mcp_env(manifest)
        # The drift check refreshes the tool's compose form itself.
        recreate = env_changed or docker_manager._compose_drifted(manifest)
    except ValueError as e:
        state.failed_start(manifest.name, str(e))
        return False
    args = ["up", "-d", "--no-build", "--pull", "never"]
    if recreate:
        args.append("--force-recreate")
    return docker_manager._compose_up(manifest, [*args, service], timeout=_WAKE_TIMEOUT_S, t1_retry=True)


@state.locked
def sleep_container(manifest) -> bool:
    """Stop the tool's own service. False when it cannot be named (a sidecar
    is never stopped in its place) or compose failed."""
    service = docker_manager._target_service(manifest)
    if service is None:
        logger.warning("Docker MCP %s: not put to sleep, its own service cannot be named", manifest.name)
        return False
    try:
        result = subprocess.run(
            docker_manager._compose_cmd(manifest, "stop", service),
            capture_output=True, text=True, timeout=_SLEEP_TIMEOUT_S,
            cwd=str(manifest.mcp_dir), env=docker_manager._compose_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Docker MCP %s: not put to sleep: %s", manifest.name, e)
        return False
    if result.returncode != 0:
        logger.warning("Docker MCP %s: not put to sleep: %s", manifest.name, result.stderr[:300])
        return False
    # Asleep, by the sweep or an admin's Stop: whatever it said it was
    # working on ended with it.
    state.of(manifest.name).held_since = 0.0
    logger.info("Docker MCP %s: asleep until its next use", manifest.name)
    return True
