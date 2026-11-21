// The order a list of accounts is shown in (both AI Engines lists): the
// server's, read when the list opens and again after the person's own change,
// never moved by anything else while the person looks. The server answers in
// the order the pool uses the accounts; the page never sorts.
//
// A change starts one timer for the whole list, and every later change starts
// it again. When it fires the cards take the server's latest order and slide
// from where they are to their places (`slide`, the dock's chips' motion), or
// change place at once under reduced motion. A spell that ends while a change
// still saves waits for the save, then orders. A spell that ends with every
// setting back where it began moves nothing: the sessions an untick moved stay
// moved, so the server's order would swap the cards for a tick taken back.
// React keeps the focus on the control that was used through a keyed move; a
// row that leaves the list hands it to the same control of its neighbour.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { boxOf, slide, type Box } from '../chat/artifacts/fold'
import { DURATION, canAnimate, useReducedMotion } from '../chat/artifacts/motion'

/** The quiet spell after the person's last change before the list orders
 *  itself again. The operator decides the number from the clip. */
export const REORDER_AFTER_MS = 3000

/** How long an opened list takes the next answer's order (the open's own
 *  read), unless the person changes something first. */
export const ADOPT_WINDOW_MS = 2000

interface Options<T> {
  /** The list is shown. */
  open: boolean
  /** A row's settings (its ticks, its status) as one string. */
  settings: (item: T) => string
  /** A change of an account still saves, or the list is being read. */
  pending: boolean
}

interface State<T> {
  /** The rows as last folded in, for one that leaves during a spell. */
  items: T[]
  key: string
  settings: string
  open: boolean
  order: string[]
  /** Rows that left during a spell, with their last data, shown until it ends. */
  kept: Record<string, T>
  /** Removed by the person: gone at once, never appended again. */
  gone: string[]
  /** A change of the person's starts it; it ends when the list orders itself. */
  spell: boolean
  /** The spell's timer fired while a change still saved: order once it saved. */
  awaiting: boolean
  /** The list just opened: the next answer is taken whole. */
  adopt: boolean
}

const keyOf = (ids: string[]) => ids.join('\n')

const CONTROLS = 'input, button, select, textarea, a[href], [tabindex]:not([tabindex="-1"])'
const controlsOf = (el: Element | undefined) => (el ? [...el.querySelectorAll<HTMLElement>(CONTROLS)] : [])
/** A control's name as a person hears it: its label's words, else its own. */
const nameOf = (el: HTMLElement) =>
  (el.closest('label')?.textContent ?? el.getAttribute('aria-label') ?? el.textContent ?? '').trim()

