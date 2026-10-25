/**
 * A terminal size reaches the PTY: the socket says whether a resize went out,
 * and the terminal records a size as sent only when it did, so one a closed
 * socket dropped goes at the next refit (and one that went out is not sent
 * twice).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import TerminalView from '@/components/chat/terminal/TerminalView'
import { OUT, type WireFrame, type WireType } from '@/api/wireEvents'
import { FakeWebSocket } from './fakeWebSocket'
import { connectDashboardWs } from './dashboardWsHarness'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function makeWs(sendPtyResize: (chatId: string, rows: number, cols: number) => boolean) {
  return {
    subscribe: (_t: WireType, _fn: (m: WireFrame) => void) => () => {},
    sendPtyAttach: vi.fn(),
    sendPtyInput: vi.fn(),
    sendPtyResize: vi.fn(sendPtyResize),
    sendPermission: vi.fn(),
    sendPtyTakeover: vi.fn(),
  }
}

const artifacts = { windows: [], minimized: [], close: vi.fn(), minimize: vi.fn() } as never

/** The tab coming back into view: one of the refits that re-syncs the size. */
function tabShown() {
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
}

describe('the terminal records a size only once it went out', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', ObserverStub)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: false, media: q, onchange: null,
      addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
      dispatchEvent: () => false,
    }))
    // A laid-out container: jsdom measures every box as 0.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('sends a dropped size again at the next refit', () => {
    let open = false
    const ws = makeWs(() => open)
    render(<TerminalView ws={ws} chatId="c1" artifacts={artifacts} />)
    expect(ws.sendPtyResize).toHaveBeenCalledTimes(1)
    open = true
    tabShown()
    expect(ws.sendPtyResize).toHaveBeenCalledTimes(2)
    tabShown()
    expect(ws.sendPtyResize).toHaveBeenCalledTimes(2)
  })

  it('never sends the same size twice once it went out', () => {
    const ws = makeWs(() => true)
    render(<TerminalView ws={ws} chatId="c1" artifacts={artifacts} />)
    tabShown()
    tabShown()
    expect(ws.sendPtyResize).toHaveBeenCalledTimes(1)
    expect(ws.sendPtyResize).toHaveBeenCalledWith('c1', expect.any(Number), expect.any(Number))
  })
})

describe('the dashboard socket says whether a resize went out', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('true on an open socket, false once it is not open', () => {
    const { hook, ws } = connectDashboardWs(() => ({ viewedChatId: null }))
    expect(hook.result.current.sendPtyResize('c1', 30, 100)).toBe(true)
    expect(ws.sent.map((s) => JSON.parse(s)).filter((m) => m.type === OUT.PTY_RESIZE)).toHaveLength(1)
    ws.readyState = FakeWebSocket.CLOSED
    expect(hook.result.current.sendPtyResize('c1', 31, 100)).toBe(false)
    expect(ws.sent.map((s) => JSON.parse(s)).filter((m) => m.type === OUT.PTY_RESIZE)).toHaveLength(1)
  })
})
