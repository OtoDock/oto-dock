"use strict";
// ref→target aliasing on forwarded tools/call lines — run with:
//   node tests/ref_alias.test.js
// Snapshots render [ref=eN] and the skill teaches `ref`; upstream
// @playwright/mcp names the param `target`. The wrapper rewrites the line so
// the model's first call lands (2026-07-19).
const assert = require("assert");
const { classifyClientLine } = require("../index.js");

const owns = () => false;

function call(args, name = "browser_click") {
  return JSON.stringify({
    jsonrpc: "2.0", id: 7, method: "tools/call",
    params: { name, arguments: args },
  });
}

// ref → target on a plain click
{
  const r = classifyClientLine(call({ element: "btn", ref: "e12" }), owns);
  assert.strictEqual(r.kind, "forward");
  const out = JSON.parse(r.line);
  assert.strictEqual(out.params.arguments.target, "e12");
  assert.strictEqual(out.params.arguments.ref, undefined);
  assert.strictEqual(out.id, 7);
}

// target already present → untouched (no line rewrite)
{
  const r = classifyClientLine(call({ target: "e1", ref: "e2" }), owns);
  assert.strictEqual(r.kind, "forward");
  assert.strictEqual(r.line, undefined);
}

// drag start/end refs
{
  const r = classifyClientLine(
    call({ startRef: "e1", endRef: "e2" }, "browser_drag"), owns);
  const out = JSON.parse(r.line);
  assert.strictEqual(out.params.arguments.startTarget, "e1");
  assert.strictEqual(out.params.arguments.endTarget, "e2");
}

// fill_form fields[]
{
  const r = classifyClientLine(
    call({ fields: [{ name: "a", ref: "e3", value: "x" }, { name: "b", target: "e4" }] },
         "browser_fill_form"), owns);
  const out = JSON.parse(r.line);
  assert.strictEqual(out.params.arguments.fields[0].target, "e3");
  assert.strictEqual(out.params.arguments.fields[0].ref, undefined);
  assert.strictEqual(out.params.arguments.fields[1].target, "e4");
}

// non-tools/call lines untouched
{
  const r = classifyClientLine(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }), owns);
  assert.strictEqual(r.kind, "tools-list");
}

// unparseable line still forwards
{
  const r = classifyClientLine("not json", owns);
  assert.strictEqual(r.kind, "forward");
  assert.strictEqual(r.line, undefined);
}

console.log("ref_alias.test.js: all assertions passed");
