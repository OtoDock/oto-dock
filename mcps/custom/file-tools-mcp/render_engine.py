"""Every conversion the server makes, in the parent process: a document to
PDF (screenshots, the rendered checks), a workbook with its formulas
computed, a file to another format (convert_document), a legacy file to
Office Open XML. Which engine runs is decided here and nowhere else: no
other module starts LibreOffice or calls Collabora.

Two engines. The install's own Collabora, through its convert-to service,
when ``COLLABORA_INTERNAL_URL`` names it and it answers: it renders what
the person sees in the preview. LibreOffice in this image otherwise, and
whenever a Collabora call fails, so the caller never sees which one ran
or that one failed (the log does).

Neither engine sees an Office Open XML package's external relationships
that load content (linked pictures, media, objects, templates): both get a
copy without them (``ooxml.render_copy``), as both fetch some of them
whatever their settings say.

Collabora receives only an Office Open XML package (no OpenDocument, no
legacy or text format, no part in another format), within size caps, one conversion at a time per
process, so a batch never queues work in the person's Collabora while
they edit there. A conversion there never runs macros and fetches nothing
(convert-to with coolwsd's defaults; proved by the tests that run against
a private Collabora). Every request names the image's locale (en-US), so
both engines compute and format the same way; A4 comes from the temporary
copy of a workbook (``ooxml.render_copy``), never from the person's file.

LibreOffice runs through the slots below (a worker may never start it:
RLIMIT_AS would follow it into soffice), into a folder the
caller owns and removes, and ends its processes when its caller is
cancelled, so a render phase cut at its budget leaves nothing running. A
conversion never recalculates and never updates links; a recalculation
runs under a profile of its own that differs only in recalculating on load
(macros off, links never updated and linked graphics blocked stay).

A recalculation computes the formulas the workbook brings: a format
refuses it before calling ``recalc`` for a workbook whose functions or
links could reach out (the facts its worker A put in ``Staged.facts``).
Collabora's convert-to keeps a formula's cached value, so a workbook that
may hold one goes to it as a copy without them.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import functools
import ipaddress
import json
import os
import re
import shutil
import signal
import stat
import tempfile
import time
import weakref
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import shared
from ooxml import render_copy

logger = shared.logger

PDF = "pdf"
XLSX = "xlsx"
XLSX_FILTER = "xlsx:Calc MS Excel 2007 XML"
# A conversion keeps today's waits; a recalculation inside a write waits at
# most RECALC_SLOT_WAIT_S for a slot and runs at most RECALC_RUN_S, else the
# write commits without values.
CONVERT_SLOT_WAIT_S = 300.0
CONVERT_RUN_S = 120.0
RECALC_SLOT_WAIT_S = 30.0
RECALC_RUN_S = 60.0

# --- Collabora ---------------------------------------------------------------

URL_SETTING = "COLLABORA_INTERNAL_URL"
# The URL schemes Collabora may be reached by; anything but TLS_SCHEME is
# plain text on the wire.
URL_SCHEMES = ("http", "https")
TLS_SCHEME = "https"
PROBE_PATH = "/hosting/capabilities"
CONVERT_PATH = "/cool/convert-to/"
PROBE_TTL_UP_S = 60.0
PROBE_TTL_DOWN_S = 30.0
PROBE_CONNECT_S = 2.0
PROBE_TOTAL_S = 3.0
PROBE_MAX_BYTES = 64 * 1024
CONNECT_S = 3.0
# Collabora answers a convert-to only once the file is converted (the
# corpus' p95 is 1.6 s): a call that has heard nothing by then goes to
# LibreOffice at once instead of waiting out its share, and Collabora is
# left alone for COLLABORA_SERVER_LIMIT_S.
FIRST_ANSWER_S = 15.0
# A call's share of its deadline on Collabora; LibreOffice has the rest.
COLLABORA_RUN_S = 60.0
COLLABORA_RECALC_S = 30.0
# How long a call waits for this process's one Collabora conversion.
COLLABORA_QUEUE_WAIT_S = 5.0
# Collabora's own bound on a conversion (coolwsd per_document.limit_convert_secs):
# a call we gave up on may run there this long, so none is sent meanwhile.
COLLABORA_SERVER_LIMIT_S = 100.0
# After a Collabora timeout, LibreOffice runs only with at least this left.
MIN_FALLBACK_S = 15.0
UNLINK_FAILED = ("the document links to content outside it, and a copy without those links could not be made, "
                 "so it was not rendered. Save the file again in the program that made it, or remove its linked "
                 "pictures and objects, then try again")
# Statuses that name one file Collabora cannot read. A 500 is also what a
# Collabora failing as a whole answers to everything: after this many in a
# row with no conversion between, it is left alone like a down one.
FILE_REFUSED_STATUSES = (400, 415, 422)
FAILED_LOAD_STATUS = 500
FAILED_LOADS_DOWN = 3
# httpx's own timeouts while the answer streams: the call's share ran out.
_HTTPX_TIMEOUTS = frozenset({"ReadTimeout", "WriteTimeout", "PoolTimeout"})
# Targets printed on pages: a workbook's copy gets A4 for these only.
PRINT_FORMATS = frozenset({"pdf", "png"})
# A call with less than this of its share left goes to LibreOffice.
MIN_SHARE_S = 2.0
SEND_MAX_BYTES = 32 * 1024 * 1024
SEND_MAX_UNCOMPRESSED = 256 * 1024 * 1024
SEND_MAX_ENTRIES = 20_000
ANSWER_MAX_BYTES = 512 * 1024 * 1024
LANG = "en-US"
OOXML_EXTENSIONS = frozenset({".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".xltx", ".xltm",
                              ".pptx", ".pptm", ".potx", ".potm"})
WORKBOOK_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".xltx", ".xltm"})
_ODF_MEMBERS = frozenset({"mimetype", "content.xml", "META-INF/manifest.xml"})
# What the parent reads of a package before a conversion: its directory and
# its relationship parts (expat, ooxml.render_copy), within these bounds. A
# package past any of them, or one the parent cannot read, gets the unlinked
# copy, which a worker makes under its own memory limit. The directory is
# read only when no zip64 record stands in for the 32-bit fields: zipfile
# parses the size the record gives, and that is the bound.
SCAN_MAX_FILE_BYTES = 256 * 1024 * 1024
SCAN_MAX_DIRECTORY = 1024 * 1024
SCAN_MAX_ENTRIES = 10_000
RELS_SCAN_MAX_BYTES = 2 * 1024 * 1024
RELS_SCAN_TOTAL_BYTES = 16 * 1024 * 1024
CONTENT_TYPES = "[Content_Types].xml"
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP64_ENTRIES = 0xFFFF
_ZIP64_SIZE = 0xFFFFFFFF
# The formats Collabora makes for ``convert`` as the image's LibreOffice
# does (compared on the corpus); any other target, or a LibreOffice filter
# name ("txt:Text"), is LibreOffice's.
COLLABORA_FORMATS = frozenset({"pdf", "docx", "odt", "rtf", "txt", "html", "xlsx", "ods", "csv", "pptx", "odp",
                               "png"})
# LibreOffice's filter for a target whose default differs from Collabora's:
# CSV in UTF-8 (comma, double quotes), where the default is the system's
# character set and Greek comes out as question marks.
LIBREOFFICE_FILTERS = {"csv": "csv:Text - txt - csv (StarCalc):44,34,76"}
_ZIP = b"PK\x03\x04"
# How an answer of each format starts; a format not named here must only be
# non-empty.
_MAGIC = {PDF: b"%PDF-", "png": b"\x89PNG", "rtf": b"{\\rtf", **{f: _ZIP for f in (
    "docx", "xlsx", "pptx", "odt", "ods", "odp")}}
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_TAIL = 65_557
_CHUNK = 1024 * 1024
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                          "127.0.0.0/8", "::1/128", "fc00::/7"))


class Engine(enum.Enum):
    """The engines a file says made it (``docProps/app.xml`` Application)."""
    LIBREOFFICE = "LibreOffice"
    COLLABORA = "Collabora_Office"


_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def engine_of_application(text: str | None) -> tuple[Engine | None, str]:
    """The engine and its "major.minor" from an Application string
    (``Collabora_Office/26.04.5.1$Linux_X86_64 LibreOffice_project/...``);
    (None, "") for anything else."""
    product, _, rest = (text or "").strip().partition("/")
    engine = next((e for e in Engine if e.value == product), None)
    if engine is None:
        return None, ""
    match = _VERSION_RE.match(rest)
    return engine, f"{match.group(1)}.{match.group(2)}" if match else ""


@functools.cache
def _base_url() -> str:
    """The Collabora address from the setting, service root included and no
    trailing slash; "" when unset or not an http(s) URL with a host."""
    raw = os.environ.get(URL_SETTING, "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in URL_SCHEMES or not parts.hostname or parts.query or parts.fragment:
        logger.warning("%s is not an http(s) address of Collabora, so conversions use LibreOffice", URL_SETTING)
        return ""
    if parts.scheme != TLS_SCHEME and not _private_host(parts.hostname):
        logger.warning("%s sends documents to %s over plain http: use https for a Collabora outside this "
                       "install's network", URL_SETTING, _redacted(raw))
    return raw


def _private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return any(address in net for net in _PRIVATE_NETS)


def _redacted(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}{f':{parts.port}' if parts.port else ''}{parts.path}"


@dataclass
class _Probe:
    at: float = float("-inf")
    up: bool = False
    known: bool = False
    # Collabora's 500s in a row since its last conversion.
    failed_loads: int = 0
    # After a timeout: down for every event loop until then, whatever its
    # probe answers (a full disk answers the probe and no conversion).
    down_until: float = float("-inf")


_probe = _Probe()


class _LoopState:
    """What one event loop's calls share: the probe lock, the one
    conversion slot, and the time before which Collabora may still be busy
    with a call we gave up on."""

    def __init__(self) -> None:
        self.probing = asyncio.Lock()
        self.slot = asyncio.Semaphore(1)
        self.busy_until = 0.0


_loops: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = weakref.WeakKeyDictionary()
_logged: set[str] = set()


def _state() -> _LoopState:
    loop = asyncio.get_running_loop()
    state = _loops.get(loop)
    if state is None:
        state = _loops[loop] = _LoopState()
    return state


def reset_backend() -> None:
    """Forget the setting, the probe and the logged failures (the setting is
    read once per process otherwise)."""
    _base_url.cache_clear()
    _probe.at, _probe.up, _probe.known, _probe.failed_loads = float("-inf"), False, False, 0
    _probe.down_until = float("-inf")
    _logged.clear()


def _client(connect: float):
    """A client that only bounds connecting: the caller's asyncio.timeout
    bounds the whole request and the streamed answer."""
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=connect), trust_env=False,
                             follow_redirects=False)


def _fresh(now: float) -> bool:
    if now < _probe.down_until:
        return True
    return _probe.known and now - _probe.at < (PROBE_TTL_UP_S if _probe.up else PROBE_TTL_DOWN_S)


def _set_probe(up: bool, why: str) -> None:
    changed = not _probe.known or up != _probe.up
    _probe.at, _probe.up, _probe.known = time.monotonic(), up, True
    if not changed:
        return
    if up:
        logger.info("Collabora at %s renders and recalculates documents", _redacted(_base_url()))
    else:
        logger.warning("Collabora at %s is not used for now (%s): LibreOffice renders instead",
                       _redacted(_base_url()), why)


async def _available() -> bool:
    if not _base_url():
        return False
    if _fresh(time.monotonic()):
        return _probe.up
    state = _state()
    async with state.probing:
        if not _fresh(time.monotonic()):
            up, why = await _probe_once()
            _set_probe(up, why)
    return _probe.up


async def _probe_once() -> tuple[bool, str]:
    try:
        async with asyncio.timeout(PROBE_TOTAL_S), _client(PROBE_CONNECT_S) as client, \
                client.stream("GET", _base_url() + PROBE_PATH) as response:
            if response.status_code != 200:
                return False, f"its capabilities answered HTTP {response.status_code}"
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > PROBE_MAX_BYTES:
                    return False, "its capabilities answer is too large"
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001  # any failure (httpx's own, a timeout, bad JSON) means not now
        return False, _reason(exc)
    convert = data.get("convert-to") if isinstance(data, dict) else None
    if not isinstance(convert, dict) or convert.get("available") is not True:
        return False, "it does not offer conversions to this service"
    return True, ""


def _reason(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__


def _log_once(kind: str, message: str, *args) -> None:
    if kind not in _logged:
        _logged.add(kind)
        logger.warning(message, *args)


# --- What may go to Collabora ------------------------------------------------


def _bounded_directory(tail: bytes, max_entries: int) -> bool:
    """True when the end record in ``tail`` (a file's last bytes) describes
    a directory within the bounds, with no zip64 record that zipfile would
    read instead."""
    pos = tail.rfind(_EOCD_SIGNATURE)
    if pos < 0 or len(tail) < pos + 22:
        return False
    if pos >= 20 and tail[pos - 20:pos - 16] == _ZIP64_LOCATOR:
        return False
    entries = int.from_bytes(tail[pos + 10:pos + 12], "little")
    directory = int.from_bytes(tail[pos + 12:pos + 16], "little")
    return entries < _ZIP64_ENTRIES and directory < _ZIP64_SIZE and entries <= max_entries \
        and directory <= SCAN_MAX_DIRECTORY


def _open_sendable(path: str):
    """``path`` opened with no link followed when Collabora may convert it
    (an Office Open XML package within the caps), else (None, why not)."""
    if os.path.splitext(path)[1].lower() not in OOXML_EXTENSIONS:
        return None, "not an Office Open XML file"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        return None, _reason(exc)
    fh = os.fdopen(fd, "rb")
    why = _package_refusal(fh)
    if why:
        fh.close()
        return None, why
    fh.seek(0)
    return fh, ""


def _package_refusal(fh) -> str:
    st = os.fstat(fh.fileno())
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_size > SEND_MAX_BYTES:
        return f"{st.st_size // (1024 * 1024)} MB, past the {SEND_MAX_BYTES // (1024 * 1024)} MB sent"
    fh.seek(max(0, st.st_size - _EOCD_TAIL))
    tail = fh.read()
    if _EOCD_SIGNATURE not in tail:
        return "not a zip package"
    if not _bounded_directory(tail, SEND_MAX_ENTRIES):
        return "too many parts"
    fh.seek(0)
    try:
        with zipfile.ZipFile(fh) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return "not a zip package"
    names = {info.filename for info in infos}
    if len(infos) > SEND_MAX_ENTRIES:
        return "too many parts"
    if "[Content_Types].xml" not in names or names & _ODF_MEMBERS:
        return "not an Office Open XML package"
    if sum(info.file_size for info in infos) > SEND_MAX_UNCOMPRESSED:
        return "too large once unpacked"
    return ""


@dataclass
class _Links:
    """What a package's relationship parts say before a render: external
    relationships that load content, and a part in another format
    (altChunk: HTML, RTF) that the engine imports as such."""
    outside: bool = False
    alt_chunk: bool = False


def _links(path: str) -> _Links:
    """What a package's relationship parts say before a conversion. A zip
    holding ``[Content_Types].xml`` is a package whatever its name (the
    engines tell a file by its content); any other file has nothing to
    remove. Read with no link followed, within the bounds above; a package
    past them or not readable here counts as linking outside, so the
    worker's copy decides."""
    found = _Links()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return found
    doubt = _Links(outside=True, alt_chunk=True)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            return found
        head = fh.read(len(_ZIP))
        fh.seek(max(0, st.st_size - _EOCD_TAIL))
        tail = fh.read()
        if head != _ZIP and _EOCD_SIGNATURE not in tail:
            return found
        if st.st_size > SCAN_MAX_FILE_BYTES or not _bounded_directory(tail, SCAN_MAX_ENTRIES):
            return doubt
        fh.seek(0)
        try:
            with zipfile.ZipFile(fh) as zf:
                infos = zf.infolist()
                if CONTENT_TYPES not in {info.filename for info in infos}:
                    return found
                read = 0
                for info in infos:
                    if not info.filename.endswith(".rels"):
                        continue
                    read += info.file_size
                    if info.file_size > RELS_SCAN_MAX_BYTES or read > RELS_SCAN_TOTAL_BYTES:
                        return doubt
                    leaves, chunk = render_copy.scan_relationships(zf.read(info))
                    found.outside |= leaves
                    found.alt_chunk |= chunk
        except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, EOFError, zlib.error):
            return doubt
    return found


