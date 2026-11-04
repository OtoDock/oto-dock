"use strict";
// Loaded into the inner @playwright/mcp with `node --require` in own-browser
// mode (index.js, only when a profile folder was chosen): the one spawn that
// opens the extension's connect page gets
// --profile-directory=<OTO_BROWSER_PROFILE_DIR>, so a running Chrome opens it
// in the profile that has the extension instead of its last-used one.
// Upstream 0.0.76 spawns the browser with shell:false and no profile flag,
// and Node refuses to spawn a .cmd or .bat without a shell on Windows
// (EINVAL), so a launcher behind PLAYWRIGHT_MCP_EXECUTABLE_PATH cannot carry
// the flag there. Upstream reads child_process.spawn at call time, so the
// wrapped function is the one it calls. A later upstream that passes the
// flag itself (PLAYWRIGHT_MCP_PROFILE_DIR_NAME) is left alone.

const childProcess = require("child_process");

const CONNECT_PAGE = "chrome-extension://mmlmfjhmonkocbjadbfplnigmagldckm/connect.html";

function withProfile(args, folder) {
  if (!folder || !Array.isArray(args)) return args;
  const at = args.findIndex((a) => typeof a === "string" && a.startsWith(CONNECT_PAGE));
  if (at < 0 || args.some((a) => typeof a === "string" && a.startsWith("--profile-directory=")))
    return args;
  return [...args.slice(0, at), `--profile-directory=${folder}`, ...args.slice(at)];
}

function install(folder) {
  if (!folder) return;
  const original = childProcess.spawn;
  childProcess.spawn = function spawn(file, args, options) {
    return original.call(this, file, withProfile(args, folder), options);
  };
}

install(process.env.OTO_BROWSER_PROFILE_DIR || "");

module.exports = { withProfile, CONNECT_PAGE };
