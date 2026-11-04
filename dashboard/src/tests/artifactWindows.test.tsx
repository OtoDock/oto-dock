import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { useState } from 'react'

// The pop-up windows over a terminal chat and their dock: a window folds
// into its chip and a pressed chip opens into its window (a plain swap under
// reduced motion), the chip is the button with its X always shown, the
// chips reorder by drag and move the dock to the other side past the middle
// (remembered per device), a phone places a window beside the dock. The
// motion tokens' mirror follows tokens.css.

import ArtifactDock from '@/components/chat/artifacts/ArtifactDock'
import ArtifactWindows from '@/components/chat/artifacts/ArtifactWindows'
import { resetFoldForTests } from '@/components/chat/artifacts/fold'
import { DURATION, EASE } from '@/components/chat/artifacts/motion'
import { DOCK_SIDE, loadDockSide, resetDockSideForTests, setDockSide, useDockSide } from '@/lib/ui/dockSide'
import { useDocumentPaneStore } from '@/store/documentPaneStore'
import type { ArtifactWindow } from '@/hooks/useArtifactWindows'

// ---- the browser parts jsdom lacks -----------------------------------------

const media = { narrow: false, reduced: false, coarse: false }
let overlayWidth = 390
function stubMedia() {
  window.matchMedia = ((q: string) => ({
    matches: (q.includes('max-width: 767px') && media.narrow)
      || (q.includes('prefers-reduced-motion') && media.reduced)
      || (q.includes('pointer: coarse') && media.coarse),
    media: q, onchange: null, addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

class FakeAnimation {
  onfinish: (() => void) | null = null
  finished: Promise<void>
  cancelled = false
  private done!: () => void
  private fail!: (e: unknown) => void
  constructor(public el: Element, public keyframes: Keyframe[], public options: KeyframeAnimationOptions) {
    this.finished = new Promise((res, rej) => { this.done = res; this.fail = rej })
  }
  finish() { this.onfinish?.(); this.done() }
  cancel() { this.cancelled = true; this.fail(new DOMException('cancelled', 'AbortError')) }
}
let animations: FakeAnimation[] = []
const animationsOn = (el: Element) => animations.filter((a) => a.el === el)

// Rectangles: a window at the right, chips stacked 44 px apart from the top
// left, the chat column 1000 px wide.
function rectOf(el: Element): DOMRect {
  const r = (left: number, top: number, width: number, height: number) =>
    ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) }) as DOMRect
  if (el instanceof HTMLElement && el.dataset.artifactWindow) return r(560, 64, 384, 300)
  const chip = (el as HTMLElement).title?.startsWith?.('Open: ') ? el : el.querySelector?.(':scope > button[title^="Open: "]')
  if (chip) {
    const wrap = chip.parentElement!
    const i = Array.from(wrap.parentElement!.children).indexOf(wrap)
    return r(12, 56 + i * 44, 36, 36)
  }
  return r(0, 0, 1000, 700)
}

beforeEach(() => {
  media.narrow = false
  media.reduced = false
  media.coarse = false
  overlayWidth = 390
  stubMedia()
  animations = []
  Element.prototype.animate = function animate(this: Element, k: Keyframe[] | PropertyIndexedKeyframes | null, o?: number | KeyframeAnimationOptions) {
    const a = new FakeAnimation(this, (k ?? []) as Keyframe[], (o ?? {}) as KeyframeAnimationOptions)
    animations.push(a)
    return a as unknown as Animation
  }
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) { return rectOf(this) })
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', { configurable: true, get() { return this.parentElement } })
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get() { return overlayWidth } })
  HTMLElement.prototype.setPointerCapture = () => {}
  HTMLElement.prototype.releasePointerCapture = () => {}
  localStorage.clear()
  resetDockSideForTests()
  resetFoldForTests()
  useDocumentPaneStore.setState({ owner: 'me', byChat: {} })
})
afterEach(() => {
  delete (Element.prototype as { animate?: unknown }).animate
  delete (HTMLElement.prototype as { offsetParent?: unknown }).offsetParent
  delete (HTMLElement.prototype as { clientWidth?: unknown }).clientWidth
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const win = (id: number, title: string): ArtifactWindow => ({ id, title, block: { type: 'url', url: `https://x/${id}`, title, description: '' } })

// ---- the tokens' mirror ----------------------------------------------------

describe('the motion tokens', () => {
  it('mirror the durations and curves of tokens.css', () => {
    const css = readFileSync(join(__dirname, '..', 'tokens.css'), 'utf8')
    const value = (name: string) => css.match(new RegExp(`--${name}:\\s*([^;]+);`))?.[1].trim()
    expect(value('transition-duration-fast')).toBe(`${DURATION.fast}ms`)
    expect(value('transition-duration-base')).toBe(`${DURATION.base}ms`)
    expect(value('transition-duration-slow')).toBe(`${DURATION.slow}ms`)
    expect(value('ease-standard')).toBe(EASE.standard)
    expect(value('ease-enter')).toBe(EASE.enter)
    expect(value('ease-exit')).toBe(EASE.exit)
  })
})

// ---- the dock's side -------------------------------------------------------

describe('the dock side', () => {
  it('reads left first, keeps a move, and reads junk as left', () => {
    expect(loadDockSide()).toBe(DOCK_SIDE.LEFT)
    setDockSide(DOCK_SIDE.RIGHT)
    expect(localStorage.getItem('oto.dock.side')).toBe('right')
    expect(loadDockSide()).toBe(DOCK_SIDE.RIGHT)
    localStorage.setItem('oto.dock.side', 'middle')
    expect(loadDockSide()).toBe(DOCK_SIDE.LEFT)
  })

  it('degrades to the left when storage refuses', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    expect(loadDockSide()).toBe(DOCK_SIDE.LEFT)
    expect(() => setDockSide(DOCK_SIDE.RIGHT)).not.toThrow()
  })

  it('follows a move made here and one made in another tab', () => {
    const { result } = renderHook(() => useDockSide())
    expect(result.current).toBe(DOCK_SIDE.LEFT)
    act(() => setDockSide(DOCK_SIDE.RIGHT))
    expect(result.current).toBe(DOCK_SIDE.RIGHT)
    localStorage.setItem('oto.dock.side', 'left')
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'oto.dock.side' })) })
    expect(result.current).toBe(DOCK_SIDE.LEFT)
  })
})

