"use strict";

// Capture studio — staged screen-capture as first-class MCP tools.
//
// A `studio_*` tool family that productizes the launch-video filming rig:
// a DEDICATED Xvfb display + kiosk Chrome at exact geometry + ffmpeg x11grab,
// driven like a human (minimum-jerk pointer glides, real XTEST input, jittered
// typing cadence), with optional PulseAudio capture muxed into the take.
//
// This browser is COMPLETELY separate from the everyday inspection browser the
// rest of this MCP serves: its own display (auto-allocated, never :98/:99 —
// see DISPLAY_CANDIDATES), its own profile (~/.oto-dock/capture-studio/…,
// outside the synced workspace so logins never leave the machine), its own
// CDP endpoint. Nothing here touches the shared browser daemon.
//
// Hard-won rig lessons encoded here so no session relearns them:
//   * UI scale is set with REAL XTEST Ctrl+= / Ctrl+- keypresses. Two
//     alternatives are known-broken: --force-device-scale-factor (X11 kiosk
//     treats the screen size as DIP and renders past the screen edge) and CDP
//     Emulation.setDeviceMetricsOverride (latches on the CDP session, leaves a
//     stale viewport, every scripted click misses).
//   * CDP-synthetic mouse events never move the real X pointer — for a
//     recording with a live cursor the pointer must go through XTEST, which is
//     what x11grab's draw_mouse composites (hover states and context cursors
//     come free). draw_mouse follows the pointer style: overlay takes record
//     with draw_mouse 0 so the parked real cursor can never appear in frame.
//   * Under page zoom, screenX/outerWidth are DIP while innerWidth is CSS px —
//     XTEST targets are css*devicePixelRatio + viewport offset, with the
//     phantom-border fix (see viewportOffsetFromMetrics).
//   * devicePixelRatio is re-read on EVERY coordinate computation — a snapshot
//     taken on about:blank goes stale after the first navigation.
//   * DOM overlay cursors are positioned imperatively each step: a mousemove
//     listener only sees events in the top document and freezes over iframes.
//   * No window manager on the studio display: Chrome gets an explicit
//     --window-position=0,0 or the frame records with black bars; the root
//     cursor must be defined (xsetroot) or XFixes has nothing to composite.
//   * Zoom persists per-origin in the profile — set it AFTER navigating.
//
// The X server is spoken to directly over its unix socket (handshake, XTEST
// FakeInput, GetKeyboardMapping) — no python-xlib, no xdotool, no new npm
// deps. playwright-core is resolved from @playwright/mcp's tree, the same way
// index.js resolves cli.js.

const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// :99 is the everyday inspection browser, :98 the hand-run film rig — both may
// be live with other sessions' work. Allocation probes DOWNWARD from :97 so a
// collision is structurally impossible, and the range is asserted anyway.
const DISPLAY_MIN = 90;
const DISPLAY_MAX = 97;
const FORBIDDEN_DISPLAYS = [98, 99];

// Chrome's fixed zoom ladder — studio_set_zoom snaps to it and walks the
// distance from 100% with real keypresses.
const ZOOM_LADDER = [25, 33, 50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400, 500];

const POINTER_STYLES = ["xtest", "overlay-arrow", "overlay-touch"];

// X11 keysyms needed for the zoom chords.
const KEYSYM_CONTROL_L = 0xffe3;
const KEYSYM_EQUAL = 0x3d;
const KEYSYM_MINUS = 0x2d;
const KEYSYM_ZERO = 0x30;

// ---------------------------------------------------------------------------
// Pure helpers (unit-tested)
// ---------------------------------------------------------------------------

function displayCandidates() {
  const out = [];
  for (let n = DISPLAY_MAX; n >= DISPLAY_MIN; n--) {
    if (!FORBIDDEN_DISPLAYS.includes(n)) out.push(n);
  }
  return out;
}

// Human-motion easing (quintic): slow start, fast middle, gentle stop.
function minimumJerk(t) {
  return t * t * t * (10 - 15 * t + 6 * t * t);
}

// PHYSICAL-pixel offset of the page viewport from window metrics. Units
// differ under page zoom: screenX/outerWidth are DIP (≈physical at system
// scale 1) while innerWidth is CSS px — the viewport's physical size is
// inner*dpr, NOT inner. Mixing them puts every XTEST click a phantom border
// off at 150% zoom. (0,0) in kiosk mode, but the general math keeps windowed
// captures working too.
function viewportOffsetFromMetrics(m, dpr) {
  const border = Math.max(0, (m.ow - m.iw * dpr) / 2);
  const oy = m.sy + (m.oh - m.ih * dpr) - border;
  return [m.sx + border, Math.max(0, oy)];
}

// Snap a requested zoom % to Chrome's ladder and plan the keypress walk from
// the Ctrl+0 reset point (100%). Returns {percent, steps} — steps > 0 means
// Ctrl+= that many times, < 0 means Ctrl+-.
function zoomPlan(requested) {
  const pct = Number(requested);
  if (!isFinite(pct) || pct <= 0) return null;
  let best = ZOOM_LADDER[0];
  for (const z of ZOOM_LADDER) {
    if (Math.abs(z - pct) < Math.abs(best - pct)) best = z;
  }
  return { percent: best, steps: ZOOM_LADDER.indexOf(best) - ZOOM_LADDER.indexOf(100) };
}

function sanitizeTakeName(name) {
  const s = String(name || "").replace(/\.mp4$/i, "").replace(/[^A-Za-z0-9._-]/g, "_");
  return s && !/^\.+$/.test(s) ? s : "take";
}

// ffmpeg argv for a take — VIDEO ONLY, always. Audio is captured by a
// SEPARATE single-input ffmpeg (buildAudioArgs) and muxed at stop
// (buildMuxArgs): a combined x11grab+raw-PCM process is a triple trap with
// this pinned static build (verified live) — PCM on stdin makes it deaf to
// SIGINT/SIGTERM, and either way the zero-based PCM pts fight x11grab's
// wallclock pts, so its dup/drop logic compresses the video timeline.
function buildRecordArgs({ display, width, height, fps, crf, drawMouse, outPath }) {
  return [
    "-hide_banner", "-loglevel", "warning",
    "-f", "x11grab", "-framerate", String(fps), "-video_size", `${width}x${height}`,
    "-draw_mouse", drawMouse ? "1" : "0", "-thread_queue_size", "512", "-i", `:${display}`,
    "-c:v", "libx264", "-crf", String(crf), "-preset", "veryfast", "-pix_fmt", "yuv420p",
    "-y", outPath,
  ];
}

// The audio leg: raw s16le from parec through a FIFO → aac. Never signalled —
// it finalizes on its own when parec dies and the FIFO EOFs. (The pinned
// static ffmpeg has no libpulse, so `-f pulse` is not an option.)
function buildAudioArgs(fifo, outPath) {
  return [
    "-hide_banner", "-loglevel", "warning",
    "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", fifo,
    "-c:a", "aac", "-b:a", "192k", "-y", outPath,
  ];
}

// Final lossless mux. `offset` is the audio stream's true start in the video
// timeline (may be slightly negative if audio connected first — then the
// lead-in is trimmed instead).
function buildMuxArgs(videoPath, audioPath, offset, outPath) {
  const audioIn = offset >= 0
    ? ["-itsoffset", offset.toFixed(3), "-i", audioPath]
    : ["-ss", (-offset).toFixed(3), "-i", audioPath];
  return [
    "-hide_banner", "-loglevel", "error",
    "-i", videoPath, ...audioIn,
    "-map", "0:v", "-map", "1:a", "-c", "copy", "-y", outPath,
  ];
}

// Chrome flags for a fake microphone: getUserMedia captures the WAV instead
// of real hardware (the studio display has none) and the mic permission
// prompt auto-accepts — no bubble on camera. Chrome opens the file lazily at
// EACH capture start, so overwriting it between turns swaps the next
// utterance without a relaunch (verified live). Default is
// play-once-then-silence (%noloop): a looping prompt would re-trigger STT
// before its silence endpoint fires.
function fakeMicArgs(wavPath, loop) {
  return [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    `--use-file-for-fake-audio-capture=${wavPath}${loop ? "" : "%noloop"}`,
  ];
}

// Column-major keysym → keycode search over a GetKeyboardMapping table.
function keycodeForKeysym(mapping, keysym) {
  const { minKeycode, perKeycode, keysyms } = mapping;
  for (let col = 0; col < perKeycode; col++) {
    for (let i = 0; i < keysyms.length / perKeycode; i++) {
      if (keysyms[i * perKeycode + col] === keysym) return minKeycode + i;
    }
  }
  return null;
}

