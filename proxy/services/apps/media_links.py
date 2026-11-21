"""An app's media links: which files a page may show, and the short-lived
addresses it shows them through.

A page in an app's frame cannot send a cookie, so it loads a workspace
video, audio file or picture through an address that carries a secret
(``api/apps/app_media.py`` serves it). This module holds the two things
that are not a gate: the table of INERT KINDS (what may ever be answered,
by the name and by the leading bytes) and the table of LINKS.

A link is made for one viewer, one app and one file by the viewer's own
signed-in page (``catalog``'s ``files.media``), and every request on it is
judged again by the same gates. Here it is only a lease:

- the address answers while its LEASE runs (``LEASE_S``); a new ask for the
  same viewer, app and file gives the lease again on the SAME address,
  lapsed or not, so a player's ``src`` never changes while it is used,
  except that the address names the file's VERSION: a browser keeps what it
  drew for an address, so a file rewritten under its name is answered under
  a new one (the same link, its lease and its secret);
- after ``MAX_LIFE_S`` from its birth an ask makes a new address, and the
  old one lives out the lease it has;
- the table is bounded per viewer and app, per app and in all; over a bound
  it drops lapsed links first, then pictures by last use, never one with an
  answer in flight.

In memory, one process: a restart forgets every link and the page asks
again. Nothing here is a secret worth keeping: the lock guards the table
because a link is made on a worker thread and found on the event loop.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from typing import NamedTuple

LEASE_S = 120.0
MAX_LIFE_S = 1800.0
MAX_PER_VIEWER = 256
MAX_PER_APP = 4000
MAX_TOTAL = 20000
# How many files one ``files.media`` call may name.
BATCH_MAX = 32
# What the kind check reads from the opened file.
HEAD_BYTES = 64
# Dead links taken from the front of the table in one call, at most.
_SWEEP_MAX = 256
# At the whole table's bound a lapsed link is looked for among this many of
# the oldest links. When they are all live the whole table is read, once in
# ``SCAN_EVERY_S`` at most, and up to ``_SWEEP_MAX`` lapsed links go at once.
_OLDEST = 2048
SCAN_EVERY_S = 1.0

VIDEO, AUDIO, IMAGE = "video", "audio", "image"


# ── the kinds ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Kind:
    kind: str
    mime: str
    check: Callable[[bytes], bool]


def _starts(*magics: bytes) -> Callable[[bytes], bool]:
    return lambda head: head.startswith(magics)


def _riff(form: bytes) -> Callable[[bytes], bool]:
    return lambda head: head[:4] == b"RIFF" and head[8:12] == form


def _brands(head: bytes) -> list[bytes]:
    """The major and the compatible brands of a leading ``ftyp`` box, as
    far as the bytes read reach."""
    if len(head) < 12 or head[4:8] != b"ftyp":
        return []
    end = min(len(head), int.from_bytes(head[:4], "big"))
    return [head[8:12]] + [head[i:i + 4] for i in range(16, end - 3, 4)]


def _iso_media(head: bytes) -> bool:
    # QuickTime's own brand is a file not every browser plays, and the
    # platform's player converts it: never answered as MP4.
    brands = _brands(head)
    return bool(brands) and brands[0] != b"qt  "


def _avif(head: bytes) -> bool:
    return any(b in (b"avif", b"avis") for b in _brands(head))


def _mp3(head: bytes) -> bool:
    if head[:3] == b"ID3":
        return True
    # An MPEG audio frame header: eleven sync bits, a version that is not
    # the reserved one, and Layer III. A text file's byte-order mark
    # (FF FE) reads as Layer I and fails.
    return (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0
            and head[1] & 0x18 != 0x08 and head[1] & 0x06 == 0x02)


_EBML = _starts(b"\x1a\x45\xdf\xa3")
_OGG = _starts(b"OggS")

# The whole allowlist: what a browser plays or draws and can never run. No
# page, no SVG, no PDF, no script, no style sheet and no playlist (a browser
# follows what a playlist names) has a suffix here.
KINDS: dict[str, Kind] = {
    ".mp4": Kind(VIDEO, "video/mp4", _iso_media),
    ".m4v": Kind(VIDEO, "video/mp4", _iso_media),
    ".webm": Kind(VIDEO, "video/webm", _EBML),
    ".m4a": Kind(AUDIO, "audio/mp4", _iso_media),
    ".mp3": Kind(AUDIO, "audio/mpeg", _mp3),
    ".wav": Kind(AUDIO, "audio/wav", _riff(b"WAVE")),
    ".ogg": Kind(AUDIO, "audio/ogg", _OGG),
    ".oga": Kind(AUDIO, "audio/ogg", _OGG),
    ".opus": Kind(AUDIO, "audio/ogg", _OGG),
    ".flac": Kind(AUDIO, "audio/flac", _starts(b"fLaC")),
    ".weba": Kind(AUDIO, "audio/webm", _EBML),
    ".jpg": Kind(IMAGE, "image/jpeg", _starts(b"\xff\xd8\xff")),
    ".jpeg": Kind(IMAGE, "image/jpeg", _starts(b"\xff\xd8\xff")),
    ".png": Kind(IMAGE, "image/png", _starts(b"\x89PNG\r\n\x1a\n")),
    ".gif": Kind(IMAGE, "image/gif", _starts(b"GIF87a", b"GIF89a")),
    ".webp": Kind(IMAGE, "image/webp", _riff(b"WEBP")),
    ".avif": Kind(IMAGE, "image/avif", _avif),
}


class MediaFile(NamedTuple):
    """A file a page may show, opened: the caller serves ``fd`` and closes it."""
    fd: int
    stat: os.stat_result
    rel: str
    kind: str
    mime: str


def kind_of(name: str) -> Kind | None:
    """The kind a file's NAME claims; the bytes are judged by its ``check``."""
    leaf = name.rsplit("/", 1)[-1]
    dot = leaf.rfind(".")
    return KINDS.get(leaf[dot:].lower()) if dot > 0 else None


