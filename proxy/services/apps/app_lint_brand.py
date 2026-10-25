"""The colour rules of an app's client, run by ``app_lint``.

``client.palette``: a Tailwind palette class, a white or black utility, an
arbitrary colour or a literal hex in the page's CSS is a fixed colour the
theme never reaches; the kit's variables follow both themes. A client's
brand is exempt: ``client/brand.css`` or a ``<style id="brand">`` gives the
kit's variables new values, and every ``--p-*`` declaration and ``var()``
fallback is the theme's own route.

``brand.*``: the brand measured the way the design system measures the kit
(``contrastPairs`` in ``dashboard/design/build.mjs``): the kit's values from
the kit file the proxy serves, the brand's over them in each theme as the
cascade orders them, each pair a brand touches held to 4.5 to 1 for text and
3 to 1 for a border, the focus ring and a status fill. A brand that sets a
status or accent colour, sets a colour for one theme only, or writes one the
check cannot read gets a warning of its own.
"""

from __future__ import annotations

import bisect
import math
import re
from pathlib import Path

from services.apps.app_lint import (
    MAX_CANDIDATES,
    WARN,
    Finding,
    _blank,
    _hits,
    _needs,
    _Rows,
    _string_end,
)

BRAND_FILE = "client/brand.css"
_CSS_SUFFIX = ".css"

# Ported from the dashboard's own rule (``dashboard/design/lint/no-raw-palette.mjs``).
_HUES = ("amber|yellow|green|emerald|red|rose|blue|sky|indigo|purple|violet|teal|cyan|"
         "gray|slate|zinc|neutral|stone|orange|lime|pink|fuchsia")
_PREFIX = ("text|bg|border|border-[trblxyse]|ring|ring-offset|inset-ring|from|via|to|fill|stroke|"
           "outline|shadow|inset-shadow|drop-shadow|text-shadow|decoration|divide|placeholder|caret|"
           "accent")
_PALETTE_RE = re.compile(rf"(?:{_PREFIX})-(?:{_HUES})-(?:50|[1-9]00|950)")
_WHITE_BLACK_RE = re.compile(rf"(?:{_PREFIX})-(?:white|black)")
_ARBITRARY_RE = re.compile(
    rf"(?:{_PREFIX})-\[[^\]]*(?:#[0-9a-f]{{3,8}}\b|rgba?\(|hsla?\(|oklch\(|oklab\(|color:)", re.I)
# Where a palette word may start: a utility prefix before a hue, white,
# black or an arbitrary value.
_PALETTE_WORD_RE = _needs(re.compile(rf"(?:{_PREFIX})-(?:{_HUES}|white|black|\[)"), "-")
_HEX_RE = _needs(re.compile(r"(^|[\s:(,])#([0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z_-])", re.I),
                 "#")
