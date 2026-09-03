"use strict";
// Wrapper unit tests — run with: node tests/profile_path.test.js
// Covers the only non-trivial logic in index.js: the dedicated-profile path
// computation (must be outside the MCP dir, no traversal) and agent-slug
// sanitization.
const assert = require("assert");
const path = require("path");
const {
  sanitizeAgent, computeProfileDir, resolveCliPath, detectBrowser,
  resolveBrowserConfig, isChromiumFamily, findOrphanHolders, agentDisplayName,
  readDevtoolsPort, seedProfileDisplayName,
  LineSplitter, peekRpc, rewriteInitId, buildErrorResponse,
  buildToolTextResponse, REINIT_ID,
} = require("../index.js");

// --- sanitizeAgent -------------------------------------------------------
assert.strictEqual(sanitizeAgent("my-agent_1"), "my-agent_1"); // slug chars kept
assert.strictEqual(sanitizeAgent(""), "default"); // empty -> default
assert.strictEqual(sanitizeAgent(undefined), "default"); // missing -> default
assert.strictEqual(sanitizeAgent(".."), "default"); // dots dropped -> "__" -> default
assert.strictEqual(sanitizeAgent("."), "default");
assert.strictEqual(sanitizeAgent("../../etc"), "______etc"); // separators + dots -> "_"
{
  const s = sanitizeAgent("a/b\\c");
  assert.ok(!s.includes("/") && !s.includes("\\"), "no path separators survive sanitize");
}

// --- computeProfileDir ---------------------------------------------------
{
  const dir = computeProfileDir("/home/u", "sales");
  assert.strictEqual(dir, path.join("/home/u", ".oto-dock", "browser-profiles", "sales"));
  // Lives under the satellite persistent root, OUTSIDE any MCP/workspace dir.
  assert.ok(dir.includes(path.join(".oto-dock", "browser-profiles")));
  assert.ok(!dir.includes("node_modules"));
  assert.ok(!dir.includes(path.join("mcps", "custom")));
  assert.ok(!dir.includes(path.join("agents")), "profile must not be inside the synced agent tree");
}
{
  // A hostile agent name can never escape the browser-profiles directory.
  const base = path.join("/home/u", ".oto-dock", "browser-profiles");
  const evil = computeProfileDir("/home/u", "../../../etc/cron.d");
  assert.ok(path.resolve(evil).startsWith(path.resolve(base)), "no path traversal via agent name");
  const evil2 = computeProfileDir("/home/u", "..");
  assert.ok(path.resolve(evil2).startsWith(path.resolve(base)), "'..' agent cannot traverse");
}

// --- resolveCliPath ------------------------------------------------------
{
  // No node_modules present in a temp dir -> null (the wrapper then errors cleanly).
  const missing = resolveCliPath(path.join(__dirname, "does-not-exist"));
  assert.ok(missing === null || typeof missing === "string");
}

// --- resolveBrowserConfig (explicit override vs auto-detection) ---------
assert.deepStrictEqual(resolveBrowserConfig("chrome", null), { browser: "chrome" });
assert.deepStrictEqual(resolveBrowserConfig("msedge", { channel: "chrome", executablePath: "/c" }), { browser: "msedge" }); // explicit wins over detection
assert.deepStrictEqual(resolveBrowserConfig("AUTO", { channel: "msedge", executablePath: "/e" }), { browser: "msedge", executablePath: "/e" }); // 'auto' is case-insensitive
assert.deepStrictEqual(resolveBrowserConfig("auto", { channel: "chrome", executablePath: "/c" }), { browser: "chrome", executablePath: "/c" });
assert.deepStrictEqual(resolveBrowserConfig("auto", { executablePath: "/opt/brave" }), { browser: "chromium", executablePath: "/opt/brave" });
assert.deepStrictEqual(resolveBrowserConfig("", null), { browser: "chrome" }); // last resort
assert.deepStrictEqual(resolveBrowserConfig(undefined, null), { browser: "chrome" });
// explicit channel resolves its exe through the lookup so the daemon can launch it
assert.deepStrictEqual(
  resolveBrowserConfig("msedge", null, (ch) => (ch === "msedge" ? "/edge" : null)),
  { browser: "msedge", executablePath: "/edge" }
);