// Merge the studio tool definitions into a forwarded tools/list response.
// Anything unexpected (error response, paged shape) passes through untouched.
function mergeToolsResult(msg, defs) {
  if (msg && msg.result && Array.isArray(msg.result.tools)) {
    msg.result.tools = msg.result.tools.concat(defs);
  }
  return msg;
}

// ---------------------------------------------------------------------------
// X11 wire format (pure builders/parsers, unit-tested)
// ---------------------------------------------------------------------------

const xpad = (n) => (4 - (n % 4)) % 4;

// Connection setup: little-endian, protocol 11.0, no auth. The studio's own
// Xvfb runs without an auth cookie (same-user unix-socket access), exactly
// like the film rig's — python-xlib connected the same way.
function buildHandshake() {
  const b = Buffer.alloc(12);
  b.writeUInt8(0x6c, 0); // 'l'
  b.writeUInt16LE(11, 2);
  b.writeUInt16LE(0, 4);
  b.writeUInt16LE(0, 6); // auth name len
  b.writeUInt16LE(0, 8); // auth data len
  return b;
}

// The fields we need from the setup reply body (after the 8-byte prelude).
function parseSetup(body) {
  return {
    resourceIdBase: body.readUInt32LE(4),
    resourceIdMask: body.readUInt32LE(8),
    minKeycode: body.readUInt8(26),
    maxKeycode: body.readUInt8(27),
  };
}

function buildQueryExtension(name) {
  const n = Buffer.byteLength(name);
  const total = 8 + n + xpad(n);
  const b = Buffer.alloc(total);
  b.writeUInt8(98, 0);
  b.writeUInt16LE(total / 4, 2);
  b.writeUInt16LE(n, 4);
  b.write(name, 8);
  return b;
}

function buildGetKeyboardMapping(firstKeycode, count) {
  const b = Buffer.alloc(8);
  b.writeUInt8(101, 0);
  b.writeUInt16LE(2, 2);
  b.writeUInt8(firstKeycode, 4);
  b.writeUInt8(count, 5);
  return b;
}

function buildGetInputFocus() {
  const b = Buffer.alloc(4);
  b.writeUInt8(43, 0);
  b.writeUInt16LE(1, 2);
  return b;
}

// XTEST FakeInput (minor opcode 2), 36 bytes. type: KeyPress=2 KeyRelease=3
// ButtonPress=4 ButtonRelease=5 MotionNotify=6. For motion, detail=0 means
// absolute and root=0 means the current screen; for keys/buttons, detail is
// the keycode/button and x/y are ignored.
function buildFakeInput(majorOpcode, type, detail, x, y) {
  const b = Buffer.alloc(36);
  b.writeUInt8(majorOpcode, 0);
  b.writeUInt8(2, 1);
  b.writeUInt16LE(9, 2);
  b.writeUInt8(type, 4);
  b.writeUInt8(detail, 5);
  b.writeUInt32LE(0, 8); // time = CurrentTime
  b.writeUInt32LE(0, 12); // root = current screen
  b.writeInt16LE(Math.round(x), 24);
  b.writeInt16LE(Math.round(y), 26);
  return b;
}

// ---------------------------------------------------------------------------
// Minimal X11 client — connect, XTEST fake input, keyboard mapping
// ---------------------------------------------------------------------------

class X11Client {
  constructor() {
    this.sock = null;
    this.seq = 0; // requests sent since setup (16-bit wrap on the wire)
    this._buf = Buffer.alloc(0);
    this._pending = new Map(); // seq16 -> {resolve, reject}
    this.xtestOpcode = null;
    this.mapping = null;
  }

  connect(displayNum) {
    return new Promise((resolve, reject) => {
      const sock = net.createConnection(`/tmp/.X11-unix/X${displayNum}`);
      sock.once("error", reject);
      sock.once("connect", () => {
        sock.write(buildHandshake());
        let setup = Buffer.alloc(0);
        const onData = (chunk) => {
          setup = Buffer.concat([setup, chunk]);
          if (setup.length < 8) return;
          const status = setup.readUInt8(0);
          const total = 8 + setup.readUInt16LE(6) * 4;
          if (setup.length < total) return;
          sock.removeListener("data", onData);
          if (status !== 1) {
            const reason = setup.toString("latin1", 8, 8 + setup.readUInt8(1));
            reject(new Error(`X11 setup failed on :${displayNum}: ${reason || "refused"}`));
            return;
          }
          this.sock = sock;
          this.setup = parseSetup(setup.subarray(8, total));
          sock.on("data", (d) => this._onData(d));
          sock.on("error", (e) => this._failAll(e));
          sock.on("close", () => this._failAll(new Error("X11 connection closed")));
          // Any bytes past the setup reply are already server messages.
          if (setup.length > total) this._onData(setup.subarray(total));
          resolve();
        };
        sock.on("data", onData);
      });
    });
  }

  close() {
    if (this.sock) {
      try { this.sock.destroy(); } catch (_) { /* already gone */ }
      this.sock = null;
    }
  }

  _failAll(err) {
    for (const [, p] of this._pending) p.reject(err);
    this._pending.clear();
  }

  // Server message pump: replies are 32 + 4*length bytes, errors and core
  // events are 32, GenericEvent (35) carries a length like replies. Replies
  // and errors are matched to pending requests by 16-bit sequence number;
  // unsolicited events (e.g. MappingNotify) are consumed and dropped.
  _onData(chunk) {
    this._buf = Buffer.concat([this._buf, chunk]);
    for (;;) {
      if (this._buf.length < 32) return;
      const type = this._buf.readUInt8(0) & 0x7f;
      let size = 32;
      if (type === 1 || type === 35) size = 32 + this._buf.readUInt32LE(4) * 4;
      if (this._buf.length < size) return;
      const msg = this._buf.subarray(0, size);
      this._buf = this._buf.subarray(size);
      if (type === 1 || type === 0) {
        const seq16 = msg.readUInt16LE(2);
        const p = this._pending.get(seq16);
        if (p) {
          this._pending.delete(seq16);
          if (type === 1) p.resolve(msg);
          else p.reject(new Error(`X11 error code ${msg.readUInt8(1)} (seq ${seq16})`));
        } else if (type === 0) {
          process.stderr.write(`[capture-studio] unmatched X11 error code ${msg.readUInt8(1)}\n`);
        }
      }
    }
  }

  _send(buf) {
    this.seq += 1;
    this.sock.write(buf);
    return this.seq & 0xffff;
  }

  _request(buf) {
    return new Promise((resolve, reject) => {
      const seq16 = this._send(buf);
      this._pending.set(seq16, { resolve, reject });
    });
  }

  // GetInputFocus round trip — the classic X "sync": resolves once the server
  // has processed everything sent before it.
  sync() {
    return this._request(buildGetInputFocus());
  }

  async init() {
    const ext = await this._request(buildQueryExtension("XTEST"));
    if (!ext.readUInt8(8)) throw new Error("the X server has no XTEST extension");
    this.xtestOpcode = ext.readUInt8(9);
    const { minKeycode, maxKeycode } = this.setup;
    const rep = await this._request(buildGetKeyboardMapping(minKeycode, maxKeycode - minKeycode + 1));
    const perKeycode = rep.readUInt8(1);
    const count = rep.readUInt32LE(4);
    const keysyms = new Array(count);
    for (let i = 0; i < count; i++) keysyms[i] = rep.readUInt32LE(32 + i * 4);
    this.mapping = { minKeycode, perKeycode, keysyms };
  }

  keycode(keysym) {
    const kc = keycodeForKeysym(this.mapping, keysym);
    if (kc == null) throw new Error(`no keycode maps keysym 0x${keysym.toString(16)}`);
    return kc;
  }

  async fakeMotion(x, y) {
    this._send(buildFakeInput(this.xtestOpcode, 6, 0, x, y));
    await this.sync();
  }

  async fakeButton(button, press) {
    this._send(buildFakeInput(this.xtestOpcode, press ? 4 : 5, button, 0, 0));
    await this.sync();
  }

  async fakeKey(keycode, press) {
    this._send(buildFakeInput(this.xtestOpcode, press ? 2 : 3, keycode, 0, 0));
    await this.sync();
  }
}

// ---------------------------------------------------------------------------
// Overlay cursors (DOM) — arrow and touch styles
// ---------------------------------------------------------------------------