async def _unlinked(path: str, folder: str, deadline: float) -> str:
    """A copy of ``path`` in ``folder`` (its own name) without the external
    relationships that load content; a copy that cannot be made is not
    rendered at all."""
    from isolation import run_parse
    from ooxml import render_copy

    copy = os.path.join(folder, os.path.basename(path))
    try:
        await run_parse(render_copy.prepare, path, copy, unlink=True, _deadline_at=deadline)
    except RuntimeError as exc:
        _log_once("unlinked", "A copy without a document's outside links could not be made (%s)", exc)
        raise RuntimeError(UNLINK_FAILED) from None
    return copy


# --- The convert-to call -----------------------------------------------------


class _TimedOut(Exception):
    """Collabora did not answer within the call's share, or did not start
    its answer within FIRST_ANSWER_S."""

    def __init__(self, after_s: float) -> None:
        super().__init__(f"no answer within {after_s:.0f} s")
        self.after_s = after_s


@dataclass
class _Attempt:
    path: str | None = None
    timed_out: bool = False


async def _via_collabora(path: str, fmt: str, out_dir: str, deadline: float, share_s: float, *,
                         drop_cached: bool = False, a4: bool = False, name: str = "",
                         alt_chunk: bool = False) -> _Attempt:
    """``path`` converted to ``fmt`` by Collabora into ``out_dir`` under the
    input's stem, or why not: an empty attempt when the call should go to
    LibreOffice (Collabora off or down, the file not one it gets, a failure),
    ``timed_out`` when Collabora used the call's share without answering."""
    state = _state()
    if time.monotonic() < state.busy_until or min(share_s, deadline - time.monotonic()) < MIN_SHARE_S:
        return _Attempt()
    if alt_chunk:
        # Collabora 26.04 fetches what an imported HTML part links to (its
        # stylesheet); the image's LibreOffice does not.
        return _Attempt()
    fh, why = await asyncio.to_thread(_open_sendable, path)
    if fh is None:
        logger.debug("LibreOffice renders %s: %s", os.path.basename(path), why)
        return _Attempt()
    if not await _available():
        fh.close()
        return _Attempt()
    ext = os.path.splitext(path)[1].lower()
    prepared = None
    try:
        if ext in WORKBOOK_EXTENSIONS and (drop_cached or a4):
            fh.close()
            prepared = await asyncio.to_thread(tempfile.mkdtemp, prefix="collabora-", dir=out_dir)
            fh = await _prepared(path, os.path.join(prepared, "workbook" + ext), deadline,
                                 drop_cached=drop_cached, a4=a4)
            if fh is None:
                return _Attempt()
        target = os.path.join(out_dir, f"{os.path.splitext(os.path.basename(name or path))[0]}.{fmt}")
        return await _send(fh, ext, fmt, target, state, min(share_s, deadline - time.monotonic()))
    finally:
        if fh is not None:
            fh.close()
        if prepared is not None:
            await asyncio.to_thread(shutil.rmtree, prepared, True)


