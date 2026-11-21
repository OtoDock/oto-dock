/**
 * The terminal's badges: no mode badge (the status bar's mode button shows
 * the mode in both views), and every badge sits in one column so none
 * covers another (the read-only one carries its Take over button).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import TerminalView from '@/components/chat/terminal/TerminalView'
import { WIRE, type WireFrame, type WireType } from '@/api/wireEvents'

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
  localStorage.clear()
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
  it('shows no mode badge: the status bar\'s mode button names the mode in both views', () => {
    const ws = makeWs()
    render(<TerminalView ws={ws} chatId="c1" artifacts={artifacts} />)
    act(() => { ws.subs.get(WIRE.PTY_STATUS)?.({ type: WIRE.PTY_STATUS, chat_id: 'c1', state: 'attached' } as WireFrame) })
    expect(screen.queryByTestId('terminal-mode')).toBeNull()
    expect(screen.queryByRole('img', { name: /Don't Ask/ })).toBeNull()
  })

  it('keeps the transport badges in one column', () => {
    const ws = makeWs()
    render(<TerminalView ws={ws} chatId="c1" artifacts={artifacts} />)
    act(() => {
      ws.subs.get(WIRE.PTY_STATUS)?.({ type: WIRE.PTY_STATUS, chat_id: 'c1', state: 'read_only', controller: 'ana' } as WireFrame)
      ws.subs.get(WIRE.PTY_STATUS)?.({ type: WIRE.PTY_STATUS, chat_id: 'c1', state: 'reconnecting' } as WireFrame)
    })
    const takeOver = screen.getByRole('button', { name: 'Take over' })
    const column = takeOver.closest('div.flex-col')
    expect(column).not.toBeNull()
    expect(column?.textContent).toContain('ana is driving')
    expect(column?.textContent).toContain('reconnecting')
  })
})

describe('the limit card under the terminal', () => {
  const card = { type: 'system' as const, subtype: 'turn_ended', reason: 'limit',
                 message: '⚠ Usage limit reached, resets 2099-01-01 06:00 UTC. You\'re out of usage credits.',
                 resetsAt: '2099-01-01T06:00:00+00:00' }

  it('shows the card in its own strip, outside the terminal', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} notice={card} />)
    const strip = screen.getByTestId('terminal-limit-card')
    expect(strip).toHaveTextContent('Usage limit reached, resets 2099-01-01 06:00 UTC.')
    expect(strip.closest('.relative')).toBeNull()            // never inside the terminal's own box
  })

  it('shows nothing without a card', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    expect(screen.queryByTestId('terminal-limit-card')).toBeNull()
  })

  it('has a Hide button a keyboard reaches and a screen reader names', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} notice={card} />)
    const hide = screen.getByRole('button', { name: 'Hide this notice' })
    expect(hide).toHaveAttribute('type', 'button')
    expect(hide.tabIndex).toBe(0)
    hide.focus()
    expect(document.activeElement).toBe(hide)
  })

  it('Hide removes the strip, and a second mount for the same chat and card shows none', () => {
    const first = render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} notice={card} />)
    fireEvent.click(screen.getByRole('button', { name: 'Hide this notice' }))
    expect(screen.queryByTestId('terminal-limit-card')).toBeNull()
    first.unmount()
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} notice={card} />)
    expect(screen.queryByTestId('terminal-limit-card')).toBeNull()
  })

  it('shows a later limit with another reset, and the same card in another chat', () => {
    const view = render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} notice={card} />)
    fireEvent.click(screen.getByRole('button', { name: 'Hide this notice' }))
    const later = { ...card, resetsAt: '2099-01-08T06:00:00+00:00' }
    view.rerender(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} notice={later} />)
    expect(screen.getByTestId('terminal-limit-card')).toBeInTheDocument()
    view.rerender(<TerminalView ws={makeWs()} chatId="c2" artifacts={artifacts} notice={card} />)
    expect(screen.getByTestId('terminal-limit-card')).toBeInTheDocument()
  })

  it('Hide still hides the card for this view when the browser refuses the write, and nothing throws', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError') })
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} notice={card} />)
    expect(() => fireEvent.click(screen.getByRole('button', { name: 'Hide this notice' }))).not.toThrow()
    expect(screen.queryByTestId('terminal-limit-card')).toBeNull()
  })

  it('gives way to the permission card, which still wins', () => {
    const ws = makeWs()
    render(<TerminalView ws={ws} chatId="c1" artifacts={artifacts} notice={card} />)
    act(() => {
      ws.subs.get(WIRE.PTY_PERMISSION)?.({ type: WIRE.PTY_PERMISSION, chat_id: 'c1', session_id: 's1', kind: 'permission',
                                           request_id: 'r1', tool_name: 'Bash', tool_input: { command: 'ls' } } as WireFrame)
    })
    expect(screen.queryByTestId('terminal-limit-card')).toBeNull()
  })
})
