"""Running a batch of operations and committing its one save.

The parent (``handle_write_call``) reads the batch, answers help, validates
every operation against the catalogue before any work, resolves the paths
through the proxy, and hands a ``WriteCall`` to a worker. The worker's core
loads the document, runs the batch (``WriteCall.run``: every operation, even
after a failure, so every failure is listed) and commits (``WriteCall.commit``):
the bytes are saved into the call's work folder, the readback is built, the
main file lands atomically beneath the mount, side outputs land after it, and
the after-write checks read the saved bytes. Nothing is saved when an
operation failed, unless the caller passed ``best_effort``; the parent
previews and posts file-written only for what was saved.

A format with a render phase between the batch and the commit (a workbook's
recalculation runs an office engine, which a worker may never start) passes
``render`` and ``finish`` to ``handle_write_call``: worker A stages the
save, the parent renders, worker B splices, checks and commits, all inside
one work folder and one time budget.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import kept_batches
from catalog import HELP_NAME_KEYS, OP_KEY, Catalog, KeySpec
from results import (
    BatchOutcome,
    Code,
    Deferred,
    NoOp,
    OpCtx,
    OpError,
    OpResult,
    ResolvedPath,
    Staged,
    Status,
    WriteReport,
    kept_for_fix,
    render_outcome,
    render_rejected,
)

logger = logging.getLogger(__name__)

STAGED_NAME = "staged"
WORK_PREFIX = "file-tools-work-"
_COPY_CHUNK = 1024 * 1024
# After-write checks read the saved bytes in the worker; past these the
# save stands and the rest of the checks are skipped with a line saying so.
CHECK_BUDGET_S = 20.0
CHECK_MAX_BYTES = 100 * 1024 * 1024
OPERATIONS_KEY = "operations"


# ---------------------------------------------------------------------------
# Reading the batch
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    """A call's operations as the caller numbered them: the help items, the
    operations, the items that are not operations at all, and how many
    operations the caller sent (help items aside)."""

    help_ops: list[dict]
    ops: list[tuple[int, dict]]
    problems: list[OpError]
    total: int


def _items(raw: Any) -> tuple[list[tuple[int, Any]], list[OpError]]:
    hint = 'Send a list of objects such as [{"op": "<name>", ...}].'
    if raw is None:
        return [], []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [], [OpError(Code.BAD_ITEM, "operations could not be read as JSON", hint=hint, index=0)]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [], [OpError(Code.BAD_ITEM, "operations must be a list of objects", hint=hint, index=0)]
    items: list[tuple[int, Any]] = []
    problems: list[OpError] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                problems.append(OpError(Code.BAD_ITEM, "this item could not be read as JSON", hint=hint,
                                        index=index))
                continue
        if isinstance(item, dict):
            items.append((index, item))
        else:
            problems.append(OpError(Code.BAD_ITEM, "this item is not an operation object",
                                    hint='Send {"op": "<name>", ...keys}.', index=index))
    return items, problems


def raw_items(raw: Any) -> list[Any]:
    """The batch as the caller sent it, one item per number the refusal
    gives: a JSON string read, each item that is a JSON string read, any
    other item kept as it came (a refusal names it, a fix replaces it)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [raw]
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, list):
        return [] if raw is None else [raw]
    out = []
    for item in raw:
        if isinstance(item, str):
            with contextlib.suppress(ValueError):
                item = json.loads(item)
        out.append(item)
    return out


def prepare_batch(raw: Any, catalog: Catalog) -> Prepared:
    """The batch read as the caller sent it: a JSON string, a list of JSON
    strings or one object are accepted; every item keeps its index."""
    items, problems = _items(raw)
    help_ops = [item for _, item in items if catalog.is_help(item)]
    ops = [(index, item) for index, item in items if not catalog.is_help(item)]
    return Prepared(help_ops, ops, problems, len(ops) + len(problems))


def batch_ops(items: list[Any], catalog: Catalog) -> list[tuple[int, dict]]:
    """The operations of ``items`` that resolve, canonical, help aside: what
    a handler that picks its workers routes on."""
    return catalog.resolve_batch(prepare_batch(items, catalog).ops)[0]


# ---------------------------------------------------------------------------
# Key recording (the key-coverage contract; off in production)
# ---------------------------------------------------------------------------


@dataclass
class KeyLog:
    read: set[str] = field(default_factory=set)
    whole: list[str] = field(default_factory=list)


