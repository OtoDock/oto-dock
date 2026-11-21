"""The pass that keeps every bundled MCP's runtime current: a scan the proxy's
boot runs, and a build that only a process of its own runs.

The scan (``scan_bundled_venvs``) walks ``mcps/custom/`` and
``mcps/community/`` (the latter is gitignored — only locally-present community
MCPs are touched) and decides what brings each MCP's runtime to the platform:

- **Python deps** — if a Python MCP's ``venv/`` is missing OR ``requirements.txt``
  is newer than the venv, rebuild via the shared ``mcp_installer.install_mcp``.
  Platform-shipped (``custom``) folders only: a community MCP installs from its
  ``server.source`` and its folder's ``requirements.txt`` is never built.
- **Python interpreter** — every venv the pass creates is pinned to the proxy's
  own minor (``install_mcp``'s ``python_spec``), the first one included. A venv
  built on an interpreter OLDER than the proxy's (e.g. a 3.10 venv after the
  platform bumped to 3.13), or one whose interpreter lies outside the folders
  the session sandbox binds, is deleted and rebuilt the same way (a host with no
  uv keeps it: nothing there can move it). A floor the package asks for is respected and recorded in the ``.oto-runtime.json``
  marker; a pin that fails is recorded too and retried after a day, never at
  every scan.
- **Node native addons** — if the system ``node`` MAJOR changed since an MCP's
  ``node_modules/`` was built, ``npm rebuild`` its native bindings (advisory).

This closes the gap where a fresh clone, a ``git pull`` that bumps
``requirements.txt``, or a platform runtime bump leaves an MCP's venv/addons stale
and the MCP silently fails to launch.

The boot runs the scan after ``mcp_registry.scan_manifests()`` and then
removes the uv-managed interpreters no venv uses
(``mcp_installer.prune_uv_pythons``), both before the server listens. It
builds nothing: an MCP the scan found stale is left out of sessions
(``tool_envs``), and the log and a notice to every admin name the stale MCPs
and the script that builds them (``build_notice``, ``tell_admins``).

The build runs from two entries, each a process of its own whose steps are
its children, with no sandbox:

- ``python -m services.mcp.mcp_venv_bootstrap --build-image OUT`` (the image
  build, ``proxy/Dockerfile``): the scan and the build at image build time,
  the environments moved into the final image, so a container's first start
  finds every tool ``fresh``.
- ``python -m services.mcp.mcp_venv_bootstrap --build`` (an install from
  source, run from the proxy folder with its venv): the scan, the build
  where the tools are, the prune.

The satellite has its own equivalent (it builds venvs locally during
``sync_mcps`` on the satellite host's interpreter); this module is proxy-only and
lives in a separate file so additions don't bump ``SHARED_MCP_INSTALLER_HASH``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from services.mcp import mcp_installer, mcp_manifest_types as _mt

logger = logging.getLogger("otodock.venv-bootstrap")

# Per-MCP runtime marker (next to manifest.json, SURVIVES a venv rmtree). Records
# the platform runtime an MCP was last reconciled against so the sweep is
# idempotent and never churns a ceiling-pinned MCP.
_RUNTIME_MARKER = ".oto-runtime.json"


# A failed interpreter pin is tried again after this long, not at every boot.
PIN_RETRY_AFTER = timedelta(hours=24)

# The system folders the session sandbox binds read-only that an interpreter
# can live in (``core/sandbox/sandbox_render.py``'s system binds; ``/snap/bin``
# holds no interpreter). ``mcps/.uv-python`` and the MCP's own folder join them.
_SYSTEM_INTERPRETER_ROOTS = ("/usr", "/bin", "/lib", "/lib64", "/lib32", "/sbin")

# A build step runs a package's own code as this process's user, so the server
# starts none: the two entries that build say their process is a build's own.
_build_process = False


def find_uv() -> str | None:
    """The ``uv`` binary the platform installs with, or ``None``: the standard
    locations first (a readable, executable file), then ``PATH``.

    Mirrors ``satellite/sessions/mcp_install_support.py::_uv_bin_if_present`` so
    platform and satellite agree on where to look. ``uv`` is preferred over plain
    ``python -m venv`` + ``pip install`` because it's an order of magnitude faster
    and auto-fetches the right Python when an MCP pins one.
    """
    for candidate in (
        os.path.expanduser("~/.local/bin/uv"),
        "/usr/local/bin/uv",
        "/usr/bin/uv",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK | os.R_OK):
            return candidate
    return shutil.which("uv")


def proxy_python_spec() -> str:
    """The proxy's own Python minor, the interpreter MCP venvs are pinned to."""
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def sandbox_interpreter_roots(mcp_dir: Path | None = None) -> list[Path]:
    """The resolved folders a venv's interpreter must live in for a session
    sandbox to run it: the system binds, the interpreter folders
    (``mcp_installer.interpreter_dirs``) and, given, the MCP's own folder."""
    roots = [Path(p).resolve() for p in _SYSTEM_INTERPRETER_ROOTS]
    roots.extend(d.resolve() for d in mcp_installer.interpreter_dirs(Path(config.MCPS_DIR)))
    if mcp_dir is not None:
        roots.append(Path(mcp_dir).resolve())
    return roots


