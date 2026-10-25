import { useSyncExternalStore, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { subscribeModalLayers, topPortalRoot } from '../../lib/ui/modalLayers'
import { setToastViewport, subscribeToastViewport, toastViewport } from './toastViewport'

/**
 * The page's one toast region, mounted once
 * in App.tsx and present even when empty: a live region created together
 * with its first toast is often not announced. It sits inside the topmost
 * open dialog while one is open (the top layer would hide it otherwise, and
 * the page behind is inert), else in the body. The toast hosts render their
 * cards into it with `ToastPortal`.
 */
export default function ToastViewport() {
  const root = useSyncExternalStore(subscribeModalLayers, topPortalRoot, topPortalRoot)
  return createPortal(
    <div
      ref={setToastViewport}
      role="region"
      aria-label="Notifications"
      className="pointer-events-none fixed top-14 right-3 left-3 z-50 flex flex-col gap-2 sm:left-auto sm:right-4 sm:w-80 *:pointer-events-auto"
    />,
    root,
  )
}

/** Toast cards, rendered into the region (nothing while it is not mounted). */
export function ToastPortal({ children }: { children: ReactNode }) {
  const el = useSyncExternalStore(subscribeToastViewport, toastViewport, toastViewport)
  return el ? createPortal(children, el) : null
}
