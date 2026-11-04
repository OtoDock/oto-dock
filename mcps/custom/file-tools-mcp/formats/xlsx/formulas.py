"""Formula text as Excel stores it in the file.

Excel writes functions added after Excel 2007 with a `_xlfn.` prefix (and
FILTER and SORT with `_xlfn._xlws.`), LAMBDA and LET parameters with
`_xlpm.`, a spill reference `A1#` as `_xlfn.ANCHORARRAY(A1)`, the
implicit-intersection `@` as `_xlfn.SINGLE()`, and `Table1[@Col]` as
`Table1[[#This Row],[Col]]`. openpyxl writes formula text exactly as given,
so a bare `XLOOKUP(` opens in Excel as `#NAME?`. Sheet names with spaces or
punctuation must be quoted in a reference. This module rewrites what an
operation writes into the stored form and reports what it cannot fix.
"""

import re
from dataclasses import dataclass, field

from formats.xlsx.refs import (
    function_names,
    locate_tokens,
    needs_quotes,
    parse_ref,
    quote_sheet,
)

# Functions Excel stores with the _xlfn. prefix: those added after Excel 2007
# ([MS-XLSX] 2.2.2, the future-function-list, and the newer functions
# Microsoft has documented since).
FUTURE_FUNCTIONS = frozenset("""
ACOT ACOTH AGGREGATE ANCHORARRAY ARABIC ARRAYTOTEXT BASE BETA.DIST BETA.INV
BINOM.DIST BINOM.DIST.RANGE BINOM.INV BITAND BITLSHIFT BITOR BITRSHIFT BITXOR
BYCOL BYROW CEILING.MATH CEILING.PRECISE CHISQ.DIST CHISQ.DIST.RT CHISQ.INV
CHISQ.INV.RT CHISQ.TEST CHOOSECOLS CHOOSEROWS COMBINA CONCAT CONFIDENCE.NORM
CONFIDENCE.T COT COTH COVARIANCE.P COVARIANCE.S CSC CSCH DAYS DECIMAL
DETECTLANGUAGE DROP ENCODEURL ERF.PRECISE ERFC.PRECISE EXPAND
EXPON.DIST F.DIST F.DIST.RT F.INV F.INV.RT F.TEST FIELDVALUE FILTERXML
FLOOR.MATH FLOOR.PRECISE FORECAST.ETS FORECAST.ETS.CONFINT
FORECAST.ETS.SEASONALITY FORECAST.ETS.STAT FORECAST.LINEAR FORMULATEXT GAMMA
GAMMA.DIST GAMMA.INV GAMMALN.PRECISE GAUSS GROUPBY HSTACK HYPGEOM.DIST IFNA IFS
IMAGE IMCOSH IMCOT IMCSC IMCSCH IMSEC IMSECH IMSINH IMTAN ISFORMULA ISOMITTED ISOWEEKNUM LAMBDA LET LOGNORM.DIST LOGNORM.INV MAKEARRAY MAP MAXIFS
MINIFS MODE.MULT MODE.SNGL MUNIT NEGBINOM.DIST NORM.DIST
NORM.INV NORM.S.DIST NORM.S.INV NUMBERVALUE PDURATION PERCENTILE.EXC
PERCENTILE.INC PERCENTOF PERCENTRANK.EXC PERCENTRANK.INC PERMUTATIONA PHI
PIVOTBY POISSON.DIST QUARTILE.EXC QUARTILE.INC RANDARRAY RANK.AVG RANK.EQ
REDUCE REGEXEXTRACT REGEXREPLACE REGEXTEST RRI SCAN SEC SECH SEQUENCE SHEET
SHEETS SINGLE SKEW.P SORTBY STDEV.P STDEV.S STOCKHISTORY SWITCH T.DIST
T.DIST.2T T.DIST.RT T.INV T.INV.2T T.TEST TAKE TEXTAFTER TEXTBEFORE TEXTJOIN
TEXTSPLIT TOCOL TOROW TRANSLATE TRIMRANGE UNICHAR UNICODE UNIQUE VALUETOTEXT
VAR.P VAR.S VSTACK WEBSERVICE WEIBULL.DIST WRAPCOLS WRAPROWS
XLOOKUP XMATCH XOR Z.TEST
""".split())

# Functions added after Excel 2007 that Excel stores without the prefix
# (the same list names them bare); a prefix given to one is dropped, since
# no engine reads it there.
BARE_FUNCTIONS = frozenset({"ECMA.CEILING", "ISO.CEILING", "NETWORKDAYS.INTL", "WORKDAY.INTL"})

