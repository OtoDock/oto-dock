// Render-side cleanup for persisted user rows — the stored data is never
// touched (the model's context keeps the full text; only the display hides it).
//
// Three noise classes land in `chat_messages` via the interactive transcript
// tailers (headless turns persist the user's original text, so they are clean)
// and one rides every delegated prompt, headless or not:
//
// 1. Injected prompt preludes — the platform stamps `[Current time: ...]` onto
//    interactive prompts (dashboard-side `withInteractiveTime`, and the same
//    header on re-warms / mode toggles). The tailer persists the PTY-typed text
//    verbatim, so the stamp renders at the top of the user bubble.
// 2. Slash-command records — Claude Code writes local command invocations and
//    their output into the transcript as user messages wrapped in
//    `<command-name>/<command-message>/<command-args>/<local-command-stdout>`
//    tags, and `/context` reports as a raw `## Context Usage` markdown dump.
// 3. The Claude TUI's paste tags — a multi-line prompt typed into the terminal
//    is journaled inside `<pasted_content id="…">…</pasted_content>`, the
//    preludes inside the tags.
// 4. The platform's wrappers around a prompt another agent or the platform
//    sent: the delegation MCP's `[DELEGATED_WORK]` and `[TASK_OUTPUT_DIR]`
//    blocks ahead of a worker's task, and the `[BACKGROUND_TASK_RESULT]`
//    envelope of a worker's report typed into a terminal (its `delegate_result`
//    row is the card that shows it, so the typed copy is not drawn at all).
//
// Matching is deliberately conservative: preludes, tags and wrappers strip only
// at the very start of the message in the exact injected shape (a wrapper must
// be closed), and command noise is recognized only when the message STARTS
// with a wrapper tag (or is a structural `## Context Usage` report) — pasted
// markdown or quoted tags mid-message are never hidden. The shapes are pinned
// by `proxy/tests/fixtures/chat_wrappers.json`, which
// tests/transcriptCleanup.test.ts reads case by case.
//
// Linear on any input: every viewer of a chat runs this on every person's
// row, a shared chat's rows included. No pattern searches with an unbounded
// run ahead of its literal, and a block that can repeat is removed by an
// indexOf scan (removeTagBlocks), never by a lazy any-character run
// searched from every position.

