import {
  useEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type RefObject,
} from 'react'

import { anchorBoxOf, usePopoverPlacement, type AnchorBox } from '../components/ui/popoverPosition'
import { usePortalRoot } from '../components/ui/portalRoot'
import { pushEscHandler } from '../lib/escStack'

const ITEMS = '[role="menuitem"]'

export interface PortalMenuOptions {
  align?: 'left' | 'right'
  prefer?: 'below' | 'above'
  /** The items the keys move through (a disabled one skipped). */
  items?: string
  /** The item that takes the focus on open when one matches (a radio menu's
   * checked row), else the first item. */
  focus?: string
  /** Also close on a window blur (a press inside an iframe never reaches the
   * page), a resize and a scroll that moves the button: for a menu over a
   * live frame, such as the document pane's header. */
  closeOnMove?: boolean
}

export interface PortalMenu {
  open: boolean
  buttonRef: RefObject<HTMLButtonElement | null>
  panelRef: RefObject<HTMLDivElement | null>
  /** The portaled panel's fixed position: hidden until it is measured. */
  style: CSSProperties
  /** Where to portal the panel: the body, or the dialog the menu is in. */
  portalRoot: HTMLElement
  toggle: () => void
  /** Close; `refocus` gives the focus back to the button (a keyboard close). */
  close: (refocus?: boolean) => void
  /** On the panel: arrows, Home and End move between the items, Tab closes. */
  onKeyDown: (e: ReactKeyboardEvent<HTMLElement>) => void
}

/**
 * A menu button with its panel portaled to the body (a toolbar's overflow
 * would clip it), usable by keyboard: the first item takes the focus once
 * the panel is placed, ArrowUp/ArrowDown/Home/End move it, Tab closes and
 * gives it back to the button, and Escape closes the menu alone (its handler
 * sits on top of the escape stack, above the panel it opened from). A press
 * outside closes it. The button carries `aria-haspopup="menu"` and
 * `aria-expanded={open}`; the panel `role="menu"`, its items
 * `role="menuitem"` (`items` names others: a radio menu's
 * `menuitemradio`, as DocumentVersionsMenu's).
 */
export function usePortalMenu(
  { align = 'left', prefer, items = ITEMS, focus, closeOnMove = false }: PortalMenuOptions = {},
): PortalMenu {
  const enabled = `${items}:not([disabled])`
  const [anchor, setAnchor] = useState<AnchorBox | null>(null)
  const open = anchor !== null
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const placement = usePopoverPlacement(anchor, panelRef, { align, prefer })
  const portalRoot = usePortalRoot()

  const close = (refocus = true) => {
    setAnchor(null)
    if (refocus) buttonRef.current?.focus()
  }
  const toggle = () => {
    if (anchor) { close(); return }
    const rect = buttonRef.current?.getBoundingClientRect()
    if (rect) setAnchor(anchorBoxOf(rect))
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!panelRef.current?.contains(t) && !buttonRef.current?.contains(t)) setAnchor(null)
    }
    // Fixed coordinates go stale when anything under them moves; only a
    // scroll that moves the button counts (not the menu's own list, not a
    // chat streaming beside it).
    const onMove = () => setAnchor(null)
    const onScroll = (e: Event) => {
      const btn = buttonRef.current
      if (e.target instanceof Node && btn && !e.target.contains(btn)) return
      setAnchor(null)
    }
    document.addEventListener('mousedown', onDown)
    if (closeOnMove) {
      window.addEventListener('blur', onMove)
      window.addEventListener('resize', onMove)
      window.addEventListener('scroll', onScroll, true)
    }
    const popEsc = pushEscHandler(() => {
      setAnchor(null)
      buttonRef.current?.focus()
    }, () => panelRef.current)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('blur', onMove)
      window.removeEventListener('resize', onMove)
      window.removeEventListener('scroll', onScroll, true)
      popEsc()
    }
  }, [open, closeOnMove])

  useEffect(() => {
    if (!open || !placement) return
    const panel = panelRef.current
    const chosen = focus ? panel?.querySelector<HTMLElement>(focus) : null
    ;(chosen ?? panel?.querySelector<HTMLElement>(enabled))?.focus()
  }, [open, placement, focus, enabled])

  const onKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    const rows = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(enabled) ?? [])
    const at = rows.indexOf(document.activeElement as HTMLElement)
    const move = (i: number) => { e.preventDefault(); rows[(i + rows.length) % rows.length]?.focus() }
    if (e.key === 'ArrowDown') move(at + 1)
    else if (e.key === 'ArrowUp') move(at < 0 ? rows.length - 1 : at - 1)
    else if (e.key === 'Home') move(0)
    else if (e.key === 'End') move(rows.length - 1)
    else if (e.key === 'Tab') { e.preventDefault(); close() }
  }

  const style: CSSProperties = {
    position: 'fixed',
    left: placement?.left ?? anchor?.left ?? 0,
    top: placement?.top ?? anchor?.bottom ?? 0,
    visibility: placement ? 'visible' : 'hidden',
    zIndex: 60,
  }

  return { open, buttonRef, panelRef, style, portalRoot, toggle, close, onKeyDown }
}
