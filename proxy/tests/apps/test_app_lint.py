"""The static checks of an app folder (APPS.md "Deploy pipeline"): every
rule fires on the mistake and stays quiet on the allowed form, comments
never count, the shipped templates lint clean, and a single-file app gets
the client FAIL rules.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import config
from services.apps import app_deploy, app_lint, releases
from services.apps.app_lint import Declared, lint_html, lint_tree, strip_comments
from tests._timing import bound

AGENT = "lint-agent"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "app"
    shutil.rmtree(root, ignore_errors=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _lint(tmp_path: Path, files: dict[str, str], declared: Declared | None = None) -> list[app_lint.Finding]:
    root = _tree(tmp_path, files)
    return lint_tree(root, releases.walk_tree(root), declared or Declared())


def _rules(findings, severity: str | None = None) -> list[str]:
    return sorted({f.rule for f in findings if severity is None or f.severity == severity})


GOOD_PAGE = """<script src="/ui-kit/tailwind.js"></script>
<div class="flex gap-2"><button class="btn" id="go">Go</button></div>
<script>
  document.getElementById("go").onclick = function () { otodock.fetch("/cards"); };
  var ws = otodock.ws("/live");
  ws.onclose = function () { document.title = "off"; };
  otodock.feed("sessions", function () {});
  location.hash = "x";
</script>
"""
GOOD_SERVER = """import { Database } from "bun:sqlite";
import path from "node:path";
import fs from "fs";
const db = new Database(`${process.env.OTODOCK_DATA_DIR}/app.db`);
Bun.serve({ port: Number(process.env.PORT || 3000), hostname: "0.0.0.0", fetch(req) {
  const u = new URL(req.url);
  if (u.pathname === "/_health") return new Response("ok");
  if (u.pathname === "/_handler/gate") return new Response("ok");
  fetch("https://api.example.com/x");
  return new Response("hi");
}});
"""


def test_a_clean_folder_has_no_findings(tmp_path):
    d = Declared(feeds={"sessions"}, egress={"api.example.com"}, handlers={"gate"})
    assert _lint(tmp_path, {"client/index.html": GOOD_PAGE, "server/index.ts": GOOD_SERVER}, d) == []


@pytest.mark.parametrize("snippet,rule", [
    ('fetch("/api/x")', "client.raw-fetch"),
    ("fetch ('/x')", "client.raw-fetch"),
    ("new WebSocket('ws://x')", "client.raw-fetch"),
    ("new XMLHttpRequest()", "client.raw-fetch"),
    ("navigator.sendBeacon('/x')", "client.raw-fetch"),
    ("localStorage.setItem('a', 1)", "client.storage"),
    ("indexedDB.open('x')", "client.storage"),
    ("if (confirm('sure?')) go()", "client.modal"),
    ("var n = prompt('name')", "client.modal"),
    ("location.reload()", "client.navigation"),
    ("location.href = '/x'", "client.navigation"),
    ("window.location = '/x'", "client.navigation"),
    ("location.assign('/x')", "client.navigation"),
    ("new Worker('w.js')", "client.worker"),
    ("navigator.serviceWorker.register('sw.js')", "client.worker"),
    # Assembled so no scanner reads the test file itself as a leaked key.
    ("var k = '" + "sk-" + "abcdefghij" * 4 + "'", "client.secret"),
    ("var k = '" + "AKIA" + "ABCDEFGHIJKLMNOP" + "'", "client.secret"),
    ("otodock.feed('tasks', f)", "client.undeclared-feed"),
    ("otodock.action('nope')", "client.undeclared-action"),
    ("otodock.platform('viewer.me')", "client.undeclared-method"),
    ("otodockWidgets.mount(el, {widget: 'tasks'})", "client.widget-feed"),
    ("otodockWidgets.mount(el, {widget: 'checks'})", "client.widget-feed"),
    ("otodockWidgets.mount(el, {widget: 'connect'})", "client.widget-feed"),
])
def test_client_fail_rules_fire_on_the_mistake(tmp_path, snippet, rule):
    page = GOOD_PAGE.replace("location.hash = \"x\";", snippet + ";")
    findings = _lint(tmp_path, {"client/index.html": page}, Declared(feeds={"sessions"}))
    assert rule in _rules(findings, "fail"), findings
    assert all(f.line >= 1 and f.file == "client/index.html" for f in findings)


@pytest.mark.parametrize("markup,rule", [
    ('<form id="f"><input></form>', "client.form"),
    ('<FORM action="/x">', "client.form"),
    ('<script src="https://cdn.example.com/x.js"></script>', "client.external-resource"),
    ('<link rel="stylesheet" href="//cdn.example.com/x.css">', "client.external-resource"),
    ('<img src="http://x.example/a.png">', "client.external-resource"),
    ('<video src="https://x.example/a.mp4"></video>', "client.external-resource"),
    ('<div style="background: url(https://x.example/a.png)"></div>', "client.external-resource"),
    ('<style>@import "https://x.example/a.css";</style>', "client.external-resource"),
    ('<iframe src="x.html"></iframe>', "client.iframe"),
    ('<object data="a.svg"></object>', "client.iframe"),
    ('<script src="/app.js"></script>', "client.asset-path"),
    ('<a href="/chat/x">open</a>', "client.asset-path"),
    ('<script src="missing.js"></script>', "client.asset-path"),
    ('<img src="page.html">', "client.asset-path"),
    ('<meta http-equiv="refresh" content="5">', "client.navigation"),
])
def test_client_markup_rules_fire_on_the_mistake(tmp_path, markup, rule):
    findings = _lint(tmp_path, {"client/index.html": GOOD_PAGE + markup},
                     Declared(feeds={"sessions"}))
    assert rule in _rules(findings, "fail"), findings


def test_client_allowed_forms_stay_quiet(tmp_path):
    page = GOOD_PAGE + """
<a href="https://github.com/x/y">bridged link</a>
<img src="logo.png"><script src="./app.js"></script>
<a href="#top">up</a><img src="data:image/png;base64,AAAA">
<script>
  dialog.confirm("x"); showConfirm(); api.fetch("/x"); otodock.fetch("/x");
  history.length; document.location.hash = "y"; var task = "task-card";
  var mask = "desk-lamp"; window.otodock.feed("sessions", f);
  otodockWidgets.mount(el, {widget: 'lanes', feed: 'project_lanes'});
