"""Docker container lifecycle management for Docker-based MCPs.

Provides start/stop/restart/status operations for MCP containers
defined via docker-compose.yml in their manifest.
"""

import contextlib
import logging
import os
import re
import subprocess
import threading
import time

import config
from core.config import deployment

logger = logging.getLogger("claude-proxy.docker-manager")

# First boot pulls multi-GB images (e.g. Collabora for file-tools) — surface
# that instead of sitting silent for minutes. Lines matching this get logged
# (throttled) while a compose subprocess streams.
_PROGRESS_RE = re.compile(
    r"\b(Pulling|Pulled|Pull complete|Downloading|Download complete|"
    r"Extracting|Building|Built|Waiting)\b",
    re.IGNORECASE,
)

_DOCKER_PERM_HINT = (
    "cannot access the Docker daemon socket (permission denied). The user "
    "running the proxy is not in the 'docker' group — after dev-setup.sh adds "
    "you, log out and back in (or `newgrp docker`), then restart the proxy. "
    "Until then the proxy runs WITHOUT its Docker MCPs (file-tools etc.)."
)
_docker_perm_logged = False


def _is_docker_perm_denied(stderr: str) -> bool:
    low = (stderr or "").lower()
    return "permission denied" in low and (
        "docker.sock" in low or "docker daemon socket" in low
    )


def _log_docker_perm_denied(context: str) -> None:
    """ERROR once per boot — this used to be an easy-to-miss warning while the
    proxy silently booted without its Docker MCPs."""
    global _docker_perm_logged
    if _docker_perm_logged:
        return
    _docker_perm_logged = True
    logger.error("%s: %s", context, _DOCKER_PERM_HINT)


