"""A number as Excel shows it under a number format, close enough to tell
whether it fits its column.

Covers what workbooks carry in practice: fixed decimals, thousands
separators and scaling commas, percentages, scientific notation, currency
and other literals (quoted, escaped, ``[$€-407]``), the positive, negative
and zero sections, and dates and times. Anything else (fractions, text
sections, conditions on the value) answers None, and the caller skips the
cell.
"""

import datetime
import re

_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_NUMBER_RE = re.compile(r"[0#?,.]*[0#?][0#?,.]*")
_DATE_TOKEN_RE = re.compile(r"yyyy|yy|mmmmm|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s|AM/PM|am/pm|A/P|a/p|\.0+",
                            re.IGNORECASE)


def _sections(code: str) -> list[str]:
    out, current, quoted = [], [], False
    i = 0
    while i < len(code):
        ch = code[i]
        if ch == '"':
            quoted = not quoted
        elif ch == "\\" and not quoted and i + 1 < len(code):
            current.append(code[i:i + 2])
            i += 2
            continue
        elif ch == ";" and not quoted:
            out.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    out.append("".join(current))
    return out


# Literal characters that would read as part of the number pattern (a
# quoted "0" or an escaped \%) stand in for themselves until the end.
_HIDE = str.maketrans({"0": "\x01", "#": "\x02", "%": "\x03", "?": "\x04"})
_SHOW = str.maketrans({"\x01": "0", "\x02": "#", "\x03": "%", "\x04": "?"})


def _literals(section: str) -> tuple[str, bool] | None:
    """The section with quotes, escapes, padding and brackets turned into
    plain text, the number pattern kept; and whether it shows a percent.
    None for what this module does not render."""
    out = []
    i = 0
    while i < len(section):
        ch = section[i]
        if ch == '"':
            j = section.find('"', i + 1)
            if j == -1:
                return None
            out.append(section[i + 1:j].translate(_HIDE))
            i = j + 1
            continue
        if ch == "\\" and i + 1 < len(section):
            out.append(section[i + 1].translate(_HIDE))
            i += 2
            continue
        if ch == "_" and i + 1 < len(section):
            out.append(" ")
            i += 2
            continue
        if ch == "*" and i + 1 < len(section):
            i += 2
            continue
        if ch == "[":
            j = section.find("]", i)
            if j == -1:
                return None
            inner = section[i + 1:j]
            if inner.startswith("$"):
                out.append(inner[1:].split("-", 1)[0])
            elif inner[:1] in "<>=":
                return None
            i = j + 1
            continue
        if ch in "/@":
            return None
        out.append(ch)
        i += 1
    text = "".join(out)
    return text, "%" in text


def _grouped(digits: str) -> str:
    head = len(digits) % 3 or 3
    return ",".join([digits[:head]] + [digits[i:i + 3] for i in range(head, len(digits), 3)])


def _number(value: float, pattern: str) -> str:
    integer, point, decimals = pattern.partition(".")
    scale = len(integer) - len(integer.rstrip(","))
    integer = integer.rstrip(",")
    value /= 1000 ** scale
    places = sum(1 for ch in decimals if ch in "0#?")
    whole, _, frac = f"{abs(value):.{places}f}".partition(".")
    least = integer.count("0")
    whole = "" if whole == "0" and not least else whole.rjust(least, "0")
    if "," in integer and whole:
        whole = _grouped(whole)
    frac = frac.rstrip("0").ljust(decimals.count("0"), "0")
    return whole + point + frac


def _scientific(value: float, pattern: str) -> str:
    mantissa, _, _exp = pattern.upper().partition("E")
    places = len(mantissa.partition(".")[2])
    text = f"{abs(value):.{places}E}"
    head, _, power = text.partition("E")
    sign = "-" if power.startswith("-") else "+"
    return f"{head}E{sign}{power.lstrip('+-').rjust(2, '0')}"


def is_date(code: str) -> bool:
    """A date or time format (Excel's own test: date letters outside quotes
    and brackets)."""
    from openpyxl.styles.numbers import is_date_format

    return is_date_format(code)


def _date(value: float, section: str) -> str | None:
    from openpyxl.utils.datetime import from_excel

    try:
        moment = from_excel(value)
    except (ValueError, OverflowError):
        return None
    if isinstance(moment, datetime.timedelta):
        moment = datetime.datetime(1899, 12, 30) + moment
    if isinstance(moment, datetime.time):
        moment = datetime.datetime.combine(datetime.date(1899, 12, 30), moment)
    plain = _BRACKET_RE.sub("", section).replace('"', "").replace("\\", "")
    twelve = bool(re.search(r"AM/PM|A/P", plain, re.IGNORECASE))
    hour = moment.hour % 12 or 12 if twelve else moment.hour
    tokens = _DATE_TOKEN_RE.findall(plain)

    def text_of(index: int, token: str) -> str:
        low = token.lower()
        if low.startswith("y"):
            return f"{moment.year:04d}" if len(low) == 4 else f"{moment.year % 100:02d}"
        if low.startswith("m"):
            minute = (index > 0 and tokens[index - 1].lower().startswith("h")) or (
                index + 1 < len(tokens) and tokens[index + 1].lower().startswith("s"))
            if minute and len(low) <= 2:
                return f"{moment.minute:0{len(low)}d}"
            return {1: str(moment.month), 2: f"{moment.month:02d}", 3: moment.strftime("%b"),
                    4: moment.strftime("%B"), 5: moment.strftime("%b")[0]}[len(low)]
        if low.startswith("d"):
            return {1: str(moment.day), 2: f"{moment.day:02d}", 3: moment.strftime("%a"),
                    4: moment.strftime("%A")}[len(low)]
        if low.startswith("h"):
            return f"{hour:0{len(low)}d}"
        if low.startswith("s"):
            return f"{moment.second:0{len(low)}d}"
        if low.startswith("."):
            return "." + "0" * (len(low) - 1)
        if low in ("am/pm", "a/p"):
            word = "AM" if moment.hour < 12 else "PM"
            return word if low == "am/pm" else word[0]
        return token

    counter = iter(range(len(tokens)))
    return _DATE_TOKEN_RE.sub(lambda m: text_of(next(counter), m.group(0)), plain)


def render(value: float, code: str) -> str | None:
    """``value`` as ``code`` shows it, or None when the format is one this
    module does not render."""
    sections = _sections(code)
    if is_date(code):
        return _date(value, sections[0])
    section = sections[0]
    negative = value < 0
    if negative and len(sections) > 1:
        section, negative = sections[1], False
    elif value == 0 and len(sections) > 2:
        section = sections[2]
    plain = _literals(section)
    if plain is None:
        return None
    text, percent = plain
    if percent:
        value *= 100
    match = _NUMBER_RE.search(text)
    if match is None:
        return text.translate(_SHOW) or None
    pattern = match.group(0)
    tail = text[match.end():]
    exponent = re.match(r"[Ee][+-][0#]+", tail)
    if exponent is not None:
        number = _scientific(value, pattern + exponent.group(0))
        tail = tail[exponent.end():]
    else:
        number = _number(value, pattern)
    out = ("-" if negative else "") + text[:match.start()] + number + tail
    return out.translate(_SHOW)