</script>
"""
    assets = {"client/logo.png": "x", "client/app.js": "console.log(1)"}
    findings = _lint(tmp_path, {"client/index.html": page, **assets},
                     Declared(feeds={"sessions", "project_lanes"}))
    assert _rules(findings, "fail") == [], findings
    # The lanes widget binds a project's lanes as well as the sessions.
    lanes_only = page.replace('window.otodock.feed("sessions", f);', "").replace(
        'otodock.feed("sessions", function () {});', "")
    findings = _lint(tmp_path, {"client/index.html": lanes_only, **assets}, Declared(feeds={"project_lanes"}))
    assert _rules(findings, "fail") == [], findings


@pytest.mark.parametrize("snippet,rule", [
    ("alert('hi')", "client.modal"),
    ("window.open('https://x.example')", "client.popup"),
    ("history.pushState({}, '', '/x')", "client.navigation"),
    ("navigator.clipboard.writeText('x')", "client.device-api"),
    ("Notification.requestPermission()", "client.device-api"),
    ("import('./m.js')", "client.module"),
    ("document.cookie", "client.storage"),
])
def test_client_warn_rules(tmp_path, snippet, rule):
    page = GOOD_PAGE.replace("location.hash = \"x\";", snippet + ";")
    findings = _lint(tmp_path, {"client/index.html": page}, Declared(feeds={"sessions"}))
    assert rule in _rules(findings, "warn"), findings
    assert rule not in _rules(findings, "fail")


def test_client_markup_warn_rules(tmp_path):
    page = GOOD_PAGE + '<a href="report.pdf" download>get</a><base href="/x/"><script type="module" src="m.js"></script>'
    findings = _lint(tmp_path, {"client/index.html": page, "client/m.js": "", "client/report.pdf": "x"},
                     Declared(feeds={"sessions"}))
    assert {"client.download", "client.base", "client.module"} <= set(_rules(findings, "warn"))
    assert _rules(findings, "fail") == []


def test_a_reconnect_of_the_pages_own_is_a_warning(tmp_path):
    page = GOOD_PAGE.replace('ws.onclose = function () { document.title = "off"; };',
                             'ws.onclose = function () {\n  setTimeout(function () { ws = otodock.ws("/live"); }, 1000);\n};')
    findings = _lint(tmp_path, {"client/index.html": page}, Declared(feeds={"sessions"}))
    assert "client.ws-reconnect" in _rules(findings, "warn")
    # A handler that only shows the state, with a setTimeout elsewhere.
    quiet = GOOD_PAGE + "<script>setTimeout(function () {}, 5);</script>"
    assert "client.ws-reconnect" not in _rules(_lint(tmp_path, {"client/index.html": quiet}, Declared(feeds={"sessions"})))


def test_tailwind_and_full_document_warnings(tmp_path):
    no_tw = '<div class="flex gap-2">x</div>'
    assert "client.no-tailwind" in _rules(_lint(tmp_path, {"client/index.html": no_tw}), "warn")
    full = "<!doctype html><html><head></head><body><div>x</div></body></html>"
    findings = _lint(tmp_path, {"client/index.html": full})
    assert [f.rule for f in findings].count("client.full-document") == 2
    # The kit goes before Tailwind, or its rules win over Tailwind's classes.
    head = ('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
            '<script src="/ui-kit/tailwind.js"></script>\n<link rel="stylesheet" href="/ui-kit/otodock-tokens.css">'
            '</head><body><div>x</div></body></html>')
    rows = [f for f in _lint(tmp_path, {"client/index.html": head}) if f.rule == "client.full-document"]
    assert len(rows) == 1 and rows[0].line == 2 and "Link /ui-kit/otodock-tokens.css first" in rows[0].message
    first = head.replace('<script src="/ui-kit/tailwind.js"></script>\n', "").replace(
        "</head>", '<script src="/ui-kit/tailwind.js"></script></head>')
    assert [f for f in _lint(tmp_path, {"client/index.html": first}) if f.rule == "client.full-document"] == []


def test_comments_never_count(tmp_path):
    page = """<!-- No <form>: the frame has no allow-forms; fetch('/x') would fail -->
<script src="/ui-kit/tailwind.js"></script>
<script>
  // localStorage.setItem('a', 1); location.reload();
  /* new WebSocket('ws://x') */
  var url = "https://x.example//path"; var pr = "//cdn.example";
  otodock.fetch("/x");
</script>
"""
    findings = _lint(tmp_path, {"client/index.html": page})
    assert _rules(findings, "fail") == [], findings
    # The blanked text keeps every newline, so line numbers stay true.
    stripped = strip_comments(page, html=True)
    assert stripped.count("\n") == page.count("\n") and len(stripped) == len(page)


def test_the_script_rules_read_a_pages_scripts_not_its_prose(tmp_path):
    # A 1.6.1 dashboard a scheduled task re-pins carries prose like this;
    # the JavaScript rules must not refuse it, and still catch the code.
    prose = """<p>Please confirm (by replying) before noon.</p>
<p>location = Berlin, office 3</p><p>Write a prompt (short) for the team.</p>
<p>We moved off localStorage last year; fetch (the report) from the drive.</p>"""
    assert [f for f in lint_html(prose, "[]") if f.severity == "fail"] == []
    code = prose + '<button onclick="if (confirm(1)) go()">x</button><script>localStorage.x = 1;</script>'
    rules = _rules(lint_html(code, "[]"), "fail")
    assert rules.count("client.modal") == 1 and rules.count("client.storage") == 1, rules
    # The markup rules still read the whole page, prose included.
    assert "client.form" in _rules(lint_html(prose + "<form><input></form>", "[]"), "fail")


def test_a_crafted_page_lints_in_linear_time():
    # Openers with no closer made the old regexes rescan to the end from
    # every later opener: minutes at this size, hours at the 2 MB cap.
    import time
    page = (("<!--" * 20_000) + ("<script>" + "/* a" * 20_000 + "</script>") + ("<a " * 30_000)
            + ("<meta " * 15_000) + ("on" * 400_000) + (" x.onclose=(" * 20_000))
    started = time.monotonic()
    lint_html(page, "[]")
    assert time.monotonic() - started < bound(5)


@pytest.mark.parametrize("snippet,rule", [
    ('import express from "express";', "server.npm-import"),
    ('const ws = require("ws");', "server.npm-import"),
    ('Bun.serve({ port: 8080, hostname: "0.0.0.0", fetch() {} });', "server.port"),
    ('Bun.serve({ port: Number(process.env.PORT), hostname: "localhost", fetch() {} });', "server.bind"),
    ('const db = new Database("/app/db/app.db");', "server.data-dir"),
    ('const db = new Database("app.db");', "server.data-dir"),
    ('fetch("https://other.example.com/x");', "server.egress"),
    ('fetch(`https://other.example.com/${x}`);', "server.egress"),
])
def test_server_fail_rules(tmp_path, snippet, rule):
    server = GOOD_SERVER + "\n" + snippet + "\n"
    d = Declared(egress={"api.example.com"}, handlers={"gate"})
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server}, d)
    assert rule in _rules(findings, "fail"), findings


def test_server_allowed_forms_and_warnings(tmp_path):
    server = GOOD_SERVER + """
