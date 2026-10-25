import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

// ─── ScrollableTabs: the active tab stays in view once the arrows appear ───
// The arrows mount only after the row measures its overflow, and they take
// width from the row: a tab scrolled into view before then (a cold
// `/admin/platform?tab=security` on a phone) would sit clipped at the edge.
// So the keep-in-view runs again when the overflow is found.

import { ScrollableTabs } from '@/components/ScrollableTabs'

type Tab = 'general' | 'engines' | 'audio' | 'security' | 'system'
const TABS: { id: Tab; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'engines', label: 'AI Engines' },
  { id: 'audio', label: 'Audio' },
  { id: 'security', label: 'Security' },
  { id: 'system', label: 'System Settings' },
]

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const original = Element.prototype.scrollIntoView

afterEach(() => {
  Element.prototype.scrollIntoView = original
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

// jsdom lays nothing out: the scrolling row reports more tabs than room, its
// box narrows by the arrows' width once they mount, and a tab's box moves
// with the row's scroll.
function layOut() {
  const scrolls = new WeakMap<Element, number>()
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockImplementation(function (this: HTMLElement) {
    return this.className.includes('overflow-x-auto') ? 600 : 0
  })
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(200)
  vi.spyOn(HTMLElement.prototype, 'scrollLeft', 'get').mockImplementation(function (this: HTMLElement) {
    return scrolls.get(this) ?? 0
  })
  vi.spyOn(HTMLElement.prototype, 'scrollLeft', 'set').mockImplementation(function (this: HTMLElement, v: number) {
    scrolls.set(this, v)
  })
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    const arrows = document.querySelector('[aria-label="Scroll tabs right"]') !== null
    if (this.className.toString().includes('overflow-x-auto')) {
      return DOMRect.fromRect({ x: 0, y: 0, width: arrows ? 144 : 200, height: 32 })
    }
    if (this.textContent === 'Security') {
      const row = this.parentElement as HTMLElement
      return DOMRect.fromRect({ x: 260 - row.scrollLeft, y: 0, width: 70, height: 32 })
    }
    return DOMRect.fromRect({ x: 0, y: 0, width: 0, height: 0 })
  })
}

describe('ScrollableTabs', () => {
  it('scrolls the active tab into view again after the arrows appear', () => {
    vi.stubGlobal('ResizeObserver', ObserverStub)
    Element.prototype.scrollIntoView = () => {}
    layOut()

    render(<ScrollableTabs tabs={TABS} active="security" onChange={() => {}} />)

    expect(screen.getByRole('button', { name: 'Scroll tabs right' })).toBeTruthy()
    // Clear of the 20 px fade inside the 200 px row (150), then of the fade
    // again once the arrows took 56 px of it (206).
    const row = screen.getByRole('button', { name: 'Security' }).parentElement as HTMLElement
    expect(row.scrollLeft).toBe(206)
  })

  it('never scrolls the page', () => {
    vi.stubGlobal('ResizeObserver', ObserverStub)
    const intoView = vi.fn()
    Element.prototype.scrollIntoView = intoView
    const scrollTo = vi.fn()
    vi.stubGlobal('scrollTo', scrollTo)
    layOut()

    render(<ScrollableTabs tabs={TABS} active="security" onChange={() => {}} />)

    expect(intoView).not.toHaveBeenCalled()
    expect(scrollTo).not.toHaveBeenCalled()
  })
})