// Both styles expose the same imperative API on window:
//   __studioCursorSet(x, y)   position (called EVERY glide step — mousemove
//                             listeners freeze over iframes)
//   __studioCursorPress(down) press feedback (touch: shrink; arrow: no-op)
//   __studioRipple()          click ripple (touch only)
function overlayScript(style) {
  if (style === "overlay-touch") {
    return `(() => {
  if (window.__studioCursor) return;
  const c = document.createElement('div');
  c.id = '__studio_cursor';
  c.style.cssText = 'position:fixed;left:0;top:0;width:28px;height:28px;' +
    'margin:-14px 0 0 -14px;border-radius:50%;background:rgba(255,255,255,.45);' +
    'border:1px solid rgba(0,0,0,.18);box-shadow:0 1px 6px rgba(0,0,0,.35);' +
    'z-index:2147483647;pointer-events:none;opacity:.25;' +
    'transition:opacity .3s ease, transform .12s ease;';
  const style = document.createElement('style');
  style.textContent = '@keyframes __studio_ripple{from{transform:scale(.5);opacity:.5}' +
    'to{transform:scale(2.8);opacity:0}}';
  (document.head || document.documentElement).appendChild(style);
  (document.body || document.documentElement).appendChild(c);
  c.style.left = (window.innerWidth / 2) + 'px';
  c.style.top = (window.innerHeight / 2) + 'px';
  let fadeT;
  const show = () => {
    c.style.opacity = '.5';
    clearTimeout(fadeT);
    fadeT = setTimeout(() => { c.style.opacity = '.25'; }, 450);
  };
  window.__studioCursorSet = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; show(); };
  window.__studioCursorPress = (down) => { c.style.transform = down ? 'scale(.82)' : 'scale(1)'; show(); };
  window.__studioRipple = () => {
    const r = document.createElement('div');
    r.style.cssText = 'position:fixed;width:28px;height:28px;margin:-14px 0 0 -14px;' +
      'border-radius:50%;border:2px solid rgba(255,255,255,.8);background:rgba(255,255,255,.15);' +
      'z-index:2147483646;pointer-events:none;left:' + c.style.left + ';top:' + c.style.top + ';' +
      'animation:__studio_ripple .5s ease-out forwards;';
    (document.body || document.documentElement).appendChild(r);
    setTimeout(() => r.remove(), 600);
  };
  window.__studioCursor = c;
})();`;
  }
  // overlay-arrow: white fill, dark outline — reads on any theme.
  return `(() => {
  if (window.__studioCursor) return;
  const c = document.createElement('div');
  c.id = '__studio_cursor';
  c.style.cssText = 'position:fixed;left:0;top:0;width:24px;height:24px;' +
    'z-index:2147483647;pointer-events:none;transform:translate(-2px,-2px);transition:none;';
  c.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24">' +
    '<path d="M5 3l14 9.3-6.2 1L16 19.5l-2.6 1.2-3.2-6.2-4.2 4.6z"' +
    ' fill="#fff" stroke="#1a1a2e" stroke-width="1.4"/></svg>';
  (document.body || document.documentElement).appendChild(c);
  c.style.left = (window.innerWidth / 2) + 'px';
  c.style.top = (window.innerHeight / 2) + 'px';
  window.__studioCursorSet = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; };
  window.__studioCursorPress = () => {};
  window.__studioRipple = () => {};
  window.__studioCursor = c;
})();`;
}

// ---------------------------------------------------------------------------
// Process helpers
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((f) => setTimeout(f, ms));
const rand = (a, b) => a + Math.random() * (b - a);

function ffmpegBin() {
  return process.env.FFMPEG || path.join(os.homedir(), "tools", "bin", "ffmpeg");
}

function ffprobeBin() {
  return process.env.FFPROBE || path.join(path.dirname(ffmpegBin()), "ffprobe");
}

// The film rig on :98 is a production lane — recording alongside it would
// drop ITS frames (CPU contention), so takes refuse to start while an x11grab
// against :98 is live.
function rigIsRecording() {
  let pids = [];
  try {
    pids = fs.readdirSync("/proc").filter((d) => /^\d+$/.test(d));
  } catch (_) {
    return false;
  }
  for (const pid of pids) {
    try {
      const cmd = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8").replace(/\0/g, " ");
      if (cmd.includes("x11grab") && / :98(\s|$)/.test(cmd)) return true;
    } catch (_) {
      /* raced exit */
    }
  }
  return false;
}

// SIGKILL any process holding a studio profile dir (main browser process
// only). Studio profiles are exclusively ours, so a holder is always a leak
// from a torn-down session — unlike the everyday profile, there is no
// live-owner case to protect.
function killProfileHolders(profileDir) {
  let pids = [];
  try {
    pids = fs.readdirSync("/proc").filter((d) => /^\d+$/.test(d));
  } catch (_) {
    return;
  }
  for (const pid of pids) {
    try {
      const cmd = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8");
      if (cmd.includes(`--user-data-dir=${profileDir}`) && !cmd.includes("--type=")) {
        process.kill(Number(pid), "SIGKILL");
      }
    } catch (_) {
      /* raced exit / not ours */
    }
  }
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (_) {
    return false;
  }
}

// The everyday browser tools block loopback origins via
// PLAYWRIGHT_MCP_BLOCKED_ORIGINS (manifest default) — the studio drives a
// raw CDP attach that never sees that list, so it enforces the same default
// here: without it, studio_goto + studio_eval would read any localhost
// service (proxy API, satellite control ports) the normal tools can't reach.
function assertStudioUrlAllowed(rawUrl) {
  let u;
  try {
    u = new URL(String(rawUrl));
  } catch (_) {
    throw new Error(`invalid url: ${rawUrl}`);
  }
  if (!/^https?:$/.test(u.protocol)) {
    throw new Error("studio_goto takes http(s) URLs only");
  }
  const host = u.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const loopback =
    host === "localhost" || host === "::1" || host.endsWith(".localhost") ||
    /^127(\.\d{1,3}){3}$/.test(host) || host === "0.0.0.0";
  if (loopback && process.env.OTO_STUDIO_ALLOW_LOOPBACK !== "1") {
    throw new Error(
      "loopback origins are blocked in the studio (same default as the " +
      "everyday browser tools); set OTO_STUDIO_ALLOW_LOOPBACK=1 in the " +
      "MCP env to opt in",
    );
  }
}

// True when something answers on the display's unix socket.
function socketAlive(sockPath) {
  return new Promise((resolve) => {
    const c = net.createConnection(sockPath);
    const done = (alive) => {
      c.destroy();
      resolve(alive);
    };
    c.setTimeout(300, () => done(false));
    c.once("connect", () => done(true));
    c.once("error", () => done(false));
  });
}

// A display is takeable when neither socket nor lock exists, or when the
// leftovers provably belong to a dead server (a SIGKILLed Xvfb leaves both
// behind) and we can remove them. A live socket is ALWAYS respected — the
// lock-pid check alone would misjudge a server started with -nolock. Racing
// another allocator is fine: Xvfb's own locking makes the loser's spawn fail
// and it moves to the next candidate.
async function displayIsFree(n) {
  const sock = `/tmp/.X11-unix/X${n}`;
  const lock = `/tmp/.X${n}-lock`;
  const sockExists = fs.existsSync(sock);
  const lockExists = fs.existsSync(lock);
  if (!sockExists && !lockExists) return true;
  if (sockExists && (await socketAlive(sock))) return false;
  let pid = NaN;
  try {
    pid = parseInt(fs.readFileSync(lock, "utf8").trim(), 10);
  } catch (_) {
    /* no lock or unreadable */
  }
  if (Number.isInteger(pid) && pidAlive(pid)) return false;
  if (lockExists && !Number.isInteger(pid)) return false; // unreadable lock — don't guess
  try {
    if (lockExists) fs.unlinkSync(lock);
    if (sockExists) fs.unlinkSync(sock);
    return true;
  } catch (_) {
    return false; // not ours to clean (perms) — skip the candidate
  }
}

function resolveChromeBin() {
  if (process.env.OTO_STUDIO_CHROME) return process.env.OTO_STUDIO_CHROME;
  // index.js exports PLAYWRIGHT_MCP_EXECUTABLE_PATH after auto-detection.
  if (process.env.PLAYWRIGHT_MCP_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_MCP_EXECUTABLE_PATH;
  for (const p of [
    "/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge", "/usr/bin/chromium", "/usr/bin/chromium-browser",
  ]) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function resolvePlaywrightCore() {
  try {
    return require.resolve("playwright-core", {
      paths: [path.join(__dirname, "node_modules", "@playwright", "mcp"), __dirname],
    });
  } catch (_) {
    return null;
  }
}

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

function run(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"], ...opts });
    let out = "";
    let err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (err += d));
    p.on("error", (e) => resolve({ code: -1, out, err: String(e.message) }));
    p.on("exit", (code) => resolve({ code, out, err }));
  });
}