async def _prepared(path: str, copy: str, deadline: float, *, drop_cached: bool, a4: bool):
    from isolation import run_parse
    from ooxml import render_copy

    try:
        await run_parse(render_copy.prepare, path, copy, drop_cached=drop_cached, a4=a4, _deadline_at=deadline)
    except RuntimeError as exc:
        _log_once("prepare", "A workbook copy for Collabora could not be made (%s): LibreOffice renders it", exc)
        return None
    fh, why = await asyncio.to_thread(_open_sendable, copy)
    if fh is None:
        _log_once("prepared", "A workbook copy for Collabora was refused (%s): LibreOffice renders it", why)
    return fh


async def _send(fh, ext: str, fmt: str, target: str, state: _LoopState, share_s: float) -> _Attempt:
    began = time.monotonic()
    try:
        await asyncio.wait_for(state.slot.acquire(), min(COLLABORA_QUEUE_WAIT_S, max(0.0, share_s)))
    except TimeoutError:
        return _Attempt()
    sent = False
    try:
        if time.monotonic() < state.busy_until:
            return _Attempt()
        sent = True
        await _post(fh, ext, fmt, target, share_s - (time.monotonic() - began))
        _probe.failed_loads = 0
        logger.info("Collabora converted a %s file to %s in %.2f s", ext, fmt, time.monotonic() - began)
        return _Attempt(path=target)
    except asyncio.CancelledError:
        if sent:
            state.busy_until = began + COLLABORA_SERVER_LIMIT_S
        _remove(target)
        raise
    except _TimedOut as exc:
        state.busy_until = began + COLLABORA_SERVER_LIMIT_S
        _remove(target)
        _down("timeout", f"no answer within {exc.after_s:.0f} s")
        _probe.down_until = began + COLLABORA_SERVER_LIMIT_S
        return _Attempt(timed_out=True)
    except _FileRefused as exc:
        _remove(target)
        _log_once(f"refused:{exc.status}", "Collabora could not convert a file (HTTP %s): LibreOffice renders "
                  "it", exc.status)
        if exc.status == FAILED_LOAD_STATUS:
            _probe.failed_loads += 1
            if _probe.failed_loads >= FAILED_LOADS_DOWN:
                _probe.failed_loads = 0
                _down("failed loads", f"it could not load {FAILED_LOADS_DOWN} files in a row")
        return _Attempt()
    except Exception as exc:  # noqa: BLE001  # any failure of the Collabora path falls back to LibreOffice
        _remove(target)
        _down(exc.__class__.__name__, _reason(exc))
        return _Attempt()
    finally:
        state.slot.release()


