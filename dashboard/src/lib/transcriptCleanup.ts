// Render-side cleanup for persisted user rows — the stored data is never
// touched (the model's context keeps the full text; only the display hides it).
//
// Two noise classes land in `chat_messages` via the interactive transcript
// tailers (headless turns persist the user's original text, so they are clean):
//
// 1. Injected prompt preludes — the platform stamps `[Current time: ...]` onto
//    interactive prompts (dashboard-side `withInteractiveTime`, and the same
//    header on re-warms / mode toggles). The tailer persists the PTY-typed text
//    verbatim, so the stamp renders at the top of the user bubble.
// 2. Slash-command records — Claude Code writes local command invocations and
//    their output into the transcript as user messages wrapped in
//    `<command-name>/<command-message>/<command-args>/<local-command-stdout>`
//    tags, and `/context` reports as a raw `## Context Usage` markdown dump.
//
// Matching is deliberately conservative: preludes strip only at the very start
// of the message in the exact injected shape, and command noise is recognized
// only when the message STARTS with a wrapper tag (or is a structural
// `## Context Usage` report) — pasted markdown or quoted tags mid-message are
// never hidden.

// `[Current time: Friday, March 21, 2026 09:30 (9:30 AM) Europe/Athens
// (UTC+03:00)]` followed by blank line(s). Repeats fold (stacked re-warm stamps).
const TIME_PRELUDE_RE = /^\[Current time: [^\]\n]{1,160}\][ \t]*(?:\r?\n+|$)/

// One wrapped slash-command segment (opening tag first, matching close tag).
const COMMAND_TAG_RE =
  /<(command-name|command-message|command-args|local-command-stdout)>[\s\S]*?<\/\1>/g
const COMMAND_TAG_START_RE =
  /^<(command-name|command-message|command-args|local-command-stdout)>/

/** Strip leading injected `[Current time: ...]` prelude line(s). */
export function stripInjectedPreludes(text: string): string {
  let out = text
  while (TIME_PRELUDE_RE.test(out)) {
    out = out.replace(TIME_PRELUDE_RE, '')
  }
  return out
}

/**
 * Clean a persisted user row's text for display. Returns the cleaned text, or
 * `null` when nothing user-authored remains (pure command noise / bare stamp)
 * and the row should be hidden entirely.
 */
export function cleanUserMessageText(text: string): string | null {
  const stripped = stripInjectedPreludes(text ?? '')
  const trimmed = stripped.trim()
  if (!trimmed) {
    // Original had content but nothing user-authored survives → hide; a row
    // that was genuinely empty stays empty (caller renders as before).
    return (text ?? '').trim() ? null : stripped
  }
  // Slash-command record: only when the message STARTS with a wrapper tag.
  if (COMMAND_TAG_START_RE.test(trimmed)) {
    const remainder = trimmed.replace(COMMAND_TAG_RE, '').trim()
    return remainder || null
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
