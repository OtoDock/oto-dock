"""The request-path traversal guard (core-seams phase 3).

Every allowlist that judges an HTTP path — the master key's endpoints, the
external principal's, the render principal's, the app proxy's forwarded
path, the app egress path and the satellite tunnel's frame path — asks
this ONE question before its anchored regexes run: does the path carry a
dot segment, a percent-encoded dot / slash / backslash, a raw backslash or
a NUL? Any of those is refused up front, so the path a regex matched is
byte-identical to the path that is forwarded: httpx collapses ``../``
(RFC 3986 remove_dot_segments) when it builds the upstream request, so
``/v1/tasks/../admin/users`` would match ``^/v1/tasks(/.*)?$`` and be sent
to ``/admin/users``.

The path is judged as the caller sees it: Starlette hands the allowlists
``request.url.path`` (percent-decoded once, so a single encoding arrives as
a real dot segment and a double encoding as ``%2e`` text), and the app
proxy also judges the raw path it forwards. Stdlib only, no imports: the
satellite keeps a byte twin of the function
(``satellite/transport/http_tunnel.py``) pinned by the release gate's twin
rule — same body, same local names, same annotation.
"""

from __future__ import annotations


def has_traversal(path: str) -> bool:
    """True if ``path`` carries a dot segment (``.`` or ``..``), a
    percent-encoded dot, slash or backslash, a raw backslash, or a NUL
    (raw or ``%00``)."""
    low = path.lower()
    if "%2e" in low or "%2f" in low or "%5c" in low or "%00" in low or "\\" in path or "\x00" in path:
        return True
    return any(seg in (".", "..") for seg in path.split("/"))