def venv_outside_sandbox(mcp_dir: Path) -> bool:
    """Whether the MCP's ``venv/`` runs on an interpreter the session sandbox
    does not bind (a hand-built venv on ``~/.local/share/uv``, say)."""
    venv_dir = Path(mcp_dir) / "venv"
    return venv_dir.is_dir() and mcp_installer.venv_outside(
        venv_dir, sandbox_interpreter_roots(mcp_dir))


def _pin_failed_recently(mcp_dir: Path, spec: str, now: datetime | None = None) -> str:
    """The time of a failed pin to ``spec`` within ``PIN_RETRY_AFTER`` ("" when
    none): the venv stays as it is until then."""
    failed = _read_marker(mcp_dir).get("python_pin_failed") or {}
    if not isinstance(failed, dict) or failed.get("target") != spec:
        return ""
    try:
        at = datetime.fromisoformat(str(failed.get("at", "")))
    except ValueError:
        return ""
    now = now or datetime.now(timezone.utc)
    return failed["at"] if now - at < PIN_RETRY_AFTER else ""


def _venv_is_stale(
    deps_file: Path, target_dir: Path, mcp_dir: Path | None = None,
) -> bool:
    """``True`` when ``target_dir`` is missing, or — when ``mcp_dir`` is
    given — not success-marked for the current requirements; without a
    marker, when it is older than ``deps_file``. The marker hash (written
    only after a dep sync exits 0) catches an INTERRUPTED install: mtime
    alone called a created-but-never-synced venv fresh forever (live-hit
    2026-07-11). A marker outranks the mtime: a sync rewrites the venv's
    contents, never the folder's own mtime, so once a checkout touched the
    requirements file the venv read older than it forever and every start
    rebuilt it. Marker-less but populated venvs (pre-marker installs) are
    backfilled without a rebuild; marker-less EMPTY venvs rebuild."""
    if not target_dir.is_dir():
        return True
    if mcp_dir is not None:
        marked = _read_marker(mcp_dir).get("requirements_sha256")
        if marked is not None:
            return marked != _requirements_hash(deps_file)
    try:
        if deps_file.stat().st_mtime > target_dir.stat().st_mtime:
            return True
    except OSError:
        return True
    if mcp_dir is None:
        return False
    if _venv_has_packages(target_dir):
        # Pre-marker healthy venv: adopt it (no fleet-wide rebuild storm on
        # upgrade) — a later requirements edit re-checks via the hash.
        _write_marker(mcp_dir, requirements_sha256=_requirements_hash(deps_file))
        return False
    return True


def _venv_has_packages(venv_dir: Path) -> bool:
    """At least one installed distribution in the venv's site-packages."""
    try:
        for sp in venv_dir.glob("lib/python*/site-packages"):
            if any(sp.glob("*.dist-info")):
                return True
    except OSError:
        pass
    return False


def _requirements_hash(deps_file: Path) -> str:
    try:
        return hashlib.sha256(deps_file.read_bytes()).hexdigest()
    except OSError:
        return ""


_node_major_cache: int | None = None
_node_major_probed = False


