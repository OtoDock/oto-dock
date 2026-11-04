"""Legacy binary documents (.doc, .xls, .ppt) read and checked through a
copy LibreOffice converts to .docx, .xlsx or .pptx.

The parent copies the file beneath the mount into a work folder of its own
(under a fixed short name: no link followed, no long name passed to
LibreOffice, no lock file beside the person's file), looks at its first
bytes, and then:

- a zip package is a modern file with an old extension: it is read as the
  modern format, unconverted;
- an OLE2 compound file (the old binary formats) is opened far enough to
  prove it is one, of the kind its extension says (its directory holds
  ``WordDocument``, ``Workbook`` or ``PowerPoint Document``), and that it is
  not encrypted (an encrypted package's streams, Word's encryption flag,
  Excel's FILEPASS record, PowerPoint's encrypted user token); only then
  does LibreOffice convert it, under the convert profile (macros off, links
  never updated). The check runs in a parse worker, and every walk of the
  file's tables is bounded by its size and stops at a sector seen before;
- anything else (RTF, HTML, XML, text named .doc) is refused before
  LibreOffice starts: LibreOffice would import it as what it really is.

The whole call keeps the write pipeline's budget: at most a minute's wait
for a LibreOffice slot, two minutes' conversion, the worker by the end.
"""

from __future__ import annotations

import asyncio
import mmap
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any

LEGACY = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}
SLOT_WAIT_S = 60.0
CONVERT_S = 120.0
LEGACY_MAX_BYTES = 100 * 1024 ** 2
SOURCE_STEM = "source"
WORK_PREFIX = "file-tools-legacy-"
_ZIP = b"PK\x03\x04"
_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CHUNK = 1024 * 1024


class LegacyError(Exception):
    """The file cannot be read through a converted copy; the message names
    the fix."""


@dataclass
class Copy:
    """The modern file a legacy one was read as: its path in ``work`` (the
    caller removes ``work``), its format's extension, and whether
    LibreOffice converted it (False: it was already a modern package)."""

    path: str
    work: str
    ext: str
    converted: bool

    def header(self, name: str) -> str:
        if not self.converted:
            return (f"[{name} is a {self.ext} package with an older name; it was read as {self.ext}.]")
        return (f"[{name} was read from a {self.ext} copy LibreOffice made of it: positions and addresses refer "
                f"to that temporary copy, and layout details may differ from the original.]")


def _copy_in(path: str, target: str) -> bytes:
    from checks import open_beneath_root

    with open_beneath_root(path) as src, open(target, "wb") as dst:
        size = os.fstat(src.fileno()).st_size
        if size > LEGACY_MAX_BYTES:
            raise LegacyError(f"the file is {size // (1024 * 1024)} MB, past the "
                              f"{LEGACY_MAX_BYTES // (1024 * 1024)} MB an old-format file is converted at. Save it "
                              "in the current format from its application and use that.")
        head = src.read(_CHUNK)
        dst.write(head)
        shutil.copyfileobj(src, dst, _CHUNK)
    return head


def _looks_like(head: bytes) -> str:
    text = head[:512].lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n").lower()
    if text.startswith(b"{\\rtf"):
        return "it is RTF"
    if text.startswith((b"<!doctype html", b"<html", b"<meta", b"<head")):
        return "it is a web page"
    if text.startswith(b"<"):
        return "it is XML"
    return "its first bytes match no Office format"


_MAIN_STREAMS = {".doc": ("WordDocument",), ".xls": ("Workbook", "Book"), ".ppt": ("PowerPoint Document",)}
_ENCRYPTED_PACKAGE = ("EncryptionInfo", "EncryptedPackage")
_FREE = 0xFFFFFFFF
_END = 0xFFFFFFFE
_SPECIAL = 0xFFFFFFFA
_DIRECTORY_ENTRY = 128
_HEADER_DIFAT = 109
_PPT_ENCRYPTED_TOKEN = 0xF3D1C4DF
_XLS_FILEPASS = 0x002F
_DOC_ENCRYPTED_FLAG = 0x0100
_DIRECTORY_MAX_BYTES = 4 * 1024 * 1024
_MINI_SECTOR = 64
CHECK_S = 30.0


