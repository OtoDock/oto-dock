"""Static checks of an app before it ships (APPS.md "Deploy pipeline").

The sandbox refuses a lot of ordinary web code without a word in the
proxy's log: a raw ``fetch`` the CSP blocks, ``localStorage`` that throws
at an opaque origin, a ``<form>`` the frame cannot submit, a server bound
to ``localhost`` that the splice never reaches. These rules name those
mistakes with the file, the line and the fix, in the words of the skill,
before the folder becomes a release. Text rules over comment-stripped
sources: a FAIL is a pattern that is unambiguous code and refuses the
deploy, a WARN is a judgement the author may override and rides in the
ack. A single-file app gets the client rules through ``lint_html``, its
warnings riding a pin that goes through.

The checks run in a worker thread but hold the interpreter lock, so a
crafted tree at the release caps must not stall the proxy, and every rule
is kept linear in its input: a pattern opens on a literal (a leading
lookbehind or word boundary makes the regex engine try every position, ten
times slower) and the boundary is checked in code; no two quantifiers of a
pattern take the same characters, and every repeat that can fail is bounded
(a backtracking repeat costs the square of its run, a run of three the
cube); a rule examines ``MAX_CANDIDATES`` per file and reports
``MAX_FINDINGS_PER_RULE``; a candidate that starts a scan of its own in
Python is one of ``MAX_READS``, its scan bounded by ``MAX_CALL_STOPS``;
every brace match is bounded in characters and in stops. The crafted-input
tests build every shape that ever broke one of these on both sides.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import cast

FAIL = "fail"
WARN = "warn"
CLIENT_SUFFIXES = (".html", ".htm", ".js", ".mjs", ".css")
SERVER_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", ".cjs", ".cts")
SERVER_ENTRIES = ("server/index.ts", "server/index.js")
INDEX_PAGE = "client/index.html"
APP_JSON = "app.json"
KIT_FILE = "/ui-kit/otodock-tokens.css"
TAILWIND_FILE = "/ui-kit/tailwind.js"
# What the description key may hold: a sentence or two for the card.
DESCRIPTION_KEY = "description"
MAX_DESCRIPTION_CHARS = 200
APP_PORT = 3000
DATA_DIR = "/app/data"
# Bare module names Bun resolves without node_modules.
NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "crypto", "dns", "events", "fs",
    "http", "https", "net", "os", "path", "perf_hooks", "querystring", "readline",
    "stream", "string_decoder", "timers", "tls", "url", "util", "worker_threads", "zlib",
})
# The feeds each widget may bind (``dashboard/src/uikit/widgets.ts``
# ``mount``): the lanes widget reads the sessions or a project's lanes.
WIDGET_FEEDS: dict[str, tuple[str, ...]] = {
    "sessions": ("sessions",), "lanes": ("sessions", "project_lanes"), "tasks": ("tasks",),
    "notifications": ("notifications",), "checks": ("checks",),
}
# The bounds that keep a crafted file linear: the matches a rule examines in
# one file, the findings it reports there, the brace-matched bodies a file
# may open and how far one may run.
MAX_CANDIDATES = 5000
MAX_FINDINGS_PER_RULE = 50
MAX_BODY_SCANS = 50
BODY_WINDOW = 20000
SERVE_WINDOW = 200_000
MAX_SERVE_OBJECTS = 4
IMPORT_REACH = 600
# The candidates that each start a scan of their own in Python (an argument,
# an answer's call, the call a leak sits in) and the stops one such scan
# walks: a few hundred calls in a file, a few dozen brackets in one call.
MAX_READS = 500
MAX_CALL_STOPS = 64
# The brackets, quotes and commas one brace match or one object literal's
# top level walks.
MAX_BODY_STOPS = 2000
MAX_SERVE_STOPS = 10000


@dataclass
class Finding:
    rule: str
    severity: str  # FAIL | WARN
    file: str
    line: int
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Declared:
    """What app.json declares, as the rules need it."""
    action_ids: set[str] = field(default_factory=set)
    feeds: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)
    egress: set[str] = field(default_factory=set)
    handlers: set[str] = field(default_factory=set)
    # The buttons that act for the agent with no floor, in manifest order.
    unfloored: list[str] = field(default_factory=list)


def declared_from_actions(actions_json: str) -> Declared:
    from auth import roles
    from services.apps.manifest import ACTING_ACTION_TYPES
    d = Declared()
    try:
        actions = json.loads(actions_json or "[]")
    except ValueError:
        actions = []
    for a in actions if isinstance(actions, list) else []:
        if not isinstance(a, dict):
            continue
        if a.get("id"):
            d.action_ids.add(str(a["id"]))
            # A raw template document may still spell the default floor.
            if a.get("type") in ACTING_ACTION_TYPES and a.get("min_role") in (None, roles.VIEWER,
                                                                              roles.NO_ACCESS):
                d.unfloored.append(str(a["id"]))
        if a.get("type") == "data_feed" and a.get("feed"):
            d.feeds.add(str(a["feed"]))
        if a.get("type") == "platform" and a.get("method"):
            d.methods.add(str(a["method"]))
    return d


def declared_from_doc(doc: dict) -> Declared:
    """From a raw ``app.json`` that has not been validated against an agent
    (a template folder before its agent exists): the same sets the client
    and server rules need, read off the document."""
    d = declared_from_actions(json.dumps(doc.get("actions") or []))
    d.egress = {h for h in (doc.get("egress") or []) if isinstance(h, str)}
    handlers_doc = doc.get("handlers")
    h = handlers_doc if isinstance(handlers_doc, dict) else {}
    d.handlers = (set(h.get("on_schedule") or {}) | set(h.get("on_trigger") or [])
                  | set(h.get("on_event") or {}))
    inbound_doc = doc.get("inbound")
    inbound = inbound_doc if isinstance(inbound_doc, dict) else {}
    d.handlers |= {cast("str", spec.get("handler")) for spec in inbound.values()
                   if isinstance(spec, dict) and isinstance(spec.get("handler"), str)}
    return d


def declared_from_manifest(m) -> Declared:
    """From a validated ``app_deploy.Manifest``."""
    d = declared_from_actions(m.actions_json)
    with contextlib.suppress(ValueError):
        d.egress = {h for h in json.loads(m.egress_json or "[]") if isinstance(h, str)}
    try:
        h = json.loads(m.handlers_json or "{}")
    except ValueError:
        h = {}
    if isinstance(h, dict):
        d.handlers = (set(h.get("on_schedule") or {}) | set(h.get("on_trigger") or [])
                      | set(h.get("on_event") or {}))
    # An inbound hook's handler is a route too (APPS.md "Inbound hooks").
    try:
        inbound = json.loads(getattr(m, "inbound_json", "") or "{}")
    except ValueError:
        inbound = {}
    if isinstance(inbound, dict):
        d.handlers |= {cast("str", spec.get("handler")) for spec in inbound.values()
                       if isinstance(spec, dict) and isinstance(spec.get("handler"), str)}
    return d


# ── comments ────────────────────────────────────────────────────────────────

# One pass over strings and comments in source order: a string is kept
# whole (so the ``//`` of a URL and a ``/*`` inside a quoted path are never
# comments), a line comment ends at the newline (so a ``/*`` inside it never
# opens a block), a block comment ends at its ``*/``. Written as a scan
# rather than a regex: an opener with no closer made the regex rescan to
# the end of the file from every later opener, hours of CPU for a crafted
# page at the size cap. An unterminated opener is plain text, as it was.


# The openers a comment scan looks for: the quotes, and the two comment
# openers; a kind that failed to close for the rest of the text drops out, so
# a crafted file of unclosed openers is passed over by the regex, not one
# Python step per opener.
_JS_OPENER_SETS: dict[tuple[bool, bool], re.Pattern] = {
    (tick, block): re.compile("[\"']" + ("|`" if tick else "") + ("|/[/*]" if block else "|//"))
    for tick in (True, False) for block in (True, False)}
# Where a string's scan stops: an escape, its own quote, and for the two
# one-line quotes a raw newline.
_STRING_STOPS = {'"': re.compile(r'[\\"\n]'), "'": re.compile(r"[\\'\n]"),
                 "`": re.compile(r"[\\`]")}


def _blank(seg: str) -> str:
    """``seg`` with every character but a newline turned to a space (one
    string operation per line, never one per character)."""
    return "\n".join(" " * len(part) for part in seg.split("\n"))


def _string_end(text: str, i: int, quote: str, limit: int | None = None) -> int:
    """The index after the string that opens at ``text[i]``, or -1 when it
    never closes before ``limit`` (a ``"`` or ``'`` string also ends,
    unclosed, at a raw newline; a backslash escapes any one character)."""
    stops = _STRING_STOPS[quote]
    end = len(text) if limit is None else min(len(text), limit)
    j = i + 1
    while True:
        m = stops.search(text, j, end)
        if m is None:
            return -1
        c = m.group()
        if c == "\\":
            j = m.start() + 2
            continue
        return m.end() if c == quote else -1


def _strip_html_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    while True:
        a = text.find("<!--", i)
        b = text.find("-->", a + 4) if a >= 0 else -1
        if b < 0:
            break
        out += [text[i:a], _blank(text[a:b + 3])]
        i = b + 3
    out.append(text[i:])
    return "".join(out)


def _strip_js_comments(text: str) -> str:
    if "//" not in text and "/*" not in text:
        return text
    n = len(text)
    out: list[str] = []
    last = i = 0
    # A scan that failed from one opener fails from every later opener of
    # the same kind before the point it reached (the escapes parse alike
    # from there), so each failure is paid once.
    failed_until = {'"': -1, "'": -1, "`": -1, "/*": -1}
    while True:
        openers = _JS_OPENER_SETS[(failed_until["`"] < n, failed_until["/*"] < n)]
        m = openers.search(text, i)
        if m is None:
            break
        i = m.start()
        c = text[i]
        if c == "/":
            nxt = text[i + 1:i + 2]
            end = -1
            if nxt == "/":
                end = text.find("\n", i)
                end = n if end < 0 else end
            elif nxt == "*" and i >= failed_until["/*"]:
                close = text.find("*/", i + 2)
                if close < 0:
                    failed_until["/*"] = n
                else:
                    end = close + 2
            if end < 0:
                i += 1
                continue
            out += [text[last:i], _blank(text[i:end])]
            last = i = end
            continue
        if i < failed_until[c]:
            i += 1
            continue
        end = _string_end(text, i, c)
        if end < 0:
            nl = text.find("\n", i)
            failed_until[c] = n if c == "`" or nl < 0 else nl
            i += 1
            continue
        i = end
    out.append(text[last:])
    return "".join(out)


def strip_comments(text: str, html: bool) -> str:
    """The same text with every comment blanked, newlines kept, so a line
    number in the result is a line number in the file."""
    if html:
        text = _strip_html_comments(text)
    return _strip_js_comments(text)


_SCRIPT_OPEN = re.compile(r"<script\b[^<>]*>", re.I)
_SCRIPT_CLOSE = re.compile(r"</script", re.I)
# An ``on*`` attribute: a space before it (the engine skips a run of letters
# at once) and a bounded name.
_HANDLER_ATTR = re.compile(r"\son[a-z]{1,32}\s{0,64}=\s{0,64}([\"'])", re.I)


def script_view(src: str) -> str:
    """An HTML page with everything but its scripts blanked (the bodies of
    its ``<script>`` elements and its ``on*`` handler attributes), lengths
    and newlines kept: the JavaScript rules read code, never the page's
    prose ("please confirm (by replying)", "location = Berlin")."""
    ranges: list[tuple[int, int]] = []
    pos = 0
    for m in _SCRIPT_OPEN.finditer(src):
        if m.start() < pos:
            continue
        close = _SCRIPT_CLOSE.search(src, m.end())
        pos = close.start() if close else len(src)
        ranges.append((m.end(), pos))
    for m in _HANDLER_ATTR.finditer(src):
        close_at = src.find(m.group(1), m.end())
        if close_at < 0:
            break
        ranges.append((m.end(), close_at))
    out: list[str] = []
    last = 0
    for a, b in sorted(ranges):
        if b <= last:
            continue
        a = max(a, last)
        out += [_blank(src[last:a]), src[a:b]]
        last = b
    out.append(_blank(src[last:]))
    return "".join(out)


# ── positions, bounds and findings ──────────────────────────────────────────


class _Lines:
    """Line numbers of one text: rules ask in rising order, so each answer
    counts only the newlines since the last one."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._line = 1

    def at(self, pos: int) -> int:
        if pos < self._pos:
            self._pos, self._line = 0, 1
        self._line += self._text.count("\n", self._pos, pos)
        self._pos = pos
        return self._line


