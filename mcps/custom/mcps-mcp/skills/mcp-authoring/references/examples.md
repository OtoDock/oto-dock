# Three complete packages

Each is a folder you can copy, edit and validate.

## 1. An npm server with an admin-managed instance

```
prometheus/
├── manifest.json
├── README.md
└── icon.png            (optional, 256×256)
```

```json
{
  "name": "prometheus",
  "label": "Prometheus",
  "description": "Metrics and monitoring queries",
  "version": "",
  "category": "community",
  "author": "idanfishman",
  "author_url": "https://github.com/idanfishman/prometheus-mcp",
  "server": {
    "runtime": "node",
    "transport": "stdio",
    "command": "node",
    "args": ["${mcp_dir}/node_modules/prometheus-mcp/dist/index.mjs", "stdio"],
    "source": "npm:prometheus-mcp"
  },
  "credentials": {"type": "none"},
  "instances": {
    "delivery": "env",
    "fields": [
      {"key": "PROMETHEUS_URL", "label": "Prometheus URL", "input_type": "url",
       "default": "http://localhost:9090"}
    ],
    "max_instances": 0
  },
  "assignment_mode": "explicit",
  "network_targets": [
    {"source": "instance", "host_key": "PROMETHEUS_URL", "port_default": 9090}
  ]
}
```

The admin creates an instance (the URL), assigns it to agents, and turns the
internal-network toggle on so the sandbox reaches that host.

## 2. A PyPI server with per-user credentials and a bundled skill

```
nextcloud/
├── manifest.json
├── README.md
└── skills/
    └── nextcloud-usage/
        └── SKILL.md
```

```json
{
  "name": "nextcloud",
  "label": "Nextcloud",
  "description": "Files, notes and calendars on your Nextcloud",
  "version": "",
  "category": "community",
  "author": "example",
  "author_url": "https://github.com/example/nextcloud-mcp",
  "server": {
    "runtime": "python",
    "transport": "stdio",
    "command": "venv/bin/python",
    "args": ["-m", "nextcloud_mcp"],
    "source": "pypi:nextcloud-mcp",
    "version_constraint": ">=1,<2"
  },
  "credentials": {
    "type": "per_user",
    "label": "Nextcloud account",
    "fields": [
      {"key": "NEXTCLOUD_URL", "label": "Server URL", "input_type": "url"},
      {"key": "NEXTCLOUD_USERNAME", "label": "Username", "input_type": "text"},
      {"key": "NEXTCLOUD_PASSWORD", "label": "App password", "input_type": "password"}
    ]
  },
  "path_env": {
    "NEXTCLOUD_DOWNLOAD_DIR": {"role": "workspace", "subpath": "downloads/nextcloud"}
  },
  "tool_arg_paths": {
    "upload_file": {"path": {"mode": "read"}}
  },
  "skills": [
    {"id": "nextcloud-usage", "file": "skills/nextcloud-usage/SKILL.md",
     "description": "How to organise files and notes on Nextcloud with this MCP.",
     "loading": "on_demand"}
  ],
  "network_targets": [
    {"source": "per_user_credential", "host_key": "NEXTCLOUD_URL"}
  ]
}
```

`skills/nextcloud-usage/SKILL.md` starts with a frontmatter block (`name`,
`description`) and holds the instructions; the id equals the folder name.

## 3. A container with a prebuilt image

```
espo-crm/
├── manifest.json
├── README.md
├── Dockerfile
├── docker-compose.yml
└── server.py           (the server the image runs)
```

```json
{
  "name": "espo-crm",
  "label": "EspoCRM",
  "description": "Read access to EspoCRM accounts, contacts and opportunities",
  "version": "1.0.1",
  "category": "community",
  "author": "example",
  "author_url": "https://github.com/example/espo-crm-mcp",
  "server": {
    "runtime": "docker",
    "transport": "http",
    "docker_compose": "docker-compose.yml",
    "port": 8934,
    "health_endpoint": "/health",
    "url_template": "http://${docker_mcp_host}:${port}",
    "source": "docker:espo-crm-mcp",
    "image": "ghcr.io/example/espo-crm-mcp:1.0.1"
  },
  "credentials": {
    "type": "infra",
    "label": "EspoCRM connection",
    "fields": [
      {"key": "ESPOCRM_URL", "label": "EspoCRM base URL", "input_type": "url", "required": true},
      {"key": "ESPOCRM_API_KEY", "label": "API key", "input_type": "password", "required": true}
    ]
  },
  "env": {"MCP_PORT": "${platform.mcp_port}"}
}
```

```yaml
services:
  espo-crm-mcp:
    build: .
    container_name: espo-crm-mcp
    restart: always
    ports:
      - "127.0.0.1:8934:8934"
    env_file: .env
```

