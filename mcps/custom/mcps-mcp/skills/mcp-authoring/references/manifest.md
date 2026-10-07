# The manifest, field by field

Every field of `manifest.json` the platform reads. The full reference with
the platform-side details is on docs.otodock.io (the MCP framework pages)
and in the catalog's CONTRIBUTING.md at github.com/OtoDock/community-mcps.

## Top level

| Field | Required | What it is |
|---|---|---|
| `name` | yes | The canonical slug: lowercase letters, digits, dashes, underscores. The key for assignments, config and tools; keep it equal to the folder name; never rename a shipped package. |
| `label` | yes | The display name. |
| `description` | yes | One line for the catalog card and the agent's tool list. |
| `version` | yes | `""` for an npm or PyPI package (the platform pins the installed release); a version for a container (equal to the image tag) and for a git source (the pinned ref). |
| `category` | yes | `"community"`. |
| `author` | catalog | Who maintains the server code the package wraps, written as the project shows it. |
| `author_url` | catalog | That code's repository (`https://…`), or the vendor's page for a hosted MCP. |
| `server` | yes | The runtime block below. |
| `server_name` | no | The tool namespace prefix when it must differ from `name`. |
| `credentials` | no | `type`: `none`, `per_user` (each person enters their own, in Settings) or `infra` (an admin enters them once); `fields` for the inputs; `oauth` for an OAuth provider; `service_account: true` lets a manager lend an account to agent-scope sessions. |
| `instances` | no | Admin-managed instances (a server URL, a token) delivered as env (`delivery: "env"`); pair with `assignment_mode: "explicit"`. |
| `config` | no | Admin-managed key/value settings shown on the MCP's page; `user_overridable: true` lets each person override one. |
| `env` | no | Static variables for every launch. |
| `agent_env` | no | Per-session variables with `${session.*}` tokens. |
| `path_env` | no | Path-bearing variables by role (`workspace`, `user_root`, `shared_workspace`, `config`, `knowledge_dir`, `credentials_dir`). The platform fills them with the right sandbox path per session. |
| `tool_arg_paths` | no | Which tool arguments are paths (`{"tool": {"arg.path": {"mode": "read"}}}`), so the platform translates and admits them before your server sees them. |
| `skills` | no | Skills bundled with the MCP: `{id, file, description, loading, default_exclude_from, audience}`. `loading` is `on_demand` (default) or `always`; `audience` (`owner`, `editor`, `workspace`) limits a skill to a tier. |
| `agent_context` | no | Prompt blocks with `${account.*}`, `${agent.*}`, `${trigger.*}` tokens. |
| `outputs` | no | Files the server writes into a shared folder that the platform moves into the session's workspace. |
| `costs` | no | Per-tool prices (`currency`, `provider`, ordered `rules`). |
| `permissions` | no | Per-tool permission tiers (`open`, `standard`, `sensitive`, `critical`). |
| `assignment_mode` | no | `auto` (managers enable it per agent) or `explicit` (an admin must configure an instance first). |
| `exclude_from` | no | Session kinds that never load it: `phone`, `task`, `terminal`, `meeting`, `external`. |
| `audience` | no | The tier of the agent this whole MCP is for: `owner`, `editor` or `workspace` (empty = everyone, the default). In a session for a person below that tier the MCP is left out — of the session, its tools and its sandbox network opening; a session the agent runs as itself is always in, except a phone caller's (a caller who is not a platform user counts as a viewer). The whole-MCP version of `skills[].audience`. |
| `network_targets` | no | The internal hosts a homelab MCP dials, read from a config key, an instance field or a credential; the admin's toggle opens the sandbox to exactly those. |
| `system_requirements` | no | OS packages and a `node_min` the installer checks before installing. |
| `replaces` | no | Sources this entry supersedes, with credential key renames: `[{"source": "<previous source>", "credentials": {"OLD": "NEW"}}]`. Information for the admin's switch, never an authorization. |
| `tool_filter` | no | For a container whose binary takes a tool-filter flag: `{arg_name, env_var_name}`. |
| `patched`, `patch_note` | no | `true` plus one line when `patches/*.patch` are shipped and applied after `npm install`. |
| `deprecated`, `platform_min_version` | no | Catalog metadata. |

Fields a community package never sets: `requires_capability`, `hosted`,
`server.proxy_callbacks`, `placement`, `device_capability`,
`device_high_risk_tools`, `companion_app` (platform and device features).

## `server`

| Field | Required | What it is |
|---|---|---|
| `runtime` | all but remote | `python`, `node` or `docker`. A remote (vendor-hosted) MCP declares none. |
| `transport` | yes | `stdio` for python and node; `http` for a container (streamable HTTP at `/mcp/`); `streamable_http` for a remote MCP. |
| `command`, `args` | stdio | What to launch, relative to the folder, `${mcp_dir}` allowed in args (`node`, `["${mcp_dir}/node_modules/<pkg>/dist/index.js"]`; `venv/bin/python`, `["-m", "<module>"]`). |
| `source` | yes | `npm:<package>`, `pypi:<package>`, `git+<url>@<ref>#subdirectory=<dir>`, `docker:<label>` or `remote:<host>`. |
| `version_constraint` | no | A PEP 440 range (`">=2,<3"`) that bounds the automatic updates of an npm or PyPI package. |
| `source_build` | no | For a PyPI package: the distributions the installer may build from source when the machine has no wheel for them (every package installs from wheels first; a listed one is built only when that resolve fails on it). |
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
  its fields, `services` with scopes; a stdio MCP that reads a token file
  adds a `path_env` entry with `role: "credentials_dir"`; a remote MCP that
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
  propose, which an admin approves under the MCP's `name`. Rules:
  `server.transport` is streamable HTTP (`streamable_http`,
  `streamable-http` or `http`); `server.url_template` is a literal URL (no
  `${…}` anywhere in it), `http` or `https`, whose host matches one of
  `proposed_hosts` (an entry there may use `*`). The validator does not
  judge the host itself: the allowlist row an admin approves and the
  credential gateway's egress guard (which refuses a host that resolves
  onto the platform's own loopback or link-local range) do; `name` is a plain header
  name and not one the platform owns or strips (`authorization`, `cookie`,
  `host`, `content-*`, `accept*`, `mcp-*`, `forwarded`, `x-forwarded-*`, and
  the transport headers); and it is never declared beside
  `oauth.bearer_required`. Like a bearer, the value is added to each request
  as it leaves and is never written into a config file. A catalog entry with
  the block declares `platform_min_version: "1.7.1"`: an older platform loads
  the MCP without adding the header.
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
