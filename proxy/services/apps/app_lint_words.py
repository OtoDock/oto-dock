"""The words of an app a person reads, run by ``app_lint``: ``words.*``
warnings on the reader rules the platform's own screens keep.

What is read: the page's text and the attributes a person reads, the
strings a client script puts on screen, the messages a server answers with
and the notifications it sends, and the labels and secret descriptions of
``app.json``. What is skipped: code (``code``, ``pre`` and their kin, a
quoted path, an address), what only the platform reads (a handler's route,
the health answer, the log), an element marked ``data-plain="off"`` and a
line under a ``plain-words: allow`` comment. The rules: no dash, no
semicolon, no arrow naming a place, no sentence over 30 words, no word for
how software works inside, no raw value (``null``, a key, a timestamp). A
page in another language, or a string in another alphabet, gets the raw
value rule only: in Greek a semicolon is the question mark. Every finding is
a warning, never a refusal: the client's own vocabulary can make a word
right.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from itertools import islice

from services.apps.app_lint import (
    MAX_BODY_SCANS,
    MAX_CANDIDATES,
    MAX_READS,
    WARN,
    Finding,
    _after_ident,
    _body_after,
    _call_rest,
    _hits,
    _Lines,
    _needs,
    _Rows,
    _string_end,
)

# The release gate's words for how the platform works inside, split for
# apps: these mean nothing to an app's reader.
APP_WORDS = (
    "egress", "daemon", "endpoint", "handler", "payload", "regex", "mutex", "stdout", "stderr",
    "cgroup", "namespace", "userns", "asyncio", "coroutine", "mixin", "runtime", "sentinel",
    "heuristic", "idempotent",
)
# Everyday words in the domains apps serve (a floor plan, a smart socket, a
# cargo manifest): never flagged in an app.
EVERYDAY_WORDS = (
    "carve", "floor", "hook", "broker", "probe", "socket", "adapter", "tombstone", "manifest",
    "pool",
)
# Words a developer reads with good reason: flagged unless the page says
# its readers are developers.
DEVELOPER_WORDS = ("API", "JSON", "HTTP", "MCP", "server", "websocket", "token", "slug", "lane",
                   "id")
AUDIENCE_META = "otodock-audience"
AUDIENCE_DEVELOPERS = "developers"
PLAIN_ATTRIBUTE = "data-plain"
PLAIN_OFF = "off"
ALLOW_COMMENT = "plain-words: allow"
# How much text the rule reads: its rows are capped in the answer anyway.
MAX_FILE_CHARS = 256 * 1024
MAX_TREE_CHARS = 1024 * 1024
MAX_TREE_TEXTS = 20000
SNIPPET_CHARS = 60
# How far either way an answer's own line is read for the health route.
LINE_REACH = 2000
LONG_SENTENCE = 30
# One text is read up to this length, and a token longer than a word ever is
# reads as code: the code patterns then run on short tokens only.
MAX_TEXT_CHARS = 2000
MAX_TOKEN_CHARS = 80

_APP_RE = re.compile(r"\b(" + "|".join(APP_WORDS) + r")s?\b", re.I)
_DEVELOPER_RE = re.compile(r"\b(" + "|".join(DEVELOPER_WORDS) + r")s?\b", re.I)
_SKIPPED_ELEMENTS = frozenset({"script", "style", "code", "pre", "kbd", "samp", "var", "template",
                               "textarea", "svg", "math"})
_VOID_ELEMENTS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                            "meta", "param", "source", "track", "wbr"})
# Elements that sit inside a sentence: their text joins the sentence around
# them.
_INLINE_ELEMENTS = frozenset({"a", "abbr", "b", "bdi", "bdo", "cite", "data", "dfn", "em", "i",
                              "mark", "q", "s", "small", "span", "strong", "sub", "sup", "time",
                              "u", "wbr", "br", "label"})
_READ_ATTRIBUTES = ("placeholder", "title", "aria-label", "alt")
_BUTTON_INPUTS = frozenset({"button", "submit", "reset"})
_HTML_TAG = "html"
_META_TAG = "meta"
_INPUT_TAG = "input"
_ENGLISH_RE = re.compile(r"en(?:-|$)", re.I)

# Code a sentence may quote keeps its own spelling (ported from the
# dashboard's plain-sentences test): a backticked span, KEY=value, an
# address, a path, a dotted name, a snake_case name, a flag, a key
# combination, a name with a digit; an email first.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CODE_RE = re.compile("|".join((
    r"`[^`]*`",
    r"[^\s=]+=[^\s=]+",
    r"\b[a-z][a-z0-9+.-]*://[^\s)\]]+",
    r"[\w.~*-]*/[\w.~*{}/-]+",
    r"\b\w+(?:\.\w+)+\b(?:\([^)]*\))?",
    r"\b[A-Za-z0-9]*_\w*",
    r"(?<!\w)--?[a-z][\w-]*",
    r"\b(?:Ctrl|Shift|Alt|Cmd)\+[\w+]+",
    r"\b[a-z][a-z-]*\d[\w.-]*",
)))
_CSS_RE = re.compile(r"(^|;)\s*[a-z-]+\s*:\s*[^;]+;")
_SENTENCE_LIKE_RE = re.compile(r"[A-Z][a-z]+ [a-z]+ [a-z]+")
_SELECTOR_RE = re.compile(r"\w\[[a-z-]+(=[^\]]*)?\]|\[[a-z-]+=|:not\(")
_DASH_RE = re.compile(r"[—–]|(?<=\s)-(?=\s)")
_ARROW_RE = re.compile(r"[A-Za-z]\s*(?:→|->)\s*[A-Z][a-z]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
# A raw value: null and its kin, text that opens like JSON (a template's
# ``{x} items`` does not), a snake_case key, an ISO timestamp, a status code.
_RAW_RE = re.compile(
    r"\b(?:null|undefined|NaN)\b|\[object Object\]|^\s*(?:\{\s*(?:\"|$)|\[\s*[{\"\d])"
    r"|\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|\banswered \d{3}\b")
_SNAKE_RE = re.compile(r"_")
_LETTERS_RE = re.compile(r"[^\W\d_]")

# Client script strings a person reads: what a script puts on screen.
_SCREEN_TEXT_RE = _needs(re.compile(
    r"\.(?:textContent|innerText|title|placeholder)\s*=\s*(?=[\"'`])"
    r"|setAttribute\s*\(\s*[\"'](?:aria-label|title|placeholder)[\"']\s*,\s*(?=[\"'`])"),
    "textContent", "innerText", ".title", "placeholder", "setAttribute")
# A server's answers: the message keys of an answer, and what a
# notification says.
_ANSWER_RE = _needs(re.compile(r"(?P<new>new\s+Response\s*\()|(?P<method>Response\.json\s*\()|json\s*\("),
                    "Response", "json")
_MESSAGE_KEY_RE = re.compile(r"\b(?:error|message|detail)\s*:\s*(?=[\"'`])")
_STATUS_4XX_RE = re.compile(r"\bstatus\s*:\s*[45]\d\d\b")
_NOTIFY_RE = _needs(re.compile(r"notifications\.create"), "notifications.create")
_NOTIFY_KEY_RE = re.compile(r"\b(?:title|body)\s*:\s*(?=[\"'`])")
_HANDLER_ROUTE_RE = _needs(re.compile(r"[\"'`]/_handler/"), "/_handler/")
_INTERPOLATION_RE = re.compile(r"\$\{[^{}`]{0,200}\}")

_RULES = {
    "words.dash": "A dash breaks this sentence: \"{s}\". Use a comma, a colon or a full stop.",
    "words.semicolon": "A semicolon joins two sentences: \"{s}\". Write two sentences.",
    "words.arrow": "An arrow names a place: \"{s}\". Name the place in words.",
    "words.long": "A sentence of {n} words: \"{s}\". Keep to one idea, 30 words at most.",
    "words.mechanism": ("The word \"{w}\" says how software works inside: \"{s}\". Say what the "
                        "person sees or does."),
    "words.raw": "A raw value shows to the person: \"{s}\". Show words or a formatted value.",
}


class _Page(HTMLParser):
    """The text of a page a person reads, by line: text joined across inline
    elements, the read attributes, the page's language and audience."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[tuple[str, int]] = []
        self.lang = ""
        self.audience = ""
        self._skip: list[list] = []
        self._buf: list[str] = []
        self._buf_line = 0

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        if text:
            self.texts.append((text, self._buf_line))
        self._buf = []

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if a.get("lang") and (tag == _HTML_TAG or not self.lang):
            self.lang = a["lang"]
        if tag == _META_TAG and a.get("name", "").lower() == AUDIENCE_META:
            self.audience = a.get("content", "").strip().lower()
        if self._skip:
            if tag == self._skip[-1][0] and tag not in _VOID_ELEMENTS:
                self._skip[-1][1] += 1
            return
        if tag not in _INLINE_ELEMENTS:
            self._flush()
        if tag in _SKIPPED_ELEMENTS or a.get(PLAIN_ATTRIBUTE, "").lower() == PLAIN_OFF:
            if tag not in _VOID_ELEMENTS:
                self._skip.append([tag, 1])
            return
        line = self.getpos()[0]
        for name in _READ_ATTRIBUTES:
            if a.get(name, "").strip():
                self.texts.append((a[name].strip(), line))
        if tag == _INPUT_TAG and a.get("type", "").lower() in _BUTTON_INPUTS and a.get("value", "").strip():
            self.texts.append((a["value"].strip(), line))

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag == self._skip[-1][0]:
                self._skip[-1][1] -= 1
                if not self._skip[-1][1]:
                    self._skip.pop()
            return
        if tag not in _INLINE_ELEMENTS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if not self._buf:
            # A run's line is the line of its first character that is no
            # space (the text after a tag often opens on a newline).
            body = data.lstrip()
            if not body:
                return
            self._buf_line = self.getpos()[0] + data.count("\n", 0, len(data) - len(body))
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def read_page(html: str) -> _Page:
    page = _Page()
    page.feed(html[:MAX_FILE_CHARS])
    page.close()
    return page


