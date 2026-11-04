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
//   * PLAYWRIGHT_MCP_BLOCKED_ORIGINS  — an admin config value (the manifest ships none)
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
const ownProfile = require("./own_profile.js");
const ownUpload = require("./own_upload.js");

// OTO_AGENT_NAME is already a filesystem-safe slug; defensively keep only
// path-safe characters and never allow a "." (so a name can never become a
// "."/".." traversal segment).
function sanitizeAgent(name) {
  const s = String(name || "").replace(/[^A-Za-z0-9_-]/g, "_");
  if (!s || /^_+$/.test(s)) return "default";
  return s;
}

// The platform's per-user state folder: ".oto-dock" on Linux and macOS,
// "OtoDock" on Windows (the satellite's own root there), so the machine's
// path policy covers the profiles with the one rule it has for that folder.
function platformStateDirname(plat) {
  return plat === "win32" ? "OtoDock" : ".oto-dock";
}

function computeProfileDir(home, agent, plat) {
  const dirname = platformStateDirname(plat === undefined ? process.platform : plat);
  return path.join(home, dirname, "browser-profiles", sanitizeAgent(agent));
}

function legacyProfileDir(home, agent) {
  return path.join(home, ".oto-dock", "browser-profiles", sanitizeAgent(agent));
}

// A profile folder no browser has used yet: absent, or an empty directory.
function isUnusedDir(dir) {
  try {
    return fs.lstatSync(dir).isDirectory() && fs.readdirSync(dir).length === 0;
  } catch (e) {
    return e.code === "ENOENT";
  }
}

// Profiles made by earlier releases on Windows sat under ".oto-dock"; a start
// moves them so the signed-in logins survive. An empty new folder (an earlier
// release created it after a failed move) does not block the move.
function migrateLegacyProfileDir(home, agent, plat) {
  if ((plat === undefined ? process.platform : plat) !== "win32") return false;
  const legacy = legacyProfileDir(home, agent);
  const current = computeProfileDir(home, agent, "win32");
  if (legacy === current || !fs.existsSync(legacy) || !isUnusedDir(current)) return false;
  try {
    fs.mkdirSync(path.dirname(current), { recursive: true });
    if (fs.existsSync(current)) fs.rmdirSync(current);
    fs.renameSync(legacy, current);
    return true;
  } catch (e) {
    process.stderr.write(
      `[browser-control] Could not move the browser profile from ${legacy} to ${current}: ${e.message}\n`
    );
    return false;
  }
}