// ---------------------------------------------------------------------------
// The studio session
// ---------------------------------------------------------------------------

class Studio {
  constructor() {
    this.s = null; // active session state, or null
  }

  _active() {
    if (!this.s) throw new Error("no studio is running — studio_start first");
    return this.s;
  }

  _page() {
    // Newest open page: popups (OAuth etc.) land on top in kiosk mode, so
    // selector and keyboard verbs follow them; XTEST screen coords reach
    // anything on the display regardless.
    const pages = this.s.context.pages().filter((p) => !p.isClosed());
    if (!pages.length) throw new Error("the studio browser has no open page");
    return pages[pages.length - 1];
  }

  async start(opts) {
    if (this.s) {
      throw new Error(`a studio is already running on :${this.s.display} — studio_stop first`);
    }
    if (process.platform !== "linux") throw new Error("the capture studio requires Linux (Xvfb/X11)");
    const width = Math.round(opts.width || 1920);
    const height = Math.round(opts.height || 1080);
    const fps = Math.round(opts.fps || 30);
    const crf = Math.round(opts.crf || 18);
    const pointer = opts.pointer || "xtest";
    if (!POINTER_STYLES.includes(pointer)) {
      throw new Error(`pointer must be one of ${POINTER_STYLES.join(", ")}`);
    }
    let fakeMic = null;
    if (opts.fake_mic_wav) {
      const wav = path.resolve(opts.fake_mic_wav);
      if (!fs.existsSync(wav)) throw new Error(`fake_mic_wav not found: ${wav}`);
      if (!/\.wav$/i.test(wav)) throw new Error("fake_mic_wav must be a .wav file (16-bit PCM is the safe format)");
      if (wav.includes("%")) throw new Error("fake_mic_wav path must not contain '%' (Chrome parses it as a flag-option separator)");
      fakeMic = { wav, loop: !!opts.fake_mic_loop };
    }
    const chromeBin = resolveChromeBin();
    if (!chromeBin) throw new Error("no Chromium-family browser found for the studio (set OTO_STUDIO_CHROME)");
    const pwPath = resolvePlaywrightCore();
    if (!pwPath) throw new Error("playwright-core is not installed next to @playwright/mcp");

    const agent = String(process.env.OTO_AGENT_NAME || "default").replace(/[^A-Za-z0-9_-]/g, "_") || "default";
    // profile_dir is confined to THIS agent's studio tree: it feeds
    // killProfileHolders (SIGKILLs whatever holds --user-data-dir=<dir>),
    // wipe_profile's recursive delete, and a CDP-driven Chrome launch — an
    // arbitrary path would let one agent kill/steal another agent's live
    // browser profile or delete any writable directory.
    const studioBase = path.join(os.homedir(), ".oto-dock", "capture-studio", agent);
    let profileDir = path.join(studioBase, "profile");
    if (opts.profile_dir) {
      profileDir = path.resolve(studioBase, String(opts.profile_dir));
      if (profileDir === studioBase) profileDir = path.join(studioBase, "profile");
      if (!profileDir.startsWith(studioBase + path.sep)) {
        throw new Error(
          "profile_dir must be a name or relative subpath inside this " +
          "agent's studio dir (" + studioBase + ") — absolute paths and " +
          "'..' are not allowed",
        );
      }
    }
    const outDir = process.env.OTO_WORKSPACE_DIR
      ? path.join(process.env.OTO_WORKSPACE_DIR, "capture-studio")
      : path.join(studioBase, "takes");

    // A leaked Chrome from a torn-down session would hold the profile lock.
    killProfileHolders(profileDir);
    if (opts.wipe_profile) fs.rmSync(profileDir, { recursive: true, force: true });
    fs.mkdirSync(profileDir, { recursive: true });
    fs.mkdirSync(outDir, { recursive: true });
    // A previous run's DevToolsActivePort would be read as the NEW Chrome's
    // port before it writes its own — clear it so the poll below can't lie.
    fs.rmSync(path.join(profileDir, "DevToolsActivePort"), { force: true });

    // -- Xvfb on a free display ------------------------------------------------
    let display = null;
    let xvfb = null;
    for (const n of displayCandidates()) {
      if (FORBIDDEN_DISPLAYS.includes(n)) continue; // structural, but belt-and-braces
      if (!(await displayIsFree(n))) continue;
      const proc = spawn("Xvfb", [`:${n}`, "-screen", "0", `${width}x${height}x24`, "-nolisten", "tcp"], {
        stdio: "ignore",
      });
      let exited = false;
      proc.on("exit", () => { exited = true; });
      let up = false;
      for (let i = 0; i < 50 && !exited; i++) {
        await sleep(100);
        if (fs.existsSync(`/tmp/.X11-unix/X${n}`)) { up = true; break; }
      }
      if (up) {
        display = n;
        xvfb = proc;
        break;
      }
      try { proc.kill("SIGKILL"); } catch (_) { /* lost the race — next candidate */ }
    }
    if (display == null) throw new Error(`no free X display in :${DISPLAY_MIN}–:${DISPLAY_MAX}`);

    // Everything created past this point registers on `pending` so a failure
    // anywhere in the bring-up tears down exactly what exists so far.
    const pending = { display, xvfb, profileDir };
    const fail = async (err) => {
      await this._teardown(pending).catch(() => {});
      throw err;
    };

    try {
      // Root cursor: Xvfb starts with none — define an arrow so XFixes has a
      // cursor image to hand x11grab. Overlay styles skip it (their takes
      // record with draw_mouse 0 and the parked pointer should stay invisible
      // even in xtest-style screenshots of a pre-Chrome frame).
      if (pointer === "xtest") {
        await run("xsetroot", ["-cursor_name", "left_ptr"], { env: { ...process.env, DISPLAY: `:${display}` } });
      }

      // -- optional audio: per-display null sink, Chrome routed into it ---------
      let audio = null;
      if (opts.audio) {
        const sink = `otostudio${display}`;
        const info = await run("pactl", ["info"]);
        if (info.code !== 0) {
          const started = await run("pulseaudio", ["--start", "--exit-idle-time=-1"]);
          if (started.code !== 0) {
            throw new Error(
              "PulseAudio is not available (install the pulseaudio + pulseaudio-utils packages for audio capture)"
            );
          }
        }
        // A torn-down session leaks its module (the daemon outlives us) —
        // sweep our display's stale sink before loading a fresh one.
        const mods = await run("pactl", ["list", "short", "modules"]);
        for (const line of mods.out.split("\n")) {
          if (line.includes(`sink_name=${sink}`)) {
            await run("pactl", ["unload-module", line.split("\t")[0]]);
          }
        }
        const loaded = await run("pactl", ["load-module", "module-null-sink", `sink_name=${sink}`]);
        if (loaded.code !== 0) throw new Error(`could not create the null sink: ${loaded.err.trim()}`);
        // An idle null sink doesn't clock its monitor — parec would deliver a
        // trickle and the mux would starve (buffering the whole take in RAM,
        // never writing a byte). A permanent silence feed keeps the sink
        // RUNNING so the monitor streams steady realtime PCM; page audio
        // simply mixes on top of the zeros.
        const feeder = spawn(
          "paplay",
          ["--raw", "--format=s16le", "--rate=48000", "--channels=2", "--latency-msec=50", "-d", sink, "/dev/zero"],
          { stdio: "ignore" }
        );
        audio = { sink, moduleId: loaded.out.trim(), feeder };
        pending.audio = audio;
      }

      // -- kiosk Chrome at exact geometry ---------------------------------------
      // No WM on the studio display: an explicit position is required or the
      // frame records with black bars. NEVER --force-device-scale-factor (the
      // X11-kiosk DIP trap) — UI scale is studio_set_zoom's job.
      const chromeArgs = [
        `--user-data-dir=${profileDir}`,
        "--window-position=0,0",
        `--window-size=${width},${height}`,
        "--kiosk",
        "--remote-debugging-port=0",
        "--no-first-run", "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
        "--disable-infobars", "--disable-features=TranslateUI",
        "--password-store=basic", "--disable-gpu", "--lang=en-US",
        "--autoplay-policy=no-user-gesture-required",
      ];
      if (!audio) chromeArgs.push("--mute-audio");
      if (fakeMic) chromeArgs.push(...fakeMicArgs(fakeMic.wav, fakeMic.loop));
      chromeArgs.push(opts.url || "about:blank");
      const chromeEnv = { ...process.env, DISPLAY: `:${display}` };
      if (audio) chromeEnv.PULSE_SINK = audio.sink;
      const chrome = spawn(chromeBin, chromeArgs, { stdio: "ignore", env: chromeEnv });

      let port = null;
      for (let i = 0; i < 100; i++) {
        await sleep(150);
        port = readDevtoolsPort(profileDir);
        if (port) break;
        if (chrome.exitCode != null) throw new Error(`the studio Chrome exited at startup (code ${chrome.exitCode})`);
      }
      if (!port) throw new Error("the studio Chrome did not expose its CDP port within 15s");

      // -- playwright attach + X input client -----------------------------------
      const pw = require(pwPath);
      let browser = null;
      for (let i = 0; ; i++) {
        try {
          browser = await pw.chromium.connectOverCDP(`http://127.0.0.1:${port}`);
          break;
        } catch (e) {
          if (i >= 5) throw e; // the port file was written, so this is real
          await sleep(500);
        }
      }
      const context = browser.contexts()[0];
      const page = context.pages()[0] || (await context.newPage());
      await page.bringToFront();

      const x11 = new X11Client();
      await x11.connect(display);
      await x11.init();

      this.s = {
        display, width, height, fps, crf, pointer, profileDir, outDir,
        xvfb, chrome, browser, context, x11, audio, fakeMic,
        drawMouse: pointer === "xtest",
        // Glides start from the visual centre in both styles (the overlay DOM
        // cursor also installs there).
        pos: [width / 2, height / 2],
        recording: null,
        zoomPercent: 100,
      };
      // Park the REAL pointer: center for xtest (it IS the visible cursor),
      // bottom-right for overlay (clipped to a pixel, but still over Chrome —
      // PointerRoot focus must land on the window for XTEST zoom keys).
      if (pointer === "xtest") await x11.fakeMotion(width / 2, height / 2);
      else await x11.fakeMotion(width - 1, height - 1);

      if (pointer !== "xtest") {
        await page.addInitScript(overlayScript(pointer));
        await page.evaluate(overlayScript(pointer)).catch(() => {}); // pre-first-goto page
      }
      if (opts.theme === "dark" || opts.theme === "light") {
        await page.emulateMedia({ colorScheme: opts.theme });
        this.s.theme = opts.theme;
      }
      if (opts.url) await this._settleAfterNav();

      const dpr = await this._dpr();
      return [
        `studio up on :${display} — ${width}x${height}@${fps} (crf ${crf}), pointer ${pointer}`,
        `chrome pid ${chrome.pid}, CDP http://127.0.0.1:${port}, dpr ${dpr}`,
        `profile ${profileDir}`,
        `takes dir ${outDir}`,
        audio ? `audio sink ${audio.sink} (record with audio:true to mux it)` : "audio off (start with audio:true to capture it)",
        fakeMic ? `fake mic serving ${fakeMic.wav}${fakeMic.loop ? " (looping)" : " (play once, then silence)"}` : null,
      ].filter(Boolean).join("\n");
    } catch (e) {
      await fail(e);
    }
  }