def _literal_at(src: str, i: int) -> str | None:
    """The text of the string literal that opens at ``src[i]``, a template's
    interpolations read as ``{x}``; None when it does not close nearby."""
    quote = src[i:i + 1]
    if quote not in ("\"", "'", "`"):
        return None
    end = _string_end(src, i, quote, i + 2000)
    if end < 0:
        return None
    body = src[i + 1:end - 1]
    return _INTERPOLATION_RE.sub("{x}", body) if quote == "`" else body


def _allowed_lines(raw: str) -> set[int]:
    """The lines a ``plain-words: allow`` comment opts out: its own line and
    the line under it."""
    out: set[int] = set()
    lines = _Lines(raw)
    start = 0
    while True:
        i = raw.find(ALLOW_COMMENT, start)
        if i < 0 or len(out) > MAX_CANDIDATES:
            return out
        line = lines.at(i)
        out |= {line, line + 1}
        start = i + len(ALLOW_COMMENT)


def script_texts(code: str) -> list[tuple[str, int]]:
    """What a client script puts on screen, from its comment-stripped code
    (an HTML page's scripts alone)."""
    out = []
    lines = _Lines(code)
    for m in _hits(_SCREEN_TEXT_RE, code):
        text = _literal_at(code, m.end())
        if text:
            out.append((text, lines.at(m.start())))
    return out