class _FileRefused(Exception):
    """Collabora answered that it cannot convert this one file."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _Unusable(Exception):
    """An answer that says Collabora cannot be used for now."""


def _down(kind: str, why: str) -> None:
    _set_probe(False, why)
    _log_once(f"down:{kind}", "A Collabora conversion failed (%s): LibreOffice renders instead", why)


async def _post(fh, ext: str, fmt: str, target: str, total_s: float) -> None:
    if total_s <= 0:
        raise _TimedOut(0.0)
    url = f"{_base_url()}{CONVERT_PATH}{fmt}"
    files = {"data": ("document" + ext, fh, "application/octet-stream")}
    first_s = min(FIRST_ANSWER_S, total_s)
    waited = total_s
    try:
        async with asyncio.timeout(total_s), _client(CONNECT_S) as client:
            waited = first_s
            async with asyncio.timeout(first_s):
                response = await client.send(client.build_request("POST", url, files=files, data={"lang": LANG}),
                                             stream=True)
            waited = total_s
            try:
                # Collabora answers 400 or 500 for a file it cannot load (a
                # DOCTYPE, a broken package): that file's, not the service's.
                if response.status_code in (*FILE_REFUSED_STATUSES, FAILED_LOAD_STATUS):
                    raise _FileRefused(response.status_code)
                if response.status_code != 200:
                    raise _Unusable(f"HTTP {response.status_code}")
                await _save(response, target, _MAGIC.get(fmt, b""), fmt)
            finally:
                await response.aclose()
    except TimeoutError:
        raise _TimedOut(waited) from None
    except Exception as exc:
        if exc.__class__.__name__ in _HTTPX_TIMEOUTS:
            raise _TimedOut(waited) from None
        raise


async def _save(response, target: str, magic: bytes, fmt: str) -> None:
    """The answer streamed into ``target``: capped, and refused unless it
    starts as ``fmt`` does."""
    out = await asyncio.to_thread(open, target, "wb")
    try:
        written = 0
        head = b""
        async for chunk in response.aiter_bytes(_CHUNK):
            written += len(chunk)
            if written > ANSWER_MAX_BYTES:
                raise _Unusable(f"an answer past {ANSWER_MAX_BYTES // (1024 * 1024)} MB")
            if len(head) < len(magic):
                head += chunk[:len(magic)]
            await asyncio.to_thread(out.write, chunk)
    finally:
        await asyncio.to_thread(out.close)
    if not written:
        raise _Unusable("an empty answer")
    if not head.startswith(magic):
        raise _Unusable(f"an answer that is not {fmt}")


def _remove(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


# --- LibreOffice: one profile per slot, the slots by memory ------------------
#
# Two LibreOffice processes on one user profile cannot run at once (the
# second exits with no output), so each conversion slot has a profile of its
# own and a conversion waits for a free slot. The slots follow the parse
# workers' permits: two on a container of 3 GiB or more or with no memory
# limit, one on a smaller one, where a second soffice beside the warm
# background model and a parse worker would pass the container's memory. A
# conversion writes only into the output directory its caller made for it,
# so two conversions of one file run side by side.

_LO_TIMEOUT_S = 120
# How long a conversion waits for a free slot: past two whole runs, a slot
# that never comes back is an error the caller sees, never a hang.
_LO_SLOT_WAIT_S = 300

# The profile the headless conversions run under: formulas are never
# recalculated on load, links are never updated, macros never run, and a
# linked graphic is never loaded (an HTML image reference, an ODF or OOXML
# linked picture: LibreOffice loads them from any path it can read unless
# BlockUntrustedRefererLinks is set; an embedded picture still renders). The
# document under conversion is untrusted input; LibreOffice is inert on the
# first three by default and the profile pins them.
_LO_PROFILE_DIR = os.environ.get("FILETOOLS_LO_PROFILE", "/tmp/file-tools-lo-profile")
_LO_REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
</oor:items>
"""
# The two kinds of run: a conversion never recalculates; a recalculation
# (0 = recalculate always) differs in the two recalculation pins only, and
# its profile is its own, so alternating kinds never rewrite a profile.
LO_CONVERT = "convert"
LO_RECALC = "recalc"
_RECALC_NEVER = '<prop oor:name="{}" oor:op="fuse"><value>1</value></prop>'
_RECALC_ALWAYS = '<prop oor:name="{}" oor:op="fuse"><value>0</value></prop>'
_LO_REGISTRIES = {
    LO_CONVERT: _LO_REGISTRY,
    LO_RECALC: _LO_REGISTRY.replace(_RECALC_NEVER.format("ODFRecalcMode"), _RECALC_ALWAYS.format("ODFRecalcMode"))
                           .replace(_RECALC_NEVER.format("OOXMLRecalcMode"), _RECALC_ALWAYS.format("OOXMLRecalcMode")),
}