# ── the links ───────────────────────────────────────────────────────────────


class TableFull(Exception):
    """No room for a new link and nothing that may be dropped."""


@dataclass
class Link:
    secret: str
    app_id: str
    sub: str
    path: str            # the page's own argument, gated again at every request
    rel: str             # the place it resolved to when the link was made
    kind: str
    born: float          # monotonic
    lease_until: float   # monotonic
    judged_at: int       # epoch second of the last ask by the signed-in page
    used: float          # monotonic
    in_flight: int = 0

    def live(self, now: float) -> bool:
        return now < self.lease_until

    def address(self, st: os.stat_result) -> str:
        """The path a page loads for the file as ``st`` found it. The secret
        is the ``t`` parameter, which the log queue's redaction removes from
        every line; the route reads nothing else."""
        return f"/v1/apps/{self.app_id}/client/media?v={version(st)}&t={self.secret}"


def version(st: os.stat_result) -> str:
    """What changes in a file's address when the file is rewritten in place
    or replaced: its inode, size and modification time, hashed. No secret,
    and nothing a page cannot read."""
    seen = f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}".encode()
    return hashlib.blake2s(seen, digest_size=6).hexdigest()


_lock = threading.Lock()
_links: dict[str, Link] = {}                       # by secret, oldest first
_by_file: dict[tuple[str, str, str], str] = {}     # (app, sub, rel) → secret
_by_viewer: dict[tuple[str, str], set[str]] = {}
_by_app: dict[str, set[str]] = {}
_scanned_at = 0.0   # monotonic, the last read of the whole table for lapsed links


def _forget(link: Link) -> None:
    _links.pop(link.secret, None)
    key = (link.app_id, link.sub, link.rel)
    if _by_file.get(key) == link.secret:
        del _by_file[key]
    viewer = (link.app_id, link.sub)
    mine = _by_viewer.get(viewer)
    if mine is not None:
        mine.discard(link.secret)
        if not mine:
            del _by_viewer[viewer]
    apps = _by_app.get(link.app_id)
    if apps is not None:
        apps.discard(link.secret)
        if not apps:
            del _by_app[link.app_id]


def _victim(held: Iterable[str], now: float) -> Link | None:
    """The link to drop for a new one: a lapsed one, else the picture used
    longest ago, else whatever was used longest ago. Never one with an
    answer in flight."""
    best: Link | None = None
    best_rank: tuple[int, float] | None = None
    for secret in held:
        link = _links.get(secret)
        if link is None or link.in_flight:
            continue
        rank = (0 if not link.live(now) else 1 if link.kind == IMAGE else 2, link.used)
        if best_rank is None or rank < best_rank:
            best, best_rank = link, rank
    return best


