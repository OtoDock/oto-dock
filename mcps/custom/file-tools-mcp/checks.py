"""Checks that judge a document, after every write and on demand.

A check is a function ``fn(ctx: CheckContext) -> list[Finding]`` registered
for one format. Every write runs its format's fast after-write checks on the
bytes it just saved and lists the findings under ``Checks:``; they never
undo the save. ``check_document`` runs every check of the format, the
completeness ones (``final_only``) included, on a file made any way.

Check modules are the ``*_checks.py`` files beside this one and in the
``formats`` sub-packages, imported by name in whichever process runs the
checks (a spawn child included), so no format edits a shared list. A check module imports no format library and no
XML parser at top level: the server process validates nothing of theirs.

Findings never carry document text in their message: text a finding quotes
goes into ``excerpt``, which is escaped, capped and labelled as untrusted
content when rendered, as ``read_document`` labels it.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO

import legacy

AFTER_WRITE = "after_write"
DOCUMENT = "document"
COST_FAST = "fast"
COST_RENDER = "render"
_COSTS = frozenset({COST_FAST, COST_RENDER})
_PHASES = frozenset({AFTER_WRITE, DOCUMENT})
CHECK_MODULE_GLOB = "*_checks.py"
FORMATS_PACKAGE = "formats"
CHECK_PACKAGES = (FORMATS_PACKAGE, "ooxml")
SKIPPED_CHECK = "checks.skipped"
LOAD_FAILED_CHECK = "checks.load_failed"
PART_REFUSED_CHECK = "checks.part_refused"
BASELINE_SKIPPED_CHECK = "checks.baseline_skipped"

# Package reads in the worker: a zip bomb or a part past these is refused,
# never inflated.
ZIP_MAX_ENTRIES = 20_000
ZIP_MAX_TOTAL_BYTES = 2 * 1024 ** 3
PART_MAX_BYTES = 256 * 1024 ** 2
BASELINE_MAX_BYTES = 100 * 1024 ** 2
_PEEK_BYTES = 4096
_DOCTYPE = "<!doctype"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
_SEVERITY_WORDS = {Severity.ERROR: ("error", "errors"), Severity.WARNING: ("warning", "warnings"),
                   Severity.INFO: ("note", "notes")}


@dataclass
class Finding:
    """One problem. ``location`` names it in the terms the write operations
    take ("slide_index 3, shape_name 'Body 2'", "paragraph index 12",
    "Sheet1!B7", "page 3") so the fix operation can be built from it;
    ``excerpt`` is document text the finding quotes; ``part`` and ``key``
    match a finding against the file's state before an edit."""

    severity: Severity
    check: str
    location: str = ""
    message: str = ""
    fix: str = ""
    excerpt: str = ""
    part: str = ""
    key: str = ""
    inherited: bool = False


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    fmt: str
    fn: Callable[[CheckContext], list[Finding]]
    after_write: bool
    final_only: bool
    cost: str
    baseline: bool
    snapshot: Callable[[CheckContext], Any] | None = None


class Baseline(list):
    """The findings a file had before an edit (a list, as before), with the
    snapshots of the checks that compare before and after
    (``snapshots[check_id]``, read by the check as
    ``ctx.baseline_snapshot``)."""

    def __init__(self, findings: Iterable[Finding] = (), snapshots: dict[str, Any] | None = None) -> None:
        super().__init__(findings)
        self.snapshots = dict(snapshots or {})


class PartRefused(Exception):
    """A package part the checks will not parse (a DTD, an oversized
    member)."""


_REGISTRY: dict[str, CheckSpec] = {}
_BLANKS: dict[str, tuple[Callable[[], bytes], str]] = {}
_LOADED: set[str] = set()
_LOAD_FAILURES: dict[str, str] = {}


def register(fmt: str, name: str, fn: Callable[[CheckContext], list[Finding]], *, after_write: bool = True,
             final_only: bool = False, cost: str = COST_FAST, baseline: bool = False,
             snapshot: Callable[[CheckContext], Any] | None = None) -> str:
    """Register ``fn`` as check ``<fmt>.<name>``; registering the same id
    again replaces it. ``final_only`` checks (completeness: a TOC without
    entries, leftover placeholders) run in ``check_document`` only; ``cost``
    ``render`` checks need the parent's rendered PDF; ``baseline`` checks
    also run on the original file so findings it already had are told
    apart; ``snapshot(ctx)`` runs on the original before an edit and its
    value reaches the check after the write as ``ctx.baseline_snapshot``
    (``None`` for a new file and in check_document)."""
    if cost not in _COSTS:
        raise ValueError(f"check {fmt}.{name}: cost is {COST_FAST} or {COST_RENDER}")
    check_id = f"{fmt}.{name}"
    _REGISTRY[check_id] = CheckSpec(check_id, fmt, fn, after_write, final_only, cost, baseline, snapshot)
    return check_id