import type { Foo } from "typething";
import { x } from "./lib/x";
const mem = new Database(":memory:");
Bun.write(`${process.env.OTODOCK_DATA_DIR}/exports/board.json`, "x");
Bun.write("/tmp/scratch", "x");
Bun.write("/app/out.txt", "x");
db.exec("DROP TABLE old");
db.exec("ALTER TABLE issues RENAME TO threads");
fs.renameSync("a", "b");
fetch(`https://${host}/x`);
"""
    d = Declared(egress={"api.example.com"}, handlers={"gate"})
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server}, d)
    assert _rules(findings, "fail") == [], findings
    warns = [f.rule for f in findings if f.severity == "warn"]
    assert warns.count("server.migration") == 2
    assert warns.count("server.write-outside-data") == 1


def test_server_health_handlers_and_entry(tmp_path):
    no_health = GOOD_SERVER.replace('if (u.pathname === "/_health") return new Response("ok");', "")
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": no_health},
                     Declared(egress={"api.example.com"}, handlers={"gate", "github"}))
    assert "server.health" in _rules(findings, "warn")
    assert "server.handler-route" in _rules(findings, "fail")
    assert any("github" in f.message for f in findings if f.rule == "server.handler-route")
    # A generic prefix route answers every declared handler.
    generic = GOOD_SERVER.replace('"/_handler/gate"', '"/_handler/x"').replace(
        'u.pathname === "/_handler/x"', 'u.pathname.startsWith("/_handler/")')
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": generic},
                     Declared(egress={"api.example.com"}, handlers={"gate", "github"}))
    assert "server.handler-route" not in _rules(findings)
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/lib.ts": "export const x = 1;"})
    assert "server.no-entry" in _rules(findings, "fail")


def test_a_single_file_app_gets_the_client_rules():
    html = "<script src=\"/ui-kit/tailwind.js\"></script><form><input></form><script>alert(1); otodock.action('go');</script>"
    findings = lint_html(html, json.dumps([{"id": "go", "type": "send_prompt", "label": "Go", "prompt": "x"}]), "apps/x.html")
    assert _rules(findings, "fail") == ["client.form"]
    assert {"client.modal", "app.action-floor"} <= set(_rules(findings, "warn"))
    assert findings[0].file == "apps/x.html"
    assert "client.asset-path" in _rules(lint_html('<img src="logo.png">', "[]"))


def test_a_single_file_page_lists_its_fails_first():
    page = '<script src="/ui-kit/tailwind.js"></script><script>alert(1);</script>\n<form><input></form>'
    assert [(f.rule, f.severity) for f in lint_html(page, "[]")] == [("client.form", "fail"),
                                                                     ("client.modal", "warn")]


def test_the_summary_cuts_the_long_families_and_never_a_fail():
    rows = ([app_lint.Finding("client.form", "fail", "client/index.html", n, "m") for n in range(1, 13)]
            + [app_lint.Finding("words.dash", "warn", "client/index.html", n, f"w{n}") for n in range(1, 13)]
            + [app_lint.Finding("client.palette", "warn", "client/index.html", n, "p") for n in range(1, 10)]
            + [app_lint.Finding("brand.contrast", "warn", "client/brand.css", n, "b") for n in range(1, 4)]
            + [app_lint.Finding("server.eval", "warn", "server/index.ts", n, "e") for n in range(1, 11)])
    s = app_lint.summary(rows)
    assert s["problems"] == 12 and s["warnings"] == 34
    by_rule: dict[str, int] = {}
    for r in s["findings"]:
        by_rule[r["rule"]] = by_rule.get(r["rule"], 0) + 1
    assert by_rule == {"client.form": 12, "words.dash": 8, "client.palette": 8, "brand.contrast": 3,
                       "server.eval": 10, "words.more": 1, "client.palette.more": 1}
    more = next(r for r in s["findings"] if r["rule"] == "words.more")
    assert more["severity"] == "warn" and more["line"] == 9
    assert more["message"] == "4 more like these. Fix the ones above, then check again."


def test_the_summary_and_the_manifest_reading():
    findings = [app_lint.Finding("a", "fail", "f", 1, "m"), app_lint.Finding("b", "warn", "f", 2, "n")]
    s = app_lint.summary(findings)
    assert s["problems"] == 1 and s["warnings"] == 1 and s["findings"][0]["rule"] == "a"
    m = app_deploy.validate_app_json({
        "title": "T", "egress": ["api.example.com"],
        "actions": [{"id": "s", "label": "S", "type": "data_feed", "feed": "sessions"},
                    {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}],
        "handlers": {"on_trigger": ["gate"], "on_schedule": {"nightly": {"cron": "0 3 * * *"}}},
    }, AGENT, True)
    d = app_lint.declared_from_manifest(m)
    assert d.feeds == {"sessions"} and d.methods == {"viewer.me"}
    assert d.egress == {"api.example.com"} and d.handlers == {"gate", "nightly"}
    assert d.action_ids == {"s", "me"}


def test_the_shipped_templates_lint_clean():
    """Authors and community agents copy the templates and the patterns as
    they are, so neither carries a finding of any grade: a warning there
    would teach every copy the mistake."""
    skill = config.MCPS_DIR / "custom" / "display-mcp" / "skills" / "app-authoring"
    templates = sorted(p for p in (skill / "templates").iterdir() if p.is_dir())
    assert templates
    for tpl in templates:
        doc = json.loads((tpl / "app.json").read_text())
        m = app_deploy.validate_app_json(doc, AGENT, True)
        findings = lint_tree(tpl, releases.walk_tree(tpl), app_lint.declared_from_manifest(m))
        assert findings == [], (tpl.name, findings)
    # A pattern is a page an agent pastes into a single-file app. The role
    # gate asks for viewer.me, which such an app declares.
    declared = json.dumps([{"id": "me", "label": "Who is viewing", "type": "platform", "method": "viewer.me"}])
    patterns = sorted((skill / "patterns").glob("*.html"))
    assert len(patterns) == 12, [p.name for p in patterns]
    for page in patterns:
        assert lint_html(page.read_text(), declared, page.name) == [], page.name


# ── the server's security rules ─────────────────────────────────────────────

# Key shapes, each assembled at run time so no scanner reads this file as a
# leaked credential.
KEYS = {
    "stripe": "sk_" + "live_" + "a1B2c3D4e5F6g7H8i9J0",
    "stripe-restricted": "rk_" + "test_" + "a1B2c3D4e5F6g7H8i9J0",
    "stripe-webhook": "whsec_" + "a1B2c3D4e5F6g7H8" * 2,
    "openai-project": "sk-" + "proj-" + "Ab1_" * 8,
    "anthropic": "sk-" + "ant-api03-" + "Ab1-" * 8,
    "openai-old": "sk-" + "Ab1" * 12,
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github": "ghp_" + "Ab1" * 12,
    "github-fine": "github_" + "pat_" + "Ab1_" * 14,
    "slack": "xoxb-" + "1234567890-" + "abcdefghij",
    "slack-hook": "https://hooks.slack.com/services/" + "T0123ABC/" + "B0123ABC/" + "Ab1" * 8,
    "gitlab": "glpat-" + "Ab1-" * 5,
    "google": "AI" + "za" + "Ab1" * 11 + "Ab",
    "google-oauth": "GOCSPX-" + "Ab1_" * 7,
    "sendgrid": "SG." + "Ab1" * 7 + "A" + "." + "Ab1" * 14 + "A",
    "pem": "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIEow" + "Ab1+" * 10,
}


@pytest.mark.parametrize("name", sorted(KEYS))
def test_a_key_in_any_file_of_the_release_refuses_it_by_where_it_sits(tmp_path, name):
    key = KEYS[name]
    files = {
        "client/index.html": "<p>x</p>",
        "client/app.js": f"var k = '{key}';",
        # A key in a comment ships in the release too.
        "server/index.ts": GOOD_SERVER + f"// {key}\n",
        "server/config.json": json.dumps({"key": key}),
        "app.json": json.dumps({"title": "T", "note": key}),
        "steps/build.sh": f"curl -H 'Authorization: {key}' x\n",
    }
    findings = _lint(tmp_path, files, Declared(egress={"api.example.com"}, handlers={"gate"}))
    where = {(f.rule, f.file) for f in findings if f.severity == "fail"}
    assert where == {("client.secret", "client/app.js"), ("server.secret", "server/index.ts"),
                     ("server.secret", "server/config.json"), ("app.secret", "app.json"),
                     ("app.secret", "steps/build.sh")}, (name, findings)
    assert next(f for f in findings if f.file == "server/index.ts").line == GOOD_SERVER.count("\n") + 1
    # A single-file app's page gets the same scan.
    assert "client.secret" in _rules(lint_html(f"<script>var k = '{key}';</script>", "[]"), "fail")


def test_no_key_shape_flags_a_class_a_locale_a_publishable_key_or_a_name(tmp_path):
    page = """<style>