# The row that follows a rule's fill in one file.
_MORE_IN_FILE = "More rows like these in this file. Fix the ones above, then check again."


class _Rows:
    """The findings of one file: a row said once per line, at most
    ``MAX_FINDINGS_PER_RULE`` per rule, and one ``<rule>.more`` row (of the
    rule's severity, so a cut fail still refuses) when the rule finds more."""

    def __init__(self, file: str, text: str) -> None:
        self.file = file
        self.lines = _Lines(text)
        self.rows: list[Finding] = []
        self._counts: dict[str, int] = {}
        self._seen: set[tuple[str, int, str]] = set()

    def add(self, rule: str, severity: str, pos: int, message: str) -> bool:
        """Record a finding at offset ``pos``; False once ``rule`` is past
        its fill (the caller stops looking)."""
        if self._counts.get(rule, 0) > MAX_FINDINGS_PER_RULE:
            return False
        return self.add_line(rule, severity, self.lines.at(pos), message)

    def add_line(self, rule: str, severity: str, line: int, message: str) -> bool:
        """``add`` for a finding whose line is known."""
        n = self._counts.get(rule, 0)
        if n > MAX_FINDINGS_PER_RULE:
            return False
        if (rule, line, message) in self._seen:
            return True
        if n == MAX_FINDINGS_PER_RULE:
            self._counts[rule] = n + 1
            self.rows.append(Finding(f"{rule}.more", severity, self.file, line, _MORE_IN_FILE))
            return False
        self._seen.add((rule, line, message))
        self._counts[rule] = n + 1
        self.rows.append(Finding(rule, severity, self.file, line, message))
        return True


def _after_word(text: str, i: int) -> bool:
    """Whether a word character stands right before ``text[i]`` (where a
    leading ``\\b`` would not match)."""
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")


def _after_ident(text: str, i: int) -> bool:
    """Whether an identifier character, a dot or a ``$`` stands right before
    ``text[i]``: the match is a method or a longer name, never the call."""
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_.$")


_GUARDS = {"word": _after_word, "ident": _after_ident}


# The literals a pattern cannot match without: a text that holds none of
# them skips the pattern's scan (a substring search costs a fraction of a
# regex pass, and most rules find nothing in most files).
_NEEDLES: dict[re.Pattern, tuple[str, ...]] = {}


def _needs(pattern: re.Pattern, *needles: str) -> re.Pattern:
    """Register ``needles`` for ``pattern``; a case-blind pattern's are
    looked for in the lowered text."""
    _NEEDLES[pattern] = tuple(n.lower() for n in needles) if pattern.flags & re.I else needles
    return pattern


def _hits(pattern: re.Pattern, text: str, guard: str = "") -> Iterator[re.Match]:
    """``pattern``'s matches in ``text``, at most ``MAX_CANDIDATES``
    examined, without the ones ``guard`` (``word`` or ``ident``) refuses."""
    needles = _NEEDLES.get(pattern)
    if needles:
        hay = text.lower() if pattern.flags & re.I else text
        if not any(n in hay for n in needles):
            return
    check = _GUARDS.get(guard)
    for m in islice(pattern.finditer(text), MAX_CANDIDATES):
        if check is not None and check(text, m.start()):
            continue
        yield m


_BRACE_STOPS = re.compile(r"[{}\"'`]")


