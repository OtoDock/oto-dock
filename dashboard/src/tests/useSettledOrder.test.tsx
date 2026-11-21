import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'

// ─── The order a list of accounts is shown in: the server's, read when the
//     list opens and after the person's own change, never moved by anything
//     else while the person looks; the cards slide after a quiet spell ───────

const { slid, from, place } = vi.hoisted(() => ({
  slid: [] as string[], from: {} as Record<string, number>, place: {} as Record<string, number>,
}))
vi.mock('@/components/chat/artifacts/fold', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/components/chat/artifacts/fold')>()
  return {
    ...mod,
    // Where a card is on the screen: the test sets it (jsdom lays nothing out).
    boxOf: (el: HTMLElement) => ({ left: 0, top: place[el.dataset.id ?? ''] ?? 0, width: 10, height: 10 }),
    slide: (el: HTMLElement, box: { top: number }) => { slid.push(el.dataset.id ?? ''); from[el.dataset.id ?? ''] = box.top },
  }
})

import { useSettledOrder, REORDER_AFTER_MS, ADOPT_WINDOW_MS } from '@/components/engines/useSettledOrder'

interface Row { id: string; on: boolean }
type Api = ReturnType<typeof useSettledOrder<Row>>

let api: Api
function List({ items, open = true, pending = false }: { items: Row[]; open?: boolean; pending?: boolean }) {
  api = useSettledOrder(items, { open, pending, settings: (i) => String(i.on) })
  return (
    <ul ref={api.listRef} tabIndex={-1}>
      {api.shown.map((i) => (
        <li key={i.id} ref={api.itemRef(i.id)} data-id={i.id}>
          <button type="button" aria-label={i.id === 'x' ? 'Other' : 'Personal use'} aria-pressed={i.on}>{i.id}</button>
        </li>
      ))}
    </ul>
  )
}

const rows = (spec: string) => spec.split(' ').map((s) => ({ id: s.replace('-', ''), on: !s.startsWith('-') }))
const shownIds = (c: HTMLElement) => [...c.querySelectorAll('li')].map((li) => li.dataset.id)

// Reduced motion is the only thing off: the browser can animate either way.
function motion(on: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: !on && q.includes('reduce'), media: q, addEventListener: () => {}, removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia
  ;(Element.prototype as unknown as { animate?: unknown }).animate = () => ({})
}

beforeEach(() => {
  vi.useFakeTimers(); slid.length = 0; motion(true)
  for (const o of [from, place]) for (const k of Object.keys(o)) delete o[k]
})
afterEach(() => { vi.useRealTimers() })

