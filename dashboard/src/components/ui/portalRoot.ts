import { createContext, useContext } from 'react'

/**
 * Where a portaled popover (a menu, a tooltip, a full-screen viewer) mounts.
 *
 * An open Dialog is in the browser's top layer and everything outside it is
 * inert and painted under it, so a popover opened from inside a dialog must
 * mount inside that dialog: the Dialog provides an empty element of its own
 * here. Outside any dialog the popover mounts in the body.
 */
export const PortalRootContext = createContext<HTMLElement | null>(null)

export function usePortalRoot(): HTMLElement {
  return useContext(PortalRootContext) ?? document.body
}