def _run_compose_streaming(
    manifest, cmd: list[str], *, timeout: int, cwd: str | None = None,
) -> tuple[int, str]:
    """Run a compose command, streaming progress lines into the log.

    Replaces ``subprocess.run(capture_output=True)`` for the long compose ops:
    that swallowed pull/build progress entirely, so a fresh install read as a
    hang. stderr (compose writes progress AND errors there) is buffered in
    full for the callers' overlap/permission checks; progress-shaped lines log
    at INFO throttled to ~one per 5s. A watchdog kills the process at
    ``timeout`` and ``subprocess.TimeoutExpired`` is raised, matching the old
    ``subprocess.run`` contract. stdout is discarded — no caller of the long
    ops reads it.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, cwd=cwd, env=_compose_env(),
    )
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        with contextlib.suppress(OSError):
            proc.kill()

    watchdog = threading.Timer(timeout, _kill)
    watchdog.daemon = True
    watchdog.start()
    lines: list[str] = []
    last_log = 0.0
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.append(line)
            stripped = line.strip()
            if stripped and _PROGRESS_RE.search(stripped):
                now = time.monotonic()
                if now - last_log >= 5.0:
                    logger.info("%s: %s", manifest.name, stripped[:200])
                    last_log = now
        proc.wait()
    finally:
        watchdog.cancel()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    return proc.returncode, "".join(lines)


def _project_name(manifest) -> str:
    """Namespaced compose project for a Docker MCP — ``otodock-<install_id>-mcp-
    <name>``. The install-id keeps it unique vs the operator's OWN compose
    projects AND vs a second OtoDock install on the same daemon; without it
    Compose derives the project from the folder name (a bare ``camoufox``), which
    collides. Used for every compose op so start/stop/restart/down/ps all target
    the same containers.
    """
    return f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}".lower()


def _compose_cmd(manifest, *args: str) -> list[str]:
    """Build docker compose command for a manifest's compose file under the
    namespaced project (see ``_project_name``).

    On bare-metal (T1) a generated ``docker-compose.override.yml`` (subnet pin +
    namespaced container_name + image tag — see
    ``compose_rewrite.ensure_t1_override``) is merged in with a second ``-f`` when
    present, so every op (up/down/ps/pull/restart) sees the same effective config.
    """
    compose_file = manifest.mcp_dir / manifest.server.docker_compose
    cmd = [
        "docker", "compose", "-p", _project_name(manifest), "-f", str(compose_file),
    ]
    override = manifest.mcp_dir / "docker-compose.override.yml"
    if override.is_file():
        cmd += ["-f", str(override)]
    return [*cmd, *args]


def _compose_env() -> dict[str, str]:
    """Environment for ``docker compose`` subprocesses.

    Bare-metal (T1): the current environment unchanged — ``docker compose``
    talks to the host's local ``/var/run/docker.sock``. Docker-Compose (T2):
    the same environment plus ``DOCKER_HOST=tcp://docker-socket-proxy:2375`` so
    the containerised proxy drives the daemon through the read-restricted
    socket-proxy (it has no local socket). ``deployment.docker_subprocess_env``
    returns ``{}`` on bare-metal, so this is a no-op there.
    """
    return {**os.environ, **deployment.docker_subprocess_env()}


def get_container_status(manifest) -> str:
    """Check Docker container status.

    Returns: 'running', 'unhealthy', 'starting', 'stopped', 'not_found', 'error'.
    A container that is up but failing its healthcheck reports 'unhealthy' (e.g. a
    wedged camoufox whose HTTP is up but `page.goto` hangs) so the dashboard shows
    it amber instead of green — the proxy never auto-restarts Docker MCPs.
    """
    try:
        result = subprocess.run(
            _compose_cmd(manifest, "ps", "--format", "json", "-a"),
            capture_output=True, text=True, timeout=15,
            env=_compose_env(),
        )
        if result.returncode != 0:
            if _is_docker_perm_denied(result.stderr):
                _log_docker_perm_denied(
                    f"Docker status check for {manifest.name} failed"
                )
            return "not_found"

        output = result.stdout.strip()
        if not output:
            return "not_found"

        # docker compose ps --format json outputs one JSON object per line
        import json
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                container = json.loads(line)
                state = container.get("State", "").lower()
                if state == "running":
                    # `Health` is "" when the image declares no healthcheck.
                    health = (container.get("Health") or "").lower()
                    if health == "unhealthy":
                        return "unhealthy"
                    if health == "starting":
                        return "starting"
                    return "running"
                elif state in ("exited", "dead", "created"):
                    return "stopped"
            except json.JSONDecodeError:
                continue

        return "not_found"
    except subprocess.TimeoutExpired:
        logger.warning("Timeout checking Docker status for %s", manifest.name)
        return "error"
    except Exception as e:
        logger.warning("Error checking Docker status for %s: %s", manifest.name, e)
        return "error"


def _inject_mcp_env(manifest) -> bool:
    """Generate Docker MCP `.env` from manifest declarations + admin secrets.

    Returns True if the .env file content changed (or was created/deleted),
    False if no change. Callers can use this to force-recreate the container
    so it picks up the new env (compose otherwise leaves a running container
    untouched on `up -d` when only env values change).

    Three sources merged in priority order (latest wins):
      1. Existing non-declared keys in `.env` (escape hatch for compose-time
         one-offs — keys not declared anywhere else stay put).
      2. ``manifest.env`` + ``manifest.agent_env`` resolved via the shared
         template resolver (``${platform.*}`` tokens, etc.).
      3. ``infra_credentials`` rows for this MCP — admin-set secrets like
         ESPOCRM_API_KEY land here so Docker containers receive them at
         startup without hand-editing ``.env``.

    Called on every proxy startup AND every container start (and on install/
    update via the API). Idempotent.
    """
    from services.mcp import mcp_registry
    from storage import credential_store

    env_path = manifest.mcp_dir / ".env"

    # Preserve any existing non-declared keys (escape hatch).
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, v = stripped.split("=", 1)
                existing[k] = v

    # Resolve manifest-declared values via the shared template resolver. We
    # merge `env` and `agent_env` — for Docker MCPs, both end up in `.env`
    # because containers don't have per-session context (they serve many
    # sessions; per-call session_id flows via HTTP query string instead).
    # Secret template tokens (_SECRET_TEMPLATE_TOKENS — empty today) must NEVER
    # be baked into a Docker MCP's .env: that file lives in the container's
    # context and would leak the secret to a (possibly untrusted) container.
    # The per-session credential broker is the only path for secrets — Docker
    # callbacks authenticate with the per-session JWT, not a baked secret.
    def _safe_resolve(key: str, raw: str) -> str | None:
        if any(tok in str(raw) for tok in mcp_registry._SECRET_TEMPLATE_TOKENS):
            logger.warning(
                "Refusing to write secret template %s into Docker .env for %s",
                key, manifest.name,
            )
            return None
        return mcp_registry._resolve_template(raw, manifest)

    declared: dict[str, str] = {}
    for k, v in (manifest.env or {}).items():
        resolved = _safe_resolve(k, v)
        if resolved is not None:
            declared[k] = resolved
    for k, v in (manifest.agent_env or {}).items():
        resolved = _safe_resolve(k, v)
        if resolved is not None:
            declared[k] = resolved

    # Admin-set infra credentials (Docker MCPs only — stdio MCPs get these
    # via env_builder per session). The credential keys are declared in
    # ``manifest.credentials.fields`` so the admin UI knows what to ask for;
    # at startup we pull the saved values out of the encrypted store.
    infra_creds: dict[str, str] = {}
    if manifest.credentials.type == "infra":
        try:
            infra_creds = credential_store.get_infra_credentials(manifest.name) or {}
        except Exception as e:
            logger.warning(
                "Failed to load infra credentials for %s: %s",
                manifest.name, e,
            )

    # Priority order: existing < manifest-declared < infra credentials.
    # Admin secrets win over declarations (so an ESPOCRM_URL admin set in the
    # dashboard overrides any default the manifest happens to ship).
    final = {**existing, **declared, **infra_creds}

    # Generic tool filter: for MCPs that declare
    # `tool_filter` AND have an admin-set regex in `mcp_state`, write the
    # composed flag (e.g. `--enabled-tools '^(mail|calendar)_.*'`) into
    # the env var the manifest names (default ENABLED_TOOLS_FLAG). The
    # MCP's Dockerfile ENTRYPOINT expands it into the binary's CLI args.
    # Clear the env var when no filter — otherwise stale flags would
    # outlive the regex being cleared.
    tf = mcp_registry.get_tool_filter(manifest.name)
    if tf is not None and manifest.tool_filter is not None:
        arg_name, regex = tf
        # Single-quote the regex so shell expansion in the ENTRYPOINT
        # doesn't choke on special characters (^ $ | etc.).
        final[manifest.tool_filter.env_var_name] = f"{arg_name} '{regex}'"
    elif manifest.tool_filter is not None:
        # Manifest supports filtering but admin hasn't set a regex — make
        # sure the env var is empty (so the ENTRYPOINT's expansion is a
        # no-op rather than stale flags from a previous run).
        final[manifest.tool_filter.env_var_name] = ""

    if not final:
        # No declarations + no preserved keys: don't litter the MCP folder with
        # an empty .env (community MCPs that need no platform vars stay clean).
        if env_path.exists():
            env_path.unlink()
            logger.debug("No env declared for %s — .env removed", manifest.name)
            return True
        logger.debug("No env declared for %s — .env not created", manifest.name)
        return False

    new_content = "\n".join(f"{k}={v}" for k, v in final.items()) + "\n"
    old_content = env_path.read_text() if env_path.exists() else None
    if old_content == new_content:
        return False
    env_path.write_text(new_content)
    logger.debug(
        "Wrote %d declared + %d infra keys + preserved %d existing keys to %s/.env",
        len(declared),
        len(infra_creds),
        len(set(existing) - set(declared) - set(infra_creds)),
        manifest.name,
    )
    return True


def start_container(manifest, *, force_recreate: bool = False) -> bool:
    """Start Docker container via docker compose up -d.

    When ``force_recreate`` is True, passes ``--force-recreate`` so a running
    container is rebuilt with the current ``.env``. Compose's plain ``up -d``
    leaves a running container untouched when only env values change, so
    callers that just wrote a new ``.env`` should pass force_recreate=True.

    The image is actively refreshed before (re)creating — a plain ``up -d``
    is not enough after an update: it only fetches/builds a MISSING image, so
    a same-tag rebuild or a moved tag would keep the stale local image.
    Topology-aware:
      * T2 (pull-form compose): ``docker compose pull`` the (possibly retagged)
        ``server.image`` first.
      * T1 (build-form compose): pull the canonical prebuilt image, falling
        back to ``--build`` from the Dockerfile when the image is absent or
        the dev build-local flag is set (only the bare-metal local daemon can
        build; the T2 socket-proxy blocks it).
    """
    try:
        _inject_mcp_env(manifest)
        from services.mcp import compose_rewrite
        in_t2 = deployment.in_docker_compose()

        if in_t2:
            # T2: rewrite the catalog's build-from-context compose to pull the
            # pre-built image (the socket-proxy blocks builds). Raises with an
            # actionable message if the MCP has no `server.image` to pull — fail
            # rather than let `compose up` attempt a build the socket-proxy 403s.
            try:
                compose_rewrite.ensure_pull_compose(manifest)
            except ValueError as e:
                logger.error("Cannot start Docker MCP %s: %s", manifest.name, e)
                return False
        else:
            # T1: generate/refresh the override (subnet pin + namespaced
            # container_name + image tag) BEFORE any compose op, so the bridge is
            # pinned to our pool and can never auto-grab a 192.168.x LAN overlap.
            # Best-effort: a parse failure just degrades to docker's auto-bridge.
            compose_rewrite.ensure_t1_override(manifest)

        args = ["up", "-d"]
        if force_recreate:
            args.append("--force-recreate")

        # ---- T2: always pull (build blocked), then up (fast). ----
        if in_t2:
            pull_rc, pull_err = _run_compose_streaming(
                manifest, _compose_cmd(manifest, "pull"),
                timeout=900, cwd=str(manifest.mcp_dir),
            )
            if pull_rc != 0:
                logger.warning(
                    "compose pull for %s failed (cached image if present): %s",
                    manifest.name, pull_err[:300],
                )
            return _compose_up(manifest, args, timeout=120)

        # ---- T1: prefer the canonical prebuilt image; build is the fallback. ----
        srv_image = getattr(manifest.server, "image", "") or ""
        build_local = config.OTODOCK_MCP_BUILD_LOCAL or not srv_image
        if build_local:
            # No published image (or the dev build-local flag) → build from the
            # Dockerfile. A build can take minutes — widen the timeout.
            args.append("--build")
            return _compose_up(manifest, args, timeout=900, t1_retry=True)

        # Pull the canonical image; if it isn't present afterwards (unpublished
        # or a failed pull), self-heal by building locally (the base keeps
        # `build:`). An update re-pulls a moved same-tag digest for free.
        pull_rc, pull_err = _run_compose_streaming(
            manifest, _compose_cmd(manifest, "pull"),
            timeout=900, cwd=str(manifest.mcp_dir),
        )
        if pull_rc != 0:
            logger.warning("compose pull for %s failed: %s", manifest.name, pull_err[:300])
        present = subprocess.run(
            ["docker", "image", "inspect", srv_image],
            capture_output=True, text=True, timeout=30, env=_compose_env(),
        ).returncode == 0
        if not present:
            logger.info(
                "%s: image %s absent after pull — building locally", manifest.name, srv_image,
            )
            args.append("--build")
        return _compose_up(
            manifest, args, timeout=900 if "--build" in args else 120, t1_retry=True,
        )
    except Exception as e:
        logger.error("Error starting Docker MCP %s: %s", manifest.name, e)
        return False


def _compose_up(manifest, args: list[str], *, timeout: int, t1_retry: bool = False) -> bool:
    """Run ``compose up`` (with the given args), logging the outcome.

    ``t1_retry`` enables the T1 subnet TOCTOU recovery: if the create fails
    because another stack grabbed our pinned /24 between pick-time and
    create-time (stderr mentions "overlap"), reallocate a fresh free /24 into the
    override and retry once. Still failing → return False (the admin sees it).
    """
    logger.info(
        "%s: compose up%s (first boot may pull/build images — can take minutes "
        "on slow links; progress is logged)",
        manifest.name, " --build" if "--build" in args else "",
    )
    rc, stderr = _run_compose_streaming(
        manifest, _compose_cmd(manifest, *args),
        timeout=timeout, cwd=str(manifest.mcp_dir),
    )
    if rc == 0:
        logger.info(
            "Started Docker MCP: %s%s",
            manifest.name, " (force-recreated)" if "--force-recreate" in args else "",
        )
        return True

    if t1_retry and "overlap" in stderr.lower():
        logger.warning(
            "%s: subnet overlap on network create — reallocating + retrying: %s",
            manifest.name, stderr[:200],
        )
        from services.mcp import compose_rewrite
        compose_rewrite.ensure_t1_override(manifest, force_realloc=True)
        rc, retry_err = _run_compose_streaming(
            manifest, _compose_cmd(manifest, *args),
            timeout=timeout, cwd=str(manifest.mcp_dir),
        )
        if rc == 0:
            logger.info("Started Docker MCP %s after subnet reallocation", manifest.name)
            return True
        stderr = retry_err

    # Name-conflict self-heal: a container holding OUR exact target name is
    # by definition ours (the name embeds this install's id — see
    # _project_name), left behind by a previous deployment generation that
    # compose can no longer see (project labels changed). Remove it and retry
    # once. Containers of a DIFFERENT install-id are never touched — a second
    # OtoDock install on the same daemon legitimately owns those.
    expected_name = f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}".lower()
    if "already in use" in stderr.lower() and expected_name in stderr.lower():
        logger.warning(
            "%s: container name %s is held by a previous deployment "
            "generation — removing it and retrying",
            manifest.name, expected_name,
        )
        try:
            rm = subprocess.run(
                ["docker", "rm", "-f", expected_name],
                capture_output=True, text=True, timeout=60,
                env=deployment.docker_subprocess_env(),
            )
        except Exception as e:
            rm = None
            logger.warning("%s: stale-container removal failed: %s", manifest.name, e)
        if rm is not None and rm.returncode == 0:
            rc, retry_err = _run_compose_streaming(
                manifest, _compose_cmd(manifest, *args),
                timeout=timeout, cwd=str(manifest.mcp_dir),
            )
            if rc == 0:
                logger.info(
                    "Started Docker MCP %s after removing the previous "
                    "generation's container", manifest.name,
                )
                return True
            stderr = retry_err
        elif rm is not None:
            logger.warning(
                "%s: could not remove stale container %s: %s",
                manifest.name, expected_name, (rm.stderr or "")[:200],
            )

    if _is_docker_perm_denied(stderr):
        _log_docker_perm_denied(f"compose up for {manifest.name} failed")
    logger.error("Failed to start Docker MCP %s: %s", manifest.name, stderr[:500])
    return False


def _warn_foreign_mcp_leftovers() -> None:
    """Log MCP containers whose name carries a DIFFERENT install identity.

    Container/project names embed the install-id (``otodock-<id>-mcp-<name>``,
    see ``_project_name``). The id lives in config.env, so a recreated
    config.env (install moved to a new folder) rotates it — the previous
    generation's containers keep running with stale config, invisible to
    every compose op here. They must NOT be auto-removed: a second OtoDock
    install on the same daemon legitimately owns identically-shaped names.
    Name them loudly with the removal command and let the operator decide.
    Best-effort — any failure is silent (T3 has no daemon at all).
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
            env=deployment.docker_subprocess_env(),
        )
        if result.returncode != 0:
            return
        ours = f"otodock-{config.INSTALL_ID}-mcp-"
        foreign = re.compile(r"^otodock-(?:[0-9a-f]+-)?mcp-")
        stale = [
            n for n in result.stdout.split()
            if foreign.match(n) and not n.startswith(ours)
        ]
        if stale:
            logger.warning(
                "%d MCP container(s) carry another OtoDock install identity: "
                "%s. If this daemon hosts ONLY this install, they are "
                "leftovers from a previous config.env (rotated install-id) "
                "still running with stale config — remove them with: "
                "docker rm -f %s",
                len(stale), ", ".join(stale), " ".join(stale),
            )
    except Exception as e:
        logger.debug("foreign-MCP-leftover scan skipped: %s", e)


