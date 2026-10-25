/**
 * The interactive terminal's WebGL renderer after a lost context: the DOM
 * renderer takes over at once (the addon's own listener would wait 3 s), the
 * terminal re-fits for the other renderer's cell width, and WebGL comes back
 * after a moment, bounded, only while the page is shown and never during
 * teardown. The addon is a fake shaped like the real one (a 2D link canvas
 * appended before its own, a listener on its canvas that reports a loss 3 s
 * later unless disposed; the real one throws in jsdom, which the
 * terminalResize and terminalImeInput tests cover); the context probe is a
 * spied getContext.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import type { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import TerminalView from '@/components/chat/terminal/TerminalView'
import type { WireFrame, WireType } from '@/api/wireEvents'

const { FakeWebglAddon } = vi.hoisted(() => {
  class FakeWebglAddon {
    static instances: FakeWebglAddon[] = []
    canvas: HTMLCanvasElement | null = null
    disposed = false
    /** The addon's own late notifications that fired (none once disposed). */
    lateLosses = 0
    private lossHandlers: Array<() => void> = []
    private ownListener: (() => void) | null = null
    private lateTimer = 0
    constructor() { FakeWebglAddon.instances.push(this) }
    onContextLoss(fn: () => void) { this.lossHandlers.push(fn); return { dispose() {} } }
    activate(term: Terminal) {
      const screen = term.element?.querySelector('.xterm-screen')
      // The link layer's 2D canvas comes first, the renderer's own last.
      screen?.appendChild(document.createElement('canvas'))
      this.canvas = document.createElement('canvas')
      screen?.appendChild(this.canvas)
      this.ownListener = () => {
        this.lateTimer = window.setTimeout(() => this.fireLateLoss(), 3000)
      }
      this.canvas.addEventListener('webglcontextlost', this.ownListener)
    }
    dispose() {
      this.disposed = true
      if (this.ownListener) this.canvas?.removeEventListener('webglcontextlost', this.ownListener)
      window.clearTimeout(this.lateTimer)
      this.canvas?.remove()
    }
    /** The addon's own notification, 3 s after a loss it saw itself. */
    fireLateLoss() { this.lateLosses++; for (const h of this.lossHandlers) h() }
  }
  return { FakeWebglAddon }
})

vi.mock('@xterm/addon-webgl', () => ({ WebglAddon: FakeWebglAddon }))

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function makeWs() {
  return {
    subscribe: (_t: WireType, _fn: (m: WireFrame) => void) => () => {},
    sendPtyAttach: vi.fn(),
    sendPtyInput: vi.fn(),
    sendPtyResize: vi.fn(() => true),
    sendPermission: vi.fn(),
    sendPtyTakeover: vi.fn(),
  }
}

const artifacts = { windows: [], minimized: [], close: vi.fn(), minimize: vi.fn() } as never

/** The loss event as the browser fires it on the context's canvas. */
function loseContextOn(canvas: Element | null) {
  act(() => {
    canvas?.dispatchEvent(new Event('webglcontextlost', { bubbles: true, cancelable: true }))
  })
}

let webgl2 = true
let probes = 0
let visibility: DocumentVisibilityState = 'visible'

