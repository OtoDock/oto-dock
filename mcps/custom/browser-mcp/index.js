#!/usr/bin/env node
"use strict";

// OtoDock browser-control wrapper — a thin launcher for @playwright/mcp.
//
// Its ONLY runtime job is to point the MCP at a DEDICATED, per-agent,
// PERSISTENT browser profile that lives OUTSIDE this MCP's directory:
//
//     <home>/.oto-dock/browser-profiles/<agent>/
//
// Why a wrapper (and not a static manifest arg): the profile path depends on
// the satellite's home directory + the agent slug, which are only known at
// runtime on the machine. It MUST live outside the MCP dir because:
//   * sync_mcps' atomic dir-swap preserves only keys/config/screenshots, so a
//     profile inside the MCP dir would be wiped (logging the user out) on every
//     MCP update; and
//   * the profile (cookies, saved sessions) must never sit inside the synced
//     agent workspace, or those credentials would be copied back to the proxy.
//
// Everything else is configured via PLAYWRIGHT_MCP_* env vars that
// @playwright/mcp reads natively, set upstream of this process:
//   * PLAYWRIGHT_MCP_BLOCKED_ORIGINS  — manifest `env` (loopback safety default)
//   * PLAYWRIGHT_MCP_ALLOWED_ORIGINS  — proxy per-agent injection (optional)
//   * OTO_BROWSER_CHANNEL             — admin/user config (which browser)
//
// We use the PERSISTENT profile (the @playwright/mcp default) — NOT --isolated,
// which would throw the logins away between sessions.
//
// The wrapper also serves the `studio_*` capture-studio tool family
// (studio.js) in-process: both forwarding modes route those tools/call
// requests to the studio instead of the bundled mcp, and append the studio
// tool definitions to forwarded tools/list responses. The studio drives its
// OWN Xvfb display + Chrome — it works even while the everyday browser (or
// the cli.js child) is down.

const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const studio = require("./studio.js");

// OTO_AGENT_NAME is already a filesystem-safe slug; defensively keep only
// path-safe characters and never allow a "." (so a name can never become a
// "."/".." traversal segment).
function sanitizeAgent(name) {
  const s = String(name || "").replace(/[^A-Za-z0-9_-]/g, "_");
  if (!s || /^_+$/.test(s)) return "default";
  return s;
}

function computeProfileDir(home, agent) {
  return path.join(home, ".oto-dock", "browser-profiles", sanitizeAgent(agent));
}

// npm installs @playwright/mcp into THIS MCP dir's node_modules (a single
// dependency, so no hoisting). The package's bin "playwright-mcp" points at
// cli.js in the package root.
function resolveCliPath(baseDir) {
  const direct = path.join(baseDir, "node_modules", "@playwright", "mcp", "cli.js");
  if (fs.existsSync(direct)) return direct;
  try {
    return require.resolve("@playwright/mcp/cli.js", { paths: [baseDir] });
  } catch (_) {
    return null;
  }
}

