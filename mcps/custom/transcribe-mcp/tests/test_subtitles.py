"""Subtitle generation — the provider-agnostic SRT logic (no network)."""

import server


def _words(*specs):
    """specs: (word, start, end) tuples → the proxy word-dict shape."""
    return [{"word": w, "start": s, "end": e} for (w, s, e) in specs]


# ── timestamp formatting ───────────────────────────────────────────

def test_format_timestamp_basic():
    assert server._format_timestamp(0.0) == "00:00:00,000"
    assert server._format_timestamp(1.5) == "00:00:01,500"
    assert server._format_timestamp(3661.789) == "01:01:01,789"


def test_format_timestamp_negative_clamps_to_zero():
    assert server._format_timestamp(-2.0) == "00:00:00,000"


# ── grouping ───────────────────────────────────────────────────────

def test_per_word_one_cue_each():
    cues = server._words_to_cues(
        _words(("a", 0.0, 0.3), ("b", 0.3, 0.6), ("c", 0.6, 0.9)),
        max_words_per_cue=7, max_chars_per_cue=42, max_duration_s=3.0,
        split_on_pause_ms=400, per_word=True,
    )
    assert [c[2] for c in cues] == ["a", "b", "c"]
    assert cues[0] == (0.0, 0.3, "a")


def test_max_words_per_cue_splits():
    cues = server._words_to_cues(
        _words(("w1", 0, 0.2), ("w2", 0.2, 0.4), ("w3", 0.4, 0.6),
               ("w4", 0.6, 0.8), ("w5", 0.8, 1.0)),
        max_words_per_cue=2, max_chars_per_cue=999, max_duration_s=999,
        split_on_pause_ms=0, per_word=False,
    )
    assert [c[2] for c in cues] == ["w1 w2", "w3 w4", "w5"]


def test_pause_split_starts_new_cue():
    cues = server._words_to_cues(
        _words(("Hello", 0.0, 0.4), ("there", 0.4, 0.8), ("world", 1.3, 1.7)),
        max_words_per_cue=10, max_chars_per_cue=999, max_duration_s=999,
        split_on_pause_ms=400, per_word=False,
    )
    assert [c[2] for c in cues] == ["Hello there", "world"]


def test_sentence_punctuation_flushes():
    cues = server._words_to_cues(
        _words(("Hello", 0.0, 0.4), ("world.", 0.4, 0.9), ("Next", 1.0, 1.4)),
        max_words_per_cue=10, max_chars_per_cue=999, max_duration_s=999,
        split_on_pause_ms=0, per_word=False,
    )
    assert [c[2] for c in cues] == ["Hello world.", "Next"]


def test_max_chars_per_cue_splits():
    cues = server._words_to_cues(
        _words(("alpha", 0, 0.3), ("beta", 0.3, 0.6), ("gamma", 0.6, 0.9)),
        max_words_per_cue=99, max_chars_per_cue=11, max_duration_s=999,
        split_on_pause_ms=0, per_word=False,
    )
    # "alpha beta" = 10 chars (ok); adding " gamma" → 16 > 11 → split.
    assert [c[2] for c in cues] == ["alpha beta", "gamma"]


def test_max_duration_splits():
    cues = server._words_to_cues(
        _words(("a", 0.0, 1.0), ("b", 1.0, 2.0), ("c", 2.0, 3.0)),
        max_words_per_cue=99, max_chars_per_cue=999, max_duration_s=2.5,
        split_on_pause_ms=0, per_word=False,
    )
    # "a b" spans 0→2.0s (ok); adding c → 0→3.0s > 2.5 → split.
    assert [c[2] for c in cues] == ["a b", "c"]


def test_malformed_words_are_skipped():
    cues = server._words_to_cues(
        [{"word": "ok", "start": 0.0, "end": 0.5},
         {"word": "", "start": 0.5, "end": 0.6},        # empty text
         {"word": "bad", "start": None, "end": "x"},     # unparseable
         {"nope": 1}],                                    # missing keys
        max_words_per_cue=7, max_chars_per_cue=42, max_duration_s=3.0,
        split_on_pause_ms=400, per_word=False,
    )
    assert [c[2] for c in cues] == ["ok"]


