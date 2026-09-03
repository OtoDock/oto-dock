---
name: miniapp-authoring
description: Authoring contracts for interactive artifacts, mini-app dashboards, and Dock panels — display_ui backchannel and theme events, pin_app action buttons (fire_task / send_prompt / mcp_tool), live data panels, platform feeds, scoped dashboards, and file pins. Use whenever building or updating a display_ui artifact, mini-app, or dashboard.
---

# Mini-app & artifact authoring

The display-tools skill covers WHEN to reach for these surfaces; this one is
the full authoring reference. Everything here renders in the same sandboxed,
theme-matched frame (token vars, primitives, and kit script tags are listed
on the `display_ui` tool itself).

## `display_ui` authoring

**Chat backchannel** — `window.otodock.send(payload)` delivers an interaction back into the chat, and YOU receive it as a new framed input (`[interaction from artifact "title"]` + the JSON payload) — sent immediately when the chat is idle, queued to the turn boundary while you're working. Use it for real decision points: an "Analyze this row" button, a confirm/apply choice, a form the user fills. Rules: send ONLY on an explicit user gesture (never on load — every delivery starts a real agent turn and is rate-limited), keep payloads small self-describing JSON (≤8KB, e.g. `{"action":"analyze","row":"2026-03"}`), and reflect delivery state from the ack — the host answers with an `otodock:action-ack` window event whose `detail.status` is `sent` / `queued` / `blocked` (user declined the first-use consent) / `denied` (rate/size) / `unavailable` (read-only views like history or task runs — disable the button there):

```html
<button class="btn primary" id="go">Analyze March</button>
<script>
  document.getElementById('go').onclick = function(){
    window.otodock.send({action: 'analyze', month: '2026-03'});
  };
  addEventListener('otodock:action-ack', function(e){
    var b = document.getElementById('go');
    if (e.detail.status === 'sent' || e.detail.status === 'queued') b.textContent = 'Sent ✓';
    else if (e.detail.status === 'unavailable') b.disabled = true;
  });
</script>
```

**Links** — a plain `<a href="https://...">` works: the runtime bridges ABSOLUTE http(s) links to the host, which opens them in a new tab after validation and a one-time per-artifact consent (the chip shows the destination site). Write real absolute URLs: relative paths and platform (same-origin) URLs never open — the sandbox has no navigation and platform routes are deliberately refused — and clicking one is cancelled, so it can't break your page either. In-page anchors (`#section`), `mailto:`/`tel:` links and `download` links behave normally. Each attempt is acked with an `otodock:open-url-ack` window event (`detail.status`: `opened` / `blocked` / `denied`, e.g. popup-blocked or rate-limited) if you want to reflect state. Prefer `otodock.send(...)` action buttons for anything that should involve YOU; links are for genuinely external destinations.

**Full documents** — content starting with `<!doctype`/`<html` keeps its own markup and `<head>` (no tokens CSS / ui-kit), but the platform runtime is still injected before `</body>`, so theme events, auto-height, actions, feeds and links all keep working.

**Design responsive** — the artifact renders on phones as well as desktop: fluid widths only (`width:100%` / `max-width`, never fixed pixel layouts), let content wrap, use Tailwind responsive variants (`sm:` `md:`) for multi-column layouts that must collapse on narrow screens, and give JS-drawn charts a percentage width with a sensible fixed height. On desktop the app surface is the FULL tab width (wide, roughly 16:9) — design for wide: multi-column grids and side-by-side panels that collapse on mobile, never one narrow column floating in empty space. If a section is long-form reading where a website-like measure genuinely reads better, self-cap just that section (`max-width` + `margin-inline:auto`) — the platform never caps your width; responsiveness is on you. And keep your OUTERMOST horizontal padding near zero (0–4px): the host frame already insets the page ~10px from the phone's screen edge, so heavy wrapper padding makes the dashboard read narrower than the platform's own chrome (the composer card sits at 12px). In grid/flex layouts use FIXED gaps and padding (`px`/`rem`, never `%`) — percentage row-gaps in auto-height grids mis-resolve and make content overlap. And keep the html LEAN: every KB is time the user spends watching you generate — aggregate data before embedding it, skip decoration that the tokens CSS already provides.

