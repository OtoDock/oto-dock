// Vitest global setup — extends `expect` with jest-dom matchers
// (toBeInTheDocument, toBeChecked, …) and clears the DOM between tests.
import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
  modals.length = 0
})

// jsdom has HTMLDialogElement but neither showModal() nor close(), and no
// layout. What follows is the browser's contract the Dialog relies on and no
// more: showModal() refuses a detached dialog, sets `open` and runs the
// focusing steps; an Escape nobody prevented fires a cancelable `cancel` on
// the topmost modal and closes it unless prevented (the browser's default
// action, after every listener); close() clears `open` and fires `close`.
// Unlike the browser, close() gives no focus back, so a test that sees the
// focus return proves the Dialog's own restore.
const modals: HTMLDialogElement[] = []

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

if (typeof HTMLDialogElement !== 'undefined' && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    if (!this.isConnected) throw new DOMException('The dialog is not in a document.', 'InvalidStateError')
    if (this.hasAttribute('open')) {
      if (modals.includes(this)) return
      throw new DOMException('The dialog is already open.', 'InvalidStateError')
    }
    this.setAttribute('open', '')
    modals.push(this)
    const target = this.querySelector<HTMLElement>('[autofocus]') ?? this.querySelector<HTMLElement>(FOCUSABLE)
    if (target) {
      target.focus()
    } else {
      if (!this.hasAttribute('tabindex')) this.tabIndex = -1
      this.focus()
    }
  }
  HTMLDialogElement.prototype.show = function show(this: HTMLDialogElement) {
    this.setAttribute('open', '')
  }
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement, returnValue?: string) {
    if (!this.hasAttribute('open')) return
    this.removeAttribute('open')
    const i = modals.indexOf(this)
    if (i >= 0) modals.splice(i, 1)
    if (returnValue !== undefined) this.returnValue = returnValue
    this.dispatchEvent(new Event('close'))
  }
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return
    const top = modals[modals.length - 1]
    if (!top) return
    const cancel = new Event('cancel', { cancelable: true })
    top.dispatchEvent(cancel)
    if (!cancel.defaultPrevented) top.close()
  })
}

if (typeof HTMLElement !== 'undefined' && !('checkVisibility' in HTMLElement.prototype)) {
  Object.defineProperty(HTMLElement.prototype, 'checkVisibility', { value: () => true, configurable: true })
}
