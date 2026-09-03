"use strict";
// Capture-studio unit tests — run with: node tests/studio.test.js
// Covers the pure logic in studio.js (display allocation policy, motion
// easing, coordinate mapping, zoom planning, X11 wire format, ffmpeg argv,
// tool routing) — the live Xvfb/Chrome/ffmpeg paths are exercised by the
// standalone smoke drive, not here.
const assert = require("assert");
const {
  displayCandidates, minimumJerk, viewportOffsetFromMetrics, zoomPlan,
  sanitizeTakeName, buildRecordArgs, buildAudioArgs, buildMuxArgs, fakeMicArgs,
  keycodeForKeysym, mergeToolsResult,
  buildHandshake, parseSetup, buildQueryExtension, buildGetKeyboardMapping,
  buildGetInputFocus, buildFakeInput, overlayScript,
  ownsTool, toolDefinitions, ZOOM_LADDER, POINTER_STYLES,
} = require("../studio.js");
const { classifyClientLine } = require("../index.js");

// --- display allocation policy ---------------------------------------------
{
  const cands = displayCandidates();
  // :98 (film rig) and :99 (inspection browser) must be structurally
  // unreachable, and allocation probes downward from :97.
  assert.ok(!cands.includes(98) && !cands.includes(99), "forbidden displays excluded");
  assert.strictEqual(cands[0], 97);
  assert.ok(cands.length >= 5, "enough candidates for parallel studios");
  assert.deepStrictEqual([...cands].sort((a, b) => b - a), cands, "descending probe order");
}

// --- minimum-jerk easing -----------------------------------------------------
{
  assert.strictEqual(minimumJerk(0), 0);
  assert.strictEqual(minimumJerk(1), 1);
  assert.ok(Math.abs(minimumJerk(0.5) - 0.5) < 1e-9, "symmetric at midpoint");
  let prev = 0;
  for (let i = 1; i <= 20; i++) {
    const v = minimumJerk(i / 20);
    assert.ok(v >= prev, "monotonic");
    prev = v;
  }
  // slow start: far less ground covered in the first 10% than linear
  assert.ok(minimumJerk(0.1) < 0.02);
}

// --- viewport offset (the DIP-vs-CSS zoom fix) -------------------------------
{
  // Kiosk at 100%: no borders, no offset.
  assert.deepStrictEqual(
    viewportOffsetFromMetrics({ sx: 0, sy: 0, ow: 1920, oh: 1080, iw: 1920, ih: 1080 }, 1),
    [0, 0]
  );
  // Kiosk at 150% zoom: inner is CSS px (1280x720), outer stays DIP — naive
  // (ow - iw) math would invent a 320px phantom border; the dpr-scaled math
  // must not.
  assert.deepStrictEqual(
    viewportOffsetFromMetrics({ sx: 0, sy: 0, ow: 1920, oh: 1080, iw: 1280, ih: 720 }, 1.5),
    [0, 0]
  );
  // Windowed: symmetric side borders, the rest of the vertical delta is the
  // title/toolbar chrome above the viewport.
  const [ox, oy] = viewportOffsetFromMetrics(
    { sx: 100, sy: 50, ow: 1000, oh: 800, iw: 996, ih: 700 }, 1
  );
  assert.strictEqual(ox, 102);
  assert.strictEqual(oy, 50 + (800 - 700) - 2);
}

// --- zoom planning ------------------------------------------------------------
{
  assert.deepStrictEqual(zoomPlan(150), { percent: 150, steps: 3 }); // 110, 125, 150
  assert.deepStrictEqual(zoomPlan(100), { percent: 100, steps: 0 });
  assert.deepStrictEqual(zoomPlan(80), { percent: 80, steps: -2 }); // 90, 80
  assert.strictEqual(zoomPlan(147).percent, 150); // snaps to the ladder
  assert.strictEqual(zoomPlan(500).steps, ZOOM_LADDER.length - 1 - ZOOM_LADDER.indexOf(100));
  assert.strictEqual(zoomPlan(-5), null);
  assert.strictEqual(zoomPlan("nope"), null);
}