def unregister(check_id: str) -> None:
    _REGISTRY.pop(check_id, None)


def register_blank(fmt: str, factory: Callable[[], bytes], *, version: str = "") -> None:
    """The bytes of the blank document a core starts from (``Document()``,
    ``Workbook()``, ``Presentation()``): the baseline of a new file.
    ``version`` names the library that made it, keying the cache."""
    _BLANKS[fmt] = (factory, version)


def registered_modules() -> tuple[str, ...]:
    """The modules whose checks this process registered, for a worker to
    import by name."""
    names = {spec.fn.__module__ for spec in _REGISTRY.values()}
    return tuple(sorted(n for n in names if n and not n.startswith("__")))


def check_module_names(folder: Path) -> list[str]:
    """The module names of the ``*_checks.py`` files beside this module and
    in the ``formats`` and ``ooxml`` packages (``formats.xlsx.formula_checks``)."""
    paths = [*sorted(folder.glob(CHECK_MODULE_GLOB))]
    for package in CHECK_PACKAGES:
        paths += sorted((folder / package).glob(f"**/{CHECK_MODULE_GLOB}"))
    return [".".join(p.relative_to(folder).with_suffix("").parts) for p in paths]


def load_check_modules(extra: Iterable[str] = (), *, folder: Path | None = None) -> list[str]:
    """Import ``extra`` and every check module of ``check_module_names``
    (``folder`` on the import path), once per process, by module name (a
    second import by file path would make a second module and register its
    checks twice)."""
    folder = folder or Path(__file__).resolve().parent
    names = [*extra, *check_module_names(folder)]
    for name in names:
        if name in _LOADED:
            continue
        _LOADED.add(name)
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001  # a broken check module costs its own checks, never a write
            _LOAD_FAILURES[name] = str(exc) or exc.__class__.__name__
    return sorted(_LOADED)


def _selected(fmt: str, phase: str, rendered: bool) -> list[CheckSpec]:
    if phase not in _PHASES:
        raise ValueError(f"phase is {AFTER_WRITE} or {DOCUMENT}")
    out = []
    for spec in _REGISTRY.values():
        if spec.fmt != fmt or (spec.cost == COST_RENDER and not rendered):
            continue
        if phase == AFTER_WRITE and (not spec.after_write or spec.final_only):
            continue
        out.append(spec)
    return sorted(out, key=lambda s: s.check_id)


def has_checks(fmt: str, phase: str) -> bool:
    return bool(_selected(fmt, phase, rendered=True))


def check_ids(fmt: str) -> list[str]:
    return sorted(spec.check_id for spec in _REGISTRY.values() if spec.fmt == fmt)


def matches_filter(check_id: str, only: Iterable[str]) -> bool:
    return any(check_id == p or check_id.startswith(p + ".") for p in only)


def unknown_filters(fmt: str, only: Iterable[str]) -> list[str]:
    ids = check_ids(fmt)
    return [p for p in only if not any(matches_filter(i, (p,)) for i in ids)]


# ---------------------------------------------------------------------------
# What a check reads
# ---------------------------------------------------------------------------


class _Prefixed(io.RawIOBase):
    """A stream that replays the bytes already peeked, then the rest."""

    def __init__(self, head: bytes, rest: BinaryIO) -> None:
        self._head = head
        self._rest = rest

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._head:
            n = min(len(buffer), len(self._head))
            buffer[:n] = self._head[:n]
            self._head = self._head[n:]
            return n
        data = self._rest.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def has_doctype(head: bytes) -> bool:
    """True when the first bytes of an XML part declare a DTD (UTF-8 or
    UTF-16)."""
    if head.startswith(_UTF16_BOMS) or b"\x00" in head[:64]:
        text = head.decode("utf-16", errors="ignore")
    else:
        text = head.decode("utf-8", errors="ignore")
    return _DOCTYPE in text.casefold()


def _xml_parser():
    from lxml import etree

    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


