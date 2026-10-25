"""The word evidence, pure: two streams' ledgers of the same speech in, a
verdict out. The fixtures are the measured numbers of the operator's clips
(mean word confidence per stream, nova-3) laid out as ledgers."""

import pytest
from audio.providers.lid import evidence as E
from audio.providers.lid.evidence import SHOWN, TARGET, UNDECIDED, Side, compare
from audio.providers.stt.base import Heard


def ledger(*entries, final=True) -> list[Heard]:
    """(start, end, words, confidence) entries, finals unless told."""
    return [Heard(s, e, w, c if w else 0.0, final) for s, e, w, c in entries]


def speech(words: int, confidence: float, *, start=0.0, seconds=6.0, pieces=3) -> list[Heard]:
    """``words`` spread over ``pieces`` finals covering ``seconds`` of speech."""
    step = seconds / pieces
    per = [words // pieces + (1 if i < words % pieces else 0) for i in range(pieces)]
    return ledger(*[(start + i * step, start + (i + 1) * step, per[i], confidence) for i in range(pieces)])


# ── The table: the words decide where the ear could not ──────────────────

ACCENTED = [(19, 0.96, 17, 0.65), (25, 0.97, 23, 0.73), (23, 0.96, 22, 0.70), (25, 0.97, 22, 0.72), (19, 0.98, 14, 0.75)]


@pytest.mark.parametrize("en_words,en_conf,el_words,el_conf", ACCENTED)
def test_accented_english_refuses_the_greek_stream_decisively(en_words, en_conf, el_words, el_conf):
    verdict = compare(Side(speech(el_words, el_conf), 0.0), Side(speech(en_words, en_conf), 0.0))
    assert verdict.outcome == SHOWN
    assert verdict.decisive and verdict.evidence_s >= E.WINDOW_FINAL_S
    assert (verdict.target_words, verdict.shown_words) == (el_words, en_words)


@pytest.mark.parametrize("el_words,el_conf", [(17, 0.93), (20, 0.88)])
def test_greek_switches_when_the_english_stream_heard_the_span_and_found_nothing(el_words, el_conf):
    nothing = ledger((0.0, 3.0, 0, 0.0), (3.0, 6.0, 0, 0.0))
    verdict = compare(Side(speech(el_words, el_conf), 0.0), Side(nothing, 0.0))
    assert verdict.outcome == TARGET and verdict.shown_words == 0


def test_a_lone_foreign_word_on_the_shown_side_is_no_evidence_for_it():
    """The third Greek clip: the English stream caught "Agent" at 0.28."""
    agent = ledger((0.0, 3.0, 1, 0.28), (3.0, 6.0, 0, 0.0))
    verdict = compare(Side(speech(19, 0.89), 0.0), Side(agent, 0.0))
    assert verdict.outcome == TARGET
    # A confident lone word is no evidence either: a minority of the span's words.
    confident = ledger((0.0, 3.0, 1, 0.95), (3.0, 6.0, 0, 0.0))
    assert compare(Side(speech(19, 0.89), 0.0), Side(confident, 0.0)).outcome == TARGET


# ── The rules ────────────────────────────────────────────────────────────

def marked(start, end, *words, final=False) -> Heard:
    """One result whose words carry marks: (start, end, confidence) each."""
    confs = [c for _a, _b, c in words]
    return Heard(start, end, len(words), sum(confs) / len(confs) if confs else 0.0, final, tuple(words))


SIX_WORDS = [(0.1 + 0.3 * i, 0.4 + 0.3 * i, 0.9) for i in range(6)]       # 0.1 s to 1.9 s


def test_an_empty_shown_side_wins_only_once_it_covered_the_targets_words():
    el = [marked(0.0, 2.0, *SIX_WORDS)]
    behind = ledger((0.0, 0.5, 0, 0.0))                     # one of the six words in its span
    assert compare(Side(el, 0.0), Side(behind, 0.0)).outcome == UNDECIDED
    halfway = ledger((0.0, 1.2, 0, 0.0))                    # the fourth word ends past it
    assert compare(Side(el, 0.0), Side(halfway, 0.0)).outcome == UNDECIDED
    covered = ledger((0.0, 2.0, 0, 0.0))
    assert compare(Side(el, 0.0), Side(covered, 0.0)).outcome == TARGET


def test_an_empty_shown_side_needs_a_target_above_its_own_floor():
    """An opponent that heard nothing at all is strong evidence: a real
    speaker on a worse line wins above ``NOTHING_FLOOR``, garbage below it
    never does."""
    nothing = ledger((0.0, 6.0, 0, 0.0))
    assert compare(Side(speech(15, 0.65), 0.0), Side(nothing, 0.0)).outcome == UNDECIDED
    assert compare(Side(speech(15, 0.72), 0.0), Side(nothing, 0.0)).outcome == TARGET


def test_too_few_target_words_while_speech_goes_on_is_undecided():
    verdict = compare(Side(ledger((0.0, 1.0, 1, 0.99)), 0.0), Side(ledger((0.0, 1.0, 0, 0.0)), 0.0))
    assert verdict.outcome == UNDECIDED and verdict.target_words == 1


def test_the_margin_band_is_undecided_and_its_edges_decide():
    shown = Side(speech(12, 0.80), 0.0)
    assert compare(Side(speech(12, 0.80 + E.MARGIN / 2), 0.0), shown).outcome == UNDECIDED
    assert compare(Side(speech(12, 0.80 + E.MARGIN), 0.0), shown).outcome == TARGET
    assert compare(Side(speech(12, 0.80 - E.MARGIN), 0.0), shown).outcome == SHOWN


def test_two_streams_in_the_wrong_language_never_switch():
    """German speech on the English and the French stream: both return
    garbage, and the margin between two garbages is no win."""
    french = Side(speech(11, 0.68), 0.0)
    english = Side(speech(18, 0.55), 0.0)
    assert compare(french, english).outcome == UNDECIDED
    assert compare(french, english, settled=True).outcome == UNDECIDED


def test_a_settled_utterance_decides_on_one_word_each_side():
    """A one-word first answer ("Ναι") after the speech ended and both
    streams were finalised."""
    el = ledger((0.0, 0.8, 1, 0.95))
    en = ledger((0.0, 0.8, 1, 0.55))
    assert compare(Side(el, 0.0), Side(en, 0.0)).outcome == UNDECIDED
    assert compare(Side(el, 0.0), Side(en, 0.0), settled=True).outcome == TARGET
    nothing = ledger((0.0, 0.9, 0, 0.0))
    assert compare(Side(el, 0.0), Side(nothing, 0.0), settled=True).outcome == TARGET
    # English stays: the English word is the clear one.
    assert compare(Side(en, 0.0), Side(el, 0.0), settled=True).outcome == SHOWN


def test_decisive_counts_worded_coverage_not_silence():
    """Silence between the words never makes a refusal decisive."""
    sparse = ledger((0.0, 1.0, 4, 0.7), (1.0, 9.0, 0, 0.0), (9.0, 10.0, 3, 0.7))
    shown = ledger((0.0, 1.0, 4, 0.97), (1.0, 9.0, 0, 0.0), (9.0, 10.0, 3, 0.97))
    verdict = compare(Side(sparse, 0.0), Side(shown, 0.0))
    assert verdict.outcome == SHOWN and verdict.evidence_s == pytest.approx(2.0) and not verdict.decisive
    dense = speech(20, 0.7, seconds=6.0)
    assert compare(Side(dense, 0.0), Side(speech(20, 0.97, seconds=6.0), 0.0)).decisive
    # Marked words: their own time, the pauses between them left out.
    gappy = [marked(0.0, 8.0, *[(i * 1.0, i * 1.0 + 0.3, 0.7) for i in range(8)], final=True)]
    clear = [marked(0.0, 8.0, *[(i * 1.0, i * 1.0 + 0.3, 0.97) for i in range(8)], final=True)]
    verdict = compare(Side(gappy, 0.0), Side(clear, 0.0))
    assert verdict.outcome == SHOWN and verdict.evidence_s == pytest.approx(2.4) and not verdict.decisive


def test_the_latest_interim_counts_with_the_finals():
    el = ledger((0.0, 2.0, 5, 0.9)) + ledger((2.0, 3.5, 4, 0.9), final=False)
    en = ledger((0.0, 2.0, 0, 0.0)) + ledger((2.0, 3.5, 0, 0.0), final=False)
    verdict = compare(Side(el, 0.0), Side(en, 0.0))
    assert verdict.outcome == TARGET and verdict.target_words == 9


def test_the_rule_is_symmetric_a_switch_back_compares_the_default_as_target():
    """The words call the shown stream back: the default beat the active."""
    en = speech(20, 0.97)
    el = speech(18, 0.68)
    verdict = compare(Side(en, 0.0), Side(el, 0.0))
    assert verdict.outcome == TARGET and verdict.decisive


def test_the_span_starts_where_both_streams_audio_does():
    """A candidate born at 2.0 s (its replay began there): the default's
    words of the turn before are outside the span."""
    en = ledger((0.0, 1.8, 6, 0.4), (2.0, 5.0, 10, 0.97))
    el = ledger((2.0, 5.0, 9, 0.7))
    verdict = compare(Side(el, 2.0), Side(en, 0.0))
    assert verdict.span == (2.0, 5.0) and verdict.shown_words == 10 and verdict.outcome == SHOWN


def test_words_count_where_their_marks_place_them():
    """A growing window (a dictation's interim over the whole utterance)
    holds words all along it: a short early span counts the words heard in
    it, not none of them and not all."""
    en = [marked(0.0, 4.0, *[(0.2 + 0.4 * i, 0.5 + 0.4 * i, 0.97) for i in range(10)])]
    el = [marked(0.8, 1.9, (0.85, 1.15, 0.9), (1.2, 1.5, 0.88), (1.5, 1.85, 0.89))]
    verdict = compare(Side(el, 0.8), Side(en, 0.0))
    assert verdict.span == (0.8, 1.9) and verdict.shown_words == 2      # not 0: the English stream heard them
    assert verdict.outcome == UNDECIDED                                  # too few to compare, too many to be nothing


def test_an_unmarked_entry_counts_by_its_share_inside_the_span():
    en = ledger((1.0, 4.0, 9, 0.96))
    el = ledger((1.7, 3.0, 4, 0.7), (3.0, 6.0, 6, 0.7), final=True)
    verdict = compare(Side(el, 1.7), Side(en, 0.0))
    assert verdict.span == (1.7, 4.0)
    assert verdict.shown_words == pytest.approx(9 * 2.3 / 3.0) and verdict.target_words == pytest.approx(6.0)
    assert verdict.outcome == SHOWN


def test_nothing_covered_is_undecided():
    assert compare(Side([], 0.0), Side(speech(10, 0.9), 0.0)).outcome == UNDECIDED
    assert compare(Side(speech(10, 0.9), 0.0), Side([], 0.0)).outcome == UNDECIDED


def test_a_settled_utterance_waits_for_its_finals_before_one_word_decides():
    """The stop came, but the candidate's first partial ("Καλη…") is not its
    reading of the word: one word a side counts once both are final."""
    el_partial = ledger((0.0, 1.0, 1, 0.70), final=False)
    en_final = ledger((0.0, 1.7, 2, 0.75))
    assert compare(Side(el_partial, 0.0), Side(en_final, 0.0), settled=True).outcome == UNDECIDED
    el_final = ledger((0.0, 1.7, 3, 0.95))
    assert compare(Side(el_final, 0.0), Side(en_final, 0.0), settled=True).outcome == TARGET
    # An empty partial after the finals (trailing silence) is no word left.
    el_after = el_final + ledger((1.7, 2.5, 0, 0.0), final=False)
    assert compare(Side(el_after, 0.0), Side(en_final, 0.0), settled=True).outcome == TARGET


# ── The asymmetry: a wrong win shows garbage, a wrong refusal only delays ──

def test_one_long_english_word_is_not_nothing():
    """The candidate's first answer covers about a second: English heard one
    long word ("Unfortunately"), Greek three Latin-letter tokens at 0.85.
    Not "nothing" (the English stream heard a word), and too few to win."""
    en = [marked(0.0, 1.2, (0.1, 1.1, 0.95))]
    el = [marked(0.0, 1.2, (0.1, 0.4, 0.85), (0.4, 0.7, 0.86), (0.7, 1.1, 0.84))]
    assert compare(Side(el, 0.0), Side(en, 0.0)).outcome == UNDECIDED


def test_two_weak_accented_words_against_two_clean_tokens_never_win_mid_speech():
    en = [marked(0.0, 1.0, (0.1, 0.5, 0.59), (0.5, 0.9, 0.79))]
    el = [marked(0.0, 1.0, (0.1, 0.5, 0.88), (0.5, 0.9, 0.88))]
    assert compare(Side(el, 0.0), Side(en, 0.0)).outcome == UNDECIDED
    # A refusal on two words a side is allowed: it only delays.
    assert compare(Side(en, 0.0), Side(el, 0.0)).outcome == SHOWN


def test_a_win_mid_speech_needs_three_target_words():
    nothing = ledger((0.0, 2.0, 0, 0.0))
    two = [marked(0.0, 2.0, (0.2, 0.6, 0.95), (0.7, 1.1, 0.95))]
    three = [marked(0.0, 2.0, (0.2, 0.6, 0.95), (0.7, 1.1, 0.95), (1.2, 1.6, 0.95))]
    assert compare(Side(two, 0.0), Side(nothing, 0.0)).outcome == UNDECIDED
    assert compare(Side(three, 0.0), Side(nothing, 0.0)).outcome == TARGET


def test_a_lone_shown_word_is_nothing_only_against_many_target_words():
    lone = [marked(0.0, 3.0, (0.5, 0.9, 0.3))]
    three = [marked(0.0, 3.0, *[(0.2 + 0.6 * i, 0.6 + 0.6 * i, 0.9) for i in range(3)])]
    four = [marked(0.0, 3.0, *[(0.2 + 0.6 * i, 0.6 + 0.6 * i, 0.9) for i in range(4)])]
    assert compare(Side(three, 0.0), Side(lone, 0.0)).outcome == UNDECIDED
    assert compare(Side(four, 0.0), Side(lone, 0.0)).outcome == TARGET


def test_a_settled_comparison_of_two_words_a_side_needs_the_wider_margin():
    en = ledger((0.0, 1.0, 2, 0.75))
    narrow = ledger((0.0, 1.0, 2, 0.90))
    wide = ledger((0.0, 1.0, 2, 0.96))
    assert compare(Side(narrow, 0.0), Side(en, 0.0), settled=True).outcome == UNDECIDED
    assert compare(Side(wide, 0.0), Side(en, 0.0), settled=True).outcome == TARGET
    # An opponent that heard nothing needs no wider margin.
    nothing = ledger((0.0, 1.0, 0, 0.0))
    assert compare(Side(ledger((0.0, 1.0, 1, 0.9)), 0.0), Side(nothing, 0.0), settled=True).outcome == TARGET


def test_the_verdict_says_whether_the_targets_words_are_final():
    nothing = ledger((0.0, 2.0, 0, 0.0))
    assert not compare(Side(ledger((0.0, 2.0, 4, 0.9), final=False), 0.0), Side(nothing, 0.0)).target_final
    assert compare(Side(ledger((0.0, 2.0, 4, 0.9)), 0.0), Side(nothing, 0.0)).target_final