.sk-folding-cube-animation-delay-two { animation-delay: .3s }
.sk-chase-dot-before-animation-delay { color: var(--p-text) }
</style>
<script>
var locales = ["sk-SK-u-ca-gregory-nu-latn", "sk-SK", "en-US-u-hc-h23-nu-arab"];
var stripe = ["pk_""" + "live_" + "a1B2c3D4e5F6g7H8i9J0" + """", "pk_""" + "test_" + "a1B2c3D4e5F6g7H8i9J0" + """"];
var sri = "sha384-oqVuAfXRKap7fdgcCY5uykM6+R9GqQ8K/uxy9rx7HNQlGYl1kPzQho1wx4JwY8wC";
var id = "550e8400-e29b-41d4-a716-446655440000"; var img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg";
var task = "task-skeleton-loader-animation-delay";
</script>"""
    readme = ("Paste the whsec_ value from Stripe's dashboard into the app's settings.\n"
              "The key file opens with -----BEGIN " + "PRIVATE KEY----- and nothing more here.\n"
              "AWS documents its example key, " + "AKIA" + "IOSFODNN7" + "EXAMPLE" + ", in every guide.\n"
              "A Slack token starts with xoxb- and a GitHub one with ghp_ or github_pat_.\n")
    findings = _lint(tmp_path, {"client/index.html": page, "server/index.ts": GOOD_SERVER,
                                "README.md": readme},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert [f for f in findings if f.rule.endswith(".secret")] == [], findings
    # A file that is not text (a NUL in it) is not read as text.
    binary = b"\x89PNG\x00\x00" + KEYS["aws"].encode()
    root = _tree(tmp_path, {"client/index.html": "<p>x</p>"})
    (root / "client" / "logo.png").write_bytes(binary)
    assert lint_tree(root, releases.walk_tree(root), Declared()) == []


@pytest.mark.parametrize("serve,severity", [
    ('Bun.serve({ development: true, fetch() { return new Response("x"); } });', "fail"),
    ('Bun.serve({ development: { hmr: false }, fetch() { return new Response("x"); } });', "fail"),
    ('export default { development: true, fetch() { return new Response("x"); } };', "fail"),
    ('Bun.serve<{ who: string }>({ development: true, fetch() { return new Response("x"); } });', "fail"),
    ('Bun.serve({ development: process.env.DEV === "1", fetch() { return new Response("x"); } });', "warn"),
    ('Bun.serve({ development: false, fetch() { return new Response("x"); } });', None),
    # Outside the serve object the key is someone else's.
    ('const opts = { development: true }; Bun.serve({ fetch() { return new Response("x"); } });', None),
    ('export default { development: true };', None),
    # A serve object built elsewhere: any development key may be its own.
    ('const opts = { development: true, fetch() { return new Response("x"); } }; Bun.serve(opts);', "warn"),
    ('const base = { development: true }; Bun.serve({ ...base, fetch() { return new Response("x"); } });',
     "warn"),
    ('const opts = { development: false, fetch() { return new Response("x"); } }; Bun.serve(opts);', None),
])
def test_development_mode_in_the_serve_object(tmp_path, serve, severity):
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": serve + "\n'/_health';\n"})
    got = [f.severity for f in findings if f.rule == "server.dev-mode"]
    assert got == ([severity] if severity else []), findings
    if severity == "fail":
        msg = next(f.message for f in findings if f.rule == "server.dev-mode")
        assert msg == ("Development mode answers an error with the server's code. Remove the key. "
                       "The platform runs the server in production mode.")


def test_the_port_and_the_address_are_read_in_the_serve_object_only(tmp_path):
    server = ('import tls from "node:tls";\nconst s = tls.connect({ port: 465, host: "smtp.example.com" });\n'
              'const db = { hostname: "localhost", port: 5432 };\n'
              'Bun.serve<{ who: Who }>({\n  port,\n  hostname: "0.0.0.0",\n  fetch(req) { return new Response("/_health"); },\n});\n')
    assert _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server}) == []
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>",
                                "server/index.ts": 'export default { port: 8080, hostname: "127.0.0.1", fetch() { return new Response("/_health"); } };'})
    assert {"server.port", "server.bind"} == set(_rules(findings, "fail"))


SECURITY_RULES = ("server.eval", "server.sql-concat", "server.spawn", "server.error-leak",
                  "server.file-path", "server.write-without-role", "server.dev-mode",
                  "server.secret", "app.secret", "client.secret")


@pytest.mark.parametrize("snippet,rule", [
    ("const v = eval(body.expr);", "server.eval"),
    ('const f = new Function("a", body.code);', "server.eval"),
    ('const g = Function("return this")();', "server.eval"),
    ("db.query(`SELECT * FROM cards WHERE id = ${body.id}`).all();", "server.sql-concat"),
    ('db.prepare("SELECT * FROM t WHERE name = \'" + url.searchParams.get("q") + "\'").all();', "server.sql-concat"),
    ("db.run(`DELETE FROM t WHERE id = ${params.id}`);", "server.sql-concat"),
    ('Bun.spawn(["convert", body.file]);', "server.spawn"),
    ('Bun.spawnSync(["ls"]);', "server.spawn"),
    ('import { execFile } from "node:child_process";', "server.spawn"),
    ("await Bun.$`ls ${dir}`;", "server.spawn"),
    ("return json({ error: String(err) }, 500);", "server.error-leak"),
    ("return new Response(`failed: ${e}`, { status: 500 });", "server.error-leak"),
    ("return json({ error: e.message }, 500);", "server.error-leak"),
    ("return Response.json({ detail: err.toString() });", "server.error-leak"),
    ("return json({ trace: error.stack }, 500);", "server.error-leak"),
    ('return new Response(Bun.file(url.searchParams.get("f") || ""));', "server.file-path"),
    ("const t = await Bun.file(`/app/data/${params.name}`).text();", "server.file-path"),
    ('const t = readFileSync(req.headers.get("x-path"));', "server.file-path"),
])
def test_the_security_rules_warn_on_the_mistake(tmp_path, snippet, rule):
    server = GOOD_SERVER + "\n" + snippet + "\n"
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert [f.rule for f in findings if f.severity == "warn" and f.rule in SECURITY_RULES] == [rule], findings
    assert _rules(findings, "fail") == []
    line = next(f.line for f in findings if f.rule == rule)
    assert line == GOOD_SERVER.count("\n") + 2


def test_the_security_rules_stay_quiet_on_correct_code(tmp_path):
    server = GOOD_SERVER + """
const table = "cards";
db.query(`SELECT id, title
            FROM cards
           WHERE col = ?`).all(col);
db.run(`UPDATE ${table} SET ${sets.join(", ")} WHERE id = ?`, [...vals, id]);
db.exec(`CREATE TABLE IF NOT EXISTS t (id INTEGER, note TEXT DEFAULT '${body}')`);
db.query("SELECT * FROM t WHERE id = ?").get(body.id);
const price = `cost $` + "5";
const json2 = r.json(); obj.eval(x); myeval(x); "evaluate(";
const t = await Bun.file("body.json").text();
await Bun.write(`${dataDir}/exports/board.json.tmp`, body);
mkdirSync(`${dataDir}/exports/${params.x}`, { recursive: true });
try { go(); } catch (e) { console.log("not written: " + String(e).slice(0, 120)); }
const parsed = await req.json().catch(() => ({}));
const nice = json({ error: "the card was not found" }, 404);
"""
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert [f for f in findings if f.rule in SECURITY_RULES] == [], findings


def test_the_serve_objects_error_member(tmp_path):
    leak = 'Bun.serve({ fetch() { return new Response("/_health"); }, error(e) { return new Response(String(e), { status: 500 }); } });'
    plain = ('Bun.serve({ fetch() { return new Response("/_health"); },\n'
             '  error(error) { console.log("failed: " + String(error).slice(0, 300)); return json({ error: "something went wrong" }, 500); } });')
    arrow = 'Bun.serve({ fetch() { return new Response("/_health"); }, error: (err) => json({ error: err.name }, 500) });'
    for server, want in ((leak, ["server.error-leak"]), (plain, []), (arrow, ["server.error-leak"])):
        findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server})
        assert [f.rule for f in findings if f.rule in SECURITY_RULES] == want, (server, findings)