// ---- the chip --------------------------------------------------------------

describe('the dock chip', () => {
  it('is the button: no badge, and its X shows on every device without a hover', () => {
    const onRestore = vi.fn()
    render(<ArtifactDock windows={[win(1, 'One')]} minimized={new Set([1])} onRestore={onRestore} onClose={() => {}} />)
    const chip = screen.getByTitle('Open: One')
    expect(chip.querySelectorAll('svg')).toHaveLength(1)
    fireEvent.click(chip)
    expect(onRestore).toHaveBeenCalledWith(1)
    const x = screen.getByTitle('Close')
    expect(x.className).not.toMatch(/hidden|hover:hover/)
    expect(x.className).toContain('-left-1')
  })

  it('gives each card kind its own glyph, none of them the document\'s', () => {
    const cards: ArtifactWindow[] = [
      { id: 1, title: 'Which house?', block: { type: 'card', cardId: 'c', title: 'Which house?', text: '', note: '', buttons: [], options: [] } },
      { id: 2, title: 'Triggers', block: { type: 'list', kind: 'triggers', filters: {}, title: 'Triggers', person: 'u' } },
      { id: 3, title: 'Theme', block: { type: 'setting', settingId: 'theme', label: 'Theme', catalogVersion: 1 } },
    ]
    render(<ArtifactDock windows={cards} minimized={new Set([1, 2, 3])} onRestore={() => {}} onClose={() => {}}
      documents={{ chips: [{ fileId: 'f', title: 'a.docx' }], more: 0, onRestore: () => {}, onClose: () => {} }} />)
    const glyph = (title: string) => screen.getByTitle(`Open: ${title}`).querySelector('svg')!.innerHTML
    const all = ['Which house?', 'Triggers', 'Theme', 'a.docx'].map(glyph)
    expect(new Set(all).size).toBe(4)
  })

  it('mirrors the X to the outer corner on the right', () => {
    render(<ArtifactDock windows={[win(1, 'One')]} minimized={new Set([1])} onRestore={() => {}} onClose={() => {}} side={DOCK_SIDE.RIGHT} />)
    expect(screen.getByTitle('Close').className).toContain('-right-1')
  })

  it('arranges the chips by the order a drag left, new ones last', () => {
    render(<ArtifactDock windows={[win(1, 'One'), win(2, 'Two'), win(3, 'Three')]} minimized={new Set([1, 2, 3])}
      onRestore={() => {}} onClose={() => {}} order={['win:3', 'win:1', 'win:9']} />)
    expect(screen.getAllByTitle(/^Open: /).map((b) => b.title)).toEqual(['Open: Three', 'Open: One', 'Open: Two'])
  })
})