def stop_container(manifest) -> bool:
    """Stop Docker container via docker compose down."""
    try:
        result = subprocess.run(
            _compose_cmd(manifest, "down"),
            capture_output=True, text=True, timeout=60,
            cwd=str(manifest.mcp_dir),
            env=_compose_env(),
        )
        if result.returncode == 0:
            logger.info("Stopped Docker MCP: %s", manifest.name)
            return True
        logger.error(
            "Failed to stop Docker MCP %s: %s", manifest.name, result.stderr[:500]
        )
        return False
    except Exception as e:
        logger.error("Error stopping Docker MCP %s: %s", manifest.name, e)
        return False


def remove_image(image: str) -> bool:
    """Best-effort ``docker rmi`` of an image tag — opportunistic disk hygiene
    after a delete (reclaim the MCP's image) or an update (reclaim the previous
    tag the new one replaced, so the daemon doesn't accumulate one stale image
    per update). Never raises: the image may be shared, still in use, or already
    absent, none of which should fail the caller.
    """
    if not image:
        return False
    try:
        r = subprocess.run(
            ["docker", "rmi", image],
            capture_output=True, text=True, timeout=60, env=_compose_env(),
        )
        if r.returncode == 0:
            logger.info("Removed image %s", image)
            return True
        logger.info(
            "Image %s not removed (shared/in-use/absent): %s",
            image, r.stderr.strip()[:200],
        )
        return False
    except Exception as e:
        logger.info("Best-effort rmi of %s skipped: %s", image, e)
        return False


