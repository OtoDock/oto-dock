"""Refused batches, kept so the agent resends only the fix.

When an all-or-nothing write is refused (an operation not valid, or one that
failed as it ran), the parent keeps the batch as it was numbered in the
refusal, for one chat, tool and file, and the refusal names its reference.
The next call sends ``fix: {"batch": ref, "replace": {n: op}, "remove": [n],
"add_after": {n: [ops]}}`` instead of ``operations``: the kept batch runs
again with those changes, all or nothing, with the checks once. A corrected
batch refused again is kept in its corrected form, for at most ``ROUNDS``
correction rounds. A reference is gone after ``KEEP_S``, when a newer batch
for the same file is kept, when the file is saved, or when the store is
full; it never serves another chat, tool or file, nor a file that changed
since the refusal.

The store lives in the server process (the parent of every write) and holds
at most ``MAX_BATCHES`` batches of at most ``MAX_BYTES`` each.
"""

from __future__ import annotations

import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

FIX_KEY = "fix"
# The line a write tool's whole help ends with.
HELP_LINE = ("REFUSED BATCHES: a refused batch (nothing saved) is kept for a few minutes; send fix: {\"batch\": "
             "\"<the reference the refusal names>\", \"replace\": {\"<n>\": {op}}, \"remove\": [n], \"add_after\": "
             "{\"<n>\": [ops]}} instead of operations to run it again with only the corrections (three rounds).")
# The write tools' ``fix`` argument, as their schemas declare it.
FIX_PROPERTY = {
    "type": "object",
    "description": "Instead of operations, after a refusal that kept the batch: {\"batch\": \"<its reference>\", "
                   "\"replace\": {\"<n>\": {op}}, \"remove\": [n], \"add_after\": {\"<n>\": [ops]}} with n the "
                   "operation's number in the refusal (add_after -1: at the start). The kept batch runs again "
                   "with only these changes, all or nothing.",
}
KEEP_S = 600.0
MAX_BATCHES = 32
MAX_BYTES = 2 * 1024 * 1024
ROUNDS = 3
_SEND_WHOLE = "send the whole batch again"


@dataclass
class Kept:
    session: str
    tool: str
    path: str
    stat: tuple[int, ...] | None
    items: list[Any]
    create_new: bool
    rounds_left: int
    at: float


STORE: OrderedDict[str, Kept] = OrderedDict()


class FixRefused(Exception):
    """A fix that cannot run: the message is the refusal's reason, and
    ``kept`` says whether the batch is still there for another try."""

    def __init__(self, message: str, *, kept: bool) -> None:
        super().__init__(message)
        self.kept = kept


def _expire(now: float) -> None:
    for ref in [ref for ref, kept in STORE.items() if now - kept.at >= KEEP_S]:
        del STORE[ref]