def test_empty_words_no_cues():
    assert server._words_to_cues(
        [], max_words_per_cue=7, max_chars_per_cue=42, max_duration_s=3.0,
        split_on_pause_ms=400, per_word=False,
    ) == []


# ── SRT rendering ──────────────────────────────────────────────────

def test_format_srt_structure():
    srt = server._format_srt([(0.0, 1.0, "Hi"), (1.0, 2.0, "There")])
    # Each cue is terminated by a blank line (standard SRT), including the last.
    assert srt == (
        "1\n00:00:00,000 --> 00:00:01,000\nHi\n"
        "\n"
        "2\n00:00:01,000 --> 00:00:02,000\nThere\n"
        "\n"
    )


def test_format_srt_zero_length_guard():
    srt = server._format_srt([(1.0, 1.0, "X")])
    assert "00:00:01,000 --> 00:00:01,300" in srt  # bumped +0.3s


def test_format_srt_clamps_overlap():
    # A's end (5) would overlap B's start (1) → clamp A to 1.
    srt = server._format_srt([(0.0, 5.0, "A"), (1.0, 2.0, "B")])
    assert "00:00:00,000 --> 00:00:01,000\nA" in srt


def test_format_srt_empty():
    assert server._format_srt([]) == ""


# ── ASS / karaoke ──────────────────────────────────────────────────

def test_group_words_returns_word_lists():
    cues = server._group_words(
        _words(("a", 0.0, 0.3), ("b", 0.3, 0.6)),
        max_words_per_cue=7, max_chars_per_cue=42, max_duration_s=3.0,
        split_on_pause_ms=0, per_word=False,
    )
    assert cues == [[(0.0, 0.3, "a"), (0.3, 0.6, "b")]]


def test_group_words_per_word():
    cues = server._group_words(
        _words(("a", 0.0, 0.3), ("b", 0.3, 0.6)),
        max_words_per_cue=7, max_chars_per_cue=42, max_duration_s=3.0,
        split_on_pause_ms=0, per_word=True,
    )
    assert cues == [[(0.0, 0.3, "a")], [(0.3, 0.6, "b")]]


def test_ass_timestamp():
    assert server._ass_ts(0.0) == "0:00:00.00"
    assert server._ass_ts(1.5) == "0:00:01.50"
    assert server._ass_ts(3661.23) == "1:01:01.23"


def test_ass_escape_strips_override_chars():
    assert server._ass_escape("a{b}c\\d") == "abcd"


def test_ass_karaoke_line_durations():
    # word0 highlights until word1 starts (0.5s → 50cs); word1 until its own end.
    cue = [(0.0, 0.4, "hello"), (0.5, 1.1, "world")]
    assert server._ass_karaoke_line(cue) == r"{\k50}hello {\k60}world"


def test_format_ass_document():
    ass = server._format_ass([[(0.0, 0.4, "hi"), (0.4, 0.9, "there")]])
    assert "[Script Info]" in ass
    assert "Style: Karaoke," in ass
    assert "[Events]" in ass
    assert r"Dialogue: 0,0:00:00.00,0:00:00.90,Karaoke,,0,0,0,,{\k40}hi {\k50}there" in ass


def test_hex_to_ass_color():
    assert server._hex_to_ass_color("#FFD700", "x") == "&H0000D7FF"   # gold → &H00BBGGRR
    assert server._hex_to_ass_color("00FF00", "x") == "&H0000FF00"     # green, no leading '#'
    assert server._hex_to_ass_color("bad", "DEFAULT") == "DEFAULT"
    assert server._hex_to_ass_color(None, "DEFAULT") == "DEFAULT"


def test_format_ass_position_and_style():
    ass = server._format_ass(
        [[(0.0, 0.4, "hi")]], position="center", primary="&H00112233", font_size=72,
    )
    style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Karaoke,"))
    assert ",72," in style                  # font size
    assert "&H00112233" in style            # active colour
    assert style.endswith(",5,40,40,0,1")   # alignment 5 (centre), margin_v 0


def test_format_ass_default_position_lower_third():
    ass = server._format_ass([[(0.0, 0.4, "hi")]])
    style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Karaoke,"))
    assert style.endswith(",2,40,40,90,1")  # alignment 2 (bottom), lifted MarginV 90