def _matched_body(text: str, start: int, window: int, stops: int = MAX_BODY_STOPS) -> str | None:
    """The brace-matched body that opens at ``text[start]``, strings
    skipped, or None when it does not close within ``window`` characters
    and ``stops`` braces and quotes."""
    limit = min(len(text), start + window)
    depth = 0
    i = start
    for _ in range(stops):
        m = _BRACE_STOPS.search(text, i, limit)
        if m is None:
            return None
        c, j = m.group(), m.start()
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        else:
            end = _string_end(text, j, c, limit)
            if end < 0:
                return None
            i = end
            continue
        i = j + 1
    return None


def _body_after(text: str, start: int) -> str:
    """The brace-matched body that opens at ``text[start]``, or its first
    2,000 characters when it does not close nearby."""
    body = _matched_body(text, start, BODY_WINDOW)
    return body if body is not None else text[start:start + 2000]


# ── the client rules ────────────────────────────────────────────────────────

_RAW_FETCH = ("the sandbox blocks it; use otodock.fetch / otodock.ws: they add the viewer token, "
              "wait for a starting server and reconnect")
_DEVICE = ("the frame delegates no device permissions (allow=\"\"); a selectable <code> for "
           "copying, notifications.create for a notice")
_MODULE = "module and cross-origin loads need CORS the asset route does not send; use a plain script"
_NAVIGATION = ("the host blanks a page that navigates or reloads itself; change the DOM instead "
               "(location.hash is fine)")
# (rule, severity, pattern, message, script_only, guard) applied to every
# client source; a ``script_only`` rule reads an HTML page's scripts alone,
# ``guard`` refuses a match right after a word (``word``) or an identifier
# or a dot (``ident``).
_CLIENT_PATTERNS: list[tuple[str, str, re.Pattern, str, bool, str]] = [
    ("client.raw-fetch", FAIL,
     _needs(re.compile(r"fetch\s*\("), "fetch"),
     _RAW_FETCH, True, "ident"),
    ("client.raw-fetch", FAIL,
     _needs(re.compile(r"XMLHttpRequest\b|EventSource\s*\("), "XMLHttpRequest", "EventSource"),
     _RAW_FETCH, True, "word"),
    ("client.raw-fetch", FAIL,
     _needs(re.compile(r"new\s+WebSocket\s*\(|navigator\.sendBeacon\s*\("), "WebSocket", "sendBeacon"),
     _RAW_FETCH, True, ""),
    ("client.storage", FAIL,
     _needs(re.compile(r"(?:localStorage|sessionStorage|indexedDB)\b"),
            "localStorage", "sessionStorage", "indexedDB"),
     "throws at an opaque origin; keep state in the app's server or in otodock.state",
     True, "word"),
    ("client.storage", WARN,
     _needs(re.compile(r"document\.cookie"), "document.cookie"),
     "always empty at an opaque origin; the frame has no cookies",
     True, ""),
    ("client.form", FAIL,
     _needs(re.compile(r"<form[\s>]", re.I), "<form"),
     "the frame has no allow-forms, so a submit is blocked before any script runs; "
     "wire the button's click and the input's Enter in script",
     False, ""),
    ("client.modal", FAIL,
     _needs(re.compile(r"(?:confirm|prompt)\s*\("), "confirm", "prompt"),
     "blocked without allow-modals: confirm() is always false and prompt() always null, "
     "a dead branch; use an in-page control",
     True, "ident"),
    ("client.modal", WARN,
     _needs(re.compile(r"alert\s*\("), "alert"),
     "blocked without allow-modals; the message is never seen",
     True, "ident"),
    ("client.popup", WARN,
     _needs(re.compile(r"window\.open\s*\("), "window.open"),
     "blocked without allow-popups; a plain <a href> is bridged out by the runtime",
     True, ""),
    ("client.navigation", FAIL,
     _needs(re.compile(r"location\.(?:assign|replace|reload)\s*\(|location\.href\s*=(?!=)"), "location."),
     _NAVIGATION, True, "word"),
    ("client.navigation", FAIL,
     _needs(re.compile(r"location\s*=(?!=)"), "location"),
     _NAVIGATION, True, "ident"),
    ("client.navigation", FAIL,
     _needs(re.compile(r"window\.location\s*=(?!=)"), "window.location"),
     _NAVIGATION, True, ""),
    ("client.navigation", FAIL,
     _needs(re.compile(r"<meta[^<>]+http-equiv\s*=\s*[\"']?refresh", re.I), "http-equiv"),
     "a meta refresh navigates the frame, which the host blanks",
     False, ""),
    ("client.navigation", WARN,
     _needs(re.compile(r"history\.(pushState|replaceState)\s*\("), "history."),
     "throws at an opaque origin",
     True, ""),
    ("client.worker", FAIL,
     _needs(re.compile(r"new\s+(Shared)?Worker\s*\(|navigator\.serviceWorker"), "Worker", "serviceWorker"),
     "a worker script cannot load from an opaque origin",
     True, ""),
    ("client.device-api", WARN,
     _needs(re.compile(r"navigator\.clipboard|new\s+Notification\s*\(|navigator\.geolocation"),
            "navigator.", "Notification"),
     _DEVICE, True, ""),
    ("client.device-api", WARN,
     _needs(re.compile(r"Notification\.requestPermission"), "Notification"),
     _DEVICE, True, "word"),
    ("client.module", WARN,
     _needs(re.compile(r"type\s*=\s*[\"']module[\"']", re.I), "module"),
     _MODULE, False, ""),
    ("client.module", WARN,
     _needs(re.compile(r"import\s*\(", re.I), "import"),
     _MODULE, False, "ident"),
    ("client.module", WARN,
     _needs(re.compile(r"crossorigin\b", re.I), "crossorigin"),
     _MODULE, False, "word"),
    ("client.base", WARN,
     _needs(re.compile(r"<base\b", re.I), "<base"),
     "base-uri 'none': the tag is ignored",
     False, ""),
    ("client.external-resource", FAIL,
     _needs(re.compile(r"url\(\s*[\"']?(https?:)?//|@import\s+[\"'(]*\s*(https?:)?//", re.I), "url(", "@import"),
     "the CSP allows the platform's own origin only; ship the file in client/ or inline it",
     False, ""),
]

_TAG_RE = _needs(re.compile(r"<(script|link|img|video|audio|source|iframe|object|embed|a)\b([^<>]*)>", re.I),
                 "<")