  async _settleAfterNav() {
    const page = this._page();
    if (this.s.pointer !== "xtest") await page.evaluate(overlayScript(this.s.pointer)).catch(() => {});
    if (this.s.theme) await page.emulateMedia({ colorScheme: this.s.theme }).catch(() => {});
  }

  async _teardown(partial) {
    const s = partial || this.s;
    if (!s) return;
    if (s.recording) await this.recordStop().catch(() => {});
    if (s.browser) await s.browser.close().catch(() => {});
    if (s.chrome && s.chrome.exitCode == null) {
      try { s.chrome.kill("SIGTERM"); } catch (_) { /* gone */ }
      for (let i = 0; i < 20 && s.chrome.exitCode == null; i++) await sleep(100);
      if (s.chrome.exitCode == null) { try { s.chrome.kill("SIGKILL"); } catch (_) { /* gone */ } }
    }
    if (s.profileDir) killProfileHolders(s.profileDir);
    if (s.x11) s.x11.close();
    if (s.xvfb && s.xvfb.exitCode == null) {
      try { s.xvfb.kill("SIGTERM"); } catch (_) { /* gone */ }
    }
    if (s.audio) {
      if (s.audio.feeder && s.audio.feeder.exitCode == null) {
        try { s.audio.feeder.kill("SIGKILL"); } catch (_) { /* gone */ }
      }
      await run("pactl", ["unload-module", s.audio.moduleId]).catch(() => {});
    }
  }

  async stop() {
    const s = this._active();
    await this._teardown(s);
    this.s = null;
    return `studio on :${s.display} stopped`;
  }

  async goto({ url, wait_until }) {
    this._active();
    if (!url) throw new Error("url is required");
    assertStudioUrlAllowed(url);
    const page = this._page();
    await page.goto(url, { waitUntil: wait_until || "load" });
    await this._settleAfterNav();
    return `at ${page.url()} (dpr ${await this._dpr()})`;
  }

  // dpr is re-read every time it is used — never cached across navigations
  // (the about:blank-snapshot bug).
  async _dpr() {
    return (await this._page().evaluate("window.devicePixelRatio").catch(() => 1)) || 1;
  }

  async _viewportOffset(dpr) {
    const m = await this._page().evaluate(
      "({sx: window.screenX, sy: window.screenY, ow: window.outerWidth, oh: window.outerHeight," +
        " iw: window.innerWidth, ih: window.innerHeight})"
    );
    return viewportOffsetFromMetrics(m, dpr);
  }

  // Page coords of the target: a selector's centre (with a human off-centre
  // nudge) or literal CSS-pixel x/y.
  async _targetPoint({ target, x, y }) {
    if (target) {
      const loc = this._page().locator(target).first();
      await loc.waitFor({ state: "visible", timeout: 10000 });
      const box = await loc.boundingBox();
      if (!box) throw new Error(`no bounding box for ${target}`);
      return [box.x + box.width * rand(0.42, 0.58), box.y + box.height * rand(0.4, 0.6)];
    }
    if (typeof x !== "number" || typeof y !== "number") {
      throw new Error("give either target (CSS selector) or numeric x + y");
    }
    return [x, y];
  }

  async glide(args) {
    const s = this._active();
    let tx, ty;
    if (args.screen) {
      if (s.pointer !== "xtest") throw new Error("screen coordinates need the xtest pointer style");
      if (typeof args.x !== "number" || typeof args.y !== "number") {
        throw new Error("screen:true needs numeric x + y (physical pixels)");
      }
      [tx, ty] = [args.x, args.y];
    } else {
      const [px, py] = await this._targetPoint(args);
      if (s.pointer === "xtest") {
        const dpr = await this._dpr();
        const [ox, oy] = await this._viewportOffset(dpr);
        [tx, ty] = [px * dpr + ox, py * dpr + oy];
      } else {
        [tx, ty] = [px, py];
      }
    }
    const [sx, sy] = s.pos;
    const dist = Math.hypot(tx - sx, ty - sy);
    if (dist < 2) return `pointer already at (${Math.round(tx)}, ${Math.round(ty)})`;
    const duration = args.duration || Math.min(1.3, Math.max(0.35, dist / 1400));
    // One bezier control point, offset perpendicular to the path — a gentle
    // arc, not a straight teleport line.
    const mx = (sx + tx) / 2;
    const my = (sy + ty) / 2;
    const bend = rand(-0.12, 0.12) * dist;
    const nx = -(ty - sy) / dist;
    const ny = (tx - sx) / dist;
    const cx = mx + nx * bend;
    const cy = my + ny * bend;
    const steps = Math.max(2, Math.round(duration * 60));
    const t0 = Date.now();
    const page = s.pointer === "xtest" ? null : this._page();
    for (let i = 1; i <= steps; i++) {
      const t = minimumJerk(i / steps);
      const x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t * t * tx;
      const y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t * t * ty;
      if (s.pointer === "xtest") {
        await s.x11.fakeMotion(x, y);
      } else {
        // NOTE: args-taking evaluates must be REAL functions — in playwright's
        // JS API a string is always an expression (unlike Python, which
        // auto-detects function strings; that difference bit this port once).
        await page.mouse.move(x, y);
        await page
          .evaluate(([px2, py2]) => window.__studioCursorSet && window.__studioCursorSet(px2, py2), [x, y])
          .catch(() => {});
      }
      const wall = t0 + duration * 1000 * (i / steps);
      const now = Date.now();
      if (wall > now) await sleep(wall - now);
    }
    s.pos = [tx, ty];
    return `glided to (${Math.round(tx)}, ${Math.round(ty)}) in ${duration.toFixed(2)}s`;
  }

