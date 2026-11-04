"use strict";
// A stand-in for upstream's `cli.js --extension` in the supervisor test: it
// logs how it was started and every call it got, and answers like upstream.
const fs = require("fs");
const log = (entry) => fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify(entry) + "\n");
log({ argv: process.argv.slice(2), execArgv: process.execArgv,
  hooked: require("child_process").spawn.toString().includes("withProfile"),
  profileDir: process.env.OTO_BROWSER_PROFILE_DIR || "",
  dirName: process.env.PLAYWRIGHT_MCP_PROFILE_DIR_NAME || "" });
const text = (id, t, isError) => JSON.stringify({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: t }], ...(isError ? { isError: true } : {}) } });
let rest = "";
process.stdin.on("data", (chunk) => {
  const lines = (rest + chunk).split("\n");
  rest = lines.pop();
  for (const line of lines.filter(Boolean)) {
    const msg = JSON.parse(line);
    if (msg.method === "initialize") {
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "fake" } } }) + "\n");
    } else if (msg.method === "tools/list") {
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: { tools: [
        { name: "browser_file_upload", inputSchema: { type: "object", properties: { paths: { type: "array" } } } }] } }) + "\n");
    } else if (msg.method === "tools/call") {
      const { name, arguments: args } = msg.params;
      log({ call: name, args });
      if (name === "browser_file_upload")
        process.stdout.write(text(msg.id, 'Error: The tool "browser_file_upload" can only be used when there is related modal state present.', true) + "\n");
      else if (name === "browser_evaluate")
        process.stdout.write(text(msg.id, `### Result\n"otodock-upload:field"\n\n### Ran Playwright code\n\`\`\`js\nawait page.evaluate(${JSON.stringify(args.function)});\n\`\`\``) + "\n");
      else if (name === "browser_snapshot")
        process.stdout.write(text(msg.id, '### Page\n- Page URL: https://example.test/\n- button "Upload" [ref=e2]') + "\n");
      else process.stdout.write(text(msg.id, `did ${name}`) + "\n");
    }
  }
});
