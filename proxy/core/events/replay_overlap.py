"""A replay never writes a row the chat already holds.

After a platform restart a machine replays the turn it kept
(``services/scheduler/run_recovery.py``). The machine keeps a bounded number
of the turn's events, so a long turn's replay is truncated: it starts in the
middle of the turn. The rows the turn saved before the cut (its checkpoints)
then stay, since the replay cannot write the turn's start again, and the
replay's own rows follow them.

The two overlap whenever the kept events reach back before the last
checkpoint. Written as they come, the overlap landed twice, in the chat and
in a run's report, under a mark that said output was lost when none was. The
kept copies are also the richer ones: a row saved before the cut carries its
tool's result, which reached the platform through a hook the replay does not
repeat.

``ReplayOverlap`` holds the rows saved before the cut and tells the replay's
pump which of a save's blocks to write (``ChatStreamPump._save_turn_blocks``).
Two facts carry it. The saved rows are a prefix of the turn (a checkpoint
saves leading blocks only) and the replay is a suffix of it, so the overlap
is the end of the one and the head of the other, in the same order. And a
tool's id names it in both. The rules, for a save's blocks in order:

1. A tool block whose tool id a saved row holds is never written.
2. A block before such a tool in the same save was saved too: everything
   ahead of a saved block was. It is dropped with no comparison, so a text
   the first run split around a block the replay does not produce (an
   artifact a hook delivered, a steer) is not written again as one piece.
3. After the last such tool, the saved rows are walked in step: a block
   equal to the next saved row is dropped. A saved card or note the replay
   does not produce again is stepped over. A text the saved rows hold in
   pieces is dropped, and one that goes on past the last saved piece is
   written from there.
4. A replay with no saved tool in its head (the saved rows end in text, or
   the replay starts past them) is matched in order against the END of the
   saved rows: its head is dropped only when it runs in step up to the last
   saved row. Never by membership: a new line that repeats an earlier one
   is kept.
5. The first tool block the chat does not hold ends the overlap: from there
   only rule 1 drops.
6. The checklist block takes the saved checklist row as its own (the pump
   rewrites that row in place) and is not written again.

The mark ("earlier output was truncated") is decided once, at the first
save that holds the whole replayed head: one with a tool block in it, or one
that ends a turn. It is written, ahead of that save's rows, only when
nothing up to there matched a saved row. Then rows may be missing between
the last saved row and the replay's first. An early save (a steer's, a
checkpoint's) with no tool decides nothing, since it can land before the
head is whole: a steer's event goes on the pump's own queue the moment the
engine took the message (``ChatStreamPump.record_steer``), between the
replay's events while the burst is still being read. Its unmatched blocks
are written, and the next save is matched against the saved rows' end again.

What the filter cannot tell, by the rows alone:

1. A head text that goes on from fewer than ``_MIN_PIECE`` saved characters
   is taken as new and written whole.
2. A replay that starts inside a saved text, with no block after that
   fragment that walks on in the saved rows, keeps the fragment. The end of
   one line proves nothing.
3. A line the turn said again after a real gap, word for word the last line
   saved, at the very start of the replay: it is taken for the saved one and
   dropped, and no mark says rows are missing.
4. A new text that starts with the whole last saved text: its start is taken
   for the saved one, the rest is written, and no mark.
5. More than ``_LOOKAHEAD`` saved cards in a row with no tool between them
   stop the walk: the text after them is written again.

1, 2 and 5 write a line twice. 3 and 4 lose one, and both read exactly like
an overlap of one row.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from core.events import tool_roles
from ws import wire_events as wire

Row = tuple[str, str, str, str]

#: The fields that name one event of a turn, in the order they are tried.
_ID_FIELDS = ("tool_id", "tool_use_id", "id", "request_id", "task_id", "command_id", "file_id")
#: How many saved rows of a kind the replay may not produce again are
#: stepped over to reach the expected one.
_LOOKAHEAD = 4
#: How far back from the saved rows' end the replay's head is looked for.
_HEAD_ROWS = 64
#: The fewest saved words (characters) that let an unanchored head text count
#: as "goes on from the last saved text": fewer can be a new line that only
#: starts alike.
_MIN_PIECE = 24

_TOOL = "tool"
_TODO = "todo"
_TEXT = "text"
_EVENT = "event"


def key_of(row: Row) -> tuple:
    """What makes a saved row and a replayed block the same thing: a tool by
    its tool id, the checklist by its kind, a text by its words with the
    outer blank lines off, any other event by its type and the id it
    carries, else by its whole payload."""
    role, content, event_type, event_data = row
    if role == "assistant":
        return (_TEXT, (content or "").strip())
    try:
        payload = json.loads(event_data or "{}")
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return (_EVENT, event_type, event_data or "")
    if event_type == wire.PERSISTED_TOOL:
        tool_id = payload.get("tool_id")
        if tool_id:
            return (_TOOL, str(tool_id))
        if payload.get("name") == tool_roles.TODO_SNAPSHOT:
            return (_TODO,)
    for field in _ID_FIELDS:
        value = payload.get(field)
        if value:
            return (_EVENT, event_type, field, str(value))
    return (_EVENT, event_type, json.dumps(payload, sort_keys=True, default=str))


class ReplayOverlap:
    """The rows a cut turn saved before the cut, and the walk of its
    truncated replay against them. ``early``: the rows in id order, each
    ``{"id", "role", "content", "event_type", "event_data"}``. ``marker``:
    the text of the truncation mark, written when the replay turns out to
    start past the last saved row."""

    def __init__(self, early: list[dict], marker: str = "") -> None:
        self._keys = [key_of((r.get("role") or "", r.get("content") or "",
                              r.get("event_type") or "", r.get("event_data") or ""))
                      for r in early]
        # A saved text's own words, as written (pieces are joined as written).
        self._raw = [(r.get("content") or "") if k[0] == _TEXT else ""
                     for r, k in zip(early, self._keys, strict=True)]
        self._tool_at = {k: i for i, k in enumerate(self._keys) if k[0] == _TOOL}
        self._todo_row = next((int(r["id"]) for r, k in zip(reversed(early), reversed(self._keys),
                                                             strict=True) if k == (_TODO,)), 0)
        self._marker = marker
        # The next saved row a block is expected to equal, once the walk has
        # its place (a saved tool, or the head matched to the rows' end);
        # None before that.
        self._pos: int | None = None
        self._over = False
        self._decided = False       # the mark's question is answered
        self.dropped = 0            # blocks not written (a held row, the checklist)
        self.matched = 0            # blocks that matched a saved row
        self.marked = False

    def keep(self, blocks: list[dict], rows_of: Callable[[list[dict]], list[Row]], *,
             todo_block: dict | None = None, early: bool = False) -> tuple[list[dict], int]:
        """The blocks of one save to write, in order, and the id of the saved
        checklist row the pump's checklist block takes (0 for none). A block
        in the result may be a copy with only the words the chat does not
        hold yet. ``rows_of``: the pump's own serialiser. ``todo_block``: the
        pump's checklist block. ``early``: a steer's save or a checkpoint,
        which may hold only a piece of the replayed head."""
        keys: list[tuple | None] = []
        for block in blocks:
            rows = rows_of([block])
            keys.append(key_of(rows[0]) if rows else None)   # None: no row of its own
        # The last saved tool of this save, before any tool the chat does not
        # hold: what comes ahead of it was saved (rule 2).
        last_saved = -1
        for i, key in enumerate(keys):
            if key is not None and key[0] == _TOOL:
                if key not in self._tool_at:
                    break
                last_saved = i
        head: dict[int, dict | None] | None = None
        kept: list[dict] = []
        adopted = 0
        for i, (block, key) in enumerate(zip(blocks, keys, strict=True)):
            if key is None:
                kept.append(block)
                continue
            if key[0] == _TOOL:
                at = self._tool_at.get(key)
                if at is not None:
                    self._pos = at + 1
                    self._drop()
                    continue
                self._over = True
                kept.append(block)
                continue
            if self._over:
                kept.append(block)
                continue
            if key == (_TODO,):
                if self._todo_row and block is todo_block:
                    adopted = self._todo_row
                    self.dropped += 1
                    continue
                kept.append(block)
                continue
            if i < last_saved:
                self._drop()
                continue
            if head is None and self._pos is None:
                # The replay's head, with no saved tool behind it or ahead of
                # it in this save (rule 4), decided once for its whole run.
                head = self._match_head(blocks, keys, i)
            if head is not None or self._pos is None:
                part = (head or {}).get(i, block)
                if part is block:
                    kept.append(block)
                elif part is None:
                    self._drop()
                else:
                    self._part(kept, part)
                continue
            step = self._match_from(self._pos, key)
            if step is None:
                kept.append(block)
                continue
            self._pos, words = step
            if words:
                self._part(kept, {**block, "content": words})
            else:
                self._drop()
        whole = not early or any(key is not None and key[0] == _TOOL for key in keys)
        if not self._decided and whole and any(key is not None for key in keys):
            # This save holds the whole replayed head (a tool ends it, and so
            # does a turn's end). Nothing up to here matched a saved row: the
            # replay starts past them.
            self._decided = True
            if not self.matched:
                self._over = True
                if self._marker:
                    self.marked = True
                    kept.insert(0, {"type": wire.LIVE_TEXT, "content": self._marker})
        return kept, adopted

    def _drop(self) -> None:
        self.dropped += 1
        self.matched += 1

    def _part(self, kept: list[dict], block: dict) -> None:
        """A text whose first words the saved rows hold: written from there."""
        self.matched += 1
        kept.append(block)

    def _match_from(self, pos: int, key: tuple) -> tuple[int, str] | None:
        """One replayed block against the saved rows from ``pos``, in step:
        the next position and the words of its text the saved rows do not
        hold ("" for none), or None when it is not the next saved row. A
        saved card, note or checklist the replay does not produce again is
        stepped over; a text or a tool never is."""
        n = len(self._keys)
        at = self._next_row(pos, key)
        if at >= n:
            return None
        if self._keys[at] == key:
            return at + 1, ""
        if key[0] != _TEXT or self._keys[at][0] != _TEXT:
            return None
        # The first run split this text around a block the replay does not
        # produce: the saved rows hold it in pieces.
        want: str = key[1]
        said, j = "", at
        while j < n:
            kind = self._keys[j][0]
            if kind == _TEXT:
                said += self._raw[j]
                got = said.strip()
                if got == want:
                    return j + 1, ""
                if not want.startswith(got):
                    return None
            elif kind == _TOOL:
                return None
            j += 1
        # The saved pieces end inside this text: the rest of it is new.
        return n, want[len(said.strip()):].strip()

    def _next_row(self, pos: int, key: tuple) -> int:
        """The saved row a block at ``pos`` is compared with: ``pos``, past
        the cards, notes and checklists that are not this block."""
        at = pos
        while (at < len(self._keys) and self._keys[at] != key
               and self._keys[at][0] in (_EVENT, _TODO) and at - pos < _LOOKAHEAD):
            at += 1
        return at

    def _match_head(self, blocks: list[dict], keys: list[tuple | None],
                    start: int) -> dict[int, dict | None]:
        """The replay's head run (the texts and events from ``start`` to the
        save's first tool) against the END of the saved rows: for each block
        the saved rows hold, None to drop it or the copy to write (its new
        words). The run is taken only when it walks the saved rows in step
        up to the last one, from some row after their last tool. A run that
        does not is new: the replay starts past the saved rows.

        The replay may start inside a text: its first block is then the end
        of a saved text. That alone proves nothing (a short new line can end
        an older one), so it counts only when the next block walks on."""
        run: list[tuple[int, tuple]] = []
        for i in range(start, len(blocks)):
            key = keys[i]
            if key is not None and key[0] == _TOOL:
                break
            if key is not None and key != (_TODO,):
                run.append((i, key))
        n = len(self._keys)
        after_tools = max(self._tool_at.values(), default=-1) + 1
        for first in range(max(after_tools, n - _HEAD_ROWS), n):
            pos = first
            taken: dict[int, dict | None] = {}
            proven = 0
            for nth, (i, key) in enumerate(run):
                step = self._match_from(pos, key)
                if step is not None and step[1] and len(key[1]) - len(step[1]) < _MIN_PIECE:
                    step = None     # too few saved words to say this text goes on from them
                if step is not None:
                    proven += 1
                elif nth == 0 and key[0] == _TEXT and key[1]:
                    at = self._next_row(pos, key)
                    if at < n and self._keys[at][0] == _TEXT and self._keys[at][1].endswith(key[1]):
                        step = (at + 1, "")
                if step is None:
                    break
                pos, rest = step
                taken[i] = {**blocks[i], "content": rest} if rest else None
                if pos >= n:
                    break
            while pos < n and self._keys[pos][0] in (_EVENT, _TODO):
                pos += 1        # what the replay does not produce again
            if pos >= n and proven:
                self._pos = n
                return taken
        return {}