// detectBrowser returns null or a {channel?, executablePath} shape (never throws)
{
  const det = detectBrowser();
  assert.ok(det === null || typeof det.channel === "string" || typeof det.executablePath === "string");
}

// --- isChromiumFamily -----------------------------------------------------
assert.strictEqual(isChromiumFamily("chrome"), true);
assert.strictEqual(isChromiumFamily("msedge"), true);
assert.strictEqual(isChromiumFamily("chromium"), true);
assert.strictEqual(isChromiumFamily("firefox"), false);
assert.strictEqual(isChromiumFamily("webkit"), false);
assert.strictEqual(isChromiumFamily(""), false);

// --- findOrphanHolders (never kill a browser a live session owns) ---------
{
  const profile = "/home/u/.oto-dock/browser-profiles/sales";
  const holderCmd = `chrome --user-data-dir=${profile} --remote-debugging-port=0`;
  // Live node parent → owned, NOT an orphan.
  let procs = [
    { pid: 10, ppid: 1, name: "node", started: 1000, cmd: "node cli.js" },
    { pid: 20, ppid: 10, name: "chrome", started: 2000, cmd: holderCmd },
  ];
  assert.deepStrictEqual(findOrphanHolders(procs, profile), []);
  // Parent missing from the table (dead) → orphan.
  procs = [{ pid: 20, ppid: 99, name: "chrome", started: 2000, cmd: holderCmd }];
  assert.strictEqual(findOrphanHolders(procs, profile).length, 1);
  // Re-parented to init → orphan.
  procs = [
    { pid: 1, ppid: 0, name: "init", started: 0, cmd: "/sbin/init" },
    { pid: 20, ppid: 1, name: "chrome", started: 2000, cmd: holderCmd },
  ];
  assert.strictEqual(findOrphanHolders(procs, profile).length, 1);
  // Adopted by a systemd subreaper → orphan.
  procs = [
    { pid: 500, ppid: 1, name: "systemd", started: 0, cmd: "systemd --user" },
    { pid: 20, ppid: 500, name: "chrome", started: 2000, cmd: holderCmd },
  ];
  assert.strictEqual(findOrphanHolders(procs, profile).length, 1);
  // PID-reuse impostor: recorded parent born AFTER the child → orphan.
  procs = [
    { pid: 10, ppid: 4, name: "someapp", started: 3000, cmd: "someapp" },
    { pid: 20, ppid: 10, name: "chrome", started: 2000, cmd: holderCmd },
  ];
  assert.strictEqual(findOrphanHolders(procs, profile).length, 1);
  // Renderer children (--type=) are never candidates — only the main process.
  procs = [
    { pid: 20, ppid: 99, name: "chrome", started: 2000, cmd: `${holderCmd} --type=renderer` },
  ];
  assert.deepStrictEqual(findOrphanHolders(procs, profile), []);
  // A different profile's browser is never touched.
  procs = [
    { pid: 20, ppid: 99, name: "chrome", started: 2000, cmd: "chrome --user-data-dir=/other/profile" },
  ];
  assert.deepStrictEqual(findOrphanHolders(procs, profile), []);
}

// --- agentDisplayName ------------------------------------------------------
assert.strictEqual(agentDisplayName("personal-assistant"), "Personal Assistant (OtoDock)");
assert.strictEqual(agentDisplayName("sales_bot"), "Sales Bot (OtoDock)");
assert.strictEqual(agentDisplayName(""), "Default (OtoDock)");

// --- seedProfileDisplayName (name-only — Chromium scrubs custom pictures
// from signed-out profiles, so no avatar keys are written) ------------------
{
  const os = require("os");
  const fs = require("fs");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "bmcp-seed-"));
  seedProfileDisplayName(tmp, "personal-assistant");
  const ls = JSON.parse(fs.readFileSync(path.join(tmp, "Local State"), "utf8"));
  const info = ls.profile.info_cache.Default;
  assert.strictEqual(info.name, "Personal Assistant (OtoDock)");
  assert.strictEqual(info.is_using_default_name, false);
  assert.strictEqual(info.use_gaia_picture, undefined);
  assert.strictEqual(info.gaia_picture_file_name, undefined);
  fs.rmSync(tmp, { recursive: true, force: true });
}

