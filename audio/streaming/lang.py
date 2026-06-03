"""Shared language registry + a lightweight automatic-language detector for chat
audio. ``SUPPORTED_LANGUAGES`` is the reference list of the dictation (STT) / TTS
language set; the dashboard's TS ``LANGUAGES`` const mirrors it by hand — keep
the two in sync when adding a language.
"""

from __future__ import annotations

import re

# Languages offered for chat dictation (STT) + TTS playback, as BCP-47 tags (the
# form the native Web Speech recognizer wants). Deepgram/Cartesia codes derive by
# stripping the region (see ``base_lang`` and the provider normalizers). Keep in
# sync with the dashboard ``LANGUAGES`` const.
SUPPORTED_LANGUAGES: list[tuple[str, str]] = [
    ("en-US", "English (US)"),
    ("en-GB", "English (UK)"),
    ("el-GR", "Greek"),
    ("de-DE", "German"),
    ("es-ES", "Spanish"),
    ("fr-FR", "French"),
    ("it-IT", "Italian"),
]


def base_lang(tag: str) -> str:
    """BCP-47 → base subtag, lowercased (``en-US`` → ``en``, ``el-GR`` → ``el``)."""
    return (tag or "").split("-", 1)[0].lower()


# High-frequency function words per Latin language we ship. A match-count scorer
# over these is enough to PICK A VOICE — it never has to be perfect: a multilingual
# Cartesia voice pronounces the text correctly regardless; this only steers the
# accent / native-voice choice. Greek is detected by script (below), not here.
_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "is", "are", "you", "of", "to", "for", "with", "this", "that", "have", "was", "not", "it"},
    "de": {"der", "die", "das", "und", "ist", "ich", "nicht", "mit", "ein", "eine", "sie", "auch", "wird", "haben"},
    "es": {"que", "de", "no", "es", "los", "las", "una", "por", "con", "para", "está", "muy", "pero", "como"},
    "fr": {"le", "les", "est", "une", "des", "que", "pour", "dans", "pas", "vous", "avec", "mais", "très", "cette"},
    "it": {"che", "di", "è", "una", "con", "per", "non", "sono", "gli", "questo", "anche", "più", "della", "ma"},
}

_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")  # Greek + Greek Extended
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_tts_language(text: str, *, default: str = "en") -> str:
    """Pick a base TTS language (``en`` / ``de`` / ``es`` / ``fr`` / ``it`` / ``el``)
    for voice selection.

    Greek by script (unambiguous); the five Latin languages by stopword frequency.
    Returns ``default`` when there's no clear signal. Heuristic on purpose — it only
    steers voice choice; explicit selection or a multilingual voice is the reliable
    path.
    """
    if not text:
        return default
    if _GREEK.search(text):
        return "el"
    words = _WORD.findall(text.lower())[:200]
    if not words:
        return default
    scores = {lang: sum(w in sw for w in words) for lang, sw in _STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default
