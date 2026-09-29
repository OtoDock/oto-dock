"""Static checks of an app before it ships (APPS.md "Deploy pipeline").

The sandbox refuses a lot of ordinary web code without a word in the
proxy's log: a raw ``fetch`` the CSP blocks, ``localStorage`` that throws
at an opaque origin, a ``<form>`` the frame cannot submit, a server bound
to ``localhost`` that the splice never reaches. These rules name those
mistakes with the file, the line and the fix, in the words of the skill,
before the folder becomes a release. Text rules over comment-stripped
sources: a FAIL is a pattern that is unambiguous code and refuses the
deploy, a WARN is a judgement the author may override and rides in the
ack. A single-file app gets the client rules through ``lint_html``.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

CLIENT_SUFFIXES = (".html", ".htm", ".js", ".mjs", ".css")
SERVER_SUFFIXES = (".ts", ".js", ".mjs")
SERVER_ENTRIES = ("server/index.ts", "server/index.js")
APP_PORT = 3000
DATA_DIR = "/app/data"
# Bare module names Bun resolves without node_modules.
NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "crypto", "dns", "events", "fs",
    "http", "https", "net", "os", "path", "perf_hooks", "querystring", "readline",
    "stream", "string_decoder", "timers", "tls", "url", "util", "worker_threads", "zlib",
})
WIDGET_FEEDS = {"sessions": "sessions", "lanes": "sessions", "tasks": "tasks",
                "notifications": "notifications"}


@dataclass
class Finding:
    rule: str
    severity: str  # "fail" | "warn"
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


def declared_from_actions(actions_json: str) -> Declared:
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
    h = doc.get("handlers") if isinstance(doc.get("handlers"), dict) else {}
    d.handlers = (set(h.get("on_schedule") or {}) | set(h.get("on_trigger") or [])
                  | set(h.get("on_event") or {}))
    inbound = doc.get("inbound") if isinstance(doc.get("inbound"), dict) else {}
    d.handlers |= {spec.get("handler") for spec in inbound.values()
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
        d.handlers |= {spec.get("handler") for spec in inbound.values()
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


_NOT_NEWLINE = re.compile(r"[^\n]")
_JS_OPENERS = re.compile(r"[\"'`/]")
# Where a string's scan stops: an escape, its own quote, and for the two
# one-line quotes a raw newline.
_STRING_STOPS = {'"': re.compile(r'[\\"\n]'), "'": re.compile(r"[\\'\n]"),
                 "`": re.compile(r"[\\`]")}


def _blank(seg: str) -> str:
    return _NOT_NEWLINE.sub(" ", seg)


def _string_end(text: str, i: int, quote: str) -> int:
    """The index after the string that opens at ``text[i]``, or -1 when it
    never closes (a ``"`` or ``'`` string also ends, unclosed, at a raw
    newline; a backslash escapes any one character)."""
    stops = _STRING_STOPS[quote]
    j = i + 1
    while True:
        m = stops.search(text, j)
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
    n = len(text)
    out: list[str] = []
    last = i = 0
    # A scan that failed from one opener fails from every later opener of
    # the same kind before the point it reached (the escapes parse alike
    # from there), so each failure is paid once.
    failed_until = {'"': -1, "'": -1, "`": -1, "/*": -1}
    while True:
        m = _JS_OPENERS.search(text, i)
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
_HANDLER_ATTR = re.compile(r"\son[a-z]+\s*=\s*([\"'])", re.I)


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
        close = src.find(m.group(1), m.end())
        if close < 0:
            break
        ranges.append((m.end(), close))
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


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ── the client rules ────────────────────────────────────────────────────────

# (rule, severity, pattern, message, script_only) applied to every client
# source; a ``script_only`` rule reads an HTML page's scripts alone.
_CLIENT_PATTERNS: list[tuple[str, str, re.Pattern, str, bool]] = [
    ("client.raw-fetch", "fail",
     re.compile(r"(?<![\w.$])fetch\s*\(|\bXMLHttpRequest\b|\bEventSource\s*\(|new\s+WebSocket\s*\(|navigator\.sendBeacon\s*\("),
     "the sandbox blocks it; use otodock.fetch / otodock.ws: they add the viewer token, "
     "wait for a starting server and reconnect",
     True),
    ("client.storage", "fail",
     re.compile(r"\b(localStorage|sessionStorage|indexedDB)\b"),
     "throws at an opaque origin; keep state in the app's server or in otodock.state",
     True),
    ("client.storage", "warn",
     re.compile(r"document\.cookie"),
     "always empty at an opaque origin; the frame has no cookies",
     True),
    ("client.form", "fail",
     re.compile(r"<form[\s>]", re.I),
     "the frame has no allow-forms, so a submit is blocked before any script runs; "
     "wire the button's click and the input's Enter in script",
     False),
    ("client.modal", "fail",
     re.compile(r"(?<![\w.$])(confirm|prompt)\s*\("),
     "blocked without allow-modals: confirm() is always false and prompt() always null, "
     "a dead branch; use an in-page control",
     True),
    ("client.modal", "warn",
     re.compile(r"(?<![\w.$])alert\s*\("),
     "blocked without allow-modals; the message is never seen",
     True),
    ("client.popup", "warn",
     re.compile(r"window\.open\s*\("),
     "blocked without allow-popups; a plain <a href> is bridged out by the runtime",
     True),
    ("client.navigation", "fail",
     re.compile(r"\blocation\.(assign|replace|reload)\s*\(|\blocation\.href\s*=(?!=)|(?<![\w.$])location\s*=(?!=)|window\.location\s*=(?!=)"),
     "the host blanks a page that navigates or reloads itself; change the DOM instead "
     "(location.hash is fine)",
     True),
    ("client.navigation", "fail",
     re.compile(r"<meta[^<>]+http-equiv\s*=\s*[\"']?refresh", re.I),
     "a meta refresh navigates the frame, which the host blanks",
     False),
    ("client.navigation", "warn",
     re.compile(r"history\.(pushState|replaceState)\s*\("),
     "throws at an opaque origin",
     True),
    ("client.worker", "fail",
     re.compile(r"new\s+(Shared)?Worker\s*\(|navigator\.serviceWorker"),
     "a worker script cannot load from an opaque origin",
     True),
    ("client.device-api", "warn",
     re.compile(r"navigator\.clipboard|\bNotification\.requestPermission|new\s+Notification\s*\(|navigator\.geolocation"),
     "the frame delegates no device permissions (allow=\"\"); a selectable <code> for "
     "copying, notifications.create for a notice",
     True),
    ("client.module", "warn",
     re.compile(r"type\s*=\s*[\"']module[\"']|(?<![\w.$])import\s*\(|\bcrossorigin\b", re.I),
     "module and cross-origin loads need CORS the asset route does not send; use a plain script",
     False),
    ("client.base", "warn",
     re.compile(r"<base\b", re.I),
     "base-uri 'none': the tag is ignored",
     False),
    ("client.secret", "fail",
     re.compile(r"\bsk-[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{36}\b|\bxoxb-\d+-"),
     "looks like a credential; nothing secret in client/, its files are served by content address",
     False),
    ("client.external-resource", "fail",
     re.compile(r"url\(\s*[\"']?(https?:)?//|@import\s+[\"'(]*\s*(https?:)?//", re.I),
     "the CSP allows the platform's own origin only; ship the file in client/ or inline it",
     False),
]

_TAG_RE = re.compile(r"<(script|link|img|video|audio|source|iframe|object|embed|a)\b([^<>]*)>", re.I)
_ATTR_RE = re.compile(r"\b(src|href|data|poster)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)
_EXTERNAL_RE = re.compile(r"^(https?:)?//", re.I)
_FEED_RE = re.compile(r"otodock\.feed\s*\(\s*[\"']([A-Za-z_]+)[\"']")
_ACTION_RE = re.compile(r"otodock\.action\s*\(\s*[\"']([\w-]+)[\"']")
_METHOD_RE = re.compile(r"otodock\.platform\s*\(\s*[\"']([\w.]+)[\"']")
_WIDGET_RE = re.compile(r"widget\s*:\s*[\"'](\w+)[\"']")
_ONCLOSE_RE = re.compile(r"\.onclose\s*=\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>|\w+\s*=>)\s*\{")
_TAILWIND_CLASS_RE = re.compile(r"class\s*=\s*[\"'][^\"']*\b(flex|grid|px-\d|py-\d|p-\d|mt-\d|mb-\d|gap-\d|text-(?:xs|sm|lg|xl)|rounded(?:-\w+)?|font-(?:medium|semibold|bold))\b")


def _body_after(text: str, start: int) -> str:
    """The brace-matched body that opens at ``text[start]``."""
    depth = 0
    for i in range(start, min(len(text), start + 20000)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:start + 2000]


def _lint_client(text: str, file: str, d: Declared, assets: set[str] | None,
                 single: bool) -> list[Finding]:
    """``assets`` is the set of files under client/ (a folder app); None
    for a single-file app, which ships nothing beside itself."""
    out: list[Finding] = []
    html = file.endswith((".html", ".htm"))
    src = strip_comments(text, html)

    def add(rule: str, severity: str, pos: int, message: str) -> None:
        out.append(Finding(rule, severity, file, _line(src, pos), message))

    code = script_view(src) if html else src
    for rule, severity, pattern, message, script_only in _CLIENT_PATTERNS:
        for m in pattern.finditer(code if script_only else src):
            add(rule, severity, m.start(), message)

    for tag in _TAG_RE.finditer(src):
        name = tag.group(1).lower()
        attrs = tag.group(2)
        if name in ("iframe", "object", "embed"):
            add("client.iframe", "fail", tag.start(),
                f"<{name}> is refused (frame-src and object-src are 'none')")
            continue
        if name == "a" and re.search(r"\bdownload\b", attrs, re.I):
            add("client.download", "warn", tag.start(),
                "the frame has no allow-downloads; the browser blocks the download")
        for attr in _ATTR_RE.finditer(attrs):
            value = (attr.group(2) or attr.group(3) or attr.group(4) or "").strip()
            low = value.lower()
            if not value or low.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                continue
            if _EXTERNAL_RE.match(value):
                if name == "a":
                    continue  # bridged out of the sandbox by the runtime
                add("client.external-resource", "fail", tag.start(),
                    f"<{name}> from {value.split('/')[2] if '//' in value else value}: the CSP "
                    "allows the platform's own origin only; ship the file in client/ or inline it")
                continue
            if "://" in low:
                add("client.external-resource", "fail", tag.start(),
                    f"<{name}> with a {low.split('://')[0]}: URL is refused by the CSP")
                continue
            if value.startswith("/"):
                if value.startswith("/ui-kit/"):
                    continue
                add("client.asset-path", "fail", tag.start(),
                    f"{value}: the document lives under /v1/apps/<id>/client/<sha>/, a "
                    "root-relative path 404s; use a relative path for an asset, "
                    "otodock.open for a page")
                continue
            if name == "a":
                continue
            rel = value.split("?")[0].split("#")[0]
            rel = rel[2:] if rel.startswith("./") else rel
            if rel.lower().endswith((".html", ".htm")):
                add("client.asset-path", "fail", tag.start(),
                    f"{value}: a page is never served as an asset")
            elif assets is None:
                add("client.asset-path", "fail", tag.start(),
                    f"{value}: a single-file app ships no assets; inline it or use /ui-kit/")
            elif rel not in assets:
                add("client.asset-path", "fail", tag.start(),
                    f"{value}: client/{rel} is not in the folder")

    for m in _FEED_RE.finditer(src):
        if m.group(1) not in d.feeds:
            add("client.undeclared-feed", "fail", m.start(),
                f"otodock.feed('{m.group(1)}') is used but not declared as a data_feed action "
                "in app.json; the host refuses it")
    for m in _ACTION_RE.finditer(src):
        if m.group(1) not in d.action_ids:
            add("client.undeclared-action", "fail", m.start(),
                f"otodock.action('{m.group(1)}') names no action in app.json")
    for m in _METHOD_RE.finditer(src):
        if m.group(1) not in d.methods:
            add("client.undeclared-method", "fail", m.start(),
                f"otodock.platform('{m.group(1)}') is not declared as a platform action in app.json")
    for m in _WIDGET_RE.finditer(src):
        w = m.group(1)
        if w in WIDGET_FEEDS and WIDGET_FEEDS[w] not in d.feeds:
            add("client.widget-feed", "fail", m.start(),
                f"the {w} widget binds the {WIDGET_FEEDS[w]} feed, which app.json does not declare")
        elif w == "connect" and "integrations.status" not in d.methods:
            add("client.widget-feed", "fail", m.start(),
                "the connect widget needs integrations.status declared as a platform action")
    for m in _ONCLOSE_RE.finditer(src):
        body = _body_after(src, m.end() - 1)
        if re.search(r"otodock\.ws\s*\(|setTimeout\s*\(", body):
            add("client.ws-reconnect", "warn", m.start(),
                "otodock.ws reconnects by itself; a reconnect of the page's own is redundant")
    if html and _TAILWIND_CLASS_RE.search(src) and "/ui-kit/tailwind.js" not in src:
        add("client.no-tailwind", "warn", 0,
            "Tailwind classes are used but /ui-kit/tailwind.js is not loaded")
    if html and re.match(r"\s*(<!doctype|<html)", src, re.I):
        if "/ui-kit/otodock-tokens.css" not in src:
            add("client.full-document", "warn", 0,
                "a full document skips the tokens CSS; add /ui-kit/otodock-tokens.css for "
                "the theme colours and .card/.btn")
        if not re.search(r"<meta[^<>]+name\s*=\s*[\"']viewport", src, re.I):
            add("client.full-document", "warn", 0,
                "a full document needs its own <meta name=\"viewport\"> for phones")
    if single:
        out = [f for f in out if f.severity == "fail"]
    return out


# ── the server rules ────────────────────────────────────────────────────────

_IMPORT_RE = re.compile(r"^\s*import\s+(?!type\b)[^'\"\n]*?from\s+[\"']([^\"']+)[\"']|require\s*\(\s*[\"']([^\"']+)[\"']", re.M)
_BIND_RE = re.compile(r"hostname\s*:\s*[\"'](localhost|127\.0\.0\.1|::1)[\"']")
_PORT_RE = re.compile(r"\bport\s*:\s*(\d+)\b")
_DB_RE = re.compile(r"Database\s*\(\s*[\"']([^\"']+)[\"']")
_EGRESS_RE = re.compile(r"fetch\s*\(\s*[`\"'](https?://([^/`\"'$\s]+))")
_MIGRATION_RE = re.compile(r"\bDROP\s+(TABLE|COLUMN)\b|\bRENAME\s+(TABLE|COLUMN|TO)\b", re.I)
_WRITE_RE = re.compile(r"(?:writeFile(?:Sync)?|appendFile(?:Sync)?|Bun\.write|mkdir(?:Sync)?)\s*\(\s*[\"'](/[^\"']*)")


def _lint_server(texts: dict[str, str], d: Declared) -> list[Finding]:
    out: list[Finding] = []
    joined = "\n".join(strip_comments(t, False) for t in texts.values())
    for file, raw in texts.items():
        src = strip_comments(raw, False)

        def add(rule: str, severity: str, pos: int, message: str, _f=file, _s=src) -> None:
            out.append(Finding(rule, severity, _f, _line(_s, pos), message))

        for m in _IMPORT_RE.finditer(src):
            mod = m.group(1) or m.group(2) or ""
            base = mod.split("/")[0]
            if mod.startswith((".", "/")) or base in ("bun", "node") or mod.startswith(("bun:", "node:")) \
                    or base in NODE_BUILTINS:
                continue
            add("server.npm-import", "fail", m.start(),
                f"'{mod}' needs node_modules, which a release never ships; use Bun's built-ins "
                "(Bun.serve, bun:sqlite, fetch) or the node: modules")
        for m in _BIND_RE.finditer(src):
            add("server.bind", "fail", m.start(),
                "the splice delivers to the namespace address, never loopback; listen on 0.0.0.0")
        for m in _PORT_RE.finditer(src):
            if int(m.group(1)) != APP_PORT:
                add("server.port", "fail", m.start(),
                    f"the sandbox delivers to $PORT ({APP_PORT}); use Number(process.env.PORT) "
                    "or leave port out")
        for m in _DB_RE.finditer(src):
            path = m.group(1)
            if path == ":memory:" or path.startswith(DATA_DIR):
                continue
            add("server.data-dir", "fail", m.start(),
                f"{path}: the only writable place is $OTODOCK_DATA_DIR ({DATA_DIR}); open "
                "`${process.env.OTODOCK_DATA_DIR}/app.db`")
        for m in _EGRESS_RE.finditer(src):
            host = m.group(2).split("@")[-1].split(":")[0].lower()
            if "${" in host or host in d.egress:
                continue
            add("server.egress", "fail", m.start(),
                f"{host} is not in app.json egress; the sandbox denies every host not declared "
                "and approved")
        for m in _MIGRATION_RE.finditer(src):
            add("server.migration", "warn", m.start(),
                "migrations are additive only: the previous release keeps serving this database "
                "during a deploy")
        for m in _WRITE_RE.finditer(src):
            path = m.group(1)
            if path.startswith((DATA_DIR, "/tmp")):
                continue
            add("server.write-outside-data", "warn", m.start(),
                f"{path}: /app is read-only; only $OTODOCK_DATA_DIR and /tmp are writable")
    if joined and "/_health" not in joined:
        first = next(iter(texts))
        out.append(Finding("server.health", "warn", first, 1,
                           "no /_health route; the supervisor accepts any answer under 500, "
                           "an explicit route is clearer"))
    # A route per handler, or the prefix alone as a string (a generic
    # `startsWith("/_handler/")` dispatcher) answers every declared name.
    generic = re.search(r"[\"'`]/_handler/[\"'`]", joined) is not None
    for name in sorted(d.handlers):
        if f"/_handler/{name}" not in joined and not generic:
            first = next(iter(texts))
            out.append(Finding("server.handler-route", "fail", first, 1,
                               f"handler '{name}' is declared in app.json but the server has no "
                               f"route for POST /_handler/{name}"))
    return out


# ── entry points ────────────────────────────────────────────────────────────


def lint_tree(source_dir: Path, files: list[tuple[str, Path]], d: Declared) -> list[Finding]:
    """The findings for a working tree, ``files`` being the release walker's
    ``(relative path, file)`` pairs. Sorted by file and line, FAIL first."""
    out: list[Finding] = []
    client_texts: dict[str, str] = {}
    server_texts: dict[str, str] = {}
    assets = {rel[len("client/"):] for rel, _ in files if rel.startswith("client/")}
    server_files = [rel for rel, _ in files if rel.startswith("server/")]
    for rel, _path in files:
        if rel.startswith("client/") and rel.lower().endswith(CLIENT_SUFFIXES):
            client_texts[rel] = _read(source_dir, rel)
        elif rel.startswith("server/") and rel.lower().endswith(SERVER_SUFFIXES):
            server_texts[rel] = _read(source_dir, rel)
    for rel, text in client_texts.items():
        out.extend(_lint_client(text, rel, d, assets, single=False))
    if server_files and not any(e in server_files for e in SERVER_ENTRIES):
        out.append(Finding("server.no-entry", "fail", "server/", 1,
                           "server/ has files but no index.ts or index.js; the platform would "
                           "run nothing and deploy a static app"))
    if server_texts:
        out.extend(_lint_server(server_texts, d))
    out.sort(key=lambda f: (f.severity != "fail", f.file, f.line))
    return out


def lint_html(content: str, actions_json: str, file: str = "app.html") -> list[Finding]:
    """A single-file app's page: the client FAIL rules against its own
    declared actions."""
    return _lint_client(content, file, declared_from_actions(actions_json), None, single=True)


def _read(source_dir: Path, rel: str) -> str:
    """A file of the tree as text, read without following a link (a link or
    a file that went away reads empty: the copy refuses it anyway)."""
    from services.apps import releases
    try:
        return releases.read_tree_file(source_dir, rel, max_size=releases.MAX_RELEASE_FILE_BYTES) \
            .decode("utf-8", "replace")
    except OSError:
        return ""


def summary(findings: list[Finding]) -> dict:
    """The hook answer's shape: counts and the rows."""
    fails = [f for f in findings if f.severity == "fail"]
    return {
        "problems": len(fails),
        "warnings": len(findings) - len(fails),
        "findings": [f.as_dict() for f in findings],
    }
