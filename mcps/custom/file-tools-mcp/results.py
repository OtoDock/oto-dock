"""Per-operation results of the batch writers, and the text the agent reads.

A write tool applies a list of operations in one save. Each operation ends
APPLIED, NOOP (it changed nothing) or FAILED. The batch saves only when no
operation failed, unless the caller passed ``best_effort``, which saves the
operations that applied. The result text keeps one order everywhere: the
verdict line, the failed and no-op operations one per line (applied ones as a
count), warnings and notes, the checks, the readback, the help answers.

Everything here crosses the worker boundary by pickle (a spawn child gets its
arguments and returns its report that way), so the exceptions rebuild from
their fields and the records are plain dataclasses.
"""

from __future__ import annotations

import itertools
import os
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(Enum):
    APPLIED = "applied"
    NOOP = "noop"
    FAILED = "failed"


class Code(Enum):
    UNKNOWN_OP = "unknown_op"
    UNKNOWN_KEY = "unknown_key"
    MISSING_KEY = "missing_key"
    BAD_TYPE = "bad_type"
    ONE_OF = "one_of"
    BAD_VALUE = "bad_value"
    BAD_ITEM = "bad_item"
    BAD_PATH = "bad_path"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class OpError(Exception):
    """An operation failed. ``hint`` names the fix; ``location`` names where
    in the document, in the terms the operations use. ``unchanged`` says the
    operation left the document as it was, so a saved batch need not warn
    that it may be partly applied."""

    unchanged = False

    def __init__(self, code: Code, message: str, *, hint: str = "", location: str = "",
                 index: int | None = None, op: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.location = location
        self.index = index
        self.op = op

    def __reduce__(self):
        return (self.__class__, (self.code, self.message),
                {"hint": self.hint, "location": self.location, "index": self.index, "op": self.op,
                 "unchanged": self.unchanged})


class NoOp(Exception):
    """An operation found nothing to change: a search that matched nothing,
    a flag already set."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __reduce__(self):
        return (self.__class__, (self.reason,))


class ResolvedPath(str):
    """A path the proxy resolved for this session. The parent marks every
    path-typed key with it before a worker runs, and the worker refuses a
    path key without it, so no path reaches a core unjudged. String methods
    return plain ``str``: a resolved value is used as it is, never edited."""

    __slots__ = ()

    def __reduce__(self):
        return (ResolvedPath, (str(self),))


@dataclass
class Deferred:
    """A side output staged by an operation, landed only when the batch
    commits."""

    dest: str
    temp: str
    index: int


@dataclass
class OpResult:
    index: int
    op: str
    status: Status
    message: str = ""
    code: Code | None = None
    hint: str = ""
    location: str = ""
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    after: int | None = None
    side_only: bool = False
    unchanged: bool = False

    @classmethod
    def from_error(cls, exc: OpError, *, op: str = "", index: int | None = None) -> OpResult:
        return cls(
            index=exc.index if index is None else index, op=op or exc.op, status=Status.FAILED,
            message=exc.message, code=exc.code, hint=exc.hint, location=exc.location, unchanged=exc.unchanged,
        )


@dataclass
class BatchOutcome:
    results: list[OpResult]
    total: int
    best_effort: bool = False
    create_new: bool = False
    warnings: list[str] = field(default_factory=list)
    deferred: list[Deferred] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    keys_read: dict[int, set[str]] = field(default_factory=dict)
    whole_access: dict[int, list[str]] = field(default_factory=dict)

    def _count(self, status: Status) -> int:
        return sum(1 for r in self.results if r.status is status)

    @property
    def applied(self) -> int:
        return self._count(Status.APPLIED)

    @property
    def noops(self) -> int:
        return self._count(Status.NOOP)

    @property
    def failed(self) -> int:
        return self._count(Status.FAILED)

    @property
    def main_changed(self) -> bool:
        return any(r.status is Status.APPLIED and not r.side_only for r in self.results)

    @property
    def should_commit(self) -> bool:
        return (self.failed == 0 or self.best_effort) and (self.applied > 0 or self.create_new)

    @property
    def should_save_main(self) -> bool:
        return self.should_commit and (self.main_changed or self.create_new)


@dataclass
class WriteReport:
    """What a write core returns: the rendered text, whether the main file
    was saved (the parent previews and posts file-written only then), the
    side files that landed (announced to the proxy one by one), and other
    outputs the core landed (a tool's own output path: previewed and
    announced like the main file). ``refused``: an all-or-nothing batch
    whose operations failed, so nothing changed (the parent keeps it for a
    fix)."""

    text: str
    saved: bool
    side_written: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    refused: bool = False


@dataclass
class Staged:
    """The first half of a write: the batch's outcome, the saved bytes in the
    call's work folder (``None`` when nothing is to be saved), the readback
    text built before anything lands, what worker A learned for the parent's
    render phase (``facts``: a workbook's external links, say, so the render
    phase can refuse a recalculation) and the original's baseline for
    worker B's checks."""

    outcome: BatchOutcome
    staged_path: str | None
    readback: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    baseline: Any = None


class OpCtx:
    """What a handler gets beside the job and its operation."""

    def __init__(self, index: int, op: str, *, work_dir: str | None = None,
                 main_path: str = "", deferred: list[Deferred] | None = None,
                 earlier: list[Deferred] | None = None, sequence: Any = None) -> None:
        self.index = index
        self.op = op
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.removed_parts: list[str] = []
        self._work_dir = work_dir
        self._main_path = os.path.normpath(main_path) if main_path else ""
        self._deferred = deferred if deferred is not None else []
        self._earlier = earlier if earlier is not None else []
        self._sequence = sequence if sequence is not None else itertools.count(1)

    def note(self, text: str) -> None:
        """A remark on an applied operation (what it did that the agent
        would not guess)."""
        self.notes.append(str(text))

    def warn(self, text: str) -> None:
        """Advice the agent should act on; never blocks the save."""
        self.warnings.append(str(text))

    def removed(self, part_name: Any) -> None:
        """Declare a package part this operation removed on purpose (a
        slide, a sheet, a picture), named as the original file names it.
        The after-write checks count it, and what only it led to (its
        notes, its charts, a picture nothing else shows), as removed, not
        lost. Only an operation that applies counts."""
        self.removed_parts.append(str(part_name).lstrip("/"))

    def side_temp(self, dest: str, suffix: str = "") -> str:
        """A fresh temp file name in the call's work folder for a side output
        bound for ``dest``."""
        if not self._work_dir:
            raise OpError(Code.ERROR, "this operation writes a side file, and the call has no work folder for it")
        return os.path.join(self._work_dir, f"{next(self._sequence)}-{os.path.basename(dest)}{suffix}")

    def side_dest(self, folder: str, name: str) -> ResolvedPath:
        """``name`` inside a resolved ``folder``, refused unless it is a plain
        file name: the result stays inside the folder the proxy judged."""
        if not isinstance(folder, ResolvedPath):
            raise OpError(Code.BAD_PATH, "the output folder was not resolved before the worker")
        if not name or os.path.basename(name) != name or name in _DOT_NAMES:
            raise OpError(Code.BAD_PATH, f"'{name}' is not a plain file name",
                          hint="Give a file name without folders.")
        return ResolvedPath(os.path.join(folder, name))

    def defer_write(self, dest: str, temp: str) -> None:
        """Land ``temp`` at ``dest`` when the batch commits, after the main
        file; removed when it does not commit."""
        if not isinstance(dest, ResolvedPath):
            raise OpError(Code.BAD_PATH, "the side output's path was not resolved before the worker")
        normal = os.path.normpath(dest)
        if self._main_path and normal == self._main_path:
            raise OpError(Code.BAD_PATH, "a side output cannot overwrite the file the batch edits",
                          hint="Choose another output path.")
        if any(os.path.normpath(d.dest) == normal for d in (*self._earlier, *self._deferred)):
            raise OpError(Code.BAD_PATH, f"two outputs of this batch would write '{os.path.basename(dest)}'",
                          hint="Give each output its own path.")
        self._deferred.append(Deferred(dest, temp, self.index))


_DOT_NAMES = frozenset({os.curdir, os.pardir})

# Each list in a result shows this many lines, then a count of the rest.
LIST_CAP = 50
# Kinds of operation the verdict names before "and N more".
TALLY_KINDS = 6
# The whole result text, well under the worker's 512 KB string cap.
TEXT_CAP = 200_000
# A refusal's advice: the whole batch again, or only the fix when the
# batch was kept and the note below the refusal names it.
RESEND_WHOLE = "Fix them and send the whole batch again"
RESEND_FIX = "Fix them (the note below says how)"

_HELP_POINTER_KEYS = frozenset({Code.UNKNOWN_KEY, Code.MISSING_KEY, Code.BAD_TYPE, Code.ONE_OF})
_HELP_POINTER = ' ({"op": "help", "name": "%s"} shows the full shape.)'
_HELP_ALL = ' ({"op": "help"} lists every operation.)'


def short_repr(value: Any, limit: int = 80) -> str:
    """``repr(value)`` cut to ``limit`` characters (a refusal echoes what
    it refused, never a megabyte of it)."""
    try:
        text = repr(value)
    except ValueError:
        return f"a {type(value).__name__} too long to show"
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _shown(path: str) -> str:
    from shared import _to_agents_relative

    return _to_agents_relative(path)


_ENDINGS = (".", "?", "!", ".)", "?)", "!)")


def _sentence(text: str) -> str:
    """``text`` ending in a full stop; a closing parenthesis gets one after
    it unless the parenthesis itself closes a sentence."""
    text = text.strip()
    if text and not text.endswith(_ENDINGS):
        text += "."
    return text


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= LIST_CAP:
        return lines
    return lines[:LIST_CAP] + [f"  ...and {len(lines) - LIST_CAP} more"]


def op_label(result: OpResult) -> str:
    return f"op #{result.index} {result.op}".rstrip()


def failure_line(result: OpResult) -> str:
    """One failed operation: its caller index and name, where, what, the
    fix, and the operation it may follow from."""
    text = _sentence(result.message)
    if result.location:
        text = f"{result.location}: {text}"
    if result.hint:
        text += " " + _sentence(result.hint)
    if result.code in _HELP_POINTER_KEYS and result.op:
        text += _HELP_POINTER % result.op
    elif result.code is Code.UNKNOWN_OP:
        text += _HELP_ALL
    if result.after is not None:
        text += f" This may follow from op #{result.after}."
    return f"  - {op_label(result)}: {text}"


def op_tally(results: list[OpResult]) -> str:
    """The operations by kind, most first (ties in batch order), at most
    TALLY_KINDS kinds: "6 add_paragraph, 3 set_cell, 1 style_table"."""
    counts = Counter(r.op or "operation" for r in results)
    shown = counts.most_common(TALLY_KINDS)
    text = ", ".join(f"{count} {op}" for op, count in shown)
    rest = len(results) - sum(count for _, count in shown)
    return f"{text} and {rest} more" if rest else text


def _verdict(outcome: BatchOutcome, *, kind: str, shown: str, saved: bool, existed: bool,
             side_written: list[str]) -> str:
    if saved:
        applied = [r for r in outcome.results if r.status is Status.APPLIED]
        parts = [f"{outcome.applied} operations applied" + (f": {op_tally(applied)}" if applied else "")]
        if outcome.noops:
            parts.append(f"{outcome.noops} changed nothing")
        clean = sum(1 for r in outcome.results if r.status is Status.FAILED and r.unchanged)
        if outcome.failed - clean:
            parts.append(f"{outcome.failed - clean} failed and may be partly applied")
        if clean:
            parts.append(f"{clean} failed and changed nothing")
        return f"{kind} saved: {shown} ({('; ' if applied else ', ').join(parts)})"
    if side_written:
        names = ", ".join(_shown(p) for p in side_written[:LIST_CAP])
        more = f" and {len(side_written) - LIST_CAP} more" if len(side_written) > LIST_CAP else ""
        return f"{kind} unchanged: {shown}. Side files written: {names}{more}"
    state = "is unchanged" if existed else "was not created"
    if outcome.failed and not outcome.best_effort:
        reason = (f"{outcome.failed} of {outcome.total} operations failed. {RESEND_WHOLE}, or pass "
                  "best_effort: true to keep the operations that worked.")
    elif outcome.failed:
        reason = f"{outcome.failed} of {outcome.total} operations failed and none changed the file."
    elif outcome.total == 0:
        reason = "No operations were given."
    else:
        reason = "No operation changed anything."
    return f"Nothing saved: {shown} {state}. {reason}"


def render_outcome(outcome: BatchOutcome, *, kind: str, path: str, saved: bool, existed: bool = True,
                   findings=(), checks_ran: bool = False, readback: str = "", hints: str = "",
                   side_written: list[str] | None = None, extra_warnings: list[str] | None = None) -> str:
    """The result text of a write, in the one order every tool keeps."""
    side_written = list(side_written or [])
    lines = [_verdict(outcome, kind=kind, shown=_shown(path), saved=saved, existed=existed,
                      side_written=side_written)]
    failed = [r for r in outcome.results if r.status is Status.FAILED]
    noops = [r for r in outcome.results if r.status is Status.NOOP]
    if failed:
        lines.append("Failed:")
        lines.extend(_capped([failure_line(r) for r in failed]))
    if noops:
        lines.append("Changed nothing:")
        lines.extend(_capped([f"  - {op_label(r)}: {_sentence(r.message)}" for r in noops]))
    warnings = [f"  - {op_label(r)}: {_sentence(w)}" for r in outcome.results for w in r.warnings]
    warnings += [f"  - {_sentence(w)}" for w in [*outcome.warnings, *(extra_warnings or [])]]
    if warnings:
        lines.append("Warnings:")
        lines.extend(_capped(warnings))
    notes = [f"  - {op_label(r)}: {_sentence(n)}" for r in outcome.results for n in r.notes]
    if notes:
        lines.append("Notes:")
        lines.extend(_capped(notes))
    if findings or checks_ran:
        from checks import render_findings

        lines.append(render_findings(list(findings)))
    text = "\n".join(lines)
    if readback and saved:
        text += "\n\n" + readback.strip("\n")
    if hints:
        text += "\n\n" + hints.strip("\n")
    return cap_text(text)


_REJECTED_STATE = {None: "was not touched", True: "is unchanged", False: "was not created"}


def render_rejected(problems: list[OpError] | list[OpResult], *, kind: str, path: str, total: int,
                    hints: str = "", existed: bool | None = None) -> str:
    """The answer to a batch refused before any work: every invalid
    operation, nothing run, nothing written. ``existed`` is unknown
    (``None``) when the batch was refused before its path was resolved."""
    results = [p if isinstance(p, OpResult) else OpResult.from_error(p) for p in problems]
    results.sort(key=lambda r: r.index)
    count = len({r.index for r in results})
    lines = [
        f"Nothing saved: {_shown(path)} {_REJECTED_STATE[existed]}. {count} of {total} operations are not "
        f"valid, so none ran. {RESEND_WHOLE}.",
        "Failed:",
        *_capped([failure_line(r) for r in results]),
    ]
    text = "\n".join(lines)
    if hints:
        text += "\n\n" + hints.strip("\n")
    return cap_text(text)


def kept_for_fix(text: str) -> str:
    """A refusal whose batch was kept: its first line points at the note
    below it instead of asking for the whole batch."""
    first, sep, rest = text.partition("\n")
    return first.replace(RESEND_WHOLE, RESEND_FIX, 1) + sep + rest


def cap_text(text: str) -> str:
    if len(text) <= TEXT_CAP:
        return text
    return text[:TEXT_CAP] + f"\n\n(The result was cut at {TEXT_CAP} characters.)"
