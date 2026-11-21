# The manifest, field by field

Every field of `manifest.json` the platform reads. The full reference with
the platform-side details is on docs.otodock.io (the MCP framework pages)
and in the catalog's CONTRIBUTING.md at github.com/OtoDock/community-mcps.
The machine-readable contract is the JSON Schema `mcps/manifest.schema.json`
in github.com/OtoDock/oto-dock (its `$id` names the release it belongs to);
`validate_mcp_package` checks a package against it and reports the platform
floor the fields imply.

## Top level

| Field | Required | What it is |
|---|---|---|
| `name` | yes | The canonical slug: lowercase letters, digits, dashes, underscores. The key for assignments, config and tools; keep it equal to the folder name; never rename a shipped package. |
| `label` | yes | The display name. |
| `description` | yes | One line for the catalog card and the agent's tool list. |
| `version` | yes | `""` for an npm or PyPI package (the platform pins the installed release); a version for a container (equal to the image tag) and for a git source (the release the pinned commit is; it moves with every new commit). |
| `category` | yes | `"community"`. |
| `author` | catalog | Who maintains the server code the package wraps, written as the project shows it. |
| `author_url` | catalog | That code's repository (`https://…`), or the vendor's page for a hosted MCP. |
| `server` | yes | The runtime block below. |
| `server_name` | no | The tool namespace prefix when it must differ from `name`. |
| `credentials` | no | `type`: `none`, `per_user` (each person enters their own, in Settings) or `infra` (an admin enters them once); `fields` for the inputs (`{key, label, input_type, required}`: a field is required unless it says `"required": false`, and an account without a required one keeps the MCP out of its sessions, so add a new field as optional); `oauth` for an OAuth provider; `service_account: true` lets a manager lend an account to agent-scope sessions. |
| `instances` | no | Admin-managed instances (a server URL, a token) delivered as env (`delivery: "env"`); pair with `assignment_mode: "explicit"`. |
| `config` | no | Admin-managed key/value settings shown on the MCP's page; `user_overridable: true` lets each person override one. A row's `default` applies until an admin saves another value (an empty default sets nothing), so a safe default belongs here, not only in the description. |
| `env` | no | Static variables for every launch. |
| `agent_env` | no | Per-session variables with `${session.*}` tokens. |
| `path_env` | no | Path-bearing variables by role (`workspace`, `user_root`, `shared_workspace`, `config`, `knowledge_dir`). The platform fills them with the right sandbox path per session. |
| `tool_arg_paths` | no | Which tool arguments are paths (`{"tool": {"arg.path": {"mode": "read"}}}`), so the platform translates and admits them before your server sees them. |
| `skills` | no | Skills bundled with the MCP: `{id, file, description, loading, default_exclude_from, audience}`. `loading` is `on_demand` (default) or `always`; `audience` (`owner`, `editor`, `workspace`) limits a skill to a tier. |
| `agent_context` | no | Prompt blocks with `${account.*}`, `${agent.*}`, `${trigger.*}` tokens. |
| `outputs` | no | Files the server writes into a shared folder that the platform moves into the session's workspace. |
| `result_files` | no | Tools whose results the person should keep: `{"tools": ["maps_static_map"], "subdir": "maps"}`. Each picture (an MCP `image` block: png, jpeg, webp or gif) and each embedded `resource` blob such a tool returns is saved in the session's workspace under `subdir` and shown in the chat with the result's text as the caption (its first link opens from the picture). Exact tool names (up to 64, none that `outputs` already moves), one plain folder name; 15 MB a file, 8 files and 30 MB a result. Never a tool that returns a picture on every call for the model's own use (a browser screenshot). Set `platform_min_version: "1.8.0"`. On Claude only pictures are kept (the CLI saves other blobs itself). |
| `costs` | no | Per-tool prices (`currency`, `provider`, ordered `rules`). |
| `permissions` | no | Per-tool permission tiers (`open`, `standard`, `sensitive`; `critical` is kept for the platform's own tools and runs as `sensitive` in any other MCP). A tool no rule names takes its tier from its own MCP annotations. `readOnlyHint: true` makes it `open`. `destructiveHint: false` together with `openWorldHint: false` makes it `standard`. Any other annotation makes it `sensitive`. A tool that sends no annotations, or whose list the platform has not read yet, takes `default_tier`. Set the annotations truthfully on every tool, and keep `permissions` for the tools whose tier should differ. |
| `assignment_mode` | no | `auto` (managers enable it per agent) or `explicit` (an admin must configure an instance first). |
| `exclude_from` | no | Session kinds that never load it: `phone`, `task`, `terminal`, `meeting`, `external`, and `shared_chat` (a session that is not the person's own chat: a Shared-only agent's chat, a task chat reopened in the dashboard, a delegated worker's chat, a chat another person drives, an unattended wake, nobody driving). |
| `audience` | no | The tier of the agent this whole MCP is for: `owner`, `editor` or `workspace` (empty = everyone, the default). In a session for a person below that tier the MCP is left out — of the session, its tools and its sandbox network opening; a session the agent runs as itself is always in, except a phone caller's (a caller who is not a platform user counts as a viewer). The whole-MCP version of `skills[].audience`. |
| `network_targets` | no | The internal hosts a homelab MCP dials, read from a config key, an instance field or a credential; the admin's toggle opens the sandbox to exactly those. |
| `system_requirements` | no | OS packages and a `node_min` the installer checks before installing. |
| `replaces` | no | Sources this entry supersedes, with credential key renames: `[{"source": "<previous source>", "credentials": {"OLD": "NEW"}}]`. Information for the admin's switch, never an authorization. |
| `tool_filter` | no | For a container whose server takes a tool-filter regex: `{arg_name, env_var_name, regex_env_var_name}`. The platform writes the admin's regex bare to `regex_env_var_name` (default `ENABLED_TOOLS_REGEX`) and the flag `arg_name '<regex>'` to `env_var_name` (default `ENABLED_TOOLS_FLAG`); read the bare one (`env_file: .env`, then `"$$ENABLED_TOOLS_REGEX"` quoted in a `sh -c` entrypoint, or the server's own env). |
| `patched`, `patch_note` | no | `true` plus one line when `patches/*.patch` are shipped and applied after `npm install`. |
| `deprecated`, `platform_min_version` | no | Catalog metadata. `platform_min_version` is the oldest platform the entry works on: 1.7.1 when it uses `audience`, a skill's `audience`, `replaces`, `credentials.api_key_header` or `oauth.authorization_server`; 1.8.0 for `result_files`, `tool_filter.regex_env_var_name` or a PyPI source with extras, and 2.0.0 for `credentials.exit`. `validate_mcp_package` computes it from the fields and warns when the declared one is lower. |

Fields a community package never sets: `requires_capability`, `hosted`,
`server.proxy_callbacks` (platform features). An app connector sets
`placement`, `requires_display`, `device_capability` and `companion_app`:
see "App connectors" below.

## `server`

| Field | Required | What it is |
|---|---|---|
| `runtime` | all but remote | `python`, `node` or `docker`. A remote (vendor-hosted) MCP declares none. |
| `transport` | yes | `stdio` for python and node; `http` for a container (streamable HTTP at `/mcp/`); `streamable_http` for a remote MCP. |
| `command`, `args` | stdio | What to launch, relative to the folder, `${mcp_dir}` allowed in args (`node`, `["${mcp_dir}/node_modules/<pkg>/dist/index.js"]`; `venv/bin/python`, `["-m", "<module>"]`). |
| `source` | yes | `npm:<package>`, `pypi:<package>` (with extras: `pypi:<package>[<extra>,<extra>]`, platform 1.8.0 and later), `git+<url>@<commit>#subdirectory=<dir>`, `docker:<label>` or `remote:<host>`. A catalog git source pins the full 40-character commit, never a tag or a branch (they can move upstream); from 1.8.0 the weekly update builds a new commit the catalog pins. An admin's own ZIP may pin a tag. |
| `version_constraint` | no | A PEP 440 range (`">=2,<3"`) that bounds the automatic updates of an npm or PyPI package. |
| `source_build` | no | For a PyPI package: the distributions the installer may build from source when the machine has no wheel for them (every package installs from wheels first; a listed one is built only when that resolve fails on it). |
| `tool_timeout_sec` | no | Seconds one tool call may take before the engine fails the call, 1 to 3600. Without it Codex allows 60 seconds and Claude Code 5 minutes of silence over HTTP. Set it on a server whose tools run for minutes: a media generation, a document conversion, an install. A platform that does not read the key ignores it, so it needs no `platform_min_version`. |
| `port`, `url_template`, `health_endpoint`, `docker_compose`, `image`, `service_name` | docker | The port, `http://${docker_mcp_host}:${port}`, an optional health path, the compose file in the folder, the prebuilt image (required for the containerised deployment), an optional service-DNS name. |
| `url_template` | remote | The endpoint URL (`https://mcp.vendor.com/mcp`); its host is the MCP's identity. |

## Credentials in short

- `type: "none"` with an `instances` block: the common shape for an
  integration an admin configures (a server URL and a token per instance,
  assigned to agents).
- `type: "per_user"` with `fields`: each person enters their own values in
  Settings; multiple accounts per person are supported.
- `type: "infra"` with `fields`: one set of values for the install, entered
  by an admin on the MCP's page.
- `oauth`: `provider_id`, `flows`, the vendor's URLs, `app_credential` and
  its fields, `services` with scopes; a stdio MCP that needs the account's
  token names its variables in `mcp_env_injection`, and the access token
  arrives in those variables in the MCP's own process when it starts. No
  stdio MCP is handed a token file, a refresh token or a client secret. Beside
  the token the platform sets `OTO_TOKEN_SCOPES` (what the sign-in granted,
  separated by spaces), `OTO_TOKEN_ACCOUNT` (the account's label, its address
  for Google) and, when the token has an expiry, `OTO_TOKEN_EXPIRES_AT`
  (ISO 8601 UTC). A token with less than 24 hours left at the session's start
  also brings `OTO_TOKEN_RENEWAL`. When `OTO_TOKEN_EXPIRES_AT` comes close,
  send `POST` to `$PROXY_URL/v1/hooks/mcp-credentials` with
  `Authorization: Bearer $OTO_TOKEN_RENEWAL`, with no HTTP proxy between (the
  address is the platform's own loopback listener). The answer is
  `{"env": {...}}` with your variables, the new expiry, the scopes and the
  account, and no other secret. A 409 means the account needs a reconnect
  (its `detail` is a sentence to show), a 503 means try again in a moment, a
  404 means the session no longer holds the token and a 401 that the renewal
  is no longer good. A remote MCP that
  needs the bearer header sets `bearer_required: true` and `proposed_hosts`,
  on a streamable-HTTP `server.transport` (`sse` is refused: the platform
  forwards to the one declared path). The platform never writes the bearer into a config file — it adds the
  header to each request as it leaves, so the value never reaches the agent.
- `api_key_header`: for a vendor-hosted server that takes an API key in a
  header of its own (Google Maps Grounding Lite takes `X-Goog-Api-Key`).
  `{name, value_from, proposed_hosts}`: `name` is the header the platform
  adds on the way out; `value_from` names the key whose value fills it — a
  `credentials.fields` entry with `input_type: "password"` under
  `credentials.type` `per_user` or `infra`, or an env-delivered
  `instances.fields` key; `proposed_hosts` are the hosts you
  propose: installed from the community catalog, the MCP's own literal
  https server host applies by itself, and any other host (a plain http one
  included), like every host of a ZIP, waits for an admin's Allow under the
  MCP's `name`, on the install card, in the MCP's row on MCP Servers or in
  Admin Setup → Security. Rules:
  `server.transport` is streamable HTTP (`streamable_http`,
  `streamable-http` or `http`); `server.url_template` is a literal URL (no
  `${…}` anywhere in it), `http` or `https`, whose host matches one of
  `proposed_hosts` (an entry there may use `*`). The validator does not
  judge the host itself: the allowlist row (the catalog's, or an admin's
  Allow) and the credential gateway's egress guard (which refuses a host
  that resolves onto the platform's own loopback or link-local range) do;
  `name` is a plain header name and not one the platform owns or strips
  (`authorization`, `cookie`,
  `host`, `content-*`, `accept*`, `mcp-*`, `forwarded`, `x-forwarded-*`, and
  the transport headers); and it is never declared beside
  `oauth.bearer_required`. Like a bearer, the value is added to each request
  as it leaves and is never written into a config file. A catalog entry with
  the block declares `platform_min_version: "1.7.1"`: an older platform loads
  the MCP without adding the header.
- `exit`: for a stdio MCP that sends a secret in a header of its own toward
  fixed vendor hosts. The session then never holds the secret. The tool's
  environment holds a placeholder (`oto-exit-` and 32 hex characters), and
  the platform writes the value into that header on the way out of the
  sandbox. `[{key, header, prefix, hosts, streams}]`, one entry per secret:
  - `key` names the variable the secret arrives in: an env-delivered
    `instances.fields` key marked `secret`, a `credentials.fields` key with
    `input_type: "password"`, or a `credentials.oauth.mcp_env_injection`
    name. No two entries share a key.
  - `header` is the header the tool writes it in (`Authorization`,
    `X-Api-Key`). Never one the platform owns: `cookie`, `host`,
    `content-*`, `accept*`, `mcp-*`, `forwarded`, `x-forwarded-*`,
    `x-real-ip`, `proxy-*` and the transport headers.
  - `prefix` is what the tool writes before the key (`"Bearer "`), 32
    printable characters at most.
  - `hosts` are the hosts the secret may go to: literal names or
    `*.domain` with two labels or more. Never an address, `localhost` or a
    name with a trailing dot. Under an engine's domain only a literal API
    host below it fits (`api.openai.com`), never the domain, a wildcard over
    it, `chatgpt.com` or `claude.com`. From the community catalog a literal
    host applies by itself. A wildcard, and every host of a ZIP, waits for
    the admin's Allow.
  - `streams: true` lets a request body above 16 MiB pass as it arrives.
    Set it when the tool sends a picture or a file inline in a request that
    carries the secret. Without it such a request is refused.

  What fits: a client that reads `HTTPS_PROXY` and the trust variables
  (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) and speaks
  HTTP/1.1. Python's httpx, requests and urllib do. Node needs axios 1.18 or
  newer, or its own fetch with `NODE_USE_ENV_PROXY`, which the platform sets
  (Node 22.21, 24 or newer). What does not fit: a key the tool sends in the
  address or in the body, since the platform refuses a placeholder there. A
  client that ignores the proxy or brings its own trust store, gRPC, and a
  client that speaks HTTP/2 alone: these would send the placeholder to the
  vendor. Leave such a key out of `exit`, and the tool gets the value itself.
  A token named in `mcp_env_injection` fits only when every name of that
  token is declared. The tool then holds it with no expiry and no renewal,
  so `OTO_TOKEN_EXPIRES_AT` and `OTO_TOKEN_RENEWAL` are absent.

  On a computer that cannot add the value on the way out, the platform
  leaves the tool out of the session and says why. Never
  beside `oauth.bearer_required`. A catalog entry with the block declares
  `platform_min_version: "2.0.0"`.
- `oauth.authorization_server`: for a vendor-hosted server that names its
  own authorization server the MCP way (Notion's and Linear's hosted servers
  do). The platform discovers that server from the endpoint URL, registers
  the install there as a client once, and each person signs in on the
  vendor's page: no OAuth app to create. Fields:
  `registration` (`"dynamic"`, the only value), `confidential` (`false` by
  default; `true` asks the server for a client secret), `accepts_app_tokens`
  (`false` by default: the server takes its own tokens only and the
  registration is the sign-in; `true` when the server also accepts the
  vendor's app tokens, as Linear's does: hosted mode and an admin's app come
  first and the registration is the fallback, tools only), `issuer`
  (optional: an https issuer the
  server's metadata lists, when it lists several), `client_name` (optional,
  shown on the consent page), `scopes` (optional, asked for at registration
  and sent at consent only when the picked services carry no scopes),
  `identity` (optional: `label_field`, `display_field`, `id_field`, dotted
  paths into the token response that name the account; used unless the
  manifest's `userinfo_url` is an https URL on the MCP server's host).
  Rules: `flows` is exactly `["authorization_code_pkce"]`, `bearer_required`
  is true, `server.url_template` is an https URL (http is refused here,
  unlike `api_key_header`) whose host is literal (no `${…}` in it) and
  matches one of `proposed_hosts` (the validator judges only that match;
  the allowlist row and the gateway's egress guard judge the host),
  `userinfo_url` (if any) is https, `provider_id`
  is neither google, slack, microsoft, zoom nor facebook, nor used by an
  installed MCP the other way (with an admin's app), and none of `authorize_params`,
  `env_injection`, `mcp_env_injection`, `git_credential_helper`,
  `device_authorization_url`, `app_credential_variants` is declared (the
  token serves the MCP server only). The vendor's URLs and an
  `app_credential` may stay beside it (a catalog entry OtoDock maintains may
  also carry `hosted.oauth_app`): with
  `accepts_app_tokens` an admin who configures their own app keeps the app
  flow and an install without one registers itself; without it they sign
  nobody in and deliver no events: the registered client is the only
  sign-in, its token is refused for webhook calls, and the admin's MCP
  Servers row shows only the registration card. A
  catalog entry with the block declares `platform_min_version: "1.7.1"`.
  People whose dashboard is reached over plain http (not localhost) cannot
  connect such an MCP: the vendors accept https or loopback callbacks only.

## App connectors

An app connector drives a program on the person's own computer. The
Blender and OBS Studio entries in the catalog are app connectors. The
server runs on that computer and reaches the program on a local port. Four
fields make a package one:

| Field | Value | What it does |
|---|---|---|
| `device_capability` | `"app"` | The tool needs the computer's App connectors grant. This field alone keeps it off every machine but a paired computer. |
| `placement` | `"satellite_only"` | The tool runs only on a paired computer. Set it beside `device_capability`, as the catalog's entries do. |
| `requires_display` | `true` | The tool is left out on a computer known to have no screen. |
| `companion_app` | an object | The program and how the server reaches it, below. The platform checks its shape and refuses a wrong one. It never starts the program. |

`companion_app` holds `program`, the program's name. `connect` holds
`host` (default `127.0.0.1`), `transport` (default `tcp_json`), `port`
and `port_discovery`, empty or `plugin_announce`. `plugin` holds `asset`,
`min_plugin_version` and `supported_host_versions`. Then `status_tool`,
and `auth`: `none`, or `per_connection_token`, the default.

```json
"placement": "satellite_only",
"requires_display": true,
"device_capability": "app",
"companion_app": {
  "program": "blender",
  "connect": {"host": "127.0.0.1", "transport": "tcp_json", "port": 9876},
  "plugin": {"asset": "", "min_plugin_version": "1.0.3",
             "supported_host_versions": ["blender>=5.1"]},
  "status_tool": "",
  "auth": "none"
}
```

Where it loads. The tool loads only in a session on a computer a person or
an admin paired. It never loads on the platform's own compute machines.

It loads only where that computer's App connectors grant is on. The person
who paired the computer turns it on in their own settings. It sits under
Remote Machines, on that computer's card, under Device control. For a
computer an admin paired, an admin turns it on under Admin, Remote Machines.

Its calls. Where the grant is on, a chat in Ask or Auto runs the
connector's tools without a prompt. The grant is the person's consent.

A turn that an API key drives follows the chat's mode like any tool. Nobody
sits at that screen then. A tool that runs code inside the program is no
exception. So the package needs no `permissions` block.

Set each tool's annotations truthfully, as for any MCP: they give its tier
where the grant does not decide. Leave out `device_high_risk_tools`: it
pins nothing.

The program must be open. Some connectors connect to their program as they
start. OBS Studio's does. Blender's connects at each call. For a connector
that connects as it starts, the program must be open before the chat
starts.

Otherwise the first chat after the tool's install or update may get none of
its tools. They come once the program is open and the chat is reloaded. In
a later chat the tools are listed. A call while the program is closed says
the tool did not start. The next call works once the program is open.

So tell the agent what to do, in the entry's `system_requirements.notes`
and in its own skill. It asks the person to open the program. Then it calls
again, or reloads the chat with `reload_session`. A server you write
yourself avoids all this. It lists its tools without the program and
connects at each call, as Blender's does.