class CheckContext:
    """The document a check judges: the bytes a write just saved
    (``data`` or the staged ``fileobj``), else the file at ``path`` opened
    beneath the mount with no link followed. The package and its parts are
    read lazily and cached; ``doc`` is the library object when the caller
    already holds it; ``rendered_pdf`` is a PDF the parent rendered (into
    a folder of its own, outside the mount: a check opens it by name)."""

    def __init__(self, path: str, fmt: str, *, data: bytes | None = None, fileobj: BinaryIO | None = None,
                 rendered_pdf: str | None = None, doc: Any = None, removed: Iterable[str] = (),
                 ops: Iterable[str] | None = None) -> None:
        self.path = path
        self.fmt = fmt
        # The operations of the write (applied after it, asked for in its
        # baseline); None outside a write (check_document).
        self.ops = None if ops is None else frozenset(ops)
        # When the checks' time runs out (run_checks sets it from its budget).
        self.deadline: float | None = None
        # The parts the write's operations declared they removed on purpose.
        self.removed = frozenset(str(name).lstrip("/") for name in removed)
        self.data = data
        self.rendered_pdf = rendered_pdf
        self.doc = doc
        self.refused: dict[str, str] = {}
        self.baseline_snapshot: Any = None
        self._fileobj = fileobj
        self._owned: list[BinaryIO] = []
        self._zip: zipfile.ZipFile | None = None
        self._xml: dict[str, Any] = {}

    def __enter__(self) -> CheckContext:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        for fh in self._owned:
            with contextlib.suppress(OSError):
                fh.close()
        self._owned.clear()
        self._xml.clear()

    def open(self) -> BinaryIO:
        """The document's bytes as a binary stream, positioned at 0."""
        if self.data is not None:
            return io.BytesIO(self.data)
        if self._fileobj is not None:
            self._fileobj.seek(0)
            return self._fileobj
        fh = open_beneath_root(self.path)
        self._owned.append(fh)
        return fh

    def zip(self) -> zipfile.ZipFile:
        """The package, refused past the entry and size caps."""
        if self._zip is None:
            archive = zipfile.ZipFile(self.open())
            infos = archive.infolist()
            total = sum(i.file_size for i in infos)
            if len(infos) > ZIP_MAX_ENTRIES or total > ZIP_MAX_TOTAL_BYTES:
                archive.close()
                raise PartRefused(f"the package holds {len(infos)} parts and {total // (1024 * 1024)} MB "
                                  "uncompressed, past what the checks read")
            self._zip = archive
        return self._zip

    def part_names(self) -> list[str]:
        return [i.filename for i in self.zip().infolist()]

    def read_part(self, name: str) -> bytes | None:
        """A part's bytes, ``None`` when the package has no such part."""
        archive = self.zip()
        try:
            info = archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > PART_MAX_BYTES:
            raise PartRefused(f"{name} is {info.file_size // (1024 * 1024)} MB uncompressed")
        with archive.open(info) as fh:
            data = fh.read(PART_MAX_BYTES + 1)
        if len(data) > PART_MAX_BYTES:
            raise PartRefused(f"{name} is past {PART_MAX_BYTES // (1024 * 1024)} MB uncompressed")
        return data

    def part_xml(self, name: str, *, cache: bool = True):
        """A part parsed (cached unless ``cache=False``, for a pass over
        every part that frees each one), ``None`` when absent or refused. A
        part that declares a DTD is refused and reported once."""
        if name in self._xml:
            return self._xml[name]
        from lxml import etree

        root = None
        data = self.read_part(name)
        if data is not None:
            if has_doctype(data[:_PEEK_BYTES]):
                self.refused[name] = "the part declares a DTD"
            else:
                tree = etree.fromstring(data, _xml_parser()).getroottree()
                if tree.docinfo.internalDTD is not None:
                    self.refused[name] = "the part declares a DTD"
                else:
                    root = tree.getroot()
        if cache:
            self._xml[name] = root
        return root

    def iterparse(self, name: str, tags: Iterable[str] | str | None = None) -> Iterator[Any]:
        """The elements of a big part as they end, streamed; the caller
        clears what it has read."""
        from lxml import etree

        archive = self.zip()
        try:
            info = archive.getinfo(name)
        except KeyError:
            return
        if info.file_size > PART_MAX_BYTES:
            raise PartRefused(f"{name} is {info.file_size // (1024 * 1024)} MB uncompressed")
        with archive.open(info) as fh:
            head = fh.read(_PEEK_BYTES)
            if has_doctype(head):
                self.refused[name] = "the part declares a DTD"
                return
            stream = io.BufferedReader(_Prefixed(head, fh))
            yield from etree.iterparse(stream, events=("end",), tag=tags, resolve_entities=False,
                                       no_network=True, load_dtd=False, huge_tree=False)


def open_beneath_root(path: str) -> BinaryIO:
    """``path`` opened for reading beneath the mount, no component followed
    through a link."""
    import safe_fs
    import shared

    root = shared._write_root()
    try:
        return safe_fs.open_file_for_read(root, safe_fs.rel_under(path, root))
    except safe_fs.SafeFsError as exc:
        raise PermissionError(f"Cannot read '{shared._to_agents_relative(path)}': {exc.strerror}") from None


# ---------------------------------------------------------------------------
# Running checks
# ---------------------------------------------------------------------------


def _baseline_key(finding: Finding) -> tuple[str, str, str]:
    return (finding.check, finding.part, finding.key or finding.message)