# Worksheet-only dynamic-array functions, stored as _xlfn._xlws.NAME.
WORKSHEET_FUNCTIONS = frozenset({"FILTER", "SORT"})

# Functions whose result is an array that spills over neighbouring cells.
SPILLING = frozenset("""
FILTER SORT SORTBY UNIQUE SEQUENCE RANDARRAY VSTACK HSTACK TOCOL TOROW
WRAPROWS WRAPCOLS TAKE DROP CHOOSEROWS CHOOSECOLS EXPAND TEXTSPLIT MAP BYROW
BYCOL SCAN MAKEARRAY GROUPBY PIVOTBY ANCHORARRAY
""".split())

# Functions that reduce an array argument to one value: a spilling call
# inside one of these does not spill.
_REDUCERS = frozenset("""
SUM SUMPRODUCT SUMIF SUMIFS COUNT COUNTA COUNTBLANK COUNTIF COUNTIFS ROWS
COLUMNS INDEX MAX MAXA MIN MINA MAXIFS MINIFS AVERAGE AVERAGEA AVERAGEIF
AVERAGEIFS MEDIAN MODE MODE.SNGL STDEV STDEV.S STDEV.P STDEVA VAR VAR.S VAR.P
VARA PRODUCT AND OR XOR CONCAT TEXTJOIN ARRAYTOTEXT LARGE SMALL PERCENTILE
PERCENTILE.INC PERCENTILE.EXC QUARTILE QUARTILE.INC QUARTILE.EXC MATCH
VLOOKUP HLOOKUP LOOKUP NPV IRR SINGLE ISREF AREAS COUNTUNIQUE REDUCE
""".split())

_PREFIXES = ("_XLFN._XLWS.", "_XLFN.", "_XLWS.", "_XLETA.", "_XLUDF.", "COM.MICROSOFT.")

# Functions that reach the network or run a program when the workbook is
# calculated (by Excel when the person opens it, or by a recalculation
# engine): fetching a URL can carry cell values out in its query string.
# Lookups such as INDIRECT and DGET read the workbook only and stay allowed.
UNSAFE_FUNCTIONS = frozenset({
    "WEBSERVICE", "IMAGE", "STOCKHISTORY", "TRANSLATE", "DETECTLANGUAGE", "COPILOT",
    "RTD", "CALL", "REGISTER", "REGISTER.ID", "EXEC", "DDE", "PY", "SQL.REQUEST",
})
DDE_LINK = "DDE"
# What may read outside the workbook when it is calculated: the unsafe
# functions, FILTERXML (its text is a document that may name others), and
# INDIRECT (its target is computed, and LibreOffice resolves a target text
# naming another file).
OUTSIDE_FUNCTIONS = UNSAFE_FUNCTIONS | {"FILTERXML", "INDIRECT"}
# Those LibreOffice was shown to leave alone under the recalculation
# profile: nothing fetched over the network, no file or FIFO opened, by a
# literal or a computed target (the office test
# test_xlsx_recalc.py::test_the_engine_reaches_nothing_outside). A workbook
# calling any other is not recalculated; WEBSERVICE and DDE values are
# still never trusted (the engine answers #N/A where Excel would fetch).
ENGINE_CONTAINED = frozenset({"WEBSERVICE", "FILTERXML", "DDE", "INDIRECT"})
OTHER_WORKBOOK = "a reference to another file"
# A reference to another file, outside string literals: a book in brackets
# (Excel's stored [1]Sheet1!A1, a typed [book.xlsx]Sheet1!A1, an external
# name [1]!Rate), a quoted qualifier no sheet name could be (sheet names
# hold none of [ ] / \ ? *: '/data/[book.xlsx]Sheet1'!A1,
# 'file:///data/x.xlsx'!A1), and LibreOffice's 'file:///x.ods'#$Sheet1.A1.
_BOOK_IN_BRACKETS_RE = re.compile(r"(?<![\w.\]])\[[^\[\]]+\](?:[^\W\d][\w.]*)?!")
_QUOTED_QUALIFIER_RE = re.compile(r"'((?:[^']|'')*)'(!|#\$)")
NOT_IN_SHEET_NAMES = frozenset("[]/\\?*")

_SPILL_REF_RE = re.compile(
    r"((?:'(?:[^']|'')+'|[^\W\d][\w.]*)!)?(\$?[A-Za-z]{1,3}\$?[0-9]{1,7})#")