def keep(*, session: str, tool: str, path: str, stat: tuple[int, ...] | None, items: list[Any],
         create_new: bool, rounds_left: int, ref: str | None = None) -> str | None:
    """Keep ``items`` (the batch as its refusal numbered it); its reference,
    or None when it is too large or no correction round is left. A newer
    batch for the same chat, tool and file replaces the older one."""
    if rounds_left <= 0:
        STORE.pop(ref or "", None)
        return None
    try:
        size = len(json.dumps(items, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return None
    if size > MAX_BYTES:
        return None
    now = time.monotonic()
    _expire(now)
    for old in [r for r, k in STORE.items() if (k.session, k.tool, k.path) == (session, tool, path) and r != ref]:
        del STORE[old]
    ref = ref or f"batch-{secrets.token_hex(3)}"
    STORE[ref] = Kept(session, tool, path, stat, items, create_new, rounds_left, now)
    STORE.move_to_end(ref)
    while len(STORE) > MAX_BATCHES:
        STORE.popitem(last=False)
    return ref


def drop_path(path: str) -> None:
    """A save of ``path``: its kept batches no longer apply."""
    for ref in [ref for ref, kept in STORE.items() if kept.path == path]:
        del STORE[ref]


def clear() -> None:
    STORE.clear()


def take(fix: Any, *, session: str, tool: str, path: str, stat: tuple[int, ...] | None) -> tuple[str, Kept, list]:
    """The kept batch ``fix`` names and the batch with its corrections, or
    FixRefused."""
    if not isinstance(fix, dict) or not isinstance(fix.get("batch"), str):
        raise FixRefused('fix needs "batch": the reference a refusal named, with replace, remove or add_after',
                         kept=False)
    ref = fix["batch"].strip()
    _expire(time.monotonic())
    kept = STORE.get(ref)
    if kept is None or (kept.session, kept.tool, kept.path) != (session, tool, path):
        raise FixRefused(f"no batch is kept as \"{ref}\" for this file (a kept batch lasts {KEEP_S / 60:.0f} "
                         f"minutes, and a newer batch or a save of the file replaces it): {_SEND_WHOLE}",
                         kept=False)
    if kept.stat != stat:
        del STORE[ref]
        raise FixRefused(f"the file changed since that batch was refused: read it again and {_SEND_WHOLE}",
                         kept=False)
    return ref, kept, corrected(kept.items, fix)


def _index(value: Any, size: int, *, start: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise FixRefused(f"'{value}' is not an operation number", kept=True) from None
    if not start <= n < size:
        raise FixRefused(f"operation {n} does not exist: the kept batch has {size} operations, numbered 0 to "
                         f"{size - 1} as its refusal numbered them", kept=True)
    return n


def corrected(items: list[Any], fix: dict) -> list[Any]:
    """``items`` with ``fix``'s corrections: ``replace`` {n: operation},
    ``remove`` [n], ``add_after`` {n: [operations]} (-1 adds at the start).
    Every number is the operation's number in the refusal."""
    unknown = sorted(set(fix) - {"batch", "replace", "remove", "add_after"})
    if unknown:
        raise FixRefused(f"fix takes batch, replace, remove and add_after, not {', '.join(unknown)}", kept=True)
    size = len(items)
    replace = fix.get("replace") or {}
    remove = fix.get("remove") or []
    add_after = fix.get("add_after") or {}
    if not isinstance(replace, dict) or not isinstance(remove, list) or not isinstance(add_after, dict):
        raise FixRefused("replace is {number: operation}, remove [numbers], add_after {number: [operations]}",
                         kept=True)
    if not (replace or remove or add_after):
        raise FixRefused("fix holds no correction: give replace, remove or add_after", kept=True)
    replaced = {_index(n, size): op for n, op in replace.items()}
    removed = {_index(n, size) for n in remove}
    both = sorted(set(replaced) & removed)
    if both:
        raise FixRefused(f"operation {both[0]} is both replaced and removed", kept=True)
    added: dict[int, list[Any]] = {}
    for n, ops in add_after.items():
        index = -1 if str(n).strip() == "-1" else _index(n, size)
        added[index] = ops if isinstance(ops, list) else [ops]
    out = list(added.get(-1, []))
    for n, item in enumerate(items):
        if n in replaced:
            out.append(replaced[n])
        elif n not in removed:
            out.append(item)
        out.extend(added.get(n, []))
    return out


def note(ref: str | None, rounds_left: int, *, corrected_batch: bool) -> str:
    """What a refusal adds about its kept batch."""
    if ref is None:
        if corrected_batch:
            return f"No correction rounds are left: {_SEND_WHOLE}."
        return ""
    rounds = f"{rounds_left} correction round{'s' if rounds_left != 1 else ''} left"
    return (f"The batch is kept as \"{ref}\" for {KEEP_S / 60:.0f} minutes ({rounds}): to resend only the fix, "
            f"call the tool again with this path and fix: {{\"batch\": \"{ref}\", \"replace\": {{\"<n>\": "
            "{...}}, \"remove\": [n], \"add_after\": {\"<n>\": [...]}} instead of operations, each n an "
            "operation's number above.")