class _Compound:
    """Just enough of an OLE2 compound file to name its streams and read
    the first bytes of one. Every walk is bounded by the file's size and
    stops at a sector it has seen: a crafted file's loops end. Sector n
    starts right after the header's sector ((n + 1) x the sector size: 512
    bytes, or 4,096 in a version 4 file). The mini FAT is read only where
    a chain needs it."""

    def __init__(self, data: Any) -> None:
        self.data = data
        if len(data) < 512 or data[:8] != _CFB or data[0x1C:0x1E] != b"\xfe\xff":
            raise LegacyError("its header is not a compound file's")
        shift = int.from_bytes(data[0x1E:0x20], "little")
        if shift not in (9, 12) or int.from_bytes(data[0x20:0x22], "little") != 6:
            raise LegacyError("its sector sizes are not a compound file's")
        self.size = 1 << shift
        self.sectors = max(0, -(-(len(data) - self.size) // self.size))  # a short last sector counts
        self.cutoff = int.from_bytes(data[0x38:0x3C], "little")
        self.per_sector = self.size // 4
        needed = -(-self.sectors // self.per_sector)
        listed: list[int] = []
        taken: set[int] = set()

        def take(numbers: list[int]) -> None:
            for number in numbers:
                if len(listed) >= needed:
                    return
                if number < _SPECIAL and number < self.sectors and number not in taken:
                    taken.add(number)
                    listed.append(number)

        take([int.from_bytes(data[0x4C + 4 * i:0x50 + 4 * i], "little") for i in range(_HEADER_DIFAT)])
        difat, visited = int.from_bytes(data[0x44:0x48], "little"), set()
        # A DIFAT sector lists per_sector - 1 FAT sectors: more than the FAT
        # this file can need only repeats entries.
        difat_needed = -(-max(0, needed - _HEADER_DIFAT) // (self.per_sector - 1))
        for _ in range(min(int.from_bytes(data[0x48:0x4C], "little"), self.sectors, difat_needed)):
            if difat >= self.sectors or difat in visited or len(listed) >= needed:
                break
            visited.add(difat)
            block = self._sector(difat)
            take([int.from_bytes(block[i:i + 4], "little") for i in range(0, self.size - 4, 4)])
            difat = int.from_bytes(block[-4:], "little")
        self.fat: list[int] = []
        for sector in listed:
            block = self._sector(sector)
            self.fat += [int.from_bytes(block[i:i + 4], "little") for i in range(0, self.size, 4)]
        del self.fat[self.sectors:]
        directory = self._chain(int.from_bytes(data[0x30:0x34], "little"), _DIRECTORY_MAX_BYTES)
        self.entries: dict[str, tuple[int, int, int]] = {}
        for offset in range(0, len(directory) - _DIRECTORY_ENTRY + 1, _DIRECTORY_ENTRY):
            entry = directory[offset:offset + _DIRECTORY_ENTRY]
            kind = entry[0x42]
            length = int.from_bytes(entry[0x40:0x42], "little")
            if kind not in (1, 2, 5) or not 2 <= length <= 64:
                continue
            name = entry[:length - 2].decode("utf-16-le", errors="replace")
            start = int.from_bytes(entry[0x74:0x78], "little")
            size = int.from_bytes(entry[0x78:0x80], "little")
            self.entries.setdefault(name if kind != 5 else "\x00root", (kind, start, size))
        if "\x00root" not in self.entries:
            raise LegacyError("its directory has no root entry")
        self._mini_loaded = False
        self._mini_fat: list[int] = []
        self._mini_stream: list[int] = []

    def _sector(self, number: int) -> bytes:
        start = (number + 1) * self.size
        return self.data[start:start + self.size]

    def _walk(self, start: int, next_of: Any, count: int, limit: int) -> list[int]:
        """The numbers of a chain from ``start`` (``next_of`` gives the next
        of each, ``count`` the numbers in range): at most ``limit``, ending
        at a number out of range or seen before."""
        found: list[int] = []
        seen: set[int] = set()
        number = start
        while number < _SPECIAL and number < count and number not in seen and len(found) < limit:
            seen.add(number)
            found.append(number)
            number = next_of(number)
        return found

    def _chain(self, start: int, limit: int) -> bytes:
        """At most ``limit`` bytes (whole sectors) of the chain from
        ``start``."""
        numbers = self._walk(start, self.fat.__getitem__, len(self.fat), -(-limit // self.size))
        return b"".join(self._sector(n) for n in numbers)

    def _mini_next(self, number: int) -> int:
        """The mini FAT's entry for mini sector ``number``, read from its
        own sector (the mini FAT is never read whole)."""
        sector = self._mini_fat[number // self.per_sector]
        at = (sector + 1) * self.size + (number % self.per_sector) * 4
        return int.from_bytes(self.data[at:at + 4], "little")

    def head(self, name: str, length: int) -> bytes:
        """The first ``length`` bytes of stream ``name`` (fewer when it is
        shorter)."""
        _, start, size = self.entries[name]
        length = min(length, size)
        if size >= self.cutoff:
            return self._chain(start, length)[:length]
        if not self._mini_loaded:
            self._mini_loaded = True
            # The mini stream lives in the root's chain: no more of it, nor
            # of the mini FAT that maps it, than the file can hold.
            _, root_start, root_size = self.entries["\x00root"]
            mini_bytes = min(root_size, len(self.data))
            self._mini_stream = self._walk(root_start, self.fat.__getitem__, len(self.fat),
                                           -(-mini_bytes // self.size))
            self._mini_fat = self._walk(int.from_bytes(self.data[0x3C:0x40], "little"), self.fat.__getitem__,
                                        len(self.fat), -(-(mini_bytes // _MINI_SECTOR) * 4 // self.size))
        per_sector = self.size // _MINI_SECTOR
        out = bytearray()
        for mini in self._walk(start, self._mini_next, len(self._mini_fat) * self.per_sector,
                               -(-length // _MINI_SECTOR)):
            index, offset = divmod(mini, per_sector)
            if index >= len(self._mini_stream):
                break
            block = self._sector(self._mini_stream[index])
            out += block[offset * _MINI_SECTOR:(offset + 1) * _MINI_SECTOR]
        return bytes(out[:length])


def _encrypted(compound: _Compound, ext: str) -> bool:
    if all(name in compound.entries for name in _ENCRYPTED_PACKAGE):
        return True
    if ext == ".doc" and "WordDocument" in compound.entries:
        fib = compound.head("WordDocument", 12)
        return len(fib) >= 12 and bool(int.from_bytes(fib[10:12], "little") & _DOC_ENCRYPTED_FLAG)
    if ext == ".xls":
        stream = next((n for n in _MAIN_STREAMS[".xls"] if n in compound.entries), None)
        if stream is None:
            return False
        records, offset = compound.head(stream, 4096), 0
        for _ in range(16):
            if offset + 4 > len(records):
                break
            kind, length = int.from_bytes(records[offset:offset + 2], "little"), int.from_bytes(
                records[offset + 2:offset + 4], "little")
            if kind == _XLS_FILEPASS:
                return True
            offset += 4 + length
        return False
    if ext == ".ppt" and "Current User" in compound.entries:
        atom = compound.head("Current User", 16)
        return len(atom) >= 16 and int.from_bytes(atom[12:16], "little") == _PPT_ENCRYPTED_TOKEN
    return False


def compound_problem(path: str, ext: str) -> str | None:
    """Why a compound file must not reach LibreOffice (broken, of another
    kind than ``ext`` says, or encrypted), or None. Run in a parse worker."""
    with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
        try:
            compound = _Compound(data)
        except LegacyError as exc:
            return (f"it is not a readable old Office file ({exc}). Open it in its application, save it as "
                    f"{LEGACY[ext]} and use that.")
        encrypted = _encrypted(compound, ext)
        kind_matches = any(name in compound.entries for name in _MAIN_STREAMS[ext])
    if encrypted:
        return "the file is password-protected, so it cannot be read. Save an unprotected copy and use that."
    if not kind_matches:
        return (f"it is not a {_KINDS[ext]} file (its content is another kind of document). Open it in its "
                "application, save it in the current format and use that.")
    return None


_KINDS = {".doc": "Word 97-2003", ".xls": "Excel 97-2003", ".ppt": "PowerPoint 97-2003"}


async def _check_compound(source: str, ext: str, target: str, deadline: float | None) -> None:
    from isolation import run_parse

    advice = f"Open it in its application, save it as {target} and use that."
    try:
        problem = await run_parse(compound_problem, source, ext, _timeout_s=CHECK_S, _deadline_at=deadline,
                                  _advice=advice)
    except RuntimeError as exc:
        raise LegacyError(f"it could not be checked as an old Office file: {exc}") from None
    if problem:
        raise LegacyError(problem)


async def converted_copy(path: str, *, deadline: float | None = None) -> Copy:
    """The modern copy of the legacy file at ``path`` (resolved, beneath
    the mount). Raises LegacyError with the reason and the fix."""
    import render_engine

    ext = os.path.splitext(path)[1].lower()
    if ext not in LEGACY:
        raise LegacyError(f"{ext} is not an old Office format")
    target = LEGACY[ext]
    work = await asyncio.to_thread(tempfile.mkdtemp, prefix=WORK_PREFIX)
    try:
        source = os.path.join(work, SOURCE_STEM + ext)
        head = await asyncio.to_thread(_copy_in, path, source)
        if head.startswith(_ZIP):
            modern = os.path.join(work, SOURCE_STEM + target)
            os.replace(source, modern)
            return Copy(modern, work, target, converted=False)
        if not head.startswith(_CFB):
            raise LegacyError(f"it is not an old Office file ({_looks_like(head)}). Open it in its application, "
                              f"save it as {target} and use that.")
        await _check_compound(source, ext, target, deadline)
        out = os.path.join(work, "out")
        os.mkdir(out, 0o700)
        timeout = CONVERT_S if deadline is None else max(1.0, min(CONVERT_S, deadline - time.monotonic()))
        try:
            produced = await render_engine.to_ooxml(source, target, out_dir=out, slot_wait_s=SLOT_WAIT_S,
                                                    timeout_s=timeout)
        except (RuntimeError, OSError) as exc:
            raise LegacyError(f"LibreOffice could not convert it to {target} ({exc}). If it opens in its "
                              f"application, save it there as {target} and use that.") from None
        return Copy(produced, work, target, converted=True)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, work, True)
        raise


async def read(path: str, options: dict, readers: dict) -> str:
    """read_document on a legacy file: the converted copy read by its
    format's reader in a worker, under one header line."""
    from batch import PIPELINE_BUDGET_S
    from isolation import run_parse

    name = os.path.basename(path)
    started = time.monotonic()
    try:
        copy = await converted_copy(path, deadline=started + PIPELINE_BUDGET_S)
    except LegacyError as exc:
        return f"Error: {name} could not be read: {exc}"
    try:
        text = await run_parse(readers[copy.ext], copy.path, options, _deadline_at=started + PIPELINE_BUDGET_S)
    finally:
        await asyncio.to_thread(shutil.rmtree, copy.work, True)
    return f"{copy.header(name)}\n{text}"