describe('the terminal after a lost WebGL context', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebglAddon.instances = []
    webgl2 = true
    probes = 0
    visibility = 'visible'
    vi.stubGlobal('ResizeObserver', ObserverStub)
    // The probe's context: a scratch canvas gets one that can be lost; the
    // cleanup's loseContext() on the terminal's canvases fires the event the
    // way a synchronous browser would.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement, type: string) {
      if (type !== 'webgl2') return null
      probes++
      if (!webgl2) return null
      const canvas = this
      return {
        getExtension: () => ({
          loseContext: () => canvas.dispatchEvent(new Event('webglcontextlost', { bubbles: true, cancelable: true })),
        }),
      } as unknown as RenderingContext
    })
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: false, media: q, onchange: null,
      addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
      dispatchEvent: () => false,
    }))
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600)
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
  })

  afterEach(() => {
    // Unmount while the probe is still faked: the cleanup loses every
    // canvas's context through getContext.
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('loads the WebGL renderer once at mount, without a probe', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    expect(FakeWebglAddon.instances).toHaveLength(1)
    expect(FakeWebglAddon.instances[0].canvas?.isConnected).toBe(true)
    expect(probes).toBe(0)
  })

  it('drops the renderer the moment its context is lost and brings it back after 1.5 s, with a probe', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    const first = FakeWebglAddon.instances[0]
    loseContextOn(first.canvas)
    expect(first.disposed).toBe(true)
    expect(FakeWebglAddon.instances).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(1499) })
    expect(FakeWebglAddon.instances).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(1) })
    expect(probes).toBe(1)
    expect(FakeWebglAddon.instances).toHaveLength(2)
    expect(FakeWebglAddon.instances[1].canvas?.isConnected).toBe(true)
    expect(FakeWebglAddon.instances[1].disposed).toBe(false)
  })

  it('cancels the addon\'s own 3 s wait: no second drop, no extra renderer', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    const first = FakeWebglAddon.instances[0]
    loseContextOn(first.canvas)
    act(() => { vi.advanceTimersByTime(3500) })
    expect(first.lateLosses).toBe(0)
    expect(FakeWebglAddon.instances).toHaveLength(2)
    expect(FakeWebglAddon.instances[1].disposed).toBe(false)
  })

  it('re-fits the terminal on every renderer swap', () => {
    const fit = vi.spyOn(FitAddon.prototype, 'fit')
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    fit.mockClear()
    loseContextOn(FakeWebglAddon.instances[0].canvas)
    expect(fit).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(1500) })
    expect(FakeWebglAddon.instances).toHaveLength(2)
    expect(fit).toHaveBeenCalledTimes(2)
  })

  it('ignores a loss on a canvas that is not the renderer\'s', () => {
    const { container } = render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    const stray = document.createElement('canvas')
    container.querySelector('.xterm-screen')?.appendChild(stray)
    loseContextOn(stray)
    expect(FakeWebglAddon.instances[0].disposed).toBe(false)
    act(() => { vi.advanceTimersByTime(20_000) })
    expect(FakeWebglAddon.instances).toHaveLength(1)
  })

  it('still drops the renderer on the addon\'s own late notification', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    const first = FakeWebglAddon.instances[0]
    act(() => { first.fireLateLoss() })
    expect(first.disposed).toBe(true)
    act(() => { vi.advanceTimersByTime(1500) })
    expect(FakeWebglAddon.instances).toHaveLength(2)
  })

  it('waits for the page to be shown before it tries again', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    loseContextOn(FakeWebglAddon.instances[0].canvas)
    visibility = 'hidden'
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(FakeWebglAddon.instances).toHaveLength(1)
    visibility = 'visible'
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    act(() => { vi.advanceTimersByTime(1500) })
    expect(FakeWebglAddon.instances).toHaveLength(2)
  })

  it('gives up after three failed tries, with a probe each and no addon half-loaded', () => {
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    loseContextOn(FakeWebglAddon.instances[0].canvas)
    webgl2 = false
    probes = 0
    act(() => { vi.advanceTimersByTime(1500 + 5000 + 15000) })
    expect(probes).toBe(3)
    act(() => { vi.advanceTimersByTime(120_000) })
    expect(probes).toBe(3)
    expect(FakeWebglAddon.instances).toHaveLength(1)
  })

  it('never retries on a device whose first load failed, even once shown again', () => {
    vi.spyOn(FakeWebglAddon.prototype, 'activate').mockImplementationOnce(() => { throw new Error('WebGL2 not supported') })
    render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    expect(FakeWebglAddon.instances).toHaveLength(1)
    expect(FakeWebglAddon.instances[0].canvas).toBeNull()
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(probes).toBe(0)
    expect(FakeWebglAddon.instances).toHaveLength(1)
  })

  it('loads nothing when the context is lost during teardown', () => {
    const { unmount } = render(<TerminalView ws={makeWs()} chatId="c1" artifacts={artifacts} />)
    const first = FakeWebglAddon.instances[0]
    // The cleanup loses every canvas's context itself (synchronously here),
    // after it stopped listening.
    act(() => { unmount() })
    expect(first.disposed).toBe(true)
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(FakeWebglAddon.instances).toHaveLength(1)
  })
})