_THIS_ROW_RE = re.compile(r"([^\W\d][\w.]*)?\[@(\[(?:[^\[\]']|'.)+\]|[^\[\]]+)\]")


def bare_name(func: str) -> str:
    """A function name without the storage prefixes, upper-cased."""
    name = func.upper()
    stripped = True
    while stripped:
        stripped = False
        for prefix in _PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix):]
                stripped = True
    return name


@dataclass
class Normalized:
    """The stored text with notes and warnings; `errors` are reasons the
    formula cannot be stored at all (the op fails)."""
    text: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _outside_strings(text: str) -> list[tuple[int, int]]:
    """Spans of `text` outside string literals and quoted sheet names."""
    spans = []
    i, start = 0, 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            spans.append((start, i))
            j = i + 1
            while j < len(text):
                if text[j] == ch:
                    if j + 1 < len(text) and text[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            start = i
            continue
        i += 1
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if a < b]


def _sub_outside(text: str, pattern: re.Pattern, repl) -> str:
    """re.sub applied only outside string literals (quoted names kept whole,
    because a spill reference may be quoted: 'My sheet'!A1#)."""
    out, last = [], 0
    for start, end in _string_spans(text):
        out.append(pattern.sub(repl, text[last:start]))
        out.append(text[start:end])
        last = end
    out.append(pattern.sub(repl, text[last:]))
    return "".join(out)


