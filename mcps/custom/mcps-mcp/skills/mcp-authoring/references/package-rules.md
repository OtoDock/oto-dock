# What the installer does with a package

The admin installer and the catalog install run one pipeline; the check tool
(`validate_mcp_package`) applies the same rules without installing.

## Refused (the whole archive is rejected)

- A `.env` file at any depth. Environment values are declared in the
  manifest (`env`, `agent_env`); secrets go to `credentials` or `instances`.
- A `.git` entry, a `node_modules/` or a `venv/` at any depth: the platform
  installs dependencies itself.
- A `skills[].file` or `server.docker_compose` that is not a plain relative
  path to a regular file inside the folder (no `..`, no absolute path, no
  symlink).
- A `category` other than `community`, a name that is not a slug, a `source`
  outside the five accepted forms, a `replaces` entry that is not
  `{"source": "...", "credentials": {"OLD": "NEW"}}` with plain identifiers.
- A name that is already a platform-bundled MCP or an installed skill
  package, or a skill id another installed MCP already provides.
- A `credentials.oauth.authorization_server` block with `flows` other than
  `["authorization_code_pkce"]`, without `bearer_required`, with a
  `server.url_template` that is not https or whose host is templated or
  absent from `proposed_hosts`, with a non-https `userinfo_url`, with one
  of `authorize_params`, `env_injection`, `mcp_env_injection`,
  `git_credential_helper`, `device_authorization_url` or
  `app_credential_variants` beside it, with a `registration` other than
  `"dynamic"`, a non-boolean `confidential` or `accepts_app_tokens`, an `issuer` that is not https
  or carries a query or fragment, or a malformed `client_name`, `scopes` or
  `identity`; on a provider id the platform implements in Python (google,
  slack, microsoft, zoom, facebook); or on a provider id an MCP installed
  on this platform already uses the other way (judged by the install and
  by `validate_mcp_package` alike; and a manifest without the block on a
  provider id an installed MCP uses with it).
- A `credentials.api_key_header` block that breaks its rules
  (references/manifest.md: a streamable-HTTP transport, a literal
  `url_template` whose host matches `proposed_hosts`, a `name` the platform
  does not own, a declared `value_from`, never beside `bearer_required`), and
  an `audience` other than `owner`, `editor` or `workspace`.
- A `credentials.oauth.bearer_required` MCP whose `server.transport` is `sse`
  (streamable HTTP only).
- An update whose source identity differs from the installed one: the
  installer refuses it. A catalog entry's move is switched by the admin from
  the MCP Servers page (a `replaces` declaration shows it as declared); a
  package installed from a zip has no catalog entry and no switch, so the
  admin uninstalls it and installs the new archive.

## Dropped (the install proceeds without them)

- Root `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`,
  `pnpm-lock.yaml`, `.npmrc`, `uv.toml`, `pip.conf`: the platform resolves
  its own lock and ships it to paired machines.
- A Python package's root `requirements.txt`: the package installs from
  `server.source`, wheels only. A dependency with no wheel for some
  machines is named in `server.source_build`: it still installs from a
  wheel where one exists and is built from source only where none fits.

## How each source installs

| Source | What happens |
|---|---|
| `npm:<package>` | `package.json` is written for the package, `npm install --omit=dev --ignore-scripts` runs (no lifecycle scripts, ever), shipped `patches/*.patch` are applied with git, the installed version is pinned into the local manifest. |
| `pypi:<package>` | A fresh `venv/` on every install, `uv pip install --only-binary=:all:` (wheels only) within `version_constraint`; when that fails naming a `source_build` package, another try builds the named package from source (once per listed package); each package-manager step runs at most 300 s, so a source build that takes longer fails the install; the version pinned into the local manifest. |
| `git+<url>@<ref>#subdirectory=<dir>` | A fresh venv, the repository installed at the ref (a build is allowed here, and only an admin's explicit install runs one). |
| `docker:<label>` + compose | The folder is copied, the compose is rewritten for the containerised deployment, `server.image` is pulled (bare metal builds when no image is published), the container is started. |
| `remote:<host>` | The folder copy is the install; sessions connect to `url_template`. |

Every dependency install runs under a scrubbed environment: the package
manager sees no platform secret, no compiler variable and no git credential
helper.

## The conventions the catalog asks for (warnings)

- `README.md` beside the manifest (shown in the install dialog): what the
  MCP does, the credentials it needs, operator notes.
- `icon.png`: a 256×256 PNG of at most 256 KB, the upstream project's official mark, unaltered, only
  where its brand terms permit the use.
- `author` and `author_url`: the project whose server code the package
  wraps.
- A container's `url_template` is `http://${docker_mcp_host}:${port}` and it
  ships a prebuilt `server.image` whose tag equals `version`.
- No `OTO_*`, `PROXY_URL` or `PROXY_API_KEY` in `env` or `agent_env`.
- No `${credential.*}` token in `agent_context`: no credential value enters
  a prompt, so it renders empty and a `requires` on it skips the block.

## The hand-over

```python
import zipfile, pathlib
folder = pathlib.Path("/workspace/my-mcp")
with zipfile.ZipFile("/workspace/my-mcp.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not any(part in {"node_modules", "venv", ".git"} for part in p.parts):
            zf.write(p, p.relative_to(folder.parent))
```

Give the zip to an admin. The admin uploads it at Admin → MCP Servers →
Install (a fresh install is enabled platform-wide at once), and a manager
enables it on each agent (Agent Settings → MCPs). An update is the same upload with the same
name: the installer keeps the MCP's settings, credentials and assignments
and replaces the files.
