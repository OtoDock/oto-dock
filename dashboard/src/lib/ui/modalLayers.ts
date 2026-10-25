// The open modal dialogs, newest last. A
// dialog opened with showModal() sits in the browser's top layer and makes
// the rest of the page inert, which leaves the page three jobs: an Escape
// must reach the dialog before the panels behind it (`lib/escStack.ts`
// reads the top here), a document-level shortcut must not act on the page
// behind it (`blockedByModal`), and a toast raised meanwhile must render
// inside the dialog to be seen at all (`topPortalRoot`). While one is open
// the page does not scroll, and two window members in the pattern the
// Android app's back button reads for the lightbox close the topmost one.

// escStack reads this module's top layer: each uses the other only at call time.
import { ensureEscListener } from '../escStack'

export interface ModalLayer {
  /** The Dialog's `useId()`: the Dialog renders its children while it is here. */
  id: string
  /** The open <dialog>. */
  el: HTMLElement
  /** Where a popover opened from inside the dialog portals (inside `el`). */
  portalRoot: HTMLElement
  /** A close request (Escape, the app's back button): the dialog decides. */
  onEscape: () => void
}

type Listener = () => void

interface BackBridge {
  __otodockDialogActive?: boolean
  __otodockDialogClose?: () => void
}

const layers: ModalLayer[] = []
const listeners = new Set<Listener>()
// The root's own overflow while a dialog holds the page still; null when none does.
let unlockedOverflow: string | null = null

function sync(): void {
  const root = document.documentElement
  if (layers.length && unlockedOverflow === null) {
    unlockedOverflow = root.style.overflow
    root.style.overflow = 'hidden'
  } else if (!layers.length && unlockedOverflow !== null) {
    root.style.overflow = unlockedOverflow
    unlockedOverflow = null
  }
  // A boolean, as `__otodockLightboxActive` is: the app's script compares it.
  const w = window as unknown as BackBridge
  if (layers.length) {
    w.__otodockDialogActive = true
    w.__otodockDialogClose = closeTopFromBack
  } else {
    w.__otodockDialogActive = false
    delete w.__otodockDialogClose
  }
  listeners.forEach((l) => l())
}

/** The app's back button on an open dialog is an Escape, dispatched where a
 *  pressed one lands so `lib/escStack.ts` routes it as it routes the key:
 *  what is open inside the topmost dialog (a menu, the image viewer, an
 *  inline rename) closes first, else the dialog. Whatever is fullscreen is
 *  what the person sees (a video in the dialog, or a chat video when a
 *  dialog opened on its own behind it), so Back leaves fullscreen first. */
function closeTopFromBack(): void {
  const top = topModal()
  if (!top) return
  ensureEscListener()
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {})
    return
  }
  const active = document.activeElement
  const target = active instanceof HTMLElement && top.el.contains(active) ? active : top.el
  target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
}

/** Register an open dialog; the returned function takes it off. */
export function pushModalLayer(layer: ModalLayer): () => void {
  layers.push(layer)
  sync()
  return () => {
    const i = layers.lastIndexOf(layer)
    if (i < 0) return
    layers.splice(i, 1)
    sync()
  }
}

export function topModal(): ModalLayer | null {
  return layers.length ? layers[layers.length - 1] : null
}

/** Whether the Dialog with this id is open (its children may render). */
export function isModalShown(id: string): boolean {
  return layers.some((l) => l.id === id)
}

/** Where a toast goes: inside the topmost dialog, else the body. */
export function topPortalRoot(): HTMLElement {
  return topModal()?.portalRoot ?? document.body
}

/** An event the page behind an open dialog must ignore: a dialog is open
 *  and the event did not come from inside the topmost one. */
export function blockedByModal(e: Event): boolean {
  const top = topModal()
  if (!top) return false
  const t = e.target
  return !(t instanceof Node && top.el.contains(t))
}

export function subscribeModalLayers(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
