"""Dashboard asset serving: precompressed siblings and the cache policy.

The dashboard build writes ``<file>.br`` and ``<file>.gz`` next to every text
asset it emits (``dist/assets`` and ``dist/ui-kit``). This module serves
whichever representation the client accepts, with the identity file's media
type and an ETag per representation, and stamps far-future immutable caching
on content-hashed names. Nothing compresses at request time: a missing
sibling means the identity file goes out exactly as before.
"""

from __future__ import annotations

import mimetypes
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from email.utils import parsedate

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# Vite's content hash: eight url-safe base64 characters before the extension
# (``index-XGlNw2dg.js``, ``comfortaa-latin-400-normal--KakQCjT.woff2``).
_HASHED_NAME = re.compile(r"-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$")

# Server preference order; the client's q-values only include or exclude.
_ENCODINGS = (("br", ".br"), ("gzip", ".gz"))


def is_hashed_asset(name: str) -> bool:
    """True for Vite output names, false for the unhashed files copied from
    ``public/`` (``favicon.png``), which must stay revalidated."""
    return _HASHED_NAME.search(name) is not None


# The build stamps its identity into index.html (``<meta name="otodock-build"
# content="<id>">`` — a hash of the built page and the unhashed public files).
# The dashboard socket repeats it on connect and in every pong, and /health
# carries it, so a running page can tell it is stale and reload once.
_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_META_NAME = re.compile(r"\bname=[\"']otodock-build[\"']", re.I)
_META_CONTENT = re.compile(r"\bcontent=[\"']([^\"']+)[\"']", re.I)
_build_cache: dict[str, tuple[int, int, str]] = {}


def _parse_build_stamp(text: str) -> str:
    """The content of the ``otodock-build`` meta tag, whatever the attribute
    order or quoting; "" when absent."""
    for tag in _META_TAG.finditer(text):
        if _META_NAME.search(tag.group(0)):
            m = _META_CONTENT.search(tag.group(0))
            return m.group(1) if m else ""
    return ""


def dashboard_build_id(dist: os.PathLike[str] | str | None = None) -> str:
    """The build id stamped into ``<dist>/index.html``; "" when there is no
    dist, no index (a build empties the directory first) or no stamp (a dev
    server page) — and "" never triggers a reload on the client.

    Cached per path on (mtime_ns, size), re-read when either changes, so a
    rebuilt dist is seen on the next call without a restart. A read that
    straddles a write (stat differs before and after) is served but not
    cached."""
    if dist is None:
        import config  # local: this module must stay importable standalone

        dist = config.DASHBOARD_DIST
    index = os.path.join(os.fspath(dist), "index.html")
    try:
        st1 = os.stat(index)
        cached = _build_cache.get(index)
        if cached and cached[0] == st1.st_mtime_ns and cached[1] == st1.st_size:
            return cached[2]
        with open(index, encoding="utf-8", errors="replace") as f:
            text = f.read()
        st2 = os.stat(index)
    except OSError:
        _build_cache.pop(index, None)
        return ""
    build = _parse_build_stamp(text)
    if (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size):
        _build_cache[index] = (st1.st_mtime_ns, st1.st_size, build)
    return build


def parse_accept_encoding(values: Iterable[str]) -> set[str]:
    """The encodings the client accepts, lower-cased; ``q=0`` excludes.

    ``identity`` and ``*`` are never returned: only an encoding the client
    named is ever added to a response, so a wildcard falls back to identity.
    A malformed ``q`` excludes the token for the same reason.
    """
    accepted: set[str] = set()
    for value in values:
        for token in value.split(","):
            name, _, params = token.strip().partition(";")
            name = name.strip().lower()
            if not name or name in ("identity", "*"):
                continue
            q = 1.0
            for param in params.split(";"):
                key, _, raw = param.strip().partition("=")
                if key.strip().lower() != "q":
                    continue
                try:
                    q = float(raw.strip())
                except ValueError:
                    q = 0.0
            if q > 0:
                accepted.add(name)
    return accepted


def _regular_file(path: str) -> os.stat_result | None:
    """The stat of ``path`` if it is a regular file and not a symlink: the
    identity path was confined by the caller, and a sibling must not be a
    link that points outside that directory."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def _is_not_modified(response_headers: Headers, request_headers: Headers) -> bool:
    # Same rule as StaticFiles.is_not_modified, usable from a plain route.
    if if_none_match := request_headers.get("if-none-match"):
        etag = response_headers["etag"]
        return etag in [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    try:
        if_modified_since = parsedate(request_headers["if-modified-since"])
        last_modified = parsedate(response_headers["last-modified"])
    except KeyError:
        return False
    return (
        if_modified_since is not None
        and last_modified is not None
        and if_modified_since >= last_modified
    )


def negotiated_file_response(
    path: str | os.PathLike[str],
    stat_result: os.stat_result,
    request_headers: Headers,
    *,
    immutable: bool = False,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    """Serve ``path`` or its precompressed sibling.

    ``stat_result`` is the identity file's stat; the caller has already
    confined the path. ``Vary`` is set whenever any sibling exists, so a cache
    never hands a compressed body to a client that did not ask for one. Every
    header is assembled before the conditional check, because
    ``NotModifiedResponse`` keeps only the caching headers of the 200; the
    extras ride on the 304 as well so the ui-kit CORS header survives a
    font revalidation from an opaque-origin iframe.
    """
    path = os.fspath(path)
    headers: dict[str, str] = dict(extra_headers or {})
    if immutable:
        headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL

    siblings = [(enc, path + ext, _regular_file(path + ext)) for enc, ext in _ENCODINGS]
    if any(st is not None for _, _, st in siblings):
        headers["Vary"] = "Accept-Encoding"

    accepted = parse_accept_encoding(request_headers.getlist("accept-encoding"))
    response: FileResponse | None = None
    for encoding, sibling, sibling_stat in siblings:
        if sibling_stat is None or encoding not in accepted:
            continue
        headers["Content-Encoding"] = encoding
        # The sibling carries the identity file's type: a ``.js.br`` is
        # JavaScript, never ``application/x-brotli``.
        media_type = mimetypes.guess_type(path)[0] or "text/plain"
        response = FileResponse(
            sibling, stat_result=sibling_stat, headers=headers, media_type=media_type)
        break
    if response is None:
        response = FileResponse(path, stat_result=stat_result, headers=headers)

    if _is_not_modified(response.headers, request_headers):
        not_modified = NotModifiedResponse(response.headers)
        for key, value in (extra_headers or {}).items():
            not_modified.headers[key] = value
        return not_modified
    return response


class PrecompressedStaticFiles(StaticFiles):
    """``StaticFiles`` that serves the precompressed siblings and applies the
    cache policy. ``immutable`` is ``True`` for a mount whose URLs carry a
    version (the wake-word bundle) or a predicate on the file name (the
    dashboard's content-hashed assets)."""

    def __init__(
        self, *, directory: str, immutable: bool | Callable[[str], bool] = False,
    ) -> None:
        super().__init__(directory=directory)
        self._immutable = immutable

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        if status_code != 200:
            return super().file_response(full_path, stat_result, scope, status_code)
        name = os.path.basename(os.fspath(full_path))
        immutable = self._immutable(name) if callable(self._immutable) else self._immutable
        return negotiated_file_response(
            full_path, stat_result, Headers(scope=scope), immutable=immutable)