def _natural(text: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def _order(finding: Finding) -> tuple:
    return (finding.inherited, _SEVERITY_RANK[finding.severity], finding.check, _natural(finding.location))


def run_checks(ctx: CheckContext, *, phase: str, baseline: list[Finding] | None = None,
               only: Iterable[str] | None = None, budget_s: float | None = None) -> list[Finding]:
    """The format's checks for ``phase`` on ``ctx``, ordered errors first. A
    check that raises becomes one note; past ``budget_s`` the rest are
    skipped with a note; findings matching ``baseline`` (the file before
    the edit) are marked inherited."""
    load_check_modules()
    specs = _selected(ctx.fmt, phase, ctx.rendered_pdf is not None)
    if only:
        specs = [s for s in specs if matches_filter(s.check_id, only)]
    findings: list[Finding] = [
        Finding(Severity.INFO, LOAD_FAILED_CHECK, message=f"the check module {name} could not load: {reason}")
        for name, reason in sorted(_LOAD_FAILURES.items())
    ]
    started = time.monotonic()
    ctx.deadline = None if budget_s is None else started + budget_s
    snapshots = getattr(baseline, "snapshots", {})
    for position, spec in enumerate(specs):
        ctx.baseline_snapshot = snapshots.get(spec.check_id)
        if budget_s is not None and time.monotonic() - started > budget_s:
            findings.append(Finding(Severity.INFO, SKIPPED_CHECK,
                                    message=f"{len(specs) - position} checks were skipped: the time for checks "
                                            "ran out", fix="Run check_document on the file."))
            break
        try:
            produced = list(spec.fn(ctx) or [])
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001  # a faulty check costs its own finding, never the write
            produced = [Finding(Severity.INFO, spec.check_id,
                                message=f"the check could not run: {exc or exc.__class__.__name__}")]
        for finding in produced:
            finding.check = finding.check or spec.check_id
        findings.extend(produced)
    for part, reason in sorted(ctx.refused.items()):
        findings.append(Finding(Severity.INFO, PART_REFUSED_CHECK, location=part,
                                message=f"{reason}, so it was not checked", part=part))
    if baseline:
        _mark_inherited(findings, baseline)
    return sorted(findings, key=_order)


def _mark_inherited(findings: list[Finding], baseline: list[Finding]) -> None:
    remaining = Counter(_baseline_key(f) for f in baseline if f.check != BASELINE_SKIPPED_CHECK)
    for finding in findings:
        key = _baseline_key(finding)
        if remaining[key] > 0:
            finding.inherited = True
            remaining[key] -= 1
    findings.extend(f for f in baseline if f.check == BASELINE_SKIPPED_CHECK)


def collect_baseline(fmt: str, *, path: str | None = None, create_new: bool = False,
                     check_modules: Iterable[str] = (), ops: Iterable[str] | None = None) -> Baseline | None:
    """The file before an edit loads it (streamed from the package, freed
    before the load): the baseline-capable checks' findings and the
    snapshot checks' snapshots; for a new file, the findings of the
    format's blank template. ``None`` when no such check exists. ``ops``
    names the batch's operations, for checks that run only after some."""
    load_check_modules(check_modules)
    selected = _selected(fmt, AFTER_WRITE, rendered=False)
    specs = [s for s in selected if s.baseline]
    snapshotting = [s for s in selected if s.snapshot is not None]
    if not specs and not snapshotting:
        return None
    if create_new or not path or not os.path.exists(path):
        blank = _blank_baseline(fmt, specs) if specs else []
        return None if blank is None else Baseline(blank)
    size = os.path.getsize(path)
    if size > BASELINE_MAX_BYTES:
        return Baseline([Finding(Severity.INFO, BASELINE_SKIPPED_CHECK,
                                 message=f"the file is {size // (1024 * 1024)} MB, so problems it already had are "
                                         "not told apart from new ones")])
    with CheckContext(path, fmt, ops=ops) as ctx:
        findings = _run_specs(ctx, specs)
        snapshots = {}
        for spec in snapshotting:
            try:
                snapshots[spec.check_id] = spec.snapshot(ctx)
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001  # without its snapshot a comparing check stays silent
                continue
        return Baseline(findings, snapshots)


def _run_specs(ctx: CheckContext, specs: list[CheckSpec]) -> list[Finding]:
    out: list[Finding] = []
    for spec in specs:
        try:
            produced = list(spec.fn(ctx) or [])
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001  # a baseline that cannot be read only weakens the comparison
            continue
        for finding in produced:
            finding.check = finding.check or spec.check_id
        out.extend(produced)
    return out


def _finding_from_dict(data: dict) -> Finding:
    return Finding(**{**data, "severity": Severity(data["severity"])})


CACHE_MAX_BYTES = 16 * 1024 ** 2


def read_private_cache(path: str) -> bytes | None:
    """A cache file in the shared temp folder, read only when it is a plain
    file (no link followed) this user owns and nobody else may write;
    ``None`` otherwise, so the caller rebuilds it."""
    import stat

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022
                or info.st_size > CACHE_MAX_BYTES):
            return None
        return fh.read(CACHE_MAX_BYTES)


