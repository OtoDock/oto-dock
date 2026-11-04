"""A1 references inside Excel formula text: parse, format, and rewrite.

Formula text is split with openpyxl's tokenizer, but never rebuilt from its
tokens (it does not round-trip every whitespace): each token is located in
the original string and only the reference spans that change are replaced.
A reference operand is `[@][qualifier!]part[:[qualifier!]part]...` where a
part is a cell (`$B$7`), a whole column (`B`, in `B:D`) or a whole row (`7`,
in `7:9`). Anything else in an operand position (a defined name, a
structured table reference, an R1C1-looking name, a cell past XFD1048576)
is not a reference and is left alone by callers.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

MAX_ROW = 1_048_576
MAX_COL = 16_384

REF_ERROR = "#REF!"

_CELL_RE = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([0-9]{1,7})")
_COL_RE = re.compile(r"(\$?)([A-Za-z]{1,3})")
_ROW_RE = re.compile(r"(\$?)([0-9]{1,7})")
# An unquoted sheet name: a letter or underscore, then word characters or
# dots (Unicode-aware, so Greek names qualify); optionally a 3-D span.
_UNQUOTED_QUAL_RE = re.compile(r"([^\W\d][\w.]*)(?::([^\W\d][\w.]*))?!")
_PLAIN_NAME_RE = re.compile(r"[^\W\d][\w.]*")
_R1C1_RE = re.compile(r"[Rr]([0-9]*)[Cc]?([0-9]*)")


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letters(idx: int) -> str:
    out = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def needs_quotes(name: str) -> bool:
    """True when a sheet name must be quoted in a reference: anything but a
    plain identifier, and identifiers Excel would read as a cell (`A1`,
    `XFD7`), an R1C1 reference (`R2C3`, `R`, `C`) or a logical."""
    if not _PLAIN_NAME_RE.fullmatch(name):
        return True
    if name.upper() in ("TRUE", "FALSE"):
        return True
    m = _CELL_RE.fullmatch(name)
    if m and col_index(m.group(2)) <= MAX_COL:
        return True
    return bool(_R1C1_RE.fullmatch(name))


def quote_sheet(name: str) -> str:
    """The qualifier text for a sheet name, `!` excluded."""
    if needs_quotes(name):
        return "'" + name.replace("'", "''") + "'"
    return name


@dataclass(frozen=True)
class Part:
    """One endpoint of a reference. kind: "cell", "col" or "row"."""
    kind: str
    col: int = 0
    row: int = 0
    col_abs: bool = False
    row_abs: bool = False

    def text(self) -> str:
        c = ("$" if self.col_abs else "") + col_letters(self.col)
        r = ("$" if self.row_abs else "") + str(self.row)
        if self.kind == "col":
            return c
        if self.kind == "row":
            return r
        return c + r


@dataclass(frozen=True)
class Ref:
    """A parsed reference operand.

    `sheet` is the unquoted sheet name or None when unqualified; `sheet_to`
    is the second sheet of a 3-D span; `external` marks a `[n]` qualifier
    (another workbook) and `dead` a `#REF!` qualifier left by a deleted
    sheet. `qual` keeps the qualifier text as written, `!` included."""
    parts: tuple[Part, ...]
    qual: str = ""
    sheet: str | None = None
    sheet_to: str | None = None
    external: bool = False
    dead: bool = False
    at: bool = False

    @property
    def three_d(self) -> bool:
        return self.sheet_to is not None

    def text(self) -> str:
        body = ":".join(p.text() for p in self.parts)
        return ("@" if self.at else "") + self.qual + body

    def with_qualifier(self, sheet: str | None, sheet_to: str | None = None) -> "Ref":
        if sheet is None:
            return replace(self, qual="", sheet=None, sheet_to=None)
        qual = quote_sheet(sheet)
        if sheet_to is not None:
            span = sheet + ":" + sheet_to
            qual = ("'" + span.replace("'", "''") + "'") if (
                needs_quotes(sheet) or needs_quotes(sheet_to)) else span
        return replace(self, qual=qual + "!", sheet=sheet, sheet_to=sheet_to)


def _read_qualifier(
    text: str, i: int,
) -> tuple[str, str | None, str | None, bool, bool, int] | None:
    """(qualifier text incl. '!', sheet, sheet_to, external, dead, next index)
    for a qualifier starting at i, or None when there is none."""
    if text.startswith(REF_ERROR, i):
        return REF_ERROR, None, None, False, True, i + len(REF_ERROR)
    if i < len(text) and text[i] == "'":
        j = i + 1
        while j < len(text):
            if text[j] == "'":
                if j + 1 < len(text) and text[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        if j + 1 >= len(text) or text[j + 1] != "!":
            return None
        inner = text[i + 1:j].replace("''", "'")
        external = inner.startswith("[")
        sheet, sheet_to = inner, None
        if not external and ":" in inner:
            sheet, sheet_to = inner.split(":", 1)
        return text[i:j + 2], sheet, sheet_to, external, False, j + 2
    if i < len(text) and text[i] == "[":
        j = text.find("!", i)
        if j == -1:
            return None
        return text[i:j + 1], text[i:j], None, True, False, j + 1
    m = _UNQUOTED_QUAL_RE.match(text, i)
    if m is None:
        return None
    return m.group(0), m.group(1), m.group(2), False, False, m.end()


def _parse_part(text: str) -> Part | None:
    m = _CELL_RE.fullmatch(text)
    if m:
        col, row = col_index(m.group(2)), int(m.group(4))
        if col > MAX_COL or not 1 <= row <= MAX_ROW:
            return None
        return Part("cell", col, row, bool(m.group(1)), bool(m.group(3)))
    m = _COL_RE.fullmatch(text)
    if m:
        col = col_index(m.group(2))
        return Part("col", col=col, col_abs=bool(m.group(1))) if col <= MAX_COL else None
    m = _ROW_RE.fullmatch(text)
    if m:
        row = int(m.group(2))
        return Part("row", row=row, row_abs=bool(m.group(1))) if 1 <= row <= MAX_ROW else None
    return None


def parse_ref(text: str) -> Ref | None:
    """Parse one operand, or None when it is not an A1 reference."""
    at = text.startswith("@")
    i = 1 if at else 0
    qual = _read_qualifier(text, i)
    q_text, sheet, sheet_to, external, dead = "", None, None, False, False
    if qual is not None:
        q_text, sheet, sheet_to, external, dead, i = qual
    pieces = text[i:].split(":")
    parts: list[Part] = []
    for n, piece in enumerate(pieces):
        if n and piece.startswith(q_text) and q_text:
            piece = piece[len(q_text):]
        part = _parse_part(piece)
        if part is None:
            return None
        parts.append(part)
    kinds = {p.kind for p in parts}
    if len(kinds) > 1 or (kinds != {"cell"} and len(parts) < 2):
        return None
    return Ref(tuple(parts), q_text, sheet, sheet_to, external, dead, at)


@dataclass
class Located:
    """A tokenizer token and where it sits in the formula text."""
    start: int
    end: int
    value: str
    type: str
    subtype: str


def locate_tokens(formula: str) -> list[Located] | None:
    """The tokens of `formula` (which starts with '=') with their offsets in
    the original text, or None when the tokenizer refuses the formula or a
    token cannot be found where it should be."""
    from openpyxl.formula.tokenizer import Tokenizer, TokenizerError

    try:
        items = Tokenizer(formula).items
    except (TokenizerError, ValueError, IndexError):
        return None
    out: list[Located] = []
    pos = 1
    for tok in items:
        # The tokenizer splits some whitespace oddly ("\r\n" comes back as a
        # space token and a "\r" operand; a tab sticks to the next operand),
        # so whitespace is skipped in the text and trimmed off token values.
        value = tok.value.strip() if tok.type != "OPERAND" or tok.subtype != "TEXT" else tok.value
        if tok.type == "WHITE-SPACE" or not value:
            while pos < len(formula) and formula[pos].isspace():
                pos += 1
            continue
        while not formula.startswith(value, pos) and pos < len(formula) and formula[pos].isspace():
            pos += 1
        if not formula.startswith(value, pos):
            return None
        out.extend(_split_range_operator(
            Located(pos, pos + len(value), value, tok.type, tok.subtype)))
        pos += len(value)
    return out


def _last_colon(text: str) -> int:
    """Index of the last ':' outside quoted sheet names, or -1."""
    quoted, found = False, -1
    for i, ch in enumerate(text):
        if ch == "'":
            quoted = not quoted
        elif ch == ":" and not quoted:
            found = i
    return found


def _split_range_operator(tok: Located) -> list[Located]:
    """The tokenizer fuses the range operator into its neighbours:
    `A2:INDEX(` and `:XLOOKUP(` come back as one function token and `:A6`
    (after a call) as one operand. Split them so the reference and the
    function are each seen as one."""
    if tok.type == "FUNC" and tok.subtype == "OPEN":
        k = _last_colon(tok.value[:-1])
        if k >= 0:
            s = tok.start
            head = [Located(s, s + k, tok.value[:k], "OPERAND", "RANGE")] if k else []
            return head + [
                Located(s + k, s + k + 1, ":", "OPERATOR-INFIX", ""),
                Located(s + k + 1, tok.end, tok.value[k + 1:], "FUNC", "OPEN"),
            ]
    if tok.type == "OPERAND" and tok.subtype == "RANGE" and tok.value.startswith(":") \
            and len(tok.value) > 1:
        return [
            Located(tok.start, tok.start + 1, ":", "OPERATOR-INFIX", ""),
            Located(tok.start + 1, tok.end, tok.value[1:], "OPERAND", "RANGE"),
        ]
    return [tok]


RefRewrite = Callable[[Ref], str | None]


def rewrite_refs(formula: str, fn: RefRewrite, *, bare: bool = False,
                 other: Callable[[str], str | None] | None = None) -> str | None:
    """`formula` with every reference operand passed through `fn` (new text,
    or None to keep it). `bare` text has no leading '=' (validation,
    conditional-format, name and chart formulas). `other` sees the operands
    in reference position that are not A1 references (names, structured
    table references). Returns None when the tokenizer refuses the text;
    the input object itself when nothing changed. An ERROR token `#REF!`
    glued to the next operand (`#REF!A1`, what a deleted sheet leaves) is
    one dead reference and is passed as such."""
    text = "=" + formula if bare else formula
    tokens = locate_tokens(text)
    if tokens is None:
        return None
    pieces: list[str] = []
    last = 0
    changed = False
    skip = -1
    for n, tok in enumerate(tokens):
        if n <= skip or tok.type != "OPERAND":
            continue
        start, end, value = tok.start, tok.end, tok.value
        if tok.subtype == "ERROR" and value == REF_ERROR:
            nxt = tokens[n + 1] if n + 1 < len(tokens) else None
            if nxt is None or nxt.type != "OPERAND" or nxt.start != end:
                continue
            end, value = nxt.end, value + nxt.value
            skip = n + 1
        elif tok.subtype != "RANGE":
            continue
        ref = parse_ref(value)
        if ref is None:
            new = other(value) if other is not None else None
        else:
            new = fn(ref)
        if new is None or new == value:
            continue
        pieces.append(text[last:start])
        pieces.append(new)
        last = end
        changed = True
    if not changed:
        return formula
    pieces.append(text[last:])
    out = "".join(pieces)
    return out[1:] if bare else out


def function_names(formula: str) -> list[str] | None:
    """Every function name called in `formula`, as written (prefixes kept),
    or None when the tokenizer refuses the text."""
    tokens = locate_tokens(formula if formula.startswith("=") else "=" + formula)
    if tokens is None:
        return None
    return [t.value[:-1] for t in tokens if t.type == "FUNC" and t.subtype == "OPEN"]
