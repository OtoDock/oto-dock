"""A template's ``{{ key }}`` tokens, as write_docx and write_pptx fill
them: the data flattened to keys (``client.name``) and the token pattern
for a pair of delimiters."""

from __future__ import annotations

import re
from typing import Any

from results import Code, OpError


def flatten_data(data: dict, prefix: str = "") -> dict[str, str]:
    """``data`` as one text value per key, nested objects named ``a.b``."""
    out: dict[str, str] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten_data(value, f"{name}."))
        elif isinstance(value, list):
            raise OpError(Code.BAD_VALUE, f"data value for '{name}' is a list",
                          hint="Give one value per key; for rows of values, write a table with add_table.")
        elif value is None:
            out[name] = ""
        elif isinstance(value, bool):
            out[name] = "true" if value else "false"
        else:
            out[name] = str(value)
    return out


def token_pattern(delimiters: Any) -> re.Pattern[str]:
    """Opening delimiter, optional spaces, a key on one line, optional
    spaces, closing delimiter; ValueError for delimiters that cannot work."""
    if not (isinstance(delimiters, (tuple, list)) and len(delimiters) == 2
            and all(isinstance(d, str) for d in delimiters)):
        raise ValueError('The delimiters must be two strings, such as "{{" and "}}".')
    opening, closing = delimiters
    if not opening or not closing or opening == closing or "\n" in opening + closing:
        raise ValueError("The delimiters must be two different, non-empty strings without a "
                         'newline, such as "{{" and "}}".')
    o, c = re.escape(opening), re.escape(closing)
    return re.compile(f"{o} *(?P<key>(?:(?!{o}|{c})[^\\n])+?) *{c}")
