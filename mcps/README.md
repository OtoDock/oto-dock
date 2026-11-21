# mcps/: MCP tool servers

The tools agents use, packaged as [MCP](https://modelcontextprotocol.io)
servers. Each MCP is a self-contained folder with a `manifest.json` at its
root. The manifest describes its tools, transport, and how the platform
should run it. Drop in a manifest, assign the MCP to an agent, done.

- `custom/`: OtoDock's first-party set. File tools and live document
  editing, agent memory, tasks and delegation, meetings, notifications,
  triggers, image generation and search, agent self-configuration, and more.
- `community/`: local mirrors of third-party MCPs installed from the
  [community catalog](https://github.com/OtoDock/community-mcps). Browse and
  install these from the dashboard (Browse Community). Contribute new ones to
  the community repo, not here.
- `_shared/`: building blocks shared across MCPs (see its README).

An MCP folder may carry an `icon.png` (256×256) that the dashboard shows
next to the MCP. The bundled ones share one family, a blue tile with a
[Lucide](https://lucide.dev) glyph (ISC licence). A community entry's icon
is the upstream project's own mark.

Three JSON schemas live here beside the folders, one per catalog kind:

- `manifest.schema.json`: an MCP package's `manifest.json`
- `agent-template.schema.json`: an agent template's folder, read as one
  document
- `skill.schema.json`: a skill's `SKILL.md` front matter

The platform checks what it installs against them. Each community catalog's
CI checks its entries against the copy of the release it pins.

Developer docs (manifest format, sandboxing, credential brokering) live at
[docs.otodock.io](https://docs.otodock.io).

An agent can author a package too. The `mcp-authoring` skill bundled with
`custom/mcps-mcp` carries the manifest contract and the package rules. That
MCP's check tool, `validate_mcp_package`, is for editors and above. It
checks a folder the way the admin installer will, before an admin installs
the zip from Admin → MCP Servers.