// The profile this start runs on. On Windows, while the legacy profile has
// not been moved (a browser of the earlier release still holds it), the start
// runs on it and the new folder stays uncreated, so a later start moves it.
function profileDirForStart(home, agent, plat) {
  const p = plat === undefined ? process.platform : plat;
  migrateLegacyProfileDir(home, agent, p);
  const current = computeProfileDir(home, agent, p);
  if (p === "win32") {
    const legacy = legacyProfileDir(home, agent);
    if (legacy !== current && fs.existsSync(legacy) && isUnusedDir(current)) return legacy;
  }
  return current;
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

// System Firefox candidates — only a SIGNAL that the user's browser is
// Firefox: the dedicated mode then drives Playwright's own Firefox build
// (downloaded on first use), never the system Firefox itself.
function firefoxCandidates() {
  const plat = process.platform;
  if (plat === "win32") {
    const pf = process.env["ProgramFiles"] || "C:\\Program Files";
    const pf86 = process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";
    return [`${pf}\\Mozilla Firefox\\firefox.exe`, `${pf86}\\Mozilla Firefox\\firefox.exe`];
  }
  if (plat === "darwin") return ["/Applications/Firefox.app/Contents/MacOS/firefox"];
  return ["/usr/bin/firefox", "/usr/bin/firefox-esr", "/snap/bin/firefox", "/usr/lib/firefox/firefox"];
}

function detectSystemFirefox() {
  return firefoxCandidates().find((p) => p && fs.existsSync(p)) || null;
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

// The profile's browser MAIN processes (no --type=; ending one tears down its
// own children): the command line names this exact profile dir, with a
// boundary after the path, so agent "test" never matches "test-agent"'s
// browser. `procs` is the full table from listProcesses().
function profileHolders(procs, profileDir) {
  const flag = `--user-data-dir=${profileDir}`;
  return procs.filter((p) => {
    if (/--type=/.test(p.cmd)) return false;
    for (let i = p.cmd.indexOf(flag); i !== -1; i = p.cmd.indexOf(flag, i + 1)) {
      const next = p.cmd.charAt(i + flag.length);
      if (next === "" || next === " " || next === '"' || next === "'") return true;
    }
    return false;
  });
}

// Pure decision: which profile holders are provably orphaned?
function findOrphanHolders(procs, profileDir) {
  const byPid = new Map(procs.map((p) => [p.pid, p]));
  const holders = profileHolders(procs, profileDir);
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
// profile. Origin limits (PLAYWRIGHT_MCP_ALLOWED_ORIGINS, and
// PLAYWRIGHT_MCP_BLOCKED_ORIGINS when an admin sets one) are enforced by
// @playwright/mcp via context.route on the attached context, so they apply
// in CDP mode too. Firefox/WebKit (no CDP) keep the classic persistent launch.

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

// How a browser starts detached on POSIX, as {command, argv}. On a Mac a
// browser inside an app bundle starts through LaunchServices (`open`): a
// direct exec of a Chrome that never ran on that Mac aborts at once (its
// Application Support folder does not exist yet), and launchd becomes the new
// instance's parent. `newInstance` (`-n`) is for the shared browser's own
// profile, never for the person's own browser. `logPath` takes the browser's
// output so a launch that dies says why. Elsewhere an intermediary shell that
// exits at once does the detaching.
function detachedLaunchArgv(exePath, args, { platform = process.platform, logPath = null, newInstance = true } = {}) {
  const bundle = platform === "darwin" ? /^(.*?\.app)\/Contents\/MacOS\/[^/]+$/.exec(exePath) : null;
  if (bundle) {
    const argv = [...(newInstance ? ["-n"] : []), "-a", bundle[1]];
    if (logPath) argv.push("--stdout", logPath, "--stderr", logPath);
    if (args.length) argv.push("--args", ...args);
    return { command: "/usr/bin/open", argv };
  }
  return { command: "/bin/sh", argv: ["-c", '"$0" "$@" &', exePath, ...args] };
}

// The line of a browser's launch log that says why it did not start, cut to
// 300 characters; "" when there is none: Chrome's FATAL line, or macOS's `open`
// refusing the launch (outside the desktop session, an app it cannot find).
// Chrome prints ERROR lines in healthy runs: those never count.
const LAUNCH_FAILURE = /:FATAL:|^FATAL\b|LSOpen\w*\(\) failed|cannot be opened|Unable to find application/;

function launchFailureLine(logPath) {
  let text;
  try {
    text = fs.readFileSync(logPath, "utf8");
  } catch (_) {
    return "";
  }
  const failed = text.split(/\r?\n/).filter((l) => LAUNCH_FAILURE.test(l)).pop();
  return failed ? failed.trim().slice(0, 300) : "";
}

// Launch a browser with its parent chain BROKEN: the session teardown
// snapshots the MCP's live descendant tree and kills it, so a plain detached
// child (still ppid-linked to this wrapper) would die with the session. An
// intermediary shell that exits immediately (or LaunchServices on a Mac)
// leaves the browser unreachable from any session's process tree. Used for
// the dedicated-profile daemon and, in own-browser mode, to start the user's
// browser when it is not running.
function launchDetachedBrowser(exePath, args, { logPath = null, newInstance = true } = {}) {
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
  // The log, rewritten per launch, also takes `open`'s own errors (a launch
  // outside the desktop session fails there).
  let out = "ignore";
  if (logPath) {
    try {
      out = fs.openSync(logPath, "w");
    } catch (_) {
      logPath = null;
    }
  }
  const { command, argv } = detachedLaunchArgv(exePath, args, { logPath, newInstance });
  const child = spawn(command, argv, { stdio: ["ignore", out, out], detached: true });
  child.unref();
  if (typeof out === "number") fs.closeSync(out);
}

// The shared browser's command line. A closed window comes back with its
// tabs; after a wedge the stuck tab must not.
function daemonLaunchArgs(profileDir, { restoreLastSession = true } = {}) {
  const args = [
    `--user-data-dir=${profileDir}`,
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
  ];
  if (restoreLastSession) args.push("--restore-last-session");
  return args;
}

// One launch or recovery at a time per profile, across every session of the
// agent on this machine (each runs its own wrapper): a directory made
// atomically in the profile. A holder that died leaves it behind; it counts
// as free once older than LAUNCH_LOCK_STALE_MS (a launch waits 15 s at most,
// a recovery about 30 s).
const LAUNCH_LOCK = ".otodock-launch.lock";
const LAUNCH_LOCK_STALE_MS = 60000;

function tryLaunchLock(profileDir, now = Date.now()) {
  const dir = path.join(profileDir, LAUNCH_LOCK);
  try {
    fs.mkdirSync(dir);
    return true;
  } catch (e) {
    if (e.code !== "EEXIST") return true; // no lock possible here: go on unlocked
  }
  try {
    if (now - fs.statSync(dir).mtimeMs > LAUNCH_LOCK_STALE_MS) {
      fs.rmdirSync(dir);
      fs.mkdirSync(dir);
      return true;
    }
  } catch (_) {
    /* another session took it over first */
  }
  return false;
}

function releaseLaunchLock(profileDir) {
  try {
    fs.rmdirSync(path.join(profileDir, LAUNCH_LOCK));
  } catch (_) {
    /* already gone */
  }
}

async function withLaunchLock(profileDir, fn) {
  while (!tryLaunchLock(profileDir)) await new Promise((f) => setTimeout(f, 250));
  try {
    return await fn();
  } finally {
    releaseLaunchLock(profileDir);
  }
}

// Resolve (or start) the shared browser daemon and return its CDP endpoint,
// or null when this machine must fall back to the classic persistent launch
// (non-Chromium browser, no known exe path, or a live non-daemon browser
// legitimately holding the profile).
async function ensureBrowserDaemon(profileDir, exePath, agentSlug, { restoreLastSession = true } = {}) {
  const live = await liveCdpEndpoint(profileDir);
  if (live) return live;
  return withLaunchLock(profileDir, async () => {
    // Another session may have launched it while this one waited.
    let endpoint = await liveCdpEndpoint(profileDir);
    if (endpoint) return endpoint;
    // A browser without a debug port holds the profile: reap it if provably
    // orphaned (pre-daemon zombie / crashed daemon); a live-owned one keeps
    // its exclusive claim and we fall back.
    reapOrphanedBrowser(profileDir);
    if (profileLooksLocked(profileDir)) return null;
    seedProfileDisplayName(profileDir, agentSlug);
    launchDetachedBrowser(exePath, daemonLaunchArgs(profileDir, { restoreLastSession }), {
      logPath: path.join(profileDir, "launch.log"),
    });
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
  });
}

// --- A shared browser that lets no new session attach ----------------------
//
// A CDP attach waits for every page already open in the browser to
// initialize, under a 30 s limit (Playwright's connectOverCDP); one tab whose
// renderer stopped answering (a page frozen or hung after hours in the
// background) makes every new session's first browser call fail with
// "initializeServer: Timeout 30000ms exceeded", the call log ending at
// "<ws connected>", while the browser itself still answers on its debugging
// port. Upstream: microsoft/playwright#42730 (closed as a duplicate of
// #41093, not planned). Only a fresh browser recovers.
const WEDGED_ATTACH_RE = /(initializeServer|connectOverCDP): Timeout \d+ms exceeded/;

function isWedgedAttachError(text) {
  const s = String(text || "");
  return WEDGED_ATTACH_RE.test(s) && s.includes("<ws connected>");
}

// What a session does about a wedged attach. `childEndpoint` is the endpoint
// its child timed out on, `live` the profile's live endpoint now (null when
// none answers), `sameBrowser` whether the browser answering there is the one
// the profile's DevToolsActivePort names.
function wedgeAction({ handled, childEndpoint, live, sameBrowser }) {
  if (handled) return "pass";
  if (!live) return "relaunch";
  if (live !== childEndpoint) return "attach";
  return sameBrowser ? "end-and-relaunch" : "pass";
}

function endpointPort(endpoint) {
  const m = /:(\d+)\/?$/.exec(String(endpoint || ""));
  return m ? parseInt(m[1], 10) : null;
}

// The browser websocket path DevToolsActivePort names (its second line), and
// the one the browser on `port` reports: equal when that port is this
// profile's browser.
function readDevtoolsBrowserPath(profileDir) {
  try {
    return fs.readFileSync(path.join(profileDir, "DevToolsActivePort"), "utf8").split("\n")[1].trim() || null;
  } catch (_) {
    return null;
  }
}

function cdpBrowserPath(port, timeoutMs) {
  return new Promise((resolve) => {
    const http = require("http");
    const req = http.get({ host: "127.0.0.1", port, path: "/json/version", timeout: timeoutMs }, (res) => {
      let body = "";
      res.on("data", (d) => (body += d));
      res.on("end", () => {
        try {
          resolve(new URL(JSON.parse(body).webSocketDebuggerUrl).pathname);
        } catch (_) {
          resolve(null);
        }
      });
    });
    req.on("timeout", () => {
      req.destroy();
      resolve(null);
    });
    req.on("error", () => resolve(null));
  });
}

// End the profile's browser: a polite stop, a forced one after 5 s, then wait
// for the profile to unlock. True once nothing holds it.
async function endProfileBrowser(profileDir) {
  const holders = profileHolders(listProcesses(), profileDir);
  const waitUnlocked = async () => {
    for (let i = 0; i < 20; i++) {
      if (!profileLooksLocked(profileDir)) return true;
      await new Promise((f) => setTimeout(f, 250));
    }
    return !profileLooksLocked(profileDir);
  };
  for (const h of holders) {
    try {
      if (process.platform === "win32") killPid(h.pid);
      else process.kill(h.pid, "SIGTERM");
    } catch (_) {
      /* already exited */
    }
  }
  if (await waitUnlocked()) return true;
  for (const h of holders) {
    try {
      killPid(h.pid);
    } catch (_) {
      /* already exited */
    }
  }
  return waitUnlocked();
}

// The tabs Chromium would reopen at the next start, moved aside after a
// wedge: a profile set to continue where it left off would otherwise bring
// the stuck tab back without --restore-last-session.
function setAsideSessions(profileDir) {
  const from = path.join(profileDir, "Default", "Sessions");
  const to = path.join(profileDir, "Default", "Sessions.before-restart");
  try {
    if (!fs.existsSync(from)) return false;
    fs.rmSync(to, { recursive: true, force: true });
    fs.renameSync(from, to);
    return true;
  } catch (_) {
    return false;
  }
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
// No Chromium browser but a system Firefox → Playwright's Firefox build
// (`firefoxFallback`, downloaded on the first browser call); nothing at all
// keeps the "install Chrome or Edge" hint rather than a silent download.
function resolveBrowserConfig(explicit, detected, channelExe, systemFirefox) {
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
  if (systemFirefox) return { browser: "firefox", firefoxFallback: true };
  return { browser: "chrome" }; // last resort; the stderr hint fires if it's absent
}

// CDP attach works for the Chromium family only (CDP is Chromium's protocol);
// Firefox/WebKit stay on the classic per-session persistent launch.
function isChromiumFamily(browser) {
  return ["chrome", "msedge", "chromium"].includes(String(browser || ""));
}

// --- Own-browser mode (Playwright Extension attach) ------------------------
//
// OTO_BROWSER_MODE=own — set by the proxy from the machine's per-machine
// opt-in, never from MCP config — attaches to the browser the OS user is
// signed into instead of the dedicated profile. @playwright/mcp's --extension
// mode starts a loopback CDP relay on the first browser tool and opens
// chrome-extension://<id>/connect.html in the user's RUNNING Chrome/Edge/Brave
// (the executable is spawned with that URL; the process singleton hands it to
// the default-profile instance), where the "Playwright Extension" gives the
// session its own tab group. PLAYWRIGHT_MCP_EXTENSION_TOKEN (broker-delivered)
// skips the Allow click. No profile dir, no daemon, no orphan sweep here.

const EXTENSION_ID = "mmlmfjhmonkocbjadbfplnigmagldckm";
const EXTENSION_INSTALL_URL =
  `https://chromewebstore.google.com/detail/playwright-extension/${EXTENSION_ID}`;

// Upstream adds --no-sandbox to the connect-page spawn for channel "chromium"
// on Linux, so every Chromium-by-path browser (Brave, Vivaldi, Chromium) is
// passed as "chrome" + its executable. The executable path also makes upstream
// skip its default-profile extension pre-check (extensionLooksInstalled
// replaces it as a hint). Null = own mode impossible on this machine.
function extensionBrowserEnv(cfg) {
  if (!cfg || !cfg.executablePath) return null;
  if (cfg.browser === "msedge")
    return { PLAYWRIGHT_MCP_BROWSER: "msedge", PLAYWRIGHT_MCP_EXECUTABLE_PATH: cfg.executablePath };
  if (cfg.browser === "chrome" || cfg.browser === "chromium")
    return { PLAYWRIGHT_MCP_BROWSER: "chrome", PLAYWRIGHT_MCP_EXECUTABLE_PATH: cfg.executablePath };
  return null;
}

// The browser's DEFAULT user-data-dir: where its process singleton lives and
// where it opens links — so where the extension must be installed and whose
// lock says "the user's browser is running".
function defaultUserDataDir(exePath, home, plat = process.platform, localAppData = process.env["LOCALAPPDATA"]) {
  const exe = String(exePath || "").toLowerCase();
  const kind = exe.includes("edge") ? "msedge"
    : exe.includes("brave") ? "brave"
    : exe.includes("vivaldi") ? "vivaldi"
    : exe.includes("chromium") ? "chromium"
    : "chrome";
  const la = localAppData || path.join(home, "AppData", "Local");
  const table = {
    chrome: { linux: [".config", "google-chrome"], darwin: ["Library", "Application Support", "Google", "Chrome"], win32: [la, "Google", "Chrome", "User Data"] },
    msedge: { linux: [".config", "microsoft-edge"], darwin: ["Library", "Application Support", "Microsoft Edge"], win32: [la, "Microsoft", "Edge", "User Data"] },
    brave: { linux: [".config", "BraveSoftware", "Brave-Browser"], darwin: ["Library", "Application Support", "BraveSoftware", "Brave-Browser"], win32: [la, "BraveSoftware", "Brave-Browser", "User Data"] },
    vivaldi: { linux: [".config", "vivaldi"], darwin: ["Library", "Application Support", "Vivaldi"], win32: [la, "Vivaldi", "User Data"] },
    chromium: { linux: [".config", "chromium"], darwin: ["Library", "Application Support", "Chromium"], win32: [la, "Chromium", "User Data"] },
  };
  const parts = table[kind][plat] || table[kind].linux;
  return plat === "win32" ? path.join(...parts) : path.join(home, ...parts);
}

// Mirror of upstream's isPlaywrightExtensionInstalled: the extension's dir or
// its id in a profile's Preferences, across "Default" + "Profile N". A hint
// only — Chrome opens links in its last-used profile, which may live elsewhere.
function extensionLooksInstalled(userDataDir) {
  let entries;
  try {
    entries = fs.readdirSync(userDataDir);
  } catch (_) {
    return false;
  }
  return entries.some(
    (entry) => ownProfile.isProfileFolder(entry) && extensionInProfile(path.join(userDataDir, entry))
  );
}

// One profile folder: the extension's folder or its id in the Preferences.
function extensionInProfile(profile) {
  if (fs.existsSync(path.join(profile, "Extensions", EXTENSION_ID))) return true;
  try {
    return fs.readFileSync(path.join(profile, "Preferences"), "utf8").includes(`"${EXTENSION_ID}"`);
  } catch (_) {
    return false;
  }
}

// The extension titles the session's tab group after the MCP client's name;
// the CLI reports "claude-code"/"codex", so the copy forwarded to the inner
// MCP carries the agent's display name instead.
function rewriteClientName(initLine, name) {
  try {
    const msg = JSON.parse(initLine);
    if (msg && msg.params && typeof msg.params === "object") {
      msg.params.clientInfo = { ...(msg.params.clientInfo || {}), name };
      return JSON.stringify(msg);
    }
  } catch (_) {
    /* forward verbatim */
  }
  return initLine;
}

// A tool response's text blocks, joined ("" for none or an unreadable line).
function resultText(line) {
  try {
    return ((JSON.parse(line).result || {}).content || []).map((c) => c.text || "").join("\n");
  } catch (_) {
    return "";
  }
}

// A tool RESPONSE carrying an error: the text (result.isError content or a
// JSON-RPC error message), else null.
function toolErrorText(line) {
  try {
    const msg = JSON.parse(line);
    if (msg && msg.result && msg.result.isError)
      return (msg.result.content || []).map((c) => c.text || "").join("\n");
    if (msg && msg.error) return String(msg.error.message || "error");
  } catch (_) {
    /* not a response */
  }
  return null;
}

// Upstream never recreates its browser after an out-of-band disconnect (the
// user hit Disconnect on the extension page, closed every group tab, the
// service worker restarted, the daemon exited with its last tab): every later
// tool fails with one of these until the inner MCP restarts. A refused
// connect means the daemon is gone; the respawned child relaunches it.
const CLOSED_BROWSER_RE =
  /Target page, context or browser has been closed|browser has been closed|Browser closed|Target closed|Extension not connected|Extension disconnected|WebSocket closed|connect ECONNREFUSED/i;
function isClosedBrowserError(text) {
  return CLOSED_BROWSER_RE.test(String(text || ""));
}

// With a token the extension connects unattended within a second or two; the
// slack covers a cold browser start. Without one a human has to click Allow.
function connectTimeoutMs(hasToken) {
  return hasToken ? 45000 : 90000;
}

// Playwright's own Firefox build (the dedicated mode's browser when no
// Chromium browser is installed): absent until `cli.js install-browser
// firefox` has run once on this machine.
function playwrightFirefoxPath() {
  try {
    return require("playwright-core").firefox.executablePath();
  } catch (_) {
    return null;
  }
}

async function main() {
  const home = os.homedir();
  if (!home) {
    process.stderr.write(
      "[browser-control] Cannot determine the home directory; refusing to start.\n"
    );
    process.exit(1);
  }

  // Which installed browser to drive. OTO_BROWSER_CHANNEL (admin/user config,
  // default "auto") is an explicit override; "auto" detects what's on THIS
  // machine. A Chromium-family browser found by path is driven via executablePath.
  const cfg = resolveBrowserConfig(
    process.env.PLAYWRIGHT_MCP_BROWSER || process.env.OTO_BROWSER_CHANNEL,
    detectBrowser(),
    channelExecutable,
    detectSystemFirefox()
  );

  const cli = resolveCliPath(__dirname);
  if (!cli) {
    process.stderr.write(
      "[browser-control] @playwright/mcp is not installed " +
        "(node_modules/@playwright/mcp/cli.js is missing). The platform installs it " +
        "automatically; if you see this, the MCP install/sync did not finish.\n"
    );
    process.exit(1);
  }

  // Own-browser mode: the machine opted into the user's signed-in browser.
  // Chrome/Edge/Brave only — anything else falls back to the dedicated
  // profile with a note, so the session still has a browser.
  if (process.env.OTO_BROWSER_MODE === "own") {
    const extEnv = extensionBrowserEnv(cfg);
    if (extEnv) {
      process.env.PLAYWRIGHT_MCP_EXTENSION = "1";
      process.env.PLAYWRIGHT_MCP_BROWSER = extEnv.PLAYWRIGHT_MCP_BROWSER;
      process.env.PLAYWRIGHT_MCP_EXECUTABLE_PATH = extEnv.PLAYWRIGHT_MCP_EXECUTABLE_PATH;
      delete process.env.PLAYWRIGHT_MCP_USER_DATA_DIR;
      runExtensionSupervisor(cli, cfg, {
        home,
        hasToken: Boolean(process.env.PLAYWRIGHT_MCP_EXTENSION_TOKEN),
        tokenExpected: process.env.OTO_BROWSER_TOKEN_EXPECTED === "1",
        tokenWithheld: process.env.OTO_BROWSER_TOKEN_WITHHELD === "1",
        unattended: process.env.OTO_BROWSER_UNATTENDED === "1",
        profileSetting: process.env.OTO_BROWSER_PROFILE || "",
        agentSlug: process.env.OTO_AGENT_NAME,
      });
      return;
    }
    process.stderr.write(
      "[browser-control] Own-browser mode needs Google Chrome, Microsoft Edge or " +
        "Brave on this machine; using the dedicated browser profile instead.\n"
    );
  }

  const profileDir = profileDirForStart(home, process.env.OTO_AGENT_NAME);
  try {
    fs.mkdirSync(profileDir, { recursive: true });
  } catch (e) {
    process.stderr.write(
      `[browser-control] Could not create the browser profile directory ${profileDir}: ${e.message}\n`
    );
    process.exit(1);
  }

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

  // Firefox fallback: Playwright's build is fetched on the FIRST browser call
  // (never at init — every session spawn would pay the download probe), the
  // call answers with "retry in a minute", and calls during the download get
  // the same answer. cli.js launches the browser lazily, so once the
  // executable exists the next call simply forwards — no restart needed.
  const firefox = { install: null, error: null };
  const firefoxReady = () => {
    const p = playwrightFirefoxPath();
    return Boolean(p && fs.existsSync(p));
  };
  const firefoxGate = (line) => {
    if (!cfg.firefoxFallback || firefoxReady()) return null;
    const { id, method, isRequest } = peekRpc(line);
    if (method !== "tools/call") return null;
    if (firefox.error)
      return isRequest ? buildErrorResponse(id,
        `Playwright's Firefox could not be installed on this machine (${firefox.error}). ` +
        "Install Google Chrome, Microsoft Edge or Brave, or set the Browser channel config."
      ) : "";
    if (!firefox.install) {
      process.stderr.write(
        "[browser-control] No Chrome/Edge/Brave on this machine — installing Playwright's " +
          "Firefox build (one-time download).\n"
      );
      const inst = spawn(process.execPath, [cli, "install-browser", "firefox"], {
        stdio: ["ignore", "ignore", "pipe"],
        env: process.env,
      });
      inst.stderr.on("data", (c) => process.stderr.write(c));
      inst.on("error", (e) => { firefox.error = e.message; });
      inst.on("exit", (code) => {
        if (code !== 0 && !firefoxReady()) firefox.error = `installer exit ${code}`;
      });
      firefox.install = inst;
    }
    return isRequest ? buildErrorResponse(id,
      "Installing Playwright's Firefox build on this machine (one-time download, no " +
      "Chrome/Edge/Brave installed) — retry the action in a minute."
    ) : "";
  };

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
      const gated = firefoxGate(route.line ?? line);
      if (gated !== null) {
        if (gated) process.stdout.write(gated + "\n");
        continue;
      }
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
// A session also restarts its child onto another session's relaunched browser
// (the profile's port moved) and after a closed-browser answer.
//
// Wedge recovery (once per session): browser calls that fail on a wedged
// attach (isWedgedAttachError) are held back, not answered. Under the launch
// lock the browser the child timed out on is ended when it is still this
// profile's, a fresh one starts without its old tabs, and the held calls are
// sent again: they never ran, since the attach failed before the tool.
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
    toolLines: new Map(), // browser tools/call id → its line, while the child has it
    held: new Map(), // id → {line, response}: calls that failed on a wedged attach
    recovering: false,
    wedgeHandled: false, // the one recovery this session gets has run
    needsRestart: false, // a closed-browser answer: restart on the next call
  };

  function spawnChild(endpoint) {
    const child = spawn(process.execPath, [cli, "--cdp-endpoint", endpoint], {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });
    const out = new LineSplitter();
    child.stdout.on("data", (chunk) => {
      // A child a restart retired answers requests the restart already
      // failed ("retry"): forwarding them would answer them twice. A child
      // that exited by itself keeps its late output: Node can deliver it after
      // 'exit', and those answers were never sent.
      if (child.retired) return;
      for (let line of out.feed(chunk)) {
        const { id } = peekRpc(line);
        if (state.swallowReinit && id === REINIT_ID) {
          state.swallowReinit = false;
          continue;
        }
        if (id !== void 0 && state.toolLines.has(id)) {
          const request = state.toolLines.get(id);
          state.toolLines.delete(id);
          const err = toolErrorText(line);
          if (err !== null && isWedgedAttachError(err) && (state.recovering || !state.wedgeHandled)) {
            state.pending.delete(id);
            state.held.set(id, { line: request, response: line });
            scheduleWedgeRecovery();
            continue;
          }
          if (err !== null && isClosedBrowserError(err)) state.needsRestart = true;
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

  async function restartFlow({ restoreLastSession = true } = {}) {
    const old = state.child;
    state.child = null;
    state.needsRestart = false;
    if (old) {
      old.retired = true;
      try {
        old.kill();
      } catch (_) {
        /* already gone */
      }
    }
    failPending("The browser was closed — it is being relaunched, retry the action.");
    state.toolLines.clear();
    const endpoint = await ensureBrowserDaemon(profileDir, cfg.executablePath, process.env.OTO_AGENT_NAME, {
      restoreLastSession,
    });
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

  // Another session relaunched the shared browser: this child still dials the
  // old port, so it follows the profile's current one.
  function endpointMoved() {
    const port = readDevtoolsPort(profileDir);
    return Boolean(port && state.endpoint && endpointPort(state.endpoint) !== port);
  }

  function scheduleWedgeRecovery() {
    if (state.recovering) return; // the running recovery sends this call too
    state.recovering = true;
    state.wedgeHandled = true;
    state.chain = state.chain.then(recoverFromWedge).catch(() => {});
  }

  async function recoverFromWedge() {
    let relaunched = false;
    let outcome = "pass";
    try {
      const childEndpoint = state.endpoint;
      outcome = await withLaunchLock(profileDir, async () => {
        const live = await liveCdpEndpoint(profileDir);
        const port = endpointPort(live);
        const sameBrowser = Boolean(
          live && port && readDevtoolsBrowserPath(profileDir) === (await cdpBrowserPath(port, 2000))
        );
        const action = wedgeAction({ handled: false, childEndpoint, live, sameBrowser });
        if (action === "end-and-relaunch") {
          const pids = profileHolders(listProcesses(), profileDir).map((h) => h.pid);
          process.stderr.write(
            `[browser-control] The shared browser on ${live} lets no new session attach ` +
              `(a tab that stopped answering blocks every new session). Ending it ` +
              `(pid ${pids.join(", ") || "unknown"}) and starting a fresh one without its old tabs.\n`
          );
          if (!(await endProfileBrowser(profileDir))) {
            process.stderr.write("[browser-control] The browser did not exit; leaving it as it is.\n");
            return "pass";
          }
          setAsideSessions(profileDir);
        } else if (action === "relaunch") {
          process.stderr.write("[browser-control] No shared browser answers; starting a fresh one.\n");
          setAsideSessions(profileDir);
        } else if (action === "attach") {
          process.stderr.write(`[browser-control] Another session already started a fresh browser on ${live}.\n`);
        } else {
          process.stderr.write(
            `[browser-control] The browser on ${live} is not this profile's; leaving it as it is.\n`
          );
        }
        return action;
      });
      if (outcome !== "pass") relaunched = await restartFlow({ restoreLastSession: false });
      if (relaunched) {
        process.stderr.write(
          `[browser-control] Fresh browser on ${state.endpoint}; sending ${state.held.size} held call(s) again.\n`
        );
        for (const [id, { line }] of state.held) {
          state.pending.add(id);
          state.toolLines.set(id, line);
          state.child.stdin.write(line + "\n");
        }
        state.held.clear();
      }
    } catch (e) {
      process.stderr.write(`[browser-control] Recovering the browser failed: ${e.message}\n`);
    } finally {
      for (const [id, { response }] of state.held) {
        process.stdout.write(
          (outcome === "pass" ? response : buildErrorResponse(id, unavailableMessage())) + "\n"
        );
      }
      state.held.clear();
      state.recovering = false;
    }
  }

  // The generic "unavailable — try again" hid the actual cause: the stderr
  // profile-lock hint is invisible to the calling agent. When the profile is
  // provably held by a LIVE browser (another session of this agent), say so —
  // the caller can close that session or wait instead of blind-retrying.
  function unavailableMessage() {
    try {
      if (profileLooksLocked(profileDir)) {
        const procs = listProcesses();
        const holders = profileHolders(procs, profileDir);
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
      // A browser that did not start (Chrome on a Mac where it never
      // ran, a missing library) left its reason in the launch log.
      const fatal = launchFailureLine(path.join(profileDir, "launch.log"));
      if (fatal && profileHolders(listProcesses(), profileDir).length === 0)
        return `The browser could not start on this machine. It said: ${fatal}`;
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
      if (state.child && !state.childIsIdle && (state.needsRestart || endpointMoved())) {
        const ok = await restartFlow();
        if (!ok) {
          if (isRequest) process.stdout.write(buildErrorResponse(id, unavailableMessage()) + "\n");
          return;
        }
      }
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
    if (isRequest && method === "tools/call") state.toolLines.set(id, line);
    try {
      state.child.stdin.write(line + "\n");
    } catch (_) {
      state.child = null; // EPIPE — recovered by the next request
      if (isRequest) {
        state.pending.delete(id);
        state.toolLines.delete(id);
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

// --- Own-browser (extension) supervisor -------------------------------------
//
// One inner cli.js per session, spawned at init (upstream creates its backend
// — relay + connect page — lazily on the first browser tool, so the handshake
// never opens anything in the user's browser). What the supervisor adds:
//   * the user's browser is never a CHILD of this session: when its default
//     profile is not locked (no instance running) it is started through the
//     intermediary shell before the first browser call, otherwise upstream's
//     connect-page spawn would launch it under cli.js and the satellite's
//     teardown tree-kill would take the user's browser down;
//   * fast failures for the cases a wait cannot fix (no token in an
//     unattended session; a token that was stored but not delivered);
//   * a connect watchdog — upstream waits forever for the extension — that
//     fails pending requests with the actual cause and resets the child;
//   * restart-on-next-call after an out-of-band disconnect (dead backend);
//   * one browser_close at session end so the agent's tab and group go away.
function runExtensionSupervisor(cli, cfg, opts) {
  const userDataDir = defaultUserDataDir(cfg.executablePath, opts.home);
  const clientName = agentDisplayName(opts.agentSlug);
  const browserName = path.basename(cfg.executablePath || "the browser");
  // The profile the connect page opens in (own_profile.js): the hook's input
  // is set here alone, never taken from the environment the MCP was given.
  const profile = ownProfile.pickProfile(opts.profileSetting,
    ownProfile.browserProfiles(userDataDir, extensionInProfile), ownProfile.browserLabel(cfg.executablePath));
  delete process.env.OTO_BROWSER_PROFILE_DIR;
  delete process.env.PLAYWRIGHT_MCP_PROFILE_DIR_NAME;
  const state = {
    child: null,
    initLine: null, // client's verbatim initialize request
    initializedLine: null, // client's verbatim notifications/initialized
    pending: new Set(), // request ids awaiting a child response
    toolIds: new Set(), // pending ids that are browser tools/call
    listIds: new Set(), // tools/list ids whose responses get studio tools appended
    swallowReinit: false,
    connected: false, // a browser tool succeeded since the child (re)spawned
    needsRestart: false, // the child's browser is gone — respawn on the next call
    watchdog: null,
    watchdogId: void 0,
    chain: Promise.resolve(),
    shuttingDown: false,
    internal: new Map(), // the supervisor's own requests to the child: id → resolve
    internalSeq: 0,
    lastClick: null, // the last browser_click's target: an upload's default element
    root: null, // the client's first root (its roots/list answer)
  };

  function clearWatchdog() {
    if (state.watchdog) clearTimeout(state.watchdog);
    state.watchdog = null;
    state.watchdogId = void 0;
  }

  function failPending(message) {
    for (const id of state.pending) process.stdout.write(buildErrorResponse(id, message) + "\n");
    state.pending.clear();
    state.toolIds.clear();
  }

  function spawnChild() {
    const env = { ...process.env };
    const args = [cli, "--extension"];
    if (profile.folder) {
      env.OTO_BROWSER_PROFILE_DIR = profile.folder;
      env.PLAYWRIGHT_MCP_PROFILE_DIR_NAME = profile.folder;
      args.unshift("--require", path.join(__dirname, "profile_hook.js"));
    }
    const child = spawn(process.execPath, args, { stdio: ["pipe", "pipe", "pipe"], env });
    const out = new LineSplitter();
    child.stdout.on("data", (chunk) => {
      if (state.child !== child) return; // a retired child's late output
      for (let line of out.feed(chunk)) {
        const { id } = peekRpc(line);
        if (state.swallowReinit && id === REINIT_ID) {
          state.swallowReinit = false;
          continue;
        }
        if (id !== void 0 && state.internal.has(id)) {
          // An answer means the connect finished: the watchdog of the
          // agent's upload stands down.
          const settle = state.internal.get(id);
          state.internal.delete(id);
          clearWatchdog();
          if (toolErrorText(line) === null) state.connected = true;
          settle(line);
          continue;
        }
        if (id !== void 0) {
          state.pending.delete(id);
          if (id === state.watchdogId) clearWatchdog();
          const err = toolErrorText(line);
          if (state.toolIds.has(id)) {
            state.toolIds.delete(id);
            if (err === null) state.connected = true;
            else if (isClosedBrowserError(err)) state.needsRestart = true;
          }
          if (state.listIds.has(id)) {
            state.listIds.delete(id);
            try {
              line = JSON.stringify(ownUpload.patchUploadSchema(
                studio.mergeToolsResult(JSON.parse(line), studio.toolDefinitions())));
            } catch (_) {
              /* pass the child's line through untouched */
            }
          }
        }
        process.stdout.write(line + "\n");
      }
    });
    child.stderr.on("data", (chunk) => process.stderr.write(chunk));
    const gone = () => {
      if (state.child !== child) return;
      state.child = null;
      clearWatchdog();
      for (const settle of state.internal.values()) settle(null);
      state.internal.clear();
      failPending("The browser session ended — retry the action.");
    };
    child.on("exit", gone);
    child.on("error", gone);
    state.child = child;
    state.connected = false;
    state.needsRestart = false;
    if (state.initLine) {
      state.swallowReinit = true;
      child.stdin.write(rewriteInitId(rewriteClientName(state.initLine, clientName)) + "\n");
      if (state.initializedLine) child.stdin.write(state.initializedLine + "\n");
    }
    return child;
  }

  function restart(message) {
    const old = state.child;
    state.child = null;
    if (old) {
      try {
        old.kill();
      } catch (_) {
        /* already gone */
      }
    }
    clearWatchdog();
    failPending(message);
  }

  // Reasons no browser call can succeed in this session — answered at once,
  // never forwarded (forwarding would open a connect page nobody can approve).
  function permanentFailure() {
    if (opts.tokenWithheld)
      return "This session does not run as this computer's owner, so it cannot use the owner's browser.";
    if (opts.tokenExpected && !opts.hasToken)
      return (
        "The browser token was not handed to this session. The next session on this computer " +
        "gets it again. If it does not, save the token again in the machine's settings."
      );
    if (profile.error) return profile.error;
    if (opts.unattended && !opts.hasToken)
      return (
        "This unattended session (task, call or meeting) cannot click Allow in the " +
        "user's browser. Save the Playwright Extension token in the machine's settings " +
        "(Remote machines → Use my own browser) so sessions connect without asking."
      );
    return null;
  }

  function installHint() {
    if (profile.several.length)
      return (
        ` These profiles have the extension: ${ownProfile.profileList(profile.several)}. ` +
        "Set Browser profile in the machine's settings to the one you use."
      );
    const where = profile.folder ? path.join(userDataDir, profile.folder) : userDataDir;
    const there = profile.folder ? extensionInProfile(where) : extensionLooksInstalled(userDataDir);
    return there
      ? ""
      : ` The extension does not look installed in ${where} — install it from ${EXTENSION_INSTALL_URL}.`;
  }

  function connectTimeoutMessage() {
    if (opts.hasToken)
      return (
        `The Playwright Extension did not connect within ${connectTimeoutMs(true) / 1000} s. ` +
        `Make sure it is installed in ${browserName} on this machine and that the token saved ` +
        "in the machine's settings is current (regenerate it on the extension's page — its " +
        "toolbar icon — and save it again)." + installHint()
      );
    return (
      `Nobody approved the connection in the browser within ${connectTimeoutMs(false) / 1000} s. ` +
      'The user must click "Allow" on the Playwright Extension page that opened in their ' +
      "browser — or save the extension token in the machine's settings so sessions " +
      "connect without asking." + installHint()
    );
  }

  function armWatchdog(id) {
    if (state.connected || state.watchdog) return;
    state.watchdogId = id;
    state.watchdog = setTimeout(() => {
      state.watchdog = null;
      restart(connectTimeoutMessage());
    }, connectTimeoutMs(opts.hasToken));
  }

  // The user's browser must already be running before upstream spawns the
  // connect page, or that spawn becomes the browser itself (a child of this
  // session). Start it detached when its default profile is unlocked.
  async function ensureUserBrowser() {
    if (profileLooksLocked(userDataDir)) return;
    process.stderr.write(
      `[browser-control] ${browserName} is not running — starting it for the user.\n`
    );
    // The person's own browser: never a second instance, never a log in
    // their own browser's folder.
    launchDetachedBrowser(cfg.executablePath,
      profile.folder ? [`--profile-directory=${profile.folder}`] : [], { newInstance: false });
    for (let i = 0; i < 60 && !profileLooksLocked(userDataDir); i++)
      await new Promise((f) => setTimeout(f, 250));
  }

  // One request of the supervisor's own to the child (the upload's steps):
  // its answer line, or null when the child went away.
  function callChild(name, args) {
    const id = `__otodock_upload_${++state.internalSeq}__`;
    return new Promise((settle) => {
      state.internal.set(id, settle);
      try {
        state.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call",
          params: { name, arguments: args } }) + "\n");
      } catch (_) {
        state.internal.delete(id);
        settle(null);
      }
    });
  }

  function answer(id, text, isError) {
    if (!state.pending.has(id)) return; // the connect watchdog answered it already
    state.pending.delete(id);
    state.toolIds.delete(id);
    process.stdout.write((isError ? buildErrorResponse(id, text) : buildToolTextResponse(id, text)) + "\n");
  }

  // browser_file_upload in own mode (own_upload.js): read, clear the
  // chooser, build the files in the page, then the page's state.
  async function upload(id, args) {
    const cwd = state.root || process.cwd();
    const picked = ownUpload.resolveUploadPaths(args.paths, { cwd });
    if (picked.error) return answer(id, picked.error, true);
    const files = picked.files;
    const source = ownUpload.pageFunction(ownUpload.payloadsOf(files));
    await callChild("browser_file_upload", {});
    const given = typeof args.target === "string" && args.target ? args : state.lastClick;
    const evaluate = (where) => callChild("browser_evaluate", where
      ? { function: source, target: where.target, element: where.element || "the file field" }
      : { function: source });
    let reply = await evaluate(given);
    if (reply && given && given === state.lastClick && toolErrorText(reply) !== null)
      reply = await evaluate(null);
    if (reply === null) return answer(id, "The browser session ended. Retry the action.", true);
    const failed = toolErrorText(reply);
    const status = ownUpload.statusOf(failed === null ? resultText(reply) : "");
    const said = ownUpload.answerFor(status, files);
    if (failed !== null && status === null) return answer(id, failed, true);
    if (said.error) return answer(id, said.error, true);
    const text = resultText(await callChild("browser_snapshot", {}));
    return answer(id, text ? `${said.text}\n\n${text}` : said.text, false);
  }

  function noteClientLine(line, method, toolName) {
    if (!method && line.includes('"roots"')) {
      try {
        const roots = JSON.parse(line).result?.roots;
        const first = Array.isArray(roots) && roots[0] && roots[0].uri;
        if (first) state.root = require("url").fileURLToPath(first);
      } catch (_) {
        /* not a roots answer the supervisor can read */
      }
    }
    if (toolName === "browser_click") {
      const args = JSON.parse(line).params.arguments || {};
      if (typeof args.target === "string") state.lastClick = { target: args.target, element: args.element };
    }
  }

  async function handleClientLine(line) {
    if (state.shuttingDown) return;
    const { id, method, toolName, isRequest } = peekRpc(line);
    if (method === "initialize") {
      state.initLine = line;
      line = rewriteClientName(line, clientName);
    } else if (method === "notifications/initialized") state.initializedLine = line;
    noteClientLine(line, method, toolName);
    if (method === "tools/call") {
      const blocked = permanentFailure();
      if (blocked) {
        if (isRequest) process.stdout.write(buildErrorResponse(id, blocked) + "\n");
        return;
      }
      // Closing an unconnected session would open the connect page just to
      // close it — answer it here (mirrors the dedicated mode).
      if (toolName === "browser_close" && !state.connected) {
        if (isRequest)
          process.stdout.write(
            buildToolTextResponse(id, "Browser was not connected — nothing to close.") + "\n"
          );
        return;
      }
      if (!state.child || state.needsRestart)
        restart("The browser connection was reset — retry the action.");
      if (!state.child) spawnChild();
      if (!state.connected) {
        await ensureUserBrowser();
        if (state.shuttingDown || !state.child) return;
        if (isRequest) armWatchdog(id);
      }
      if (isRequest) state.toolIds.add(id);
      const args = isRequest && toolName === "browser_file_upload" ? JSON.parse(line).params.arguments || {} : null;
      if (args && Array.isArray(args.paths) && args.paths.length) {
        state.pending.add(id);
        await upload(id, args);
        return;
      }
    } else if (!state.child) {
      spawnChild();
    }
    if (isRequest) state.pending.add(id);
    try {
      state.child.stdin.write(line + "\n");
    } catch (_) {
      state.child = null; // EPIPE — recovered by the next request
      if (isRequest) {
        state.pending.delete(id);
        state.toolIds.delete(id);
        process.stdout.write(
          buildErrorResponse(id, "The browser session restarted — retry the action.") + "\n"
        );
      }
    }
  }

  // Session end: close the agent's tab + group cleanly (bounded), then exit.
  const BYE_ID = "__otodock_browser_bye__";
  function shutdown(signal) {
    if (state.shuttingDown) return;
    state.shuttingDown = true;
    clearWatchdog();
    const child = state.child;
    const finish = () => {
      if (child) {
        try {
          child.kill(signal);
        } catch (_) {
          /* already gone */
        }
      }
      process.exit(0);
    };
    if (!child || !state.connected) return finish();
    const timer = setTimeout(finish, 2000);
    child.stdout.on("data", (chunk) => {
      if (chunk.toString("utf8").includes(BYE_ID)) {
        clearTimeout(timer);
        finish();
      }
    });
    child.on("exit", () => {
      clearTimeout(timer);
      finish();
    });
    try {
      child.stdin.write(
        JSON.stringify({ jsonrpc: "2.0", id: BYE_ID, method: "tools/call",
          params: { name: "browser_close", arguments: {} } }) + "\n"
      );
    } catch (_) {
      finish();
    }
  }

  const stdinLines = new LineSplitter();
  process.stdin.on("data", (chunk) => {
    for (const line of stdinLines.feed(chunk)) {
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
  process.stdin.on("end", () => shutdown());
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(sig, () => shutdown(sig));

  spawnChild();
}

if (require.main === module) {
  main().catch((e) => {
    process.stderr.write(`[browser-control] Startup failed: ${e.message}\n`);
    process.exit(1);
  });
} else {
  module.exports = {
    sanitizeAgent, computeProfileDir, platformStateDirname, migrateLegacyProfileDir,
    profileDirForStart, resolveCliPath, detectBrowser,
    resolveBrowserConfig, isChromiumFamily, channelExecutable,
    profileLooksLocked, findOrphanHolders, readDevtoolsPort, agentDisplayName,
    seedProfileDisplayName,
    profileHolders, daemonLaunchArgs, tryLaunchLock, releaseLaunchLock, LAUNCH_LOCK,
    LAUNCH_LOCK_STALE_MS, isWedgedAttachError, wedgeAction, endpointPort, setAsideSessions,
    LineSplitter, peekRpc, rewriteInitId, buildErrorResponse,
    buildToolTextResponse, REINIT_ID,
    classifyClientLine,
    firefoxCandidates, detectSystemFirefox, playwrightFirefoxPath,
    EXTENSION_ID, EXTENSION_INSTALL_URL, extensionBrowserEnv, defaultUserDataDir,
    extensionLooksInstalled, extensionInProfile, rewriteClientName, toolErrorText, resultText,
    isClosedBrowserError, connectTimeoutMs,
    detachedLaunchArgv, launchFailureLine, runExtensionSupervisor,
  };
}
