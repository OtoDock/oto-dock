"""split_sentences contract tests (pure — no network)."""

from audio.streaming.text_chunks import split_sentences


def test_empty_and_whitespace():
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


def test_basic_split_keeps_terminator():
    assert split_sentences("Hello there. How are you? Great!") == [
        "Hello there.", "How are you?", "Great!",
    ]


def test_ellipsis_is_a_terminator():
    assert split_sentences("Well… maybe. Sure.") == ["Well…", "maybe.", "Sure."]


def test_no_split_on_decimals():
    # the dot in 3.14 isn't followed by whitespace → not a boundary
    assert split_sentences("Pi is 3.14 today.") == ["Pi is 3.14 today."]


def test_clause_punctuation_is_not_a_boundary():
    # semicolons / colons join clauses — not sentence ends (English-safe; a Greek
    # question typed with ";" likewise stays one chunk, synthesized correctly)
    assert split_sentences("one; two: three.") == ["one; two: three."]


def test_non_latin_splits_on_period():
    # Greek (and other scripts) split on the ASCII period like everything else
    assert split_sentences("Καλημέρα. Τι κάνεις.") == ["Καλημέρα.", "Τι κάνεις."]


def test_newlines_split_paragraphs():
    assert split_sentences("Line one\nLine two") == ["Line one", "Line two"]


def test_long_sentence_soft_split_at_space():
    long = ("word " * 80).strip()  # ~399 chars, no terminator
    out = split_sentences(long, max_len=240)
    assert len(out) >= 2
    assert all(len(c) <= 240 for c in out)
    assert "".join(out).replace(" ", "") == long.replace(" ", "")


def test_joined_chunks_reconstruct_transcript():
    text = "First sentence. Second one!"
    assert " ".join(split_sentences(text)) == text
