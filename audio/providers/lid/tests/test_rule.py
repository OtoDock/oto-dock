"""The language rule, pure: scripted readings in, events out."""

import math

import pytest
from audio.providers.lid import rule as R
from audio.providers.lid.rule import LEAN, SURE, Event, LanguageRule

LANGS = ["en", "el", "de"]


def scores(**langs) -> dict[str, float]:
    """Log scores from probabilities over the three languages (the rule adds
    its own prior, so the posterior differs a little from these)."""
    return {lang: math.log(p) for lang, p in langs.items()}


def dictation(default="en", **kw) -> LanguageRule:
    return LanguageRule(default, LANGS, switch_at=LEAN, prior=0.0, **kw)


def calls(default="en", **kw) -> LanguageRule:
    return LanguageRule(default, LANGS, switch_at=SURE, prior=0.0, lean_at_take=True, **kw)


def test_the_posterior_is_over_the_default_and_the_candidates_with_the_prior():
    r = LanguageRule("en", LANGS, switch_at=LEAN, prior=1.0)
    p = r.posterior(scores(en=0.5, el=0.5, de=0.0001))
    assert p["en"] > p["el"] > p["de"]
    assert abs(sum(p.values()) - 1.0) < 1e-9
    # A language outside the install list never enters the softmax.
    assert "fr" not in r.posterior(scores(en=0.3, el=0.3, de=0.3, fr=0.9))


def test_a_lean_opens_once_and_a_switch_waits_for_the_voice_floor():
    r = dictation()
    assert r.reading(scores(en=0.2, el=0.7, de=0.1), 1.0) == [Event(R.OPEN, "el")]
    # Still under 1.5 s of voice: no switch, and the candidate is not reopened.
    assert r.reading(scores(en=0.1, el=0.85, de=0.05), 1.25) == []
    assert r.reading(scores(en=0.1, el=0.85, de=0.05), 1.5) == [Event(R.SWITCH, "el")]
    assert r.current == "el" and r.final is None


def test_dictation_switches_back_and_onward_until_final():
    r = dictation()
    r.reading(scores(en=0.1, el=0.8, de=0.1), 1.5)
    assert r.current == "el"
    assert r.reading(scores(en=0.9, el=0.05, de=0.05), 2.0) == [Event(R.SWITCH, "en")]
    events = r.reading(scores(en=0.1, el=0.1, de=0.8), 2.5)
    assert events == [Event(R.OPEN, "de"), Event(R.SWITCH, "de")]
    assert r.opened == "de"


def test_calls_do_not_switch_below_sure():
    r = calls()
    assert r.reading(scores(en=0.2, el=0.75, de=0.05), 1.5) == [Event(R.OPEN, "el")]
    assert r.reading(scores(en=0.15, el=0.8, de=0.05), 2.0) == []
    assert r.current == "en"
    assert r.reading(scores(en=0.05, el=0.92, de=0.03), 2.5) == [Event(R.SWITCH, "el")]


def test_final_needs_three_stable_seconds_after_the_last_switch():
    r = dictation()
    r.reading(scores(en=0.1, el=0.8, de=0.1), 1.5)                  # switch at 1.5 s
    for t in (2.0, 2.5, 3.0, 3.5, 4.0):
        assert r.reading(scores(en=0.1, el=0.8, de=0.1), t) == []
    assert r.reading(scores(en=0.1, el=0.8, de=0.1), 4.5) == [Event(R.FINAL, "el")]
    assert r.final == "el" and r.done
    assert r.reading(scores(en=0.9, el=0.05, de=0.05), 5.0) == []   # nothing after final


def test_an_unstable_leader_delays_the_final_until_the_voice_cap():
    r = dictation()
    leaders = [("el", 1.0), ("en", 1.5), ("el", 2.0), ("en", 2.5), ("el", 3.0), ("en", 3.5),
               ("el", 4.0), ("en", 4.5), ("el", 5.0), ("en", 5.5), ("el", 6.0), ("en", 6.5), ("el", 7.0), ("en", 7.5)]
    for leader, t in leaders:
        other = "en" if leader == "el" else "el"
        events = r.reading(scores(**{leader: 0.8, other: 0.15, "de": 0.05}), t)
        assert Event(R.FINAL, r.current) not in events
    events = r.reading(scores(en=0.8, el=0.15, de=0.05), 8.0)
    assert events[-1] == Event(R.FINAL, "en")


def test_the_prior_keeps_an_accented_default_speaker():
    r = LanguageRule("en", LANGS, switch_at=LEAN, prior=1.0)
    # Raw scores lean Spanish-ish German at 0.6 against English 0.35: the
    # prior (e) puts English back in front.
    assert r.reading(scores(en=0.35, el=0.05, de=0.6), 2.0) == []
    assert r.current == "en"


def test_an_unavailable_leader_never_moves_and_never_falls_to_the_runner_up():
    r = dictation()
    r.unavailable("el")
    assert r.reading(scores(en=0.1, el=0.8, de=0.1), 2.0) == []
    assert r.current == "en" and r.opened is None