_SOURCE_DIGESTS: dict[str, str] = {}


def _source_digest(fn: Callable) -> str:
    """The digest of the module a check lives in: a changed check changes
    the blank template's cached findings."""
    import inspect

    try:
        path = inspect.getsourcefile(fn) or ""
    except TypeError:
        return ""
    if path not in _SOURCE_DIGESTS:
        try:
            _SOURCE_DIGESTS[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
        except OSError:
            _SOURCE_DIGESTS[path] = ""
    return _SOURCE_DIGESTS[path]


def _blank_baseline(fmt: str, specs: list[CheckSpec]) -> list[Finding] | None:
    entry = _BLANKS.get(fmt)
    if entry is None:
        return None
    factory, version = entry
    keys = [fmt, version, *(f"{s.check_id}:{_source_digest(s.fn)}" for s in specs)]
    digest = hashlib.sha256("\n".join(keys).encode()).hexdigest()[:24]
    cache = os.path.join(tempfile.gettempdir(), f"file-tools-blank-{fmt}-{digest}.json")
    with contextlib.suppress(ValueError, TypeError, KeyError, UnicodeDecodeError):
        data = read_private_cache(cache)
        if data is not None:
            return [_finding_from_dict(d) for d in json.loads(data)]
    with CheckContext("", fmt, data=factory()) as ctx:
        findings = _run_specs(ctx, specs)
    payload = [{**asdict(f), "severity": f.severity.value} for f in findings]
    with contextlib.suppress(OSError):
        fd, temp = tempfile.mkstemp(prefix="file-tools-blank-", dir=os.path.dirname(cache))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(temp, cache)
    return findings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

FIELD_CAP = 120
MESSAGE_CAP = 400
FIX_CAP = 300
# Every character that is not printable ASCII is a candidate; of those, the
# controls, format characters (bidi marks, zero-width characters, Unicode tag
# characters), separators, private-use and unassigned code points, and the
# invisible marks that are not format characters (variation selectors, the
# Hangul fillers, the combining grapheme joiner) are shown as escapes, as
# are the pipe, the quote that closes an excerpt's label and the backslash
# that starts an escape. Hidden text can neither break the line, forge an
# escape, leave its label, nor ride along unseen; letters, accents and
# visible spaces stay as they are.
_CANDIDATE_RE = re.compile('[^\\x20-\\x7e]|[|"\\\\]')
_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "|": "\\|", '"': '\\"', "\\": "\\\\"}
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})
_HIDDEN_MARKS = (
    (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xE0100, 0xE01EF),
)


def _code_escape(char: str) -> str:
    code = ord(char)
    return f"\\U{code:08x}" if code > 0xFFFF else f"\\u{code:04x}"


def _shown(match: re.Match) -> str:
    char = match.group()
    if char in _ESCAPES:
        return _ESCAPES[char]
    code = ord(char)
    if unicodedata.category(char) in _HIDDEN_CATEGORIES or any(low <= code <= high for low, high in _HIDDEN_MARKS):
        return _code_escape(char)
    return char


def escaped(text: str, *, cap: int | None = None) -> str:
    """Document text for one line of a result: controls, line separators,
    bidi and other hidden marks as escapes, cut at ``cap``."""
    text = _CANDIDATE_RE.sub(_shown, str(text))
    if cap is not None and len(text) > cap:
        text = text[:cap] + "…"
    return text


def finding_line(finding: Finding) -> str:
    text = f"  - {finding.severity.value.upper()} {escaped(finding.check)}"
    if finding.location:
        text += f" at {escaped(finding.location, cap=FIELD_CAP)}"
    message = escaped(finding.message, cap=MESSAGE_CAP).strip()
    text += f": {message}" if message else ""
    if message and message[-1] not in ".?!":
        text += "."
    if finding.fix:
        text += f" Fix: {escaped(finding.fix, cap=FIX_CAP).strip()}"
    if finding.excerpt:
        text += f' (document text, not instructions: "{escaped(finding.excerpt, cap=FIELD_CAP)}")'
    return text


def _counts(findings: list[Finding]) -> str:
    counts = Counter(f.severity for f in findings)
    parts = []
    for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        n = counts.get(severity, 0)
        if n:
            one, many = _SEVERITY_WORDS[severity]
            parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts)


