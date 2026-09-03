"""Per-call metadata cache, keyed by AudioSocket UUID.

Caller info (phone, DID, source-specific dial_event) lives here
between the moment a phone driver knows the call is about to start and the
moment the AudioSocket TCP connection actually lands on the phone server.

The cache is populated by whichever path applies to the driver:

  - **Asterisk / FreePBX / FusionPBX**: dialplan does a synchronous
    ``System(curl ...)`` to the phone server's ``POST /v1/calls/register``
    endpoint BEFORE invoking ``AudioSocket()``. Works on any Asterisk with
    zero AMI permission gymnastics.
  - **3CX / Twilio / browser SDK / future drivers**: the same HTTP endpoint
    serves them. Whatever speaks the call-setup protocol pushes a
    normalised payload here.
  - **Outbound calls** (initiated by phone-mcp via the phone server's own
    ``/api/calls`` endpoint): the phone server registers the call entry
    directly from ``OutboundCall.phone_number`` — no HTTP loopback needed.

The contract: by the time ``AudioSocketConnection.read_uuid()`` returns,
``pop_for_uuid(uuid)`` should hand back the metadata that the proxy warmup
will turn into ``${trigger.*}`` tokens. Misses degrade gracefully — the
call proceeds without enrichment.
"""

from __future__ import annotations

import logging
import threading
import time


logger = logging.getLogger("call_registry")

# How long a registration stays in the cache if nothing claims it. Tight
# enough that a half-second-late call setup doesn't bind to last hour's
# entry; loose enough to cover legitimate slow Asterisk dispatches.
_TTL_SECONDS = 120.0

# Hard cap on cached registrations — bounds memory if a driver (or an
# attacker) floods /v1/calls/register with entries that no call ever claims.
_MAX_ENTRIES = 10000


class CallRegistry:
    """Thread-safe in-memory cache of call metadata.

    Single global instance lives in this module (``registry``). Both HTTP
    handlers (aiohttp threads) and the asyncio TCP server hit it, so we
    serialise mutations with a plain ``threading.Lock``.
    """

    def __init__(self, ttl_seconds: float = _TTL_SECONDS):
        self.ttl = ttl_seconds
        self._by_uuid: dict[str, dict] = {}
        self._lock = threading.Lock()

    def register(
        self,
        audiosocket_uuid: str,
        *,
        phone: str = "",
        did: str = "",
        source: str = "phone",
        dial_event: dict | None = None,
    ) -> None:
        """Store metadata for an upcoming call. Idempotent (last write wins)."""
        if not audiosocket_uuid:
            return
        key = audiosocket_uuid.lower()
        entry = {
            "phone": phone or "",
            "did": did or "",
            "source": source or "phone",
            "dial_event": dial_event or {},
            "ts": time.monotonic(),
        }
        with self._lock:
            if len(self._by_uuid) >= _MAX_ENTRIES and key not in self._by_uuid:
                self._gc_locked()
                if len(self._by_uuid) >= _MAX_ENTRIES:
                    oldest = min(self._by_uuid, key=lambda k: self._by_uuid[k].get("ts", 0))
                    self._by_uuid.pop(oldest, None)
            self._by_uuid[key] = entry
        logger.info(
            f"Registered call uuid={key} phone={phone!r} did={did!r} source={source!r}"
        )

    def pop_for_uuid(self, audiosocket_uuid: str) -> dict:
        """Return + remove the metadata for ``audiosocket_uuid``.

        Empty dict means no registration arrived → the call proceeds
        without enrichment (proxy still resolves the route + bound trigger
        but ``${trigger.phone}`` resolves empty).
        """
        if not audiosocket_uuid:
            return {}
        self._gc()
        key = audiosocket_uuid.lower()
        with self._lock:
            return self._by_uuid.pop(key, {})

    def _gc(self) -> None:
        with self._lock:
            self._gc_locked()

    def _gc_locked(self) -> None:
        """Drop expired entries. Caller must already hold ``self._lock``."""
        cutoff = time.monotonic() - self.ttl
        stale = [k for k, v in self._by_uuid.items() if v.get("ts", 0) < cutoff]
        for k in stale:
            self._by_uuid.pop(k, None)


# Module-level singleton — referenced by http_api (register path) and
# main (consume path).
registry = CallRegistry()
