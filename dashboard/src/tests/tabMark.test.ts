// The dot on the tab's icon when the chat a hidden tab shows answered
// (lib/ui/tabMark), hung on the end of turn ping's own callback.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

let visibility: 'visible' | 'hidden' = 'visible'
let loads: Array<() => void> = []
const ops: string[] = []

class FakeImage {
  naturalWidth = 64
  naturalHeight = 64
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  set src(_v: string) { loads.push(() => this.onload?.()) }
}

function fakeContext() {
  return {
    set globalCompositeOperation(v: string) { ops.push(`op:${v}`) },
    set fillStyle(v: string) { ops.push(`fill:${v}`) },
    drawImage: () => { ops.push('draw') },
    beginPath: () => {}, arc: () => {}, fill: () => { ops.push('paint') },
  }
}

async function loadAll() {
  while (loads.length) loads.shift()!()
  for (let i = 0; i < 5; i++) await Promise.resolve()
}

function setVisible(v: 'visible' | 'hidden') {
  visibility = v
  document.dispatchEvent(new Event('visibilitychange'))
}

async function freshModule() {
  vi.resetModules()
  return import('../lib/ui/tabMark')
}

describe('the tab mark', () => {
  let toDataURL: ReturnType<typeof vi.fn>
  beforeEach(() => {
    visibility = 'visible'
    loads = []
    ops.length = 0
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility })
    vi.stubGlobal('Image', FakeImage)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => fakeContext() as never)
    toDataURL = vi.fn(() => 'data:image/png;base64,MARKED')
    HTMLCanvasElement.prototype.toDataURL = toDataURL as never
    document.documentElement.style.setProperty('--ot-brand', '#146bb5')
    document.head.innerHTML = '<link rel="icon" type="image/png" href="/assets/favicon.png">'
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })
  const href = () => document.querySelector('link[rel="icon"]')!.getAttribute('href')

  it('shows when the shown chat answers in a hidden tab, in the brand colour', async () => {
    const m = await freshModule()
    setVisible('hidden')
    m.markTabForAnswer('chat-1', 'chat-1')
    await loadAll()
    expect(href()).toBe('data:image/png;base64,MARKED')
    expect(m.tabMarked()).toBe(true)
    expect(ops).toContain('fill:#146bb5')
    // The ring is cut out before the dot is painted.
    expect(ops.indexOf('op:destination-out')).toBeLessThan(ops.indexOf('fill:#146bb5'))
  })

  it('leaves when the tab is seen again', async () => {
    const m = await freshModule()
    setVisible('hidden')
    m.markTabForAnswer('chat-1', 'chat-1')
    await loadAll()
    expect(href()).toBe('data:image/png;base64,MARKED')
    setVisible('visible')
    expect(href()).toBe('/assets/favicon.png')
    expect(m.tabMarked()).toBe(false)
  })

  it('does nothing in a visible tab or for another chat', async () => {
    const m = await freshModule()
    m.markTabForAnswer('chat-1', 'chat-1')
    await loadAll()
    expect(href()).toBe('/assets/favicon.png')
    setVisible('hidden')
    m.markTabForAnswer('chat-2', 'chat-1')
    m.markTabForAnswer(undefined, 'chat-1')
    await loadAll()
    expect(href()).toBe('/assets/favicon.png')
  })

  it('stays away when the tab is seen before the drawing is done', async () => {
    const m = await freshModule()
    setVisible('hidden')
    m.markTabForAnswer('chat-1', 'chat-1')
    setVisible('visible')
    await loadAll()
    expect(href()).toBe('/assets/favicon.png')
    expect(m.tabMarked()).toBe(false)
  })

  it('draws a second answer from the original icon', async () => {
    const m = await freshModule()
    setVisible('hidden')
    m.markTabForAnswer('chat-1', 'chat-1')
    await loadAll()
    setVisible('visible')
    setVisible('hidden')
    const seen: string[] = []
    class Spy extends FakeImage { set src(v: string) { seen.push(v); super.src = v } }
    vi.stubGlobal('Image', Spy)
    m.markTabForAnswer('chat-1', 'chat-1')
    await loadAll()
    expect(seen).toEqual(['/assets/favicon.png'])
    expect(href()).toBe('data:image/png;base64,MARKED')
  })

  it('leaves the icon as it is when it cannot draw', async () => {
    let m = await freshModule()
    document.documentElement.style.removeProperty('--ot-brand')
    setVisible('hidden')
    m.markTabForAnswer('chat-1', 'chat-1')
    await loadAll()
    expect(href()).toBe('/assets/favicon.png')
    m = await freshModule()
    document.documentElement.style.setProperty('--ot-brand', '#146bb5')
    toDataURL.mockImplementation(() => { throw new Error('tainted') })
    m.markTabForAnswer('chat-1', 'chat-1')
    await loadAll()
    expect(href()).toBe('/assets/favicon.png')
  })

  it("is hung on the end of turn ping's own callback", () => {
    const src = readFileSync(join(__dirname, '..', 'pages', 'agent', 'chat', 'useAgentChatStream.ts'), 'utf8')
    const body = src.slice(src.indexOf('onTurnComplete: (data) => {'), src.indexOf('enableDefensiveRefetch'))
    expect(body).toContain('markTabForAnswer(data.chat_id, chatIdRef.current)')
    expect(body.indexOf('markTabForAnswer')).toBeLessThan(body.indexOf('chatNotif.playPing()'))
    expect(body.indexOf('isNativePlatform()')).toBeLessThan(body.indexOf('markTabForAnswer'))
  })
})