// ---- the drag --------------------------------------------------------------

function Dock({ onReorder, onRestore = () => {} }: { onReorder: (k: string[]) => void; onRestore?: (id: number) => void }) {
  const [order, setOrder] = useState<string[]>([])
  return (
    <div>
      <ArtifactDock windows={[win(1, 'One'), win(2, 'Two'), win(3, 'Three')]} minimized={new Set([1, 2, 3])}
        onRestore={onRestore} onClose={() => {}} order={order}
        onReorder={(k) => { setOrder(k); onReorder(k) }} />
    </div>
  )
}
const titles = () => screen.getAllByTitle(/^Open: /).map((b) => b.title.slice(6))
const press = (el: Element, init: PointerEventInit) => fireEvent.pointerDown(el, { pointerId: 1, button: 0, ...init })

describe('dragging a chip', () => {
  it('reorders the dock with a mouse and swallows the click the drop sends, not the next one', () => {
    const onReorder = vi.fn()
    const onRestore = vi.fn()
    render(<Dock onReorder={onReorder} onRestore={onRestore} />)
    const one = screen.getByTitle('Open: One')
    press(one, { pointerType: 'mouse', clientX: 20, clientY: 66 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'mouse', clientX: 22, clientY: 120 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'mouse', clientX: 22, clientY: 160 })
    // Mid-drag the DOM keeps its order (moving the captured button would
    // release the pointer): the others shift by a transform.
    expect(titles()).toEqual(['One', 'Two', 'Three'])
    expect(screen.getByTitle('Open: Two').parentElement!.style.transform).toBe('translateY(-44px)')
    fireEvent.pointerUp(one, { pointerId: 1, pointerType: 'mouse', clientX: 22, clientY: 160 })
    fireEvent.click(one)
    expect(screen.getByTitle('Open: Two').parentElement!.style.transform).toBe('')
    expect(onReorder).toHaveBeenLastCalledWith(['win:2', 'win:3', 'win:1'])
    expect(titles()).toEqual(['Two', 'Three', 'One'])
    expect(onRestore).not.toHaveBeenCalled()
    const two = screen.getByTitle('Open: Two')
    press(two, { pointerType: 'mouse', clientX: 20, clientY: 66 })
    fireEvent.pointerUp(two, { pointerId: 1, pointerType: 'mouse', clientX: 20, clientY: 66 })
    fireEvent.click(two)
    expect(onRestore).toHaveBeenCalledWith(2)
  })

  it('takes a finger only after a hold; a quick tap opens', () => {
    vi.useFakeTimers()
    const onReorder = vi.fn()
    const onRestore = vi.fn()
    render(<Dock onReorder={onReorder} onRestore={onRestore} />)
    const one = screen.getByTitle('Open: One')
    press(one, { pointerType: 'touch', clientX: 20, clientY: 66 })
    act(() => { vi.advanceTimersByTime(100) })
    fireEvent.pointerUp(one, { pointerId: 1, pointerType: 'touch', clientX: 20, clientY: 66 })
    fireEvent.click(one)
    expect(onRestore).toHaveBeenCalledWith(1)
    expect(onReorder).not.toHaveBeenCalled()

    press(one, { pointerType: 'touch', clientX: 20, clientY: 66 })
    act(() => { vi.advanceTimersByTime(400) })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'touch', clientX: 22, clientY: 112 })
    fireEvent.pointerUp(one, { pointerId: 1, pointerType: 'touch', clientX: 22, clientY: 112 })
    expect(onReorder).toHaveBeenLastCalledWith(['win:2', 'win:1', 'win:3'])
  })

  it('leaves a finger that travels before the hold to the page', () => {
    vi.useFakeTimers()
    const onReorder = vi.fn()
    render(<Dock onReorder={onReorder} />)
    const one = screen.getByTitle('Open: One')
    press(one, { pointerType: 'touch', clientX: 20, clientY: 66 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'touch', clientX: 20, clientY: 110 })
    act(() => { vi.advanceTimersByTime(400) })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'touch', clientX: 20, clientY: 160 })
    fireEvent.pointerUp(one, { pointerId: 1, pointerType: 'touch', clientX: 20, clientY: 160 })
    expect(onReorder).not.toHaveBeenCalled()
    expect(titles()).toEqual(['One', 'Two', 'Three'])
  })

  it('moves the dock to the other side when dropped past the middle, and remembers it', () => {
    const onReorder = vi.fn()
    render(<Dock onReorder={onReorder} />)
    const one = screen.getByTitle('Open: One')
    press(one, { pointerType: 'mouse', clientX: 20, clientY: 66 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'mouse', clientX: 300, clientY: 70 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'mouse', clientX: 700, clientY: 70 })
    expect(one.className).toContain('ring-2')
    fireEvent.pointerUp(one, { pointerId: 1, pointerType: 'mouse', clientX: 700, clientY: 70 })
    expect(localStorage.getItem('oto.dock.side')).toBe('right')
    expect(onReorder).toHaveBeenCalled()
  })

  it('ends a drag whose chip leaves the dock, and drops the lift', () => {
    const onLift = vi.fn()
    const onReorder = vi.fn()
    const wins = [win(1, 'One'), win(2, 'Two'), win(3, 'Three')]
    const dock = (min: number[]) => (
      <div><ArtifactDock windows={wins} minimized={new Set(min)} onRestore={() => {}} onClose={() => {}}
        onReorder={onReorder} onLift={onLift} /></div>
    )
    const { rerender } = render(dock([1, 2, 3]))
    const one = screen.getByTitle('Open: One')
    press(one, { pointerType: 'mouse', clientX: 20, clientY: 66 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'mouse', clientX: 22, clientY: 120 })
    expect(onLift).toHaveBeenLastCalledWith(true)
    expect(screen.getByTitle('Open: Two').parentElement!.style.transform).not.toBe('')
    // The dragged chip's window opens elsewhere: its button leaves the DOM.
    rerender(dock([2, 3]))
    expect(onLift).toHaveBeenLastCalledWith(false)
    expect(screen.getByTitle('Open: Two').parentElement!.style.transform).toBe('')
    // No drag is left to end: a release elsewhere reorders nothing.
    fireEvent.pointerUp(screen.getByTitle('Open: Two'), { pointerId: 1, pointerType: 'mouse', clientX: 22, clientY: 160 })
    expect(onReorder).not.toHaveBeenCalled()
  })

  it('drops the lift when the dock unmounts mid-drag', () => {
    const onLift = vi.fn()
    const { unmount } = render(<div><ArtifactDock windows={[win(1, 'One'), win(2, 'Two')]} minimized={new Set([1, 2])}
      onRestore={() => {}} onClose={() => {}} onReorder={() => {}} onLift={onLift} /></div>)
    const one = screen.getByTitle('Open: One')
    press(one, { pointerType: 'mouse', clientX: 20, clientY: 66 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'mouse', clientX: 22, clientY: 120 })
    expect(onLift).toHaveBeenLastCalledWith(true)
    unmount()
    expect(onLift).toHaveBeenLastCalledWith(false)
  })

  it('puts everything back on a cancel', () => {
    const onReorder = vi.fn()
    render(<Dock onReorder={onReorder} />)
    const one = screen.getByTitle('Open: One')
    press(one, { pointerType: 'mouse', clientX: 20, clientY: 66 })
    fireEvent.pointerMove(one, { pointerId: 1, pointerType: 'mouse', clientX: 22, clientY: 160 })
    fireEvent.pointerCancel(one, { pointerId: 1, pointerType: 'mouse' })
    expect(onReorder).not.toHaveBeenCalled()
    expect(titles()).toEqual(['One', 'Two', 'Three'])
    expect(loadDockSide()).toBe(DOCK_SIDE.LEFT)
  })
})