  async click(args = {}) {
    const s = this._active();
    let where = "";
    if (args.target || typeof args.x === "number") where = await this.glide(args) + "; ";
    const button = { left: 1, middle: 2, right: 3 }[args.button || "left"];
    if (!button) throw new Error("button must be left, middle or right");
    await sleep(rand(80, 180));
    if (s.pointer === "xtest") {
      await s.x11.fakeButton(button, true);
      await sleep(rand(50, 90));
      await s.x11.fakeButton(button, false);
    } else {
      const page = this._page();
      const pwButton = args.button === "middle" ? "middle" : args.button === "right" ? "right" : "left";
      await page.evaluate(() => window.__studioCursorPress && window.__studioCursorPress(true)).catch(() => {});
      await page.mouse.down({ button: pwButton });
      await sleep(rand(50, 90));
      await page.mouse.up({ button: pwButton });
      await page
        .evaluate(() => {
          if (window.__studioCursorPress) window.__studioCursorPress(false);
          if (window.__studioRipple) window.__studioRipple();
        })
        .catch(() => {});
    }
    await sleep(rand(50, 120));
    return where + "clicked";
  }

  async type({ text, cps }) {
    this._active();
    if (typeof text !== "string" || !text) throw new Error("text is required");
    const page = this._page();
    const base = 1 / (cps || 9);
    for (const ch of text) {
      await page.keyboard.type(ch);
      let delay = rand(0.6, 1.5) * base;
      if (" .,!?—".includes(ch)) delay += rand(0.05, 0.18);
      await sleep(delay * 1000);
    }
    return `typed ${text.length} chars`;
  }

  async press({ key }) {
    this._active();
    if (!key) throw new Error("key is required");
    await this._page().keyboard.press(key);
    return `pressed ${key}`;
  }

  async scroll({ dy, steps, duration }) {
    const s = this._active();
    if (typeof dy !== "number" || !dy) throw new Error("dy (CSS px, positive = down) is required");
    const n = Math.max(1, Math.round(steps || 12));
    const total = (duration || 0.6) * 1000;
    const page = this._page();
    if (s.pointer === "xtest") {
      // Position the synthetic wheel where the real cursor is (physical px →
      // back to CSS px for CDP).
      const dpr = await this._dpr();
      const [ox, oy] = await this._viewportOffset(dpr);
      await page.mouse.move((s.pos[0] - ox) / dpr, (s.pos[1] - oy) / dpr);
    }
    for (let i = 0; i < n; i++) {
      await page.mouse.wheel(0, dy / n);
      await sleep(total / n);
    }
    return `scrolled ${dy}px`;
  }

  // REAL Ctrl+0 / Ctrl+= / Ctrl+- keypresses via XTEST — the only zoom path
  // that both renders correctly and keeps coordinates honest (see the trap
  // notes at the top). Zoom persists per-origin in the profile: call this
  // AFTER navigating to the page being filmed.
  async setZoom({ percent }) {
    const s = this._active();
    const plan = zoomPlan(percent);
    if (!plan) throw new Error("percent must be a positive number");
    const chord = async (keysym) => {
      const ctrl = s.x11.keycode(KEYSYM_CONTROL_L);
      const key = s.x11.keycode(keysym);
      await s.x11.fakeKey(ctrl, true);
      await sleep(30);
      await s.x11.fakeKey(key, true);
      await sleep(40);
      await s.x11.fakeKey(key, false);
      await sleep(30);
      await s.x11.fakeKey(ctrl, false);
      await sleep(120);
    };
    await chord(KEYSYM_ZERO); // reset to 100% so the walk is absolute
    for (let i = 0; i < Math.abs(plan.steps); i++) {
      await chord(plan.steps > 0 ? KEYSYM_EQUAL : KEYSYM_MINUS);
    }
    s.zoomPercent = plan.percent;
    await sleep(200);
    const dpr = await this._dpr();
    const note = plan.percent !== Math.round(Number(percent)) ? ` (snapped from ${percent})` : "";
    return `zoom ${plan.percent}%${note}, dpr now ${dpr}`;
  }

  async recordStart({ take_name, audio, out_dir }) {
    const s = this._active();
    if (s.recording) throw new Error(`already recording ${s.recording.path} — studio_record_stop first`);
    if (rigIsRecording()) {
      throw new Error("the film rig on :98 is recording right now — starting a second capture would drop its frames; wait and retry");
    }
    if (audio && !s.audio) throw new Error("the studio was started without audio — studio_stop, then studio_start with audio:true");
    const name = sanitizeTakeName(take_name);
    // out_dir stays inside the workspace (or the default takes dir): the
    // recorder overwrites `<name>.mp4` with -y, so an arbitrary directory
    // would be a file-clobber primitive from a tool argument.
    let dir = s.outDir;
    if (out_dir) {
      const baseOut = process.env.OTO_WORKSPACE_DIR
        ? path.resolve(process.env.OTO_WORKSPACE_DIR)
        : s.outDir;
      dir = path.resolve(baseOut, String(out_dir));
      if (dir !== baseOut && !dir.startsWith(baseOut + path.sep)) {
        throw new Error(
          "out_dir must be a relative subpath inside the agent workspace " +
          "(" + baseOut + ")",
        );
      }
    }
    fs.mkdirSync(dir, { recursive: true });
    const outPath = path.join(dir, `${name}.mp4`);
    const videoPath = audio ? `${outPath}.video.mp4` : outPath;
    const args = buildRecordArgs({
      display: s.display, width: s.width, height: s.height, fps: s.fps, crf: s.crf,
      drawMouse: s.drawMouse, outPath: videoPath,
    });
    const ff = spawn(ffmpegBin(), args, { stdio: ["ignore", "ignore", "pipe"] });
    let ffErr = "";
    ff.stderr.on("data", (d) => (ffErr += d));
    let parec = null;
    let audioProc = null;
    let fifo = null;
    let audioPath = null;
    if (audio) {
      fifo = path.join(os.tmpdir(), `otostudio${s.display}.pcm`);
      audioPath = `${outPath}.audio.m4a`;
      fs.rmSync(fifo, { force: true });
      const mk = await run("mkfifo", [fifo]);
      if (mk.code !== 0) throw new Error(`mkfifo failed: ${mk.err.trim()}`);
      audioProc = spawn(ffmpegBin(), buildAudioArgs(fifo, audioPath), { stdio: "ignore" });
      // The shell blocks opening the FIFO until the audio ffmpeg opens the
      // read side, then execs parec — so the pid IS parec and the two connect
      // cleanly. Low latency matters: at the null sink's default
      // (seconds-deep) buffering the monitor delivers bursty, seconds-late
      // content, wrecking the end-anchored A/V alignment below.
      parec = spawn(
        "/bin/sh",
        ["-c", `exec parec -d '${s.audio.sink}.monitor' --format=s16le --rate=48000 --channels=2 --latency-msec=50 > '${fifo}'`],
        { stdio: "ignore" }
      );
    }
    await sleep(700);
    if (ff.exitCode != null || (audioProc && audioProc.exitCode != null)) {
      for (const p of [ff, audioProc, parec]) {
        if (p && p.exitCode == null) { try { p.kill("SIGKILL"); } catch (_) { /* gone */ } }
      }
      if (fifo) fs.rmSync(fifo, { force: true });
      throw new Error(`ffmpeg failed to start: ${ffErr.trim().slice(-400)}`);
    }
    s.recording = {
      proc: ff, audioProc, parec, fifo, path: outPath, videoPath, audioPath,
      startedAt: Date.now(), getErr: () => ffErr,
    };
    return `recording ${outPath}${audio ? " (with audio)" : ""} — studio_record_stop to finalize`;
  }

