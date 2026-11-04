"""Document text on one line of a result: what a deck holds (names, text,
attribute values) is shown with its controls, line separators and bidi
marks as escapes, so a file cannot draw lines that pose as the tool's own."""

import re

_UNSAFE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u061c\\u200e\\u200f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069|]")
_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "|": "\\|", "\v": "\\n"}
NAME_CAP = 80


def escaped(text: str, cap: int | None = None) -> str:
    """``text`` with controls, separators and bidi marks as escapes, cut at
    ``cap`` with the count of what was left out."""
    text = _UNSAFE.sub(lambda m: _ESCAPES.get(m.group(), f"\\u{ord(m.group()):04x}"), str(text))
    if cap is not None and len(text) > cap:
        return f"{text[:cap]}… (+{len(text) - cap} characters)"
    return text