// ---- the windows' motion ---------------------------------------------------

/** The page's order: the dock (ChatSidePanels) before the windows (the slot). */
function Page({ wins, initial = [] }: { wins: ArtifactWindow[]; initial?: number[] }) {
  const [min, setMin] = useState(() => new Set(initial))
  const restore = (id: number) => setMin((s) => { const n = new Set(s); n.delete(id); return n })
  return (
    <>
      <div><ArtifactDock windows={wins} minimized={min} onRestore={restore} onClose={() => {}} onReorder={() => {}} /></div>
      <div>
        <ArtifactWindows windows={wins} minimized={min} onClose={() => {}} onMinimize={(id) => setMin((s) => new Set(s).add(id))} chatId="c1" />
      </div>
    </>
  )
}
const windowEl = (id: number) => document.querySelector<HTMLElement>(`[data-artifact-window="${id}"]`)!

describe('a window and its chip', () => {
  it('folds into its chip: the window stays, inert, until the motion lands, then hides', async () => {
    render(<Page wins={[win(1, 'One')]} />)
    fireEvent.click(screen.getByTitle('Minimize'))
    const el = windowEl(1)
    expect(el.className).not.toMatch(/\bhidden\b/)
    expect(el.hasAttribute('inert')).toBe(true)
    const [motion] = animationsOn(el)
    expect(motion.options.fill).toBe('forwards')
    expect(String(motion.keyframes[motion.keyframes.length - 1].transform)).toMatch(/^translate\(.*\) scale\(/)
    const chip = screen.getByTitle('Open: One')
    expect(animationsOn(chip)[0].options.fill).toBe('backwards')
    await act(async () => { motion.finish() })
    expect(windowEl(1).className).toMatch(/\bhidden\b/)
    expect(motion.cancelled).toBe(true)
    expect(document.activeElement).toBe(screen.getByTitle('Open: One'))
  })

  it('leaves the focus where the person moved it while the window folded', async () => {
    render(<><Page wins={[win(1, 'One')]} /><textarea aria-label="terminal" /></>)
    fireEvent.click(screen.getByTitle('Minimize'))
    const [motion] = animationsOn(windowEl(1))
    const terminal = screen.getByLabelText('terminal')
    terminal.focus()
    await act(async () => { motion.finish() })
    expect(windowEl(1).className).toMatch(/\bhidden\b/)
    expect(document.activeElement).toBe(terminal)
  })

  it('opens out of the pressed chip and comes with the focus', () => {
    render(<Page wins={[win(1, 'One')]} initial={[1]} />)
    expect(windowEl(1).className).toMatch(/\bhidden\b/)
    fireEvent.click(screen.getByTitle('Open: One'))
    const el = windowEl(1)
    expect(el.className).not.toMatch(/\bhidden\b/)
    const [motion] = animationsOn(el)
    expect(motion.keyframes[0].opacity).toBe(0)
    expect(String(motion.keyframes[0].transform)).toMatch(/^translate\(/)
    expect(motion.options.fill).toBeUndefined()
    expect(document.activeElement).toBe(el.querySelector('button[title="Minimize"]'))
  })

  it('opens again from a chip pressed while it is still folding', () => {
    render(<Page wins={[win(1, 'One')]} />)
    fireEvent.click(screen.getByTitle('Minimize'))
    const [foldMotion] = animationsOn(windowEl(1))
    fireEvent.click(screen.getByTitle('Open: One'))
    expect(foldMotion.cancelled).toBe(true)
    const el = windowEl(1)
    expect(el.hasAttribute('inert')).toBe(false)
    expect(el.className).not.toMatch(/\bhidden\b/)
    expect(animationsOn(el).length).toBe(2)
  })

  it('is a plain swap under reduced motion', () => {
    media.reduced = true
    render(<Page wins={[win(1, 'One')]} />)
    expect(windowEl(1).className).not.toContain('oto-pop-in')
    fireEvent.click(screen.getByTitle('Minimize'))
    expect(windowEl(1).className).toMatch(/\bhidden\b/)
    expect(animations).toHaveLength(0)
    expect(document.activeElement).toBe(screen.getByTitle('Open: One'))
    fireEvent.click(screen.getByTitle('Open: One'))
    expect(windowEl(1).className).not.toMatch(/\bhidden\b/)
    expect(animations).toHaveLength(0)
  })

  it('on a phone, opens beside the dock once the dock has chips', () => {
    media.narrow = true
    render(<Page wins={[win(1, 'One'), win(2, 'Two')]} initial={[1]} />)
    const el = windowEl(2)
    expect(el.style.left).toBe('56px')
    expect(el.style.width).toBe(`${390 - 56 - 8}px`)
  })

  it('cascades windows that arrive one by one, each a step lower', () => {
    overlayWidth = 1000
    const { rerender } = render(<Page wins={[win(1, 'One')]} />)
    expect(windowEl(1).style.left).toBe(`${1000 - 384 - 24}px`)
    rerender(<Page wins={[win(1, 'One'), win(2, 'Two')]} />)
    expect(windowEl(2).style.left).toBe(`${1000 - 384 - 24 - 28}px`)
    expect(windowEl(2).style.top).toBe('44px')
  })

  it('cascades new windows from the side away from the dock', () => {
    overlayWidth = 1000
    render(<Page wins={[win(1, 'One')]} />)
    expect(windowEl(1).style.left).toBe(`${1000 - 384 - 24}px`)
    setDockSide(DOCK_SIDE.RIGHT)
    render(<Page wins={[win(2, 'Two')]} />)
    expect(windowEl(2).style.left).toBe('24px')
  })
})
