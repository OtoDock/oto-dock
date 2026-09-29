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
    ("var k = 'sk-abcdefghijklmnopqrstuvwxyz1234'", "client.secret"),
    # Assembled so no scanner reads the test file itself as a leaked key.
    ("var k = '" + "AKIA" + "ABCDEFGHIJKLMNOP" + "'", "client.secret"),
    ("otodock.feed('tasks', f)", "client.undeclared-feed"),
    ("otodock.action('nope')", "client.undeclared-action"),
    ("otodock.platform('viewer.me')", "client.undeclared-method"),
    ("otodockWidgets.mount(el, {widget: 'tasks'})", "client.widget-feed"),
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
</script>
"""
    findings = _lint(tmp_path, {"client/index.html": page, "client/logo.png": "x",
                                "client/app.js": "console.log(1)"}, Declared(feeds={"sessions"}))
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
    assert lint_html(prose, "[]") == []
    code = prose + '<button onclick="if (confirm(1)) go()">x</button><script>localStorage.x = 1;</script>'
    rules = _rules(lint_html(code, "[]"), "fail")
    assert rules.count("client.modal") == 1 and rules.count("client.storage") == 1, rules
    # The markup rules still read the whole page, prose included.
    assert "client.form" in _rules(lint_html(prose + "<form><input></form>", "[]"), "fail")


def test_a_crafted_page_lints_in_linear_time():
    # Openers with no closer made the old regexes rescan to the end from
    # every later opener: minutes at this size, hours at the 2 MB cap.
    import time
    page = ("<!--" * 20_000) + ("<script>" + "/* a" * 20_000 + "</script>") + ("<a " * 30_000) + ("<meta " * 15_000)
    started = time.monotonic()
    lint_html(page, "[]")
    assert time.monotonic() - started < 5


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


def test_a_single_file_app_gets_the_client_fail_rules_only():
    html = "<script src=\"/ui-kit/tailwind.js\"></script><form><input></form><script>alert(1); otodock.action('go');</script>"
    findings = lint_html(html, json.dumps([{"id": "go", "type": "send_prompt", "label": "Go", "prompt": "x"}]), "apps/x.html")
    assert _rules(findings) == ["client.form"]
    assert findings[0].file == "apps/x.html"
    assert "client.asset-path" in _rules(lint_html('<img src="logo.png">', "[]"))


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
    root = config.MCPS_DIR / "custom" / "display-mcp" / "skills" / "app-authoring" / "templates"
    for tpl in sorted(p for p in root.iterdir() if p.is_dir()):
        doc = json.loads((tpl / "app.json").read_text())
        m = app_deploy.validate_app_json(doc, AGENT, True)
        findings = lint_tree(tpl, releases.walk_tree(tpl), app_lint.declared_from_manifest(m))
        assert [f for f in findings if f.severity == "fail"] == [], (tpl.name, findings)
