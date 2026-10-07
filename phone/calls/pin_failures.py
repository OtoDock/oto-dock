"""Anti-brute-force state for the inbound PIN gate.

Two sliding 15-minute windows over wrong-PIN attempts, in memory and rebuilt
after a restart from the proxy's call log (``seed``: each inbound call's
outcome and attempt count, never a digit):

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
from datetime import datetime

from config_manager import normalize_did

WINDOW_S = 15 * 60.0
NUMBER_LIMIT = 5
ROUTE_LIMIT = 20
_CAP = 10_000
#: Outcomes that end the call at the gate: every attempt failed (a timeout
#: counts the attempt it cut), stamped when the call ended.
_REFUSED = frozenset({"pin_failed", "pin_timeout"})


def _wall(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


def _replayed(call: dict) -> tuple[float, float, str, str, int, bool] | None:
    """``(start, at, number, route, failures, verified)`` of one logged call
    on the wall clock, as the gate recorded it: a refused call failed every
    attempt it reached and ended at the gate; any other call failed all but
    its last attempt near its start (a hangup mid-entry, an error), and a
    completed one passed the gate."""
    try:
        attempts = int(call.get("pin_attempts") or 0)
    except (TypeError, ValueError):
        return None
    start = _wall(call.get("started_at"))
    if attempts <= 0 or start is None:
        return None
    outcome = call.get("outcome") or ""
    if outcome in _REFUSED:
        at = _wall(call.get("ended_at")) or start
        failures = attempts
    else:
        at, failures = start, attempts - 1
    return (start, at, call.get("from_number") or "", call.get("route_id") or "",
            failures, outcome == "completed")


class PinFailureStore:
    """Sliding-window failure counters for the PIN gate (asyncio
    single-threaded — no lock needed; only the gate touches this)."""

    def __init__(self, *, now=time.monotonic, wall=time.time):
        self._now = now
        self._wall = wall
        self._numbers: dict[str, list[float]] = {}
        self._routes: dict[str, list[float]] = {}
        #: This process's start: a call that started later is in memory.
        self.started_wall = wall()
        self.seeded = False
        # Callers this process cleared before the seed landed: their
        # earlier failures are not replayed over the clear.
        self._cleared: set[str] = set()

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
        key = normalize_did(number)
        self._numbers.pop(key, None)
        if not self.seeded and key:
            self._cleared.add(key)

    def seed(self, calls: list[dict]) -> int:
        """Replay the proxy's recent PIN-gate calls (``GET
        /v1/phone/pin-failures``) into the windows, once per process: only
        the calls that started before this process did, oldest first, each
        failure moved onto this clock, a verified call clearing its caller
        as the gate did. Built apart and merged, so a clear never touches
        what this process recorded itself. Returns the failures replayed."""
        offset = self._now() - self._wall()
        cutoff = self._now() - WINDOW_S
        numbers: dict[str, list[float]] = {}
        routes: dict[str, list[float]] = {}
        events = [e for e in map(_replayed, calls) if e and e[0] < self.started_wall]
        replayed = 0
        for _start, at, number, route_id, failures, verified in sorted(events, key=lambda e: e[1]):
            t = at + offset
            key = normalize_did(number)
            if failures and t > cutoff:
                if key:
                    numbers.setdefault(key, []).extend([t] * failures)
                if route_id:
                    routes.setdefault(route_id, []).extend([t] * failures)
                replayed += failures
            if verified and key:
                numbers.pop(key, None)
        for key in self._cleared:
            numbers.pop(key, None)
        self._cleared.clear()
        for table, seeded in ((self._numbers, numbers), (self._routes, routes)):
            for key, stamps in seeded.items():
                table[key] = sorted(stamps + table.get(key, []))
        self._evict_if_needed()
        self.seeded = True
        return replayed

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
