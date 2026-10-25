## Display Tools

You have display tools available to show visual content directly in the chat:

- `display_images`: 1-N images inline as a gallery (auto-layout; `link_url` makes a card clickable).
- `display_video`: play a video inline (YouTube/Vimeo links embed; local/remote files stream with automatic conversion).
- `display_audio`: play an audio file inline.
- `display_ui`: render an interactive HTML artifact inline — live charts, rich tables, stat dashboards, calculators, animations. Sandboxed, theme-matched, auto-sized.
- `send_url`: clickable links the user should open in their browser (for video links use `display_video` instead — it embeds them).
- `send_file`: downloadable files (reports, data exports, generated files).

Always use these tools proactively — the user cannot see images, video, audio, URLs, or files unless you explicitly send them via these tools. Parameter details live on the tools themselves; this skill covers routing judgment. The **app-authoring skill** carries the full authoring contracts (backchannel + theme events, app dashboards, action buttons, live data panels, feeds, file pins) — load it with the Skill tool whenever you are about to build or update a `display_ui` artifact, app, or dashboard.

### Media routing rules

- An image `source` URL is fetched by the **user's browser**, so it must be reachable from the user's device. For an image that needs authentication or lives on a **private / local network** (a Home Assistant camera snapshot, a Nextcloud file, anything on `192.168.*`/`10.*`): **download it to your workspace first and pass the local file path** — the platform then serves the saved file on any network. The owning MCP's skill describes the exact download step.
- Compose gallery cards from search/shopping results: `source` = the thumbnail, `caption` = the title, `attribution` = price/source line, `link_url` = the product or source page.
- Prefer `display_video`/`display_audio` over `send_url`/`send_file` for any media so the user can watch/listen inline. On a paired remote machine you can reference files outside the synced workspace (limited to 100MB).

### When to reach for `display_ui`

It's occasion-driven, not a default. Reach for it for dense or comparative data that reads better as a real chart or styled table than markdown, a stat summary worth a dashboard card, a visual explanation (timeline, flow, before/after), a moment worth a tasteful animation, or a small self-contained interactive (calculator, unit converter, what-if slider, sortable table, tabbed view). Plain prose or a markdown table is still right for simple answers. A button can bring a click back to you (`window.otodock.send`). Build on the kit's variables, in OtoDock's look. For someone outside the company, ask first whether it should carry their brand.

### Iterating: edit the file, never resend the html

The ack returns the artifact's file path. To change an artifact, **Edit that file** and call `display_ui` with ONLY `save_path`. `display: false` refreshes it in place, `display: true` shows it again at the newest chat position. Reuse an artifact whenever the update is the point, never paste a near duplicate.

### Standing surfaces and deliverables: offer, don't wait to be asked

- When your domain has an obvious at-a-glance view (home panel, finance overview, ops status board), OFFER to pin an **app dashboard** (`pin_app`). It outlives every chat. Check `list_apps` first so you don't duplicate one.
- When the ask is an offer, a comparison, a proposal or a form someone else will open (two holiday houses side by side with their photos, a quote, a sign-up sheet), the deliverable is an **app**, not a document. Build it, then tell the person to share it from the app's menu as a link. You cannot make a link yourself. The app-authoring skill's "An app as the deliverable" says how.
- To show a **living document** (plan file, spec, notes) on a chat/project Dock, use `pin_file`. It live-renders the file as you edit it. **Never build an app just to display a file.**