// --- readDevtoolsPort ------------------------------------------------------
{
  const os = require("os");
  const fs = require("fs");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "bmcp-test-"));
  assert.strictEqual(readDevtoolsPort(tmp), null); // no file
  fs.writeFileSync(path.join(tmp, "DevToolsActivePort"), "38121\n/devtools/browser/abc");
  assert.strictEqual(readDevtoolsPort(tmp), 38121);
  fs.writeFileSync(path.join(tmp, "DevToolsActivePort"), "garbage\n");
  assert.strictEqual(readDevtoolsPort(tmp), null);
  fs.rmSync(tmp, { recursive: true, force: true });
}

// --- LineSplitter (NDJSON framing for the CDP supervisor) ------------------
{
  const ls = new LineSplitter();
  assert.deepStrictEqual(ls.feed(Buffer.from('{"a":1}\n{"b"')), ['{"a":1}']);
  assert.deepStrictEqual(ls.feed(Buffer.from(':2}\n')), ['{"b":2}']); // partials join
  assert.deepStrictEqual(ls.feed(Buffer.from('\n\n')), []); // empty lines dropped
  assert.deepStrictEqual(ls.feed(Buffer.from('{"c":3}\r\n')), ['{"c":3}']); // CRLF stripped
}

// --- peekRpc ---------------------------------------------------------------
{
  assert.deepStrictEqual(peekRpc('{"jsonrpc":"2.0","id":7,"method":"tools/call"}'),
    { id: 7, method: "tools/call", toolName: undefined, isRequest: true });
  assert.deepStrictEqual(peekRpc('{"jsonrpc":"2.0","id":7,"result":{}}'),
    { id: 7, method: undefined, toolName: undefined, isRequest: false }); // response, not request
  assert.deepStrictEqual(peekRpc('{"jsonrpc":"2.0","method":"notifications/initialized"}'),
    { id: undefined, method: "notifications/initialized", toolName: undefined, isRequest: false });
  assert.deepStrictEqual(peekRpc("not json"),
    { id: undefined, method: undefined, toolName: undefined, isRequest: false });
  // toolName only for tools/call — the browser_close no-launch gate keys on it.
  assert.deepStrictEqual(
    peekRpc('{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"browser_close","arguments":{}}}'),
    { id: 9, method: "tools/call", toolName: "browser_close", isRequest: true });
  assert.deepStrictEqual(
    peekRpc('{"jsonrpc":"2.0","id":9,"method":"tools/list","params":{"name":"x"}}'),
    { id: 9, method: "tools/list", toolName: undefined, isRequest: true });
}

// --- rewriteInitId / buildErrorResponse -------------------------------------
{
  const init = '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"clientInfo":{"name":"x"}}}';
  const replayed = JSON.parse(rewriteInitId(init));
  assert.strictEqual(replayed.id, REINIT_ID);
  assert.strictEqual(replayed.method, "initialize");
  assert.deepStrictEqual(replayed.params, { clientInfo: { name: "x" } }); // params preserved

  const err = JSON.parse(buildErrorResponse(42, "retry"));
  assert.strictEqual(err.id, 42);
  assert.strictEqual(err.error.code, -32000);
  assert.strictEqual(err.error.message, "retry");
  assert.strictEqual(err.jsonrpc, "2.0");

  // buildToolTextResponse: the supervisor's own successful tools/call reply
  // (browser_close with no browser running must NOT launch one just to close).
  const ok = JSON.parse(buildToolTextResponse(43, "nothing to close"));
  assert.strictEqual(ok.id, 43);
  assert.strictEqual(ok.jsonrpc, "2.0");
  assert.deepStrictEqual(ok.result.content, [{ type: "text", text: "nothing to close" }]);
  assert.strictEqual(ok.error, undefined);
}

console.log("browser-control wrapper: all tests passed");