export function useSettledOrder<T extends { id: string }>(
  items: T[], { open, settings: settingsOf, pending }: Options<T>,
) {
  const motion = canAnimate(useReducedMotion())
  const ids = items.map((i) => i.id)
  // The list's settings in no particular order: a card's place is not one.
  const settings = items.map((i) => `${i.id}:${settingsOf(i)}`).sort().join('|')
  const key = keyOf(ids)
  const [st, setSt] = useState<State<T>>(() => ({
    items, key, settings, open, order: ids, kept: {}, gone: [], spell: false, awaiting: false, adopt: false,
  }))

  // The server's answer or the list's opening, folded into what is shown.
  // Compared by content: a caller may build its array in render.
  let cur = st
  if (st.key !== key || st.settings !== settings || st.open !== open) {
    cur = { ...st, items, key, settings, open }
    if (open && !st.open) {
      cur = { ...cur, order: ids, kept: {}, gone: [], spell: false, awaiting: false, adopt: true }
    } else if (!open) {
      cur = { ...cur, kept: {}, spell: false, awaiting: false, adopt: false }
    } else if (st.key !== key) {
      const gone = st.gone.filter((id) => ids.includes(id))
      if (st.adopt && !st.spell) {
        cur = { ...cur, order: ids.filter((id) => !gone.includes(id)), kept: {}, gone, adopt: false }
      } else {
        const kept = { ...st.kept }
        const order = st.order.filter((id) => {
          if (ids.includes(id)) return true
          if (!st.spell || gone.includes(id)) return false
          const last = st.items.find((i) => i.id === id) ?? st.kept[id]
          if (last) kept[id] = last
          return !!last
        })
        for (const id of ids) if (!order.includes(id) && !gone.includes(id)) order.push(id)
        cur = { ...cur, order, kept, gone }
      }
    }
    setSt(cur)
  }

  // What the timer reads when it fires.
  const latest = useRef({ ids, settings, open })
  const frozen = useRef<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const adoptTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    latest.current = { ids, settings, open }
  })

  // The cards' elements by id, the list's own element, where each card was
  // right before the order changed, and the control that had the focus then.
  const els = useRef(new Map<string, HTMLElement>())
  const listEl = useRef<HTMLElement | null>(null)
  const boxes = useRef(new Map<string, Box>())
  const focusFrom = useRef<{ id: string; name: string } | null>(null)

  /** Right before the order changes: where every card is now, and which card's
   *  control holds the focus. */
  const measure = useCallback(() => {
    boxes.current = new Map([...els.current].map(([id, el]) => [id, boxOf(el)]))
    const active = document.activeElement as HTMLElement | null
    focusFrom.current = null
    for (const [id, el] of els.current) {
      if (active && el.contains(active)) focusFrom.current = { id, name: nameOf(active) }
    }
  }, [])

  const stopTimers = useCallback(() => {
    if (timer.current) clearTimeout(timer.current)
    if (adoptTimer.current) clearTimeout(adoptTimer.current)
    timer.current = adoptTimer.current = null
  }, [])

  // An opened list takes the next answer for a short while; a closed one
  // forgets its spell and where its cards were.
  useEffect(() => {
    if (open) {
      adoptTimer.current = setTimeout(() => {
        adoptTimer.current = null
        setSt((s) => (s.adopt ? { ...s, adopt: false } : s))
      }, ADOPT_WINDOW_MS)
    } else {
      frozen.current = null
      boxes.current.clear()
    }
    return stopTimers
  }, [open, stopTimers])

  /** The list takes the server's latest order. */
  const order = useCallback(() => {
    measure()
    setSt((s) => ({ ...s, kept: {}, spell: false, awaiting: false,
                    order: latest.current.ids.filter((id) => !s.gone.includes(id)) }))
  }, [measure])

  const settle = useCallback(() => {
    timer.current = null
    const was = frozen.current
    frozen.current = null
    if (!latest.current.open) return
    if (was !== null && latest.current.settings === was) {
      measure()
      setSt((s) => ({ ...s, kept: {}, spell: false, awaiting: false,
                      order: s.order.filter((id) => latest.current.ids.includes(id)) }))
    } else {
      setSt((s) => ({ ...s, awaiting: true }))
    }
  }, [measure])

  // A spell that ended orders once nothing saves and the list is read.
  useEffect(() => {
    if (st.awaiting && !pending && open) order()
  }, [st.awaiting, pending, open, order])

  /** A change of the person's in the list: the timer starts again, and an
   *  opened list's window to take the next answer ends. */
  const changed = useCallback(() => {
    if (frozen.current === null) frozen.current = latest.current.settings
    if (timer.current) clearTimeout(timer.current)
    if (adoptTimer.current) clearTimeout(adoptTimer.current)
    adoptTimer.current = null
    timer.current = setTimeout(settle, REORDER_AFTER_MS)
    setSt((s) => (s.spell && !s.adopt && !s.awaiting ? s : { ...s, spell: true, adopt: false, awaiting: false }))
  }, [settle])

  /** The person removed a row: it goes at once. */
  const removed = useCallback((id: string) => {
    setSt((s) => {
      const kept = { ...s.kept }
      delete kept[id]
      return { ...s, order: s.order.filter((x) => x !== id), kept, gone: [...s.gone, id] }
    })
  }, [])

  const shownOrder = useRef<string[]>(cur.order)
  const itemRef = useCallback((id: string) => (el: HTMLElement | null) => {
    if (!el) return
    els.current.set(id, el)
    return () => { if (els.current.get(id) === el) els.current.delete(id) }
  }, [])
  const listRef = useCallback((el: HTMLElement | null) => { listEl.current = el }, [])

  useLayoutEffect(() => {
    const before = shownOrder.current
    shownOrder.current = cur.order
    if (keyOf(before) === keyOf(cur.order)) return
    if (motion) {
      for (const [id, el] of els.current) {
        const from = boxes.current.get(id)
        if (from) slide(el, from, DURATION.slow)
      }
    }
    boxes.current.clear()
    // A card that left with the focus in it: the control of the same name in
    // the nearest card after it, else before it, else the list itself.
    const f = focusFrom.current
    focusFrom.current = null
    if (f && !cur.order.includes(f.id)) {
      const at = before.indexOf(f.id)
      const near = [...before.slice(at + 1), ...before.slice(0, at).reverse()].filter((id) => cur.order.includes(id))
      const same = near.map((id) => controlsOf(els.current.get(id)).find((c) => nameOf(c) === f.name)).find(Boolean)
      ;(same ?? listEl.current)?.focus({ preventScroll: true })
    }
  })

  const shown = cur.order
    .map((id) => items.find((i) => i.id === id) ?? cur.kept[id])
    .filter((i): i is T => !!i)
  return { shown, changed, removed, itemRef, listRef }
}
