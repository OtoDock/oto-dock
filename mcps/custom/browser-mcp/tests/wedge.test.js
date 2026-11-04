"use strict";
// A shared browser that lets no new session attach — run with: node tests/wedge.test.js
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const {
  profileHolders, findOrphanHolders, daemonLaunchArgs, tryLaunchLock, releaseLaunchLock,
  LAUNCH_LOCK, LAUNCH_LOCK_STALE_MS, isWedgedAttachError, wedgeAction, endpointPort,
  setAsideSessions,
} = require("../index.js");

// --- the error that means "the browser lets no new session attach" ----------
// The text the field and the reproduction both showed, word for word.
const FIELD =
  "### Error\nTimeoutError: async initializeServer: Timeout 30000ms exceeded.\nCall log:\n" +
  "  - <ws preparing> retrieving websocket url from http://127.0.0.1:33161\n" +
  "  - <ws connecting> ws://127.0.0.1:33161/devtools/browser/9ee5d5c7\n" +
  "  - <ws connected> ws://127.0.0.1:33161/devtools/browser/9ee5d5c7\n";
assert.strictEqual(isWedgedAttachError(FIELD), true);
// The same failure under another bundle's naming.
assert.strictEqual(
  isWedgedAttachError("browserType.connectOverCDP: Timeout 15000ms exceeded.\n - <ws connected> ws://x"),
  true
);
// A websocket that never connected is a gone browser, not a wedged one.
assert.strictEqual(
  isWedgedAttachError("TimeoutError: async initializeServer: Timeout 30000ms exceeded.\n - <ws connecting> ws://x"),
  false
);
// A page that loads slowly is the page's problem.
assert.strictEqual(isWedgedAttachError("page.goto: Timeout 60000ms exceeded.\n - <ws connected>"), false);
assert.strictEqual(isWedgedAttachError("connect ECONNREFUSED 127.0.0.1:33161"), false);
assert.strictEqual(isWedgedAttachError(null), false);

// --- what a session does about it -------------------------------------------
const EP = "http://127.0.0.1:33161";
assert.strictEqual(wedgeAction({ handled: false, childEndpoint: EP, live: EP, sameBrowser: true }), "end-and-relaunch");
// Another session already started a fresh browser on another port.
assert.strictEqual(
  wedgeAction({ handled: false, childEndpoint: EP, live: "http://127.0.0.1:40001", sameBrowser: true }),
  "attach"
);
// The browser is gone already: start one, end nothing.
assert.strictEqual(wedgeAction({ handled: false, childEndpoint: EP, live: null, sameBrowser: false }), "relaunch");
// Something answers on the port but it is not this profile's browser.
assert.strictEqual(wedgeAction({ handled: false, childEndpoint: EP, live: EP, sameBrowser: false }), "pass");
// One recovery per session.
assert.strictEqual(wedgeAction({ handled: true, childEndpoint: EP, live: EP, sameBrowser: true }), "pass");

assert.strictEqual(endpointPort("http://127.0.0.1:33161"), 33161);
assert.strictEqual(endpointPort("http://127.0.0.1:33161/"), 33161);
assert.strictEqual(endpointPort(null), null);

// --- the profile's browser, and only that profile's ------------------------
const P = "/home/u/.oto-dock/browser-profiles/test";
const procs = [
  { pid: 10, ppid: 1, name: "chrome", cmd: `/opt/chrome --user-data-dir=${P} --remote-debugging-port=0` },
  { pid: 11, ppid: 10, name: "chrome", cmd: `/opt/chrome --type=renderer --user-data-dir=${P}` },
  // A profile whose name starts with this one's: never this profile's browser.
  { pid: 20, ppid: 1, name: "chrome", cmd: `/opt/chrome --user-data-dir=${P}-agent --remote-debugging-port=0` },
  { pid: 30, ppid: 1, name: "chrome", cmd: `/opt/chrome --user-data-dir=${P}` },
  { pid: 40, ppid: 1, name: "chrome.exe", cmd: `"C:\\chrome.exe" "--user-data-dir=${P}" --no-first-run` },
];
assert.deepStrictEqual(profileHolders(procs, P).map((p) => p.pid), [10, 30, 40]);
assert.deepStrictEqual(profileHolders(procs, `${P}-agent`).map((p) => p.pid), [20]);
// The orphan sweep uses the same match: the prefix-named profile's orphan is safe.
assert.deepStrictEqual(findOrphanHolders(procs, P).map((p) => p.pid), [10, 30, 40]);

// --- the launch -------------------------------------------------------------
assert.ok(daemonLaunchArgs(P).includes("--restore-last-session"));
assert.ok(!daemonLaunchArgs(P, { restoreLastSession: false }).includes("--restore-last-session"));
assert.strictEqual(daemonLaunchArgs(P, { restoreLastSession: false })[0], `--user-data-dir=${P}`);

// --- one launch at a time per profile --------------------------------------
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wedge-test-"));
assert.strictEqual(tryLaunchLock(dir), true);
assert.strictEqual(tryLaunchLock(dir), false); // held by the first
releaseLaunchLock(dir);
assert.strictEqual(tryLaunchLock(dir), true);
// A holder that died long ago: the lock is free again.
const later = Date.now() + LAUNCH_LOCK_STALE_MS + 1000;
assert.strictEqual(tryLaunchLock(dir, later), true);
assert.ok(fs.existsSync(path.join(dir, LAUNCH_LOCK)));
releaseLaunchLock(dir);
releaseLaunchLock(dir); // twice is harmless
assert.ok(!fs.existsSync(path.join(dir, LAUNCH_LOCK)));

// --- the tabs the browser would reopen go aside ----------------------------
assert.strictEqual(setAsideSessions(dir), false); // nothing to move
fs.mkdirSync(path.join(dir, "Default", "Sessions"), { recursive: true });
fs.writeFileSync(path.join(dir, "Default", "Sessions", "Session_1"), "tabs");
assert.strictEqual(setAsideSessions(dir), true);
assert.ok(!fs.existsSync(path.join(dir, "Default", "Sessions")));
assert.strictEqual(fs.readFileSync(path.join(dir, "Default", "Sessions.before-restart", "Session_1"), "utf8"), "tabs");
// A second wedge replaces the older copy.
fs.mkdirSync(path.join(dir, "Default", "Sessions"));
fs.writeFileSync(path.join(dir, "Default", "Sessions", "Session_2"), "newer");
assert.strictEqual(setAsideSessions(dir), true);
assert.deepStrictEqual(fs.readdirSync(path.join(dir, "Default", "Sessions.before-restart")), ["Session_2"]);
fs.rmSync(dir, { recursive: true, force: true });

console.log("wedge tests: ok");
