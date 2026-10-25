import {
  Suspense, useCallback, useId, useLayoutEffect, useRef, useState, useSyncExternalStore,
  type KeyboardEvent, type MouseEvent, type PointerEvent, type ReactNode, type RefObject,
} from 'react'
import { createPortal } from 'react-dom'

import { ensureEscListener, hasEscEntryWithin } from '../../lib/escStack'
import { isModalShown, pushModalLayer, subscribeModalLayers } from '../../lib/ui/modalLayers'
import { DIALOG_PANEL } from './classes'
import { captureFocusOrigin, restoreFocus, wrapTab } from './focusReturn'
import { PortalRootContext } from './portalRoot'

/** Where the panel sits: centred, near the top (the long browsers), a sheet
 *  on a phone that centres from `sm` up, or the whole viewport. */
export type DialogPlacement = 'center' | 'top' | 'sheet' | 'fullscreen'
/** What closes it besides its own buttons: Escape and the backdrop, Escape
 *  only (something would be lost to a stray click), or nothing. */
export type DialogDismiss = 'any' | 'escape' | 'none'

const WRAPPER: Record<DialogPlacement, string> = {
  center: 'items-center p-3 sm:p-4',
  top: 'items-start px-3 py-8 sm:px-4',
  sheet: 'items-end p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:items-center sm:p-4',
  fullscreen: 'items-stretch',
}
const PANEL = `${DIALOG_PANEL} max-w-md`
const ON_ESCAPE: Record<DialogDismiss, boolean> = { any: true, escape: true, none: false }
const ON_BACKDROP: Record<DialogDismiss, boolean> = { any: true, escape: false, none: false }

interface Props {
  /** Mounted is open unless this says otherwise. */
  open?: boolean
  /** Escape, the backdrop, the header's Close: the parent decides. */
  onClose: () => void
  dismissible?: DialogDismiss
  /** While a request runs nothing dismisses it. */
  busy?: boolean
  /** The standard header: the title (the dialog's label) and a Close button. */
  title?: ReactNode
  placement?: DialogPlacement
  /** The panel's look; a card of `max-w-md` when omitted. */
  panelClassName?: string
  role?: 'dialog' | 'alertdialog'
  'aria-label'?: string
  'aria-labelledby'?: string
  'aria-describedby'?: string
  'data-testid'?: string
  /** The element to focus once open (else a child's autoFocus, else the dialog). */
  initialFocus?: RefObject<HTMLElement | null>
  /** Where the focus goes on close when the element that opened it is gone. */
  returnFocus?: RefObject<HTMLElement | null>
  children: ReactNode
}

/**
 * A modal dialog on the native <dialog>:
 * `showModal()` puts it in the top layer and makes the page inert; the
 * focus goes back to what opened it; Tab cycles inside it; Escape and the
 * backdrop are close requests the parent answers through `onClose`.
 *
 * The children render only once the dialog is open and registered, so a
 * child's `autoFocus` and its measurements happen in an open dialog; until
 * then the browser has focused the (empty) dialog itself.
 */