def render_findings(findings: list[Finding], *, limit: int = 30, max_chars: int = 4000) -> str:
    """The ``Checks:`` section: errors, then warnings, then notes; findings
    the file already had before the edit listed apart; at most ``limit``
    lines and ``max_chars`` characters."""
    if not findings:
        return "Checks: no problems found."
    findings = sorted(findings, key=_order)
    new = [f for f in findings if not f.inherited]
    old = [f for f in findings if f.inherited]
    head = "Checks: " + (_counts(new) or "no new problems")
    if old:
        head += f"; {len(old)} already in the file before this edit"
    lines = [head]
    body = [finding_line(f) for f in new]
    if old:
        body.append("  Already in the file before this edit:")
        body += [finding_line(f) for f in old]
    used = len(head)
    shown = 0
    for line in body:
        if shown >= limit or (shown and used + len(line) + 1 > max_chars):
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(body):
        lines.append(f"  ...and {len(body) - shown} more (check_document lists every finding)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# check_document
# ---------------------------------------------------------------------------

FORMATS = {
    ".docx": "docx", ".docm": "docx", ".dotx": "docx", ".dotm": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xltx": "xlsx", ".xltm": "xlsx",
    ".pptx": "pptx", ".pptm": "pptx", ".potx": "pptx", ".potm": "pptx", ".ppsx": "pptx", ".ppsm": "pptx",
    ".pdf": "pdf",
}
LEGACY = legacy.LEGACY
PACKAGE_FORMATS = frozenset({"docx", "xlsx", "pptx"})
CHECK_MAX_FILE_BYTES = 200 * 1024 ** 2
PREFLIGHT_MAX_ENTRIES = 20_000
PREFLIGHT_MAX_DIRECTORY = 16 * 1024 ** 2
_CFB_MAGIC = b"\xd0\xcf\x11\xe0"
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_TAIL = 65_557
_ZIP64_ENTRIES = 0xFFFF
_ZIP64_SIZE = 0xFFFFFFFF
_MB = 1024 * 1024
_CHECK_ADVICE = "Check a smaller copy of the file, or split it"


def preflight(path: str, fmt: str, *, own_copy: bool = False) -> str | None:
    """Refuse, before any worker, a file the checks will not read: too big,
    not a package, or a package whose directory alone is past the caps. Reads
    only the size and the last 64 KB. ``own_copy``: a file the server made
    in its own work folder (a converted copy), opened directly."""
    name = os.path.basename(path)
    with (open(path, "rb") if own_copy else open_beneath_root(path)) as fh:
        size = os.fstat(fh.fileno()).st_size
        if size > CHECK_MAX_FILE_BYTES:
            return (f"Error: {name} is {size // _MB} MB, past the {CHECK_MAX_FILE_BYTES // _MB} MB the checks "
                    f"read. {_CHECK_ADVICE}.")
        if fmt not in PACKAGE_FORMATS:
            return None
        if fh.read(len(_CFB_MAGIC)) == _CFB_MAGIC:
            return (f"Error: {name} is password-protected or a legacy binary file, not an Office Open XML "
                    "package, so it cannot be checked. Save an unprotected copy and check that.")
        fh.seek(max(0, size - _EOCD_TAIL))
        tail = fh.read()
    pos = tail.rfind(_EOCD_SIGNATURE)
    if pos < 0 or len(tail) < pos + 22:
        return f"Error: {name} is not a valid Office file (it is not a zip package)."
    entries = int.from_bytes(tail[pos + 10:pos + 12], "little")
    directory = int.from_bytes(tail[pos + 12:pos + 16], "little")
    if entries >= _ZIP64_ENTRIES or directory >= _ZIP64_SIZE or entries > PREFLIGHT_MAX_ENTRIES \
            or directory > PREFLIGHT_MAX_DIRECTORY:
        return (f"Error: {name} holds more parts than the checks read ({entries} entries). "
                f"{_CHECK_ADVICE}.")
    return None


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    ran: int = 0
    unknown: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    read_rendering: int = 0


def check_document_core(path: str, fmt: str, only: tuple[str, ...], check_modules: tuple[str, ...],
                        rendered_pdf: str | None = None, *, own_copy: bool = False,
                        exclude: tuple[str, ...] = ()) -> CheckResult:
    """Worker core: every check of the format (or those ``only`` names, less
    ``exclude``) on the file, read beneath the mount (``own_copy``: a copy
    the server made, opened directly); the ``render`` checks too when the
    parent rendered it."""
    load_check_modules(check_modules)
    known = check_ids(fmt)
    unknown = unknown_filters(fmt, only)
    if unknown:
        return CheckResult(unknown=unknown, known=known)
    selected = [s for s in _selected(fmt, DOCUMENT, rendered=rendered_pdf is not None)
                if (not only or matches_filter(s.check_id, only)) and not matches_filter(s.check_id, exclude)]
    if exclude and not selected:
        return CheckResult(ran=0, known=known)
    fileobj = open(path, "rb") if own_copy else None
    try:
        with CheckContext(path, fmt, rendered_pdf=rendered_pdf, fileobj=fileobj) as ctx:
            findings = run_checks(ctx, phase=DOCUMENT,
                                  only=[s.check_id for s in selected] if exclude else only or None)
    finally:
        if fileobj is not None:
            fileobj.close()
    return CheckResult(findings=findings, ran=len(selected), known=known,
                       read_rendering=len([s for s in selected if s.cost == COST_RENDER]))


def _filters(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = value.split(",")
    return tuple(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))


async def handle_check_document(args: dict) -> str:
    """check_document: the path resolved as a READ (a view-only role can
    check), never previewed, never announced as written."""
    import asyncio
    import shutil

    import shared
    from batch import PIPELINE_BUDGET_S
    from isolation import run_parse

    path = await shared._resolve_path(args["path"])
    if not await asyncio.to_thread(os.path.exists, path):
        return f"Error: File not found: {args['path']}"
    if not await asyncio.to_thread(os.path.isfile, path):
        return f"Error: {args['path']} is not a file."
    ext = Path(path).suffix.lower()
    started = time.monotonic()
    if ext in LEGACY:
        return await _check_legacy(args, path, started)
    fmt = FORMATS.get(ext)
    if fmt is None:
        return (f"Error: check_document does not read '{ext}' files. Supported: {', '.join(FORMATS)}, "
                f"{', '.join(LEGACY)}.")
    problem = await asyncio.to_thread(preflight, path, fmt)
    if problem:
        return problem
    only = _filters(args.get("checks"))
    notes: list[str] = []
    wanted = bool(args.get("render"))
    # The rendering's folder is made here, so it is removed even when the
    # call is cancelled while LibreOffice writes into it.
    work = await asyncio.to_thread(tempfile.mkdtemp, prefix="ft-check-render-") if wanted and fmt != "pdf" else None
    try:
        checked = None
        if work is not None and fmt == "xlsx":
            checked = await _recalculated_copy(path, work, started + PIPELINE_BUDGET_S, notes)
        rendered_pdf = await _rendering(checked or path, fmt, wanted, work, notes, started + PIPELINE_BUDGET_S,
                                        own_copy=checked is not None)
        result = await run_parse(check_document_core, checked or path, fmt, only, registered_modules(),
                                 rendered_pdf, own_copy=checked is not None,
                                 _deadline_at=started + PIPELINE_BUDGET_S, _advice=_CHECK_ADVICE)
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    shown = shared._to_agents_relative(path)
    if result.unknown:
        known = ", ".join(result.known) if result.known else f"none yet for {fmt}"
        return f"Error: no check matches {', '.join(repr(u) for u in result.unknown)}. Checks: {known}."
    if rendered_pdf is not None and not result.read_rendering:
        notes.append("No check of this format reads the rendering yet.")
    tail = "".join(f"\n{note}" for note in notes)
    if not result.ran and not result.findings:
        return f"Checked {shown} ({fmt}): no checks exist for this format yet.{tail}"
    return f"Checked {shown} ({fmt}): {result.ran} checks ran.\n{render_findings(result.findings)}{tail}"


# On a converted copy the package checks would judge LibreOffice's export,
# not the person's file.
LEGACY_EXCLUDED = ("package", "parts_lost", "schema")


async def _check_legacy(args: dict, path: str, started: float) -> str:
    """check_document on .doc, .xls or .ppt: the checks of the converted
    copy, under a header that says so."""
    import asyncio
    import shutil

    import legacy
    import shared
    from batch import PIPELINE_BUDGET_S
    from isolation import run_parse

    name = os.path.basename(path)
    try:
        copy = await legacy.converted_copy(path, deadline=started + PIPELINE_BUDGET_S)
    except legacy.LegacyError as exc:
        return f"Error: {name} could not be checked: {exc}"
    notes: list[str] = []
    try:
        fmt = FORMATS[copy.ext]
        problem = await asyncio.to_thread(preflight, copy.path, fmt, own_copy=True)
        if problem:
            return problem
        exclude = tuple(f"{fmt}.{check}" for check in LEGACY_EXCLUDED) if copy.converted else ()
        rendered_pdf = await _rendering(copy.path, fmt, bool(args.get("render")), copy.work, notes,
                                        started + PIPELINE_BUDGET_S, own_copy=True)
        result = await run_parse(check_document_core, copy.path, fmt, _filters(args.get("checks")),
                                 registered_modules(), rendered_pdf, own_copy=True, exclude=exclude,
                                 _deadline_at=started + PIPELINE_BUDGET_S, _advice=_CHECK_ADVICE)
    finally:
        await asyncio.to_thread(shutil.rmtree, copy.work, True)
    if result.unknown:
        known = ", ".join(result.known) if result.known else f"none yet for {fmt}"
        return f"Error: no check matches {', '.join(repr(u) for u in result.unknown)}. Checks: {known}."
    shown = shared._to_agents_relative(path)
    tail = "".join(f"\n{note}" for note in notes)
    return (f"Checked {shown} ({fmt}, as {'the' if copy.converted else 'a'} {copy.ext} "
            f"{'copy LibreOffice made of it' if copy.converted else 'package under an older name'}; "
            f"locations refer to that copy): {result.ran} checks ran.\n{render_findings(result.findings)}{tail}")


WORKBOOK_COPY_STEM = "checked"
_VALUES_KEPT = "so the checks read the values the file has"


def copy_workbook(path: str, work: str) -> tuple[str, dict[str, int], list[str]]:
    """Worker core: the workbook at ``path`` copied (read beneath the
    mount) into a folder of ``work`` of its own (the rendering is written
    into ``work``), its formula count per sheet, and where it reads outside
    data."""
    import openpyxl

    from formats.xlsx import computed

    folder = os.path.join(work, WORKBOOK_COPY_STEM)
    os.mkdir(folder, 0o700)
    copy = os.path.join(folder, WORKBOOK_COPY_STEM + Path(path).suffix.lower())
    with open_beneath_root(path) as src, open(copy, "wb") as dst:
        shutil.copyfileobj(src, dst)
    book = openpyxl.load_workbook(copy)
    try:
        return copy, computed.formula_counts(book), computed.outside_reads(book, computed.outside_parts(copy))
    finally:
        book.close()


def fill_workbook(copy: str, counts: dict[str, int], recalculated: str) -> str:
    """Worker core: the recalculated values written into the copy; the
    line that counts them."""
    from formats.xlsx import computed, recalc

    return computed.verdict(recalc.fill(copy, expected=counts, computed=recalculated))


async def _recalculated_copy(path: str, work: str, deadline: float, notes: list[str]) -> str | None:
    """``render: true`` on a workbook: a copy in ``work`` with every formula
    recalculated, so the checks and the rendering see values
    (the person's file is never touched); None, with a note, when there is
    nothing to compute or it could not be done. A workbook that reads
    outside data is never recalculated: nothing is fetched."""
    import asyncio

    import render_engine
    from formats.xlsx import computed
    from isolation import run_parse

    try:
        copy, counts, outside = await run_parse(copy_workbook, path, work, _deadline_at=deadline,
                                                _advice=_CHECK_ADVICE)
    except RuntimeError as exc:
        notes.append(f"The workbook could not be read for a recalculation ({exc}), {_VALUES_KEPT}.")
        return None
    if not any(counts.values()):
        return None
    if outside:
        notes.append(f"This workbook reads outside data ({'; '.join(outside)}), so it was not recalculated and "
                     f"nothing was fetched: {_VALUES_KEPT.removeprefix('so ')}.")
        return None
    folder = await asyncio.to_thread(render_engine.output_folder, work, computed.RECALC_FOLDER)
    try:
        recalculated = await render_engine.recalc(copy, out_dir=folder, deadline=deadline)
        line = await run_parse(fill_workbook, copy, counts, recalculated, _deadline_at=deadline,
                               _advice=_CHECK_ADVICE)
    except FileNotFoundError:
        notes.append(f"No calculation engine is installed here, {_VALUES_KEPT}.")
        return None
    except (OSError, RuntimeError, ValueError) as exc:
        notes.append(f"The recalculation failed ({str(exc) or exc.__class__.__name__}), {_VALUES_KEPT}.")
        return None
    notes.insert(0, f"The checks read a recalculated copy. {line}".rstrip())
    return copy


PRINT_MARKS_FOLDER = "print-marks"


async def _print_marked(path: str, work: str, deadline: float, notes: list[str], *, own_copy: bool) -> str:
    """The workbook's marked copy in a folder of ``work``, or ``path`` itself
    (with a note) when it cannot be made."""
    import asyncio

    import render_engine
    from formats.xlsx import print_marks
    from isolation import run_parse

    folder = await asyncio.to_thread(render_engine.output_folder, work, PRINT_MARKS_FOLDER)
    try:
        return await run_parse(print_marks.marked_copy, path, folder, own_copy=own_copy, _deadline_at=deadline,
                               _advice=_CHECK_ADVICE)
    except (RuntimeError, OSError, ValueError) as exc:
        notes.append(f"How the workbook prints could not be checked ({str(exc) or exc.__class__.__name__}).")
        return path


async def _rendering(path: str, fmt: str, wanted: bool, work: str | None, notes: list[str], deadline: float, *,
                     own_copy: bool = False) -> str | None:
    """The PDF the ``render`` checks read, made in the parent: a PDF is its
    own rendering, any other format goes through LibreOffice into ``work``
    (the caller's folder); a workbook as its marked copy, which prints as it
    does and shows where its charts, pictures and columns land. A rendering
    that fails leaves those checks out and says so; the other checks still
    run."""
    import render_engine

    if not wanted:
        return None
    if fmt == "pdf" or work is None:
        return path
    if fmt == "xlsx":
        path = await _print_marked(path, work, deadline, notes, own_copy=own_copy)
    try:
        return await render_engine.to_pdf(path, out_dir=work)
    except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
        notes.append(f"The rendering failed ({str(exc) or exc.__class__.__name__}), so the checks that read it "
                     "did not run.")
        return None
