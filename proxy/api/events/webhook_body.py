"""The body read of every route under ``/v1/webhooks/`` (the vendor and relay
receivers, the API-key trigger fires): its cap, its deadlines and the bound
on what is read at once.

Caps (each a config key; 0 is the request backstop):

* ``MAX_WEBHOOK_BODY_BYTES`` (2 MB): a sender nothing has checked yet.
* ``MAX_WEBHOOK_SIGNED_BODY_BYTES`` (25 MB): a live subscription of a
  provider that delivers large events (``webhook_providers.LARGE_BODY_PROVIDERS``)
  whose manifest declares a signature; the signature is verified before
  anything else touches the body.
* ``MAX_WEBHOOK_KEYED_BODY_BYTES`` (25 MB): a trigger fire whose API key
  verified.

A larger cap is never below the first one. The HTTP middleware keeps the
first cap as its outer bound and lifts it only for a request whose route
set ``SCOPE_KEY`` (``lift``) after its check, so a large body is read only
once the route has decided it may be.

What is read at once is bounded twice: by count (``_READS_PER_CLIENT`` per
distinct client, ``_READS_TOTAL`` in all) and by bytes in flight
(``MAX_WEBHOOK_INFLIGHT_BYTES`` in all, half of it for reads at the first
cap, ``MAX_WEBHOOK_INFLIGHT_BYTES_PER_CLIENT`` per distinct client). A read
reserves its declared length, or its cap when none is declared, and never
less than ``_MIN_RESERVE``; the reservation is given back on every exit.
A sender gets 10 s between chunks and a whole-body deadline of 30 s, or
longer for a large read (``_MIN_RATE``), at most ``_BODY_MAX_S``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Request

import config
from auth.lan_check import client_address

logger = logging.getLogger("claude-proxy.api.webhooks")

#: The scope key a route sets to lift the middleware's cap (``lift``).
SCOPE_KEY = "otodock.webhook_body_cap"

_CHUNK_GAP_S = 10.0
_BODY_S = 30.0
_BODY_MAX_S = 120.0
_MIN_RATE = 512 * 1024
_MIN_RESERVE = 64 * 1024
_READS_PER_CLIENT = 4
_READS_TOTAL = 256
# A body this large is parsed in a worker thread.
_PARSE_OFF_LOOP_BYTES = 1024 * 1024


def _or_backstop(value: int) -> int:
    return value if value > 0 else config.MAX_REQUEST_BODY_BYTES


def unknown_cap() -> int:
    return _or_backstop(config.MAX_WEBHOOK_BODY_BYTES)


def signed_cap() -> int:
    return max(unknown_cap(), _or_backstop(config.MAX_WEBHOOK_SIGNED_BODY_BYTES))


def keyed_cap() -> int:
    return max(unknown_cap(), _or_backstop(config.MAX_WEBHOOK_KEYED_BODY_BYTES))


def ceiling() -> int:
    """The most any webhook route may lift its cap to (the middleware's
    bound for a declared length before the route has run)."""
    return max(signed_cap(), keyed_cap())


def lift(request: Request, cap: int) -> None:
    """Let the middleware pass a body up to ``cap`` for this request."""
    request.scope[SCOPE_KEY] = cap


class TooLarge(Exception):
    pass


class Busy(Exception):
    pass


@dataclass
class _Client:
    reads: int = 0
    bytes: int = 0


class _InFlight:
    """The reads and bytes in flight; single-threaded (the event loop)."""

    def __init__(self) -> None:
        self.reads = 0
        self.bytes = 0
        self.small_bytes = 0
        self.clients: dict[str, _Client] = {}

    def reserve(self, key: str, shared: bool, amount: int, small: bool) -> None:
        total = config.MAX_WEBHOOK_INFLIGHT_BYTES
        per_client = config.MAX_WEBHOOK_INFLIGHT_BYTES_PER_CLIENT
        c = self.clients.get(key)
        if (self.reads >= _READS_TOTAL
                or (total > 0 and self.bytes + amount > total)
                or (small and total > 0 and self.small_bytes + amount > total // 2)
                or (not shared and c is not None and (
                    c.reads >= _READS_PER_CLIENT
                    or (per_client > 0 and c.bytes + amount > per_client)))):
            raise Busy
        self.reads += 1
        self.bytes += amount
        if small:
            self.small_bytes += amount
        if not shared:
            c = self.clients.setdefault(key, _Client())
            c.reads += 1
            c.bytes += amount

    def release(self, key: str, shared: bool, amount: int, small: bool) -> None:
        self.reads -= 1
        self.bytes -= amount
        if small:
            self.small_bytes -= amount
        if not shared:
            c = self.clients.get(key)
            if c is not None:
                c.reads -= 1
                c.bytes -= amount
                if c.reads <= 0:
                    self.clients.pop(key, None)


_in_flight = _InFlight()


def in_flight() -> dict:
    """The current reservations (tests, diagnostics)."""
    return {"reads": _in_flight.reads, "bytes": _in_flight.bytes,
            "small_bytes": _in_flight.small_bytes, "clients": len(_in_flight.clients)}


def _declared(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


async def read(request: Request, *, cap: int, source: str) -> bytes:
    """The request body, at most ``cap`` bytes, under the deadlines and the
    in-flight bounds. Raises ``TooLarge`` (413), ``Busy`` (503) or
    ``TimeoutError`` (408); a disconnect propagates."""
    declared = _declared(request)
    if declared is not None and declared > cap:
        logger.warning("webhook body refused from %s: %d bytes declared, cap %d",
                       source, declared, cap)
        raise TooLarge
    # One resolution: the reservation and its release use the same key and
    # the same shared flag.
    addr = client_address(request)
    key = f"ip:{addr.bucket_key}"
    amount = max(_MIN_RESERVE, min(declared if declared is not None else cap, cap))
    small = cap <= unknown_cap()
    _in_flight.reserve(key, addr.shared, amount, small)
    try:
        loop = asyncio.get_running_loop()
        whole = min(_BODY_MAX_S, max(_BODY_S, amount / _MIN_RATE))
        deadline = loop.time() + whole
        chunks: list[bytes] = []
        total = 0
        stream = request.stream().__aiter__()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                chunk = await asyncio.wait_for(stream.__anext__(),
                                               timeout=min(_CHUNK_GAP_S, remaining))
            except StopAsyncIteration:
                break
            total += len(chunk)
            if total > cap:
                logger.warning("webhook body refused from %s: over %d bytes", source, cap)
                raise TooLarge
            chunks.append(chunk)
    finally:
        _in_flight.release(key, addr.shared, amount, small)
    if total > unknown_cap():
        logger.info("webhook body of %d bytes from %s", total, source)
    else:
        logger.debug("webhook body of %d bytes from %s", total, source)
    return b"".join(chunks)


def _parse(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8", errors="replace"))


async def parse_json(raw: bytes) -> Any:
    """``raw`` as JSON (a large body in a worker thread); ValueError on a
    malformed one."""
    if len(raw) > _PARSE_OFF_LOOP_BYTES:
        return await asyncio.to_thread(_parse, raw)
    return _parse(raw)