def _lo_slot_count() -> int:
    from isolation import parse_permits

    return parse_permits()


_lo_free: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Queue] = weakref.WeakKeyDictionary()
# The runs in flight: a run outlives a caller that stopped waiting.
_lo_runs: set[asyncio.Task] = set()


def _lo_free_slots() -> asyncio.Queue:
    """This loop's free slot numbers (one queue per event loop)."""
    loop = asyncio.get_running_loop()
    free = _lo_free.get(loop)
    if free is None:
        free = _lo_free[loop] = asyncio.Queue()
        for slot in range(_lo_slot_count()):
            free.put_nowait(slot)
    return free


def _libreoffice_profile(slot: int, kind: str = LO_CONVERT) -> str:
    """The user-installation directory of one slot's runs of ``kind``, its
    registry seeded with that kind's pins (written once, rewritten on
    drift)."""
    profile = os.path.join(_LO_PROFILE_DIR, f"slot-{slot}-{kind}")
    user = os.path.join(profile, "user")
    os.makedirs(user, exist_ok=True)
    xcu = os.path.join(user, "registrymodifications.xcu")
    try:
        current = open(xcu, encoding="utf-8").read()
    except OSError:
        current = ""
    if current != _LO_REGISTRIES[kind]:
        with open(xcu, "w", encoding="utf-8") as fh:
            fh.write(_LO_REGISTRIES[kind])
    return profile


