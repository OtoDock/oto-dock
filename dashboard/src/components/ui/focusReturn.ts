import { useLayoutEffect, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react'

// The keyboard rules every modal surface keeps: the focus goes back where it came from when the surface
// closes, and Tab cycles inside it. The Dialog uses both; the two
// full-screen viewers (the file preview, the image lightbox) use them
// without being dialogs.

const CANDIDATES = [
  'a[href]', 'area[href]', 'button', 'input:not([type="hidden"])', 'select', 'textarea',
  'iframe', 'audio[controls]', 'video[controls]', 'summary',
  '[contenteditable]:not([contenteditable="false"])', '[tabindex]',
].join(',')

function shown(el: HTMLElement): boolean {
  if (typeof el.checkVisibility === 'function') return el.checkVisibility()
  return el.getClientRects().length > 0
}

function focusable(el: HTMLElement): boolean {
  if ((el as HTMLButtonElement).disabled) return false
  if (el.closest('[inert]')) return false
  return el.isConnected && shown(el)
}

/** The elements Tab visits inside `container`, in order: one radio per
 *  group (the checked one, else the first), nothing disabled or hidden. */
export function tabbables(container: HTMLElement): HTMLElement[] {
  const out: HTMLElement[] = []
  const groups = new Set<string>()
  for (const el of container.querySelectorAll<HTMLElement>(CANDIDATES)) {
    if (el.tabIndex < 0 || !focusable(el)) continue
    if (el instanceof HTMLInputElement && el.matches('input[type="radio"][name]')) {
      if (groups.has(el.name)) continue
      const scope = el.form ?? container
      const checked = Array.from(scope.querySelectorAll<HTMLInputElement>('input[type="radio"]:checked'))
        .find((r) => r.name === el.name)
      if (checked && checked !== el) continue
      groups.add(el.name)
    }
    out.push(el)
  }
  return out
}

/** Tab and Shift+Tab wrap between the first and the last tabbable element
 *  of `container`. A keydown handler only: the focus is never corrected on
 *  focusout, so a screen reader's virtual cursor is never pulled back. */
export function wrapTab(e: ReactKeyboardEvent<HTMLElement> | KeyboardEvent, container: HTMLElement): void {
  if (e.key !== 'Tab' || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
  const list = tabbables(container)
  if (!list.length) {
    e.preventDefault()
    return
  }
  const first = list[0]
  const last = list[list.length - 1]
  const active = document.activeElement
  const outside = !(active instanceof Node) || !container.contains(active) || active === container
  if (e.shiftKey && (outside || active === first)) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && (outside || active === last)) {
    e.preventDefault()
    first.focus()
  }
}

// Where a list ends: a row is looked for below these, a neighbour never
// beyond them.
const BOUNDARY = [
  'ul', 'ol', 'table', 'nav', 'aside', 'main', 'section', 'dialog',
  '[role="list"]', '[role="listbox"]', '[role="grid"]', '[role="table"]', '[role="tree"]',
  '[role="navigation"]', '[role="region"]', '[role="dialog"]',
].join(',')
// A control is never the row it sits in, however many of it repeat.
const CONTROL = [
  'button', 'a[href]', 'input', 'select', 'textarea', 'summary', '[role="button"]', '[role="link"]',
  '[role="menuitem"]', '[role="tab"]', '[role="switch"]', '[role="checkbox"]', '[role="radio"]',
].join(',')
const LIST_ROW = 'li, tr, [role="row"], [role="listitem"], [role="option"], [role="treeitem"]'
// A labelled section (a card with its heading): a row is never looked for
// at or above it, so a list's last row cannot lead the focus into the next
// card.
const HEADING = 'h1, h2, h3, h4, h5, h6, [role="heading"]'
// The focus moved back after a confirm keeps watching the list this long:
// the deletion lands after the dialog closed.
const WATCH_MS = 10_000

/** Two rows of one list: the same tag and role, and either a list row or
 *  classes in common (half the smaller list at least). */
function sameKind(a: Element, b: Element): boolean {
  if (a.tagName !== b.tagName || a.getAttribute('role') !== b.getAttribute('role')) return false
  if (a.matches(LIST_ROW)) return true
  const ca = a.classList
  const cb = b.classList
  if (!ca.length || !cb.length) return false
  let shared = 0
  for (const c of ca) if (cb.contains(c)) shared++
  return shared > 0 && shared * 2 >= Math.min(ca.length, cb.length)
}

function hasKin(el: Element, parent: Element): boolean {
  for (const sib of parent.children) if (sib !== el && sameKind(el, sib)) return true
  return false
}

/** A wrapper of one control (a tooltip's span): its one focusable carries
 *  all its text. */
function wrapsOneControl(el: Element): boolean {
  const inner = el.querySelectorAll(CANDIDATES)
  return inner.length === 1 && (el.textContent ?? '').trim() === (inner[0].textContent ?? '').trim()
}

function holdsHeading(el: Element): boolean {
  if (el.hasAttribute('aria-labelledby')) return true
  for (const c of el.children) if (c.matches(HEADING)) return true
  return false
}

/** The row the opener sits in: the lowest element below a list boundary or
 *  a labelled section that repeats among its siblings and is neither a
 *  control nor a wrapper of one (an icon-button group is never the row). */
function findRow(el: HTMLElement): HTMLElement | null {
  for (let n: HTMLElement | null = el; n && n !== document.body; n = n.parentElement) {
    if (n.matches(BOUNDARY) || (n !== el && holdsHeading(n))) return null
    const parent = n.parentElement
    if (!parent) return null
    if (!n.matches(CONTROL) && hasKin(n, parent) && !wrapsOneControl(n)) return n
  }
  return null
}

function kinAfter(kind: Element, from: Element | null, forward: boolean): Element | null {
  for (let e = from; e; e = forward ? e.nextElementSibling : e.previousElementSibling) {
    if (sameKind(kind, e)) return e
  }
  return null
}

/** Where one list sits among its siblings: the recorded next and previous
 *  of its kind and its index, for when it is gone. */
interface Place {
  el: HTMLElement
  parent: HTMLElement
  index: number
  next: Element | null
  prev: Element | null
}

function placeOf(el: HTMLElement, parent: HTMLElement): Place {
  return {
    el, parent, index: Array.prototype.indexOf.call(parent.children, el),
    next: kinAfter(el, el.nextElementSibling, true), prev: kinAfter(el, el.previousElementSibling, false),
  }
}

/** The opener's row, its list's group (a date group that empties and goes:
 *  the list repeats among siblings that hold rows of the row's kind) and
 *  the label of the section it sits in, read now: an emptied list is usually
 *  replaced by an empty state, and a lone row cannot be told from a wrapper,
 *  so the label is the answer for the last row of a list. */
export interface RowOrigin {
  row: Place | null
  group: Place | null
  label: HTMLElement | null
  /** The labelled section, watched when no row is known. */
  section: HTMLElement | null
}

function groupOf(row: HTMLElement, list: HTMLElement): Place | null {
  const outer = list.parentElement
  if (!outer || list.matches(BOUNDARY)) return null
  for (const sib of outer.children) {
    if (sib !== list && sameKind(list, sib) && Array.from(sib.children).some((c) => sameKind(row, c))) {
      return placeOf(list, outer)
    }
  }
  return null
}

function rowOrigin(el: HTMLElement): RowOrigin | null {
  const row = findRow(el)
  const list = row?.parentElement ?? null
  let label: HTMLElement | null = null
  let section: HTMLElement | null = null
  for (let n: HTMLElement | null = list ?? el.parentElement; n && n !== document.body; n = n.parentElement) {
    const ids = n.getAttribute('aria-labelledby')
    if (ids) {
      label = document.getElementById(ids.split(/\s+/)[0])
      section = n
      break
    }
    if (n.matches(BOUNDARY)) break
  }
  if (!(row && list) && !label) return null
  return {
    row: row && list ? placeOf(row, list) : null,
    group: row && list ? groupOf(row, list) : null,
    label, section,
  }
}

/** What to give the focus back to: the element focused when the surface
 *  opened and the chain of its ancestors, taken at that moment (once the
 *  element is gone, its old parents can no longer be found), and the row it
 *  sat in when it sat in a list. */
export interface FocusOrigin {
  element: HTMLElement | null
  ancestors: HTMLElement[]
  row?: RowOrigin | null
}

/** `withRow`: a dialog's origin also records the row it sat in (a confirm
 *  may delete it); a full-screen viewer's does not. */
export function captureFocusOrigin(withRow = false): FocusOrigin {
  const active = document.activeElement
  const element = active instanceof HTMLElement && active !== document.body ? active : null
  const ancestors: HTMLElement[] = []
  for (let p = element?.parentElement ?? null; p && p !== document.body; p = p.parentElement) ancestors.push(p)
  return { element, ancestors, row: withRow && element ? rowOrigin(element) : null }
}

function firstTarget(el: Element): HTMLElement | null {
  if (el instanceof HTMLElement && el.matches(CANDIDATES) && el.tabIndex >= 0 && focusable(el)) return el
  return el instanceof HTMLElement ? tabbables(el)[0] ?? null : null
}

/** The first element of `place`'s kind with a target, after where it was,
 *  else before it, in its own parent only. */
function sideways(
  place: Place, after: (el: Element) => HTMLElement | null, before: (el: Element) => HTMLElement | null,
): HTMLElement | null {
  const { el: kind, parent } = place
  const live = (e: Element | null) => (e && e.isConnected && e.parentElement === parent ? e : null)
  const start = live(place.next) ?? (live(kind) ? kind.nextElementSibling : parent.children[place.index] ?? null)
  for (let e = start; e; e = e.nextElementSibling) {
    if (e !== kind && sameKind(kind, e)) {
      const t = after(e)
      if (t) return t
    }
  }
  const back = live(place.prev) ?? (start ? start.previousElementSibling : parent.lastElementChild)
  for (let e = back; e; e = e.previousElementSibling) {
    if (e !== kind && sameKind(kind, e)) {
      const t = before(e)
      if (t) return t
    }
  }
  return null
}

function rowTargetIn(group: Element, kind: Element, last: boolean): HTMLElement | null {
  const rows = Array.from(group.children).filter((c) => sameKind(kind, c))
  for (const r of last ? rows.reverse() : rows) {
    const t = firstTarget(r)
    if (t) return t
  }
  return null
}

/** A label takes the focus for the moment it needs it (a hidden one never). */
function labelTarget(label: HTMLElement): HTMLElement | null {
  if (!focusable(label)) return null
  if (!label.hasAttribute('tabindex')) {
    label.setAttribute('tabindex', '-1')
    label.addEventListener('blur', () => label.removeAttribute('tabindex'), { once: true })
  }
  return label
}

/** Where the focus goes when the opener is gone: the row's next focusable
 *  when only the opener went, else the next row of its kind, else the
 *  previous one, else the next or previous group's row, else the list's
 *  label. Never outside the row's own list or its sibling groups. */
function neighbour(origin: FocusOrigin): HTMLElement | null {
  const ctx = origin.row
  if (!ctx) return null
  const place = ctx.row
  if (place) {
    const row = place.el
    if (row.isConnected) {
      const t = firstTarget(row)
      if (t) return t
    }
    if (place.parent.isConnected) {
      const t = sideways(place, firstTarget, firstTarget)
      if (t) return t
    }
    if (ctx.group?.parent.isConnected) {
      const t = sideways(ctx.group, (g) => rowTargetIn(g, row, false), (g) => rowTargetIn(g, row, true))
      if (t) return t
    }
  }
  return ctx.label?.isConnected ? labelTarget(ctx.label) : null
}

function focusLost(): boolean {
  const a = document.activeElement
  return !a || a === document.body || !a.isConnected
}

/** The deletion a confirm asked for lands after the dialog closed: watch
 *  the opener's row and list for a while, and when the focus the restore
 *  (or this watch) placed is lost with what held it, move it to the
 *  neighbour. The person's own focus, click or key ends the watch. */
function watchRow(
  origin: FocusOrigin, placed: HTMLElement | null, fallback?: RefObject<HTMLElement | null>,
): void {
  const ctx = origin.row
  if (!ctx || typeof MutationObserver === 'undefined') return
  let ours = placed
  const put = (el: HTMLElement) => {
    ours = el
    el.focus({ preventScroll: true })
  }
  const observer = new MutationObserver(() => {
    if (!focusLost()) return
    // A keyed reorder can drop the focus from an opener still on the page.
    const back = [origin.element, fallback?.current ?? null].find((el) => el && el.isConnected && focusable(el))
    const t = back ?? neighbour(origin)
    if (t) put(t)
  })
  const stop = () => {
    observer.disconnect()
    clearTimeout(timer)
    document.removeEventListener('focusin', onFocusIn, true)
    document.removeEventListener('pointerdown', stop, true)
    document.removeEventListener('keydown', stop, true)
  }
  const onFocusIn = (e: FocusEvent) => {
    if (e.target !== ours) stop()
  }
  if (ctx.row) {
    observer.observe(ctx.row.el, { childList: true, subtree: true })
    observer.observe(ctx.row.parent, { childList: true })
    if (ctx.group) observer.observe(ctx.group.parent, { childList: true })
  } else if (ctx.section) {
    observer.observe(ctx.section, { childList: true, subtree: true })
  }
  document.addEventListener('focusin', onFocusIn, true)
  document.addEventListener('pointerdown', stop, true)
  document.addEventListener('keydown', stop, true)
  const timer = setTimeout(stop, WATCH_MS)
}

/** Focus the origin if it is still there, else what `fallback` holds at
 *  this moment, else the neighbour of the row the origin sat in, else the
 *  first ancestor still on the page that takes the focus, else nothing.
 *  From a row, the focus keeps following it while the deletion lands. */
export function restoreFocus(origin: FocusOrigin, fallback?: RefObject<HTMLElement | null>): void {
  const take = (el: HTMLElement | null): boolean => {
    if (!el || !focusable(el)) return false
    el.focus({ preventScroll: true })
    return document.activeElement === el
  }
  if (take(origin.element)) {
    watchRow(origin, origin.element, fallback)
    return
  }
  if (take(fallback?.current ?? null)) return
  const next = neighbour(origin)
  if (take(next)) {
    watchRow(origin, next, fallback)
    return
  }
  for (const el of origin.ancestors) {
    if (el.matches(CANDIDATES) && take(el)) return
  }
}

/** While `active`, remember where the focus was; give it back when `active`
 *  turns off or the component unmounts. */
export function useReturnFocus(active: boolean, fallback?: RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    if (!active) return
    const origin = captureFocusOrigin()
    return () => restoreFocus(origin, fallback)
  }, [active, fallback])
}
