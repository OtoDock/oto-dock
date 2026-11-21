"""Orchestrate MCP install/update/remove on a satellite for a session.

Called from ``RemoteExecutionLayer.start_session`` BEFORE the actual
``start_session`` command. Computes the desired set of MCPs for the
satellite (union over all active sessions plus this agent's assignment),
diffs it against the satellite's reported ``installed_mcps``, and ships a
batched ``sync_mcps`` command with pre-built tarballs for anything to
install or update.

Per-MCP failures are soft — the session still starts with the failed MCP
excluded (surfaced to the caller via ``SyncResult.excluded_names`` so it
can strip those entries from the shipped MCP config). The caller routes
plan + progress events through its own ``plan_cb`` / ``progress_cb``
callbacks; this module is registry-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from core import placement as _placement
from services.mcp import (
    mcp_installer,
    mcp_manifest_types as _mt,
    mcp_pin,
    mcp_registry,
    mcp_tarball,
)
from services.remote import machine_tools

logger = logging.getLogger("otodock.mcp-sync")

# Per-(machine_id, mcp) backoff for updates the satellite DEFERRED because the
# old version was still in use — its running process held the files so the
# in-place swap was blocked (common on Windows; see sync_mcps). Without this we
# would re-ship + rebuild the same update (~70s on the satellite) on EVERY
# session until the holding session closes. In-memory + transient: maps key ->
# monotonic deadline, self-expires, and is cleared once the update lands. The
# companion leak fix (reap a session's MCP children on close) bounds the hold to
# the holder's lifetime, so a short backoff is enough to absorb that window.
_DEFER_BACKOFF_S = 600.0
_deferred_updates: dict[tuple[str, str], float] = {}


def _is_update_deferred(machine_id: str, mcp_name: str) -> bool:
    """True while a recently-deferred update for (machine, mcp) is backed off."""
    deadline = _deferred_updates.get((machine_id, mcp_name))
    if deadline is None:
        return False
    if time.monotonic() >= deadline:
        _deferred_updates.pop((machine_id, mcp_name), None)
        return False
    return True


def _mark_update_deferred(machine_id: str, mcp_name: str) -> None:
    _deferred_updates[(machine_id, mcp_name)] = time.monotonic() + _DEFER_BACKOFF_S


# Per-(machine_id, mcp) memo of DETERMINISTIC install failures — the uv
# resolver declaring the requirements unsatisfiable (e.g. the MCP needs
# Python >= 3.11 and the satellite venv is 3.10). Retrying an unsatisfiable
# resolve on EVERY session start re-ran a doomed ~0.5s install per warmup —
# wasted latency plus an install-bar flash the user reads as "it's installing
# something again". The memo keys on the SHIPPED version_hash: a platform-side
# MCP change (new requirements) invalidates it, a successful install or a new
# satellite version clears it, ``force`` bypasses + clears it, and an entry
# expires after ``_UNSAT_MEMO_TTL_S``: a package the machine could not build
# may build once the person installs what it lacked (Rust, a library's
# headers). In-memory: a proxy restart grants every memoized MCP one fresh
# attempt. Transient failures (network, file locks, disk) are NEVER memoized.
# The version hash per MCP dir, keyed by the manifest version plus the mtime
# and size of each hash input file present, served for a minute: an
# install, update or switch rewrites the manifest (a new key at once), a
# source edit under an unchanged manifest (the documented way to drift the
# hash) is seen within the minute, and a remote session start no longer
# hashes every MCP's source on the loop. Misses are computed off the loop
# (``version_hashes_for``) before ``_diff`` runs.
_HASH_TTL_S = 60.0
_version_hashes: dict[str, tuple[tuple, str, float]] = {}


def _hash_key(manifest) -> tuple | None:
    parts: list = [manifest.version]
    mcp_dir = Path(manifest.mcp_dir)
    for fname in mcp_installer._HASH_INPUT_FILES:
        try:
            st = (mcp_dir / fname).stat()
        except FileNotFoundError:
            parts.append(None)
        except OSError:
            return None
        else:
            parts.append((st.st_mtime_ns, st.st_size))
    return tuple(parts)


def _version_hash(manifest, *, force: bool = False) -> str:
    """``compute_version_hash`` for ``manifest``, from the cache when its key
    and age allow. Synchronous: a miss walks the MCP's source."""
    key = _hash_key(manifest)
    now = time.monotonic()
    entry = _version_hashes.get(str(manifest.mcp_dir))
    if (not force and key is not None and entry is not None
            and entry[0] == key and now - entry[2] < _HASH_TTL_S):
        return entry[1]
    digest = mcp_installer.compute_version_hash(manifest.mcp_dir)
    if key is not None:
        _version_hashes[str(manifest.mcp_dir)] = (key, digest, now)
    return digest


async def version_hashes_for(names, *, force: bool = False) -> dict[str, str]:
    """The current version hash of every named MCP with a manifest, the
    misses computed in a worker thread."""
    manifests = {n: m for n in names if (m := mcp_registry.get_manifest(n)) is not None}

    def _job() -> dict[str, str]:
        out: dict[str, str] = {}
        for name, manifest in manifests.items():
            try:
                out[name] = _version_hash(manifest, force=force)
            except (OSError, ValueError):
                out[name] = ""
        return out

    return await asyncio.to_thread(_job)


_UNSAT_FAIL_RE = re.compile(
    r"No solution found when resolving dependencies"
    r"|does not satisfy Python"
    # A package this machine has no build for and cannot build (no Rust, a
    # missing library's headers): lasting until the MCP or the machine changes.
    r"|has no usable wheels|Failed to build|Failed building wheel"
    r"|RequiredDependencyException|(?:rustc|cargo)\b[^\n]*not (?:found|installed)"
)
# Words of a network, service, disk or file-lock failure (uv's, pip's,
# cargo's, the OS's) and of the satellite's memory guard stopping a build:
# never memoized, whatever else the error names, since the next start may
# well succeed. The satellite keeps such lines first in a failure's cause, by the
# same words (``mcp_install_support._NETWORK_WORDS``, held equal by a test).
_TRANSIENT_FAIL_RE = re.compile(
    r"Failed to fetch|Failed to download|resolve host|spurious network error|error sending request"
    r"|dns error|timed out|Connection (?:reset|refused|closed)|Network is unreachable|tls handshake"
    r"|No space left on device|Disk quota exceeded|Text file busy|resource busy"
    r"|being used by another process|Resource temporarily unavailable|used too much memory and was stopped",
    re.IGNORECASE,
)
# How long a memoized failure is skipped before the next start tries again.
_UNSAT_MEMO_TTL_S = 3600.0
_unsatisfiable_installs: dict[tuple[str, str], dict] = {}

# Callback signature: proxy-side mcp_sync calls this with each progress
# event received from the satellite. Used by the pump to surface progress
# as SYSTEM CommonEvents in the chat.
ProgressCb = Callable[[dict], Awaitable[None]] | Callable[[dict], None] | None