async def _libreoffice_convert(input_path: str, output_format: str, output_dir: str, *, kind: str = LO_CONVERT,
                               extension: str | None = None, slot_wait_s: float | None = None,
                               timeout_s: float | None = None) -> str:
    """Convert a file with LibreOffice headless. Returns output path. The
    caller passes an ``output_dir`` outside the agent tree (a temp directory)
    and lands the result through ``safe_open_write``: LibreOffice writes by
    name and must never write into the tree itself. ``output_format`` may
    name a filter (``xlsx:Calc MS Excel 2007 XML``); ``extension`` is then the
    output's suffix. ``kind`` picks the profile (LO_CONVERT or LO_RECALC).

    The run is a task of its own: a caller that stops waiting (the client
    went away, a budget ran out) ends it, and the run still ends its
    processes and waits for them before it frees its slot."""
    run = asyncio.ensure_future(_libreoffice_run(
        input_path, output_format, output_dir, kind=kind,
        slot_wait_s=_LO_SLOT_WAIT_S if slot_wait_s is None else slot_wait_s,
        timeout_s=_LO_TIMEOUT_S if timeout_s is None else timeout_s))
    _lo_runs.add(run)
    run.add_done_callback(_lo_runs.discard)
    try:
        await asyncio.shield(run)
    except asyncio.CancelledError:
        # The caller removes the output folder next: wait (bounded) until the
        # run has ended LibreOffice, so nothing still writes into it.
        run.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait({run}, timeout=_LO_CANCEL_WAIT_S)
        raise
    stem = Path(input_path).stem
    out = Path(output_dir) / f"{stem}.{extension or output_format}"
    if not out.exists():
        raise RuntimeError(f"Conversion produced no output file at {out}")
    return str(out)


