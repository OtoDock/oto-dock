"use strict";
// Own-browser mode's file upload. Upstream sets a file input with
// DOM.setFileInputFiles and the file's PATH (its extension mode connects as
// a browser on the same machine), which Chrome refuses to an extension's
// debugger ("Not allowed"). A file built in the page from its bytes works
// (upstream's own browser_drop does that), so in own mode the supervisor
// answers browser_file_upload itself: the files read under upstream's path
// rule, a pending chooser cleared, the files built in the page and handed to
// the chooser's field, or dropped on the element when it has none.

const fs = require("fs");
const os = require("os");
const path = require("path");

// The bytes travel as base64 inside one page script through the extension's
// debugger connection (about 13.4 MB on the wire for 10 MB), and the page
// holds the text, the bytes and the File at once: 10 MB keeps that well
// inside what Chrome's extension messaging and a page's memory take.
const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;

const STATUS = "otodock-upload";
const STATUS_RE = new RegExp(`"${STATUS}:(\\w+)"`);

const MIME = {
  ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml", ".heic": "image/heic",
  ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".html": "text/html",
  ".json": "application/json", ".xml": "application/xml", ".zip": "application/zip",
  ".doc": "application/msword", ".xls": "application/vnd.ms-excel",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
  ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
};

function isPathInside(root, file) {
  const rel = path.relative(root, file);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

function isWritable(dir) {
  try {
    fs.accessSync(dir, fs.constants.W_OK);
    return true;
  } catch (_) {
    return false;
  }
}

// Upstream's output folder (its outputDir): PLAYWRIGHT_MCP_OUTPUT_DIR, else
// `.playwright-mcp` in the working folder, in the temp folder when that is
// the file system's root or cannot be written.
function outputDir(cwd, env) {
  if (env.PLAYWRIGHT_MCP_OUTPUT_DIR) return path.resolve(env.PLAYWRIGHT_MCP_OUTPUT_DIR);
  if (path.parse(cwd).root === cwd || !isWritable(cwd)) return path.join(os.tmpdir(), ".playwright-mcp");
  return path.join(cwd, ".playwright-mcp");
}

// The files of one upload under upstream's rule for a client file (its
// workspaceFile and checkFile): inside the client's first root (the
// session's working folder) or the output folder, unless
// PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS. Answers `{files}` or
// `{error}`.
function resolveUploadPaths(paths, { cwd, env = process.env }) {
  const unrestricted = /^(1|true|yes|on)$/i.test(String(env.PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS || ""));
  const out = outputDir(cwd, env);
  const files = [];
  let total = 0;
  for (const given of paths) {
    const resolved = path.resolve(cwd, String(given));
    if (!unrestricted && !isPathInside(cwd, resolved) && !isPathInside(out, resolved))
      return { error: `File access denied: ${resolved} is outside allowed roots. Allowed roots: ${out}, ${cwd}` };
    let stat;
    try {
      stat = fs.statSync(resolved);
    } catch (_) {
      return { error: `There is no file at ${resolved}.` };
    }
    if (!stat.isFile()) return { error: `${resolved} is not a file.` };
    total += stat.size;
    if (total > UPLOAD_MAX_BYTES)
      return {
        error:
          "The files are larger than 10 MB, which your own browser takes through OtoDock. " +
          "Hand them to the page by hand, or use the dedicated browser.",
      };
    files.push({ path: resolved, name: path.basename(resolved), size: stat.size, mtime: stat.mtimeMs });
  }
  return { files };
}

function payloadsOf(files) {
  return files.map((f) => ({
    name: f.name,
    mime: MIME[path.extname(f.name).toLowerCase()] || "application/octet-stream",
    mtime: Math.round(f.mtime),
    b64: fs.readFileSync(f.path).toString("base64"),
  }));
}

// The page function browser_evaluate runs, with the element of `target`
// when there is one. It answers a status the supervisor reads from the
// evaluation result alone: the token is joined at run time, so the code
// upstream echoes back never matches it.
function pageFunction(payloads) {
  return `async (element) => {
  const payloads = ${JSON.stringify(payloads)};
  const said = (s) => ${JSON.stringify(STATUS)} + ":" + s;
  const files = payloads.map((p) => {
    const bin = atob(p.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], p.name, { type: p.mime, lastModified: p.mtime });
  });
  const isField = (n) => !!n && n.tagName === "INPUT" && String(n.type).toLowerCase() === "file";
  const fieldsIn = (root) => {
    const found = [];
    const walk = (r) => {
      for (const n of r.querySelectorAll("*")) {
        if (isField(n)) found.push(n);
        if (n.shadowRoot) walk(n.shadowRoot);
      }
    };
    if (isField(root)) found.push(root);
    walk(root);
    return found;
  };
  let field = null;
  if (element) {
    if (isField(element)) field = element;
    else if (element.control && isField(element.control)) field = element.control;
    for (let up = element, i = 0; !field && up && i < 7; up = up.parentElement, i++) {
      const near = fieldsIn(up);
      if (near.length === 1) field = near[0];
      if (near.length > 1) break;
    }
  }
  if (!field) {
    const all = fieldsIn(document);
    if (all.length === 1) field = all[0];
  }
  const transfer = () => {
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    return dt;
  };
  if (field) {
    if (files.length > 1 && !field.multiple) return said("single");
    field.files = transfer().files;
    field.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
    return said("field");
  }
  if (!element) return said("nofield");
  const dt = transfer();
  const drag = (type) => new DragEvent(type, { bubbles: true, cancelable: true, composed: true, dataTransfer: dt });
  element.dispatchEvent(drag("dragenter"));
  const over = drag("dragover");
  element.dispatchEvent(over);
  if (!over.defaultPrevented) {
    element.dispatchEvent(drag("dragleave"));
    return said("refused");
  }
  element.dispatchEvent(drag("drop"));
  return said("dropped");
}`;
}

// The page function's status, from the evaluation result's text only.
function statusOf(responseText) {
  const m = STATUS_RE.exec(String(responseText || ""));
  return m ? m[1] : null;
}

function answerFor(status, files) {
  const names = files.map((f) => f.name).join(", ");
  const n = files.length;
  switch (status) {
    case "field":
    case "dropped":
      return { text: `Gave the page ${n} file${n === 1 ? "" : "s"} (${names}).` };
    case "single":
      return { error: "The page's file field takes one file." };
    case "refused":
      return { error: "The page did not take the file. Hand it to the page by hand, or use the dedicated browser." };
    case "nofield":
      return { error: "Name the file field or the drop area with its ref from the snapshot." };
    default:
      return { error: "The page gave no answer to the upload. Take a snapshot and try again." };
  }
}

// The upload tool's schema in own mode gains the element it targets.
function patchUploadSchema(listResult) {
  const tools = listResult && listResult.result && listResult.result.tools;
  if (!Array.isArray(tools)) return listResult;
  for (const tool of tools) {
    if (tool && tool.name === "browser_file_upload" && tool.inputSchema && tool.inputSchema.properties) {
      tool.inputSchema.properties.target = {
        type: "string",
        description:
          "The file field or the drop area, from the snapshot. Leave it out after a click that " +
          "opened a file chooser.",
      };
      tool.inputSchema.properties.element = {
        type: "string",
        description: "A short description of that element, for the record.",
      };
    }
  }
  return listResult;
}

module.exports = {
  UPLOAD_MAX_BYTES, resolveUploadPaths, payloadsOf, pageFunction, statusOf, answerFor,
  patchUploadSchema, outputDir,
};