class KeyRecorder(dict):
    """An operation that records which keys a handler reads. An overridden
    ``__iter__`` routes ``dict(op)``, ``{**op}``, ``f(**op)``, ``op | {}`` and
    ``update`` through ``keys()``, and ``json.dumps`` through ``items()``, so
    whole-operation access is recorded too. ``==`` and ``type(op) is dict``
    are not seen."""

    def __init__(self, data: Mapping, log: KeyLog, prefix: str = "") -> None:
        super().__init__(data)
        self._log = log
        self._prefix = prefix

    def _read(self, key: Any) -> None:
        self._log.read.add(f"{self._prefix}{key}")

    def _whole(self, how: str) -> None:
        self._log.whole.append(f"{self._prefix or 'op.'}{how}()")

    def __getitem__(self, key):
        self._read(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._read(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._read(key)
        return super().__contains__(key)

    def pop(self, key, *default):
        self._read(key)
        return super().pop(key, *default)

    def setdefault(self, key, default=None):
        self._read(key)
        return super().setdefault(key, default)

    def __iter__(self):
        self._whole("__iter__")
        return super().__iter__()

    def keys(self):
        self._whole("keys")
        return super().keys()

    def items(self):
        self._whole("items")
        return super().items()

    def values(self):
        self._whole("values")
        return super().values()

    def copy(self):
        self._whole("copy")
        return dict(super().items())


def wrap_recorder(values: Mapping, specs: Mapping[str, KeySpec], log: KeyLog, prefix: str = "") -> KeyRecorder:
    data = {}
    for key, value in values.items():
        spec = specs.get(key)
        if spec is not None and isinstance(value, dict) and spec.fields is not None:
            value = wrap_recorder(value, {f.name: f for f in spec.fields}, log, f"{prefix}{key}.")
        elif spec is not None and isinstance(value, list) and spec.item is not None:
            fields = {f.name: f for f in spec.item}
            value = [wrap_recorder(v, fields, log, f"{prefix}{key}[].") if isinstance(v, dict) else v
                     for v in value]
        data[key] = value
    return KeyRecorder(data, log, prefix)


def declared_names(keys: tuple[KeySpec, ...], prefix: str = "") -> set[str]:
    """Every key name an operation may read, nested keys dotted the way the
    recorder writes them (``header.fill``, ``series[].name``)."""
    names = {OP_KEY} if not prefix else set()
    for key in keys:
        names.add(f"{prefix}{key.name}")
        if key.fields is not None:
            names |= declared_names(key.fields, f"{prefix}{key.name}.")
        if key.item is not None:
            names |= declared_names(key.item, f"{prefix}{key.name}[].")
    return names


# ---------------------------------------------------------------------------
# Running the batch (worker side)
# ---------------------------------------------------------------------------


def discard_deferred(deferred: list[Deferred]) -> None:
    for item in deferred:
        with contextlib.suppress(OSError):
            os.unlink(item.temp)


def run_batch(ops: list[tuple[int, dict]], catalog: Catalog, job: Any, *, best_effort: bool = False,
              create_new: bool = False, finalize: Callable[[Any, BatchOutcome], list[str]] | None = None,
              rejected: list[OpResult] | tuple = (), pre_notes: Mapping[int, list[str]] | None = None,
              work_dir: str | None = None, main_path: str = "", record_keys: bool = False,
              total: int | None = None) -> BatchOutcome:
    """Every operation run in the caller's order; each ends applied, no-op
    or failed. ``rejected`` are operations the parent already refused (they
    count as failed, and changed nothing, as does an operation refused
    before its handler ran); ``finalize(job, outcome)`` returns the
    batch-level warnings and runs only when the batch will commit."""
    outcome = BatchOutcome(results=[], total=len(ops) + len(rejected) if total is None else total,
                           best_effort=best_effort, create_new=create_new)
    results: list[OpResult] = [replace(r, unchanged=True) for r in rejected]
    # Only an operation whose handler ran can have caused a later failure.
    executed: set[int] = set()
    sequence = itertools.count(1)
    pre_notes = pre_notes or {}
    for index, raw in ops:
        raw_name = raw.get(OP_KEY, "") if isinstance(raw, dict) else ""
        try:
            spec, op, notes = catalog.resolve(raw, require_resolved_paths=True)
        except OpError as exc:
            refused = OpResult.from_error(exc, op=exc.op or str(raw_name), index=index)
            refused.unchanged = True
            results.append(refused)
            continue
        pending: list[Deferred] = []
        ctx = OpCtx(index, spec.name, work_dir=work_dir, main_path=main_path, deferred=pending,
                    earlier=outcome.deferred, sequence=sequence)
        log = KeyLog()
        target = wrap_recorder(op, {k.name: k for k in spec.keys}, log) if record_keys else op
        result = OpResult(index, spec.name, Status.APPLIED, side_only=spec.side_only)
        executed.add(index)
        try:
            spec.handler(job, ctx, target)
        except NoOp as exc:
            result.status, result.message = Status.NOOP, exc.reason
        except OpError as exc:
            result.status, result.code = Status.FAILED, exc.code
            result.message, result.hint, result.location = exc.message, exc.hint, exc.location
            result.unchanged = exc.unchanged
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001  # any handler fault fails its op; the batch reports every failure
            result.status, result.code = Status.FAILED, Code.ERROR
            result.message = str(exc) or exc.__class__.__name__
        result.notes = [*pre_notes.get(index, []), *notes, *ctx.notes]
        result.warnings = ctx.warnings
        if result.status is Status.APPLIED:
            outcome.deferred.extend(pending)
            outcome.removed.extend(ctx.removed_parts)
        else:
            discard_deferred(pending)
        if record_keys:
            outcome.keys_read[index] = set(log.read)
            if log.whole:
                outcome.whole_access[index] = list(log.whole)
        results.append(result)
    results.sort(key=lambda r: r.index)
    first_failure = None
    for result in results:
        if result.status is not Status.FAILED or result.index not in executed:
            continue
        if first_failure is None:
            first_failure = result.index
        else:
            result.after = first_failure
    outcome.results = results
    if outcome.should_commit and finalize is not None:
        try:
            outcome.warnings.extend(finalize(job, outcome) or [])
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001  # a batch-level step never fails the save
            outcome.warnings.append(f"a final step could not run: {exc or exc.__class__.__name__}")
    if not outcome.should_commit:
        discard_deferred(outcome.deferred)
        outcome.deferred = []
    return outcome


# ---------------------------------------------------------------------------
# Committing the save (worker side)
# ---------------------------------------------------------------------------


def stage_write(outcome: BatchOutcome, *, work_dir: str, save: Callable[[Any], None] | None = None,
                save_to: Callable[[str], None] | None = None,
                readback: Callable[[], str] | None = None, suffix: str = "",
                facts: dict[str, Any] | None = None, baseline: Any = None) -> Staged:
    """The first half of a commit: the document saved into the work folder
    (``save(fileobj)`` gets a binary file object; ``save_to(path)`` a name
    ending in ``suffix``, the target's own, for a library that writes by
    name and reads the format off it) and the readback built, before
    anything lands."""
    if not outcome.should_save_main:
        return Staged(outcome, None, "", dict(facts or {}), baseline)
    if not work_dir:
        raise RuntimeError("a write needs its work folder")
    staged_path = os.path.join(work_dir, STAGED_NAME + suffix)
    if save_to is not None:
        save_to(staged_path)
    elif save is not None:
        with open(staged_path, "wb") as fh:
            save(fh)
    else:
        raise RuntimeError("stage_write needs save or save_to")
    return Staged(outcome, staged_path, readback() if readback is not None else "", dict(facts or {}), baseline)


def land_deferred(deferred: list[Deferred]) -> tuple[list[str], list[str]]:
    """Each side output copied to its destination beneath the mount, no
    link followed; (the landed paths, a warning per one that failed)."""
    import shared

    written: list[str] = []
    warnings: list[str] = []
    for item in deferred:
        try:
            with open(item.temp, "rb") as src, shared.safe_open_write(item.dest) as fh:
                shutil.copyfileobj(src, fh, _COPY_CHUNK)
            written.append(item.dest)
        except OSError as exc:
            warnings.append(f"the side file {shared._to_agents_relative(item.dest)} could not be written: {exc}")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(item.temp)
    return written, warnings


def _after_write_checks(staged_path: str, path: str, fmt: str, baseline: list | None,
                        check_modules: tuple[str, ...], budget_s: float,
                        removed: Iterable[str] = (), ops: Iterable[str] = ()) -> tuple[list, bool]:
    import checks

    checks.load_check_modules(check_modules)
    if not checks.has_checks(fmt, checks.AFTER_WRITE):
        return [], False
    size = os.path.getsize(staged_path)
    if size > CHECK_MAX_BYTES:
        return [checks.Finding(checks.Severity.INFO, checks.SKIPPED_CHECK,
                               message=f"the file is {size // (1024 * 1024)} MB, so the after-write checks "
                                       "were skipped", fix="Run check_document on it.")], True
    with open(staged_path, "rb") as fh:
        ctx = checks.CheckContext(path, fmt, fileobj=fh, removed=removed, ops=ops)
        try:
            return checks.run_checks(ctx, phase=checks.AFTER_WRITE, baseline=baseline, budget_s=budget_s), True
        finally:
            ctx.close()


def commit_staged(staged: Staged, path: str, *, original: tuple[int, ...] | None, kind: str, fmt: str,
                  existed: bool = True, transform: Callable[[str], None] | None = None,
                  baseline: list | None = None, hints: str = "", check_modules: tuple[str, ...] = (),
                  check_budget_s: float = CHECK_BUDGET_S, warnings: list[str] | None = None,
                  readback: Callable[[str], str] | None = None) -> WriteReport:
    """The second half: ``transform(staged_path)`` (a splice of computed
    values), the main file landed atomically, the side files landed, the
    after-write checks on the saved bytes, the text rendered.
    ``readback(staged_path)``, when given, is built after the transform and
    replaces the staged readback (a recalculated grid shows its values).
    ``original`` is the target as the parent found it (``_stat``; ``()``
    for no file): when the target has changed since, nothing lands. It has
    no default: a caller says what it compares (``None`` compares nothing),
    so a finish that skips ``WriteCall`` cannot drop the guard unseen."""
    import shared

    outcome = staged.outcome
    baseline = staged.baseline if baseline is None else baseline
    if not outcome.should_commit:
        discard_deferred(outcome.deferred)
        text = render_outcome(outcome, kind=kind, path=path, saved=False, existed=existed, hints=hints,
                              extra_warnings=warnings)
        return WriteReport(text, False, [], refused=bool(outcome.failed and not outcome.best_effort))
    if original is not None and staged.staged_path and (_stat(path) or ()) != original:
        discard_deferred(outcome.deferred)
        return _nothing_saved(path, existed, "It changed while this edit ran (something else saved it), "
                              "so this edit would have undone that save. Read it again and redo the edit.")
    saved = False
    readback_text = staged.readback
    if staged.staged_path:
        if transform is not None:
            transform(staged.staged_path)
        if readback is not None:
            readback_text = readback(staged.staged_path)
        with open(staged.staged_path, "rb") as src, shared.safe_open_write(path) as fh:
            shutil.copyfileobj(src, fh, _COPY_CHUNK)
        saved = True
    side_written, side_warnings = land_deferred(outcome.deferred)
    findings: list = []
    ran = False
    if saved:
        applied = {result.op for result in outcome.results if result.status is Status.APPLIED}
        findings, ran = _after_write_checks(staged.staged_path, path, fmt, baseline, check_modules,
                                            check_budget_s, outcome.removed, applied)
    text = render_outcome(outcome, kind=kind, path=path, saved=saved, existed=existed, findings=findings,
                          checks_ran=ran, readback=readback_text, hints=hints, side_written=side_written,
                          extra_warnings=[*(warnings or []), *side_warnings])
    return WriteReport(text, saved, side_written)


def _nothing_saved(path: str, existed: bool, reason: str) -> WriteReport:
    import shared

    state = "is unchanged" if existed else "was not created"
    return WriteReport(f"Nothing saved: {shared._to_agents_relative(path)} {state}. {reason}", False, [])


def commit_write(path: str, outcome: BatchOutcome, *, kind: str, fmt: str, work_dir: str,
                 save: Callable[[Any], None] | None = None, save_to: Callable[[str], None] | None = None,
                 readback: Callable[[], str] | None = None, baseline: list | None = None, hints: str = "",
                 existed: bool = True, transform: Callable[[str], None] | None = None,
                 check_modules: tuple[str, ...] = (), original: tuple[int, ...] | None) -> WriteReport:
    """Both halves in one worker."""
    staged = stage_write(outcome, work_dir=work_dir, save=save, save_to=save_to, readback=readback,
                         suffix=os.path.splitext(path)[1])
    return commit_staged(staged, path, kind=kind, fmt=fmt, existed=existed, transform=transform,
                         baseline=baseline, hints=hints, check_modules=check_modules, original=original)


@dataclass
class WriteCall:
    """What the parent hands a write core: the resolved path, the canonical
    operations with the caller's indexes, the flags, the operations refused
    before the worker (best_effort), the notes the parent's resolve made,
    the call's work folder, the check modules the parent registered, the
    tool's other arguments (``extra``; a path among them resolved), and the
    target as the parent found it while it held the path's lock
    (``original``: the commit refuses when the target changed since)."""

    path: str
    ops: list[tuple[int, dict]]
    create_new: bool = False
    best_effort: bool = False
    existed: bool = True
    rejected: list[OpResult] = field(default_factory=list)
    pre_notes: dict[int, list[str]] = field(default_factory=dict)
    work_dir: str = ""
    check_modules: tuple[str, ...] = ()
    total: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    original: tuple[int, ...] | None = None

    @property
    def edit_existing(self) -> bool:
        """True when the core loads the file; False when it starts from its
        blank template."""
        return self.existed and not self.create_new

    def run(self, catalog: Catalog, job: Any, *, finalize: Callable[[Any, BatchOutcome], list[str]] | None = None,
            record_keys: bool = False) -> BatchOutcome:
        return run_batch(self.ops, catalog, job, best_effort=self.best_effort, create_new=self.create_new,
                         finalize=finalize, rejected=self.rejected, pre_notes=self.pre_notes,
                         work_dir=self.work_dir, main_path=self.path, record_keys=record_keys,
                         total=self.total)

    def stage(self, outcome: BatchOutcome, *, save: Callable[[Any], None] | None = None,
              save_to: Callable[[str], None] | None = None, readback: Callable[[], str] | None = None,
              facts: dict[str, Any] | None = None, baseline: Any = None) -> Staged:
        return stage_write(outcome, work_dir=self.work_dir, save=save, save_to=save_to, readback=readback,
                           suffix=os.path.splitext(self.path)[1], facts=facts, baseline=baseline)

    def commit_staged(self, staged: Staged, *, kind: str, fmt: str, transform: Callable[[str], None] | None = None,
                      baseline: list | None = None, hints: str = "", warnings: list[str] | None = None,
                      readback: Callable[[str], str] | None = None) -> WriteReport:
        return commit_staged(staged, self.path, kind=kind, fmt=fmt, existed=self.existed, transform=transform,
                             baseline=baseline, hints=hints, check_modules=self.check_modules, warnings=warnings,
                             readback=readback, original=self.original)

    def commit(self, outcome: BatchOutcome, *, kind: str, fmt: str, save: Callable[[Any], None] | None = None,
               save_to: Callable[[str], None] | None = None, readback: Callable[[], str] | None = None,
               baseline: list | None = None, hints: str = "",
               transform: Callable[[str], None] | None = None) -> WriteReport:
        return commit_write(self.path, outcome, kind=kind, fmt=fmt, work_dir=self.work_dir, save=save,
                            save_to=save_to, readback=readback, baseline=baseline, hints=hints,
                            existed=self.existed, transform=transform, check_modules=self.check_modules,
                            original=self.original)


# ---------------------------------------------------------------------------
# The parent
# ---------------------------------------------------------------------------


@dataclass
class Rendered:
    """What the parent's render phase hands worker B: its result (a
    recalculated copy's path, say) or, when it gave nothing, why (a line in
    the result's warnings)."""

    payload: Any = None
    note: str = ""


# A write call ends within this, the wait for a worker included (the tool
# times out at 600 s, and an agent that sees a timeout resends operations
# that are not idempotent). With a render phase: worker A by
# PIPELINE_BUDGET_S - RENDER_BUDGET_S - FINISH_MIN_S, the render phase at
# most RENDER_BUDGET_S, worker B what is left, never started with less than
# FINISH_MIN_S.
PIPELINE_BUDGET_S = 540.0
RENDER_BUDGET_S = 90.0
FINISH_MIN_S = 120.0


def _stat(path: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class _PathLock:
    def __init__(self) -> None:
        import asyncio

        self.lock = asyncio.Lock()
        self.users = 0


_PATH_LOCKS: dict[str, _PathLock] = {}


@contextlib.asynccontextmanager
async def _path_lock(path: str, until: float):
    """One write at a time per target in this server: two workers widen the
    window in which a second write to the same file could land between them
    and be undone. Yields False when the path did not come free by
    ``until`` (monotonic), so the wait counts in the call's budget."""
    import asyncio
    import time

    entry = _PATH_LOCKS.get(path)
    if entry is None:
        entry = _PATH_LOCKS[path] = _PathLock()
    entry.users += 1
    try:
        try:
            await asyncio.wait_for(entry.lock.acquire(), max(0.0, until - time.monotonic()))
        except TimeoutError:
            yield False
            return
        try:
            yield True
        finally:
            entry.lock.release()
    finally:
        entry.users -= 1
        if not entry.users:
            _PATH_LOCKS.pop(path, None)


def _joined(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


async def _render_phase(render: Callable[[Staged, WriteCall, float], Any], staged: Staged, call: WriteCall,
                        budget_s: float) -> Rendered:
    """The parent's render phase, cancelled at ``budget_s``. ``render`` must
    be cancellable and end its subprocess when cancelled (the LibreOffice
    slots of render_engine do); a value it returns after its budget is
    discarded, so a late result never reaches worker B."""
    import asyncio
    import time

    if staged.staged_path is None:
        return Rendered()
    if budget_s <= 0:
        return Rendered(note="the render phase was skipped: the call ran out of time")
    late = Rendered(note=f"the render phase ran past {budget_s:.3g} s and was skipped")
    began = time.monotonic()
    try:
        result = await asyncio.wait_for(render(staged, call, budget_s), budget_s)
    except TimeoutError:
        return late
    except Exception as exc:  # noqa: BLE001  # a failed render phase still commits, with a line saying so
        return Rendered(note=f"the render phase failed: {exc or exc.__class__.__name__}")
    if time.monotonic() - began > budget_s:
        return late
    return result if isinstance(result, Rendered) else Rendered(payload=result)


def _out_of_time(staged: Staged, call: WriteCall) -> WriteReport:
    discard_deferred(staged.outcome.deferred)
    return _nothing_saved(call.path, call.existed, "The call ran out of time before it could save, because the "
                          "server is busy. Send the batch again.")


async def _two_workers(stage: Callable[[WriteCall], Staged | WriteReport],
                       render: Callable[[Staged, WriteCall, float], Any],
                       finish: Callable[[Staged, WriteCall, Rendered], WriteReport], call: WriteCall,
                       advice: str | None, started: float) -> WriteReport:
    """Worker A applies the batch and stages the save; the parent runs the
    render phase (render_engine, which a worker may never call); worker B
    splices, checks and commits. Worker A may commit at once instead (it
    returns its WriteReport: a batch with nothing to render pays for one
    worker). Each end is absolute, the wait for a worker included, so the
    call ends within PIPELINE_BUDGET_S; worker B never starts with less
    than FINISH_MIN_S left."""
    import time

    from isolation import run_parse

    end = started + PIPELINE_BUDGET_S
    staged = await run_parse(stage, call, _advice=advice, _deadline_at=end - RENDER_BUDGET_S - FINISH_MIN_S)
    if isinstance(staged, WriteReport):
        return staged
    rendered = await _render_phase(render, staged, call, min(RENDER_BUDGET_S, end - time.monotonic() - FINISH_MIN_S))
    if end - time.monotonic() < FINISH_MIN_S:
        return _out_of_time(staged, call)
    return await run_parse(finish, staged, call, rendered, _advice=advice, _deadline_at=end)


# Writes in a thread whose caller was cancelled, held until they end.
_THREAD_WRITES: set = set()


def _thread_write_settled(task: Any) -> None:
    """A finished thread write leaves the set; its exception is logged here,
    since a cancelled caller is no longer waiting for it."""
    _THREAD_WRITES.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("a write whose caller was cancelled failed: %s", task.exception())


async def _in_this_process(core: Callable[[WriteCall], Any], call: WriteCall) -> Any:
    import asyncio
    import contextvars
    import functools

    run = functools.partial(contextvars.copy_context().run, core, call)
    return await asyncio.get_running_loop().run_in_executor(None, run)


@dataclass
class CallBatch:
    """The batch a write call runs: its operations as sent, or for a fix
    the kept batch with its corrections (``fixing`` its reference and
    record, ``fix_path`` the file the fix resolved). ``refused`` answers a
    fix that cannot run; ``started`` is when the call began, so the time
    budget counts the fix's path resolution."""

    items: list[Any]
    session: str
    started: float
    best_effort: bool = False
    create_new: bool = False
    fixing: tuple[str, kept_batches.Kept] | None = None
    fix_path: str | None = None
    refused: str | None = None


async def read_call(args: dict, *, kind: str) -> CallBatch:
    """The batch ``args`` runs, read once: a handler that picks its workers
    from the operations reads it first and hands it to
    ``handle_write_call``, so a fix routes on the batch it runs and its
    kept batch is taken once. A call without fix resolves no path."""
    import asyncio
    import time

    import shared

    started = time.monotonic()
    session = shared._current_session()[0]
    fix = args.get(kept_batches.FIX_KEY)
    if fix is None:
        return CallBatch(raw_items(args.get(OPERATIONS_KEY)), session, started,
                         best_effort=bool(args.get("best_effort", False)),
                         create_new=bool(args.get("create_new", False)))
    path = await shared._resolve_path(args["path"], writing=True)
    stat = await asyncio.to_thread(_stat, path)
    if args.get(OPERATIONS_KEY) is not None:
        return CallBatch([], session, started, refused=_nothing_saved(
            path, stat is not None, "Send fix or operations, not both: fix resends a refused batch with its "
            "corrections.").text)
    try:
        ref, kept, items = kept_batches.take(fix, session=session, tool=kind, path=path, stat=stat)
    except kept_batches.FixRefused as exc:
        still = kept_batches.STORE.get(str(fix.get("batch", "")).strip()) if isinstance(fix, dict) else None
        tail = (" " + kept_batches.note(fix["batch"].strip(), still.rounds_left, corrected_batch=True)
                if exc.kept and still is not None else "")
        return CallBatch([], session, started, refused=_nothing_saved(
            path, stat is not None, f"{str(exc)[:1].upper()}{str(exc)[1:]}.{tail}").text)
    return CallBatch(items, session, started, create_new=kept.create_new, fixing=(ref, kept), fix_path=path)


async def handle_write_call(args: dict, catalog: Catalog, core: Callable[[WriteCall], Any], *, kind: str,
                            advice: str | None = None, carry: tuple[str, ...] = (),
                            carry_paths: Mapping[str, bool] | None = None,
                            render: Callable[[Staged, WriteCall, float], Any] | None = None,
                            finish: Callable[[Staged, WriteCall, Rendered], WriteReport] | None = None,
                            in_thread: bool = False, batch: CallBatch | None = None) -> str:
    """The parent of a write: read the batch, answer help, refuse an invalid
    batch before any work, resolve the paths, run the worker, preview only
    what was saved.

    One worker: ``core(WriteCall) -> WriteReport``. With a render phase:
    ``core(WriteCall) -> Staged`` is worker A, ``await render(staged, call,
    budget_s)`` runs in the parent (its result reaches worker B as
    ``Rendered.payload``; a failure or a timeout as ``Rendered.note``), and
    ``finish(staged, call, rendered) -> WriteReport`` is worker B. Worker A
    may return a WriteReport instead (``call.commit``): nothing to render,
    one worker. What worker A hands on rides in ``Staged.facts`` (small,
    pickled: the render phase and worker B read it), ``Staged.baseline``
    (the original's check baseline) and files in ``call.work_dir``, which
    all three phases share and the parent removes (data too large to
    pickle). ``carry`` names tool arguments copied into ``WriteCall.extra``;
    ``carry_paths`` names path arguments resolved first (``True`` for an
    output). The parent holds a per-path lock from worker A to the commit,
    and the commit refuses when the target changed during the call.

    ``in_thread``: the one worker is a thread of this process (a core that
    starts an outside program a worker's memory cap would break), given the
    call's end as ``call.extra["deadline"]`` to stop by itself. A thread
    cannot be stopped, so a cancelled call returns at once while the write
    runs to its end with the path's lock and the work folder: a resent call
    waits for it instead of running beside it.

    ``batch``: the call as ``read_call`` read it, given by a handler that
    picked its workers from it; without one the call is read here."""
    import asyncio
    import tempfile

    import checks
    import shared
    from isolation import run_parse

    if (render is None) != (finish is None):
        raise ValueError("a render phase needs both render and finish")
    if in_thread and render is not None:
        raise ValueError("a write in a thread has one worker, no render phase")
    if batch is None:
        batch = await read_call(args, kind=kind)
    if batch.refused is not None:
        return batch.refused
    started, session, items = batch.started, batch.session, batch.items
    best_effort, create_new, fixing = batch.best_effort, batch.create_new, batch.fixing

    async def keep(text: str, at: str | None) -> str:
        """The refusal with its batch kept for a fix (all-or-nothing only)."""
        if best_effort:
            return text
        if at is None:
            try:
                at = await shared._resolve_path(args["path"], writing=True)
            except Exception:  # noqa: BLE001  # a path that will not resolve is kept by no one
                return text
        rounds = kept_batches.ROUNDS if fixing is None else fixing[1].rounds_left - 1
        ref = kept_batches.keep(session=session, tool=kind, path=at, stat=await asyncio.to_thread(_stat, at),
                                items=items, create_new=create_new, rounds_left=rounds,
                                ref=None if fixing is None else fixing[0])
        added = kept_batches.note(ref, rounds, corrected_batch=fixing is not None)
        if ref is not None:
            text = kept_for_fix(text)
        return f"{text}\n\n{added}" if added else text

    prepared = prepare_batch(items, catalog)
    answer = catalog.answer_help(prepared.help_ops)
    if any(not any(op.get(k) for k in HELP_NAME_KEYS) for op in prepared.help_ops):
        answer = f"{answer}\n\n{kept_batches.HELP_LINE}"
    if prepared.help_ops and not prepared.ops and not prepared.problems:
        # A question never creates or previews a file.
        return answer
    ops, problems, notes = catalog.resolve_batch(prepared.ops)
    problems = [*prepared.problems, *problems]
    if problems and not best_effort:
        refused = render_rejected(problems, kind=kind, path=args["path"], total=prepared.total)
        return _joined(await keep(refused, batch.fix_path), answer)
    path = await shared._resolve_path(args["path"], writing=True)
    ops, path_problems = await catalog.preresolve(ops, main_path=path)
    before = await asyncio.to_thread(_stat, path)
    if path_problems and not best_effort:
        refused = render_rejected([*problems, *path_problems], kind=kind, path=path, total=prepared.total,
                                  existed=before is not None)
        return _joined(await keep(refused, path), answer)
    rejected = [OpResult.from_error(p) for p in [*problems, *path_problems]]
    extra: dict[str, Any] = {name: args[name] for name in carry if name in args}
    outputs: list[str] = []
    for name, writing in (carry_paths or {}).items():
        if args.get(name):
            extra[name] = ResolvedPath(await shared._resolve_path(args[name], writing=writing))
            if writing:
                outputs.append(extra[name])
    watched = [path, *outputs]
    if in_thread:
        extra["deadline"] = started + PIPELINE_BUDGET_S
    first_worker_by = started + PIPELINE_BUDGET_S - (0.0 if render is None else RENDER_BUDGET_S + FINISH_MIN_S)

    async def locked() -> str:
        async with _path_lock(path, first_worker_by) as held:
            if not held:
                return _nothing_saved(path, before is not None, "Another write to the same file was still running, "
                                      "and this call ran out of time waiting for it. Send the batch again.").text
            before_all = {p: await asyncio.to_thread(_stat, p) for p in watched}
            work_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix=WORK_PREFIX)
            call = WriteCall(path=path, ops=ops, create_new=create_new, best_effort=best_effort,
                             existed=before_all[path] is not None, rejected=rejected, pre_notes=notes,
                             work_dir=work_dir, check_modules=checks.registered_modules(), total=prepared.total,
                             extra=extra, original=before_all[path] or ())
            try:
                try:
                    if in_thread:
                        report = await _in_this_process(core, call)
                    elif render is None:
                        report = await run_parse(core, call, _advice=advice,
                                                 _deadline_at=started + PIPELINE_BUDGET_S)
                    else:
                        report = await _two_workers(core, render, finish, call, advice, started)
                except Exception as exc:
                    for out in [*watched, *catalog.path_values(ops, writing=True)]:
                        shared.cleanup_partials(out)
                    landed = [p for p in watched
                              if (now := await asyncio.to_thread(_stat, p)) is not None and now != before_all[p]]
                    if landed:
                        for out in landed:
                            await shared._push_preview(out)
                        names = ", ".join(shared._to_agents_relative(p) for p in landed)
                        return f"{kind} saved: {names}. The report could not be built: {exc}"
                    raise
            finally:
                await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)
        for side in report.side_written:
            await shared._notify_file_written(side)
        if report.saved:
            kept_batches.drop_path(path)
            await shared._push_preview(path)
        for out in report.outputs:
            await shared._push_preview(out)
        if report.refused:
            return await keep(report.text, path)
        return report.text

    if not in_thread:
        return _joined(await locked(), answer)
    # The body runs as its own task: a cancelled call returns at once, while
    # the thread runs to its end with the path's lock and the work folder.
    task = asyncio.ensure_future(locked())
    _THREAD_WRITES.add(task)
    task.add_done_callback(_thread_write_settled)
    return _joined(await asyncio.shield(task), answer)
