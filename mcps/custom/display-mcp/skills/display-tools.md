## Display Tools

You have display tools available to show visual content directly in the chat:

- `display_images`: 1-N images inline as a gallery (auto-layout; `link_url` makes a card clickable).
- `display_video`: play a video inline (YouTube/Vimeo links embed; local/remote files stream with automatic conversion).
- `display_audio`: play an audio file inline.
- `display_ui`: render an interactive HTML artifact inline — live charts, rich tables, stat dashboards, calculators, animations. Sandboxed, theme-matched, auto-sized.
- `send_url`: clickable links the user should open in their browser (for video links use `display_video` instead — it embeds them).
- `send_file`: downloadable files (reports, data exports, generated files).

Always use these tools proactively — the user cannot see images, video, audio, URLs, or files unless you explicitly send them via these tools. Parameter details live on the tools themselves; this skill covers routing judgment. The **miniapp-authoring skill** carries the full authoring contracts (backchannel + theme events, mini-app dashboards, action buttons, live data panels, feeds, file pins) — read it whenever you are about to build or update a `display_ui` artifact, mini-app, or dashboard.

### Media routing rules

- An image `source` URL is fetched by the **user's browser**, so it must be reachable from the user's device. For an image that needs authentication or lives on a **private / local network** (a Home Assistant camera snapshot, a Nextcloud file, anything on `192.168.*`/`10.*`): **download it to your workspace first and pass the local file path** — the platform then serves the saved file on any network. The owning MCP's skill describes the exact download step.
- Compose gallery cards from search/shopping results: `source` = the thumbnail, `caption` = the title, `attribution` = price/source line, `link_url` = the product or source page.
- Prefer `display_video`/`display_audio` over `send_url`/`send_file` for any media so the user can watch/listen inline. On a paired remote machine you can reference files outside the synced workspace (limited to 100MB).

### When to reach for `display_ui`

It's occasion-driven, not a default: dense or comparative data that reads better as a real chart or styled table than markdown; a stat summary worth presenting as a dashboard card; a visual explanation (timeline, flow, before/after); a moment worth celebrating with a tasteful animation; or a small self-contained interactive (calculator, unit converter, what-if slider, sortable table, tabbed view). Plain prose or a markdown table is still right for simple answers.

Three levels of ambition, all fully supported: **display** (charts, tables, cards, animated SVG — one-way), **self-contained interactivity** (inputs, sliders, client-side calculation inside the sandbox), and the **chat backchannel** (`window.otodock.send(payload)` delivers a user gesture back to you as a new framed input). The authoring contracts, snippets, and styling rules for all three live in the miniapp-authoring skill.

### Iterating — Edit the file, don't resend the html

The ack returns the artifact's file path. To update an artifact you already displayed: **Edit that file directly** (normal file edit), then call `display_ui` with ONLY `save_path` (omit `html`) — the file's current content is re-read and goes live. Resending full `html` is only for the first creation or a total rewrite; for a small change it makes the user wait while you regenerate the whole file. Two re-display modes: `display: true` (default — re-appears at the newest chat position, older copy collapses to a chip) for turn-based updates; `display: false` (silent in-place refresh, no new chat block) for many small updates mid-turn. Reuse an existing artifact whenever the update is the point — don't rebuild from scratch or paste a near-duplicate.

### Standing surfaces — offer, don't wait to be asked

- When your domain has an obvious at-a-glance view (home panel, finance overview, ops status board), OFFER to pin a **mini-app dashboard** (`pin_app`) — it outlives every chat; check `list_apps` first so you don't duplicate one. Authoring rules + action buttons: miniapp-authoring skill.
- To show a **living document** (plan file, spec, notes) on a chat/project Dock, use `pin_file` — it live-renders the file as you edit it. **Never build a mini-app just to display a file.**