  async recordStop() {
    const s = this._active();
    const rec = s.recording;
    if (!rec) throw new Error("no recording is running");
    s.recording = null;
    // Audio leg first: parec's death EOFs the FIFO and its ffmpeg finalizes
    // the m4a BY ITSELF (it is never signalled). Then SIGINT finalizes the
    // video. The two kill moments anchor the A/V alignment below.
    if (rec.parec) {
      try { rec.parec.kill("SIGTERM"); } catch (_) { /* already gone */ }
      for (let i = 0; i < 20 && rec.parec.exitCode == null; i++) await sleep(50);
    }
    const parecKilledAt = Date.now();
    try { rec.proc.kill("SIGINT"); } catch (_) { /* already gone */ }
    const sigintAt = Date.now();
    for (let i = 0; i < 100 && rec.proc.exitCode == null; i++) await sleep(100);
    if (rec.proc.exitCode == null) { try { rec.proc.kill("SIGKILL"); } catch (_) { /* gone */ } }
    if (rec.audioProc) {
      for (let i = 0; i < 100 && rec.audioProc.exitCode == null; i++) await sleep(100);
      if (rec.audioProc.exitCode == null) { try { rec.audioProc.kill("SIGKILL"); } catch (_) { /* gone */ } }
    }
    if (rec.fifo) fs.rmSync(rec.fifo, { force: true });
    const probeFile = async (file) => {
      const p = await run(ffprobeBin(), [
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
        "-of", "json", file,
      ]);
      if (p.code !== 0) return null;
      try { return JSON.parse(p.out); } catch (_) { return null; }
    };
    const vInfo = await probeFile(rec.videoPath);
    if (!vInfo) {
      throw new Error(`recording finalized badly (${rec.videoPath}): ${rec.getErr().trim().slice(-400)}`);
    }
    let aligned = "";
    if (rec.audioPath) {
      // The audio file starts when parec CONNECTED — later than the video
      // start — so muxed at zero it would play early. Both ENDS are moments
      // we timed (audio ends at the parec kill, video at the SIGINT), which
      // pins the audio's true start in the video timeline.
      const aInfo = await probeFile(rec.audioPath);
      const vDur = vInfo && Number(vInfo.format.duration);
      const aDur = aInfo && Number(aInfo.format.duration);
      if (aInfo && isFinite(vDur) && isFinite(aDur)) {
        const offset = vDur - (sigintAt - parecKilledAt) / 1000 - aDur;
        const mux = await run(ffmpegBin(), buildMuxArgs(rec.videoPath, rec.audioPath, offset, rec.path));
        if (mux.code === 0) {
          fs.rmSync(rec.videoPath, { force: true });
          fs.rmSync(rec.audioPath, { force: true });
          aligned = `, audio muxed (start ${offset >= 0 ? "+" : ""}${offset.toFixed(2)}s)`;
        } else {
          fs.renameSync(rec.videoPath, rec.path);
          aligned = `, AUDIO MUX FAILED (${mux.err.trim().slice(-200)}) — video kept, audio at ${rec.audioPath}`;
        }
      } else {
        fs.renameSync(rec.videoPath, rec.path);
        aligned = `, AUDIO LEG FAILED — video-only take (audio file unreadable)`;
      }
    }
    // Self-QC: report what was actually written, not what was intended.
    const info = (await probeFile(rec.path)) || vInfo;
    const streams = (info.streams || [])
      .map((st) => (st.codec_type === "video" ? `${st.codec_name} ${st.width}x${st.height}@${st.avg_frame_rate}` : `${st.codec_name} audio`))
      .join(" + ");
    return `recorded ${rec.path} — ${Number(info.format.duration).toFixed(2)}s, ${streams}${aligned}`;
  }

  // A true recorded frame (x11grab, cursor composited per pointer style) —
  // what ffmpeg would capture, not what the DOM thinks it looks like.
  async screenshot({ name }) {
    const s = this._active();
    const file = path.join(s.outDir, `${sanitizeTakeName(name || `frame-${Date.now()}`)}.png`);
    fs.mkdirSync(s.outDir, { recursive: true });
    const res = await run(ffmpegBin(), [
      "-hide_banner", "-loglevel", "error",
      "-f", "x11grab", "-video_size", `${s.width}x${s.height}`, "-draw_mouse", s.drawMouse ? "1" : "0",
      "-i", `:${s.display}`, "-frames:v", "1", "-y", file,
    ]);
    if (res.code !== 0) throw new Error(`screenshot failed: ${res.err.trim().slice(-300)}`);
    return `frame saved: ${file}`;
  }

  async _textVisible(text) {
    return this._page().evaluate((t) => {
      if (document.body && document.body.innerText.includes(t)) return true;
      for (const el of document.querySelectorAll("input[placeholder],textarea[placeholder]")) {
        if (el.placeholder.includes(t)) return true;
      }
      return false;
    }, text);
  }

  async waitText({ text, timeout_s, gone }) {
    this._active();
    if (!text) throw new Error("text is required");
    const deadline = Date.now() + (timeout_s || 15) * 1000;
    for (;;) {
      const visible = await this._textVisible(text).catch(() => false);
      if (gone ? !visible : visible) return `${JSON.stringify(text)} is ${gone ? "gone" : "visible"}`;
      if (Date.now() > deadline) {
        throw new Error(`timed out waiting for ${JSON.stringify(text)} to ${gone ? "disappear" : "appear"}`);
      }
      await sleep(250);
    }
  }

  async evalJs({ expression }) {
    this._active();
    if (!expression) throw new Error("expression is required");
    const value = await this._page().evaluate(expression);
    let text;
    try {
      text = JSON.stringify(value);
    } catch (_) {
      text = String(value);
    }
    if (text === undefined) text = "undefined";
    return text.length > 8192 ? text.slice(0, 8192) + "…(truncated)" : text;
  }

  async status() {
    if (!this.s) {
      return `no studio running${rigIsRecording() ? " (NOTE: the film rig on :98 IS recording)" : ""}`;
    }
    const s = this.s;
    let pageLine = "page unreachable (browser gone?)";
    try {
      pageLine = `page ${this._page().url()}, zoom ${s.zoomPercent}%, dpr ${await this._dpr()}`;
    } catch (_) {
      /* keep the fallback line */
    }
    const lines = [
      `display :${s.display} — ${s.width}x${s.height}@${s.fps}, pointer ${s.pointer}`,
      `chrome ${s.chrome.exitCode == null ? `up (pid ${s.chrome.pid})` : `EXITED (${s.chrome.exitCode})`}`,
      pageLine,
      `recording ${s.recording ? s.recording.path : "no"}`,
      `audio ${s.audio ? `sink ${s.audio.sink}` : "off"}`,
      `fake mic ${s.fakeMic ? `${s.fakeMic.wav}${s.fakeMic.loop ? " (looping)" : ""}` : "off"}`,
      `takes dir ${s.outDir}`,
    ];
    if (rigIsRecording()) lines.push("NOTE: the film rig on :98 is recording — takes will refuse to start");
    return lines.join("\n");
  }
}

// ---------------------------------------------------------------------------
// MCP tool surface
// ---------------------------------------------------------------------------

const TARGET_PROPS = {
  target: { type: "string", description: "CSS selector — glides to its centre (with a human off-centre nudge)" },
  x: { type: "number", description: "page CSS x (or physical screen x with screen:true)" },
  y: { type: "number", description: "page CSS y (or physical screen y with screen:true)" },
  screen: { type: "boolean", description: "treat x/y as RAW physical screen pixels (xtest style only) — for Chrome's own bubbles and cross-origin iframe chrome, coords as read off a screenshot" },
  duration: { type: "number", description: "glide seconds (default scales with distance, ≤1.3s)" },
};

