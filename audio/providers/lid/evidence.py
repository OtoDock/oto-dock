"""The word evidence: two streams' readings of the same speech, compared.

The acoustic identifier opens a candidate stream and proposes a switch; it
cannot tell an accent from a language (a Greek speaker's English reads as
Greek on every window). The speech-to-text words can: the stream in the
spoken language returns the right words with a high word confidence, the
other one returns few words or garbage with a low one. ``compare`` judges a
proposed switch on the two streams' ``Heard`` ledgers (numbers only) over
the audio both have covered.

Pure: no clock, no I/O, no provider names. ``target`` is the stream a switch
proposes to show, ``shown`` the one shown now; a switch back to the default
is ``compare(default, active)``, the same rule. The rule is asymmetric on
purpose: a wrong TARGET shows garbage, a wrong SHOWN only delays a switch, so
a win needs more words, a clearer margin on few words and its floor, and a
refusal does not. The constants were set by replaying the measured streams of
accented and native speakers through it (``audio/models/README.md`` names
the model they hold for).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from audio.providers.stt.base import Heard

#: Fewer words on a side is no evidence from that side while speech goes on
#: (a refusal needs this many of the target's).
MIN_WORDS = 2
#: A switch while speech goes on needs this many of the target's words.
TARGET_MIN_WORDS = 3
#: The mean word confidence gap that separates the languages, and the one a
#: settled comparison of at most two words a side needs.
MARGIN = 0.10
SETTLED_MARGIN = 0.20
#: A win by the margin needs the target's words above this (a stream in the
#: wrong language returns garbage below it: two such streams on a third
#: language differ by the margin at times, and neither may win).
FLOOR = 0.78
#: The opponent "heard nothing": no word at all, or (once the target has
#: ``2 * MIN_WORDS``) fewer than ``MIN_WORDS`` and at most this share of the
#: target's (a lone foreign word in the speech). A win against nothing needs
#: the target's words above ``NOTHING_FLOOR``: an empty opponent is strong
#: evidence, a worse line lowers a real speaker's confidence.
NOTHING_SHARE = 1 / 3
NOTHING_FLOOR = 0.70
#: The target's worded coverage that makes a verdict decisive.
WINDOW_FINAL_S = 3.0

TARGET = "target"
SHOWN = "shown"
UNDECIDED = "undecided"

_EPSILON = 1e-9


@dataclass(frozen=True)
class Side:
    """One stream's ledger on the shared clock, and where its audio
    begins."""

    heard: Sequence[Heard]
    born_at: float

    @property
    def covered_end(self) -> float:
        """How far the stream has answered (an empty entry is coverage)."""
        return max((h.end for h in self.heard), default=self.born_at)


@dataclass(frozen=True)
class Verdict:
    outcome: str
    target_words: float
    target_confidence: float
    shown_words: float
    shown_confidence: float
    span: tuple[float, float]
    #: The time the target's words stretch over inside the span (speech,
    #: never the silence around it).
    evidence_s: float
    #: A verdict that decides over ``WINDOW_FINAL_S`` of the target's words.
    decisive: bool
    #: Some of the target's counted words come from a final result.
    target_final: bool = False


@dataclass(frozen=True)
class _Tally:
    words: float
    confidence: float
    last: float
    #: The time the words themselves take (the pauses between them never count).
    worded_s: float
    final: bool


def _tally(side: Side, start: float, end: float) -> _Tally:
    """The side's words heard in the span: a timed word belongs to it by
    its middle; an entry without word marks counts its words by the share
    of it inside the span (a result's window can grow over a whole
    utterance, so the entry alone does not say where its words are)."""
    words = weight = worded = 0.0
    last = start
    final = False
    for h in side.heard:
        if h.marks:
            for a, b, c in h.marks:
                if start <= (a + b) / 2 < end:
                    words += 1
                    weight += c
                    worded += max(0.0, min(b, end) - max(a, start))
                    last = max(last, b)
                    final = final or h.final
        elif h.words:
            lo, hi = max(h.start, start), min(h.end, end)
            if h.end > h.start:
                share = max(0.0, hi - lo) / (h.end - h.start)
            else:
                share = 1.0 if start <= h.start < end else 0.0
            if share > 0:
                words += h.words * share
                weight += h.words * share * h.confidence
                worded += max(0.0, hi - lo)
                last = max(last, hi)
                final = final or h.final
    return _Tally(words, weight / words if words else 0.0, last, worded, final)


def _finalised(side: Side) -> bool:
    """No partial with words left: what the stream heard is final."""
    return not any(h.words and not h.final for h in side.heard)


def compare(target: Side, shown: Side, *, settled: bool = False) -> Verdict:
    """The words' verdict on showing ``target`` instead of ``shown``.

    ``settled``: the speech has ended (a call's take, a dictation's stop):
    once both streams finalised what they heard, one word on a side is
    evidence (a first partial is not)."""
    settled = settled and _finalised(target) and _finalised(shown)
    start = max(target.born_at, shown.born_at)
    end = max(start, min(target.covered_end, shown.covered_end))
    t = _tally(target, start, end)
    sh = _tally(shown, start, end)
    evidence = t.worded_s
    min_words = 1 if settled else MIN_WORDS
    win_words = 1 if settled else TARGET_MIN_WORDS
    nothing = sh.words < _EPSILON or (
        t.words >= 2 * MIN_WORDS - _EPSILON and sh.words < min_words - _EPSILON
        and sh.words <= NOTHING_SHARE * t.words + _EPSILON)
    few = settled and t.words <= 2 + _EPSILON and sh.words <= 2 + _EPSILON
    margin = SETTLED_MARGIN if few else MARGIN
    gap = t.confidence - sh.confidence

    if t.words < min_words - _EPSILON:
        outcome = UNDECIDED
    elif nothing:
        # The shown stream heard the target's words and found (nearly) none.
        covered = shown.covered_end >= t.last - _EPSILON
        won = covered and t.words >= win_words - _EPSILON and t.confidence >= NOTHING_FLOOR - _EPSILON
        outcome = TARGET if won else UNDECIDED
    elif sh.words < min_words - _EPSILON:
        outcome = UNDECIDED
    elif gap >= margin - _EPSILON:
        won = t.words >= win_words - _EPSILON and t.confidence >= FLOOR - _EPSILON
        outcome = TARGET if won else UNDECIDED
    elif gap <= -MARGIN + _EPSILON:
        outcome = SHOWN
    else:
        outcome = UNDECIDED
    return Verdict(
        outcome=outcome, target_words=t.words, target_confidence=t.confidence,
        shown_words=sh.words, shown_confidence=sh.confidence, span=(start, end),
        evidence_s=evidence,
        decisive=outcome != UNDECIDED and evidence >= WINDOW_FINAL_S - _EPSILON,
        target_final=t.final,
    )