def remove_container(manifest, *, prune_image: bool = True) -> bool:
    """Tear a Docker MCP down completely — used when an MCP is DELETED (not just
    stopped). ``compose down --volumes --remove-orphans`` stops and removes the
    container, its per-MCP named volumes (the rewritten T2 compose mounts data
    in ``otodock-mcp-<name>-*`` volumes) and any orphaned siblings; then a
    best-effort ``docker rmi`` of ``server.image`` reclaims the image layer.

    Self-host only (T1 bare-metal + T2 socket-proxy). On cloud (external-pool)
    there is no per-install container, so ``delete_mcp`` never calls this.

    Returns True when the ``down`` succeeds. Image removal is best-effort and
    never fails the call — the image may be shared with another MCP or still in
    use, and reclaiming the container + volumes is what "delete" must guarantee.
    """
    try:
        # ``--rmi local`` reclaims an image-less MCP's locally-built image (tagged
        # ``<project>-<service>``, which the separate ``remove_image`` below can't
        # target). It does NOT touch a custom-tagged image (``server.image``) —
        # that's what ``remove_image`` reclaims — so the two together cover both
        # the T1 build-fallback and the pull/T2 cases.
        result = subprocess.run(
            _compose_cmd(manifest, "down", "--rmi", "local", "--volumes", "--remove-orphans"),
            capture_output=True, text=True, timeout=300,
            cwd=str(manifest.mcp_dir),
            env=_compose_env(),
        )
        ok = result.returncode == 0
        if ok:
            logger.info("Removed Docker MCP container + volumes: %s", manifest.name)
        else:
            logger.warning(
                "compose down for %s exited %s: %s",
                manifest.name, result.returncode, result.stderr[:500],
            )

        if prune_image:
            image = getattr(getattr(manifest, "server", None), "image", "") or ""
            remove_image(image)
        return ok
    except Exception as e:
        logger.error("Error removing Docker MCP %s: %s", manifest.name, e)
        return False