// `[Current time: Friday, March 21, 2026 09:30 (9:30 AM) Europe/Athens
// (UTC+03:00)]` followed by blank line(s), the viewer focus line
// `[The user is looking at the app "Title" (slug) right now.]` that rides
// interactive sends the same way, and the parallel-activity line
// (`[Parallel activity — … (peek_session / get_task_result).]`) a task run's
// first prompt and server-delivered prompts carry. A satellite single-lines
// a launch-argument prompt, so a prelude may end in spaces instead of a line
// break. Repeats fold (stacked re-warm stamps).
const TIME_PRELUDE_RE =
  /^\[(?:(?:Current time: |The user is looking at the app )[^\]\n]{1,200}|Parallel activity — [^\n]{0,2000}?\(peek_session \/ get_task_result\)\.|Meeting "[^\n]{0,4000}? you have only this note\.)\](?:[ \t]*(?:\r?\n+|$)|[ \t]+)/
// The same shape matched at an index (sticky): stacked preludes are walked
// by index and the text is sliced once.
const TIME_PRELUDE_AT_RE = new RegExp(TIME_PRELUDE_RE.source.slice(1), 'y')

// Claude Code's record of a local slash command: rows wrapped in these tags.
const COMMAND_TAGS = ['command-name', 'command-message', 'command-args', 'local-command-stdout']
const COMMAND_TAG_START_RE =
  /^<(command-name|command-message|command-args|local-command-stdout)>/

// Platform-injected context blocks (e.g. the duplex chat context prepended to
// the first spoken turn after each attach — `ws/duplex_attach.py::_build_prompt`).
// The interactive tailer persists the PTY-typed prompt verbatim, so the block
// renders inside the user's own bubble on interactive-terminal chats.
const SYSTEM_REMINDER_TAGS = ['system-reminder']

// The image marks the Claude TUI journals ahead of a paste whose photo paths
// it took in as images (`[Image #1] [Image #2]`): a leading run is dropped so
// the paste and the preludes behind it are found. Anchored at the start.
const IMAGE_MARKS_RE = /^(?:\[Image #\d{1,6}\]\s*)+/

// The Claude TUI's paste tags: only a LEADING open tag with its trailing close
// tag is the TUI's (a tag quoted mid-message stays). The TUI repeats the
// paste's id on the close tag too (`</pasted_content id="5957">`). The close
// is searched without the whitespace ahead of it (that run is trimmed
// after), its attributes bounded.
const PASTED_OPEN_RE = /^\s*<pasted_content\b[^>]*>\s*/
const PASTED_CLOSE_RE = /<\/pasted_content\b[^>]{0,200}>\s*$/

// The delegation MCP's two wrappers around a worker's prompt, outer first;
// only a LEADING, CLOSED one is the platform's.
const DELEGATED_WORK_RE = /^\s*\[DELEGATED_WORK\][\s\S]*?\[\/DELEGATED_WORK\]\s*/
const TASK_OUTPUT_DIR_RE = /^\s*\[TASK_OUTPUT_DIR\][\s\S]*?\[\/TASK_OUTPUT_DIR\]\s*/

// The typed copy of a worker's report (its `delegate_result` row is the card):
// the envelope's header line, or the line a report-less result opens with.
const RESULT_CALLBACK_PREFIXES = ['[BACKGROUND_TASK_RESULT]', '[The worker ended without a report:']

/** Strip leading injected `[Current time: ...]` prelude line(s). */
export function stripInjectedPreludes(text: string): string {
  let pos = 0
  for (;;) {
    TIME_PRELUDE_AT_RE.lastIndex = pos
    const m = TIME_PRELUDE_AT_RE.exec(text)
    if (!m || TIME_PRELUDE_AT_RE.lastIndex === pos) return pos ? text.slice(pos) : text
    pos = TIME_PRELUDE_AT_RE.lastIndex
  }
}

/**
 * `text` with every `<tag>…</tag>` block of the given tags removed, each open
 * ending at its first close: what `text.replace(/<(a|b)>[\s\S]*?<\/\1>/g, '')`
 * answers, in one pass. An open with no close after it ends that tag's scan
 * (a later open cannot find a close either).
 */
export function removeTagBlocks(text: string, tags: readonly string[]): string {
  const nextOpen = new Map(tags.map((t) => [t, text.indexOf(`<${t}>`)]))
  let out = ''
  let kept = 0
  for (;;) {
    let at = -1
    let tag = ''
    for (const [t, i] of nextOpen) {
      if (i >= 0 && (at < 0 || i < at)) { at = i; tag = t }
    }
    if (at < 0) break
    const end = text.indexOf(`</${tag}>`, at + tag.length + 2)
    if (end < 0) { nextOpen.set(tag, -1); continue }
    out += text.slice(kept, at)
    kept = end + tag.length + 3
    for (const [t, i] of nextOpen) {
      if (i >= 0 && i < kept) nextOpen.set(t, text.indexOf(`<${t}>`, kept))
    }
  }
  return out + text.slice(kept)
}

/** The text inside the TUI's paste tags when they wrap the whole message. */
function stripPastedTags(text: string): string {
  const opened = PASTED_OPEN_RE.exec(text)
  if (!opened || !PASTED_CLOSE_RE.test(text)) return text
  const rest = text.slice(opened[0].length)
  const closed = PASTED_CLOSE_RE.exec(rest)
  return closed ? rest.slice(0, closed.index).trimEnd() : rest
}

/** The delegation MCP's leading, closed wrappers dropped. */
export function stripPlatformWrappers(text: string): string {
  return text.replace(DELEGATED_WORK_RE, '').replace(TASK_OUTPUT_DIR_RE, '')
}

/**
 * Clean a persisted user row's text for display. Returns the cleaned text, or
 * `null` when nothing user-authored remains (pure command noise / bare stamp /
 * the typed copy of a worker's report) and the row should be hidden entirely.
 */
export function cleanUserMessageText(text: string | null | undefined): string | null {
  let stripped = stripInjectedPreludes(stripPastedTags((text ?? '').replace(IMAGE_MARKS_RE, '')))
  // Injected <system-reminder> blocks: stripped only when the message STARTS
  // with one (the injected shape — the block is prepended to the user's
  // text), same start-anchor discipline as command tags below so a pasted or
  // quoted fragment mid-message is never hidden. Preludes run again after —
  // a time stamp that sat between the block and the user's text becomes
  // leading once the block is gone.
  if (stripped.trimStart().startsWith('<system-reminder>')) {
    stripped = stripInjectedPreludes(removeTagBlocks(stripped, SYSTEM_REMINDER_TAGS).trimStart())
  }
  // The platform's wrappers sit behind the preludes; a prelude behind them
  // (the parallel-activity line of a task's first prompt) becomes leading.
  stripped = stripInjectedPreludes(stripPlatformWrappers(stripped))
  const trimmed = stripped.trim()
  if (!trimmed) {
    // Original had content but nothing user-authored survives → hide; a row
    // that was genuinely empty stays empty (caller renders as before).
    return (text ?? '').trim() ? null : stripped
  }
  if (RESULT_CALLBACK_PREFIXES.some((p) => trimmed.startsWith(p))) {
    return null
  }
  // Slash-command record: only when the message STARTS with a wrapper tag.
  if (COMMAND_TAG_START_RE.test(trimmed)) {
    return removeTagBlocks(trimmed, COMMAND_TAGS).trim() || null
  }
  // /context report: structural match, not just the heading.
  if (trimmed.startsWith('## Context Usage') && /\*\*Tokens:\*\*/.test(trimmed)) {
    return null
  }
  // Harness-injected background task-notification (bash / subagent
  // completion). The proxy tailer now skips these at persist time; this
  // hides rows persisted before that fix. Start-anchored + closing tag so
  // a pasted fragment mid-discussion is never hidden.
  if (
    trimmed.startsWith('<task-notification>') &&
    trimmed.includes('</task-notification>')
  ) {
    return null
  }
  return stripped
}
