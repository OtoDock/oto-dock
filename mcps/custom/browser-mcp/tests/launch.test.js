"use strict";
// How the browser starts detached — run with: node tests/launch.test.js
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { detachedLaunchArgv, launchFailureLine } = require("../index.js");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const ARGS = ["--user-data-dir=/Users/p/x", "--remote-debugging-port=0"];

// --- a Mac starts an app bundle through LaunchServices ---------------------
assert.deepStrictEqual(detachedLaunchArgv(CHROME, ARGS, { platform: "darwin", logPath: "/tmp/l.log" }), {
  command: "/usr/bin/open",
  argv: ["-n", "-a", "/Applications/Google Chrome.app", "--stdout", "/tmp/l.log", "--stderr", "/tmp/l.log",
         "--args", ...ARGS],
});
// The person's own browser: no second instance, no log, no arguments.
assert.deepStrictEqual(detachedLaunchArgv(CHROME, [], { platform: "darwin", newInstance: false }), {
  command: "/usr/bin/open",
  argv: ["-a", "/Applications/Google Chrome.app"],
});
// A browser outside a bundle on a Mac, and every Linux browser, use the shell.
for (const [exe, platform] of [["/opt/homebrew/bin/chromium", "darwin"], ["/usr/bin/google-chrome", "linux"]]) {
  assert.deepStrictEqual(detachedLaunchArgv(exe, ARGS, { platform, logPath: "/tmp/l.log" }), {
    command: "/bin/sh",
    argv: ["-c", '"$0" "$@" &', exe, ...ARGS],
  });
}

// --- the launch log's cause -------------------------------------------------
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "launch-test-"));
const log = path.join(dir, "launch.log");
assert.strictEqual(launchFailureLine(log), ""); // no log yet
fs.writeFileSync(log, "[1027/062600.000:ERROR:gpu_init.cc:1] harmless\n");
assert.strictEqual(launchFailureLine(log), ""); // ERROR lines are normal
fs.writeFileSync(log, "[1027/062637.012480:FATAL:base/path_service.cc:264] Failed to get the path for 1001\n" +
                      "Trace/BPT trap: 5\n");
assert.strictEqual(launchFailureLine(log),
  "[1027/062637.012480:FATAL:base/path_service.cc:264] Failed to get the path for 1001");
// macOS's own refusal, when the launch ran outside the desktop session.
fs.writeFileSync(log, "LSOpenURLsWithRole() failed with error -10810 for the file /Applications/Google Chrome.app.\n");
assert.strictEqual(launchFailureLine(log),
  "LSOpenURLsWithRole() failed with error -10810 for the file /Applications/Google Chrome.app.");
fs.writeFileSync(log, "Unable to find application named 'Google Chrome'\n");
assert.strictEqual(launchFailureLine(log), "Unable to find application named 'Google Chrome'");
fs.writeFileSync(log, "x:FATAL:" + "y".repeat(400) + "\n");
assert.strictEqual(launchFailureLine(log).length, 300);
fs.rmSync(dir, { recursive: true, force: true });

console.log("launch tests: ok");