def server_texts(src: str) -> list[tuple[str, int]]:
    """The messages a server answers with and the notifications it sends,
    from its comment-stripped code; the handler routes and the health
    answer, which only the platform reads, left out."""
    skipped: list[tuple[int, int]] = []
    for m in islice(_hits(_HANDLER_ROUTE_RE, src), MAX_BODY_SCANS):
        brace = src.find("{", m.end(), m.end() + 300)
        if brace >= 0:
            skipped.append((brace, brace + len(_body_after(src, brace))))
    out = []
    lines = _Lines(src)

    def keep(pos: int) -> bool:
        if any(a <= pos < b for a, b in skipped):
            return False
        line_start = src.rfind("\n", max(0, pos - LINE_REACH), pos) + 1
        line_end = src.find("\n", pos, pos + LINE_REACH)
        return "/_health" not in src[max(line_start, pos - LINE_REACH):
                                     line_end if line_end >= 0 else pos + LINE_REACH]

    for m in islice(_hits(_ANSWER_RE, src), MAX_READS):
        if not (m.group("new") or m.group("method")) and _after_ident(src, m.start()):
            continue
        if not keep(m.start()):
            continue
        args = _call_rest(src, m.end())
        for k in _MESSAGE_KEY_RE.finditer(args):
            text = _literal_at(args, k.end())
            if text:
                out.append((text, lines.at(m.end() + k.end())))
        if m.group("new") and _STATUS_4XX_RE.search(args):
            text = _literal_at(args.lstrip(), 0)
            if text:
                out.append((text, lines.at(m.end())))
    for m in islice(_hits(_NOTIFY_RE, src), MAX_READS):
        window = src[m.end():m.end() + 1500]
        for k in _NOTIFY_KEY_RE.finditer(window):
            text = _literal_at(window, k.end())
            if text:
                out.append((text, lines.at(m.end() + k.end())))
    return out