async def _libreoffice_run(input_path: str, output_format: str, output_dir: str, *, kind: str = LO_CONVERT,
                           slot_wait_s: float = _LO_SLOT_WAIT_S, timeout_s: float = _LO_TIMEOUT_S) -> None:
    free = _lo_free_slots()
    try:
        slot = await asyncio.wait_for(free.get(), slot_wait_s)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"LibreOffice is busy: no conversion slot came free within {slot_wait_s:g}s"
        ) from exc
    proc = None
    try:
        # The slot is held: a drift rewrite never lands while a run reads it.
        profile = await asyncio.to_thread(_libreoffice_profile, slot, kind)
        # Its own process group: the wrapper starts oosplash and soffice.bin,
        # and ending the run ends all three.
        proc = await asyncio.create_subprocess_exec(
            *_oom_first(), "libreoffice", f"-env:UserInstallation=file://{profile}",
            "--headless", "--norestore", "--convert-to",
            output_format, "--outdir", output_dir, input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"LibreOffice conversion timed out ({timeout_s:g}s)") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"LibreOffice error: {stderr.decode()[:500]}")
    finally:
        # Ending the group and freeing the slot is one task of its own: a
        # cancel that arrives while a lingering group is ended (the person
        # stops a conversion that just timed out) leaves it running, and the
        # slot still comes back once the group is gone.
        cleanup = asyncio.ensure_future(_end_and_free(proc, free, slot))
        _lo_runs.add(cleanup)
        cleanup.add_done_callback(_lo_runs.discard)
        await asyncio.shield(cleanup)


_LO_CANCEL_WAIT_S = 10.0


@functools.cache
def _oom_first() -> tuple[str, ...]:
    """LibreOffice runs with the highest OOM score where ``choom`` exists:
    a document that balloons is killed before the server."""
    return ("choom", "-n", "1000", "--") if shutil.which("choom") and shutil.which("libreoffice") else ()


async def _end_and_free(proc, free: asyncio.Queue, slot: int) -> None:
    try:
        if proc is not None and proc.returncode is None:
            await _end_process_group(proc)
    finally:
        free.put_nowait(slot)


