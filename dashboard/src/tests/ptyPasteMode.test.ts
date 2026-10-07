import { describe, it, expect } from 'vitest'
import { PASTE_MODE_ON, TRIMMED_REPLAY_BYTES, pasteModePrefix } from '@/lib/ptyPasteMode'

// A scrollback replay that the proxy's ring trimmed past the TUI's
// ESC[?2004h gets the mode written back before it; a replay that still holds
// a toggle, or is too short to have been trimmed, is left to xterm.

const enc = new TextEncoder()
function replay(body: string, size = TRIMMED_REPLAY_BYTES + 1024): Uint8Array {
  const pad = 'x'.repeat(Math.max(0, size - body.length))
  return enc.encode(pad + body)
}

describe('pasteModePrefix', () => {
  it('restores the mode when a trimmed replay holds no toggle', () => {
    expect(pasteModePrefix(replay('\x1b[2J redraw'))).toBe(PASTE_MODE_ON)
  })

  it('leaves a replay that still turns it on to xterm', () => {
    expect(pasteModePrefix(replay('\x1b[?2004h'))).toBe('')
  })

  it('lets an explicit turn-off win', () => {
    expect(pasteModePrefix(replay('\x1b[?2004l'))).toBe('')
  })

  it('reads a combined DECSET', () => {
    expect(pasteModePrefix(replay('\x1b[?1004;2004h'))).toBe('')
    expect(pasteModePrefix(replay('\x1b[?2004;1049l'))).toBe('')
  })

  it('never forces a short replay: the TUI may not have turned it on yet', () => {
    expect(pasteModePrefix(replay('', 2048))).toBe('')
    expect(pasteModePrefix(new Uint8Array())).toBe('')
  })

  it('does not take plain text for the sequence', () => {
    expect(pasteModePrefix(replay('mode 2004h and [?2004h without ESC'))).toBe(PASTE_MODE_ON)
  })

  it('reads a non-UTF-8 replay byte for byte', () => {
    const bytes = replay('\x1b[?2004h')
    bytes[0] = 0xff
    expect(pasteModePrefix(bytes)).toBe('')
  })
})
