"use strict";
// Runs the extension supervisor over a fake inner MCP (argv[2]) with the
// options the test hands it in SUP_OPTS.
const { runExtensionSupervisor } = require("../../index.js");
runExtensionSupervisor(process.argv[2], { browser: "chrome", executablePath: "/bin/true" },
  JSON.parse(process.env.SUP_OPTS));