_ATTR_RE = re.compile(r"\b(src|href|data|poster)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)
_EXTERNAL_RE = re.compile(r"^(https?:)?//", re.I)
_FEED_RE = _needs(re.compile(r"otodock\.feed\s*\(\s*[\"']([A-Za-z_]+)[\"']"), "otodock.feed")
_ACTION_RE = _needs(re.compile(r"otodock\.action\s*\(\s*[\"']([\w-]+)[\"']"), "otodock.action")
_METHOD_RE = _needs(re.compile(r"otodock\.platform\s*\(\s*[\"']([\w.]+)[\"']"), "otodock.platform")
_WIDGET_RE = _needs(re.compile(r"widget\s*:\s*[\"'](\w+)[\"']"), "widget")
_ONCLOSE_RE = _needs(re.compile(
    r"\.onclose\s*=\s*(?:function\s*\([^)]{0,200}\)|\([^)]{0,200}\)\s*=>|\w{1,64}\s*=>)\s*\{"), ".onclose")
_TAILWIND_CLASS_RE = re.compile(r"class\s*=\s*[\"'][^\"']*\b(flex|grid|px-\d|py-\d|p-\d|mt-\d|mb-\d|gap-\d|text-(?:xs|sm|lg|xl)|rounded(?:-\w+)?|font-(?:medium|semibold|bold))\b")


def _lint_client(text: str, file: str, d: Declared, assets: set[str] | None) -> list[Finding]:
    """``assets`` is the set of files under client/ (a folder app); None
    for a single-file app, which ships nothing beside itself."""
    html = file.endswith((".html", ".htm"))
    src = strip_comments(text, html)
    rows = _Rows(file, src)
    add = rows.add

    code = script_view(src) if html else src
    for rule, severity, pattern, message, script_only, guard in _CLIENT_PATTERNS:
        for m in _hits(pattern, code if script_only else src, guard):
            if not add(rule, severity, m.start(), message):
                break

    for tag in _hits(_TAG_RE, src):
        name = tag.group(1).lower()
        attrs = tag.group(2)
        if name in ("iframe", "object", "embed"):
            add("client.iframe", FAIL, tag.start(),
                f"<{name}> is refused (frame-src and object-src are 'none')")
            continue
        if name == "a" and re.search(r"\bdownload\b", attrs, re.I):
            add("client.download", WARN, tag.start(),
                "the frame has no allow-downloads; the browser blocks the download")
        for attr in _ATTR_RE.finditer(attrs):
            value = (attr.group(2) or attr.group(3) or attr.group(4) or "").strip()
            low = value.lower()
            if not value or low.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                continue
            if _EXTERNAL_RE.match(value):
                if name == "a":
                    continue  # bridged out of the sandbox by the runtime
                add("client.external-resource", FAIL, tag.start(),
                    f"<{name}> from {value.split('/')[2] if '//' in value else value}: the CSP "
                    "allows the platform's own origin only; ship the file in client/ or inline it")
                continue
            if "://" in low:
                add("client.external-resource", FAIL, tag.start(),
                    f"<{name}> with a {low.split('://')[0]}: URL is refused by the CSP")
                continue
            if value.startswith("/"):
                if value.startswith("/ui-kit/"):
                    continue
                add("client.asset-path", FAIL, tag.start(),
                    f"{value}: the document lives under /v1/apps/<id>/client/<sha>/, a "
                    "root-relative path 404s; use a relative path for an asset, "
                    "otodock.open for a page")
                continue
            if name == "a":
                continue
            rel = value.split("?")[0].split("#")[0]
            rel = rel[2:] if rel.startswith("./") else rel
            if rel.lower().endswith((".html", ".htm")):
                add("client.asset-path", FAIL, tag.start(),
                    f"{value}: a page is never served as an asset")
            elif assets is None:
                add("client.asset-path", FAIL, tag.start(),
                    f"{value}: a single-file app ships no assets; inline it or use /ui-kit/")
            elif rel not in assets:
                add("client.asset-path", FAIL, tag.start(),
                    f"{value}: client/{rel} is not in the folder")

    for m in _hits(_FEED_RE, src):
        if m.group(1) not in d.feeds:
            add("client.undeclared-feed", FAIL, m.start(),
                f"otodock.feed('{m.group(1)}') is used but not declared as a data_feed action "
                "in app.json; the host refuses it")
    for m in _hits(_ACTION_RE, src):
        if m.group(1) not in d.action_ids:
            add("client.undeclared-action", FAIL, m.start(),
                f"otodock.action('{m.group(1)}') names no action in app.json")
    for m in _hits(_METHOD_RE, src):
        if m.group(1) not in d.methods:
            add("client.undeclared-method", FAIL, m.start(),
                f"otodock.platform('{m.group(1)}') is not declared as a platform action in app.json")
    for m in _hits(_WIDGET_RE, src):
        w = m.group(1)
        if w in WIDGET_FEEDS and not set(WIDGET_FEEDS[w]) & d.feeds:
            add("client.widget-feed", FAIL, m.start(),
                f"the {w} widget binds the {' or the '.join(WIDGET_FEEDS[w])} feed, which "
                "app.json does not declare")
        elif w == "connect" and "integrations.status" not in d.methods:
            add("client.widget-feed", FAIL, m.start(),
                "the connect widget needs integrations.status declared as a platform action")
    for m in islice(_ONCLOSE_RE.finditer(src), MAX_BODY_SCANS):
        body = _body_after(src, m.end() - 1)
        if re.search(r"otodock\.ws\s*\(|setTimeout\s*\(", body):
            add("client.ws-reconnect", WARN, m.start(),
                "otodock.ws reconnects by itself; a reconnect of the page's own is redundant")
    if html and _TAILWIND_CLASS_RE.search(src) and TAILWIND_FILE not in src:
        add("client.no-tailwind", WARN, 0,
            "Tailwind classes are used but /ui-kit/tailwind.js is not loaded")
    if html and re.match(r"\s*(<!doctype|<html)", src, re.I):
        kit_at, tailwind_at = src.find(KIT_FILE), src.find(TAILWIND_FILE)
        if kit_at < 0:
            add("client.full-document", WARN, 0,
                "A full document skips the kit. Link /ui-kit/otodock-tokens.css in its head, before "
                "/ui-kit/tailwind.js, for the theme colours and .card and .btn.")
        elif 0 <= tailwind_at < kit_at:
            add("client.full-document", WARN, kit_at,
                "The kit is linked after /ui-kit/tailwind.js, so its rules win over Tailwind's "
                "classes. Link /ui-kit/otodock-tokens.css first.")
        if not re.search(r"<meta[^<>]+name\s*=\s*[\"']viewport", src, re.I):
            add("client.full-document", WARN, 0,
                "a full document needs its own <meta name=\"viewport\"> for phones")
    from services.apps import app_lint_brand
    return rows.rows + app_lint_brand.lint_palette(src, code, file, html)


# ── credentials, in every file ──────────────────────────────────────────────

# Credentials by their vendor's shape, read over the raw text: a key in a
# comment ships in the release too. Every shape has its length, so a line
# that names a prefix ("paste the whsec_ value") or a bare key header is no
# key. A hyphen counts after a known ``sk-`` prefix only (a spinner's
# ``sk-folding-cube`` class and the locale tag ``sk-SK`` are no keys); each
# match must open a word; AWS's documented example key is no key. A key
# body may follow its header after a line break written as ``\n`` (a key
# kept in JSON).
_SECRET_RE = _needs(re.compile(
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{32,}"
    r"|sk-(?:proj|svcacct|admin|ant-[a-z0-9]+)-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}"
    r"|AKIA(?![0-9A-Z]{9}EXAMPLE)[0-9A-Z]{16}\b|gh[pousr]_[A-Za-z0-9]{36}\b"
    r"|github_pat_[A-Za-z0-9_]{50,}|xox[abpr]-[0-9]{8,}-[A-Za-z0-9-]{8,}"
    r"|hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{24}"
    r"|glpat-[A-Za-z0-9_-]{20}|AIza[0-9A-Za-z_-]{35}|GOCSPX-[A-Za-z0-9_-]{28}"
    r"|SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"
    r"|-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----(?:\s|\\[rn]){0,8}"
    r"[A-Za-z0-9+/=]{40}"),
    "k_live_", "k_test_", "whsec_", "sk-", "AKIA", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
    "github_pat_", "xox", "hooks.slack.com", "glpat-", "AIza", "GOCSPX-", "SG.", "PRIVATE KEY")
_SECRET_MESSAGES = {
    "client.secret": ("This looks like a key or a password. Files in client/ are served to "
                      "anyone with the address. Declare it under secrets in app.json."),
    "server.secret": ("This looks like a key or a password. Declare it under secrets in "
                      "app.json and a person sets its value."),
    "app.secret": ("This looks like a key or a password. Declare it under secrets in "
                   "app.json and a person sets its value."),
}


def secret_rule(rel: str) -> str:
    """The rule a credential in ``rel`` is reported under."""
    if rel.startswith("client/"):
        return "client.secret"
    if rel.startswith("server/"):
        return "server.secret"
    return "app.secret"


def _lint_secrets(text: str, file: str, rule: str) -> list[Finding]:
    rows = _Rows(file, text)
    for m in _hits(_SECRET_RE, text, "word"):
        if not rows.add(rule, FAIL, m.start(), _SECRET_MESSAGES[rule]):
            break
    return rows.rows


# ── the server rules ────────────────────────────────────────────────────────

# An import's module, the way Bun reads it: a list over several lines, a
# bare `import "pkg"`, `export … from`; `import type` and `export type`
# bring nothing at run time. Found from the statement's first word, then
# read with a bounded pattern from there.
_IMPORT_START_RE = _needs(re.compile(r"^[ \t]*(?:import|export)\b", re.M), "import", "export")
# The list opens on a character that is no space and runs lazily to its
# ``from``: no two quantifiers ever take the same whitespace.
_IMPORT_STATEMENT_RE = re.compile(
    r"[ \t]*(?:import(?!\s+type\b)\s*(?:[\w$*{},][\w$*\s{},]{0,500}?\bfrom\s*)?"
    r"|export(?!\s+type\b)\s*(?:\*(?:\s+as\s+[\w$]+)?|\{[^{}]{0,500}\})\s*from\s*)"
    r"[\"']([^\"'\n]+)[\"']")
_IMPORT_CALL_RE = _needs(re.compile(r"(?:import|require)\s*\(\s*[\"']([^\"'\n]+)[\"']"),
                         "import", "require")
_DB_RE = _needs(re.compile(r"Database\s*\(\s*[\"']([^\"']+)[\"']"), "Database")
_EGRESS_RE = _needs(re.compile(r"fetch\s*\(\s*[`\"'](https?://([^/`\"'$\s]+))"), "fetch")
# The egress route's host (a route of the app's own like ``/egress/summary/``
# names no host).
_EGRESS_ROUTE_RE = _needs(re.compile(r"/egress/([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8})/"),
                          "/egress/")
_MIGRATION_RE = _needs(re.compile(r"(?:DROP\s+(?:TABLE|COLUMN)|RENAME\s+(?:TABLE|COLUMN|TO))\b", re.I),
                       "drop", "rename")
_WRITE_RE = _needs(re.compile(
    r"(?:writeFile(?:Sync)?|appendFile(?:Sync)?|Bun\.write|mkdir(?:Sync)?)\s*\(\s*[\"'](/[^\"']*)"),
    "File", "Bun.write", "mkdir")
# The serve object: the object literal Bun.serve takes (a type argument
# allowed, `Bun.serve<{ who: Who }>({`), or a default export Bun serves.
_BUN_SERVE = "Bun.serve"
_SERVE_START_RE = _needs(re.compile(r"Bun\.serve\b|export\s+default\b"), _BUN_SERVE, "default")
_DEV_KEY_RE = _needs(re.compile(r"development\s*:"), "development")
_DEV_FALSE_RE = re.compile(r"\s*false\b")
_SERVE_RE = re.compile(
    r"Bun\.serve\s*(?:<[^()]{0,200}?>)?\s*\(\s*\{|(?P<export>export\s+default\s*\{)")
# The serve object's keys the rules read.
_PORT_KEY = "port"
_HOSTNAME_KEY = "hostname"
_DEVELOPMENT_KEY = "development"
_ERROR_MEMBER = "error"
_FETCH_MEMBER = "fetch"
# ``development: true`` or an options object switches it on, ``false``
# leaves it off, anything else may switch it on.
_DEV_ON_RE = re.compile(r"true\s*$|\{")
_DEV_OFF_RE = re.compile(r"false\s*$")
_KEY_RE = re.compile(r"\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*(:|\()?")
_LEVEL_STOPS = re.compile(r"[{}()\[\],\"'`]")
_SPREAD = "..."
_SPREAD_RE = re.compile(r"\s*\.\.\.")
_LOOPBACK_RE = re.compile(r"\s*[\"'`](?:localhost|127\.0\.0\.1|::1)[\"'`]")
_DEV_MODE = ("Development mode answers an error with the server's code. Remove the key. The "
             "platform runs the server in production mode.")
_DEV_MODE_MAYBE = ("Development mode may switch on here, and it answers an error with the "
                   "server's code. Remove the key.")
_EVAL_RE = _needs(re.compile(r"eval\s*\(|Function\s*\(\s*[\"'`]"), "eval", "Function")
_NEW_FUNCTION_RE = _needs(re.compile(r"new\s+Function\s*\("), "Function")
_SQL_CALL_RE = _needs(re.compile(r"\.(?:query|prepare|run)\s*\(\s*"), ".query", ".prepare", ".run")
_INTERPOLATION_RE = re.compile(r"\$\{([^{}`]{0,200})\}")
_REQUEST_DATA_RE = re.compile(r"\b(?:url|req|body|params|searchParams|query)\b")
_QUOTED_RE = re.compile(r"\"[^\"\n]{0,4000}\"|'[^'\n]{0,4000}'")
_SPAWN_RE = _needs(re.compile(r"Bun\.spawn(?:Sync)?\s*\(|Bun\.\$\s*`"), "Bun.spawn", "Bun.$")
_CHILD_PROCESS_RE = _needs(re.compile(r"child_process\b"), "child_process")
_BARE_SHELL_RE = re.compile(r"\$\s*`")
_BUN_SHELL_IMPORT_RE = _needs(re.compile(r"import\s*\{([^{}]{0,600})\}\s*from\s*[\"']bun[\"']"), "bun")
_LEAK_NAMES = r"(?:e|err|error|ex|exception)"
_LEAK_RE = _needs(re.compile(
    r"(?P<own>\.stack\b|\$\{\s*" + _LEAK_NAMES + r"\s*\})"
    r"|" + _LEAK_NAMES + r"\s*\.\s*(?:message\b|toString\s*\(\s*\))"
    r"|String\s*\(\s*" + _LEAK_NAMES + r"\s*\)"),
    ".stack", "message", "toString", "String", "${")
# An answer: ``new Response(``, ``Response.json(``, or a bare ``json(``
# helper (never a method like ``r.json()``, which reads a body).
_ANSWER_RE = _needs(re.compile(r"(?P<response>new\s+Response\s*\(|Response\.json\s*\()|json\s*\("),
                    "Response", "json")


def _is_answer(text: str, m: re.Match) -> bool:
    return bool(m.group("response")) or not _after_ident(text, m.start())
_FILE_CALL_RE = _needs(re.compile(
    r"(?:Bun\.(?:file|write)|(?:read|write|append)File(?:Sync)?|createReadStream|readdir(?:Sync)?"
    r"|unlink(?:Sync)?)\s*\("), "Bun.file", "Bun.write", "File", "createReadStream", "readdir", "unlink")
# A write route: a method compare, a route key whose value is a function
# (``POST: (req) =>``, ``POST: async req =>``, ``POST: function``), or a
# method shorthand (``async POST(req) {``).
_WRITE_ROUTE_RE = _needs(re.compile(
    r"(?P<method>\.method\s*===?\s*[\"'](?:POST|PUT|PATCH|DELETE)[\"'])"
    r"|(?:POST|PUT|PATCH|DELETE)(?:\s*:\s*(?:async\s*)?(?:\(|function\b|[A-Za-z_$][\w$]{0,63}\s*=>)"
    r"|\s*\([^()]{0,200}\)\s*(?::\s*[^{};=]{1,80})?\{)"),
    "POST", "PUT", "PATCH", "DELETE")
_ROLE_WORD_RE = _needs(re.compile(r"role\b"), "role")
ROLE_READ_REACH = 400


def _level_one(body: str) -> Iterator[int]:
    """The offsets in an object literal's body (its outer braces included)
    where a property may start: right after the opening brace and after
    each comma at the top level."""
    depth = 0
    i = 0
    for _ in range(MAX_SERVE_STOPS):
        m = _LEVEL_STOPS.search(body, i)
        if m is None:
            return
        c, j = m.group(), m.start()
        if c in "\"'`":
            end = _string_end(body, j, c)
            if end < 0:
                return
            i = end
            continue
        if c in "{([":
            depth += 1
            if depth == 1:
                yield j + 1
        elif c in "})]":
            depth -= 1
            if depth <= 0:
                return
        elif depth == 1:
            yield j + 1
        i = j + 1


def _top_keys(body: str) -> list[tuple[str, str, int, int]]:
    """The top-level properties of an object literal's body as ``(key,
    kind, key offset, value end)``: kind ``:`` for a value, ``(`` for a
    method, ``""`` for a shorthand, ``...`` for a spread. The value runs to
    the next top-level comma or the closing brace."""
    starts = list(_level_one(body))
    out = []
    for n, s in enumerate(starts):
        if _SPREAD_RE.match(body, s):
            out.append((_SPREAD, _SPREAD, s, s))
            continue
        m = _KEY_RE.match(body, s)
        if m is None:
            continue
        end = starts[n + 1] - 1 if n + 1 < len(starts) else len(body) - 1
        out.append((m.group(1), m.group(2) or "", m.start(1), end))
    return out


def _serve_objects(src: str) -> tuple[list[tuple[int, str, list]], bool]:
    """The serve objects of a server file, as ``(offset, body, top keys)``,
    and whether the file also hands ``Bun.serve`` an object it builds
    elsewhere (a name, a call, a spread) that the rules cannot read."""
    out = []
    opaque = False
    for found in islice(_hits(_SERVE_START_RE, src, "ident"), MAX_SERVE_OBJECTS):
        m = _SERVE_RE.match(src, found.start())
        if m is None:
            opaque = opaque or src.startswith(_BUN_SERVE, found.start())
            continue
        start = m.end() - 1
        body = _matched_body(src, start, SERVE_WINDOW, MAX_SERVE_STOPS)
        if body is None:
            continue
        keys = _top_keys(body)
        if m.group("export") and not any(k == _FETCH_MEMBER for k, *_ in keys):
            continue
        opaque = opaque or any(k == _SPREAD for k, *_ in keys)
        out.append((start, body, keys))
    return out, opaque


def _expressions(arg: str) -> str:
    """The code of an argument without its literal text: a template's
    interpolations, or the argument with its quoted strings emptied (a
    ``"body.json"`` names no request data)."""
    arg = arg.lstrip()
    if arg.startswith("`"):
        return " ".join(_INTERPOLATION_RE.findall(arg))
    return _QUOTED_RE.sub('""', arg)


_ARGUMENT_STOPS = re.compile(r"[()\[\]{},\n]")
_CALL_STOPS = re.compile(r"[()\[\]{}]")


def _first_argument(src: str, i: int, reach: int = 300) -> str:
    """The text from ``i`` to the first top-level comma or closing bracket
    on the line, at most ``reach`` characters and ``MAX_CALL_STOPS``
    brackets."""
    end = min(len(src), i + reach)
    depth = 0
    for m in islice(_ARGUMENT_STOPS.finditer(src, i, end), MAX_CALL_STOPS):
        c = m.group()
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return src[i:m.start()]
            depth -= 1
        elif depth == 0 or c == "\n":
            return src[i:m.start()]
    return src[i:end]


def _lint_serve_object(src: str, start: int, body: str, keys: list, add) -> None:
    for key, kind, at, end in keys:
        value = body[at + len(key):end].lstrip()
        value = value[1:].strip() if value.startswith(":") else value
        pos = start + at
        if key == _PORT_KEY and kind == ":":
            port = re.match(r"(\d+)\b", value)
            if port and int(port.group(1)) != APP_PORT:
                add("server.port", FAIL, pos,
                    f"the sandbox delivers to $PORT ({APP_PORT}); use Number(process.env.PORT) "
                    "or leave port out")
        elif key == _HOSTNAME_KEY and kind == ":" and _LOOPBACK_RE.match(value):
            add("server.bind", FAIL, pos,
                "the splice delivers to the namespace address, never loopback; listen on 0.0.0.0")
        elif key == _DEVELOPMENT_KEY and kind == ":":
            if _DEV_ON_RE.match(value):
                add("server.dev-mode", FAIL, pos, _DEV_MODE)
            elif not _DEV_OFF_RE.match(value):
                add("server.dev-mode", WARN, pos, _DEV_MODE_MAYBE)
        elif key == _DEVELOPMENT_KEY:
            add("server.dev-mode", WARN, pos, _DEV_MODE_MAYBE)
        elif key == _ERROR_MEMBER and kind:
            _lint_error_member(src, start + at, body[at:end], add)


_NOT_PARAMETERS = frozenset({"async", "function"})
_PARAM_RE = re.compile(r"error\s*(?::\s*(?:async\s+)?(?:function\s*[\w$]*\s*)?)?\(?\s*([A-Za-z_$][\w$]*)")


def _lint_error_member(src: str, start: int, member: str, add) -> None:
    """The serve object's ``error`` member: an answer that carries its
    parameter sends the error's own text to the page."""
    p = _PARAM_RE.match(member)
    if p is None or p.group(1) in _NOT_PARAMETERS:
        return
    name = re.compile(r"(?<![\w$.])" + re.escape(p.group(1)) + r"\b(?!\s*:)")
    for m in islice(_hits(_ANSWER_RE, member), MAX_READS):
        if not _is_answer(member, m):
            continue
        if name.search(_call_rest(member, m.end())):
            add("server.error-leak", WARN, start + m.start(), _ERROR_LEAK)


def _call_rest(text: str, i: int, reach: int = 2000) -> str:
    """The arguments of the call whose ``(`` ends right before ``text[i]``,
    up to its closing parenthesis, at most ``reach`` characters and
    ``MAX_CALL_STOPS`` brackets."""
    end = min(len(text), i + reach)
    depth = 0
    for m in islice(_CALL_STOPS.finditer(text, i, end), MAX_CALL_STOPS):
        if m.group() in "([{":
            depth += 1
        elif depth == 0:
            return text[i:m.start()]
        else:
            depth -= 1
    return text[i:end]


# What closes a destructuring that reads the role: an assignment, or a
# parameter list (``({ role }: Claim) =>``, ``function rankOf({ role })``).
_DESTRUCTURED_RE = re.compile(r"\}\s*(?:=(?![=>])|(?::\s*[\w$.<>\[\]|, ]{1,80}?)?\s*[),])")


def _reads_role(src: str) -> bool:
    """Whether a server file reads the role in a claim: ``x.role``,
    ``x["role"]``, or a destructuring ``{ …, role, … }`` assigned or in a
    parameter list (bounded at 400 characters each side, evaluated at each
    ``role`` word instead of each brace)."""
    for m in _hits(_ROLE_WORD_RE, src, "word"):
        i = m.start()
        before = src[max(0, i - ROLE_READ_REACH):i]
        if before.rstrip().endswith("."):
            return True
        if re.search(r"\[\s*[\"']$", before) and re.match(r"role[\"']\s*\]", src[i:i + 12]):
            return True
        opened = before.rfind("{")
        if opened < 0 or "}" in before[opened:]:
            continue
        after = src[i:i + ROLE_READ_REACH + 4]
        closed = after.find("}")
        if closed >= 0 and "{" not in after[:closed] and _DESTRUCTURED_RE.match(after, closed):
            return True
    return False


def _lint_server(texts: dict[str, str], d: Declared) -> list[Finding]:
    out: list[Finding] = []
    stripped = {file: strip_comments(raw, False) for file, raw in texts.items()}
    joined = "\n".join(stripped.values())
    first_write: tuple[str, int] | None = None
    for file, src in stripped.items():
        rows = _Rows(file, src)
        add = rows.add

        starts = [m.start() for m in _hits(_IMPORT_START_RE, src)]
        for n, at in enumerate(starts):
            # Each statement is read up to the next one, so no character is
            # read twice.
            end = min(starts[n + 1] if n + 1 < len(starts) else len(src), at + IMPORT_REACH)
            s = _IMPORT_STATEMENT_RE.match(src, at, end)
            if s is not None:
                _lint_import(s.group(1), at, add)
        for m in _hits(_IMPORT_CALL_RE, src, "ident"):
            _lint_import(m.group(1), m.start(), add)
        objects, opaque = _serve_objects(src)
        for start, body, keys in objects:
            _lint_serve_object(src, start, body, keys, add)
        if opaque:
            # The serve object is built elsewhere: any development key in
            # the file may be its own.
            read = [(start, start + len(body)) for start, body, _keys in objects]
            for m in _hits(_DEV_KEY_RE, src, "ident"):
                if not any(a <= m.start() < b for a, b in read) and not _DEV_FALSE_RE.match(src, m.end()):
                    add("server.dev-mode", WARN, m.start(), _DEV_MODE_MAYBE)
        for m in _hits(_DB_RE, src):
            path = m.group(1)
            if path == ":memory:" or path.startswith(DATA_DIR):
                continue
            add("server.data-dir", FAIL, m.start(),
                f"{path}: the only writable place is $OTODOCK_DATA_DIR ({DATA_DIR}); open "
                "`${process.env.OTODOCK_DATA_DIR}/app.db`")
        for m in _hits(_EGRESS_RE, src):
            host = m.group(2).split("@")[-1].split(":")[0].lower()
            if "${" in host or host in d.egress:
                continue
            add("server.egress", FAIL, m.start(),
                f"{host} is not in app.json egress; the sandbox denies every host not declared "
                "and approved")
        for m in _hits(_EGRESS_ROUTE_RE, src):
            if m.group(1) not in d.egress:
                add("server.egress", FAIL, m.start(),
                    f"{m.group(1)} is not in app.json egress. The /egress/ route refuses it.")
        for m in _hits(_MIGRATION_RE, src, "word"):
            add("server.migration", WARN, m.start(),
                "migrations are additive only: the previous release keeps serving this database "
                "during a deploy")
        for m in _hits(_WRITE_RE, src):
            path = m.group(1)
            if path.startswith((DATA_DIR, "/tmp")):
                continue
            add("server.write-outside-data", WARN, m.start(),
                f"{path}: /app is read-only; only $OTODOCK_DATA_DIR and /tmp are writable")
        _lint_security(src, rows)
        if first_write is None:
            for w in _hits(_WRITE_ROUTE_RE, src):
                if not w.group("method") and _after_word(src, w.start()):
                    continue
                first_write = (file, _Lines(src).at(w.start()))
                break
        out.extend(rows.rows)
    if first_write and not any(_reads_role(src) for src in stripped.values()):
        out.append(Finding("server.write-without-role", WARN, first_write[0], first_write[1],
                           "A route changes data but the server never reads the role in the "
                           "viewer claim. Rank claim.role before each write."))
    if joined and "/_health" not in joined:
        first = next(iter(texts))
        out.append(Finding("server.health", WARN, first, 1,
                           "no /_health route; the supervisor accepts any answer under 500, "
                           "an explicit route is clearer"))
    # A route per handler, or the prefix alone as a string (a generic
    # `startsWith("/_handler/")` dispatcher) answers every declared name.
    generic = any(re.search(r"[\"'`]/_handler/[\"'`]", src) for src in stripped.values()
                  if "/_handler/" in src)
    for name in sorted(d.handlers):
        if f"/_handler/{name}" not in joined and not generic:
            first = next(iter(texts))
            out.append(Finding("server.handler-route", FAIL, first, 1,
                               f"handler '{name}' is declared in app.json but the server has no "
                               f"route for POST /_handler/{name}"))
    return out


def _lint_import(mod: str, pos: int, add) -> None:
    base = mod.split("/")[0]
    if mod.startswith((".", "/")) or base in ("bun", "node") or mod.startswith(("bun:", "node:")) \
            or base in NODE_BUILTINS:
        return
    add("server.npm-import", FAIL, pos,
        f"'{mod}' needs node_modules, which a release never ships; use Bun's built-ins "
        "(Bun.serve, bun:sqlite, fetch) or the node: modules")


_EVAL = "eval and new Function run text as code. Parse the input instead."
_SQL = "SQL is built from request data. Use ? placeholders and pass the values."
_SPAWN = ("The server starts a program. Keep request data out of its arguments, or use Bun's own "
          "functions.")
_ERROR_LEAK = "The answer carries the error's own text. Send a plain message and log the details."
_FILE_PATH = ("A file path is built from request data. Map the value to a fixed name, never join "
              "it into a path.")


_PLUS_RE = re.compile(r"\s*\+")


def _sql_code(src: str, i: int) -> str:
    """The code of a SQL call's first argument that opens at ``src[i]``: a
    template's interpolations and what a template or a quoted string is
    joined with by ``+``; empty for anything else (a plain string, a name)."""
    quote = src[i:i + 1]
    if quote not in ("`", "\"", "'"):
        return ""
    end = _string_end(src, i, quote, i + 4000)
    code = _expressions(src[i:end] if end > 0 else src[i:i + 4000]) if quote == "`" else ""
    plus = _PLUS_RE.match(src, end, end + 200) if end > 0 else None
    if plus:
        code += " " + _expressions(_first_argument(src, plus.end(), 200))
    return code


# How far back from an error's text the call that answers with it may open,
# and how many calls around the text are looked at.
LEAK_REACH = 300
MAX_CALL_LEVELS = 8
_ANSWER_NAME_RE = re.compile(r"(?:new\s+Response|Response\.json|(?:^|[^\w$.])json)\s*$")


def _inside_answer(src: str, pos: int) -> bool:
    """Whether ``pos`` sits in the arguments of an answer call that opens at
    most ``LEAK_REACH`` characters before it, earlier in the same statement:
    the call may wrap lines and hold other calls (``json({`` then ``error:
    String(err)`` on the next line)."""
    start = max(0, pos - LEAK_REACH)
    depth = levels = 0
    k = pos
    for _ in range(MAX_CALL_STOPS):
        j = max(src.rfind("(", start, k), src.rfind(")", start, k))
        if j < 0:
            return False
        if src[j] == ")":
            depth += 1
        elif depth:
            depth -= 1
        else:
            if _ANSWER_NAME_RE.search(src[max(0, j - 32):j]):
                return True
            levels += 1
            if levels >= MAX_CALL_LEVELS:
                return False
        k = j
    return False


def _lint_security(src: str, rows: _Rows) -> None:
    """The security rules of one server file (comment-stripped): code that
    runs text, SQL, a program or a file path built from request data, and an
    answer that carries an error's own text."""
    for pattern, guard in ((_EVAL_RE, "ident"), (_NEW_FUNCTION_RE, "word")):
        for m in _hits(pattern, src, guard):
            if not rows.add("server.eval", WARN, m.start(), _EVAL):
                break
    for m in islice(_hits(_SQL_CALL_RE, src), MAX_READS):
        if _REQUEST_DATA_RE.search(_sql_code(src, m.end())) \
                and not rows.add("server.sql-concat", WARN, m.start(), _SQL):
            break
    imports_shell = any("$" in m.group(1) for m in _hits(_BUN_SHELL_IMPORT_RE, src))
    shell = [(_BARE_SHELL_RE, "ident")] if imports_shell else []
    for pattern, guard in [(_SPAWN_RE, "ident"), (_CHILD_PROCESS_RE, "word"), *shell]:
        for m in _hits(pattern, src, guard):
            if not rows.add("server.spawn", WARN, m.start(), _SPAWN):
                break
    for m in islice(_hits(_LEAK_RE, src), MAX_READS):
        if not m.group("own") and _after_ident(src, m.start()):
            continue
        if _inside_answer(src, m.start()) \
                and not rows.add("server.error-leak", WARN, m.start(), _ERROR_LEAK):
            break
    for m in islice(_hits(_FILE_CALL_RE, src, "word"), MAX_READS):
        if _REQUEST_DATA_RE.search(_expressions(_first_argument(src, m.end()))) \
                and not rows.add("server.file-path", WARN, m.start(), _FILE_PATH):
            break


# ── entry points ────────────────────────────────────────────────────────────


def lint_tree(source_dir: Path, files: list[tuple[str, Path]], d: Declared) -> list[Finding]:
    """The findings for a working tree, ``files`` being the release walker's
    ``(relative path, file)`` pairs. Sorted by file and line, FAIL first."""
    out: list[Finding] = []
    client_texts: dict[str, str] = {}
    server_texts: dict[str, str] = {}
    assets = {rel[len("client/"):] for rel, _ in files if rel.startswith("client/")}
    server_files = [rel for rel, _ in files if rel.startswith("server/")]
    app_json = ""
    for rel, _path in files:
        data = _read(source_dir, rel)
        # A source is read whatever bytes it holds (a stray byte must not hide
        # a key); any other file only when it is text.
        if rel.startswith("client/") and rel.lower().endswith(CLIENT_SUFFIXES):
            text: str | None = client_texts.setdefault(rel, data.decode("utf-8", "replace"))
        elif rel.startswith("server/") and rel.lower().endswith(SERVER_SUFFIXES):
            text = server_texts.setdefault(rel, data.decode("utf-8", "replace"))
        else:
            text = _text_or_none(data)
            if rel == APP_JSON:
                app_json = text or ""
        if text is not None:
            out.extend(_lint_secrets(text, rel, secret_rule(rel)))
    for rel, text in client_texts.items():
        out.extend(_lint_client(text, rel, d, assets))
    if server_files and not any(e in server_files for e in SERVER_ENTRIES):
        out.append(Finding("server.no-entry", FAIL, "server/", 1,
                           "server/ has files but no index.ts or index.js; the platform would "
                           "run nothing and deploy a static app"))
    if server_texts:
        out.extend(_lint_server(server_texts, d))
    out.extend(_lint_words_tree(client_texts, server_texts, app_json))
    out.extend(_lint_brand_tree(client_texts))
    if app_json:
        out.extend(_lint_manifest(app_json, d))
    out.sort(key=lambda f: (f.severity != FAIL, f.file, f.line))
    return out


def _lint_brand_tree(client_texts: dict[str, str]) -> list[Finding]:
    """The brand's rows: ``client/brand.css`` and every page's
    ``<style id="brand">``."""
    from services.apps import app_lint_brand as brand
    out: list[Finding] = []
    for rel, text in client_texts.items():
        if rel == brand.BRAND_FILE:
            src = strip_comments(text, False)
            out.extend(brand.lint_brand(src, 0, rel, src))
        elif _is_page(rel) and _BRAND_HINT in text:
            out.extend(_page_brand(strip_comments(text, True), rel))
    return _kit_missing_once(out)


def _kit_missing_once(rows: list[Finding]) -> list[Finding]:
    """``rows`` with the missing kit said once (each brand block finds it)."""
    from services.apps.app_lint_brand import KIT_MISSING_RULE
    first = next((f for f in rows if f.rule == KIT_MISSING_RULE), None)
    return [f for f in rows if f.rule != KIT_MISSING_RULE or f is first]


# A page that names no brand holds no brand style (a cheap test before the
# page is read again).
_BRAND_HINT = "brand"


def _page_brand(src: str, file: str) -> list[Finding]:
    from services.apps import app_lint_brand as brand
    out: list[Finding] = []
    for a, b in brand.brand_style_spans(src):
        out.extend(brand.lint_brand(src[a:b], a, file, src))
    return out


_ACTION_FLOOR = ("The button {id} acts for the agent, and everyone who opens the app can press it. "
                 "Give it a min_role, such as editor.")


def _lint_manifest(raw: str, d: Declared) -> list[Finding]:
    """The manifest's own warnings: a description that is no short text, a
    button that acts for the agent with no floor (at its ``id`` line)."""
    rows = _Rows(APP_JSON, raw)
    try:
        doc = json.loads(raw)
    except ValueError:
        return []
    if isinstance(doc, dict) and DESCRIPTION_KEY in doc:
        at = raw.find(f'"{DESCRIPTION_KEY}"')
        value = doc[DESCRIPTION_KEY]
        if not isinstance(value, str):
            rows.add("app.description", WARN, max(at, 0), "The description is not text.")
        elif len(value) > MAX_DESCRIPTION_CHARS:
            rows.add("app.description", WARN, max(at, 0),
                     f"The description runs past {MAX_DESCRIPTION_CHARS} characters. Keep it to a "
                     "sentence or two.")
    for action_id in d.unfloored:
        m = re.search(r'"id"\s*:\s*"' + re.escape(action_id) + '"', raw)
        rows.add("app.action-floor", WARN, m.start() if m else 0, _ACTION_FLOOR.format(id=action_id))
    return rows.rows


def _is_page(rel: str) -> bool:
    return rel.lower().endswith((".html", ".htm"))


def _lint_words_tree(client_texts: dict[str, str], server_texts: dict[str, str],
                     app_json: str) -> list[Finding]:
    """The words rule over a tree, within its reading budget: the readers'
    audience and language come from ``client/index.html``."""
    from services.apps import app_lint_words as words
    out: list[Finding] = []
    first = words.read_page(client_texts.get(INDEX_PAGE, ""))
    developers = first.audience == words.AUDIENCE_DEVELOPERS
    english = words.is_english(first.lang)
    budget, runs = words.MAX_TREE_CHARS, words.MAX_TREE_TEXTS
    for rel, text in [*client_texts.items(), *server_texts.items()]:
        if budget <= 0 or runs <= 0:
            break
        part = text[:min(words.MAX_FILE_CHARS, budget)]
        budget -= len(part)
        local = english
        if rel.startswith("server/"):
            texts = words.server_texts(strip_comments(part, False))
        elif _is_page(rel):
            page = first if rel == INDEX_PAGE else words.read_page(part)
            local = words.is_english(page.lang) if page.lang else english
            texts = page.texts + words.script_texts(script_view(strip_comments(part, True)))
        elif rel.lower().endswith((".js", ".mjs")):
            texts = words.script_texts(strip_comments(part, False))
        else:
            continue
        texts = texts[:runs]
        runs -= len(texts)
        out.extend(words.lint_words(texts, rel, part, developers=developers, english=local))
    if app_json:
        out.extend(words.lint_words(words.manifest_texts(app_json), APP_JSON, app_json,
                                    developers=developers, english=english))
    return out


def lint_html(content: str, actions_json: str, file: str = "app.html") -> list[Finding]:
    """A single-file app's page: the client rules against its own declared
    actions. Sorted by line, FAIL first."""
    from services.apps import app_lint_words as words
    d = declared_from_actions(actions_json)
    out = (_lint_secrets(content, file, "client.secret") + _lint_client(content, file, d, None)
           + [Finding("app.action-floor", WARN, file, 1, _ACTION_FLOOR.format(id=action_id))
              for action_id in d.unfloored])
    part = content[:words.MAX_FILE_CHARS]
    page = words.read_page(part)
    texts = page.texts + words.script_texts(script_view(strip_comments(part, True)))
    out += words.lint_words(texts[:words.MAX_TREE_TEXTS], file, part,
                            developers=page.audience == words.AUDIENCE_DEVELOPERS,
                            english=words.is_english(page.lang))
    if _BRAND_HINT in content:
        out += _kit_missing_once(_page_brand(strip_comments(content, True), file))
    out.sort(key=lambda f: (f.severity != FAIL, f.line))
    return out


def _text_or_none(data: bytes) -> str | None:
    """A file's text when it is UTF-8 without a NUL, else None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def _read(source_dir: Path, rel: str) -> bytes:
    """A file of the tree, read without following a link (a link or a file
    that went away reads empty: the copy refuses it anyway)."""
    from services.apps import releases
    try:
        return releases.read_tree_file(source_dir, rel, max_size=releases.MAX_RELEASE_FILE_BYTES)
    except OSError:
        return b""


# The warning families that run long on one app (a page of prose, a
# palette, a brand measured pair by pair): eight rows each, then one row that
# says how many more there are. A fail is never cut.
CAPPED_FAMILIES = {"words": "words.", "palette": "client.palette", "brand": "brand."}
FAMILY_ROWS = 8


def _family(rule: str) -> str:
    for name, prefix in CAPPED_FAMILIES.items():
        if rule == prefix or (prefix.endswith(".") and rule.startswith(prefix)):
            return name
    return ""


def summary(findings: list[Finding]) -> dict:
    """The hook answer's shape: counts and the rows, each long warning family
    cut to ``FAMILY_ROWS`` rows and one row naming the rest."""
    fails = [f for f in findings if f.severity == FAIL]
    rows: list[Finding] = []
    shown: dict[str, int] = {}
    hidden: dict[str, list[Finding]] = {}
    for f in findings:
        family = "" if f.severity == FAIL else _family(f.rule)
        if not family:
            rows.append(f)
        elif shown.get(family, 0) < FAMILY_ROWS:
            shown[family] = shown.get(family, 0) + 1
            rows.append(f)
        else:
            hidden.setdefault(family, []).append(f)
    for family, rest in hidden.items():
        first = rest[0]
        rows.append(Finding(f"{CAPPED_FAMILIES[family].rstrip('.')}.more", WARN, first.file,
                            first.line, f"{len(rest)} more like these. Fix the ones above, then "
                            "check again."))
    return {
        "problems": len(fails),
        "warnings": len(findings) - len(fails),
        "findings": [f.as_dict() for f in rows],
    }
