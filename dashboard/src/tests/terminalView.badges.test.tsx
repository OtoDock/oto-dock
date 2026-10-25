/**
 * The terminal's badges: the chat's mode shows only in Don't Ask (the TUI's
 * own mode line names Ask, Auto and Plan; a Don't Ask chat's CLI footer
 * reads "manual mode"), and every badge sits in one column so none covers
 * another (the read-only one carries its Take over button).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import TerminalView from '@/components/chat/terminal/TerminalView'
import { WIRE, type WireFrame, type WireType } from '@/api/wireEvents'
import { DONT_ASK_CHIP } from '@/lib/engines/modes'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function makeWs() {
  const subs = new Map<string, (m: WireFrame) => void>()
  return {
    subs,
    subscribe: (t: WireType, fn: (m: WireFrame) => void) => {
      subs.set(t, fn)
      return () => { subs.delete(t) }
    },
    sendPtyAttach: vi.fn(),
    sendPtyInput: vi.fn(),
    sendPtyResize: vi.fn(),
    sendPermission: vi.fn(),
    sendPtyTakeover: vi.fn(),
  }
}

const artifacts = { windows: [], minimized: [], close: vi.fn(), minimize: vi.fn() } as never

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ObserverStub)
  // jsdom has no canvas: the xterm renderer probes one at open.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
    dispatchEvent: () => false,
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('TerminalView badges', () => {
  it("shows only Don't Ask (a task's unattended word too), none in Ask, Auto or Plan", () => {
    for (const [mode, shown] of [
      ['ask', false], ['default', false], ['autoReview', false], ['acceptEdits', false],
      ['dontAsk', true], ['auto', true], ['plan', false], [undefined, false],
    ] as const) {
      const { unmount } = render(
        <TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} permissionMode={mode} />,
      )
      const badge = screen.queryByTestId('terminal-mode')
      if (shown) expect(badge?.getAttribute('aria-label')).toMatch(/^Don't Ask:/)
      else expect(badge).toBeNull()
      unmount()
    }
  })

  it.each([1280, 390])('at %i px a live terminal shows the badge in Don\'t Ask alone', (width) => {
    vi.stubGlobal('innerWidth', width)
    vi.stubGlobal('matchMedia', (q: string) => ({
      // The dashboard's mobile queries are max-width ones.
      matches: /max-width/.test(q) ? width < 640 : !/max-width/.test(q) && width >= 640,
      media: q, onchange: null,
      addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
      dispatchEvent: () => false,
    }))
    const seen = (['ask', 'autoReview', 'dontAsk'] as const).map((mode) => {
      const ws = makeWs()
      const { unmount } = render(<TerminalView ws={ws} chatId="c1" artifacts={artifacts} permissionMode={mode} />)
      // A live terminal: its attach acknowledged.
      act(() => { ws.subs.get(WIRE.PTY_STATUS)?.({ type: WIRE.PTY_STATUS, chat_id: 'c1', state: 'attached' } as WireFrame) })
      const badge = screen.queryByTestId('terminal-mode')
      unmount()
      return [mode, badge !== null]
    })
    expect(seen).toEqual([['ask', false], ['autoReview', false], ['dontAsk', true]])
  })

  it("draws Don't Ask as the status bar's chip, the bolt alone, named for a screen reader", () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} permissionMode="dontAsk" />)
    const badge = screen.getByRole('img', { name: /^Don't Ask: this chat runs every tool without asking/ })
    expect(badge).toBe(screen.getByTestId('terminal-mode'))
    expect(badge.textContent).toBe('')
    expect(badge.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true')
    for (const cls of [DONT_ASK_CHIP.bg, DONT_ASK_CHIP.border, DONT_ASK_CHIP.text, 'w-7', 'h-7', 'rounded-control']) {
      expect(badge.classList.contains(cls)).toBe(true)
    }
  })

  it('takes focus, and the focus shows its note', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} permissionMode="dontAsk" />)
    const badge = screen.getByTestId('terminal-mode')
    expect(badge.tabIndex).toBe(0)
    act(() => { badge.focus() })
    expect(document.activeElement).toBe(badge)
    expect(screen.getByRole('tooltip').textContent).toMatch(/^Don't Ask: this chat runs every tool without asking/)
    // Its label already says the note: no second reading as a description.
    expect(badge.getAttribute('aria-describedby')).toBeNull()
  })

  it('keeps the mode pill and the transport badges in one column', () => {
    const ws = makeWs()
    render(<TerminalView ws={ws} chatId="c1" artifacts={artifacts} permissionMode="dontAsk" />)
    act(() => {
      ws.subs.get(WIRE.PTY_STATUS)?.({ type: WIRE.PTY_STATUS, chat_id: 'c1', state: 'read_only', controller: 'ana' } as WireFrame)
      ws.subs.get(WIRE.PTY_STATUS)?.({ type: WIRE.PTY_STATUS, chat_id: 'c1', state: 'reconnecting' } as WireFrame)
    })
    const pill = screen.getByTestId('terminal-mode')
    const column = pill.closest('div.flex-col')
    expect(column).not.toBeNull()
    expect(column?.textContent).toContain('ana is driving')
    expect(column?.textContent).toContain('reconnecting')
    expect(screen.getByRole('button', { name: 'Take over' })).toBeInTheDocument()
  })
})
