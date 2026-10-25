"use strict";
// Own-browser mode + Firefox fallback — the pure helpers behind the
// extension supervisor. Run with: node tests/own_mode.test.js
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const {
  resolveBrowserConfig, extensionBrowserEnv, defaultUserDataDir,
  extensionLooksInstalled, rewriteClientName, toolErrorText,
  isClosedBrowserError, connectTimeoutMs, firefoxCandidates,
  playwrightFirefoxPath, EXTENSION_ID, EXTENSION_INSTALL_URL,
} = require("../index.js");

// --- resolveBrowserConfig: Firefox fallback only with a system Firefox -----
{
  // No Chromium, system Firefox present → Playwright's Firefox build.
  const r = resolveBrowserConfig("auto", null, null, "/usr/bin/firefox");
  assert.deepStrictEqual(r, { browser: "firefox", firefoxFallback: true });
  // No browser at all → the old "chrome" last resort (stderr hint), no download.
  assert.deepStrictEqual(resolveBrowserConfig("auto", null, null, null), { browser: "chrome" });
  // A detected Chromium browser always wins over Firefox.
  assert.deepStrictEqual(
    resolveBrowserConfig("auto", { channel: "chrome", executablePath: "/usr/bin/google-chrome" }, null, "/usr/bin/firefox"),
    { browser: "chrome", executablePath: "/usr/bin/google-chrome" }
  );
  assert.deepStrictEqual(
    resolveBrowserConfig("auto", { executablePath: "/usr/bin/brave" }, null, "/usr/bin/firefox"),
    { browser: "chromium", executablePath: "/usr/bin/brave" }
  );
  // An explicit channel is honoured regardless of Firefox.
  assert.deepStrictEqual(resolveBrowserConfig("webkit", null, () => null, "/usr/bin/firefox"), { browser: "webkit" });
  // The 3-arg legacy call keeps working.
  assert.deepStrictEqual(resolveBrowserConfig("auto", null, null), { browser: "chrome" });
  assert.ok(firefoxCandidates().length >= 1);
  assert.ok(typeof playwrightFirefoxPath() === "string" || playwrightFirefoxPath() === null);
}

// --- extensionBrowserEnv: Chrome/Edge/Brave only, never channel "chromium" --
{
  assert.deepStrictEqual(
    extensionBrowserEnv({ browser: "chrome", executablePath: "/usr/bin/google-chrome" }),
    { PLAYWRIGHT_MCP_BROWSER: "chrome", PLAYWRIGHT_MCP_EXECUTABLE_PATH: "/usr/bin/google-chrome" }
  );
  assert.deepStrictEqual(
    extensionBrowserEnv({ browser: "msedge", executablePath: "/usr/bin/microsoft-edge" }),
    { PLAYWRIGHT_MCP_BROWSER: "msedge", PLAYWRIGHT_MCP_EXECUTABLE_PATH: "/usr/bin/microsoft-edge" }
  );
  // Brave (detected by path → "chromium") is passed as "chrome" + its exe:
  // upstream adds --no-sandbox to the connect-page spawn for "chromium".
  assert.deepStrictEqual(
    extensionBrowserEnv({ browser: "chromium", executablePath: "/usr/bin/brave-browser" }),
    { PLAYWRIGHT_MCP_BROWSER: "chrome", PLAYWRIGHT_MCP_EXECUTABLE_PATH: "/usr/bin/brave-browser" }
  );
  // No executable / non-Chromium → own mode impossible (dedicated fallback).
  assert.strictEqual(extensionBrowserEnv({ browser: "chrome" }), null);
  assert.strictEqual(extensionBrowserEnv({ browser: "firefox", firefoxFallback: true }), null);
  assert.strictEqual(extensionBrowserEnv({ browser: "webkit", executablePath: "/x" }), null);
  assert.strictEqual(extensionBrowserEnv(null), null);
}