const TOOL_DEFS = [
  {
    name: "studio_start",
    description:
      "Start the capture studio: a dedicated Xvfb display (auto-allocated — never the inspection browser's or the film rig's) + kiosk Chrome at exact geometry with a persistent profile, ready to record. Separate from the everyday browser tools. Pointer styles: xtest (REAL X cursor — desktop takes), overlay-arrow (DOM arrow), overlay-touch (soft fingertip dot with tap ripple — mobile-look 9:16 takes).",
    inputSchema: {
      type: "object",
      properties: {
        width: { type: "number", description: "screen width px (default 1920)" },
        height: { type: "number", description: "screen height px (default 1080; use 1080x1920 for 9:16)" },
        fps: { type: "number", description: "recording framerate (default 30)" },
        url: { type: "string", description: "initial URL to open" },
        pointer: { type: "string", enum: POINTER_STYLES, description: "pointer style (default xtest)" },
        profile_dir: { type: "string", description: "profile name or relative subpath inside this agent's studio dir (default: its persistent 'profile' — logins and per-origin zoom survive restarts); absolute paths are rejected" },
        wipe_profile: { type: "boolean", description: "wipe the profile first for a clean-slate take (costs any staged logins)" },
        audio: { type: "boolean", description: "set up audio capture: a dedicated PulseAudio null sink, Chrome unmuted into it" },
        fake_mic_wav: { type: "string", description: "WAV file served as the MICROPHONE (Chrome's fake capture device — the studio display has no real mic): getUserMedia/STT hears this file and the mic permission auto-accepts. 16-bit PCM WAV (48 kHz safest). Chrome re-reads the file at each capture start — overwrite it between turns to change the next utterance" },
        fake_mic_loop: { type: "boolean", description: "loop the fake-mic WAV (default: play once then feed silence — right for STT silence endpointing)" },
        theme: { type: "string", enum: ["dark", "light"], description: "emulate prefers-color-scheme (sites with their own theme toggle need studio_eval instead)" },
        crf: { type: "number", description: "x264 CRF (default 18 — intermediate quality for editing)" },
      },
    },
  },
  {
    name: "studio_stop",
    description: "Stop the capture studio: finalize any recording, close Chrome and the display, release the audio sink.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "studio_goto",
    description: "Navigate the studio browser. Set zoom AFTER this (zoom persists per-origin).",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string" },
        wait_until: { type: "string", enum: ["load", "domcontentloaded", "networkidle", "commit"], description: "default load" },
      },
      required: ["url"],
    },
  },
  {
    name: "studio_record_start",
    description: "Start recording the studio display to <takes dir>/<take_name>.mp4 (H.264 yuv420p at the studio fps). Refuses while the film rig on :98 is recording — wait and retry. audio:true muxes the studio's audio sink (needs studio_start with audio:true).",
    inputSchema: {
      type: "object",
      properties: {
        take_name: { type: "string", description: "output name (sanitized, .mp4 appended)" },
        audio: { type: "boolean", description: "mux the studio audio sink into the take" },
        out_dir: { type: "string", description: "output directory as a relative subpath inside the agent workspace (default <workspace>/capture-studio/)" },
      },
      required: ["take_name"],
    },
  },
  {
    name: "studio_record_stop",
    description: "Finalize the current recording and report its probed duration/streams.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "studio_glide",
    description: "Glide the pointer to a target like a human: gently curved path, minimum-jerk pacing, DPR-correct even under zoom.",
    inputSchema: { type: "object", properties: { ...TARGET_PROPS } },
  },
  {
    name: "studio_click",
    description: "Click (optionally gliding to a target first): human settle, ~70ms press-release. xtest style sends REAL X button events (reaches Chrome's own bubbles via screen:true); overlay-touch shows a tap ripple.",
    inputSchema: {
      type: "object",
      properties: { ...TARGET_PROPS, button: { type: "string", enum: ["left", "middle", "right"], description: "default left" } },
    },
  },
  {
    name: "studio_type",
    description: "Type into the focused element with human cadence (jittered, longer pauses after spaces/punctuation). Click the field first.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string" },
        cps: { type: "number", description: "average chars/second (default 9)" },
      },
      required: ["text"],
    },
  },
  {
    name: "studio_press",
    description: "Press a key or chord in the page (Playwright names: Enter, Tab, Control+a…).",
    inputSchema: { type: "object", properties: { key: { type: "string" } }, required: ["key"] },
  },
  {
    name: "studio_scroll",
    description: "Smooth wheel scroll at the current pointer position.",
    inputSchema: {
      type: "object",
      properties: {
        dy: { type: "number", description: "CSS px, positive = down" },
        steps: { type: "number", description: "wheel increments (default 12)" },
        duration: { type: "number", description: "seconds (default 0.6)" },
      },
      required: ["dy"],
    },
  },
  {
    name: "studio_set_zoom",
    description: "Set the browser UI zoom with REAL Ctrl+=/Ctrl+- keypresses (the only correct path — device-scale flags and CDP metrics emulation are known traps). Snaps to Chrome's zoom ladder (…90, 100, 110, 125, 150…). Zoom persists per-origin: navigate first.",
    inputSchema: { type: "object", properties: { percent: { type: "number" } }, required: ["percent"] },
  },
  {
    name: "studio_screenshot",
    description: "Save a true recorded frame (x11grab — exactly what a take would capture, cursor included for xtest) to the takes dir for QC.",
    inputSchema: { type: "object", properties: { name: { type: "string", description: "file name (png)" } } },
  },
  {
    name: "studio_wait_text",
    description: "Wait until text is visible on the page (body text or an input/textarea placeholder — placeholders cover composer-idle signals).",
    inputSchema: {
      type: "object",
      properties: { text: { type: "string" }, timeout_s: { type: "number", description: "default 15" } },
      required: ["text"],
    },
  },
  {
    name: "studio_wait_text_gone",
    description: "Wait until text disappears from the page (body text or placeholders).",
    inputSchema: {
      type: "object",
      properties: { text: { type: "string" }, timeout_s: { type: "number", description: "default 15" } },
      required: ["text"],
    },
  },
  {
    name: "studio_eval",
    description: "Evaluate a JS expression in the studio page (staging escape hatch: localStorage themes, feature flags, seeding state). Returns the JSON value.",
    inputSchema: { type: "object", properties: { expression: { type: "string" } }, required: ["expression"] },
  },
  {
    name: "studio_status",
    description: "Report the studio state: display, geometry, Chrome, page URL, zoom/DPR, recording, audio sink, and whether the film rig is busy.",
    inputSchema: { type: "object", properties: {} },
  },
];

const studio = new Studio();

// Best-effort reaping when the wrapper dies without studio_stop (the platform
// tree-kill normally covers this; a bare kill of the wrapper alone would
// otherwise orphan Xvfb/Chrome/ffmpeg and leak the display).
process.on("exit", () => {
  const s = studio.s;
  if (!s) return;
  for (const p of [
    s.recording && s.recording.proc, s.recording && s.recording.audioProc,
    s.recording && s.recording.parec, s.audio && s.audio.feeder, s.chrome, s.xvfb,
  ]) {
    if (p && p.exitCode == null) {
      try { p.kill("SIGKILL"); } catch (_) { /* gone */ }
    }
  }
});

const HANDLERS = {
  studio_start: (a) => studio.start(a),
  studio_stop: () => studio.stop(),
  studio_goto: (a) => studio.goto(a),
  studio_record_start: (a) => studio.recordStart(a),
  studio_record_stop: () => studio.recordStop(),
  studio_glide: (a) => studio.glide(a),
  studio_click: (a) => studio.click(a),
  studio_type: (a) => studio.type(a),
  studio_press: (a) => studio.press(a),
  studio_scroll: (a) => studio.scroll(a),
  studio_set_zoom: (a) => studio.setZoom(a),
  studio_screenshot: (a) => studio.screenshot(a),
  studio_wait_text: (a) => studio.waitText({ ...a, gone: false }),
  studio_wait_text_gone: (a) => studio.waitText({ ...a, gone: true }),
  studio_eval: (a) => studio.evalJs(a),
  studio_status: () => studio.status(),
};

function ownsTool(name) {
  return Object.prototype.hasOwnProperty.call(HANDLERS, name);
}

// The studio is Linux-only (Xvfb/X11); on other satellite hosts the tools are
// not advertised at all. ownsTool stays platform-agnostic so a stray studio_*
// call still reaches dispatch and gets the clear "requires Linux" error from
// start() instead of leaking to the playwright child as an unknown tool.
// Read process.platform at call time — tests stub it.
function toolDefinitions() {
  return process.platform === "linux" ? TOOL_DEFS : [];
}

// Serialized dispatch: studio verbs must never interleave (a glide landing
// mid-click would corrupt the take), so every call queues behind the last.
let opChain = Promise.resolve();

function dispatch(id, params) {
  const runOp = async () => {
    try {
      if (process.platform !== "linux") {
        throw new Error("the capture studio requires Linux (Xvfb/X11)");
      }
      const text = await HANDLERS[params.name](params.arguments || {});
      return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text }] } };
    } catch (e) {
      return {
        jsonrpc: "2.0", id,
        result: { content: [{ type: "text", text: `Error: ${e.message}` }], isError: true },
      };
    }
  };
  const p = opChain.then(runOp, runOp);
  opChain = p.then(() => {}, () => {});
  return p;
}

module.exports = {
  ownsTool, toolDefinitions, dispatch,
  // pure internals, exported for unit tests
  displayCandidates, minimumJerk, viewportOffsetFromMetrics, zoomPlan,
  sanitizeTakeName, buildRecordArgs, buildAudioArgs, buildMuxArgs, fakeMicArgs,
  keycodeForKeysym, mergeToolsResult,
  buildHandshake, parseSetup, buildQueryExtension, buildGetKeyboardMapping,
  buildGetInputFocus, buildFakeInput, overlayScript, X11Client,
  DISPLAY_CANDIDATES: displayCandidates(), ZOOM_LADDER, POINTER_STYLES,
};
