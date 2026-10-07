---
name: mcp-authoring
description: "Author an MCP package for OtoDock: the manifest.json fields and their rules, the package layout, what the installer refuses or drops, credentials and instances, skills bundled with an MCP, and the hand-over to an admin. Use when asked to wrap an MCP server (npm, PyPI, a git repository, a container image or a vendor-hosted endpoint, including one that signs people in itself) as an installable OtoDock MCP, to check a package with validate_mcp_package, or to prepare a catalog entry."
---

# MCP authoring

An OtoDock MCP is a folder with a `manifest.json` at its root. The manifest
says what to run, how to install it, what credentials it needs and which
skills ride with it; the platform does the rest (install, sandbox, env,
credentials, updates). You write the manifest and the files beside it, check
the package, then hand the zip to an admin who installs it from the
dashboard. You never install anything yourself.

## The flow

1. **Pick the source.** Wrap an existing server: `npm:<package>` (Node),
   `pypi:<package>` (Python), `git+<url>@<ref>#subdirectory=<dir>` (a
   repository), `docker:<label>` with a compose file and a prebuilt
   `server.image` (a container; the image repository is its identity), or
   `remote:<host>` (a vendor-hosted endpoint, nothing runs locally; one that
   signs people in itself the MCP way declares
   `credentials.oauth.authorization_server` and needs no OAuth app). Node and
   Python packages stay **unpinned** in the manifest: the platform installs
   the latest release and pins what it installed.
2. **Write the folder** in your workspace: `manifest.json`, `README.md`,
   optionally `icon.png` (256×256), `skills/<id>/SKILL.md`, and for a
   container the `docker-compose.yml` and its build context. Read
   `references/manifest.md` for every field and `references/examples.md`
   for five complete packages (the fourth a vendor-hosted server that
   signs people in itself, the fifth one that takes a header-style API key).
3. **Check it**: call `validate_mcp_package` with the folder's path (a
   workspace path such as `/workspace/my-mcp`). Fix every error; read the
   warnings (they are the catalog's conventions). Validate again until it
   passes. The check runs the installer's own rules without installing.
4. **Hand it over**: zip the folder (`manifest.json` at the zip root or one
   folder down; use python's `zipfile`, never include `node_modules`, `venv`,
   `.git` or a `.env`) and give the zip to an admin. The admin installs it
   from **Admin → MCP Servers → Install** (a fresh install is enabled
   platform-wide at once), and managers enable it per agent. A package meant for everyone goes to the community catalog
   instead: open a pull request against github.com/OtoDock/community-mcps
   and follow its CONTRIBUTING.md.

## Rules the installer enforces

- No `.env` anywhere, no `.git`, no `node_modules`, no `venv`: the archive is
  refused. Root lock files (`package-lock.json`, `yarn.lock`, `uv.toml`, …)
  are dropped, and so is a Python package's `requirements.txt`: the platform
  installs from `server.source`, wheels only, with a scrubbed environment.
- `category` is `"community"`. The name is lowercase letters, digits,
  dashes and underscores; it is the key everything refers to, so never rename
  a shipped package.
- Every `skills[].file` and a container's `server.docker_compose` is a
  plain relative path to a regular file inside the folder. No `..`, no
  absolute path, no symlink.
- Never declare `PROXY_URL`, `PROXY_API_KEY` or any `OTO_*` variable: the
  platform injects them. Never reference the master key
  (`${platform.api_key}`, `${proxy_api_key}`): it is refused.
- Secrets live in `credentials` (per user or shared) or `instances` (admin
  managed); the platform stores them encrypted and delivers them per
  session. See `references/package-rules.md`.

## Writing the server side

- A stdio MCP reads its config from env. Paths come as sandbox paths
  (`/workspace/...`, `/users/<name>/...`): declare path-bearing variables
  with `path_env` and tool arguments that take paths with `tool_arg_paths`,
  and never re-check them against your own allowlist.
- Read `OTO_SESSION_ID`, `OTO_AGENT_NAME`, `OTO_SCOPE` and the `OTO_CAN_*`
  flags when the behaviour depends on the session; compare no role words.
- A container MCP serves streamable HTTP at `/mcp/` on `server.port` and
  uses `url_template: "http://${docker_mcp_host}:${port}"`; ship a prebuilt
  `server.image` so the containerised deployment can pull it.
- A tool that bills an upstream API declares its prices in `costs`.

## Updating a shipped package

Installs converge to the catalog: an edit of the manifest reaches them at
the next update, a newer package release is installed within
`server.version_constraint`. A change of the source itself (another
package, image repository, repository or host) is never applied on its own:
declare it in `replaces` (the previous source, and the credential keys the
new source renames) so the admin sees a declared change, lists the renames
and switches with the install's settings and credentials kept. Without the
declaration the change is shown as unexplained and the admin is warned.
