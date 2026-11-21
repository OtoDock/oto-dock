"""The conversion of a document for the platform's document pane: the
person's "Download as PDF" and "Download as CSV" (the proxy's
``api/media/document_downloads.py``).

The proxy reads the file behind its own proof, streams its bytes here with
a one-shot token as the bearer, and serves the answer as a download. The
token is verified with one call back to the proxy before the body is read
(``render._verify``, the render token's shape), so a sibling container gains
nothing from the route. The bytes land in a folder of their own, the
conversion is ``render_engine.convert`` (the install's Collabora first,
LibreOffice as fallback, with every guard of the engine), and both folders
are removed once the answer is sent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

import render
import render_engine

logger = logging.getLogger("file-tools.convert")

VERIFY_PATH = "/v1/internal/convert/verify"
# The source cap of a document render (pdf._MAX_SOURCE_BYTES) and the
# largest answer the platform keeps for a download.
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_ANSWER_BYTES = 128 * 1024 * 1024
# The proxy's whole request stays under a gateway's 100 s.
DEADLINE_S = 70.0

WORKBOOKS = frozenset({"xlsx", "xlsm", "xltx", "xltm", "xls", "ods"})
INPUTS = WORKBOOKS | frozenset({
    "docx", "docm", "dotx", "dotm", "doc", "odt", "rtf",
    "pptx", "pptm", "potx", "potm", "ppsx", "ppsm", "ppt", "odp",
})


def _refuse(status: int, reason: str) -> JSONResponse:
    return JSONResponse({"reason": reason}, status_code=status)


def _remove(*folders: str) -> None:
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)


class _Answer(FileResponse):
    """The converted file, its folders removed once it is sent, on a client
    that went away too (a background task would be skipped then)."""

    def __init__(self, path: str, folders: tuple[str, ...]) -> None:
        super().__init__(path, media_type="application/octet-stream")
        self._folders = folders

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.to_thread(_remove, *self._folders)


async def _write_body(request: Request, path: str) -> int:
    """The request's body into ``path``: the bytes written, or -1 past
    ``MAX_INPUT_BYTES``."""
    written = 0
    out = await asyncio.to_thread(open, path, "xb")
    try:
        async for chunk in request.stream():
            written += len(chunk)
            if written > MAX_INPUT_BYTES:
                return -1
            await asyncio.to_thread(out.write, chunk)
    finally:
        await asyncio.to_thread(out.close)
    return written


async def handle_convert(request: Request):
    """``POST /convert?ext=<input extension>&format=pdf|csv`` with the
    document's bytes as the body: the converted file, or 401 / 400 / 413 /
    422 / 504 with a reason."""
    if not await render._verify(render._bearer(request), VERIFY_PATH):
        return _refuse(401, "the conversion token was refused")
    ext = (request.query_params.get("ext") or "").lower()
    fmt = (request.query_params.get("format") or "").lower()
    if ext not in INPUTS:
        return _refuse(400, "this file type is not converted here")
    if fmt != "pdf" and not (fmt == "csv" and ext in WORKBOOKS):
        return _refuse(400, "this target is not offered for this file type")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        return _refuse(400, "the content length is not a number")
    if declared > MAX_INPUT_BYTES:
        return _refuse(413, "the file is too large to convert here")
    work = await asyncio.to_thread(tempfile.mkdtemp, prefix="file-tools-convert-")
    out_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="file-tools-converted-")
    answered = False
    try:
        source = os.path.join(work, f"document.{ext}")
        written = await _write_body(request, source)
        if written < 0:
            return _refuse(413, "the file is too large to convert here")
        if not written:
            return _refuse(400, "the request carried no file")
        started = time.monotonic()
        try:
            produced = await render_engine.convert(source, fmt, out_dir=out_dir,
                                                   deadline=started + DEADLINE_S)
        except RuntimeError as exc:
            text = str(exc)
            if "timed out" in text or "ran out of time" in text:
                return _refuse(504, text)
            logger.info("conversion of a .%s to %s refused: %s", ext, fmt, text)
            return _refuse(422, text)
        size = await asyncio.to_thread(os.path.getsize, produced)
        if size > MAX_ANSWER_BYTES:
            return _refuse(413, "the converted file is too large to download")
        logger.info("converted a .%s to %s (%d bytes) in %d ms", ext, fmt, size,
                    int((time.monotonic() - started) * 1000))
        answered = True
        return _Answer(produced, (work, out_dir))
    finally:
        if not answered:
            await asyncio.to_thread(_remove, work, out_dir)
