/**
 * The interactive terminal's bracketed-paste state across a scrollback
 * replay. xterm wraps a paste in `ESC[200~ … ESC[201~` only while its own
 * tracked mode is on, which it learns from the TUI's `ESC[?2004h`, and both
 * TUIs (Claude Code, Codex) send that once, at start, then keep the mode on.
 * A replay starts a fresh mirror (a new xterm, or `term.reset()`), so once
 * the proxy's ring has trimmed the session's start away the mirror pastes
 * unbracketed: every line break goes out as a bare CR and the TUI submits
 * the first lines as a prompt.
 */

// The proxy keeps the last 256 KB of output (DEFAULT_SCROLLBACK_BYTES in
// proxy/core/sandbox/pty_relay.py and core/remote/remote_pty.py) as whole PTY
// reads of up to 64 KB (_READ_CHUNK there and in the satellite's relays). A
// replay under 256 - 64 KB therefore still holds the session's first bytes:
// no toggle in it means a TUI that has not turned the mode on yet, and it
// must stay off. Change this with either side.
export const TRIMMED_REPLAY_BYTES = (256 - 64) * 1024

export const PASTE_MODE_ON = '\x1b[?2004h'

const TOGGLE = /\x1b\[\?(?:\d+;)*2004(?:;\d+)*[hl]/

/** What to write into the fresh mirror BEFORE a replay's own bytes: the
 * mode the live TUI holds when the ring has trimmed its toggle away, else
 * nothing (a toggle inside the replay is xterm's to parse, so an explicit
 * `?2004l` still wins). Before, never after: a replay can end inside an
 * escape sequence, which an appended ESC would cut short. */
export function pasteModePrefix(replay: Uint8Array): string {
  if (replay.length < TRIMMED_REPLAY_BYTES) return ''
  return TOGGLE.test(new TextDecoder('latin1').decode(replay)) ? '' : PASTE_MODE_ON
}