@dataclass
class SyncResult:
    """What happened during a sync_mcps round."""
    ok: bool = True
    installed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    # MCPs excluded from this session due to install failures. The caller
    # should strip these from the MCP config it ships to the satellite.
    excluded_names: set[str] = field(default_factory=set)
    # Subset of excluded_names that were skipped from a MEMOIZED deterministic
    # failure (no install was attempted this session) — the caller must not
    # emit per-session mcp_install_failed frames for these: a lone failure
    # frame outside a started/done lifecycle would seed a ghost install bar.
    memoized: set[str] = field(default_factory=set)
    # MCPs that installed fine but failed the post-install pre-warm boot
    # check: name -> reason. Advisory only — these are NOT excluded
    # (a missing startup credential isn't a real failure; the real session
    # env supplies it). Surfaced so the caller can log + show a UI hint.
    warmup_failed: dict[str, str] = field(default_factory=dict)
    # MCPs whose UPDATE the satellite deferred because the old version was still
    # in use (swap blocked). NOT excluded — the session uses the working old
    # version; the update lands once the holder closes. name -> reason.
    deferred: dict[str, str] = field(default_factory=dict)
    # MCPs of the plan not done when the session started (``early_start_s``):
    # excluded too, never reported as failures; ``watch`` tells when they end.
    still_installing: set[str] = field(default_factory=set)
    # MCPs a start beside another session's plan found missing and outside
    # that plan: excluded, never promised an end; the next start installs them.
    not_installed: set[str] = field(default_factory=set)
    watch: InstallWatch | None = field(default=None, repr=False, compare=False)


def unavailable_reasons(result: SyncResult, assigned) -> dict[str, str]:
    """The prompt's reason for each of the session's own MCPs the sync
    excluded (one line each); a folder the machine holds for no assigned MCP
    is the sync's business, never the prompt's."""
    out: dict[str, str] = {}
    for name in result.excluded_names & set(assigned):
        if name in result.still_installing:
            out[name] = "still installing on this machine, a new chat here can use it once it is done"
            continue
        if name in result.not_installed:
            out[name] = "not installed yet on this machine, a new chat here installs it"
            continue
        # Whole enough to carry the cause the machine reported first.
        err = machine_tools.said(result.failed.get(name))
        out[name] = (f"could not be installed on this machine ({err})" if err
                     else "could not be installed on this machine")
    return out


# Each MCP's install budget, counted from its own start (the satellite
# installs a plan one MCP after the other): generous, as a slow machine takes
# minutes for a heavy one. Past it the MCP reads as failed and the ones queued
# behind it as not started; the satellite carries on, and the next session
# finds them installed. One ceiling for a whole plan (600 s) ran out on a
# slow machine with a long plan while every install was still on its way.
INSTALL_BUDGET_S = 600.0
SOURCE_BUILD_BUDGET_S = 1200.0   # a package the manifest lets build from source
# The satellite's check after the last install (it boots each fresh MCP once).
VERIFY_BUDGET_S = 180.0
# How long a session start waits for its installs before it starts with the
# tools that are ready (``SyncResult.still_installing``): a person's chat,
# and a phone call sooner (a caller is on the line).
EARLY_START_S = 120.0
PHONE_EARLY_START_S = 45.0
# The progress phase of the satellite's check after the installs (no ``mcp``).
VERIFYING_PHASE = "verifying"
# The phases of an MCP's own end: installed (or its update kept back), failed.
_END_PHASES = {"done": True, "failed": False}
# The plan running on each machine, while its listener runs.
_watches: dict[str, InstallWatch] = {}


_COUNT_WORDS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
                "Nine", "Ten")


def tool_labels(names) -> list[str]:
    """The tools' names as a person reads them (each manifest's label)."""
    out = []
    for name in names:
        manifest = mcp_registry.get_manifest(name)
        out.append(str(getattr(manifest, "label", "") or name))
    return out