async def _end_process_group(proc) -> None:
    """End a run's whole process group and wait, five seconds at most, until
    none of it is left, so the next run on the slot's profile never meets a
    dying instance. The grandchildren are reaped by the image's init."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        await asyncio.sleep(0.05)
    logger.warning("LibreOffice run %s: its processes outlived the 5 s wait", proc.pid)


# --- The calls ---------------------------------------------------------------


def output_folder(work_dir: str, name: str) -> str:
    """``name``, a plain folder name, made inside ``work_dir`` (once) and
    returned: where a write renders its staged file, which lives in the
    work folder itself (an output keeps its input's name, so it never goes
    to the input's own folder)."""
    if not name or os.path.basename(name) != name or name in (os.curdir, os.pardir):
        raise ValueError(f"'{name}' is not a plain folder name")
    folder = os.path.join(work_dir, name)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return folder


def _owned_folder(path: str, out_dir: str) -> None:
    if not os.path.isdir(out_dir):
        raise ValueError(f"the output folder {out_dir} does not exist")
    if os.path.realpath(os.path.dirname(path)) == os.path.realpath(out_dir):
        raise ValueError("the output folder must not be the input's own folder (the output keeps the input's name)")


async def _libreoffice(path: str, output_format: str, out_dir: str, *, kind: str, extension: str | None,
                       slot_wait_s: float, timeout_s: float, deadline: float | None) -> str:
    """LibreOffice with the caller's limits, inside ``deadline`` when one
    applies (a Collabora attempt spent part of the call)."""
    if deadline is None:
        return await _libreoffice_convert(path, output_format, out_dir, kind=kind, extension=extension,
                                                 slot_wait_s=slot_wait_s, timeout_s=timeout_s)
    left = deadline - time.monotonic()
    if left <= 0:
        raise RuntimeError("the conversion ran out of time")
    try:
        async with asyncio.timeout(left):
            return await _libreoffice_convert(path, output_format, out_dir, kind=kind, extension=extension,
                                                     slot_wait_s=min(slot_wait_s, left),
                                                     timeout_s=min(timeout_s, left))
    except TimeoutError:
        raise RuntimeError(f"the conversion timed out ({left:.0f}s)") from None


async def _render(path: str, fmt: str, out_dir: str, *, share_s: float, kind: str, lo_format: str,
                  extension: str | None, slot_wait_s: float, timeout_s: float, deadline: float | None,
                  drop_cached: bool = False, a4: bool = False, collabora: bool = True) -> str:
    end = deadline if deadline is not None else time.monotonic() + slot_wait_s + timeout_s
    links = await asyncio.to_thread(_links, path)
    folder = None
    source = path
    try:
        if links.outside:
            folder = await asyncio.to_thread(tempfile.mkdtemp, prefix="unlinked-", dir=out_dir)
            source = await _unlinked(path, folder, end)
            deadline = end
        if collabora and _base_url():
            attempt = await _via_collabora(source, fmt, out_dir, end, share_s, drop_cached=drop_cached, a4=a4,
                                           name=path, alt_chunk=links.alt_chunk)
            if attempt.path is not None:
                return attempt.path
            if attempt.timed_out and end - time.monotonic() < MIN_FALLBACK_S:
                raise RuntimeError(f"the conversion timed out ({share_s:.0f}s)")
            deadline = end
        return await _libreoffice(source, lo_format, out_dir, kind=kind, extension=extension,
                                  slot_wait_s=slot_wait_s, timeout_s=timeout_s, deadline=deadline)
    finally:
        if folder is not None:
            await asyncio.to_thread(shutil.rmtree, folder, True)


async def to_pdf(path: str, *, out_dir: str, timeout_s: float = CONVERT_RUN_S,
                 slot_wait_s: float = CONVERT_SLOT_WAIT_S, deadline: float | None = None) -> str:
    """``path`` (docx, xlsx, pptx, odt and the rest LibreOffice reads)
    converted to a PDF in ``out_dir``; its path. ``deadline`` (monotonic)
    bounds the whole call, else the slot wait and the run do."""
    _owned_folder(path, out_dir)
    return await _render(path, PDF, out_dir, share_s=COLLABORA_RUN_S, kind=LO_CONVERT, lo_format=PDF,
                         extension=None, slot_wait_s=slot_wait_s, timeout_s=timeout_s, deadline=deadline, a4=True)


async def convert(path: str, fmt: str, *, out_dir: str, timeout_s: float = CONVERT_RUN_S,
                  slot_wait_s: float = CONVERT_SLOT_WAIT_S, deadline: float | None = None) -> str:
    """``path`` converted to ``fmt`` in ``out_dir`` under the input's stem;
    its path. ``fmt`` is a target extension (pdf, docx, odt, xlsx, csv,
    pptx, html, txt...), or LibreOffice's "extension:Filter name", which
    only LibreOffice makes. Collabora makes the COLLABORA_FORMATS."""
    _owned_folder(path, out_dir)
    target, _, chosen_filter = fmt.partition(":")
    lo_format = fmt if chosen_filter else LIBREOFFICE_FILTERS.get(target, target)
    return await _render(path, target, out_dir, share_s=COLLABORA_RUN_S, kind=LO_CONVERT, lo_format=lo_format,
                         extension=target if lo_format != target else None, slot_wait_s=slot_wait_s,
                         timeout_s=timeout_s, deadline=deadline, a4=target in PRINT_FORMATS,
                         collabora=not chosen_filter and target in COLLABORA_FORMATS)


OOXML_FILTERS = {
    ".docx": "docx:MS Word 2007 XML",
    ".xlsx": "xlsx:Calc MS Excel 2007 XML",
    ".pptx": "pptx:Impress MS PowerPoint 2007 XML",
}


async def to_ooxml(path: str, target_ext: str, *, out_dir: str, timeout_s: float = CONVERT_RUN_S,
                   slot_wait_s: float = CONVERT_SLOT_WAIT_S) -> str:
    """``path`` (a legacy .doc, .xls or .ppt, or anything LibreOffice
    reads) saved as ``target_ext`` (.docx, .xlsx or .pptx) in ``out_dir``;
    its path. Always this image's LibreOffice, under the convert profile:
    macros off, links never updated."""
    if target_ext not in OOXML_FILTERS:
        raise ValueError(f"to_ooxml makes {', '.join(OOXML_FILTERS)}, not {target_ext}")
    _owned_folder(path, out_dir)
    return await _libreoffice_convert(path, OOXML_FILTERS[target_ext], out_dir, kind=LO_CONVERT,
                                             extension=target_ext.lstrip("."), slot_wait_s=slot_wait_s,
                                             timeout_s=timeout_s)


async def recalc(path: str, *, out_dir: str, timeout_s: float = RECALC_RUN_S,
                 slot_wait_s: float = RECALC_SLOT_WAIT_S, deadline: float | None = None,
                 cached_values: bool = True) -> str:
    """``path`` (a workbook) opened with every formula recalculated and saved
    as xlsx in ``out_dir``: the computed values cached beside the formulas.
    Bounded by the recalculation limits even when the caller allows more.
    ``out_dir`` is never the input's own folder; a write's render phase
    passes ``output_folder(call.work_dir, "recalc")``. ``cached_values``
    False says the workbook holds no cached value (an openpyxl save), so
    Collabora gets it as it is. The engine that computed the values is
    named in the result's ``docProps/app.xml`` (``engine_of_application``)."""
    _owned_folder(path, out_dir)
    slot_wait_s = min(slot_wait_s, RECALC_SLOT_WAIT_S)
    timeout_s = min(timeout_s, RECALC_RUN_S)
    return await _render(path, XLSX, out_dir, share_s=COLLABORA_RECALC_S, kind=LO_RECALC,
                         lo_format=XLSX_FILTER, extension=XLSX, slot_wait_s=slot_wait_s, timeout_s=timeout_s,
                         deadline=deadline, drop_cached=cached_values)
