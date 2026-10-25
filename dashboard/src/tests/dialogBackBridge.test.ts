import { afterEach, describe, expect, it, vi } from 'vitest'

import { pushEscHandler } from '../lib/escStack'
import { pushModalLayer } from '../lib/ui/modalLayers'

// The Android app's back button reads `__otodockDialogActive` and calls
// `__otodockDialogClose()` before any other overlay of the page. The close is
// an Escape routed as the key is: what is open inside the topmost dialog
// first, never a panel behind it.

interface BackBridge {
  __otodockDialogActive?: boolean
  __otodockDialogClose?: () => void
}

const w = window as unknown as BackBridge

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
  Reflect.deleteProperty(document, 'fullscreenElement')
})

describe("the Android app's back button on a dialog", () => {
  it('closes the dialog when nothing inside it is open, never a panel behind it', () => {
    const behind = vi.fn()
    cleanups.push(pushEscHandler(behind))
    const m = modal()
    cleanups.push(m.pop)
    expect(w.__otodockDialogActive).toBe(true)
    w.__otodockDialogClose?.()
    expect(m.onEscape).toHaveBeenCalledTimes(1)
    expect(behind).not.toHaveBeenCalled()
  })

  it('closes what is open inside the dialog first (a menu, the image viewer)', () => {
    const m = modal()
    cleanups.push(m.pop)
    const viewer = document.createElement('div')
    m.portalRoot.appendChild(viewer)
    const closeViewer = vi.fn()
    const popViewer = pushEscHandler(closeViewer, () => viewer)
    w.__otodockDialogClose?.()
    expect(closeViewer).toHaveBeenCalledTimes(1)
    expect(m.onEscape).not.toHaveBeenCalled()
    popViewer()
    viewer.remove()
    w.__otodockDialogClose?.()
    expect(m.onEscape).toHaveBeenCalledTimes(1)
  })

  it('lands where a pressed Escape would: a field that takes its own Escape keeps it', () => {
    const m = modal()
    cleanups.push(m.pop)
    const rename = document.createElement('input')
    rename.addEventListener('keydown', (e) => { if (e.key === 'Escape') e.preventDefault() })
    m.el.appendChild(rename)
    rename.focus()
    w.__otodockDialogClose?.()
    expect(m.onEscape).not.toHaveBeenCalled()
  })

  it('closes the topmost of two dialogs only', () => {
    const below = modal()
    cleanups.push(below.pop)
    const top = modal()
    cleanups.push(top.pop)
    w.__otodockDialogClose?.()
    expect(top.onEscape).toHaveBeenCalledTimes(1)
    expect(below.onEscape).not.toHaveBeenCalled()
  })

  it('leaves fullscreen first, a video inside the dialog or one behind it', () => {
    const exit = vi.fn(() => Promise.resolve())
    Object.defineProperty(document, 'exitFullscreen', { configurable: true, value: exit })
    cleanups.push(() => Reflect.deleteProperty(document, 'exitFullscreen'))
    const m = modal()
    cleanups.push(m.pop)
    const inside = document.createElement('video')
    m.el.appendChild(inside)
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => inside })
    w.__otodockDialogClose?.()
    expect(exit).toHaveBeenCalledTimes(1)
    expect(m.onEscape).not.toHaveBeenCalled()
    // A chat video in fullscreen when a dialog opened on its own: the video
    // is what the person sees, so Back leaves it, never the hidden dialog.
    const outside = document.createElement('video')
    document.body.appendChild(outside)
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => outside })
    w.__otodockDialogClose?.()
    expect(exit).toHaveBeenCalledTimes(2)
    expect(m.onEscape).not.toHaveBeenCalled()
    // Out of fullscreen, the next Back closes the dialog.
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => null })
    w.__otodockDialogClose?.()
    expect(exit).toHaveBeenCalledTimes(2)
    expect(m.onEscape).toHaveBeenCalledTimes(1)
  })

  it('takes the pair off when the last dialog closes', () => {
    const m = modal()
    m.pop()
    expect(w.__otodockDialogActive).toBe(false)
    expect(w.__otodockDialogClose).toBeUndefined()
  })
})