def _string_spans(text: str) -> list[tuple[int, int]]:
    """Spans of string literals ("...") in formula text."""
    spans, i = [], 0
    in_quote = False
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_quote:
            j = i + 1
            while j < len(text):
                if text[j] == "'":
                    if j + 1 < len(text) and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            while j < len(text):
                if text[j] == '"':
                    if j + 1 < len(text) and text[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            spans.append((i, min(j + 1, len(text))))
            i = j + 1
            continue
        i += 1
    return spans


def _anchorarray(m: re.Match) -> str:
    return f"_xlfn.ANCHORARRAY({m.group(1) or ''}{m.group(2)})"


def _this_row(text: str, table: str | None, errors: list[str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or table
        col = m.group(2)
        col = col if col.startswith("[") else f"[{col}]"
        if not name:
            errors.append(
                f"[@{col[1:-1]}] works only inside a table, and this cell is in none. "
                f"Write it as TableName[[#This Row],{col}]."
            )
            return m.group(0)
        return f"{name}[[#This Row],{col}]"

    return _sub_outside(text, _THIS_ROW_RE, repl)


def _single(text: str) -> str:
    """`@X` → `_xlfn.SINGLE(X)` for a reference, name or function call X."""
    tokens = locate_tokens(text)
    if tokens is None:
        return text
    edits = []
    for n, tok in enumerate(tokens):
        if tok.type == "OPERAND" and tok.subtype == "RANGE" and tok.value.startswith("@"):
            edits.append((tok.start, tok.end, f"_xlfn.SINGLE({tok.value[1:]})"))
        elif tok.type == "FUNC" and tok.subtype == "OPEN" and tok.value.startswith("@"):
            depth, end = 0, None
            for later in tokens[n:]:
                if later.type == "FUNC":
                    depth += 1 if later.subtype == "OPEN" else -1
                    if depth == 0:
                        end = later.end
                        break
            if end is not None:
                edits.append((tok.start, end, f"_xlfn.SINGLE({text[tok.start + 1:end]})"))
    for start, end, new in reversed(edits):
        text = text[:start] + new + text[end:]
    return text


def quote_sheet_names(text: str, sheets: list[str]) -> tuple[str, list[str]]:
    """Quote an unquoted sheet name that needs quotes where it stands before
    `!` (the tokenizer would otherwise split `Sales Data!A1` at the space).
    Returns the text and the names it quoted."""
    targets = sorted((s for s in sheets if needs_quotes(s)), key=len, reverse=True)
    if not targets:
        return text, []
    folded = [(s, s.casefold()) for s in targets]
    quoted: list[str] = []
    out, i = [], 0
    spans = _outside_strings(text)
    for start, end in spans:
        out.append(text[i:start])
        j = start
        while j < end:
            prev = text[j - 1] if j else ""
            hit = None
            if not (prev.isalnum() or prev in "_.'"):
                low = text[j:end].casefold()
                for name, f in folded:
                    if low.startswith(f) and text[j + len(name):j + len(name) + 1] == "!":
                        hit = name
                        break
            if hit is not None:
                out.append(quote_sheet(hit))
                quoted.append(hit)
                j += len(hit)
            else:
                out.append(text[j])
                j += 1
        i = end
    out.append(text[i:])
    return "".join(out), quoted


def _future_prefix(text: str) -> str:
    tokens = locate_tokens(text)
    if tokens is None:
        return text
    edits: list[tuple[int, int, str]] = []  # (where, characters removed, text put in)
    for tok in tokens:
        if tok.type != "FUNC" or tok.subtype != "OPEN":
            continue
        name = tok.value[:-1]
        upper = name.upper()
        if upper.startswith("_XLFN.") and upper[6:] in WORKSHEET_FUNCTIONS:
            # A worksheet-only function needs both prefixes.
            edits.append((tok.start + 6, 0, "_xlws."))
            continue
        if upper.startswith("_XLFN.") and upper[6:] in BARE_FUNCTIONS:
            edits.append((tok.start, 6, ""))
            continue
        if upper.startswith(("_XLFN.", "_XLWS.", "_XLUDF.")):
            continue
        if upper in WORKSHEET_FUNCTIONS:
            edits.append((tok.start, 0, "_xlfn._xlws."))
        elif upper in FUTURE_FUNCTIONS:
            edits.append((tok.start, 0, "_xlfn."))
    for pos, removed, prefix in reversed(edits):
        text = text[:pos] + prefix + text[pos + removed:]
    return text


def _lambda_params(text: str) -> str:
    """Prefix LET and LAMBDA parameter names with _xlpm. wherever they are
    used inside the call that declares them."""
    tokens = locate_tokens(text)
    if tokens is None:
        return text
    frames = []
    stack = []
    for n, tok in enumerate(tokens):
        if tok.type == "FUNC" and tok.subtype == "OPEN":
            stack.append({"name": bare_name(tok.value[:-1]), "open": n, "args": [[]]})
        elif tok.type == "FUNC" and tok.subtype == "CLOSE" and stack:
            frame = stack.pop()
            frame["close"] = n
            frames.append(frame)
        elif tok.type == "SEP" and tok.value == "," and stack:
            stack[-1]["args"].append([])
        elif stack:
            stack[-1]["args"][-1].append(n)
    edits: set[int] = set()
    for frame in frames:
        if frame["name"] not in ("LET", "LAMBDA"):
            continue
        args = frame["args"][:-1]
        if frame["name"] == "LET":
            args = args[0::2]
        params = set()
        for arg in args:
            if len(arg) == 1:
                tok = tokens[arg[0]]
                if (tok.type == "OPERAND" and tok.subtype == "RANGE"
                        and re.fullmatch(r"[^\W\d][\w.]*", tok.value)
                        and not tok.value.casefold().startswith("_xlpm.")
                        and parse_ref(tok.value) is None):
                    params.add(tok.value.casefold())
        for n in range(frame["open"] + 1, frame["close"]):
            tok = tokens[n]
            if tok.type == "OPERAND" and tok.subtype == "RANGE" and tok.value.casefold() in params:
                edits.add(tok.start)
    for pos in sorted(edits, reverse=True):
        text = text[:pos] + "_xlpm." + text[pos:]
    return text


def spills(text: str) -> str | None:
    """The spilling function whose result reaches the cell, or None."""
    tokens = locate_tokens(text)
    if tokens is None:
        return None
    stack: list[str] = []
    for tok in tokens:
        if tok.type == "FUNC" and tok.subtype == "OPEN":
            name = bare_name(tok.value[:-1])
            if name in SPILLING and not any(f in _REDUCERS for f in stack):
                return name
            stack.append(name)
        elif tok.type == "FUNC" and tok.subtype == "CLOSE" and stack:
            stack.pop()
    return None


_STORAGE_PREFIX_RE = re.compile(r"_XLFN\.|_XLWS\.|_XLUDF\.|COM\.MICROSOFT\.")
# A function passed as a value (an eta-reduced LAMBDA) is stored with this
# prefix and runs when the call it is handed to runs.
_ETA_PREFIX = "_XLETA."


def blocked_functions(text: str, blocked: frozenset[str]) -> list[str]:
    """The blocked functions `text` calls, matched on whole function names
    with the storage prefixes removed and case ignored, so
    `_xlfn.WEBSERVICE(` and `webservice (` are caught and a sheet named
    RTDATA is not. Function tokens are read first; every `NAME (` outside
    string literals and quoted sheet names is matched too, because the
    tokenizer reads a call with a space before its bracket (which
    LibreOffice accepts) as a name followed by a parenthesis."""
    found: set[str] = set()
    for name in function_names(text) or []:
        bare = bare_name(name)
        if bare in blocked:
            found.add(bare)
    for start, end in _outside_strings(text):
        chunk = _STORAGE_PREFIX_RE.sub("", text[start:end].upper())
        for name in blocked:
            if re.search(rf"(?<![\w.]){re.escape(name)}\s*\(", chunk):
                found.add(name)
    return sorted(found)


def unsafe_functions(text: str) -> list[str]:
    """The functions in `text` that reach the network or run a program:
    calls in any spelling (see blocked_functions), a function handed to a
    LAMBDA helper by name (`_xlfn._xleta.WEBSERVICE`), and a DDE link
    (`app|topic!item`, the only use of `|` in a formula)."""
    found = set(blocked_functions(text, UNSAFE_FUNCTIONS))
    for start, end in _outside_strings(text):
        chunk = text[start:end].upper()
        if "|" in chunk:
            found.add(DDE_LINK)
        for name in UNSAFE_FUNCTIONS:
            if re.search(rf"{re.escape(_ETA_PREFIX)}{re.escape(name)}(?![\w.])", chunk):
                found.add(name)
    return sorted(found)


def names_another_file(text: str) -> bool:
    """True when ``text`` holds a reference to another file in any of the
    shapes Excel or LibreOffice read (a false match only withholds a
    recalculation)."""
    plain, last = [], 0
    for start, end in _string_spans(text):
        plain.append(text[last:start])
        plain.append('""')
        last = end
    plain.append(text[last:])
    code = "".join(plain)
    if _BOOK_IN_BRACKETS_RE.search(code):
        return True
    for m in _QUOTED_QUALIFIER_RE.finditer(code):
        if m.group(2) == "#$" or NOT_IN_SHEET_NAMES & set(m.group(1)):
            return True
    return False


def reads_outside(text: str) -> list[str]:
    """What ``text`` reads from outside the workbook: the OUTSIDE_FUNCTIONS
    it calls, a DDE link, a reference to another file."""
    found = set(blocked_functions(text, OUTSIDE_FUNCTIONS)) | set(unsafe_functions(text))
    if names_another_file(text):
        found.add(OTHER_WORKBOOK)
    return sorted(found)


def parens_balanced(text: str) -> bool:
    """Parentheses balance outside string literals and quoted names."""
    depth = 0
    for start, end in _outside_strings(text):
        for ch in text[start:end]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return False
    return depth == 0


def missing_sheets(text: str, sheets: list[str]) -> list[str]:
    """Sheet names `text` qualifies a reference with that the workbook does
    not have (external and 3-D references checked per end sheet)."""
    tokens = locate_tokens(text if text.startswith("=") else "=" + text)
    if tokens is None:
        return []
    known = {s.casefold() for s in sheets}
    out = []
    for tok in tokens:
        if tok.type != "OPERAND" or tok.subtype != "RANGE":
            continue
        ref = parse_ref(tok.value)
        if ref is None or ref.external or ref.dead or ref.sheet is None:
            continue
        for name in (ref.sheet, ref.sheet_to):
            if name is not None and name.casefold() not in known and name not in out:
                out.append(name)
    return out


def normalize(formula: str, sheets: list[str], *, table: str | None = None,
              bare: bool = False) -> Normalized:
    """`formula` in the form Excel stores, with notes and warnings.
    `sheets` are the workbook's sheet names; `table` the table holding the
    cell, for a bare `[@Col]`; `bare` text has no leading '='."""
    text = "=" + formula if bare else formula
    result = Normalized(text)
    if not text.startswith("="):
        return Normalized(formula)
    text, quoted = quote_sheet_names(text, sheets)
    for name in quoted:
        result.notes.append(f"the sheet name '{name}' was quoted in the formula")
    text = _sub_outside(text, _SPILL_REF_RE, _anchorarray)
    text = _this_row(text, table, result.errors)
    text = _single(text)
    text = _future_prefix(text)
    text = _lambda_params(text)
    name = spills(text)
    if name is not None:
        result.notes.append(f"{name} spills: Excel shows its results in the cells below and to the right")
    result.text = text[1:] if bare else text
    return result