def _lapsed(links: Iterable[Link], now: float) -> Iterator[Link]:
    return (link for link in links if not link.live(now) and not link.in_flight)


def _drop(victim: Link | None) -> None:
    if victim is None:
        raise TableFull
    _forget(victim)


def _make_room(app_id: str, sub: str, now: float) -> None:
    # Links past their life sit at the front (the table is in birth order).
    for _ in range(_SWEEP_MAX):
        first = next(iter(_links.values()), None)
        if first is None or now - first.born < MAX_LIFE_S or first.live(now) or first.in_flight:
            break
        _forget(first)
    mine = _by_viewer.get((app_id, sub))
    if mine is not None and len(mine) >= MAX_PER_VIEWER:
        _drop(_victim(mine, now))
    held = _by_app.get(app_id)
    if held is not None and len(held) >= MAX_PER_APP:
        # For the app's sake a person gives up a lapsed link of anyone or a
        # link of their own, never a link another person's page still uses.
        lapsed = next((link for link in map(_links.get, held)
                       if link is not None and not link.live(now) and not link.in_flight), None)
        _drop(lapsed or _victim(_by_viewer.get((app_id, sub), ()), now))
    if len(_links) >= MAX_TOTAL:
        # Only a lapsed link is dropped for another app's sake.
        lapsed = next(_lapsed(islice(_links.values(), _OLDEST), now), None)
        if lapsed is not None:
            _forget(lapsed)
            return
        global _scanned_at
        if now - _scanned_at < SCAN_EVERY_S:
            raise TableFull
        _scanned_at = now
        found = list(islice(_lapsed(_links.values(), now), _SWEEP_MAX))
        if not found:
            raise TableFull
        for link in found:
            _forget(link)


def issue(app_id: str, sub: str, path: str, rel: str, kind: str) -> tuple[Link, int]:
    """The link for this viewer, app and file with a fresh lease, and the
    seconds that lease runs: the same address while the link is younger
    than its life, a new one after. ``TableFull`` when there is no room."""
    with _lock:
        now = time.monotonic()
        key = (app_id, sub, rel)
        link = _links.get(_by_file.get(key, ""))
        # Close to its end an address is not given again: the lease left
        # would be too short to play on, so the new address starts now and
        # the old one lives out what it has.
        if link is None or now - link.born >= MAX_LIFE_S - LEASE_S / 2:
            _make_room(app_id, sub, now)
            link = Link(secret=secrets.token_urlsafe(32), app_id=app_id, sub=sub, path=path,
                        rel=rel, kind=kind, born=now, lease_until=now, judged_at=0, used=now)
            _links[link.secret] = link
            _by_file[key] = link.secret
            _by_viewer.setdefault((app_id, sub), set()).add(link.secret)
            _by_app.setdefault(app_id, set()).add(link.secret)
        # The lease as a length first: read back as ``lease_until - now`` a
        # whole lease falls just under itself in floating point.
        lease = min(LEASE_S, link.born + MAX_LIFE_S - now)
        link.lease_until = now + lease
        link.judged_at = int(time.time())
        link.path = path
        return link, max(1, int(lease))


def find(secret: str, app_id: str) -> Link | None:
    """The link ``secret`` names on ``app_id`` while its lease runs."""
    with _lock:
        link = _links.get(secret)
        now = time.monotonic()
        if link is None or link.app_id != app_id or not link.live(now):
            return None
        link.used = now
        return link


def enter(link: Link) -> None:
    """Count one answer in flight on the link: the table keeps it meanwhile."""
    with _lock:
        link.in_flight += 1


def leave(link: Link) -> None:
    with _lock:
        link.in_flight = max(0, link.in_flight - 1)


def drop_person(sub: str) -> None:
    """Every link of a person whose sign-ins ended."""
    with _lock:
        for link in [link for link in _links.values() if link.sub == sub]:
            _forget(link)


def drop_app(app_id: str) -> int:
    """Every link of an app that was unpinned or purged."""
    with _lock:
        gone = [_links[s] for s in list(_by_app.get(app_id, ())) if s in _links]
        for link in gone:
            _forget(link)
        return len(gone)


def reset() -> None:
    """Forget everything (the tests' clean start)."""
    global _scanned_at
    with _lock:
        _scanned_at = 0.0
        _links.clear()
        _by_file.clear()
        _by_viewer.clear()
        _by_app.clear()
