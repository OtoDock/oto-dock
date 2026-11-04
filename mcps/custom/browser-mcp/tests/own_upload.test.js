"use strict";
// Own-browser mode's upload: upstream's path rule, the size cap, the page
// function's status read from the result alone (never from the code echo),
// the answers, and the schema the agent sees. The page function itself runs
// in a real Chromium in the lane's walk. Run with: node tests/own_upload.test.js
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const up = require("../own_upload.js");

const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "own-upload-"));
fs.writeFileSync(path.join(cwd, "invoice.pdf"), "%PDF-1.7 tiny");
fs.mkdirSync(path.join(cwd, ".playwright-mcp"));
fs.writeFileSync(path.join(cwd, ".playwright-mcp", "shot.png"), "png");
const outside = fs.mkdtempSync(path.join(os.tmpdir(), "own-upload-outside-"));
fs.writeFileSync(path.join(outside, "secret.txt"), "no");

// Inside the session's folder (relative or absolute) and the output folder: read.
{
  const ok = up.resolveUploadPaths(["invoice.pdf", path.join(cwd, ".playwright-mcp", "shot.png")], { cwd, env: {} });
  assert.deepStrictEqual(ok.files.map((f) => f.name), ["invoice.pdf", "shot.png"]);
}
// Outside both: upstream's own refusal.
{
  const no = up.resolveUploadPaths([path.join(outside, "secret.txt")], { cwd, env: {} });
  assert.match(no.error, /^File access denied: .*secret\.txt is outside allowed roots\. Allowed roots: /);
  const free = up.resolveUploadPaths([path.join(outside, "secret.txt")],
    { cwd, env: { PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS: "true" } });
  assert.strictEqual(free.files.length, 1);
  assert.match(up.resolveUploadPaths(["../" + path.basename(outside) + "/secret.txt"], { cwd, env: {} }).error,
    /^File access denied/);
}
// A folder, a missing file, and the 10 MB cap over the whole upload.
{
  assert.match(up.resolveUploadPaths([".playwright-mcp"], { cwd, env: {} }).error, /is not a file\.$/);
  assert.match(up.resolveUploadPaths(["gone.pdf"], { cwd, env: {} }).error, /^There is no file at /);
  const big = path.join(cwd, "big.bin");
  fs.writeFileSync(big, Buffer.alloc(up.UPLOAD_MAX_BYTES - 4));
  assert.strictEqual(up.resolveUploadPaths(["big.bin"], { cwd, env: {} }).files.length, 1);
  assert.match(up.resolveUploadPaths(["big.bin", "invoice.pdf"], { cwd, env: {} }).error,
    /^The files are larger than 10 MB/);
}

// The payload carries the bytes and the type; the status is read from the
// result's text, never from the echoed code (which holds every status word).
{
  const files = up.resolveUploadPaths(["invoice.pdf"], { cwd, env: {} }).files;
  const [p] = up.payloadsOf(files);
  assert.strictEqual(p.mime, "application/pdf");
  assert.strictEqual(Buffer.from(p.b64, "base64").toString(), "%PDF-1.7 tiny");
  const source = up.pageFunction([p]);
  assert.strictEqual(up.statusOf(source), null);
  const echoed = `### Result\n"otodock-upload:field"\n### Ran Playwright code\nawait page.evaluate('${source}');`;
  assert.strictEqual(up.statusOf(echoed), "field");
  assert.strictEqual(up.statusOf(`### Ran Playwright code\n${source}`), null);
  assert.deepStrictEqual(up.answerFor("field", files), { text: "Gave the page 1 file (invoice.pdf)." });
  assert.match(up.answerFor("refused", files).error, /^The page did not take the file\./);
  assert.strictEqual(up.answerFor("single", files).error, "The page's file field takes one file.");
  assert.match(up.answerFor("nofield", files).error, /^Name the file field or the drop area/);
  assert.match(up.answerFor(null, files).error, /^The page gave no answer/);
}

// The agent's tool gains the element it may name, in own mode only.
{
  const list = { result: { tools: [
    { name: "browser_file_upload", inputSchema: { type: "object", properties: { paths: { type: "array" } } } },
    { name: "browser_click", inputSchema: { type: "object", properties: {} } },
  ] } };
  const out = up.patchUploadSchema(list).result.tools;
  assert.deepStrictEqual(Object.keys(out[0].inputSchema.properties), ["paths", "target", "element"]);
  assert.deepStrictEqual(out[1].inputSchema.properties, {});
  assert.deepStrictEqual(up.patchUploadSchema({ error: { message: "x" } }), { error: { message: "x" } });
}

console.log("own_upload tests passed");
