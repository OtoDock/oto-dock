# mcps/ — MCP tool servers

The tools agents use, packaged as [MCP](https://modelcontextprotocol.io)
servers. Each MCP is a self-contained folder with a `manifest.json` at its
root describing its tools, transport, and how the platform should run it —
drop in a manifest, assign the MCP to an agent, done.

- `custom/` — OtoDock's first-party set: file tools and live document
  editing, agent memory, tasks and delegation, meetings, notifications,
  triggers, image generation and search, agent self-configuration, and more.
- `community/` — local mirrors of third-party MCPs installed from the
  [community catalog](https://github.com/OtoDock/community-mcps). Browse and
  install these from the dashboard (Browse Community); contribute new ones to
  the community repo, not here.
- `_shared/` — building blocks shared across MCPs (see its README).

An MCP folder may carry an `icon.png` (256×256) that the dashboard shows
next to the MCP. The bundled ones share one family, a blue tile with a
[Lucide](https://lucide.dev) glyph (ISC licence); a community entry's icon
is the upstream project's own mark.

Developer docs — manifest format, sandboxing, credential brokering — live at
[docs.otodock.io](https://docs.otodock.io).