def test_a_server_that_writes_must_read_the_role(tmp_path):
    writes = GOOD_SERVER.replace('if (u.pathname === "/_health")',
                                 'if (req.method === "POST" && u.pathname === "/cards") return new Response("ok");\n'
                                 '  if (u.pathname === "/_health")')
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": writes},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    rows = [f for f in findings if f.rule == "server.write-without-role"]
    assert len(rows) == 1 and rows[0].severity == "warn" and rows[0].line == 7, findings
    for read in ("const r = claim.role;", "const r = claim?.role;", 'const r = claim["role"];',
                 "const { name, role } = claim;", "const {\n  role,\n  sub,\n} = claimOf(req);"):
        quiet = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": writes,
                                 "server/roles.ts": read + "\n"},
                      Declared(egress={"api.example.com"}, handlers={"gate"}))
        assert "server.write-without-role" not in _rules(quiet), (read, quiet)
    # A type that names the field reads nothing.
    typed = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": writes,
                             "server/types.ts": "type Claim = { principal?: string; role?: string };\n"},
                  Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert "server.write-without-role" in _rules(typed)
    # A role destructured in a parameter list is a read.
    for read in ("const canWrite = ({ role }: Claim) => RANK[role] >= 2;",
                 "function rankOf({ role }) { return RANK[role]; }",
                 "const may = ({ role, external }: { role: string; external?: boolean }, basis) => !external;"):
        quiet = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": writes,
                                 "server/roles.ts": read + "\n"},
                      Declared(egress={"api.example.com"}, handlers={"gate"}))
        assert "server.write-without-role" not in _rules(quiet), (read, quiet)


@pytest.mark.parametrize("route", [
    'Bun.serve({ routes: { "/cards": { POST: async req => new Response("ok") } }, fetch() { return new Response("/_health"); } });',
    'Bun.serve({ routes: { "/cards": { POST: (req) => new Response("ok") } }, fetch() { return new Response("/_health"); } });',
    'Bun.serve({ routes: { "/cards": { DELETE: function (req) { return new Response("ok"); } } }, fetch() { return new Response("/_health"); } });',
    'class Cards {\n  async POST(req: Request): Promise<Response> {\n    return new Response("ok");\n  }\n}\nconst h = "/_health";',
])
def test_every_route_form_is_a_write_route(tmp_path, route):
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": route + "\n"})
    assert "server.write-without-role" in _rules(findings, "warn"), findings


def test_the_egress_route_names_a_declared_host(tmp_path):
    route = GOOD_SERVER + "\nfetch(`${proxy}/v1/apps/${id}/egress/api.other.com/v1/x`);\nfetch(`${proxy}/v1/apps/${id}/egress/api.example.com/v1/x`);\n"
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": route},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert [(f.rule, f.severity) for f in findings] == [("server.egress", "fail")]
    assert findings[0].message == "api.other.com is not in app.json egress. The /egress/ route refuses it."


@pytest.mark.parametrize("snippet,refused", [
    ('import {\n  Hono,\n  type Context,\n} from "hono";', True),
    ('import "reflect-metadata";', True),
    ('export { z } from "zod";', True),
    ('export * from "lodash";', True),
    ('const m = await import("left-pad");', True),
    ('import type {\n  Foo,\n} from "typething";', False),
    ('export type { Bar } from "typething";', False),
    ('import { Database } from "bun:sqlite";\nimport x from "./lib/x.ts";', False),
    ('import typeorm from "typeorm";', True),
])
def test_an_import_is_read_the_way_bun_reads_it(tmp_path, snippet, refused):
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>",
                                "server/index.ts": snippet + "\n" + GOOD_SERVER},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert ("server.npm-import" in _rules(findings, "fail")) is refused, findings


def test_every_server_suffix_is_read(tmp_path):
    for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", ".cjs", ".cts"):
        findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": GOOD_SERVER,
                                    f"server/lib{suffix}": "export const v = eval(code);\n"},
                         Declared(egress={"api.example.com"}, handlers={"gate"}))
        assert ("server.eval", f"server/lib{suffix}") in {(f.rule, f.file) for f in findings}, suffix


def test_a_real_app_server_lints_clean():
    # The platform's own operations app, a server of fourteen modules: every
    # rule stays quiet on the patterns it uses (SQL interpolated from its own
    # constants, hoisted role checks, a typed serve object). Its folder is in
    # the private tree only, named here from parts.
    root = config.BASE_DIR.parent.joinpath("dev", "apps", "developer" + "-dashboard")
    if not root.is_dir():
        pytest.skip("the operations app is in the private tree only")
    doc = json.loads((root / "app.json").read_text())
    findings = lint_tree(root, releases.walk_tree(root), app_lint.declared_from_doc(doc))
    assert [f for f in findings if f.rule in SECURITY_RULES or f.severity == "fail"] == [], findings


# Shapes that made a rule backtrack, rescan or loop once per character, each
# repeated to fill a file: dense code, the patterns that once ran away (an
# unbounded handler name, an unclosed reconnect handler, an import list of
# spaces, a property with no colon, an rgb() of spaces, a shell import of
# dollar signs), deep nesting in the brace and call scans, a long serve
# object, a long style attribute.
CRAFTED_SHAPES = [
    "{ a: 1, ", "json(x, ", "db.query(`" + "a${b}", ".query(`", "Bun.serve({", "Bun.serve<",
    "{" + " role" * 80, "import {\n", "import a\n", "x ${e} ", "json(e.stack ", "\"a\" ", "\"a\n",
    "x.onclose=()=>{", "\"/_handler/\"{", "sk-ab ", "/egress/a.b/", "export default {", "fetch (",
    "`${", "/* a", "plain words here ", "#fab { ", "bg-[", "<b>x", "< ", "<a href=",
    "class=\"text-red-500 ", "el.textContent = 'a — b'; ", "<p>a — b; c</p>",
    "eval(x); new Function(a); Bun.spawn(['a']); Bun.file(url.pathname); "
    "if (req.method === \"POST\") {} ",
    "on", "onclick ", "x.onclose=(", "x.onclose=function(", "import" + " " * 590 + "\n",
    "import{" + "$" * 300, "{ " + "a-b " * 4000 + "}", "<p style=\"" + "a-b " * 400_000 + "\">",
    "Bun.serve({" + "{" * 2000, "x.onclose=()=>{" + "{" * 2000, "\"/_handler/\"{" + "{" * 2000,
    "Bun.serve({" + "a:1," * 40_000 + "});\n", "Bun.file(", ".query(\"a\" + ", "json(" + "(" * 50,
    "notifications.create({ title: ", "<style id=\"brand\">:root{--p-action:#f00}</style>",
    "error(e) { return json(", ":root { --p-action: rgb(1 2 3" + " " * 400 + "x); }\n",
    "import { $ } from \"bun\"; import{" + "$" * 300, "Bun.serve(opts); development: true, ",
    "POST: async req => ", "return json({\n  error: ", "<style ", "<script ",
]
# Every shape fills a whole tree on each side whose rules read it: the
# server's files, or the client's pages, scripts and stylesheets with the
# brand file among them.
CRAFTED_FILES = {
    "server": [f"server/f{n}.ts" for n in range(15)],
    "client": [f"client/f{n}.{('html', 'js', 'css')[n % 3]}" for n in range(14)] + ["client/brand.css"],
}


