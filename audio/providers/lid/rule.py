"""The language rule: pure, no clock, no I/O.

Every surface runs the same rule with TWO parameters of its own:
``switch_at`` (the posterior that moves the shown transcript: LEAN on
dictation and live voice, where a person watches and a wrong early switch is
corrected by the next reading, SURE on calls, where the caller hears the
voice change) and ``lean_at_take`` (whether the first turn's take lets a
LEAN leader win: a call's dispatch and a dictation's stop; live voice's
takes ask for no verdict). Readings arrive as the identifier's log scores
plus the voiced seconds so far; the rule answers with events the consumer
applies:

- ``open(L)``: a candidate language is worth a stream (its posterior reached
  ``lean_at``), emitted when it leads and is not the one already open (a
  candidate the streams closed may open again); the streams keep the
  default, the active and at most one more, so a new candidate closes the
  previous one.
- ``switch(L)``: the shown transcript moves to L. On calls only at
  ``switch_at`` = SURE; on dictation and live voice at LEAN, as often as the
  leader changes, back to the default included, until final.
- ``final(L)``: the decision stands. The readings over the last
  ``final_after_s`` of voice all named the current language and that much
  voice passed since the last switch; or ``MAX_VOICED_S`` of voice passed
  (the current language stands, no flip-flop forever).

The take rule (``take``): at the first turn's take on a call, and at a
dictation's stop, a LEAN leader wins once (the caller's first words reach
the agent in their language); a very short first utterance (a one-word
answer) wins at SURE only, since the
benchmark put the Latin one-word answers at 42 to 75 per cent right and the
Greek and English ones at 100. A call finalises at its second take at the
latest. The numbers come from the benchmark the thresholds were set by
(``audio/models/README.md`` names the model they hold for).

The words have the last say. The ear opens a candidate and proposes the
switch; the identifier cannot tell an accent from a language, so when the
streams' providers report a word confidence the streams compare the two
readings of the same speech (``audio.providers.lid.evidence``) and the
consumer hands the outcome back here: ``refused`` (the shown stream's
words won: the rule goes back to it, and a decisive refusal long enough
after that candidate opened finalises), or ``words_switch`` (the default's
words decisively beat the active stream's: the rule switches back).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: The posterior that moves the shown transcript on dictation and live
#: voice, and opens a candidate stream everywhere.
LEAN = 0.6
#: The posterior that moves a call (the caller hears the voice change).
SURE = 0.9
#: Log-score bias on the default language: an accented speaker of the
#: default stays, a clear speaker of another language still moves.
PRIOR = 1.0
#: Voiced seconds before the first reading, between readings, before the
#: first switch (a single 1 s reading moves a tenth of the default
#: speakers, a 2 s one none), of stable readings before a final, and of
#: voice after which the current language stands whatever the readings.
MIN_VOICED_S = 1.0
STEP_VOICED_S = 0.5
SWITCH_MIN_VOICED_S = 1.5
FINAL_AFTER_S = 3.0
MAX_VOICED_S = 8.0
#: A take with at least this much voice (and less than ``MIN_VOICED_S``)
#: gets one reading that wins at SURE only.
MIN_WORD_S = 0.5
#: The speech the identifier reads at once.
WINDOW_S = 3.0

OPEN = "open"
SWITCH = "switch"
FINAL = "final"


@dataclass(frozen=True)
class Event:
    kind: str
    language: str


class LanguageRule:
    """One utterance's worth of readings, one decision."""

    def __init__(
        self, default: str, candidates: list[str] | tuple[str, ...] | frozenset[str], switch_at: float, *,
        prior: float = PRIOR, lean_at: float = LEAN, final_after_s: float = FINAL_AFTER_S,
        lean_at_take: bool = False,
    ) -> None:
        self.default = default
        self.current = default
        self.candidates = [c for c in candidates if c != default]
        self.switch_at = switch_at
        self.prior = prior
        self.lean_at = lean_at
        self.final_after_s = final_after_s
        self.lean_at_take = lean_at_take
        self.final: str | None = None
        self.frozen = False
        #: The candidate the consumer was told to open (one at a time).
        self.opened: str | None = None
        self._unavailable: set[str] = set()
        self._history: list[tuple[float, str, float]] = []
        self._switched_at = 0.0
        self._takes = 0
        self.latest: dict[str, float] | None = None
        #: The language before the last switch, the voiced second of each
        #: OPEN, and the voice so far.
        self._previous = default
        self._opened_at: dict[str, float] = {}
        self._voiced_s = 0.0

    # ── Readings ───────────────────────────────────────────────────

    @property
    def languages(self) -> list[str]:
        return [self.default, *self.candidates]

    def posterior(self, log_scores: dict[str, float]) -> dict[str, float]:
        """Softmax over the default and the install's candidates (never
        narrowed further: a language the line cannot serve stays in the
        denominator, so its speakers are not pushed onto the runner-up),
        with the prior on the default."""
        raw = {lang: float(log_scores.get(lang, -math.inf)) for lang in self.languages}
        raw[self.default] += self.prior
        top = max(raw.values())
        weights = {lang: math.exp(score - top) for lang, score in raw.items()}
        total = sum(weights.values()) or 1.0
        return {lang: weight / total for lang, weight in weights.items()}

    @property
    def done(self) -> bool:
        return self.frozen or self.final is not None

    def reading(self, log_scores: dict[str, float], voiced_s: float) -> list[Event]:
        if self.done:
            return []
        posterior = self.posterior(log_scores)
        self.latest = posterior
        self._voiced_s = max(self._voiced_s, voiced_s)
        leader = max(posterior, key=posterior.get)
        p = posterior[leader]
        self._history.append((voiced_s, leader, p))
        events: list[Event] = []
        if leader != self.current and leader not in self._unavailable:
            if leader != self.default and p >= self.lean_at and self.opened != leader:
                events.append(self._open(leader, voiced_s))
            if p >= self.switch_at and voiced_s >= SWITCH_MIN_VOICED_S:
                events.append(self._switch(leader, voiced_s))
        final = self._final_due(voiced_s)
        if final is not None:
            events.append(final)
        return events

    def _open(self, language: str, voiced_s: float) -> Event:
        self.opened = language
        self._opened_at[language] = voiced_s
        return Event(OPEN, language)

    def _switch(self, language: str, voiced_s: float) -> Event:
        if language != self.current:
            self._previous = self.current
        self.current = language
        self._switched_at = voiced_s
        return Event(SWITCH, language)

    def _final_due(self, voiced_s: float) -> Event | None:
        if voiced_s >= MAX_VOICED_S:
            return self._finalise()
        if voiced_s - self._switched_at < self.final_after_s:
            return None
        span = [leader for t, leader, _p in self._history if t > voiced_s - self.final_after_s]
        if span and all(leader == self.current for leader in span):
            return self._finalise()
        return None

    def _finalise(self) -> Event:
        self.final = self.current
        return Event(FINAL, self.current)

    # ── The take rule ──────────────────────────────────────────────

    def take(self, voiced_s: float, posterior: dict[str, float] | None) -> list[Event]:
        """A turn was taken: a call's or live voice's dispatch (never a queued
        turn), a dictation's stop. With ``lean_at_take`` the first take lets a
        LEAN leader win once (SURE for a one-word utterance) and the second
        finalises whatever stands; without it, nothing."""
        if self.done or not self.lean_at_take:
            return []
        self._voiced_s = max(self._voiced_s, voiced_s)
        self._takes += 1
        if self._takes >= 2:
            return [self._finalise()]
        if not posterior:
            return []
        leader = max(posterior, key=posterior.get)
        if leader == self.current or leader in self._unavailable:
            return []
        if voiced_s >= MIN_VOICED_S:
            threshold = self.lean_at
        elif voiced_s >= MIN_WORD_S:
            threshold = SURE
        else:
            return []
        if posterior[leader] < threshold:
            return []
        events: list[Event] = []
        if leader != self.default and self.opened != leader:
            events.append(self._open(leader, voiced_s))
        events.append(self._switch(leader, voiced_s))
        return events

    # ── The consumer's word ────────────────────────────────────────

    def unavailable(self, language: str) -> None:
        """The line cannot serve this language (no stream, no voice, the
        candidate failed to open): the rule never moves to it, and never to
        the runner-up in its place."""
        self._unavailable.add(language)
        if self.opened == language:
            self.opened = None

    def expire(self) -> list[Event]:
        """The window closed on audio time (``WINDOW_MAX_S`` after the first
        voice): the current language stands."""
        if self.done:
            return []
        return [self._finalise()]

    def revert(self, language: str) -> None:
        """A switch the streams could not make (no stream, the candidate
        never answered): the rule's current language is the one still
        heard, so the next readings judge against it."""
        if self.final is None:
            self.current = language

    def refused(self, language: str, *, decisive: bool) -> list[Event]:
        """The words refused the switch to ``language`` (the shown stream's
        words won): the rule goes back to the language still shown, and the
        next reading may propose it again with more words heard. A decisive
        refusal (``evidence.WINDOW_FINAL_S`` of the candidate's words lost)
        finalises the shown language, but only once ``final_after_s`` of
        voice passed since that candidate opened: a caller who opens with a
        few words of another language is refused once, not for the call."""
        if self.final is not None or self.current != language:
            return []
        self.current = self._previous
        if not decisive:
            return []
        opened_at = self._opened_at.get(language, self._opened_at.get(self.current, 0.0))
        if self._voiced_s - opened_at < self.final_after_s:
            return []
        return [self._finalise()]

    def words_switch(self, language: str) -> list[Event]:
        """The words call the shown stream back to ``language`` (the default
        decisively beat the active stream): a switch, and the acoustic
        final's stability window starts over."""
        if self.final is not None:
            return []
        return [self._switch(language, self._voiced_s)]

    def candidate_closed(self) -> None:
        """The streams closed the open candidate (a take): the next lean may
        open one again."""
        self.opened = None

    def freeze(self) -> None:
        self.frozen = True