// Per-platform installed-browser candidate table, shared by auto-detection and
// the explicit-channel executable lookup (the CDP daemon needs a concrete exe
// path even when the user configured a channel by name).
function browserCandidates() {
  const plat = process.platform;
  if (plat === "win32") {
    const pf = process.env["ProgramFiles"] || "C:\\Program Files";
    const pf86 = process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";
    const la = process.env["LOCALAPPDATA"] || "";
    return [
      { channel: "chrome", paths: [`${pf}\\Google\\Chrome\\Application\\chrome.exe`, `${pf86}\\Google\\Chrome\\Application\\chrome.exe`, `${la}\\Google\\Chrome\\Application\\chrome.exe`] },
      { channel: "msedge", paths: [`${pf}\\Microsoft\\Edge\\Application\\msedge.exe`, `${pf86}\\Microsoft\\Edge\\Application\\msedge.exe`] },
      { paths: [`${pf}\\BraveSoftware\\Brave-Browser\\Application\\brave.exe`, `${pf86}\\BraveSoftware\\Brave-Browser\\Application\\brave.exe`] },
      { paths: [`${pf}\\Vivaldi\\Application\\vivaldi.exe`, `${la}\\Vivaldi\\Application\\vivaldi.exe`] },
    ];
  }
  if (plat === "darwin") {
    return [
      { channel: "chrome", paths: ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"] },
      { channel: "msedge", paths: ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"] },
      { paths: ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"] },
      { paths: ["/Applications/Vivaldi.app/Contents/MacOS/Vivaldi", "/Applications/Chromium.app/Contents/MacOS/Chromium"] },
    ];
  }
  return [
    { channel: "chrome", paths: ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/opt/google/chrome/chrome"] },
    { channel: "msedge", paths: ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"] },
    { paths: ["/usr/bin/brave-browser", "/usr/bin/brave", "/snap/bin/brave"] },
    { paths: ["/usr/bin/vivaldi", "/usr/bin/vivaldi-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium"] },
  ];
}

// Auto-detect an installed, drivable browser on THIS machine (a fleet may have
// Chrome on one box, Edge or Brave on another — so the choice belongs per-machine,
// not in a single admin config). Returns the Playwright channel for system
// Chrome/Edge (no download) and/or the executablePath for other Chromium-family
// browsers (Brave/Vivaldi/Opera/Chromium) — the exe path is always included so
// the CDP daemon can launch the browser itself. Firefox/WebKit are Playwright's
// OWN downloaded builds (not the system Firefox), so we don't auto-pick them —
// set the channel explicitly via OTO_BROWSER_CHANNEL if that's what you want.
function detectBrowser() {
  for (const c of browserCandidates()) {
    for (const p of c.paths) {
      if (p && fs.existsSync(p)) {
        return c.channel ? { channel: c.channel, executablePath: p } : { executablePath: p };
      }
    }
  }
  return null;
}

// Concrete exe path for an explicitly-configured channel (chrome / msedge),
// so an explicit channel still gets the CDP daemon. Null when not found.
function channelExecutable(channel) {
  for (const c of browserCandidates()) {
    if (c.channel !== channel) continue;
    for (const p of c.paths) {
      if (p && fs.existsSync(p)) return p;
    }
  }
  return null;
}

// --- Orphaned-browser recovery ---------------------------------------------
//
// The dedicated profile can drive only ONE browser at a time (Chromium's
// ProcessSingleton). A browser that outlives its session — Chrome's
// self-relaunch detaches it from the MCP's process tree, so a session
// teardown's tree-kill can miss it — holds the profile lock forever, and every
// later session's browser tools fail with "Browser is already in use for
// <profile>". Nobody owns that zombie, so the wrapper reaps it at startup.
//
// Safety rule: NEVER kill a browser another live session owns. A browser
// launched by a live playwright-mcp has a live node parent; we only kill a
// holder that is provably orphaned — its parent is dead, it was re-parented to
// init/systemd (Unix), or its recorded parent is a PID-reuse impostor (born
// AFTER the child, Windows). Anything ambiguous is left alone and the
// existing "close the other session" stderr hint fires as before. The whole
// sweep runs only when the profile actually looks locked, so the common
// startup path costs nothing.

// Mirror playwright-core's own isProfileLocked probe: on Windows a live
// Chromium holds <profile>/lockfile open exclusively (an r+ open fails); on
// Unix SingletonLock is a symlink to "<host>-<pid>" and the lock is live only
// while that pid is.
function profileLooksLocked(profileDir) {
  if (process.platform === "win32") {
    const lockPath = path.join(profileDir, "lockfile");
    try {
      fs.closeSync(fs.openSync(lockPath, "r+"));
      return false;
    } catch (e) {
      return e.code !== "ENOENT";
    }
  }
  try {
    const target = fs.readlinkSync(path.join(profileDir, "SingletonLock"));
    const pid = parseInt(target.split("-").pop() || "", 10);
    if (isNaN(pid)) return false;
    process.kill(pid, 0);
    return true;
  } catch (_) {
    return false;
  }
}

// Platform process table → [{pid, ppid, name, started (ms|null), cmd}].
function listProcesses() {
  if (process.platform === "win32") {
    const script =
      "Get-CimInstance Win32_Process | " +
      "Select-Object ProcessId,ParentProcessId,Name,CommandLine," +
      "@{n='Started';e={if($_.CreationDate){([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds()}}} | " +
      "ConvertTo-Json -Compress";
    const out = execFileSync(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-Command", script],
      { encoding: "utf8", maxBuffer: 32 * 1024 * 1024, windowsHide: true }
    );
    const rows = JSON.parse(out || "[]");
    return (Array.isArray(rows) ? rows : [rows]).map((r) => ({
      pid: r.ProcessId,
      ppid: r.ParentProcessId,
      name: String(r.Name || ""),
      started: typeof r.Started === "number" ? r.Started : null,
      cmd: String(r.CommandLine || ""),
    }));
  }
  const out = execFileSync("ps", ["-eo", "pid=,ppid=,comm=,args="], {
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
  });
  return out
    .split("\n")
    .map((line) => line.match(/^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$/))
    .filter(Boolean)
    .map((m) => ({
      pid: parseInt(m[1], 10),
      ppid: parseInt(m[2], 10),
      name: m[3],
      started: null,
      cmd: m[4],
    }));
}

// Pure decision: which profile holders are provably orphaned? A holder is a
// browser MAIN process (no --type=, killing it tears down its own children)
// whose command line names this exact profile dir. `procs` is the full table
// from listProcesses().
function findOrphanHolders(procs, profileDir) {
  const byPid = new Map(procs.map((p) => [p.pid, p]));
  const holders = procs.filter(
    (p) => p.cmd.includes(`--user-data-dir=${profileDir}`) && !/--type=/.test(p.cmd)
  );
  return holders.filter((h) => {
    const parent = byPid.get(h.ppid);
    if (!parent) return true; // parent dead → orphan
    if (h.ppid <= 1) return true; // re-parented to init → orphan
    if (/^(systemd|init)/.test(parent.name)) return true; // subreaper adoption → orphan
    if (h.started != null && parent.started != null && parent.started > h.started)
      return true; // recorded parent is a PID-reuse impostor born after the child
    return false; // a live process owns it — never kill
  });
}

function killPid(pid) {
  if (process.platform === "win32") {
    execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
    });
    return;
  }
  process.kill(pid, "SIGKILL");
}

function sleepMs(ms) {
  const buf = new SharedArrayBuffer(4);
  Atomics.wait(new Int32Array(buf), 0, 0, ms);
}

function reapOrphanedBrowser(profileDir) {
  if (!profileLooksLocked(profileDir)) return;
  let orphans = [];
  try {
    orphans = findOrphanHolders(listProcesses(), profileDir);
  } catch (e) {
    process.stderr.write(
      `[browser-control] Profile is locked and the orphan sweep failed (${e.message}); ` +
        "leaving it to the launch error.\n"
    );
    return;
  }
  if (!orphans.length) return; // a live session owns the browser — leave it
  for (const o of orphans) {
    process.stderr.write(
      `[browser-control] Reaping orphaned browser pid=${o.pid} (${o.name}) holding ` +
        `${profileDir} — its owning session is gone.\n`
    );
    try {
      killPid(o.pid);
    } catch (_) {
      /* already exited */
    }
  }
  // The OS releases the lock on process death; give it a bounded moment so
  // the launch below doesn't race the teardown.
  for (let i = 0; i < 20 && profileLooksLocked(profileDir); i++) sleepMs(250);
}

// --- Shared browser daemon (CDP attach) -------------------------------------
//
// Chromium's ProcessSingleton allows only ONE browser process per profile, so
// per-session `launchPersistentContext` means one session at a time and a
// wedged profile whenever a browser outlives its session. Instead, the profile
// is served by a LONG-LIVED headed browser ("daemon") that no session owns:
// the wrapper launches it once — detached through an intermediary shell so a
// session teardown's process-tree kill can never reap it — with
// --remote-debugging-port=0, and every MCP session ATTACHES to it over CDP
// (`--cdp-endpoint`). That gives:
//   * concurrent sessions on the same browser + cookies (CDP multiplexes),
//   * tabs that survive session switches and session death,
//   * no profile lock to go stale (nobody launches on a locked profile).
// The endpoint is discovered from Chromium's own DevToolsActivePort file in
// the profile dir and verified live via /json/version before use. The debug
// port binds to 127.0.0.1 only (Chromium default) on a dedicated automation
// profile. Origin blocking (PLAYWRIGHT_MCP_BLOCKED_ORIGINS) is enforced by
// @playwright/mcp via context.route on the attached context, so it applies in
// CDP mode too. Firefox/WebKit (no CDP) keep the classic persistent launch.

function readDevtoolsPort(profileDir) {
  try {
    const first = fs
      .readFileSync(path.join(profileDir, "DevToolsActivePort"), "utf8")
      .split("\n")[0]
      .trim();
    const port = parseInt(first, 10);
    return Number.isInteger(port) && port > 0 ? port : null;
  } catch (_) {
    return null;
  }
}

function probeCdp(port, timeoutMs) {
  return new Promise((resolve) => {
    const http = require("http");
    const req = http.get(
      { host: "127.0.0.1", port, path: "/json/version", timeout: timeoutMs },
      (res) => {
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => {
          try {
            resolve(Boolean(JSON.parse(body).webSocketDebuggerUrl));
          } catch (_) {
            resolve(false);
          }
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

async function liveCdpEndpoint(profileDir) {
  const port = readDevtoolsPort(profileDir);
  if (!port) return null;
  return (await probeCdp(port, 500)) ? `http://127.0.0.1:${port}` : null;
}

// Best-effort profile display name — the browser window's avatar badge shows
// "<Agent Name> (OtoDock)" instead of "Person 1"/"Guest". Name ONLY: a custom
// avatar IMAGE is not possible for a signed-out profile — Chromium's identity
// service scrubs non-GAIA profile pictures at startup (deletes the picture
// file and clears gaia_picture_file_name; verified live), so seeding one is
// pure churn. Chromium reads these files at startup only, and we only get
// here when no browser runs on the profile, so the writes are safe. Any
// failure is cosmetic — swallow it.
function agentDisplayName(agentSlug) {
  const words = sanitizeAgent(agentSlug).split(/[-_]+/).filter(Boolean);
  const title = words.map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
  return `${title || "Agent"} (OtoDock)`;
}

function seedProfileDisplayName(profileDir, agentSlug) {
  const name = agentDisplayName(agentSlug);
  try {
    const localState = path.join(profileDir, "Local State");
    const obj = fs.existsSync(localState) ? JSON.parse(fs.readFileSync(localState, "utf8")) : {};
    obj.profile = obj.profile || {};
    obj.profile.info_cache = obj.profile.info_cache || {};
    obj.profile.info_cache.Default = {
      ...(obj.profile.info_cache.Default || {}),
      name,
      is_using_default_name: false,
    };
    fs.writeFileSync(localState, JSON.stringify(obj));
  } catch (_) {
    /* cosmetic */
  }
  try {
    const prefsPath = path.join(profileDir, "Default", "Preferences");
    if (fs.existsSync(prefsPath)) {
      const prefs = JSON.parse(fs.readFileSync(prefsPath, "utf8"));
      prefs.profile = { ...(prefs.profile || {}), name };
      fs.writeFileSync(prefsPath, JSON.stringify(prefs));
    }
  } catch (_) {
    /* cosmetic */
  }
}

// Launch the browser daemon with its parent chain BROKEN: the session teardown
// snapshots the MCP's live descendant tree and kills it, so a plain detached
// child (still ppid-linked to this wrapper) would die with the session. An
// intermediary shell that exits immediately leaves the browser unreachable
// from any session's process tree.
function launchDetachedBrowser(exePath, profileDir) {
  const args = [
    `--user-data-dir=${profileDir}`,
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    "--restore-last-session",
  ];
  if (process.platform === "win32") {
    // `start ""` detaches; cmd exits at once. Quote args for cmd.
    const quoted = args.map((a) => `"${a}"`).join(" ");
    spawn("cmd.exe", ["/d", "/s", "/c", `start "" "${exePath}" ${quoted}`], {
      stdio: "ignore",
      windowsHide: true,
      windowsVerbatimArguments: true,
    }).unref();
    return;
  }
  const sh = spawn("/bin/sh", ["-c", '"$0" "$@" >/dev/null 2>&1 &', exePath, ...args], {
    stdio: "ignore",
    detached: true,
  });
  sh.unref();
}

// Resolve (or start) the shared browser daemon and return its CDP endpoint,
// or null when this machine must fall back to the classic persistent launch
// (non-Chromium browser, no known exe path, or a live non-daemon browser
// legitimately holding the profile).
async function ensureBrowserDaemon(profileDir, exePath, agentSlug) {
  let endpoint = await liveCdpEndpoint(profileDir);
  if (endpoint) return endpoint;
  // A browser without a debug port holds the profile: reap it if provably
  // orphaned (pre-daemon zombie / crashed daemon); a live-owned one keeps its
  // exclusive claim and we fall back.
  reapOrphanedBrowser(profileDir);
  if (profileLooksLocked(profileDir)) return null;
  seedProfileDisplayName(profileDir, agentSlug);
  launchDetachedBrowser(exePath, profileDir);
  for (let i = 0; i < 60; i++) {
    await new Promise((f) => setTimeout(f, 250));
    endpoint = await liveCdpEndpoint(profileDir);
    if (endpoint) return endpoint;
  }
  process.stderr.write(
    `[browser-control] Browser daemon did not expose a CDP endpoint within 15s ` +
      `(profile ${profileDir}); falling back to a per-session browser.\n`
  );
  return null;
}

// --- stdio supervisor plumbing (pure helpers, unit-tested) ------------------
//
// In CDP mode the wrapper pipes the MCP's NDJSON stdio through itself so it
// can transparently restart cli.js when the shared browser daemon dies (the
// bundled @playwright/mcp creates its browser connection ONCE and never
// recreates it — a closed browser wedges the session's browser tools until
// the MCP process restarts). See runCdpSupervisor below.

// Accumulates stream chunks and yields complete newline-terminated lines.
class LineSplitter {
  constructor() {
    this._rest = "";
  }
  feed(chunk) {
    const text = this._rest + chunk.toString("utf8");
    const lines = text.split("\n");
    this._rest = lines.pop();
    return lines.filter((l) => l.length > 0).map((l) => (l.endsWith("\r") ? l.slice(0, -1) : l));
  }
}

// Minimal JSON-RPC peek: {id, method, toolName, isRequest} (nulls when
// unparseable). toolName is set only for tools/call lines.
function peekRpc(line) {
  try {
    const msg = JSON.parse(line);
    const id = Object.prototype.hasOwnProperty.call(msg, "id") ? msg.id : void 0;
    const method = typeof msg.method === "string" ? msg.method : void 0;
    const toolName =
      method === "tools/call" && msg.params && typeof msg.params.name === "string"
        ? msg.params.name
        : void 0;
    return { id, method, toolName, isRequest: id !== void 0 && method !== void 0 };
  } catch (_) {
    return { id: void 0, method: void 0, toolName: void 0, isRequest: false };
  }
}

const REINIT_ID = "__otodock_browser_reinit__";

// Never-routable CDP endpoint (TCP port 9 = discard) for the IDLE child: cli.js
// serves the MCP handshake without dialing CDP (the connection is made lazily
// on the first browser tool), so a session can initialize + list tools while
// the shared browser stays closed.
const PLACEHOLDER_CDP_ENDPOINT = "http://127.0.0.1:9/otodock-browser-idle";

// Route decision for one client NDJSON line: a studio tools/call is handled
// in-process, a tools/list is forwarded but its response gets the studio
// tool definitions appended, everything else passes through untouched.
function classifyClientLine(line, owns) {
  let msg = null;
  try {
    msg = JSON.parse(line);
  } catch (_) {
    return { kind: "forward" };
  }
  if (msg && msg.method === "tools/call" && msg.id !== void 0 && msg.params && owns(msg.params.name)) {
    return { kind: "studio", id: msg.id, params: msg.params };
  }
  if (msg && msg.method === "tools/call" && msg.params && aliasRefArgs(msg.params.arguments)) {
    // Forward the REWRITTEN line — both supervisors write route.line ?? line.
    return { kind: "forward", line: JSON.stringify(msg) };
  }
  if (msg && msg.method === "tools/list" && msg.id !== void 0) {
    return { kind: "tools-list", id: msg.id };
  }
  return { kind: "forward" };
}

// Habit-absorbing alias: snapshots render [ref=eN] and the skill teaches
// `ref`, but upstream @playwright/mcp names the element param `target`
// (drag: startTarget/endTarget; fill_form: fields[].target) — every agent's
// first click failed on the name mismatch. Generic on purpose: no tool
// whitelist, so an upstream bump adding target-taking tools stays covered.
function aliasRefArgs(args) {
  if (!args || typeof args !== "object") return false;
  let changed = false;
  const move = (obj, from, to) => {
    if (obj[from] !== void 0 && obj[to] === void 0) {
      obj[to] = obj[from];
      delete obj[from];
      changed = true;
    }
  };
  move(args, "ref", "target");
  move(args, "startRef", "startTarget");
  move(args, "endRef", "endTarget");
  if (Array.isArray(args.fields)) {
    for (const f of args.fields) {
      if (f && typeof f === "object") move(f, "ref", "target");
    }
  }
  return changed;
}

function rewriteInitId(initLine) {
  const msg = JSON.parse(initLine);
  msg.id = REINIT_ID;
  return JSON.stringify(msg);
}

function buildErrorResponse(id, message) {
  return JSON.stringify({ jsonrpc: "2.0", id, error: { code: -32000, message } });
}

// Successful tools/call result carrying a plain text block — for calls the
// supervisor answers itself without a child (e.g. browser_close while no
// browser is running).
function buildToolTextResponse(id, text) {
  return JSON.stringify({
    jsonrpc: "2.0", id,
    result: { content: [{ type: "text", text }] },
  });
}

// Decide the final browser env from the (optional) explicit OTO_BROWSER_CHANNEL
// and what auto-detection found. Pure + unit-tested. A Chromium-family browser
// found by path is driven via the chromium engine + executablePath (@playwright/mcp
// accepts `chromium`; executablePath overrides the binary, so no download).
// `executablePath` (when known) is what lets the CDP daemon launch the browser.
function resolveBrowserConfig(explicit, detected, channelExe) {
  const e = String(explicit || "").trim().toLowerCase();
  if (e && e !== "auto") {
    // honor an explicit channel/engine; resolve its exe for the daemon when we can
    const exe = channelExe ? channelExe(e) : null;
    return exe ? { browser: e, executablePath: exe } : { browser: e };
  }
  if (detected && detected.channel)
    return { browser: detected.channel, executablePath: detected.executablePath };
  if (detected && detected.executablePath)
    return { browser: "chromium", executablePath: detected.executablePath };
  return { browser: "chrome" }; // last resort; the stderr hint fires if it's absent
}

// CDP attach works for the Chromium family only (CDP is Chromium's protocol);
// Firefox/WebKit stay on the classic per-session persistent launch.
function isChromiumFamily(browser) {
  return ["chrome", "msedge", "chromium"].includes(String(browser || ""));
}

async function main() {
  const home = os.homedir();
  if (!home) {
    process.stderr.write(
      "[browser-control] Cannot determine the home directory; refusing to start.\n"
    );
    process.exit(1);
  }

  const profileDir = computeProfileDir(home, process.env.OTO_AGENT_NAME);
  try {
    fs.mkdirSync(profileDir, { recursive: true });
  } catch (e) {
    process.stderr.write(
      `[browser-control] Could not create the browser profile directory ${profileDir}: ${e.message}\n`
    );
    process.exit(1);
  }

  // Which installed browser to drive. OTO_BROWSER_CHANNEL (admin/user config,
  // default "auto") is an explicit override; "auto" detects what's on THIS
  // machine. A Chromium-family browser found by path is driven via executablePath.
  const cfg = resolveBrowserConfig(
    process.env.PLAYWRIGHT_MCP_BROWSER || process.env.OTO_BROWSER_CHANNEL,
    detectBrowser(),
    channelExecutable
  );
  if (!process.env.PLAYWRIGHT_MCP_BROWSER) {
    process.env.PLAYWRIGHT_MCP_BROWSER = cfg.browser;
    if (cfg.executablePath && !process.env.PLAYWRIGHT_MCP_EXECUTABLE_PATH) {
      process.env.PLAYWRIGHT_MCP_EXECUTABLE_PATH = cfg.executablePath;
    }
  }
  // Headed by default — the user must see the window to sign in once. We leave
  // PLAYWRIGHT_MCP_HEADLESS unset. The *_ORIGINS env vars flow through from the
  // manifest/proxy unchanged.

  // Chromium family → attach to the shared browser daemon over CDP (concurrent
  // sessions, tabs survive session death — see "Shared browser daemon" above).
  // NEVER launch the daemon here: sessions connect their MCPs on every spawn
  // (scheduled tasks included), and an init-time launch popped the browser
  // open on the user's screen with no agent actually using it. Probe for an
  // ALREADY-RUNNING daemon and attach; otherwise the supervisor starts with
  // no endpoint and the daemon launches lazily on the first browser
  // tools/call (restartFlow). Non-Chromium → classic per-session launch.
  let cdpMode = false;
  let cdpEndpoint = null;
  if (isChromiumFamily(cfg.browser) && cfg.executablePath) {
    cdpMode = true;
    cdpEndpoint = await liveCdpEndpoint(profileDir);
  }
  if (!cdpMode) {
    // Recover the profile from a browser its dead session left behind (see
    // "Orphaned-browser recovery" above). No-op when the profile isn't locked
    // and when the daemon path already swept it.
    reapOrphanedBrowser(profileDir);
    // Dedicated persistent profile (NOT --isolated — that would discard logins).
    process.env.PLAYWRIGHT_MCP_USER_DATA_DIR = profileDir;
  }

  const cli = resolveCliPath(__dirname);
  if (!cli) {
    process.stderr.write(
      "[browser-control] @playwright/mcp is not installed " +
        "(node_modules/@playwright/mcp/cli.js is missing). The platform installs it " +
        "automatically; if you see this, the MCP install/sync did not finish.\n"
    );
    process.exit(1);
  }

  if (cdpMode) {
    runCdpSupervisor(cli, profileDir, cfg, cdpEndpoint);
    return;
  }

  // Classic persistent launch (Firefox/WebKit, or no daemon): the JSON-RPC
  // stream is piped through so the studio tools can be routed in-process (no
  // restart logic here — that is CDP-mode-only); stderr is teed for the
  // friendly hints.
  const child = spawn(process.execPath, [cli], {
    stdio: ["pipe", "pipe", "pipe"],
    env: process.env,
  });
  wireStderrHints(child, profileDir);

  const listIds = new Set();
  const outLines = new LineSplitter();
  child.stdout.on("data", (chunk) => {
    for (let line of outLines.feed(chunk)) {
      const { id } = peekRpc(line);
      if (id !== void 0 && listIds.has(id)) {
        listIds.delete(id);
        try {
          line = JSON.stringify(studio.mergeToolsResult(JSON.parse(line), studio.toolDefinitions()));
        } catch (_) {
          /* pass the child's line through untouched */
        }
      }
      process.stdout.write(line + "\n");
    }
  });
  const inLines = new LineSplitter();
  process.stdin.on("data", (chunk) => {
    for (const line of inLines.feed(chunk)) {
      const route = classifyClientLine(line, studio.ownsTool);
      if (route.kind === "studio") {
        studio.dispatch(route.id, route.params).then((resp) => {
          process.stdout.write(JSON.stringify(resp) + "\n");
        });
        continue;
      }
      if (route.kind === "tools-list") listIds.add(route.id);
      try {
        child.stdin.write((route.line ?? line) + "\n");
      } catch (_) {
        /* child gone — its exit handler ends the process */
      }
    }
  });
  process.stdin.on("end", () => {
    try {
      child.stdin.end();
    } catch (_) {
      /* child already gone */
    }
  });

  child.on("error", (e) => {
    process.stderr.write(`[browser-control] Failed to start @playwright/mcp: ${e.message}\n`);
    process.exit(1);
  });
  child.on("exit", (code, signal) => {
    process.exit(signal ? 1 : code == null ? 0 : code);
  });

  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(sig, () => {
      try {
        child.kill(sig);
      } catch (_) {
        /* child already gone */
      }
    });
  }
}

// Friendly one-line hints appended to a raw Playwright stack on the two
// common failures: the profile already being open, and the browser missing.
function wireStderrHints(child, profileDir) {
  let lockHinted = false;
  let browserHinted = false;
  child.stderr.on("data", (chunk) => {
    process.stderr.write(chunk);
    const text = chunk.toString();
    if (
      !lockHinted &&
      /ProcessSingleton|SingletonLock|already in use|user data directory is already/i.test(text)
    ) {
      lockHinted = true;
      process.stderr.write(
        `[browser-control] This agent's browser profile (${profileDir}) is already open in ` +
          "another session on this machine. A logged-in profile can drive only one browser at a " +
          "time — close the other session and try again.\n"
      );
    }
    if (
      !browserHinted &&
      /Executable doesn't exist|is not found|Failed to launch|Chromium distribution|channel .*is not installed|No such file/i.test(
        text
      )
    ) {
      browserHinted = true;
      const ch = process.env.PLAYWRIGHT_MCP_BROWSER || "chrome";
      process.stderr.write(
        `[browser-control] Could not launch the '${ch}' browser. Install Google Chrome (or ` +
          "Microsoft Edge) on this machine, or set the Browser channel config (OTO_BROWSER_CHANNEL) " +
          "to one you have installed: chrome / msedge / firefox / webkit.\n"
      );
    }
  });
}

// --- CDP-mode stdio supervisor ----------------------------------------------
//
// The wrapper forwards the NDJSON JSON-RPC stream between the CLI client and
// cli.js so it can survive the shared browser dying (user closed the window,
// browser_close, a crash): the bundled mcp binds its browser connection once
// and never recreates it, so the only recovery is restarting cli.js against a
// freshly-launched daemon. Recovery is LAZY — it runs when the next browser
// request arrives, so closing the window doesn't pop a new one open
// unprompted, and --restore-last-session brings the tabs back when it does.
//
// Restart flow: fail any in-flight requests with a JSON-RPC error ("retry"),
// relaunch the daemon (ensureBrowserDaemon), spawn a new cli.js against the
// new endpoint, replay the captured `initialize` handshake (REINIT_ID, its
// response swallowed) + `notifications/initialized`, then resume forwarding.
function runCdpSupervisor(cli, profileDir, cfg, initialEndpoint) {
  const state = {
    child: null,
    childIsIdle: false, // child bound to the placeholder — browser NOT launched
    endpoint: initialEndpoint, // null until a daemon is (found) running
    initLine: null, // client's verbatim initialize request
    initializedLine: null, // client's verbatim notifications/initialized
    pending: new Set(), // request ids awaiting a child response
    listIds: new Set(), // tools/list ids whose responses get studio tools appended
    swallowReinit: false,
    chain: Promise.resolve(), // serializes client-line handling
    shuttingDown: false,
  };

  function spawnChild(endpoint) {
    const child = spawn(process.execPath, [cli, "--cdp-endpoint", endpoint], {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });
    const out = new LineSplitter();
    child.stdout.on("data", (chunk) => {
      for (let line of out.feed(chunk)) {
        const { id } = peekRpc(line);
        if (state.swallowReinit && id === REINIT_ID) {
          state.swallowReinit = false;
          continue;
        }
        if (id !== void 0) state.pending.delete(id);
        if (id !== void 0 && state.listIds.has(id)) {
          state.listIds.delete(id);
          try {
            line = JSON.stringify(studio.mergeToolsResult(JSON.parse(line), studio.toolDefinitions()));
          } catch (_) {
            /* pass the child's line through untouched */
          }
        }
        process.stdout.write(line + "\n");
      }
    });
    wireStderrHints(child, profileDir);
    child.on("exit", () => {
      if (state.child === child) state.child = null; // next request restarts
    });
    child.on("error", () => {
      if (state.child === child) state.child = null;
    });
    state.child = child;
    return child;
  }

  function failPending(message) {
    for (const id of state.pending) process.stdout.write(buildErrorResponse(id, message) + "\n");
    state.pending.clear();
  }

  // Child for protocol traffic (initialize / tools/list / notifications) while
  // the browser is NOT running — bound to a never-routable endpoint (TCP port 9,
  // discard). cli.js only dials CDP when a browser tool actually runs, so the
  // handshake is served without popping a window open. The daemon launches in
  // exactly ONE place: a browser tools/call (restartFlow below).
  function ensureIdleChild() {
    if (state.child) return;
    state.childIsIdle = true;
    const child = spawnChild(PLACEHOLDER_CDP_ENDPOINT);
    if (state.initLine) {
      state.swallowReinit = true;
      child.stdin.write(rewriteInitId(state.initLine) + "\n");
      if (state.initializedLine) child.stdin.write(state.initializedLine + "\n");
    }
  }

  function browserAlive() {
    // Chromium removes DevToolsActivePort on clean shutdown, so a missing
    // file means the daemon is gone. (A hard crash can leave it behind —
    // that case surfaces as one failed tool call, and the child's death or
    // the next request's restart recovers.)
    return fs.existsSync(path.join(profileDir, "DevToolsActivePort"));
  }

  async function restartFlow() {
    const old = state.child;
    state.child = null;
    if (old) {
      try {
        old.kill();
      } catch (_) {
        /* already gone */
      }
    }
    failPending("The browser was closed — it is being relaunched, retry the action.");
    const endpoint = await ensureBrowserDaemon(profileDir, cfg.executablePath, process.env.OTO_AGENT_NAME);
    if (!endpoint) {
      process.stderr.write(
        `[browser-control] Could not relaunch the browser for ${profileDir}; ` +
          "the next request will try again.\n"
      );
      return false;
    }
    state.endpoint = endpoint;
    state.childIsIdle = false;
    const child = spawnChild(endpoint);
    if (state.initLine) {
      state.swallowReinit = true;
      child.stdin.write(rewriteInitId(state.initLine) + "\n");
      if (state.initializedLine) child.stdin.write(state.initializedLine + "\n");
    }
    return true;
  }

  // The generic "unavailable — try again" hid the actual cause: the stderr
  // profile-lock hint is invisible to the calling agent. When the profile is
  // provably held by a LIVE browser (another session of this agent), say so —
  // the caller can close that session or wait instead of blind-retrying.
  function unavailableMessage() {
    try {
      if (profileLooksLocked(profileDir)) {
        const procs = listProcesses();
        const holders = procs.filter(
          (p) =>
            p.cmd.includes(`--user-data-dir=${profileDir}`) &&
            !/--type=/.test(p.cmd)
        );
        const orphans = findOrphanHolders(procs, profileDir);
        const live = holders.filter((h) => !orphans.includes(h));
        if (live.length > 0)
          return (
            "The browser is in use by another live session of this agent on " +
            `this machine (pid ${live[0].pid}). A logged-in profile drives one ` +
            "browser at a time — close that session's browser (or wait for it " +
            "to finish), then try again."
          );
      }
    } catch (_) {
      /* diagnosis is best-effort — fall through to the generic message */
    }
    return "The browser is unavailable on this machine right now — try again.";
  }

  async function handleClientLine(line) {
    if (state.shuttingDown) return;
    const { id, method, toolName, isRequest } = peekRpc(line);
    if (method === "initialize") state.initLine = line;
    else if (method === "notifications/initialized") state.initializedLine = line;
    // ONLY a browser tools/call may launch the daemon (studio calls were
    // routed out before the chain). Handshake/protocol traffic from a fresh
    // session must never pop the browser open — that was the "browser opens
    // by itself" bug: every session spawn sends initialize/tools/list, and
    // the old unconditional restart relaunched a browser the user had closed.
    if (method === "tools/call") {
      if (!state.child || state.childIsIdle || !browserAlive()) {
        // browser_close with no daemon anywhere: closing nothing is success.
        // Launching just to close was the last "opens by itself" path — an
        // end-of-task cleanup close popped a window open. Only the LIVE
        // probe can decide (the daemon is shared across sessions; this
        // supervisor's child being idle says nothing about other sessions).
        if (toolName === "browser_close" && !(await liveCdpEndpoint(profileDir))) {
          if (isRequest)
            process.stdout.write(
              buildToolTextResponse(id, "Browser was not running — nothing to close.") + "\n"
            );
          return;
        }
        // restartFlow probes for a live daemon FIRST (another session may
        // have launched it since we went idle), so this attaches rather than
        // double-launches; it only spawns a browser when none is running.
        const ok = await restartFlow();
        if (!ok) {
          if (isRequest)
            process.stdout.write(
              buildErrorResponse(id, unavailableMessage()) + "\n"
            );
          return;
        }
      }
    } else if (!state.child) {
      ensureIdleChild();
    }
    if (isRequest) state.pending.add(id);
    try {
      state.child.stdin.write(line + "\n");
    } catch (_) {
      state.child = null; // EPIPE — recovered by the next request
      if (isRequest) {
        state.pending.delete(id);
        process.stdout.write(
          buildErrorResponse(id, "The browser session restarted — retry the action.") + "\n"
        );
      }
    }
  }

  const stdinLines = new LineSplitter();
  process.stdin.on("data", (chunk) => {
    for (const line of stdinLines.feed(chunk)) {
      // Studio calls never enter the chain: they need no child (and must not
      // stall behind — or be stalled by — a browser restart). The studio
      // serializes its own operations internally.
      const route = classifyClientLine(line, studio.ownsTool);
      if (route.kind === "studio") {
        studio.dispatch(route.id, route.params).then((resp) => {
          process.stdout.write(JSON.stringify(resp) + "\n");
        });
        continue;
      }
      if (route.kind === "tools-list") state.listIds.add(route.id);
      const fwd = route.line ?? line;
      state.chain = state.chain.then(() => handleClientLine(fwd)).catch(() => {});
    }
  });
  process.stdin.on("end", () => {
    state.shuttingDown = true;
    if (state.child) {
      try {
        state.child.kill();
      } catch (_) {
        /* already gone */
      }
    }
    process.exit(0);
  });
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(sig, () => {
      state.shuttingDown = true;
      if (state.child) {
        try {
          state.child.kill(sig);
        } catch (_) {
          /* already gone */
        }
      }
      process.exit(0);
    });
  }

  // A daemon was already running at init → attach to it now. Otherwise start
  // IDLE: the placeholder child serves the handshake and the daemon launches
  // on the first browser tools/call.
  if (initialEndpoint) spawnChild(initialEndpoint);
  else ensureIdleChild();
}

if (require.main === module) {
  main().catch((e) => {
    process.stderr.write(`[browser-control] Startup failed: ${e.message}\n`);
    process.exit(1);
  });
} else {
  module.exports = {
    sanitizeAgent, computeProfileDir, resolveCliPath, detectBrowser,
    resolveBrowserConfig, isChromiumFamily, channelExecutable,
    profileLooksLocked, findOrphanHolders, readDevtoolsPort, agentDisplayName,
    seedProfileDisplayName,
    LineSplitter, peekRpc, rewriteInitId, buildErrorResponse,
    buildToolTextResponse, REINIT_ID,
    classifyClientLine,
  };
}