def _crafted(shape: str, size: int) -> bytes:
    unit = shape.encode()
    return (unit * (size // len(unit) + 1))[:size]


@pytest.mark.parametrize("side", sorted(CRAFTED_FILES))
@pytest.mark.parametrize("shape", CRAFTED_SHAPES, ids=[f"shape{n}" for n in range(len(CRAFTED_SHAPES))])
def test_a_crafted_tree_at_the_release_caps_lints_in_bounded_time(tmp_path, side, shape):
    # Python's re holds the interpreter lock for a whole search, so a rule
    # that backtracks or rescans freezes the proxy while it runs. Every
    # file at the cap, the whole tree at the cap, every file the same shape.
    import os
    import time
    per_file = releases.MAX_RELEASE_FILE_BYTES - 64
    root = _tree(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": GOOD_SERVER})
    first = root / CRAFTED_FILES[side][0]
    first.write_bytes(_crafted(shape, per_file))
    for rel in CRAFTED_FILES[side][1:]:
        # Each file is read on its own: a link to the same bytes costs the
        # test one write.
        os.link(first, root / rel)
    walked = releases.walk_tree(root)
    assert len(walked) == 17
    started = time.monotonic()
    lint_tree(root, walked, Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert time.monotonic() - started < bound(5)


@pytest.mark.parametrize("shape", CRAFTED_SHAPES, ids=[f"shape{n}" for n in range(len(CRAFTED_SHAPES))])
def test_a_crafted_page_at_the_pin_cap_lints_in_bounded_time(shape):
    import time
    page = _crafted(shape, 2 * 1024 * 1024).decode("utf-8", "replace")
    started = time.monotonic()
    lint_html(page, "[]")
    assert time.monotonic() - started < bound(1)


# ── a palette the theme never reaches ───────────────────────────────────────

SAMPLE_BRAND = """:root {
  --p-action: #0b5cad;
  --p-action-hover: #094c8f;
  --p-action-fg: #ffffff;
  --p-primary-fg: #ffffff;
  --p-link: #0b5cad;
  --p-primary: #0b5cad;
  --p-brand-surface: #e7f0fa;
  --p-focus: #0b5cad;
  --p-font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
:root.dark {
  --p-action: #0b5cad;
  --p-action-hover: #094c8f;
  --p-action-fg: #ffffff;
  --p-primary-fg: #ffffff;
  --p-link: #7fb3ea;
  --p-primary: #7fb3ea;
  --p-brand-surface: #16314f;
  --p-focus: #7fb3ea;
  --p-font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
"""


@pytest.mark.parametrize("markup", [
    '<div class="flex text-amber-700">x</div>',
    '<div class="hover:bg-white">x</div>',
    '<div class="bg-[#f4b206]/50">x</div>',
    '<div style="color: #333">x</div>',
    "<style>.x { background: #0b5cad; }</style>",
    "<script>el.className = 'p-2 border-red-500';</script>",
])
def test_a_fixed_colour_is_named(tmp_path, markup):
    findings = _lint(tmp_path, {"client/index.html": GOOD_PAGE + markup}, Declared(feeds={"sessions"}))
    rows = [f for f in findings if f.rule == "client.palette"]
    assert len(rows) == 1 and rows[0].severity == "warn", findings


def test_a_stylesheet_hex_is_named_and_the_brand_is_not(tmp_path):
    files = {"client/index.html": GOOD_PAGE + '<link rel="stylesheet" href="app.css"><link rel="stylesheet" href="brand.css">',
             "client/app.css": ".a { color: var(--p-text, #1b2430); border-color: #ccc; }\n:root { --p-x: #fff; }\n",
             "client/brand.css": SAMPLE_BRAND}
    findings = _lint(tmp_path, files, Declared(feeds={"sessions"}))
    rows = [(f.file, f.line) for f in findings if f.rule == "client.palette"]
    assert rows == [("client/app.css", 1)], findings


def test_the_theme_routes_and_css_ids_stay_quiet(tmp_path):
    page = GOOD_PAGE + f'<style id="brand">\n{SAMPLE_BRAND}</style>' + """
<div class="bg-[var(--p-surface,#fff)] text-[var(--p-text)] border-p-border">PR #123 and issue #456</div>
<div style="background: var(--p-card, #ffffff)">x</div>
<svg><rect style="fill: url(#fade); stroke: url(#edge)"></rect></svg>
<style>.y { color: var(--p-link, #146bb5); --p-local: #222; }
#fab { color: var(--p-text); } #add, #feed:hover { border: 0; }</style>
<script>var label = "PR #123"; var cls = "text-secondary contextual-black";</script>
"""
    findings = _lint(tmp_path, {"client/index.html": page}, Declared(feeds={"sessions"}))
    assert [f for f in findings if f.rule == "client.palette"] == [], findings
    assert [f for f in lint_html(page, "[]") if f.rule == "client.palette"] == []


# ── the words a person reads ────────────────────────────────────────────────


def _words(findings) -> list[tuple[str, int]]:
    return [(f.rule, f.line) for f in findings if f.rule.startswith("words.")]


@pytest.mark.parametrize("text,rule", [
    ("Saved — see you soon", "words.dash"),
    ("Saved - see you soon", "words.dash"),
    ("Saved; see you soon", "words.semicolon"),
    ("Open the menu → Share to send it", "words.arrow"),
    (" ".join(["Every"] + ["word"] * 30) + ".", "words.long"),
    ("The webhook endpoint is down", "words.mechanism"),
    ("Your API token is ready", "words.mechanism"),
    ("Server is down", "words.mechanism"),
    ("Order ID 42", "words.mechanism"),
    ("null", "words.raw"),
    ("Created 2026-10-23T10:00", "words.raw"),
    ("The user_id is missing", "words.raw"),
    ("The board answered 404", "words.raw"),
])
def test_each_words_rule_names_its_miss(tmp_path, text, rule):
    findings = _lint(tmp_path, {"client/index.html": f"<p>{text}</p>"})
    assert rule in {r for r, _ in _words(findings)}, findings
    assert all(f.severity == "warn" for f in findings if f.rule.startswith("words."))


@pytest.mark.parametrize("text", [
    "Open 09:00 – 17:00 on weekdays", "Ready in 5 - 10 min", "It is -5 °C outside",
    "Microsoft Sentinel watches the doors", "See https://example.com/a-b for more",
    "Mail us at desk@example.com today", "The file sits at /var/lib/x-y today",
    "A cargo manifest, a pool and a floor plan", "Price (EUR)", "Notes (optional)",
    "Τι κάνεις; Όλα καλά.",
])
def test_the_false_friends_stay_quiet(tmp_path, text):
    findings = _lint(tmp_path, {"client/index.html": f"<p>{text}</p>"})
    assert _words(findings) == [], (text, findings)


def test_where_the_words_are_read(tmp_path):
    page = """<p>Hello</p>
<input placeholder="Type — here">
<button title="Saved; done">Go</button>
<pre>a; b — c</pre><code>x; y</code>
<div data-plain="off"><p>The endpoint; ok</p></div>
<script>
  el.textContent = "Card moved — " + n;
  el.setAttribute("aria-label", `Card ${n}; moved`);
  var cls = "a; b — not read";
</script>
"""
    server = GOOD_SERVER + """
const a = json({ error: "the card — gone" }, 404);
const b = new Response("not here; sorry", { status: 404 });
console.log("log lines — never read");
if (u.pathname.startsWith("/_handler/")) { const h = json({ error: "handler — never read" }, 400); }
if (u.pathname === "/_health") return json({ message: "ok — fine" });
await notify({ fn: "notifications.create", title: "New booking — today", body: "plain words" });
"""
    manifest = {"title": "T", "actions": [{"id": "go", "label": "Go — now", "type": "send_prompt", "prompt": "x"}]}
    findings = _lint(tmp_path, {"client/index.html": page, "server/index.ts": server,
                                "app.json": json.dumps(manifest, indent=1)},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    rows = {(f.file, f.line) for f in findings if f.rule.startswith("words.")}
    srv = GOOD_SERVER.count("\n") + 1
    assert rows == {("client/index.html", 2), ("client/index.html", 3), ("client/index.html", 7),
                    ("client/index.html", 8), ("server/index.ts", srv + 1), ("server/index.ts", srv + 2),
                    ("server/index.ts", srv + 6), ("app.json", 6)}, sorted(rows)


def test_the_audience_and_the_language(tmp_path):
    dev = '<meta name="otodock-audience" content="developers"><p>Your API token is ready</p>'
    assert _words(_lint(tmp_path, {"client/index.html": dev})) == []
    assert _words(lint_html(dev, "[]")) == []
    # The everyday words of the list stay flagged for developers too.
    assert _words(_lint(tmp_path, {"client/index.html": dev + "<p>The webhook endpoint is down</p>"}))
    greek = '<html lang="el"><body><p>Τι κάνεις; Όλα καλά — ναι.</p><p>null</p></body></html>'
    assert [r for r, _ in _words(_lint(tmp_path, {"client/index.html": greek}))] == ["words.raw"]
    french = '<html lang="fr"><body><p>Prix ; total — 5 euros, le serveur API</p></body></html>'
    assert _words(_lint(tmp_path, {"client/index.html": french})) == []


def test_an_opt_out_drops_the_words_rows_only(tmp_path):
    key = "sk-" + "Ab1" * 12
    page = f"""<div data-plain="off"><p>The endpoint; ok</p><script>var k = '{key}'; fetch('/x');</script></div>
<script>
// plain-words: allow
el.textContent = "Moved — here";
</script>"""
    server = GOOD_SERVER + f"""
// plain-words: allow
const a = json({{ error: "the card — gone", k: "{key}" }}, 404);
"""
    findings = _lint(tmp_path, {"client/index.html": page, "server/index.ts": server},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert _words(findings) == [], findings
    assert {"client.secret", "server.secret", "client.raw-fetch"} <= set(_rules(findings, "fail"))


def test_the_word_lists_split_the_release_gates_list():
    import ast
    path = config.BASE_DIR.parent.joinpath("dev", "release-check", "changelog_lint.py")
    if not path.is_file():
        pytest.skip("the release gate is in the private tree only")
    tree = ast.parse(path.read_text())
    gate = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "INTERNAL_WORDS" for t in node.targets))
    from services.apps import app_lint_words as words
    assert set(words.APP_WORDS) | set(words.EVERYDAY_WORDS) == set(gate)
    assert not set(words.APP_WORDS) & set(words.EVERYDAY_WORDS)
    assert len(words.DEVELOPER_WORDS) == 10




# ── a client's brand, measured ──────────────────────────────────────────────


@pytest.fixture
def kit_dist(tmp_path, monkeypatch):
    """The kit file where the proxy serves it from, copied from the repo."""
    dist = tmp_path / "dist"
    (dist / "ui-kit").mkdir(parents=True)
    shutil.copy(config.BASE_DIR.parent / "dashboard" / "ui-kit" / "otodock-tokens.css",
                dist / "ui-kit" / "otodock-tokens.css")
    monkeypatch.setattr(config, "DASHBOARD_DIST", dist)
    return dist


def test_the_kit_passes_its_own_pairs_and_the_contrast_port_agrees(kit_dist):
    from services.apps import app_lint_brand as brand
    kit = brand.kit_values()
    assert kit is not None
    for theme in ("light", "dark"):
        values = kit[theme]
        ratio = brand.contrast(brand.parse_colour(values["--p-action-fg"]),
                               brand.parse_colour(values["--p-action"]))
        assert round(ratio, 2) == 5.53, theme
        measured = 0
        for fg, bg, floor in brand.PAIRS:
            if fg in values and bg in values:
                measured += 1
                assert brand.contrast(brand.parse_colour(values[fg]), brand.parse_colour(values[bg])) >= floor, (theme, fg, bg)
        assert measured >= 61


def test_a_translucent_value_is_laid_over_its_surface(tmp_path, kit_dist):
    from services.apps import app_lint_brand as brand
    edge = brand.parse_colour("rgb(255 255 255 / 0.3)")
    card = brand.parse_colour("#141b24")
    assert edge == (255, 255, 255, 0.3)
    # Rounded the way build.mjs rounds: 90.5 goes up.
    assert brand.composite(edge, card) == (91, 95, 102, 1.0)
    assert brand.contrast(edge, card) == brand.contrast(brand.composite(edge, card), card)
    faint = ":root {\n  --p-border-strong: #8c8782;\n}\n:root.dark {\n  --p-border-strong: rgb(255 255 255 / 0.12);\n}\n"
    findings = _lint(tmp_path, {"client/index.html": GOOD_PAGE, "client/brand.css": faint},
                     Declared(feeds={"sessions"}))
    rows = [f.message for f in findings if f.rule == "brand.contrast"]
    assert rows and all("in the dark theme" in m and "under 3 to 1" in m for m in rows), findings


def test_an_orange_action_under_white_text_is_named_by_pair_and_theme(tmp_path, kit_dist):
    orange = ":root {\n  --p-action: #f97316;\n  --p-action-fg: #ffffff;\n}\n:root.dark {\n  --p-action: #f97316;\n  --p-action-fg: #ffffff;\n}\n"
    findings = _lint(tmp_path, {"client/index.html": GOOD_PAGE, "client/brand.css": orange},
                     Declared(feeds={"sessions"}))
    rows = [(f.line, f.message[:58]) for f in findings if f.rule == "brand.contrast"]
    assert (3, "--p-action-fg on --p-action measures 2.80 to 1 in the ligh") in rows
    assert (7, "--p-action-fg on --p-action measures 2.80 to 1 in the dark") in rows
    assert all(f.severity == "warn" for f in findings if f.rule.startswith("brand."))
    # A sample brand passes every pair, in a file and in a page.
    assert [f for f in _lint(tmp_path, {"client/index.html": GOOD_PAGE, "client/brand.css": SAMPLE_BRAND},
                             Declared(feeds={"sessions"})) if f.rule.startswith("brand.")] == []
    page = GOOD_PAGE + f'<style id="brand">{SAMPLE_BRAND}</style>'
    assert [f for f in lint_html(page, "[]") if f.rule.startswith("brand.")] == []


def test_the_brand_keeps_to_the_variables_it_may_set(tmp_path, kit_dist):
    css = (":root {\n  --p-status-success-fg: #0a7a3a;\n  --p-link: hsl(210 80% 40%);\n  --p-text: #222222;\n}\n"
           ":root.dark {\n  --p-border-strong: rgb(255 255 255 / 0.4);\n  --p-code-bg: #101418;\n  color-scheme: dark;\n}\n")
    findings = _lint(tmp_path, {"client/index.html": GOOD_PAGE, "client/brand.css": css},
                     Declared(feeds={"sessions"}))
    rules = {(f.rule, f.line) for f in findings if f.rule.startswith("brand.")}
    assert ("brand.reserved", 2) in rules
    assert ("brand.reserved", 8) in rules and ("brand.reserved", 9) in rules
    assert any("code slab" in f.message for f in findings if f.rule == "brand.reserved")
    assert ("brand.not-hex", 3) in rules
    assert ("brand.one-theme", 4) in rules and ("brand.one-theme", 7) in rules


def test_without_the_kit_file_the_brand_is_not_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_DIST", tmp_path / "no-dist")
    orange = ":root { --p-action: #f97316; }\n:root.dark { --p-action: #f97316; }\n"
    # A brand file and a page's brand style: the missing kit is said once.
    page = GOOD_PAGE + f'<style id="brand">{orange}</style><style id="brand">{orange}</style>'
    findings = _lint(tmp_path, {"client/index.html": page, "client/brand.css": orange},
                     Declared(feeds={"sessions"}))
    rows = [f for f in findings if f.rule.startswith("brand.")]
    assert [(f.rule, f.message) for f in rows] == [
        ("brand.kit-missing", "The kit file is missing, so the brand's contrast was not measured.")]
    assert [f.rule for f in lint_html(page, "[]") if f.rule.startswith("brand.")] == ["brand.kit-missing"]


def test_the_brand_follows_the_cascade_and_the_kit_reader_reads_its_rules(tmp_path, kit_dist):
    from services.apps import app_lint_brand as brand
    # A .dark rule written before :root ties with it and loses: the light
    # value shows in the dark theme, and is measured there.
    css = (".dark {\n  --p-action: #0b5cad;\n}\n:root {\n  --p-action: #f97316;\n  --p-action-fg: #ffffff;\n}\n"
           ":root.dark {\n  --p-action-fg: #ffffff;\n}\n")
    rows = [f.message for f in brand.lint_brand(css, 0, "client/brand.css", css) if f.rule == "brand.contrast"]
    assert any("--p-action-fg on --p-action" in m and "dark theme" in m for m in rows), rows
    # Rules inside @layer are read, rules inside @media are not, a :root rule
    # without a --p-* value adds nothing.
    kit = ("@layer theme, otodock;\n@layer otodock {\n  :root { scrollbar-color: auto; }\n"
           "  :root { --p-bg: #ffffff; --p-text: #111111; }\n  .dark { --p-bg: #000000; --p-text: #eeeeee; }\n}\n"
           "@media (prefers-color-scheme: dark) { :root { --p-text: #000000; } }\n")
    found = brand._declarations(kit)
    assert [(n, v) for n, v, _at, _w in found[brand.LIGHT]] == [("--p-bg", "#ffffff"), ("--p-text", "#111111")]
    assert [(n, v) for n, v, _at, _w in found[brand.DARK]] == [("--p-bg", "#000000"), ("--p-text", "#eeeeee")]
    # A crafted stylesheet is read in bounded time.
    import time
    started = time.monotonic()
    brand.lint_brand(";{" * (releases.MAX_RELEASE_FILE_BYTES // 2), 0, "client/brand.css", "")
    assert time.monotonic() - started < bound(2)



# ── the manifest's own warnings ─────────────────────────────────────────────


def test_a_button_that_acts_for_the_agent_with_no_floor_is_named(tmp_path):
    doc = {"title": "T", "actions": [
        {"id": "feed", "label": "Feed", "type": "data_feed", "feed": "sessions"},
        {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"},
        {"id": "ask", "label": "Ask", "type": "send_prompt", "prompt": "x"},
        {"id": "run", "label": "Run", "type": "fire_task", "task_id": "t", "min_role": "editor"},
        {"id": "tool", "label": "Tool", "type": "mcp_tool", "mcp": "m", "tool": "t", "min_role": "viewer"},
    ]}
    raw = json.dumps(doc, indent=1)
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "app.json": raw},
                     app_lint.declared_from_doc(doc))
    rows = [(f.line, f.message) for f in findings if f.rule == "app.action-floor"]
    lines = raw.split("\n")
    assert [line for line, _ in rows] == [next(n for n, text in enumerate(lines, 1) if f'"{i}"' in text)
                                          for i in ("ask", "tool")]
    assert rows[0][1] == ("The button ask acts for the agent, and everyone who opens the app can press it. "
                          "Give it a min_role, such as editor.")
    # A single-file pin's actions: the page's first line.
    pin = lint_html("<p>x</p>", json.dumps([{"id": "ask", "label": "Ask", "type": "send_prompt", "prompt": "x"}]))
    assert [(f.rule, f.line) for f in pin if f.rule == "app.action-floor"] == [("app.action-floor", 1)]


def test_the_description_is_a_warning_never_a_refusal(tmp_path):
    long = {"title": "T", "description": "word " * 60}
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "app.json": json.dumps(long)})
    assert [(f.rule, f.severity) for f in findings if f.rule == "app.description"] == [("app.description", "warn")]
    odd = {"title": "T", "description": ["a list"]}
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "app.json": json.dumps(odd)})
    assert [f.message for f in findings if f.rule == "app.description"] == ["The description is not text."]
    assert app_deploy.validate_app_json(long, AGENT, True).title == "T"
    short = {"title": "T", "description": "The board of the week."}
    assert [f for f in _lint(tmp_path, {"client/index.html": "<p>x</p>", "app.json": json.dumps(short)})
            if f.rule == "app.description"] == []


# ── the fix round's cases ───────────────────────────────────────────────────


def test_an_answer_that_wraps_lines_still_carries_the_error(tmp_path):
    leaks = GOOD_SERVER + """
try { go(); } catch (err) {
  return json({
    error: err.message,
  }, 500);
}
"""
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": leaks},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    rows = [f for f in findings if f.rule == "server.error-leak"]
    assert [f.line for f in rows] == [GOOD_SERVER.count("\n") + 4], findings
    # An answer the statement closed, and a log call: no leak.
    quiet = GOOD_SERVER + """
const done = json({ ok: true })
console.log("failed: " + err.message)
const shown = json({ error: format(err) === "x" ? "plain words" : "other words" }, 500);
"""
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": quiet},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert "server.error-leak" not in _rules(findings), findings


def test_a_template_joined_with_request_data_is_sql_built_from_it(tmp_path):
    server = GOOD_SERVER + "\ndb.query(`SELECT * FROM t WHERE id = ` + params.id).all();\n" \
        "db.query(`SELECT * FROM t WHERE id = ` + String(LIMIT)).all();\n"
    findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert [f.line for f in findings if f.rule == "server.sql-concat"] == [GOOD_SERVER.count("\n") + 2]


def test_a_rule_past_its_fill_says_there_is_more(tmp_path):
    from services.apps.app_lint import MAX_FINDINGS_PER_RULE
    server = GOOD_SERVER + "\n" + "const v = eval(x);\n" * (MAX_FINDINGS_PER_RULE + 10)
    key = "sk-" + "Ab1" * 12
    page = "<p>x</p>\n" + f"<script>var k = '{key}';</script>\n" * (MAX_FINDINGS_PER_RULE + 10)
    findings = _lint(tmp_path, {"client/index.html": page, "server/index.ts": server},
                     Declared(egress={"api.example.com"}, handlers={"gate"}))
    evals = [f for f in findings if f.rule == "server.eval"]
    assert len(evals) == MAX_FINDINGS_PER_RULE
    more = [f for f in findings if f.rule == "server.eval.more"]
    assert len(more) == 1 and more[0].severity == "warn"
    assert more[0].line == GOOD_SERVER.count("\n") + 2 + MAX_FINDINGS_PER_RULE
    assert more[0].message == "More rows like these in this file. Fix the ones above, then check again."
    # A cut fail says so too, and still counts as a problem.
    secrets = [f for f in findings if f.rule == "client.secret"]
    assert len(secrets) == MAX_FINDINGS_PER_RULE
    assert [f.severity for f in findings if f.rule == "client.secret.more"] == ["fail"]
    assert app_lint.summary(findings)["problems"] == MAX_FINDINGS_PER_RULE + 1


def test_a_rule_examines_a_bounded_number_of_candidates(tmp_path):
    # Candidates the boundary check refuses count too: past the cap the scan
    # stops, so a finding after five thousand of them is not read.
    from services.apps.app_lint import MAX_CANDIDATES
    for refused, found in ((MAX_CANDIDATES - 1, True), (MAX_CANDIDATES, False)):
        server = GOOD_SERVER + "\n" + "x.eval(1); " * refused + "\nconst v = eval(code);\n"
        findings = _lint(tmp_path, {"client/index.html": "<p>x</p>", "server/index.ts": server},
                         Declared(egress={"api.example.com"}, handlers={"gate"}))
        assert ("server.eval" in _rules(findings)) is found, refused


def test_a_stray_byte_hides_no_key(tmp_path):
    key = "sk-" + "Ab1" * 12
    root = _tree(tmp_path, {"client/index.html": "<p>x</p>"})
    (root / "server").mkdir()
    (root / "server" / "index.ts").write_bytes(
        GOOD_SERVER.encode() + b"\n// caf\xe9 au lait\nconst k = '" + key.encode() + b"';\n")
    findings = lint_tree(root, releases.walk_tree(root), Declared(egress={"api.example.com"}, handlers={"gate"}))
    assert [(f.rule, f.line) for f in findings if f.rule == "server.secret"] == [
        ("server.secret", GOOD_SERVER.count("\n") + 3)]


def test_a_words_row_names_the_line_its_text_starts_on(tmp_path):
    page = "<div>\n  <label>\n    Price in minor units (1250 = 12.50; 0 = free)\n  </label>\n</div>\n"
    findings = _lint(tmp_path, {"client/index.html": page})
    assert [(f.rule, f.line) for f in findings if f.rule.startswith("words.")] == [("words.semicolon", 3)]