export default function Dialog({
  open = true, onClose, dismissible = 'any', busy = false, title, placement = 'center',
  panelClassName, role = 'dialog', initialFocus, returnFocus, children, ...aria
}: Props) {
  const id = useId()
  const titleId = `${id}-title`
  const [dialogEl, setDialogEl] = useState<HTMLDialogElement | null>(null)
  const [rootEl, setRootEl] = useState<HTMLDivElement | null>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const shown = useSyncExternalStore(subscribeModalLayers, () => isModalShown(id), () => false)

  const latest = useRef({ onClose, dismissible, busy, open })
  useLayoutEffect(() => {
    latest.current = { onClose, dismissible, busy, open }
  })
  // Set while this Dialog closes the element itself: a `close` event it did
  // not cause is the browser's, and asks again.
  const closingRef = useRef(false)
  const keyUpSeenRef = useRef(false)
  const pressedBackdropRef = useRef(false)

  const request = useCallback((allowed: Record<DialogDismiss, boolean>) => {
    const now = latest.current
    if (now.busy || !allowed[now.dismissible]) return
    now.onClose()
  }, [])

  useLayoutEffect(() => {
    if (!open || !dialogEl || !rootEl) return
    ensureEscListener()
    const origin = captureFocusOrigin(true)
    closingRef.current = false
    keyUpSeenRef.current = false
    pressedBackdropRef.current = false
    if (!dialogEl.open) dialogEl.showModal()
    const pop = pushModalLayer({ id, el: dialogEl, portalRoot: rootEl, onEscape: () => request(ON_ESCAPE) })
    return () => {
      pop()
      closingRef.current = true
      if (dialogEl.open) dialogEl.close()
      restoreFocus(origin, returnFocus)
    }
  }, [open, dialogEl, rootEl, id, request, returnFocus])

  useLayoutEffect(() => {
    if (shown) initialFocus?.current?.focus()
  }, [shown, initialFocus])

  if (!open) return null

  const onCancel = (e: { preventDefault: () => void }) => {
    e.preventDefault()
    request(ON_ESCAPE)
  }
  const onNativeClose = () => {
    if (!dialogEl || closingRef.current || dialogEl.open || !latest.current.open) return
    // The browser closed it (a close request it would not let the page
    // refuse twice): show it again first, so whatever the request opens
    // stacks above it, then ask.
    dialogEl.showModal()
    request(ON_ESCAPE)
  }
  // A dialog rendered inside this one's React tree (a request form inside a
  // catalog browser) sits in its own <dialog>, yet React bubbles its events
  // through this one: those are not this dialog's to handle.
  const mine = (target: EventTarget) => !!dialogEl && target instanceof Node && dialogEl.contains(target)
  const onKeyDown = (e: KeyboardEvent<HTMLDialogElement>) => {
    if (!dialogEl || !mine(e.target)) return
    wrapTab(e, dialogEl)
    // A key held from before the dialog opened must not press its buttons.
    if (!keyUpSeenRef.current && e.repeat && (e.key === 'Enter' || e.key === ' ')
      && e.target instanceof Element && e.target.matches('button, a[href]')) e.preventDefault()
  }
  const isBackdrop = (target: EventTarget) => target === dialogEl || target === wrapperRef.current
  const onPointerDown = (e: PointerEvent<HTMLDialogElement>) => {
    if (!mine(e.target)) return
    pressedBackdropRef.current = isBackdrop(e.target) && !!dialogEl && !hasEscEntryWithin(dialogEl)
  }
  const onClick = (e: MouseEvent<HTMLDialogElement>) => {
    // A click inside a dialog is the dialog's: React would carry it on
    // through the portal to the row or card that rendered the dialog (a
    // chat row that opens on click). No listener above the body needs it.
    e.stopPropagation()
    if (!isBackdrop(e.target)) return
    const pressed = pressedBackdropRef.current
    pressedBackdropRef.current = false
    if (pressed && e.detail <= 1) request(ON_BACKDROP)
  }
  const keepInside = (e: { stopPropagation: () => void }) => e.stopPropagation()

  return createPortal(
    <dialog
      ref={setDialogEl}
      role={role}
      aria-modal="true"
      aria-label={aria['aria-label']}
      aria-labelledby={title ? titleId : aria['aria-labelledby']}
      aria-describedby={aria['aria-describedby']}
      data-testid={aria['data-testid']}
      className="oto-dialog fixed inset-0 m-0 h-full max-h-none w-full max-w-none overflow-y-auto overscroll-contain bg-transparent p-0 text-primary outline-none"
      onCancel={onCancel}
      onClose={onNativeClose}
      onKeyDown={onKeyDown}
      onKeyUp={(e) => { if (mine(e.target)) keyUpSeenRef.current = true }}
      onPointerDown={onPointerDown}
      onClick={onClick}
      onDoubleClick={keepInside}
      onContextMenu={keepInside}
    >
      <div ref={wrapperRef} className={`flex min-h-full w-full justify-center ${WRAPPER[placement]}`}>
        {shown && (
          <PortalRootContext value={rootEl}>
            <div className={panelClassName ?? PANEL}>
              {title && (
                <div className="flex items-center justify-between gap-3 border-b border-subtle px-4 py-3">
                  <h2 id={titleId} className="min-w-0 truncate font-semibold text-primary">{title}</h2>
                  <button
                    type="button"
                    onClick={onClose}
                    disabled={busy}
                    aria-label="Close"
                    className="shrink-0 rounded-md p-1 text-xl leading-none text-secondary transition-colors hover:text-primary disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
                  >
                    &times;
                  </button>
                </div>
              )}
              <Suspense fallback={null}>{children}</Suspense>
            </div>
          </PortalRootContext>
        )}
      </div>
      <div ref={setRootEl} />
    </dialog>,
    document.body,
  )
}