// --- defaultUserDataDir: per browser + platform ----------------------------
{
  const h = "/home/u";
  assert.strictEqual(defaultUserDataDir("/usr/bin/google-chrome", h, "linux"), "/home/u/.config/google-chrome");
  assert.strictEqual(defaultUserDataDir("/usr/bin/microsoft-edge-stable", h, "linux"), "/home/u/.config/microsoft-edge");
  assert.strictEqual(defaultUserDataDir("/usr/bin/brave-browser", h, "linux"), "/home/u/.config/BraveSoftware/Brave-Browser");
  assert.strictEqual(defaultUserDataDir("/usr/bin/vivaldi", h, "linux"), "/home/u/.config/vivaldi");
  assert.strictEqual(defaultUserDataDir("/usr/bin/chromium", h, "linux"), "/home/u/.config/chromium");
  assert.strictEqual(
    defaultUserDataDir("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", h, "darwin"),
    "/home/u/Library/Application Support/Google/Chrome"
  );
  assert.strictEqual(
    defaultUserDataDir("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", h, "darwin"),
    "/home/u/Library/Application Support/BraveSoftware/Brave-Browser"
  );
  const win = defaultUserDataDir("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Users\\u", "win32", "C:\\Users\\u\\AppData\\Local");
  assert.ok(win.endsWith(path.join("Google", "Chrome", "User Data")), win);
  assert.ok(win.startsWith("C:"), win);
  const edgeWin = defaultUserDataDir("C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", "C:\\Users\\u", "win32", "C:\\Users\\u\\AppData\\Local");
  assert.ok(edgeWin.includes(path.join("Microsoft", "Edge")), edgeWin);
}

// --- extensionLooksInstalled: Extensions dir or Preferences mention ---------
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "own-mode-"));
  assert.strictEqual(extensionLooksInstalled(path.join(root, "missing")), false);
  const udd = path.join(root, "udd");
  fs.mkdirSync(path.join(udd, "Default"), { recursive: true });
  assert.strictEqual(extensionLooksInstalled(udd), false);
  fs.writeFileSync(path.join(udd, "Default", "Preferences"), JSON.stringify({ extensions: { settings: { [EXTENSION_ID]: {} } } }));
  assert.strictEqual(extensionLooksInstalled(udd), true);
  const udd2 = path.join(root, "udd2");
  fs.mkdirSync(path.join(udd2, "Profile 3", "Extensions", EXTENSION_ID), { recursive: true });
  assert.strictEqual(extensionLooksInstalled(udd2), true);
  // A non-profile dir is ignored.
  const udd3 = path.join(root, "udd3");
  fs.mkdirSync(path.join(udd3, "Guest Profile", "Extensions", EXTENSION_ID), { recursive: true });
  assert.strictEqual(extensionLooksInstalled(udd3), false);
  fs.rmSync(root, { recursive: true, force: true });
  assert.ok(EXTENSION_INSTALL_URL.endsWith(EXTENSION_ID));
}

// --- rewriteClientName: the tab group carries the agent's name -------------
{
  const init = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize",
    params: { protocolVersion: "2024-11-05", clientInfo: { name: "claude-code", version: "2.1" }, capabilities: {} } });
  const out = JSON.parse(rewriteClientName(init, "Sales (OtoDock)"));
  assert.deepStrictEqual(out.params.clientInfo, { name: "Sales (OtoDock)", version: "2.1" });
  assert.strictEqual(out.id, 1);
  // Missing clientInfo is created; a non-JSON line is forwarded verbatim.
  const noInfo = JSON.stringify({ jsonrpc: "2.0", id: 2, method: "initialize", params: {} });
  assert.deepStrictEqual(JSON.parse(rewriteClientName(noInfo, "X")).params.clientInfo, { name: "X" });
  assert.strictEqual(rewriteClientName("garbage", "X"), "garbage");
}

// --- toolErrorText / isClosedBrowserError -----------------------------------
{
  const ok = JSON.stringify({ jsonrpc: "2.0", id: 5, result: { content: [{ type: "text", text: "- Page URL: x" }] } });
  assert.strictEqual(toolErrorText(ok), null);
  const err = JSON.stringify({ jsonrpc: "2.0", id: 5, result: { isError: true, content: [{ type: "text", text: "### Error\nError: Target page, context or browser has been closed" }] } });
  assert.ok(toolErrorText(err).includes("has been closed"));
  assert.ok(isClosedBrowserError(toolErrorText(err)));
  const rpcErr = JSON.stringify({ jsonrpc: "2.0", id: 6, error: { code: -32000, message: "Extension not connected" } });
  assert.strictEqual(toolErrorText(rpcErr), "Extension not connected");
  assert.ok(isClosedBrowserError("Extension disconnected: User disconnected"));
  assert.ok(isClosedBrowserError("Error: async initializeServer: connect ECONNREFUSED 127.0.0.1:40263"));
  assert.ok(!isClosedBrowserError("Timeout 5000ms exceeded"));
  assert.ok(!isClosedBrowserError(null));
  assert.strictEqual(toolErrorText("not json"), null);
  assert.strictEqual(toolErrorText(JSON.stringify({ jsonrpc: "2.0", method: "notifications/x" })), null);
}

// --- connectTimeoutMs -------------------------------------------------------
{
  assert.strictEqual(connectTimeoutMs(true), 45000);
  assert.strictEqual(connectTimeoutMs(false), 90000);
}

console.log("own_mode.test.js: all assertions passed");
