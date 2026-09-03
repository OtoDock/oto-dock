"""Anti-brute-force state for the inbound PIN gate.

Two sliding 15-minute windows over wrong-PIN attempts, both in-memory (a
daemon restart clears them — accepted v1 trade-off, documented):

- Per caller number: ≥5 failures → that number is in cooldown until 15
  minutes after its last failure. Deliberately NOT a permanent ban — caller
  ID is spoofable, a permanent ban would let an attacker lock out the real
  caller.
- Per route (circuit breaker): ≥20 aggregate failures → the whole route
  answers with the lockout phrase while the window stays hot. Rotating
  spoofed caller IDs defeats the per-number window (3 free guesses per
  call), and AudioSocket calls without registry enrichment carry no caller
  number at all — the route breaker is the backstop that turns a sustained
  brute-force into a self-limiting temporary lockout.

Numbers are keyed on `normalize_did` (Twilio sends `+1555…`, dialplans post
whatever the admin curl'd) so one human caller occupies one bucket. Locked
entries are never evicted by the size cap (eviction would be a cooldown
bypass); expired entries go first. Only attempt counts live here — never a
digit, never a PIN.
"""

from __future__ import annotations

import time

from config_manager import normalize_did

WINDOW_S = 15 * 60.0
NUMBER_LIMIT = 5
ROUTE_LIMIT = 20
_CAP = 10_000


class PinFailureStore:
    """Sliding-window failure counters for the PIN gate (asyncio
    single-threaded — no lock needed; only the gate touches this)."""

    def __init__(self, *, now=time.monotonic):
        self._now = now
        self._numbers: dict[str, list[float]] = {}
        self._routes: dict[str, list[float]] = {}

    # -- queries --------------------------------------------------------------

    def number_locked(self, number: str) -> bool:
        key = normalize_did(number)
        if not key:
            return False
        return len(self._prune(self._numbers, key)) >= NUMBER_LIMIT

    def route_locked(self, route_id: str) -> bool:
        if not route_id:
            return False
        return len(self._prune(self._routes, route_id)) >= ROUTE_LIMIT

    # -- updates --------------------------------------------------------------

    def record_failure(self, number: str, route_id: str) -> None:
        now = self._now()
        key = normalize_did(number)
        if key:
            self._evict_if_needed()
            self._numbers[key] = self._prune(self._numbers, key) + [now]
        if route_id:
            self._routes[route_id] = self._prune(self._routes, route_id) + [now]

    def clear_number(self, number: str) -> None:
        """A correct PIN clears the caller's slate (route window stays —
        one success mustn't reset an in-progress route-wide attack)."""
        self._numbers.pop(normalize_did(number), None)

    # -- internals ------------------------------------------------------------

    def _prune(self, table: dict[str, list[float]], key: str) -> list[float]:
        """Drop expired stamps; never leaves an empty entry behind (reads
        must not grow the table)."""
        cutoff = self._now() - WINDOW_S
        entry = [t for t in table.get(key, ()) if t > cutoff]
        if entry:
            table[key] = entry
        else:
            table.pop(key, None)
        return entry

    def _evict_if_needed(self) -> None:
        if len(self._numbers) < _CAP:
            return
        cutoff = self._now() - WINDOW_S
        # Expired entries first; then the least-recently-active UNLOCKED
        # entries. Locked entries always survive.
        for key in [k for k, v in self._numbers.items()
                    if not v or v[-1] <= cutoff]:
            del self._numbers[key]
        while len(self._numbers) >= _CAP:
            candidates = [
                (v[-1], k) for k, v in self._numbers.items()
                if len(v) < NUMBER_LIMIT
            ]
            if not candidates:
                return
            del self._numbers[min(candidates)[1]]


store = PinFailureStore()