def manifest_texts(raw: str) -> list[tuple[str, int]]:
    """The labels and secret descriptions ``app.json`` shows a person on
    the approval card and in the app's settings."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(doc, dict):
        return []
    found = []
    for a in doc.get("actions") or []:
        if isinstance(a, dict) and isinstance(a.get("label"), str):
            found.append(a["label"])
    for s in doc.get("secrets") or []:
        if isinstance(s, dict) and isinstance(s.get("description"), str):
            found.append(s["description"])
    out = []
    for text in found:
        at = raw.find(json.dumps(text, ensure_ascii=False)[1:-1])
        if at < 0:
            at = raw.find(json.dumps(text)[1:-1])
        out.append((text, raw.count("\n", 0, at) + 1 if at >= 0 else 1))
    return out


def _not_latin(text: str) -> bool:
    letters = _LETTERS_RE.findall(text)
    latin = sum(1 for c in letters if ord(c) < 0x250)
    return len(letters) - latin > latin


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS].rstrip() + "…"


def faults(text: str, *, developers: bool = False, english: bool = True) -> list[tuple[str, str]]:
    """The reader rules ``text`` breaks, as ``(rule, message)`` rows."""
    out: list[tuple[str, str]] = []
    text = " ".join(t if len(t) <= MAX_TOKEN_CHARS else "Code" for t in text[:MAX_TEXT_CHARS].split())
    s = _snippet(text)
    plain = _EMAIL_RE.sub("Code", text)
    if _RAW_RE.search(_CODE_RE.sub(lambda m: m.group() if _SNAKE_RE.search(m.group()) else "Code", plain)):
        out.append(("words.raw", _RULES["words.raw"].format(s=s)))
    if not english or _not_latin(text):
        return out
    if len(re.findall(r"[A-Za-z]{2,}", text)) < 2:
        return out
    if (_CSS_RE.search(text) and not _SENTENCE_LIKE_RE.search(text)) or _SELECTOR_RE.search(text):
        return out
    words = _CODE_RE.sub("Code", plain)
    if ";" in words:
        out.append(("words.semicolon", _RULES["words.semicolon"].format(s=s)))
    for d in _DASH_RE.finditer(words):
        before = words[:d.start()].rstrip()[-1:]
        after = words[d.end():].lstrip()[:1]
        if not (before.isdigit() and after.isdigit()):
            out.append(("words.dash", _RULES["words.dash"].format(s=s)))
            break
    if _ARROW_RE.search(words):
        out.append(("words.arrow", _RULES["words.arrow"].format(s=s)))
    sentences = _SENTENCE_SPLIT_RE.split(" ".join(words.split()))
    longest = max((len(x.split()) for x in sentences), default=0)
    if longest > LONG_SENTENCE:
        out.append(("words.long", _RULES["words.long"].format(n=longest, s=s)))
    for sentence in sentences:
        lead = len(sentence) - len(sentence.lstrip("\"'“‘([ "))
        for pattern in (_APP_RE,) if developers else (_APP_RE, _DEVELOPER_RE):
            for m in pattern.finditer(sentence):
                w = m.group()
                titled = w[:1].isupper() and any(c.islower() for c in w)
                if titled and m.start() > lead:
                    continue
                out.append(("words.mechanism", _RULES["words.mechanism"].format(w=w, s=s)))
                return out
    return out


def lint_words(texts: list[tuple[str, int]], file: str, raw: str, *,
               developers: bool, english: bool) -> list[Finding]:
    """The ``words.*`` rows of one file's ``texts`` (each with its line);
    ``raw`` is the file as written, where the opt-out comments sit."""
    rows = _Rows(file, raw)
    allowed = _allowed_lines(raw)
    for t, line in texts[:MAX_CANDIDATES]:
        if line in allowed:
            continue
        for rule, message in faults(t, developers=developers, english=english):
            rows.add_line(rule, WARN, line, message)
    return rows.rows


def is_english(lang: str) -> bool:
    return not lang or bool(_ENGLISH_RE.match(lang))
