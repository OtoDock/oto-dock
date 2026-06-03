"""Sentence-level text chunking for streaming TTS.

Splitting prose into complete sentences lets a streaming TTS engine begin audio
as soon as the first sentence is available (low time-to-first-audio) while still
seeing whole sentences (natural intonation — question/exclamation pitch depends
on the terminal punctuation). Used by the proxy's chat sound icon; the phone
pipeline paces its own full-text TTS delivery.
"""

from __future__ import annotations

import re

# Sentence terminators we split AFTER (keeping the punctuation with the sentence):
# ASCII ``.  !  ?`` plus the ellipsis ``…`` (U+2026). These cover the primary
# sentence ends of every language we ship (en / de / es / fr / it / el — all use
# ``. ! ?``). We deliberately do NOT split on ``;`` / ``:`` / ``·``: in English
# they join clauses rather than end sentences, and a Greek question typed with a
# plain ";" just yields one slightly larger chunk (still synthesized correctly).
# The trailing ``(\s+|$)`` requires whitespace/end after the terminator, so
# decimals ("3.14") and URLs are not split.
_SENTENCE_END = re.compile(r"([.!?…]+)(\s+|$)")


def split_sentences(text: str, *, max_len: int = 240) -> list[str]:
    """Split ``text`` into sentence-level chunks (terminator kept).

    Paragraph (newline) boundaries always split. A sentence longer than
    ``max_len`` is soft-split at the last space before the limit so one runaway
    sentence can't delay first audio. Whitespace-only input → ``[]``. Joining the
    result with spaces reconstructs the transcript (what Cartesia expects).
    """
    text = (text or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        start = 0
        for m in _SENTENCE_END.finditer(para):
            sent = para[start:m.end(1)].strip()  # include the punctuation, drop trailing space
            if sent:
                chunks.append(sent)
            start = m.end()
        tail = para[start:].strip()
        if tail:
            chunks.append(tail)

    # Soft-split over-long chunks at a word boundary (hard-cut only if no space).
    out: list[str] = []
    for c in chunks:
        while len(c) > max_len:
            cut = c.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len
            head = c[:cut].strip()
            if head:
                out.append(head)
            c = c[cut:].strip()
        if c:
            out.append(c)
    return out