def restart_container(manifest) -> bool:
    """Restart Docker container via docker compose restart."""
    try:
        result = subprocess.run(
            _compose_cmd(manifest, "restart"),
            capture_output=True, text=True, timeout=120,
            cwd=str(manifest.mcp_dir),
            env=_compose_env(),
        )
        if result.returncode == 0:
            logger.info("Restarted Docker MCP: %s", manifest.name)
            return True
        logger.error(
            "Failed to restart Docker MCP %s: %s", manifest.name, result.stderr[:500]
        )
        return False
    except Exception as e:
        logger.error("Error restarting Docker MCP %s: %s", manifest.name, e)
        return False


def startup_docker_mcps() -> None:
    """Called on proxy startup. Start all enabled Docker MCPs that aren't running.

    Always re-injects the platform `.env` (PROXY_URL, HOST_AGENTS_DIR,
    WOPI_BASE_URL, COLLABORA_FRAME_ANCESTORS, COLLABORA_SERVICE_ROOT) so
    containers see the current platform config. Note: NO master key — Docker
    MCPs that call back authenticate with a per-session JWT (Authorization
    header injected per session via server.proxy_callbacks), not PROXY_API_KEY.
    When the `.env` content actually changed, the affected container is
    force-recreated so it picks up the new values — without this, plain
    `up -d` would leave a running container untouched and env-only changes
    (Collabora deployment shape, WOPI_BASE_URL, infra credentials, etc.)
    would silently fail until manual recreate.
    """
    from services.mcp import mcp_registry
    from storage import mcp_store

    # Every topology with a local daemon: name (but never touch) MCP
    # containers left behind by a previous install identity.
    _warn_foreign_mcp_leftovers()

    # T2 (Docker-Compose): Docker MCPs are NOT started from their per-MCP
    # build-context compose files here. The core file-tools runs as an
    # operator-managed compose *sibling* (brought up by the platform compose,
    # reached by service-DNS), and community Docker MCPs are managed via the
    # pull-based socket-proxy path — never `docker compose build`,
    # which the socket-proxy blocks by design. Starting them from here would
    # both fail (403 on build) and fight the operator over the sibling's
    # lifecycle. Discovery (scan_manifests) + service-DNS routing already
    # happened; nothing to start. No-op on bare-metal (T1), where the proxy
    # owns the local-daemon lifecycle exactly as before.
    # Only bare-metal T1 (managed-local) drives per-MCP container lifecycle from
    # here. T2 Docker-Compose: community Docker MCPs are pull-managed siblings
    # (started at install via the socket-proxy); starting them here would
    # 403 on build and fight the operator. T3 cloud (external-pool): there is no
    # local daemon — Docker MCPs are connections to the OtoDock-hosted central
    # pool, never started locally. Both skip; only T1 proceeds.
    if deployment.current_mode() != deployment.MANAGED_LOCAL:
        logger.info(
            "%s: skipping per-MCP docker startup — Docker MCPs are not "
            "locally-managed in this topology",
            deployment.current_mode(),
        )
        return

    states = mcp_store.get_all_mcp_states()
    manifests = mcp_registry.get_all_manifests()

    for name, manifest in manifests.items():
        if manifest.server.runtime != "docker":
            continue
        if not states.get(name, False):
            continue  # not enabled

        env_changed = False
        try:
            env_changed = _inject_mcp_env(manifest)
        except Exception as e:
            logger.warning("Failed to refresh .env for %s: %s", name, e)

        status = get_container_status(manifest)
        if status == "running":
            if env_changed:
                logger.info(
                    "Docker MCP %s: .env changed, force-recreating to pick up new values",
                    name,
                )
                start_container(manifest, force_recreate=True)
            else:
                logger.info("Docker MCP %s: already running", name)
        else:
            logger.info("Docker MCP %s: status=%s, starting...", name, status)
            start_container(manifest)