The platform writes the container's `.env` from the manifest and the admin's
credentials, rewrites the compose for the containerised deployment, and
pulls `server.image` (which equals the version as its tag).

## 4. A vendor-hosted server that signs people in itself

```
notion-hosted-mcp/
├── manifest.json
├── README.md
└── icon.png
```

```json
{
  "name": "notion-hosted-mcp",
  "label": "Notion",
  "description": "Notion pages, databases, comments and search through Notion's hosted MCP server",
  "version": "1.0.0",
  "category": "community",
  "author": "Notion",
  "author_url": "https://developers.notion.com/guides/mcp/overview",
  "platform_min_version": "1.7.1",
  "server": {
    "transport": "streamable_http",
    "url_template": "https://mcp.notion.com/mcp",
    "source": "remote:mcp.notion.com"
  },
  "credentials": {
    "type": "per_user",
    "label": "Notion Account",
    "description": "Sign in to Notion in the browser. This install registers itself with Notion's authorization server; there is no OAuth app to create.",
    "service_account": true,
    "oauth": {
      "provider_id": "notion-hosted",
      "flows": ["authorization_code_pkce"],
      "authorization_server": {
        "registration": "dynamic",
        "confidential": false,
        "identity": {"label_field": "workspace_id", "display_field": "email_domain", "id_field": "user_id"}
      },
      "bearer_required": true,
      "proposed_hosts": ["mcp.notion.com"],
      "services": [
        {"key": "default", "label": "Notion workspace", "description": "Pages, databases, comments and search, as you", "scopes": ["default"]}
      ]
    }
  },
  "exclude_from": ["phone"]
}
```

Nothing runs locally and no admin creates an app: the platform reads the
server's metadata, registers the install at the authorization server it
names, and sends each person to the vendor's consent page with PKCE. An
admin adds `(notion-hosted, mcp.notion.com)` to the OAuth Bearer Allowlist
in Admin Setup → Security (until then a connect answers 400), and the token
reaches the MCP server as its bearer and nowhere else. A server that also
accepts the vendor's own app tokens (Linear's) declares
`"accepts_app_tokens": true` and keeps the app flow's URLs and `app_credential`
beside the block (OtoDock's own catalog entry also carries `hosted.oauth_app`): hosted mode and an admin's app sign
people in first (with events), and the registration is the fallback for an
install without either (tools only).

## 5. A vendor-hosted server with a header-style API key

Some hosted servers take an API key in a header of their own instead of a
bearer. Google Maps Grounding Lite takes `X-Goog-Api-Key`:

```json
{
  "name": "google-maps-grounding-mcp",
  "label": "Google Maps Grounding Lite",
  "description": "Grounded place and route answers from Google Maps through its hosted MCP server",
  "version": "1.0.0",
  "category": "community",
  "author": "Google",
  "author_url": "https://developers.google.com/maps",
  "platform_min_version": "1.7.1",
  "server": {
    "transport": "streamable_http",
    "url_template": "https://mapstools.googleapis.com/mcp",
    "source": "remote:mapstools.googleapis.com"
  },
  "credentials": {
    "type": "per_user",
    "label": "Google Maps API key",
    "description": "Paste a Google Maps Platform API key.",
    "fields": [
      {"key": "GOOGLE_MAPS_API_KEY", "label": "API key", "input_type": "password", "required": true}
    ],
    "api_key_header": {
      "name": "X-Goog-Api-Key",
      "value_from": "GOOGLE_MAPS_API_KEY",
      "proposed_hosts": ["mapstools.googleapis.com"]
    }
  },
  "exclude_from": ["phone"]
}
```

Nothing runs locally. Each person pastes their key in Settings; the platform
adds `X-Goog-Api-Key: <key>` to each request as it leaves and never writes
the key into a config file or hands it to the agent. `value_from` must name
a `credentials.fields` password key (as here) or an env-delivered
`instances.fields` key. An admin approves `(google-maps-grounding-mcp,
mapstools.googleapis.com)` on the OAuth Bearer Allowlist in Admin Setup →
Security — a header-style key is approved under the MCP's own `name`, not a
provider id — and until then the MCP is left out of sessions with a clear
reason. The header name may not be one the platform owns (`Authorization`, a
transport header, and the rest the manifest reference lists).

## A `replaces` declaration

When a shipped package moves to another source, the new manifest says so:

```json
"server": {"runtime": "node", "transport": "stdio", "command": "node",
           "args": ["${mcp_dir}/node_modules/nextcloud-next/dist/index.js"],
           "source": "npm:nextcloud-next"},
"replaces": [
  {"source": "npm:nextcloud-mcp-server",
   "credentials": {"NEXTCLOUD_USER": "NEXTCLOUD_USERNAME"}}
]
```

The admin's page then shows the move as declared, lists the renamed key, and
the switch keeps every stored value under its new name.