def test_the_take_rule_lets_a_lean_leader_win_once_on_calls():
    r = calls()
    r.reading(scores(en=0.3, el=0.65, de=0.05), 1.5)               # a lean, no switch (SURE)
    assert r.current == "en"
    assert r.take(1.8, r.latest) == [Event(R.SWITCH, "el")]
    assert r.current == "el"
    # The second take finalises what stands.
    assert r.take(4.0, r.latest) == [Event(R.FINAL, "el")]


def test_a_one_word_take_wins_at_sure_only():
    r = calls()
    r.reading(scores(en=0.2, el=0.75, de=0.05), 0.7)               # opens (a lean) but the voice is short
    assert r.take(0.7, r.latest) == []
    r2 = calls()
    events = r2.reading(scores(en=0.03, el=0.95, de=0.02), 0.7)
    assert events == [Event(R.OPEN, "el")]
    assert r2.take(0.7, r2.latest) == [Event(R.SWITCH, "el")]
    r3 = calls()
    assert r3.take(0.3, scores(en=0.01, el=0.98, de=0.01)) == []    # too little voice for any reading to count


def test_the_take_rule_is_off_on_dictation_and_live_voice():
    r = dictation()
    r.reading(scores(en=0.3, el=0.65, de=0.05), 1.2)
    assert r.take(1.3, r.latest) == []
    assert r.take(5.0, r.latest) == []
    assert r.final is None


def test_expire_finalises_the_current_language_once():
    r = dictation()
    r.reading(scores(en=0.1, el=0.8, de=0.1), 1.5)
    assert r.expire() == [Event(R.FINAL, "el")]
    assert r.expire() == []


def test_freeze_stops_everything():
    r = dictation()
    r.freeze()
    assert r.reading(scores(en=0.1, el=0.8, de=0.1), 2.0) == []
    assert r.take(2.0, scores(en=0.1, el=0.8, de=0.1)) == []


@pytest.mark.parametrize("default", ["en", "el"])
def test_the_default_is_never_opened_as_a_candidate(default):
    other = "el" if default == "en" else "en"
    r = dictation(default=default)
    r.reading(scores(**{other: 0.8, default: 0.15, "de": 0.05}), 1.5)
    assert r.current == other
    events = r.reading(scores(**{default: 0.9, other: 0.05, "de": 0.05}), 2.0)
    assert events == [Event(R.SWITCH, default)]


# ── The words' say ───────────────────────────────────────────────────────

def _switched_to_el() -> LanguageRule:
    r = dictation()
    assert r.reading(scores(en=0.1, el=0.8, de=0.1), 1.0) == [Event(R.OPEN, "el")]
    assert r.reading(scores(en=0.1, el=0.8, de=0.1), 1.5) == [Event(R.SWITCH, "el")]
    return r


def test_a_refusal_goes_back_and_the_next_reading_proposes_again():
    r = _switched_to_el()
    assert r.refused("el", decisive=False) == [] and r.current == "en"
    assert r.reading(scores(en=0.1, el=0.8, de=0.1), 2.0) == [Event(R.SWITCH, "el")]


def test_a_decisive_refusal_finalises_only_long_enough_after_the_candidate_opened():
    r = _switched_to_el()
    assert r.refused("el", decisive=True) == [] and r.final is None    # 0.5 s of voice since the open
    for t in (2.0, 2.5, 3.0, 3.5):
        assert r.reading(scores(en=0.1, el=0.8, de=0.1), t) == [Event(R.SWITCH, "el")]
        assert r.refused("el", decisive=False) == []                     # each proposal refused again
    assert r.reading(scores(en=0.1, el=0.8, de=0.1), 4.0) == [Event(R.SWITCH, "el")]
    assert r.refused("el", decisive=True) == [Event(R.FINAL, "en")]
    assert r.final == "en" and r.current == "en"


def test_a_refusal_on_a_final_rule_or_a_switch_it_no_longer_holds_does_nothing():
    r = _switched_to_el()
    r.expire()
    assert r.refused("el", decisive=True) == [] and r.final == "el"
    r2 = _switched_to_el()
    r2.reading(scores(en=0.1, el=0.1, de=0.8), 2.0)                     # the leader moved on to German
    assert r2.current == "de"
    assert r2.refused("el", decisive=True) == [] and r2.current == "de"


def test_the_words_switch_back_and_the_stability_window_starts_over():
    r = _switched_to_el()
    for t in (2.0, 2.5, 3.0):
        r.reading(scores(en=0.1, el=0.8, de=0.1), t)
    assert r.words_switch("en") == [Event(R.SWITCH, "en")]
    assert r.current == "en" and r.final is None
    # The ear still says Greek; the acoustic final waits for three stable
    # seconds of English after the words' switch, the cap still holds.
    for t in (3.5, 4.0, 4.5, 5.0, 5.5):
        assert Event(R.FINAL, "en") not in r.reading(scores(en=0.8, el=0.1, de=0.1), t)
    assert r.reading(scores(en=0.8, el=0.1, de=0.1), 6.0) == [Event(R.FINAL, "en")]
    assert r.words_switch("el") == []                                  # final: nothing moves