describe('useSettledOrder', () => {
  it('an answer in another order moves nothing while the person looks', () => {
    const { container, rerender } = render(<List items={rows('a b c')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    rerender(<List items={rows('c a b')} />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS * 3) })
    expect(shownIds(container)).toEqual(['a', 'b', 'c'])
  })

  it('after a change the list takes the server order once the spell is quiet, and the moved cards slide', () => {
    const { container, rerender } = render(<List items={rows('a b c')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('b c -a')} />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS - 50) })
    expect(shownIds(container)).toEqual(['a', 'b', 'c'])        // the tick shows where the card is
    act(() => { vi.advanceTimersByTime(100) })
    expect(shownIds(container)).toEqual(['b', 'c', 'a'])
    expect(slid.sort()).toEqual(['a', 'b', 'c'])
  })

  it('a tick taken back moves nothing, though the server order changed meanwhile', () => {
    const { container, rerender } = render(<List items={rows('a b')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('b -a')} />)
    act(() => { api.changed() })
    rerender(<List items={rows('b a')} />)   // the sessions the untick moved stay on b
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS * 2) })
    expect(shownIds(container)).toEqual(['a', 'b'])
  })

  it('a second change starts the timer again', () => {
    const { container, rerender } = render(<List items={rows('a b')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('b -a')} />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS - 500) })
    act(() => { api.changed() })
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS - 100) })
    expect(shownIds(container)).toEqual(['a', 'b'])
    act(() => { vi.advanceTimersByTime(200) })
    expect(shownIds(container)).toEqual(['b', 'a'])
  })

  it('a tick right after the list opens moves no card before the spell ends', () => {
    const { container, rerender } = render(<List items={rows('a b c')} open={false} />)
    rerender(<List items={rows('a b c')} open />)          // the person opens the section
    act(() => { vi.advanceTimersByTime(500) })
    act(() => { api.changed() })                            // and unticks a within two seconds
    rerender(<List items={rows('b c -a')} open />)          // the save's re-read: a goes last
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS - 100) })
    expect(shownIds(container)).toEqual(['a', 'b', 'c'])
    act(() => { vi.advanceTimersByTime(200) })
    expect(shownIds(container)).toEqual(['b', 'c', 'a'])
  })

  it('a spell that ends while something saves orders once it is saved, whatever the answer', () => {
    const { container, rerender } = render(<List items={rows('a b c')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('b c -a')} />)               // the re-read lands inside the spell
    rerender(<List items={rows('b c -a')} pending />)       // another save is in flight at the spell's end
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(shownIds(container)).toEqual(['a', 'b', 'c'])
    rerender(<List items={rows('b c -a')} />)               // it saved, the list answers the same order
    expect(shownIds(container)).toEqual(['b', 'c', 'a'])
  })

  it('a change while the list waits for a save starts a spell of its own', () => {
    const { container, rerender } = render(<List items={rows('a b c')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('a -b c')} pending />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })   // the spell ended, still saving
    act(() => { api.changed() })                                      // another tick: c off
    rerender(<List items={rows('a -c -b')} />)                        // saved: an answer in a new order
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS - 100) })
    expect(shownIds(container)).toEqual(['a', 'b', 'c'])
    act(() => { vi.advanceTimersByTime(200) })
    expect(shownIds(container)).toEqual(['a', 'c', 'b'])
  })

  it('the cards slide from where they are when the order changes, not from where they were', () => {
    const { rerender } = render(<List items={rows('a b')} />)
    place.a = 0; place.b = 50
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('b -a')} />)
    place.a = -400; place.b = -350                          // the person scrolled during the spell
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(from).toEqual({ a: -400, b: -350 })
  })

  it('a reopened list slides nothing from where its cards were last time', () => {
    const { container, rerender } = render(<List items={rows('a b')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('b -a')} />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    slid.length = 0
    rerender(<List items={rows('b -a')} open={false} />)
    rerender(<List items={rows('-a b')} open />)
    expect(shownIds(container)).toEqual(['a', 'b'])
    expect(slid).toEqual([])
  })

  it('a row that leaves with the focus in it hands the focus to the same control of its neighbour', () => {
    const { container, rerender } = render(<List items={rows('a b c')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    container.querySelector<HTMLButtonElement>('[data-id="b"] button')!.focus()
    act(() => { api.changed() })
    rerender(<List items={rows('a -b c')} />)
    rerender(<List items={rows('a c')} />)                  // b left the list (an Admin untick)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(shownIds(container)).toEqual(['a', 'c'])
    expect(document.activeElement).toBe(container.querySelector('[data-id="c"] button'))
  })

  it('the focus skips a neighbour with no control of that name, else goes to the list', () => {
    const { container, rerender } = render(<List items={rows('b a x')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    container.querySelector<HTMLButtonElement>('[data-id="a"] button')!.focus()
    act(() => { api.changed() })
    rerender(<List items={rows('b -a x')} />)
    rerender(<List items={rows('b x')} />)                  // a left; x after it is another admin's row
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(document.activeElement).toBe(container.querySelector('[data-id="b"] button'))
    container.querySelector<HTMLButtonElement>('[data-id="b"] button')!.focus()
    act(() => { api.changed() })
    rerender(<List items={rows('-b x')} />)
    rerender(<List items={rows('x')} />)                    // b left: no control of its name is left
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(document.activeElement).toBe(container.querySelector('ul'))
  })

  it('the last row leaving with the focus hands it to the list', () => {
    const { container, rerender } = render(<List items={rows('a')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    container.querySelector<HTMLButtonElement>('[data-id="a"] button')!.focus()
    act(() => { api.changed() })
    rerender(<List items={rows('-a')} />)
    rerender(<List items={[]} />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(document.activeElement).toBe(container.querySelector('ul'))
  })

  it('a change still saving when the spell ends takes the next answer', () => {
    const { container, rerender } = render(<List items={rows('a b')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('a -b')} pending />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(shownIds(container)).toEqual(['a', 'b'])
    rerender(<List items={rows('-b a')} />)
    expect(shownIds(container)).toEqual(['b', 'a'])
  })

  it('a row that leaves after the change stays until the spell ends, a removed one goes at once', () => {
    const { container, rerender } = render(<List items={rows('a b c')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('a -b c')} />)
    rerender(<List items={rows('a c')} />)                // b left the list (an Admin untick)
    expect(shownIds(container)).toEqual(['a', 'b', 'c'])
    expect(container.querySelector('[data-id="b"] button')?.getAttribute('aria-pressed')).toBe('false')
    act(() => { api.removed('c') })
    expect(shownIds(container)).toEqual(['a', 'b'])
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(shownIds(container)).toEqual(['a'])
  })

  it('an opened list shows the server order and takes the next answer, briefly', () => {
    const { container, rerender } = render(<List items={rows('a b')} open={false} />)
    rerender(<List items={rows('b a')} open />)
    expect(shownIds(container)).toEqual(['b', 'a'])
    rerender(<List items={rows('a b')} open />)            // the read the open asked for
    expect(shownIds(container)).toEqual(['a', 'b'])
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    rerender(<List items={rows('b a')} open />)
    expect(shownIds(container)).toEqual(['a', 'b'])
  })

  it('under reduced motion the cards change place with no slide', () => {
    motion(false)
    const { container, rerender } = render(<List items={rows('a b')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    act(() => { api.changed() })
    rerender(<List items={rows('b -a')} />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(shownIds(container)).toEqual(['b', 'a'])
    expect(slid).toEqual([])
  })

  it('a caller that passes a new array on every render is fine (a list filtered in render)', () => {
    function Filtered({ items }: { items: Row[] }) {
      const { shown } = useSettledOrder(items.filter(() => true), { open: true, pending: false, settings: (i) => String(i.on) })
      return <ul>{shown.map((i) => <li key={i.id} data-id={i.id} />)}</ul>
    }
    const items = rows('a b')
    const { container, rerender } = render(<Filtered items={items} />)
    rerender(<Filtered items={items} />)      // any later render (a poll's answer, a fetch starting)
    expect(shownIds(container)).toEqual(['a', 'b'])
  })

  it('the control that was used keeps the focus when its card moves', () => {
    const { container, rerender } = render(<List items={rows('a b c')} />)
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    const used = container.querySelector<HTMLButtonElement>('[data-id="a"] button')!
    used.focus()
    act(() => { api.changed() })
    rerender(<List items={rows('b c -a')} />)
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS + 10) })
    expect(shownIds(container)).toEqual(['b', 'c', 'a'])
    expect(document.activeElement).toBe(used)
  })
})