_VAR_RE = re.compile(r"var\([^()]{0,500}\)")
# An SVG paint server or an image (``fill: url(#fade)``) names no colour.
_URL_RE = re.compile(r"url\([^()]{0,2000}\)")
_P_DECLARATION_RE = re.compile(r"--p-[\w-]{1,80}\s*:[^;{}]{0,500}")
# How far back from a hex its declaration's colon is looked for.
VALUE_REACH = 2000
_CSS_BODY_RE = re.compile(r"\{([^{}]{0,20000})\}")
_CLASS_ATTR_RE = _needs(re.compile(r"\bclass\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I), "class")
_STYLE_ATTR_RE = _needs(re.compile(r"\bstyle\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I), "style")
_STYLE_OPEN_RE = _needs(re.compile(r"<style\b([^<>]*)>", re.I), "<style")
_STYLE_CLOSE_RE = re.compile(r"</style", re.I)
_BRAND_ID_RE = re.compile(r"\bid\s*=\s*[\"']?brand(?:[\"'\s/]|$)", re.I)
_QUOTES_RE = re.compile(r"[\"'`]")
_WORD_RE = re.compile(r"\S+")
_PALETTE_WORD = ("{word} is a fixed colour, so it ignores the theme. Use the kit's variables, like "
                 "bg-[var(--p-surface)].")
_PALETTE_HEX = ("{word} is a fixed colour, so it ignores the theme. Use a kit variable, or put a "
                "client's colours in brand.css.")


def _bare_utility(word: str) -> str:
    """A class word without its variants, important marks and opacity."""
    depth = 0
    cut = 0
    for i, ch in enumerate(word):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == ":" and depth == 0:
            cut = i + 1
    u = word[cut:].lstrip("!-").rstrip("!")
    return re.sub(r"/(?:\d+|\[[^\]]*\])$", "", u)


def class_problem(word: str) -> bool:
    """Whether one class word paints a fixed colour; an arbitrary value
    whose colour is a ``var()`` fallback is the theme's."""
    if not word or len(word) > 200:
        return False
    u = _bare_utility(word)
    if _PALETTE_RE.fullmatch(u) or _WHITE_BLACK_RE.fullmatch(u):
        return True
    return bool(_ARBITRARY_RE.match(u)) and bool(_ARBITRARY_RE.match(_VAR_RE.sub("var()", u)))


_WORD_REACH = 201


def _word_at(text: str, i: int) -> tuple[int, str]:
    """The word around ``text[i]``, ended by a space or a quote, read at
    most ``_WORD_REACH`` characters each way (a longer word is no class)."""
    start = i
    while start > max(0, i - _WORD_REACH) and not text[start - 1].isspace() \
            and text[start - 1] not in "\"'`":
        start -= 1
    end = i
    limit = min(len(text), i + _WORD_REACH)
    while end < limit and not text[end].isspace() and text[end] not in "\"'`":
        end += 1
    return start, text[start:end]


def _string_spans(code: str) -> list[tuple[int, int]]:
    """The string literals of comment-stripped script code, as spans."""
    spans: list[tuple[int, int]] = []
    i = 0
    for _ in range(4 * MAX_CANDIDATES):
        m = _QUOTES_RE.search(code, i)
        if m is None:
            break
        end = _string_end(code, m.start(), m.group())
        if end < 0:
            i = m.start() + 1
            continue
        spans.append((m.start(), end))
        i = end
    return spans


def _value_hexes(values: str, offset: int, rows: _Rows) -> bool:
    """Literal hex colours in declaration text (a style attribute, or a
    rule's body), its ``--p-*`` declarations, ``var()`` fallbacks and
    ``url()`` references left out; False once the rule has its fill. A hex
    counts in a declaration's value: a colon stands between it and the
    declaration's start (a selector outside a rule body is never read)."""
    for pattern in (_P_DECLARATION_RE, _VAR_RE, _URL_RE):
        values = pattern.sub(lambda m: _blank(m.group()), values)
    for m in _hits(_HEX_RE, values):
        at = m.start(2) - 1
        declaration = values.rfind(";", max(0, at - VALUE_REACH), at) + 1
        if values.find(":", max(declaration, at - VALUE_REACH), at) < 0:
            continue
        if not rows.add("client.palette", WARN, offset + at, _PALETTE_HEX.format(word=f"#{m.group(2)}")):
            return False
    return True


def _css_hexes(css: str, offset: int, rows: _Rows) -> None:
    """Literal hex colours in a stylesheet's declarations."""
    for body in _hits(_CSS_BODY_RE, css):
        if not _value_hexes(body.group(1), offset + body.start(1), rows):
            return


def _style_blocks(src: str) -> list[tuple[int, int, str]]:
    """Each ``<style>`` element's body as ``(start, end, attributes)``: an
    opening tag inside a body is the body's text, and an unclosed one runs
    to the end of the page."""
    out = []
    pos = 0
    for m in _hits(_STYLE_OPEN_RE, src):
        if m.start() < pos:
            continue
        close = _STYLE_CLOSE_RE.search(src, m.end())
        pos = close.start() if close else len(src)
        out.append((m.end(), pos, m.group(1)))
        if close is None:
            break
    return out


# A page carries one brand: the first few brand styles are measured.
MAX_BRAND_BLOCKS = 4


def brand_style_spans(src: str) -> list[tuple[int, int]]:
    """The bodies of the page's first ``<style id="brand">`` elements."""
    spans = [(a, b) for a, b, attrs in _style_blocks(src) if _BRAND_ID_RE.search(attrs)]
    return spans[:MAX_BRAND_BLOCKS]


def lint_palette(src: str, code: str, file: str, html: bool) -> list[Finding]:
    """``client.palette`` over one comment-stripped client file: ``code`` is
    the text its script rules read (an HTML page's scripts alone)."""
    if file == BRAND_FILE:
        return []
    rows = _Rows(file, src)
    if file.lower().endswith(_CSS_SUFFIX):
        _css_hexes(src, 0, rows)
        return rows.rows
    if html:
        for m in _hits(_CLASS_ATTR_RE, src):
            group = 1 if m.group(1) is not None else 2
            for w in _WORD_RE.finditer(m.group(group)):
                if class_problem(w.group()) and not rows.add(
                        "client.palette", WARN, m.start(group) + w.start(), _PALETTE_WORD.format(word=w.group())):
                    break
        for m in _hits(_STYLE_ATTR_RE, src):
            group = 1 if m.group(1) is not None else 2
            _value_hexes(m.group(group), m.start(group), rows)
        for a, b, attrs in _style_blocks(src):
            if not _BRAND_ID_RE.search(attrs):
                _css_hexes(src[a:b], a, rows)
    # A class word in a script's string literal (a class built in code).
    spans: list[tuple[int, int]] | None = None
    opens: list[int] = []
    seen: set[int] = set()
    for m in _hits(_PALETTE_WORD_RE, code):
        start, word = _word_at(code, m.start())
        if start in seen:
            continue
        seen.add(start)
        if spans is None:
            spans = _string_spans(code)
            opens = [a for a, _ in spans]
        k = bisect.bisect_right(opens, m.start()) - 1
        if k < 0 or not spans[k][0] < m.start() < spans[k][1]:
            continue
        if class_problem(word) and not rows.add("client.palette", WARN, start,
                                                _PALETTE_WORD.format(word=word)):
            break
    return rows.rows


# ── the brand ───────────────────────────────────────────────────────────────

# The pairs ``build.mjs`` measures, in the kit's names: text 4.5 to 1, a
# border, the focus ring and a status fill 3 to 1. A pair whose name the kit
# lacks is skipped.
_SURFACES = ("--p-bg", "--p-card", "--p-surface", "--p-surface-raised", "--p-surface-overlay",
             "--p-surface-sunken")
_STATUSES = ("success", "warning", "danger", "info")
TEXT_MIN = 4.5
EDGE_MIN = 3.0


def _pairs() -> list[tuple[str, str, float]]:
    out = [(fg, bg, TEXT_MIN) for fg in ("--p-text", "--p-text-secondary", "--p-text-light",
                                         "--p-link", "--p-primary") for bg in _SURFACES]
    out += [(fg, "--p-brand-surface", TEXT_MIN) for fg in ("--p-text", "--p-link")]
    out += [("--p-action-fg", bg, TEXT_MIN) for bg in ("--p-action", "--p-action-hover")]
    out += [("--p-primary-fg", "--p-action", TEXT_MIN)]
    out += [(f"--p-status-{s}-fg", bg, TEXT_MIN) for s in _STATUSES
            for bg in ("--p-bg", "--p-card", "--p-surface")]
    out += [(f"--p-status-{s}-solid", bg, EDGE_MIN) for s in _STATUSES for bg in ("--p-bg", "--p-card")]
    out += [("--p-focus", bg, EDGE_MIN) for bg in ("--p-bg", "--p-card", "--p-surface-raised",
                                                   "--p-surface-overlay")]
    out += [("--p-border-strong", bg, EDGE_MIN) for bg in ("--p-bg", "--p-card")]
    out += [("--p-link", "--p-action-subtle", TEXT_MIN)]
    return out


PAIRS = _pairs()


def _reserved_message(name: str) -> str:
    if name.startswith("--p-code-"):
        return f"{name} is the code slab's colour. A brand never sets it, so code reads the same in every app."
    if name == "color-scheme":
        return "color-scheme follows the page that holds the app. A brand never sets it."
    return (f"{name} is kept for status and accents. A brand never sets it, so green, "
            "amber and red mean the same in every app.")
# The colours a brand never sets: one meaning per colour in every app, the
# code slab dark in both themes, and color-scheme matching the page that
# holds the frame (the kit's list in the design system's "The artifact kit").
_RESERVED_PREFIXES = ("--p-status-", "--p-accent-", "--p-code-")
_RESERVED_NAMES = frozenset({"--p-success", "--p-error", "--p-on-solid", "--p-mono", "color-scheme"})
# Kit names that hold no colour.
_NOT_COLOUR_PREFIXES = ("--p-font", "--p-radius", "--p-shadow", "--p-elevation", "--p-mono",
                        "--p-control", "--p-space", "--p-size", "--p-line", "--p-weight",
                        "--p-duration", "--p-ease", "--p-z")
LIGHT = "light"
DARK = "dark"
THEMES = (LIGHT, DARK)
KIT_MISSING_RULE = "brand.kit-missing"
KIT_MISSING = "The kit file is missing, so the brand's contrast was not measured."
_DECLARATION_RE = re.compile(r"(--p-[\w-]{1,80}|color-scheme)\s*:\s*([^;{}]{1,500})")
_HEX_VALUE_RE = re.compile(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", re.I)
# The alpha's own spaces sit inside its group, so no two quantifiers take
# the same whitespace.
_RGB_VALUE_RE = re.compile(
    r"rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})\s*(?:[/,]\s*([0-9.]{1,12})(%?)\s*)?\)", re.I)
_RULE_STOPS = re.compile(r"[{};]")
# The braces and semicolons a stylesheet is read up to: a brand is a few
# dozen declarations, the kit a few hundred.
MAX_RULE_STOPS = 50000
_AT_KEPT_RE = re.compile(r"@layer\b", re.I)
# A selector's weight as the cascade ranks the brand's own rules: ``:root``
# and ``.dark`` tie (the later wins), ``html.dark`` and ``:root.dark`` win.
_DARK_SELECTOR_RE = re.compile(r"\.dark\b")
_ROOT_SELECTOR_RE = re.compile(r":root\b")
_HTML_DARK_RE = re.compile(r"\bhtml\.dark\b")
_ROOT_DARK_RE = re.compile(r":root\.dark\b")
_kit_cache: dict[str, tuple[float, dict[str, dict[str, str]]]] = {}

Colour = tuple[int, int, int, float]


def parse_colour(value: str) -> Colour | None:
    """``#rgb``, ``#rrggbb``, ``#rrggbbaa`` or ``rgb(r g b / a)`` (the form
    the kit writes a translucent value in), or None."""
    value = value.strip()
    m = _HEX_VALUE_RE.fullmatch(value)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        alpha = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha
    m = _RGB_VALUE_RE.fullmatch(value)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        if max(r, g, b) > 255:
            return None
        alpha = 1.0
        if m.group(4):
            alpha = float(m.group(4)) / (100 if m.group(5) else 1)
        return r, g, b, min(max(alpha, 0.0), 1.0)
    return None


def _round(x: float) -> int:
    # Math.round: a half goes up, never to the even neighbour.
    return math.floor(x + 0.5)


def composite(fg: Colour, bg: Colour) -> Colour:
    """``fg`` laid over an opaque ``bg``."""
    a = fg[3]
    return (_round(fg[0] * a + bg[0] * (1 - a)), _round(fg[1] * a + bg[1] * (1 - a)),
            _round(fg[2] * a + bg[2] * (1 - a)), 1.0)


def luminance(c: Colour) -> float:
    def lin(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def contrast(fg: Colour, bg: Colour) -> float:
    """WCAG 2 contrast ratio; a translucent foreground is composited over
    the (opaque) background first."""
    a = luminance(composite(fg, bg))
    b = luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _rules(css: str) -> list[tuple[str, str, int]]:
    """The style rules of a stylesheet as ``(selector, body, body offset)``,
    in source order: rules inside ``@layer`` are read, rules inside any other
    at-rule (``@media``, ``@supports``, ``@container``) are not."""
    out: list[tuple[str, str, int]] = []
    skip_depth = 0
    depth = 0
    prelude_start = 0
    i = 0
    for _ in range(MAX_RULE_STOPS):
        if len(out) >= MAX_CANDIDATES:
            break
        m = _RULE_STOPS.search(css, i)
        if m is None:
            break
        c, j = m.group(), m.start()
        if c == ";":
            prelude_start = j + 1
        elif c == "{":
            prelude = css[prelude_start:j].strip()
            depth += 1
            if skip_depth:
                pass
            elif prelude.startswith("@"):
                if not _AT_KEPT_RE.match(prelude):
                    skip_depth = depth
            else:
                close = css.find("}", j + 1)
                if close < 0:
                    break
                out.append((prelude, css[j + 1:close], j + 1))
                depth -= 1
                prelude_start = i = close + 1
                continue
            prelude_start = j + 1
        else:
            if skip_depth == depth:
                skip_depth = 0
            depth = max(0, depth - 1)
            prelude_start = j + 1
        i = j + 1
    return out


def _weight(selector: str) -> int | None:
    """How a rule ranks among the brand's own (higher wins, a tie goes to
    the later): None for a rule that sets no theme value."""
    if _ROOT_DARK_RE.search(selector):
        return 3
    if _HTML_DARK_RE.search(selector):
        return 2
    if _ROOT_SELECTOR_RE.search(selector) or _DARK_SELECTOR_RE.search(selector):
        return 1
    return None


def _declarations(css: str, offset: int = 0) -> dict[str, list[tuple[str, str, int, int]]]:
    """The ``--p-*`` declarations of a stylesheet by theme, as ``(name,
    value, offset, weight)`` in source order: a ``:root`` rule without
    ``.dark`` is light, a ``.dark`` or ``:root.dark`` rule dark."""
    out: dict[str, list[tuple[str, str, int, int]]] = {LIGHT: [], DARK: []}
    for selector, body, at in _rules(css):
        weight = _weight(selector)
        if weight is None:
            continue
        theme = DARK if _DARK_SELECTOR_RE.search(selector) else LIGHT
        for d in _DECLARATION_RE.finditer(body):
            out[theme].append((d.group(1), d.group(2).strip(), offset + at + d.start(), weight))
    return out


def kit_values() -> dict[str, dict[str, str]] | None:
    """The kit's values per theme, read from the kit file the proxy serves
    (``config.DASHBOARD_DIST``, the file the image carries); None when it is
    absent."""
    import config
    path = Path(config.DASHBOARD_DIST) / "ui-kit" / "otodock-tokens.css"
    try:
        mtime = path.stat().st_mtime
        cached = _kit_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    from services.apps.app_lint import strip_comments
    found = _declarations(strip_comments(text, False))
    light = {n: v for n, v, _at, _w in found[LIGHT]}
    values = {LIGHT: light, DARK: {**light, **{n: v for n, v, _at, _w in found[DARK]}}}
    _kit_cache[str(path)] = (mtime, values)
    return values


def _is_colour_name(name: str, kit: dict[str, dict[str, str]] | None) -> bool:
    if name.startswith(_NOT_COLOUR_PREFIXES):
        return False
    if kit is not None and name in kit[LIGHT]:
        return parse_colour(kit[LIGHT][name]) is not None
    return True


def lint_brand(css: str, offset: int, file: str, text: str) -> list[Finding]:
    """The brand's rows: ``css`` is the brand stylesheet (``client/brand.css``
    or a ``<style id="brand">`` body) found at ``offset`` in ``text``, the
    file ``file`` holds."""
    rows = _Rows(file, text)
    found = _declarations(css, offset)
    if not found[LIGHT] and not found[DARK]:
        return []
    kit = kit_values()
    # The value each theme takes, and where the brand set it: light reads
    # the brand's light rules in order, dark every rule by weight then order.
    light = {n: (v, at) for n, v, at, _w in found[LIGHT]}
    ranked = sorted(found[LIGHT] + found[DARK], key=lambda d: (d[3], d[2]))
    dark = {n: (v, at) for n, v, at, _w in ranked}
    declared = {LIGHT: light, DARK: {n: (v, at) for n, v, at, _w in found[DARK]}}
    for theme in THEMES:
        other = DARK if theme == LIGHT else LIGHT
        for name, (value, at) in declared[theme].items():
            if name.startswith(_RESERVED_PREFIXES) or name in _RESERVED_NAMES:
                rows.add("brand.reserved", WARN, at, _reserved_message(name))
                continue
            if not _is_colour_name(name, kit):
                continue
            if parse_colour(value) is None:
                rows.add("brand.not-hex", WARN, at,
                         f"{name} is not a hex colour, so its contrast was not measured. Write "
                         "brand colours as hex.")
            if name not in declared[other]:
                block = ":root.dark" if other == DARK else ":root"
                rows.add("brand.one-theme", WARN, at,
                         f"{name} is set for the {theme} theme only. Set it in {block} as well.")
    if kit is None:
        rows.add(KIT_MISSING_RULE, WARN, offset, KIT_MISSING)
        return rows.rows
    for theme, brand in ((LIGHT, light), (DARK, dark)):
        values = {**kit[theme], **{n: v for n, (v, _at) in brand.items()}}
        ground: Colour = (255, 255, 255, 1.0) if theme == LIGHT else (0, 0, 0, 1.0)
        page = parse_colour(values.get("--p-bg", "")) or ground
        page = composite(page, ground)
        for fg_name, bg_name, floor in PAIRS:
            touched = [n for n in (fg_name, bg_name) if n in brand]
            if not touched or fg_name not in values or bg_name not in values:
                continue
            fg, bg = parse_colour(values[fg_name]), parse_colour(values[bg_name])
            if fg is None or bg is None:
                continue
            if bg[3] < 1:
                bg = composite(bg, page)
            ratio = contrast(fg, bg)
            if ratio < floor:
                rows.add("brand.contrast", WARN, brand[touched[0]][1],
                         f"{fg_name} on {bg_name} measures {ratio:.2f} to 1 in the {theme} theme, "
                         f"under {floor:g} to 1. Use a darker shade in the light theme or a lighter "
                         "one in the dark theme.")
    return rows.rows
