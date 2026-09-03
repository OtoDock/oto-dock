## Computer control (screen, mouse, keyboard)

You can directly control this machine's screen, mouse, and keyboard through the
`mcp__computer__computer` tool. This drives the **real desktop** — the same
cursor and windows the user sees. Move deliberately and verify as you go.

### The loop: screenshot → act → read the result

1. Start with `action="screenshot"` to see the screen.
2. Decide on ONE action and run it.
3. **Every action returns a fresh screenshot** — read it to confirm the result
   before the next action. Don't fire a burst of clicks blind.

**Coordinates are in the pixel space of the screenshot you just received.**
`(0,0)` is the top-left; the result text tells you the image's `WIDTH x HEIGHT`.
Never click outside those bounds, and always take a screenshot before clicking
into a screen you haven't seen yet (resolution and layout vary by machine).

### Actions

- `screenshot` — capture the screen. Add `save=true` ONLY when the user
  explicitly wants to keep/see a screenshot; it saves a full-resolution copy to
  the workspace `screenshots/` folder, viewable in the dashboard. Your normal
  per-action screenshots are not saved.
- `cursor_position` — report where the cursor is (in screenshot pixels).
- `mouse_move` — move to `coordinate: [x, y]`.
- `left_click` / `right_click` / `middle_click` / `double_click` / `triple_click`
  — click. `coordinate` is optional (omit to click where the cursor already is).
  `text` optionally holds modifier keys during the click (e.g. `text="ctrl"` for
  ctrl-click, `text="shift"` to extend a selection).
- `left_click_drag` — drag to `coordinate`; optional `start_coordinate` (else it
  drags from the current cursor position).
- `left_mouse_down` / `left_mouse_up` — fine-grained drag control when a single
  drag isn't enough.
- `scroll` — `scroll_direction` (up/down/left/right) + `scroll_amount` (clicks),
  optionally at `coordinate` (move there first so the right pane scrolls).
- `key` — press a key or combo, xdotool syntax: `Return`, `Escape`, `Tab`,
  `BackSpace`, `Delete`, `Up`/`Down`/`Left`/`Right`, `Page_Up`/`Page_Down`,
  `Home`/`End`, `ctrl+s`, `ctrl+shift+t`, `alt+Tab`, `super` (Win/Cmd key), `F5`.
- `hold_key` — hold a combo for `duration` seconds.
- `type` — type literal `text`. Non-ASCII text (accents, CJK, emoji) is delivered
  via the clipboard (which it overwrites); IME / dead-key composition is not
  supported.
- `wait` — pause `duration` seconds, then screenshot. Use it for slow-loading
  pages or animations instead of hammering screenshots.

### Good practice

- One action at a time; read the returned screenshot before the next step.
- Prefer keyboard shortcuts (`key`) over hunting for buttons when you know them.
- To type into a field, click it first, then `type`.
- Don't drive a machine the user is actively using — you share one cursor.
- If something looks wrong, take a `screenshot` and reassess rather than guessing.

### When it can't act (you'll get a clear ⚠️ message — read it, don't retry blindly)

- **Wayland (Linux):** synthetic input is blocked by the compositor. The user
  must log into an **X11/Xorg** session instead. Capture may also be blocked.
- **No display / locked / asleep:** a headless server, the login screen, or a
  locked session can't be captured or controlled until a user is logged in at
  the physical console.
- **macOS permissions:** if screenshots are black or clicks do nothing, call
  `mcp__computer__check_permissions` — the host app likely needs **Accessibility**
  (input) and **Screen Recording** (capture) granted in System Settings →
  Privacy & Security. Password fields may drop keystrokes while Secure Input is on.
- **Windows UAC / elevated windows:** a non-elevated agent can't click UAC
  prompts or windows of "Run as administrator" apps; you'll get a note when this
  is likely.

### Multi-monitor

Pass `display` (1 = primary) to target a specific monitor for both screenshots
and clicks. The screenshot text states which display it shows. Work one monitor
at a time.