def _listed(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"


def installing_sentence(labels: list[str], place: str) -> str:
    """The chat's line when its session starts with tools still installing."""
    if len(labels) == 1:
        return (f"One tool is still installing on {place}: {labels[0]}. Start a new chat on "
                "this machine once it is done to use it.")
    count = _COUNT_WORDS[len(labels)] if len(labels) < len(_COUNT_WORDS) else str(len(labels))
    return (f"{count} tools are still installing on {place}: {_listed(labels)}. Start a new "
            "chat on this machine once they are done to use them.")


def finished_sentence(ok: list[str], failed: list[str], place: str) -> str:
    """The chat's line once those tools ended: finished, or some failed."""
    them = "it" if len(ok) == 1 else "them"
    if not failed:
        done = "The tool" if len(ok) == 1 else "The tools"
        return f"{done} finished installing on {place}. Start a new chat on {place} to use {them}."
    out = f"{_listed(failed)} could not be installed on {place}."
    if ok:
        out += f" {_listed(ok)} finished installing. Start a new chat on {place} to use {them}."
    return out


def early_start_for(client_type: str | None) -> float | None:
    """How long a session start of this kind waits for its installs before
    it starts with the tools that are ready: a person's chat, a phone call
    sooner; ``None`` for a run nobody watches (it waits for its plan)."""
    from core.session import session_kind
    if session_kind.attended(client_type):
        return EARLY_START_S
    if session_kind.of(client_type) is session_kind.PHONE:
        return PHONE_EARLY_START_S
    return None


def _install_budget(spec: dict) -> float:
    return SOURCE_BUILD_BUDGET_S if spec.get("source_build") else INSTALL_BUDGET_S


class InstallWatch:
    """One sync's plan on a machine, read from the satellite's progress
    events. The satellite installs the plan in its order and names each
    MCP's own start and end (``mcp`` = the name; the installer's own lines
    say ``<name>.new``), so each MCP is queued, installing or ended, and the
    one installing is judged by its own budget from its own start."""

    def __init__(self, machine_id: str, order: list[str], budgets: dict[str, float],
                 installs: set[str]) -> None:
        self.machine_id = machine_id
        self.order = order
        self.budgets = budgets
        self.installs = installs           # the rest of the plan are updates
        self.ended: dict[str, bool] = {}   # name -> installed (or kept), else failed
        self.errors: dict[str, str] = {}
        self.unstarted: set[str] = set()   # queued behind one past its budget: unknown
        self.detached = False              # the session started: its progress_cb is done
        self.told: set[str] = set()        # the chats whose line names this plan
        self.changed = asyncio.Event()
        self.result: SyncResult | None = None
        self._since = time.monotonic()     # the start of the MCP installing now
        self._verifying_since: float | None = None
        self._listeners: list[Callable[[InstallWatch], object]] = []

    def pending(self) -> list[str]:
        return [n for n in self.order if n not in self.ended]

    def ceiling(self) -> float:
        """The plan's own command timeout: past every budget, so the watch
        always ends the wait first."""
        return sum(self.budgets.values()) + VERIFY_BUDGET_S

    def on_event(self, ev: dict) -> None:
        name = str(ev.get("mcp") or "")
        phase = str(ev.get("phase") or "")
        if phase == VERIFYING_PHASE:
            self._verifying_since = time.monotonic()
        elif name in self.budgets and name not in self.ended and phase in _END_PHASES:
            self.ended[name] = _END_PHASES[phase]
            if not self.ended[name]:
                self.errors[name] = str(ev.get("error") or ev.get("message") or "install failed")
            self._since = time.monotonic()
        self.changed.set()

    def deadline(self) -> float:
        pending = self.pending()
        if pending and self._verifying_since is None:
            return self._since + self.budgets[pending[0]]
        return (self._verifying_since or self._since) + VERIFY_BUDGET_S

    def expire(self) -> None:
        """The MCP installing ran past its budget: it reads as failed; the
        ones queued behind it stay unknown (they never started)."""
        pending = self.pending()
        if pending and self._verifying_since is None:
            name = pending[0]
            self.ended[name] = False
            self.errors[name] = (f"did not finish installing in "
                                 f"{round(self.budgets[name] / 60)} minutes")
        self.unstarted = set(self.pending())
        logger.warning("sync_mcps %s: past the install budget, %s did not end",
                       _placement.log_id(self.machine_id), sorted(self.unstarted) or "the check")

    def so_far(self) -> SyncResult:
        """The plan as far as it went, for a session that starts before it
        ends: an MCP not ended is still installing and left out (an install
        is not there yet, an update may swap its folder under the session's
        process)."""
        result = SyncResult(watch=self)
        for name in self.order:
            end = self.ended.get(name)
            if end is None:
                result.still_installing.add(name)
                result.excluded_names.add(name)
            elif end:
                (result.installed if name in self.installs else result.updated).append(name)
            else:
                result.failed[name] = self.errors.get(name, "install failed")
                result.excluded_names.add(name)
        return result

    def outcome(self, names) -> tuple[list[str], list[str], list[str]]:
        """``names`` split by how each ended: installed, failed, unknown."""
        ok = [n for n in names if self.ended.get(n) is True]
        failed = [n for n in names if self.ended.get(n) is False]
        return ok, failed, [n for n in names if n not in self.ended]

    def settle(self, result: SyncResult) -> None:
        """The plan ended: the result's word on each MCP, then the listeners."""
        for name in (*result.installed, *result.updated, *result.deferred):
            self.ended[name] = True
        for name in result.failed:
            if name in self.budgets and name not in self.unstarted:
                self.ended.setdefault(name, False)
        self.result = result
        listeners, self._listeners = self._listeners, []
        for cb in listeners:
            self._call(cb)

    def when_settled(self, cb: Callable[[InstallWatch], object]) -> None:
        """Call ``cb(watch)`` once the plan ended (now when it has)."""
        if self.result is not None:
            self._call(cb)
        else:
            self._listeners.append(cb)

    def _call(self, cb) -> None:
        try:
            r = cb(self)
            if asyncio.iscoroutine(r):
                from core import background
                background.spawn(r, name=f"settled-{self.machine_id[:8]}", owner="mcp-sync")
        except Exception:  # a listener's failure never stops the others
            logger.exception("sync_mcps: a settled listener failed")


async def _await_plan(watch: InstallWatch, ack: asyncio.Future, *, until: float | None) -> bool:
    """Wait for the plan's ack, the end of the current MCP's budget, or
    ``until`` (the session's early start, a monotonic instant). True when the
    plan ended (the ack, a failed command, a budget passed); False at
    ``until``."""
    while not ack.done():
        now = time.monotonic()
        deadline = watch.deadline()
        if now >= deadline:
            watch.expire()
            return True
        if until is not None and now >= until:
            return False
        stop = deadline if until is None else min(deadline, until)
        watch.changed.clear()
        changed = asyncio.ensure_future(watch.changed.wait())
        try:
            await asyncio.wait({ack, changed}, timeout=stop - now,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            changed.cancel()
    return True


async def _finish_in_background(watch: InstallWatch, ack: asyncio.Future,
                                stack: contextlib.AsyncExitStack,
                                conclude: Callable[[], SyncResult],
                                abandon: Callable[[], None]) -> None:
    """The rest of a plan a session started beside: wait for the ack or the
    budget, settle the watch (its listeners say how the plan ended), then
    free the machine's install lock. A tail cancelled first (the proxy
    shutting down) leaves no listener behind."""
    try:
        await _await_plan(watch, ack, until=None)
        conclude()
    finally:
        abandon()
        await stack.aclose()


def _skip_known(machine_id: str, to_install: set[str], to_update: set[str],
                hashes: dict[str, str], sat_ver: str, *, force: bool) -> dict[str, str]:
    """Drop from the plan what this machine already answered: an update it
    recently deferred (its old version in use) and an install the resolver
    declared unsatisfiable for the same bytes. Returns the memoized ones with
    their error; ``force`` (the retry button) clears both and skips nothing."""
    # Re-shipping a deferred update would rebuild ~70s on the satellite
    # every session for nothing; the session keeps the working old version.
    # The backoff self-expires and is cleared on a successful install.
    if to_update and not force:
        deferred_now = {n for n in to_update if _is_update_deferred(machine_id, n)}
        if deferred_now:
            logger.info(
                "sync_mcps: skipping %d deferred update(s) for %s (in use): %s",
                len(deferred_now), _placement.log_id(machine_id), sorted(deferred_now),
            )
            to_update -= deferred_now
    memo_skips: dict[str, str] = {}
    for name in sorted(to_install | to_update):
        memo = _unsatisfiable_installs.get((machine_id, name))
        if memo is None:
            continue
        if force or time.monotonic() - memo.get("at", 0.0) >= _UNSAT_MEMO_TTL_S:
            _unsatisfiable_installs.pop((machine_id, name), None)
            continue
        if memo.get("satellite_version") != sat_ver:
            # The satellite self-updated since the failure — installer
            # behavior may have changed (e.g. a new python-floor retry) —
            # the verdict deserves a fresh attempt.
            _unsatisfiable_installs.pop((machine_id, name), None)
            continue
        current_hash = hashes.get(name, "")
        if current_hash and current_hash == memo.get("hash"):
            memo_skips[name] = memo.get("error", "install unsatisfiable")
        else:
            # Platform-side MCP changed — the verdict may differ now.
            _unsatisfiable_installs.pop((machine_id, name), None)
    if memo_skips:
        to_install -= set(memo_skips)
        to_update -= set(memo_skips)
        logger.info(
            "sync_mcps: skipping %d memoized unsatisfiable install(s) "
            "for %s: %s", len(memo_skips), _placement.log_id(machine_id),
            sorted(memo_skips),
        )
    return memo_skips


def _with_memo(result: SyncResult, memo_skips: dict[str, str]) -> SyncResult:
    """``result`` with the memoized unsatisfiable installs excluded as
    failed (no install was attempted for them this session)."""
    for name, err in memo_skips.items():
        result.failed[name] = err
        result.excluded_names.add(name)
        result.memoized.add(name)
    return result


async def _beside_running_sync(cm, machine_id: str, desired: set[str], *,
                               force: bool) -> SyncResult:
    """A session start while another session's plan still holds the
    machine's install lock past the early start: no install of its own (the
    running plan may be swapping the very folders). It starts with what the
    satellite has. What the running plan still installs or updates reads as
    still installing, and its watch tells when it ends; what the satellite
    lacks outside that plan is left out as not installed yet (the next start
    installs it, nothing here promises an end); the deferred updates and the
    memoized unsatisfiable installs are answered as on the main path."""
    installed = await _fetch_satellite_state(cm, machine_id)
    hashes = await version_hashes_for(desired, force=force)
    to_install, to_update, _ = _diff(desired=desired, installed=installed, force=force,
                                     hashes=hashes)
    busy = _being_installed(to_install | to_update)
    to_update -= busy
    sat_ver = getattr(cm.get_connection(machine_id), "satellite_version", "") or ""
    memo_skips = _skip_known(machine_id, to_install, to_update, hashes, sat_ver, force=force)
    watch = _watches.get(machine_id)
    # Only what the running plan's watch follows reads as still installing,
    # so the chat hears when it ends. What an ahead run has still queued
    # reads as not installed yet: no watch follows it, nothing is promised.
    running = set(watch.pending()) if watch is not None else set()
    still = (to_install | to_update) & running
    missing = to_install - running
    logger.info("sync_mcps: %s still installs for another session; this one starts without "
                "%s (installing) and %s (not installed yet)",
                _placement.log_id(machine_id), sorted(still), sorted(missing))
    return _with_memo(SyncResult(still_installing=still, not_installed=missing,
                                 excluded_names=still | missing,
                                 watch=watch if still else None), memo_skips)


# The categories a removal may name at all.
_MCP_CATEGORIES = frozenset({"core", "custom", "community"})


def _session_contexts(client_type: str) -> set[str]:
    """The ``exclude_from`` contexts a session of this kind was built for
    (``build_session_mcp_config``: meetings ride the task lane). A kind's
    name is its ``exclude_from`` word."""
    from core.session import session_kind
    kind = session_kind.of(client_type)
    if kind is session_kind.MEETING:
        return {session_kind.TASK.name, session_kind.MEETING.name}
    if kind in (session_kind.TASK, session_kind.PHONE):
        return {kind.name}
    return {session_kind.DASHBOARD.name}


def _lifted(agent_name: str, launched: set[str], *, contexts: set[str],
            external: bool, role: str, placement) -> set[str]:
    """See ``lifted_by_context``. Reads the database: off the loop."""
    candidates = [
        m for m in mcp_registry.get_agent_mcps(agent_name, placement=placement)
        if m.name not in launched and _mt.installs_on_host(m.server)
        # another gate left it out: it stays out
        and mcp_registry.session_exclusion_reason(m, contexts=contexts, external=external) is not None
    ]
    return _admitted(agent_name, candidates, role=role, placement=placement, per_user=False)


def machine_admitted(agent_name: str, *, role: str, placement) -> set[str]:
    """What a session of ``agent_name`` on this machine could be given, by
    the gates a session's build applies on a machine (``_admitted``), its
    context aside: what install ahead sends the machine, never more. A tool
    on a person's account is in: the package is the same for everyone. Reads
    the database: off the loop."""
    candidates = [m for m in mcp_registry.get_agent_mcps(agent_name, placement=placement)
                  if _mt.installs_on_host(m.server)]
    return _admitted(agent_name, candidates, role=role, placement=placement, per_user=True)


def _admitted(agent_name: str, manifests, *, role: str, placement, per_user: bool) -> set[str]:
    """The names of ``manifests`` a session on this machine may get: its
    role's audience, the admin-machine-only rule, the instances a machine
    can be given and the agent's service credentials."""
    from services.mcp.mcp_session_config import kept_off_a_persons_machine
    from storage.mcp import mcp_store
    out: set[str] = set()
    infra: list[str] = []
    for m in manifests:
        if getattr(m, "audience", "") and not mcp_registry.audience_admits(m.audience, role):
            continue
        if placement.user_paired and kept_off_a_persons_machine(m):
            continue
        inst = getattr(m, "instances", None)
        if inst is not None:
            if inst.delivery == "config_file":
                continue                # never delivered to a satellite
            if (inst.delivery == "env"
                    and not mcp_store.get_instance_for_agent_env_delivery(m.name, agent_name)):
                continue
        kind = getattr(getattr(m, "credentials", None), "type", "none")
        if kind == "per_user" and not per_user:
            continue                    # the build's identity is not known here
        if kind == "infra":
            infra.append(m.name)
        else:
            out.add(m.name)
    if infra:
        from services.oauth import credential_resolver
        excluded = credential_resolver.resolve_credentials(
            agent_name, None, task_scope="agent").excluded_mcps
        out.update(n for n in infra if n not in excluded)
    return out


async def lifted_by_context(config, launched: set[str]) -> set[str]:
    """The agent's enabled MCPs this remote session leaves out by its
    context alone (a task leaves out ``exclude_from: task``): they stay
    wanted at its start, installed and updated, so a task never makes the
    next chat on the machine rebuild them.

    Never more than the session's build would have admitted: an MCP left
    out by another gate (the placement, the audience, the credentials, an
    instance) stays out. The build's credential identity is not on the
    session config, so an MCP with per-user credentials is never lifted,
    an audience needs the session's role, and a judge session lifts
    nothing. Leaving one out only defers its install to the first session
    that launches it."""
    from core.session import external_identity
    sc = getattr(config, "security_context", None)
    placement = getattr(sc, "placement", None)
    if sc is None or placement is None or getattr(config, "permission_mode", "") == "judge":
        return set()
    try:
        return await asyncio.to_thread(
            _lifted, config.agent_name, set(launched),
            contexts=_session_contexts(getattr(config, "client_type", "") or ""),
            external=external_identity.is_external_ctx(sc),
            role=getattr(sc, "role", "") or "",
            placement=placement,
        )
    except Exception:  # noqa: BLE001  # lifting reads the store and the credential resolver, a miss lifts none
        logger.warning("sync_mcps: the lifted set failed for %s", config.agent_name,
                       exc_info=True)
        return set()


def _sent_here(cm, machine_id: str, names) -> set[str]:
    """``names`` as far as the machine is sent them: all of them, and to the
    compute machine beside the platform the community tools alone (the
    bundled ones come with its image, in folders it never writes)."""
    names = set(names)
    if not cm.builds_own_tools(machine_id):
        return names
    return {n for n in names
            if (m := mcp_registry.get_manifest(n)) is not None and machine_tools.built_on_machine(m)}


def _key(name: str) -> str:
    """The machine's key of the registry's tool ``name`` (its folder,
    ``machine_tools.key_of``), ``name`` itself for one the registry does not
    hold."""
    manifest = mcp_registry.get_manifest(name)
    return machine_tools.key_of(manifest) if manifest is not None else name


def _names_by_key() -> dict[str, str]:
    """``{folder: name}`` of the registry's tools a machine keys otherwise
    than the platform: a machine keys a tool by its folder, which is its
    name for every tool but one uploaded before names had the machine's
    rule. A folder that is another tool's name stays that tool's."""
    manifests = mcp_registry.get_all_manifests()
    return {key: m.name for m in manifests.values()
            if (key := machine_tools.key_of(m)) != m.name and key not in manifests}


def _named(answer: dict) -> dict:
    """A machine's answer keyed by tool (its verify, an ack's results), by
    the registry's names (``_names_by_key``). A folder named after a tool
    whose own folder is another (``machine_tools.key_of``) is a copy a
    machine kept from before tools were keyed by folder: it is not that
    tool, so the sync installs the tool under its folder, where its start
    command points. That copy is left on the machine: nothing starts it, and
    removing it by the tool's name would take the tool's own folder."""
    manifests = mcp_registry.get_all_manifests()
    keys = {machine_tools.key_of(m) for m in manifests.values()}
    stale = {name for name, m in manifests.items() if machine_tools.key_of(m) != name and name not in keys}
    by_key = _names_by_key()
    return {by_key.get(key, key): info for key, info in answer.items() if key not in stale}


#: The installer's own progress lines name the folder it builds in.
_STAGED = ".new"


def _event(ev: dict) -> dict:
    """A machine's progress event as the platform takes it: its tool by the
    registry's name, its words cut (``machine_tools.said``), its percentage
    a whole number from 0 to 100."""
    out = dict(ev)
    for field_name in ("mcp", "phase", "message", "error"):
        if field_name in out:
            out[field_name] = machine_tools.said(out[field_name])
    if out.get("mcp"):
        key, staged = str(out["mcp"]), ""
        if key.endswith(_STAGED):
            key, staged = key[:-len(_STAGED)], _STAGED
        out["mcp"] = _names_by_key().get(key, key) + staged
    if "pct" in out:
        try:
            out["pct"] = max(0, min(100, int(out["pct"])))
        except (TypeError, ValueError, OverflowError):
            out["pct"] = 0
    return out


def _being_installed(names) -> set[str]:
    """The tools of ``names`` an install on the platform is rewriting now
    (its install lock is held): no sync plans one, or a machine would be
    sent sources the install has not accepted yet. The install's own build
    (``build_on_machine``) sends its tool itself."""
    from core.credentials import catalog_install_registry
    return {n for n in names if catalog_install_registry.lock_for(n).locked()}


async def _tarball_of(name: str) -> mcp_tarball.TarballResult:
    """``name``'s tarball, off the loop (the walk and the gzip took over a
    second on a session start), built once more when an install changed the
    folder under the first build."""
    try:
        return await asyncio.to_thread(mcp_tarball.build_tarball, name)
    except mcp_tarball.TreeChanged:
        return await asyncio.to_thread(mcp_tarball.build_tarball, name)


async def sync_mcps_for_session(
    machine_id: str,
    session_id: str,
    agent_assigned_mcps: list[str],
    *,
    also_wanted: set[str] | frozenset[str] = frozenset(),
    plan_cb: Callable[[dict], Awaitable[None]] | None = None,
    progress_cb: ProgressCb = None,
    force: bool = False,
    early_start_s: float | None = None,
    remove_unwanted: bool | None = None,
    install_only: bool = False,
) -> SyncResult:
    """Align the satellite's installed MCPs with what this session needs.

    ``agent_assigned_mcps`` is the set of MCP names this session requires,
    ``also_wanted`` the ones it leaves out by its context alone
    (``lifted_by_context``): installed and kept current too. The desired
    set is their union with MCPs already in use by any active session on
    the same satellite (an MCP the platform uninstalled while a session
    uses it stays until that session ends).

    At a session start nothing the platform still ships is removed, only
    an MCP it no longer has. With no session (admin "Sync Now", the
    explicit clean-up) an MCP outside the desired set goes too, the desired
    set being the agents whose default target is the machine. Nothing is
    removed while the registry holds no manifest.

    ``plan_cb`` (optional) is invoked once the install plan is known
    (``mcps_to_install`` / ``mcps_to_update`` resolved) so the caller can
    surface the per-MCP rows in any progress UI before the first satellite
    progress event arrives. Caller-owned: this module knows nothing about
    chat_id / install_registry routing.

    Returns SyncResult detailing installs/updates/removes/failures. On any
    failed install, the MCP name is added to ``excluded_names``; callers
    should filter it from the shipped MCP config.

    Each MCP of the plan is judged by its own budget (``INSTALL_BUDGET_S``
    from its own start). With ``early_start_s`` (a session start) the sync
    returns once that much time passed: what ended is in the result, the
    rest in ``still_installing`` (excluded too), and the plan goes on in the
    background with the machine's install lock until it ends; ``watch``
    tells when. A start that finds another plan holding the lock that long
    starts beside it, installing nothing.

    ``remove_unwanted``: ``True`` removes every MCP outside the desired set
    (Sync Now; ``None`` with no session means it too), ``False`` removes
    nothing at all (install ahead). ``install_only`` leaves an outdated MCP
    for a session's start (install ahead at a sign-in).
    """
    from core.remote.remote_execution import get_remote_layer
    from core.remote.satellite_connection import get_connection_manager

    cm = get_connection_manager()
    if not cm.is_connected(machine_id):
        return SyncResult(ok=False, failed={"__connection__": "machine not connected"})

    layer = get_remote_layer()
    # Desired set = this agent's assignment ∪ every other active session's
    # MCPs on the same satellite. Prevents uninstalling an MCP a peer
    # session still depends on (scope-aware GC).
    desired: set[str] = set(agent_assigned_mcps) | set(also_wanted)
    for other_sid, other_info in layer._sessions.items():
        if other_sid == session_id:
            continue
        if other_info.machine_id != machine_id:
            continue
        desired.update(getattr(other_info, "used_mcps", set()) or set())
    desired = _sent_here(cm, machine_id, desired)

    until = None if early_start_s is None else time.monotonic() + early_start_s
    stack = contextlib.AsyncExitStack()
    try:
        await asyncio.wait_for(stack.enter_async_context(cm.get_install_lock(machine_id)),
                               timeout=early_start_s)
    except TimeoutError:
        return await _beside_running_sync(cm, machine_id, desired, force=force)
    handed_off = False   # the lock goes with a plan that ends in the background
    try:
        # Compute satellite state. Fresh verify on every sync to avoid
        # acting on stale capability reports (the heartbeat capability
        # doesn't refresh on each install — a reconnect might though).
        installed = await _fetch_satellite_state(cm, machine_id)

        hashes = await version_hashes_for(desired, force=force)
        gc = (not session_id) if remove_unwanted is None else remove_unwanted
        to_install, to_update, to_remove = _diff(
            desired=desired,
            installed=installed,
            force=force,
            hashes=hashes,
            gc=gc,
        )
        # A removal goes only where the satellite can act on it: a known
        # category (a failed verify reports none), and ``core`` only when the
        # frame says ``remove_any_category``. The flag rides a session
        # start's frame only: Sync Now keeps its reach.
        any_category = not gc
        to_remove = set() if remove_unwanted is False else {
            n for n in to_remove
            if (installed.get(n) or {}).get("category") in _MCP_CATEGORIES
            and (any_category or installed[n]["category"] != "core")
        }
        if install_only:
            to_update = set()
        # A tool the platform is installing right now is never sent: its
        # update waits for the install's end, and one the machine lacks is
        # left out of this session as not installed yet.
        busy = _being_installed(to_install | to_update)
        left_out = to_install & busy
        to_install -= busy
        to_update -= busy

        # What this machine already answered: a recently deferred update, a
        # memoized unsatisfiable install.
        _conn = cm.get_connection(machine_id)
        _sat_ver = getattr(_conn, "satellite_version", "") or ""
        memo_skips = _skip_known(machine_id, to_install, to_update, hashes, _sat_ver,
                                 force=force)
        # A machine takes no tool under a folder name outside its own rule:
        # such a tool is not sent, and reads as failed with what to do.
        for name in sorted(n for n in to_install | to_update if not machine_tools.safe_key(_key(n))):
            to_install.discard(name)
            to_update.discard(name)
            memo_skips[name] = machine_tools.UNSAFE_KEY_CAUSE

        def _with_memo_skips(r: SyncResult) -> SyncResult:
            r.not_installed |= left_out
            r.excluded_names |= left_out
            return _with_memo(r, memo_skips)

        if not (to_install or to_update or to_remove):
            return _with_memo_skips(SyncResult(ok=True))
        logger.info(
            "sync_mcps plan %s: install=%s update=%s remove=%s",
            _placement.log_id(machine_id), sorted(to_install), sorted(to_update), sorted(to_remove),
        )

        # Announce the install plan upfront so the caller's UI can render
        # per-MCP rows even before the first progress event fires.
        if plan_cb and (to_install or to_update):
            await plan_cb({
                "mcps_to_install": sorted(to_install),
                "mcps_to_update": sorted(to_update),
            })

        # Build tarballs for install + update. Docker MCPs stay on the
        # platform; don't ship those.
        specs = []
        planned: list[str] = []
        shipped_hashes: dict[str, str] = {}
        for name in sorted(to_install | to_update):
            manifest = mcp_registry.get_manifest(name)
            if manifest is None:
                logger.warning("sync_mcps: unknown MCP in desired set: %s", name)
                continue
            if not _mt.installs_on_host(manifest.server):
                # Docker MCPs live on the platform host; context-only MCPs
                # (runtime "none") have no server to install anywhere. Skip
                # satellite install for both.
                continue
            try:
                tb = await _tarball_of(name)
            except mcp_tarball.TreeChanged as e:
                logger.warning("%s, twice: it is left out of this session's sync", e)
                continue
            except Exception as e:
                logger.exception("tarball build failed for %s: %s", name, e)
                continue
            shipped_hashes[name] = tb.version_hash
            specs.append(_install_spec(name, manifest, tb))
            planned.append(name)

        command_id = str(uuid.uuid4())
        watch = InstallWatch(machine_id, planned,
                             {name: _install_budget(spec) for name, spec in zip(planned, specs, strict=True)},
                             set(to_install))

        async def _progress_forwarder(ev: dict):
            ev = _event(ev)
            logger.debug(
                "mcp_sync._progress_forwarder: cmd=%s mcp=%s phase=%s pct=%s cb=%s",
                command_id[:8], ev.get("mcp", "-"), ev.get("phase", "-"),
                ev.get("pct", "-"), "set" if progress_cb else "NONE",
            )
            watch.on_event(ev)
            if progress_cb is None or watch.detached:
                return
            r = progress_cb(ev)
            if asyncio.iscoroutine(r):
                await r

        cm.register_install_progress(command_id, _progress_forwarder)
        logger.info("mcp_sync: registered progress cb for command_id=%s", command_id[:8])
        # Critical: pass command_id as the kwarg, NOT just in the msg
        # dict. ``SatelliteConnectionManager.send_command`` overwrites
        # ``msg["command_id"]`` with the kwarg value (or generates a
        # fresh UUID if the kwarg is None). Without this, the
        # `register_install_progress` above keyed on `command_id`, but
        # the satellite would receive a different generated id and emit
        # `mcp_install_progress` events with that one — leaving the
        # callback orphaned and the dashboard install bar stuck at 0%.
        frame = await _frame(cm, machine_id, specs, to_remove, any_category=any_category)
        # The command's own timeout lies past every budget: the watch ends
        # the wait first, each MCP by its own budget.
        ack = asyncio.ensure_future(cm.send_command(
            machine_id, frame, timeout=watch.ceiling(), command_id=command_id,
        ))
        _watches[machine_id] = watch

        ended = False

        def _release() -> None:
            nonlocal ended
            ended = True
            cm.unregister_install_progress(command_id)
            if _watches.get(machine_id) is watch:
                del _watches[machine_id]

        def _conclude() -> SyncResult:
            _release()
            result = _concluded(watch, ack, machine_id=machine_id, to_install=to_install,
                                shipped_hashes=shipped_hashes, sat_ver=_sat_ver)
            watch.settle(result)
            return result

        def _abandon() -> None:
            # A wait that never concluded (its caller cancelled: the person
            # stopped or closed a starting chat; the tail at shutdown): the
            # ack, the listener and the watch go with it, so no later event
            # reaches a dead start. The satellite finishes on its own.
            if ended:
                return
            _release()
            ack.cancel()

        try:
            plan_ended = await _await_plan(watch, ack, until=until)
        except BaseException:
            _abandon()
            raise
        if plan_ended:
            return _with_memo_skips(_conclude())
        # The session starts now with the tools that are ready; the rest go
        # on installing with the lock and the listener.
        watch.detached = True
        handed_off = True
        from core import background
        background.spawn(_finish_in_background(watch, ack, stack, _conclude, _abandon),
                         name=f"plan-{machine_id[:8]}", owner="mcp-sync")
        # One turn of the loop, so the tail is inside its ``finally`` before
        # anything can cancel it: a task cancelled before its first step
        # never runs its body, and the lock would stay held.
        await asyncio.sleep(0)
        logger.info("sync_mcps %s: the session starts while %s still install",
                    _placement.log_id(machine_id), watch.pending())
        return _with_memo_skips(watch.so_far())
    finally:
        if not handed_off:
            await stack.aclose()


async def _frame(cm, machine_id: str, specs: list[dict], to_remove=(), *,
                 any_category: bool = False) -> dict:
    """The ``sync_mcps`` frame for the machine: the installs, the removals
    and what its builds must know of the platform (a hosted install's
    ranges; for the compute machine beside the platform, the platform's own
    egress settings, ``mcp_egress.build_egress_block``)."""
    frame: dict = {
        "type": "sync_mcps",
        "mcps_to_install": specs,
        "mcps_to_remove": sorted(_key(name) for name in to_remove),
    }
    if any_category and to_remove:
        frame["remove_any_category"] = True
    import config as app_config
    if app_config.OTODOCK_CLOUD:
        # A compute machine's own builds block a hosted install's ranges
        # too (the render's --hosted); an older satellite ignores it.
        frame["hosted"] = True
    if cm.builds_own_tools(machine_id):
        from services.mcp import mcp_egress
        block = await asyncio.to_thread(mcp_egress.build_egress_block)
        if block:
            frame["egress"] = block
    return frame


def _concluded(watch: InstallWatch, ack: asyncio.Future, *, machine_id: str,
               to_install: set[str], shipped_hashes: dict[str, str],
               sat_ver: str) -> SyncResult:
    """The plan's result once it ended: the ack's, a failed command's, or,
    past a budget, what the events said (an MCP not ended did not start)."""
    if not ack.done() or ack.cancelled():
        ack.cancel()
        result = watch.so_far()
        result.ok = False
        result.watch = None
        for name in sorted(result.still_installing):
            result.failed[name] = "did not start installing"
        result.still_installing = set()
        return result
    exc = ack.exception()
    if exc is not None:
        logger.error("sync_mcps command failed: %s", exc)
        return SyncResult(ok=False, failed={"__transport__": str(exc)})
    results = _named(ack.result().get("results", {}) or {})
    result = SyncResult()
    for name, info in results.items():
        status = info.get("status", "error")
        if status == "ok":
            # Update landed — clear any deferral backoff for this MCP.
            _deferred_updates.pop((machine_id, name), None)
            _unsatisfiable_installs.pop((machine_id, name), None)
            machine_tools.note_installed(machine_id, name, str(info.get("version_hash") or ""))
            if name in to_install:
                result.installed.append(name)
            else:
                result.updated.append(name)
        elif status == "deferred":
            # Update couldn't swap (old version in use); the satellite kept
            # the working old version. NOT a failure — do not exclude. Back
            # off so we don't re-ship + rebuild it every session until the
            # holding session closes.
            result.deferred[name] = machine_tools.said(info.get("error", ""))
            _mark_update_deferred(machine_id, name)
        elif status == "removed":
            machine_tools.note_removed(machine_id, name)
            result.removed.append(name)
        elif status == "not_found":
            # Nothing to remove — not a failure.
            pass
        else:
            # The machine cuts its own cause to this length: the bound is
            # for one that says more.
            err = machine_tools.said(info.get("error", "unknown"))
            result.failed[name] = err
            result.excluded_names.add(name)
            # A resolver-unsatisfiable failure is DETERMINISTIC for these
            # platform bytes on this machine — memo it so the sessions of the
            # next hour skip the doomed re-attempt (and its bar flash), unless
            # the MCP or the satellite changes or an install succeeds first.
            if (_UNSAT_FAIL_RE.search(err) and not _TRANSIENT_FAIL_RE.search(err)
                    and shipped_hashes.get(name)):
                _unsatisfiable_installs[(machine_id, name)] = {
                    "hash": shipped_hashes[name], "error": err,
                    "satellite_version": sat_ver, "at": time.monotonic(),
                }
        # Pre-warm boot check. "warn:<reason>" = installed but
        # didn't answer initialize in time. Advisory — not excluded.
        warm = info.get("warmup", "")
        if isinstance(warm, str) and warm.startswith("warn:"):
            result.warmup_failed[name] = machine_tools.said(warm[len("warn:"):])
    return result


async def pending_for(machine_id: str, names: set[str], *, install_only: bool = False) -> set[str]:
    """The tools of ``names`` a sync would install on the machine now: those
    it lacks, and those it holds outdated unless ``install_only``. A read of
    the machine's own state; the sync checks again under the lock. When the
    machine did not answer, the list it reported at sign-in knows no hash:
    the missing tools alone, never every tool read as outdated."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    names = _sent_here(cm, machine_id, names)
    installed = await _fetch_satellite_state(cm, machine_id)
    hashes = await version_hashes_for(names)
    to_install, to_update, _ = _diff(desired=set(names), installed=installed, hashes=hashes)
    busy = _being_installed(to_install | to_update)
    to_install -= busy
    to_update -= busy
    if install_only or any(v.get("unverified") for v in installed.values()):
        return to_install
    return to_install | to_update


class NoBuildMachine(RuntimeError):
    """No compute machine is connected to build a tool on: the install is
    refused with the sentence and nothing of it ran."""


@dataclass
class Built:
    """How an install's build on a compute machine ended: ``log`` is the
    cause of a failure, ``resolved_version`` the version the build read back
    (a plain version, ``""`` for a source with none) and ``version_hash``
    the machine's hash of the folder it now holds."""
    ok: bool
    log: str = ""
    resolved_version: str = ""
    version_hash: str = ""
    machine_id: str = ""


_SYSTEM_LISTS = ("debian", "ubuntu", "rhel", "arch", "macos_brew")


def _build_spec(name: str, mcp_dir: Path, manifest_data: dict, source: str) -> dict:
    """The install spec of a folder the registry does not know yet, from its
    own manifest: ``_install_spec``'s shape, keyed by the folder as the
    machine keeps it, with no expected hash (the pin the machine writes
    after the build changes it) and ``pin`` for a source whose version is
    read back. Blocking (packs the folder)."""
    server = manifest_data.get("server") or {}
    reqs = manifest_data.get("system_requirements") or {}
    parsed = mcp_installer.parse_source(source) if source else None
    return {
        "name": Path(mcp_dir).name,
        "category": mcp_installer.COMMUNITY,
        "runtime": server.get("runtime", ""),
        "source": source,
        "source_build": list(server.get("source_build") or []),
        "manifest_data": {"name": name, "server_name": manifest_data.get("server_name") or "",
                          "category": mcp_installer.COMMUNITY, "version": manifest_data.get("version") or ""},
        "tarball_b64": mcp_tarball.pack_folder(mcp_dir, manifest_data.get("data_dirs")),
        "version_hash": "",
        "pin": bool(parsed and parsed.registry in mcp_pin.PINNED_REGISTRIES),
        "system_requirements": {
            **{key: list(reqs.get(key) or []) for key in _SYSTEM_LISTS},
            "node_min": reqs.get("node_min") or "", "notes": reqs.get("notes") or "",
        },
    }


async def build_on_machine(name: str, mcp_dir: Path, manifest_data: dict, source: str, *,
                           progress_cb: ProgressCb = None,
                           on_wait: Callable[[], Awaitable[None]] | None = None) -> Built:
    """An install's build of ``name``. The platform builds no tool: the
    folder the install copied in (``mcp_dir``, not in the registry yet) goes
    as a one-tool ``sync_mcps`` to the compute machine that builds for the
    install (``building_machine``), which builds it into ``<tool>.new`` and
    swaps it in only when the build succeeded, so a failure leaves the
    machine's installed version as it was. ``source`` is the source to
    install (pinned by the caller where it could resolve a version).

    With no such machine connected it waits for one, at most
    ``concurrency.NO_COMPUTE_WAIT_S`` (``on_wait`` is told first), then
    raises ``NoBuildMachine``. A build that failed, ran past its budget or
    lost its link answers ``Built.ok`` False with the cause, never raises.
    Under the machine's install lock, as every sync is."""
    from core import concurrency
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if not cm.building_machine():
        if on_wait is not None:
            await on_wait()
        await concurrency.wait_for_compute(lambda: bool(cm.building_machine()))
    machine_id = cm.building_machine()
    if not machine_id:
        raise NoBuildMachine(_placement.NO_COMPUTE_SENTENCE)
    spec = await asyncio.to_thread(_build_spec, name, Path(mcp_dir), manifest_data, source)
    command_id = str(uuid.uuid4())

    async def _forward(ev: dict) -> None:
        r = progress_cb(_event(ev)) if progress_cb is not None else None
        if asyncio.iscoroutine(r):
            await r

    async with cm.get_install_lock(machine_id):
        cm.register_install_progress(command_id, _forward)
        try:
            ack = await cm.send_command(
                machine_id, await _frame(cm, machine_id, [spec]),
                timeout=_install_budget(spec) + VERIFY_BUDGET_S, command_id=command_id)
        except RuntimeError as e:
            logger.warning("the build of %s on %s did not end: %s", name, _placement.log_id(machine_id), e)
            return Built(False, f"The build on the compute machine did not finish: {e}",
                         machine_id=machine_id)
        finally:
            cm.unregister_install_progress(command_id)
    info = (ack.get("results") or {}).get(spec["name"])
    if not isinstance(info, dict) or info.get("status") != "ok":
        cause = machine_tools.said((info or {}).get("error")) or "The compute machine did not build the tool."
        return Built(False, cause, machine_id=machine_id)
    version_hash = str(info.get("version_hash") or "")
    machine_tools.note_installed(machine_id, name, version_hash, category=mcp_installer.COMMUNITY)
    return Built(True, "", mcp_installer.plain_version(info.get("resolved_version")),
                 version_hash, machine_id)


async def remove_gone(machine_id: str) -> list[str]:
    """Take away from the machine the tools the platform no longer has (an
    uninstalled tool's folder), for a machine no session's sync reaches: the
    compute machine beside the platform. Nothing while the registry holds no
    manifest, and never a tool an install is writing (``_gone``). Here
    "no longer has" means uninstalled, never a tool a scan lost (a tools
    folder that was not mounted, a manifest that did not parse): an
    uninstall deletes the tool's state row, so a name that still has one is
    kept, with what the tool wrote on the machine, and named in one
    warning. Answers the names removed."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if not cm.is_connected(machine_id):
        return []
    async with cm.get_install_lock(machine_id):
        installed = await _fetch_satellite_state(cm, machine_id)
        gone = {n for n in _gone(installed) if installed[n].get("category") in _MCP_CATEGORIES}
        if gone:
            from storage.mcp import mcp_store
            rows = await asyncio.to_thread(mcp_store.get_all_mcp_states)
            lost = sorted(n for n in gone if n in rows)
            if lost:
                logger.warning("sync_mcps %s: %s not removed. The registry lost them and nobody uninstalled them: "
                               "check the platform's tools folder", _placement.log_id(machine_id), ", ".join(lost))
            gone -= set(lost)
        if not gone:
            return []
        try:
            ack = await cm.send_command(machine_id, await _frame(cm, machine_id, [], gone),
                                        timeout=VERIFY_BUDGET_S)
        except RuntimeError as e:
            logger.warning("sync_mcps %s: removing %s failed: %s", _placement.log_id(machine_id), sorted(gone), e)
            return []
    removed = sorted(n for n, info in _named(ack.get("results") or {}).items()
                     if isinstance(info, dict) and info.get("status") == "removed")
    for name in removed:
        machine_tools.note_removed(machine_id, name)
    if removed:
        logger.info("sync_mcps %s: removed %s, which the platform no longer has",
                    _placement.log_id(machine_id), removed)
    return removed


async def forget_never_installed(name: str) -> None:
    """A first install failed after its scan had made the tool's state row.
    The platform never came to have the tool: the row goes, or
    ``remove_gone`` would read the tool as installed and leave its copy on
    the machine that built it. Best effort."""
    from storage.mcp import mcp_store
    with contextlib.suppress(Exception):
        await asyncio.to_thread(mcp_store.delete_mcp_all_data, name)


async def _fetch_satellite_state(cm, machine_id: str) -> dict[str, dict]:
    """Run sync_mcps_verify and return {mcp_name: {version_hash, healthy}},
    by the registry's names (``_named``)."""
    try:
        ack = await cm.send_command(machine_id, {
            "type": "sync_mcps_verify",
        }, timeout=15.0)
        mcps = _named(ack.get("mcps", {}) or {})
        machine_tools.note_verify(machine_id, mcps)
        return mcps
    except RuntimeError as e:
        logger.warning("sync_mcps_verify failed: %s", e)
        # Fall back to the last-reported capabilities — better than nothing.
        # They carry no hash (``unverified``): the session's sync reads each
        # as outdated, as before; the plan of an ahead run never does.
        conn = cm.get_connection(machine_id)
        if conn:
            caps_installed = conn.capabilities.get("installed_mcps", [])
            return _named({name: {"version_hash": "", "healthy": True, "unverified": True}
                           for name in caps_installed})
        return {}


def _diff(
    desired: set[str], installed: dict[str, dict], *, force: bool = False,
    hashes: dict[str, str] | None = None, gc: bool = False,
) -> tuple[set[str], set[str], set[str]]:
    """Split desired vs installed into (install, update, remove).

    install: in desired but not installed (or unhealthy)
    update:  in desired, installed, but version_hash differs from current
    remove:  installed, not desired, and no longer shipped by the platform;
             with ``gc`` (admin Sync Now) shipped ones too (``_gone``).

    ``hashes`` is the current version hash per name (``version_hashes_for``,
    computed off the loop); without it each one is read here, from the cache
    when it can be.
    """
    to_install: set[str] = set()
    to_update: set[str] = set()

    for name in desired:
        info = installed.get(name)
        manifest = mcp_registry.get_manifest(name)
        if manifest is None:
            continue
        if not _mt.installs_on_host(manifest.server):
            # Docker = platform-hosted; "none" = context-only (no server).
            # Neither ever installs on a satellite.
            continue
        if info is None or not info.get("healthy", False):
            to_install.add(name)
            continue
        # Compare version_hash against what the platform has now.
        if hashes is not None:
            current_hash = hashes.get(name, "")
        else:
            try:
                current_hash = _version_hash(manifest, force=force)
            except (OSError, ValueError):
                current_hash = ""
        if force or (current_hash and current_hash != info.get("version_hash", "")):
            to_update.add(name)

    return to_install, to_update, _gone(installed, desired, gc=gc)


def _gone(installed: dict[str, dict], desired=frozenset(), *, gc: bool = False) -> set[str]:
    """The tools a machine holds (``installed``, its verify) that a sync
    takes away: outside ``desired`` and no longer on the platform, with
    ``gc`` the ones it still has too. Nothing while the registry holds no
    manifest (a failed scan must not wipe a working machine), and never a
    tool an install is writing now: the machine that built it holds it
    before the registry knows it. Tools only: a machine's verify also names
    the folders an install or a copy staged, and a machine removes nothing
    under a name it would not install under (``machine_tools.safe_key``)."""
    known = mcp_registry.get_all_manifests()
    if not known:
        return set()
    gone = {name for name in installed if name not in desired and (gc or name not in known)
            and machine_tools.safe_key(_key(name))}
    return gone - _being_installed(gone)


def _install_spec(name: str, manifest, tb) -> dict:
    """One entry of ``mcps_to_install``, keyed by the tool's folder as the
    machine keeps it (``machine_tools.key_of``): what the satellite's
    installer needs beside the tarball, ``source_build`` included (the
    packages the manifest allows to build from source; a satellite older
    than 0.5.130 ignores the key and installs the way its own vendored
    installer does)."""
    return {
        "name": machine_tools.key_of(manifest),
        "category": manifest.category,
        "runtime": manifest.server.runtime,
        "source": manifest.server.source,
        "source_build": list(getattr(manifest.server, "source_build", None) or []),
        "manifest_data": _manifest_to_dict(manifest),
        "tarball_b64": tb.tarball_b64,
        "version_hash": tb.version_hash,
        # Optional: system_requirements forwarded so the satellite
        # installer runs its pre-install checks.
        "system_requirements": {
            "debian": manifest.system_requirements.debian,
            "ubuntu": manifest.system_requirements.ubuntu,
            "rhel": manifest.system_requirements.rhel,
            "arch": manifest.system_requirements.arch,
            "macos_brew": manifest.system_requirements.macos_brew,
            "node_min": manifest.system_requirements.node_min,
            "notes": manifest.system_requirements.notes,
        },
    }


def _manifest_to_dict(manifest) -> dict:
    """Minimal serialization of a manifest for the satellite.

    The satellite doesn't re-parse the full manifest; it just needs the
    basics so future versioning can evolve without a breaking schema
    change. For now we ship name, server_name, category, version — the
    rest is already baked into the tarball.
    """
    return {
        "name": manifest.name,
        "server_name": manifest.server_name,
        "category": manifest.category,
        "version": manifest.version,
    }
