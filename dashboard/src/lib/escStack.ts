/**
 * Escape-key precedence stack.
 *
 * Several panels and portals (FindBar, PlanPanel, TodoPanel, file previews,
 * the workspace overlay…) all want to handle Escape. With each one binding
 * its own `keydown` listener directly to `document`, every visible handler
 * fires on a single keypress, which collapses unrelated panels together.
 *
 * `pushEscHandler(handler, scope?)` registers a handler on a LIFO stack and
 * returns a pop function. Only the topmost handler is invoked per Escape
 * press. Components call this in a `useEffect` and pop on unmount.
 *
 * An open modal dialog (`lib/ui/modalLayers.ts`) comes first: the chat
 * page's panels push again whenever their callbacks change, so a dialog's
 * place in the stack would not hold. While one is open, an Escape goes to
 * the topmost entry whose `scope` (the element it belongs to, given at push
 * time) lies inside the topmost dialog, else to the dialog itself, and the
 * key's default action is always prevented so the browser never closes the
 * dialog on its own beside it; a control inside the dialog that handles its
 * own Escape (an inline rename) says so with `preventDefault()`. An Escape
 * that ends an input method's composition is the input method's.
 */

import { topModal } from './ui/modalLayers'

type EscHandler = () => void
type EscScope = Element | null | (() => Element | null)

interface Entry {
  handler: EscHandler
  scope?: EscScope
}

const stack: Entry[] = []
let installed = false

function scopeElement(scope: EscScope | undefined): Element | null {
  return typeof scope === 'function' ? scope() : scope ?? null
}

function composing(e: KeyboardEvent): boolean {
  // 229: the key code a browser reports while an input method holds the key.
  return e.isComposing || e.keyCode === 229
}

/** The topmost entry whose scope lies inside `container`. */
function topEntryWithin(container: Element): Entry | null {
  for (let i = stack.length - 1; i >= 0; i--) {
    const el = scopeElement(stack[i].scope)
    if (el && container.contains(el)) return stack[i]
  }
  return null
}

function onKeyDown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  const modal = topModal()
  if (modal) {
    // Taken by a control inside the dialog (an inline rename's Escape).
    if (e.defaultPrevented) return
    e.preventDefault()
    if (composing(e)) return
    const inner = topEntryWithin(modal.el)
    if (inner) inner.handler()
    else modal.onEscape()
    return
  }
  // Taken by the control that had it (an inline form's own Escape), so
  // the panel behind it stays open.
  if (e.defaultPrevented || composing(e)) return
  const top = stack[stack.length - 1]
  if (!top) return
  top.handler()
}

/** Listen for Escape. The first push does it; a dialog does it on opening,
 *  since a page may hold no other entry. */
export function ensureEscListener(): void {
  if (installed) return
  document.addEventListener('keydown', onKeyDown)
  installed = true
}

export function pushEscHandler(handler: EscHandler, scope?: EscScope): () => void {
  ensureEscListener()
  const entry: Entry = { handler, scope }
  stack.push(entry)
  return () => {
    const i = stack.lastIndexOf(entry)
    if (i >= 0) stack.splice(i, 1)
  }
}

/** Whether a scoped entry inside `container` is open (a menu in a dialog):
 *  a press on the dialog's backdrop then closes that entry, not the dialog. */
export function hasEscEntryWithin(container: Element): boolean {
  return topEntryWithin(container) !== null
}