**Styling judgment**: load Tailwind BY DEFAULT and compose utilities over the token vars (`bg-[var(--p-surface)]`, `text-[var(--p-primary)]`) so custom styling stays native in both themes — the polish is what makes artifacts impressive, and the modest extra output tokens are worth it. Skip it only for the simplest cases — a single plain card, table, or chart — where the token primitives alone already look native and generate fastest.

**Theme contract**: the frame sets `.dark` on `<html>` and fires a `otodock:theme` window event on live theme switches. Read colors from the CSS vars, and re-render JS-drawn visuals on that event. The ECharts idiom:

```html
<div class="card"><h3>Weekly sessions</h3><div id="c" style="height:320px"></div></div>
<script src="/ui-kit/echarts.min.js"></script>
<script>
  function cssVar(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function render(){
    var chart = echarts.init(document.getElementById('c'));
    chart.setOption({
      textStyle:{ color: cssVar('--p-text') },
      xAxis:{ type:'category', data:['Mon','Tue','Wed'], axisLabel:{color: cssVar('--p-text-secondary')} },
      yAxis:{ type:'value', splitLine:{lineStyle:{color: cssVar('--p-border-light')}} },
      series:[{ type:'bar', data:[120,132,101], itemStyle:{color: cssVar('--p-primary')} }]
    });
    window.__c = chart;
  }
  render();
  addEventListener('resize', function(){ window.__c && window.__c.resize(); });
  addEventListener('otodock:theme', function(){ window.__c.dispose(); render(); });
</script>
```

**3D (`/ui-kit/three.min.js`)**: global `THREE` including a curated addon set — `OrbitControls` + `MapControls`, the post-processing chain (`EffectComposer`, `RenderPass`, `UnrealBloomPass`, `OutputPass`, `ShaderPass`), fat lines (`Line2`, `LineGeometry`, `LineMaterial`) and `RoundedBoxGeometry`. A living 3D scene is the kit's signature move for dashboards that should IMPRESS: an animated hero panel above the numbers, a glowing service graph, a slowly rotating data sculpture, a floor plan you can pan. Reach for it whenever motion or space adds real life to a dashboard — but don't force it on a surface that is honestly just a bar chart. The sandbox sets HARD limits, so design within them from the start:

- **No asset loaders.** `connect-src` is 'none' — `GLTFLoader`, `FileLoader`, `OBJLoader` and every fetch/XHR-based loader fails silently-looking. No model files: build scenes from procedural geometry (`BoxGeometry`, `SphereGeometry`, `BufferGeometry` from typed arrays). `TextureLoader` works ONLY with `data:`/`blob:` URLs — canvas-generated `CanvasTexture` is the idiom.
- **No workers, no WASM** (DRACO/KTX2/meshopt decoders are out), **no WebXR**. Plain WebGL/WebGL2 rendering works fine.
- Guard init: `new THREE.WebGLRenderer()` inside try/catch with an HTML fallback message — some WebViews have no WebGL.
- **The canvas is fluid**: width follows the container, height comes from a CLAMPED style — `height:clamp(240px, 36vh, 460px)` is the house default for a hero panel. NEVER a bare `aspect-ratio` (a 16/6 hero that looks right on a laptop grows monstrous on a 32" desktop — the 2D cards around it stay CSS-px sized, and the 3D must too). A `resize` handler is MANDATORY — update renderer size, `camera.aspect` + `updateProjectionMatrix()`, and (when used) `composer.setSize(w,h)` and every `LineMaterial.resolution` (see below). The frame resizes when the tab or device does; a 3D panel that skips this renders stretched or letterboxed.
- **Frame for physical size, not screen fraction**: on a big canvas, pull the camera back so objects read laptop-sized instead of scaling up with the panel — in the resize handler, scale the camera's distance by `Math.min(1.5, Math.max(1, Math.sqrt(w * h / (1100 * 380))))` (reference ≈ a laptop-width hero). The platform's own 3D company map frames this way; a scene that skips it is the #1 "looks great on my laptop, huge on the office monitor" complaint.
- Theme: read colors from the CSS vars like the ECharts idiom; on `otodock:theme` update materials in place (`mat.color.set(cssVar(...))`) — no renderer rebuild.
- Mobile: touch just works on both control types — `OrbitControls` for orbit-a-scene, `MapControls` for pan-first surfaces (floor plans, boards). Cap `setPixelRatio(Math.min(devicePixelRatio, 2))`, scale object counts down on small screens (`matchMedia('(max-width: 640px)')`), and honor `prefers-reduced-motion`: keep interactivity but skip self-animation.

The impressive idioms, all sandbox-safe:

- **Bloom glow** — emissive materials + `UnrealBloomPass` (strength ~0.4–0.8) is the single biggest visual lever; it's what makes dark dashboards look alive.
- **Fat lines** — network edges and flows drawn with `Line2` + `LineMaterial({ linewidth: 2-4 })`, impossible with 1px `Line`. FOOTGUN: `LineMaterial` needs `material.resolution.set(w, h)` at init AND in the resize handler, or the lines render at the wrong width.
- **Instancing** — hundreds+ of similar objects go in ONE `InstancedMesh` (per-instance color via `setColorAt`), never N meshes; this is the difference between 60fps and a slideshow.
- **Data textures** — draw heatmaps, gradients and labels onto an offscreen canvas and wrap it in `CanvasTexture`; that plus procedural geometry replaces every blocked loader.

```html
<div class="card"><div id="v" style="width:100%;height:clamp(240px,36vh,460px)"></div></div>
<script src="/ui-kit/three.min.js"></script>
<script>
  var el = document.getElementById('v');
  try {
    var renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    el.appendChild(renderer.domElement);
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
    var CAM_DIR = new THREE.Vector3(0, 2, 6);
    camera.position.copy(CAM_DIR);
    function cssVar(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
    var mat = new THREE.MeshStandardMaterial({ color: cssVar('--p-primary'),
      emissive: cssVar('--p-primary'), emissiveIntensity: 0.35 });
    var mesh = new THREE.Mesh(new THREE.RoundedBoxGeometry(2, 2, 2, 4, 0.25), mat);
    scene.add(mesh);
    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
    var key = new THREE.DirectionalLight(0xffffff, 1.6); key.position.set(3, 5, 4); scene.add(key);
    var composer = new THREE.EffectComposer(renderer);
    composer.addPass(new THREE.RenderPass(scene, camera));
    composer.addPass(new THREE.UnrealBloomPass(new THREE.Vector2(1, 1), 0.55, 0.8, 0.75));
    composer.addPass(new THREE.OutputPass());
    var controls = new THREE.OrbitControls(camera, renderer.domElement);
    function size(){ var w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h); composer.setSize(w, h);
      camera.aspect = w / h; camera.updateProjectionMatrix();
      // Physical-size framing: bigger canvas → camera farther back.
      var vs = Math.min(1.5, Math.max(1, Math.sqrt(w * h / (1100 * 380))));
      camera.position.copy(CAM_DIR).multiplyScalar(vs); }
    size(); addEventListener('resize', size);
    addEventListener('otodock:theme', function(){
      mat.color.set(cssVar('--p-primary')); mat.emissive.set(cssVar('--p-primary')); });
    var still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    (function tick(){ requestAnimationFrame(tick);
      if (!still) mesh.rotation.y += 0.004;
      controls.update(); composer.render(); })();
  } catch (e) { el.textContent = '3D not available on this device.'; }
</script>
```

The Edit-then-refresh loop (edit the saved file, re-call `display_ui` with only `save_path`) is described in the display-tools skill; the same loop works for mini-apps — edit `apps/<slug>.html`, then re-pin the slug (`pin_app` with no `html`) — open tabs live-reload.

## Pinned mini-apps (`pin_app` / `unpin_app` / `list_apps`)

A mini-app is a STANDING dashboard, pinned once and opened any time from the apps button on the chat page — it outlives every chat. Pin one when the user wants a recurring surface (morning brief, project status board, finance dashboard, home-control panel); a standing in-chat artifact covers "for this conversation", `pin_app` covers "for every visit". Once pinned, keep it fresh with a scheduled task instead of rebuilding on request.

**Who sees it — `visibility`** (orthogonal to `scope`, which picks WHERE it lives): `visibility="agent"` pins ONE shared dashboard for every user of this agent (its file lives in the shared `workspace/apps/`; needs editor+ when a human drives the session); `visibility="user"` pins the current user's personal dashboard (`users/<u>/workspace/apps/`). Omitted, it follows the agent's mode default — the tool schema shows this agent's default. Match intent: "pin this for the whole team" → `"agent"`; "just for me" → `"user"`. The same slug may exist in BOTH visibilities (two rows, two files) — re-pin with the SAME visibility to update; `unpin_app` auto-detects unless the slug is in both (then pass `visibility`). Users can hide a shared app from their OWN strip without affecting the team — that per-user hide is theirs, not yours; a re-pin never un-hides it for them.

- **Authoring is the same sandbox as `display_ui`** (body fragment, tokens CSS auto-loaded, kit scripts available) with two HARD requirements because apps are seen daily on every device: load Tailwind and design mobile-responsive.
- **Updating**: re-pin the same slug (check `list_apps` first) — open tabs live-reload; that is also how a scheduled task refreshes an app every morning. Editing the workspace file alone does not notify open tabs.
- **Restoring after an unpin — never rebuild**: the dashboard's X only HIDES an app (`list_apps` shows the slug marked `unpinned`); `pin_app(slug)` alone brings it back with its file, actions and approval intact. Even after your own `unpin_app` (the hard remove), the workspace file survives at `apps/<slug>.html` — `pin_app(slug, actions=[…])` re-registers over it without resending html.
- **Action buttons** — declared, never free-form. Declare the manifest in `actions`; the USER must approve it before any button works (the pin ack tells you `pending user approval` — say so). In the app, invoke by id:

```html
<button class="btn primary" onclick="otodock.action('refresh')">Refresh now</button>
<button class="btn" onclick="otodock.action('analyze', {month: '2026-03'})">Analyze March</button>
```

  - `{"id":"refresh","label":"Refresh now","type":"fire_task","task_id":"<id>"}` fires an existing task VERBATIM (no args). Create a `task_type='trigger'` task (schedules-mcp) as the canonical button target — a `one_time` task would delete itself after the first click and is rejected. Add an `args_schema` (below) and the validated args substitute into the task's `{{placeholders}}` at fire time.
  - `{"id":"analyze","label":"Analyze month","type":"send_prompt","prompt":"Analyze my {{month}} spending"}` delivers the prompt into the chat the user has open (framed as an app action, like the artifact backchannel; on the front page it starts a new chat). `{{key}}` placeholders fill from the `otodock.action` args.
  - `{"id":"lights","label":"Toggle office lights","type":"mcp_tool","mcp":"ha-mcp","tool":"toggle_light","fixed_args":{"entity":"light.office"},"args_schema":{"type":"object","properties":{"brightness":{"type":"integer","minimum":0,"maximum":255}}}}` calls ONE tool on one of YOUR OWN MCPs directly — no agent turn, no token spend: the click IS the tool call, and its result comes back to the page. Use it for instant controls (toggle a device, refresh a value); use `fire_task`/`send_prompt` when the work needs an agent thinking. `mcp` is the namespace segment from your own tool names (`mcp__display__…` → `"display"`); local MCPs only — device/satellite MCPs are rejected. `fixed_args` are baked in verbatim (the page can never change them); `args_schema` is a FLAT object of scalar props gating what the page may pass — every string needs `maxLength` (or `enum`), keys must not overlap `fixed_args`, and anything outside the schema is refused server-side. Schema-less actions accept NO args.
  - Acks arrive as the same `otodock:action-ack` window event (`sent` / `queued` / `denied` / `unavailable`; mcp_tool resolves to `done` / `error` when the tool finishes) — reflect button state.
  - mcp_tool results ALSO arrive as an `otodock:action-result` window event: `detail = {id, ok, result}` (result is the tool's text output, truncated at 32KB). Render it into the page — e.g. update a status badge or value the button controls. EVERY mcp_tool invocation ends in exactly one action-result — refusals too (`ok:false` with the reason as `result`: rate-limited, network error, tool unavailable), so keying ALL busy/spinner state off this event is safe and correct.
  - **Busy state is REQUIRED on every mcp_tool control**: the first call after a quiet period warms the tool's MCP (up to ~10s), and a control that looks dead gets pressed again — for a TOGGLE that means fire-twice-back-to-original. On press: disable the control + show a spinner/label change immediately; re-enable on the `otodock:action-result` for that id (an `ok:false` result still terminates the call — show its `result` text briefly). One in-flight call per action+args is enforced server-side — the disabled state is how the user sees it.
  - **Controls beyond buttons — sliders, selects, steppers**: any value control maps naturally to an mcp_tool action with the value declared in `args_schema` (e.g. a brightness slider → `{"type":"mcp_tool","mcp":"ha-mcp","tool":"set_brightness","fixed_args":{"entity":"light.desk"},"args_schema":{"type":"object","properties":{"brightness":{"type":"integer","minimum":0,"maximum":255}},"required":["brightness"]}}`). Fire on **`change`** (release), never on `input` while dragging — rate limits eat mid-drag floods — and reflect the confirmed value from the action-result, not the optimistic one. Prefer a slider over an on/off button wherever the underlying tool takes a level.

## Scoped dashboards (`pin_app` with `scope`) — the Dock

Besides standing apps, `pin_app` can bind a dashboard to a **chat** or a **delegation project** — it then lives on that chat's **Dock** (the panel button by the composer) instead of the apps strip. (`scope` picks WHERE the pin lives — the standing strip vs a Dock; `visibility` picks WHO sees it — don't conflate them. Multi-user project dashboards want `visibility="agent"` so every participant sees the pin.)

- `scope="project"` — pin it from any chat of the project (ids resolve from YOUR session's chat, never passed). It renders beside the platform's live lane cards on the project view. **Default to NOT pinning one**: the platform's built-in delegation dock (orchestrator card + live lane cards) is the standard surface for every delegation, projects included. Reserve a pinned project dashboard for genuinely BIG projects — many lanes over hours/days where a plan overview with owners and progress bars adds something the lane cards don't. If you do pin one, keep it about the PLAN (the lane cards already show live per-worker state) and re-pin on every board change (the same Edit-the-file + slug-only re-pin loop).
- `scope="chat"` — one progress dashboard for THIS chat. Use it for plan-scale single-chat work (a dev plan being executed, a research program, a long migration): milestones done/remaining, current phase, key numbers. Update it at milestones, not every message.
- **One dashboard per chat/project — a scoped re-pin REPLACES it** (that's the point: you own the scope's dashboard; approval carries over when the actions manifest is unchanged). The slug can stay the same across updates; `list_apps` shows scoped pins tagged `chat-scoped`/`project-scoped`.
- Scoped dashboards die with their scope (chat deleted / last project chat deleted) — right for progress views. Anything the user should keep across chats belongs in a standing app instead.
- Authoring rules are identical to standing apps (Tailwind + mobile-responsive REQUIRED, declared actions, live `mcp_tool` data panels).

## File pins (`pin_file`) — living documents on the Dock

`pin_file(path, scope="chat"|"project")` pins an **existing workspace text file** to the Dock as a read-only row: collapsed by default, expand → rich markdown render that **live-updates as you edit the file** — zero upkeep, no re-pin loop, no HTML. This is the RIGHT tool for living documents; **never build a mini-app just to display a file**:

- Prime use: pin the **plan file** to the project Dock (`pin_file("projects/<id>/plan.md", scope="project")`) so the user reads the current plan next to the live lane cards. Specs, meeting notes, reports, a lane's findings file — same pattern.
- Path is workspace-relative and must exist (the Dock renders it, it can't create it). `.md` renders rich; other text types render as plain code. Up to 6 pins per chat/project; re-pin the same path to retitle; `unpin_file(path)` removes the row (the file stays).
- Division of labor on a project Dock: the **board file** block is automatic (lanes/decisions), file pins carry the DOCUMENTS, a pinned dashboard (rare, big projects only) carries interactive views.
- On a remote machine the platform mirror renders — your edits appear after the end-of-turn sync, not mid-turn.

**Platform live feeds (`otodock.feed`) — live STATE without re-pins.** Declare a feed in `actions` — `{"id":"lanes","label":"Live lane status","type":"data_feed","feed":"project_lanes"}` — and subscribe in-page; the host pushes an initial snapshot plus every change, each viewer seeing their own permission-filtered slice (read-only; covered by the same one-time approval as buttons):

```html
<script>
otodock.feed('project_lanes', function(rows, err){
  if (err) { document.getElementById('lanes').textContent = err; return; }
  // rows: [{id, title, agent, delegate_role, status: generating|awaiting_user|idle, updated_at}]
  renderLanes(rows);
});
</script>
```

Two feeds exist: `project_lanes` (rows above — only flows on a project dock) and `active_chats` (`{id, agent, title, phase: streaming|warming|finished}` — the user's active chats across agents, works on any app). NOTE: the dock itself always renders the live lane cards at the top — a pinned project dashboard must NOT duplicate a plain lane list under them. Use `project_lanes` only when the dashboard presents lane state in a genuinely different shape (per-lane progress against the plan, phase grouping, burndown) — otherwise skip the feed and let the built-in cards do their job. Division of labor: the feed carries live STATE, the Edit + re-pin loop carries CONTENT changes (plan/board edits). Feeds are subscriptions, not buttons — `otodock.action()` on a feed id is refused.

**Live data panels — refresh WITHOUT an agent turn (the DEFAULT for data).** When a dashboard's numbers come from your MCP tools (monitor statuses, device states, metrics, task lists), do NOT wire "Refresh" to `send_prompt`/`fire_task` — declare the queries as `mcp_tool` actions and let the PAGE call them and render the results itself: on load (fresh data on every visit, automatically) and from the same Refresh button. Zero agent turns, zero tokens, seconds instead of a whole conversation. Reserve `fire_task`/`send_prompt` for refreshes that genuinely need YOU — reasoning over the data, web research, rebuilding the page layout.

- **NEVER bake tool values into the page as constants.** A `const data = [...]` filled with the numbers you just fetched is a snapshot — stale the moment it's pinned, and "Refresh via agent re-pin" burns a whole agent turn to update what one direct tool call returns in seconds. If a number came from an MCP tool, the page must FETCH it: fire the declared `mcp_tool` action on load and render from its `otodock:action-result`. Declaring the actions in the manifest is NOT enough — a page that never calls `otodock.action(...)` has dead actions and fake data.
- **Parsing tool output in the page**: you already called these tools in this conversation — you KNOW their exact output text. Write the page's parser against that real output (JSON.parse when it's JSON, line/regex extraction when it's prose). When parsing fails, render the raw result text in place — never keep showing the previous numbers as if they were fresh. CAVEAT for agents running on a remote machine: your session's MCPs run THERE while app buttons run the platform-side install — the same tool can return a slightly different shape (version drift). Write shape-TOLERANT parsers (e.g. accept a bare array AND a `{items:[…]}` wrapper) — the raw-text fallback then makes any residual mismatch diagnosable in one glance.
- A `send_prompt` "deep refresh"/"analyze" button may COEXIST with the live queries — but the default Refresh (and the on-load fill) must be the direct `mcp_tool` calls.

```html
<script>
function refresh(){
  otodock.action('monitors');   // mcp_tool → uptime summary
  otodock.action('metrics');    // mcp_tool → prometheus query
}
addEventListener('otodock:action-result', function(e){
  if (!e.detail.ok) return;     // keep the last good data on errors
  if (e.detail.id === 'monitors') renderMonitors(e.detail.result);
  if (e.detail.id === 'metrics') renderMetrics(e.detail.result);
});
addEventListener('load', refresh);   // auto-refresh on every open
</script>
```

  Rules: the frame itself has NO network access — `otodock.action` on a declared `mcp_tool` action IS the bridge to your MCPs, so never conclude a live data panel is impossible because the sandbox blocks networking. Auto-firing on load is allowed ONLY for `mcp_tool` (never auto-fire `send_prompt`/`fire_task` — those cost real agent turns and stay user-gesture-only); pin the LAST KNOWN data into the HTML so the page is never empty, and let the on-load results overwrite it; results are the tool's text output — parse it (ask the tool for JSON where it supports it). Rate limits key on the action PLUS its args: one parameterized action can serve a whole control panel (different widgets = different args = independent calls), while an identical repeat within ~1s is refused — fire each query once per refresh, not in loops.
