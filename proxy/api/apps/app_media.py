"""An app's media link, served: ``GET /v1/apps/{id}/client/media?t=<secret>``.

A page in an app's frame shows a workspace video, audio file or picture
through this address. The frame cannot send a cookie, so the route reads
none: the secret names a link the viewer's own signed-in page was given
(``catalog``'s ``files.media``), and that is all it proves. What may be
answered is decided again at EVERY request, by the functions that decided
the mint:

1. the link's lease still runs (``media_links.find``), no store read;
2. where the browser says what is asking (``Sec-Fetch-Dest``), only a
   video, an audio or an image element is answered: never a tab, a frame
   or a script's own request; and one range at most, as an element asks;
3. the bounds, before any work: requests per viewer and app, the requests
   of the whole platform that wait for their answer to start, answers in
   flight per viewer and app (a request past them waits in line for a
   moment), gates at once for the whole platform;
4. on a worker thread, the person (still there, their sign-in still good
   since the link's last lease, no password gate), the route's gates of a
   platform method (``apps.method_gate``: the app visible to them, the
   method declared and approved, the floor), a dormant app, then
   ``catalog.page_media`` (a member, the declared folder, their own file
   role, an inert kind by name and bytes, the file opened with no link
   followed), and the file is still the one the link was made for.

Any refusal is the same 404. The answer streams the descriptor step 4
opened (``FdFileResponse``: ranges, ``If-Range``, HEAD), and an answer that
runs long is judged again every few seconds and ends where it is refused.
It also ends when its reader went away: a browser that seeks drops the
range it was reading, and the answer must not read on to the end of the
file with one of the viewer's places.

The path sits under ``client/``: a login gateway already lets an app's
cookie-less files through there, and the secret is
the ``t`` parameter the log queue removes from every line.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import APIRouter, HTTPException, Request, Response

from api.media.media import FdFileResponse
from auth import token_holder
from services.apps import call_rate, catalog, media_links
from storage import database as task_store, db_apps

logger = logging.getLogger("otodock.apps")
router = APIRouter()

# Requests on one viewer's links of one app: a scrubbed timeline asks often.
REQUESTS_PER_S = 40.0
REQUEST_BURST = 240.0
# Answers one viewer may have in flight on one app. A browser asks a host
# for six files at a time, but a reverse proxy that speaks HTTP/2 to it
# passes a page's requests on all at once: the ones past the bound wait in
# line this long, this many of them, and are told to come back after that.
IN_FLIGHT_PER_VIEWER = 16
IN_FLIGHT_WAIT_S = 2.0
WAITING_PER_VIEWER = 64
# Requests of the whole platform whose answer has not started: they wait for
# a place or for the gate, and each holds its connection. The same share of
# the server's own bound as the line of platform calls.
WAITING_PLATFORM = call_rate.line_bound()
# The gate does store reads and file work on the default thread pool: this
# many at once for the whole platform, the rest wait.
GATES_AT_ONCE = 8
# An answer that still streams is judged again this often.
RECHECK_S = 5.0
# What a media element says it is. A document, a frame, an embed, a script
# and a page's own fetch each say something else, or (not a browser) nothing.
_ELEMENT_DESTS = frozenset({"video", "audio", "image"})

# On every answer the route starts, the refusals and Starlette's own 400 and
# 416 included. The type is the kind table's, never the file's; the policy
# runs nothing and frames nothing; a navigation downloads. The file is
# readable by the page that shows it (a canvas), which is what the page's
# declaration already allows for a text file; the wildcard trusts no origin,
# the secret alone is the right, and no request here carries a credential.
_ROUTE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"content-security-policy", b"default-src 'none'; sandbox; frame-ancestors 'none'"),
    (b"cache-control", b"private, no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-accel-buffering", b"no"),
    (b"access-control-allow-origin", b"*"),
    (b"cross-origin-resource-policy", b"cross-origin"),
)
_HEADERS = {k.decode(): v.decode() for k, v in _ROUTE_HEADERS}

_gates = call_rate.Gate(GATES_AT_ONCE)
_places: dict[tuple[str, str], call_rate.Gate] = {}   # (app, viewer), dropped when idle
_waiting = 0

# A person whose sign-ins ended in this process loses their links at once
# (a change made by another process is caught by the gate's own read).
token_holder.on_forget(media_links.drop_person)


def _refused(status: int = 404, **extra: str) -> HTTPException:
    return HTTPException(status_code=status, detail="Not found" if status == 404 else "Too fast",
                         headers={**_HEADERS, **extra})


async def _enter(link: media_links.Link) -> bool:
    """One of the viewer's places among the answers in flight, at once or
    after a short wait in line; False when the line is full or the wait
    ran out."""
    key = (link.app_id, link.sub)
    gate = _places.get(key)
    if gate is None:
        gate = _places[key] = call_rate.Gate(IN_FLIGHT_PER_VIEWER)
    entered = False
    try:
        if len(gate.waiters) >= WAITING_PER_VIEWER:
            return False
        try:
            async with asyncio.timeout(IN_FLIGHT_WAIT_S):
                await gate.enter()
        except TimeoutError:
            return False
        entered = True
    finally:
        # A wait that ends as a place is handed over gives the place on,
        # which can leave the gate with nobody: it goes then.
        if not entered and gate.idle and _places.get(key) is gate:
            del _places[key]
    media_links.enter(link)
    return True


def _leave(link: media_links.Link) -> None:
    media_links.leave(link)
    key = (link.app_id, link.sub)
    gate = _places.get(key)
    if gate is not None:
        gate.leave()
        if gate.idle:
            del _places[key]


def _judge(link: media_links.Link) -> media_links.MediaFile | None:
    """Step 4, on a worker thread: the opened file, or None for any
    refusal (the caller says nothing about which)."""
    from api.agents import files as files_api
    from api.apps.apps import method_gate
    from auth.providers import auth_gate, session_cookie_current, user_context_for_sub
    person = task_store.get_user(link.sub)
    if not person or not session_cookie_current(person, {"iat": link.judged_at}) or auth_gate(person):
        return None
    user = user_context_for_sub(link.sub)
    if user is None:
        return None
    try:
        row, _role, _args = method_gate(link.app_id, user, catalog.MEDIA_METHOD, {"path": link.path})
    except HTTPException:
        return None
    # An admin passes the access rule of a personal app whose owner lost
    # the agent; nobody is answered a file of a dormant app without a cookie.
    if db_apps.personal_row_dormant(row):
        return None
    try:
        media = catalog.page_media(row.get("agent") or "", row, user, link.path,
                                   files_api.safe_agent_path)
    except (ValueError, PermissionError):
        return None
    if media.rel != link.rel or media.kind != link.kind:
        os.close(media.fd)   # the name now leads to another file than the link was made for
        return None
    return media


async def _judged(link: media_links.Link) -> media_links.MediaFile | None:
    await _gates.enter()
    try:
        return await asyncio.to_thread(_judge, link)
    finally:
        _gates.leave()


class _Withdrawn(Exception):
    """The gate refused an answer that was already streaming."""


class _ReaderGone(Exception):
    """The reader of an answer went away."""


def _with_route_headers(headers: list) -> list:
    have = {bytes(name).lower() for name, _value in headers}
    return list(headers) + [h for h in _ROUTE_HEADERS if h[0] not in have]


class JudgedFileResponse(FdFileResponse):
    """``FdFileResponse`` for a media link. Every start it sends carries the
    route's headers (Starlette answers a bad range itself), and an answer
    that still streams after ``RECHECK_S`` is judged again before its next
    chunk: on a refusal nothing more is sent and the server closes the
    connection. A file that shrank under the read ends the same way. So
    does an answer whose reader went away: the server's ``send`` says
    nothing about that and the file response would read its range to the
    end, so the request's own channel is watched for the disconnect. The
    response owns the link's place among the answers in flight."""

    def __init__(self, fd: int, stat_result: os.stat_result, *, link: media_links.Link,
                 **kwargs) -> None:
        self._link = link
        super().__init__(fd, stat_result, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        checked = time.monotonic()
        link = self._link
        gone = False

        async def watch() -> None:
            nonlocal gone
            # The request has no body: after its one message comes the
            # disconnect. Anything else ends the watch, it is never a loop.
            for _ in range(2):
                if (await receive())["type"] == "http.disconnect":
                    gone = True
                    return

        async def judged_send(message) -> None:
            nonlocal checked
            if message["type"] == "http.response.start":
                message = {**message, "headers": _with_route_headers(message.get("headers") or [])}
            elif gone:
                raise _ReaderGone
            elif time.monotonic() - checked >= RECHECK_S:
                checked = time.monotonic()
                again = await _judged(link)
                if again is None:
                    raise _Withdrawn
                os.close(again.fd)
            await send(message)

        watcher = asyncio.ensure_future(watch())
        try:
            await super().__call__(scope, receive, judged_send)
        except _ReaderGone:
            pass
        except _Withdrawn:
            logger.info("media link ended in the middle of an answer: app=%s viewer=%s",
                        link.app_id, link.sub)
        except RuntimeError as exc:
            # The server's word for a body shorter than the length the start
            # declared: the file shrank while it was read.
            if "Content-Length" not in str(exc):
                raise
            logger.info("media file changed under an answer: app=%s viewer=%s", link.app_id, link.sub)
        finally:
            watcher.cancel()
            _leave(link)


@router.options("/v1/apps/{app_id}/client/media")
async def preflight_app_media(app_id: str) -> Response:
    """No media element should ask first; one that does is told yes."""
    return Response(status_code=204, headers={
        **_HEADERS, "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "range", "Access-Control-Max-Age": "600",
    })


@router.api_route("/v1/apps/{app_id}/client/media", methods=["GET", "HEAD"])
async def serve_app_media(app_id: str, request: Request, t: str = ""):
    """One file through its media link (the module's head has the rules)."""
    link = media_links.find(t, app_id) if t else None
    if link is None:
        raise _refused()
    dest = request.headers.get("sec-fetch-dest")
    if dest is not None and dest.strip().lower() not in _ELEMENT_DESTS:
        raise _refused()
    # No media element asks for more than one range. Several are answered in
    # parts, each a seek and a read, all under one of the viewer's places.
    if "," in request.headers.get("range", ""):
        raise _refused()
    if not call_rate.take(f"{app_id}|media|{link.sub}", REQUESTS_PER_S, REQUEST_BURST):
        raise _refused(429, **{"Retry-After": "1"})
    global _waiting
    if _waiting >= WAITING_PLATFORM:
        raise _refused(429, **{"Retry-After": "1"})
    _waiting += 1
    try:
        if not await _enter(link):
            raise _refused(429, **{"Retry-After": "1"})
        handed_over = False
        try:
            media = await _judged(link)
            if media is None:
                logger.info("media link refused: app=%s viewer=%s file=%s", app_id, link.sub,
                            link.rel.rsplit("/", 1)[-1])
                raise _refused()
            response = JudgedFileResponse(media.fd, media.stat, link=link, media_type=media.mime,
                                          filename=media.rel.rsplit("/", 1)[-1], headers=dict(_HEADERS))
            handed_over = True
            return response
        finally:
            if not handed_over:
                _leave(link)
    finally:
        _waiting -= 1
