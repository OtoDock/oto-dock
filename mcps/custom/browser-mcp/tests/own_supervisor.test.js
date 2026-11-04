"use strict";
// The extension supervisor end to end over a fake inner MCP
// (fixtures/fake_inner_mcp.js): an upload answered by the supervisor's own
// three calls (the chooser cleared, the page function on the clicked
// element, the snapshot) with none of their answers or the file's bytes
// reaching the agent; the profile hook loaded with its folder; the
// withheld-token and the missing-profile sentences.
// Run with: node tests/own_supervisor.test.js
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const FAKE = path.join(__dirname, "fixtures", "fake_inner_mcp.js");
const RUN = path.join(__dirname, "fixtures", "run_supervisor.js");

function machine({ profiles } = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "own-sup-home-"));
  const userData = path.join(home, ".config", "google-chrome");
  fs.mkdirSync(userData, { recursive: true });
  // The person's browser is running: no detached start in the test.
  fs.symlinkSync(`host-${process.pid}`, path.join(userData, "SingletonLock"));
  if (profiles) {
    fs.writeFileSync(path.join(userData, "Local State"), JSON.stringify({ profile: { info_cache: profiles } }));
    for (const f of Object.keys(profiles)) fs.mkdirSync(path.join(userData, f));
  }
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "own-sup-cwd-"));
  fs.writeFileSync(path.join(cwd, "invoice.pdf"), "%PDF-1.7 the bytes");
  return { home, cwd, log: path.join(cwd, "fake.log") };
}

function run(m, opts, lines) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [RUN, FAKE], {
      cwd: m.cwd, stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, FAKE_LOG: m.log, OTO_BROWSER_PROFILE_DIR: "Injected",
        SUP_OPTS: JSON.stringify({ home: m.home, hasToken: true, agentSlug: "dev", ...opts }) },
    });
    const answers = new Map();
    let rest = "";
    const want = new Set(lines.filter((l) => l.id !== undefined).map((l) => l.id));
    const timer = setTimeout(() => { child.kill(); reject(new Error(`timeout: ${[...want]}`)); }, 15000);
    child.stdout.on("data", (chunk) => {
      const parts = (rest + chunk).split("\n");
      rest = parts.pop();
      for (const p of parts.filter(Boolean)) {
        const msg = JSON.parse(p);
        answers.set(msg.id, msg);
        want.delete(msg.id);
      }
      if (!want.size) {
        clearTimeout(timer);
        child.stdin.end();
        child.kill();
        resolve(answers);
      }
    });
    for (const l of lines) child.stdin.write(JSON.stringify({ jsonrpc: "2.0", ...l }) + "\n");
  });
}

const calls = (m) => (fs.existsSync(m.log) ? fs.readFileSync(m.log, "utf8") : "")
  .split("\n").filter(Boolean).map((l) => JSON.parse(l));

(async () => {
  // The upload after a click that opened a chooser.
  {
    const m = machine();
    const answers = await run(m, {}, [
      { id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t" } } },
      { id: 2, method: "tools/list", params: {} },
      { id: 3, method: "tools/call", params: { name: "browser_click", arguments: { ref: "e2", element: "Upload" } } },
      { id: 4, method: "tools/call", params: { name: "browser_file_upload", arguments: { paths: ["invoice.pdf"] } } },
    ]);
    const tools = answers.get(2).result.tools;
    assert.ok(tools.find((t) => t.name === "browser_file_upload").inputSchema.properties.target);
    const reply = answers.get(4).result.content[0].text;
    assert.ok(reply.startsWith("Gave the page 1 file (invoice.pdf)."), reply);
    assert.ok(reply.includes('button "Upload" [ref=e2]'));
    // Nothing of the evaluate's echo or of the file's bytes reaches the agent.
    const bytes = Buffer.from("%PDF-1.7 the bytes").toString("base64");
    assert.ok(!JSON.stringify([...answers.values()]).includes(bytes));
    assert.ok(![...answers.keys()].some((k) => String(k).includes("__otodock_upload_")));
    const log = calls(m);
    // (The browser_close after them is the session's end, when stdin closes.)
    assert.deepStrictEqual(log.filter((e) => e.call && e.call !== "browser_close").map((e) => e.call),
      ["browser_click", "browser_file_upload", "browser_evaluate", "browser_snapshot"]);
    const evaluate = log.find((e) => e.call === "browser_evaluate").args;
    assert.strictEqual(evaluate.target, "e2");
    assert.ok(evaluate.function.includes(bytes));
    // No profile chosen: no hook, and the environment's own value never reaches it.
    assert.ok(!log[0].execArgv.includes("--require") && !log[0].hooked && log[0].profileDir === "");
  }
  // A path outside the session's folder: upstream's refusal, nothing called.
  {
    const m = machine();
    const answers = await run(m, {}, [
      { id: 1, method: "tools/call", params: { name: "browser_file_upload", arguments: { paths: ["/etc/hostname"] } } },
    ]);
    assert.match(answers.get(1).error.message, /^File access denied/);
    assert.ok(!calls(m).some((e) => e.call));
  }
  // Browser profile names a profile: the hook loads with its folder.
  {
    const m = machine({ profiles: { Default: { name: "Dimitris" }, "Profile 1": { name: "Work" } } });
    await run(m, { profileSetting: "work" }, [
      { id: 1, method: "tools/call", params: { name: "browser_navigate", arguments: { url: "https://example.test" } } },
    ]);
    const start = calls(m)[0];
    assert.ok(start.execArgv[0] === "--require" && start.execArgv[1].endsWith("profile_hook.js"));
    assert.ok(start.hooked);
    assert.strictEqual(start.profileDir, "Profile 1");
    assert.strictEqual(start.dirName, "Profile 1");
  }
  // A name no profile has, and a token the platform withheld: refused at once.
  {
    const m = machine({ profiles: { Default: { name: "Dimitris" } } });
    const named = await run(m, { profileSetting: "Home" }, [
      { id: 1, method: "tools/call", params: { name: "browser_navigate", arguments: { url: "https://example.test" } } },
    ]);
    assert.strictEqual(named.get(1).error.message,
      'No Chrome profile is named "Home". This browser has: Dimitris (Default). ' +
      "Set Browser profile in the machine's settings to one of them.");
    const withheld = await run(machine(), { tokenWithheld: true, hasToken: false }, [
      { id: 1, method: "tools/call", params: { name: "browser_navigate", arguments: { url: "https://example.test" } } },
    ]);
    assert.strictEqual(withheld.get(1).error.message,
      "This session does not run as this computer's owner, so it cannot use the owner's browser.");
    const lost = await run(machine(), { tokenExpected: true, hasToken: false }, [
      { id: 1, method: "tools/call", params: { name: "browser_navigate", arguments: { url: "https://example.test" } } },
    ]);
    assert.match(lost.get(1).error.message, /^The browser token was not handed to this session\./);
    assert.ok(!/new chat/.test(lost.get(1).error.message));
  }
  console.log("own_supervisor tests passed");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