// --- take names ----------------------------------------------------------------
assert.strictEqual(sanitizeTakeName("take1"), "take1");
assert.strictEqual(sanitizeTakeName("take one.mp4"), "take_one");
assert.strictEqual(sanitizeTakeName("../../etc/passwd"), ".._.._etc_passwd".replace(/\//g, "_"));
assert.ok(!sanitizeTakeName("a/b\\c").match(/[/\\]/));
assert.strictEqual(sanitizeTakeName(""), "take");
assert.strictEqual(sanitizeTakeName(".."), "take");

// --- ffmpeg argv -----------------------------------------------------------------
{
  const base = { display: 97, width: 1920, height: 1080, fps: 30, crf: 18, drawMouse: true, outPath: "/tmp/t.mp4" };
  const v = buildRecordArgs(base);
  assert.ok(v.includes("x11grab") && v.includes(":97") && v.includes("1920x1080"));
  assert.ok(v.includes("-draw_mouse") && v[v.indexOf("-draw_mouse") + 1] === "1");
  // The take ffmpeg is VIDEO-ONLY by design — a combined x11grab+PCM process
  // is signal-deaf and mangles the video timeline (see buildRecordArgs).
  assert.ok(!v.includes("s16le") && !v.includes("aac") && !v.includes("pipe:0"), "video leg carries no audio");
  assert.strictEqual(v[v.length - 1], "/tmp/t.mp4");
  assert.ok(buildRecordArgs({ ...base, drawMouse: false })[v.indexOf("-draw_mouse") + 1] === "0",
    "overlay takes hide the real cursor");
  const a = buildAudioArgs("/tmp/a.pcm", "/tmp/a.m4a");
  assert.ok(a.includes("/tmp/a.pcm") && a.includes("s16le") && a.includes("aac"), "audio leg: fifo → aac");
  assert.strictEqual(a[a.length - 1], "/tmp/a.m4a");
  // Mux: positive offset delays the audio; negative trims its lead-in.
  const mPos = buildMuxArgs("/v.mp4", "/a.m4a", 1.234, "/out.mp4");
  assert.ok(mPos.join(" ").includes("-itsoffset 1.234 -i /a.m4a"));
  assert.ok(mPos.join(" ").includes("-c copy"), "mux is lossless");
  const mNeg = buildMuxArgs("/v.mp4", "/a.m4a", -0.5, "/out.mp4");
  assert.ok(mNeg.join(" ").includes("-ss 0.500 -i /a.m4a"));
  assert.ok(!mNeg.includes("-itsoffset"));
}

// --- fake microphone flags --------------------------------------------------------
{
  const f = fakeMicArgs("/takes/prompt.wav", false);
  assert.ok(f.includes("--use-fake-device-for-media-stream"), "fake capture device");
  assert.ok(f.includes("--use-fake-ui-for-media-stream"), "permission auto-accept (no bubble on camera)");
  // Default plays once then feeds silence — a looping prompt would re-trigger
  // STT before its silence endpoint fires.
  assert.ok(f.includes("--use-file-for-fake-audio-capture=/takes/prompt.wav%noloop"));
  const loop = fakeMicArgs("/takes/bed.wav", true);
  assert.ok(loop.includes("--use-file-for-fake-audio-capture=/takes/bed.wav"), "loop mode drops %noloop");
  assert.ok(!loop.join(" ").includes("%noloop"));
}

// --- keysym → keycode -----------------------------------------------------------
{
  // Synthetic 2-column map for keycodes 8..10: '=' unshifted on 9, Control on 10.
  const mapping = { minKeycode: 8, perKeycode: 2, keysyms: [0x61, 0x41, 0x3d, 0x2b, 0xffe3, 0] };
  assert.strictEqual(keycodeForKeysym(mapping, 0x3d), 9);
  assert.strictEqual(keycodeForKeysym(mapping, 0xffe3), 10);
  assert.strictEqual(keycodeForKeysym(mapping, 0x2b), 9); // shifted column found too
  assert.strictEqual(keycodeForKeysym(mapping, 0x999), null);
}

// --- X11 wire format --------------------------------------------------------------
{
  const hs = buildHandshake();
  assert.strictEqual(hs.length, 12);
  assert.strictEqual(hs.readUInt8(0), 0x6c); // little-endian
  assert.strictEqual(hs.readUInt16LE(2), 11); // protocol 11
  assert.strictEqual(hs.readUInt16LE(6), 0); // no auth
}
{
  const qe = buildQueryExtension("XTEST");
  assert.strictEqual(qe.length, 16); // 8 + 5 + 3 pad
  assert.strictEqual(qe.readUInt8(0), 98);
  assert.strictEqual(qe.readUInt16LE(2), 4); // length in 4-byte units
  assert.strictEqual(qe.readUInt16LE(4), 5); // name length
  assert.strictEqual(qe.toString("latin1", 8, 13), "XTEST");
}
{
  const km = buildGetKeyboardMapping(8, 248);
  assert.strictEqual(km.length, 8);
  assert.strictEqual(km.readUInt8(0), 101);
  assert.strictEqual(km.readUInt16LE(2), 2);
  assert.strictEqual(km.readUInt8(4), 8);
  assert.strictEqual(km.readUInt8(5), 248);
}
{
  const gif = buildGetInputFocus();
  assert.strictEqual(gif.length, 4);
  assert.strictEqual(gif.readUInt8(0), 43);
  assert.strictEqual(gif.readUInt16LE(2), 1);
}
{
  // FakeInput is exactly 36 bytes (xXTestFakeInputReq): motion carries
  // absolute root coords at offsets 24/26.
  const fi = buildFakeInput(130, 6, 0, 960, 540);
  assert.strictEqual(fi.length, 36);
  assert.strictEqual(fi.readUInt8(0), 130); // queried major opcode
  assert.strictEqual(fi.readUInt8(1), 2); // X_XTestFakeInput
  assert.strictEqual(fi.readUInt16LE(2), 9); // 36 / 4
  assert.strictEqual(fi.readUInt8(4), 6); // MotionNotify
  assert.strictEqual(fi.readInt16LE(24), 960);
  assert.strictEqual(fi.readInt16LE(26), 540);
  assert.strictEqual(fi.readUInt32LE(8), 0); // CurrentTime
  assert.strictEqual(fi.readUInt32LE(12), 0); // current-screen root
  const key = buildFakeInput(130, 2, 37, 0, 0); // KeyPress keycode 37
  assert.strictEqual(key.readUInt8(4), 2);
  assert.strictEqual(key.readUInt8(5), 37);
  const btn = buildFakeInput(130, 5, 1, 0, 0); // ButtonRelease button 1
  assert.strictEqual(btn.readUInt8(4), 5);
  assert.strictEqual(btn.readUInt8(5), 1);
}
{
  // Setup body: min/max keycode at offsets 26/27 of the fixed part.
  const body = Buffer.alloc(40);
  body.writeUInt32LE(0x02000000, 4); // resource-id-base
  body.writeUInt32LE(0x001fffff, 8); // resource-id-mask
  body.writeUInt8(8, 26);
  body.writeUInt8(255, 27);
  const setup = parseSetup(body);
  assert.strictEqual(setup.minKeycode, 8);
  assert.strictEqual(setup.maxKeycode, 255);
  assert.strictEqual(setup.resourceIdBase, 0x02000000);
}

// --- overlay cursors ---------------------------------------------------------------
{
  for (const style of ["overlay-arrow", "overlay-touch"]) {
    const js = overlayScript(style);
    // The imperative hook is the iframe-freeze fix — every glide step calls it.
    assert.ok(js.includes("__studioCursorSet"), `${style} has the imperative position hook`);
    assert.ok(js.includes("pointer-events:none"), `${style} cursor never eats clicks`);
    assert.ok(js.includes("2147483647"), `${style} sits above everything`);
  }
  const touch = overlayScript("overlay-touch");
  assert.ok(touch.includes("__studio_ripple") && touch.includes("__studioRipple"), "touch has the tap ripple");
  assert.ok(touch.includes("scale(.82)"), "touch has press-shrink feedback");
  assert.ok(overlayScript("overlay-arrow").includes("<svg"), "arrow is the SVG cursor");
}

// --- tool surface -------------------------------------------------------------------
{
  const defs = toolDefinitions();
  const names = defs.map((d) => d.name);
  assert.strictEqual(new Set(names).size, names.length, "unique tool names");
  for (const d of defs) {
    assert.ok(d.name.startsWith("studio_"), "family prefix");
    assert.ok(d.description && d.inputSchema && d.inputSchema.type === "object");
    assert.ok(ownsTool(d.name), `handler exists for ${d.name}`);
  }
  for (const required of [
    "studio_start", "studio_stop", "studio_record_start", "studio_record_stop",
    "studio_glide", "studio_click", "studio_type", "studio_press", "studio_scroll",
    "studio_set_zoom", "studio_screenshot", "studio_wait_text", "studio_wait_text_gone",
  ]) {
    assert.ok(names.includes(required), `${required} present`);
  }
  assert.ok(!ownsTool("browser_navigate"), "everyday browser tools are not intercepted");
  const start = defs.find((d) => d.name === "studio_start");
  assert.deepStrictEqual(start.inputSchema.properties.pointer.enum, POINTER_STYLES);
}

// --- tools/list merge ----------------------------------------------------------------
{
  const msg = { jsonrpc: "2.0", id: 3, result: { tools: [{ name: "browser_navigate" }] } };
  const merged = mergeToolsResult(msg, toolDefinitions());
  assert.strictEqual(merged.result.tools[0].name, "browser_navigate");
  assert.ok(merged.result.tools.some((t) => t.name === "studio_start"));
  // Error responses and odd shapes pass through untouched.
  const err = { jsonrpc: "2.0", id: 4, error: { code: -1, message: "x" } };
  assert.deepStrictEqual(mergeToolsResult(err, toolDefinitions()), err);
}

// --- client-line routing ---------------------------------------------------------------
{
  const owns = (n) => n.startsWith("studio_");
  assert.deepStrictEqual(
    classifyClientLine('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"studio_status"}}', owns),
    { kind: "studio", id: 1, params: { name: "studio_status" } }
  );
  assert.deepStrictEqual(
    classifyClientLine('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}', owns),
    { kind: "tools-list", id: 2 }
  );
  assert.strictEqual(
    classifyClientLine('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"browser_click"}}', owns).kind,
    "forward"
  );
  assert.strictEqual(classifyClientLine('{"jsonrpc":"2.0","id":4,"method":"initialize"}', owns).kind, "forward");
  assert.strictEqual(classifyClientLine("not json", owns).kind, "forward");
  // A notification named like a studio call (no id) is not a request — forward.
  assert.strictEqual(
    classifyClientLine('{"jsonrpc":"2.0","method":"tools/call","params":{"name":"studio_stop"}}', owns).kind,
    "forward"
  );
}

// --- non-Linux gating ---------------------------------------------------------------
// Off-Linux the tools must not be advertised, but a stray studio_* call must
// still be answered with the clear "requires Linux" error (not forwarded to
// the playwright child as an unknown tool). Async because dispatch reads
// process.platform inside its queued op — the stub must stay in place until
// the dispatches are awaited.
(async () => {
  const { dispatch } = require("../studio.js");
  const realPlatform = Object.getOwnPropertyDescriptor(process, "platform");
  const setPlatform = (v) =>
    Object.defineProperty(process, "platform", { value: v, configurable: true });
  try {
    for (const os of ["win32", "darwin"]) {
      setPlatform(os);
      assert.deepStrictEqual(toolDefinitions(), [], `no studio tools advertised on ${os}`);
      assert.ok(ownsTool("studio_start"), `dispatch still owns studio calls on ${os}`);
      const msg = { jsonrpc: "2.0", id: 9, result: { tools: [{ name: "browser_navigate" }] } };
      assert.strictEqual(mergeToolsResult(msg, toolDefinitions()).result.tools.length, 1);
    }
    setPlatform("win32");
    for (const name of ["studio_start", "studio_goto", "studio_click"]) {
      const resp = await dispatch(10, { name, arguments: {} });
      assert.ok(resp.result.isError, `${name} errors on win32`);
      assert.ok(
        resp.result.content[0].text.includes("requires Linux"),
        `${name} reports the Linux requirement, got: ${resp.result.content[0].text}`
      );
    }
    setPlatform("linux");
    assert.ok(toolDefinitions().length >= 15, "linux keeps the full studio tool set");
  } finally {
    Object.defineProperty(process, "platform", realPlatform);
  }
  console.log("capture-studio: all tests passed");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
