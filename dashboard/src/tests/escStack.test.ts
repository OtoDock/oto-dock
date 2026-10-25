import { afterEach, describe, expect, it, vi } from 'vitest'

import { ensureEscListener, hasEscEntryWithin, pushEscHandler } from '../lib/escStack'
import { blockedByModal, pushModalLayer, topPortalRoot } from '../lib/ui/modalLayers'

function esc(target: EventTarget = document.body, init: KeyboardEventInit = {}): KeyboardEvent {
  const e = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true, ...init })
  target.dispatchEvent(e)
  return e
}

function modal() {
  const el = document.createElement('dialog')
  const portalRoot = document.createElement('div')
  el.appendChild(portalRoot)
  document.body.appendChild(el)
  const onEscape = vi.fn()
  const pop = pushModalLayer({ id: `m${Math.random()}`, el, portalRoot, onEscape })
  return { el, portalRoot, onEscape, pop }
}

const cleanups: Array<() => void> = []
afterEach(() => {
  while (cleanups.length) cleanups.pop()!()
  document.body.innerHTML = ''
})

describe('escStack outside a dialog', () => {
  it('runs only the topmost handler', () => {
    const a = vi.fn()
    const b = vi.fn()
    cleanups.push(pushEscHandler(a), pushEscHandler(b))
    esc()
    expect(b).toHaveBeenCalledTimes(1)
    expect(a).not.toHaveBeenCalled()
  })

  it("leaves an Escape that ends an input method's composition alone", () => {
    const a = vi.fn()
    cleanups.push(pushEscHandler(a))
    esc(document.body, { isComposing: true })
    esc(document.body, { keyCode: 229 })
    expect(a).not.toHaveBeenCalled()
  })

  it('leaves an Escape a control took, and takes the next one', () => {
    const a = vi.fn()
    cleanups.push(pushEscHandler(a))
    const input = document.createElement('input')
    document.body.appendChild(input)
    const own = (e: KeyboardEvent) => e.preventDefault()
    input.addEventListener('keydown', own)
    esc(input)
    expect(a).not.toHaveBeenCalled()
    input.removeEventListener('keydown', own)
    esc(input)
    expect(a).toHaveBeenCalledTimes(1)
  })
})

describe('escStack with a modal dialog open', () => {
  it('goes to the dialog, not to the panels behind it that pushed later', () => {
    const m = modal()
    cleanups.push(m.pop)
    const panelBehind = vi.fn()
    // A chat panel pushing again on a re-render while the dialog is open.
    cleanups.push(pushEscHandler(panelBehind))
    const e = esc(m.el)
    expect(m.onEscape).toHaveBeenCalledTimes(1)
    expect(panelBehind).not.toHaveBeenCalled()
    expect(e.defaultPrevented).toBe(true)
  })

  it('goes to a scoped entry inside the dialog first (an open menu)', () => {
    const m = modal()
    cleanups.push(m.pop)
    const menu = document.createElement('div')
    m.portalRoot.appendChild(menu)
    const closeMenu = vi.fn()
    cleanups.push(pushEscHandler(closeMenu, () => menu))
    expect(hasEscEntryWithin(m.el)).toBe(true)
    esc(menu)
    expect(closeMenu).toHaveBeenCalledTimes(1)
    expect(m.onEscape).not.toHaveBeenCalled()
  })

  it('prevents every Escape, so the browser never closes the dialog beside the stack', () => {
    const m = modal()
    cleanups.push(m.pop)
    // The focus fell to the body (the focused button unmounted).
    const e = esc(document.body)
    expect(e.defaultPrevented).toBe(true)
    expect(m.onEscape).toHaveBeenCalledTimes(1)
    // While composing: prevented, but not routed.
    const composing = esc(m.el, { isComposing: true })
    expect(composing.defaultPrevented).toBe(true)
    expect(m.onEscape).toHaveBeenCalledTimes(1)
  })

  it('leaves an Escape a control inside the dialog took (an inline rename)', () => {
    const m = modal()
    cleanups.push(m.pop)
    const input = document.createElement('input')
    m.el.appendChild(input)
    input.addEventListener('keydown', (e) => e.preventDefault())
    esc(input)
    expect(m.onEscape).not.toHaveBeenCalled()
  })

  it('routes to the topmost of two stacked dialogs', () => {
    const lower = modal()
    const upper = modal()
    cleanups.push(lower.pop, upper.pop)
    esc(upper.el)
    expect(upper.onEscape).toHaveBeenCalledTimes(1)
    expect(lower.onEscape).not.toHaveBeenCalled()
  })

  it('listens once a dialog asks, with no entry ever pushed', () => {
    ensureEscListener()
    const m = modal()
    cleanups.push(m.pop)
    esc(m.el)
    expect(m.onEscape).toHaveBeenCalledTimes(1)
  })
})

describe('modalLayers', () => {
  it('blocks events from outside the topmost dialog and points toasts into it', () => {
    expect(topPortalRoot()).toBe(document.body)
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    const m = modal()
    cleanups.push(m.pop)
    const inner = document.createElement('button')
    m.el.appendChild(inner)
    expect(blockedByModal({ target: outside } as unknown as Event)).toBe(true)
    expect(blockedByModal({ target: inner } as unknown as Event)).toBe(false)
    expect(topPortalRoot()).toBe(m.portalRoot)
  })

  it('holds the page still while a dialog is open and gives the scroll back', () => {
    document.documentElement.style.overflow = 'auto'
    const a = modal()
    const b = modal()
    expect(document.documentElement.style.overflow).toBe('hidden')
    a.pop()
    expect(document.documentElement.style.overflow).toBe('hidden')
    b.pop()
    expect(document.documentElement.style.overflow).toBe('auto')
    document.documentElement.style.overflow = ''
  })

  it("publishes the Android back button's pair while a dialog is open", () => {
    const w = window as unknown as { __otodockDialogActive?: boolean; __otodockDialogClose?: () => void }
    const m = modal()
    expect(w.__otodockDialogActive).toBe(true)
    w.__otodockDialogClose?.()
    expect(m.onEscape).toHaveBeenCalledTimes(1)
    m.pop()
    expect(w.__otodockDialogActive).toBe(false)
    expect(w.__otodockDialogClose).toBeUndefined()
  })
})