def _node_major() -> int | None:
    """Major version of the system ``node`` (cached), or ``None`` if absent."""
    global _node_major_cache, _node_major_probed
    if _node_major_probed:
        return _node_major_cache
    _node_major_probed = True
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            head = r.stdout.strip().lstrip("v").split(".")
            if head and head[0].isdigit():
                _node_major_cache = int(head[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return _node_major_cache


def _read_marker(mcp_dir: Path) -> dict:
    try:
        return json.loads((mcp_dir / _RUNTIME_MARKER).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_marker(mcp_dir: Path, **updates: object) -> None:
    data = _read_marker(mcp_dir)
    data.update(updates)
    try:
        (mcp_dir / _RUNTIME_MARKER).write_text(json.dumps(data))
    except OSError as e:
        logger.warning("could not write runtime marker for %s: %s", mcp_dir.name, e)


def _needs_python_reconcile(venv_dir: Path, target: tuple[int, int], mcp_dir: Path) -> bool:
    """``True`` when the venv interpreter lags the proxy and hasn't been reconciled.

    A venv whose interpreter is older than the proxy's is rebuilt — UNLESS the
    marker shows it's already been reconciled to this exact target, which means a
    real upstream ``requires-python`` ceiling (e.g. ``ha-mcp <3.14`` while the
    platform is 3.14) kept it on its highest allowed interpreter. Re-installing it
    every boot would just land on the same interpreter, so skip it.
    """
    if not venv_dir.is_dir():
        return False  # a missing venv is a full (re)build via the mtime path
    vm = mcp_installer.venv_python_minor(venv_dir)
    if vm is None or vm >= target:
        return False
    return _read_marker(mcp_dir).get("python") != f"{target[0]}.{target[1]}"


@dataclass(frozen=True)
class VenvStep:
    """One bundled MCP's work for the build: a Python venv to build, rebuild
    on the proxy's interpreter (``interp_stale``) or move inside the sandbox's
    folders (``relocate``), or a Node MCP's native addons to rebuild."""

    name: str
    mcp_dir: Path
    runtime: str
    interp_stale: bool = False
    relocate: bool = False
    node_from: int | None = None
    node_to: int | None = None


@dataclass
class VenvPlan:
    """What the scan found: the outcomes it decided (``fresh`` and the skips),
    the steps a build runs and the label of each MCP it read, by name."""

    results: dict[str, str] = field(default_factory=dict)
    steps: list[VenvStep] = field(default_factory=list)
    uv: str | None = None
    spec: str = ""
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(step.name for step in self.steps)


def scan_bundled_venvs() -> VenvPlan:
    """Decide, per bundled MCP, what a build must do. Synchronous (the
    folder reads and one ``node --version``): the boot runs it in a thread
    before the server listens. Its only writes are the markers' backfills
    (``_venv_is_stale``'s adoption of a healthy pre-marker venv, the node
    major recorded on a first sight), both idempotent.

    Outcomes decided here: ``fresh``; ``skipped-no-reqs``;
    ``skipped-community-python`` (a catalog Python MCP, installed from its
    ``server.source`` and never from a folder ``requirements.txt``: one
    outside the sandbox's folders is offered as an update by the check);
    ``skipped-docker`` (``docker_manager``'s); ``skipped-bundled-node``;
    ``skipped-runtime-<x>``.
    """
    plan = VenvPlan(uv=find_uv(), spec=proxy_python_spec())
    mcps_root = Path(config.MCPS_DIR)
    if not mcps_root.is_dir():
        logger.warning("ensure_bundled_venvs: MCPS_DIR %s not found; skipping", mcps_root)
        return plan
    target = sys.version_info[:2]  # the proxy's interpreter — venvs must not lag it
    for category in ("custom", "community"):
        cat_dir = mcps_root / category
        if not cat_dir.is_dir():
            continue
        for mcp_dir in sorted(cat_dir.iterdir()):
            manifest_file = mcp_dir / "manifest.json"
            if not mcp_dir.is_dir() or not manifest_file.is_file():
                continue
            try:
                manifest = json.loads(manifest_file.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("ensure_bundled_venvs: bad manifest at %s: %s", mcp_dir, e)
                continue
            name = manifest.get("name") or mcp_dir.name
            plan.labels[name] = str(manifest.get("label") or name)
            runtime = manifest.get("server", {}).get("runtime", "")
            if runtime == _mt.RUNTIME_DOCKER:
                plan.results[name] = "skipped-docker"
            elif runtime == _mt.RUNTIME_PYTHON:
                _scan_python(plan, name, mcp_dir, category, target)
            elif runtime == _mt.RUNTIME_NODE:
                # A rebuild runs the packages' own lifecycle scripts, which the
                # install refuses for catalog content: platform-shipped
                # folders only (a community MCP with a native addon already
                # depends on prebuilt binaries under --ignore-scripts).
                step = (_scan_node(name, mcp_dir)
                        if category == "custom" and (mcp_dir / "node_modules").is_dir() else None)
                if step is None:
                    plan.results[name] = "skipped-bundled-node"
                else:
                    plan.steps.append(step)
            else:
                plan.results[name] = f"skipped-runtime-{runtime}"
    return plan


def _scan_python(plan: VenvPlan, name: str, mcp_dir: Path, category: str,
                 target: tuple[int, int]) -> None:
    # A requirements build runs any index, VCS or sdist line the file names,
    # outside the wheels-only rule: platform-shipped folders only (a
    # community MCP installs from server.source).
    if category != "custom":
        if venv_outside_sandbox(mcp_dir):
            logger.warning(
                "ensure_bundled_venvs: %s runs on an interpreter outside the folders the "
                "session sandbox binds; Check updates offers its rebuild", name,
            )
        plan.results[name] = "skipped-community-python"
        return
    req_file = mcp_dir / "requirements.txt"
    venv_dir = mcp_dir / "venv"
    if not req_file.is_file():
        plan.results[name] = "skipped-no-reqs"
        return
    reqs_stale = _venv_is_stale(req_file, venv_dir, mcp_dir)
    interp_stale = _needs_python_reconcile(venv_dir, target, mcp_dir)
    relocate = venv_outside_sandbox(mcp_dir)
    if relocate and plan.uv is None:
        # Without uv a rebuild lands on this same interpreter, and the next
        # start would delete it again.
        logger.warning(
            "ensure_bundled_venvs: %s runs on an interpreter outside the folders the "
            "session sandbox binds; install uv to move it", name,
        )
        relocate = False
    failed_at = _pin_failed_recently(mcp_dir, plan.spec) if interp_stale or relocate else ""
    if failed_at:
        logger.warning(
            "ensure_bundled_venvs: the venv of %s stays as it is: its rebuild on Python %s "
            "failed at %s, tried again a day later", name, plan.spec, failed_at,
        )
        interp_stale = relocate = False
    if not reqs_stale and not interp_stale and not relocate:
        plan.results[name] = "fresh"
        return
    plan.steps.append(VenvStep(name, mcp_dir, _mt.RUNTIME_PYTHON,
                               interp_stale=interp_stale, relocate=relocate))


def _scan_node(name: str, mcp_dir: Path) -> VenvStep | None:
    """A Node MCP's addons to rebuild when the system node MAJOR changed
    since ``node_modules`` was built; None otherwise. An absent marker means
    ``node_modules`` was just built on the current node: it is recorded, not
    rebuilt (no spurious first-run rebuild)."""
    current = _node_major()
    if current is None:
        return None  # no node on this host — nothing to rebuild
    recorded = _read_marker(mcp_dir).get("node_major")
    if recorded is None:
        _write_marker(mcp_dir, node_major=current)
        return None
    if recorded == current:
        return None
    return VenvStep(name, mcp_dir, _mt.RUNTIME_NODE, node_from=recorded, node_to=current)


def prune_unused_pythons() -> list[str]:
    """Remove the uv-managed interpreters no venv uses. Only while nothing
    installs: the boot runs it before the server listens, after the scan,
    and the build command after its own build."""
    removed = mcp_installer.prune_uv_pythons(Path(config.MCPS_DIR))
    if removed:
        logger.info("ensure_bundled_venvs: removed unused uv interpreters: %s", removed)
    return removed


#: The admins' notice of the bundled MCPs a start left out (``tell_admins``).
NOTICE_TITLE = "Tools need their environment built"


def build_notice(plan: VenvPlan) -> str:
    """What admins are told once per boot whose scan found MCPs to build, in
    the log and in their notice: the MCPs by label, and the script that
    builds them (it runs this module's ``--build``). The script refuses
    while OtoDock runs, and a start's scan is what reads them built."""
    labels = sorted((plan.labels.get(name, name) for name in plan.names), key=str.casefold)
    return (f"These tools need their environment built: {', '.join(labels)}. No agent can use them until then. "
            f"On the server, stop OtoDock, run {config.BASE_DIR.parent}/scripts/dev-setup.sh and start "
            "OtoDock again.")


async def tell_admins() -> str | None:
    """Once per start, with the boot's other notices
    (``startup._boot_continue``): every admin is told which bundled MCPs the
    scan left out and what builds them, in the log's own words
    (``tool_envs.notice``). The server used to build them at its boot, so
    without this the duty moved onto an admin nobody told. None when every
    MCP is built."""
    from services.mcp import tool_envs
    from services.notifications import notification_manager
    text = tool_envs.notice()
    if not text:
        return None
    await notification_manager.fire_notification(
        title=NOTICE_TITLE, body=text, severity="warning", scope="admin",
        source="platform", source_id="tools/environment",
    )
    return f"{len(tool_envs.stale_names())} tools named to the admins"


async def build_bundled_venvs(plan: VenvPlan, causes: dict[str, str] | None = None) -> dict[str, str]:
    """Run the plan's steps, each as this process's own child with no
    sandbox (``mcp_installer.run_unconfined``): refused, before anything is
    touched, in a process no entry marked as a build's. Returns every
    outcome, the scan's included:

    - ``"ok"`` — venv was (re)built (missing or deps changed)
    - ``"ok-py-reconcile"`` — venv rebuilt because its interpreter lagged the proxy
    - ``"ok-py-relocate"`` — venv rebuilt because its interpreter lay outside the
      folders the session sandbox binds
    - ``"ok-node-rebuild"`` — node addons rebuilt after a node MAJOR bump
    - ``"failed"`` — install_mcp returned ``ok=False``
    - ``"exception"`` — install_mcp raised
    - ``"skipped-node-rebuild-fail"`` — npm rebuild failed (the image build
      goes on, the build command counts it as failed)

    ``causes`` receives, by MCP name, one line that says why a step did not
    work.
    """
    if plan.steps and not _build_process:
        raise RuntimeError("A bundled tool is built by the image build or by the build command, "
                           "never by the server.")
    results = dict(plan.results)
    said: dict[str, str] = {} if causes is None else causes
    for step in plan.steps:
        if step.runtime == _mt.RUNTIME_NODE:
            results[step.name] = await _rebuild_node_addons(step, said)
        else:
            results[step.name] = await _build_python(step, plan, said)
    return results


def _cause_line(text: str) -> str:
    """One line of a build's output for a person to read as the cause: the
    last line that holds a word. pip ends on its error line and uv on the
    innermost reason of its chain, or on the line it quotes (the caret under
    it holds no word)."""
    lines = [line.strip() for line in text.splitlines() if any(ch.isalnum() for ch in line)]
    return lines[-1][:300] if lines else ""


async def _build_python(step: VenvStep, plan: VenvPlan, causes: dict[str, str]) -> str:
    mcp_dir, name = step.mcp_dir, step.name
    req_file = mcp_dir / "requirements.txt"
    venv_dir = mcp_dir / "venv"
    rebuild = step.interp_stale or step.relocate
    created = rebuild or not venv_dir.is_dir()
    if rebuild:
        # Swapping the interpreter REQUIRES deleting the old venv —
        # install_mcp reuses an existing one and creates a missing one on the
        # pin. The success mark goes first: a rebuild that fails must not
        # leave an empty venv reading fresh.
        _forget_success(mcp_dir)
        await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
    logger.info(
        "ensure_bundled_venvs: %s venv for %s at %s",
        "relocating" if step.relocate else "reconciling" if step.interp_stale else "building",
        name, mcp_dir,
    )
    try:
        result = await mcp_installer.install_mcp(
            mcp_dir, "python", "", uv_bin=plan.uv,
            python_spec=plan.spec, sandbox_roots=sandbox_interpreter_roots(mcp_dir),
            run=mcp_installer.run_unconfined,
        )
    except Exception as exc:
        logger.exception("ensure_bundled_venvs: %s unexpected error", name)
        causes[name] = _cause_line(str(exc)) or type(exc).__name__
        return "exception"
    if not result.ok:
        logger.warning("ensure_bundled_venvs: %s install failed:\n%s", name, result.log[-500:])
        causes[name] = _cause_line(result.log) or "the install did not finish"
        return "failed"
    # Success mark: the dep sync exited 0 for THESE requirements — the
    # staleness check trusts only this.
    _write_marker(mcp_dir, requirements_sha256=_requirements_hash(req_file))
    if created:
        _mark_interpreter(mcp_dir, name, plan.spec, result.venv_python, pinnable=plan.uv is not None)
    return "ok-py-relocate" if step.relocate else "ok-py-reconcile" if step.interp_stale else "ok"


async def _rebuild_node_addons(step: VenvStep, causes: dict[str, str]) -> str:
    """``npm rebuild`` a Node MCP's native addons. A rebuild that fails is
    logged and never raised."""
    logger.info("ensure_bundled_venvs: %s node %s→%s — npm rebuild",
                step.name, step.node_from, step.node_to)
    try:
        # The packages' lifecycle scripts run here, with the installer's
        # environment and time bound. A timeout keeps the old marker, so the
        # next scan asks for the rebuild again.
        rc, out = await mcp_installer.run_unconfined(
            ["npm", "rebuild"], cwd=str(step.mcp_dir), env=mcp_installer._install_env(),
            timeout=mcp_installer.DEFAULT_INSTALL_TIMEOUT, step="rebuild", mcp_dir=step.mcp_dir)
        if rc == 0:
            _write_marker(step.mcp_dir, node_major=step.node_to)
            return "ok-node-rebuild"
        logger.warning("npm rebuild for %s failed: %s", step.name, out[-300:])
        causes[step.name] = _cause_line(out) or f"npm rebuild exited {rc}"
        return "skipped-node-rebuild-fail"
    except Exception as exc:
        logger.exception("npm rebuild for %s errored", step.name)
        causes[step.name] = _cause_line(str(exc)) or type(exc).__name__
        return "skipped-node-rebuild-fail"


async def ensure_bundled_venvs_at_startup() -> dict[str, str]:
    """Scan, prune and build in one go: the image build's pass."""
    plan = await asyncio.to_thread(scan_bundled_venvs)
    await asyncio.to_thread(prune_unused_pythons)
    return await build_bundled_venvs(plan)


def _forget_success(mcp_dir: Path) -> None:
    """Drop the success mark (and the reconciled interpreter) before a venv is
    deleted for a rebuild: until the rebuild succeeds the venv is stale."""
    data = _read_marker(mcp_dir)
    dropped = [data.pop(key, None) for key in ("requirements_sha256", "python")]
    if dropped == [None, None]:
        return
    try:
        (mcp_dir / _RUNTIME_MARKER).write_text(json.dumps(data))
    except OSError as e:
        logger.warning("could not write runtime marker for %s: %s", mcp_dir.name, e)


def _mark_interpreter(mcp_dir: Path, name: str, spec: str, venv_python: str,
                      *, pinnable: bool = True) -> None:
    """Record what the venv just created runs on. Made on the pin or on a
    floor the package asked for, inside the sandbox's folders: reconciled.
    On uv's default (the pin failed) or still outside: the failure, so the
    next start leaves it alone for a day instead of reading it reconciled.
    With no uv on the host nothing can be pinned: the venv reads reconciled
    as before, never a daily rebuild."""
    data = _read_marker(mcp_dir)
    if not pinnable or (venv_python and not venv_outside_sandbox(mcp_dir)):
        data.pop("python_pin_failed", None)
        data["python"] = spec
    else:
        data.pop("python", None)
        data["python_pin_failed"] = {
            "target": spec, "at": datetime.now(timezone.utc).isoformat(),
        }
        logger.warning(
            "ensure_bundled_venvs: %s could not be built on Python %s inside the "
            "folders the session sandbox binds; tried again a day later", name, spec,
        )
    try:
        (mcp_dir / _RUNTIME_MARKER).write_text(json.dumps(data))
    except OSError as e:
        logger.warning("could not write runtime marker for %s: %s", mcp_dir.name, e)


# -- the two entries that build -----------------------------------------------

#: The outcomes that fail an image build: a release never ships a tool that
#: did not build.
_IMAGE_FAILURES = frozenset({"failed", "exception"})


def collect_for_image(out_root: Path) -> list[str]:
    """Move every bundled tool's built environment and marker under
    ``out_root`` (its ``custom/<folder>/`` tree, and ``.uv-python`` when uv
    fetched an interpreter): what the image takes from the build stage. It
    walks the folders, since the outcomes are keyed by manifest name. Returns
    the problems: a marker that records a failed interpreter pin (a day
    after every first start the scan would read that tool stale, and a
    container builds none)."""
    problems: list[str] = []
    mcps_root = Path(config.MCPS_DIR)
    custom = mcps_root / "custom"
    for mcp_dir in sorted(custom.iterdir()) if custom.is_dir() else []:
        venv = mcp_dir / "venv"
        marker = mcp_dir / _RUNTIME_MARKER
        if not venv.is_dir() and not marker.is_file():
            continue
        if _read_marker(mcp_dir).get("python_pin_failed"):
            problems.append(f"{mcp_dir.name}: its interpreter pin failed")
        target = out_root / "custom" / mcp_dir.name
        target.mkdir(parents=True, exist_ok=True)
        if venv.is_dir():
            shutil.move(str(venv), str(target / "venv"))
        if marker.is_file():
            shutil.move(str(marker), str(target / _RUNTIME_MARKER))
    pythons = mcps_root / ".uv-python"
    if pythons.is_dir() and any(pythons.iterdir()):
        shutil.move(str(pythons), str(out_root / ".uv-python"))
    return problems


def build_for_image(out_root: Path) -> int:
    """The image build's whole job: the boot's own scan and prune, the
    build, then the environments moved under ``out_root``. The exit code:
    non-zero when a tool did not build or its interpreter pin failed. A
    build stage holds no secret and no data."""
    global _build_process
    _build_process = True
    results = asyncio.run(ensure_bundled_venvs_at_startup())
    failed = sorted(name for name, outcome in results.items() if outcome in _IMAGE_FAILURES)
    problems = collect_for_image(out_root)
    for name in failed:
        logger.error("the image build: %s did not build", name)
    for problem in problems:
        logger.error("the image build: %s", problem)
    return 1 if failed or problems else 0


def build_in_place() -> int:
    """The build command of an install from source, run from the proxy
    folder with its venv: the boot's own scan, the build of what it found
    stale where the tools are, then the prune. One line per tool on standard
    output. The exit code: non-zero when a tool did not build. A step runs a
    pinned package's build code as the install's OS user, the trust the
    install's script gives the server's own packages."""
    global _build_process
    _build_process = True
    plan = scan_bundled_venvs()
    causes: dict[str, str] = {}
    results = asyncio.run(build_bundled_venvs(plan, causes))
    prune_unused_pythons()
    if not plan.steps:
        print("Nothing to build.")
        return 0
    failed = [step.name for step in plan.steps if not results[step.name].startswith("ok")]
    for step in plan.steps:
        if step.name in failed:
            print(f"failed {step.name}: {causes.get(step.name) or results[step.name]}")
        else:
            print(f"built {step.name}")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    """``--build`` or ``--build-image OUT``: the exit code of the entry."""
    if argv != ["--build"] and not (len(argv) == 2 and argv[0] == "--build-image"):
        sys.exit("usage: python -m services.mcp.mcp_venv_bootstrap --build | --build-image OUT")
    logging.basicConfig(level=logging.INFO)
    return build_in_place() if argv == ["--build"] else build_for_image(Path(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
